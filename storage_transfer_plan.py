#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time

from storage_metrics import emit_storage_metric
from storage_constants import (
    RATE_LIMIT_WAIT_TIME,
    TRANSFER_BATCH_SIZE,
    TRANSFER_FAILED_RETRY_ATTEMPTS,
    TRANSFER_FAILED_RETRY_DELAY,
)
from storage_errors import (
    classify_storage_error,
    is_rate_limit_error,
    is_storage_temporary_error_info,
    is_transfer_count_limit_error,
)
from utils import handle_error_and_notify


def execute_transfer_plan(
    storage,
    transfer_list,
    share_url,
    uk,
    share_id,
    bdstoken,
    target_dir,
    progress_callback=None,
):
    if progress_callback:
        progress_callback("info", f"【步骤4/4】开始执行转存操作，共 {len(transfer_list)} 个文件")

    def build_entries(items, batch_size):
        grouped_transfer_items = {}
        for item in items:
            _, dir_path, _, _, _ = item
            grouped_transfer_items.setdefault(dir_path, []).append(item)

        entries = []
        batch_size = max(1, int(batch_size))
        for dir_path, dir_items in grouped_transfer_items.items():
            for start in range(0, len(dir_items), batch_size):
                batch_items = dir_items[start : start + batch_size]
                batch_fs_ids = [item[0] for item in batch_items]
                entries.append((dir_path, batch_fs_ids, batch_items))
        return entries

    def reduce_transfer_batch_size(batch_size):
        for candidate in (300, 200, 100, 50, 20, 10, 5, 1):
            if candidate < batch_size:
                return candidate
        return 1

    def add_pending_items(next_pending, items):
        for item in items:
            key = storage._transfer_item_key(item)
            if key not in successful_keys:
                next_pending[key] = item

    def add_remaining_entries(next_pending, entries):
        for _, _, remaining_items in entries:
            add_pending_items(next_pending, remaining_items)

    successful_transfer_items = []
    successful_keys = set()
    failed_records = {}
    pending_items = list(transfer_list)
    max_attempt = TRANSFER_FAILED_RETRY_ATTEMPTS + 1
    attempt = 1
    current_batch_size = TRANSFER_BATCH_SIZE

    scan_cache = {}
    local_files_cache_dirty = False
    local_files_cache_touched = False

    def mark_local_files_cache_dirty():
        nonlocal local_files_cache_dirty, local_files_cache_touched
        if target_dir:
            local_files_cache_dirty = True
            local_files_cache_touched = True

    def add_successful_items(items):
        for item in items:
            key = storage._transfer_item_key(item)
            if key in successful_keys:
                continue
            successful_keys.add(key)
            failed_records.pop(key, None)
            successful_transfer_items.append(item)

    if any(item[4] for item in pending_items):
        existing_items, pending_items = storage._split_existing_transfer_items(
            pending_items,
            target_dir,
            progress_callback,
            scan_cache,
        )
        add_successful_items(existing_items)

    try:
        while pending_items and attempt <= max_attempt:
            batch_size = current_batch_size
            grouped_transfer_entries = build_entries(pending_items, batch_size)
            next_pending = {}
            reduced_batch_size = batch_size
            batch_size_reduced = False
            regular_retry_needed = False
            stop_due_to_temporary_error = False

            for index, (dir_path, fs_ids, batch_items) in enumerate(grouped_transfer_entries):
                try:
                    normalized_dir_path = storage._transfer_group(
                        dir_path,
                        fs_ids,
                        share_url,
                        uk,
                        share_id,
                        bdstoken,
                        progress_callback,
                    )
                    mark_local_files_cache_dirty()
                    add_successful_items(batch_items)
                    if progress_callback:
                        progress_callback("success", f"成功转存到 {normalized_dir_path}")
                except Exception as e:
                    mark_local_files_cache_dirty()
                    final_error = e
                    if is_transfer_count_limit_error(e) and len(batch_items) > 1:
                        next_size = reduce_transfer_batch_size(len(batch_items))
                        reduced_batch_size = min(reduced_batch_size, next_size)
                        if progress_callback:
                            progress_callback(
                                "warning",
                                f"转存批次超量，降低批量到 {next_size} 后重试: "
                                f"{dir_path} ({len(batch_items)} 个文件)",
                            )
                        add_pending_items(next_pending, batch_items)
                        add_remaining_entries(next_pending, grouped_transfer_entries[index + 1 :])
                        batch_size_reduced = True
                        final_error = None
                        break
                    elif is_rate_limit_error(e):
                        if progress_callback:
                            progress_callback(
                                "warning",
                                f"触发频率限制，等待{RATE_LIMIT_WAIT_TIME}秒后重试...",
                            )
                        emit_storage_metric(
                            "rate_limit_sleep",
                            dir_path=dir_path,
                            fs_ids=len(fs_ids),
                            sleep_seconds=RATE_LIMIT_WAIT_TIME,
                        )
                        time.sleep(RATE_LIMIT_WAIT_TIME)
                        try:
                            normalized_dir_path = storage._transfer_group(
                                dir_path,
                                fs_ids,
                                share_url,
                                uk,
                                share_id,
                                bdstoken,
                                None,
                            )
                            mark_local_files_cache_dirty()
                            add_successful_items(batch_items)
                            if progress_callback:
                                progress_callback("success", f"重试成功: {normalized_dir_path}")
                            final_error = None
                        except Exception as retry_e:
                            mark_local_files_cache_dirty()
                            final_error = retry_e

                    if final_error is not None:
                        error_info = classify_storage_error(final_error)
                        error_msg = f"转存失败: {dir_path} - {error_info.message}"
                        if progress_callback:
                            progress_callback(
                                "warning" if error_info.retryable else "error",
                                error_msg,
                            )
                        if not error_info.retryable:
                            handle_error_and_notify(
                                final_error,
                                f"转存失败: {dir_path}",
                                storage.wechat_notifier,
                                None,
                                collect=True,
                            )
                        force_refresh = local_files_cache_dirty
                        existing_items, missing_items = storage._verify_existing_for_batch(
                            batch_items,
                            target_dir,
                            progress_callback,
                            scan_cache,
                            force_refresh=force_refresh,
                        )
                        if force_refresh:
                            local_files_cache_dirty = False
                        missing_keys = {storage._transfer_item_key(item) for item in missing_items}
                        for item in batch_items:
                            if storage._transfer_item_key(item) not in missing_keys:
                                failed_records.pop(storage._transfer_item_key(item), None)
                        add_successful_items(existing_items)
                        is_temporary_error = is_storage_temporary_error_info(error_info)
                        if (
                            error_info.retryable
                            and not is_temporary_error
                            and len(missing_items) > 1
                        ):
                            next_size = reduce_transfer_batch_size(len(missing_items))
                            reduced_batch_size = min(reduced_batch_size, next_size)
                            if progress_callback:
                                progress_callback(
                                    "warning",
                                    f"转存批次失败，降低批量到 {next_size} 后隔离重试: "
                                    f"{dir_path} ({len(missing_items)} 个文件)",
                                )
                            add_pending_items(next_pending, missing_items)
                            add_remaining_entries(
                                next_pending, grouped_transfer_entries[index + 1 :]
                            )
                            batch_size_reduced = True
                            break

                        for item in missing_items:
                            key = storage._transfer_item_key(item)
                            if key in successful_keys:
                                continue
                            failed_records[key] = storage._build_transfer_failed_record(
                                item, error_info
                            )
                            if error_info.retryable and not is_temporary_error:
                                next_pending[key] = item
                                regular_retry_needed = True

                        if is_temporary_error:
                            for _, _, remaining_items in grouped_transfer_entries[index + 1 :]:
                                for item in remaining_items:
                                    key = storage._transfer_item_key(item)
                                    if key in successful_keys:
                                        continue
                                    failed_records[key] = storage._build_transfer_failed_record(
                                        item, error_info
                                    )
                            stop_due_to_temporary_error = True
                            break

            pending_items = [] if stop_due_to_temporary_error else list(next_pending.values())
            if batch_size_reduced:
                current_batch_size = reduced_batch_size
            if pending_items and regular_retry_needed and attempt < max_attempt:
                if progress_callback:
                    progress_callback(
                        "warning",
                        f"记录到 {len(pending_items)} 个转存失败文件，"
                        f"等待{TRANSFER_FAILED_RETRY_DELAY}秒后重试...",
                    )
                time.sleep(TRANSFER_FAILED_RETRY_DELAY)
            if regular_retry_needed or not pending_items:
                attempt += 1
    finally:
        if local_files_cache_touched and target_dir:
            affected = storage._affected_relative_dirs(transfer_list, target_dir)
            storage._clear_local_files_cache(target_dir, affected)

    return len(successful_transfer_items), successful_transfer_items, list(failed_records.values())
