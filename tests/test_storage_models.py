#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from storage_models import TransferResult, TransferResultBuilder


class TransferResultTests(unittest.TestCase):
    def test_defaults_and_to_dict_success_shape(self):
        result = TransferResult(
            success=True,
            message="完成",
            transferred_files=[{"path": "a.mp4"}],
            skipped_count=2,
        )

        self.assertTrue(result.success)
        self.assertFalse(result.partial)
        self.assertEqual([], result.failed_files)
        self.assertEqual(2, result.skipped_count)

        self.assertEqual(
            {
                "success": True,
                "partial": False,
                "message": "完成",
                "transferred_files": [{"path": "a.mp4"}],
                "transfer_failed_files": [],
                "transfer_failed_count": 0,
                "rename_failed_files": [],
                "rename_failed_count": 0,
                "completed_count": 1,
                "transfer_success_count": 1,
                "skipped_count": 2,
            },
            result.to_dict(),
        )

    def test_success_with_failed_files_is_invalid(self):
        with self.assertRaisesRegex(ValueError, "success=True"):
            TransferResult(
                success=True,
                failed_files=[{"path": "failed.mp4"}],
            )

    def test_success_to_dict_omits_skipped_when_not_skipped(self):
        result = TransferResult(success=True)

        self.assertNotIn("skipped", result.to_dict())

    def test_default_file_lists_are_not_shared(self):
        first = TransferResult(success=True)
        second = TransferResult(success=True)

        self.assertIsNot(first.transferred_files, second.transferred_files)
        self.assertIsNot(first.failed_files, second.failed_files)

        first.transferred_files.append({"path": "first.mp4"})

        self.assertEqual([], second.transferred_files)
        self.assertEqual([], second.failed_files)

    def test_from_dict_reconstructs_result(self):
        result = TransferResult.from_dict(
            {
                "success": False,
                "partial": True,
                "message": "部分成功",
                "transferred_files": [{"path": "ok.mp4"}],
                "transfer_failed_files": [{"path": "bad.mp4"}],
                "transfer_failed_count": 1,
                "rename_failed_files": [],
                "rename_failed_count": 0,
                "completed_count": 1,
                "transfer_success_count": 1,
                "skipped_count": 3,
                "error": "部分成功",
            }
        )

        self.assertFalse(result.success)
        self.assertTrue(result.partial)
        self.assertEqual("部分成功", result.message)
        self.assertEqual([{"path": "ok.mp4"}], result.transferred_files)
        self.assertEqual([{"path": "bad.mp4"}], result.failed_files)
        self.assertEqual(3, result.skipped_count)
        self.assertEqual("部分成功", result.error_details)

    def test_from_dict_accepts_error_only_failure_shape(self):
        result = TransferResult.from_dict({"success": False, "error": "失败"})

        self.assertFalse(result.success)
        self.assertFalse(result.partial)
        self.assertEqual("失败", result.message)
        self.assertEqual([], result.transferred_files)
        self.assertEqual([], result.failed_files)
        self.assertEqual(0, result.skipped_count)
        self.assertEqual("失败", result.error_details)

    def test_from_dict_accepts_skipped_success_shape(self):
        result = TransferResult.from_dict(
            {"success": True, "skipped": True, "message": "没有新文件"}
        )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertFalse(result.partial)
        self.assertEqual("没有新文件", result.message)
        self.assertEqual([], result.transferred_files)
        self.assertEqual([], result.failed_files)
        self.assertEqual(0, result.skipped_count)
        self.assertIsNone(result.error_details)
        self.assertIs(result.to_dict()["skipped"], True)

    def test_from_dict_requires_success_key(self):
        with self.assertRaisesRegex(KeyError, "success"):
            TransferResult.from_dict({})


class TransferResultBuilderTests(unittest.TestCase):
    def test_builder_chain_builds_success_result(self):
        builder = TransferResultBuilder()

        self.assertIs(builder.set_message("完成"), builder)
        self.assertIs(builder.add_transferred({"path": "a.mp4"}), builder)
        self.assertIs(builder.set_skipped(1), builder)

        result = builder.build()

        self.assertTrue(result.success)
        self.assertFalse(result.partial)
        self.assertEqual("完成", result.message)
        self.assertEqual([{"path": "a.mp4"}], result.transferred_files)
        self.assertEqual([], result.failed_files)
        self.assertEqual(1, result.skipped_count)
        self.assertIsNone(result.error_details)

    def test_builder_builds_failure_result(self):
        result = TransferResultBuilder().set_message("失败").set_error("转存失败").build()

        self.assertFalse(result.success)
        self.assertFalse(result.partial)
        self.assertEqual("失败", result.message)
        self.assertEqual("转存失败", result.error_details)

    def test_builder_builds_partial_result(self):
        result = (
            TransferResultBuilder()
            .add_transferred({"path": "ok.mp4"})
            .add_failed({"path": "bad.mp4"})
            .set_partial(True)
            .set_message("部分成功")
            .build()
        )

        self.assertFalse(result.success)
        self.assertTrue(result.partial)
        self.assertEqual("部分成功", result.message)
        self.assertEqual([{"path": "ok.mp4"}], result.transferred_files)
        self.assertEqual([{"path": "bad.mp4"}], result.failed_files)

    def test_builder_failed_file_without_partial_builds_failure(self):
        result = TransferResultBuilder().add_failed({"path": "bad.mp4"}).build()

        self.assertFalse(result.success)
        self.assertFalse(result.partial)
        self.assertEqual([{"path": "bad.mp4"}], result.failed_files)

    def test_builder_empty_error_builds_failure(self):
        result = TransferResultBuilder().set_error("").build()

        self.assertFalse(result.success)
        self.assertFalse(result.partial)
        self.assertEqual("", result.error_details)

    def test_builder_raises_when_built_twice(self):
        builder = TransferResultBuilder()
        builder.build()

        with self.assertRaisesRegex(RuntimeError, "TransferResultBuilder"):
            builder.build()

    def test_builder_raises_when_mutated_after_build(self):
        builder = TransferResultBuilder()
        builder.build()

        with self.assertRaisesRegex(RuntimeError, "TransferResultBuilder"):
            builder.set_message("完成")


if __name__ == "__main__":
    unittest.main()
