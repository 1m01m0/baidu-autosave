#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import patch

from storage_metrics import emit_storage_metric, storage_metrics_enabled


class StorageMetricsTests(unittest.TestCase):
    @patch.dict("os.environ", {}, clear=False)
    def test_metrics_disabled_by_default(self):
        self.assertFalse(storage_metrics_enabled())

    @patch.dict("os.environ", {"TRANSFERSHARE_STORAGE_METRICS": "1"}, clear=False)
    @patch("storage_metrics.metrics_logger")
    def test_emit_storage_metric_logs_json_when_enabled(self, mock_logger):
        payload = emit_storage_metric("shared_page", pages=3, elapsed_ms=12.0)

        self.assertIsNotNone(payload)
        self.assertEqual("shared_page", payload["event"])
        self.assertEqual(3, payload["pages"])
        mock_logger.info.assert_called_once()
        logged_text = mock_logger.info.call_args.args[0]
        self.assertIn('"event": "shared_page"', logged_text)


if __name__ == "__main__":
    unittest.main()
