#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进度回调的空对象封装。"""


class ProgressReporter:
    def __init__(self, callback=None):
        self._callback = callback

    def report(self, level, message):
        if self._callback is not None:
            self._callback(level, message)


__all__ = ["ProgressReporter"]
