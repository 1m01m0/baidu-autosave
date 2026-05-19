#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from storage_progress import ProgressReporter


class ProgressReporterTests(unittest.TestCase):
    def test_missing_callback_is_noop(self):
        reporter = ProgressReporter()

        reporter.report("info", "开始处理")
        reporter.report("success", "处理完成")

    def test_callback_receives_exact_arguments(self):
        calls = []

        def callback(level, message):
            calls.append((level, message))

        reporter = ProgressReporter(callback)
        reporter.report("warning", "部分成功")

        self.assertEqual([("warning", "部分成功")], calls)

    def test_callback_exception_propagates(self):
        expected_error = RuntimeError("callback failed")

        def callback(level, message):
            raise expected_error

        reporter = ProgressReporter(callback)

        with self.assertRaises(RuntimeError) as cm:
            reporter.report("error", "失败")
        self.assertIs(expected_error, cm.exception)


if __name__ == "__main__":
    unittest.main()
