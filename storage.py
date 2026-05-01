#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from collections import Counter

import os
import posixpath
import queue
import threading
import time

# 添加 WeChatNotifier 和工具方法导入
from wechat_notifier import WeChatNotifier
from utils import handle_error_and_notify, ErrorCollector, mask_share_url
from config_utils import parse_share_links_from_text
from storage_client import BaiduClientAdapter
from storage_errors import (
    classify_storage_error,
    is_rate_limit_error,
    is_transfer_count_limit_error,
    parse_share_error,
)
from storage_paths import StoragePathService
from storage_rules import apply_regex_rules, should_exclude_folder
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
FREQUENCY_LIMIT_DELAY = _read_non_negative_float_env(
    "TRANSFERSHARE_TRANSFER_GROUP_DELAY", 1
)
RENAME_DELAY = _read_non_negative_float_env("TRANSFERSHARE_RENAME_DELAY", 0.5)
BATCH_SHARE_DELAY = _read_non_negative_float_env("TRANSFERSHARE_BATCH_SHARE_DELAY", 2)
TRANSFER_BATCH_SIZE = _read_positive_int_env("TRANSFERSHARE_TRANSFER_BATCH_SIZE", 999)


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

    def _build_result_record(self, index, share_url, save_dir, result):
        masked_share_url = mask_share_url(share_url) or share_url
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
            record["rename_failed_files"] = result.get("rename_failed_files", [])
            record["rename_failed_count"] = result.get("rename_failed_count", 0)
            record["completed_count"] = result.get("completed_count", 0)
            record["transfer_success_count"] = result.get("transfer_success_count", 0)
        else:
            record["error"] = result.get("error", "未知错误")
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
        result_record = self._build_result_record(index, share_url, save_dir, result)

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
            for item in results:
                all_rename_failed_files.extend(item.get("rename_failed_files", []))

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
                "candidate_count": 0,
                "rename_candidate_count": 0,
            }
        )

        for file_info in shared_files_info:
            clean_path = file_info["path"]
            if is_single_folder and "/" in clean_path:
                clean_path = "/".join(clean_path.split("/")[1:])

            should_transfer, final_path = apply_regex_rules(
                clean_path, regex_pattern, regex_replace
            )
            if not should_transfer:
                summary["regex_filtered_count"] += 1
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
            logger.info(f"检测到 {len(dup_names)} 个重复文件名：")
            for name in dup_names:
                logger.info(f"  - {name} 出现 {name_counts[name]} 次")
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

    def _filter_transfer_candidates_core(
        self,
        candidates,
        local_files_dict,
        summary,
        warning_samples,
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
                if source_exists:
                    summary["existing_count"] += 1
                    if src_md5:
                        if source_md5 == src_md5 or source_md5 is None:
                            continue
                        summary["conflict_count"] += 1
                        self._add_warning_sample(
                            warning_samples,
                            f"同路径已存在,但内容不同(md5不同),跳过： {final_path}",
                        )
                        continue
                    continue
            elif target_exists:
                summary["existing_count"] += 1
                if src_md5 and target_md5 == src_md5:
                    continue
                if src_md5 and target_md5 is None:
                    continue
                summary["conflict_count"] += 1
                self._add_warning_sample(
                    warning_samples,
                    f"重命名目标已存在，跳过转存: {final_path}",
                )
                continue
            elif source_exists:
                summary["existing_count"] += 1
                if src_md5 and source_md5 == src_md5:
                    continue
                summary["conflict_count"] += 1
                self._add_warning_sample(
                    warning_samples,
                    f"源路径已存在，跳过重复转存以避免副本: {clean_path} -> {final_path}",
                )
                continue

            if candidate["dir_path"] is None or clean_path is None:
                continue

            transfer_list.append(
                (
                    candidate["fs_id"],
                    candidate["dir_path"],
                    clean_path,
                    final_path,
                    need_rename,
                )
            )
            summary["transfer_needed_count"] += 1
            if need_rename:
                summary["rename_needed_count"] += 1

        return transfer_list

    @staticmethod
    def _report_transfer_candidate_summary(summary, warning_samples, progress_callback=None):
        if not progress_callback:
            return
        progress_callback(
            "info",
            "候选分析完成："
            f"共享文件 {summary['shared_count']} 个，候选 {summary['candidate_count']} 个，"
            f"正则过滤 {summary['regex_filtered_count']} 个，本地已存在 {summary['existing_count']} 个，"
            f"冲突跳过 {summary['conflict_count']} 个，需要转存 {summary['transfer_needed_count']} 个，"
            f"其中需重命名 {summary['rename_needed_count']} 个",
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
            candidates, local_files_dict, summary, warning_samples
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

        grouped_transfer_items = {}
        for item in transfer_list:
            _, dir_path, _, _, _ = item
            grouped_transfer_items.setdefault(dir_path, []).append(item)

        success_count = 0
        successful_transfer_items = []
        grouped_transfer_entries = []
        for dir_path, items in grouped_transfer_items.items():
            for start in range(0, len(items), TRANSFER_BATCH_SIZE):
                batch_items = items[start : start + TRANSFER_BATCH_SIZE]
                batch_fs_ids = [item[0] for item in batch_items]
                grouped_transfer_entries.append((dir_path, batch_fs_ids, batch_items))

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
                success_count += len(batch_items)
                successful_transfer_items.extend(batch_items)
                if target_dir:
                    self._clear_local_files_cache(target_dir)
                if progress_callback:
                    progress_callback("success", f"成功转存到 {normalized_dir_path}")
            except Exception as e:
                if is_rate_limit_error(e):
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
                        success_count += len(batch_items)
                        successful_transfer_items.extend(batch_items)
                        if target_dir:
                            self._clear_local_files_cache(target_dir)
                        if progress_callback:
                            progress_callback("success", f"重试成功: {normalized_dir_path}")
                    except Exception as retry_e:
                        retry_error = classify_storage_error(retry_e)
                        error_msg = f"转存失败: {dir_path} - {retry_error.message}"
                        if progress_callback:
                            progress_callback("error", error_msg)
                        handle_error_and_notify(
                            retry_e,
                            f"转存失败: {dir_path}",
                            self.wechat_notifier,
                            None,
                            collect=True,
                        )
                else:
                    error_info = classify_storage_error(e)
                    error_msg = f"转存失败: {dir_path} - {error_info.message}"
                    if progress_callback:
                        progress_callback("error", error_msg)
                    handle_error_and_notify(
                        e,
                        f"转存失败: {dir_path}",
                        self.wechat_notifier,
                        None,
                        collect=True,
                    )
            if index < len(grouped_transfer_entries) - 1:
                time.sleep(FREQUENCY_LIMIT_DELAY)

        return success_count, successful_transfer_items

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
    ):
        renamed_files = rename_result.get("transferred_files", [])
        rename_failed_files = rename_result.get("rename_failed_files", [])
        rename_failed_count = rename_result.get("rename_failed_count", 0)
        completed_count = rename_result.get("completed_count", 0)

        if completed_count == total_files:
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
            if rename_failed_count > 0:
                message = (
                    f"部分转存成功，成功完成 {completed_count}/{total_files} 个文件，"
                    f"另有 {rename_failed_count} 个文件转存后重命名失败"
                )
            else:
                message = f"部分转存成功，成功完成 {completed_count}/{total_files} 个文件"
            if progress_callback:
                progress_callback("warning", message)
            return {
                "success": False,
                "partial": True,
                "message": message,
                "error": message,
                "transferred_files": renamed_files,
                "rename_failed_files": rename_failed_files,
                "rename_failed_count": rename_failed_count,
                "completed_count": completed_count,
                "transfer_success_count": transfer_success_count,
            }

        handle_error_and_notify(
            ValueError("转存失败，没有文件成功转存"),
            "转存失败，没有文件成功转存",
            self.wechat_notifier,
            None,
            collect=True,
        )
        return {
            "success": False,
            "partial": False,
            "error": "转存失败，没有文件成功转存",
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
        }

    @staticmethod
    def _build_dir_tree_divide_result(stats, progress_callback=None):
        completed_count = stats["completed_count"]
        transfer_success_count = stats["transfer_success_count"]
        skipped_dir_count = stats["skipped_dir_count"]
        failed_count = stats["failed_count"]
        transferred_files = stats["transferred_files"]

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

        total_count = len(file_transfer_list)
        success_count, successful_items = self._execute_transfer_plan(
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
        stats["failed_count"] += total_count - success_count
        file_transfer_list.clear()

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
        if not self.path_service.ensure_dir_exists(target_dir):
            stats["failed_count"] += 1
            if progress_callback:
                progress_callback("error", f"创建目录失败: {target_dir}")
            return

        shared_dir_path = getattr(shared_dir, "path", shared_dir)
        if progress_callback:
            progress_callback("info", f"目录分治扫描: {shared_dir_path}")

        child_count = 0
        file_transfer_list = []
        try:
            child_iter = iter(
                self.share_service.iter_shared_dir_children(
                    shared_dir,
                    context["uk"],
                    context["share_id"],
                    context["bdstoken"],
                )
            )
        except Exception as exc:
            stats["failed_count"] += 1
            handle_error_and_notify(
                exc,
                f"目录分治列目录失败: {shared_dir_path}",
                self.wechat_notifier,
                None,
                collect=True,
            )
            return

        while True:
            try:
                child = next(child_iter)
            except StopIteration:
                break
            except Exception as exc:
                self._flush_dir_tree_file_batch(
                    file_transfer_list,
                    target_dir,
                    context,
                    share_url,
                    stats,
                    progress_callback,
                )
                stats["failed_count"] += 1
                handle_error_and_notify(
                    exc,
                    f"目录分治列目录失败: {shared_dir_path}",
                    self.wechat_notifier,
                    None,
                    collect=True,
                )
                return

            child_count += 1
            if child.get("is_file") and child.get("fs_id"):
                file_transfer_list.append(
                    (child["fs_id"], target_dir, child["name"], child["name"], False)
                )
                if len(file_transfer_list) >= TRANSFER_BATCH_SIZE:
                    self._flush_dir_tree_file_batch(
                        file_transfer_list,
                        target_dir,
                        context,
                        share_url,
                        stats,
                        progress_callback,
                    )
                continue

            if not child.get("is_dir") or not child.get("fs_id"):
                continue

            self._flush_dir_tree_file_batch(
                file_transfer_list,
                target_dir,
                context,
                share_url,
                stats,
                progress_callback,
            )

            folder_name = child.get("name") or os.path.basename(str(child.get("path", "")).rstrip("/"))
            if should_exclude_folder(folder_name, exclude_folder_filter):
                stats["skipped_dir_count"] += 1
                if progress_callback:
                    progress_callback("info", f"跳过排除目录: {folder_name}")
                continue
            if exclude_folder_filter:
                self._transfer_dir_tree_divide_collect(
                    child["raw"],
                    posixpath.join(target_dir, folder_name),
                    context,
                    share_url,
                    exclude_folder_filter,
                    stats,
                    progress_callback,
                )
                continue

            try:
                self._transfer_group(
                    target_dir,
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
                    self._transfer_dir_tree_divide_collect(
                        child["raw"],
                        posixpath.join(target_dir, folder_name),
                        context,
                        share_url,
                        exclude_folder_filter,
                        stats,
                        progress_callback,
                    )
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

        self._flush_dir_tree_file_batch(
            file_transfer_list,
            target_dir,
            context,
            share_url,
            stats,
            progress_callback,
        )
        if progress_callback:
            progress_callback("info", f"目录分治扫描完成: {shared_dir_path}，处理 {child_count} 个子项")

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
        shared_file_batch = []
        transfer_item_buffer = []
        total_transfer_count = 0
        transfer_success_count = 0
        successful_transfer_items = []
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
                return dir_error

            success_count, successful_items = self._execute_transfer_plan(
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
                candidates, local_files_dict, summary, warning_samples
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
            producer_thread.join()

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
