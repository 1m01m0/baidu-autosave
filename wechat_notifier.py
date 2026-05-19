#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业微信机器人通知器。

设计要点：

- 仅依赖 ``utils`` 提供的脱敏函数；自身不再回调 ``handle_error_and_notify``，
  彻底打破与 ``utils`` 的循环依赖。
- 失败重试只覆盖网络层异常和 5xx；对 4xx / 业务错误（``errcode != 0``）不再无差别重试。
- 通过 ``GitHubActionsContext`` 可注入运行时信息，避免方法直接读环境变量，便于测试。
- markdown 报告统一通过 ``_render_report`` 拼装，移除原来四个分支的复制粘贴。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

from utils import collect_transferred_files, mask_sensitive

# ============================================================================
# 1. 配置常量
# ============================================================================

# HTTP 行为
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_DELAY = 5  # seconds
DEFAULT_CONNECT_TIMEOUT = 10  # seconds
DEFAULT_READ_TIMEOUT = 30  # seconds

# 文案
MAX_FILES_TO_SHOW = 5
DEFAULT_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_SAVE_DIR = "默认"

# 仅这些 HTTP 状态视为可重试网络故障
_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


_logger = logging.getLogger("transfershare.wechat")


# ============================================================================
# 2. GitHub Actions 元数据（可注入，便于测试）
# ============================================================================


@dataclass(frozen=True)
class GitHubActionsContext:
    """GitHub Actions 运行时元数据。"""

    repository: str = ""
    run_id: str = ""
    run_number: str = ""
    workflow: str = ""
    ref: str = ""
    sha: str = ""
    server_url: str = "https://github.com"

    @property
    def short_sha(self) -> str:
        return self.sha[:7] if self.sha else ""

    @property
    def run_url(self) -> Optional[str]:
        if self.repository and self.run_id:
            return f"{self.server_url}/{self.repository}/actions/runs/{self.run_id}"
        return None

    @property
    def commit_url(self) -> Optional[str]:
        if self.repository and self.sha:
            return f"{self.server_url}/{self.repository}/commit/{self.sha}"
        return None

    @property
    def ref_label(self) -> str:
        return (
            self.ref.replace("refs/heads/", "")
            .replace("refs/tags/", "")
            .replace("refs/pull/", "PR-")
        )


def _read_github_actions_context_from_env(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[GitHubActionsContext]:
    """仅当 ``GITHUB_ACTIONS=true`` 时返回上下文，否则 None。"""
    values = env if env is not None else os.environ
    if values.get("GITHUB_ACTIONS") != "true":
        return None
    return GitHubActionsContext(
        repository=values.get("GITHUB_REPOSITORY", ""),
        run_id=values.get("GITHUB_RUN_ID", ""),
        run_number=values.get("GITHUB_RUN_NUMBER", ""),
        workflow=values.get("GITHUB_WORKFLOW", ""),
        ref=values.get("GITHUB_REF", ""),
        sha=values.get("GITHUB_SHA", ""),
        server_url=values.get("GITHUB_SERVER_URL", "https://github.com"),
    )


# ============================================================================
# 3. 通知器
# ============================================================================


_FieldList = List[Tuple[str, str]]


class WeChatNotifier:
    """企业微信机器人通知器。"""

    def __init__(
        self,
        webhook_url: str,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        timezone: ZoneInfo = DEFAULT_TIMEZONE,
        github_context_provider: Optional[
            Callable[[], Optional[GitHubActionsContext]]
        ] = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout: Tuple[float, float] = (connect_timeout, read_timeout)
        self.timezone = timezone
        self._github_context_provider = (
            github_context_provider or _read_github_actions_context_from_env
        )

    # ---- 对外接口 -----------------------------------------------------------

    def send_message(self, message: str, msg_type: str = "text") -> bool:
        """发送一条消息，必要时重试。"""
        masked = self._mask_sensitive(message) or message
        payload = self._build_message_data(masked, msg_type)

        last_error: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    self.webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                _logger.warning(
                    "企业微信通知请求异常 (%s/%s): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    self._mask_sensitive(last_error),
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)
                    continue
                return False

            ok, retryable, message = self._classify_response(response)
            if ok:
                _logger.info("企业微信通知发送成功")
                return True

            last_error = message
            _logger.warning(
                "企业微信通知发送失败 (%s/%s): %s",
                attempt + 1,
                self.max_retries + 1,
                self._mask_sensitive(message),
            )
            if not retryable or attempt >= self.max_retries:
                return False
            time.sleep(self.retry_delay)

        _logger.error("企业微信通知最终失败: %s", self._mask_sensitive(last_error or ""))
        return False

    def send_transfer_result(
        self, result: Dict[str, Any], config: Optional[Dict[str, Any]]
    ) -> bool:
        """根据转存结果发送通知。"""
        save_dir = self._get_save_dir(config)
        total_count = result.get("total_count", 1)
        task_desc = (
            f"批量转存任务 ({total_count}个链接)" if total_count > 1 else "转存任务"
        )

        if result.get("success"):
            return self._send_success_or_skipped_report(result, task_desc, save_dir)
        if result.get("partial"):
            return self._send_partial_report(result, task_desc, save_dir)
        return self._send_failure_report(result, task_desc, save_dir)

    def send_error_notification(
        self, error_msg: str, config: Optional[Dict[str, Any]]
    ) -> bool:
        """发送系统级异常通知，自动附带 GitHub Actions 元数据。"""
        save_dir = self._get_save_dir(config)
        masked_error = self._mask_sensitive(error_msg) or error_msg
        github_block = self._render_github_actions_block()

        body_parts = [
            ("时间", self._get_current_time()),
            ("状态", "❌ 执行异常"),
            ("任务类型", "自动转存任务"),
            ("保存目录", save_dir),
        ]

        sections = [github_block] if github_block else []
        sections.append(f"**错误信息**: {masked_error}")
        sections.append("请检查配置或联系管理员处理。")

        message = self._render_report(
            heading="## ⚠️ 百度网盘转存异常",
            fields=body_parts,
            extra_sections=sections,
        )
        return self.send_message(message, "markdown")

    def send_test_message(self) -> bool:
        message = self._render_report(
            heading="## 🔔 测试通知",
            fields=[
                ("时间", self._get_current_time()),
                ("状态", "✅ 企业微信通知测试成功"),
            ],
            extra_sections=["百度网盘自动转存系统已就绪！"],
        )
        return self.send_message(message, "markdown")

    # ---- 报告渲染 -----------------------------------------------------------

    def _send_success_or_skipped_report(
        self, result: Dict[str, Any], task_desc: str, save_dir: str
    ) -> bool:
        if result.get("skipped"):
            result_msg = result.get("message") or result.get(
                "summary", "没有新文件需要转存"
            )
            message = self._render_report(
                heading="## 📋 百度网盘转存报告",
                fields=[
                    ("时间", self._get_current_time()),
                    ("状态", "✅ 完成（无新文件）"),
                    ("任务", task_desc),
                    ("保存目录", save_dir),
                    ("结果", result_msg),
                ],
            )
            return self.send_message(message, "markdown")

        transferred_files = collect_transferred_files(result)
        result_msg = result.get("message") or result.get("summary", "转存成功")
        files_section = self._format_files_block("转存文件", transferred_files, lambda x: x)
        message = self._render_report(
            heading="## 🎉 百度网盘转存报告",
            fields=[
                ("时间", self._get_current_time()),
                ("状态", "✅ 转存成功"),
                ("任务", task_desc),
                ("保存目录", save_dir),
                ("结果", result_msg),
            ],
            extra_sections=[files_section] if files_section else [],
        )
        return self.send_message(message, "markdown")

    def _send_partial_report(
        self, result: Dict[str, Any], task_desc: str, save_dir: str
    ) -> bool:
        error_msg = result.get("error", "部分转存成功")
        transfer_failed_block = self._format_files_block(
            "转存失败",
            result.get("transfer_failed_files", []),
            lambda item: (
                f"{item.get('final_path') or item.get('clean_path')}: "
                f"{item.get('error')}"
            ),
        )
        rename_failed_block = self._format_files_block(
            "重命名失败",
            result.get("rename_failed_files", []),
            lambda item: (
                f"{item.get('source_path')} -> {item.get('target_path')}: "
                f"{item.get('error')}"
            ),
        )
        sections = [block for block in (transfer_failed_block, rename_failed_block) if block]
        message = self._render_report(
            heading="## ⚠️ 百度网盘转存报告",
            fields=[
                ("时间", self._get_current_time()),
                ("状态", "⚠️ 部分成功（按失败处理，退出码 1）"),
                ("任务", task_desc),
                ("保存目录", save_dir),
                ("结果", error_msg),
            ],
            extra_sections=sections,
        )
        return self.send_message(message, "markdown")

    def _send_failure_report(
        self, result: Dict[str, Any], task_desc: str, save_dir: str
    ) -> bool:
        error_msg = result.get("error", "未知错误")
        message = self._render_report(
            heading="## ❌ 百度网盘转存报告",
            fields=[
                ("时间", self._get_current_time()),
                ("状态", "❌ 转存失败"),
                ("任务", task_desc),
                ("保存目录", save_dir),
                ("错误信息", error_msg),
            ],
            extra_sections=["请检查分享链接是否有效，或查看详细日志排查问题。"],
        )
        return self.send_message(message, "markdown")

    @staticmethod
    def _render_report(
        *,
        heading: str,
        fields: _FieldList,
        extra_sections: Optional[List[str]] = None,
    ) -> str:
        lines = [heading]
        lines.extend(f"**{name}**: {value}" for name, value in fields)
        result = "\n".join(lines)
        for section in extra_sections or []:
            if not section:
                continue
            result = f"{result}\n{section}" if not section.startswith("\n") else f"{result}{section}"
        return result

    def _render_github_actions_block(self) -> str:
        ctx = self._github_context_provider()
        if ctx is None:
            return ""

        lines = [
            "**GitHub Actions 详情**:",
            f"- 仓库: `{ctx.repository or 'N/A'}`",
            f"- 工作流: `{ctx.workflow or 'N/A'}`",
            f"- 运行编号: `#{ctx.run_number or 'N/A'}`",
            f"- 分支/标签: `{ctx.ref_label or 'N/A'}`",
            f"- 提交: `{ctx.short_sha or 'N/A'}`",
        ]
        if ctx.run_url:
            lines.append(f"- 🔗 [查看运行详情]({ctx.run_url})")
        if ctx.commit_url:
            lines.append(f"- 🔗 [查看提交详情]({ctx.commit_url})")
        return "\n".join(lines)

    @staticmethod
    def _format_files_block(
        title: str,
        items: List[Any],
        formatter: Callable[[Any], str],
    ) -> str:
        if not items:
            return ""
        shown = items[:MAX_FILES_TO_SHOW]
        lines = [f"\n**{title}**:"] + [f"• {formatter(item)}" for item in shown]
        if len(items) > MAX_FILES_TO_SHOW:
            lines.append(f"• ... 还有 {len(items) - MAX_FILES_TO_SHOW} 个文件")
        return "\n".join(lines)

    # ---- HTTP 辅助 ----------------------------------------------------------

    @staticmethod
    def _build_message_data(message: str, msg_type: str) -> Dict[str, Any]:
        if msg_type == "text":
            return {"msgtype": "text", "text": {"content": message}}
        if msg_type == "markdown":
            return {"msgtype": "markdown", "markdown": {"content": message}}
        raise ValueError(f"不支持的消息类型: {msg_type}")

    @staticmethod
    def _classify_response(response: requests.Response) -> Tuple[bool, bool, str]:
        """返回 (是否成功, 是否值得重试, 描述信息)。"""
        if response.status_code != 200:
            retryable = response.status_code in _RETRYABLE_HTTP_STATUS_CODES
            return False, retryable, f"HTTP {response.status_code}"

        try:
            payload = response.json()
        except ValueError:
            return False, True, "返回内容不是合法 JSON"

        errcode = payload.get("errcode")
        if errcode == 0:
            return True, False, "ok"
        return False, False, payload.get("errmsg") or f"errcode={errcode}"

    # ---- 工具方法 -----------------------------------------------------------

    def _get_current_time(self) -> str:
        return datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _get_save_dir(config: Optional[Dict[str, Any]]) -> str:
        return config.get("save_dir", DEFAULT_SAVE_DIR) if config else DEFAULT_SAVE_DIR

    @staticmethod
    def _mask_sensitive(text: Optional[str]) -> Optional[str]:
        if text is None:
            return text
        return mask_sensitive(text)
