#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os

from env_utils import read_positive_int_env
from storage_metrics import emit_storage_metric
from storage_rules import extract_file_info, should_exclude_folder, should_include_folder
from utils import handle_error_and_notify


# 百度分享目录列表分页 size。官方接口实测可支持到 ~1000。
# 默认保持 100 兼容现有测试断言；用户可通过环境变量调高来减少多页扫描的网络往返。
_DEFAULT_SHARED_DIR_PAGE_SIZE = read_positive_int_env("TRANSFERSHARE_SHARED_PAGE_SIZE", 100)
# 大页短页探测开关。当用户主动调大 page_size 后，若服务端静默截断（例如
# 请求 size=1000 但只返回 100），默认的 "len < page_size 即停止" 会导致漏扫。
# 设为 1 后，遇到非空短页继续请求下一页直到出现空页；默认 0 关闭保持旧行为。
_DEFAULT_SHARED_DIR_PAGE_PROBE_ON_SHORT = (
    read_positive_int_env("TRANSFERSHARE_SHARED_PAGE_PROBE_ON_SHORT", 0) >= 1
)


class SharedPathService:
    SHARED_DIR_PAGE_SIZE = _DEFAULT_SHARED_DIR_PAGE_SIZE
    SHARED_DIR_PAGE_PROBE_ON_SHORT = _DEFAULT_SHARED_DIR_PAGE_PROBE_ON_SHORT
    PROGRESS_DIR_INTERVAL = 20
    PROGRESS_PAGE_INTERVAL = 20
    PROGRESS_FILE_INTERVAL = 500

    def __init__(self, client, wechat_notifier=None):
        self.client = client
        self.wechat_notifier = wechat_notifier

    def load_shared_paths(self, share_url, pwd=None):
        self.client.access_shared(share_url, pwd)
        return self.client.shared_paths(shared_url=share_url)

    @staticmethod
    def _resolve_shared_root(shared_paths):
        root_candidates = []
        for shared_path in shared_paths or []:
            raw_path = getattr(shared_path, "path", "")
            if not raw_path:
                continue

            normalized_path = str(raw_path).rstrip("/") or str(raw_path).strip()
            if not normalized_path or normalized_path == "/":
                continue

            parent_path = os.path.dirname(normalized_path)
            if parent_path and parent_path != "/":
                root_candidates.append(parent_path)
            elif getattr(shared_path, "is_dir", False):
                root_candidates.append(normalized_path)

        if not root_candidates:
            return ""

        try:
            shared_root = os.path.commonpath(root_candidates)
        except ValueError:
            return ""

        if shared_root in ("", "/"):
            return ""
        return shared_root.rstrip("/")

    @staticmethod
    def _trim_shared_root(shared_file_path, shared_root=""):
        if not shared_file_path:
            return ""

        normalized_path = str(shared_file_path).strip()
        if not normalized_path:
            return ""

        normalized_root = str(shared_root or "").rstrip("/")
        if normalized_root:
            root_prefix = f"{normalized_root}/"
            if normalized_path == normalized_root:
                return ""
            if normalized_path.startswith(root_prefix):
                return normalized_path[len(root_prefix) :].lstrip("/")

        return normalized_path.lstrip("/")

    @classmethod
    def _new_scan_stats(cls):
        return {
            "dirs": 0,
            "files": 0,
            "pages": 0,
            "skipped_dirs": 0,
            "last_report_dirs": 0,
            "last_report_files": 0,
            "last_report_pages": 0,
        }

    @classmethod
    def _should_report_progress(cls, stats):
        return (
            stats["dirs"] - stats["last_report_dirs"] >= cls.PROGRESS_DIR_INTERVAL
            or stats["files"] - stats["last_report_files"] >= cls.PROGRESS_FILE_INTERVAL
            or stats["pages"] - stats["last_report_pages"] >= cls.PROGRESS_PAGE_INTERVAL
        )

    @classmethod
    def _report_scan_progress(cls, progress_callback, stats):
        if not progress_callback:
            return
        progress_callback(
            "info",
            "共享文件扫描进度："
            f"已扫描 {stats['dirs']} 个目录 / {stats['pages']} 页，"
            f"发现 {stats['files']} 个文件，跳过 {stats['skipped_dirs']} 个目录",
        )
        stats["last_report_dirs"] = stats["dirs"]
        stats["last_report_files"] = stats["files"]
        stats["last_report_pages"] = stats["pages"]

    @classmethod
    def _normalize_shared_file_info(cls, shared_file, shared_root=""):
        if hasattr(shared_file, "_asdict"):
            shared_file_dict = shared_file._asdict()
        elif isinstance(shared_file, dict):
            shared_file_dict = dict(shared_file)
        else:
            shared_file_dict = {
                "server_filename": os.path.basename(getattr(shared_file, "path", "")),
                "fs_id": getattr(shared_file, "fs_id", ""),
                "path": getattr(shared_file, "path", ""),
                "size": getattr(shared_file, "size", 0),
                "isdir": 1 if getattr(shared_file, "is_dir", False) else 0,
                "md5": getattr(shared_file, "md5", None),
            }

        file_info = extract_file_info(shared_file_dict)
        if file_info:
            shared_file_path = getattr(shared_file, "path", file_info.get("path", ""))
            file_info["path"] = cls._trim_shared_root(shared_file_path, shared_root)
        return file_info

    @staticmethod
    def _normalize_shared_child(shared_file):
        if isinstance(shared_file, dict):
            path = shared_file.get("path") or shared_file.get("server_filename", "")
            is_dir = shared_file.get("is_dir") or shared_file.get("isdir") == 1
            is_file = shared_file.get("is_file") or shared_file.get("isdir") == 0
            fs_id = shared_file.get("fs_id", "")
            md5 = shared_file.get("md5")
        else:
            path = getattr(shared_file, "path", "")
            is_dir = getattr(shared_file, "is_dir", False)
            is_file = getattr(shared_file, "is_file", not is_dir)
            fs_id = getattr(shared_file, "fs_id", "")
            md5 = getattr(shared_file, "md5", None)

        name = os.path.basename(str(path).rstrip("/"))
        return {
            "raw": shared_file,
            "fs_id": fs_id,
            "path": path,
            "name": name,
            "is_dir": bool(is_dir),
            "is_file": bool(is_file),
            "md5": md5,
        }

    def _iter_shared_dir_pages(self, dir_path, uk, share_id, bdstoken):
        page = 1
        page_size = self.SHARED_DIR_PAGE_SIZE
        probe_on_short = getattr(
            self, "SHARED_DIR_PAGE_PROBE_ON_SHORT", _DEFAULT_SHARED_DIR_PAGE_PROBE_ON_SHORT
        )
        last_yielded_page = 0
        total_pages_yielded = 0
        last_page_count = 0
        last_page_key = None

        try:
            while True:
                sub_paths = self.client.list_shared_paths(
                    dir_path, uk, share_id, bdstoken, page=page, size=page_size
                )
                if isinstance(sub_paths, list):
                    sub_files = sub_paths
                elif isinstance(sub_paths, dict):
                    sub_files = sub_paths.get("list", [])
                else:
                    break

                page_key = tuple(
                    getattr(item, "fs_id", getattr(item, "path", item)) for item in sub_files
                )
                if not sub_files:
                    last_yielded_page = page
                    total_pages_yielded += 1
                    last_page_count = 0
                    yield page, sub_files
                    break
                if page_key == last_page_key:
                    break
                last_page_key = page_key
                last_yielded_page = page
                total_pages_yielded += 1
                last_page_count = len(sub_files)
                yield page, sub_files
                if last_page_count < page_size and not probe_on_short:
                    break
                page += 1
        finally:
            emit_storage_metric(
                "shared_dir_pages",
                dir_path=dir_path,
                page_size=page_size,
                pages=total_pages_yielded,
                last_page=last_yielded_page,
                last_page_count=last_page_count,
            )

    def iter_shared_dir_children(self, path, uk, share_id, bdstoken):
        dir_path = getattr(path, "path", path)
        for _, sub_files in self._iter_shared_dir_pages(dir_path, uk, share_id, bdstoken):
            for sub_file in sub_files:
                yield self._normalize_shared_child(sub_file)

    def list_shared_dir_children(self, path, uk, share_id, bdstoken):
        return list(self.iter_shared_dir_children(path, uk, share_id, bdstoken))

    def iter_shared_dir_files(
        self,
        path,
        uk,
        share_id,
        bdstoken,
        folder_filter=None,
        shared_root="",
        progress_callback=None,
        stats=None,
        exclude_folder_filter=None,
    ):
        if stats is None:
            stats = self._new_scan_stats()

        if not self.client:
            handle_error_and_notify(
                ValueError("客户端未初始化或初始化失败"),
                "获取共享目录文件失败: 客户端不可用",
                self.wechat_notifier,
                None,
                collect=False,
            )
            return

        stack = [(path, None, None)]
        while stack:
            current_path_obj, page_iter, file_iter = stack.pop()
            dir_path = getattr(current_path_obj, "path", current_path_obj)
            try:
                if page_iter is None:
                    stats["dirs"] += 1
                    if self._should_report_progress(stats):
                        self._report_scan_progress(progress_callback, stats)
                    page_iter = iter(self._iter_shared_dir_pages(dir_path, uk, share_id, bdstoken))

                while True:
                    if file_iter is None:
                        page, sub_files = next(page_iter)
                        stats["pages"] += 1
                        if self._should_report_progress(stats):
                            self._report_scan_progress(progress_callback, stats)
                        if not sub_files:
                            if page == 1 and progress_callback:
                                progress_callback("info", f"共享目录为空: {dir_path}")
                            break
                        file_iter = iter(sub_files)

                    try:
                        sub_file = next(file_iter)
                    except StopIteration:
                        file_iter = None
                        continue

                    child = self._normalize_shared_child(sub_file)
                    if child["is_dir"]:
                        folder_name = child["name"]
                        if should_exclude_folder(folder_name, exclude_folder_filter):
                            stats["skipped_dirs"] += 1
                        elif should_include_folder(folder_name, folder_filter):
                            stack.append((current_path_obj, page_iter, file_iter))
                            stack.append((child["raw"], None, None))
                            break
                        else:
                            stats["skipped_dirs"] += 1
                    else:
                        file_info = self._normalize_shared_file_info(child["raw"], shared_root)
                        if file_info:
                            stats["files"] += 1
                            if self._should_report_progress(stats):
                                self._report_scan_progress(progress_callback, stats)
                            yield file_info
            except StopIteration:
                continue
            except Exception as exc:
                handle_error_and_notify(
                    exc,
                    f"获取共享目录文件时发生异常\n目录路径: {dir_path}",
                    self.wechat_notifier,
                    None,
                    collect=True,
                )
                raise

    def list_shared_dir_files(
        self,
        path,
        uk,
        share_id,
        bdstoken,
        folder_filter=None,
        shared_root="",
        progress_callback=None,
        stats=None,
        exclude_folder_filter=None,
    ):
        return list(
            self.iter_shared_dir_files(
                path,
                uk,
                share_id,
                bdstoken,
                folder_filter,
                shared_root,
                progress_callback,
                stats,
                exclude_folder_filter=exclude_folder_filter,
            )
        )

    def iter_shared_files(
        self,
        shared_paths,
        folder_filter=None,
        progress_callback=None,
        exclude_folder_filter=None,
    ):
        if not shared_paths:
            return

        uk = shared_paths[0].uk
        share_id = shared_paths[0].share_id
        bdstoken = shared_paths[0].bdstoken
        shared_root = self._resolve_shared_root(shared_paths)
        stats = self._new_scan_stats()

        if progress_callback:
            progress_callback("info", f"开始获取共享文件列表，共 {len(shared_paths)} 个入口")

        for index, path in enumerate(shared_paths, 1):
            if path.is_dir:
                folder_name = os.path.basename(path.path.rstrip("/"))
                if should_exclude_folder(folder_name, exclude_folder_filter):
                    stats["skipped_dirs"] += 1
                elif should_include_folder(folder_name, folder_filter):
                    if progress_callback:
                        progress_callback(
                            "info",
                            f"开始扫描共享入口 {index}/{len(shared_paths)}: {path.path}",
                        )
                    yield from self.iter_shared_dir_files(
                        path,
                        uk,
                        share_id,
                        bdstoken,
                        folder_filter,
                        shared_root,
                        progress_callback,
                        stats,
                        exclude_folder_filter=exclude_folder_filter,
                    )
                else:
                    stats["skipped_dirs"] += 1
                continue

            file_info = self._normalize_shared_file_info(path, shared_root)
            if file_info:
                stats["files"] += 1
                if self._should_report_progress(stats):
                    self._report_scan_progress(progress_callback, stats)
                yield file_info

        if progress_callback:
            progress_callback(
                "info",
                f"共享文件列表获取完成：扫描 {stats['dirs']} 个目录 / {stats['pages']} 页，"
                f"发现 {stats['files']} 个文件，跳过 {stats['skipped_dirs']} 个目录",
            )

    def list_shared_files(
        self,
        shared_paths,
        folder_filter=None,
        progress_callback=None,
        exclude_folder_filter=None,
    ):
        return list(
            self.iter_shared_files(
                shared_paths,
                folder_filter,
                progress_callback,
                exclude_folder_filter=exclude_folder_filter,
            )
        )
