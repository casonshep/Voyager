"""Skill tree for fast mode.

A **skill** is a goal the bot has verified reaching: a node identified by
its goal target (``item`` + count family, ``nearBlock``, or a judged goal).
Every node carries a ``verify`` spec so code can ask Node whether the goal
is already satisfied by the current world. High goals become nodes when
reached, and are connected to the subgoal nodes that were used on the way,
so the tree records *how* each goal was reached from earlier ones:

    Craft a wooden pickaxe  (high)
      +-- Obtain 3 oak_planks (sub)
      +-- Obtain 2 stick      (sub)

Per node, ``routes`` keeps the primitive sequence that worked in each coarse
world fingerprint (biome class, pickaxe tier, day/night) for replay, plus
timing and the tools held. Records are plain dicts in
``<ckpt_dir>/fast/skills.json`` so fields can be added later.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any


import re

# what counts as a tool on a skill record (the held item is otherwise whatever was last used)
TOOL_RE = re.compile(r"_(pickaxe|axe|sword|shovel|hoe)$|^(shears|flint_and_steel|bucket|bow|crossbow|fishing_rod|shield)$")


def fingerprint_key(fingerprint: dict | None) -> str:
    fp = fingerprint or {}
    return f"{fp.get('biome', '?')}|{fp.get('toolTier', '?')}|{fp.get('daylight', '?')}"


def expand(collapsed: list[dict]) -> list[str]:
    out: list[str] = []
    for step in collapsed:
        out.extend([step["action"]] * int(step.get("times", 1)))
    return out


def target_key(target: dict | None) -> str:
    """Stable identity of a goal: what has to be true for it to count as reached."""
    if not target or target.get("none"):
        return "none"
    if target.get("milestone"):
        return f"milestone:{target['milestone']}"
    if target.get("nearBlock"):
        return f"near:{target['nearBlock']}"
    if target.get("dimension"):
        return f"dimension:{target['dimension']}"
    if target.get("nearPlayer"):
        return f"player:{target['nearPlayer']}"
    if target.get("give"):
        return f"give:{target['give'].get('item')}:{target['give'].get('to')}"
    if target.get("judged"):
        return "none"
    return str(target.get("item") or "none")


def verify_spec(target: dict | None) -> dict[str, Any]:
    """How to check this goal against the world. ``judged`` goals need a Jev Noul."""
    if not target or target.get("none"):
        return {"kind": "judged"}
    if target.get("milestone"):
        return {"kind": "milestone", "targets": list(target.get("targets") or [])}
    if target.get("nearBlock"):
        return {"kind": "nearBlock", "block": target["nearBlock"]}
    if target.get("dimension"):
        return {"kind": "dimension", "dimension": target["dimension"]}
    if target.get("nearPlayer"):
        return {"kind": "nearPlayer", "player": target["nearPlayer"], "distance": int(target.get("distance", 3))}
    if target.get("give"):
        g = target["give"]
        return {"kind": "give", "item": g.get("item"), "count": int(g.get("count", 1)), "to": g.get("to")}
    if target.get("judged"):
        return {"kind": "judged"}
    return {"kind": "item", "item": target.get("item"), "count": int(target.get("count", 1))}


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

    # ---- identity ----------------------------------------------------------
    @staticmethod
    def node_id(goal_text: str, target: dict | None) -> str:
        key = target_key(target)
        if key == "none":
            # judged goals are identified by their text
            return "judged:" + " ".join(goal_text.lower().split())
        return key

    def node(self, goal_text: str, target: dict | None) -> dict[str, Any] | None:
        return self.skills.get(self.node_id(goal_text, target))

    def known(self, goal_text: str, target: dict | None) -> bool:
        """Has this goal already been reached as a high goal before?"""
        n = self.node(goal_text, target)
        return bool(n and n.get("kind") == "high")

    # ---- recording -----------------------------------------------------------
    def record(
        self,
        *,
        goal: str,
        target: dict | None,
        kind: str,
        high_goal: str | None,
        fingerprint: dict | None,
        seconds: float,
        trace: list[dict],
        collapsed: list[dict],
        inventory_before: dict | None,
        inventory_after: dict | None,
        children: list[str] | None = None,
    ) -> dict[str, Any]:
        """Store a reached goal as a node (or update it) and return the node."""
        nid = self.node_id(goal, target)
        fk = fingerprint_key(fingerprint)
        tools = sorted({s["tool"] for s in trace if s.get("tool") and TOOL_RE.search(s["tool"])})
        actions = expand(collapsed)
        delta = {}
        for item in sorted(set(inventory_before or {}) | set(inventory_after or {})):
            change = int((inventory_after or {}).get(item, 0)) - int((inventory_before or {}).get(item, 0))
            if change:
                delta[item] = change
        now = time.time()
        node = self.skills.get(nid)
        if node is None:
            node = {
                "id": nid,
                "goal": goal,
                "verify": verify_spec(target),
                "target": target,
                "kind": kind,
                "high_goals": [],
                "children": [],
                "parents": [],
                "routes": {},
                "tools_used": [],
                "seconds": round(seconds, 1),
                "last_seconds": round(seconds, 1),
                "inventory_delta": delta,
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "created": now,
                "last_used": now,
            }
            self.skills[nid] = node
        if kind == "high":
            node["kind"] = "high"  # a subgoal node can be promoted to a learned high goal
        if high_goal and high_goal not in node["high_goals"]:
            node["high_goals"].append(high_goal)
        node["attempts"] += 1
        node["successes"] += 1
        node["tools_used"] = sorted(set(node["tools_used"]) | set(tools))
        node["last_seconds"] = round(seconds, 1)
        node["last_used"] = now
        if not node.get("inventory_delta"):
            node["inventory_delta"] = delta
        route = node["routes"].get(fk)
        if actions and (route is None or len(actions) < len(route["actions"])):
            node["routes"][fk] = {
                "fingerprint": fingerprint,
                "actions": actions,
                "seconds": round(seconds, 1),
                "tools": tools,
            }
            node["seconds"] = round(seconds, 1)
        for child in children or []:
            if child != nid and child in self.skills:
                self.connect(nid, child)
        self._save()
        return node

    def connect(self, parent_id: str, child_id: str) -> None:
        parent = self.skills.get(parent_id)
        child = self.skills.get(child_id)
        if not parent or not child:
            return
        if child_id not in parent["children"]:
            parent["children"].append(child_id)
        if parent_id not in child["parents"]:
            child["parents"].append(parent_id)

    def note_failure(self, goal: str, target: dict | None) -> None:
        node = self.node(goal, target)
        if node:
            node["attempts"] += 1
            node["failures"] += 1
            self._save()

    # ---- lookup ----------------------------------------------------------------
    def route_for(self, target: dict | None, fingerprint: dict | None, goal_text: str = "") -> list[str] | None:
        """Primitive replay for this goal in this situation, if one was learned."""
        node = self.node(goal_text, target)
        if not node:
            return None
        route = node["routes"].get(fingerprint_key(fingerprint))
        return list(route["actions"]) if route else None

    def describe(self, node: dict[str, Any], fingerprint: dict | None) -> str:
        """Short history line for a subgoal candidate note."""
        route = node["routes"].get(fingerprint_key(fingerprint))
        where = "here" if route else "elsewhere"
        tools = ", ".join(node["tools_used"]) or "no tools"
        return (
            f"known skill: reached {node['successes']} time(s), failed {node['failures']}, "
            f"about {node['seconds']}s using {tools}, learned {where}"
        )

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.skills, fh, indent=2)
        except Exception as exc:
            print(f"\033[31mSkillMemory: could not save {self.path}: {exc}\033[0m")
