#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from storage import BaiduStorage
from wechat_notifier import WeChatNotifier
from utils import handle_error_and_notify, collect_transferred_files, mask_share_url, mask_sensitive
from storage_errors import (
    classify_storage_error,
    is_storage_error_kind_retryable,
    is_storage_temporary_error_info,
)
from logger import (
    get_logger,
    setup_logging,
    log_startup,
    log_shutdown,
    log_config_loaded,
)
from config_utils import (
    build_retry_share_config,
    load_runtime_config,
    retry_share_config_key,
    validate_runtime_config,
)


FAILED_TRANSFERS_FILE = Path(__file__).resolve().parent / ".transfershare_failed_transfers.json"
MAX_FAILED_TRANSFER_RECORDS = 100
MAX_FAILED_FILES_PER_RECORD = 1000


def _read_positive_int_env(name, default):
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


MAX_FAILED_TRANSFER_ATTEMPTS = _read_positive_int_env(
    "TRANSFERSHARE_MAX_FAILED_TRANSFER_ATTEMPTS", 3
)
FAILED_RECORD_SCHEMA_VERSION = 2
TEMPORARY_FAILED_RETRY_DELAY_SECONDS = 6 * 60 * 60
FAILED_RECORD_PERSISTED_KEYS = (
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
    "next_retry_after",
)
FAILED_FILE_PERSISTED_KEYS = (
    "fs_id",
    "dir_path",
    "clean_path",
    "final_path",
    "error",
    "error_kind",
    "retryable",
    "temporary",
    "failed_at",
)


def load_failed_transfer_records(path=None):
    path = Path(path or FAILED_TRANSFERS_FILE)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise ValueError(f"读取失败清单失败: {exc}") from exc

    if isinstance(data, dict):
        records = data.get("records", [])
    elif isinstance(data, list):
        records = data
    else:
        records = []
    if not isinstance(records, list):
        return []
    return [
        record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("share_config"), dict)
    ]


def save_failed_transfer_records(records, path=None):
    path = Path(path or FAILED_TRANSFERS_FILE)
    if not records:
        path.unlink(missing_ok=True)
        return

    fd = None
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump({"records": records}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
        tmp_path = None
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _current_timestamp():
    return int(time.time())


def _record_attempts(records):
    attempts = {}
    for record in records or []:
        config = record.get("share_config") or {}
        try:
            attempts[retry_share_config_key(config)] = int(record.get("attempts", 0))
        except (TypeError, ValueError):
            attempts[retry_share_config_key(config)] = 0
    return attempts


def _records_by_config_key(records):
    keyed_records = {}
    for record in records or []:
        config = record.get("share_config") or {}
        if config.get("share_url"):
            keyed_records[retry_share_config_key(config)] = record
    return keyed_records


def _failed_record_keys(records):
    return set(_records_by_config_key(records).keys())


def _filter_current_share_configs(share_configs, skipped_keys):
    current_configs = []
    skipped_count = 0
    for share_config in share_configs or []:
        if retry_share_config_key(share_config) in skipped_keys:
            skipped_count += 1
        else:
            current_configs.append(share_config)
    return current_configs, skipped_count


def _build_no_current_share_result():
    return {
        "success": True,
        "skipped": True,
        "summary": "没有新的分享任务需要执行",
        "message": "没有新的分享任务需要执行",
        "results": [],
    }


def _coerce_bool(value, default):
    return value if isinstance(value, bool) else default


def _default_retryable_for_error(error_info):
    if error_info.kind == "unknown":
        return True
    return error_info.retryable


def _normalize_failed_file(failed_file, fallback_error, now):
    detail = dict(failed_file)
    error_text = detail.get("error") or fallback_error or ""
    error_info = classify_storage_error(error_text)
    error_kind = detail.get("error_kind") or error_info.kind
    retryable_default = is_storage_error_kind_retryable(
        error_kind, _default_retryable_for_error(error_info)
    )
    retryable = _coerce_bool(detail.get("retryable"), retryable_default)
    temporary = _coerce_bool(
        detail.get("temporary"), is_storage_temporary_error_info(error_info)
    )

    normalized = {}
    for key in ("fs_id", "dir_path"):
        if detail.get(key) not in (None, ""):
            normalized[key] = detail[key]
    clean_path = detail.get("clean_path") or detail.get("path")
    if clean_path:
        normalized["clean_path"] = clean_path
    final_path = detail.get("final_path")
    if final_path:
        normalized["final_path"] = final_path
    if error_text:
        normalized["error"] = error_text
    normalized["error_kind"] = error_kind
    normalized["retryable"] = retryable
    normalized["temporary"] = temporary
    normalized["failed_at"] = detail.get("failed_at", now)
    return {
        key: normalized[key]
        for key in FAILED_FILE_PERSISTED_KEYS
        if key in normalized and normalized[key] not in (None, "")
    }


def _normalize_failed_files(failed_files, fallback_error, now):
    normalized = []
    for failed_file in failed_files or []:
        if not isinstance(failed_file, dict):
            continue
        normalized.append(_normalize_failed_file(failed_file, fallback_error, now))
        if len(normalized) >= MAX_FAILED_FILES_PER_RECORD:
            break
    return normalized


def _filter_failed_record_fields(record):
    return {
        key: record[key]
        for key in FAILED_RECORD_PERSISTED_KEYS
        if key in record and record[key] not in (None, "")
    }


def _trim_failed_record(record):
    fallback_error = record.get("error") or record.get("message") or ""
    now = record.get("last_failed_at") or record.get("failed_at") or _current_timestamp()
    normalized_files = _normalize_failed_files(record.get("failed_files", []), fallback_error, now)
    source = {
        **record,
        "schema_version": record.get("schema_version") or FAILED_RECORD_SCHEMA_VERSION,
        "share_config": build_retry_share_config(record.get("share_config") or {}),
        "failed_files": normalized_files,
    }
    return _filter_failed_record_fields(source)


def build_failed_transfer_records(result, previous_records=None, increment_attempts=False):
    previous_attempts = _record_attempts(previous_records)
    previous_by_key = _records_by_config_key(previous_records)
    records = []
    now = _current_timestamp()
    result_items = result.get("results") if isinstance(result, dict) else []
    for item in result_items or []:
        if not isinstance(item, dict):
            continue
        retry_config = item.get("retry_config")
        failed_files = item.get("transfer_failed_files", [])
        if not retry_config or not failed_files:
            continue
        key = retry_share_config_key(retry_config)
        error_text = item.get("error") or item.get("message") or "转存失败"
        normalized_failed_files = _normalize_failed_files(failed_files, error_text, now)
        if not normalized_failed_files:
            continue
        retryable = any(file.get("retryable") for file in normalized_failed_files)
        temporary = any(file.get("temporary") for file in normalized_failed_files)
        previous_record = previous_by_key.get(key, {})
        record = {
            "schema_version": FAILED_RECORD_SCHEMA_VERSION,
            "share_config": build_retry_share_config(retry_config),
            "failed_files": normalized_failed_files,
            "error": error_text,
            "error_kind": normalized_failed_files[0].get("error_kind", "unknown"),
            "retryable": retryable,
            "temporary": temporary,
            "failed_at": previous_record.get("failed_at", now),
            "last_failed_at": now,
            "attempts": previous_attempts.get(key, 0) + (1 if increment_attempts else 0),
        }
        if retryable and temporary:
            record["next_retry_after"] = now + TEMPORARY_FAILED_RETRY_DELAY_SECONDS
        records.append(_filter_failed_record_fields(record))
    return records


def merge_failed_transfer_records(*record_groups):
    merged = {}
    for records in record_groups:
        for record in records or []:
            config = record.get("share_config") or {}
            if config.get("share_url"):
                merged[retry_share_config_key(config)] = _trim_failed_record(record)
    return list(merged.values())[-MAX_FAILED_TRANSFER_RECORDS:]


def _failed_record_attempts(record):
    try:
        return int(record.get("attempts", 0))
    except (TypeError, ValueError):
        return 0


def _failed_record_retryable(record):
    if "retryable" in record:
        return _coerce_bool(record.get("retryable"), True)
    failed_files = record.get("failed_files", [])
    if isinstance(failed_files, list) and failed_files:
        explicit_values = [
            item.get("retryable")
            for item in failed_files
            if isinstance(item, dict) and "retryable" in item
        ]
        if explicit_values:
            return any(_coerce_bool(value, True) for value in explicit_values)
    return True


def _failed_record_next_retry_after(record):
    try:
        return int(record.get("next_retry_after") or 0)
    except (TypeError, ValueError):
        return 0


def split_failed_transfer_records_by_status(records, now=None):
    now = _current_timestamp() if now is None else int(now)
    retryable_records = []
    deferred_records = []
    permanent_records = []
    exhausted_records = []
    for record in records or []:
        if not _failed_record_retryable(record):
            permanent_records.append(record)
        elif _failed_record_attempts(record) >= MAX_FAILED_TRANSFER_ATTEMPTS:
            exhausted_records.append(record)
        elif _failed_record_next_retry_after(record) > now:
            deferred_records.append(record)
        else:
            retryable_records.append(record)
    return retryable_records, deferred_records, permanent_records, exhausted_records


def split_failed_transfer_records_by_attempts(records):
    retryable_records, deferred_records, permanent_records, exhausted_records = (
        split_failed_transfer_records_by_status(records)
    )
    return retryable_records + deferred_records + permanent_records, exhausted_records


def log_exhausted_failed_records(logger, records, reason):
    if not records:
        return
    logger.warning(
        f"{reason}: {len(records)} 个分享任务已达到重试上限 "
        f"{MAX_FAILED_TRANSFER_ATTEMPTS}，不再自动重试"
    )
    log_transfer_failed_files(logger, _failed_records_to_files(records))


def log_permanent_failed_records(logger, records, reason):
    if not records:
        return
    logger.warning(f"{reason}: {len(records)} 个分享任务不可自动恢复，不再自动重试")
    log_transfer_failed_files(logger, _failed_records_to_files(records))


def log_deferred_failed_records(logger, records, reason):
    if records:
        logger.info(f"{reason}: {len(records)} 个分享任务暂未到下次重试时间")


def _failed_records_to_files(records):
    files = []
    for record in records or []:
        config = record.get("share_config") or {}
        masked_share_url = mask_share_url(config.get("share_url")) or config.get("share_url")
        for failed_file in record.get("failed_files", []):
            detail = dict(failed_file)
            detail.setdefault("share_url", masked_share_url)
            detail.setdefault("save_dir", config.get("save_dir"))
            detail.setdefault("error", record.get("error"))
            detail.setdefault("error_kind", record.get("error_kind"))
            files.append(detail)
    return files


def log_transfer_failed_files(logger, transfer_failed_files):
    if not transfer_failed_files:
        return
    logger.warning(f"转存失败文件 ({len(transfer_failed_files)}个):")
    for index, item in enumerate(transfer_failed_files[:10], 1):
        file_path = item.get("final_path") or item.get("clean_path") or item.get("path")
        dir_path = item.get("dir_path") or item.get("save_dir") or ""
        detail = f"  {index}. {dir_path}/{file_path}: {item.get('error')}"
        logger.warning(mask_sensitive(detail) or detail)
    if len(transfer_failed_files) > 10:
        logger.warning(f"  ... 还有 {len(transfer_failed_files) - 10} 个文件")


def attach_failed_records_to_result(result, records):
    failed_files = _failed_records_to_files(records)
    if not failed_files:
        return result

    existing_files = list(result.get("transfer_failed_files", []))
    existing_keys = {
        (
            item.get("share_url"),
            item.get("save_dir"),
            item.get("fs_id"),
            item.get("clean_path"),
            item.get("final_path"),
        )
        for item in existing_files
    }
    for failed_file in failed_files:
        key = (
            failed_file.get("share_url"),
            failed_file.get("save_dir"),
            failed_file.get("fs_id"),
            failed_file.get("clean_path"),
            failed_file.get("final_path"),
        )
        if key not in existing_keys:
            existing_keys.add(key)
            existing_files.append(failed_file)

    result["transfer_failed_files"] = existing_files
    result["transfer_failed_count"] = len(existing_files)
    suffix = f"仍有 {len(failed_files)} 个历史失败文件等待下次重试"
    base_message = result.get("error") or result.get("message") or result.get("summary", "")
    if result.get("success"):
        result["success"] = False
        result["partial"] = True
        result["error"] = suffix
        result["message"] = suffix
    elif base_message and suffix not in base_message:
        result["error"] = f"{base_message}；{suffix}"
        result["message"] = result["error"]
    return result


def retry_history_failed_records(storage, logger, failed_records):
    remaining_failed_records = []
    (
        retryable_failed_records,
        deferred_failed_records,
        permanent_failed_records,
        exhausted_failed_records,
    ) = split_failed_transfer_records_by_status(failed_records)
    remaining_failed_records.extend(deferred_failed_records)
    log_deferred_failed_records(logger, deferred_failed_records, "历史失败清单")
    log_permanent_failed_records(logger, permanent_failed_records, "历史失败清单")
    log_exhausted_failed_records(logger, exhausted_failed_records, "历史失败清单")
    if not retryable_failed_records:
        return remaining_failed_records

    logger.info(f"发现历史失败清单，先重试 {len(retryable_failed_records)} 个分享任务")
    retry_result = storage.transfer_multiple_shares(
        share_configs=[record["share_config"] for record in retryable_failed_records],
        progress_callback=progress_callback,
    )
    retry_failed_records = build_failed_transfer_records(
        retry_result, retryable_failed_records, increment_attempts=True
    )
    (
        retry_remaining_records,
        retry_deferred_records,
        retry_permanent_records,
        retry_exhausted_records,
    ) = split_failed_transfer_records_by_status(retry_failed_records)
    remaining_failed_records = merge_failed_transfer_records(
        remaining_failed_records, retry_remaining_records, retry_deferred_records
    )
    log_permanent_failed_records(logger, retry_permanent_records, "历史失败清单重试后")
    log_exhausted_failed_records(logger, retry_exhausted_records, "历史失败清单重试后")
    if remaining_failed_records:
        logger.warning(f"历史失败清单仍有 {len(remaining_failed_records)} 个分享任务未完成")
    elif retry_failed_records:
        logger.info("历史失败清单已不再需要写回")
    else:
        logger.info("历史失败清单已全部重试成功")
    return remaining_failed_records


def run_current_transfer(storage, logger, config, failed_records):
    current_share_configs, skipped_current_count = _filter_current_share_configs(
        config["share_configs"], _failed_record_keys(failed_records)
    )
    if skipped_current_count:
        logger.info(
            f"跳过 {skipped_current_count} 个已由历史失败清单处理的分享任务，避免同一 run 重复转存"
        )

    logger.info("开始执行批量转存任务...")
    if not current_share_configs:
        return _build_no_current_share_result()
    return storage.transfer_multiple_shares(
        share_configs=current_share_configs,
        progress_callback=progress_callback,
    )


def check_network_connectivity():
    """检查网络连通性"""
    try:
        import requests

        try:
            logger = get_logger()
        except Exception:
            logger = None

        if os.getenv("GITHUB_ACTIONS") == "true":
            msg = "检测到GitHub Actions环境，正在检查网络连通性..."
            if logger:
                logger.info(msg)
            else:
                print(msg)

            try:
                response = requests.get("https://www.baidu.com", timeout=10)
                if response.status_code == 200:
                    msg = "✅ 百度主站连通正常"
                    if logger:
                        logger.info(msg)
                    else:
                        print(msg)
                else:
                    msg = f"⚠️ 百度主站连通异常: HTTP {response.status_code}"
                    if logger:
                        logger.warning(msg)
                    else:
                        print(msg)
            except Exception as e:
                msg = f"❌ 百度主站连通失败: {str(e)}"
                if logger:
                    logger.error(msg)
                else:
                    print(msg)

            try:
                response = requests.get("https://pan.baidu.com", timeout=10)
                if response.status_code == 200:
                    msg = "✅ 百度网盘连通正常"
                    if logger:
                        logger.info(msg)
                    else:
                        print(msg)
                else:
                    msg = f"⚠️ 百度网盘连通异常: HTTP {response.status_code}"
                    if logger:
                        logger.warning(msg)
                    else:
                        print(msg)
            except Exception as e:
                msg = f"❌ 百度网盘连通失败: {str(e)}"
                if logger:
                    logger.error(msg)
                else:
                    print(msg)
                msg2 = "提示: GitHub Actions环境可能存在网络访问限制"
                if logger:
                    logger.info(msg2)
                else:
                    print(msg2)

    except ImportError:
        try:
            logger = get_logger()
            logger.debug("网络检查跳过: requests库不可用")
        except Exception:
            print("网络检查跳过: requests库不可用")
    except Exception as e:
        try:
            logger = get_logger()
            logger.error(f"网络检查异常: {str(e)}")
        except Exception:
            print(f"网络检查异常: {str(e)}")


def progress_callback(level, message):
    """进度回调函数 - 实时输出进度信息"""
    safe_message = mask_sensitive(message) or message
    print(f"[{(level or 'INFO').upper()}] {safe_message}")


def main():
    """主函数"""
    setup_logging()
    logger = get_logger()
    log_startup()

    check_network_connectivity()

    config = None
    notifier = None
    run_success = False

    try:
        config = load_runtime_config()
        validation = validate_runtime_config(config)
        if validation["errors"]:
            raise ValueError("配置校验失败: " + "; ".join(validation["errors"]))
        validated_config = validation["config"]
        if validated_config is not config:
            config.clear()
            config.update(validated_config)
        for warning in validation["warnings"]:
            logger.warning(warning)

        if config.get("config_source") == "file":
            logger.info(f"检测到本地配置文件: {config['config_path']}，优先使用本地配置")
        elif config.get("config_load_warning"):
            logger.warning(
                f"读取本地配置文件失败，回退到环境变量: {config['config_load_warning']}"
            )

        log_config_loaded(config)
        logger.info(
            f"  企业微信通知: {'已配置' if config['wechat_webhook'] else '未配置'}"
        )

        if config["wechat_webhook"]:
            notifier = WeChatNotifier(config["wechat_webhook"])
            logger.info("企业微信通知器初始化成功")

        logger.info("初始化百度网盘客户端...")
        storage = BaiduStorage(config["cookies"], config["wechat_webhook"])

        if not storage.is_valid():
            raise Exception("百度网盘客户端初始化失败，请检查cookies是否有效")

        quota_info = storage.get_quota_info()
        if quota_info:
            logger.info(
                f"网盘空间: {quota_info['used_gb']}GB / {quota_info['total_gb']}GB"
            )

        failed_records_load_error = False
        try:
            failed_records = load_failed_transfer_records()
        except Exception as exc:
            failed_records = []
            failed_records_load_error = True
            logger.warning(f"读取历史失败清单失败，保留原文件不覆盖: {exc}")
        remaining_failed_records = retry_history_failed_records(
            storage, logger, failed_records
        )
        result = run_current_transfer(storage, logger, config, failed_records)
        new_failed_records = build_failed_transfer_records(
            result, failed_records, increment_attempts=True
        )
        (
            new_retryable_records,
            new_deferred_records,
            new_permanent_records,
            new_exhausted_records,
        ) = split_failed_transfer_records_by_status(new_failed_records)
        log_permanent_failed_records(logger, new_permanent_records, "本次失败清单")
        log_exhausted_failed_records(logger, new_exhausted_records, "本次失败清单")
        if remaining_failed_records:
            result = attach_failed_records_to_result(result, remaining_failed_records)
        all_failed_records = merge_failed_transfer_records(
            remaining_failed_records, new_retryable_records, new_deferred_records
        )
        if failed_records_load_error:
            if all_failed_records:
                logger.warning("因历史失败清单读取失败，跳过更新失败清单以避免覆盖原文件")
        else:
            save_failed_transfer_records(all_failed_records)
            if all_failed_records:
                logger.warning(f"失败清单已保存: {FAILED_TRANSFERS_FILE}")
            elif failed_records:
                logger.info("失败清单已清理")

        if result["success"]:
            if result.get("skipped"):
                logger.info(
                    f"✅ 任务完成: {result.get('message', result.get('summary', '转存完成'))}"
                )
            else:
                if "results" in result:
                    summary = mask_sensitive(result["summary"]) or result["summary"]
                    logger.info(f"🎉 批量转存成功: {summary}")
                else:
                    success_message = result.get('message', result.get('summary', '转存成功'))
                    safe_success_message = mask_sensitive(success_message) or success_message
                    logger.info(f"🎉 转存成功: {safe_success_message}")

                transferred_files = collect_transferred_files(result)
                if transferred_files:
                    logger.info(f"转存文件列表 ({len(transferred_files)}个):")
                    for index, file in enumerate(transferred_files[:10], 1):
                        detail = f"  {index}. {file}"
                        logger.info(mask_sensitive(detail) or detail)
                    if len(transferred_files) > 10:
                        logger.info(
                            f"  ... 还有 {len(transferred_files) - 10} 个文件"
                        )
        elif result.get("partial"):
            error_msg = result.get("error", result.get("summary", "部分转存成功"))
            safe_error_msg = mask_sensitive(error_msg) or error_msg
            logger.warning(f"⚠️ 转存部分成功（按失败处理，退出码 1）: {safe_error_msg}")
            log_transfer_failed_files(logger, result.get("transfer_failed_files", []))
            rename_failed_files = result.get("rename_failed_files", [])
            if rename_failed_files:
                logger.warning(f"重命名失败文件 ({len(rename_failed_files)}个):")
                for index, item in enumerate(rename_failed_files[:10], 1):
                    detail = (
                        f"  {index}. {item.get('source_path')} -> "
                        f"{item.get('target_path')}: {item.get('error')}"
                    )
                    logger.warning(mask_sensitive(detail) or detail)
                if len(rename_failed_files) > 10:
                    logger.warning(
                        f"  ... 还有 {len(rename_failed_files) - 10} 个文件"
                    )
        else:
            error_msg = result.get("error", result.get("summary", "未知错误"))
            safe_error_msg = mask_sensitive(error_msg) or error_msg
            logger.error(f"❌ 转存失败: {safe_error_msg}")
            log_transfer_failed_files(logger, result.get("transfer_failed_files", []))

        if notifier:
            logger.info("发送企业微信通知...")
            notification_sent = notifier.send_transfer_result(result, config)
            if not notification_sent:
                logger.warning("企业微信通知发送失败")

        if not result["success"]:
            sys.exit(1)

        run_success = True

    except Exception as e:
        handle_error_and_notify(e, "主任务执行失败", notifier, config, collect=False)
        sys.exit(1)

    finally:
        log_shutdown(success=run_success)


if __name__ == "__main__":
    main()
