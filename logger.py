#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目统一日志入口。

设计要点：

- 仍维护一个 ``_logger`` 单例，便于 ``get_logger()`` 在 import 期就拿到 logger。
- ``setup_logging`` 显式接收 ``name`` 参数，避免把项目 logger 与 root logger 混淆。
- 新增 ``TRANSFERSHARE_LOG_LEVEL`` 环境变量作为缺省级别，调试时无需改代码。
- ``_close_handlers`` 只清理本 logger 自己注册的 handler，不会触碰 root logger。
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# 日志级别定义
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# 日志格式 - 简化格式避免行截断问题
LOG_FORMAT = "[%(asctime)s] [%(levelname)-8s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOGGER_NAME = "transfershare"
LOG_LEVEL_ENV_VAR = "TRANSFERSHARE_LOG_LEVEL"

# 全局日志器（保留语义供 tests/test_logger.py 直接读写）
_logger: Optional[logging.Logger] = None


def _resolve_default_level(level: Optional[str]) -> int:
    """优先使用显式参数；其次读环境变量；都没有时回落到 INFO。"""
    if level:
        return LOG_LEVELS.get(level.upper(), logging.INFO)
    env_level = os.getenv(LOG_LEVEL_ENV_VAR)
    if env_level:
        return LOG_LEVELS.get(env_level.upper(), logging.INFO)
    return logging.INFO


def _close_handlers(target_logger: logging.Logger) -> None:
    for handler in list(target_logger.handlers):
        target_logger.removeHandler(handler)
        handler.close()


def _add_console_handler(
    target_logger: logging.Logger, level: int, formatter: logging.Formatter
) -> None:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    target_logger.addHandler(console_handler)


def _add_file_handler(
    target_logger: logging.Logger, log_file: str, formatter: logging.Formatter
) -> None:
    try:
        log_path = Path(log_file).parent
        log_path.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        target_logger.addHandler(file_handler)
    except Exception as exc:
        target_logger.warning(f"无法创建日志文件 {log_file}: {exc}")


def _get_logger(
    name: str = DEFAULT_LOGGER_NAME,
    level: Optional[str] = None,
    log_file: Optional[str] = None,
    console_output: bool = True,
    reconfigure: bool = False,
) -> logging.Logger:
    """获取或创建项目 logger。

    Args:
        name: logger 名称，默认 ``transfershare``
        level: 显式日志级别字符串；为空时按环境变量回落
        log_file: 可选的日志文件路径
        console_output: 是否输出到控制台
        reconfigure: True 时按参数重置已有 logger 的 handler

    Returns:
        配置好的 logger 实例
    """
    global _logger

    if _logger is not None and not reconfigure:
        return _logger

    if _logger is None:
        _logger = logging.getLogger(name)
    else:
        _close_handlers(_logger)

    log_level = _resolve_default_level(level)
    _logger.setLevel(log_level)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    if console_output:
        _add_console_handler(_logger, log_level, formatter)

    if log_file:
        _add_file_handler(_logger, log_file, formatter)

    _logger.propagate = False
    return _logger


def get_logger(name: str = DEFAULT_LOGGER_NAME) -> logging.Logger:
    """获取 logger 实例（首次调用时初始化）。"""
    global _logger
    if _logger is None:
        _logger = _get_logger(name)
    return _logger


def setup_logging(
    level: Optional[str] = None,
    log_file: Optional[str] = None,
    console_output: bool = True,
    name: str = DEFAULT_LOGGER_NAME,
) -> logging.Logger:
    """显式配置项目 logger，可重复调用。"""
    return _get_logger(name, level, log_file, console_output, reconfigure=True)


def log_startup(version: Optional[str] = None) -> None:
    """记录启动信息。"""
    target = get_logger()
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    target.info("=" * 50)
    target.info("百度网盘自动转存任务开始")
    if version:
        target.info(f"版本: {version}")
    target.info(f"执行时间: {timestamp}")
    target.info("=" * 50)


def log_shutdown(success: bool = True) -> None:
    """记录关闭信息。"""
    target = get_logger()
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    status = "成功完成" if success else "异常中断"
    target.info("=" * 50)
    target.info(f"任务{status}")
    target.info(f"结束时间: {timestamp}")
    target.info("=" * 50)


def log_config_loaded(config: Optional[dict] = None) -> None:
    """记录配置加载信息。"""
    target = get_logger()
    target.info("配置信息:")
    if not config:
        return

    if config.get("cookies"):
        target.debug("✓ Cookies 已配置")
    if config.get("share_urls"):
        share_urls = config["share_urls"]
        if isinstance(share_urls, list):
            share_count = len(share_urls)
        else:
            share_count = len(
                [line.strip() for line in str(share_urls).split("\n") if line.strip()]
            )
        target.info(f"  分享链接数量: {share_count} 个")
    if config.get("save_dir"):
        target.info(f"  保存目录: {config['save_dir']}")
    if config.get("regex_pattern"):
        target.debug(f"  文件过滤规则: {config['regex_pattern']}")
    if config.get("wechat_webhook"):
        target.debug("  ✓ 企业微信通知已配置")


def log_separator(char: str = "-", width: int = 60) -> None:
    """记录分隔线。"""
    get_logger().info(char * width)
