#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转存阶段共享的数据模型。"""

from dataclasses import dataclass, field


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
