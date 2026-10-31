"""Aggregate-only metrics. This is the ONLY telemetry in the product and it never leaves the process
unless an operator reads the counters. Nothing per-request is stored: no arguments, no dates queried,
no client identifiers.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

BUCKETS_MS = (1, 2, 5, 10, 20, 50, 100, 500, 1000)


def bucket(ms: float) -> str:
    for b in BUCKETS_MS:
        if ms <= b:
            return f"le_{b}ms"
    return "gt_1000ms"


@dataclass
class _Obs:
    tool: str
    status: str = "ok"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: Counter[tuple[str, str]] = Counter()  # (tool, status)
        self.latency: Counter[tuple[str, str]] = Counter()  # (tool, bucket)
        self.started = time.time()

    @contextmanager
    def observe(self, tool: str) -> Iterator[_Obs]:
        obs = _Obs(tool)
        t0 = time.perf_counter()
        try:
            yield obs
        except Exception:
            if obs.status == "ok":
                obs.status = "error"
            raise
        finally:
            ms = (time.perf_counter() - t0) * 1000
            with self._lock:
                self.calls[(tool, obs.status)] += 1
                self.latency[(tool, bucket(ms))] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "uptime_s": int(time.time() - self.started),
                "calls": {f"{t}.{s}": n for (t, s), n in sorted(self.calls.items())},
                "latency": {f"{t}.{b}": n for (t, b), n in sorted(self.latency.items())},
                "note": "aggregate counters only; no request content or client identifiers are recorded",
            }

    def reset(self) -> None:
        with self._lock:
            self.calls.clear()
            self.latency.clear()


METRICS = Metrics()
