#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错误收集 / 通知 / 敏感信息脱敏的统一入口。

公共 API（被 storage.py / wechat_notifier.py / transfer_runner.py 使用）：

- 脱敏：``mask_sensitive``、``mask_cookies``、``mask_share_url``
- 错误收集：``ErrorCollector``、``error_collection``、
  ``start_error_collection``、``collect_error``、
  ``send_collected_errors``、``end_error_collection``
- 错误处理：``handle_error_and_notify``、``print_detailed_error``、
  ``format_error_info``、``send_wechat_alert``
- 结果聚合：``collect_transferred_files``

模块仅依赖标准库；与 ``wechat_notifier`` 之间通过结构化协议交互
（外部传入的 notifier 只需提供 ``send_error_notification`` 方法），
避免双向 import。
"""

from __future__ import annotations

import re
import threading
import traceback
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Pattern, Tuple

# ============================================================================
# 1. 公共常量
# ============================================================================

MASK_REPLACEMENT = "***"


# ============================================================================
# 2. 敏感信息脱敏
# ============================================================================
#
# 设计要点：
# - 所有正则集中预编译，避免重复 re.compile
# - mask_sensitive 一次性遍历一组 (pattern, replacement) 规则，从而把所有
#   敏感字段替换完毕；调用方按需选择 mask_share_url / mask_cookies。

_SHARE_LINK_TOKEN_PATTERN = re.compile(
    r"(https?://pan\.baidu\.com/s/)([A-Za-z0-9_-]+)", re.IGNORECASE
)
_SHARE_SURL_TOKEN_PATTERN = re.compile(r"(\bsurl=)([A-Za-z0-9_-]+)", re.IGNORECASE)
_PWD_PATTERN = re.compile(
    r"(((?<![A-Za-z0-9_])pwd|密码|提取码)\s*[:：=]?\s*)([A-Za-z0-9]{4})",
    re.IGNORECASE,
)
_UK_PATTERN = re.compile(r"(\buk\s*[:=]\s*)(\d+)", re.IGNORECASE)
_SHARE_ID_PATTERN = re.compile(r"(\bshare_?id\s*[:=]\s*)(\d+)", re.IGNORECASE)
_BDSTOKEN_PATTERN = re.compile(r"(\bbdstoken\s*[:=]\s*)([A-Za-z0-9_-]+)", re.IGNORECASE)
_TOKEN_PATTERN = re.compile(
    r"((?:\baccess_token\b|\brefresh_token\b|\btoken\b)\s*[:=]\s*)"
    r"([A-Za-z0-9._~+/=-]{6,})",
    re.IGNORECASE,
)
_AUTHORIZATION_BEARER_PATTERN = re.compile(
    r"(\bAuthorization\s*:\s*Bearer\s+)([A-Za-z0-9._~+/=-]{6,})",
    re.IGNORECASE,
)
_WEBHOOK_KEY_PATTERN = re.compile(r"(\bkey=)([^&\s]+)", re.IGNORECASE)


_COOKIE_KEYS: Tuple[str, ...] = (
    "BDUSS_BFESS",
    "STOKEN_BFESS",
    "BAIDUID_BFESS",
    "BDUSS",
    "STOKEN",
    "BDCLND",
    "BAIDUID",
    "PANWEB",
    "H_PS_PSSID",
    "BDORZ",
    "BDRCVFR",
    "PTOKEN",
    "PANPSC",
    "BA_HECTOR",
    "ZFY",
)

_COOKIE_PATTERNS: Tuple[Pattern[str], ...] = tuple(
    re.compile(
        rf"((?:['\"]?{key}['\"]?)\s*[:=]\s*['\"]?)([^;,'\"\s}}{{\]]+)(['\"]?)",
        re.IGNORECASE,
    )
    for key in _COOKIE_KEYS
)


def _cookie_repl(match: "re.Match[str]") -> str:
    return f"{match.group(1)}{MASK_REPLACEMENT}{match.group(3)}"


# 一组通用脱敏规则：每条都是 (compiled pattern, replacement template)。
# replacement 中使用 \g<1> 引用第一个捕获组，未匹配的捕获组直接被掩码替代。
_GENERAL_MASK_RULES: Tuple[Tuple[Pattern[str], str], ...] = (
    (_SHARE_LINK_TOKEN_PATTERN, rf"\g<1>{MASK_REPLACEMENT}"),
    (_SHARE_SURL_TOKEN_PATTERN, rf"\g<1>{MASK_REPLACEMENT}"),
    (_PWD_PATTERN, rf"\g<1>{MASK_REPLACEMENT}"),
    (_UK_PATTERN, rf"\g<1>{MASK_REPLACEMENT}"),
    (_SHARE_ID_PATTERN, rf"\g<1>{MASK_REPLACEMENT}"),
    (_BDSTOKEN_PATTERN, rf"\g<1>{MASK_REPLACEMENT}"),
    (_TOKEN_PATTERN, rf"\g<1>{MASK_REPLACEMENT}"),
    (_AUTHORIZATION_BEARER_PATTERN, rf"\g<1>{MASK_REPLACEMENT}"),
    (_WEBHOOK_KEY_PATTERN, rf"\g<1>{MASK_REPLACEMENT}"),
)


def mask_cookies(text: Optional[str]) -> Optional[str]:
    """针对常见 Cookie 键的脱敏，仅替换值不破坏原格式。"""
    if text is None:
        return text
    masked = str(text)
    for pattern in _COOKIE_PATTERNS:
        masked = pattern.sub(_cookie_repl, masked)
    return masked


def mask_share_url(text: Optional[str]) -> Optional[str]:
    """掩码百度网盘分享链接 / surl，仅隐藏链接标识。"""
    if text is None:
        return text
    masked = _SHARE_LINK_TOKEN_PATTERN.sub(rf"\g<1>{MASK_REPLACEMENT}", str(text))
    return _SHARE_SURL_TOKEN_PATTERN.sub(rf"\g<1>{MASK_REPLACEMENT}", masked)


def mask_sensitive(text: Optional[str]) -> Optional[str]:
    """对常见敏感字段（cookie / pwd / token / webhook key / 分享链接）统一脱敏。"""
    if text is None:
        return text
    masked = mask_cookies(str(text))
    for pattern, replacement in _GENERAL_MASK_RULES:
        masked = pattern.sub(replacement, masked)
    return masked


# 为兼容历史代码（包括内部调用）保留一个下划线别名
_mask_sensitive = mask_sensitive


# ============================================================================
# 3. 结果聚合工具
# ============================================================================


def collect_transferred_files(result: Optional[Dict[str, Any]]) -> List[str]:
    """从单次或批量转存结果中提取成功转存的文件清单。"""
    if not isinstance(result, dict):
        return []

    if "results" not in result:
        return list(result.get("transferred_files", []))

    transferred_files: List[str] = []
    for item in result["results"]:
        if item.get("success") and not item.get("skipped"):
            transferred_files.extend(item.get("transferred_files", []))
    return transferred_files


# ============================================================================
# 4. 错误信息格式化
# ============================================================================


def _format_error_base(error: BaseException, context: str = "") -> str:
    return (
        f"发生异常: {context}\n"
        f"  错误类型: {type(error).__name__}\n"
        f"  错误信息: {str(error)}\n"
        f"  详细堆栈: {traceback.format_exc()}"
    )


def format_error_info(error: BaseException, context: str = "") -> str:
    """格式化错误信息，自动脱敏。"""
    base = _format_error_base(error, context)
    masked = mask_sensitive(base)
    return masked if masked is not None else base


def _emit_error_log(message: str) -> None:
    """优先使用项目 logger，缺失时回退到 print，确保 GA 日志格式一致。"""
    try:
        from logger import get_logger  # 延迟导入避免循环
    except Exception:
        print(message)
        return

    try:
        get_logger().error(message)
    except Exception:  # pragma: no cover - logger 异常时退化
        print(message)


def print_detailed_error(
    error: BaseException,
    context: str = "",
    wechat_notifier: Any = None,  # 仅为兼容旧签名，不再使用
    config: Optional[Dict[str, Any]] = None,  # 同上
) -> None:
    """打印（脱敏后）的详细错误堆栈，不直接发送通知。"""
    _emit_error_log(format_error_info(error, context))


def send_wechat_alert(
    wechat_notifier: Any,
    error: BaseException,
    context: str = "",
    config: Optional[Dict[str, Any]] = None,
) -> None:
    """显式发送一次企业微信告警（脱敏 + 自动附带 GitHub Actions 信息）。"""
    if wechat_notifier is None:
        return
    try:
        wechat_notifier.send_error_notification(format_error_info(error, context), config)
    except Exception as exc:  # pragma: no cover - 发送失败不再回流给自身
        _emit_error_log(f"发送企业微信告警失败: {type(exc).__name__}: {exc}")


# ============================================================================
# 5. 错误收集（线程局部 + 嵌套栈）
# ============================================================================
#
# 每个线程维护一个收集栈：start 入栈、end 出栈。
# - collect_error 仅写当前栈顶，并按 (type|msg|context) 去重
# - send_collected_errors 只发送当前栈顶，避免内层把外层的错误吞掉
# - ErrorCollector 是结构化的 with 入口

_ErrorRecord = Dict[str, Any]
_CollectionFrame = Dict[str, Any]

_error_collections: Dict[int, List[_CollectionFrame]] = defaultdict(list)
_collection_lock = threading.Lock()


def _new_frame(context: str) -> _CollectionFrame:
    return {"context": context, "errors": [], "seen": set()}


def start_error_collection(context: str = "") -> None:
    """在当前线程压入一层错误收集帧。"""
    thread_id = threading.get_ident()
    with _collection_lock:
        _error_collections[thread_id].append(_new_frame(context))


def end_error_collection() -> None:
    """弹出当前线程栈顶帧；栈空时清理 thread 项。"""
    thread_id = threading.get_ident()
    with _collection_lock:
        stack = _error_collections.get(thread_id)
        if not stack:
            return
        stack.pop()
        if not stack:
            _error_collections.pop(thread_id, None)


def _has_active_collection() -> bool:
    thread_id = threading.get_ident()
    with _collection_lock:
        return bool(_error_collections.get(thread_id))


def collect_error(error: BaseException, context: str = "") -> bool:
    """把一个错误写入当前栈顶帧，返回是否成功收集（去重命中时返回 False）。"""
    thread_id = threading.get_ident()
    with _collection_lock:
        stack = _error_collections.get(thread_id)
        if not stack:
            return False

        etype = type(error).__name__
        emsg = str(error)
        ectx = str(context)
        key = f"{etype}|{emsg}|{ectx}"

        frame = stack[-1]
        seen = frame["seen"]
        if key in seen:
            return False
        seen.add(key)
        frame["errors"].append(
            {
                "type": etype,
                "message": emsg,
                "context": ectx,
                "traceback": traceback.format_exc(),
            }
        )
        return True


def _format_aggregate_message(frame: _CollectionFrame) -> Optional[str]:
    errors: Iterable[_ErrorRecord] = frame.get("errors") or []
    errors = list(errors)
    if not errors:
        return None

    parts = [
        "方法调用过程中发生一系列错误",
        f"主上下文: {frame['context']}",
        "",
    ]
    for index, error_info in enumerate(errors, 1):
        parts.append(f"{index}. {error_info['context']}")
        parts.append(f"   错误类型: {error_info['type']}")
        parts.append(f"   错误信息: {error_info['message']}")
        parts.append(f"   详细堆栈:\n{error_info['traceback']}")
        parts.append("")
    return "\n".join(parts).strip()


def send_collected_errors(wechat_notifier: Any, config: Optional[Dict[str, Any]] = None) -> None:
    """发送当前栈顶帧聚合后的错误（仅发送本层，外层不受影响）。"""
    if wechat_notifier is None:
        return

    thread_id = threading.get_ident()
    with _collection_lock:
        stack = _error_collections.get(thread_id)
        if not stack:
            return
        message = _format_aggregate_message(stack[-1])

    if not message:
        return

    masked = mask_sensitive(message) or message
    try:
        wechat_notifier.send_error_notification(masked, config)
    except Exception as exc:  # pragma: no cover
        _emit_error_log(f"发送聚合错误通知失败: {type(exc).__name__}: {exc}")


def handle_error_and_notify(
    error: BaseException,
    context: str,
    wechat_notifier: Any,
    config: Optional[Dict[str, Any]] = None,
    collect: bool = True,
) -> None:
    """统一的错误处理入口。

    Args:
        error: 待处理的异常
        context: 错误上下文，用于打印与聚合
        wechat_notifier: 企业微信通知器（任意提供 send_error_notification 的对象）
        config: 配置信息，会透传给通知器
        collect: True 表示纳入当前 ErrorCollector，由其 with 退出时聚合发送；
                 False 且当前没有活跃的 collector 时立即发送一次

    保证不会重复告警：如果存在活跃 collector，本函数永远不立即发送。
    """
    if collect:
        collect_error(error, context)

    has_active = _has_active_collection()
    print_detailed_error(error, context)

    if not collect and not has_active:
        send_wechat_alert(wechat_notifier, error, context, config)


# ============================================================================
# 6. ErrorCollector 上下文管理器
# ============================================================================


class ErrorCollector:
    """聚合收集错误并统一发送。

    示例::

        with ErrorCollector("批量转存", notifier, config) as ec:
            try:
                run()
            except Exception as e:
                ec.capture(e, "子步骤说明")

    退出时会发送当前帧聚合后的错误，再弹栈。
    """

    def __init__(
        self,
        context: str = "",
        wechat_notifier: Any = None,
        config: Optional[Dict[str, Any]] = None,
        auto_send: bool = True,
        suppress: bool = False,
    ) -> None:
        self.context = context
        self.wechat_notifier = wechat_notifier
        self.config = config
        self.auto_send = auto_send
        self.suppress = suppress

    def __enter__(self) -> "ErrorCollector":
        start_error_collection(self.context)
        return self

    def capture(self, error: BaseException, context: str = "") -> bool:
        collect_error(error, context)
        return False  # 配合 `return ec.capture(e)` 用法

    def __exit__(
        self,
        exc_type: Optional[type],
        exc: Optional[BaseException],
        tb: Optional[Any],
    ) -> bool:
        if exc is not None:
            collect_error(exc, f"{self.context}（未捕获异常）")
            print_detailed_error(exc, self.context)

        try:
            if self.auto_send:
                send_collected_errors(self.wechat_notifier, self.config)
        finally:
            end_error_collection()

        return bool(self.suppress)


@contextmanager
def error_collection(
    context: str = "",
    wechat_notifier: Any = None,
    config: Optional[Dict[str, Any]] = None,
    auto_send: bool = True,
    suppress: bool = False,
):
    """``ErrorCollector`` 的函数式包装，便于 with 语法使用。"""
    with ErrorCollector(context, wechat_notifier, config, auto_send, suppress) as collector:
        yield collector


# ============================================================================
# 7. 公共导出
# ============================================================================

__all__ = [
    "MASK_REPLACEMENT",
    "ErrorCollector",
    "collect_error",
    "collect_transferred_files",
    "end_error_collection",
    "error_collection",
    "format_error_info",
    "handle_error_and_notify",
    "mask_cookies",
    "mask_sensitive",
    "mask_share_url",
    "print_detailed_error",
    "send_collected_errors",
    "send_wechat_alert",
    "start_error_collection",
]
