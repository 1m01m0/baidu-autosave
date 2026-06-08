#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""目录分治转存遍历器。"""

import os
import posixpath

from storage_constants import TRANSFER_BATCH_SIZE
from storage_errors import classify_storage_error, is_transfer_count_limit_error
from storage_models import DirTreeFrame, TransferItem
from storage_progress import ProgressReporter
from storage_rules import should_exclude_folder


class DirTreeTraverser:
    def __init__(
        self,
        path_service,
        share_service,
        transfer_executor,
        progress=None,
        error_notifier=None,
        batch_size=TRANSFER_BATCH_SIZE,
    ):
        self.path_service = path_service
        self.share_service = share_service
        self.transfer_executor = transfer_executor
        self.progress = progress or ProgressReporter()
        self.error_notifier = error_notifier
        self.batch_size = batch_size

    @staticmethod
    def new_stats():
        return {
            "completed_count": 0,
            "transfer_success_count": 0,
            "skipped_dir_count": 0,
            "failed_count": 0,
            "transferred_files": [],
            "transfer_failed_files": [],
        }

    def build_result(self, stats):
        completed_count = stats["completed_count"]
        transfer_success_count = stats["transfer_success_count"]
        skipped_dir_count = stats["skipped_dir_count"]
        failed_count = stats["failed_count"]
        transferred_files = stats["transferred_files"]
        transfer_failed_files = stats.get("transfer_failed_files", [])
        transfer_failed_count = len(transfer_failed_files)

        if transfer_success_count and failed_count:
            message = (
                f"目录分治转存部分成功，成功 {completed_count} 项，"
                f"失败 {failed_count} 项，跳过 {skipped_dir_count} 个目录"
            )
            self.progress.report("warning", message)
            return {
                "success": False,
                "partial": True,
                "divide_path": True,
                "message": message,
                "error": message,
                "transferred_files": transferred_files,
                "transfer_failed_files": transfer_failed_files,
                "transfer_failed_count": transfer_failed_count,
                "completed_count": completed_count,
                "transfer_success_count": transfer_success_count,
                "skipped_dir_count": skipped_dir_count,
                "failed_count": failed_count,
                "rename_failed_count": 0,
            }

        if transfer_success_count:
            message = (
                f"目录分治转存完成，成功 {completed_count} 项，跳过 {skipped_dir_count} 个目录"
            )
            self.progress.report("success", message)
            return {
                "success": True,
                "partial": False,
                "divide_path": True,
                "message": message,
                "transferred_files": transferred_files,
                "transfer_failed_files": transfer_failed_files,
                "transfer_failed_count": transfer_failed_count,
                "completed_count": completed_count,
                "transfer_success_count": transfer_success_count,
                "skipped_dir_count": skipped_dir_count,
                "failed_count": failed_count,
                "rename_failed_count": 0,
            }

        if failed_count:
            error = f"目录分治转存失败，失败 {failed_count} 项，跳过 {skipped_dir_count} 个目录"
            self.progress.report("error", error)
            return {
                "success": False,
                "partial": False,
                "divide_path": True,
                "error": error,
                "transfer_failed_files": transfer_failed_files,
                "transfer_failed_count": transfer_failed_count,
                "completed_count": completed_count,
                "transfer_success_count": transfer_success_count,
                "skipped_dir_count": skipped_dir_count,
                "failed_count": failed_count,
                "rename_failed_count": 0,
            }

        message = "目录分治转存没有可转存内容"
        if skipped_dir_count:
            message = f"{message}，跳过 {skipped_dir_count} 个目录"
        self.progress.report("info", message)
        return {
            "success": True,
            "partial": False,
            "divide_path": True,
            "skipped": True,
            "message": message,
            "completed_count": completed_count,
            "transfer_success_count": transfer_success_count,
            "skipped_dir_count": skipped_dir_count,
            "failed_count": failed_count,
            "rename_failed_count": 0,
        }

    @staticmethod
    def _successful_item_path(item):
        try:
            return item[3]
        except (TypeError, KeyError, IndexError):
            return getattr(item, "final_path", getattr(item, "clean_path", item))

    def flush_file_batch(self, file_transfer_list, target_dir, context, share_url, stats):
        if not file_transfer_list:
            return

        success_count, successful_items, failed_items = (
            self.transfer_executor.execute_transfer_plan(
                file_transfer_list,
                share_url,
                context["uk"],
                context["share_id"],
                context["bdstoken"],
                target_dir,
            )
        )
        failed_items = failed_items or []
        stats["transfer_success_count"] += success_count
        stats["completed_count"] += success_count
        stats["transferred_files"].extend(
            self._successful_item_path(item) for item in successful_items
        )
        stats["transfer_failed_files"].extend(failed_items)
        stats["failed_count"] += len(failed_items)
        file_transfer_list.clear()

    def initialize_frame(self, frame, context, stats):
        if not self.path_service.ensure_dir_exists(frame.target_dir):
            stats["failed_count"] += 1
            self.progress.report("error", f"创建目录失败: {frame.target_dir}")
            return False

        self.progress.report("info", f"目录分治扫描: {frame.shared_dir_path}")

        try:
            frame.child_iter = iter(
                self.share_service.iter_shared_dir_children(
                    frame.shared_dir,
                    context["uk"],
                    context["share_id"],
                    context["bdstoken"],
                )
            )
        except Exception as exc:
            stats["failed_count"] += 1
            if self.error_notifier:
                self.error_notifier(
                    exc,
                    f"目录分治列目录失败: {frame.shared_dir_path}",
                    collect=True,
                )
            return False
        return True

    def finish_frame(self, frame, context, share_url, stats):
        self.flush_file_batch(
            frame.file_transfer_list,
            frame.target_dir,
            context,
            share_url,
            stats,
        )
        self.progress.report(
            "info",
            f"目录分治扫描完成: {frame.shared_dir_path}，处理 {frame.child_count} 个子项",
        )

    def handle_iter_error(self, frame, context, share_url, stats, exc):
        self.flush_file_batch(
            frame.file_transfer_list,
            frame.target_dir,
            context,
            share_url,
            stats,
        )
        stats["failed_count"] += 1
        if self.error_notifier:
            self.error_notifier(
                exc,
                f"目录分治列目录失败: {frame.shared_dir_path}",
                collect=True,
            )

    def handle_file_child(self, frame, child, context, share_url, stats):
        if not (child.get("is_file") and child.get("fs_id")):
            return False
        frame.file_transfer_list.append(
            TransferItem(
                child["fs_id"],
                frame.target_dir,
                child["name"],
                child["name"],
                False,
                child.get("md5"),
            )
        )
        if len(frame.file_transfer_list) >= self.batch_size:
            self.flush_file_batch(
                frame.file_transfer_list,
                frame.target_dir,
                context,
                share_url,
                stats,
            )
        return True

    def handle_dir_child(
        self,
        stack,
        frame,
        child,
        context,
        share_url,
        exclude_folder_filter,
        stats,
    ):
        self.flush_file_batch(
            frame.file_transfer_list,
            frame.target_dir,
            context,
            share_url,
            stats,
        )

        folder_name = child.get("name") or os.path.basename(str(child.get("path", "")).rstrip("/"))
        child_target_dir = posixpath.join(frame.target_dir, folder_name)
        if should_exclude_folder(folder_name, exclude_folder_filter):
            stats["skipped_dir_count"] += 1
            self.progress.report("info", f"跳过排除目录: {folder_name}")
            return
        if exclude_folder_filter:
            stack.append(DirTreeFrame(child["raw"], child_target_dir))
            return

        try:
            self.transfer_executor.transfer_group(
                frame.target_dir,
                [child["fs_id"]],
                share_url,
                context["uk"],
                context["share_id"],
                context["bdstoken"],
            )
            stats["transfer_success_count"] += 1
            stats["completed_count"] += 1
            stats["transferred_files"].append(folder_name)
            clear_cache = getattr(self.transfer_executor, "clear_local_files_cache", None)
            if clear_cache:
                clear_cache(frame.target_dir, {folder_name})
        except Exception as exc:
            if is_transfer_count_limit_error(exc):
                self.progress.report("warning", f"子目录超量，继续拆分: {folder_name}")
                stack.append(DirTreeFrame(child["raw"], child_target_dir))
            else:
                stats["failed_count"] += 1
                error_info = classify_storage_error(exc)
                self.progress.report(
                    "error", f"转存子目录失败: {folder_name} - {error_info.message}"
                )
                if self.error_notifier:
                    self.error_notifier(
                        exc,
                        f"转存子目录失败: {folder_name}",
                        collect=True,
                    )

    def collect(
        self,
        shared_dir,
        target_dir,
        context,
        share_url,
        exclude_folder_filter,
        stats,
    ):
        stack = [DirTreeFrame(shared_dir, target_dir)]
        while stack:
            frame = stack[-1]

            if frame.child_iter is None:
                if not self.initialize_frame(frame, context, stats):
                    stack.pop()
                    continue

            try:
                child = next(frame.child_iter)
            except StopIteration:
                self.finish_frame(frame, context, share_url, stats)
                stack.pop()
                continue
            except Exception as exc:
                self.handle_iter_error(frame, context, share_url, stats, exc)
                stack.pop()
                continue

            frame.child_count += 1
            if self.handle_file_child(frame, child, context, share_url, stats):
                continue
            if child.get("is_dir") and child.get("fs_id"):
                self.handle_dir_child(
                    stack,
                    frame,
                    child,
                    context,
                    share_url,
                    exclude_folder_filter,
                    stats,
                )

    def traverse(
        self,
        shared_dir,
        target_dir,
        context,
        share_url,
        exclude_folder_filter=None,
    ):
        stats = self.new_stats()
        folder_name = os.path.basename(str(getattr(shared_dir, "path", shared_dir)).rstrip("/"))
        if should_exclude_folder(folder_name, exclude_folder_filter):
            stats["skipped_dir_count"] = 1
            self.progress.report("info", f"跳过排除目录: {folder_name}")
            return self.build_result(stats)

        self.collect(
            shared_dir,
            target_dir,
            context,
            share_url,
            exclude_folder_filter,
            stats,
        )
        return self.build_result(stats)


__all__ = ["DirTreeTraverser"]
