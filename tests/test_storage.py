import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

if "baidupcs_py" not in sys.modules:
    baidupcs_module = types.ModuleType("baidupcs_py")
    baidupcs_submodule = types.ModuleType("baidupcs_py.baidupcs")

    class DummyBaiduPCSApi:
        pass

    baidupcs_submodule.BaiduPCSApi = DummyBaiduPCSApi
    baidupcs_module.baidupcs = baidupcs_submodule
    sys.modules["baidupcs_py"] = baidupcs_module
    sys.modules["baidupcs_py.baidupcs"] = baidupcs_submodule

from storage import (
    BaiduStorage,
    BATCH_SHARE_DELAY,
    FREQUENCY_LIMIT_DELAY,
    RENAME_DELAY,
    TRANSFER_BATCH_SIZE,
    _read_non_negative_float_env,
    _read_positive_int_env,
)
from storage_client import BaiduClientAdapter
from storage_errors import classify_storage_error, is_transfer_count_limit_error, parse_share_error
from storage_paths import StoragePathService
from storage_rules import apply_regex_rules, should_include_folder
from storage_shares import SharedPathService
from utils import format_error_info
from wechat_notifier import WeChatNotifier


class BaiduStoragePureMethodTests(unittest.TestCase):
    def setUp(self):
        self.storage = BaiduStorage.__new__(BaiduStorage)
        self.storage.path_service = Mock()
        self.storage.share_service = Mock()

    def test_parse_share_error_maps_known_cases(self):
        self.assertEqual(
            "分享链接已失效（文件禁止分享）",
            parse_share_error("error_code: 115"),
        )
        self.assertEqual(
            "提取码输入错误，请检查提取码",
            parse_share_error("{'errno': 200025}"),
        )
        self.assertEqual(
            "网络请求失败，请检查网络连接或稍后重试",
            parse_share_error("BaiduPCS._request timeout"),
        )

    def test_parse_share_error_simplifies_long_json_errors(self):
        long_error = "{" + "'errno': 999, " + "'message': 'x'" * 80 + "}"

        result = parse_share_error(long_error)

        self.assertEqual("分享链接访问失败（错误码：999）", result)

    def test_apply_regex_rules_handles_match_replace_and_invalid_pattern(self):
        self.assertEqual((True, "/dir/file.mp4"), apply_regex_rules("/dir/file.mp4"))
        self.assertEqual((False, "/dir/file.txt"), apply_regex_rules("/dir/file.txt", r"\\.mp4$"))
        self.assertEqual(
            (True, "/dir/video.mp4"),
            apply_regex_rules("/dir/file.mp4", r"file", "video"),
        )
        self.assertEqual((True, "/dir/file.mp4"), apply_regex_rules("/dir/file.mp4", "["))

    def test_should_include_folder_supports_none_string_list_and_invalid_regex(self):
        self.assertTrue(should_include_folder("Movies"))
        self.assertTrue(should_include_folder("Movies-2026", r"Movies"))
        self.assertFalse(should_include_folder("Shows-2026", r"Movies"))
        self.assertTrue(should_include_folder("Anime", [r"Movies", r"Anime"]))
        self.assertTrue(should_include_folder("Anything", "["))

    def test_classify_storage_error_supports_rate_limit_missing_path_and_exists(self):
        rate_limit = classify_storage_error("error_code: -65")
        self.assertEqual("rate_limit", rate_limit.kind)
        self.assertTrue(rate_limit.retryable)

        missing_path = classify_storage_error("error_code: 31066, message: 文件不存在")
        self.assertEqual("missing_path", missing_path.kind)

        already_exists = classify_storage_error("error_code: 31061, message: 文件已经存在")
        self.assertEqual("already_exists", already_exists.kind)

    def test_read_non_negative_float_env_falls_back_for_invalid_values(self):
        env_name = "TRANSFERSHARE_TEST_DELAY"
        with patch.dict(os.environ, {env_name: "0.25"}):
            self.assertEqual(0.25, _read_non_negative_float_env(env_name, 2))
        with patch.dict(os.environ, {env_name: "bad"}):
            self.assertEqual(2, _read_non_negative_float_env(env_name, 2))
        with patch.dict(os.environ, {env_name: "-1"}):
            self.assertEqual(2, _read_non_negative_float_env(env_name, 2))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(2, _read_non_negative_float_env(env_name, 2))

    def test_read_positive_int_env_falls_back_for_invalid_values(self):
        env_name = "TRANSFERSHARE_TEST_BATCH_SIZE"
        with patch.dict(os.environ, {env_name: "999"}):
            self.assertEqual(999, _read_positive_int_env(env_name, 1))
        with patch.dict(os.environ, {env_name: "0"}):
            self.assertEqual(1, _read_positive_int_env(env_name, 1))
        with patch.dict(os.environ, {env_name: "bad"}):
            self.assertEqual(1, _read_positive_int_env(env_name, 1))

    def test_transfer_count_limit_error_detection(self):
        self.assertTrue(is_transfer_count_limit_error("error_code: -33"))
        self.assertTrue(is_transfer_count_limit_error("error_code: 120, message: 转存文件数超限"))
        self.assertTrue(is_transfer_count_limit_error("error_code: 4, message: share transfer pcs error"))
        self.assertTrue(is_transfer_count_limit_error("一次支持操作999个，减点试试吧"))
        self.assertFalse(is_transfer_count_limit_error("error_code: 4, message: 请求被中止"))
        self.assertFalse(is_transfer_count_limit_error("error_code: -32, message: 剩余空间不足"))

    def test_format_error_info_masks_share_urls_and_pwd(self):
        error = ValueError(
            "分享链接: https://pan.baidu.com/s/abc12345?pwd=1a2B&foo=bar, 备用: surl=xyz987"
        )

        result = format_error_info(error, "处理失败")

        self.assertIn("https://pan.baidu.com/s/***?pwd=***&foo=bar", result)
        self.assertIn("surl=***", result)
        self.assertNotIn("abc12345", result)
        self.assertNotIn("1a2B", result)
        self.assertNotIn("xyz987", result)

    def test_format_error_info_masks_standalone_surl_in_plain_text(self):
        error = ValueError("普通文本里有备用码 surl=xyz987，可直接打开")

        result = format_error_info(error, "处理失败")

        self.assertIn("surl=***", result)
        self.assertNotIn("xyz987", result)


class SharedPathServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = SharedPathService(Mock())

    def test_resolve_shared_root_uses_common_parent(self):
        shared_paths = [
            SimpleNamespace(path="/sharelink123-456/电影", is_dir=True),
            SimpleNamespace(path="/sharelink123-456/单集.mp4", is_dir=False),
        ]

        self.assertEqual(
            "/sharelink123-456", self.service._resolve_shared_root(shared_paths)
        )

    def test_trim_shared_root_keeps_relative_structure(self):
        self.assertEqual(
            "电影/单集.mp4",
            self.service._trim_shared_root(
                "/sharelink123-456/电影/单集.mp4", "/sharelink123-456"
            ),
        )
        self.assertEqual(
            "单集.mp4",
            self.service._trim_shared_root(
                "/single-share/单集.mp4", "/single-share"
            ),
        )

    def test_list_shared_files_trims_root_without_hardcoded_sharelink_prefix(self):
        shared_paths = [
            SimpleNamespace(
                uk=1,
                share_id=2,
                bdstoken="token",
                is_dir=False,
                path="/single-share/单集.mp4",
                fs_id=10,
                size=123,
                md5="abc",
            )
        ]

        files = self.service.list_shared_files(shared_paths)

        self.assertEqual(
            [
                {
                    "server_filename": "单集.mp4",
                    "fs_id": 10,
                    "path": "单集.mp4",
                    "size": 123,
                    "isdir": 0,
                    "md5": "abc",
                }
            ],
            files,
        )

    def test_resolve_shared_root_keeps_top_level_dir_when_only_root_node_exists(self):
        shared_paths = [
            SimpleNamespace(
                path="/single-share",
                is_dir=True,
                uk=1,
                share_id=2,
                bdstoken="token",
            )
        ]

        self.assertEqual("/single-share", self.service._resolve_shared_root(shared_paths))

    def test_list_shared_files_trims_nested_paths_when_only_root_dir_node_is_provided(self):
        root_dir = SimpleNamespace(
            path="/single-share",
            is_dir=True,
            uk=1,
            share_id=2,
            bdstoken="token",
        )
        nested_file = SimpleNamespace(
            path="/single-share/子目录/单集.mp4",
            is_dir=False,
            fs_id=11,
            size=456,
            md5="def",
        )
        self.service.client.list_shared_paths.return_value = [nested_file]

        files = self.service.list_shared_files([root_dir])

        self.assertEqual(
            [
                {
                    "server_filename": "单集.mp4",
                    "fs_id": 11,
                    "path": "子目录/单集.mp4",
                    "size": 456,
                    "isdir": 0,
                    "md5": "def",
                }
            ],
            files,
        )

    def test_list_shared_files_reports_throttled_progress(self):
        root_dir = SimpleNamespace(
            path="/single-share",
            is_dir=True,
            uk=1,
            share_id=2,
            bdstoken="token",
        )
        nested_file = SimpleNamespace(
            path="/single-share/单集.mp4",
            is_dir=False,
            fs_id=11,
            size=456,
            md5="def",
        )
        progress_callback = Mock()
        self.service.client.list_shared_paths.return_value = [nested_file]

        files = self.service.list_shared_files([root_dir], progress_callback=progress_callback)

        self.assertEqual(1, len(files))
        messages = [call.args[1] for call in progress_callback.call_args_list]
        self.assertTrue(any("开始获取共享文件列表，共 1 个入口" in msg for msg in messages))
        self.assertTrue(any("开始扫描共享入口 1/1: /single-share" in msg for msg in messages))
        self.assertTrue(any("共享文件列表获取完成：扫描 1 个目录 / 1 页，发现 1 个文件" in msg for msg in messages))
        self.assertFalse(any("正在获取共享目录第" in msg for msg in messages))


class StoragePathServiceTests(unittest.TestCase):
    def test_list_local_files_in_dirs_only_scans_candidate_dirs(self):
        client = Mock()
        client.list.side_effect = [
            [
                SimpleNamespace(
                    is_file=True,
                    is_dir=False,
                    path="/save/A/a.txt",
                    md5="md5-a",
                )
            ],
            [
                SimpleNamespace(
                    is_file=True,
                    is_dir=False,
                    path="/save/B/C/c.txt",
                    md5="md5-c",
                )
            ],
        ]
        service = StoragePathService(client)

        result = service.list_local_files_in_dirs("/save", {"A", "B/C"}, use_cache=True)

        self.assertEqual(
            [
                {"relative_path": "A/a.txt", "file_name": "a.txt", "md5": "md5-a"},
                {"relative_path": "B/C/c.txt", "file_name": "c.txt", "md5": "md5-c"},
            ],
            result,
        )
        self.assertEqual([call("/save/A"), call("/save/B/C")], client.list.call_args_list)

    def test_list_local_files_in_dirs_merges_sibling_dirs_when_enabled(self):
        client = Mock()
        listings = {
            "/save/A": [
                SimpleNamespace(is_file=True, is_dir=False, path="/save/A/root.txt", md5="md5-root"),
                SimpleNamespace(is_file=False, is_dir=True, path="/save/A/1"),
                SimpleNamespace(is_file=False, is_dir=True, path="/save/A/2"),
                SimpleNamespace(is_file=False, is_dir=True, path="/save/A/other"),
            ],
            "/save/A/1": [
                SimpleNamespace(is_file=True, is_dir=False, path="/save/A/1/a.txt", md5="md5-a"),
                SimpleNamespace(is_file=False, is_dir=True, path="/save/A/1/sub"),
            ],
            "/save/A/2": [
                SimpleNamespace(is_file=True, is_dir=False, path="/save/A/2/b.txt", md5="md5-b")
            ],
            "/save/A/other": [
                SimpleNamespace(is_file=True, is_dir=False, path="/save/A/other/c.txt", md5="md5-c")
            ],
        }
        client.list.side_effect = lambda path: listings[path]
        service = StoragePathService(client)

        result = service.list_local_files_in_dirs(
            "/save", {"A/1", "A/2"}, merge_dirs=True
        )

        self.assertEqual(
            [
                {"relative_path": "A/1/a.txt", "file_name": "a.txt", "md5": "md5-a"},
                {"relative_path": "A/2/b.txt", "file_name": "b.txt", "md5": "md5-b"},
            ],
            result,
        )
        self.assertEqual([call("/save/A"), call("/save/A/1"), call("/save/A/2")], client.list.call_args_list)

    def test_list_local_files_in_dirs_cache_separates_merge_mode(self):
        client = Mock()
        listings = {
            "/save/A": [
                SimpleNamespace(is_file=False, is_dir=True, path="/save/A/1"),
                SimpleNamespace(is_file=False, is_dir=True, path="/save/A/2"),
            ],
            "/save/A/1": [
                SimpleNamespace(is_file=True, is_dir=False, path="/save/A/1/a.txt", md5="md5-a")
            ],
            "/save/A/2": [
                SimpleNamespace(is_file=True, is_dir=False, path="/save/A/2/b.txt", md5="md5-b")
            ],
        }
        client.list.side_effect = lambda path: listings[path]
        service = StoragePathService(client)

        direct_result = service.list_local_files_in_dirs(
            "/save", {"A/1", "A/2"}, use_cache=True
        )
        merged_result = service.list_local_files_in_dirs(
            "/save", {"A/1", "A/2"}, use_cache=True, merge_dirs=True
        )

        self.assertEqual(direct_result, merged_result)
        self.assertEqual(
            [call("/save/A/1"), call("/save/A/2"), call("/save/A"), call("/save/A/1"), call("/save/A/2")],
            client.list.call_args_list,
        )

    def test_list_local_files_in_dirs_handles_root_target_dir(self):
        client = Mock()
        client.list.return_value = [
            SimpleNamespace(is_file=True, is_dir=False, path="/a.txt", md5="root-md5")
        ]
        service = StoragePathService(client)

        result = service.list_local_files_in_dirs("/", {""})

        self.assertEqual(
            [{"relative_path": "a.txt", "file_name": "a.txt", "md5": "root-md5"}],
            result,
        )
        client.list.assert_called_once_with("/")

    def test_list_local_files_in_dirs_treats_missing_dir_as_empty(self):
        client = Mock()
        client.list.side_effect = RuntimeError("error_code: 31066, message: 文件不存在")
        service = StoragePathService(client)

        with patch("storage_paths.handle_error_and_notify") as notify:
            result = service.list_local_files_in_dirs("/save", {"missing"})

        self.assertEqual([], result)
        client.list.assert_called_once_with("/save/missing")
        notify.assert_not_called()

    def test_ensure_dir_exists_ignores_existing_directory_error(self):
        client = Mock()
        client.makedir.side_effect = RuntimeError("error_code: 31061, message: 文件已经存在")
        service = StoragePathService(client)

        with patch("storage_paths.handle_error_and_notify") as notify:
            result = service.ensure_dir_exists("/save/a")

        self.assertTrue(result)
        self.assertEqual([call("/save"), call("/save/a")], client.makedir.call_args_list)
        client.list.assert_not_called()
        notify.assert_not_called()

    def test_ensure_dir_exists_caches_confirmed_prefixes(self):
        client = Mock()
        service = StoragePathService(client)

        with patch("storage_paths.handle_error_and_notify") as notify:
            first_result = service.ensure_dir_exists("/save/a")
            second_result = service.ensure_dir_exists("/save/a/b")

        self.assertTrue(first_result)
        self.assertTrue(second_result)
        self.assertEqual(
            [call("/save"), call("/save/a"), call("/save/a/b")],
            client.makedir.call_args_list,
        )
        client.list.assert_not_called()
        notify.assert_not_called()

    def test_ensure_dir_exists_retries_transient_empty_response(self):
        client = Mock()
        client.makedir.side_effect = [
            RuntimeError("Expecting value: line 1 column 1 (char 0)"),
            None,
        ]
        client.list.side_effect = [RuntimeError("error_code: 31066, message: 文件不存在")]
        service = StoragePathService(client)

        with patch("storage_paths.time.sleep") as sleep, patch("storage_paths.handle_error_and_notify") as notify:
            result = service.ensure_dir_exists("/save")

        self.assertTrue(result)
        self.assertEqual([call("/save"), call("/save")], client.makedir.call_args_list)
        client.list.assert_called_once_with("/save")
        sleep.assert_called_once()
        notify.assert_not_called()

    def test_ensure_dir_exists_confirms_after_transient_empty_response(self):
        client = Mock()
        client.makedir.side_effect = RuntimeError("Expecting value: line 1 column 1 (char 0)")
        client.list.return_value = []
        service = StoragePathService(client)

        with patch("storage_paths.handle_error_and_notify") as notify:
            result = service.ensure_dir_exists("/save")

        self.assertTrue(result)
        client.list.assert_called_once_with("/save")
        notify.assert_not_called()

    def test_list_local_files_treats_root_31023_as_empty_dir(self):
        client = Mock()
        client.list.side_effect = RuntimeError("error_code: 31023, message: 输入参数错误")
        service = StoragePathService(client)

        with patch("storage_paths.handle_error_and_notify") as notify:
            result = service.list_local_files("/考公/2026/政治理论常识背诵手册", use_cache=True)

        self.assertEqual([], result)
        client.list.assert_called_once_with("/考公/2026/政治理论常识背诵手册")
        notify.assert_not_called()


class WeChatNotifierTests(unittest.TestCase):
    def test_mask_sensitive_uses_shared_helper(self):
        notifier = WeChatNotifier("https://example.com")
        text = "分享链接: https://pan.baidu.com/s/abc12345?pwd=1a2B surl=xyz987"

        masked = notifier._mask_sensitive(text)

        self.assertEqual(
            "分享链接: https://pan.baidu.com/s/***?pwd=*** surl=***",
            masked,
        )
        self.assertNotIn("abc12345", masked)
        self.assertNotIn("1a2B", masked)
        self.assertNotIn("xyz987", masked)


class BaiduStorageFlowTests(unittest.TestCase):
    def setUp(self):
        self.storage = BaiduStorage.__new__(BaiduStorage)
        self.storage.client = Mock()
        self.storage.wechat_notifier = None
        self.storage._local_files_cache = {}
        self.storage.path_service = Mock()
        self.storage.share_service = Mock()

    def test_transfer_share_returns_skipped_when_no_transfer_candidates(self):
        self.storage._normalize_save_dir = Mock(return_value="/save")
        entry_context = {
            "shared_paths": [Mock(is_dir=False)],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock(
            return_value={**entry_context, "shared_files_info": []}
        )
        self.storage._scan_local_files_dict = Mock(return_value={})

        result = self.storage.transfer_share("https://pan.baidu.com/s/abc")

        self.assertEqual(
            {"success": True, "skipped": True, "message": "没有新文件需要转存"},
            result,
        )

    def test_transfer_share_returns_dir_error_directly(self):
        self.storage._normalize_save_dir = Mock(return_value="/save")
        entry_context = {
            "shared_paths": [Mock(is_dir=False)],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock(
            return_value={**entry_context, "shared_files_info": [{"fs_id": 1, "path": "a.txt"}]}
        )
        self.storage._scan_local_files_dict = Mock(return_value={})
        self.storage._ensure_transfer_dirs = Mock(
            return_value={"success": False, "error": "创建目录失败: /save"}
        )

        result = self.storage.transfer_share("https://pan.baidu.com/s/abc")

        self.assertEqual({"success": False, "error": "创建目录失败: /save"}, result)

    def test_scan_local_files_dict_uses_merged_candidate_dir_scan(self):
        self.storage.path_service.list_local_files_in_dirs.return_value = []

        result = self.storage._scan_local_files_dict("/save", relative_dirs={"A/1", "A/2"})

        self.assertEqual({}, result)
        self.storage.path_service.list_local_files_in_dirs.assert_called_once_with(
            "/save", {"A/1", "A/2"}, use_cache=True, merge_dirs=True
        )

    def test_transfer_share_executes_plan_and_builds_result(self):
        self.storage._normalize_save_dir = Mock(return_value="/save")
        entry_context = {
            "shared_paths": [Mock(is_dir=False)],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock(
            return_value={**entry_context, "shared_files_info": [{"fs_id": 1, "path": "a.txt"}]}
        )
        self.storage._scan_local_files_dict = Mock(return_value={})
        transfer_list = [(1, "/save", "a.txt", "a.txt", False)]
        self.storage._ensure_transfer_dirs = Mock(return_value=None)
        self.storage._execute_transfer_plan = Mock(return_value=(1, transfer_list))
        rename_result = {
            "transferred_files": ["a.txt"],
            "rename_failed_files": [],
            "rename_failed_count": 0,
            "completed_count": 1,
        }
        self.storage._rename_transferred_files = Mock(return_value=rename_result)
        self.storage._build_transfer_result = Mock(
            return_value={"success": True, "message": "成功转存 1/1 个文件", "transferred_files": ["a.txt"]}
        )

        result = self.storage.transfer_share("https://pan.baidu.com/s/abc")

        self.assertTrue(result["success"])
        self.storage._execute_transfer_plan.assert_called_once()
        self.storage._rename_transferred_files.assert_called_once_with(transfer_list, "/save", None)
        self.storage._build_transfer_result.assert_called_once_with(
            1,
            1,
            {
                "transferred_files": ["a.txt"],
                "rename_failed_files": [],
                "rename_failed_count": 0,
                "completed_count": 1,
            },
            None,
        )

    def test_transfer_share_uses_dir_fast_path_for_single_directory(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/course")
        entry_context = {
            "shared_paths": [shared_dir],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock()
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.return_value = []

        result = self.storage.transfer_share("url", save_dir="/save")

        self.assertTrue(result["success"])
        self.assertTrue(result["fast_path"])
        self.storage._load_share_files.assert_not_called()
        self.storage.client.transfer_shared_paths.assert_called_once_with(
            remotedir="/save",
            fs_ids=[10],
            uk=1,
            share_id=2,
            bdstoken="token",
            shared_url="url",
        )

    def test_transfer_share_falls_back_when_dir_fast_path_hits_count_limit(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/course")
        entry_context = {
            "shared_paths": [shared_dir],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock(
            return_value={**entry_context, "shared_files_info": [{"fs_id": 11, "path": "a.txt"}]}
        )
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.return_value = []
        self.storage.client.transfer_shared_paths.side_effect = RuntimeError(
            "error_code: -33, message: 一次支持操作999个"
        )
        self.storage._scan_local_files_dict = Mock(return_value={})
        transfer_list = [(11, "/save/course", "a.txt", "a.txt", False)]
        self.storage._ensure_transfer_dirs = Mock(return_value=None)
        self.storage._execute_transfer_plan = Mock(return_value=(1, transfer_list))
        rename_result = {
            "transferred_files": ["a.txt"],
            "rename_failed_files": [],
            "rename_failed_count": 0,
            "completed_count": 1,
        }
        self.storage._rename_transferred_files = Mock(return_value=rename_result)
        self.storage._build_transfer_result = Mock(return_value={"success": True})

        result = self.storage.transfer_share("url", save_dir="/save")

        self.assertTrue(result["success"])
        self.storage._load_share_files.assert_called_once()
        self.storage._scan_local_files_dict.assert_called_once_with("/save/course", None, {""})
        self.storage._execute_transfer_plan.assert_called_once_with(
            transfer_list, "url", 1, 2, "token", "/save/course", None
        )

    def test_transfer_share_does_not_fallback_for_unknown_fast_path_error(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/course")
        entry_context = {
            "shared_paths": [shared_dir],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock()
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.return_value = []
        self.storage.client.transfer_shared_paths.side_effect = RuntimeError("boom")

        result = self.storage.transfer_share("url", save_dir="/save")

        self.assertFalse(result["success"])
        self.assertEqual("boom", result["error"])
        self.storage._load_share_files.assert_not_called()

    def test_transfer_share_falls_back_when_target_folder_exists(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/course")
        entry_context = {
            "shared_paths": [shared_dir],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock(return_value={**entry_context, "shared_files_info": []})
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.return_value = [SimpleNamespace(path="/save/course")]

        result = self.storage.transfer_share("url", save_dir="/save")

        self.assertTrue(result["skipped"])
        self.storage.client.transfer_shared_paths.assert_not_called()
        self.storage._load_share_files.assert_called_once()

    def test_transfer_share_falls_back_when_target_probe_fails(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/course")
        entry_context = {
            "shared_paths": [shared_dir],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock(return_value={**entry_context, "shared_files_info": []})
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.side_effect = RuntimeError("list failed")

        result = self.storage.transfer_share("url", save_dir="/save")

        self.assertTrue(result["skipped"])
        self.storage.client.transfer_shared_paths.assert_not_called()
        self.storage._load_share_files.assert_called_once()

    def test_transfer_share_skips_dir_fast_path_when_regex_is_set(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/course")
        entry_context = {
            "shared_paths": [shared_dir],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock(return_value={**entry_context, "shared_files_info": []})

        result = self.storage.transfer_share("url", save_dir="/save", regex_pattern="old")

        self.assertTrue(result["skipped"])
        self.storage.client.transfer_shared_paths.assert_not_called()
        self.storage._load_share_files.assert_called_once()

    def test_process_single_share_config_handles_successful_result(self):
        self.storage.transfer_share = Mock(
            return_value={"success": True, "message": "成功", "transferred_files": ["f1"]}
        )
        progress_callback = Mock()

        result = self.storage._process_single_share_config(
            1,
            2,
            {"share_url": "https://pan.baidu.com/s/abc", "save_dir": "/save"},
            progress_callback,
        )

        self.assertEqual(1, result["index"])
        self.assertTrue(result["success"])
        self.assertEqual("成功", result["message"])
        progress_callback.assert_any_call("success", "【1/2】成功: 成功")

    def test_process_single_share_config_handles_skipped_result(self):
        self.storage.transfer_share = Mock(
            return_value={"success": True, "skipped": True, "message": "没有新文件需要转存"}
        )
        progress_callback = Mock()

        result = self.storage._process_single_share_config(
            1,
            2,
            {"share_url": "https://pan.baidu.com/s/abc", "save_dir": "/save"},
            progress_callback,
        )

        self.assertTrue(result["skipped"])
        progress_callback.assert_any_call("info", "【1/2】跳过: 没有新文件需要转存")

    def test_process_single_share_config_handles_invalid_config(self):
        progress_callback = Mock()

        result = self.storage._process_single_share_config(1, 2, "bad-config", progress_callback)

        self.assertFalse(result["success"])
        self.assertIn("缺少分享链接", result["error"])
        progress_callback.assert_any_call("error", f"【1/2】失败: {result['error']}")

    def test_transfer_multiple_shares_aggregates_counts_as_partial_when_has_failure(self):
        self.storage._process_single_share_config = Mock(
            side_effect=[
                {"index": 1, "share_url": "u1", "save_dir": "/a", "success": True, "partial": False, "message": "成功"},
                {"index": 2, "share_url": "u2", "save_dir": "/b", "success": True, "partial": False, "skipped": True, "message": "跳过"},
                {"index": 3, "share_url": "u3", "save_dir": "/c", "success": False, "partial": False, "error": "失败"},
            ]
        )
        progress_callback = Mock()

        with patch("storage.time.sleep") as sleep:
            result = self.storage.transfer_multiple_shares(
                [
                    {"share_url": "u1"},
                    {"share_url": "u2"},
                    {"share_url": "u3"},
                ],
                progress_callback,
            )

        sleep.assert_has_calls([call(BATCH_SHARE_DELAY), call(BATCH_SHARE_DELAY)])
        self.assertEqual(2, sleep.call_count)
        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(1, result["success_count"])
        self.assertEqual(1, result["skipped_count"])
        self.assertEqual(1, result["failed_count"])
        self.assertEqual(0, result["partial_count"])
        self.assertEqual(3, len(result["results"]))
        progress_callback.assert_any_call(
            "warning", result["summary"] + "（部分成功按失败退出，退出码 1）"
        )

    def test_transfer_multiple_shares_collects_partial_rename_failed_details(self):
        self.storage._process_single_share_config = Mock(
            side_effect=[
                {
                    "index": 1,
                    "share_url": "u1",
                    "save_dir": "/a",
                    "success": False,
                    "partial": True,
                    "message": "部分成功",
                    "rename_failed_files": [
                        {"source_path": "old/a.txt", "target_path": "new/a.txt", "error": "boom"}
                    ],
                }
            ]
        )

        result = self.storage.transfer_multiple_shares([{"share_url": "u1"}])

        self.assertTrue(result["partial"])
        self.assertEqual(1, result["rename_failed_count"])
        self.assertEqual("old/a.txt", result["rename_failed_files"][0]["source_path"])

    def test_build_transfer_list_skips_rename_candidate_when_source_exists_to_avoid_duplicate_copy(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, file_only=False: path.strip("/")
        progress_callback = Mock()

        result = self.storage._build_transfer_list(
            [{"fs_id": 1, "path": "old/a.txt", "md5": "src-md5"}],
            [Mock(is_dir=False)],
            "/save",
            {"old/a.txt": "other-md5"},
            regex_pattern=r"old",
            regex_replace="new",
            progress_callback=progress_callback,
        )

        self.assertEqual([], result)
        progress_callback.assert_any_call(
            "warning", "源路径已存在，跳过重复转存以避免副本: old/a.txt -> new/a.txt"
        )

    def test_build_transfer_list_skips_rename_candidate_when_target_exists(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, file_only=False: path.strip("/")
        progress_callback = Mock()

        result = self.storage._build_transfer_list(
            [{"fs_id": 1, "path": "old/a.txt", "md5": "src-md5"}],
            [Mock(is_dir=False)],
            "/save",
            {"new/a.txt": "src-md5"},
            regex_pattern=r"old",
            regex_replace="new",
            progress_callback=progress_callback,
        )

        self.assertEqual([], result)
        progress_callback.assert_any_call(
            "info",
            "候选分析完成：共享文件 1 个，候选 1 个，正则过滤 0 个，本地已存在 1 个，"
            "冲突跳过 0 个，需要转存 0 个，其中需重命名 0 个",
        )

    def test_clear_local_files_cache_removes_full_and_targeted_entries(self):
        self.storage.path_service.normalize_path.side_effect = lambda path: path
        self.storage._local_files_cache = {
            "/save": ["full"],
            ("/save", ("a",)): ["targeted"],
            "/other": ["other"],
        }

        self.storage._clear_local_files_cache("/save")

        self.assertEqual({"/other": ["other"]}, self.storage._local_files_cache)

    def test_execute_transfer_plan_sleeps_only_between_groups(self):
        self.storage.path_service.normalize_path.side_effect = lambda path: path
        transfer_list = [
            (1, "/save/a", "a/1.txt", "a/1.txt", False),
            (2, "/save/b", "b/2.txt", "b/2.txt", False),
        ]

        with patch("storage.time.sleep") as sleep:
            success_count, successful_items = self.storage._execute_transfer_plan(
                transfer_list, "url", 1, 2, "token", "/save"
            )

        self.assertEqual(2, success_count)
        self.assertEqual(transfer_list, successful_items)
        sleep.assert_called_once_with(FREQUENCY_LIMIT_DELAY)

    def test_execute_transfer_plan_splits_fs_ids_by_batch_size(self):
        transfer_list = [
            (fs_id, "/save", f"{fs_id}.txt", f"{fs_id}.txt", False)
            for fs_id in range(TRANSFER_BATCH_SIZE * 2 + 1)
        ]

        with patch("storage.time.sleep"):
            success_count, successful_items = self.storage._execute_transfer_plan(
                transfer_list, "url", 1, 2, "token", "/save"
            )

        calls = self.storage.client.transfer_shared_paths.call_args_list
        self.assertEqual(len(transfer_list), success_count)
        self.assertEqual(transfer_list, successful_items)
        self.assertEqual(3, len(calls))
        self.assertEqual(TRANSFER_BATCH_SIZE, len(calls[0].kwargs["fs_ids"]))
        self.assertEqual(TRANSFER_BATCH_SIZE, len(calls[1].kwargs["fs_ids"]))
        self.assertEqual(1, len(calls[2].kwargs["fs_ids"]))

    def test_rename_transferred_files_sleeps_only_between_renames(self):
        self.storage.path_service.ensure_dir_exists.return_value = True
        transfer_items = [
            (1, "/save", "a.txt", "renamed-a.txt", True),
            (2, "/save", "b.txt", "renamed-b.txt", True),
        ]

        with patch("storage.time.sleep") as sleep:
            result = self.storage._rename_transferred_files(transfer_items, "/save")

        self.assertEqual(["renamed-a.txt", "renamed-b.txt"], result["transferred_files"])
        sleep.assert_called_once_with(RENAME_DELAY)

    def test_rename_transferred_files_reports_failures_as_partial(self):
        self.storage.client.rename.side_effect = RuntimeError("rename boom")
        self.storage.path_service.ensure_dir_exists.return_value = True
        progress_callback = Mock()

        result = self.storage._rename_transferred_files(
            [(1, "/save/old", "old/a.txt", "new/a.txt", True)],
            "/save",
            progress_callback,
        )

        self.assertEqual([], result["transferred_files"])
        self.assertEqual(1, result["rename_failed_count"])
        self.assertEqual(0, result["completed_count"])
        self.assertEqual("old/a.txt", result["rename_failed_files"][0]["source_path"])
        self.assertEqual("new/a.txt", result["rename_failed_files"][0]["target_path"])

    def test_build_transfer_result_handles_partial_rename_failure(self):
        result = self.storage._build_transfer_result(
            2,
            2,
            {
                "transferred_files": ["done.txt"],
                "rename_failed_files": [
                    {"source_path": "old/a.txt", "target_path": "new/a.txt", "error": "boom"}
                ],
                "rename_failed_count": 1,
                "completed_count": 1,
            },
            None,
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(1, result["completed_count"])
        self.assertEqual(1, result["rename_failed_count"])
        self.assertIn("重命名失败", result["message"])

    def test_build_transfer_result_treats_transfer_success_with_all_rename_failures_as_partial(self):
        result = self.storage._build_transfer_result(
            2,
            2,
            {
                "transferred_files": [],
                "rename_failed_files": [
                    {"source_path": "old/a.txt", "target_path": "new/a.txt", "error": "boom"},
                    {"source_path": "old/b.txt", "target_path": "new/b.txt", "error": "boom"},
                ],
                "rename_failed_count": 2,
                "completed_count": 0,
            },
            None,
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(0, result["completed_count"])
        self.assertEqual(2, result["rename_failed_count"])
        self.assertEqual(2, result["transfer_success_count"])

    def test_build_transfer_result_handles_complete_failure(self):
        result = self.storage._build_transfer_result(
            0,
            2,
            {
                "transferred_files": [],
                "rename_failed_files": [],
                "rename_failed_count": 0,
                "completed_count": 0,
            },
            None,
        )

        self.assertEqual(
            {
                "success": False,
                "partial": False,
                "error": "转存失败，没有文件成功转存",
                "rename_failed_files": [],
                "rename_failed_count": 0,
                "completed_count": 0,
                "transfer_success_count": 0,
            },
            result,
        )


class BaiduClientAdapterTests(unittest.TestCase):
    def test_parse_cookies_skips_invalid_items(self):
        result = BaiduClientAdapter.parse_cookies(
            "BDUSS=foo; invalid; STOKEN=bar; key = value "
        )

        self.assertEqual({"BDUSS": "foo", "STOKEN": "bar", "key": "value"}, result)

    def test_validate_cookies_requires_bduss_and_stoken(self):
        self.assertTrue(BaiduClientAdapter.validate_cookies({"BDUSS": "1", "STOKEN": "2"}))
        self.assertFalse(BaiduClientAdapter.validate_cookies({"BDUSS": "1"}))

    def test_call_with_retry_can_raise_retry_abort_errors(self):
        adapter = BaiduClientAdapter.__new__(BaiduClientAdapter)
        adapter.max_retries = 1
        adapter.is_github_actions = False
        adapter.base_retry_delay = 0

        def fail_with_code_4():
            raise RuntimeError("error_code: 4, message: share transfer pcs error")

        self.assertIsNone(adapter.call_with_retry(fail_with_code_4))
        with self.assertRaises(RuntimeError):
            adapter.call_with_retry(fail_with_code_4, suppress_retry_abort=False)


if __name__ == "__main__":
    unittest.main()
