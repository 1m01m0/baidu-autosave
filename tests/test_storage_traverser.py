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
        if path not in self.children_by_path:
            raise AssertionError(f"unexpected shared dir path: {path}")
        children = self.children_by_path[path]
        if callable(children):
            return children()
        return iter(children)


class FakeTransferExecutor:
    def __init__(self):
        self.transfer_plan_batches = []
        self.group_calls = []
        self.group_side_effects = []
        self.cache_clear_calls = []

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

    def clear_local_files_cache(self, target_dir, affected_relative_dirs=None):
        self.cache_clear_calls.append((target_dir, affected_relative_dirs))


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
        traverser, _, _, executor, _, error_notifications = self.make_traverser(
            children_by_path
        )
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

        expected_target_dir = "/save/course/" + "/".join(
            f"d{index}" for index in range(depth)
        )
        first_batch = executor.transfer_plan_batches[0]
        items = first_batch["items"]
        self.assertTrue(result["success"])
        self.assertEqual(1, result["completed_count"])
        self.assertEqual(expected_target_dir, first_batch["target_dir"])
        self.assertEqual(expected_target_dir, items[0].dir_path)
        self.assertEqual("file.txt", items[0].clean_path)
        self.assertEqual("file.txt", items[0].final_path)
        self.assertEqual([999], [item.fs_id for item in items])
        self.assertEqual([], error_notifications)

    def test_file_children_flush_by_batch_size(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        batch_size = 2
        children = [
            self.file_child("/share/course", f"{fs_id}.txt", fs_id)
            for fs_id in range(1, batch_size * 2 + 2)
        ]
        traverser, _, _, executor, _, error_notifications = self.make_traverser(
            {"/share/course": children}, batch_size=batch_size
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
        self.assertEqual(
            [2, 2, 1],
            [len(batch["items"]) for batch in executor.transfer_plan_batches],
        )
        self.assertEqual(
            [[1, 2], [3, 4], [5]],
            [
                [item.fs_id for item in batch["items"]]
                for batch in executor.transfer_plan_batches
            ],
        )
        self.assertEqual([], error_notifications)

    def test_full_file_batch_flushes_before_generator_is_exhausted(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        batch_size = 2
        call_counts_after_full_batch = []
        executor_ref = {}

        def children():
            for fs_id in range(1, batch_size + 1):
                yield self.file_child("/share/course", f"{fs_id}.txt", fs_id)
            call_counts_after_full_batch.append(
                len(executor_ref["executor"].transfer_plan_batches)
            )
            yield self.file_child(
                "/share/course", f"{batch_size + 1}.txt", batch_size + 1
            )

        traverser, _, _, executor, _, error_notifications = self.make_traverser(
            {"/share/course": children}, batch_size=batch_size
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
        self.assertEqual(batch_size + 1, result["completed_count"])
        self.assertEqual([1], call_counts_after_full_batch)
        self.assertEqual(
            [2, 1],
            [len(batch["items"]) for batch in executor.transfer_plan_batches],
        )
        self.assertEqual([], error_notifications)

    def test_child_file_md5_is_preserved_in_transfer_item(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        traverser, _, _, executor, _, error_notifications = self.make_traverser(
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
        self.assertEqual([], error_notifications)

    def test_excluded_folder_is_skipped(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        (
            traverser,
            _,
            share_service,
            executor,
            progress_messages,
            error_notifications,
        ) = self.make_traverser(
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
        self.assertEqual([], error_notifications)

    def test_child_directory_group_transfer_succeeds_without_scanning_child(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        child_dir = self.dir_child("/share/course", "big", 20)
        (
            traverser,
            _,
            share_service,
            executor,
            _,
            error_notifications,
        ) = self.make_traverser({"/share/course": [child_dir]})

        result = traverser.traverse(
            shared_dir,
            "/save/course",
            {"uk": 1, "share_id": 2, "bdstoken": "token"},
            "url",
            None,
        )

        self.assertTrue(result["success"])
        self.assertEqual(1, result["completed_count"])
        self.assertEqual(["big"], result["transferred_files"])
        self.assertEqual(
            [
                {
                    "dir_path": "/save/course",
                    "fs_ids": [20],
                    "share_url": "url",
                    "uk": 1,
                    "share_id": 2,
                    "bdstoken": "token",
                }
            ],
            executor.group_calls,
        )
        self.assertEqual([("/share/course", 1, 2, "token")], share_service.calls)
        self.assertEqual([], executor.transfer_plan_batches)
        self.assertEqual([("/save/course", {"big"})], executor.cache_clear_calls)
        self.assertEqual([], error_notifications)

    def test_count_limit_dir_transfer_pushes_child_to_stack(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        child_dir = self.dir_child("/share/course", "big", 20)
        children_by_path = {
            "/share/course": [child_dir],
            "/share/course/big": [self.file_child("/share/course/big", "a.txt", 21)],
        }
        (
            traverser,
            path_service,
            share_service,
            executor,
            progress_messages,
            error_notifications,
        ) = self.make_traverser(children_by_path)
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

        first_batch = executor.transfer_plan_batches[0]
        items = first_batch["items"]
        self.assertTrue(result["success"])
        self.assertEqual(1, result["completed_count"])
        self.assertEqual(
            [
                {
                    "dir_path": "/save/course",
                    "fs_ids": [20],
                    "share_url": "url",
                    "uk": 1,
                    "share_id": 2,
                    "bdstoken": "token",
                }
            ],
            executor.group_calls,
        )
        self.assertEqual([], executor.group_side_effects)
        self.assertEqual(
            [("/share/course", 1, 2, "token"), ("/share/course/big", 1, 2, "token")],
            share_service.calls,
        )
        self.assertEqual(
            ["/save/course", "/save/course/big"], path_service.ensured_dirs
        )
        self.assertIn(("warning", "子目录超量，继续拆分: big"), progress_messages)
        self.assertEqual("/save/course/big", first_batch["target_dir"])
        self.assertEqual("/save/course/big", items[0].dir_path)
        self.assertEqual("a.txt", items[0].clean_path)
        self.assertEqual("a.txt", items[0].final_path)
        self.assertEqual([21], [item.fs_id for item in items])
        self.assertEqual([], error_notifications)

    def test_child_directory_creation_failure_after_count_limit_reports_progress_only(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        child_dir = self.dir_child("/share/course", "big", 20)
        children_by_path = {
            "/share/course": [child_dir],
            "/share/course/big": [self.file_child("/share/course/big", "a.txt", 21)],
        }
        (
            traverser,
            _,
            _,
            executor,
            progress_messages,
            error_notifications,
        ) = self.make_traverser(
            children_by_path, failing_dirs={"/save/course/big"}
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

        self.assertFalse(result["success"])
        self.assertEqual(1, result["failed_count"])
        self.assertEqual(
            [
                {
                    "dir_path": "/save/course",
                    "fs_ids": [20],
                    "share_url": "url",
                    "uk": 1,
                    "share_id": 2,
                    "bdstoken": "token",
                }
            ],
            executor.group_calls,
        )
        self.assertEqual([], executor.group_side_effects)
        self.assertEqual([], executor.transfer_plan_batches)
        self.assertIn(("error", "创建目录失败: /save/course/big"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_directory_creation_failure_returns_failure_result(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        traverser, _, share_service, executor, progress_messages, _ = self.make_traverser(
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
        self.assertEqual([], share_service.calls)
        self.assertEqual([], executor.transfer_plan_batches)
        self.assertEqual([], executor.group_calls)
        self.assertIn(("error", "创建目录失败: /save/course"), progress_messages)

    def test_progress_reports_scan_completion_and_final_success(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        traverser, _, _, _, progress_messages, error_notifications = self.make_traverser(
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
        self.assertEqual([], error_notifications)


if __name__ == "__main__":
    unittest.main()
