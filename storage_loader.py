#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分享链接加载器。"""

from storage_progress import ProgressReporter
from utils import mask_share_url


class ShareLoader:
    def __init__(self, share_service, progress=None, error_notifier=None):
        self.share_service = share_service
        self.progress = progress or ProgressReporter()
        self.error_notifier = error_notifier

    def load_entries(self, share_url, pwd=None):
        """Return the same base context shape as BaiduStorage._load_share_entries()."""
        masked_share_url = mask_share_url(share_url) or share_url
        self.progress.report("info", f"【步骤1/4】访问分享链接: {masked_share_url}")
        if pwd:
            self.progress.report("info", "使用密码访问分享链接")

        shared_paths = self.share_service.load_shared_paths(share_url, pwd)
        if not shared_paths:
            error = ValueError("获取分享文件列表失败")
            if self.error_notifier:
                self.error_notifier(error, "获取分享文件列表失败", collect=True)
            return None

        return {
            "shared_paths": shared_paths,
            "uk": shared_paths[0].uk,
            "share_id": shared_paths[0].share_id,
            "bdstoken": shared_paths[0].bdstoken,
        }

    def load_files(self, context, folder_filter=None, exclude_folder_filter=None):
        """Return a copied context with shared_files_info added."""
        self.progress.report("info", "开始获取共享文件列表")
        shared_files_info = self.share_service.list_shared_files(
            context["shared_paths"],
            folder_filter,
            self.progress.report,
            exclude_folder_filter=exclude_folder_filter,
        )
        self.progress.report("info", f"获取到 {len(shared_files_info)} 个共享文件")

        copied_context = dict(context)
        copied_context["shared_files_info"] = shared_files_info
        return copied_context

    def load_context(
        self,
        share_url,
        pwd=None,
        folder_filter=None,
        exclude_folder_filter=None,
    ):
        """Return the same context shape as BaiduStorage._load_share_context()."""
        context = self.load_entries(share_url, pwd)
        if not context:
            return None
        return self.load_files(
            context,
            folder_filter,
            exclude_folder_filter=exclude_folder_filter,
        )


__all__ = ["ShareLoader"]
