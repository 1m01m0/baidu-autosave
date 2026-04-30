#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import posixpath

from storage_errors import (
    classify_storage_error,
    is_already_exists_error,
    is_invalid_name_error,
)
from utils import handle_error_and_notify


class StoragePathService:
    def __init__(self, client, wechat_notifier=None, local_files_cache=None):
        self.client = client
        self.wechat_notifier = wechat_notifier
        self._local_files_cache = local_files_cache if local_files_cache is not None else {}
        self._ensured_dirs = set()

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

            parts = [p for p in path.strip("/").split("/") if p]
            prefixes = []
            curr = ""
            for part in parts:
                curr = f"{curr}/{part}" if curr else f"/{part}"
                prefixes.append(curr)

            for seg in prefixes:
                if seg in self._ensured_dirs:
                    continue
                try:
                    self.client.makedir(seg)
                    self._ensured_dirs.add(seg)
                except Exception as exc:
                    if is_already_exists_error(exc):
                        self._ensured_dirs.add(seg)
                        continue
                    if is_invalid_name_error(exc):
                        handle_error_and_notify(
                            ValueError(f"创建目录失败，文件名非法: {seg}"),
                            "创建目录失败: 文件名非法",
                            self.wechat_notifier,
                            None,
                            collect=True,
                        )
                        return False
                    try:
                        self.client.list(seg)
                        self._ensured_dirs.add(seg)
                        continue
                    except Exception:
                        handle_error_and_notify(
                            exc,
                            f"创建目录出错\n目录路径: {seg}",
                            self.wechat_notifier,
                            None,
                            collect=True,
                        )
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

    def list_local_files_in_dirs(
        self, dir_path, relative_dirs, use_cache=False, merge_dirs=False
    ):
        normalized_dir_path = self.normalize_path(dir_path)
        normalized_relative_dirs = {
            self._normalize_relative_dir(relative_dir) for relative_dir in (relative_dirs or {""})
        }
        cache_key = (
            normalized_dir_path,
            tuple(sorted(normalized_relative_dirs)),
            bool(merge_dirs),
        )
        if use_cache and cache_key in self._local_files_cache:
            return [dict(item) for item in self._local_files_cache[cache_key]]

        try:
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
            scan_plan = self._build_local_scan_plan(
                normalized_relative_dirs, merge_dirs=merge_dirs
            )

            def _to_relative_path(item_path):
                item_path = item_path.replace("\\", "/")
                if item_path.startswith(base):
                    return item_path[len(base) :]
                return item_path.lstrip("/")

            def _append_file(item):
                item_path = getattr(item, "path", "").replace("\\", "/")
                if merge_dirs and not item_path.startswith(base):
                    return
                relative_path = _to_relative_path(item_path)
                if merge_dirs:
                    parent_dir = self._normalize_relative_dir(posixpath.dirname(relative_path))
                    if parent_dir not in normalized_relative_dirs:
                        return
                files.append(
                    {
                        "relative_path": relative_path,
                        "file_name": os.path.basename(item_path),
                        "md5": getattr(item, "md5", None),
                    }
                )

            def _should_descend(relative_path):
                relative_path = self._normalize_relative_dir(relative_path)
                return any(
                    candidate == relative_path or candidate.startswith(f"{relative_path}/")
                    for candidate in normalized_relative_dirs
                    if candidate
                )

            def _list_dir(scan_path, recursive=False):
                try:
                    content = self.client.list(scan_path)
                except Exception as exc:
                    error_info = classify_storage_error(exc)
                    if error_info.kind == "missing_path" or error_info.code == "31023":
                        return
                    handle_error_and_notify(
                        exc,
                        f"列出目录内容时发生错误\n目录路径: {scan_path}",
                        self.wechat_notifier,
                        None,
                        collect=False,
                    )
                    raise

                for item in content:
                    if item.is_file:
                        _append_file(item)
                    elif recursive and item.is_dir:
                        item_path = getattr(item, "path", "").replace("\\", "/")
                        relative_path = _to_relative_path(item_path)
                        if _should_descend(relative_path):
                            _list_dir(item.path, recursive=True)

            for relative_dir, recursive in sorted(scan_plan.items()):
                scan_path = normalized_dir_path.rstrip("/") or "/"
                if relative_dir:
                    scan_path = f"{scan_path.rstrip('/')}/{relative_dir}"
                _list_dir(scan_path, recursive=recursive)

            if use_cache:
                self._local_files_cache[cache_key] = [dict(item) for item in files]
            return files
        except Exception as exc:
            handle_error_and_notify(
                exc,
                f"获取本地文件列表时发生异常\n目录路径: {dir_path}",
                self.wechat_notifier,
                None,
                collect=False,
            )
            return []

    def list_local_files(self, dir_path, use_cache=False):
        normalized_dir_path = self.normalize_path(dir_path)
        if use_cache and normalized_dir_path in self._local_files_cache:
            return [dict(item) for item in self._local_files_cache[normalized_dir_path]]

        try:
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

            def _list_dir(path):
                try:
                    content = self.client.list(path)
                    for item in content:
                        if item.is_file:
                            item_path = getattr(item, "path", "").replace("\\", "/")
                            if item_path.startswith(base):
                                relative_path = item_path[len(base) :]
                            else:
                                relative_path = item_path.lstrip("/")
                            files.append(
                                {
                                    "relative_path": relative_path,
                                    "file_name": os.path.basename(item_path),
                                    "md5": getattr(item, "md5", None),
                                }
                            )
                        elif item.is_dir:
                            _list_dir(item.path)
                except Exception as exc:
                    error_info = classify_storage_error(exc)
                    if path == normalized_dir_path and (
                        error_info.kind == "missing_path" or error_info.code == "31023"
                    ):
                        return
                    handle_error_and_notify(
                        exc,
                        f"列出目录内容时发生错误\n目录路径: {path}",
                        self.wechat_notifier,
                        None,
                        collect=False,
                    )
                    raise

            _list_dir(normalized_dir_path)
            if use_cache:
                self._local_files_cache[normalized_dir_path] = [dict(item) for item in files]
            return files
        except Exception as exc:
            handle_error_and_notify(
                exc,
                f"获取本地文件列表时发生异常\n目录路径: {dir_path}",
                self.wechat_notifier,
                None,
                collect=False,
            )
            return []

