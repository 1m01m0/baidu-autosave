#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from collections import Counter
import posixpath

from storage_models import TransferItem
from storage_progress import ProgressReporter
from storage_rules import (
    REGEX_FILTER_UNMATCHED,
    REGEX_FILTER_UNSAFE_REPLACE,
    apply_regex_rules_detail,
)


class CandidateFilter:
    def __init__(self, path_service, progress=None):
        self.path_service = path_service
        self.progress = progress or ProgressReporter()

    @staticmethod
    def candidate_parent_dirs(*paths):
        relative_dirs = set()
        for path in paths:
            normalized_path = str(path or "").replace("\\", "/").lstrip("/")
            parent_dir = posixpath.dirname(normalized_path)
            relative_dirs.add("" if parent_dir in ("", ".") else parent_dir)
        return relative_dirs

    def prepare_candidates(
        self,
        shared_files_info,
        shared_paths,
        target_dir,
        regex_pattern=None,
        regex_replace=None,
    ):
        is_single_folder = len(shared_paths) == 1 and shared_paths[0].is_dir
        single_folder_name = ""
        if is_single_folder:
            single_folder_path = str(getattr(shared_paths[0], "path", "") or "").replace("\\", "/")
            single_folder_name = posixpath.basename(single_folder_path.rstrip("/"))
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
            if (
                is_single_folder
                and single_folder_name
                and clean_path.startswith(f"{single_folder_name}/")
            ):
                clean_path = clean_path[len(single_folder_name) + 1 :]

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
                relative_dirs.update(self.candidate_parent_dirs(clean_path, final_path))

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

    @staticmethod
    def add_warning_sample(warning_samples, message, max_samples=5):
        if len(warning_samples) < max_samples:
            warning_samples.append(message)

    @staticmethod
    def is_verified_same_file(src_md5, local_md5):
        return bool(src_md5 and local_md5 and src_md5 == local_md5)

    @staticmethod
    def existing_conflict_message(path, src_md5, local_md5, prefix):
        if src_md5 and local_md5:
            return f"{prefix}已存在,但内容不同(md5不同),跳过： {path}"
        return f"{prefix}已存在,但缺少MD5无法确认是否相同,跳过： {path}"

    def filter_planned_path_conflict(
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
        if self.is_verified_same_file(src_md5, planned_md5):
            return True
        summary["conflict_count"] += 1
        self.add_warning_sample(
            warning_samples,
            self.existing_conflict_message(display_path, src_md5, planned_md5, prefix),
        )
        return True

    def filter_candidates_core(
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
                if self.filter_planned_path_conflict(
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
                    if self.is_verified_same_file(src_md5, source_md5):
                        continue
                    summary["conflict_count"] += 1
                    self.add_warning_sample(
                        warning_samples,
                        self.existing_conflict_message(final_path, src_md5, source_md5, "同路径"),
                    )
                    continue
            elif self.filter_planned_path_conflict(
                planned_paths,
                candidate["final_normalized"],
                final_path,
                src_md5,
                summary,
                warning_samples,
                "本轮重命名目标",
            ):
                continue
            elif self.filter_planned_path_conflict(
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
                if self.is_verified_same_file(src_md5, target_md5):
                    continue
                summary["conflict_count"] += 1
                self.add_warning_sample(
                    warning_samples,
                    self.existing_conflict_message(final_path, src_md5, target_md5, "重命名目标"),
                )
                continue
            elif source_exists:
                summary["existing_count"] += 1
                if not self.is_verified_same_file(src_md5, source_md5):
                    summary["conflict_count"] += 1
                    self.add_warning_sample(
                        warning_samples,
                        self.existing_conflict_message(clean_path, src_md5, source_md5, "源路径"),
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

    def report_summary(self, summary, warning_samples):
        regex_detail = ""
        if summary["regex_unmatched_count"] or summary["unsafe_regex_replace_count"]:
            regex_detail = (
                f"（未匹配 {summary['regex_unmatched_count']} 个，"
                f"不安全替换 {summary['unsafe_regex_replace_count']} 个）"
            )
        self.progress.report(
            "info",
            "候选分析完成："
            f"共享文件 {summary['shared_count']} 个，候选 {summary['candidate_count']} 个，"
            f"正则过滤 {summary['regex_filtered_count']} 个{regex_detail}，"
            f"本地已存在 {summary['existing_count']} 个，冲突跳过 {summary['conflict_count']} 个，"
            f"需要转存 {summary['transfer_needed_count']} 个，其中需重命名 {summary['rename_needed_count']} 个",
        )
        for message in warning_samples:
            self.progress.report("warning", message)

    def filter_candidates(self, candidates, local_files_dict, summary):
        self.progress.report("info", "【步骤3/4】准备转存: 对比文件和准备目录")

        warning_samples = []
        transfer_list = self.filter_candidates_core(
            candidates, local_files_dict, summary, warning_samples, {}
        )
        self.report_summary(summary, warning_samples)
        return transfer_list

    def build_transfer_list(
        self,
        shared_files_info,
        shared_paths,
        target_dir,
        local_files_dict,
        regex_pattern=None,
        regex_replace=None,
    ):
        candidates, summary, _ = self.prepare_candidates(
            shared_files_info,
            shared_paths,
            target_dir,
            regex_pattern,
            regex_replace,
        )
        return self.filter_candidates(candidates, local_files_dict, summary)


__all__ = ["CandidateFilter"]
