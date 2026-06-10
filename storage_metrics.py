#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import logging
import os
import time

metrics_logger = logging.getLogger("transfershare.storage.metrics")
_TRUE_VALUES = {"1", "true", "yes", "on"}


def storage_metrics_enabled() -> bool:
    return os.getenv("TRANSFERSHARE_STORAGE_METRICS", "").strip().lower() in _TRUE_VALUES


def emit_storage_metric(event: str, **fields):
    try:
        if not storage_metrics_enabled():
            return None
        payload = {
            "event": event,
            "ts_ms": int(time.time() * 1000),
            **fields,
        }
        metrics_logger.info(json.dumps(payload, ensure_ascii=False, default=str))
        return payload
    except Exception:
        return None
