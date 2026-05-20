# DirTreeTraverser Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the directory divide-and-conquer traversal core from `storage.py` into `DirTreeTraverser` while preserving `BaiduStorage` wrapper behavior and public result dictionaries.

**Architecture:** Add `storage_traverser.py` as a focused collaborator for stack-based directory traversal, file batching, child-directory fallback, progress reporting, and result construction. Keep `_try_transfer_dir_fast_path()` in `BaiduStorage`; keep the existing directory-tree private method names in `storage.py` as compatibility wrappers that delegate to `DirTreeTraverser` through a small transfer-executor adapter.

**Tech Stack:** Python, `unittest`, existing `storage_models.DirTreeFrame` / `TransferItem`, `storage_progress.ProgressReporter`, `storage_constants.TRANSFER_BATCH_SIZE`, existing pyenv environment `transfer_share`.

---

## File Structure

- Create `storage_traverser.py`
  - Owns directory divide-and-conquer traversal state and behavior.
  - Imports focused dependencies only: `os`, `posixpath`, `TRANSFER_BATCH_SIZE`, `classify_storage_error`, `is_transfer_count_limit_error`, `DirTreeFrame`, `TransferItem`, `ProgressReporter`, and `should_exclude_folder`.
  - Does not import `storage.py` or depend on `BaiduStorage` directly.

- Create `tests/test_storage_traverser.py`
  - Direct `unittest` coverage for `DirTreeTraverser` with fake path/share/executor collaborators.
  - Verifies deep traversal, batching, generator flush timing, md5 propagation, excluded folders, count-limit fallback, creation failure, and progress reporting.

- Modify `storage.py`
  - Import `DirTreeTraverser`.
  - Add `BaiduStorageDirTransferExecutor` adapter.
  - Add `_dir_tree_traverser(progress_callback=None)` factory.
  - Replace directory-tree helper bodies with wrappers while preserving old method signatures.
  - Leave `_try_transfer_dir_fast_path()` in `BaiduStorage`.

---

## Task 1: Add direct DirTreeTraverser tests

**Files:**
- Create: `tests/test_storage_traverser.py`
- Read for behavior examples: `tests/test_storage.py:1550-1584`, `tests/test_storage.py:1853-1978`

- [ ] **Step 1: Write the failing test file**

Create `tests/test_storage_traverser.py` with this full content:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import unittest
from types import SimpleNamespace

from storage_constants import TRANSFER_BATCH_SIZE
from storage_progress import ProgressReporter
from storage_traverser import DirTreeTraverser


class FakePathService:
    def __init__(self, failing_dirs=None):
        self.failing_dirs = set(failing_dirs or [])
        self.ensured_dirs = []

    def ensure_dir_exists(self, target_dir):
        self.ensured_dirs.append(target_dir)
        return target_dir not in self.failing_dirs


class FakeShareService:
    def __init__(self, children_by_path):
        self.children_by_path = children_by_path
        self.calls = []

    def iter_shared_dir_children(self, shared_dir, uk, share_id, bdstoken):
        path = getattr(shared_dir, "path", shared_dir)
        self.calls.append((path, uk, share_id, bdstoken))
        children = self.children_by_path.get(path, [])
        if callable(children):
            return children()
        return iter(children)


class FakeTransferExecutor:
    def __init__(self):
        self.transfer_plan_batches = []
        self.group_calls = []
        self.group_side_effects = []

    def execute_transfer_plan(
        self,
        file_transfer_list,
        share_url,
        uk,
        share_id,
        bdstoken,
        target_dir,
    ):
        batch = list(file_transfer_list)
        self.transfer_plan_batches.append(
            {
                "items": batch,
                "share_url": share_url,
                "uk": uk,
                "share_id": share_id,
                "bdstoken": bdstoken,
                "target_dir": target_dir,
            }
        )
        return len(batch), batch, []

    def transfer_group(self, dir_path, fs_ids, share_url, uk, share_id, bdstoken):
        self.group_calls.append(
            {
                "dir_path": dir_path,
                "fs_ids": list(fs_ids),
                "share_url": share_url,
                "uk": uk,
                "share_id": share_id,
                "bdstoken": bdstoken,
            }
        )
        if self.group_side_effects:
            effect = self.group_side_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            return effect
        return dir_path


class DirTreeTraverserTest(unittest.TestCase):
    def make_traverser(
        self,
        children_by_path,
        failing_dirs=None,
        batch_size=TRANSFER_BATCH_SIZE,
        progress_messages=None,
        error_notifications=None,
    ):
        path_service = FakePathService(failing_dirs)
        share_service = FakeShareService(children_by_path)
        executor = FakeTransferExecutor()
        progress_messages = progress_messages if progress_messages is not None else []
        error_notifications = error_notifications if error_notifications is not None else []

        def notify_error(error, context_message, collect=True):
            error_notifications.append((error, context_message, collect))

        traverser = DirTreeTraverser(
            path_service,
            share_service,
            executor,
            ProgressReporter(
                lambda level, message: progress_messages.append((level, message))
            ),
            notify_error,
            batch_size=batch_size,
        )
        return traverser, path_service, share_service, executor, progress_messages, error_notifications

    def file_child(self, parent_path, name, fs_id, md5=None):
        return {
            "raw": SimpleNamespace(
                path=f"{parent_path}/{name}",
                is_dir=False,
                is_file=True,
                fs_id=fs_id,
                md5=md5,
            ),
            "fs_id": fs_id,
            "path": f"{parent_path}/{name}",
            "name": name,
            "is_dir": False,
            "is_file": True,
            "md5": md5,
        }

    def dir_child(self, parent_path, name, fs_id):
        child_path = f"{parent_path}/{name}"
        return {
            "raw": SimpleNamespace(path=child_path, is_dir=True, is_file=False, fs_id=fs_id),
            "fs_id": fs_id,
            "path": child_path,
            "name": name,
            "is_dir": True,
            "is_file": False,
        }

    def test_deep_directory_traversal_uses_explicit_stack(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        depth = 150
        children_by_path = {}
        path = "/share/course"
        for index in range(depth):
            child = self.dir_child(path, f"d{index}", index + 20)
            children_by_path[path] = [child]
            path = child["path"]
        children_by_path[path] = [self.file_child(path, "file.txt", 999, "md5-file")]
        traverser, _, _, executor, _, _ = self.make_traverser(children_by_path)
        old_limit = sys.getrecursionlimit()

        try:
            sys.setrecursionlimit(100)
            result = traverser.traverse(
                shared_dir,
                "/save/course",
                {"uk": 1, "share_id": 2, "bdstoken": "token"},
                "url",
                r"^skip$",
            )
        finally:
            sys.setrecursionlimit(old_limit)

        self.assertTrue(result["success"])
        self.assertEqual(1, result["completed_count"])
        self.assertEqual([999], [item.fs_id for item in executor.transfer_plan_batches[0]["items"]])

    def test_file_children_flush_by_batch_size(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        children = [
            self.file_child("/share/course", f"{fs_id}.txt", fs_id)
            for fs_id in range(1, TRANSFER_BATCH_SIZE * 2 + 2)
        ]
        traverser, _, _, executor, _, _ = self.make_traverser(
            {"/share/course": children}
        )

        result = traverser.traverse(
            shared_dir,
            "/save/course",
            {"uk": 1, "share_id": 2, "bdstoken": "token"},
            "url",
            None,
        )

        self.assertTrue(result["success"])
        self.assertEqual(len(children), result["completed_count"])
        self.assertEqual(3, len(executor.transfer_plan_batches))
        self.assertEqual(TRANSFER_BATCH_SIZE, len(executor.transfer_plan_batches[0]["items"]))
        self.assertEqual(TRANSFER_BATCH_SIZE, len(executor.transfer_plan_batches[1]["items"]))
        self.assertEqual(1, len(executor.transfer_plan_batches[2]["items"]))

    def test_full_file_batch_flushes_before_generator_is_exhausted(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        call_counts_after_full_batch = []
        executor_ref = {}

        def children():
            for fs_id in range(1, TRANSFER_BATCH_SIZE + 1):
                yield self.file_child("/share/course", f"{fs_id}.txt", fs_id)
            call_counts_after_full_batch.append(
                len(executor_ref["executor"].transfer_plan_batches)
            )
            yield self.file_child(
                "/share/course", f"{TRANSFER_BATCH_SIZE + 1}.txt", TRANSFER_BATCH_SIZE + 1
            )

        traverser, _, _, executor, _, _ = self.make_traverser(
            {"/share/course": children}
        )
        executor_ref["executor"] = executor

        result = traverser.traverse(
            shared_dir,
            "/save/course",
            {"uk": 1, "share_id": 2, "bdstoken": "token"},
            "url",
            None,
        )

        self.assertTrue(result["success"])
        self.assertEqual(TRANSFER_BATCH_SIZE + 1, result["completed_count"])
        self.assertEqual([1], call_counts_after_full_batch)
        self.assertEqual(2, len(executor.transfer_plan_batches))

    def test_child_file_md5_is_preserved_in_transfer_item(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        traverser, _, _, executor, _, _ = self.make_traverser(
            {"/share/course": [self.file_child("/share/course", "a.txt", 11, "md5-a")]}
        )

        traverser.traverse(
            shared_dir,
            "/save/course",
            {"uk": 1, "share_id": 2, "bdstoken": "token"},
            "url",
            None,
        )

        self.assertEqual(
            "md5-a", getattr(executor.transfer_plan_batches[0]["items"][0], "src_md5")
        )

    def test_excluded_folder_is_skipped(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        traverser, _, share_service, executor, progress_messages, _ = self.make_traverser(
            {"/share/course": [self.dir_child("/share/course", "skip", 20)]}
        )

        result = traverser.traverse(
            shared_dir,
            "/save/course",
            {"uk": 1, "share_id": 2, "bdstoken": "token"},
            "url",
            r"^skip$",
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["skipped"])
        self.assertEqual(1, result["skipped_dir_count"])
        self.assertEqual([], executor.group_calls)
        self.assertEqual([("/share/course", 1, 2, "token")], share_service.calls)
        self.assertIn(("info", "跳过排除目录: skip"), progress_messages)

    def test_count_limit_dir_transfer_pushes_child_to_stack(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        child_dir = self.dir_child("/share/course", "big", 20)
        children_by_path = {
            "/share/course": [child_dir],
            "/share/course/big": [self.file_child("/share/course/big", "a.txt", 21)],
        }
        traverser, _, share_service, executor, progress_messages, _ = self.make_traverser(
            children_by_path
        )
        executor.group_side_effects.append(
            RuntimeError("error_code: -33, message: 一次支持操作999个")
        )

        result = traverser.traverse(
            shared_dir,
            "/save/course",
            {"uk": 1, "share_id": 2, "bdstoken": "token"},
            "url",
            None,
        )

        self.assertTrue(result["success"])
        self.assertEqual(1, result["completed_count"])
        self.assertEqual(
            [("/share/course", 1, 2, "token"), ("/share/course/big", 1, 2, "token")],
            share_service.calls,
        )
        self.assertIn(("warning", "子目录超量，继续拆分: big"), progress_messages)
        self.assertEqual([21], [item.fs_id for item in executor.transfer_plan_batches[0]["items"]])

    def test_directory_creation_failure_returns_failure_result(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        traverser, _, _, _, progress_messages, _ = self.make_traverser(
            {"/share/course": []}, failing_dirs={"/save/course"}
        )

        result = traverser.traverse(
            shared_dir,
            "/save/course",
            {"uk": 1, "share_id": 2, "bdstoken": "token"},
            "url",
            None,
        )

        self.assertFalse(result["success"])
        self.assertFalse(result["partial"])
        self.assertEqual(1, result["failed_count"])
        self.assertEqual("目录分治转存失败，失败 1 项，跳过 0 个目录", result["error"])
        self.assertIn(("error", "创建目录失败: /save/course"), progress_messages)

    def test_progress_reports_scan_completion_and_final_success(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        traverser, _, _, _, progress_messages, _ = self.make_traverser(
            {"/share/course": [self.file_child("/share/course", "a.txt", 11)]}
        )

        result = traverser.traverse(
            shared_dir,
            "/save/course",
            {"uk": 1, "share_id": 2, "bdstoken": "token"},
            "url",
            None,
        )

        self.assertTrue(result["success"])
        self.assertIn(("info", "目录分治扫描: /share/course"), progress_messages)
        self.assertIn(
            ("info", "目录分治扫描完成: /share/course，处理 1 个子项"),
            progress_messages,
        )
        self.assertIn(
            ("success", "目录分治转存完成，成功 1 项，跳过 0 个目录"),
            progress_messages,
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new test to verify it fails for the expected reason**

Run:

```bash
pyenv version
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_traverser
```

Expected:

```text
transfer_share (set by /Users/jack/project/transfershare/.python-version)
ModuleNotFoundError: No module named 'storage_traverser'
FAILED (errors=1)
```

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_storage_traverser.py
git commit -m "$(cat <<'EOF'
添加目录遍历器行为测试
EOF
)"
```

---

## Task 2: Implement DirTreeTraverser

**Files:**
- Create: `storage_traverser.py`
- Test: `tests/test_storage_traverser.py`

- [ ] **Step 1: Create `storage_traverser.py`**

Create `storage_traverser.py` with this full content:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""目录分治转存遍历逻辑。"""

import os
import posixpath

from storage_constants import TRANSFER_BATCH_SIZE
from storage_errors import classify_storage_error, is_transfer_count_limit_error
from storage_models import DirTreeFrame, TransferItem
from storage_progress import ProgressReporter
from storage_rules import should_exclude_folder


class DirTreeTraverser:
    def __init__(
        self,
        path_service,
        share_service,
        transfer_executor,
        progress=None,
        error_notifier=None,
        batch_size=TRANSFER_BATCH_SIZE,
    ):
        self.path_service = path_service
        self.share_service = share_service
        self.transfer_executor = transfer_executor
        self.progress = progress or ProgressReporter()
        self.error_notifier = error_notifier
        self.batch_size = batch_size

    @staticmethod
    def new_stats():
        return {
            "completed_count": 0,
            "transfer_success_count": 0,
            "skipped_dir_count": 0,
            "failed_count": 0,
            "transferred_files": [],
            "transfer_failed_files": [],
        }

    def build_result(self, stats):
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
            self.progress.report("warning", message)
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
            self.progress.report("success", message)
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
            self.progress.report("error", error)
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
        self.progress.report("info", message)
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

    def notify_error(self, error, context_message, collect=True):
        if self.error_notifier is not None:
            self.error_notifier(error, context_message, collect=collect)

    def flush_file_batch(
        self,
        file_transfer_list,
        target_dir,
        context,
        share_url,
        stats,
    ):
        if not file_transfer_list:
            return

        success_count, successful_items, failed_items = (
            self.transfer_executor.execute_transfer_plan(
                file_transfer_list,
                share_url,
                context["uk"],
                context["share_id"],
                context["bdstoken"],
                target_dir,
            )
        )
        stats["transfer_success_count"] += success_count
        stats["completed_count"] += success_count
        stats["transferred_files"].extend(item[3] for item in successful_items)
        stats["transfer_failed_files"].extend(failed_items)
        stats["failed_count"] += len(failed_items)
        file_transfer_list.clear()

    def initialize_frame(self, frame, context, stats):
        if not self.path_service.ensure_dir_exists(frame.target_dir):
            stats["failed_count"] += 1
            self.progress.report("error", f"创建目录失败: {frame.target_dir}")
            return False

        self.progress.report("info", f"目录分治扫描: {frame.shared_dir_path}")

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
            self.notify_error(
                exc,
                f"目录分治列目录失败: {frame.shared_dir_path}",
                collect=True,
            )
            return False
        return True

    def finish_frame(self, frame, context, share_url, stats):
        self.flush_file_batch(
            frame.file_transfer_list,
            frame.target_dir,
            context,
            share_url,
            stats,
        )
        self.progress.report(
            "info",
            f"目录分治扫描完成: {frame.shared_dir_path}，处理 {frame.child_count} 个子项",
        )

    def handle_iter_error(self, frame, context, share_url, stats, exc):
        self.flush_file_batch(
            frame.file_transfer_list,
            frame.target_dir,
            context,
            share_url,
            stats,
        )
        stats["failed_count"] += 1
        self.notify_error(
            exc,
            f"目录分治列目录失败: {frame.shared_dir_path}",
            collect=True,
        )

    def handle_file_child(self, frame, child, context, share_url, stats):
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
        if len(frame.file_transfer_list) >= self.batch_size:
            self.flush_file_batch(
                frame.file_transfer_list,
                frame.target_dir,
                context,
                share_url,
                stats,
            )
        return True

    def handle_dir_child(
        self,
        stack,
        frame,
        child,
        context,
        share_url,
        exclude_folder_filter,
        stats,
    ):
        self.flush_file_batch(
            frame.file_transfer_list,
            frame.target_dir,
            context,
            share_url,
            stats,
        )

        folder_name = child.get("name") or os.path.basename(str(child.get("path", "")).rstrip("/"))
        child_target_dir = posixpath.join(frame.target_dir, folder_name)
        if should_exclude_folder(folder_name, exclude_folder_filter):
            stats["skipped_dir_count"] += 1
            self.progress.report("info", f"跳过排除目录: {folder_name}")
            return
        if exclude_folder_filter:
            stack.append(DirTreeFrame(child["raw"], child_target_dir))
            return

        try:
            self.transfer_executor.transfer_group(
                frame.target_dir,
                [child["fs_id"]],
                share_url,
                context["uk"],
                context["share_id"],
                context["bdstoken"],
            )
            stats["transfer_success_count"] += 1
            stats["completed_count"] += 1
            stats["transferred_files"].append(folder_name)
        except Exception as exc:
            if is_transfer_count_limit_error(exc):
                self.progress.report("warning", f"子目录超量，继续拆分: {folder_name}")
                stack.append(DirTreeFrame(child["raw"], child_target_dir))
            else:
                stats["failed_count"] += 1
                error_info = classify_storage_error(exc)
                self.progress.report(
                    "error", f"转存子目录失败: {folder_name} - {error_info.message}"
                )
                self.notify_error(
                    exc,
                    f"转存子目录失败: {folder_name}",
                    collect=True,
                )

    def collect(
        self,
        shared_dir,
        target_dir,
        context,
        share_url,
        exclude_folder_filter,
        stats,
    ):
        stack = [DirTreeFrame(shared_dir, target_dir)]
        while stack:
            frame = stack[-1]

            if frame.child_iter is None:
                if not self.initialize_frame(frame, context, stats):
                    stack.pop()
                    continue

            try:
                child = next(frame.child_iter)
            except StopIteration:
                self.finish_frame(frame, context, share_url, stats)
                stack.pop()
                continue
            except Exception as exc:
                self.handle_iter_error(frame, context, share_url, stats, exc)
                stack.pop()
                continue

            frame.child_count += 1
            if self.handle_file_child(frame, child, context, share_url, stats):
                continue
            if child.get("is_dir") and child.get("fs_id"):
                self.handle_dir_child(
                    stack,
                    frame,
                    child,
                    context,
                    share_url,
                    exclude_folder_filter,
                    stats,
                )

    def traverse(
        self,
        shared_dir,
        target_dir,
        context,
        share_url,
        exclude_folder_filter=None,
    ):
        stats = self.new_stats()
        folder_name = os.path.basename(str(getattr(shared_dir, "path", shared_dir)).rstrip("/"))
        if should_exclude_folder(folder_name, exclude_folder_filter):
            stats["skipped_dir_count"] = 1
            self.progress.report("info", f"跳过排除目录: {folder_name}")
            return self.build_result(stats)

        self.collect(
            shared_dir,
            target_dir,
            context,
            share_url,
            exclude_folder_filter,
            stats,
        )
        return self.build_result(stats)


__all__ = ["DirTreeTraverser"]
```

- [ ] **Step 2: Run direct DirTreeTraverser tests**

Run:

```bash
pyenv version
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_traverser
```

Expected:

```text
transfer_share (set by /Users/jack/project/transfershare/.python-version)
Ran 8 tests
OK
```

- [ ] **Step 3: Commit DirTreeTraverser implementation**

```bash
git add storage_traverser.py tests/test_storage_traverser.py
git commit -m "$(cat <<'EOF'
添加目录分治遍历器
EOF
)"
```

---

## Task 3: Delegate BaiduStorage directory-tree wrappers

**Files:**
- Modify: `storage.py:39-43`
- Modify: `storage.py:62-75`
- Modify: `storage.py:1189-1549`
- Test: `tests/test_storage.py:1550-1584`
- Test: `tests/test_storage.py:1853-1978`
- Test: `tests/test_storage_traverser.py`

- [ ] **Step 1: Update imports and add the transfer executor adapter**

Change the import area in `storage.py` from:

```python
from storage_filter import CandidateFilter
from storage_models import DirTreeFrame, TransferItem
from storage_paths import StoragePathService
from storage_progress import ProgressReporter
from storage_rules import should_exclude_folder
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
from storage_traverser import DirTreeTraverser
```

Add this class after `_NULL_LOCK = _NullLock()`:

```python
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
```

- [ ] **Step 2: Add `BaiduStorage._dir_tree_traverser()` factory**

Add this method near the other small factory/helper methods in `BaiduStorage`, before `_new_dir_tree_divide_stats()`:

```python
    def _dir_tree_traverser(self, progress_callback=None):
        def notify_error(error, context_message, collect=True):
            handle_error_and_notify(
                error,
                context_message,
                self.wechat_notifier,
                None,
                collect=collect,
            )

        return DirTreeTraverser(
            self.path_service,
            self.share_service,
            BaiduStorageDirTransferExecutor(self, progress_callback),
            ProgressReporter(progress_callback),
            notify_error,
        )
```

- [ ] **Step 3: Replace directory-tree helper bodies with wrappers**

In `storage.py`, replace the methods from `_new_dir_tree_divide_stats()` through `_transfer_dir_tree_divide()` with this wrapper block. Do not move or edit `_try_transfer_dir_fast_path()`.

```python
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
        return self._dir_tree_traverser(progress_callback).finish_frame(
            frame,
            context,
            share_url,
            stats,
        )

    def _handle_dir_tree_iter_error(
        self, frame, context, share_url, stats, exc, progress_callback=None
    ):
        return self._dir_tree_traverser(progress_callback).handle_iter_error(
            frame,
            context,
            share_url,
            stats,
            exc,
        )

    def _handle_dir_tree_file_child(
        self, frame, child, context, share_url, stats, progress_callback=None
    ):
        return self._dir_tree_traverser(progress_callback).handle_file_child(
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
        return self._dir_tree_traverser(progress_callback).handle_dir_child(
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
        flush_override = self.__dict__.get("_flush_dir_tree_file_batch")
        if flush_override is not None:
            def flush_file_batch(
                file_transfer_list,
                target_dir,
                context,
                share_url,
                stats,
            ):
                return flush_override(
                    file_transfer_list,
                    target_dir,
                    context,
                    share_url,
                    stats,
                    progress_callback,
                )

            traverser.flush_file_batch = flush_file_batch

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
        return self._dir_tree_traverser(progress_callback).traverse(
            shared_dir,
            target_dir,
            context,
            share_url,
            exclude_folder_filter,
        )
```

The `flush_override` block preserves the existing `tests/test_storage.py` behavior where a test assigns `self.storage._flush_dir_tree_file_batch = Mock(side_effect=flush_items)` before calling `_transfer_dir_tree_divide_collect()`.

- [ ] **Step 4: Run wrapper compatibility tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_traverser
```

Expected:

```text
OK
```

Success requires command exit code 0, zero failures, and zero errors.

- [ ] **Step 5: Commit wrapper delegation**

```bash
git add storage.py storage_traverser.py tests/test_storage_traverser.py
git commit -m "$(cat <<'EOF'
委托目录分治到遍历器
EOF
)"
```

---

## Task 4: Full verification and cleanup

**Files:**
- Verify: `storage.py`
- Verify: `storage_traverser.py`
- Verify: `tests/test_storage_traverser.py`

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
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_traverser
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_traverser
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

- [ ] **Step 4: Check whitespace and working tree status**

Run:

```bash
git diff --check
git status --short
```

Expected:

```text
# git diff --check has no output
# git status --short shows only intended tracked changes plus the pre-existing untracked .kiro/specs/storage-architecture-refactor/.config.kiro if it is still untracked
```

If `__pycache__` directories appear, remove only those generated by this verification run before reporting completion.

- [ ] **Step 5: Commit final cleanup only if tracked files changed after Task 3**

If Task 4 only verifies and does not change tracked files, skip this commit. If a verification fix changes tracked files, commit the specific changed files:

```bash
git add storage.py storage_traverser.py tests/test_storage_traverser.py
git commit -m "$(cat <<'EOF'
完善目录遍历器验证修复
EOF
)"
```

---

## Self-Review Checklist

- Spec coverage:
  - `DirTreeTraverser` component: Task 2 creates `storage_traverser.py`.
  - Directory traversal, batching, child handling, count-limit fallback, and result construction: Task 2 implements these methods.
  - `ProgressReporter` directory traversal integration: Task 2 uses it; Task 1 verifies scan/completion/final progress messages.
  - `BaiduStorage` wrapper compatibility: Task 3 keeps old method names and runs `tests.test_storage`.
  - `_try_transfer_dir_fast_path()` stays in `BaiduStorage`: Task 3 explicitly avoids moving it.
  - `unittest` only: all commands use `python -m unittest`.
  - No public API changes: Task 3 preserves `BaiduStorage` wrappers and leaves `transfer_runner.py` untouched.
- Type consistency:
  - `DirTreeTraverser.traverse()` returns the same result dictionary shape as `_transfer_dir_tree_divide()`.
  - `DirTreeTraverser.collect()` mutates the provided `stats` dictionary and returns `None` like the current collect helper.
  - `BaiduStorageDirTransferExecutor.execute_transfer_plan()` returns `(success_count, successful_items, failed_items)`.
  - `ProgressReporter(progress_callback)` wraps legacy callbacks before passing into `DirTreeTraverser`.
- Scope check:
  - No `ShareLoader`, `TransferOrchestrator`, public return type, or global progress-callback replacement is included.
