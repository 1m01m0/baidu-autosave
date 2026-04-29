#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os

from storage_rules import extract_file_info, should_include_folder
from utils import handle_error_and_notify


class SharedPathService:
    SHARED_DIR_PAGE_SIZE = 100

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
    ):
        files = []
        if stats is None:
            stats = {"dirs": 0, "files": 0}

        try:
            if not self.client:
                handle_error_and_notify(
                    ValueError("客户端未初始化或初始化失败"),
                    "获取共享目录文件失败: 客户端不可用",
                    self.wechat_notifier,
                    None,
                    collect=False,
                )
                return files

            page = 1
            page_size = self.SHARED_DIR_PAGE_SIZE
            dir_items_count = 0
            dir_path = getattr(path, "path", path)
            stats["dirs"] += 1

            while True:
                if progress_callback:
                    progress_callback(
                        "info",
                        f"正在获取共享目录第 {page} 页: {dir_path}"
                        f"（已扫描 {stats['dirs']} 个目录，已发现 {stats['files']} 个文件）",
                    )

                sub_paths = self.client.list_shared_paths(
                    path.path, uk, share_id, bdstoken, page=page, size=page_size
                )

                if isinstance(sub_paths, list):
                    sub_files = sub_paths
                elif isinstance(sub_paths, dict):
                    sub_files = sub_paths.get("list", [])
                else:
                    break

                if not sub_files:
                    if page == 1 and progress_callback:
                        progress_callback("info", f"共享目录为空: {dir_path}")
                    break

                dir_items_count += len(sub_files)
                for sub_file in sub_files:
                    is_dir = getattr(sub_file, "is_dir", False)
                    if is_dir:
                        folder_name = os.path.basename(getattr(sub_file, "path", ""))
                        if should_include_folder(folder_name, folder_filter):
                            files.extend(
                                self.list_shared_dir_files(
                                    sub_file,
                                    uk,
                                    share_id,
                                    bdstoken,
                                    folder_filter,
                                    shared_root,
                                    progress_callback,
                                    stats,
                                )
                            )
                    else:
                        file_info = self._normalize_shared_file_info(sub_file, shared_root)
                        if file_info:
                            files.append(file_info)
                            stats["files"] += 1

                if len(sub_files) < page_size:
                    break
                page += 1

            if progress_callback:
                progress_callback(
                    "info",
                    f"共享目录获取完成: {dir_path}，目录内 {dir_items_count} 项，"
                    f"累计发现 {stats['files']} 个文件",
                )

        except Exception as exc:
            handle_error_and_notify(
                exc,
                f"获取共享目录文件时发生异常\n目录路径: {getattr(path, 'path', path)}",
                self.wechat_notifier,
                None,
                collect=True,
            )
            raise

        return files

    def list_shared_files(self, shared_paths, folder_filter=None, progress_callback=None):
        if not shared_paths:
            return []

        uk = shared_paths[0].uk
        share_id = shared_paths[0].share_id
        bdstoken = shared_paths[0].bdstoken
        shared_root = self._resolve_shared_root(shared_paths)
        files = []
        stats = {"dirs": 0, "files": 0}

        if progress_callback:
            progress_callback("info", f"开始获取共享文件列表，共 {len(shared_paths)} 个入口")

        for index, path in enumerate(shared_paths, 1):
            if path.is_dir:
                folder_name = os.path.basename(path.path)
                if should_include_folder(folder_name, folder_filter):
                    if progress_callback:
                        progress_callback(
                            "info",
                            f"开始扫描共享入口 {index}/{len(shared_paths)}: {path.path}",
                        )
                    files.extend(
                        self.list_shared_dir_files(
                            path,
                            uk,
                            share_id,
                            bdstoken,
                            folder_filter,
                            shared_root,
                            progress_callback,
                            stats,
                        )
                    )
                elif progress_callback:
                    progress_callback("info", f"共享入口被文件夹过滤跳过: {path.path}")
                continue

            file_info = self._normalize_shared_file_info(path, shared_root)
            if file_info:
                files.append(file_info)
                stats["files"] += 1

        if progress_callback:
            progress_callback(
                "info",
                f"共享文件列表获取完成：扫描 {stats['dirs']} 个目录，发现 {stats['files']} 个文件",
            )

        return files
