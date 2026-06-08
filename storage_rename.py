#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import posixpath
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from storage_constants import RENAME_CONCURRENCY, RENAME_DELAY
from storage_errors import classify_storage_error
from storage_rules import is_safe_relative_target_path
from utils import handle_error_and_notify


def rename_one_transferred_file(
    storage, dir_path, clean_path, final_path, target_dir, progress_callback=None
):
    """对单个文件执行重命名，返回成功路径或抛异常。"""
    if not is_safe_relative_target_path(final_path):
        raise ValueError(f"重命名目标路径不安全: {final_path}")
    original_full_path = posixpath.join(target_dir, clean_path)
    final_full_path = posixpath.join(target_dir, final_path)
    final_parent_dir = posixpath.dirname(final_full_path).replace("\\", "/")

    if final_parent_dir and final_parent_dir != dir_path:
        if not storage.path_service.ensure_dir_exists(final_parent_dir):
            raise ValueError(f"创建重命名目标目录失败: {final_parent_dir}")

    if progress_callback:
        progress_callback("info", f"重命名文件: {clean_path} -> {final_path}")

    storage.client.rename(original_full_path, final_full_path)
    affected_dirs = storage._candidate_parent_dirs(clean_path, final_path)
    storage._clear_local_files_cache(target_dir, affected_dirs)
    return final_path


def rename_transferred_files(
    storage, successful_transfer_items, target_dir, progress_callback=None
):
    renamed_files = []
    rename_failed_files = []
    completed_count = 0

    rename_jobs = []
    for item in successful_transfer_items:
        _, dir_path, clean_path, final_path, need_rename = item[:5]
        if not need_rename:
            renamed_files.append(final_path)
            completed_count += 1
        else:
            rename_jobs.append((dir_path, clean_path, final_path))

    rename_total = len(rename_jobs)
    if rename_total == 0:
        return {
            "transferred_files": renamed_files,
            "rename_failed_files": rename_failed_files,
            "rename_failed_count": 0,
            "completed_count": completed_count,
        }

    concurrency = max(1, min(RENAME_CONCURRENCY, rename_total))

    if concurrency == 1:
        for index, (dir_path, clean_path, final_path) in enumerate(rename_jobs):
            try:
                result = storage._rename_one_transferred_file(
                    dir_path, clean_path, final_path, target_dir, progress_callback
                )
                renamed_files.append(result)
                completed_count += 1
                if index < rename_total - 1:
                    time.sleep(RENAME_DELAY)
            except Exception as exc:
                storage._record_rename_failure(
                    clean_path, final_path, exc, rename_failed_files, progress_callback
                )
        return {
            "transferred_files": renamed_files,
            "rename_failed_files": rename_failed_files,
            "rename_failed_count": len(rename_failed_files),
            "completed_count": completed_count,
        }

    with ThreadPoolExecutor(
        max_workers=concurrency, thread_name_prefix="transfershare-rename"
    ) as executor:
        future_map = {
            executor.submit(
                storage._rename_one_transferred_file,
                dir_path,
                clean_path,
                final_path,
                target_dir,
                progress_callback,
            ): (clean_path, final_path)
            for dir_path, clean_path, final_path in rename_jobs
        }
        for future in as_completed(future_map):
            clean_path, final_path = future_map[future]
            try:
                result = future.result()
                renamed_files.append(result)
                completed_count += 1
            except Exception as exc:
                storage._record_rename_failure(
                    clean_path, final_path, exc, rename_failed_files, progress_callback
                )

    return {
        "transferred_files": renamed_files,
        "rename_failed_files": rename_failed_files,
        "rename_failed_count": len(rename_failed_files),
        "completed_count": completed_count,
    }


def record_rename_failure(
    storage, clean_path, final_path, exc, rename_failed_files, progress_callback
):
    error_info = classify_storage_error(exc)
    error_msg = (
        f"重命名文件失败: {os.path.basename(clean_path)} -> "
        f"{os.path.basename(final_path)}"
    )
    if progress_callback:
        progress_callback("error", f"{error_msg}: {error_info.message}")
    handle_error_and_notify(
        exc,
        f"重命名文件失败\n原始文件: {os.path.basename(clean_path)}\n"
        f"目标文件: {os.path.basename(final_path)}",
        storage.wechat_notifier,
        None,
        collect=True,
    )
    rename_failed_files.append(
        {
            "source_path": clean_path,
            "target_path": final_path,
            "error": error_info.message,
        }
    )
