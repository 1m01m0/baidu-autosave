#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import functools
import os
import random
import sys
import threading
import time
from pathlib import Path
from threading import Lock
from typing import Optional

_VENDOR_BAIDUPCS_PATH = Path(__file__).resolve().parent / "vendor" / "BaiduPCS-Py"
if _VENDOR_BAIDUPCS_PATH.exists():
    sys.path.insert(0, str(_VENDOR_BAIDUPCS_PATH))

from baidupcs_py.baidupcs import BaiduPCSApi

from env_utils import read_non_negative_int_env, read_positive_int_env
from storage_errors import classify_storage_error
from utils import mask_sensitive

try:
    from logger import get_logger
except ImportError:
    import logging

    def get_logger(name="transfershare"):
        return logging.getLogger(name)


DEFAULT_REQUEST_TIMEOUT = 60
DEFAULT_RETRY_DELAY = 2
DEFAULT_MIN_RETRY_DELAY = 1
MAX_RETRY_DELAY = 30
MAX_RETRIES_GITHUB = 5
MAX_RETRIES_LOCAL = 3

# 哨兵值：表示用户未显式设置 TRANSFERSHARE_PCS_POOL_MAXSIZE。
# `_compute_pool_maxsize` 会据此走 GA / 本地默认决策；
# 显式设置 0 则跳过连接池替换（Requirement 1.6）。
_PCS_POOL_MAXSIZE_UNSET = -1

# HTTPAdapter 连接池容量决策的默认参数；可被环境变量覆盖。
# 详见 design.md "Components and Interfaces" 与 Requirement 6。
DEFAULT_PCS_POOL_MAXSIZE_FLOOR = 32
DEFAULT_PCS_POOL_FANOUT = 4
DEFAULT_PCS_POOL_GA_OFFSET = 8


class BaiduClientAdapter:
    # 类属性 hook：测试可通过 `patch.object(BaiduClientAdapter, "_pcs_factory", ...)`
    # 注入 fake，避免依赖真实 BaiduPCSApi 行为。
    _pcs_factory = staticmethod(BaiduPCSApi)

    # session_pool_info 的初始值；用 classmethod 复制以避免共享可变默认。
    @staticmethod
    def _new_session_pool_info():
        return {
            "pool_maxsize": 0,
            "pool_connections": 0,
            "fanout": 0,
            "patched": False,
        }

    def __init__(self, cookies):
        self._client_lock = Lock()
        self.client = None
        self._quota_info = None
        self.default_timeout = DEFAULT_REQUEST_TIMEOUT
        self.is_github_actions = os.getenv("GITHUB_ACTIONS") == "true"
        self.base_retry_delay = (
            DEFAULT_RETRY_DELAY if self.is_github_actions else DEFAULT_MIN_RETRY_DELAY
        )
        self.max_retries = MAX_RETRIES_GITHUB if self.is_github_actions else MAX_RETRIES_LOCAL
        # 会话并发增强相关状态：在 _apply_session_patches 中填充。
        self._session_cookie_lock: Optional[threading.RLock] = None
        self._session_pool_info = self._new_session_pool_info()
        self._session_patches_applied = False
        self._debug_pool_enabled: Optional[bool] = None
        self._init_client(cookies)

    @property
    def session_pool_info(self) -> dict:
        """会话连接池调优结果的只读快照。

        返回一份字典副本，外部修改不会影响内部状态。在某些测试通过
        ``BaiduClientAdapter.__new__`` 跳过 ``__init__`` 的场景下，
        通过 ``getattr`` 兜底返回默认值。
        """
        info = getattr(self, "_session_pool_info", None)
        if info is None:
            info = self._new_session_pool_info()
        return dict(info)

    def _compute_pool_maxsize(self):
        """决定 HTTPAdapter 的 (pool_maxsize, pool_connections, fanout)。

        决策矩阵（design.md / Requirement 1 + 6）：
        - 显式 ``TRANSFERSHARE_PCS_POOL_MAXSIZE=0`` → 返回 (0, 0, fanout)，
          调用方据此跳过 mount。
        - 显式 ``TRANSFERSHARE_PCS_POOL_MAXSIZE>0`` → ``maxsize=connections=该值``，
          不叠加 GA 偏置。
        - 未显式设置：``base = max(32, MULTI_SHARE_CONCURRENCY * fanout)``；
          GA 环境下再 ``+8`` 抵消 runner 抖动。
        """
        logger = get_logger()

        explicit_maxsize = read_non_negative_int_env(
            "TRANSFERSHARE_PCS_POOL_MAXSIZE", _PCS_POOL_MAXSIZE_UNSET
        )

        # fanout 必须 >=1；非法回退到默认并 WARNING
        raw_fanout = os.getenv("TRANSFERSHARE_PCS_POOL_FANOUT")
        fanout = read_positive_int_env("TRANSFERSHARE_PCS_POOL_FANOUT", DEFAULT_PCS_POOL_FANOUT)
        if raw_fanout is not None and fanout == DEFAULT_PCS_POOL_FANOUT:
            try:
                if int(raw_fanout) >= 1:
                    pass  # 用户给的就是默认值，不报警
                else:
                    logger.warning(
                        "环境变量 TRANSFERSHARE_PCS_POOL_FANOUT 解析失败，使用默认 %s",
                        DEFAULT_PCS_POOL_FANOUT,
                    )
            except (TypeError, ValueError):
                logger.warning(
                    "环境变量 TRANSFERSHARE_PCS_POOL_FANOUT 解析失败，使用默认 %s",
                    DEFAULT_PCS_POOL_FANOUT,
                )

        # 显式禁用：返回 0 让调用方跳过 mount
        if explicit_maxsize == 0:
            return 0, 0, fanout

        multi_concurrency = read_positive_int_env("TRANSFERSHARE_MULTI_SHARE_CONCURRENCY", 1)

        # 显式正数：直接用，不叠加 GA 偏置
        if explicit_maxsize > 0:
            return explicit_maxsize, explicit_maxsize, fanout

        # 未显式：按 GA 决定
        base = max(DEFAULT_PCS_POOL_MAXSIZE_FLOOR, multi_concurrency * fanout)
        if self.is_github_actions:
            base += DEFAULT_PCS_POOL_GA_OFFSET
        return base, base, fanout

    def _inject_timeout(self):
        logger = get_logger()
        pcs_candidate = self._get_pcs_candidate()

        if pcs_candidate:
            if not getattr(pcs_candidate, "_timeout_patched", False):
                self._patch_request_methods(pcs_candidate)
                logger.debug("成功注入超时逻辑到 BaiduPCSApi 请求方法。")
            else:
                logger.debug("超时逻辑已存在，无需重复注入。")
        else:
            logger.warning("未找到可用的 pcs 属性，无法注入超时逻辑。")

    def _patch_session_pool(self, pcs_candidate) -> bool:
        """替换 pcs_candidate._session 上的 HTTPAdapter，扩大连接池容量。

        语义见 design.md / Requirement 1。失败时还原快照、不抛异常。
        返回 True 表示替换成功；False 表示跳过或回滚（含被禁用、不是 Session、
        mount 抛异常）。
        """
        # 延迟到此处再 import，避免在 storage_client 顶部加载 requests 时
        # 影响那些只想用 BaiduClientAdapter 静态方法的测试场景
        import requests
        from requests.adapters import HTTPAdapter

        logger = get_logger()
        session = getattr(pcs_candidate, "_session", None)
        if not isinstance(session, requests.Session):
            logger.warning("pcs_candidate._session 不是 requests.Session 实例，跳过连接池调优")
            return False

        pool_maxsize, pool_connections, fanout = self._compute_pool_maxsize()
        if pool_maxsize == 0:
            logger.debug("TRANSFERSHARE_PCS_POOL_MAXSIZE=0，已禁用连接池调优")
            self._session_pool_info.update(
                {
                    "pool_maxsize": 0,
                    "pool_connections": 0,
                    "fanout": fanout,
                    "patched": False,
                }
            )
            return False

        snapshot = dict(session.adapters)
        try:
            adapter_https = HTTPAdapter(
                pool_connections=pool_connections,
                pool_maxsize=pool_maxsize,
                pool_block=False,
            )
            adapter_http = HTTPAdapter(
                pool_connections=pool_connections,
                pool_maxsize=pool_maxsize,
                pool_block=False,
            )
            session.mount("https://", adapter_https)
            session.mount("http://", adapter_http)
        except Exception as exc:
            # 回滚到 snapshot：保证 adapters 状态完全恢复
            try:
                session.adapters.clear()
                session.adapters.update(snapshot)
            except Exception:  # pragma: no cover - 极端情况下吞掉
                pass
            logger.warning(
                "会话连接池替换失败，已回滚: %s: %s",
                type(exc).__name__,
                exc,
            )
            self._session_pool_info.update(
                {
                    "pool_maxsize": 0,
                    "pool_connections": 0,
                    "fanout": fanout,
                    "patched": False,
                }
            )
            return False

        self._session_pool_info.update(
            {
                "pool_maxsize": pool_maxsize,
                "pool_connections": pool_connections,
                "fanout": fanout,
                "patched": True,
            }
        )
        return True

    def _patch_cookies_update(self, pcs_candidate) -> bool:
        """用 threading.RLock 包裹 pcs_candidate._cookies_update，
        让"`_session.cookies.update` + `_cookies.update`"对外呈现原子语义。
        使用 RLock 而非 Lock 以避免同一线程递归调用时死锁。

        语义见 Requirement 2 / 9.2。失败时还原原方法、不抛异常。
        """
        logger = get_logger()
        original = getattr(pcs_candidate, "_cookies_update", None)
        if original is None:
            logger.debug("pcs_candidate 未暴露 _cookies_update，跳过 cookie 原子化")
            return False

        lock = threading.RLock()

        def patched(cookies, *args, **kwargs):
            with lock:
                return original(cookies, *args, **kwargs)

        try:
            setattr(pcs_candidate, "_cookies_update", patched)
            self._session_cookie_lock = lock
            return True
        except Exception as exc:
            logger.warning(
                "包裹 _cookies_update 失败，已回滚: %s: %s",
                type(exc).__name__,
                exc,
            )
            self._session_cookie_lock = None
            try:
                setattr(pcs_candidate, "_cookies_update", original)
            except Exception:  # pragma: no cover
                pass
            return False

    def _apply_session_patches(self, pcs_candidate) -> None:
        """在 pcs_candidate 上一次性应用所有会话并发增强 patch。

        幂等：通过 `pcs_candidate._session_concurrency_patched` 标记防止重复挂载。
        任意子 patch 失败都不会抛异常给调用方（保护 _init_client 的 Property 1）。
        """
        logger = get_logger()
        if getattr(pcs_candidate, "_session_concurrency_patched", False):
            logger.debug("会话并发 patch 已存在，跳过本次应用")
            return

        try:
            pool_ok = self._patch_session_pool(pcs_candidate)
        except Exception as exc:  # pragma: no cover - _patch_session_pool 内部已兜底
            logger.warning(
                "会话连接池 patch 抛出未预期异常: %s: %s",
                type(exc).__name__,
                exc,
            )
            pool_ok = False

        try:
            cookie_ok = self._patch_cookies_update(pcs_candidate)
        except Exception as exc:  # pragma: no cover - _patch_cookies_update 内部已兜底
            logger.warning(
                "Cookie patch 抛出未预期异常: %s: %s",
                type(exc).__name__,
                exc,
            )
            cookie_ok = False

        if pool_ok or cookie_ok:
            try:
                pcs_candidate._session_concurrency_patched = True
            except Exception:  # pragma: no cover - 极少见的只读 pcs
                pass
            self._session_patches_applied = True

        ga_flag = "true" if self.is_github_actions else "false"
        info = self._session_pool_info
        logger.info(
            "会话并发 patch 已启用: pool_maxsize=%s pool_connections=%s "
            "fanout=%s ga_environment=%s patched_cookies_update=%s",
            info.get("pool_maxsize", 0),
            info.get("pool_connections", 0),
            info.get("fanout", 0),
            ga_flag,
            "true" if cookie_ok else "false",
        )

    def _get_pcs_candidate(self):
        logger = get_logger()
        for attr in ("_pcs", "pcs", "baidupcs", "_baidupcs"):
            if hasattr(self.client, attr):
                logger.debug(f"找到 pcs 属性：{attr}")
                return getattr(self.client, attr)
        logger.debug("未找到任何有效的 pcs 属性。")
        return None

    def _patch_request_methods(self, pcs_candidate):
        def _wrap_timeout(fn):
            @functools.wraps(fn)
            def _wrapped(*args, **kwargs):
                if "timeout" not in kwargs or kwargs.get("timeout") is None:
                    kwargs["timeout"] = self.default_timeout
                return fn(*args, **kwargs)

            return _wrapped

        logger = get_logger()
        request_methods = [
            "_requestf",
            "_request_get",
            "_request_post",
            "request",
            "_request",
        ]
        for method_name in request_methods:
            if hasattr(pcs_candidate, method_name):
                logger.debug(f"为方法 {method_name} 注入超时逻辑。")
                setattr(
                    pcs_candidate,
                    method_name,
                    _wrap_timeout(getattr(pcs_candidate, method_name)),
                )

        setattr(pcs_candidate, "_timeout_patched", True)
        logger.debug("成功注入超时设置并标记 '_timeout_patched'。")

    def _maybe_log_pool_state(self):
        """可选：当 ``TRANSFERSHARE_PCS_DEBUG_POOL=1`` 时打印连接池状态。

        仅在 patch 已生效时执行；urllib3 不暴露 pools 时静默跳过。
        热路径：本方法每次 call_with_retry 成功返回前调用，必须低开销。
        """
        if not getattr(self, "_session_patches_applied", False):
            return
        # 缓存 debug 开关判断，避免每次都读环境变量
        debug_enabled = self._debug_pool_enabled
        if debug_enabled is None:
            debug_enabled = read_positive_int_env("TRANSFERSHARE_PCS_DEBUG_POOL", 0) >= 1
            self._debug_pool_enabled = debug_enabled
        if not debug_enabled:
            return
        try:
            pcs_candidate = self._get_pcs_candidate()
            if pcs_candidate is None:
                return
            session = getattr(pcs_candidate, "_session", None)
            if session is None:
                return
            adapter = session.adapters.get("https://")
            pools = getattr(getattr(adapter, "poolmanager", None), "pools", None)
            num_pools = len(pools) if pools is not None else -1
            get_logger().debug("会话池状态: scheme=https:// num_pools=%s", num_pools)
        except Exception:  # pragma: no cover - 诊断日志失败不应抛
            pass

    def call_with_retry(self, func, *args, suppress_retry_abort=True, **kwargs):
        last_error = None
        logger = get_logger()

        for attempt in range(self.max_retries):
            try:
                if attempt > 0:
                    if self.is_github_actions:
                        delay = self.base_retry_delay * (2 ** (attempt - 1)) + random.uniform(
                            0, 1.5
                        )
                    else:
                        delay = self.base_retry_delay * attempt
                    delay = min(delay, MAX_RETRY_DELAY)
                    logger.debug(f"第{attempt + 1}次重试，等待{delay:.1f}秒...")
                    time.sleep(delay)

                result = func(*args, **kwargs)
                # 成功返回前，可选地打印池状态（用于排查并发瓶颈）
                self._maybe_log_pool_state()
                return result
            except Exception as exc:
                last_error = exc
                error_info = classify_storage_error(exc)
                if error_info.retryable:
                    if attempt < self.max_retries - 1:
                        safe_raw_message = (
                            mask_sensitive(error_info.raw_message) or error_info.message
                        )
                        logger.debug(f"可重试请求失败（第{attempt + 1}次尝试）: {safe_raw_message}")
                        continue
                    logger.warning(f"可重试请求最终失败，已重试{self.max_retries}次")
                    break
                if error_info.kind == "retry_abort":
                    if suppress_retry_abort:
                        last_error = None
                        break
                    raise
                raise

        if last_error is not None:
            raise last_error
        return None

    def _init_client(self, cookies):
        with self._client_lock:
            cookies_dict = self.parse_cookies(cookies)
            if not self.validate_cookies(cookies_dict):
                raise ValueError("Cookies 验证失败，缺少 BDUSS 或 STOKEN")

            for retry in range(3):
                try:
                    self.client = type(self)._pcs_factory(cookies=cookies_dict)
                    self.default_timeout = int(
                        os.getenv("BAIDU_REQUEST_TIMEOUT", str(DEFAULT_REQUEST_TIMEOUT))
                    )
                    self._inject_timeout()
                    pcs_candidate = self._get_pcs_candidate()
                    if pcs_candidate is not None:
                        self._apply_session_patches(pcs_candidate)
                    self._quota_info = self.client.quota()
                    return True
                except Exception as exc:
                    if retry < 2:
                        time.sleep(3)
                    else:
                        raise ValueError(f"百度网盘客户端初始化失败: {str(exc)}") from exc

    @staticmethod
    def validate_cookies(cookies):
        try:
            required_cookies = ["BDUSS", "STOKEN"]
            missing = [c for c in required_cookies if c not in cookies]
            if missing:
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def parse_cookies(cookies_str):
        cookies = {}
        if not cookies_str:
            return cookies

        items = cookies_str.split(";")
        for item in items:
            if not item.strip() or "=" not in item:
                continue
            key, value = item.split("=", 1)
            cookies[key.strip()] = value.strip()
        return cookies

    def quota(self, refresh=False):
        if not refresh and self._quota_info is not None:
            return self._quota_info
        self._quota_info = self.client.quota()
        return self._quota_info

    def list(self, path):
        return self.call_with_retry(self.client.list, path, suppress_retry_abort=False)

    def makedir(self, path):
        return self.call_with_retry(self.client.makedir, path, suppress_retry_abort=False)

    def rename(self, source, target):
        return self.call_with_retry(self.client.rename, source, target, suppress_retry_abort=False)

    def access_shared(self, share_url, pwd=None):
        return self.call_with_retry(self.client.access_shared, share_url, pwd)

    def shared_paths(self, **kwargs):
        return self.call_with_retry(self.client.shared_paths, **kwargs)

    def list_shared_paths(self, *args, **kwargs):
        return self.call_with_retry(self.client.list_shared_paths, *args, **kwargs)

    def transfer_shared_paths(self, **kwargs):
        return self.client.transfer_shared_paths(**kwargs)
