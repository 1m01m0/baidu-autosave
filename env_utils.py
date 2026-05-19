#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境变量解析的小工具，供 storage / transfer_runner 共享。"""

import os
from typing import Optional


def read_non_negative_float_env(name: str, default: float) -> float:
    """读取非负浮点数环境变量，无效或缺失时返回 default。"""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def read_positive_int_env(name: str, default: int) -> int:
    """读取正整数环境变量，无效或缺失时返回 default。"""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def read_non_negative_int_env(name: str, default: int) -> int:
    """读取非负整数环境变量；无效（负数 / 非整数）或缺失时返回 default。

    与 ``read_positive_int_env`` 的区别：值 ``0`` 是合法返回值，
    例如 ``TRANSFERSHARE_PCS_POOL_MAXSIZE=0`` 用于显式禁用连接池调优。
    """
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def read_optional_str_env(name: str) -> Optional[str]:
    """读取字符串环境变量，空白视为未设置。"""
    raw_value = os.getenv(name)
    if raw_value is None:
        return None
    stripped = raw_value.strip()
    return stripped or None
