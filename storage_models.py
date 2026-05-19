#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转存阶段共享的数据模型。"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True, eq=False)
class TransferItem:
    """单个文件的转存计划。

    保留对 5 元 tuple 的序列兼容性，方便老调用方按位置解包：
        fs_id, dir_path, clean_path, final_path, need_rename = item
    """

    fs_id: int
    dir_path: str
    clean_path: str
    final_path: str
    need_rename: bool
    src_md5: str = None
    _payload: tuple = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(
            self,
            "_payload",
            (
                self.fs_id,
                self.dir_path,
                self.clean_path,
                self.final_path,
                self.need_rename,
            ),
        )

    def as_tuple(self):
        return self._payload

    def __iter__(self):
        return iter(self.as_tuple())

    def __len__(self):
        return 5

    def __getitem__(self, index):
        return self.as_tuple()[index]

    def count(self, value):
        return self.as_tuple().count(value)

    def index(self, value, *args):
        return self.as_tuple().index(value, *args)

    def __eq__(self, other):
        if isinstance(other, TransferItem):
            return self.as_tuple() == other.as_tuple()
        if isinstance(other, tuple):
            return self.as_tuple() == other
        return False

    def __hash__(self):
        return hash(self.as_tuple())


@dataclass(frozen=True)
class TransferResult:
    success: bool
    partial: bool = False
    message: str = ""
    transferred_files: list = field(default_factory=list)
    failed_files: list = field(default_factory=list)
    skipped_count: int = 0
    error_details: Optional[str] = None

    def __post_init__(self):
        if self.success and self.failed_files:
            raise ValueError("TransferResult 状态不一致: success=True 时 failed_files 必须为空")

    def to_dict(self):
        result = {
            "success": self.success,
            "partial": self.partial,
            "message": self.message,
            "transferred_files": list(self.transferred_files),
            "transfer_failed_files": list(self.failed_files),
            "transfer_failed_count": len(self.failed_files),
            "rename_failed_files": [],
            "rename_failed_count": 0,
            "completed_count": len(self.transferred_files),
            "transfer_success_count": len(self.transferred_files),
            "skipped_count": self.skipped_count,
        }
        if self.error_details is not None:
            result["error"] = self.error_details
        return result

    @classmethod
    def from_dict(cls, data):
        required_keys = (
            "success",
            "partial",
            "message",
            "transferred_files",
            "transfer_failed_files",
            "skipped_count",
        )
        for key in required_keys:
            if key not in data:
                raise KeyError(key)
        return cls(
            success=data["success"],
            partial=data["partial"],
            message=data["message"],
            transferred_files=list(data["transferred_files"]),
            failed_files=list(data["transfer_failed_files"]),
            skipped_count=data["skipped_count"],
            error_details=data.get("error"),
        )


class TransferResultBuilder:
    def __init__(self):
        self._transferred_files = []
        self._failed_files = []
        self._skipped_count = 0
        self._message = ""
        self._error_details = None
        self._partial = False

    def add_transferred(self, file_info):
        self._transferred_files.append(file_info)
        return self

    def add_failed(self, file_info):
        self._failed_files.append(file_info)
        return self

    def set_skipped(self, count):
        self._skipped_count = count
        return self

    def set_message(self, message):
        self._message = message
        return self

    def set_error(self, details):
        self._error_details = details
        return self

    def set_partial(self, partial=True):
        self._partial = partial
        return self

    def build(self):
        success = not self._partial and not self._error_details and not self._failed_files
        return TransferResult(
            success=success,
            partial=self._partial,
            message=self._message,
            transferred_files=list(self._transferred_files),
            failed_files=list(self._failed_files),
            skipped_count=self._skipped_count,
            error_details=self._error_details,
        )


@dataclass
class DirTreeFrame:
    """目录分治转存的栈帧。"""

    shared_dir: object
    target_dir: str
    child_iter: object = None
    child_count: int = 0
    file_transfer_list: list = field(default_factory=list)

    @property
    def shared_dir_path(self):
        return getattr(self.shared_dir, "path", self.shared_dir)


# 历史名称兼容：storage.py 内原本叫 `_DirTreeFrame`
_DirTreeFrame = DirTreeFrame
