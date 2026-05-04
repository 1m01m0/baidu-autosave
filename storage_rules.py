#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re


_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
REGEX_FILTER_UNMATCHED = "unmatched"
REGEX_FILTER_UNSAFE_REPLACE = "unsafe_replace"


def is_safe_relative_target_path(path):
    try:
        normalized = str(path).replace("\\", "/")
    except Exception:
        return False
    if "\x00" in normalized:
        return False
    normalized = normalized.strip()
    if not normalized or normalized.startswith("/"):
        return False
    if _WINDOWS_DRIVE_PATTERN.match(normalized):
        return False
    return all(part not in ("", ".", "..") for part in normalized.split("/"))


def apply_regex_rules_detail(file_path, regex_pattern=None, regex_replace=None):
    if not regex_pattern:
        return True, file_path, None

    try:
        match = re.search(regex_pattern, file_path)
        if not match:
            return False, file_path, REGEX_FILTER_UNMATCHED

        if regex_replace and regex_replace.strip():
            new_path = re.sub(regex_pattern, regex_replace, file_path)
            if new_path != file_path:
                if not is_safe_relative_target_path(new_path):
                    return False, file_path, REGEX_FILTER_UNSAFE_REPLACE
                return True, new_path, None

        return True, file_path, None
    except re.error:
        return True, file_path, None
    except Exception:
        return True, file_path, None


def apply_regex_rules(file_path, regex_pattern=None, regex_replace=None):
    should_transfer, final_path, _ = apply_regex_rules_detail(
        file_path, regex_pattern, regex_replace
    )
    return should_transfer, final_path


def _matches_folder_filter(folder_name, folder_filter):
    if isinstance(folder_filter, list):
        return any(re.search(pattern, folder_name) for pattern in folder_filter)
    if isinstance(folder_filter, str):
        return bool(re.search(folder_filter, folder_name))
    return None


def should_include_folder(folder_name, folder_filter=None):
    if not folder_filter:
        return True

    try:
        matched = _matches_folder_filter(folder_name, folder_filter)
        return True if matched is None else matched
    except re.error:
        return True
    except Exception:
        return True


def should_exclude_folder(folder_name, exclude_folder_filter=None):
    if not exclude_folder_filter:
        return False

    try:
        return bool(_matches_folder_filter(folder_name, exclude_folder_filter))
    except re.error:
        return False
    except Exception:
        return False


def extract_file_info(file_dict):
    try:
        if isinstance(file_dict, dict):
            server_filename = file_dict.get("server_filename", "")
            if not server_filename and file_dict.get("path"):
                server_filename = file_dict["path"].split("/")[-1]

            return {
                "server_filename": server_filename,
                "fs_id": file_dict.get("fs_id", ""),
                "path": file_dict.get("path", ""),
                "size": file_dict.get("size", 0),
                "isdir": file_dict.get("isdir", 0),
                "md5": file_dict.get("md5", None),
            }
        return None
    except Exception:
        return None
