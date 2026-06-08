#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from collections import Counter

import os
import posixpath
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# 添加 WeChatNotifier 和工具方法导入
from wechat_notifier import WeChatNotifier
from utils import handle_error_and_notify, ErrorCollector, mask_share_url
import storage_rename
import storage_streaming
import storage_transfer_plan
from config_utils import build_retry_share_config, parse_share_links_from_text
from env_utils import read_non_negative_float_env, read_positive_int_env
from storage_constants import (
    BATCH_SHARE_DELAY as BATCH_SHARE_DELAY,
    MULTI_SHARE_CONCURRENCY as MULTI_SHARE_CONCURRENCY,
    RATE_LIMIT_WAIT_TIME as RATE_LIMIT_WAIT_TIME,
    RENAME_CONCURRENCY as RENAME_CONCURRENCY,
    RENAME_DELAY as RENAME_DELAY,
    STREAM_PRODUCER_JOIN_TIMEOUT as STREAM_PRODUCER_JOIN_TIMEOUT,
    TRANSFER_BATCH_SIZE as TRANSFER_BATCH_SIZE,
    TRANSFER_FAILED_RETRY_ATTEMPTS as TRANSFER_FAILED_RETRY_ATTEMPTS,
    TRANSFER_FAILED_RETRY_DELAY as TRANSFER_FAILED_RETRY_DELAY,
    TRANSFER_PIPELINE_ENABLED as TRANSFER_PIPELINE_ENABLED,
)
from storage_client import BaiduClientAdapter
from storage_errors import (
    classify_storage_error,
    is_storage_temporary_error_info,
    is_transfer_count_limit_error,
    parse_share_error,
)
from storage_filter import CandidateFilter
from storage_loader import ShareLoader
from storage_models import DirTreeFrame, TransferItem
from storage_paths import StoragePathService
from storage_progress import ProgressReporter
from storage_rules import should_exclude_folder
from storage_shares import SharedPathService
from storage_traverser import DirTreeTraverser

try:
    from logger import get_logger
except ImportError:
    # 日志模块不可用时，使用标准日志
    import logging

    def get_logger(name="transfershare"):
        return logging.getLogger(name)


# 兼容老测试：保留下划线开头的别名
_read_non_negative_float_env = read_non_negative_float_env
_read_positive_int_env = read_positive_int_env
_DirTreeFrame = DirTreeFrame


# 兼容 BaiduStorage.__new__(BaiduStorage) 绕过 __init__ 的测试代码：
# 当实例没有 _shared_state_lock 属性时，用一个总是放行的锁占位。
class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


_NULL_LOCK = _NullLock()


class BaiduStorageDirTransferExecutor:
    def __init__(self, storage, progress_callback=None):
        self.storage = storage
        self.progress_callback = progress_callback

    def execute_transfer_plan(
        self,
        file_transfer_list,
        share_url,
        uk,
        share_id,
        bdstoken,
        target_dir,
    ):
        return self.storage._execute_transfer_plan(
            file_transfer_list,
            share_url,
            uk,
            share_id,
            bdstoken,
            target_dir,
            self.progress_callback,
        )

    def transfer_group(self, dir_path, fs_ids, share_url, uk, share_id, bdstoken):
        return self.storage._transfer_group(
            dir_path,
            fs_ids,
            share_url,
            uk,
            share_id,
            bdstoken,
            self.progress_callback,
        )

    def clear_local_files_cache(self, target_dir, affected_relative_dirs=None):
        self.storage._clear_local_files_cache(target_dir, affected_relative_dirs)


class BaiduStorage:
    def __init__(self, cookies, wechat_webhook=None):
        self.wechat_notifier = (
            WeChatNotifier(wechat_webhook) if wechat_webhook else None
        )
        self._local_files_cache = {}
        # 多链接并发场景下保护共享可变状态（_local_files_cache 的写/iter）。
        # _ensured_dirs（path_service 内）的并发竞态可以容忍——重复 makedir 走"已存在"
        # 错误码后是幂等的，付出的代价只是若干次浪费的 API 调用，不影响正确性。
        self._shared_state_lock = threading.RLock()
        self.client = BaiduClientAdapter(cookies)
        self.path_service = StoragePathService(
            self.client, self.wechat_notifier, self._local_files_cache
        )
        self.path_service.shared_state_lock = self._shared_state_lock
        self.share_service = SharedPathService(self.client, self.wechat_notifier)

    def set_notifier(self, notifier):
        """设置微信通知器实例

        Args:
            notifier: WeChatNotifier 实例或 None
        """
        self.wechat_notifier = notifier
        self.path_service.wechat_notifier = notifier
        self.share_service.wechat_notifier = notifier

    def _notify_error(self, error, context_message, extra_info=None, collect=True):
        if extra_info is not None:
            context_message = f"{context_message}\n{extra_info}"
        handle_error_and_notify(
            error,
            context_message,
            self.wechat_notifier,
            None,
            collect=collect,
        )

    def get_quota_info(self):
        """获取网盘配额信息"""
        try:
            if not self.client:
                return None

            quota_info = self.client.quota()
            if isinstance(quota_info, (tuple, list)):
                quota = {
                    "total": quota_info[0],
                    "used": quota_info[1],
                    "total_gb": round(quota_info[0] / (1024**3), 2),
                    "used_gb": round(quota_info[1] / (1024**3), 2),
                }
            else:
                quota = quota_info

            return quota
        except Exception as e:
            self._notify_error(e, "获取网盘配额信息时发生异常", collect=False)
            return None

    def is_valid(self):
        """检查存储是否可用"""
        try:
            if not self.client:
                return False

            quota_info = self.get_quota_info()
            return bool(quota_info)

        except Exception as e:
            self._notify_error(e, "检查存储可用性时发生异常", collect=False)
            return False

    def _build_invalid_share_result(self, index, config):
        error_msg = f"第 {index} 个配置格式错误：缺少分享链接"
        invalid_share_url = (
            config.get("share_url", "未知") if isinstance(config, dict) else str(config)
        )
        self._notify_error(
            ValueError(error_msg),
            f"批量转存配置错误: 第 {index} 个配置格式错误",
        )
        return {
            "index": index,
            "share_url": mask_share_url(invalid_share_url) or invalid_share_url,
            "success": False,
            "partial": False,
            "error": error_msg,
        }

    def _notify_batch_progress(self, level, index, total_count, message, progress_callback):
        if progress_callback:
            progress_callback(level, f"【{index}/{total_count}】{message}")

    def _build_result_record(self, index, share_url, save_dir, result, source_config=None):
        masked_share_url = mask_share_url(share_url) or share_url
        transfer_failed_files = result.get("transfer_failed_files", [])
        record = {
            "index": index,
            "share_url": masked_share_url,
            "save_dir": save_dir,
            "success": result.get("success", False),
            "partial": result.get("partial", False),
        }
        if result.get("success"):
            if result.get("skipped"):
                record["skipped"] = True
                record["message"] = result.get("message", "没有新文件需要转存")
            else:
                record["message"] = result.get("message", "转存成功")
                record["transferred_files"] = result.get("transferred_files", [])
        elif result.get("partial"):
            record["message"] = result.get("message", "部分转存成功")
            record["transferred_files"] = result.get("transferred_files", [])
            record["transfer_failed_files"] = transfer_failed_files
            record["transfer_failed_count"] = result.get(
                "transfer_failed_count", len(transfer_failed_files)
            )
            record["rename_failed_files"] = result.get("rename_failed_files", [])
            record["rename_failed_count"] = result.get("rename_failed_count", 0)
            record["completed_count"] = result.get("completed_count", 0)
            record["transfer_success_count"] = result.get("transfer_success_count", 0)
        else:
            record["error"] = result.get("error", "未知错误")
            record["transfer_failed_files"] = transfer_failed_files
            record["transfer_failed_count"] = result.get(
                "transfer_failed_count", len(transfer_failed_files)
            )

        if transfer_failed_files and source_config:
            record["retry_config"] = build_retry_share_config(source_config)
        return record

    def _record_batch_result(self, counters, result_record):
        if result_record.get("success"):
            if result_record.get("skipped"):
                counters["skipped_count"] += 1
            else:
                counters["success_count"] += 1
        elif result_record.get("partial"):
            counters["partial_count"] += 1
        else:
            counters["failed_count"] += 1

    def _handle_batch_failure(self, index, share_url, save_dir, error_msg, partial=False):
        masked_share_url = mask_share_url(share_url) or share_url
        detail_title = "批量转存中单个链接部分成功" if partial else "批量转存中单个链接失败"
        detailed_error = (
            f"{detail_title}\n分享链接: {masked_share_url}\n保存目录: {save_dir}\n错误信息: {error_msg}"
        )
        self._notify_error(
            ValueError(detailed_error),
            f"批量转存单个链接{'部分成功' if partial else '失败'}: 第 {index} 个链接",
        )

    def _run_one_share_config_safely(
        self, index, total_count, config, progress_callback
    ):
        """对单个 share_config 执行 _process_single_share_config，捕获意外异常。

        返回 result_record（始终是 dict）；异常路径下也会构造一个失败 record。
        在并发模式下被多个 worker 调用，依赖：
        - _process_single_share_config 内部已自处理常规错误
        - ErrorCollector 是 thread-local，worker 内的 collect 不会污染主线程
        """
        try:
            return self._process_single_share_config(
                index, total_count, config, progress_callback
            )
        except Exception as e:
            error_info = classify_storage_error(e)
            error_msg = f"处理第 {index} 个分享链接时发生异常: {error_info.message}"
            share_url = (
                config.get("share_url", "未知") if isinstance(config, dict) else "未知"
            )
            masked_share_url = mask_share_url(share_url) or share_url
            self._notify_batch_progress(
                "error",
                index,
                total_count,
                f"异常: {error_info.message}",
                progress_callback,
            )
            self._notify_error(
                e,
                f"处理第 {index} 个分享链接时发生异常",
                extra_info=f"分享链接: {masked_share_url}",
            )
            return {
                "index": index,
                "share_url": masked_share_url,
                "success": False,
                "partial": False,
                "error": error_msg,
            }

    def _run_share_configs(
        self, share_configs, total_count, counters, progress_callback, concurrency
    ):
        """串行或并发地执行 share_configs，返回按原索引顺序排列的 result 列表。

        计数器累加统一在所有结果就绪后做，避免在串行/并发两条路径中重复累加。
        """
        if concurrency <= 1:
            results = []
            for index, config in enumerate(share_configs, 1):
                result_record = self._run_one_share_config_safely(
                    index, total_count, config, progress_callback
                )
                results.append(result_record)
                if index < total_count:
                    # 即便 BATCH_SHARE_DELAY=0 也调用 sleep(0)，保留与历史
                    # 串行行为一致的契约（部分测试断言相邻链接间 sleep 被调用）。
                    time.sleep(BATCH_SHARE_DELAY)
        else:
            # 并发路径：worker 各自调用 _run_one_share_config_safely。
            # 结果按原索引占位合并，保证 results 顺序与串行实现一致。

            results = [None] * total_count
            with ThreadPoolExecutor(
                max_workers=concurrency, thread_name_prefix="transfershare-share"
            ) as executor:
                future_index = {}
                for index, config in enumerate(share_configs, 1):
                    # 启动错峰：以 BATCH_SHARE_DELAY 为间隔提交，避免一瞬间打出 N 个分享
                    # 访问请求触发限频。BATCH_SHARE_DELAY 默认 0；用户可设非零做软节流。
                    if index > 1 and BATCH_SHARE_DELAY > 0:
                        time.sleep(BATCH_SHARE_DELAY)
                    future = executor.submit(
                        self._run_one_share_config_safely,
                        index,
                        total_count,
                        config,
                        progress_callback,
                    )
                    future_index[future] = index - 1
                for future in as_completed(future_index):
                    slot = future_index[future]
                    results[slot] = future.result()

        # 计数器在主线程顺序累加，避免并发竞争 counters dict
        for result_record in results:
            self._record_batch_result(counters, result_record)
        return results

    def _process_single_share_config(self, index, total_count, config, progress_callback=None):
        if not isinstance(config, dict) or "share_url" not in config:
            result_record = self._build_invalid_share_result(index, config)
            self._notify_batch_progress(
                "error", index, total_count, f"失败: {result_record['error']}", progress_callback
            )
            return result_record

        share_url = config["share_url"]
        masked_share_url = mask_share_url(share_url) or share_url
        pwd = config.get("pwd")
        save_dir = config.get("save_dir")
        regex_pattern = config.get("regex_pattern")
        regex_replace = config.get("regex_replace")
        folder_filter = config.get("folder_filter")
        exclude_folder_filter = config.get("exclude_folder_filter")

        self._notify_batch_progress(
            "info", index, total_count, f"处理分享链接: {masked_share_url}", progress_callback
        )

        result = self.transfer_share(
            share_url=share_url,
            pwd=pwd,
            save_dir=save_dir,
            progress_callback=progress_callback,
            regex_pattern=regex_pattern,
            regex_replace=regex_replace,
            folder_filter=folder_filter,
            exclude_folder_filter=exclude_folder_filter,
        )
        result_record = self._build_result_record(index, share_url, save_dir, result, config)

        if result.get("success"):
            if result.get("skipped"):
                self._notify_batch_progress(
                    "info",
                    index,
                    total_count,
                    f"跳过: {result_record.get('message')}",
                    progress_callback,
                )
            else:
                self._notify_batch_progress(
                    "success",
                    index,
                    total_count,
                    f"成功: {result_record.get('message')}",
                    progress_callback,
                )
        elif result.get("partial"):
            message = result_record.get("message", result.get("error", "部分成功"))
            self._notify_batch_progress(
                "warning", index, total_count, f"部分成功: {message}", progress_callback
            )
            self._handle_batch_failure(index, share_url, save_dir, message, partial=True)
        else:
            error_msg = result_record.get("error", "未知错误")
            self._notify_batch_progress(
                "error", index, total_count, f"失败: {error_msg}", progress_callback
            )
            self._handle_batch_failure(index, share_url, save_dir, error_msg)

        return result_record

    def _build_batch_summary(
        self, total_count, success_count, partial_count, failed_count, skipped_count
    ):
        summary_parts = []
        if success_count > 0:
            summary_parts.append(f"成功 {success_count} 个")
        if partial_count > 0:
            summary_parts.append(f"部分成功 {partial_count} 个")
        if skipped_count > 0:
            summary_parts.append(f"跳过 {skipped_count} 个")
        if failed_count > 0:
            summary_parts.append(f"失败 {failed_count} 个")
        return f"批量转存完成：共 {total_count} 个链接，" + "、".join(summary_parts)

    def transfer_multiple_shares(self, share_configs, progress_callback=None):
        """批量转存多个分享链接"""
        with ErrorCollector(
            "批量转存多个分享链接", self.wechat_notifier, None, auto_send=False
        ):
            if not share_configs or not isinstance(share_configs, list):
                error_msg = "分享配置列表不能为空或格式错误"
                self._notify_error(ValueError(error_msg), "批量转存配置错误")
                return {
                    "success": False,
                    "partial": False,
                    "error": error_msg,
                    "total_count": 0,
                    "success_count": 0,
                    "partial_count": 0,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "results": [],
                }

            # 不再清空 self._local_files_cache：精确失效在 _execute_transfer_plan 内
            # 完成；保留 cache 让多链接落到同 target_dir 下兄弟子目录时可以复用扫描结果。
            total_count = len(share_configs)
            counters = {
                "success_count": 0,
                "partial_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
            }

            # 多链接并发：默认 1（串行），>1 时让多个独立分享的转存流水线重叠。
            # 受百度限频影响，并发上限建议 ≤4。
            concurrency = max(1, min(MULTI_SHARE_CONCURRENCY, total_count))
            results = self._run_share_configs(
                share_configs, total_count, counters, progress_callback, concurrency
            )

            has_partial_items = counters["partial_count"] > 0
            has_failed_items = counters["failed_count"] > 0
            has_success = counters["success_count"] > 0
            has_skipped = counters["skipped_count"] > 0
            overall_partial = (has_success or has_skipped) and (
                has_partial_items or has_failed_items
            )
            all_rename_failed_files = []
            all_transfer_failed_files = []
            for item in results:
                all_rename_failed_files.extend(item.get("rename_failed_files", []))
                for failed_file in item.get("transfer_failed_files", []):
                    failed_detail = dict(failed_file)
                    failed_detail.setdefault("index", item.get("index"))
                    failed_detail.setdefault("share_url", item.get("share_url"))
                    failed_detail.setdefault("save_dir", item.get("save_dir"))
                    all_transfer_failed_files.append(failed_detail)

            summary = self._build_batch_summary(
                total_count,
                counters["success_count"],
                counters["partial_count"],
                counters["failed_count"],
                counters["skipped_count"],
            )
            if progress_callback:
                if overall_partial or has_partial_items:
                    progress_callback(
                        "warning",
                        summary + "（部分成功按失败退出，退出码 1）",
                    )
                elif has_success or has_skipped:
                    progress_callback("success", summary)
                else:
                    progress_callback("error", summary)

            overall_success = (has_success or has_skipped) and not (
                has_partial_items or has_failed_items
            )
            return {
                "success": overall_success,
                "partial": overall_partial or (has_partial_items and not overall_success),
                "skipped": has_skipped and not has_success and not has_partial_items and not has_failed_items,
                "total_count": total_count,
                "success_count": counters["success_count"],
                "partial_count": counters["partial_count"],
                "failed_count": counters["failed_count"],
                "skipped_count": counters["skipped_count"],
                "results": results,
                "transfer_failed_files": all_transfer_failed_files,
                "transfer_failed_count": len(all_transfer_failed_files),
                "rename_failed_files": all_rename_failed_files,
                "rename_failed_count": len(all_rename_failed_files),
                "summary": summary,
                "message": summary,
            }

    @staticmethod
    def parse_share_links_from_text(text, default_save_dir=None):
        """兼容旧调用方式，实际委托给共享配置工具。"""
        return parse_share_links_from_text(text, default_save_dir)

    def transfer_shares_from_text(
        self, text, default_save_dir=None, progress_callback=None
    ):
        """
        从文本中解析并批量转存分享链接
        只支持 https://pan.baidu.com/s/xxxxx?pwd=xxxx 格式
        Args:
            text: 包含分享链接的文本
            default_save_dir: 默认保存目录
            progress_callback: 进度回调函数
        Returns:
            dict: 批量转存结果
        """
        with ErrorCollector(
            "从文本中解析并批量转存分享链接", self.wechat_notifier, None
        ):
            try:
                if progress_callback:
                    progress_callback("info", "解析文本中的分享链接...")

                share_configs = parse_share_links_from_text(text, default_save_dir)

                if not share_configs:
                    error_msg = "文本中未找到有效的分享链接，请确保使用 https://pan.baidu.com/s/xxxxx?pwd=xxxx 格式"
                    if progress_callback:
                        progress_callback("warning", error_msg)
                    self._notify_error(ValueError(error_msg), "解析分享链接失败")
                    return {
                        "success": False,
                        "partial": False,
                        "error": error_msg,
                        "total_count": 0,
                        "success_count": 0,
                        "partial_count": 0,
                        "failed_count": 0,
                        "skipped_count": 0,
                        "results": [],
                    }

                if progress_callback:
                    progress_callback(
                        "success", f"解析完成，找到 {len(share_configs)} 个分享链接"
                    )

                return self.transfer_multiple_shares(share_configs, progress_callback)

            except Exception as e:
                error_info = classify_storage_error(e)
                error_msg = f"从文本转存失败: {error_info.message}"
                if progress_callback:
                    progress_callback("error", error_msg)
                self._notify_error(e, "从文本转存失败")
                return {
                    "success": False,
                    "partial": False,
                    "error": error_msg,
                    "total_count": 0,
                    "success_count": 0,
                    "partial_count": 0,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "results": [],
                }

    def _normalize_save_dir(self, save_dir):
        return self.path_service.normalize_path(save_dir) if save_dir else save_dir

    def _share_loader(self, progress_callback=None):
        def notify_error(error, context_message, collect=True):
            self._notify_error(error, context_message, collect=collect)

        progress = ProgressReporter(progress_callback) if progress_callback else None
        return ShareLoader(self.share_service, progress, notify_error)

    def _load_share_entries(self, share_url, pwd, progress_callback=None):
        return self._share_loader(progress_callback).load_entries(share_url, pwd)

    def _load_share_files(
        self,
        context,
        folder_filter,
        progress_callback=None,
        exclude_folder_filter=None,
    ):
        return self._share_loader(progress_callback).load_files(
            context,
            folder_filter,
            exclude_folder_filter=exclude_folder_filter,
        )

    def _load_share_context(
        self,
        share_url,
        pwd,
        folder_filter,
        progress_callback=None,
        exclude_folder_filter=None,
    ):
        return self._share_loader(progress_callback).load_context(
            share_url,
            pwd,
            folder_filter,
            exclude_folder_filter=exclude_folder_filter,
        )

    def _candidate_filter(self, progress_callback=None):
        return CandidateFilter(
            self.path_service,
            ProgressReporter(progress_callback),
        )

    @staticmethod
    def _candidate_parent_dirs(*paths):
        return CandidateFilter.candidate_parent_dirs(*paths)

    def _prepare_transfer_candidates(
        self,
        shared_files_info,
        shared_paths,
        target_dir,
        regex_pattern=None,
        regex_replace=None,
    ):
        return self._candidate_filter().prepare_candidates(
            shared_files_info,
            shared_paths,
            target_dir,
            regex_pattern,
            regex_replace,
        )

    def _scan_local_files_dict(self, save_dir, progress_callback=None, relative_dirs=None):
        if progress_callback:
            progress_callback("info", f"【步骤2/4】扫描本地目录: {save_dir}")

        local_files = []
        if save_dir:
            if relative_dirs is None:
                local_files = self.path_service.list_local_files(save_dir, use_cache=True)
            else:
                local_files = self.path_service.list_local_files_in_dirs(
                    save_dir, relative_dirs, use_cache=True, merge_dirs=True
                )
            if progress_callback:
                if relative_dirs is None:
                    progress_callback(
                        "info",
                        f"本地目录中有 {len(local_files)} 个文件（按相对路径统计）",
                    )
                else:
                    progress_callback(
                        "info",
                        f"已扫描 {len(relative_dirs)} 个候选本地目录，发现 {len(local_files)} 个文件",
                    )

        file_names = [
            self.path_service.normalize_path(file_info["file_name"], file_only=True)
            for file_info in local_files
        ]
        # 重复名检测：先用 set 与 list 长度差异早判，绝大多数没有重名的目录
        # 直接走 fast path，避免在大目录上无谓地走完整 Counter。
        unique_names = set(file_names)
        has_duplicates = len(unique_names) != len(file_names)

        logger = get_logger()
        if has_duplicates:
            name_counts = Counter(file_names)
            dup_names = [name for name, count in name_counts.items() if count > 1]
            dup_name_set = set(dup_names)
            paths_by_name = {name: [] for name in dup_names}
            for file_info, file_name in zip(local_files, file_names):
                if file_name not in dup_name_set:
                    continue
                relative_path = str(file_info.get("relative_path") or "").lstrip("/")
                full_path = self.path_service.normalize_path(
                    posixpath.join(save_dir, relative_path)
                )
                paths_by_name[file_name].append(full_path)

            logger.info(f"检测到 {len(dup_names)} 个重复文件名：")
            for name in dup_names:
                duplicate_paths = paths_by_name[name]
                logger.info(f"  - {name} 出现 {len(duplicate_paths)} 次")
                for path in duplicate_paths:
                    logger.info(f"    {path}")
        else:
            logger.debug("没有发现重复文件名。")

        return {
            self.path_service.normalize_path(file_info["relative_path"]): file_info["md5"]
            for file_info in local_files
            if file_info.get("relative_path")
        }

    @staticmethod
    def _add_warning_sample(warning_samples, message, max_samples=5):
        return CandidateFilter.add_warning_sample(warning_samples, message, max_samples)

    @staticmethod
    def _is_verified_same_file(src_md5, local_md5):
        return CandidateFilter.is_verified_same_file(src_md5, local_md5)

    @staticmethod
    def _existing_conflict_message(path, src_md5, local_md5, prefix):
        return CandidateFilter.existing_conflict_message(path, src_md5, local_md5, prefix)

    def _transfer_item_local_entries(self, item):
        _, _, clean_path, final_path, need_rename = item
        src_md5 = getattr(item, "src_md5", None)
        entries = []
        clean_normalized = self.path_service.normalize_path(str(clean_path or "").lstrip("/"))
        if clean_normalized:
            entries.append((clean_normalized, src_md5))
        final_normalized = self.path_service.normalize_path(str(final_path or "").lstrip("/"))
        if need_rename and final_normalized and final_normalized != clean_normalized:
            entries.append((final_normalized, src_md5))
        return entries

    def _record_transfer_item_paths(self, item, local_files_dict):
        for normalized_path, src_md5 in self._transfer_item_local_entries(item):
            local_files_dict[normalized_path] = src_md5

    def _filter_planned_path_conflict(
        self,
        planned_paths,
        normalized_path,
        display_path,
        src_md5,
        summary,
        warning_samples,
        prefix,
    ):
        return self._candidate_filter().filter_planned_path_conflict(
            planned_paths,
            normalized_path,
            display_path,
            src_md5,
            summary,
            warning_samples,
            prefix,
        )

    def _filter_transfer_candidates_core(
        self,
        candidates,
        local_files_dict,
        summary,
        warning_samples,
        planned_paths=None,
    ):
        return self._candidate_filter().filter_candidates_core(
            candidates,
            local_files_dict,
            summary,
            warning_samples,
            planned_paths,
        )

    @staticmethod
    def _report_transfer_candidate_summary(summary, warning_samples, progress_callback=None):
        if not progress_callback:
            return
        CandidateFilter(None, ProgressReporter(progress_callback)).report_summary(
            summary,
            warning_samples,
        )

    def _filter_transfer_candidates(
        self,
        candidates,
        local_files_dict,
        summary,
        progress_callback=None,
    ):
        return self._candidate_filter(progress_callback).filter_candidates(
            candidates,
            local_files_dict,
            summary,
        )

    def _build_transfer_list(
        self,
        shared_files_info,
        shared_paths,
        target_dir,
        local_files_dict,
        regex_pattern=None,
        regex_replace=None,
        progress_callback=None,
    ):
        return self._candidate_filter(progress_callback).build_transfer_list(
            shared_files_info,
            shared_paths,
            target_dir,
            local_files_dict,
            regex_pattern,
            regex_replace,
        )

    def _clear_local_files_cache(self, target_dir, affected_relative_dirs=None):
        """清理指定 target_dir 下的本地文件 cache。

        Args:
            target_dir: 目标目录路径
            affected_relative_dirs: 本次只清理这些相对子目录下的 cache 条目；
                None 时全清（保留旧行为，用于失败重试等保守场景）
        """
        normalized_target_dir = self.path_service.normalize_path(target_dir)

        # 多链接并发下，迭代和 pop 必须互斥保护，防止 RuntimeError: dict changed size。
        # 测试中常用 BaiduStorage.__new__ 绕过 __init__，此时无锁；用 getattr 兜底。
        lock = getattr(self, "_shared_state_lock", None) or _NULL_LOCK
        with lock:
            if not affected_relative_dirs:
                # 全清：与历史行为一致
                cache_keys = [
                    key
                    for key in self._local_files_cache
                    if key == normalized_target_dir
                    or (isinstance(key, tuple) and key and key[0] == normalized_target_dir)
                ]
                for key in cache_keys:
                    self._local_files_cache.pop(key, None)
                return

            # 精确失效：仅清掉与受影响目录有交集的 cache key
            normalized_affected = {
                StoragePathService._normalize_relative_dir(d) for d in affected_relative_dirs
            }
            cache_keys = []
            for key in self._local_files_cache:
                # list_local_files 缓存的整目录条目
                if key == normalized_target_dir:
                    cache_keys.append(key)
                    continue
                # list_local_files_in_dirs 的 (target, relative_dirs_tuple, merge) 元组键
                if (
                    isinstance(key, tuple)
                    and len(key) >= 2
                    and key[0] == normalized_target_dir
                    and isinstance(key[1], tuple)
                ):
                    cache_relative_dirs = set(key[1])
                    if cache_relative_dirs & normalized_affected:
                        cache_keys.append(key)
            for key in cache_keys:
                self._local_files_cache.pop(key, None)

    @staticmethod
    def _transfer_item_key(item):
        fs_id, dir_path, clean_path, final_path, _ = item
        return (str(fs_id), dir_path or "", clean_path or "", final_path or "")

    def _affected_relative_dirs(self, transfer_list, target_dir):
        """从 transfer_list 与 target_dir 反推本批写入涉及的相对子目录集合。"""
        if not target_dir:
            return set()
        normalized_target = self.path_service.normalize_path(target_dir).rstrip("/")
        target_prefix = f"{normalized_target}/"
        affected = set()
        for item in transfer_list:
            _, dir_path, clean_path, final_path, _ = item[:5]
            for path in (clean_path, final_path):
                affected.update(self._candidate_parent_dirs(path))
            # dir_path 是绝对路径，提取它相对 target_dir 的部分
            if dir_path:
                normalized_dir = self.path_service.normalize_path(dir_path)
                if normalized_dir == normalized_target:
                    affected.add("")
                elif normalized_dir.startswith(target_prefix):
                    affected.add(normalized_dir[len(target_prefix):])
        return affected

    def _build_transfer_failed_record(self, item, error_info):
        fs_id, dir_path, clean_path, final_path, _ = item
        return {
            "fs_id": fs_id,
            "dir_path": dir_path,
            "clean_path": clean_path,
            "final_path": final_path,
            "error": error_info.message,
            "error_code": error_info.code,
            "error_kind": error_info.kind,
            "retryable": error_info.retryable,
            "temporary": is_storage_temporary_error_info(error_info),
            "failed_at": int(time.time()),
        }

    def _split_existing_transfer_items(
        self,
        items,
        target_dir,
        progress_callback=None,
        scan_cache=None,
        force_refresh=False,
    ):
        if not target_dir:
            return [], list(items)

        relative_dirs = set()
        for _, _, clean_path, final_path, _ in items:
            relative_dirs.update(self._candidate_parent_dirs(clean_path, final_path))
        normalized_relative_dirs = tuple(
            sorted(
                "" if relative_dir in ("", ".") else relative_dir
                for relative_dir in (
                    str(relative_dir or "").replace("\\", "/").strip("/")
                    for relative_dir in relative_dirs
                )
            )
        )
        normalized_target_dir = self.path_service.normalize_path(target_dir)
        cache_key = (normalized_target_dir, normalized_relative_dirs, True)
        if force_refresh and scan_cache is not None:
            scan_cache.clear()
        if not force_refresh and scan_cache is not None and cache_key in scan_cache:
            local_files_dict = scan_cache[cache_key]
        else:
            if force_refresh:
                self._clear_local_files_cache(target_dir)
            local_files_dict = self._scan_local_files_dict(
                target_dir, progress_callback, set(normalized_relative_dirs)
            )
            if scan_cache is not None:
                scan_cache[cache_key] = local_files_dict

        existing_items = []
        missing_items = []
        for item in items:
            fs_id, dir_path, clean_path, final_path, need_rename = item
            src_md5 = getattr(item, "src_md5", None)
            if not src_md5:
                missing_items.append(item)
                continue

            clean_normalized = self.path_service.normalize_path(
                str(clean_path or "").lstrip("/")
            )
            final_normalized = (
                self.path_service.normalize_path(str(final_path or "").lstrip("/"))
                if need_rename
                else clean_normalized
            )
            clean_md5 = local_files_dict.get(clean_normalized)
            final_md5 = (
                clean_md5
                if final_normalized == clean_normalized
                else local_files_dict.get(final_normalized)
            )
            clean_verified = clean_md5 == src_md5
            final_verified = final_md5 == src_md5

            if need_rename and final_verified:
                existing_items.append(
                    TransferItem(fs_id, dir_path, final_path, final_path, False, src_md5)
                )
            elif clean_verified or (not need_rename and final_verified):
                existing_items.append(item)
            else:
                missing_items.append(item)

        return existing_items, missing_items

    def _ensure_transfer_dirs(self, transfer_list):
        created_dirs = set()
        for _, dir_path, _, _, _ in transfer_list:
            if dir_path in created_dirs:
                continue
            if not self.path_service.ensure_dir_exists(dir_path):
                self._notify_error(
                    ValueError(f"创建目录失败: {dir_path}"),
                    f"创建目录失败: {dir_path}",
                )
                return {"success": False, "error": f"创建目录失败: {dir_path}"}
            created_dirs.add(dir_path)
        return None

    def _transfer_group(
        self,
        dir_path,
        fs_ids,
        share_url,
        uk,
        share_id,
        bdstoken,
        progress_callback=None,
    ):
        dir_path = dir_path.replace("\\", "/")
        if progress_callback:
            progress_callback("info", f"转存到目录 {dir_path} ({len(fs_ids)} 个文件)")

        if not (self.client and uk is not None and share_id is not None and bdstoken is not None):
            raise ValueError("转存失败: 客户端或参数无效")

        self.client.transfer_shared_paths(
            remotedir=dir_path,
            fs_ids=fs_ids,
            uk=int(uk),
            share_id=int(share_id),
            bdstoken=str(bdstoken),
            shared_url=share_url,
        )
        return dir_path

    def _execute_transfer_plan(
        self,
        transfer_list,
        share_url,
        uk,
        share_id,
        bdstoken,
        target_dir,
        progress_callback=None,
    ):
        return storage_transfer_plan.execute_transfer_plan(
            self,
            transfer_list,
            share_url,
            uk,
            share_id,
            bdstoken,
            target_dir,
            progress_callback,
        )

    def _rename_one_transferred_file(self, dir_path, clean_path, final_path, target_dir, progress_callback=None):
        return storage_rename.rename_one_transferred_file(
            self, dir_path, clean_path, final_path, target_dir, progress_callback
        )

    def _rename_transferred_files(self, successful_transfer_items, target_dir, progress_callback=None):
        return storage_rename.rename_transferred_files(
            self, successful_transfer_items, target_dir, progress_callback
        )

    def _record_rename_failure(
        self, clean_path, final_path, exc, rename_failed_files, progress_callback
    ):
        return storage_rename.record_rename_failure(
            self, clean_path, final_path, exc, rename_failed_files, progress_callback
        )

    def _build_transfer_result(
        self,
        transfer_success_count,
        total_files,
        rename_result,
        progress_callback=None,
        transfer_failed_files=None,
    ):
        renamed_files = rename_result.get("transferred_files", [])
        rename_failed_files = rename_result.get("rename_failed_files", [])
        rename_failed_count = rename_result.get("rename_failed_count", 0)
        completed_count = rename_result.get("completed_count", 0)
        transfer_failed_files = transfer_failed_files or []
        transfer_failed_count = len(transfer_failed_files)

        # 统一基础结构，所有分支都包含完整 key 集合，调用方无需 .get() 防御
        base = {
            "success": False,
            "partial": False,
            "message": "",
            "error": "",
            "transferred_files": renamed_files,
            "transfer_failed_files": transfer_failed_files,
            "transfer_failed_count": transfer_failed_count,
            "rename_failed_files": rename_failed_files,
            "rename_failed_count": rename_failed_count,
            "completed_count": completed_count,
            "transfer_success_count": transfer_success_count,
        }

        if completed_count == total_files and transfer_failed_count == 0:
            message = f"成功转存 {completed_count}/{total_files} 个文件"
            if progress_callback:
                progress_callback("success", f"转存完成，{message}")
            base.update({"success": True, "message": message})
            return base

        if completed_count > 0 or transfer_success_count > 0:
            message = f"部分转存成功，成功完成 {completed_count}/{total_files} 个文件"
            failed_parts = []
            if transfer_failed_count > 0:
                failed_parts.append(f"另有 {transfer_failed_count} 个文件转存失败")
            if rename_failed_count > 0:
                failed_parts.append(f"另有 {rename_failed_count} 个文件转存后重命名失败")
            if failed_parts:
                message = f"{message}，" + "，".join(failed_parts)
            if progress_callback:
                progress_callback("warning", message)
            base.update({"partial": True, "message": message, "error": message})
            return base

        error = "转存失败，没有文件成功转存"
        if transfer_failed_count > 0:
            error = f"转存失败，{transfer_failed_count}/{total_files} 个文件转存失败"
        self._notify_error(ValueError(error), error)
        base.update({"error": error})
        return base

    @staticmethod
    def _can_try_dir_fast_path(
        context, save_dir, regex_pattern=None, regex_replace=None, folder_filter=None
    ):
        if not save_dir or regex_pattern or regex_replace or folder_filter:
            return False
        shared_paths = context.get("shared_paths") or []
        if len(shared_paths) != 1:
            return False
        shared_path = shared_paths[0]
        return bool(getattr(shared_path, "is_dir", False) and getattr(shared_path, "fs_id", None))

    def _target_child_exists(self, dir_path, child_name):
        if not child_name:
            return False
        for item in self.client.list(dir_path):
            item_name = os.path.basename(str(getattr(item, "path", "")).rstrip("/"))
            if item_name == child_name:
                return True
        return False

    @staticmethod
    def _dir_fast_path_target_dir(context, save_dir):
        shared_path = context["shared_paths"][0]
        folder_name = os.path.basename(str(getattr(shared_path, "path", "")).rstrip("/"))
        return posixpath.join(save_dir, folder_name) if folder_name else save_dir

    def _dir_tree_traverser(self, progress_callback=None):
        def notify_error(error, context_message, collect=True):
            self._notify_error(error, context_message, collect=collect)

        return DirTreeTraverser(
            self.path_service,
            self.share_service,
            BaiduStorageDirTransferExecutor(self, progress_callback),
            ProgressReporter(progress_callback),
            notify_error,
        )

    def _dir_tree_traverser_with_legacy_flush(self, progress_callback=None):
        traverser = self._dir_tree_traverser(progress_callback)

        def flush_file_batch(file_transfer_list, target_dir, context, share_url, stats):
            return self._flush_dir_tree_file_batch(
                file_transfer_list,
                target_dir,
                context,
                share_url,
                stats,
                progress_callback,
            )

        traverser.flush_file_batch = flush_file_batch
        return traverser

    @staticmethod
    def _new_dir_tree_divide_stats():
        return DirTreeTraverser.new_stats()

    @staticmethod
    def _build_dir_tree_divide_result(stats, progress_callback=None):
        return DirTreeTraverser(
            None,
            None,
            None,
            ProgressReporter(progress_callback),
        ).build_result(stats)

    def _flush_dir_tree_file_batch(
        self,
        file_transfer_list,
        target_dir,
        context,
        share_url,
        stats,
        progress_callback=None,
    ):
        return self._dir_tree_traverser(progress_callback).flush_file_batch(
            file_transfer_list,
            target_dir,
            context,
            share_url,
            stats,
        )

    def _initialize_dir_tree_frame(self, frame, context, stats, progress_callback=None):
        return self._dir_tree_traverser(progress_callback).initialize_frame(
            frame,
            context,
            stats,
        )

    def _finish_dir_tree_frame(self, frame, context, share_url, stats, progress_callback=None):
        return self._dir_tree_traverser_with_legacy_flush(progress_callback).finish_frame(
            frame,
            context,
            share_url,
            stats,
        )

    def _handle_dir_tree_iter_error(
        self, frame, context, share_url, stats, exc, progress_callback=None
    ):
        return self._dir_tree_traverser_with_legacy_flush(
            progress_callback
        ).handle_iter_error(
            frame,
            context,
            share_url,
            stats,
            exc,
        )

    def _handle_dir_tree_file_child(
        self, frame, child, context, share_url, stats, progress_callback=None
    ):
        return self._dir_tree_traverser_with_legacy_flush(
            progress_callback
        ).handle_file_child(
            frame,
            child,
            context,
            share_url,
            stats,
        )

    def _handle_dir_tree_dir_child(
        self,
        stack,
        frame,
        child,
        context,
        share_url,
        exclude_folder_filter,
        stats,
        progress_callback=None,
    ):
        return self._dir_tree_traverser_with_legacy_flush(
            progress_callback
        ).handle_dir_child(
            stack,
            frame,
            child,
            context,
            share_url,
            exclude_folder_filter,
            stats,
        )

    def _transfer_dir_tree_divide_collect(
        self,
        shared_dir,
        target_dir,
        context,
        share_url,
        exclude_folder_filter,
        stats,
        progress_callback=None,
    ):
        traverser = self._dir_tree_traverser(progress_callback)

        def flush_file_batch(file_transfer_list, target_dir, context, share_url, stats):
            return self._flush_dir_tree_file_batch(
                file_transfer_list,
                target_dir,
                context,
                share_url,
                stats,
                progress_callback,
            )

        def initialize_frame(frame, context, stats):
            return self._initialize_dir_tree_frame(
                frame,
                context,
                stats,
                progress_callback,
            )

        def finish_frame(frame, context, share_url, stats):
            return self._finish_dir_tree_frame(
                frame,
                context,
                share_url,
                stats,
                progress_callback,
            )

        def handle_iter_error(frame, context, share_url, stats, exc):
            return self._handle_dir_tree_iter_error(
                frame,
                context,
                share_url,
                stats,
                exc,
                progress_callback,
            )

        def handle_file_child(frame, child, context, share_url, stats):
            return self._handle_dir_tree_file_child(
                frame,
                child,
                context,
                share_url,
                stats,
                progress_callback,
            )

        def handle_dir_child(
            stack,
            frame,
            child,
            context,
            share_url,
            exclude_folder_filter,
            stats,
        ):
            return self._handle_dir_tree_dir_child(
                stack,
                frame,
                child,
                context,
                share_url,
                exclude_folder_filter,
                stats,
                progress_callback,
            )

        traverser.flush_file_batch = flush_file_batch
        traverser.initialize_frame = initialize_frame
        traverser.finish_frame = finish_frame
        traverser.handle_iter_error = handle_iter_error
        traverser.handle_file_child = handle_file_child
        traverser.handle_dir_child = handle_dir_child
        return traverser.collect(
            shared_dir,
            target_dir,
            context,
            share_url,
            exclude_folder_filter,
            stats,
        )

    def _transfer_dir_tree_divide(
        self,
        shared_dir,
        target_dir,
        context,
        share_url,
        exclude_folder_filter,
        progress_callback=None,
    ):
        stats = self._new_dir_tree_divide_stats()
        folder_name = os.path.basename(
            str(getattr(shared_dir, "path", shared_dir)).rstrip("/")
        )
        if should_exclude_folder(folder_name, exclude_folder_filter):
            stats["skipped_dir_count"] = 1
            ProgressReporter(progress_callback).report(
                "info", f"跳过排除目录: {folder_name}"
            )
            return self._build_dir_tree_divide_result(stats, progress_callback)

        self._transfer_dir_tree_divide_collect(
            shared_dir,
            target_dir,
            context,
            share_url,
            exclude_folder_filter,
            stats,
            progress_callback,
        )
        return self._build_dir_tree_divide_result(stats, progress_callback)

    def _try_transfer_dir_fast_path(
        self,
        context,
        share_url,
        save_dir,
        exclude_folder_filter=None,
        progress_callback=None,
    ):
        shared_path = context["shared_paths"][0]
        folder_name = os.path.basename(str(getattr(shared_path, "path", "")).rstrip("/"))
        if progress_callback:
            progress_callback("info", f"尝试整目录直接转存: {folder_name or shared_path.path}")

        if not self.path_service.ensure_dir_exists(save_dir):
            return {"success": False, "error": f"创建目录失败: {save_dir}"}
        try:
            target_exists = self._target_child_exists(save_dir, folder_name)
        except Exception:
            if progress_callback:
                progress_callback("warning", "目标目录探测失败，回退逐文件对比转存")
            return None
        if target_exists:
            if progress_callback:
                progress_callback("info", "目标目录已存在，回退逐文件对比转存")
            return None

        divide_target_dir = self._dir_fast_path_target_dir(context, save_dir)
        if exclude_folder_filter:
            if progress_callback:
                progress_callback("info", "检测到排除目录规则，改用目录分治转存")
            return self._transfer_dir_tree_divide(
                shared_path,
                divide_target_dir,
                context,
                share_url,
                exclude_folder_filter,
                progress_callback,
            )

        max_attempts = 2
        for attempt in range(max_attempts):
            try:
                self._transfer_group(
                    save_dir,
                    [getattr(shared_path, "fs_id")],
                    share_url,
                    context["uk"],
                    context["share_id"],
                    context["bdstoken"],
                    None,
                )
                break
            except Exception as exc:
                if is_transfer_count_limit_error(exc):
                    if progress_callback:
                        progress_callback(
                            "warning",
                            "整目录直接转存超量，改用目录分治转存",
                        )
                    return self._transfer_dir_tree_divide(
                        shared_path,
                        divide_target_dir,
                        context,
                        share_url,
                        exclude_folder_filter,
                        progress_callback,
                    )
                error_info = classify_storage_error(exc)
                if error_info.retryable and attempt < max_attempts - 1:
                    if progress_callback:
                        progress_callback("warning", f"整目录直接转存失败，准备重试: {error_info.message}")
                    time.sleep(TRANSFER_FAILED_RETRY_DELAY)
                    continue
                try:
                    if self._target_child_exists(save_dir, folder_name):
                        if progress_callback:
                            progress_callback("warning", "整目录直接转存失败但目标目录已存在，回退逐文件对比转存")
                        return None
                except Exception:
                    pass
                raise

        self._clear_local_files_cache(save_dir, {folder_name} if folder_name else None)
        message = f"整目录直接转存成功: {folder_name or shared_path.path}"
        if progress_callback:
            progress_callback("success", message)
        return {
            "success": True,
            "partial": False,
            "fast_path": True,
            "message": message,
            "transferred_files": [folder_name or str(getattr(shared_path, "path", ""))],
            "completed_count": 1,
            "transfer_success_count": 1,
            "rename_failed_count": 0,
        }

    @staticmethod
    def _put_stream_queue_item(stream_queue, item, stop_event):
        return storage_streaming.put_stream_queue_item(stream_queue, item, stop_event)

    def _transfer_share_streaming(
        self,
        context,
        share_url,
        transfer_target_dir,
        regex_pattern=None,
        regex_replace=None,
        folder_filter=None,
        exclude_folder_filter=None,
        progress_callback=None,
    ):
        return storage_streaming.transfer_share_streaming(
            self,
            context,
            share_url,
            transfer_target_dir,
            regex_pattern,
            regex_replace,
            folder_filter,
            exclude_folder_filter,
            progress_callback,
        )

    def transfer_share(
        self,
        share_url,
        pwd=None,
        save_dir=None,
        progress_callback=None,
        regex_pattern=None,
        regex_replace=None,
        folder_filter=None,
        exclude_folder_filter=None,
    ):
        """转存分享文件"""
        masked_share_url = mask_share_url(share_url) or share_url
        with ErrorCollector(
            f"转存分享文件: {masked_share_url}", self.wechat_notifier, None, auto_send=False
        ):
            if not self.client:
                error_msg = "客户端未初始化或初始化失败"
                self._notify_error(
                    ValueError(error_msg),
                    "转存分享文件: 客户端不可用",
                    extra_info=f"分享链接: {masked_share_url}",
                )
                return {"success": False, "error": error_msg}

            save_dir = self._normalize_save_dir(save_dir)

            try:
                context = self._load_share_entries(share_url, pwd, progress_callback)
                if not context:
                    return {"success": False, "error": "获取分享文件列表失败"}

                transfer_target_dir = save_dir
                if self._can_try_dir_fast_path(
                    context, save_dir, regex_pattern, regex_replace, folder_filter
                ):
                    fast_path_result = self._try_transfer_dir_fast_path(
                        context,
                        share_url,
                        save_dir,
                        exclude_folder_filter,
                        progress_callback,
                    )
                    if fast_path_result is not None:
                        return fast_path_result
                    transfer_target_dir = self._dir_fast_path_target_dir(context, save_dir)

                return self._transfer_share_streaming(
                    context,
                    share_url,
                    transfer_target_dir,
                    regex_pattern,
                    regex_replace,
                    folder_filter,
                    exclude_folder_filter,
                    progress_callback,
                )
            except Exception as e:
                return {"success": False, "error": parse_share_error(e)}

    def get_share_folder_name(self, share_url, pwd=None):
        """获取分享链接的主文件夹名称"""
        masked_share_url = mask_share_url(share_url) or share_url
        try:
            if not self.client:
                error_msg = "客户端未初始化或初始化失败"
                self._notify_error(
                    ValueError(error_msg),
                    "获取分享文件夹名称失败: 客户端不可用",
                )
                return {"success": False, "error": error_msg}

            shared_paths = self.share_service.load_shared_paths(share_url, pwd)
            if not shared_paths:
                error_msg = "获取分享文件列表失败"
                self._notify_error(
                    ValueError(error_msg),
                    "获取分享文件列表失败",
                    extra_info=f"分享链接: {masked_share_url}",
                )
                return {"success": False, "error": error_msg}

            if len(shared_paths) == 1 and hasattr(shared_paths[0], "is_dir") and shared_paths[0].is_dir:
                folder_name = os.path.basename(shared_paths[0].path)
                return {"success": True, "folder_name": folder_name}

            first_item = shared_paths[0]
            if hasattr(first_item, "is_dir") and first_item.is_dir:
                folder_name = os.path.basename(first_item.path)
            else:
                folder_name = os.path.splitext(os.path.basename(first_item.path))[0]
            return {"success": True, "folder_name": folder_name}

        except Exception as e:
            self._notify_error(
                e,
                "获取分享文件夹名称时发生异常",
                extra_info=f"分享链接: {masked_share_url}",
            )
            return {"success": False, "error": parse_share_error(e)}
