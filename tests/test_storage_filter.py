#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

from storage_filter import CandidateFilter
from storage_progress import ProgressReporter


class CandidateFilterTest(unittest.TestCase):
    def setUp(self):
        self.path_service = Mock()
        self.path_service.normalize_path.side_effect = (
            lambda path, file_only=False: str(path or "").strip("/")
        )
        self.progress_messages = []
        self.candidate_filter = CandidateFilter(
            self.path_service,
            ProgressReporter(
                lambda level, message: self.progress_messages.append((level, message))
            ),
        )

    def test_empty_input_returns_empty_transfer_list_and_zero_counts(self):
        summary = Counter()
        warning_samples = []

        result = self.candidate_filter.filter_candidates_core(
            [], {}, summary, warning_samples, {}
        )

        self.assertEqual([], result)
        self.assertEqual([], warning_samples)
        self.assertEqual(0, summary["existing_count"])
        self.assertEqual(0, summary["conflict_count"])
        self.assertEqual(0, summary["transfer_needed_count"])
        self.assertEqual(0, summary["rename_needed_count"])

    def test_prepare_candidates_splits_regex_filter_reasons(self):
        candidates, summary, relative_dirs = self.candidate_filter.prepare_candidates(
            [
                {"fs_id": 1, "path": "safe/file.mp4", "md5": "md5-safe"},
                {"fs_id": 2, "path": "skip.txt", "md5": "md5-skip"},
                {"fs_id": 3, "path": "bad/../file.mp4", "md5": "md5-bad"},
            ],
            [SimpleNamespace(is_dir=False)],
            "/save",
            regex_pattern=r"^(safe|bad/\.\.)/file\.mp4$",
            regex_replace=r"\1/out.mp4",
        )

        self.assertEqual([1], [candidate["fs_id"] for candidate in candidates])
        self.assertEqual(2, summary["regex_filtered_count"])
        self.assertEqual(1, summary["regex_unmatched_count"])
        self.assertEqual(1, summary["unsafe_regex_replace_count"])
        self.assertEqual(1, summary["candidate_count"])
        self.assertEqual({"safe"}, relative_dirs)

    def test_single_folder_keeps_already_trimmed_nested_path(self):
        candidates, summary, relative_dirs = self.candidate_filter.prepare_candidates(
            [{"fs_id": 1, "path": "子目录/a.txt", "md5": "md5-a"}],
            [SimpleNamespace(is_dir=True, path="/share/course")],
            "/save/course",
        )

        self.assertEqual("子目录/a.txt", candidates[0]["clean_path"])
        self.assertEqual("子目录/a.txt", candidates[0]["final_path"])
        self.assertEqual("/save/course/子目录", candidates[0]["dir_path"])
        self.assertEqual({"子目录"}, relative_dirs)
        self.assertEqual(1, summary["candidate_count"])

    def test_existing_same_path_with_same_md5_is_skipped(self):
        candidates, summary, _ = self.candidate_filter.prepare_candidates(
            [{"fs_id": 1, "path": "a.txt", "md5": "md5-a"}],
            [SimpleNamespace(is_dir=False)],
            "/save",
        )
        warning_samples = []

        result = self.candidate_filter.filter_candidates_core(
            candidates, {"a.txt": "md5-a"}, summary, warning_samples, {}
        )

        self.assertEqual([], result)
        self.assertEqual([], warning_samples)
        self.assertEqual(1, summary["existing_count"])
        self.assertEqual(0, summary["conflict_count"])
        self.assertEqual(0, summary["transfer_needed_count"])
        self.assertEqual(0, summary["rename_needed_count"])

    def test_existing_same_path_without_md5_records_conflict_warning(self):
        candidates, summary, _ = self.candidate_filter.prepare_candidates(
            [{"fs_id": 1, "path": "a.txt", "md5": "src-md5"}],
            [SimpleNamespace(is_dir=False)],
            "/save",
        )
        warning_samples = []

        result = self.candidate_filter.filter_candidates_core(
            candidates, {"a.txt": None}, summary, warning_samples, {}
        )

        self.assertEqual([], result)
        self.assertEqual(1, summary["existing_count"])
        self.assertEqual(1, summary["conflict_count"])
        self.assertEqual(0, summary["transfer_needed_count"])
        self.assertEqual(0, summary["rename_needed_count"])
        self.assertEqual(
            ["同路径已存在,但缺少MD5无法确认是否相同,跳过： a.txt"],
            warning_samples,
        )

    def test_rename_candidate_is_skipped_when_source_path_exists(self):
        candidates, summary, _ = self.candidate_filter.prepare_candidates(
            [{"fs_id": 1, "path": "old/a.txt", "md5": "src-md5"}],
            [SimpleNamespace(is_dir=False)],
            "/save",
            regex_pattern=r"old",
            regex_replace="new",
        )
        warning_samples = []

        result = self.candidate_filter.filter_candidates_core(
            candidates, {"old/a.txt": "other-md5"}, summary, warning_samples, {}
        )

        self.assertEqual([], result)
        self.assertEqual(1, summary["existing_count"])
        self.assertEqual(1, summary["conflict_count"])
        self.assertEqual(0, summary["transfer_needed_count"])
        self.assertEqual(0, summary["rename_needed_count"])
        self.assertEqual(
            ["源路径已存在,但内容不同(md5不同),跳过： old/a.txt"],
            warning_samples,
        )

    def test_rename_candidate_is_skipped_when_target_path_exists_with_same_md5(self):
        candidates, summary, _ = self.candidate_filter.prepare_candidates(
            [{"fs_id": 1, "path": "old/a.txt", "md5": "src-md5"}],
            [SimpleNamespace(is_dir=False)],
            "/save",
            regex_pattern=r"old",
            regex_replace="new",
        )
        warning_samples = []

        result = self.candidate_filter.filter_candidates_core(
            candidates, {"new/a.txt": "src-md5"}, summary, warning_samples, {}
        )

        self.assertEqual([], result)
        self.assertEqual(1, summary["existing_count"])
        self.assertEqual(0, summary["conflict_count"])
        self.assertEqual(0, summary["transfer_needed_count"])
        self.assertEqual(0, summary["rename_needed_count"])
        self.assertEqual([], warning_samples)

    def test_rename_candidate_with_existing_source_same_md5_is_kept_for_retry(self):
        candidates, summary, _ = self.candidate_filter.prepare_candidates(
            [{"fs_id": 1, "path": "old/a.txt", "md5": "src-md5"}],
            [SimpleNamespace(is_dir=False)],
            "/save",
            regex_pattern=r"old",
            regex_replace="new",
        )
        warning_samples = []

        result = self.candidate_filter.filter_candidates_core(
            candidates, {"old/a.txt": "src-md5"}, summary, warning_samples, {}
        )

        self.assertEqual(1, len(result))
        self.assertEqual("old/a.txt", result[0].clean_path)
        self.assertEqual("new/a.txt", result[0].final_path)
        self.assertTrue(result[0].need_rename)
        self.assertEqual(1, summary["existing_count"])
        self.assertEqual(0, summary["conflict_count"])
        self.assertEqual(1, summary["transfer_needed_count"])
        self.assertEqual(1, summary["rename_needed_count"])
        self.assertEqual([], warning_samples)

    def test_duplicate_planned_paths_keep_first_transfer_item(self):
        candidates, summary, _ = self.candidate_filter.prepare_candidates(
            [
                {"fs_id": 1, "path": "a.txt", "md5": "md5-a"},
                {"fs_id": 2, "path": "a.txt", "md5": "md5-a"},
                {"fs_id": 3, "path": "a.txt", "md5": "md5-b"},
            ],
            [SimpleNamespace(is_dir=False)],
            "/save",
        )
        warning_samples = []

        result = self.candidate_filter.filter_candidates_core(
            candidates, {}, summary, warning_samples, {}
        )

        self.assertEqual([1], [item.fs_id for item in result])
        self.assertEqual(2, summary["existing_count"])
        self.assertEqual(1, summary["conflict_count"])
        self.assertEqual(1, summary["transfer_needed_count"])
        self.assertEqual(0, summary["rename_needed_count"])
        self.assertEqual(
            ["本轮同路径已存在,但内容不同(md5不同),跳过： a.txt"],
            warning_samples,
        )

    def test_rename_targets_are_recorded_as_planned_paths(self):
        candidates, summary, _ = self.candidate_filter.prepare_candidates(
            [
                {"fs_id": 1, "path": "old/a.txt", "md5": "md5-a"},
                {"fs_id": 2, "path": "copy/a.txt", "md5": "md5-b"},
            ],
            [SimpleNamespace(is_dir=False)],
            "/save",
            regex_pattern=r"^(old|copy)/a\.txt$",
            regex_replace="new/a.txt",
        )
        warning_samples = []

        result = self.candidate_filter.filter_candidates_core(
            candidates, {}, summary, warning_samples, {}
        )

        self.assertEqual([1], [item.fs_id for item in result])
        self.assertEqual("new/a.txt", result[0].final_path)
        self.assertEqual(1, summary["existing_count"])
        self.assertEqual(1, summary["conflict_count"])
        self.assertEqual(1, summary["transfer_needed_count"])
        self.assertEqual(1, summary["rename_needed_count"])
        self.assertEqual(
            ["本轮重命名目标已存在,但内容不同(md5不同),跳过： new/a.txt"],
            warning_samples,
        )

    def test_progress_reporter_receives_step_summary_and_warning_messages(self):
        result = self.candidate_filter.build_transfer_list(
            [{"fs_id": 1, "path": "a.txt", "md5": "src-md5"}],
            [SimpleNamespace(is_dir=False)],
            "/save",
            {"a.txt": None},
        )

        self.assertEqual([], result)
        self.assertEqual(
            ("info", "【步骤3/4】准备转存: 对比文件和准备目录"),
            self.progress_messages[0],
        )
        self.assertIn(
            (
                "info",
                "候选分析完成：共享文件 1 个，候选 1 个，正则过滤 0 个，"
                "本地已存在 1 个，冲突跳过 1 个，需要转存 0 个，其中需重命名 0 个",
            ),
            self.progress_messages,
        )
        self.assertEqual(
            ("warning", "同路径已存在,但缺少MD5无法确认是否相同,跳过： a.txt"),
            self.progress_messages[-1],
        )


if __name__ == "__main__":
    unittest.main()
