#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from collections import Counter
from dataclasses import dataclass, field

import os
import posixpath
import queue
import threading
import time

# 添加 WeChatNotifier 和工具方法导入
from wechat_notifier import WeChatNotifier
from utils import handle_error_and_notify, ErrorCollector, mask_share_url
from config_utils import build_retry_share_config, parse_share_links_from_text
from storage_client import BaiduClientAdapter
from storage_errors import (
    classify_storage_error,
    is_rate_limit_error,
    is_storage_temporary_error_info,
    is_transfer_count_limit_error,
    parse_share_error,
)
from storage_paths import StoragePathService
from storage_rules import (
    REGEX_FILTER_UNMATCHED,
    REGEX_FILTER_UNSAFE_REPLACE,
    apply_regex_rules_detail,
    is_safe_relative_target_path,
    should_exclude_folder,
)
from storage_shares import SharedPathService

try:
    from logger import get_logger
except ImportError:
    # 日志模块不可用时，使用标准日志
    import logging

    def get_logger(name="transfershare"):
        return logging.getLogger(name)


def _read_non_negative_float_env(name, default):
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _read_positive_int_env(name, default):
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


# 常量定义
RATE_LIMIT_WAIT_TIME = 10
RENAME_DELAY = _read_non_negative_float_env("TRANSFERSHARE_RENAME_DELAY", 0.5)
BATCH_SHARE_DELAY = _read_non_negative_float_env("TRANSFERSHARE_BATCH_SHARE_DELAY", 2)
TRANSFER_BATCH_SIZE = _read_positive_int_env("TRANSFERSHARE_TRANSFER_BATCH_SIZE", 999)
TRANSFER_FAILED_RETRY_ATTEMPTS = _read_positive_int_env(
    "TRANSFERSHARE_TRANSFER_FAILED_RETRY_ATTEMPTS", 2
)
TRANSFER_FAILED_RETRY_DELAY = _read_non_negative_float_env(
    "TRANSFERSHARE_TRANSFER_FAILED_RETRY_DELAY", 5
)
STREAM_PRODUCER_JOIN_TIMEOUT = _read_non_negative_float_env(
    "TRANSFERSHARE_STREAM_PRODUCER_JOIN_TIMEOUT", 5
)


@dataclass(frozen=True, eq=False)
class TransferItem:
    fs_id: int
    dir_path: str
    clean_path: str
    final_path: str
    need_rename: bool
    src_md5: str = None
    _payload: tuple = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(
            self,
            "_payload",
            (
                self.fs_id,
                self.dir_path,
                self.clean_path,
                self.final_path,
                self.need_rename,
            ),
        )

    def as_tuple(self):
        return self._payload

    def __iter__(self):
        return iter(self.as_tuple())

    def __len__(self):
        return 5

    def __getitem__(self, index):
        return self.as_tuple()[index]

    def count(self, value):
        return self.as_tuple().count(value)

    def index(self, value, *args):
        return self.as_tuple().index(value, *args)

    def __eq__(self, other):
        if isinstance(other, TransferItem):
            return self.as_tuple() == other.as_tuple()
        if isinstance(other, tuple):
            return self.as_tuple() == other
        return False

    def __hash__(self):
        return hash(self.as_tuple())


@dataclass
class _DirTreeFrame:
    shared_dir: object
    target_dir: str
    child_iter: object = None
    child_count: int = 0
    file_transfer_list: list = field(default_factory=list)

    @property
    def shared_dir_path(self):
        return getattr(self.shared_dir, "path", self.shared_dir)


class BaiduStorage:
    def __init__(self, cookies, wechat_webhook=None):
        self.wechat_notifier = (
            WeChatNotifier(wechat_webhook) if wechat_webhook else None
        )
        self._local_files_cache = {}
        self.client = BaiduClientAdapter(cookies)
        self.path_service = StoragePathService(
            self.client, self.wechat_notifier, self._local_files_cache
        )
        self.share_service = SharedPathService(self.client, self.wechat_notifier)

    def set_notifier(self, notifier):
        """设置微信通知器实例

        Args:
            notifier: WeChatNotifier 实例或 None
        """
        self.wechat_notifier = notifier
        self.path_service.wechat_notifier = notifier
        self.share_service.wechat_notifier = notifier

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
            handle_error_and_notify(
                e,
                "获取网盘配额信息时发生异常",
                self.wechat_notifier,
                None,
                collect=False,
            )
            return None

    def is_valid(self):
        """检查存储是否可用"""
        try:
            if not self.client:
                return False

            quota_info = self.get_quota_info()
            return bool(quota_info)

        except Exception as e:
            handle_error_and_notify(
                e, "检查存储可用性时发生异常", self.wechat_notifier, None, collect=False
            )
            return False

    def _build_invalid_share_result(self, index, config):
        error_msg = f"第 {index} 个配置格式错误：缺少分享链接"
        invalid_share_url = (
            config.get("share_url", "未知") if isinstance(config, dict) else str(config)
        )
        handle_error_and_notify(
            ValueError(error_msg),
            f"批量转存配置错误: 第 {index} 个配置格式错误",
            self.wechat_notifier,
            None,
            collect=True,
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
        handle_error_and_notify(
            ValueError(detailed_error),
            f"批量转存单个链接{'部分成功' if partial else '失败'}: 第 {index} 个链接",
            self.wechat_notifier,
            None,
            collect=True,
        )

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
                handle_error_and_notify(
                    ValueError(error_msg),
                    "批量转存配置错误",
                    self.wechat_notifier,
                    None,
                    collect=True,
                )
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

            self._local_files_cache.clear()
            total_count = len(share_configs)
            counters = {
                "success_count": 0,
                "partial_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
            }
            results = []

            for index, config in enumerate(share_configs, 1):
                try:
                    result_record = self._process_single_share_config(
                        index, total_count, config, progress_callback
                    )
                    self._record_batch_result(counters, result_record)
                    results.append(result_record)
                    if index < total_count:
                        time.sleep(BATCH_SHARE_DELAY)
                except Exception as e:
                    error_info = classify_storage_error(e)
                    error_msg = f"处理第 {index} 个分享链接时发生异常: {error_info.message}"
                    share_url = config.get("share_url", "未知") if isinstance(config, dict) else "未知"
                    masked_share_url = mask_share_url(share_url) or share_url
                    results.append(
                        {
                            "index": index,
                            "share_url": masked_share_url,
                            "success": False,
                            "partial": False,
                            "error": error_msg,
                        }
                    )
                    counters["failed_count"] += 1
                    self._notify_batch_progress(
                        "error", index, total_count, f"异常: {error_info.message}", progress_callback
                    )
                    handle_error_and_notify(
                        e,
                        f"处理第 {index} 个分享链接时发生异常\n分享链接: {masked_share_url}",
                        self.wechat_notifier,
                        None,
                        collect=True,
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
                    handle_error_and_notify(
                        ValueError(error_msg),
                        "解析分享链接失败",
                        self.wechat_notifier,
                        None,
                        collect=True,
                    )
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
                handle_error_and_notify(
                    e,
                    "从文本转存失败",
                    self.wechat_notifier,
                    None,
                    collect=True,
                )
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

    def _load_share_entries(self, share_url, pwd, progress_callback=None):
        masked_share_url = mask_share_url(share_url) or share_url
        if progress_callback:
            progress_callback("info", f"【步骤1/4】访问分享链接: {masked_share_url}")
        if pwd and progress_callback:
            progress_callback("info", "使用密码访问分享链接")

        shared_paths = self.share_service.load_shared_paths(share_url, pwd)
        if not shared_paths:
            handle_error_and_notify(
                ValueError("获取分享文件列表失败"),
                "获取分享文件列表失败",
                self.wechat_notifier,
                None,
                collect=True,
            )
            return None

        return {
            "shared_paths": shared_paths,
            "uk": shared_paths[0].uk,
            "share_id": shared_paths[0].share_id,
            "bdstoken": shared_paths[0].bdstoken,
        }

    def _load_share_files(
        self,
        context,
        folder_filter,
        progress_callback=None,
        exclude_folder_filter=None,
    ):
        if progress_callback:
            progress_callback("info", "开始获取共享文件列表")
        shared_files_info = self.share_service.list_shared_files(
            context["shared_paths"],
            folder_filter,
            progress_callback,
            exclude_folder_filter=exclude_folder_filter,
        )

        if progress_callback:
            progress_callback("info", f"获取到 {len(shared_files_info)} 个共享文件")

        context = dict(context)
        context["shared_files_info"] = shared_files_info
        return context

    def _load_share_context(
        self,
        share_url,
        pwd,
        folder_filter,
        progress_callback=None,
        exclude_folder_filter=None,
    ):
        context = self._load_share_entries(share_url, pwd, progress_callback)
        if not context:
            return None
        return self._load_share_files(
            context,
            folder_filter,
            progress_callback,
            exclude_folder_filter=exclude_folder_filter,
        )

    @staticmethod
    def _candidate_parent_dirs(*paths):
        relative_dirs = set()
        for path in paths:
            normalized_path = str(path or "").replace("\\", "/").lstrip("/")
            parent_dir = posixpath.dirname(normalized_path)
            relative_dirs.add("" if parent_dir in ("", ".") else parent_dir)
        return relative_dirs

    def _prepare_transfer_candidates(
        self,
        shared_files_info,
        shared_paths,
        target_dir,
        regex_pattern=None,
        regex_replace=None,
    ):
        is_single_folder = len(shared_paths) == 1 and shared_paths[0].is_dir
        candidates = []
        relative_dirs = set()
        summary = Counter(
            {
                "shared_count": len(shared_files_info),
                "regex_filtered_count": 0,
                "regex_unmatched_count": 0,
                "unsafe_regex_replace_count": 0,
                "candidate_count": 0,
                "rename_candidate_count": 0,
            }
        )

        for file_info in shared_files_info:
            clean_path = file_info["path"]
            if is_single_folder and "/" in clean_path:
                clean_path = "/".join(clean_path.split("/")[1:])

            should_transfer, final_path, filter_reason = apply_regex_rules_detail(
                clean_path, regex_pattern, regex_replace
            )
            if not should_transfer:
                summary["regex_filtered_count"] += 1
                if filter_reason == REGEX_FILTER_UNMATCHED:
                    summary["regex_unmatched_count"] += 1
                elif filter_reason == REGEX_FILTER_UNSAFE_REPLACE:
                    summary["unsafe_regex_replace_count"] += 1
                continue

            clean_normalized = self.path_service.normalize_path(clean_path.lstrip("/"))
            final_normalized = self.path_service.normalize_path(final_path.lstrip("/"))
            need_rename = final_path != clean_path
            if need_rename:
                summary["rename_candidate_count"] += 1

            dir_path = None
            if target_dir is not None and clean_path is not None:
                target_path = posixpath.join(target_dir, clean_path.lstrip("/"))
                dir_path = posixpath.dirname(target_path).replace("\\", "/")
                relative_dirs.update(self._candidate_parent_dirs(clean_path, final_path))

            candidates.append(
                {
                    "fs_id": file_info["fs_id"],
                    "clean_path": clean_path,
                    "final_path": final_path,
                    "clean_normalized": clean_normalized,
                    "final_normalized": final_normalized,
                    "need_rename": need_rename,
                    "src_md5": file_info.get("md5") if isinstance(file_info, dict) else None,
                    "dir_path": dir_path,
                }
            )
            summary["candidate_count"] += 1

        return candidates, summary, relative_dirs

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
        name_counts = Counter(file_names)
        dup_names = [name for name, count in name_counts.items() if count > 1]

        logger = get_logger()
        if dup_names:
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
            logger.info("没有发现重复文件名。")

        return {
            self.path_service.normalize_path(file_info["relative_path"]): file_info["md5"]
            for file_info in local_files
            if file_info.get("relative_path")
        }

    @staticmethod
    def _add_warning_sample(warning_samples, message, max_samples=5):
        if len(warning_samples) < max_samples:
            warning_samples.append(message)

    @staticmethod
    def _is_verified_same_file(src_md5, local_md5):
        return bool(src_md5 and local_md5 and src_md5 == local_md5)

    @staticmethod
    def _existing_conflict_message(path, src_md5, local_md5, prefix):
        if src_md5 and local_md5:
            return f"{prefix}已存在,但内容不同(md5不同),跳过： {path}"
        return f"{prefix}已存在,但缺少MD5无法确认是否相同,跳过： {path}"

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
        if planned_paths is None or normalized_path not in planned_paths:
            return False
        planned_md5 = planned_paths.get(normalized_path)
        summary["existing_count"] += 1
        if self._is_verified_same_file(src_md5, planned_md5):
            return True
        summary["conflict_count"] += 1
        self._add_warning_sample(
            warning_samples,
            self._existing_conflict_message(display_path, src_md5, planned_md5, prefix),
        )
        return True

    def _filter_transfer_candidates_core(
        self,
        candidates,
        local_files_dict,
        summary,
        warning_samples,
        planned_paths=None,
    ):
        transfer_list = []

        for candidate in candidates:
            clean_path = candidate["clean_path"]
            final_path = candidate["final_path"]
            need_rename = candidate["need_rename"]
            src_md5 = candidate["src_md5"]
            source_md5 = local_files_dict.get(candidate["clean_normalized"])
            target_md5 = local_files_dict.get(candidate["final_normalized"])
            source_exists = candidate["clean_normalized"] in local_files_dict
            target_exists = candidate["final_normalized"] in local_files_dict

            if not need_rename:
                if self._filter_planned_path_conflict(
                    planned_paths,
                    candidate["clean_normalized"],
                    clean_path,
                    src_md5,
                    summary,
                    warning_samples,
                    "本轮同路径",
                ):
                    continue
                if source_exists:
                    summary["existing_count"] += 1
                    if self._is_verified_same_file(src_md5, source_md5):
                        continue
                    summary["conflict_count"] += 1
                    self._add_warning_sample(
                        warning_samples,
                        self._existing_conflict_message(
                            final_path, src_md5, source_md5, "同路径"
                        ),
                    )
                    continue
            elif self._filter_planned_path_conflict(
                planned_paths,
                candidate["final_normalized"],
                final_path,
                src_md5,
                summary,
                warning_samples,
                "本轮重命名目标",
            ):
                continue
            elif self._filter_planned_path_conflict(
                planned_paths,
                candidate["clean_normalized"],
                clean_path,
                src_md5,
                summary,
                warning_samples,
                "本轮源路径",
            ):
                continue
            elif target_exists:
                summary["existing_count"] += 1
                if self._is_verified_same_file(src_md5, target_md5):
                    continue
                summary["conflict_count"] += 1
                self._add_warning_sample(
                    warning_samples,
                    self._existing_conflict_message(
                        final_path, src_md5, target_md5, "重命名目标"
                    ),
                )
                continue
            elif source_exists:
                summary["existing_count"] += 1
                if self._is_verified_same_file(src_md5, source_md5):
                    continue
                summary["conflict_count"] += 1
                self._add_warning_sample(
                    warning_samples,
                    self._existing_conflict_message(
                        clean_path, src_md5, source_md5, "源路径"
                    ),
                )
                continue

            if candidate["dir_path"] is None or clean_path is None:
                continue

            transfer_item = TransferItem(
                candidate["fs_id"],
                candidate["dir_path"],
                clean_path,
                final_path,
                need_rename,
                src_md5,
            )
            if planned_paths is not None:
                clean_normalized = candidate["clean_normalized"]
                final_normalized = candidate["final_normalized"]
                if clean_normalized:
                    planned_paths[clean_normalized] = src_md5
                if need_rename and final_normalized and final_normalized != clean_normalized:
                    planned_paths[final_normalized] = src_md5
            transfer_list.append(transfer_item)
            summary["transfer_needed_count"] += 1
            if need_rename:
                summary["rename_needed_count"] += 1

        return transfer_list

    @staticmethod
    def _report_transfer_candidate_summary(summary, warning_samples, progress_callback=None):
        if not progress_callback:
            return
        regex_detail = ""
        if summary["regex_unmatched_count"] or summary["unsafe_regex_replace_count"]:
            regex_detail = (
                f"（未匹配 {summary['regex_unmatched_count']} 个，"
                f"不安全替换 {summary['unsafe_regex_replace_count']} 个）"
            )
        progress_callback(
            "info",
            "候选分析完成："
            f"共享文件 {summary['shared_count']} 个，候选 {summary['candidate_count']} 个，"
            f"正则过滤 {summary['regex_filtered_count']} 个{regex_detail}，"
            f"本地已存在 {summary['existing_count']} 个，冲突跳过 {summary['conflict_count']} 个，"
            f"需要转存 {summary['transfer_needed_count']} 个，其中需重命名 {summary['rename_needed_count']} 个",
        )
        for message in warning_samples:
            progress_callback("warning", message)

    def _filter_transfer_candidates(
        self,
        candidates,
        local_files_dict,
        summary,
        progress_callback=None,
    ):
        if progress_callback:
            progress_callback("info", "【步骤3/4】准备转存: 对比文件和准备目录")

        warning_samples = []
        transfer_list = self._filter_transfer_candidates_core(
            candidates, local_files_dict, summary, warning_samples, {}
        )
        self._report_transfer_candidate_summary(
            summary, warning_samples, progress_callback
        )
        return transfer_list

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
        candidates, summary, _ = self._prepare_transfer_candidates(
            shared_files_info,
            shared_paths,
            target_dir,
            regex_pattern,
            regex_replace,
        )
        return self._filter_transfer_candidates(
            candidates, local_files_dict, summary, progress_callback
        )

    def _clear_local_files_cache(self, target_dir):
        normalized_target_dir = self.path_service.normalize_path(target_dir)
        cache_keys = [
            key
            for key in self._local_files_cache
            if key == normalized_target_dir
            or (isinstance(key, tuple) and key and key[0] == normalized_target_dir)
        ]
        for key in cache_keys:
            self._local_files_cache.pop(key, None)

    @staticmethod
    def _transfer_item_key(item):
        fs_id, dir_path, clean_path, final_path, _ = item
        return (str(fs_id), dir_path or "", clean_path or "", final_path or "")

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

        try:
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
        except Exception:
            return [], list(items)

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
                handle_error_and_notify(
                    ValueError(f"创建目录失败: {dir_path}"),
                    f"创建目录失败: {dir_path}",
                    self.wechat_notifier,
                    None,
                    collect=True,
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
        if progress_callback:
            progress_callback(
                "info", f"【步骤4/4】开始执行转存操作，共 {len(transfer_list)} 个文件"
            )

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
                key = self._transfer_item_key(item)
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
                key = self._transfer_item_key(item)
                if key in successful_keys:
                    continue
                successful_keys.add(key)
                failed_records.pop(key, None)
                successful_transfer_items.append(item)

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
                        normalized_dir_path = self._transfer_group(
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
                            add_remaining_entries(
                                next_pending, grouped_transfer_entries[index + 1 :]
                            )
                            batch_size_reduced = True
                            final_error = None
                            break
                        elif is_rate_limit_error(e):
                            if progress_callback:
                                progress_callback(
                                    "warning", f"触发频率限制，等待{RATE_LIMIT_WAIT_TIME}秒后重试..."
                                )
                            time.sleep(RATE_LIMIT_WAIT_TIME)
                            try:
                                normalized_dir_path = self._transfer_group(
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
                                    self.wechat_notifier,
                                    None,
                                    collect=True,
                                )
                            force_refresh = local_files_cache_dirty
                            existing_items, missing_items = self._split_existing_transfer_items(
                                batch_items,
                                target_dir,
                                progress_callback,
                                scan_cache,
                                force_refresh=force_refresh,
                            )
                            if force_refresh:
                                local_files_cache_dirty = False
                            missing_keys = {
                                self._transfer_item_key(item) for item in missing_items
                            }
                            for item in batch_items:
                                if self._transfer_item_key(item) not in missing_keys:
                                    failed_records.pop(self._transfer_item_key(item), None)
                            add_successful_items(existing_items)
                            is_temporary_error = is_storage_temporary_error_info(error_info)
                            if error_info.retryable and not is_temporary_error and len(missing_items) > 1:
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
                                key = self._transfer_item_key(item)
                                if key in successful_keys:
                                    continue
                                failed_records[key] = self._build_transfer_failed_record(
                                    item, error_info
                                )
                                if error_info.retryable and not is_temporary_error:
                                    next_pending[key] = item
                                    regular_retry_needed = True

                            if is_temporary_error:
                                for _, _, remaining_items in grouped_transfer_entries[index + 1 :]:
                                    for item in remaining_items:
                                        key = self._transfer_item_key(item)
                                        if key in successful_keys:
                                            continue
                                        failed_records[key] = self._build_transfer_failed_record(
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
                            f"记录到 {len(pending_items)} 个转存失败文件，等待{TRANSFER_FAILED_RETRY_DELAY}秒后重试...",
                        )
                    time.sleep(TRANSFER_FAILED_RETRY_DELAY)
                if regular_retry_needed or not pending_items:
                    attempt += 1
        finally:
            if local_files_cache_touched and target_dir:
                self._clear_local_files_cache(target_dir)

        return len(successful_transfer_items), successful_transfer_items, list(failed_records.values())

    def _rename_transferred_files(self, successful_transfer_items, target_dir, progress_callback=None):
        renamed_files = []
        rename_failed_files = []
        completed_count = 0
        rename_total = sum(1 for item in successful_transfer_items if item[4])
        rename_attempt = 0
        for _, dir_path, clean_path, final_path, need_rename in successful_transfer_items:
            if not need_rename:
                renamed_files.append(final_path)
                completed_count += 1
                continue
            try:
                if not is_safe_relative_target_path(final_path):
                    raise ValueError(f"重命名目标路径不安全: {final_path}")
                original_full_path = posixpath.join(target_dir, clean_path)
                final_full_path = posixpath.join(target_dir, final_path)
                final_parent_dir = posixpath.dirname(final_full_path).replace("\\", "/")

                if final_parent_dir and final_parent_dir != dir_path:
                    if not self.path_service.ensure_dir_exists(final_parent_dir):
                        raise ValueError(f"创建重命名目标目录失败: {final_parent_dir}")

                if progress_callback:
                    progress_callback("info", f"重命名文件: {clean_path} -> {final_path}")

                rename_attempt += 1
                self.client.rename(original_full_path, final_full_path)
                renamed_files.append(final_path)
                completed_count += 1
                if rename_attempt < rename_total:
                    time.sleep(RENAME_DELAY)
            except Exception as e:
                error_info = classify_storage_error(e)
                error_msg = (
                    f"重命名文件失败: {os.path.basename(clean_path)} -> {os.path.basename(final_path)}"
                )
                if progress_callback:
                    progress_callback("error", f"{error_msg}: {error_info.message}")
                handle_error_and_notify(
                    e,
                    f"重命名文件失败\n原始文件: {os.path.basename(clean_path)}\n目标文件: {os.path.basename(final_path)}",
                    self.wechat_notifier,
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
        return {
            "transferred_files": renamed_files,
            "rename_failed_files": rename_failed_files,
            "rename_failed_count": len(rename_failed_files),
            "completed_count": completed_count,
        }

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

        if completed_count == total_files and transfer_failed_count == 0:
            message = f"成功转存 {completed_count}/{total_files} 个文件"
            if progress_callback:
                progress_callback("success", f"转存完成，{message}")
            return {
                "success": True,
                "partial": False,
                "message": message,
                "transferred_files": renamed_files,
                "completed_count": completed_count,
                "transfer_success_count": transfer_success_count,
                "rename_failed_count": rename_failed_count,
            }

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
            return {
                "success": False,
                "partial": True,
                "message": message,
                "error": message,
                "transferred_files": renamed_files,
                "transfer_failed_files": transfer_failed_files,
                "transfer_failed_count": transfer_failed_count,
                "rename_failed_files": rename_failed_files,
                "rename_failed_count": rename_failed_count,
                "completed_count": completed_count,
                "transfer_success_count": transfer_success_count,
            }

        error = "转存失败，没有文件成功转存"
        if transfer_failed_count > 0:
            error = f"转存失败，{transfer_failed_count}/{total_files} 个文件转存失败"
        handle_error_and_notify(
            ValueError(error),
            error,
            self.wechat_notifier,
            None,
            collect=True,
        )
        return {
            "success": False,
            "partial": False,
            "error": error,
            "transfer_failed_files": transfer_failed_files,
            "transfer_failed_count": transfer_failed_count,
            "rename_failed_files": rename_failed_files,
            "rename_failed_count": rename_failed_count,
            "completed_count": completed_count,
            "transfer_success_count": transfer_success_count,
        }

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

    @staticmethod
    def _new_dir_tree_divide_stats():
        return {
            "completed_count": 0,
            "transfer_success_count": 0,
            "skipped_dir_count": 0,
            "failed_count": 0,
            "transferred_files": [],
            "transfer_failed_files": [],
        }

    @staticmethod
    def _build_dir_tree_divide_result(stats, progress_callback=None):
        completed_count = stats["completed_count"]
        transfer_success_count = stats["transfer_success_count"]
        skipped_dir_count = stats["skipped_dir_count"]
        failed_count = stats["failed_count"]
        transferred_files = stats["transferred_files"]
        transfer_failed_files = stats.get("transfer_failed_files", [])
        transfer_failed_count = len(transfer_failed_files)

        if transfer_success_count and failed_count:
            message = (
                f"目录分治转存部分成功，成功 {completed_count} 项，"
                f"失败 {failed_count} 项，跳过 {skipped_dir_count} 个目录"
            )
            if progress_callback:
                progress_callback("warning", message)
            return {
                "success": False,
                "partial": True,
                "divide_path": True,
                "message": message,
                "error": message,
                "transferred_files": transferred_files,
                "transfer_failed_files": transfer_failed_files,
                "transfer_failed_count": transfer_failed_count,
                "completed_count": completed_count,
                "transfer_success_count": transfer_success_count,
                "skipped_dir_count": skipped_dir_count,
                "failed_count": failed_count,
                "rename_failed_count": 0,
            }

        if transfer_success_count:
            message = (
                f"目录分治转存完成，成功 {completed_count} 项，"
                f"跳过 {skipped_dir_count} 个目录"
            )
            if progress_callback:
                progress_callback("success", message)
            return {
                "success": True,
                "partial": False,
                "divide_path": True,
                "message": message,
                "transferred_files": transferred_files,
                "transfer_failed_files": transfer_failed_files,
                "transfer_failed_count": transfer_failed_count,
                "completed_count": completed_count,
                "transfer_success_count": transfer_success_count,
                "skipped_dir_count": skipped_dir_count,
                "failed_count": failed_count,
                "rename_failed_count": 0,
            }

        if failed_count:
            error = f"目录分治转存失败，失败 {failed_count} 项，跳过 {skipped_dir_count} 个目录"
            if progress_callback:
                progress_callback("error", error)
            return {
                "success": False,
                "partial": False,
                "divide_path": True,
                "error": error,
                "transfer_failed_files": transfer_failed_files,
                "transfer_failed_count": transfer_failed_count,
                "completed_count": completed_count,
                "transfer_success_count": transfer_success_count,
                "skipped_dir_count": skipped_dir_count,
                "failed_count": failed_count,
                "rename_failed_count": 0,
            }

        message = "目录分治转存没有可转存内容"
        if skipped_dir_count:
            message = f"{message}，跳过 {skipped_dir_count} 个目录"
        if progress_callback:
            progress_callback("info", message)
        return {
            "success": True,
            "partial": False,
            "divide_path": True,
            "skipped": True,
            "message": message,
            "completed_count": completed_count,
            "transfer_success_count": transfer_success_count,
            "skipped_dir_count": skipped_dir_count,
            "failed_count": failed_count,
            "rename_failed_count": 0,
        }

    def _flush_dir_tree_file_batch(
        self,
        file_transfer_list,
        target_dir,
        context,
        share_url,
        stats,
        progress_callback=None,
    ):
        if not file_transfer_list:
            return

        success_count, successful_items, failed_items = self._execute_transfer_plan(
            file_transfer_list,
            share_url,
            context["uk"],
            context["share_id"],
            context["bdstoken"],
            target_dir,
            progress_callback,
        )
        stats["transfer_success_count"] += success_count
        stats["completed_count"] += success_count
        stats["transferred_files"].extend(item[3] for item in successful_items)
        stats["transfer_failed_files"].extend(failed_items)
        stats["failed_count"] += len(failed_items)
        file_transfer_list.clear()

    def _initialize_dir_tree_frame(self, frame, context, stats, progress_callback=None):
        if not self.path_service.ensure_dir_exists(frame.target_dir):
            stats["failed_count"] += 1
            if progress_callback:
                progress_callback("error", f"创建目录失败: {frame.target_dir}")
            return False

        if progress_callback:
            progress_callback("info", f"目录分治扫描: {frame.shared_dir_path}")

        try:
            frame.child_iter = iter(
                self.share_service.iter_shared_dir_children(
                    frame.shared_dir,
                    context["uk"],
                    context["share_id"],
                    context["bdstoken"],
                )
            )
        except Exception as exc:
            stats["failed_count"] += 1
            handle_error_and_notify(
                exc,
                f"目录分治列目录失败: {frame.shared_dir_path}",
                self.wechat_notifier,
                None,
                collect=True,
            )
            return False
        return True

    def _finish_dir_tree_frame(self, frame, context, share_url, stats, progress_callback=None):
        self._flush_dir_tree_file_batch(
            frame.file_transfer_list,
            frame.target_dir,
            context,
            share_url,
            stats,
            progress_callback,
        )
        if progress_callback:
            progress_callback(
                "info",
                f"目录分治扫描完成: {frame.shared_dir_path}，处理 {frame.child_count} 个子项",
            )

    def _handle_dir_tree_iter_error(
        self, frame, context, share_url, stats, exc, progress_callback=None
    ):
        self._flush_dir_tree_file_batch(
            frame.file_transfer_list,
            frame.target_dir,
            context,
            share_url,
            stats,
            progress_callback,
        )
        stats["failed_count"] += 1
        handle_error_and_notify(
            exc,
            f"目录分治列目录失败: {frame.shared_dir_path}",
            self.wechat_notifier,
            None,
            collect=True,
        )

    def _handle_dir_tree_file_child(
        self, frame, child, context, share_url, stats, progress_callback=None
    ):
        if not (child.get("is_file") and child.get("fs_id")):
            return False
        frame.file_transfer_list.append(
            TransferItem(
                child["fs_id"],
                frame.target_dir,
                child["name"],
                child["name"],
                False,
                child.get("md5"),
            )
        )
        if len(frame.file_transfer_list) >= TRANSFER_BATCH_SIZE:
            self._flush_dir_tree_file_batch(
                frame.file_transfer_list,
                frame.target_dir,
                context,
                share_url,
                stats,
                progress_callback,
            )
        return True

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
        self._flush_dir_tree_file_batch(
            frame.file_transfer_list,
            frame.target_dir,
            context,
            share_url,
            stats,
            progress_callback,
        )

        folder_name = child.get("name") or os.path.basename(str(child.get("path", "")).rstrip("/"))
        child_target_dir = posixpath.join(frame.target_dir, folder_name)
        if should_exclude_folder(folder_name, exclude_folder_filter):
            stats["skipped_dir_count"] += 1
            if progress_callback:
                progress_callback("info", f"跳过排除目录: {folder_name}")
            return
        if exclude_folder_filter:
            stack.append(_DirTreeFrame(child["raw"], child_target_dir))
            return

        try:
            self._transfer_group(
                frame.target_dir,
                [child["fs_id"]],
                share_url,
                context["uk"],
                context["share_id"],
                context["bdstoken"],
                progress_callback,
            )
            stats["transfer_success_count"] += 1
            stats["completed_count"] += 1
            stats["transferred_files"].append(folder_name)
        except Exception as exc:
            if is_transfer_count_limit_error(exc):
                if progress_callback:
                    progress_callback("warning", f"子目录超量，继续拆分: {folder_name}")
                stack.append(_DirTreeFrame(child["raw"], child_target_dir))
            else:
                stats["failed_count"] += 1
                error_info = classify_storage_error(exc)
                if progress_callback:
                    progress_callback(
                        "error", f"转存子目录失败: {folder_name} - {error_info.message}"
                    )
                handle_error_and_notify(
                    exc,
                    f"转存子目录失败: {folder_name}",
                    self.wechat_notifier,
                    None,
                    collect=True,
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
        stack = [_DirTreeFrame(shared_dir, target_dir)]
        while stack:
            frame = stack[-1]

            if frame.child_iter is None:
                if not self._initialize_dir_tree_frame(frame, context, stats, progress_callback):
                    stack.pop()
                    continue

            try:
                child = next(frame.child_iter)
            except StopIteration:
                self._finish_dir_tree_frame(frame, context, share_url, stats, progress_callback)
                stack.pop()
                continue
            except Exception as exc:
                self._handle_dir_tree_iter_error(
                    frame, context, share_url, stats, exc, progress_callback
                )
                stack.pop()
                continue

            frame.child_count += 1
            if self._handle_dir_tree_file_child(
                frame, child, context, share_url, stats, progress_callback
            ):
                continue
            if child.get("is_dir") and child.get("fs_id"):
                self._handle_dir_tree_dir_child(
                    stack,
                    frame,
                    child,
                    context,
                    share_url,
                    exclude_folder_filter,
                    stats,
                    progress_callback,
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
        folder_name = os.path.basename(str(getattr(shared_dir, "path", shared_dir)).rstrip("/"))
        if should_exclude_folder(folder_name, exclude_folder_filter):
            stats["skipped_dir_count"] = 1
            if progress_callback:
                progress_callback("info", f"跳过排除目录: {folder_name}")
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
            try:
                if self._target_child_exists(save_dir, folder_name):
                    if progress_callback:
                        progress_callback("warning", "整目录直接转存失败但目标目录已存在，回退逐文件对比转存")
                    return None
            except Exception:
                pass
            raise

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
        while not stop_event.is_set():
            try:
                stream_queue.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

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
        if progress_callback:
            progress_callback("info", "【步骤3/4】准备转存: 流式扫描并对比文件")

        sentinel = object()
        stream_queue = queue.Queue(maxsize=max(TRANSFER_BATCH_SIZE * 2, 1))
        stop_event = threading.Event()
        producer_error = {}

        def producer():
            try:
                for file_info in self.share_service.iter_shared_files(
                    context["shared_paths"],
                    folder_filter,
                    progress_callback,
                    exclude_folder_filter=exclude_folder_filter,
                ):
                    if not self._put_stream_queue_item(stream_queue, file_info, stop_event):
                        return
            except Exception as exc:
                producer_error["error"] = exc
            finally:
                self._put_stream_queue_item(stream_queue, sentinel, stop_event)

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

        def flush_transfer_item_buffer(force=False):
            nonlocal total_transfer_count, transfer_success_count, dir_error
            if not transfer_item_buffer:
                return None
            if not force and len(transfer_item_buffer) < TRANSFER_BATCH_SIZE:
                return None

            flush_count = len(transfer_item_buffer) if force else TRANSFER_BATCH_SIZE
            transfer_list = transfer_item_buffer[:flush_count]
            del transfer_item_buffer[:flush_count]

            dir_error = self._ensure_transfer_dirs(transfer_list)
            if dir_error:
                error_info = classify_storage_error(dir_error.get("error", "创建目录失败"))
                transfer_failed_files.extend(
                    self._build_transfer_failed_record(item, error_info)
                    for item in transfer_list
                )
                total_transfer_count += len(transfer_list)
                return dir_error

            success_count, successful_items, failed_items = self._execute_transfer_plan(
                transfer_list,
                share_url,
                context["uk"],
                context["share_id"],
                context["bdstoken"],
                transfer_target_dir,
                progress_callback,
            )
            total_transfer_count += len(transfer_list)
            transfer_success_count += success_count
            successful_transfer_items.extend(successful_items)
            for item in successful_items:
                self._record_transfer_item_paths(item, local_files_dict)
            transfer_failed_files.extend(failed_items)
            return None

        def flush_shared_file_batch():
            if not shared_file_batch:
                return None

            candidates, batch_summary, relative_dirs = self._prepare_transfer_candidates(
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
                    self._scan_local_files_dict(
                        transfer_target_dir, progress_callback, new_relative_dirs
                    )
                )
                scanned_relative_dirs.update(new_relative_dirs)

            transfer_list = self._filter_transfer_candidates_core(
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
        finally:
            stop_event.set()
            producer_thread.join(STREAM_PRODUCER_JOIN_TIMEOUT)
            if producer_thread.is_alive():
                get_logger().warning("共享文件扫描线程未及时退出，继续处理已完成结果")

        self._report_transfer_candidate_summary(
            summary, warning_samples, progress_callback
        )

        if dir_error:
            if transfer_success_count > 0:
                rename_result = self._rename_transferred_files(
                    successful_transfer_items, transfer_target_dir, progress_callback
                )
                result = self._build_transfer_result(
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

        rename_result = self._rename_transferred_files(
            successful_transfer_items, transfer_target_dir, progress_callback
        )
        result = self._build_transfer_result(
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
                handle_error_and_notify(
                    ValueError(error_msg),
                    f"转存分享文件: 客户端不可用\n分享链接: {masked_share_url}",
                    self.wechat_notifier,
                    None,
                    collect=True,
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
                handle_error_and_notify(
                    ValueError(error_msg),
                    f"获取分享文件夹名称失败: 客户端不可用",
                    self.wechat_notifier,
                    None,
                    collect=True,
                )
                return {"success": False, "error": error_msg}

            shared_paths = self.share_service.load_shared_paths(share_url, pwd)
            if not shared_paths:
                error_msg = "获取分享文件列表失败"
                handle_error_and_notify(
                    ValueError(error_msg),
                    f"获取分享文件列表失败\n分享链接: {masked_share_url}",
                    self.wechat_notifier,
                    None,
                    collect=True,
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
            handle_error_and_notify(
                e,
                f"获取分享文件夹名称时发生异常\n分享链接: {masked_share_url}",
                self.wechat_notifier,
                None,
                collect=True,
            )
            return {"success": False, "error": parse_share_error(e)}
