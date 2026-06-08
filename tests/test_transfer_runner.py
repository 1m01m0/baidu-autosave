import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

if "baidupcs_py" not in sys.modules:
    baidupcs_module = types.ModuleType("baidupcs_py")
    baidupcs_submodule = types.ModuleType("baidupcs_py.baidupcs")

    class DummyBaiduPCSApi:
        pass

    baidupcs_submodule.BaiduPCSApi = DummyBaiduPCSApi
    baidupcs_module.baidupcs = baidupcs_submodule
    sys.modules["baidupcs_py"] = baidupcs_module
    sys.modules["baidupcs_py.baidupcs"] = baidupcs_submodule

import transfer_runner


class TransferRunnerSmokeTests(unittest.TestCase):
    def test_main_runs_success_flow(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
            "share_urls": [{"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/AutoTransfer"}],
            "share_configs": [{"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/AutoTransfer"}],
        }
        result = {
            "success": True,
            "summary": "完成",
            "transferred_files": ["video.mp4"],
        }
        fake_storage = Mock()
        fake_storage.is_valid.return_value = True
        fake_storage.get_quota_info.return_value = {"used_gb": 1, "total_gb": 10}
        fake_storage.transfer_multiple_shares.return_value = result
        fake_notifier = Mock()
        fake_notifier.send_transfer_result.return_value = True
        fake_logger = Mock()

        with patch.object(transfer_runner, "setup_logging"), patch.object(
            transfer_runner, "get_logger", return_value=fake_logger
        ), patch.object(transfer_runner, "log_startup"), patch.object(
            transfer_runner, "log_config_loaded"
        ), patch.object(transfer_runner, "check_network_connectivity"), patch.object(
            transfer_runner, "load_runtime_config", return_value=config
        ), patch.object(
            transfer_runner, "WeChatNotifier", return_value=fake_notifier
        ), patch.object(
            transfer_runner, "BaiduStorage", return_value=fake_storage
        ), patch.object(transfer_runner, "log_shutdown") as mock_shutdown:
            transfer_runner.main()

        fake_storage.transfer_multiple_shares.assert_called_once_with(
            share_configs=config["share_configs"],
            progress_callback=transfer_runner.progress_callback,
        )
        fake_notifier.send_transfer_result.assert_called_once_with(result, config)
        mock_shutdown.assert_called_once_with(success=True)

    def test_main_exits_when_load_runtime_config_raises_validation_error(self):
        fake_logger = Mock()

        with patch.object(transfer_runner, "setup_logging"), patch.object(
            transfer_runner, "get_logger", return_value=fake_logger
        ), patch.object(transfer_runner, "log_startup"), patch.object(
            transfer_runner, "check_network_connectivity"
        ), patch.object(
            transfer_runner,
            "load_runtime_config",
            side_effect=ValueError("配置校验失败: bad config"),
        ), patch.object(
            transfer_runner, "handle_error_and_notify"
        ) as mock_handle_error, patch.object(
            transfer_runner.sys, "exit", side_effect=SystemExit(1)
        ), patch.object(
            transfer_runner, "log_shutdown"
        ) as mock_shutdown:
            with self.assertRaises(SystemExit) as cm:
                transfer_runner.main()

        self.assertEqual(1, cm.exception.code)
        mock_handle_error.assert_called_once()
        mock_shutdown.assert_called_once_with(success=False)

    def test_notify_transfer_result_can_be_suppressed_by_env(self):
        notifier = Mock()
        logger = Mock()

        with patch.dict(
            os.environ, {"TRANSFERSHARE_SUPPRESS_RESULT_NOTIFICATION": "1"}, clear=False
        ):
            transfer_runner.notify_transfer_result(
                notifier, {"success": False}, {"wechat_webhook": "url"}, logger
            )

        notifier.send_transfer_result.assert_not_called()
        logger.info.assert_called_with("已抑制本次运行的最终结果通知")

    def test_main_exits_when_transfer_fails(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
            "share_urls": [{"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/AutoTransfer"}],
            "share_configs": [{"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/AutoTransfer"}],
        }
        result = {"success": False, "error": "失败"}
        fake_storage = Mock()
        fake_storage.is_valid.return_value = True
        fake_storage.get_quota_info.return_value = None
        fake_storage.transfer_multiple_shares.return_value = result
        fake_notifier = Mock()
        fake_notifier.send_transfer_result.return_value = True
        fake_logger = Mock()

        with patch.object(transfer_runner, "setup_logging"), patch.object(
            transfer_runner, "get_logger", return_value=fake_logger
        ), patch.object(transfer_runner, "log_startup"), patch.object(
            transfer_runner, "log_config_loaded"
        ), patch.object(transfer_runner, "check_network_connectivity"), patch.object(
            transfer_runner, "load_runtime_config", return_value=config
        ), patch.object(
            transfer_runner, "WeChatNotifier", return_value=fake_notifier
        ), patch.object(
            transfer_runner, "BaiduStorage", return_value=fake_storage
        ), patch.object(transfer_runner.sys, "exit", side_effect=SystemExit(1)), patch.object(
            transfer_runner, "log_shutdown"
        ) as mock_shutdown:
            with self.assertRaises(SystemExit) as cm:
                transfer_runner.main()

        self.assertEqual(1, cm.exception.code)
        fake_notifier.send_transfer_result.assert_called_once_with(result, config)
        mock_shutdown.assert_called_once_with(success=False)

    def test_main_exits_when_transfer_is_partial(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
            "share_urls": [{"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/AutoTransfer"}],
            "share_configs": [{"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/AutoTransfer"}],
        }
        result = {
            "success": False,
            "partial": True,
            "error": "部分转存成功，成功完成 1/2 个文件，另有 1 个文件转存后重命名失败",
            "rename_failed_files": [
                {"source_path": "old/a.txt", "target_path": "new/a.txt", "error": "boom"}
            ],
        }
        fake_storage = Mock()
        fake_storage.is_valid.return_value = True
        fake_storage.get_quota_info.return_value = None
        fake_storage.transfer_multiple_shares.return_value = result
        fake_notifier = Mock()
        fake_notifier.send_transfer_result.return_value = True
        fake_logger = Mock()

        with patch.object(transfer_runner, "setup_logging"), patch.object(
            transfer_runner, "get_logger", return_value=fake_logger
        ), patch.object(transfer_runner, "log_startup"), patch.object(
            transfer_runner, "log_config_loaded"
        ), patch.object(transfer_runner, "check_network_connectivity"), patch.object(
            transfer_runner, "load_runtime_config", return_value=config
        ), patch.object(
            transfer_runner, "WeChatNotifier", return_value=fake_notifier
        ), patch.object(
            transfer_runner, "BaiduStorage", return_value=fake_storage
        ), patch.object(transfer_runner.sys, "exit", side_effect=SystemExit(1)), patch.object(
            transfer_runner, "log_shutdown"
        ) as mock_shutdown:
            with self.assertRaises(SystemExit) as cm:
                transfer_runner.main()

        self.assertEqual(1, cm.exception.code)
        fake_logger.warning.assert_any_call(
            f"⚠️ 转存部分成功（按失败处理，退出码 1）: {result['error']}"
        )
        fake_logger.warning.assert_any_call("重命名失败文件 (1个):")
        fake_notifier.send_transfer_result.assert_called_once_with(result, config)
        mock_shutdown.assert_called_once_with(success=False)

    def test_failed_transfer_records_round_trip(self):
        records = [
            {
                "share_config": {"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/a"},
                "failed_files": [{"fs_id": 1, "clean_path": "a.txt"}],
                "error": "boom",
                "attempts": 1,
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "failed.json"
            transfer_runner.save_failed_transfer_records(records, path)
            self.assertEqual(records, transfer_runner.load_failed_transfer_records(path))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual([], list(Path(tmpdir).glob("failed.json.*.tmp")))

            transfer_runner.save_failed_transfer_records([], path)
            self.assertFalse(path.exists())

    def test_save_failed_transfer_records_temp_file_is_private_while_writing(self):
        records = [
            {
                "share_config": {"share_url": "https://pan.baidu.com/s/abc12345"},
                "failed_files": [{"fs_id": 1}],
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "failed.json"
            original_dump = transfer_runner.json.dump
            modes = []

            def dump_and_capture_mode(*args, **kwargs):
                modes.extend(
                    tmp_path.stat().st_mode & 0o777
                    for tmp_path in Path(tmpdir).glob("failed.json.*.tmp")
                )
                return original_dump(*args, **kwargs)

            with patch.object(transfer_runner.json, "dump", side_effect=dump_and_capture_mode):
                transfer_runner.save_failed_transfer_records(records, path)

        self.assertEqual([0o600], modes)

    def test_save_failed_transfer_records_cleans_temp_file_on_write_error(self):
        records = [
            {
                "share_config": {"share_url": "https://pan.baidu.com/s/abc12345"},
                "failed_files": [{"fs_id": 1}],
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "failed.json"
            path.write_text("old", encoding="utf-8")
            os.chmod(path, 0o600)

            with patch.object(transfer_runner.json, "dump", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    transfer_runner.save_failed_transfer_records(records, path)

            self.assertEqual("old", path.read_text(encoding="utf-8"))
            self.assertEqual([], list(Path(tmpdir).glob("failed.json.*.tmp")))

    def test_load_failed_transfer_records_raises_for_bad_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "failed.json"
            path.write_text("{bad", encoding="utf-8")

            with self.assertRaises(ValueError):
                transfer_runner.load_failed_transfer_records(path)

            self.assertTrue(path.exists())

    def test_build_failed_transfer_records_collects_retry_config(self):
        result = {
            "results": [
                {
                    "retry_config": {"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/a"},
                    "transfer_failed_files": [{"fs_id": 1, "clean_path": "a.txt"}],
                    "error": "boom",
                }
            ]
        }
        previous = [
            {
                "share_config": {"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/a"},
                "attempts": 2,
            }
        ]

        records = transfer_runner.build_failed_transfer_records(
            result, previous, increment_attempts=True
        )

        self.assertEqual(1, len(records))
        self.assertEqual(transfer_runner.FAILED_RECORD_SCHEMA_VERSION, records[0]["schema_version"])
        self.assertEqual(3, records[0]["attempts"])
        self.assertTrue(records[0]["retryable"])
        self.assertEqual("unknown", records[0]["error_kind"])
        self.assertEqual("a.txt", records[0]["failed_files"][0]["clean_path"])
        self.assertTrue(records[0]["failed_files"][0]["retryable"])

    def test_build_failed_transfer_records_marks_permanent_storage_errors(self):
        result = {
            "results": [
                {
                    "retry_config": {"share_url": "https://pan.baidu.com/s/abc12345"},
                    "transfer_failed_files": [
                        {"fs_id": 1, "clean_path": "a.txt", "error": "error_code: 31066"}
                    ],
                    "error": "error_code: 31066",
                }
            ]
        }

        records = transfer_runner.build_failed_transfer_records(result)

        self.assertEqual(1, len(records))
        self.assertFalse(records[0]["retryable"])
        self.assertEqual("missing_path", records[0]["error_kind"])
        self.assertFalse(records[0]["failed_files"][0]["retryable"])

    def test_build_failed_transfer_records_collects_rename_failures(self):
        result = {
            "results": [
                {
                    "retry_config": {
                        "share_url": "https://pan.baidu.com/s/abc12345",
                        "save_dir": "/save",
                        "regex_pattern": "old",
                        "regex_replace": "new",
                    },
                    "rename_failed_files": [
                        {
                            "source_path": "old/a.txt",
                            "target_path": "new/a.txt",
                            "error": "rename boom",
                        }
                    ],
                    "error": "重命名失败",
                }
            ]
        }

        records = transfer_runner.build_failed_transfer_records(result)

        self.assertEqual(1, len(records))
        self.assertTrue(records[0]["retryable"])
        self.assertEqual("rename_failed", records[0]["error_kind"])
        self.assertEqual("old/a.txt", records[0]["failed_files"][0]["clean_path"])
        self.assertEqual("new/a.txt", records[0]["failed_files"][0]["final_path"])
        self.assertNotIn("need_rename", records[0]["failed_files"][0])
        self.assertEqual("rename boom", records[0]["failed_files"][0]["error"])

    def test_build_failed_transfer_records_minimizes_persisted_file_fields(self):
        result = {
            "results": [
                {
                    "retry_config": {
                        "share_url": "https://pan.baidu.com/s/abc12345",
                        "save_dir": "/a",
                        "cookies": "BDUSS=secret",
                    },
                    "transfer_failed_files": [
                        {
                            "fs_id": 1,
                            "target_dir": "/a",
                            "dir_path": "/a/sub",
                            "path": "legacy.txt",
                            "clean_path": "sub/a.txt",
                            "final_path": "sub/b.txt",
                            "need_rename": True,
                            "error": "boom",
                            "error_code": "4",
                            "attempts": 1,
                            "md5": "secret-md5",
                        }
                    ],
                    "error": "boom",
                    "debug": "drop-me",
                }
            ]
        }

        with patch.object(transfer_runner, "_current_timestamp", return_value=123):
            records = transfer_runner.build_failed_transfer_records(result)

        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual(
            {
                "schema_version",
                "share_config",
                "failed_files",
                "error",
                "error_kind",
                "retryable",
                "temporary",
                "failed_at",
                "last_failed_at",
                "attempts",
            },
            set(record),
        )
        self.assertEqual(
            {"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/a"},
            record["share_config"],
        )
        self.assertEqual(
            {
                "fs_id",
                "dir_path",
                "clean_path",
                "final_path",
                "error",
                "error_kind",
                "retryable",
                "temporary",
                "failed_at",
            },
            set(record["failed_files"][0]),
        )
        self.assertEqual("sub/a.txt", record["failed_files"][0]["clean_path"])
        self.assertEqual(123, record["failed_files"][0]["failed_at"])

    def test_merge_failed_transfer_records_trims_legacy_record_fields(self):
        record = {
            "schema_version": 1,
            "share_config": {
                "share_url": "https://pan.baidu.com/s/abc12345",
                "save_dir": "/a",
                "cookies": "BDUSS=secret",
            },
            "failed_files": [
                {
                    "fs_id": 1,
                    "path": "legacy.txt",
                    "target_dir": "/a",
                    "need_rename": True,
                    "error": "boom",
                    "error_code": "4",
                    "attempts": 1,
                }
            ],
            "error": "boom",
            "attempts": 1,
            "debug": "drop-me",
        }

        with patch.object(transfer_runner, "_current_timestamp", return_value=456):
            merged = transfer_runner.merge_failed_transfer_records([record])

        self.assertEqual(1, len(merged))
        self.assertNotIn("debug", merged[0])
        self.assertEqual(
            {"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/a"},
            merged[0]["share_config"],
        )
        self.assertEqual("legacy.txt", merged[0]["failed_files"][0]["clean_path"])
        self.assertNotIn("target_dir", merged[0]["failed_files"][0])
        self.assertNotIn("need_rename", merged[0]["failed_files"][0])
        self.assertNotIn("error_code", merged[0]["failed_files"][0])
        self.assertNotIn("attempts", merged[0]["failed_files"][0])

    def test_split_failed_transfer_records_by_status(self):
        retryable = {"share_config": {"share_url": "url-1"}, "attempts": 1}
        deferred = {
            "share_config": {"share_url": "url-2"},
            "attempts": 1,
            "retryable": True,
            "next_retry_after": 200,
        }
        permanent = {
            "share_config": {"share_url": "url-3"},
            "attempts": 1,
            "retryable": False,
        }
        exhausted = {
            "share_config": {"share_url": "url-4"},
            "attempts": 3,
            "retryable": True,
        }

        with patch.object(transfer_runner, "MAX_FAILED_TRANSFER_ATTEMPTS", 3):
            result = transfer_runner.split_failed_transfer_records_by_status(
                [retryable, deferred, permanent, exhausted], now=100
            )

        self.assertEqual(([retryable], [deferred], [permanent], [exhausted]), result)

    def _run_main_with_same_key_history_record(self, history_record, expect_exit=False):
        history_config = {"share_url": "https://pan.baidu.com/s/old12345", "save_dir": "/old"}
        history_record = {"share_config": history_config, **history_record}
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "",
            "share_urls": [history_config],
            "share_configs": [history_config],
        }
        fake_storage = Mock()
        fake_storage.is_valid.return_value = True
        fake_storage.get_quota_info.return_value = None
        fake_storage.transfer_multiple_shares.return_value = {
            "success": True,
            "results": [],
            "summary": "完成",
        }
        fake_logger = Mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            failed_path = Path(tmpdir) / "failed.json"
            transfer_runner.save_failed_transfer_records([history_record], failed_path)
            with patch.object(transfer_runner, "FAILED_TRANSFERS_FILE", failed_path), patch.object(
                transfer_runner, "MAX_FAILED_TRANSFER_ATTEMPTS", 3
            ), patch.object(transfer_runner, "setup_logging"), patch.object(
                transfer_runner, "get_logger", return_value=fake_logger
            ), patch.object(transfer_runner, "log_startup"), patch.object(
                transfer_runner, "log_config_loaded"
            ), patch.object(transfer_runner, "check_network_connectivity"), patch.object(
                transfer_runner, "load_runtime_config", return_value=config
            ), patch.object(
                transfer_runner, "BaiduStorage", return_value=fake_storage
            ), patch.object(transfer_runner.sys, "exit", side_effect=SystemExit(1)) as exit_mock, patch.object(
                transfer_runner, "log_shutdown"
            ):
                if expect_exit:
                    with self.assertRaises(SystemExit):
                        transfer_runner.main()
                else:
                    transfer_runner.main()
                    exit_mock.assert_not_called()

            return failed_path.exists(), fake_storage.transfer_multiple_shares.call_count

    def test_main_keeps_deferred_history_record_without_current_retry(self):
        path_exists, call_count = self._run_main_with_same_key_history_record(
            {
                "failed_files": [{"clean_path": "a.txt"}],
                "attempts": 1,
                "retryable": True,
                "next_retry_after": 9999999999,
            },
            expect_exit=True,
        )

        self.assertTrue(path_exists)
        self.assertEqual(0, call_count)

    def test_main_drops_exhausted_history_record_without_current_retry(self):
        path_exists, call_count = self._run_main_with_same_key_history_record(
            {"failed_files": [{"clean_path": "a.txt"}], "attempts": 3, "retryable": True}
        )

        self.assertFalse(path_exists)
        self.assertEqual(0, call_count)

    def test_main_drops_permanent_history_record_without_current_retry(self):
        path_exists, call_count = self._run_main_with_same_key_history_record(
            {"failed_files": [{"clean_path": "a.txt"}], "attempts": 1, "retryable": False}
        )

        self.assertFalse(path_exists)
        self.assertEqual(0, call_count)

    def test_main_retries_history_failures_and_clears_file(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "",
            "share_urls": [{"share_url": "https://pan.baidu.com/s/new12345", "save_dir": "/AutoTransfer"}],
            "share_configs": [{"share_url": "https://pan.baidu.com/s/new12345", "save_dir": "/AutoTransfer"}],
        }
        history_config = {"share_url": "https://pan.baidu.com/s/old12345", "save_dir": "/old"}
        fake_storage = Mock()
        fake_storage.is_valid.return_value = True
        fake_storage.get_quota_info.return_value = None
        fake_storage.transfer_multiple_shares.side_effect = [
            {"success": True, "results": [], "summary": "历史成功"},
            {"success": True, "results": [], "summary": "完成"},
        ]
        fake_logger = Mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            failed_path = Path(tmpdir) / "failed.json"
            transfer_runner.save_failed_transfer_records(
                [{"share_config": history_config, "failed_files": [], "attempts": 1}],
                failed_path,
            )
            with patch.object(transfer_runner, "FAILED_TRANSFERS_FILE", failed_path), patch.object(
                transfer_runner, "setup_logging"
            ), patch.object(transfer_runner, "get_logger", return_value=fake_logger), patch.object(
                transfer_runner, "log_startup"
            ), patch.object(transfer_runner, "log_config_loaded"), patch.object(
                transfer_runner, "check_network_connectivity"
            ), patch.object(transfer_runner, "load_runtime_config", return_value=config), patch.object(
                transfer_runner, "BaiduStorage", return_value=fake_storage
            ), patch.object(transfer_runner, "log_shutdown"):
                transfer_runner.main()

            self.assertFalse(failed_path.exists())

        fake_storage.transfer_multiple_shares.assert_any_call(
            share_configs=[history_config],
            progress_callback=transfer_runner.progress_callback,
        )
        fake_storage.transfer_multiple_shares.assert_any_call(
            share_configs=config["share_configs"],
            progress_callback=transfer_runner.progress_callback,
        )

    def test_main_skips_history_records_at_attempt_limit(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "",
            "share_urls": [{"share_url": "https://pan.baidu.com/s/new12345", "save_dir": "/AutoTransfer"}],
            "share_configs": [{"share_url": "https://pan.baidu.com/s/new12345", "save_dir": "/AutoTransfer"}],
        }
        history_config = {"share_url": "https://pan.baidu.com/s/old12345", "save_dir": "/old"}
        fake_storage = Mock()
        fake_storage.is_valid.return_value = True
        fake_storage.get_quota_info.return_value = None
        fake_storage.transfer_multiple_shares.return_value = {
            "success": True,
            "results": [],
            "summary": "完成",
        }
        fake_logger = Mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            failed_path = Path(tmpdir) / "failed.json"
            transfer_runner.save_failed_transfer_records(
                [
                    {
                        "share_config": history_config,
                        "failed_files": [{"clean_path": "secret.txt", "error": "pwd=1a2B"}],
                        "attempts": 3,
                    }
                ],
                failed_path,
            )
            with patch.object(transfer_runner, "FAILED_TRANSFERS_FILE", failed_path), patch.object(
                transfer_runner, "MAX_FAILED_TRANSFER_ATTEMPTS", 3
            ), patch.object(transfer_runner, "setup_logging"), patch.object(
                transfer_runner, "get_logger", return_value=fake_logger
            ), patch.object(transfer_runner, "log_startup"), patch.object(
                transfer_runner, "log_config_loaded"
            ), patch.object(transfer_runner, "check_network_connectivity"), patch.object(
                transfer_runner, "load_runtime_config", return_value=config
            ), patch.object(
                transfer_runner, "BaiduStorage", return_value=fake_storage
            ), patch.object(transfer_runner, "log_shutdown"):
                transfer_runner.main()

            self.assertFalse(failed_path.exists())

        fake_storage.transfer_multiple_shares.assert_called_once_with(
            share_configs=config["share_configs"],
            progress_callback=transfer_runner.progress_callback,
        )
        self.assertTrue(
            any("历史失败清单" in call.args[0] and "重试上限" in call.args[0]
                for call in fake_logger.warning.call_args_list)
        )
        self.assertFalse(
            any("1a2B" in str(call.args) for call in fake_logger.warning.call_args_list)
        )

    def test_main_skips_permanent_history_records(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "",
            "share_urls": [{"share_url": "https://pan.baidu.com/s/new12345", "save_dir": "/AutoTransfer"}],
            "share_configs": [{"share_url": "https://pan.baidu.com/s/new12345", "save_dir": "/AutoTransfer"}],
        }
        history_config = {"share_url": "https://pan.baidu.com/s/old12345", "save_dir": "/old"}
        fake_storage = Mock()
        fake_storage.is_valid.return_value = True
        fake_storage.get_quota_info.return_value = None
        fake_storage.transfer_multiple_shares.return_value = {
            "success": True,
            "results": [],
            "summary": "完成",
        }
        fake_logger = Mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            failed_path = Path(tmpdir) / "failed.json"
            transfer_runner.save_failed_transfer_records(
                [
                    {
                        "share_config": history_config,
                        "failed_files": [{"clean_path": "bad.txt", "retryable": False}],
                        "attempts": 1,
                        "retryable": False,
                    }
                ],
                failed_path,
            )
            with patch.object(transfer_runner, "FAILED_TRANSFERS_FILE", failed_path), patch.object(
                transfer_runner, "setup_logging"
            ), patch.object(transfer_runner, "get_logger", return_value=fake_logger), patch.object(
                transfer_runner, "log_startup"
            ), patch.object(transfer_runner, "log_config_loaded"), patch.object(
                transfer_runner, "check_network_connectivity"
            ), patch.object(transfer_runner, "load_runtime_config", return_value=config), patch.object(
                transfer_runner, "BaiduStorage", return_value=fake_storage
            ), patch.object(transfer_runner, "log_shutdown"):
                transfer_runner.main()

            self.assertFalse(failed_path.exists())

        fake_storage.transfer_multiple_shares.assert_called_once_with(
            share_configs=config["share_configs"],
            progress_callback=transfer_runner.progress_callback,
        )
        self.assertTrue(
            any("不可自动恢复" in call.args[0] for call in fake_logger.warning.call_args_list)
        )

    def test_main_does_not_reset_attempts_when_history_key_matches_current_config(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "",
            "share_urls": [{"share_url": "https://pan.baidu.com/s/old12345", "save_dir": "/old"}],
            "share_configs": [{"share_url": "https://pan.baidu.com/s/old12345", "save_dir": "/old"}],
        }
        history_config = config["share_configs"][0]
        retry_result = {
            "success": False,
            "partial": True,
            "results": [
                {
                    "success": False,
                    "partial": True,
                    "retry_config": history_config,
                    "transfer_failed_files": [{"clean_path": "a.txt", "error": "boom"}],
                    "error": "boom",
                }
            ],
        }
        fake_storage = Mock()
        fake_storage.is_valid.return_value = True
        fake_storage.get_quota_info.return_value = None
        fake_storage.transfer_multiple_shares.return_value = retry_result
        fake_logger = Mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            failed_path = Path(tmpdir) / "failed.json"
            transfer_runner.save_failed_transfer_records(
                [{"share_config": history_config, "failed_files": [], "attempts": 2}],
                failed_path,
            )
            with patch.object(transfer_runner, "FAILED_TRANSFERS_FILE", failed_path), patch.object(
                transfer_runner, "MAX_FAILED_TRANSFER_ATTEMPTS", 3
            ), patch.object(transfer_runner, "setup_logging"), patch.object(
                transfer_runner, "get_logger", return_value=fake_logger
            ), patch.object(transfer_runner, "log_startup"), patch.object(
                transfer_runner, "log_config_loaded"
            ), patch.object(transfer_runner, "check_network_connectivity"), patch.object(
                transfer_runner, "load_runtime_config", return_value=config
            ), patch.object(
                transfer_runner, "BaiduStorage", return_value=fake_storage
            ), patch.object(transfer_runner, "log_shutdown"):
                transfer_runner.main()

            self.assertFalse(failed_path.exists())

        fake_storage.transfer_multiple_shares.assert_called_once_with(
            share_configs=[history_config],
            progress_callback=transfer_runner.progress_callback,
        )
        self.assertTrue(
            any("避免同一 run 重复转存" in call.args[0] for call in fake_logger.info.call_args_list)
        )

    def test_main_drops_history_record_after_retry_reaches_attempt_limit(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "",
            "share_urls": [{"share_url": "https://pan.baidu.com/s/new12345", "save_dir": "/AutoTransfer"}],
            "share_configs": [{"share_url": "https://pan.baidu.com/s/new12345", "save_dir": "/AutoTransfer"}],
        }
        history_config = {"share_url": "https://pan.baidu.com/s/old12345", "save_dir": "/old"}
        retry_result = {
            "success": False,
            "partial": True,
            "results": [
                {
                    "success": False,
                    "partial": True,
                    "retry_config": history_config,
                    "transfer_failed_files": [{"clean_path": "a.txt", "error": "boom"}],
                    "error": "boom",
                }
            ],
        }
        fake_storage = Mock()
        fake_storage.is_valid.return_value = True
        fake_storage.get_quota_info.return_value = None
        fake_storage.transfer_multiple_shares.side_effect = [
            retry_result,
            {"success": True, "results": [], "summary": "完成"},
        ]
        fake_logger = Mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            failed_path = Path(tmpdir) / "failed.json"
            transfer_runner.save_failed_transfer_records(
                [{"share_config": history_config, "failed_files": [], "attempts": 2}],
                failed_path,
            )
            with patch.object(transfer_runner, "FAILED_TRANSFERS_FILE", failed_path), patch.object(
                transfer_runner, "MAX_FAILED_TRANSFER_ATTEMPTS", 3
            ), patch.object(transfer_runner, "setup_logging"), patch.object(
                transfer_runner, "get_logger", return_value=fake_logger
            ), patch.object(transfer_runner, "log_startup"), patch.object(
                transfer_runner, "log_config_loaded"
            ), patch.object(transfer_runner, "check_network_connectivity"), patch.object(
                transfer_runner, "load_runtime_config", return_value=config
            ), patch.object(
                transfer_runner, "BaiduStorage", return_value=fake_storage
            ), patch.object(transfer_runner, "log_shutdown"):
                transfer_runner.main()

            self.assertFalse(failed_path.exists())

        self.assertEqual(2, fake_storage.transfer_multiple_shares.call_count)
        self.assertTrue(
            any("重试后" in call.args[0] and "重试上限" in call.args[0]
                for call in fake_logger.warning.call_args_list)
        )

    def test_main_saves_current_failed_records(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "",
            "share_urls": [{"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/a"}],
            "save_dir": "/a",
            "share_configs": [{"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/a"}],
        }
        result = {
            "success": False,
            "partial": True,
            "error": "部分成功",
            "results": [
                {
                    "success": False,
                    "partial": True,
                    "retry_config": config["share_configs"][0],
                    "transfer_failed_files": [{"fs_id": 1, "clean_path": "a.txt", "error": "boom"}],
                }
            ],
            "transfer_failed_files": [{"fs_id": 1, "clean_path": "a.txt", "error": "boom"}],
        }
        fake_storage = Mock()
        fake_storage.is_valid.return_value = True
        fake_storage.get_quota_info.return_value = None
        fake_storage.transfer_multiple_shares.return_value = result
        fake_logger = Mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            failed_path = Path(tmpdir) / "failed.json"
            with patch.object(transfer_runner, "FAILED_TRANSFERS_FILE", failed_path), patch.object(
                transfer_runner, "setup_logging"
            ), patch.object(transfer_runner, "get_logger", return_value=fake_logger), patch.object(
                transfer_runner, "log_startup"
            ), patch.object(transfer_runner, "log_config_loaded"), patch.object(
                transfer_runner, "check_network_connectivity"
            ), patch.object(transfer_runner, "load_runtime_config", return_value=config), patch.object(
                transfer_runner, "BaiduStorage", return_value=fake_storage
            ), patch.object(transfer_runner.sys, "exit", side_effect=SystemExit(1)), patch.object(
                transfer_runner, "log_shutdown"
            ):
                with self.assertRaises(SystemExit):
                    transfer_runner.main()

            saved = json.loads(failed_path.read_text(encoding="utf-8"))

        self.assertEqual(config["share_configs"][0], saved["records"][0]["share_config"])
        self.assertEqual("a.txt", saved["records"][0]["failed_files"][0]["clean_path"])


if __name__ == "__main__":
    unittest.main()
