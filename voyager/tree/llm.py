"""Thin wrapper over the OpenAI SDK (PLAN.md section 10): no langchain.

``LLM.complete`` returns text; ``LLM.complete_json`` returns a parsed dict,
using JSON mode when the model supports it and falling back to extracting
the first JSON object from the reply. Construction never touches the network;
a missing key is reported on first use so offline tests can pass a stub.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

# OPENAI_MODEL (set in .env) wins; the fallback matches it so a missing .env does not
# silently drop the builder to a weak model (the seven-rewrite run predates the switch)
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-6.1-sol")

Message = dict[str, str]


def system(text: str) -> Message:
    return {"role": "system", "content": text}


def user(text: str) -> Message:
    return {"role": "user", "content": text}


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._client = None
        self.calls = 0

    @property
    def client(self):
        if self._client is None:
            if not self._api_key:
                raise LLMError("OPENAI_API_KEY is not set")
            from openai import OpenAI  # imported lazily so tests never need the package configured

            self._client = OpenAI(api_key=self._api_key, timeout=self.timeout, max_retries=self.max_retries)
        return self._client

    def complete(self, messages: list[Message], temperature: float | None = None, json_mode: bool = False) -> str:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        # Some models (reasoning ones) accept only their default temperature; once a
        # model refuses it, stop sending it for the rest of the run.
        if not getattr(self, "_no_temperature", False):
            kwargs["temperature"] = self.temperature if temperature is None else temperature
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        self.calls += 1
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            text = str(exc)
            if "temperature" in kwargs and "temperature" in text and ("unsupported" in text.lower() or "does not support" in text):
                self._no_temperature = True
                kwargs.pop("temperature")
                try:
                    response = self.client.chat.completions.create(**kwargs)
                except Exception as exc2:
                    raise LLMError(f"{type(exc2).__name__}: {exc2}") from exc2
            else:  # surface one error type to callers
                raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        choice = response.choices[0]
        return (choice.message.content or "").strip()

    def complete_json(self, messages: list[Message], schema_hint: str | None = None) -> dict[str, Any]:
        """Ask for a single JSON object. ``schema_hint`` is appended to the last message."""
        msgs = [dict(m) for m in messages]
        hint = "Answer with one JSON object and nothing else."
        if schema_hint:
            hint += f" Shape: {schema_hint}"
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] = f"{msgs[-1]['content']}\n\n{hint}"
        else:
            msgs.append(user(hint))
        try:
            text = self.complete(msgs, json_mode=True)
        except LLMError as exc:
            # some models reject response_format; retry without it
            if "response_format" not in str(exc):
                raise
            text = self.complete(msgs, json_mode=False)
        return parse_json_object(text)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object in ``text`` (tolerates code fences and prose)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError(f"no JSON object in reply: {text[:200]!r}")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise LLMError("reply is JSON but not an object")
    return data
