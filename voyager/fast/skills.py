"""Skills in fast mode: one record per completed subgoal.

A skill is a subgoal that was completed while working toward a high goal.
It stores what was being attempted (high goal and subgoal), how long it
took, which tools were held, and the primitive sequence that worked, keyed
by the coarse world fingerprint it started from (biome class, pickaxe tier,
day/night). Skills are the candidate set the brain offers Jev when choosing
the next subgoal; a bot starts with none and accumulates them as it goes.

Records live in ``<ckpt_dir>/fast/skills.json`` so they can be inspected and
extended later (the record is a plain dict; add fields freely).
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


def target_key(target: dict | None) -> str:
    """Stable key for a goal target override or summary."""
    if not target:
        return "none"
    if target.get("none"):
        return "none"
    if target.get("nearBlock"):
        return f"near:{target['nearBlock']}"
    return str(target.get("item") or "none")


class SkillMemory:
    def __init__(self, path: str):
        self.path = path
        self.skills: dict[str, dict[str, Any]] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.skills = json.load(fh)
            except Exception as exc:
                print(f"\033[31mSkillMemory: could not load {path}: {exc}\033[0m")
                self.skills = {}

    @staticmethod
    def key(target: dict | None, fingerprint: dict | None) -> str:
        return f"{target_key(target)}|{fingerprint_key(fingerprint)}"

    def record(
        self,
        *,
        high_goal: str,
        subgoal: str,
        target: dict | None,
        fingerprint: dict | None,
        seconds: float,
        trace: list[dict],
        collapsed: list[dict],
        inventory_before: dict | None,
        inventory_after: dict | None,
    ) -> dict[str, Any]:
        """Store a completed subgoal. Returns the skill record."""
        key = self.key(target, fingerprint)
        tools = sorted({s["tool"] for s in trace if s.get("tool")})
        actions = expand(collapsed)
        delta = {}
        for item in sorted(set(inventory_before or {}) | set(inventory_after or {})):
            change = int((inventory_after or {}).get(item, 0)) - int((inventory_before or {}).get(item, 0))
            if change:
                delta[item] = change
        existing = self.skills.get(key)
        if existing:
            existing["successes"] += 1
            existing["attempts"] += 1
            existing["high_goals"] = sorted(set(existing.get("high_goals", [])) | {high_goal})
            existing["tools_used"] = sorted(set(existing.get("tools_used", [])) | set(tools))
            existing["last_seconds"] = round(seconds, 1)
            existing["last_used"] = time.time()
            # keep the shortest primitive sequence that worked
            if actions and (not existing.get("actions") or len(actions) < len(existing["actions"])):
                existing["actions"] = actions
                existing["seconds"] = round(seconds, 1)
            skill = existing
        else:
            skill = {
                "id": key,
                "subgoal": subgoal,
                "target": target,
                "high_goal": high_goal,
                "high_goals": [high_goal],
                "seconds": round(seconds, 1),
                "last_seconds": round(seconds, 1),
                "tools_used": tools,
                "actions": actions,
                "fingerprint": fingerprint,
                "inventory_delta": delta,
                "attempts": 1,
                "successes": 1,
                "failures": 0,
                "created": time.time(),
                "last_used": time.time(),
            }
            self.skills[key] = skill
        self._save()
        return skill

    def note_failure(self, target: dict | None, fingerprint: dict | None) -> None:
        skill = self.skills.get(self.key(target, fingerprint))
        if skill:
            skill["attempts"] += 1
            skill["failures"] += 1
            self._save()

    def matching(self, fingerprint: dict | None) -> list[dict[str, Any]]:
        """Skills learned in this coarse situation, most reliable first."""
        fk = fingerprint_key(fingerprint)
        out = [s for s in self.skills.values() if fingerprint_key(s.get("fingerprint")) == fk]
        out.sort(key=lambda s: (-(s["successes"] - s["failures"]), s["seconds"]))
        return out

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.skills, fh, indent=2)
        except Exception as exc:
            print(f"\033[31mSkillMemory: could not save {self.path}: {exc}\033[0m")
