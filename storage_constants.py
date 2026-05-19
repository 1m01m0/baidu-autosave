#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存储相关的环境变量派生常量。

本模块只允许依赖 ``env_utils`` 与 Python 标准库，
用于打破 ``storage.py`` / ``storage_streaming.py`` /
``storage_transfer_plan.py`` / ``storage_rename.py`` 之间的循环依赖。

所有常量的环境变量名、默认值与解析方式必须与原 ``storage.py``
模块级定义完全一致，使得相同环境变量状态下的取值无差异。
"""

from env_utils import read_non_negative_float_env, read_positive_int_env


# 触发限频时的固定等待秒数（用于退避后重试）。
RATE_LIMIT_WAIT_TIME = 10

# 重命名相邻调用间的固定延时；默认 0，因 call_with_retry 已处理限频。
# 历史默认值是 0.5s，保留为可调 env，若用户遇到限频可手动调高。
RENAME_DELAY = read_non_negative_float_env("TRANSFERSHARE_RENAME_DELAY", 0)

# 重命名并发度；默认串行，>1 时启用 ThreadPoolExecutor，触发限频会自动退避。
RENAME_CONCURRENCY = read_positive_int_env("TRANSFERSHARE_RENAME_CONCURRENCY", 1)

# 批量分享之间的延时（秒），用于控制批量调用频率。
BATCH_SHARE_DELAY = read_non_negative_float_env("TRANSFERSHARE_BATCH_SHARE_DELAY", 0)

# 多链接并发执行的 worker 数。默认 1（串行），>1 时让多个独立分享的转存
# 流水线重叠执行。注意：百度对同一账号有 list/transfer 限频，并发过高会
# 触发 error_code: -65；建议从 2 起步实测，推荐上限 4。
MULTI_SHARE_CONCURRENCY = read_positive_int_env(
    "TRANSFERSHARE_MULTI_SHARE_CONCURRENCY", 1
)

# 单次 transfer 调用打包的最大 fs_id 数量。
TRANSFER_BATCH_SIZE = read_positive_int_env("TRANSFERSHARE_TRANSFER_BATCH_SIZE", 999)

# 转存失败的整体重试次数（不含首次调用）。
TRANSFER_FAILED_RETRY_ATTEMPTS = read_positive_int_env(
    "TRANSFERSHARE_TRANSFER_FAILED_RETRY_ATTEMPTS", 2
)

# 转存失败重试之间的等待秒数。
TRANSFER_FAILED_RETRY_DELAY = read_non_negative_float_env(
    "TRANSFERSHARE_TRANSFER_FAILED_RETRY_DELAY", 5
)

# 流式生产者线程在主线程结束时的 join 超时秒数。
STREAM_PRODUCER_JOIN_TIMEOUT = read_non_negative_float_env(
    "TRANSFERSHARE_STREAM_PRODUCER_JOIN_TIMEOUT", 5
)

# Streaming 路径上的"扫描-转存"双缓冲开关。默认启用：把 _execute_transfer_plan
# 调用丢给单个 worker 线程异步执行，主线程立即回去消费扫描队列，从而让网络
# 调用与后续扫描重叠。worker 数固定为 1，避免对同一账号触发并发转存限频。
TRANSFER_PIPELINE_ENABLED = read_positive_int_env(
    "TRANSFERSHARE_TRANSFER_PIPELINE", 1
) >= 1


__all__ = [
    "RATE_LIMIT_WAIT_TIME",
    "RENAME_DELAY",
    "RENAME_CONCURRENCY",
    "BATCH_SHARE_DELAY",
    "MULTI_SHARE_CONCURRENCY",
    "TRANSFER_BATCH_SIZE",
    "TRANSFER_FAILED_RETRY_ATTEMPTS",
    "TRANSFER_FAILED_RETRY_DELAY",
    "STREAM_PRODUCER_JOIN_TIMEOUT",
    "TRANSFER_PIPELINE_ENABLED",
]
