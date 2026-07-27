#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import queue
import threading

from logger import get_logger
from storage_constants import (
    STREAM_PRODUCER_JOIN_TIMEOUT,
    TRANSFER_BATCH_SIZE,
    TRANSFER_PIPELINE_ENABLED,
)
from storage_errors import classify_storage_error, parse_share_error
from utils import handle_error_and_notify


def put_stream_queue_item(stream_queue, item, stop_event):
    while not stop_event.is_set():
        try:
            stream_queue.put(item, timeout=0.1)
            return True
        except queue.Full:
            continue
    return False


def transfer_share_streaming(
    storage,
    context,
    share_url,
    transfer_target_dir,
    regex_pattern=None,
    regex_replace=None,
    folder_filter=None,
    exclude_folder_filter=None,
    progress_callback=None,
):
    if progress_callback:
        progress_callback("info", "【步骤2/4】扫描共享文件并过滤")

    sentinel = object()
    stream_queue = queue.Queue(maxsize=max(TRANSFER_BATCH_SIZE * 2, 1))
    stop_event = threading.Event()
    producer_error = {}

    def producer():
        try:
            for file_info in storage.share_service.iter_shared_files(
                context["shared_paths"],
                folder_filter,
                progress_callback,
                exclude_folder_filter=exclude_folder_filter,
            ):
                if not storage._put_stream_queue_item(stream_queue, file_info, stop_event):
                    return
        except Exception as exc:
            producer_error["error"] = exc
        finally:
            storage._put_stream_queue_item(stream_queue, sentinel, stop_event)

    producer_thread = threading.Thread(
        target=producer, name="transfershare-share-file-producer", daemon=True
    )
    producer_thread.start()

    summary = Counter()
    warning_samples = []
    scanned_relative_dirs = set()
    local_files_dict = {}
    current_planned_paths = {}
    shared_file_batch = []
    transfer_item_buffer = []
    total_transfer_count = 0
    transfer_success_count = 0
    successful_transfer_items = []
    transfer_failed_files = []
    dir_error = None

    transfer_executor = None
    pending_transfer = {"future": None, "items": None}
    if TRANSFER_PIPELINE_ENABLED:
        transfer_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="transfershare-transfer"
        )

    def _run_transfer_plan(transfer_list):
        return storage._execute_transfer_plan(
            transfer_list,
            share_url,
            context["uk"],
            context["share_id"],
            context["bdstoken"],
            transfer_target_dir,
            progress_callback,
        )

    def _drain_pending_transfer():
        nonlocal total_transfer_count, transfer_success_count
        future = pending_transfer["future"]
        if future is None:
            return None
        transfer_list = pending_transfer["items"]
        pending_transfer["future"] = None
        pending_transfer["items"] = None
        try:
            success_count, successful_items, failed_items = future.result()
        except Exception as exc:
            error_info = classify_storage_error(exc)
            handle_error_and_notify(
                exc,
                "流水线转存批次异常",
                storage.wechat_notifier,
                None,
                collect=True,
            )
            total_transfer_count += len(transfer_list)
            transfer_failed_files.extend(
                storage._build_transfer_failed_record(item, error_info) for item in transfer_list
            )
            return None

        total_transfer_count += len(transfer_list)
        transfer_success_count += success_count
        successful_transfer_items.extend(successful_items)
        for item in successful_items:
            storage._record_transfer_item_paths(item, local_files_dict)
        transfer_failed_files.extend(failed_items)
        return None

    def _submit_transfer_plan(transfer_list):
        if not transfer_list:
            return
        if pending_transfer["future"] is not None:
            _drain_pending_transfer()
        pending_transfer["future"] = transfer_executor.submit(_run_transfer_plan, transfer_list)
        pending_transfer["items"] = transfer_list

    def flush_transfer_item_buffer(force=False):
        nonlocal total_transfer_count, transfer_success_count, dir_error
        if not transfer_item_buffer:
            return None
        if not force and len(transfer_item_buffer) < TRANSFER_BATCH_SIZE:
            return None

        flush_count = len(transfer_item_buffer) if force else TRANSFER_BATCH_SIZE
        transfer_list = transfer_item_buffer[:flush_count]
        del transfer_item_buffer[:flush_count]

        dir_error = storage._ensure_transfer_dirs(transfer_list)
        if dir_error:
            _drain_pending_transfer()
            error_info = classify_storage_error(dir_error.get("error", "创建目录失败"))
            transfer_failed_files.extend(
                storage._build_transfer_failed_record(item, error_info) for item in transfer_list
            )
            total_transfer_count += len(transfer_list)
            return dir_error

        if transfer_executor is None:
            success_count, successful_items, failed_items = _run_transfer_plan(transfer_list)
            total_transfer_count += len(transfer_list)
            transfer_success_count += success_count
            successful_transfer_items.extend(successful_items)
            for item in successful_items:
                storage._record_transfer_item_paths(item, local_files_dict)
            transfer_failed_files.extend(failed_items)
            return None

        _submit_transfer_plan(transfer_list)
        return None

    def flush_shared_file_batch():
        if not shared_file_batch:
            return None

        candidates, batch_summary, relative_dirs = storage._prepare_transfer_candidates(
            shared_file_batch,
            context["shared_paths"],
            transfer_target_dir,
            regex_pattern,
            regex_replace,
        )
        shared_file_batch.clear()
        summary.update(batch_summary)

        new_relative_dirs = set(relative_dirs) - scanned_relative_dirs
        if new_relative_dirs:
            local_files_dict.update(
                storage._scan_local_files_dict(
                    transfer_target_dir, progress_callback, new_relative_dirs
                )
            )
            scanned_relative_dirs.update(new_relative_dirs)

        transfer_list = storage._filter_transfer_candidates_core(
            candidates, local_files_dict, summary, warning_samples, current_planned_paths
        )
        transfer_item_buffer.extend(transfer_list)
        while len(transfer_item_buffer) >= TRANSFER_BATCH_SIZE:
            buffer_error = flush_transfer_item_buffer()
            if buffer_error:
                return buffer_error
        return None

    try:
        while True:
            item = stream_queue.get()
            if item is sentinel:
                break
            shared_file_batch.append(item)
            if len(shared_file_batch) >= TRANSFER_BATCH_SIZE:
                dir_error = flush_shared_file_batch()
                if dir_error:
                    break

        if not dir_error:
            dir_error = flush_shared_file_batch()
        if not dir_error:
            dir_error = flush_transfer_item_buffer(force=True)

        _drain_pending_transfer()
    finally:
        stop_event.set()
        producer_thread.join(STREAM_PRODUCER_JOIN_TIMEOUT)
        if producer_thread.is_alive():
            get_logger().warning("共享文件扫描线程未及时退出，继续处理已完成结果")
        if transfer_executor is not None:
            transfer_executor.shutdown(wait=True)

    storage._report_transfer_candidate_summary(summary, warning_samples, progress_callback)

    if dir_error:
        if transfer_success_count > 0:
            rename_result = storage._rename_transferred_files(
                successful_transfer_items, transfer_target_dir, progress_callback
            )
            result = storage._build_transfer_result(
                transfer_success_count,
                total_transfer_count,
                rename_result,
                progress_callback,
                transfer_failed_files,
            )
            error_msg = dir_error.get("error", "创建目录失败")
            result.update(
                {
                    "success": False,
                    "partial": True,
                    "error": error_msg,
                    "message": f"部分转存完成，但准备后续目录失败: {error_msg}",
                }
            )
            return result
        dir_error["transfer_failed_files"] = transfer_failed_files
        dir_error["transfer_failed_count"] = len(transfer_failed_files)
        return dir_error

    scan_error = producer_error.get("error")
    if not total_transfer_count:
        if scan_error:
            return {"success": False, "error": parse_share_error(scan_error)}
        if progress_callback:
            progress_callback("info", "没有找到需要处理的文件")
        return {
            "success": True,
            "skipped": True,
            "message": "没有新文件需要转存",
        }

    rename_result = storage._rename_transferred_files(
        successful_transfer_items, transfer_target_dir, progress_callback
    )
    result = storage._build_transfer_result(
        transfer_success_count,
        total_transfer_count,
        rename_result,
        progress_callback,
        transfer_failed_files,
    )
    if scan_error:
        error_msg = parse_share_error(scan_error)
        if transfer_success_count > 0:
            result.update(
                {
                    "success": False,
                    "partial": True,
                    "error": error_msg,
                    "message": f"部分转存完成，但扫描共享文件失败: {error_msg}",
                }
            )
        else:
            result["error"] = f"{result.get('error', '转存失败')}；扫描共享文件失败: {error_msg}"
    return result
