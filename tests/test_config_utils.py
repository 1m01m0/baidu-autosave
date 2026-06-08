import json
import os
import tempfile
import unittest
from unittest.mock import patch

from config_utils import (
    apply_global_share_defaults,
    load_runtime_config,
    normalize_share_urls_value,
    parse_share_links_from_text,
    retry_share_config_key,
    validate_runtime_config,
)


class ParseShareLinksTests(unittest.TestCase):
    def test_parse_share_links_from_text_supports_inline_and_next_line_values(self):
        text = """
https://pan.baidu.com/s/abc12345?pwd=1a2B /Movies
https://pan.baidu.com/s/xyz_789
提取码: 9Z8y /Shows
        """.strip()

        result = parse_share_links_from_text(text, "/Default")

        self.assertEqual(2, len(result))
        self.assertEqual(
            {
                "share_url": "https://pan.baidu.com/s/abc12345",
                "pwd": "1a2B",
                "line_number": 1,
                "save_dir": "/Movies",
            },
            result[0],
        )
        self.assertEqual("https://pan.baidu.com/s/xyz_789", result[1]["share_url"])
        self.assertEqual("9Z8y", result[1]["pwd"])
        self.assertEqual("/Shows", result[1]["save_dir"])

    def test_parse_share_links_from_text_extracts_pwd_from_query_params(self):
        result = parse_share_links_from_text(
            "https://pan.baidu.com/s/abc12345?foo=bar&pwd=7X6z /Docs"
        )

        self.assertEqual("https://pan.baidu.com/s/abc12345", result[0]["share_url"])
        self.assertEqual("7X6z", result[0]["pwd"])
        self.assertEqual("/Docs", result[0]["save_dir"])

    def test_parse_share_links_from_text_skips_invalid_pwd_query(self):
        self.assertEqual(
            [], parse_share_links_from_text("https://pan.baidu.com/s/abc12345?pwd=abcde")
        )


class NormalizeShareUrlsValueTests(unittest.TestCase):
    def test_normalize_share_urls_value_supports_comma_separated_string(self):
        share_urls = (
            "https://pan.baidu.com/s/abc12345?pwd=1a2B,https://pan.baidu.com/s/xyz_789 /Shows"
        )

        result = normalize_share_urls_value(share_urls, "/Default")

        self.assertEqual(2, result["raw_count"])
        self.assertEqual(2, result["share_count"])
        self.assertEqual(2, len(result["share_configs"]))
        self.assertEqual("/Shows", result["share_configs"][1]["save_dir"])

    def test_normalize_share_urls_value_supports_mixed_list(self):
        share_urls = [
            {"share_url": "https://pan.baidu.com/s/abc12345", "pwd": "1a2B"},
            "https://pan.baidu.com/s/xyz_789 /Shows",
            None,
            "",
        ]

        result = normalize_share_urls_value(share_urls, "/Default")

        self.assertIsInstance(result["share_urls"], list)
        self.assertEqual(2, result["raw_count"])
        self.assertEqual(2, result["share_count"])
        self.assertNotIn("save_dir", result["share_configs"][0])
        self.assertEqual("/Shows", result["share_configs"][1]["save_dir"])

    def test_normalize_share_urls_value_canonicalizes_object_share_url_query_pwd(self):
        result = normalize_share_urls_value(
            [{"share_url": "https://pan.baidu.com/s/abc12345?pwd=1a2B"}], "/Default"
        )

        self.assertEqual(
            "https://pan.baidu.com/s/abc12345", result["share_configs"][0]["share_url"]
        )
        self.assertEqual("1a2B", result["share_configs"][0]["pwd"])


class RetryShareConfigKeyTests(unittest.TestCase):
    def test_retry_share_config_key_canonicalizes_pwd_in_share_url(self):
        key_from_url = retry_share_config_key(
            {"share_url": "https://pan.baidu.com/s/abc12345?pwd=1a2B", "save_dir": "/Auto"}
        )
        key_from_field = retry_share_config_key(
            {
                "share_url": "https://pan.baidu.com/s/abc12345",
                "pwd": "1a2B",
                "save_dir": "/Auto",
            }
        )

        self.assertEqual(key_from_field, key_from_url)

    def test_retry_share_config_key_keeps_distinct_save_dirs_distinct(self):
        self.assertNotEqual(
            retry_share_config_key(
                {"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/A"}
            ),
            retry_share_config_key(
                {"share_url": "https://pan.baidu.com/s/abc12345", "save_dir": "/B"}
            ),
        )


class ApplyGlobalShareDefaultsTests(unittest.TestCase):
    def test_apply_global_share_defaults_propagates_exclude_filter_and_preserves_override(self):
        share_configs = [
            {"share_url": "https://pan.baidu.com/s/abc12345"},
            {
                "share_url": "https://pan.baidu.com/s/xyz_789",
                "exclude_folder_filter": r"^dist$",
            },
        ]

        result = apply_global_share_defaults(
            share_configs,
            {
                "save_dir": "/Auto",
                "exclude_folder_filter": r"^node_modules$",
            },
        )

        self.assertEqual(r"^node_modules$", result[0]["exclude_folder_filter"])
        self.assertEqual(r"^dist$", result[1]["exclude_folder_filter"])


class ValidateRuntimeConfigTests(unittest.TestCase):
    def test_validate_runtime_config_accepts_alias_fields(self):
        config = {
            "BAIDU_COOKIES": "BDUSS=foo; STOKEN=bar",
            "SHARE_URLS": "https://pan.baidu.com/s/abc12345",
            "SAVE_DIR": "/Auto",
        }

        result = validate_runtime_config(config)

        self.assertEqual([], result["errors"])
        self.assertEqual(1, result["config"]["share_count"])
        self.assertEqual("/Auto", result["config"]["save_dir"])
        self.assertTrue(any("Cookies 有效" in msg for msg in result["info"]))

    def test_validate_runtime_config_reports_missing_stoken_and_bad_regex(self):
        config = {
            "cookies": "BDUSS=foo",
            "share_urls": "https://pan.baidu.com/s/abc12345",
            "regex_pattern": "[",
        }

        result = validate_runtime_config(config)

        self.assertTrue(any("STOKEN" in msg for msg in result["errors"]))
        self.assertTrue(any("正则过滤规则错误" in msg for msg in result["errors"]))

    def test_validate_runtime_config_reports_bad_exclude_folder_filter(self):
        config = {
            "cookies": "BDUSS=foo; STOKEN=bar",
            "share_urls": "https://pan.baidu.com/s/abc12345",
            "exclude_folder_filter": "[",
        }

        result = validate_runtime_config(config)

        self.assertTrue(any("排除文件夹规则错误" in msg for msg in result["errors"]))

    def test_validate_runtime_config_reports_non_string_filter_item(self):
        config = {
            "cookies": "BDUSS=foo; STOKEN=bar",
            "share_urls": "https://pan.baidu.com/s/abc12345",
            "exclude_folder_filter": [r"^dist$", 123],
        }

        result = validate_runtime_config(config)

        self.assertTrue(any("排除文件夹规则类型错误" in msg for msg in result["errors"]))

    def test_validate_runtime_config_reports_non_string_regex_fields(self):
        config = {
            "cookies": "BDUSS=foo; STOKEN=bar",
            "share_urls": "https://pan.baidu.com/s/abc12345",
            "regex_pattern": [".*"],
            "regex_replace": 123,
        }

        result = validate_runtime_config(config)

        self.assertTrue(any("regex_pattern 类型错误" in msg for msg in result["errors"]))
        self.assertTrue(any("regex_replace 类型错误" in msg for msg in result["errors"]))

    def test_validate_runtime_config_reports_bad_share_object(self):
        config = {
            "cookies": "BDUSS=foo; STOKEN=bar",
            "share_urls": [
                {
                    "pwd": 123,
                    "save_dir": 456,
                    "regex_pattern": [".*"],
                    "regex_replace": 789,
                    "folder_filter": ["ok", 1],
                    "exclude_folder_filter": "[",
                }
            ],
        }

        result = validate_runtime_config(config)

        self.assertTrue(any("缺少 share_url" in msg for msg in result["errors"]))
        self.assertTrue(any("pwd 必须是字符串" in msg for msg in result["errors"]))
        self.assertTrue(any("save_dir 必须是字符串" in msg for msg in result["errors"]))
        self.assertTrue(any("regex_pattern 类型错误" in msg for msg in result["errors"]))
        self.assertTrue(any("regex_replace 类型错误" in msg for msg in result["errors"]))
        self.assertTrue(any("文件夹过滤规则类型错误" in msg for msg in result["errors"]))
        self.assertTrue(any("排除文件夹规则错误" in msg for msg in result["errors"]))

    def test_validate_runtime_config_rejects_dirty_object_share_url_and_bad_pwd(self):
        result = validate_runtime_config(
            {
                "cookies": "BDUSS=foo; STOKEN=bar",
                "share_urls": [
                    {"share_url": "prefix https://pan.baidu.com/s/abc12345"},
                    {"share_url": "https://pan.baidu.com/s/xyz_789?pwd=abcde"},
                ],
            }
        )

        self.assertTrue(any("格式不正确" in msg for msg in result["errors"]))
        self.assertTrue(any("提取码必须是 4 位" in msg for msg in result["errors"]))

    def test_validate_runtime_config_warns_about_dollar_regex_replace_groups(self):
        result = validate_runtime_config(
            {
                "cookies": "BDUSS=foo; STOKEN=bar",
                "share_urls": "https://pan.baidu.com/s/abc12345",
                "regex_pattern": r"([0-9]+)-(.+)\.pdf$",
                "regex_replace": "$1_$2.pdf",
            }
        )

        self.assertTrue(any("Python re.sub()" in msg for msg in result["warnings"]))

    def test_validate_runtime_config_rejects_unsafe_regex_replace(self):
        for regex_replace in ("../\\1", "/\\1", "a/../../evil.txt", r"C:\\evil.txt"):
            with self.subTest(regex_replace=regex_replace):
                result = validate_runtime_config(
                    {
                        "cookies": "BDUSS=foo; STOKEN=bar",
                        "share_urls": "https://pan.baidu.com/s/abc12345",
                        "regex_pattern": r"(.+)\.txt$",
                        "regex_replace": regex_replace,
                    }
                )

                self.assertTrue(any("不能生成绝对路径" in msg for msg in result["errors"]))

    def test_validate_runtime_config_allows_safe_regex_replace_backreferences(self):
        result = validate_runtime_config(
            {
                "cookies": "BDUSS=foo; STOKEN=bar",
                "share_urls": [
                    {
                        "share_url": "https://pan.baidu.com/s/abc12345",
                        "regex_pattern": r"(.+)\.txt$",
                        "regex_replace": r"new/\1.txt",
                    }
                ],
            }
        )

        self.assertEqual([], result["errors"])


class LoadRuntimeConfigTests(unittest.TestCase):
    def test_load_runtime_config_prefers_file_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "cookies": "BDUSS=foo; STOKEN=bar",
                        "share_urls": "https://pan.baidu.com/s/abc12345",
                        "save_dir": "/FromFile",
                    },
                    fh,
                )

            result = load_runtime_config(config_path)

        self.assertEqual("file", result["config_source"])
        self.assertEqual(config_path, result["config_path"])
        self.assertEqual(1, result["share_count"])
        self.assertNotIn("config_load_warning", result)

    def test_load_runtime_config_falls_back_to_env_only_when_file_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "missing.json")
            env = {
                "BAIDU_COOKIES": "BDUSS=foo; STOKEN=bar",
                "SHARE_URLS": "https://pan.baidu.com/s/xyz_789 /FromEnv",
                "SAVE_DIR": "/EnvDefault",
            }
            with patch.dict(os.environ, env, clear=False):
                result = load_runtime_config(config_path)

        self.assertEqual("env", result["config_source"])
        self.assertEqual(config_path, result["config_path"])
        self.assertIn("配置文件不存在", result["config_load_warning"])
        self.assertEqual(1, result["share_count"])
        self.assertEqual("/FromEnv", result["share_configs"][0]["save_dir"])

    def test_load_runtime_config_raises_when_config_file_contains_invalid_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write('{"cookies": "BDUSS=foo; STOKEN=bar", invalid')

            with patch.dict(
                os.environ,
                {
                    "BAIDU_COOKIES": "BDUSS=env; STOKEN=env",
                    "SHARE_URLS": "https://pan.baidu.com/s/xyz_789",
                },
                clear=False,
            ):
                with self.assertRaises(json.JSONDecodeError):
                    load_runtime_config(config_path)

    def test_load_runtime_config_raises_when_existing_file_misses_required_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"cookies": "BDUSS=foo; STOKEN=bar"}, fh)

            with patch.dict(
                os.environ,
                {
                    "BAIDU_COOKIES": "BDUSS=env; STOKEN=env",
                    "SHARE_URLS": "https://pan.baidu.com/s/xyz_789",
                },
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "配置文件缺少 share_urls"):
                    load_runtime_config(config_path)

    def test_load_runtime_config_raises_for_invalid_runtime_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "cookies": "BDUSS=foo; STOKEN=bar",
                        "share_urls": "https://pan.baidu.com/s/abc12345",
                        "regex_pattern": [".*"],
                    },
                    fh,
                )

            with self.assertRaisesRegex(ValueError, "配置校验失败"):
                load_runtime_config(config_path)


if __name__ == "__main__":
    unittest.main()
