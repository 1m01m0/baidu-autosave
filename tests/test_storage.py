import os
import sys
import threading
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from requests.exceptions import JSONDecodeError as RequestsJSONDecodeError

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
    RATE_LIMIT_WAIT_TIME,
    RENAME_DELAY,
    TRANSFER_BATCH_SIZE,
    TransferItem,
    _read_non_negative_float_env,
    _read_positive_int_env,
)
from storage_client import BaiduClientAdapter
from storage_errors import classify_storage_error, is_transfer_count_limit_error, parse_share_error
from storage_paths import StoragePathService
from storage_rules import (
    REGEX_FILTER_UNMATCHED,
    REGEX_FILTER_UNSAFE_REPLACE,
    apply_regex_rules,
    apply_regex_rules_detail,
    should_exclude_folder,
    should_include_folder,
)
from storage_shares import SharedPathService
from utils import format_error_info, mask_sensitive
from wechat_notifier import WeChatNotifier


class BaiduStoragePureMethodTests(unittest.TestCase):
    def setUp(self):
        self.storage = BaiduStorage.__new__(BaiduStorage)
        self.storage.path_service = Mock()
        self.storage.share_service = Mock()

    def test_transfer_item_keeps_sequence_compatibility(self):
        item = TransferItem(1, "/save", "a.txt", "b.txt", True, "md5")

        self.assertEqual((1, "/save", "a.txt", "b.txt", True), item.as_tuple())
        self.assertEqual([1, "/save", "a.txt", "b.txt", True], list(item))
        self.assertEqual("b.txt", item[3])
        self.assertEqual(("a.txt", "b.txt"), item[2:4])
        self.assertEqual(1, item.count("a.txt"))
        self.assertEqual(3, item.index("b.txt"))
        self.assertEqual((1, "/save", "a.txt", "b.txt", True), item)
        self.assertEqual("md5", item.src_md5)

    def test_transfer_item_equality_matches_old_tuple_payload(self):
        item = TransferItem(1, "/save", "a.txt", "b.txt", True, "md5-a")
        same_payload = TransferItem(1, "/save", "a.txt", "b.txt", True, "md5-b")

        self.assertEqual(item, same_payload)
        self.assertEqual(hash(item), hash(same_payload))

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
        self.assertEqual((True, "dir/file.mp4"), apply_regex_rules("dir/file.mp4"))
        self.assertEqual((False, "dir/file.txt"), apply_regex_rules("dir/file.txt", r"\\.mp4$"))
        self.assertEqual(
            (True, "dir/video.mp4"),
            apply_regex_rules("dir/file.mp4", r"file", "video"),
        )
        self.assertEqual((True, "dir/file.mp4"), apply_regex_rules("dir/file.mp4", "["))

    def test_apply_regex_rules_detail_reports_filter_reason(self):
        self.assertEqual(
            (False, "dir/file.txt", REGEX_FILTER_UNMATCHED),
            apply_regex_rules_detail("dir/file.txt", r"\.mp4$"),
        )
        self.assertEqual(
            (False, "dir/file.mp4", REGEX_FILTER_UNSAFE_REPLACE),
            apply_regex_rules_detail("dir/file.mp4", r"dir/file\.mp4$", "../evil.mp4"),
        )
        self.assertEqual(
            (True, "dir/file.mp4", None),
            apply_regex_rules_detail("dir/file.mp4", "["),
        )

    def test_apply_regex_rules_rejects_unsafe_replace_targets(self):
        unsafe_replacements = [
            "../evil.mp4",
            "/evil.mp4",
            "a/../../evil.mp4",
            r"C:\\evil.mp4",
            "a//evil.mp4",
            "\x00evil.mp4",
        ]
        for replacement in unsafe_replacements:
            with self.subTest(replacement=replacement):
                self.assertEqual(
                    (False, "dir/file.mp4"),
                    apply_regex_rules("dir/file.mp4", r"dir/file\.mp4$", replacement),
                )

    def test_should_include_folder_supports_none_string_list_and_invalid_regex(self):
        self.assertTrue(should_include_folder("Movies"))
        self.assertTrue(should_include_folder("Movies-2026", r"Movies"))
        self.assertFalse(should_include_folder("Shows-2026", r"Movies"))
        self.assertTrue(should_include_folder("Anime", [r"Movies", r"Anime"]))
        self.assertTrue(should_include_folder("Anything", "["))

    def test_should_exclude_folder_supports_string_list_and_invalid_regex(self):
        self.assertFalse(should_exclude_folder("Movies"))
        self.assertTrue(should_exclude_folder("node_modules", r"^node_modules$"))
        self.assertFalse(should_exclude_folder("src", r"^node_modules$"))
        self.assertTrue(should_exclude_folder("__pycache__", [r"^node_modules$", r"^__pycache__$"]))
        self.assertFalse(should_exclude_folder("Anything", "["))
        self.assertFalse(should_exclude_folder("Anything", 123))

    def test_classify_storage_error_supports_rate_limit_missing_path_and_exists(self):
        rate_limit = classify_storage_error("error_code: -65")
        self.assertEqual("rate_limit", rate_limit.kind)
        self.assertTrue(rate_limit.retryable)

        missing_path = classify_storage_error("error_code: 31066, message: 文件不存在")
        self.assertEqual("missing_path", missing_path.kind)

        already_exists = classify_storage_error("error_code: 31061, message: 文件已经存在")
        self.assertEqual("already_exists", already_exists.kind)

    def test_classify_storage_error_treats_cookie_markers_case_insensitively(self):
        self.assertEqual("cookie_invalid", classify_storage_error("BDUSS expired").kind)
        self.assertEqual("cookie_invalid", classify_storage_error("STOKEN invalid").kind)

    def test_classify_storage_error_treats_requests_json_decode_as_retryable_network(self):
        error = RequestsJSONDecodeError("Expecting value", "", 0)

        result = classify_storage_error(error)

        self.assertEqual("network", result.kind)
        self.assertTrue(result.retryable)
        self.assertEqual("网盘接口返回非 JSON 响应，请稍后重试", result.message)

    def test_classify_storage_error_treats_baidu_storage_issue_as_retryable_network(self):
        result = classify_storage_error("error_code: 4, message: 存储好像出问题了，请稍候再试")

        self.assertEqual("network", result.kind)
        self.assertTrue(result.retryable)
        self.assertEqual(
            "网盘存储临时异常，请稍后重试：error_code: 4, message: 存储好像出问题了，请稍候再试",
            result.message,
        )

    def test_classify_storage_error_keeps_generic_code_4_as_retry_abort(self):
        result = classify_storage_error("error_code: 4, message: please try again later")

        self.assertEqual("retry_abort", result.kind)
        self.assertFalse(result.retryable)

    def test_classify_storage_error_treats_code_4_network_keywords_as_retryable_network(self):
        result = classify_storage_error("error_code: 4, message: http timeout")

        self.assertEqual("network", result.kind)
        self.assertTrue(result.retryable)

    def test_classify_storage_error_prioritizes_known_codes_over_network_keywords(self):
        result = classify_storage_error("error_code: 31061, message: 文件已经存在 http request")

        self.assertEqual("already_exists", result.kind)
        self.assertFalse(result.retryable)

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

    def test_read_non_negative_int_env_accepts_zero_and_falls_back_for_invalid(self):
        from env_utils import read_non_negative_int_env

        env_name = "TRANSFERSHARE_TEST_NON_NEGATIVE"
        # 合法 0：与 read_positive_int_env 的关键差异
        with patch.dict(os.environ, {env_name: "0"}):
            self.assertEqual(0, read_non_negative_int_env(env_name, 5))
        # 合法正数
        with patch.dict(os.environ, {env_name: "32"}):
            self.assertEqual(32, read_non_negative_int_env(env_name, 5))
        # 负数回退
        with patch.dict(os.environ, {env_name: "-1"}):
            self.assertEqual(5, read_non_negative_int_env(env_name, 5))
        # 非整数回退
        with patch.dict(os.environ, {env_name: "bad"}):
            self.assertEqual(5, read_non_negative_int_env(env_name, 5))
        # 未设置回退
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(5, read_non_negative_int_env(env_name, 5))

    def test_transfer_count_limit_error_detection(self):
        self.assertTrue(is_transfer_count_limit_error("error_code: -33"))
        self.assertTrue(is_transfer_count_limit_error("error_code: 120, message: 转存文件数超限"))
        self.assertTrue(is_transfer_count_limit_error("error_code: 4, message: share transfer pcs error"))
        self.assertTrue(is_transfer_count_limit_error("一次支持操作999个，减点试试吧"))
        self.assertTrue(is_transfer_count_limit_error("error_code: 4, message: too many files"))
        self.assertFalse(is_transfer_count_limit_error("error_code: 4, message: too many requests"))
        self.assertFalse(is_transfer_count_limit_error("error_code: 4, message: 请求被中止"))
        self.assertFalse(is_transfer_count_limit_error("error_code: -32, message: 剩余空间不足"))

    def test_format_error_info_masks_share_urls_and_pwd(self):
        error = ValueError(
            "分享链接: https://pan.baidu.com/s/abc12345?pwd=1a2B&foo=bar, "
            "备用: surl=xyz987，提取码 9Z8y，pwd7X6z"
        )

        result = format_error_info(error, "处理失败")

        self.assertIn("https://pan.baidu.com/s/***?pwd=***&foo=bar", result)
        self.assertIn("surl=***", result)
        self.assertIn("提取码 ***", result)
        self.assertIn("pwd***", result)
        self.assertNotIn("abc12345", result)
        self.assertNotIn("1a2B", result)
        self.assertNotIn("9Z8y", result)
        self.assertNotIn("7X6z", result)
        self.assertNotIn("xyz987", result)

    def test_format_error_info_masks_standalone_surl_in_plain_text(self):
        error = ValueError("普通文本里有备用码 surl=xyz987，可直接打开")

        result = format_error_info(error, "处理失败")

        self.assertIn("surl=***", result)
        self.assertNotIn("xyz987", result)

    def test_format_error_info_masks_webhook_and_tokens(self):
        error = ValueError(
            "webhook https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=webhook-secret "
            "access_token=access-secret refresh_token=refresh-secret "
            "Authorization: Bearer bearer-secret"
        )

        result = format_error_info(error, "处理失败")

        self.assertIn("key=***", result)
        self.assertIn("access_token=***", result)
        self.assertIn("refresh_token=***", result)
        self.assertIn("Authorization: Bearer ***", result)
        self.assertNotIn("webhook-secret", result)
        self.assertNotIn("access-secret", result)
        self.assertNotIn("refresh-secret", result)
        self.assertNotIn("bearer-secret", result)

    def test_mask_sensitive_masks_cookie_json_dict_and_header_formats(self):
        result = mask_sensitive(
            "Cookie: BDUSS=bduss-secret; stoken=stoken-secret; "
            "{'PANWEB': 'panweb-secret', \"H_PS_PSSID\": \"pssid-secret\", "
            "BDORZ: bdorz-secret}"
        )

        for secret in (
            "bduss-secret",
            "stoken-secret",
            "panweb-secret",
            "pssid-secret",
            "bdorz-secret",
        ):
            self.assertNotIn(secret, result)
        self.assertIn("BDUSS=***", result)
        self.assertIn("stoken=***", result)
        self.assertIn("'PANWEB': '***'", result)
        self.assertIn('"H_PS_PSSID": "***"', result)
        self.assertIn("BDORZ: ***", result)


class BaiduClientAdapterRetryTests(unittest.TestCase):
    def _adapter(self):
        adapter = BaiduClientAdapter.__new__(BaiduClientAdapter)
        adapter.client = Mock()
        adapter.max_retries = 3
        adapter.is_github_actions = False
        adapter.base_retry_delay = 0
        return adapter

    def test_list_makedir_and_rename_retry_temporary_json_error(self):
        error = RequestsJSONDecodeError("Expecting value", "", 0)
        cases = [
            ("list", ("/save",), [error, ["ok"]], ["ok"]),
            ("makedir", ("/save",), [error, None], None),
            ("rename", ("/save/a", "/save/b"), [error, None], None),
        ]

        with patch("storage_client.time.sleep"):
            for method_name, args, side_effect, expected in cases:
                with self.subTest(method_name=method_name):
                    adapter = self._adapter()
                    getattr(adapter.client, method_name).side_effect = side_effect

                    result = getattr(adapter, method_name)(*args)

                    self.assertEqual(expected, result)
                    self.assertEqual(2, getattr(adapter.client, method_name).call_count)

    def test_list_retries_rate_limit_errors(self):
        adapter = self._adapter()
        adapter.client.list.side_effect = [RuntimeError("error_code: -65"), ["ok"]]

        with patch("storage_client.time.sleep"):
            result = adapter.list("/save")

        self.assertEqual(["ok"], result)
        self.assertEqual(2, adapter.client.list.call_count)

    def test_transfer_shared_paths_does_not_retry_in_adapter(self):
        adapter = self._adapter()
        adapter.client.transfer_shared_paths.side_effect = RequestsJSONDecodeError(
            "Expecting value", "", 0
        )

        with self.assertRaises(RequestsJSONDecodeError):
            adapter.transfer_shared_paths(remotedir="/save", fs_ids=[1])

        adapter.client.transfer_shared_paths.assert_called_once_with(
            remotedir="/save", fs_ids=[1]
        )

    def test_list_does_not_swallow_retry_abort_errors(self):
        adapter = self._adapter()
        adapter.client.list.side_effect = RuntimeError(
            "error_code: 4, message: please try again later"
        )

        with self.assertRaises(RuntimeError):
            adapter.list("/save")


class BaiduStorageErrorNotificationTests(unittest.TestCase):
    def setUp(self):
        self.storage = BaiduStorage.__new__(BaiduStorage)
        self.storage.wechat_notifier = Mock()

    def test_notify_error_preserves_none_config_and_default_collect(self):
        error = RuntimeError("boom")
        self.storage.config = {"name": "test-config"}

        with patch("storage.handle_error_and_notify") as notify:
            self.storage._notify_error(error, "主上下文")

        notify.assert_called_once_with(
            error,
            "主上下文",
            self.storage.wechat_notifier,
            None,
            collect=True,
        )

    def test_notify_error_appends_extra_info_and_uses_none_config_when_missing(self):
        error = ValueError("bad")

        with patch("storage.handle_error_and_notify") as notify:
            self.storage._notify_error(
                error,
                "主上下文",
                extra_info="分享链接: masked-url",
            )

        notify.assert_called_once_with(
            error,
            "主上下文\n分享链接: masked-url",
            self.storage.wechat_notifier,
            None,
            collect=True,
        )

    def test_notify_error_forwards_collect_false(self):
        error = RuntimeError("boom")

        with patch("storage.handle_error_and_notify") as notify:
            self.storage._notify_error(error, "立即通知上下文", collect=False)

        notify.assert_called_once_with(
            error,
            "立即通知上下文",
            self.storage.wechat_notifier,
            None,
            collect=False,
        )


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

    def test_list_shared_files_keeps_progress_callback_positional_compatibility(self):
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

        files = self.service.list_shared_files([root_dir], None, progress_callback)

        self.assertEqual(1, len(files))
        progress_callback.assert_any_call("info", "开始获取共享文件列表，共 1 个入口")

    def test_list_shared_files_skips_excluded_node_modules(self):
        root_dir = SimpleNamespace(
            path="/single-share",
            is_dir=True,
            uk=1,
            share_id=2,
            bdstoken="token",
        )
        node_modules = SimpleNamespace(
            path="/single-share/node_modules",
            is_dir=True,
            fs_id=10,
        )
        app_file = SimpleNamespace(
            path="/single-share/app.py",
            is_dir=False,
            fs_id=11,
            size=456,
            md5="def",
        )
        self.service.client.list_shared_paths.return_value = [node_modules, app_file]

        files = self.service.list_shared_files(
            [root_dir], exclude_folder_filter=r"^node_modules$"
        )

        self.assertEqual(1, len(files))
        self.assertEqual("app.py", files[0]["path"])
        self.service.client.list_shared_paths.assert_called_once_with(
            "/single-share", 1, 2, "token", page=1, size=100
        )

    def test_list_shared_files_handles_deep_directory_without_recursion(self):
        root_dir = SimpleNamespace(
            path="/single-share",
            is_dir=True,
            uk=1,
            share_id=2,
            bdstoken="token",
        )
        depth = 150
        listings = {}
        path = "/single-share"
        relative_parts = []
        for index in range(depth):
            child_path = f"{path}/d{index}"
            listings[path] = [SimpleNamespace(path=child_path, is_dir=True, fs_id=index + 10)]
            path = child_path
            relative_parts.append(f"d{index}")
        listings[path] = [
            SimpleNamespace(path=f"{path}/file.txt", is_dir=False, fs_id=99, size=123, md5="md5-file")
        ]
        self.service.client.list_shared_paths.side_effect = lambda scan_path, *args, **kwargs: listings[scan_path]
        old_limit = sys.getrecursionlimit()

        try:
            sys.setrecursionlimit(100)
            files = self.service.list_shared_files([root_dir])
        finally:
            sys.setrecursionlimit(old_limit)

        self.assertEqual(1, len(files))
        self.assertEqual("/".join(relative_parts + ["file.txt"]), files[0]["path"])
        self.assertEqual("md5-file", files[0]["md5"])

    def test_list_shared_files_excludes_deep_filtered_directory_without_recursion(self):
        root_dir = SimpleNamespace(
            path="/single-share",
            is_dir=True,
            uk=1,
            share_id=2,
            bdstoken="token",
        )
        src_dir = SimpleNamespace(path="/single-share/src", is_dir=True, fs_id=10)
        node_modules = SimpleNamespace(path="/single-share/src/node_modules", is_dir=True, fs_id=11)
        app_file = SimpleNamespace(path="/single-share/src/app.py", is_dir=False, fs_id=12, size=1, md5="app")
        listings = {
            "/single-share": [src_dir],
            "/single-share/src": [node_modules, app_file],
        }
        self.service.client.list_shared_paths.side_effect = lambda scan_path, *args, **kwargs: listings.get(scan_path, [])

        files = self.service.list_shared_files(
            [root_dir], exclude_folder_filter=r"^node_modules$"
        )

        self.assertEqual(1, len(files))
        self.assertEqual("src/app.py", files[0]["path"])
        self.assertNotIn(
            call("/single-share/src/node_modules", 1, 2, "token", page=1, size=100),
            self.service.client.list_shared_paths.call_args_list,
        )

    def test_list_shared_dir_children_returns_direct_child_metadata(self):
        child_dir = SimpleNamespace(
            path="/single-share/src",
            is_dir=True,
            is_file=False,
            fs_id=10,
        )
        child_file = SimpleNamespace(
            path="/single-share/app.py",
            is_dir=False,
            is_file=True,
            fs_id=11,
            md5="md5-app",
        )
        self.service.client.list_shared_paths.return_value = [child_dir, child_file]

        children = self.service.list_shared_dir_children(
            "/single-share", 1, 2, "token"
        )

        self.assertEqual(
            [
                {
                    "raw": child_dir,
                    "fs_id": 10,
                    "path": "/single-share/src",
                    "name": "src",
                    "is_dir": True,
                    "is_file": False,
                    "md5": None,
                },
                {
                    "raw": child_file,
                    "fs_id": 11,
                    "path": "/single-share/app.py",
                    "name": "app.py",
                    "is_dir": False,
                    "is_file": True,
                    "md5": "md5-app",
                },
            ],
            children,
        )

    def test_normalize_shared_child_preserves_dict_md5(self):
        child = SharedPathService._normalize_shared_child(
            {
                "path": "/single-share/app.py",
                "isdir": 0,
                "fs_id": 11,
                "md5": "md5-app",
            }
        )

        self.assertEqual("md5-app", child["md5"])

    def test_iter_shared_dir_children_yields_multiple_pages(self):
        first_child = SimpleNamespace(
            path="/single-share/1.txt",
            is_dir=False,
            is_file=True,
            fs_id=1,
        )
        second_child = SimpleNamespace(
            path="/single-share/2.txt",
            is_dir=False,
            is_file=True,
            fs_id=2,
        )
        self.service.client.list_shared_paths.side_effect = [
            {"list": [first_child]},
            {"list": [second_child]},
            {"list": []},
        ]

        with patch.object(SharedPathService, "SHARED_DIR_PAGE_SIZE", 1):
            children = list(
                self.service.iter_shared_dir_children("/single-share", 1, 2, "token")
            )

        self.assertEqual(["1.txt", "2.txt"], [child["name"] for child in children])
        self.service.client.list_shared_paths.assert_has_calls(
            [
                call("/single-share", 1, 2, "token", page=1, size=1),
                call("/single-share", 1, 2, "token", page=2, size=1),
                call("/single-share", 1, 2, "token", page=3, size=1),
            ]
        )


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

    def test_list_local_files_handles_deep_directory_without_recursion(self):
        client = Mock()
        depth = 150
        listings = {}
        path = "/save"
        relative_parts = []
        for index in range(depth):
            child_path = f"{path}/d{index}"
            listings[path] = [SimpleNamespace(is_file=False, is_dir=True, path=child_path)]
            path = child_path
            relative_parts.append(f"d{index}")
        listings[path] = [
            SimpleNamespace(is_file=True, is_dir=False, path=f"{path}/file.txt", md5="md5-file")
        ]
        client.list.side_effect = lambda scan_path: listings[scan_path]
        service = StoragePathService(client)
        old_limit = sys.getrecursionlimit()

        try:
            sys.setrecursionlimit(100)
            result = service.list_local_files("/save")
        finally:
            sys.setrecursionlimit(old_limit)

        self.assertEqual(
            [
                {
                    "relative_path": "/".join(relative_parts + ["file.txt"]),
                    "file_name": "file.txt",
                    "md5": "md5-file",
                }
            ],
            result,
        )

    def test_list_local_files_in_dirs_handles_deep_merged_scan_without_recursion(self):
        client = Mock()
        depth = 150
        listings = {
            "/save/A": [
                SimpleNamespace(is_file=False, is_dir=True, path="/save/A/1"),
                SimpleNamespace(is_file=False, is_dir=True, path="/save/A/2"),
                SimpleNamespace(is_file=False, is_dir=True, path="/save/A/other"),
            ],
            "/save/A/2": [
                SimpleNamespace(is_file=True, is_dir=False, path="/save/A/2/b.txt", md5="md5-b")
            ],
            "/save/A/other": [
                SimpleNamespace(is_file=True, is_dir=False, path="/save/A/other/ignored.txt", md5="ignored")
            ],
        }
        path = "/save/A/1"
        relative_parts = ["A", "1"]
        for index in range(depth):
            child_path = f"{path}/d{index}"
            listings[path] = [SimpleNamespace(is_file=False, is_dir=True, path=child_path)]
            path = child_path
            relative_parts.append(f"d{index}")
        listings[path] = [
            SimpleNamespace(is_file=True, is_dir=False, path=f"{path}/a.txt", md5="md5-a")
        ]
        client.list.side_effect = lambda scan_path: listings[scan_path]
        service = StoragePathService(client)
        old_limit = sys.getrecursionlimit()

        try:
            sys.setrecursionlimit(100)
            result = service.list_local_files_in_dirs(
                "/save", {"/".join(relative_parts), "A/2"}, merge_dirs=True
            )
        finally:
            sys.setrecursionlimit(old_limit)

        self.assertEqual(
            [
                {
                    "relative_path": "/".join(relative_parts + ["a.txt"]),
                    "file_name": "a.txt",
                    "md5": "md5-a",
                },
                {"relative_path": "A/2/b.txt", "file_name": "b.txt", "md5": "md5-b"},
            ],
            result,
        )
        self.assertNotIn(call("/save/A/other"), client.list.call_args_list)

    def test_list_local_files_in_dirs_concurrent_scan_preserves_order(self):
        """并发扫描下，结果列表仍按 sorted scan_plan 顺序合并。

        即便 worker 之间 client.list 的真实调用顺序受调度影响，
        files 的相对顺序应与串行实现一致。
        """
        import threading

        client = Mock()
        listings = {
            "/save/A": [
                SimpleNamespace(is_file=True, is_dir=False, path="/save/A/a.txt", md5="md5-a"),
            ],
            "/save/B/C": [
                SimpleNamespace(is_file=True, is_dir=False, path="/save/B/C/c.txt", md5="md5-c"),
            ],
        }

        thread_names = set()
        thread_names_lock = threading.Lock()

        def list_side_effect(path):
            with thread_names_lock:
                thread_names.add(threading.current_thread().name)
            return list(listings[path])

        client.list.side_effect = list_side_effect
        service = StoragePathService(client)

        with patch("storage_paths.LOCAL_SCAN_CONCURRENCY", 4):
            result = service.list_local_files_in_dirs("/save", {"A", "B/C"})

        # 结果顺序：先 A，后 B/C（按 sorted plan）
        self.assertEqual(
            [
                {"relative_path": "A/a.txt", "file_name": "a.txt", "md5": "md5-a"},
                {"relative_path": "B/C/c.txt", "file_name": "c.txt", "md5": "md5-c"},
            ],
            result,
        )
        # 验证确实使用了 worker 线程（至少一个非主线程参与了 client.list）
        self.assertTrue(
            any(name.startswith("transfershare-local-scan") for name in thread_names),
            f"未观察到本地扫描 worker 线程参与: {thread_names}",
        )

    def test_list_local_files_in_dirs_concurrent_propagates_worker_exception(self):
        """并发模式下 worker 抛出的异常应被外层捕获并降级为空列表。"""
        client = Mock()
        client.list.side_effect = RuntimeError("boom")
        service = StoragePathService(client)

        with patch("storage_paths.LOCAL_SCAN_CONCURRENCY", 2), patch(
            "storage_paths.handle_error_and_notify"
        ):
            result = service.list_local_files_in_dirs("/save", {"A", "B"})

        self.assertEqual([], result)


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

    def test_send_message_masks_sensitive_payload_before_posting(self):
        notifier = WeChatNotifier(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=webhook-secret"
        )
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"errcode": 0}
        message = (
            "cookie BDUSS=bduss-secret; STOKEN=stoken-secret "
            "链接 https://pan.baidu.com/s/abc12345?pwd=1a2B "
            "token=access-secret key=webhook-secret"
        )

        with patch("wechat_notifier.requests.post", return_value=response) as post, patch(
            "builtins.print"
        ):
            self.assertTrue(notifier.send_message(message, "markdown"))

        payload = post.call_args.kwargs["json"]
        content = payload["markdown"]["content"]
        self.assertNotIn("bduss-secret", content)
        self.assertNotIn("stoken-secret", content)
        self.assertNotIn("abc12345", content)
        self.assertNotIn("1a2B", content)
        self.assertNotIn("access-secret", content)
        self.assertNotIn("webhook-secret", content)


class BaiduStorageFlowTests(unittest.TestCase):
    def setUp(self):
        self.storage = BaiduStorage.__new__(BaiduStorage)
        self.storage.client = Mock()
        self.storage.wechat_notifier = None
        self.storage._local_files_cache = {}
        self.storage.path_service = Mock()
        self.storage.path_service.normalize_path.side_effect = lambda path, file_only=False: (
            str(path).replace("\\", "/").strip("/").split("/")[-1]
            if file_only
            else str(path).replace("\\", "/").strip("/")
        )
        self.storage.share_service = Mock()
        self.storage.share_service.iter_shared_files.return_value = []

    def test_rename_transferred_files_rejects_unsafe_target_path(self):
        item = TransferItem(1, "/save", "clean.txt", "../evil.txt", True, "md5")

        with patch("storage_rename.handle_error_and_notify") as notify:
            result = self.storage._rename_transferred_files([item], "/save")

        self.assertEqual([], result["transferred_files"])
        self.assertEqual(1, result["rename_failed_count"])
        self.assertEqual(0, result["completed_count"])
        self.assertEqual("../evil.txt", result["rename_failed_files"][0]["target_path"])
        self.storage.path_service.ensure_dir_exists.assert_not_called()
        self.storage.client.rename.assert_not_called()
        notify.assert_called_once()

    def test_transfer_share_returns_skipped_when_no_transfer_candidates(self):
        self.storage._normalize_save_dir = Mock(return_value="/save")
        entry_context = {
            "shared_paths": [Mock(is_dir=False)],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock()
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
        self.storage._load_share_files = Mock()
        self.storage.share_service.iter_shared_files.return_value = [
            {"fs_id": 1, "path": "a.txt"}
        ]
        self.storage._scan_local_files_dict = Mock(return_value={})
        self.storage._ensure_transfer_dirs = Mock(
            return_value={"success": False, "error": "创建目录失败: /save"}
        )

        result = self.storage.transfer_share("https://pan.baidu.com/s/abc")

        self.assertFalse(result["success"])
        self.assertEqual("创建目录失败: /save", result["error"])
        self.assertEqual(1, result["transfer_failed_count"])
        self.assertEqual("a.txt", result["transfer_failed_files"][0]["clean_path"])

    def test_scan_local_files_dict_uses_merged_candidate_dir_scan(self):
        self.storage.path_service.list_local_files_in_dirs.return_value = []

        with patch("storage.get_logger", return_value=Mock()):
            result = self.storage._scan_local_files_dict("/save", relative_dirs={"A/1", "A/2"})

        self.assertEqual({}, result)
        self.storage.path_service.list_local_files_in_dirs.assert_called_once_with(
            "/save", {"A/1", "A/2"}, use_cache=True, merge_dirs=True
        )

    def test_scan_local_files_dict_logs_duplicate_full_paths(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, file_only=False: (
            str(path).replace("\\", "/").strip("/").split("/")[-1]
            if file_only
            else "/" + str(path).replace("\\", "/").strip("/")
        )
        self.storage.path_service.list_local_files.return_value = [
            {"relative_path": "A/a.txt", "file_name": "a.txt", "md5": "md5-a"},
            {"relative_path": "B/a.txt", "file_name": "a.txt", "md5": "md5-b"},
        ]
        logger = Mock()

        with patch("storage.get_logger", return_value=logger):
            result = self.storage._scan_local_files_dict("/save")

        self.assertEqual({"/A/a.txt": "md5-a", "/B/a.txt": "md5-b"}, result)
        logger.info.assert_has_calls(
            [
                call("检测到 1 个重复文件名："),
                call("  - a.txt 出现 2 次"),
                call("    /save/A/a.txt"),
                call("    /save/B/a.txt"),
            ]
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
        self.storage._load_share_files = Mock()
        self.storage.share_service.iter_shared_files.return_value = [
            {"fs_id": 1, "path": "a.txt"}
        ]
        self.storage._scan_local_files_dict = Mock(return_value={})
        transfer_list = [(1, "/save", "a.txt", "a.txt", False)]
        self.storage._ensure_transfer_dirs = Mock(return_value=None)
        self.storage._execute_transfer_plan = Mock(return_value=(1, transfer_list, []))
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
            [],
        )

    def test_transfer_share_streams_transfer_before_scan_finishes(self):
        self.storage._normalize_save_dir = Mock(return_value="/save")
        entry_context = {
            "shared_paths": [Mock(is_dir=False)],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        transfer_started = threading.Event()
        scan_continued_after_transfer = []

        def file_info(fs_id):
            return {"fs_id": fs_id, "path": f"{fs_id}.txt"}

        def shared_files():
            yield file_info(1)
            yield file_info(2)
            scan_continued_after_transfer.append(transfer_started.wait(1))
            yield file_info(3)

        def transfer_side_effect(**kwargs):
            transfer_started.set()

        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock()
        self.storage.share_service.iter_shared_files.side_effect = lambda *args, **kwargs: shared_files()
        self.storage._scan_local_files_dict = Mock(return_value={})
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.transfer_shared_paths.side_effect = transfer_side_effect

        with patch("storage_streaming.TRANSFER_BATCH_SIZE", 2):
            result = self.storage.transfer_share("https://pan.baidu.com/s/abc")

        self.assertTrue(result["success"])
        self.assertEqual([True], scan_continued_after_transfer)
        self.assertEqual(2, self.storage.client.transfer_shared_paths.call_count)
        self.storage._load_share_files.assert_not_called()

    def test_transfer_share_streaming_batches_transfer_items_across_scan_batches(self):
        self.storage._normalize_save_dir = Mock(return_value="/save")
        entry_context = {
            "shared_paths": [Mock(is_dir=False)],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage.share_service.iter_shared_files.return_value = [
            {"fs_id": 1, "path": "1.mp4"},
            {"fs_id": 2, "path": "2.txt"},
            {"fs_id": 3, "path": "3.mp4"},
            {"fs_id": 4, "path": "4.txt"},
        ]
        self.storage._scan_local_files_dict = Mock(return_value={})
        self.storage.path_service.ensure_dir_exists.return_value = True

        with patch("storage_streaming.TRANSFER_BATCH_SIZE", 2):
            result = self.storage.transfer_share(
                "https://pan.baidu.com/s/abc",
                regex_pattern=r"\.mp4$",
            )

        self.assertTrue(result["success"])
        self.storage.client.transfer_shared_paths.assert_called_once_with(
            remotedir="/save",
            fs_ids=[1, 3],
            uk=1,
            share_id=2,
            bdstoken="token",
            shared_url="https://pan.baidu.com/s/abc",
        )

    def test_transfer_share_streaming_skips_duplicate_path_across_scan_batches(self):
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage.path_service.normalize_path.side_effect = lambda path, file_only=False: str(path).strip("/")
        entry_context = {
            "shared_paths": [Mock(is_dir=False)],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage.share_service.iter_shared_files.return_value = [
            {"fs_id": 1, "path": "dup.txt", "md5": "same-md5"},
            {"fs_id": 2, "path": "dup.txt", "md5": "same-md5"},
        ]
        self.storage._scan_local_files_dict = Mock(return_value={})
        self.storage.path_service.ensure_dir_exists.return_value = True

        with patch("storage_streaming.TRANSFER_BATCH_SIZE", 1):
            result = self.storage.transfer_share("https://pan.baidu.com/s/abc")

        self.assertTrue(result["success"])
        self.assertEqual(1, result["completed_count"])
        self.storage.client.transfer_shared_paths.assert_called_once_with(
            remotedir="/save",
            fs_ids=[1],
            uk=1,
            share_id=2,
            bdstoken="token",
            shared_url="https://pan.baidu.com/s/abc",
        )

    def test_transfer_share_streaming_preserves_regex_rename(self):
        self.storage._normalize_save_dir = Mock(return_value="/save")
        entry_context = {
            "shared_paths": [Mock(is_dir=False)],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage.share_service.iter_shared_files.return_value = [
            {"fs_id": 1, "path": "old.mp4"}
        ]
        self.storage._scan_local_files_dict = Mock(return_value={})
        self.storage.path_service.ensure_dir_exists.return_value = True

        result = self.storage.transfer_share(
            "https://pan.baidu.com/s/abc",
            regex_pattern="old",
            regex_replace="new",
        )

        self.assertTrue(result["success"])
        self.assertEqual(["new.mp4"], result["transferred_files"])
        self.storage.client.transfer_shared_paths.assert_called_once_with(
            remotedir="/save",
            fs_ids=[1],
            uk=1,
            share_id=2,
            bdstoken="token",
            shared_url="https://pan.baidu.com/s/abc",
        )
        self.storage.client.rename.assert_called_once_with(
            "/save/old.mp4", "/save/new.mp4"
        )

    def test_transfer_share_streaming_returns_partial_when_scan_fails_after_transfer(self):
        self.storage._normalize_save_dir = Mock(return_value="/save")
        entry_context = {
            "shared_paths": [Mock(is_dir=False)],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }

        def shared_files():
            yield {"fs_id": 1, "path": "a.txt"}
            raise RuntimeError("scan failed")

        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage.share_service.iter_shared_files.side_effect = lambda *args, **kwargs: shared_files()
        self.storage._scan_local_files_dict = Mock(return_value={})
        self.storage.path_service.ensure_dir_exists.return_value = True

        with patch("storage_streaming.TRANSFER_BATCH_SIZE", 1):
            result = self.storage.transfer_share("https://pan.baidu.com/s/abc")

        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertIn("scan failed", result["error"])
        self.assertEqual(1, result["completed_count"])
        self.storage.client.transfer_shared_paths.assert_called_once()

    def test_transfer_share_streaming_warns_when_producer_thread_stays_alive(self):
        context = {
            "shared_paths": [Mock(is_dir=False)],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        fake_logger = Mock()
        joins = []

        class FakeThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

            def join(self, timeout=None):
                joins.append(timeout)

            def is_alive(self):
                return True

        self.storage.share_service.iter_shared_files.return_value = []

        with patch("storage_streaming.threading.Thread", FakeThread), patch(
            "storage_streaming.get_logger", return_value=fake_logger
        ), patch("storage_streaming.STREAM_PRODUCER_JOIN_TIMEOUT", 0.01):
            result = self.storage._transfer_share_streaming(
                context,
                "https://pan.baidu.com/s/abc",
                "/save",
            )

        self.assertTrue(result["skipped"])
        self.assertEqual([0.01], joins)
        fake_logger.warning.assert_called_once_with(
            "共享文件扫描线程未及时退出，继续处理已完成结果"
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

    def test_transfer_share_divides_tree_when_root_dir_hits_count_limit(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/course")
        child_file = {
            "raw": SimpleNamespace(path="/share/course/a.txt", is_dir=False, is_file=True, fs_id=11),
            "fs_id": 11,
            "path": "/share/course/a.txt",
            "name": "a.txt",
            "is_dir": False,
            "is_file": True,
        }
        entry_context = {
            "shared_paths": [shared_dir],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock()
        self.storage.share_service.iter_shared_dir_children.return_value = [child_file]
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.return_value = []
        self.storage.client.transfer_shared_paths.side_effect = [
            RuntimeError("error_code: -33, message: 一次支持操作999个"),
            None,
        ]

        result = self.storage.transfer_share("url", save_dir="/save")

        self.assertTrue(result["success"])
        self.assertTrue(result["divide_path"])
        self.assertEqual(1, result["completed_count"])
        self.storage._load_share_files.assert_not_called()
        self.storage.share_service.iter_shared_dir_children.assert_called_once_with(
            shared_dir, 1, 2, "token"
        )
        self.assertEqual(2, self.storage.client.transfer_shared_paths.call_count)
        self.storage.client.transfer_shared_paths.assert_has_calls(
            [
                call(
                    remotedir="/save",
                    fs_ids=[10],
                    uk=1,
                    share_id=2,
                    bdstoken="token",
                    shared_url="url",
                ),
                call(
                    remotedir="/save/course",
                    fs_ids=[11],
                    uk=1,
                    share_id=2,
                    bdstoken="token",
                    shared_url="url",
                ),
            ]
        )

    def test_transfer_dir_tree_divide_collect_keeps_child_md5_from_metadata(self):
        child_file = {
            "raw": {
                "path": "/share/course/a.txt",
                "isdir": 0,
                "fs_id": 11,
                "md5": "raw-md5",
            },
            "fs_id": 11,
            "path": "/share/course/a.txt",
            "name": "a.txt",
            "is_dir": False,
            "is_file": True,
            "md5": "md5-a",
        }
        flushed_items = []

        def flush_items(file_transfer_list, *args):
            flushed_items.extend(file_transfer_list)
            file_transfer_list.clear()

        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.share_service.iter_shared_dir_children.return_value = [child_file]
        self.storage._flush_dir_tree_file_batch = Mock(side_effect=flush_items)

        self.storage._transfer_dir_tree_divide_collect(
            SimpleNamespace(path="/share/course"),
            "/save/course",
            {"uk": 1, "share_id": 2, "bdstoken": "token"},
            "url",
            None,
            {"failed_count": 0},
        )

        self.assertEqual("md5-a", getattr(flushed_items[0], "src_md5"))

    def test_transfer_share_uses_subdir_fast_path_during_tree_divide(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/course")
        child_dir = {
            "raw": SimpleNamespace(path="/share/course/src", is_dir=True, is_file=False, fs_id=20),
            "fs_id": 20,
            "path": "/share/course/src",
            "name": "src",
            "is_dir": True,
            "is_file": False,
        }
        entry_context = {
            "shared_paths": [shared_dir],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock()
        self.storage.share_service.iter_shared_dir_children.return_value = [child_dir]
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.return_value = []
        self.storage.client.transfer_shared_paths.side_effect = [
            RuntimeError("error_code: -33, message: 一次支持操作999个"),
            None,
        ]

        result = self.storage.transfer_share("url", save_dir="/save")

        self.assertTrue(result["success"])
        self.assertEqual(1, self.storage.share_service.iter_shared_dir_children.call_count)
        self.storage.client.transfer_shared_paths.assert_has_calls(
            [
                call(
                    remotedir="/save",
                    fs_ids=[10],
                    uk=1,
                    share_id=2,
                    bdstoken="token",
                    shared_url="url",
                ),
                call(
                    remotedir="/save/course",
                    fs_ids=[20],
                    uk=1,
                    share_id=2,
                    bdstoken="token",
                    shared_url="url",
                ),
            ]
        )

    def test_transfer_share_recurses_when_subdir_fast_path_hits_count_limit(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/course")
        big_dir = SimpleNamespace(path="/share/course/big", is_dir=True, is_file=False, fs_id=20)
        big_child = SimpleNamespace(path="/share/course/big/a.txt", is_dir=False, is_file=True, fs_id=21)
        child_dir = {
            "raw": big_dir,
            "fs_id": 20,
            "path": "/share/course/big",
            "name": "big",
            "is_dir": True,
            "is_file": False,
        }
        child_file = {
            "raw": big_child,
            "fs_id": 21,
            "path": "/share/course/big/a.txt",
            "name": "a.txt",
            "is_dir": False,
            "is_file": True,
        }
        entry_context = {
            "shared_paths": [shared_dir],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock()
        self.storage.share_service.iter_shared_dir_children.side_effect = [
            [child_dir],
            [child_file],
        ]
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.return_value = []
        self.storage.client.transfer_shared_paths.side_effect = [
            RuntimeError("error_code: -33, message: 一次支持操作999个"),
            RuntimeError("error_code: -33, message: 一次支持操作999个"),
            None,
        ]

        result = self.storage.transfer_share("url", save_dir="/save")

        self.assertTrue(result["success"])
        self.assertEqual(2, self.storage.share_service.iter_shared_dir_children.call_count)
        self.storage.share_service.iter_shared_dir_children.assert_has_calls(
            [call(shared_dir, 1, 2, "token"), call(big_dir, 1, 2, "token")]
        )
        self.storage.client.transfer_shared_paths.assert_has_calls(
            [
                call(
                    remotedir="/save",
                    fs_ids=[10],
                    uk=1,
                    share_id=2,
                    bdstoken="token",
                    shared_url="url",
                ),
                call(
                    remotedir="/save/course",
                    fs_ids=[20],
                    uk=1,
                    share_id=2,
                    bdstoken="token",
                    shared_url="url",
                ),
                call(
                    remotedir="/save/course/big",
                    fs_ids=[21],
                    uk=1,
                    share_id=2,
                    bdstoken="token",
                    shared_url="url",
                ),
            ]
        )

    def test_transfer_share_uses_tree_divide_when_exclude_filter_is_set(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/course")
        excluded_dir = {
            "raw": SimpleNamespace(path="/share/course/node_modules", is_dir=True, is_file=False, fs_id=20),
            "fs_id": 20,
            "path": "/share/course/node_modules",
            "name": "node_modules",
            "is_dir": True,
            "is_file": False,
        }
        app_file = {
            "raw": SimpleNamespace(path="/share/course/app.py", is_dir=False, is_file=True, fs_id=21),
            "fs_id": 21,
            "path": "/share/course/app.py",
            "name": "app.py",
            "is_dir": False,
            "is_file": True,
        }
        entry_context = {
            "shared_paths": [shared_dir],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock()
        self.storage.share_service.iter_shared_dir_children.return_value = [excluded_dir, app_file]
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.return_value = []

        result = self.storage.transfer_share(
            "url", save_dir="/save", exclude_folder_filter=r"^node_modules$"
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["divide_path"])
        self.assertEqual(1, result["skipped_dir_count"])
        self.storage._load_share_files.assert_not_called()
        self.storage.client.transfer_shared_paths.assert_called_once_with(
            remotedir="/save/course",
            fs_ids=[21],
            uk=1,
            share_id=2,
            bdstoken="token",
            shared_url="url",
        )

    def test_transfer_share_skips_root_directory_when_exclude_filter_matches(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/node_modules")
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

        result = self.storage.transfer_share(
            "url", save_dir="/save", exclude_folder_filter=r"^node_modules$"
        )

        self.assertTrue(result["skipped"])
        self.assertEqual(1, result["skipped_dir_count"])
        self.storage.share_service.iter_shared_dir_children.assert_not_called()
        self.storage.client.transfer_shared_paths.assert_not_called()

    def test_transfer_share_recurses_when_exclude_filter_may_match_nested_dir(self):
        shared_dir = SimpleNamespace(is_dir=True, fs_id=10, path="/share/course")
        src_dir = SimpleNamespace(path="/share/course/src", is_dir=True, is_file=False, fs_id=20)
        node_modules = SimpleNamespace(
            path="/share/course/src/node_modules", is_dir=True, is_file=False, fs_id=21
        )
        app_file = SimpleNamespace(
            path="/share/course/src/app.py", is_dir=False, is_file=True, fs_id=22
        )
        src_child = {
            "raw": src_dir,
            "fs_id": 20,
            "path": "/share/course/src",
            "name": "src",
            "is_dir": True,
            "is_file": False,
        }
        excluded_child = {
            "raw": node_modules,
            "fs_id": 21,
            "path": "/share/course/src/node_modules",
            "name": "node_modules",
            "is_dir": True,
            "is_file": False,
        }
        app_child = {
            "raw": app_file,
            "fs_id": 22,
            "path": "/share/course/src/app.py",
            "name": "app.py",
            "is_dir": False,
            "is_file": True,
        }
        entry_context = {
            "shared_paths": [shared_dir],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        self.storage._normalize_save_dir = Mock(return_value="/save")
        self.storage._load_share_entries = Mock(return_value=entry_context)
        self.storage._load_share_files = Mock()
        self.storage.share_service.iter_shared_dir_children.side_effect = [
            [src_child],
            [excluded_child, app_child],
        ]
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.return_value = []

        result = self.storage.transfer_share(
            "url", save_dir="/save", exclude_folder_filter=r"^node_modules$"
        )

        self.assertTrue(result["success"])
        self.assertEqual(1, result["skipped_dir_count"])
        self.storage.share_service.iter_shared_dir_children.assert_has_calls(
            [call(shared_dir, 1, 2, "token"), call(src_dir, 1, 2, "token")]
        )
        self.storage.client.transfer_shared_paths.assert_called_once_with(
            remotedir="/save/course/src",
            fs_ids=[22],
            uk=1,
            share_id=2,
            bdstoken="token",
            shared_url="url",
        )

    def test_transfer_dir_tree_divide_handles_deep_directory_without_recursion(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        depth = 150
        children_by_path = {}
        path = "/share/course"
        for index in range(depth):
            child_path = f"{path}/d{index}"
            child_dir = SimpleNamespace(path=child_path, is_dir=True, is_file=False, fs_id=index + 20)
            children_by_path[path] = [
                {
                    "raw": child_dir,
                    "fs_id": index + 20,
                    "path": child_path,
                    "name": f"d{index}",
                    "is_dir": True,
                    "is_file": False,
                }
            ]
            path = child_path
        children_by_path[path] = [
            {
                "raw": SimpleNamespace(path=f"{path}/file.txt", is_dir=False, is_file=True, fs_id=999),
                "fs_id": 999,
                "path": f"{path}/file.txt",
                "name": "file.txt",
                "is_dir": False,
                "is_file": True,
                "md5": "md5-file",
            }
        ]

        def iter_children(current_dir, *args):
            return children_by_path[getattr(current_dir, "path", current_dir)]

        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.share_service.iter_shared_dir_children.side_effect = iter_children
        old_limit = sys.getrecursionlimit()

        try:
            sys.setrecursionlimit(100)
            result = self.storage._transfer_dir_tree_divide(
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
        self.storage.client.transfer_shared_paths.assert_called_once()
        self.assertEqual([999], self.storage.client.transfer_shared_paths.call_args.kwargs["fs_ids"])

    def test_transfer_dir_tree_divide_splits_files_by_batch_size(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        children = [
            {
                "raw": SimpleNamespace(path=f"/share/course/{fs_id}.txt", is_dir=False, is_file=True, fs_id=fs_id),
                "fs_id": fs_id,
                "path": f"/share/course/{fs_id}.txt",
                "name": f"{fs_id}.txt",
                "is_dir": False,
                "is_file": True,
            }
            for fs_id in range(1, TRANSFER_BATCH_SIZE * 2 + 2)
        ]
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.share_service.iter_shared_dir_children.return_value = children

        with patch("storage.time.sleep"):
            result = self.storage._transfer_dir_tree_divide(
                shared_dir,
                "/save/course",
                {"uk": 1, "share_id": 2, "bdstoken": "token"},
                "url",
                None,
            )

        calls = self.storage.client.transfer_shared_paths.call_args_list
        self.assertTrue(result["success"])
        self.assertEqual(len(children), result["completed_count"])
        self.assertEqual(3, len(calls))
        self.assertEqual(TRANSFER_BATCH_SIZE, len(calls[0].kwargs["fs_ids"]))
        self.assertEqual(TRANSFER_BATCH_SIZE, len(calls[1].kwargs["fs_ids"]))
        self.assertEqual(1, len(calls[2].kwargs["fs_ids"]))

    def test_transfer_dir_tree_divide_flushes_before_generator_exhausted(self):
        shared_dir = SimpleNamespace(path="/share/course", is_dir=True, fs_id=10)
        call_counts_after_full_batch = []

        def child(fs_id):
            return {
                "raw": SimpleNamespace(path=f"/share/course/{fs_id}.txt", is_dir=False, is_file=True, fs_id=fs_id),
                "fs_id": fs_id,
                "path": f"/share/course/{fs_id}.txt",
                "name": f"{fs_id}.txt",
                "is_dir": False,
                "is_file": True,
            }

        def children():
            for fs_id in range(1, TRANSFER_BATCH_SIZE + 1):
                yield child(fs_id)
            call_counts_after_full_batch.append(
                self.storage.client.transfer_shared_paths.call_count
            )
            yield child(TRANSFER_BATCH_SIZE + 1)

        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.share_service.iter_shared_dir_children.side_effect = lambda *args: children()

        with patch("storage.time.sleep"):
            result = self.storage._transfer_dir_tree_divide(
                shared_dir,
                "/save/course",
                {"uk": 1, "share_id": 2, "bdstoken": "token"},
                "url",
                None,
            )

        self.assertTrue(result["success"])
        self.assertEqual(TRANSFER_BATCH_SIZE + 1, result["completed_count"])
        self.assertEqual([1], call_counts_after_full_batch)
        self.assertEqual(2, self.storage.client.transfer_shared_paths.call_count)

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

    def test_transfer_share_falls_back_when_unknown_fast_path_error_created_target(self):
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
        self.storage.client.list.side_effect = [[], [SimpleNamespace(path="/save/course")]]
        self.storage.client.transfer_shared_paths.side_effect = RuntimeError("boom")

        result = self.storage.transfer_share("url", save_dir="/save")

        self.assertTrue(result["skipped"])
        self.storage._load_share_files.assert_not_called()
        self.storage.share_service.iter_shared_files.assert_called_once()

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
        self.storage._load_share_files = Mock()
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.return_value = [SimpleNamespace(path="/save/course")]

        result = self.storage.transfer_share("url", save_dir="/save")

        self.assertTrue(result["skipped"])
        self.storage.client.transfer_shared_paths.assert_not_called()
        self.storage._load_share_files.assert_not_called()
        self.storage.share_service.iter_shared_files.assert_called_once()

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
        self.storage._load_share_files = Mock()
        self.storage.path_service.ensure_dir_exists.return_value = True
        self.storage.client.list.side_effect = RuntimeError("list failed")

        result = self.storage.transfer_share("url", save_dir="/save")

        self.assertTrue(result["skipped"])
        self.storage.client.transfer_shared_paths.assert_not_called()
        self.storage._load_share_files.assert_not_called()
        self.storage.share_service.iter_shared_files.assert_called_once()

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
        self.storage._load_share_files = Mock()

        result = self.storage.transfer_share("url", save_dir="/save", regex_pattern="old")

        self.assertTrue(result["skipped"])
        self.storage.client.transfer_shared_paths.assert_not_called()
        self.storage._load_share_files.assert_not_called()
        self.storage.share_service.iter_shared_files.assert_called_once()

    def test_transfer_share_skips_tree_divide_when_folder_filter_is_set(self):
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

        result = self.storage.transfer_share("url", save_dir="/save", folder_filter="course")

        self.assertTrue(result["skipped"])
        self.storage.client.transfer_shared_paths.assert_not_called()
        self.storage.share_service.iter_shared_dir_children.assert_not_called()
        self.storage._load_share_files.assert_not_called()
        self.storage.share_service.iter_shared_files.assert_called_once()

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

    def test_process_single_share_config_passes_exclude_folder_filter(self):
        self.storage.transfer_share = Mock(return_value={"success": True, "message": "成功"})

        self.storage._process_single_share_config(
            1,
            1,
            {
                "share_url": "https://pan.baidu.com/s/abc",
                "save_dir": "/save",
                "exclude_folder_filter": r"^node_modules$",
            },
        )

        self.storage.transfer_share.assert_called_once_with(
            share_url="https://pan.baidu.com/s/abc",
            pwd=None,
            save_dir="/save",
            progress_callback=None,
            regex_pattern=None,
            regex_replace=None,
            folder_filter=None,
            exclude_folder_filter=r"^node_modules$",
        )

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

        with patch("storage.handle_error_and_notify"):
            result = self.storage._process_single_share_config(1, 2, "bad-config", progress_callback)

        self.assertFalse(result["success"])
        self.assertIn("缺少分享链接", result["error"])
        progress_callback.assert_any_call("error", f"【1/2】失败: {result['error']}")

    def test_transfer_multiple_shares_keeps_local_cache_across_share_links(self):
        # 跨链接 cache 复用：批量入口不再清空 _local_files_cache，
        # 让落到不同子目录的多链接可以复用对方扫过的本地索引。
        self.storage._local_files_cache = {("/save", ("preserved",), True): ["pre"]}
        self.storage._process_single_share_config = Mock(
            return_value={
                "index": 1,
                "share_url": "u1",
                "save_dir": "/save",
                "success": True,
                "partial": False,
                "message": "成功",
            }
        )

        with patch("storage.time.sleep"):
            result = self.storage.transfer_multiple_shares([{"share_url": "u1"}])

        self.assertTrue(result["success"])
        # 入口未清缓存
        self.assertIn(("/save", ("preserved",), True), self.storage._local_files_cache)

    def test_transfer_multiple_shares_concurrent_preserves_index_order(self):
        """并发模式下结果按原索引顺序合并，counters 与串行一致。"""
        import threading

        # 不同 worker 完成顺序不一致：让 index=1 的 worker 慢一点
        slowest_event = threading.Event()

        def side_effect(index, total_count, config, progress_callback):
            if index == 1:
                # 等待其他 worker 都进入再继续，模拟"快的先完成"
                slowest_event.wait(0.5)
            elif index == 3:
                slowest_event.set()
            return {
                "index": index,
                "share_url": config["share_url"],
                "save_dir": f"/save/{index}",
                "success": True,
                "partial": False,
                "message": f"成功-{index}",
            }

        self.storage._process_single_share_config = Mock(side_effect=side_effect)

        configs = [
            {"share_url": "u1"},
            {"share_url": "u2"},
            {"share_url": "u3"},
        ]
        with patch("storage.MULTI_SHARE_CONCURRENCY", 3), patch("storage.time.sleep"):
            result = self.storage.transfer_multiple_shares(configs)

        self.assertTrue(result["success"])
        self.assertEqual(3, result["success_count"])
        # 即便 worker 完成顺序受调度影响，results 仍按 index=1,2,3 的顺序合并
        self.assertEqual(
            [1, 2, 3], [item["index"] for item in result["results"]]
        )

    def test_transfer_multiple_shares_concurrent_aggregates_failed_count_once(self):
        """并发模式下 counters 不应重复累加（修复双重计数风险）。"""
        self.storage._process_single_share_config = Mock(
            side_effect=[
                {"index": 1, "share_url": "u1", "success": True, "partial": False, "message": "ok"},
                {"index": 2, "share_url": "u2", "success": False, "partial": False, "error": "fail"},
            ]
        )

        with patch("storage.MULTI_SHARE_CONCURRENCY", 2), patch("storage.time.sleep"):
            result = self.storage.transfer_multiple_shares(
                [{"share_url": "u1"}, {"share_url": "u2"}]
            )

        self.assertEqual(1, result["success_count"])
        self.assertEqual(1, result["failed_count"])
        self.assertEqual(0, result["partial_count"])

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

    def test_transfer_multiple_shares_collects_transfer_failed_details(self):
        self.storage._process_single_share_config = Mock(
            return_value={
                "index": 1,
                "share_url": "https://pan.baidu.com/s/***",
                "save_dir": "/a",
                "success": False,
                "partial": True,
                "message": "部分成功",
                "transfer_failed_files": [
                    {"fs_id": 1, "clean_path": "a.txt", "final_path": "a.txt", "error": "boom"}
                ],
                "transfer_failed_count": 1,
                "retry_config": {"share_url": "https://pan.baidu.com/s/abc", "save_dir": "/a"},
            }
        )

        result = self.storage.transfer_multiple_shares([{"share_url": "https://pan.baidu.com/s/abc"}])

        self.assertTrue(result["partial"])
        self.assertEqual(1, result["transfer_failed_count"])
        self.assertEqual("a.txt", result["transfer_failed_files"][0]["clean_path"])
        self.assertEqual("/a", result["transfer_failed_files"][0]["save_dir"])

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
            "warning", "源路径已存在,但内容不同(md5不同),跳过： old/a.txt"
        )

    def test_prepare_transfer_candidates_splits_regex_filter_reasons(self):
        candidates, summary, _ = self.storage._prepare_transfer_candidates(
            [
                {"fs_id": 1, "path": "safe/file.mp4", "md5": "md5-safe"},
                {"fs_id": 2, "path": "skip.txt", "md5": "md5-skip"},
                {"fs_id": 3, "path": "bad/../file.mp4", "md5": "md5-bad"},
            ],
            [Mock(is_dir=False)],
            "/save",
            regex_pattern=r"^(safe|bad/\.\.)/file\.mp4$",
            regex_replace=r"\1/out.mp4",
        )

        self.assertEqual([1], [candidate["fs_id"] for candidate in candidates])
        self.assertEqual(2, summary["regex_filtered_count"])
        self.assertEqual(1, summary["regex_unmatched_count"])
        self.assertEqual(1, summary["unsafe_regex_replace_count"])
        self.assertEqual(1, summary["candidate_count"])

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

    def test_build_transfer_list_marks_existing_path_without_md5_as_conflict(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, file_only=False: path.strip("/")
        progress_callback = Mock()

        result = self.storage._build_transfer_list(
            [{"fs_id": 1, "path": "a.txt", "md5": "src-md5"}],
            [Mock(is_dir=False)],
            "/save",
            {"a.txt": None},
            progress_callback=progress_callback,
        )

        self.assertEqual([], result)
        progress_callback.assert_any_call(
            "warning", "同路径已存在,但缺少MD5无法确认是否相同,跳过： a.txt"
        )
        progress_callback.assert_any_call(
            "info",
            "候选分析完成：共享文件 1 个，候选 1 个，正则过滤 0 个，本地已存在 1 个，"
            "冲突跳过 1 个，需要转存 0 个，其中需重命名 0 个",
        )

    def test_build_transfer_list_skips_duplicate_path_planned_in_same_batch(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, file_only=False: path.strip("/")
        progress_callback = Mock()

        result = self.storage._build_transfer_list(
            [
                {"fs_id": 1, "path": "a.txt", "md5": "md5-a"},
                {"fs_id": 2, "path": "a.txt", "md5": "md5-a"},
                {"fs_id": 3, "path": "a.txt", "md5": "md5-b"},
            ],
            [Mock(is_dir=False)],
            "/save",
            {},
            progress_callback=progress_callback,
        )

        self.assertEqual([1], [item.fs_id for item in result])
        progress_callback.assert_any_call(
            "warning", "本轮同路径已存在,但内容不同(md5不同),跳过： a.txt"
        )
        progress_callback.assert_any_call(
            "info",
            "候选分析完成：共享文件 3 个，候选 3 个，正则过滤 0 个，本地已存在 2 个，"
            "冲突跳过 1 个，需要转存 1 个，其中需重命名 0 个",
        )

    def test_build_transfer_list_treats_rename_target_as_planned_path(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, file_only=False: path.strip("/")
        progress_callback = Mock()

        result = self.storage._build_transfer_list(
            [
                {"fs_id": 1, "path": "old/a.txt", "md5": "md5-a"},
                {"fs_id": 2, "path": "copy/a.txt", "md5": "md5-b"},
            ],
            [Mock(is_dir=False)],
            "/save",
            {},
            regex_pattern=r"^(old|copy)/a\.txt$",
            regex_replace="new/a.txt",
            progress_callback=progress_callback,
        )

        self.assertEqual([1], [item.fs_id for item in result])
        self.assertEqual("new/a.txt", result[0].final_path)
        progress_callback.assert_any_call(
            "warning", "本轮重命名目标已存在,但内容不同(md5不同),跳过： new/a.txt"
        )

    def test_split_existing_transfer_items_requires_matching_md5(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        items = [
            TransferItem(1, "/save", "same.txt", "same.txt", False, "md5-a"),
            TransferItem(2, "/save", "diff.txt", "diff.txt", False, "md5-b"),
            TransferItem(3, "/save", "unknown.txt", "unknown.txt", False, "md5-c"),
        ]
        self.storage._scan_local_files_dict = Mock(
            return_value={"same.txt": "md5-a", "diff.txt": "other-md5", "unknown.txt": None}
        )

        existing_items, missing_items = self.storage._split_existing_transfer_items(items, "/save")

        self.assertEqual([items[0]], existing_items)
        self.assertEqual([items[1], items[2]], missing_items)

    def test_clear_local_files_cache_removes_full_and_targeted_entries(self):
        self.storage.path_service.normalize_path.side_effect = lambda path: path
        self.storage._local_files_cache = {
            "/save": ["full"],
            ("/save", ("a",)): ["legacy-targeted"],
            ("/save", ("a",), True): ["targeted"],
            "/other": ["other"],
        }

        self.storage._clear_local_files_cache("/save")

        self.assertEqual({"/other": ["other"]}, self.storage._local_files_cache)

    def test_clear_local_files_cache_with_affected_dirs_preserves_siblings(self):
        # 精确失效：传入受影响目录集合时，保留与之无关的兄弟子目录 cache
        self.storage.path_service.normalize_path.side_effect = lambda path: path
        self.storage.path_service._normalize_relative_dir = staticmethod(
            lambda d: "" if str(d or "").strip("/") in ("", ".") else str(d).strip("/")
        )
        self.storage._local_files_cache = {
            ("/save", ("a",), True): ["scan-a"],
            ("/save", ("b",), True): ["scan-b"],
            ("/save", ("c",), True): ["scan-c"],
            "/other": ["other"],
        }

        self.storage._clear_local_files_cache("/save", {"a", "b"})

        self.assertEqual(
            {("/save", ("c",), True): ["scan-c"], "/other": ["other"]},
            self.storage._local_files_cache,
        )

    def test_execute_transfer_plan_clears_local_cache_once_after_successful_groups(self):
        transfer_list = [
            (1, "/save/a", "a/1.txt", "a/1.txt", False),
            (2, "/save/b", "b/2.txt", "b/2.txt", False),
        ]
        self.storage._clear_local_files_cache = Mock()

        with patch("storage_transfer_plan.time.sleep"):
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                transfer_list, "url", 1, 2, "token", "/save"
            )

        self.assertEqual(2, success_count)
        self.assertEqual(transfer_list, successful_items)
        self.assertEqual([], failed_items)
        # 改造后只清掉本批写入的相对子目录 cache，保留同 target 下兄弟目录 cache
        self.storage._clear_local_files_cache.assert_called_once()
        call_args, _ = self.storage._clear_local_files_cache.call_args
        self.assertEqual("/save", call_args[0])
        self.assertEqual({"a", "b"}, call_args[1])

    def test_execute_transfer_plan_does_not_sleep_between_successful_groups(self):
        self.storage.path_service.normalize_path.side_effect = lambda path: path
        transfer_list = [
            (1, "/save/a", "a/1.txt", "a/1.txt", False),
            (2, "/save/b", "b/2.txt", "b/2.txt", False),
        ]

        with patch("storage_transfer_plan.time.sleep") as sleep:
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                transfer_list, "url", 1, 2, "token", "/save"
            )

        self.assertEqual(2, success_count)
        self.assertEqual(transfer_list, successful_items)
        self.assertEqual([], failed_items)
        sleep.assert_not_called()

    def test_execute_transfer_plan_still_waits_before_rate_limit_retry(self):
        self.storage.client.transfer_shared_paths.side_effect = [
            RuntimeError("error_code: -65"),
            None,
        ]
        transfer_item = (1, "/save", "a.txt", "a.txt", False)

        with patch("storage_transfer_plan.time.sleep") as sleep:
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                [transfer_item], "url", 1, 2, "token", "/save"
            )

        self.assertEqual(1, success_count)
        self.assertEqual([transfer_item], successful_items)
        self.assertEqual([], failed_items)
        sleep.assert_called_once_with(RATE_LIMIT_WAIT_TIME)

    def test_execute_transfer_plan_splits_fs_ids_by_batch_size(self):
        transfer_list = [
            (fs_id, "/save", f"{fs_id}.txt", f"{fs_id}.txt", False)
            for fs_id in range(TRANSFER_BATCH_SIZE * 2 + 1)
        ]

        with patch("storage_transfer_plan.time.sleep"):
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                transfer_list, "url", 1, 2, "token", "/save"
            )

        calls = self.storage.client.transfer_shared_paths.call_args_list
        self.assertEqual(len(transfer_list), success_count)
        self.assertEqual(transfer_list, successful_items)
        self.assertEqual([], failed_items)
        self.assertEqual(3, len(calls))
        self.assertEqual(TRANSFER_BATCH_SIZE, len(calls[0].kwargs["fs_ids"]))
        self.assertEqual(TRANSFER_BATCH_SIZE, len(calls[1].kwargs["fs_ids"]))
        self.assertEqual(1, len(calls[2].kwargs["fs_ids"]))

    def test_execute_transfer_plan_retries_count_limit_batch_with_smaller_batches(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        transfer_list = [
            (fs_id, "/save", f"{fs_id}.txt", f"{fs_id}.txt", False)
            for fs_id in range(350)
        ]
        self.storage.client.transfer_shared_paths.side_effect = [
            RuntimeError("error_code: 120, message: 转存文件数超限"),
            RuntimeError("error_code: 120, message: 转存文件数超限"),
            None,
            None,
        ]

        with patch("storage_transfer_plan.TRANSFER_BATCH_SIZE", 999), patch(
            "storage_transfer_plan.time.sleep"
        ), patch("storage_transfer_plan.handle_error_and_notify") as notify:
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                transfer_list, "url", 1, 2, "token", "/save"
            )

        batch_lengths = [
            len(call_args.kwargs["fs_ids"])
            for call_args in self.storage.client.transfer_shared_paths.call_args_list
        ]
        self.assertEqual(350, success_count)
        self.assertEqual(transfer_list, successful_items)
        self.assertEqual([], failed_items)
        self.assertEqual([350, 300, 200, 150], batch_lengths)
        notify.assert_not_called()

    def test_execute_transfer_plan_isolates_retryable_batch_failures(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        self.storage._scan_local_files_dict = Mock(return_value={})
        transfer_list = [
            (fs_id, "/save", f"{fs_id}.txt", f"{fs_id}.txt", False)
            for fs_id in range(3)
        ]
        failed_full_batch = False
        failed_single_item = False

        def transfer_side_effect(**kwargs):
            nonlocal failed_full_batch, failed_single_item
            fs_ids = kwargs["fs_ids"]
            if len(fs_ids) > 1 and not failed_full_batch:
                failed_full_batch = True
                raise RequestsJSONDecodeError("Expecting value", "", 0)
            if fs_ids == [1] and not failed_single_item:
                failed_single_item = True
                raise RequestsJSONDecodeError("Expecting value", "", 0)

        self.storage.client.transfer_shared_paths.side_effect = transfer_side_effect

        with patch("storage_transfer_plan.TRANSFER_BATCH_SIZE", 3), patch(
            "storage_transfer_plan.TRANSFER_FAILED_RETRY_ATTEMPTS", 1
        ), patch("storage_transfer_plan.time.sleep"), patch(
            "storage_transfer_plan.handle_error_and_notify"
        ) as notify:
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                transfer_list, "url", 1, 2, "token", "/save"
            )

        batch_lengths = [
            len(call_args.kwargs["fs_ids"])
            for call_args in self.storage.client.transfer_shared_paths.call_args_list
        ]
        self.assertEqual(3, success_count)
        self.assertCountEqual(transfer_list, successful_items)
        self.assertEqual([], failed_items)
        self.assertEqual([3, 1, 1, 1, 1], batch_lengths)
        notify.assert_not_called()

    def test_execute_transfer_plan_does_not_hot_retry_storage_temporary_errors(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        transfer_item = (1, "/save", "a.txt", "a.txt", False)
        self.storage.client.transfer_shared_paths.side_effect = RuntimeError(
            "error_code: 4, message: 存储好像出问题了，请稍候再试"
        )
        self.storage._split_existing_transfer_items = Mock(
            return_value=([], [transfer_item])
        )

        with patch("storage_transfer_plan.TRANSFER_FAILED_RETRY_ATTEMPTS", 5), patch(
            "storage_transfer_plan.time.sleep"
        ) as sleep, patch("storage_transfer_plan.handle_error_and_notify") as notify:
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                [transfer_item], "url", 1, 2, "token", "/save"
            )

        self.assertEqual(0, success_count)
        self.assertEqual([], successful_items)
        self.assertEqual(1, len(failed_items))
        self.assertEqual("4", failed_items[0]["error_code"])
        self.assertEqual("network", failed_items[0]["error_kind"])
        self.assertTrue(failed_items[0]["retryable"])
        self.assertTrue(failed_items[0]["temporary"])
        self.assertIn("failed_at", failed_items[0])
        self.assertNotIn("target_dir", failed_items[0])
        self.assertNotIn("need_rename", failed_items[0])
        self.assertNotIn("attempts", failed_items[0])
        self.assertEqual(1, self.storage.client.transfer_shared_paths.call_count)
        sleep.assert_not_called()
        notify.assert_not_called()

    def test_execute_transfer_plan_stops_remaining_batches_on_storage_temporary_error(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        transfer_list = [
            (1, "/save", "a.txt", "a.txt", False),
            (2, "/save", "b.txt", "b.txt", False),
        ]
        self.storage.client.transfer_shared_paths.side_effect = RuntimeError(
            "error_code: 4, message: 存储好像出问题了，请稍候再试"
        )
        self.storage._split_existing_transfer_items = Mock(
            return_value=([], [transfer_list[0]])
        )

        with patch("storage_transfer_plan.TRANSFER_BATCH_SIZE", 1), patch(
            "storage_transfer_plan.TRANSFER_FAILED_RETRY_ATTEMPTS", 5
        ), patch("storage_transfer_plan.time.sleep") as sleep, patch(
            "storage_transfer_plan.handle_error_and_notify"
        ):
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                transfer_list, "url", 1, 2, "token", "/save"
            )

        self.assertEqual(0, success_count)
        self.assertEqual([], successful_items)
        self.assertEqual(["a.txt", "b.txt"], [item["clean_path"] for item in failed_items])
        self.assertEqual(1, self.storage.client.transfer_shared_paths.call_count)
        sleep.assert_not_called()

    def test_execute_transfer_plan_retries_failed_items_only(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        transfer_list = [
            (1, "/save/a", "a/1.txt", "a/1.txt", False),
            (2, "/save/b", "b/2.txt", "b/2.txt", False),
        ]
        self.storage.client.transfer_shared_paths.side_effect = [
            None,
            RequestsJSONDecodeError("Expecting value", "", 0),
            None,
        ]
        self.storage._split_existing_transfer_items = Mock(
            return_value=([], [transfer_list[1]])
        )

        with patch("storage_transfer_plan.time.sleep"):
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                transfer_list, "url", 1, 2, "token", "/save"
            )

        calls = self.storage.client.transfer_shared_paths.call_args_list
        self.assertEqual(2, success_count)
        self.assertEqual(transfer_list, successful_items)
        self.assertEqual([], failed_items)
        self.assertEqual([1], calls[0].kwargs["fs_ids"])
        self.assertEqual([2], calls[1].kwargs["fs_ids"])
        self.assertEqual([2], calls[2].kwargs["fs_ids"])

    def test_execute_transfer_plan_does_not_retry_non_retryable_errors(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        transfer_item = (1, "/save", "a.txt", "a.txt", False)
        self.storage.client.transfer_shared_paths.side_effect = RuntimeError(
            "error_code: 31066"
        )
        self.storage._split_existing_transfer_items = Mock(
            return_value=([], [transfer_item])
        )

        with patch("storage_transfer_plan.TRANSFER_FAILED_RETRY_ATTEMPTS", 1), patch(
            "storage_transfer_plan.time.sleep"
        ) as sleep, patch("storage_transfer_plan.handle_error_and_notify") as notify:
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                [transfer_item], "url", 1, 2, "token", "/save"
            )

        self.assertEqual(0, success_count)
        self.assertEqual([], successful_items)
        self.assertEqual(1, len(failed_items))
        self.assertEqual("a.txt", failed_items[0]["clean_path"])
        self.assertEqual("31066", failed_items[0]["error_code"])
        self.assertEqual("missing_path", failed_items[0]["error_kind"])
        self.assertFalse(failed_items[0]["retryable"])
        self.assertFalse(failed_items[0]["temporary"])
        self.assertNotIn("target_dir", failed_items[0])
        self.assertNotIn("need_rename", failed_items[0])
        self.assertNotIn("attempts", failed_items[0])
        self.assertEqual(1, self.storage.client.transfer_shared_paths.call_count)
        sleep.assert_not_called()
        notify.assert_called_once()

    def test_execute_transfer_plan_clears_failed_record_when_retry_finds_final_path(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        transfer_item = (1, "/save", "old.txt", "done.txt", True)
        completed_item = (1, "/save", "done.txt", "done.txt", False)
        self.storage.client.transfer_shared_paths.side_effect = [
            RequestsJSONDecodeError("Expecting value", "", 0),
            RequestsJSONDecodeError("Expecting value", "", 0),
        ]
        self.storage._split_existing_transfer_items = Mock(
            side_effect=[([], [transfer_item]), ([completed_item], [])]
        )

        with patch("storage_transfer_plan.TRANSFER_FAILED_RETRY_ATTEMPTS", 1), patch(
            "storage_transfer_plan.time.sleep"
        ):
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                [transfer_item], "url", 1, 2, "token", "/save"
            )

        self.assertEqual(1, success_count)
        self.assertEqual([completed_item], successful_items)
        self.assertEqual([], failed_items)

    def test_split_existing_transfer_items_counts_already_transferred_files(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        items = [
            TransferItem(1, "/save", "a.txt", "a.txt", False, "md5-a"),
            TransferItem(2, "/save", "old.txt", "done.txt", True, "md5-done"),
            TransferItem(3, "/save", "missing.txt", "missing.txt", False, "md5-missing"),
        ]
        self.storage._scan_local_files_dict = Mock(
            return_value={"a.txt": "md5-a", "done.txt": "md5-done"}
        )

        existing_items, missing_items = self.storage._split_existing_transfer_items(
            items, "/save"
        )

        self.assertEqual(
            [
                (1, "/save", "a.txt", "a.txt", False),
                (2, "/save", "done.txt", "done.txt", False),
            ],
            existing_items,
        )
        self.assertEqual([items[2]], missing_items)

    def test_split_existing_transfer_items_cache_miss_does_not_clear_without_force_refresh(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        item = TransferItem(1, "/save", "a.txt", "a.txt", False, "md5-a")
        scan_cache = {}
        self.storage._clear_local_files_cache = Mock()
        self.storage._scan_local_files_dict = Mock(return_value={"a.txt": "md5-a"})

        existing_items, missing_items = self.storage._split_existing_transfer_items(
            [item], "/save", scan_cache=scan_cache
        )
        cached_existing_items, cached_missing_items = self.storage._split_existing_transfer_items(
            [item], "/save", scan_cache=scan_cache
        )

        self.assertEqual([item], existing_items)
        self.assertEqual([], missing_items)
        self.assertEqual([item], cached_existing_items)
        self.assertEqual([], cached_missing_items)
        self.storage._clear_local_files_cache.assert_not_called()
        self.storage._scan_local_files_dict.assert_called_once_with("/save", None, {""})
        self.assertEqual({("/save", ("",), True): {"a.txt": "md5-a"}}, scan_cache)

    def test_split_existing_transfer_items_force_refresh_ignores_stale_scan_cache(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        item = TransferItem(1, "/save", "a.txt", "a.txt", False, "md5-a")
        scan_cache = {("/save", ("",), True): {}}
        self.storage._clear_local_files_cache = Mock()
        self.storage._scan_local_files_dict = Mock(return_value={"a.txt": "md5-a"})

        existing_items, missing_items = self.storage._split_existing_transfer_items(
            [item], "/save", scan_cache=scan_cache, force_refresh=True
        )

        self.assertEqual([item], existing_items)
        self.assertEqual([], missing_items)
        self.storage._clear_local_files_cache.assert_called_once_with("/save")
        self.storage._scan_local_files_dict.assert_called_once_with("/save", None, {""})
        self.assertEqual({("/save", ("",), True): {"a.txt": "md5-a"}}, scan_cache)

    def test_execute_transfer_plan_force_refreshes_after_each_failed_attempt(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        transfer_item = TransferItem(1, "/save", "a.txt", "a.txt", False, "md5-a")
        self.storage.client.transfer_shared_paths.side_effect = [
            RequestsJSONDecodeError("Expecting value", "", 0),
            RequestsJSONDecodeError("Expecting value", "", 0),
        ]
        self.storage._clear_local_files_cache = Mock()
        self.storage._scan_local_files_dict = Mock(side_effect=[{}, {"a.txt": "md5-a"}])

        with patch("storage_transfer_plan.TRANSFER_FAILED_RETRY_ATTEMPTS", 1), patch(
            "storage_transfer_plan.time.sleep"
        ), patch("storage_transfer_plan.handle_error_and_notify"):
            success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
                [transfer_item], "url", 1, 2, "token", "/save"
            )

        self.assertEqual(1, success_count)
        self.assertEqual([transfer_item], successful_items)
        self.assertEqual([], failed_items)
        self.assertEqual(2, self.storage._scan_local_files_dict.call_count)

    def test_execute_transfer_plan_renames_existing_source_without_retransfer(self):
        self.storage.path_service.normalize_path.side_effect = lambda path, **kwargs: path
        transfer_item = TransferItem(
            1, "/save/old", "old/a.txt", "new/a.txt", True, "md5-a"
        )
        self.storage._scan_local_files_dict = Mock(return_value={"old/a.txt": "md5-a"})

        success_count, successful_items, failed_items = self.storage._execute_transfer_plan(
            [transfer_item], "url", 1, 2, "token", "/save"
        )

        self.assertEqual(1, success_count)
        self.assertEqual([transfer_item], successful_items)
        self.assertEqual([], failed_items)
        self.storage.client.transfer_shared_paths.assert_not_called()
        self.storage._scan_local_files_dict.assert_called_once_with("/save", None, {"new", "old"})

    def test_rename_transferred_files_sleeps_only_between_renames(self):
        self.storage.path_service.ensure_dir_exists.return_value = True
        transfer_items = [
            (1, "/save", "a.txt", "renamed-a.txt", True),
            (2, "/save", "b.txt", "renamed-b.txt", True),
        ]

        with patch("storage_rename.time.sleep") as sleep:
            result = self.storage._rename_transferred_files(transfer_items, "/save")

        self.assertEqual(["renamed-a.txt", "renamed-b.txt"], result["transferred_files"])
        sleep.assert_called_once_with(RENAME_DELAY)

    def test_rename_transferred_files_reports_failures_as_partial(self):
        self.storage.client.rename.side_effect = RuntimeError("rename boom")
        self.storage.path_service.ensure_dir_exists.return_value = True
        progress_callback = Mock()

        with patch("storage_rename.handle_error_and_notify"):
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

    def test_build_transfer_result_includes_transfer_failed_files(self):
        failed_files = [
            {"fs_id": 2, "clean_path": "b.txt", "final_path": "b.txt", "error": "boom"}
        ]

        result = self.storage._build_transfer_result(
            1,
            2,
            {
                "transferred_files": ["a.txt"],
                "rename_failed_files": [],
                "rename_failed_count": 0,
                "completed_count": 1,
            },
            None,
            failed_files,
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(1, result["transfer_failed_count"])
        self.assertEqual(failed_files, result["transfer_failed_files"])
        self.assertIn("转存失败", result["message"])

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
        with patch("storage.handle_error_and_notify") as notify:
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

        notify.assert_called_once()
        self.assertEqual(
            {
                "success": False,
                "partial": False,
                "message": "",
                "error": "转存失败，没有文件成功转存",
                "transferred_files": [],
                "transfer_failed_files": [],
                "transfer_failed_count": 0,
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

    def test_quota_reuses_initialized_cache(self):
        adapter = BaiduClientAdapter.__new__(BaiduClientAdapter)
        adapter.client = Mock()
        adapter._quota_info = (100, 20)

        self.assertEqual((100, 20), adapter.quota())

        adapter.client.quota.assert_not_called()

    def test_quota_refresh_updates_cache(self):
        adapter = BaiduClientAdapter.__new__(BaiduClientAdapter)
        adapter.client = Mock()
        adapter.client.quota.return_value = (200, 50)
        adapter._quota_info = (100, 20)

        self.assertEqual((200, 50), adapter.quota(refresh=True))
        self.assertEqual((200, 50), adapter.quota())

        adapter.client.quota.assert_called_once_with()

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

    def test_call_with_retry_retries_json_decode_errors(self):
        adapter = BaiduClientAdapter.__new__(BaiduClientAdapter)
        adapter.max_retries = 2
        adapter.is_github_actions = False
        adapter.base_retry_delay = 0
        calls = []

        def fail_once():
            calls.append(1)
            if len(calls) == 1:
                raise RequestsJSONDecodeError("Expecting value", "", 0)
            return "ok"

        self.assertEqual("ok", adapter.call_with_retry(fail_once))
        self.assertEqual(2, len(calls))

    def test_call_with_retry_retries_baidu_storage_issue(self):
        adapter = BaiduClientAdapter.__new__(BaiduClientAdapter)
        adapter.max_retries = 2
        adapter.is_github_actions = False
        adapter.base_retry_delay = 1
        calls = []

        def fail_once():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("error_code: 4, message: 存储好像出问题了，请稍候再试")
            return "ok"

        with patch("storage_client.time.sleep") as sleep:
            self.assertEqual("ok", adapter.call_with_retry(fail_once, suppress_retry_abort=False))

        self.assertEqual(2, len(calls))
        sleep.assert_called_once_with(1)


class BaiduClientAdapterPoolSizingTests(unittest.TestCase):
    """覆盖 _compute_pool_maxsize 决策矩阵与 session_pool_info 行为。"""

    def _adapter(self, is_github_actions=False):
        adapter = BaiduClientAdapter.__new__(BaiduClientAdapter)
        adapter.is_github_actions = is_github_actions
        adapter._session_pool_info = BaiduClientAdapter._new_session_pool_info()
        return adapter

    def test_compute_pool_maxsize_local_default(self):
        adapter = self._adapter(is_github_actions=False)
        with patch.dict(os.environ, {}, clear=True):
            maxsize, connections, fanout = adapter._compute_pool_maxsize()
        # 本地默认：max(32, 1*4) = 32，无 GA 偏置
        self.assertEqual(32, maxsize)
        self.assertEqual(32, connections)
        self.assertEqual(4, fanout)

    def test_compute_pool_maxsize_ga_default_adds_offset(self):
        adapter = self._adapter(is_github_actions=True)
        with patch.dict(os.environ, {}, clear=True):
            maxsize, connections, fanout = adapter._compute_pool_maxsize()
        # GA 默认：max(32, 1*4) + 8 = 40
        self.assertEqual(40, maxsize)
        self.assertEqual(40, connections)

    def test_compute_pool_maxsize_explicit_value_ignores_ga_offset(self):
        adapter = self._adapter(is_github_actions=True)
        with patch.dict(
            os.environ, {"TRANSFERSHARE_PCS_POOL_MAXSIZE": "64"}, clear=True
        ):
            maxsize, connections, _ = adapter._compute_pool_maxsize()
        # 显式 64：直接用，不再 +8
        self.assertEqual(64, maxsize)
        self.assertEqual(64, connections)

    def test_compute_pool_maxsize_zero_signals_disable(self):
        adapter = self._adapter(is_github_actions=True)
        with patch.dict(
            os.environ, {"TRANSFERSHARE_PCS_POOL_MAXSIZE": "0"}, clear=True
        ):
            maxsize, connections, fanout = adapter._compute_pool_maxsize()
        # 显式 0：表示禁用 patch
        self.assertEqual(0, maxsize)
        self.assertEqual(0, connections)
        self.assertEqual(4, fanout)

    def test_compute_pool_maxsize_invalid_fanout_falls_back(self):
        adapter = self._adapter(is_github_actions=False)
        with patch.dict(
            os.environ, {"TRANSFERSHARE_PCS_POOL_FANOUT": "bad"}, clear=True
        ):
            with self.assertLogs("transfershare", level="WARNING") as ctx:
                _, _, fanout = adapter._compute_pool_maxsize()
        self.assertEqual(4, fanout)
        self.assertTrue(
            any("TRANSFERSHARE_PCS_POOL_FANOUT" in msg for msg in ctx.output)
        )

    def test_compute_pool_maxsize_scales_with_multi_concurrency(self):
        adapter = self._adapter(is_github_actions=False)
        with patch.dict(
            os.environ,
            {"TRANSFERSHARE_MULTI_SHARE_CONCURRENCY": "16"},
            clear=True,
        ):
            maxsize, _, _ = adapter._compute_pool_maxsize()
        # 16 * 4 = 64 > 32，取 64
        self.assertEqual(64, maxsize)

    def test_session_pool_info_is_a_copy(self):
        adapter = self._adapter()
        info = adapter.session_pool_info
        info["pool_maxsize"] = 9999
        # 修改返回字典不影响内部状态
        self.assertEqual(0, adapter._session_pool_info["pool_maxsize"])
        self.assertEqual(0, adapter.session_pool_info["pool_maxsize"])

    def test_session_pool_info_falls_back_when_init_skipped(self):
        # 测试场景：BaiduClientAdapter.__new__ 跳过 __init__，
        # 没设置 _session_pool_info 字段
        adapter = BaiduClientAdapter.__new__(BaiduClientAdapter)
        info = adapter.session_pool_info
        self.assertEqual(
            {
                "pool_maxsize": 0,
                "pool_connections": 0,
                "fanout": 0,
                "patched": False,
            },
            info,
        )


class BaiduClientAdapterSessionPatchTests(unittest.TestCase):
    """覆盖 _patch_session_pool / _apply_session_patches / _init_client 集成路径。"""

    def _adapter(self, is_github_actions=False):
        adapter = BaiduClientAdapter.__new__(BaiduClientAdapter)
        adapter.is_github_actions = is_github_actions
        adapter._session_pool_info = BaiduClientAdapter._new_session_pool_info()
        adapter._session_patches_applied = False
        adapter._session_cookie_lock = None
        return adapter

    @staticmethod
    def _make_pcs_candidate(session=None):
        import requests

        if session is None:
            session = requests.Session()
        return SimpleNamespace(_session=session)

    def test_patch_session_pool_replaces_https_and_http_adapters(self):
        adapter = self._adapter(is_github_actions=False)
        pcs_candidate = self._make_pcs_candidate()
        original_https = pcs_candidate._session.adapters["https://"]
        original_http = pcs_candidate._session.adapters["http://"]

        with patch.dict(os.environ, {}, clear=True):
            ok = adapter._patch_session_pool(pcs_candidate)

        self.assertTrue(ok)
        new_https = pcs_candidate._session.adapters["https://"]
        new_http = pcs_candidate._session.adapters["http://"]
        # 必须是新实例（adapters 被替换）
        self.assertIsNot(original_https, new_https)
        self.assertIsNot(original_http, new_http)
        # 池容量等于 _compute_pool_maxsize 决策值（本地默认 32）
        self.assertEqual(
            32, new_https.poolmanager.connection_pool_kw.get("maxsize")
        )
        self.assertEqual(
            32, new_http.poolmanager.connection_pool_kw.get("maxsize")
        )
        self.assertTrue(adapter.session_pool_info["patched"])
        self.assertEqual(32, adapter.session_pool_info["pool_maxsize"])

    def test_patch_session_pool_skips_when_session_is_not_requests_session(self):
        adapter = self._adapter()
        pcs_candidate = SimpleNamespace(_session=object())

        with self.assertLogs("transfershare", level="WARNING") as ctx:
            ok = adapter._patch_session_pool(pcs_candidate)

        self.assertFalse(ok)
        self.assertFalse(adapter.session_pool_info["patched"])
        self.assertTrue(
            any("不是 requests.Session 实例" in msg for msg in ctx.output)
        )

    def test_patch_session_pool_disabled_by_explicit_zero(self):
        adapter = self._adapter()
        pcs_candidate = self._make_pcs_candidate()
        original_adapters = dict(pcs_candidate._session.adapters)

        with patch.dict(
            os.environ, {"TRANSFERSHARE_PCS_POOL_MAXSIZE": "0"}, clear=True
        ):
            ok = adapter._patch_session_pool(pcs_candidate)

        self.assertFalse(ok)
        # adapters 完全没动
        self.assertEqual(original_adapters, dict(pcs_candidate._session.adapters))
        self.assertFalse(adapter.session_pool_info["patched"])

    def test_patch_session_pool_rolls_back_on_mount_failure(self):
        adapter = self._adapter(is_github_actions=False)
        pcs_candidate = self._make_pcs_candidate()
        snapshot = dict(pcs_candidate._session.adapters)

        # 第一次 mount https:// 成功，第二次 mount http:// 抛异常
        original_mount = pcs_candidate._session.mount
        call_counter = {"count": 0}

        def faulty_mount(prefix, ad):
            call_counter["count"] += 1
            if call_counter["count"] == 2:
                raise RuntimeError("boom")
            return original_mount(prefix, ad)

        with patch.dict(os.environ, {}, clear=True), patch.object(
            pcs_candidate._session, "mount", side_effect=faulty_mount
        ), self.assertLogs("transfershare", level="WARNING") as ctx:
            ok = adapter._patch_session_pool(pcs_candidate)

        self.assertFalse(ok)
        # adapters 必须完全回滚到 snapshot
        self.assertEqual(set(snapshot.keys()), set(pcs_candidate._session.adapters.keys()))
        for key, value in snapshot.items():
            self.assertIs(value, pcs_candidate._session.adapters[key])
        self.assertFalse(adapter.session_pool_info["patched"])
        self.assertTrue(
            any("会话连接池替换失败" in msg for msg in ctx.output)
        )

    def test_apply_session_patches_skips_when_already_patched(self):
        adapter = self._adapter()
        pcs_candidate = self._make_pcs_candidate()
        pcs_candidate._session_concurrency_patched = True

        with patch.dict(os.environ, {}, clear=True), patch.object(
            adapter, "_patch_session_pool"
        ) as mock_pool, self.assertLogs("transfershare", level="DEBUG") as ctx:
            adapter._apply_session_patches(pcs_candidate)

        mock_pool.assert_not_called()
        self.assertTrue(any("已存在" in msg for msg in ctx.output))

    def test_apply_session_patches_is_idempotent(self):
        adapter = self._adapter()
        pcs_candidate = self._make_pcs_candidate()

        with patch.dict(os.environ, {}, clear=True):
            adapter._apply_session_patches(pcs_candidate)
            # 第二次：因 _session_concurrency_patched=True 应跳过
            with patch.object(
                pcs_candidate._session, "mount"
            ) as mock_mount:
                adapter._apply_session_patches(pcs_candidate)
        # 第二次完全没调 mount
        mock_mount.assert_not_called()
        self.assertTrue(getattr(pcs_candidate, "_session_concurrency_patched", False))

    def test_apply_session_patches_logs_info_with_status(self):
        adapter = self._adapter(is_github_actions=True)
        pcs_candidate = self._make_pcs_candidate()

        with patch.dict(os.environ, {}, clear=True), self.assertLogs(
            "transfershare", level="INFO"
        ) as ctx:
            adapter._apply_session_patches(pcs_candidate)

        info_lines = [msg for msg in ctx.output if "会话并发 patch 已启用" in msg]
        self.assertEqual(1, len(info_lines))
        info_line = info_lines[0]
        self.assertIn("pool_maxsize=40", info_line)  # GA 默认 32+8
        self.assertIn("ga_environment=true", info_line)
        self.assertIn("patched_cookies_update=false", info_line)  # Phase 3 才打开

    def test_init_client_keeps_working_when_session_pool_patch_fails(self):
        """集成：_pcs_factory 注入 fake；mount 抛异常时 _init_client 仍然成功。"""
        import requests

        class FakeSession(requests.Session):
            pass

        class FakePcs:
            def __init__(self):
                self._session = FakeSession()
                # 让 mount 抛异常以触发回滚
                self._session.mount = Mock(side_effect=RuntimeError("mount-boom"))

        class FakeApi:
            def __init__(self, cookies):
                self._pcs = FakePcs()

            def quota(self):
                return (1024, 512)

        with patch.object(
            BaiduClientAdapter, "_pcs_factory", staticmethod(FakeApi)
        ), patch.dict(os.environ, {}, clear=True):
            adapter = BaiduClientAdapter("BDUSS=foo; STOKEN=bar")

        # _init_client 不抛异常
        self.assertIsNotNone(adapter.client)
        self.assertEqual((1024, 512), adapter._quota_info)
        # patch 失败被回滚，patched=False
        self.assertFalse(adapter.session_pool_info["patched"])

    def test_patch_cookies_update_skipped_when_method_missing(self):
        adapter = self._adapter()
        pcs_candidate = SimpleNamespace()  # 无 _cookies_update

        with self.assertLogs("transfershare", level="DEBUG") as ctx:
            ok = adapter._patch_cookies_update(pcs_candidate)

        self.assertFalse(ok)
        self.assertIsNone(adapter._session_cookie_lock)
        self.assertTrue(any("未暴露 _cookies_update" in msg for msg in ctx.output))

    def test_patch_cookies_update_wraps_original_method(self):
        adapter = self._adapter()
        calls = []

        def original(cookies, *args, **kwargs):
            calls.append(dict(cookies))

        pcs_candidate = SimpleNamespace(_cookies_update=original)

        ok = adapter._patch_cookies_update(pcs_candidate)
        self.assertTrue(ok)
        self.assertIsNotNone(adapter._session_cookie_lock)

        # 调用 patched 后原方法应当被调用
        pcs_candidate._cookies_update({"K": "V"})
        self.assertEqual([{"K": "V"}], calls)

    def test_patch_cookies_update_serializes_concurrent_updates(self):
        """并发场景下 patched _cookies_update 必须串行进入原方法。"""
        import time

        adapter = self._adapter()
        # 用 set + lock 记录"任意时刻最多一个线程在 critical section"的不变量
        in_critical = set()
        violations = []
        record_lock = threading.Lock()

        def original(cookies, *args, **kwargs):
            tid = threading.get_ident()
            with record_lock:
                if in_critical:
                    violations.append(("overlap", in_critical.copy(), tid))
                in_critical.add(tid)
            time.sleep(0.005)  # 模拟非原子写
            with record_lock:
                in_critical.discard(tid)

        pcs_candidate = SimpleNamespace(_cookies_update=original)
        self.assertTrue(adapter._patch_cookies_update(pcs_candidate))

        n = 8
        barrier = threading.Barrier(n)
        threads = []
        errors = []

        def worker(i):
            try:
                barrier.wait(timeout=2)
                pcs_candidate._cookies_update({f"K{i}_a": "x", f"K{i}_b": "y"})
            except Exception as exc:
                errors.append(exc)

        for i in range(n):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual([], errors)
        self.assertEqual([], violations, f"observed concurrent entries: {violations}")

    def test_apply_session_patches_logs_cookie_status(self):
        adapter = self._adapter(is_github_actions=False)
        pcs_candidate = self._make_pcs_candidate()
        # 给 pcs_candidate 一个简单的 _cookies_update
        pcs_candidate._cookies_update = lambda c, *a, **kw: None

        with patch.dict(os.environ, {}, clear=True), self.assertLogs(
            "transfershare", level="INFO"
        ) as ctx:
            adapter._apply_session_patches(pcs_candidate)

        info_lines = [msg for msg in ctx.output if "patched_cookies_update=true" in msg]
        self.assertEqual(1, len(info_lines))
        # 同时 cookie lock 已建立
        self.assertIsNotNone(adapter._session_cookie_lock)


if __name__ == "__main__":
    unittest.main()
