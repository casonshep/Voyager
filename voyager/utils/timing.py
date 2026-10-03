"""Wall-clock instrumentation for the slow parts of Voyager.

Every LLM call and every environment round trip is appended as one JSON line
to ``<ckpt_dir>/timing.jsonl`` and echoed to the console, so the split between
GPT time, Jev time, and Minecraft time can be read off a run instead of guessed.

Usage::

    timing.configure(ckpt_dir)
    with timing.timed("llm.curriculum"):
        ...
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any


_log_path: str | None = None
_totals: dict[str, list[float]] = {}


def configure(ckpt_dir: str) -> None:
    global _log_path
    os.makedirs(ckpt_dir, exist_ok=True)
    _log_path = os.path.join(ckpt_dir, "timing.jsonl")


def record(name: str, latency_ms: float, **extra: Any) -> None:
    _totals.setdefault(name, []).append(latency_ms)
    print(f"\033[90m[timing] {name}: {latency_ms / 1000:.1f}s\033[0m")
    if _log_path is None:
        return
    try:
        with open(_log_path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"ts": time.time(), "name": name, "latency_ms": round(latency_ms), **extra})
                + "\n"
            )
    except Exception as exc:  # timing must never break a run
        print(f"\033[31mtiming log write failed: {exc}\033[0m")


@contextlib.contextmanager
def timed(name: str, **extra: Any):
    started = time.perf_counter()
    try:
        yield
    finally:
        record(name, (time.perf_counter() - started) * 1000, **extra)


def summary() -> dict[str, dict[str, float]]:
    """Per-name count, total seconds, and mean seconds for the current process."""
    out = {}
    for name, samples in _totals.items():
        total = sum(samples) / 1000
        out[name] = {"count": len(samples), "total_s": round(total, 1), "mean_s": round(total / len(samples), 2)}
    return out
