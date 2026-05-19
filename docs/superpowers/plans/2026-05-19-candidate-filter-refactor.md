# Candidate Filter Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract transfer-candidate analysis from `storage.py` into `CandidateFilter` while preserving existing `BaiduStorage` wrapper behavior and callback output.

**Architecture:** Add `storage_filter.py` as a focused collaborator that depends on `storage_rules`, `storage_models.TransferItem`, and `storage_progress.ProgressReporter`. Keep `BaiduStorage` private wrapper methods in `storage.py`, but delegate their candidate-preparation, filtering, planned-path conflict, warning, and summary-reporting work to `CandidateFilter`.

**Tech Stack:** Python, `unittest`, `collections.Counter`, existing `StoragePathService` path normalization contract, existing pyenv environment `transfer_share`.

---

## File Structure

- Create `storage_filter.py`
  - Owns candidate preparation, local existence/MD5 filtering, planned-path conflict filtering, warning sampling, and candidate summary reporting.
  - Imports only focused dependencies: `Counter`, `posixpath`, `TransferItem`, `ProgressReporter`, and regex helpers from `storage_rules`.
  - Does not import `storage.py` or depend on `BaiduStorage` instance state.

- Create `tests/test_storage_filter.py`
  - Direct `unittest` coverage for `CandidateFilter` behavior.
  - Uses a mocked path service with the same `normalize_path(path, file_only=False)` behavior expected by current storage tests.
  - Captures `ProgressReporter` callback calls to verify message order and warning propagation.

- Modify `storage.py`
  - Import `CandidateFilter` and `ProgressReporter`.
  - Remove candidate-filter-only imports from `storage_rules` that become unused.
  - Keep existing private method names as wrappers so `tests/test_storage.py` and `storage_streaming.py` continue calling the same methods.
  - Leave `_scan_local_files_dict()`, `_transfer_item_local_entries()`, and `_record_transfer_item_paths()` in `BaiduStorage` for this step.

---

## Task 1: Add direct CandidateFilter tests

**Files:**
- Create: `tests/test_storage_filter.py`
- Read for compatibility examples: `tests/test_storage.py:2330-2458`

- [ ] **Step 1: Write the failing test file**

Create `tests/test_storage_filter.py` with this full content:

```python
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
        self.assertEqual(
            ["同路径已存在,但缺少MD5无法确认是否相同,跳过： a.txt"],
            warning_samples,
        )

    def test_rename_candidate_is_skipped_when_source_path_exists(self):
        result = self.candidate_filter.build_transfer_list(
            [{"fs_id": 1, "path": "old/a.txt", "md5": "src-md5"}],
            [SimpleNamespace(is_dir=False)],
            "/save",
            {"old/a.txt": "other-md5"},
            regex_pattern=r"old",
            regex_replace="new",
        )

        self.assertEqual([], result)
        self.assertIn(
            ("warning", "源路径已存在,但内容不同(md5不同),跳过： old/a.txt"),
            self.progress_messages,
        )

    def test_rename_candidate_is_skipped_when_target_path_exists_with_same_md5(self):
        result = self.candidate_filter.build_transfer_list(
            [{"fs_id": 1, "path": "old/a.txt", "md5": "src-md5"}],
            [SimpleNamespace(is_dir=False)],
            "/save",
            {"new/a.txt": "src-md5"},
            regex_pattern=r"old",
            regex_replace="new",
        )

        self.assertEqual([], result)
        self.assertIn(
            (
                "info",
                "候选分析完成：共享文件 1 个，候选 1 个，正则过滤 0 个，"
                "本地已存在 1 个，冲突跳过 0 个，需要转存 0 个，其中需重命名 0 个",
            ),
            self.progress_messages,
        )

    def test_duplicate_planned_paths_keep_first_transfer_item(self):
        result = self.candidate_filter.build_transfer_list(
            [
                {"fs_id": 1, "path": "a.txt", "md5": "md5-a"},
                {"fs_id": 2, "path": "a.txt", "md5": "md5-a"},
                {"fs_id": 3, "path": "a.txt", "md5": "md5-b"},
            ],
            [SimpleNamespace(is_dir=False)],
            "/save",
            {},
        )

        self.assertEqual([1], [item.fs_id for item in result])
        self.assertIn(
            ("warning", "本轮同路径已存在,但内容不同(md5不同),跳过： a.txt"),
            self.progress_messages,
        )
        self.assertIn(
            (
                "info",
                "候选分析完成：共享文件 3 个，候选 3 个，正则过滤 0 个，"
                "本地已存在 2 个，冲突跳过 1 个，需要转存 1 个，其中需重命名 0 个",
            ),
            self.progress_messages,
        )

    def test_rename_targets_are_recorded_as_planned_paths(self):
        result = self.candidate_filter.build_transfer_list(
            [
                {"fs_id": 1, "path": "old/a.txt", "md5": "md5-a"},
                {"fs_id": 2, "path": "copy/a.txt", "md5": "md5-b"},
            ],
            [SimpleNamespace(is_dir=False)],
            "/save",
            {},
            regex_pattern=r"^(old|copy)/a\.txt$",
            regex_replace="new/a.txt",
        )

        self.assertEqual([1], [item.fs_id for item in result])
        self.assertEqual("new/a.txt", result[0].final_path)
        self.assertIn(
            (
                "warning",
                "本轮重命名目标已存在,但内容不同(md5不同),跳过： new/a.txt",
            ),
            self.progress_messages,
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
```

- [ ] **Step 2: Run the new test to verify it fails for the expected reason**

Run:

```bash
pyenv version
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_filter
```

Expected:

```text
transfer_share (set by /Users/jack/project/transfershare/.python-version)
ModuleNotFoundError: No module named 'storage_filter'
FAILED (errors=1)
```

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_storage_filter.py
git commit -m "$(cat <<'EOF'
添加候选过滤器行为测试
EOF
)"
```

---

## Task 2: Implement CandidateFilter

**Files:**
- Create: `storage_filter.py`
- Test: `tests/test_storage_filter.py`

- [ ] **Step 1: Create `storage_filter.py`**

Create `storage_filter.py` with this full content:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转存候选文件筛选逻辑。"""

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
                relative_dirs.update(
                    self.candidate_parent_dirs(clean_path, final_path)
                )

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
                        self.existing_conflict_message(
                            final_path, src_md5, source_md5, "同路径"
                        ),
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
                    self.existing_conflict_message(
                        final_path, src_md5, target_md5, "重命名目标"
                    ),
                )
                continue
            elif source_exists:
                summary["existing_count"] += 1
                if self.is_verified_same_file(src_md5, source_md5):
                    continue
                summary["conflict_count"] += 1
                self.add_warning_sample(
                    warning_samples,
                    self.existing_conflict_message(
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

    def filter_candidates(
        self,
        candidates,
        local_files_dict,
        summary,
    ):
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
```

- [ ] **Step 2: Run direct CandidateFilter tests**

Run:

```bash
pyenv version
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_filter
```

Expected:

```text
transfer_share (set by /Users/jack/project/transfershare/.python-version)
Ran 9 tests
OK
```

- [ ] **Step 3: Commit CandidateFilter implementation**

```bash
git add storage_filter.py tests/test_storage_filter.py
git commit -m "$(cat <<'EOF'
添加候选过滤器组件
EOF
)"
```

---

## Task 3: Delegate BaiduStorage candidate wrappers to CandidateFilter

**Files:**
- Modify: `storage.py:39-46`
- Modify: `storage.py:657-1021`
- Test: `tests/test_storage.py:2330-2458`
- Test: `tests/test_storage_filter.py`

- [ ] **Step 1: Update imports in `storage.py`**

Change the import block near `storage.py:39-46` from:

```python
from storage_models import DirTreeFrame, TransferItem
from storage_paths import StoragePathService
from storage_rules import (
    REGEX_FILTER_UNMATCHED,
    REGEX_FILTER_UNSAFE_REPLACE,
    apply_regex_rules_detail,
    should_exclude_folder,
)
from storage_shares import SharedPathService
```

to:

```python
from storage_filter import CandidateFilter
from storage_models import DirTreeFrame, TransferItem
from storage_paths import StoragePathService
from storage_progress import ProgressReporter
from storage_rules import should_exclude_folder
from storage_shares import SharedPathService
```

- [ ] **Step 2: Replace candidate-related methods with wrappers**

In `storage.py`, replace the existing methods from `_candidate_parent_dirs()` through `_build_transfer_list()` with this wrapper block. Keep `_scan_local_files_dict()` unchanged between `_prepare_transfer_candidates()` and `_add_warning_sample()` as shown:

```python
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
```

- [ ] **Step 3: Run wrapper compatibility tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_filter
```

Expected:

```text
OK
```

Success requires command exit code 0, zero failures, and zero errors.

- [ ] **Step 4: Commit wrapper delegation**

```bash
git add storage.py storage_filter.py tests/test_storage_filter.py
git commit -m "$(cat <<'EOF'
委托候选分析到过滤器
EOF
)"
```

---

## Task 4: Full verification and cleanup

**Files:**
- Verify: `storage.py`
- Verify: `storage_filter.py`
- Verify: `tests/test_storage_filter.py`

- [ ] **Step 1: Verify pyenv environment**

Run:

```bash
pyenv version
```

Expected:

```text
transfer_share (set by /Users/jack/project/transfershare/.python-version)
```

- [ ] **Step 2: Run targeted tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_filter
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_filter
```

Expected:

```text
OK
OK
```

- [ ] **Step 3: Run the full test suite**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p "test_*.py" -b
```

Expected:

```text
OK
```

- [ ] **Step 4: Check whitespace and generated files**

Run:

```bash
git diff --check
git status --short
```

Expected:

```text
# git diff --check has no output
# git status --short shows only intended tracked changes plus the pre-existing untracked .kiro/specs/storage-architecture-refactor/ directory if it is still untracked
```

If `__pycache__` directories appear, remove only those generated by this verification run before reporting completion.

- [ ] **Step 5: Commit final cleanup if any tracked files changed after Task 3**

If Task 4 only verifies and does not change tracked files, skip this commit. If a verification fix changes tracked files, commit the specific changed files:

```bash
git add storage.py storage_filter.py tests/test_storage_filter.py
git commit -m "$(cat <<'EOF'
完善候选过滤器验证修复
EOF
)"
```

---

## Self-Review Checklist

- Spec coverage:
  - `CandidateFilter` component: Task 2 creates `storage_filter.py`.
  - Candidate preparation/filtering/planned-path/warning/summary extraction: Task 2 implements it; Task 3 delegates wrappers.
  - `ProgressReporter` candidate-analysis integration: Task 2 uses it; Task 1 verifies step, summary, and warning callbacks.
  - Existing wrapper compatibility: Task 3 keeps wrapper method names and runs `tests.test_storage`.
  - `unittest` only: all commands use `python -m unittest`.
  - No public API changes: Task 3 preserves `BaiduStorage` method names and leaves `storage_streaming.py` calls intact.
- Type consistency:
  - `CandidateFilter.prepare_candidates()` returns `(candidates, summary, relative_dirs)`.
  - `CandidateFilter.filter_candidates_core()` returns `list[TransferItem]`.
  - `CandidateFilter.report_summary(summary, warning_samples)` uses `ProgressReporter.report(level, message)`.
  - `BaiduStorage._candidate_filter(progress_callback=None)` wraps callbacks with `ProgressReporter`.
- Scope check:
  - No `DirTreeTraverser`, `TransferOrchestrator`, public return type, or global progress-callback replacement is included.
