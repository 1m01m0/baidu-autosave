#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit
from typing import Any, Dict, List, Mapping, Optional, Union

from storage_rules import is_safe_relative_target_path

DEFAULT_SAVE_DIR = "/AutoTransfer"
RETRY_SHARE_CONFIG_KEYS = (
    "share_url",
    "pwd",
    "save_dir",
    "regex_pattern",
    "regex_replace",
    "folder_filter",
    "exclude_folder_filter",
)
_SHARE_URL_PATTERN = re.compile(
    r"https://pan\.baidu\.com/s/[A-Za-z0-9_-]+(?:\?[^\s]+)?",
    re.IGNORECASE,
)
_SHARE_PATH_PATTERN = re.compile(r"/s/[A-Za-z0-9_-]+")
_PWD_VALUE_PATTERN = re.compile(r"[A-Za-z0-9]{4}")
_PWD_INLINE_PATTERN = re.compile(
    r"(?:\bpwd\b|密码|提取码)[:：]?\s*([A-Za-z0-9]{4})", re.IGNORECASE
)
_REGEX_BACKREFERENCE_PATTERN = re.compile(r"\\g<[^>]+>|\\[1-9][0-9]*")


def _is_safe_regex_replace_template(value: str) -> bool:
    template = _REGEX_BACKREFERENCE_PATTERN.sub("group", value)
    return is_safe_relative_target_path(template)


def build_retry_share_config(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: config[key]
        for key in RETRY_SHARE_CONFIG_KEYS
        if key in config and config[key] is not None
    }


def retry_share_config_key(config: Dict[str, Any]) -> str:
    return json.dumps(
        build_retry_share_config(config),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _parse_share_url(value: Any, require_full: bool = False) -> Optional[Dict[str, Any]]:
    text = str(value or "").strip()
    if not text:
        return None
    match = _SHARE_URL_PATTERN.fullmatch(text) if require_full else _SHARE_URL_PATTERN.search(text)
    if not match:
        return None

    raw_url = match.group(0)
    parsed = urlsplit(raw_url)
    path = parsed.path.rstrip("/")
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != "pan.baidu.com":
        return None
    if not _SHARE_PATH_PATTERN.fullmatch(path):
        return None

    pwd_values = parse_qs(parsed.query, keep_blank_values=True).get("pwd", [])
    pwd = pwd_values[0] if pwd_values else None
    if pwd not in (None, "") and not _PWD_VALUE_PATTERN.fullmatch(pwd):
        raise ValueError("提取码必须是 4 位字母或数字")

    return {
        "share_url": urlunsplit(("https", "pan.baidu.com", path, "", "")),
        "pwd": pwd or None,
        "match": match,
    }


def _normalize_share_object(item: Dict[str, Any]) -> Dict[str, Any]:
    """归一化对象式 share_urls 项；解析失败时保持原值，
    最终由 ``validate_runtime_config`` 复查并产出明确错误，
    避免在 normalize 阶段吞掉异常导致排错困难。
    """
    share_config = dict(item)
    try:
        parsed = _parse_share_url(share_config.get("share_url"), require_full=True)
    except ValueError:
        # share_url 含非法 pwd 等，留给校验阶段统一报错
        return share_config
    if parsed:
        share_config["share_url"] = parsed["share_url"]
        if parsed.get("pwd") and not share_config.get("pwd"):
            share_config["pwd"] = parsed["pwd"]
    return share_config


def resolve_config_path(config_path: Union[Path, str] = "config.json") -> Path:
    path = Path(config_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def load_json_config(config_path: Union[Path, str] = "config.json") -> Dict[str, Any]:
    path = resolve_config_path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_config_aliases(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = dict(data or {})
    normalized = dict(raw)
    normalized["cookies"] = raw.get("cookies") or raw.get("BAIDU_COOKIES")
    normalized["share_urls"] = raw.get("share_urls") or raw.get("SHARE_URLS")
    normalized["save_dir"] = (
        raw.get("save_dir") or raw.get("SAVE_DIR") or DEFAULT_SAVE_DIR
    )
    normalized["wechat_webhook"] = raw.get("wechat_webhook") or raw.get(
        "WECHAT_WEBHOOK"
    )
    normalized["folder_filter"] = raw.get("folder_filter")
    normalized["exclude_folder_filter"] = raw.get("exclude_folder_filter")
    normalized["regex_pattern"] = raw.get("regex_pattern")
    normalized["regex_replace"] = raw.get("regex_replace")
    return normalized


def load_env_config(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    values = env or os.environ
    return normalize_config_aliases(
        {
            "BAIDU_COOKIES": values.get("BAIDU_COOKIES"),
            "SHARE_URLS": values.get("SHARE_URLS"),
            "SAVE_DIR": values.get("SAVE_DIR", DEFAULT_SAVE_DIR),
            "WECHAT_WEBHOOK": values.get("WECHAT_WEBHOOK"),
        }
    )


def parse_share_links_from_text(text: str, default_save_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    share_configs: List[Dict[str, Any]] = []
    lines = text.strip().split("\n") if text else []

    for line_idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue

        try:
            parsed = _parse_share_url(line)
        except ValueError:
            continue
        if not parsed:
            continue

        match = parsed["match"]
        share_url = parsed["share_url"]
        pwd = parsed.get("pwd")
        save_dir = None

        if not pwd:
            remain = line[match.end() :]
            pwd_match = _PWD_INLINE_PATTERN.search(remain)
            if pwd_match:
                pwd = pwd_match.group(1)

        next_line = lines[line_idx + 1].strip() if line_idx + 1 < len(lines) else ""
        if not _SHARE_URL_PATTERN.search(next_line):
            if not pwd and next_line:
                next_pwd_match = _PWD_INLINE_PATTERN.search(next_line)
                if next_pwd_match:
                    pwd = next_pwd_match.group(1)

        remain_after_url = line[match.end() :].strip()
        if remain_after_url:
            for token in remain_after_url.split():
                if token.startswith("/"):
                    save_dir = token
                    break

        if not save_dir and next_line and not _SHARE_URL_PATTERN.search(next_line):
            for token in next_line.split():
                if token.startswith("/"):
                    save_dir = token
                    break

        if not save_dir:
            save_dir = default_save_dir

        config = {
            "share_url": share_url,
            "pwd": pwd,
            "line_number": line_idx + 1,
        }
        if save_dir:
            config["save_dir"] = save_dir
        share_configs.append(config)

    return share_configs


def _serialize_share_config(
    share_config: Dict[str, Any], default_save_dir: Optional[str] = None
) -> str:
    share_url = str(share_config.get("share_url", "")).strip()
    if not share_url:
        raise ValueError("share_urls 中存在缺少 share_url 的对象配置")

    try:
        parsed = _parse_share_url(share_url, require_full=True)
    except ValueError:
        parsed = None
    pwd = str(share_config.get("pwd") or "").strip()
    if parsed:
        share_url = parsed["share_url"]
        if not pwd and parsed.get("pwd"):
            pwd = parsed["pwd"]
    if pwd:
        share_url = f"{share_url}?pwd={pwd}"

    save_dir = share_config.get("save_dir") or default_save_dir
    if save_dir:
        return f"{share_url} {save_dir}"
    return share_url


def _normalize_share_list_item(
    item: Any, default_save_dir: Optional[str] = None
) -> List[Dict[str, Any]]:
    if isinstance(item, dict):
        return [_normalize_share_object(item)]
    if isinstance(item, str) and item.strip():
        return parse_share_links_from_text(item.strip(), default_save_dir)
    return []


def normalize_share_urls_value(
    share_urls: Any, default_save_dir: Optional[str] = None
) -> Dict[str, Any]:
    if not share_urls:
        return {
            "share_urls": None,
            "share_urls_text": "",
            "share_configs": [],
            "share_count": 0,
            "raw_count": 0,
        }

    if isinstance(share_urls, str):
        text = share_urls.strip()
        if "," in text and "\n" not in text:
            text = "\n".join([item.strip() for item in text.split(",") if item.strip()])
        share_configs = parse_share_links_from_text(text, default_save_dir)
        raw_count = len([line for line in text.splitlines() if line.strip()])
        return {
            "share_urls": text,
            "share_urls_text": text,
            "share_configs": share_configs,
            "share_count": len(share_configs),
            "raw_count": raw_count,
        }

    if isinstance(share_urls, list):
        share_configs: List[Dict[str, Any]] = []
        share_urls_text_parts: List[str] = []
        raw_count = 0
        has_object_item = False

        for item in share_urls:
            if item in (None, "", []):
                continue

            raw_count += 1
            if isinstance(item, dict):
                has_object_item = True
                share_urls_text_parts.append(
                    _serialize_share_config(item, default_save_dir)
                )
            elif isinstance(item, str) and item.strip():
                share_urls_text_parts.append(item.strip())

            share_configs.extend(_normalize_share_list_item(item, default_save_dir))

        if has_object_item:
            normalized_value: Any = share_configs
        else:
            normalized_value = "\n".join(share_urls_text_parts)

        return {
            "share_urls": normalized_value,
            "share_urls_text": "\n".join(share_urls_text_parts),
            "share_configs": share_configs,
            "share_count": len(share_configs),
            "raw_count": raw_count,
        }

    raise TypeError(
        f"share_urls 格式错误，应为列表或字符串，当前类型: {type(share_urls).__name__}"
    )


def apply_global_share_defaults(
    share_configs: List[Dict[str, Any]], config: Dict[str, Any]
) -> List[Dict[str, Any]]:
    applied_configs: List[Dict[str, Any]] = []
    for item in share_configs or []:
        share_config = dict(item)
        if not share_config.get("save_dir"):
            share_config["save_dir"] = config.get("save_dir") or DEFAULT_SAVE_DIR
        if config.get("folder_filter") and "folder_filter" not in share_config:
            share_config["folder_filter"] = config["folder_filter"]
        if config.get("exclude_folder_filter") and "exclude_folder_filter" not in share_config:
            share_config["exclude_folder_filter"] = config["exclude_folder_filter"]
        if config.get("regex_pattern") and "regex_pattern" not in share_config:
            share_config["regex_pattern"] = config["regex_pattern"]
        if config.get("regex_replace") is not None and "regex_replace" not in share_config:
            share_config["regex_replace"] = config["regex_replace"]
        applied_configs.append(share_config)
    return applied_configs


def build_share_urls_text(
    share_urls: Any, default_save_dir: Optional[str] = None
) -> str:
    share_data = normalize_share_urls_value(share_urls, default_save_dir)
    if share_data["share_urls_text"]:
        return share_data["share_urls_text"]
    return ""


def load_runtime_config(config_path: Union[Path, str] = "config.json") -> Dict[str, Any]:
    """读取运行时配置。

    优先级与回退策略：

    1. 优先读取 ``config.json``（或 ``config_path``）；
    2. 仅当文件 **不存在** （``FileNotFoundError``）时回退到环境变量；
       JSON 解析失败、权限错误等其他异常一律向上抛出，避免静默用环境变量
       覆盖一个用户期望存在但损坏的配置。
    3. 任何来源都必须包含 ``cookies`` 与 ``share_urls``，否则抛 ``ValueError``。
    4. 最后通过 ``validate_runtime_config`` 做语义校验，校验失败抛 ``ValueError``。
    """
    path = resolve_config_path(config_path)

    try:
        config = normalize_config_aliases(load_json_config(path))
        if not config.get("cookies"):
            raise ValueError("配置文件缺少 cookies (cookies/BAIDU_COOKIES)")
        if not config.get("share_urls"):
            raise ValueError("配置文件缺少 share_urls (share_urls/SHARE_URLS)")
        config["config_source"] = "file"
    except FileNotFoundError as exc:
        config = load_env_config()
        if not config.get("cookies"):
            raise ValueError("BAIDU_COOKIES 环境变量未设置")
        if not config.get("share_urls"):
            raise ValueError("SHARE_URLS 环境变量未设置")
        config["config_source"] = "env"
        config["config_load_warning"] = str(exc)

    validation = validate_runtime_config(config)
    if validation["errors"]:
        raise ValueError("配置校验失败: " + "; ".join(validation["errors"]))

    config = validation["config"]
    config["config_path"] = str(path)
    return config


def _validate_regex_filter_config(
    value: Any,
    field_name: str,
    label: str,
    info_messages: List[str],
    errors: List[str],
) -> None:
    if not value:
        info_messages.append(f"ℹ️  未设置{label} (可选)")
        return

    if isinstance(value, str):
        try:
            re.compile(value)
        except (re.error, TypeError) as exc:
            errors.append(f"❌ {label}错误: {exc}")
        else:
            info_messages.append(f"✅ {label}有效: {value}")
        return

    if isinstance(value, list):
        has_error = False
        for idx, pattern in enumerate(value, 1):
            if not isinstance(pattern, str):
                errors.append(
                    f"❌ 第 {idx} 个{label}类型错误，应为字符串，"
                    f"当前类型: {type(pattern).__name__}"
                )
                has_error = True
                continue
            try:
                re.compile(pattern)
            except (re.error, TypeError) as exc:
                errors.append(f"❌ 第 {idx} 个{label}错误: {exc}")
                has_error = True
        if not has_error:
            info_messages.append(f"✅ {label}有效 (共 {len(value)} 个)")
        return

    errors.append(
        f"❌ {field_name} 类型错误，应为字符串或列表，"
        f"当前类型: {type(value).__name__}"
    )


def _validate_regex_pattern_config(
    value: Any,
    field_name: str,
    label: str,
    info_messages: List[str],
    errors: List[str],
    optional_message: Optional[str] = None,
) -> bool:
    if not value:
        if optional_message:
            info_messages.append(optional_message)
        return False
    if not isinstance(value, str):
        errors.append(
            f"❌ {field_name} 类型错误，应为字符串，当前类型: {type(value).__name__}"
        )
        return False
    try:
        re.compile(value)
    except (re.error, TypeError) as exc:
        errors.append(f"❌ {label}错误: {exc}")
        return False
    info_messages.append(f"✅ {label}有效: {value}")
    return True


def _validate_regex_replace_config(
    value: Any,
    field_name: str,
    label: str,
    regex_pattern: Any,
    warnings: List[str],
    errors: List[str],
    info_messages: List[str],
) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str):
        errors.append(
            f"❌ {field_name} 类型错误，应为字符串，当前类型: {type(value).__name__}"
        )
        return
    if not _is_safe_regex_replace_template(value):
        errors.append(f"❌ {label}不能生成绝对路径或包含上级目录跳转")
        return
    if re.search(r"\$[1-9]\d*", value):
        warnings.append(f"⚠️  {label}使用 Python re.sub() 语法，请用 \\1、\\2 表示分组引用")
    if isinstance(regex_pattern, str):
        try:
            sample_result = re.sub(regex_pattern, value, "test_file.mp4")
        except Exception as exc:
            warnings.append(f"⚠️  {label}可能有问题: {exc}")
            return
        if sample_result != "test_file.mp4" and not is_safe_relative_target_path(sample_result):
            errors.append(f"❌ {label}不能生成绝对路径或包含上级目录跳转")
            return
    info_messages.append(f"✅ {label}有效: {value}")


def _validate_share_object_config(
    item: Dict[str, Any],
    idx: int,
    warnings: List[str],
    errors: List[str],
) -> None:
    prefix = f"第 {idx} 个链接"
    share_url = item.get("share_url")
    if not isinstance(share_url, str) or not share_url.strip():
        errors.append(f"❌ {prefix}缺少 share_url 字段或类型错误")
    else:
        try:
            parsed_share_url = _parse_share_url(share_url, require_full=True)
        except ValueError as exc:
            errors.append(f"❌ {prefix}格式不正确: {exc}")
        else:
            if not parsed_share_url:
                errors.append(f"❌ {prefix}格式不正确: {share_url[:50]}...")

    pwd = item.get("pwd")
    if pwd not in (None, ""):
        if not isinstance(pwd, str):
            errors.append(f"❌ {prefix}的 pwd 必须是字符串，当前类型: {type(pwd).__name__}")
        elif not _PWD_VALUE_PATTERN.fullmatch(pwd):
            errors.append(f"❌ {prefix}的 pwd 必须是 4 位字母或数字")

    save_dir = item.get("save_dir")
    if save_dir not in (None, ""):
        if not isinstance(save_dir, str):
            errors.append(
                f"❌ {prefix}的 save_dir 必须是字符串，当前类型: {type(save_dir).__name__}"
            )
        elif not save_dir.startswith("/"):
            warnings.append(f"⚠️  {prefix}保存目录不以 / 开头，可能导致问题: {save_dir}")

    temp_info: List[str] = []
    regex_pattern = item.get("regex_pattern")
    _validate_regex_pattern_config(
        regex_pattern,
        f"{prefix}的 regex_pattern",
        f"{prefix}正则过滤规则",
        temp_info,
        errors,
    )
    _validate_regex_replace_config(
        item.get("regex_replace"),
        f"{prefix}的 regex_replace",
        f"{prefix}正则替换规则",
        regex_pattern,
        warnings,
        errors,
        temp_info,
    )
    for field_name, label in (
        ("folder_filter", f"{prefix}文件夹过滤规则"),
        ("exclude_folder_filter", f"{prefix}排除文件夹规则"),
    ):
        if field_name in item and item.get(field_name):
            _validate_regex_filter_config(
                item.get(field_name), f"{prefix}的 {field_name}", label, temp_info, errors
            )


def validate_runtime_config(config: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_config_aliases(config)
    errors: List[str] = []
    warnings: List[str] = []
    info_messages: List[str] = []

    cookies = normalized.get("cookies")
    cookie_errors: List[str] = []
    if not cookies:
        cookie_errors.append("❌ 缺少 cookies 字段 (cookies 或 BAIDU_COOKIES)")
    elif not isinstance(cookies, str):
        cookie_errors.append(
            f"❌ cookies 必须是字符串，当前类型: {type(cookies).__name__}"
        )
    else:
        if "BDUSS" not in cookies:
            cookie_errors.append("❌ Cookies 中缺少 BDUSS")
        if "STOKEN" not in cookies:
            cookie_errors.append("❌ Cookies 中缺少 STOKEN")
        if not cookie_errors:
            cookie_count = len([item for item in cookies.split(";") if "=" in item])
            info_messages.append(f"✅ Cookies 有效 (包含 {cookie_count} 个值)")
    errors.extend(cookie_errors)

    share_urls = normalized.get("share_urls")
    try:
        share_data = normalize_share_urls_value(share_urls, normalized.get("save_dir"))
    except (TypeError, ValueError) as exc:
        share_data = {
            "share_urls": share_urls,
            "share_urls_text": "",
            "share_configs": [],
            "share_count": 0,
            "raw_count": 0,
        }
        errors.append(f"❌ {exc}")
    else:
        if not share_urls:
            errors.append("❌ 缺少 share_urls 字段 (share_urls 或 SHARE_URLS)")
        elif share_data["raw_count"] == 0:
            errors.append("❌ share_urls 为空")
        elif share_data["share_count"] == 0:
            errors.append("❌ 没有找到有效的分享链接")
        else:
            info_messages.append(
                f"✅ 分享链接有效 (共 {share_data['raw_count']} 项，其中 {share_data['share_count']} 个有效)"
            )

    if isinstance(share_urls, list):
        for idx, item in enumerate(share_urls, 1):
            if isinstance(item, dict):
                _validate_share_object_config(item, idx, warnings, errors)
            elif isinstance(item, str):
                if item.strip() and not _SHARE_URL_PATTERN.search(item):
                    warnings.append(
                        f"⚠️  第 {idx} 个链接格式可能不正确: {item.strip()[:50]}..."
                    )
            elif item not in (None, "", []):
                errors.append(
                    f"❌ 第 {idx} 个链接类型错误，应为字符串或对象，当前类型: {type(item).__name__}"
                )

    save_dir = normalized.get("save_dir") or DEFAULT_SAVE_DIR
    if not isinstance(save_dir, str):
        errors.append(
            f"❌ save_dir 必须是字符串，当前类型: {type(save_dir).__name__}"
        )
    else:
        if not save_dir:
            warnings.append("⚠️  未指定保存目录，将使用默认值: /AutoTransfer")
        elif not save_dir.startswith("/"):
            warnings.append(f"⚠️  保存目录不以 / 开头，可能导致问题: {save_dir}")
        info_messages.append(f"✅ 保存目录有效: {save_dir}")

    webhook = normalized.get("wechat_webhook")
    if not webhook:
        info_messages.append("ℹ️  未配置企业微信通知 (可选，不影响转存)")
    elif not isinstance(webhook, str):
        errors.append(
            f"❌ wechat_webhook 必须是字符串，当前类型: {type(webhook).__name__}"
        )
    else:
        if "qyapi.weixin.qq.com" not in webhook:
            warnings.append("⚠️  企业微信 Webhook 格式可能不正确")
        else:
            info_messages.append("✅ 企业微信 Webhook 有效")

    regex_pattern = normalized.get("regex_pattern")
    regex_replace = normalized.get("regex_replace")
    _validate_regex_pattern_config(
        regex_pattern,
        "regex_pattern",
        "正则过滤规则",
        info_messages,
        errors,
        optional_message="ℹ️  未设置文件过滤规则 (可选)",
    )
    _validate_regex_replace_config(
        regex_replace,
        "regex_replace",
        "正则替换规则",
        regex_pattern,
        warnings,
        errors,
        info_messages,
    )

    _validate_regex_filter_config(
        normalized.get("folder_filter"),
        "folder_filter",
        "文件夹过滤规则",
        info_messages,
        errors,
    )
    _validate_regex_filter_config(
        normalized.get("exclude_folder_filter"),
        "exclude_folder_filter",
        "排除文件夹规则",
        info_messages,
        errors,
    )

    normalized.update(share_data)
    normalized["share_configs"] = apply_global_share_defaults(
        share_data["share_configs"], normalized
    )
    normalized["share_count"] = len(normalized["share_configs"])

    return {
        "config": normalized,
        "errors": errors,
        "warnings": warnings,
        "info": info_messages,
    }
