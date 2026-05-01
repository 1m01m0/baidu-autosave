import json
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
            "share_configs": [{"share_url": "https://pan.baidu.com/s/abc12345"}],
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

    def test_main_exits_when_transfer_fails(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
            "share_configs": [{"share_url": "https://pan.baidu.com/s/abc12345"}],
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
            "share_configs": [{"share_url": "https://pan.baidu.com/s/abc12345"}],
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

            transfer_runner.save_failed_transfer_records([], path)
            self.assertFalse(path.exists())

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
        self.assertEqual(3, records[0]["attempts"])
        self.assertEqual("a.txt", records[0]["failed_files"][0]["clean_path"])

    def test_main_retries_history_failures_and_clears_file(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "",
            "share_configs": [{"share_url": "https://pan.baidu.com/s/new12345"}],
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

    def test_main_saves_current_failed_records(self):
        config = {
            "config_source": "file",
            "config_path": "config.json",
            "cookies": "BDUSS=foo; STOKEN=bar",
            "wechat_webhook": "",
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
