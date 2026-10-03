"""Wall-clock instrumentation for the slow parts of Voyager.

Every LLM call and every environment round trip is appended as one JSON line
to ``<ckpt_dir>/timing.jsonl`` and echoed to the console, so the split between
GPT time, Jev time, and Minecraft time can be read off a run instead of guessed.

Usage::

    timing.configure(ckpt_dir)
    llm = ChatOpenAI(..., callbacks=[timing.llm_callback("critic")])
    with timing.timed("env.step"):
        ...
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

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


class LLMTimingCallback(BaseCallbackHandler):
    """Times each LLM request made through a LangChain chat model."""

    def __init__(self, agent: str):
        self.agent = agent
        self._starts: dict[Any, float] = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._starts[run_id] = time.perf_counter()

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        self._starts[run_id] = time.perf_counter()

    def on_llm_end(self, response, *, run_id, **kwargs):
        started = self._starts.pop(run_id, None)
        if started is None:
            return
        model = None
        try:
            model = (response.llm_output or {}).get("model_name")
        except Exception:
            pass
        record(f"llm.{self.agent}", (time.perf_counter() - started) * 1000, model=model)

    def on_llm_error(self, error, *, run_id, **kwargs):
        started = self._starts.pop(run_id, None)
        if started is not None:
            record(f"llm.{self.agent}", (time.perf_counter() - started) * 1000, error=str(error)[:200])


def llm_callback(agent: str) -> LLMTimingCallback:
    return LLMTimingCallback(agent)
