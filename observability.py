"""Shared observability helpers: structured JSON logging + in-memory metrics.

Standard library only, so there is nothing to install.
"""
import json
import logging
import os
import sys
import threading
import time
from collections import deque

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


class JsonFormatter(logging.Formatter):
    """Formats every log record as one JSON line (easy to grep by request_id)."""

    def __init__(self, service):
        super().__init__()
        self.service = service

    def format(self, record):
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "service": self.service,
            "level": record.levelname,
            "event": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry)


def get_logger(service):
    """Logger that writes JSON lines to stdout AND logs/<service>.log."""
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger(service)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = JsonFormatter(service)
    for handler in (
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, f"{service}.log")),
    ):
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


def log(logger, level, event, **fields):
    logger.log(level, event, extra={"fields": fields})


class Metrics:
    """Thread-safe counters + latency stats.

    Keeps lifetime totals and a sliding window of the most recent requests,
    so recovery is visible quickly (the recent error rate drops back to 0).
    """

    def __init__(self, window=10):
        self._lock = threading.Lock()
        self.total = 0
        self.success = 0
        self.failure = 0
        self.timeouts = 0
        self._all_latencies = []
        self._recent = deque(maxlen=window)  # (ok, latency_ms)

    def record(self, ok, latency_ms, timeout=False):
        with self._lock:
            self.total += 1
            if ok:
                self.success += 1
            else:
                self.failure += 1
            if timeout:
                self.timeouts += 1
            self._all_latencies.append(latency_ms)
            self._recent.append((ok, latency_ms))

    @staticmethod
    def _pct(values, p):
        if not values:
            return 0.0
        s = sorted(values)
        idx = min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))
        return round(s[idx], 1)

    def snapshot(self):
        with self._lock:
            recent_lat = [lat for _, lat in self._recent]
            recent_fail = sum(1 for ok, _ in self._recent if not ok)
            return {
                "requests_total": self.total,
                "requests_success": self.success,
                "requests_failure": self.failure,
                "requests_timeout": self.timeouts,
                "error_rate_total": round(self.failure / self.total, 3) if self.total else 0.0,
                "latency_avg_ms": round(sum(self._all_latencies) / len(self._all_latencies), 1)
                if self._all_latencies else 0.0,
                "latency_p95_ms": self._pct(self._all_latencies, 95),
                f"recent_window_size": len(self._recent),
                "recent_error_rate": round(recent_fail / len(self._recent), 3) if self._recent else 0.0,
                "recent_latency_avg_ms": round(sum(recent_lat) / len(recent_lat), 1) if recent_lat else 0.0,
            }
