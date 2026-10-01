"""Record-and-replay memory of primitive sequences that reached a goal.

When the fast loop reaches a goal, the collapsed trace of primitives that
worked (``[{"action": "mine:oak_log", "times": 3}, ...]``) is stored under the
goal's target item and the coarse world fingerprint it started from (biome
class, pickaxe tier, day/night). The next time the same target comes up in an
exactly matching fingerprint, the stored actions are handed to the loop as a
replay; the loop falls back to Jev the moment a replay step is unavailable or
fails. Matching is exact on the fingerprint: no similarity search.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any


def fingerprint_key(fingerprint: dict | None) -> str:
    fp = fingerprint or {}
    return f"{fp.get('biome', '?')}|{fp.get('toolTier', '?')}|{fp.get('daylight', '?')}"


def expand(collapsed: list[dict]) -> list[str]:
    out: list[str] = []
    for step in collapsed:
        out.extend([step["action"]] * int(step.get("times", 1)))
    return out


class SequenceMemory:
    def __init__(self, path: str):
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.entries = json.load(fh)
            except Exception as exc:
                print(f"\033[31mSequenceMemory: could not load {path}: {exc}\033[0m")
                self.entries = {}

    @staticmethod
    def key(item: str, fingerprint: dict | None) -> str:
        return f"{item}|{fingerprint_key(fingerprint)}"

    def store(self, item: str, fingerprint: dict | None, collapsed: list[dict], seconds: float) -> None:
        actions = expand(collapsed)
        if not item or not actions:
            return
        key = self.key(item, fingerprint)
        existing = self.entries.get(key)
        # keep the shortest sequence that worked for this situation
        if existing and len(existing["actions"]) <= len(actions):
            existing["successes"] += 1
            existing["last_used"] = time.time()
        else:
            self.entries[key] = {
                "item": item,
                "fingerprint": fingerprint,
                "actions": actions,
                "seconds": round(seconds, 1),
                "successes": 1,
                "replays": 0,
                "last_used": time.time(),
            }
        self._save()

    def lookup(self, item: str, fingerprint: dict | None) -> list[str] | None:
        entry = self.entries.get(self.key(item, fingerprint))
        return list(entry["actions"]) if entry else None

    def library_for(self, fingerprint: dict | None) -> dict[str, list[str]]:
        """Every stored sequence whose fingerprint matches, keyed by target item.

        The fast loop parses the target itself, so Python hands over all
        candidates for the current situation and Node picks the matching one.
        """
        fk = fingerprint_key(fingerprint)
        out: dict[str, list[str]] = {}
        for entry in self.entries.values():
            if fingerprint_key(entry.get("fingerprint")) == fk:
                out[entry["item"]] = list(entry["actions"])
        return out

    def note_replay(self, item: str, fingerprint: dict | None) -> None:
        entry = self.entries.get(self.key(item, fingerprint))
        if entry:
            entry["replays"] += 1
            self._save()

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.entries, fh, indent=2)
        except Exception as exc:
            print(f"\033[31mSequenceMemory: could not save {self.path}: {exc}\033[0m")
