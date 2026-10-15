"""Guarded wrapper around the TypeSafe SDK.

``JevClient`` never raises into Voyager's control flow: when disabled, or when
a request fails, ``ask`` returns ``None`` and callers fall back to current
behaviour. Every successful call is appended as one JSON line to
``<ckpt_dir>/typesafe/<gate>.jsonl`` so judgments can be audited offline.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import voyager.utils as U

try:  # The SDK is optional at import time so Voyager runs without it.
    from typesafe_sdk import RetryPolicy, TypeSafeClient, TypeSafeError

    _SDK_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - depends on environment
    TypeSafeClient = None  # type: ignore[assignment]
    RetryPolicy = None  # type: ignore[assignment]
    TypeSafeError = Exception  # type: ignore[assignment,misc]
    _SDK_IMPORT_ERROR = exc


def answers_to_dict(response: Any) -> dict[str, Any]:
    """Flatten a SystemOneResponse into plain floats/strings for logging."""
    out: dict[str, Any] = {}
    for key, ans in (getattr(response, "nouls", None) or {}).items():
        out[key] = {"type": "noul", "noul": ans.noul}
    for key, ans in (getattr(response, "choices", None) or {}).items():
        out[key] = {
            "type": "choice",
            "choice": ans.choice,
            "confidence": ans.confidence,
            "probabilities": dict(ans.probabilities or {}),
        }
    for key, ans in (getattr(response, "scores", None) or {}).items():
        out[key] = {
            "type": "score",
            "score": ans.score,
            "confidence": ans.confidence,
            "probabilities": list(ans.probabilities or []),
        }
    return out


class JevClient:
    def __init__(
        self,
        *,
        enabled: bool = True,
        model: str = "jev-latest",
        timeout: float = 20.0,
        max_retries: int = 2,
        ckpt_dir: str = "ckpt",
    ):
        self.model = model
        self.ckpt_dir = ckpt_dir
        self.log_dir = f"{ckpt_dir}/typesafe"
        self.iteration: int | None = None
        self._client = None
        self.disabled_reason: str | None = None

        if not enabled:
            self.disabled_reason = "typesafe_enabled=False"
        elif _SDK_IMPORT_ERROR is not None:
            self.disabled_reason = f"typesafe-sdk import failed: {_SDK_IMPORT_ERROR}"
        elif not os.environ.get("TYPESAFE_API_KEY"):
            self.disabled_reason = "TYPESAFE_API_KEY is not set"
        else:
            try:
                self._client = TypeSafeClient(
                    model=model,
                    timeout=timeout,
                    retry=RetryPolicy(max_retries=max_retries, timeout=timeout),
                )
            except TypeSafeError as exc:
                self.disabled_reason = f"TypeSafe client creation failed: {exc}"
            except Exception as exc:  # defensive: never block startup
                self.disabled_reason = f"TypeSafe client creation failed: {exc}"

        if self.enabled:
            U.f_mkdir(self.log_dir)
            print(f"\033[33mTypeSafe gates enabled (model={model}); logging to {self.log_dir}\033[0m")
        else:
            print(f"\033[33mTypeSafe gates disabled: {self.disabled_reason}\033[0m")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def ask(
        self,
        gate: str,
        state: Any,
        questions: dict[str, Any],
        extra_record: dict[str, Any] | None = None,
    ):
        """Run one System One request. Returns the SDK response or ``None``."""
        if not self.enabled or not questions:
            return None
        started = time.time()
        try:
            response = self._client.system_one(state, questions)
        except Exception as exc:
            print(f"\033[31mTypeSafe gate '{gate}' failed: {type(exc).__name__}: {exc}\033[0m")
            self._append(
                gate,
                {
                    "ts": started,
                    "iteration": self.iteration,
                    "gate": gate,
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": round((time.time() - started) * 1000),
                    "state": state,
                    "question_ids": list(questions.keys()),
                    "extra": extra_record or {},
                },
            )
            return None

        self._append(
            gate,
            {
                "ts": started,
                "iteration": self.iteration,
                "gate": gate,
                "model": getattr(response, "model", None),
                "request_id": getattr(response, "request_id", None),
                "latency_ms": round((time.time() - started) * 1000),
                "usage": _usage_to_dict(getattr(response, "usage", None)),
                "state": state,
                "question_ids": list(questions.keys()),
                "answers": answers_to_dict(response),
                "extra": extra_record or {},
            },
        )
        return response

    def log_only(self, gate: str, record: dict[str, Any]) -> None:
        """Append a record that is not tied to a single request (e.g. a decision summary)."""
        if self.enabled:
            self._append(gate, {"ts": time.time(), "iteration": self.iteration, "gate": gate, **record})

    def _append(self, gate: str, record: dict[str, Any]) -> None:
        try:
            with open(f"{self.log_dir}/{gate}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:  # logging must never break the run
            print(f"\033[31mTypeSafe log write failed: {exc}\033[0m")


def _usage_to_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }
