#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import posixpath
import threading
import time

from env_utils import read_positive_int_env
from storage_metrics import emit_storage_metric
from storage_errors import (
    classify_storage_error,
    is_already_exists_error,
    is_invalid_name_error,
)
from utils import handle_error_and_notify


DIR_OPERATION_MAX_ATTEMPTS = 3
DIR_OPERATION_RETRY_DELAY = 1
# list_local_files_in_dirs 的顶层并发度：当 scan_plan 包含多个独立子目录时，
# 用 ThreadPoolExecutor 同时扫多个子目录。默认 1（串行，与历史行为一致）。
# 注意：百度对 list 接口有限频，调高后命中限频会自动通过 call_with_retry 退避，
# 但实际加速比依赖于扫描目录数 / 限频阈值的关系，建议从 2~4 起步实测。
LOCAL_SCAN_CONCURRENCY = read_positive_int_env("TRANSFERSHARE_LOCAL_SCAN_CONCURRENCY", 1)


class StoragePathService:
    def __init__(self, client, wechat_notifier=None, local_files_cache=None):
        self.client = client
        self.wechat_notifier = wechat_notifier
        self._local_files_cache = local_files_cache if local_files_cache is not None else {}
        self._ensured_dirs = set()
        # 由 BaiduStorage 在创建时注入；多链接并发下用来保护 _local_files_cache 的
        # 读写一致性。默认是一个本地 RLock，单实例使用时也安全。
        self.shared_state_lock = threading.RLock()

    @staticmethod
    def normalize_path(path, file_only=False):
        try:
            path = path.replace("\\", "/").strip("/")
            if file_only:
                return path.split("/")[-1]
            if not path.startswith("/"):
                path = "/" + path
            return path
        except Exception:
            return path

    @staticmethod
    def _is_transient_dir_error(exc):
        error_info = classify_storage_error(exc)
        raw_message = error_info.raw_message.lower()
        error_type = type(exc).__name__.lower()
        return (
            error_info.kind == "network"
            or "jsondecodeerror" in error_type
            or "expecting value" in raw_message
        )

    def _list_dir_exists(self, path):
        try:
            self.client.list(path)
            return True, None
        except Exception as exc:
            error_info = classify_storage_error(exc)
            if error_info.kind == "missing_path" or error_info.code == "31023":
                return False, exc
            return None, exc

    def _ensure_single_dir_exists(self, path):
        last_error = None
        for attempt in range(DIR_OPERATION_MAX_ATTEMPTS):
            try:
                self.client.makedir(path)
                self._ensured_dirs.add(path)
                return True
            except Exception as exc:
                if is_already_exists_error(exc):
                    self._ensured_dirs.add(path)
                    return True
                if is_invalid_name_error(exc):
                    handle_error_and_notify(
                        ValueError(f"创建目录失败，文件名非法: {path}"),
                        "创建目录失败: 文件名非法",
                        self.wechat_notifier,
                        None,
                        collect=True,
                    )
                    return False

                last_error = exc
                exists, list_error = self._list_dir_exists(path)
                if exists:
                    self._ensured_dirs.add(path)
                    return True
                if list_error is not None and exists is None:
                    last_error = list_error

                should_retry = self._is_transient_dir_error(exc) or (
                    list_error is not None and self._is_transient_dir_error(list_error)
                )
                if should_retry and attempt < DIR_OPERATION_MAX_ATTEMPTS - 1:
                    time.sleep(DIR_OPERATION_RETRY_DELAY)
                    continue
                break

        handle_error_and_notify(
            last_error,
            f"创建目录出错\n目录路径: {path}",
            self.wechat_notifier,
            None,
            collect=True,
        )
        return False

    def ensure_dir_exists(self, path):
        try:
            if not self.client:
                handle_error_and_notify(
                    ValueError("客户端未初始化或初始化失败"),
                    "创建目录失败: 客户端不可用",
                    self.wechat_notifier,
                    None,
                    collect=True,
                )
                return False

            path = self.normalize_path(path)
            if path in ("", "/"):
                return True

            # 完整路径已确认存在时直接返回，跳过逐级前缀检查；
            # 批量目录场景（多个分享落到同一深路径）下减少了字符串处理与 set 查找。
            if path in self._ensured_dirs:
                return True

            parts = [p for p in path.strip("/").split("/") if p]
            prefixes = []
            curr = ""
            for part in parts:
                curr = f"{curr}/{part}" if curr else f"/{part}"
                prefixes.append(curr)

            for seg in prefixes:
                if seg in self._ensured_dirs:
                    continue
                if not self._ensure_single_dir_exists(seg):
                    return False

            return True
        except Exception as exc:
            handle_error_and_notify(
                exc,
                f"确保目录存在时发生异常\n目录路径: {path}",
                self.wechat_notifier,
                None,
                collect=True,
            )
            return False

    @staticmethod
    def _normalize_relative_dir(relative_dir):
        relative_dir = str(relative_dir or "").replace("\\", "/").strip("/")
        return "" if relative_dir == "." else relative_dir

    @staticmethod
    def _is_same_or_child_path(path, parent):
        if not parent:
            return bool(path)
        return path == parent or path.startswith(f"{parent}/")

    @staticmethod
    def _relative_item_path(item_path, base):
        item_path = item_path.replace("\\", "/")
        if item_path.startswith(base):
            return item_path[len(base) :]
        return item_path.lstrip("/")

    def _iter_listed_dir(self, current_path, missing_ok=False):
        try:
            return iter(self.client.list(current_path))
        except Exception as exc:
            error_info = classify_storage_error(exc)
            if missing_ok and (error_info.kind == "missing_path" or error_info.code == "31023"):
                return None
            handle_error_and_notify(
                exc,
                f"列出目录内容时发生错误\n目录路径: {current_path}",
                self.wechat_notifier,
                None,
                collect=False,
            )
            raise

    def _iter_local_tree_items(self, root_path, missing_ok=False, should_descend=None):
        def is_missing_ok(current_path):
            return missing_ok(current_path) if callable(missing_ok) else missing_ok

        item_iter = self._iter_listed_dir(root_path, missing_ok=is_missing_ok(root_path))
        if item_iter is None:
            return
        stack = [(root_path, item_iter)]
        while stack:
            _, item_iter = stack[-1]
            try:
                item = next(item_iter)
            except StopIteration:
                stack.pop()
                continue

            yield item
            if item.is_dir and (should_descend is None or should_descend(item)):
                child_iter = self._iter_listed_dir(item.path, missing_ok=is_missing_ok(item.path))
                if child_iter is not None:
                    stack.append((item.path, child_iter))

    @classmethod
    def _build_local_scan_plan(cls, relative_dirs, merge_dirs=False):
        scan_plan = {relative_dir: False for relative_dir in relative_dirs}
        if not merge_dirs:
            return scan_plan

        children_by_parent = {}
        for relative_dir in relative_dirs:
            if not relative_dir:
                continue
            parent_dir = cls._normalize_relative_dir(posixpath.dirname(relative_dir))
            if not parent_dir:
                continue
            children_by_parent.setdefault(parent_dir, set()).add(relative_dir)

        for parent_dir, children in children_by_parent.items():
            if len(children) < 2:
                continue
            for child_dir in children:
                scan_plan.pop(child_dir, None)
            scan_plan[parent_dir] = True

        recursive_roots = sorted(
            [path for path, recursive in scan_plan.items() if recursive], key=len
        )
        for root in recursive_roots:
            for path in list(scan_plan):
                if path != root and cls._is_same_or_child_path(path, root):
                    scan_plan.pop(path, None)
        return scan_plan

    def list_local_files_in_dirs(self, dir_path, relative_dirs, use_cache=False, merge_dirs=False):
        normalized_dir_path = self.normalize_path(dir_path)
        normalized_relative_dirs = {
            self._normalize_relative_dir(relative_dir) for relative_dir in (relative_dirs or {""})
        }
        cache_key = (
            normalized_dir_path,
            tuple(sorted(normalized_relative_dirs)),
            bool(merge_dirs),
        )
        if use_cache:
            with self.shared_state_lock:
                if cache_key in self._local_files_cache:
                    return [dict(item) for item in self._local_files_cache[cache_key]]

        if not self.client:
            handle_error_and_notify(
                ValueError("客户端未初始化或初始化失败"),
                "获取本地文件列表失败: 客户端不可用",
                self.wechat_notifier,
                None,
                collect=False,
            )
            return []

        base = normalized_dir_path.replace("\\", "/")
        if not base.endswith("/"):
            base += "/"
        scan_plan = self._build_local_scan_plan(normalized_relative_dirs, merge_dirs=merge_dirs)

        sorted_plan = sorted(scan_plan.items())
        start_time = time.monotonic()
        files = self._collect_local_scan_results(
            normalized_dir_path,
            base,
            normalized_relative_dirs,
            merge_dirs,
            sorted_plan,
        )
        elapsed_ms = (time.monotonic() - start_time) * 1000
        emit_storage_metric(
            "local_dir_list",
            dir_path=normalized_dir_path,
            relative_dirs=sorted(normalized_relative_dirs),
            merge_dirs=bool(merge_dirs),
            scanned=len(sorted_plan),
            files=len(files),
            elapsed_ms=round(elapsed_ms, 3),
        )

        if use_cache:
            with self.shared_state_lock:
                self._local_files_cache[cache_key] = [dict(item) for item in files]
        return files

    def _scan_single_relative_dir(
        self,
        normalized_dir_path,
        base,
        normalized_relative_dirs,
        merge_dirs,
        relative_dir,
        recursive,
    ):
        """扫描 scan_plan 中单个 (relative_dir, recursive) 项，返回该项的文件列表。

        作为 ``list_local_files_in_dirs`` 的并发单元抽出来；不依赖外层 closure，
        因此可以安全地在 ThreadPoolExecutor 中并行调用。
        """

        def _should_descend(relative_path):
            relative_path = self._normalize_relative_dir(relative_path)
            return any(
                candidate == relative_path or candidate.startswith(f"{relative_path}/")
                for candidate in normalized_relative_dirs
                if candidate
            )

        scan_path = normalized_dir_path.rstrip("/") or "/"
        if relative_dir:
            scan_path = f"{scan_path.rstrip('/')}/{relative_dir}"

        def should_descend(item):
            if not recursive:
                return False
            item_path = getattr(item, "path", "").replace("\\", "/")
            return _should_descend(self._relative_item_path(item_path, base))

        local_files = []
        for item in self._iter_local_tree_items(
            scan_path, missing_ok=True, should_descend=should_descend
        ):
            if not item.is_file:
                continue
            item_path = getattr(item, "path", "").replace("\\", "/")
            if merge_dirs and not item_path.startswith(base):
                continue
            relative_path = self._relative_item_path(item_path, base)
            if merge_dirs:
                parent_dir = self._normalize_relative_dir(posixpath.dirname(relative_path))
                if parent_dir not in normalized_relative_dirs:
                    continue
            local_files.append(
                {
                    "relative_path": relative_path,
                    "file_name": os.path.basename(item_path),
                    "md5": getattr(item, "md5", None),
                }
            )
        return local_files

    def _collect_local_scan_results(
        self,
        normalized_dir_path,
        base,
        normalized_relative_dirs,
        merge_dirs,
        sorted_plan,
    ):
        """串行或并发地执行 sorted_plan，按 plan 顺序合并结果。"""
        if not sorted_plan:
            return []

        concurrency = max(1, min(LOCAL_SCAN_CONCURRENCY, len(sorted_plan)))
        if concurrency == 1:
            files = []
            for relative_dir, recursive in sorted_plan:
                files.extend(
                    self._scan_single_relative_dir(
                        normalized_dir_path,
                        base,
                        normalized_relative_dirs,
                        merge_dirs,
                        relative_dir,
                        recursive,
                    )
                )
            return files

        # 并发路径：每个 plan 项产生一个 future，结果按 plan 顺序合并以保证
        # files 的相对顺序与串行实现一致。_iter_listed_dir 已在内部走 call_with_retry
        # 处理限频；并发只是把多个独立子目录的网络等待重叠起来。
        from concurrent.futures import ThreadPoolExecutor

        results = [None] * len(sorted_plan)
        with ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="transfershare-local-scan"
        ) as executor:
            future_index = {}
            for index, (relative_dir, recursive) in enumerate(sorted_plan):
                future = executor.submit(
                    self._scan_single_relative_dir,
                    normalized_dir_path,
                    base,
                    normalized_relative_dirs,
                    merge_dirs,
                    relative_dir,
                    recursive,
                )
                future_index[future] = index
            for future in future_index:
                index = future_index[future]
                # future.result() 会把 worker 内的异常 re-raise；
                # 让外层 try/except 统一处理（与串行路径一致）
                results[index] = future.result()

        files = []
        for chunk in results:
            if chunk:
                files.extend(chunk)
        return files

    def list_local_files(self, dir_path, use_cache=False):
        normalized_dir_path = self.normalize_path(dir_path)
        if use_cache:
            with self.shared_state_lock:
                if normalized_dir_path in self._local_files_cache:
                    return [dict(item) for item in self._local_files_cache[normalized_dir_path]]

        if not self.client:
            handle_error_and_notify(
                ValueError("客户端未初始化或初始化失败"),
                "获取本地文件列表失败: 客户端不可用",
                self.wechat_notifier,
                None,
                collect=False,
            )
            return []

        files = []
        base = normalized_dir_path.replace("\\", "/")
        if not base.endswith("/"):
            base += "/"

        for item in self._iter_local_tree_items(
            normalized_dir_path,
            missing_ok=lambda current_path: current_path == normalized_dir_path,
        ):
            if item.is_file:
                item_path = getattr(item, "path", "").replace("\\", "/")
                files.append(
                    {
                        "relative_path": self._relative_item_path(item_path, base),
                        "file_name": os.path.basename(item_path),
                        "md5": getattr(item, "md5", None),
                    }
                )

        if use_cache:
            with self.shared_state_lock:
                self._local_files_cache[normalized_dir_path] = [dict(item) for item in files]
        return files
