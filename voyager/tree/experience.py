"""Raw experience: decision records and completed-subtask traces.

Two stores, both plain JSON lines, both raw evidence rather than conclusions
(Learn2Play: raw histories keep learning revisable where distilled rules do not):

* ``<ckpt>/decisions.jsonl`` is written by the Node agent loop, one line per
  action: a digest of what the agent saw, the menu, Jev's answer, the action,
  its outcome text, a typed outcome label, the state delta, and timings. Every
  line has an ``id``; lessons, remedies and repairs cite those ids.
  ``read_decisions`` loads it, optionally filtered to one goal.
* ``<library>/traces/<family>.jsonl`` holds the action records of every
  subtask that was completed, keyed by its description. It lives next to the
  learned actions so it carries across runs. ``TraceStore.most_similar``
  returns the single closest prior trace for a subtask (EmbodiedCLBench: one
  exemplar gives most of the benefit; more did not help).

Families group subtasks by what kind of step they are, so lessons and traces
for "find logs" are not mixed with "craft planks".
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Iterable

FAMILIES = {
    "locate": "find",
    "mine": "acquire",
    "kill": "acquire",
    "collect": "acquire",
    "craft": "craft",
    "smelt": "craft",
    "place": "place",
    "equip": "use",
    "eat": "use",
    "use": "use",
}


def family_of(kind: str | None) -> str:
    """The lesson family of a node kind (unknown kinds and plain goals are 'goal')."""
    return FAMILIES.get(str(kind or "").lower(), "goal")


_WORD = re.compile(r"[a-z0-9_*]+")
# step verbs are dropped too: families already separate mining from crafting, and
# "by mining" must not make "logs" look like "cobblestone"
_STOP = {"a", "an", "the", "of", "by", "nearby", "any", "wood", "and", "or", "to", "obtain", "goal", "from",
         "mining", "mine", "craft", "crafting", "smelt", "place", "find", "get", "collect", "equip", "use", "eat"}


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall(str(text or "").lower()) if t not in _STOP and not t.isdigit()}


def similarity(a: str, b: str) -> float:
    """Overlap of the content words of two subtask descriptions, relative to the shorter
    one: "obtain 3 cobblestone" fully matches "obtain 11 cobblestone by mining stone"."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def read_decisions(path: str, goal_id: str | None = None, since: float | None = None) -> list[dict[str, Any]]:
    """Decision records from Node's log, oldest first; bad lines are skipped."""
    out: list[dict[str, Any]] = []
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if goal_id is not None and rec.get("goal_id") != goal_id:
                continue
            if since is not None and (rec.get("ts") or 0) / 1000.0 < since:
                continue
            out.append(rec)
    return out


def compact_record(r: dict[str, Any]) -> dict[str, Any]:
    """The fields of an action record worth keeping in a trace."""
    delta = r.get("delta") or {}
    keep = {k: v for k, v in delta.items() if v not in (0, None, {}, [])}
    return {k: v for k, v in {"id": r.get("id"), "action": r.get("action"), "outcome": str(r.get("outcome") or "")[:200],
                              "label": r.get("label"), "delta": keep}.items() if v not in (None, {}, "")}


def render_trace(records: Iterable[dict[str, Any]], limit: int = 30) -> str:
    """Records as numbered lines: '3. x:mine_log -> ok mined oak_log [OK] (+oak_log 1)'."""
    lines = []
    recs = list(records)[-limit:]
    for i, r in enumerate(recs, 1):
        inv = (r.get("delta") or {}).get("inv") or {}
        gained = ", ".join(f"{'+' if n > 0 else ''}{k} {n}" for k, n in inv.items())
        lines.append(f"{i}. {r.get('action')} -> {str(r.get('outcome') or '')[:140]} [{r.get('label') or '?'}]"
                     + (f" ({gained})" if gained else "") + (f" {{{r.get('id')}}}" if r.get("id") else ""))
    return "\n".join(lines)


class TraceStore:
    """Completed-subtask traces under ``<root>/traces``, one JSONL file per family."""

    def __init__(self, root: str):
        self.dir = os.path.join(root, "traces")
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, family: str) -> str:
        return os.path.join(self.dir, f"{family}.jsonl")

    def save(self, *, description: str, kind: str | None, records: list[dict[str, Any]], goal_id: str | None = None,
             node_id: str | None = None) -> dict[str, Any] | None:
        recs = [compact_record(r) for r in records or [] if isinstance(r, dict) and r.get("action")]
        if not recs:
            return None
        entry = {
            "ts": time.time(),
            "description": description,
            "kind": kind,
            "family": family_of(kind),
            "goal_id": goal_id,
            "node_id": node_id,
            "actions": len(recs),
            "records": recs,
        }
        try:
            with open(self._path(entry["family"]), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            return None
        return entry

    def entries(self, family: str | None = None) -> list[dict[str, Any]]:
        files = [self._path(family)] if family else [os.path.join(self.dir, f) for f in os.listdir(self.dir) if f.endswith(".jsonl")]
        out = []
        for path in files:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def most_similar(self, description: str, kind: str | None = None, min_similarity: float = 0.5) -> dict[str, Any] | None:
        """The closest prior completed trace: same family first, then highest word
        overlap, then fewest actions (the most efficient way it was done)."""
        fam = family_of(kind) if kind else None
        best, best_key = None, None
        for e in self.entries(fam) or (self.entries() if fam else []):
            sim = similarity(description, e.get("description", ""))
            if sim < min_similarity:
                continue
            key = (sim, -int(e.get("actions") or 0))
            if best_key is None or key > best_key:
                best, best_key = e, key
        return best
