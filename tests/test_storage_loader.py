#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from storage_loader import ShareLoader
from storage_progress import ProgressReporter
from utils import mask_share_url


class ShareLoaderTest(unittest.TestCase):
    def make_shared_path(
        self,
        uk=1,
        share_id=2,
        bdstoken="token",
        path="/share/movie.mp4",
    ):
        return SimpleNamespace(
            uk=uk,
            share_id=share_id,
            bdstoken=bdstoken,
            path=path,
            is_dir=False,
        )

    def make_loader(self, share_service=None, progress_messages=None, error_notifications=None):
        if share_service is None:
            share_service = Mock(spec_set=["load_shared_paths", "list_shared_files"])
        progress_messages = progress_messages if progress_messages is not None else []
        error_notifications = error_notifications if error_notifications is not None else []

        def notify_error(error, context_message, collect=True):
            error_notifications.append((error, context_message, collect))

        loader = ShareLoader(
            share_service,
            ProgressReporter(
                lambda level, message: progress_messages.append((level, message))
            ),
            notify_error,
        )
        return loader, share_service, progress_messages, error_notifications

    def test_load_entries_returns_context_with_first_share_metadata(self):
        first_shared_path = self.make_shared_path(
            uk=11,
            share_id=22,
            bdstoken="first-token",
            path="/share/first.mp4",
        )
        second_shared_path = self.make_shared_path(
            uk=99,
            share_id=88,
            bdstoken="second-token",
            path="/share/second.mp4",
        )
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.load_shared_paths.return_value = [first_shared_path, second_shared_path]

        result = loader.load_entries("https://pan.baidu.com/s/abc12345?pwd=1a2B", "1a2B")

        self.assertEqual(
            {
                "shared_paths": [first_shared_path, second_shared_path],
                "uk": 11,
                "share_id": 22,
                "bdstoken": "first-token",
            },
            result,
        )
        share_service.load_shared_paths.assert_called_once_with(
            "https://pan.baidu.com/s/abc12345?pwd=1a2B", "1a2B"
        )
        self.assertIn(
            (
                "info",
                f"【步骤1/4】访问分享链接: {mask_share_url('https://pan.baidu.com/s/abc12345?pwd=1a2B')}",
            ),
            progress_messages,
        )
        self.assertIn(("info", "使用密码访问分享链接"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_entries_without_password_does_not_report_password_message(self):
        shared_path = self.make_shared_path()
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.load_shared_paths.return_value = [shared_path]

        result = loader.load_entries("plain-share-url", None)

        self.assertEqual([shared_path], result["shared_paths"])
        self.assertIn(("info", "【步骤1/4】访问分享链接: plain-share-url"), progress_messages)
        self.assertNotIn(("info", "使用密码访问分享链接"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_entries_empty_share_notifies_and_returns_none(self):
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.load_shared_paths.return_value = []

        result = loader.load_entries("share-url", None)

        self.assertIsNone(result)
        self.assertEqual(1, len(error_notifications))
        error, context_message, collect = error_notifications[0]
        self.assertIsInstance(error, ValueError)
        self.assertEqual("获取分享文件列表失败", str(error))
        self.assertEqual("获取分享文件列表失败", context_message)
        self.assertTrue(collect)
        self.assertIn(("info", "【步骤1/4】访问分享链接: share-url"), progress_messages)

    def test_load_entries_propagates_share_service_error(self):
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.load_shared_paths.side_effect = RuntimeError("access failed")

        with self.assertRaises(RuntimeError):
            loader.load_entries("share-url", None)

        self.assertIn(("info", "【步骤1/4】访问分享链接: share-url"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_files_returns_copied_context_and_forwards_usable_callback(self):
        shared_path = self.make_shared_path()
        context = {
            "shared_paths": [shared_path],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        shared_files_info = [{"fs_id": 10, "path": "movie.mp4"}]
        calls = {}

        class FakeShareService:
            def load_shared_paths(self, share_url, password):
                raise AssertionError("load_shared_paths should not be called")

            def list_shared_files(
                self,
                shared_paths,
                folder_filter=None,
                progress_callback=None,
                exclude_folder_filter=None,
            ):
                calls["shared_paths"] = shared_paths
                calls["folder_filter"] = folder_filter
                calls["exclude_folder_filter"] = exclude_folder_filter
                if progress_callback is None:
                    raise AssertionError("progress_callback should be forwarded")
                progress_callback("info", "来自 SharedPathService 的进度")
                return shared_files_info

        loader, _share_service, progress_messages, error_notifications = self.make_loader(
            FakeShareService()
        )

        result = loader.load_files(context, "movies", r"^node_modules$")

        self.assertIsNot(result, context)
        self.assertNotIn("shared_files_info", context)
        self.assertEqual(shared_files_info, result["shared_files_info"])
        self.assertEqual([shared_path], result["shared_paths"])
        self.assertEqual([shared_path], calls["shared_paths"])
        self.assertEqual("movies", calls["folder_filter"])
        self.assertEqual(r"^node_modules$", calls["exclude_folder_filter"])
        self.assertIn(("info", "来自 SharedPathService 的进度"), progress_messages)
        self.assertIn(("info", "开始获取共享文件列表"), progress_messages)
        self.assertIn(("info", "获取到 1 个共享文件"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_files_with_default_progress_passes_none_callback(self):
        shared_path = self.make_shared_path()
        context = {
            "shared_paths": [shared_path],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        shared_files_info = [{"fs_id": 10, "path": "movie.mp4"}]
        calls = {}

        class FakeShareService:
            def list_shared_files(
                self,
                shared_paths,
                folder_filter=None,
                progress_callback=None,
                exclude_folder_filter=None,
            ):
                calls["shared_paths"] = shared_paths
                calls["folder_filter"] = folder_filter
                calls["progress_callback_is_none"] = progress_callback is None
                calls["exclude_folder_filter"] = exclude_folder_filter
                return shared_files_info

        loader = ShareLoader(FakeShareService())

        result = loader.load_files(context, "movies", r"^node_modules$")

        self.assertEqual(shared_files_info, result["shared_files_info"])
        self.assertEqual([shared_path], calls["shared_paths"])
        self.assertEqual("movies", calls["folder_filter"])
        self.assertTrue(calls["progress_callback_is_none"])
        self.assertEqual(r"^node_modules$", calls["exclude_folder_filter"])

    def test_load_files_returns_copied_context_for_empty_file_list(self):
        shared_path = self.make_shared_path()
        context = {
            "shared_paths": [shared_path],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        original_context = dict(context)
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.list_shared_files.return_value = []

        result = loader.load_files(context, "movies", r"^node_modules$")

        self.assertIsNot(result, context)
        self.assertEqual(original_context, context)
        self.assertEqual([], result["shared_files_info"])
        self.assertEqual([shared_path], result["shared_paths"])
        self.assertIn(("info", "获取到 0 个共享文件"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_files_propagates_list_error(self):
        shared_path = self.make_shared_path()
        context = {
            "shared_paths": [shared_path],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.list_shared_files.side_effect = RuntimeError("list failed")

        with self.assertRaises(RuntimeError):
            loader.load_files(context, None, None)

        self.assertIn(("info", "开始获取共享文件列表"), progress_messages)
        self.assertNotIn(("info", "获取到 0 个共享文件"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_context_combines_entries_and_files(self):
        shared_path = self.make_shared_path()
        shared_files_info = [{"fs_id": 10, "path": "movie.mp4"}]
        calls = {}

        class FakeShareService:
            def load_shared_paths(self, share_url, password):
                calls["load_shared_paths"] = (share_url, password)
                return [shared_path]

            def list_shared_files(
                self,
                shared_paths,
                folder_filter=None,
                progress_callback=None,
                exclude_folder_filter=None,
            ):
                calls["shared_paths"] = shared_paths
                calls["folder_filter"] = folder_filter
                calls["exclude_folder_filter"] = exclude_folder_filter
                calls["progress_callback_callable"] = callable(progress_callback)
                if not callable(progress_callback):
                    raise AssertionError("progress_callback should be callable")
                progress_callback("info", "来自 SharedPathService 的进度")
                return shared_files_info

        loader, _share_service, progress_messages, error_notifications = self.make_loader(
            FakeShareService()
        )

        result = loader.load_context("share-url", "pwd", "movies", r"^skip$")

        self.assertEqual([shared_path], result["shared_paths"])
        self.assertEqual(shared_files_info, result["shared_files_info"])
        self.assertEqual(("share-url", "pwd"), calls["load_shared_paths"])
        self.assertEqual([shared_path], calls["shared_paths"])
        self.assertEqual("movies", calls["folder_filter"])
        self.assertEqual(r"^skip$", calls["exclude_folder_filter"])
        self.assertTrue(calls["progress_callback_callable"])
        self.assertIn(("info", "使用密码访问分享链接"), progress_messages)
        self.assertIn(("info", "开始获取共享文件列表"), progress_messages)
        self.assertIn(("info", "来自 SharedPathService 的进度"), progress_messages)
        self.assertIn(("info", "获取到 1 个共享文件"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_context_stops_when_entries_fail(self):
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.load_shared_paths.return_value = []

        result = loader.load_context("share-url", None, "movies", r"^skip$")

        self.assertIsNone(result)
        share_service.list_shared_files.assert_not_called()
        self.assertEqual(1, len(error_notifications))
        self.assertIn(("info", "【步骤1/4】访问分享链接: share-url"), progress_messages)


if __name__ == "__main__":
    unittest.main()
