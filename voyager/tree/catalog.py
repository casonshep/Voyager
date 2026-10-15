"""Verification predicate catalog, Python side (PLAN.md section 3).

The predicates are implemented in Node (``lib/verify.js``). This module is
the matching table: names, params and one-line meanings for prompts, a
validator that behaves like the Node one, spec constructors, and a small
local evaluator over a facts dict so trees can be checked offline (tests,
build-time done flags without a bot).
"""

from __future__ import annotations

import re
from typing import Any

from .schema import VerifySpec

COMBINATORS = ("all", "any", "not")

# Tool materials from weakest to strongest. Golden tools mine at wooden tier.
TOOL_TIERS = ["wooden", "stone", "iron", "diamond", "netherite"]
TIER_ALIASES = {"golden": "wooden", "gold": "wooden", "wood": "wooden"}

# name -> {"params": {param: type}, "optional": {param: default}, "meaning": str}
PREDICATES: dict[str, dict[str, Any]] = {
    "has_item": {
        "params": {"name": "item", "count": "int"},
        "optional": {"count": 1},
        "meaning": "inventory holds at least `count` of item `name`",
    },
    "has_equipped": {
        "params": {"name": "item", "slot": "slot"},
        "optional": {},
        "meaning": "item `name` is in equipment slot `slot` (hand, offhand, head, chest, legs, feet)",
    },
    "has_tool_tier": {
        "params": {"tool": "tool", "material": "material"},
        "optional": {},
        "meaning": "holds a `tool` (pickaxe, axe, shovel, sword, hoe) of `material` or a stronger material",
    },
    "near_block": {
        "params": {"name": "block", "radius": "int"},
        "optional": {"radius": 4},
        "meaning": "a block named `name` exists within `radius` blocks of the bot",
    },
    "block_placed_by_me": {
        "params": {"name": "block"},
        "optional": {},
        "meaning": "the bot placed a `name` block during this run and it is still in the world",
    },
    "near_entity": {
        "params": {"name": "entity", "radius": "int"},
        "optional": {"radius": 8},
        "meaning": "an entity of type `name` (cow, villager, zombie...) is within `radius` blocks",
    },
    "in_biome": {
        "params": {"name": "biome"},
        "optional": {},
        "meaning": "the bot stands in biome `name`",
    },
    "at_y_level": {
        "params": {"min": "int", "max": "int"},
        "optional": {},
        "meaning": "the bot's y coordinate is between `min` and `max` inclusive",
    },
    "position_within": {
        "params": {"x": "int", "z": "int", "radius": "int"},
        "optional": {"radius": 3},
        "meaning": "the bot is within `radius` blocks (horizontal) of (`x`, `z`)",
    },
    "status_at_least": {
        "params": {"field": "status_field", "min": "number"},
        "optional": {},
        "meaning": "`field` (health, food, xp) is at least `min`",
    },
    "time_of_day_in": {
        "params": {"min": "int", "max": "int"},
        "optional": {},
        "meaning": "world time (0-24000, night from about 13000) is between `min` and `max`",
    },
    "is_sheltered": {
        "params": {},
        "optional": {},
        "meaning": "a solid block is overhead and at most one side of the bot is open",
    },
    "container_contains": {
        "params": {"pos": "pos", "name": "item", "count": "int"},
        "optional": {"count": 1},
        "meaning": "the container at `pos` {x,y,z} holds at least `count` of `name`",
    },
    "block_count_in_region": {
        "params": {"name": "block", "region": "region", "count": "int"},
        "optional": {},
        "meaning": "at least `count` blocks named `name` lie inside `region` {min:{x,y,z}, max:{x,y,z}}",
    },
}

FAMILIES = {
    "has": ["has_item", "has_equipped", "has_tool_tier"],
    "near": ["near_block", "block_placed_by_me", "near_entity", "container_contains", "block_count_in_region"],
    "status": ["status_at_least", "time_of_day_in", "is_sheltered"],
    "location": ["in_biome", "at_y_level", "position_within"],
}

SLOTS = ("hand", "offhand", "head", "chest", "legs", "feet")
STATUS_FIELDS = ("health", "food", "xp", "oxygen")
TOOLS = ("pickaxe", "axe", "shovel", "sword", "hoe")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def validate(spec: Any, path: str = "spec") -> list[str]:
    """Return a list of problems; empty means the spec is valid."""
    if isinstance(spec, VerifySpec):
        spec = spec.spec
    errors: list[str] = []
    if not isinstance(spec, dict) or len(spec) != 1:
        return [f"{path}: must be an object with exactly one key"]
    op, body = next(iter(spec.items()))
    if op in ("all", "any"):
        if not isinstance(body, list) or not body:
            return [f"{path}.{op}: must be a non-empty list of specs"]
        for i, child in enumerate(body):
            errors.extend(validate(child, f"{path}.{op}[{i}]"))
        return errors
    if op == "not":
        return validate(body, f"{path}.not")
    if op not in PREDICATES:
        return [f"{path}: unknown predicate '{op}'"]
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return [f"{path}.{op}: params must be an object"]
    meta = PREDICATES[op]
    for key in body:
        if key not in meta["params"]:
            errors.append(f"{path}.{op}: unknown param '{key}'")
    for key, kind in meta["params"].items():
        if key not in body:
            if key in meta["optional"]:
                continue
            errors.append(f"{path}.{op}: missing param '{key}'")
            continue
        err = _check_type(body[key], kind)
        if err:
            errors.append(f"{path}.{op}.{key}: {err}")
    return errors


def _check_type(value: Any, kind: str) -> str | None:
    if kind in ("int",):
        return None if isinstance(value, int) and not isinstance(value, bool) else "must be an integer"
    if kind == "number":
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) else "must be a number"
    if kind in ("item", "block", "entity", "biome"):
        # '*_log' is a family wildcard (any log); Node's matcherFor understands it
        bare = value[2:] if isinstance(value, str) and value.startswith("*_") else value
        return None if isinstance(bare, str) and NAME_RE.match(bare) else "must be a lowercase snake_case name"
    if kind == "slot":
        return None if value in SLOTS else f"must be one of {SLOTS}"
    if kind == "tool":
        return None if value in TOOLS else f"must be one of {TOOLS}"
    if kind == "material":
        return None if value in TOOL_TIERS or value in TIER_ALIASES else f"must be one of {TOOL_TIERS}"
    if kind == "status_field":
        return None if value in STATUS_FIELDS else f"must be one of {STATUS_FIELDS}"
    if kind == "pos":
        ok = isinstance(value, dict) and all(isinstance(value.get(k), int) for k in ("x", "y", "z"))
        return None if ok else "must be {x, y, z} integers"
    if kind == "region":
        ok = (
            isinstance(value, dict)
            and all(isinstance(value.get(side), dict) for side in ("min", "max"))
            and all(isinstance(value[side].get(k), int) for side in ("min", "max") for k in ("x", "y", "z"))
        )
        return None if ok else "must be {min:{x,y,z}, max:{x,y,z}}"
    return None


def is_valid(spec: Any) -> bool:
    return not validate(spec)


def normalize(spec: Any) -> VerifySpec:
    """Fill optional params with their defaults; raises ValueError when invalid."""
    errors = validate(spec)
    if errors:
        raise ValueError("; ".join(errors))
    raw = spec.spec if isinstance(spec, VerifySpec) else spec

    def fill(s: dict[str, Any]) -> dict[str, Any]:
        op, body = next(iter(s.items()))
        if op in ("all", "any"):
            return {op: [fill(c) for c in body]}
        if op == "not":
            return {"not": fill(body)}
        params = dict(body or {})
        for key, default in PREDICATES[op]["optional"].items():
            params.setdefault(key, default)
        return {op: params}

    return VerifySpec(fill(raw))


# --------------------------------------------------------------------------- #
# constructors
# --------------------------------------------------------------------------- #
def count_matching(inventory: dict[str, int], name: str) -> int:
    """Inventory count for ``name``; a '*_log' wildcard sums every *_log (mirror of Node's matcherFor)."""
    if isinstance(name, str) and name.startswith("*_"):
        suffix = name[2:]
        return sum(int(c) for n, c in (inventory or {}).items() if n == suffix or n.endswith(f"_{suffix}"))
    return int((inventory or {}).get(name, 0))


def item_spec(name: str, count: int = 1) -> VerifySpec:
    return VerifySpec({"has_item": {"name": name, "count": int(count)}})


def near_spec(block: str, radius: int = 4) -> VerifySpec:
    return VerifySpec({"near_block": {"name": block, "radius": int(radius)}})


def placed_spec(block: str) -> VerifySpec:
    return VerifySpec({"block_placed_by_me": {"name": block}})


def tool_tier_spec(tool: str, material: str) -> VerifySpec:
    return VerifySpec({"has_tool_tier": {"tool": tool, "material": material}})


def near_entity_spec(name: str, radius: int = 8) -> VerifySpec:
    return VerifySpec({"near_entity": {"name": name, "radius": int(radius)}})


def biome_spec(name: str) -> VerifySpec:
    return VerifySpec({"in_biome": {"name": name}})


def position_spec(x: int, z: int, radius: int = 3) -> VerifySpec:
    return VerifySpec({"position_within": {"x": int(x), "z": int(z), "radius": int(radius)}})


def status_spec(field: str, minimum: float) -> VerifySpec:
    return VerifySpec({"status_at_least": {"field": field, "min": minimum}})


def all_of(*specs: VerifySpec | dict) -> VerifySpec:
    return VerifySpec.all_of(*specs)


def any_of(*specs: VerifySpec | dict) -> VerifySpec:
    return VerifySpec.any_of(*specs)


# --------------------------------------------------------------------------- #
# prompt text
# --------------------------------------------------------------------------- #
def describe_catalog(families: list[str] | None = None) -> str:
    """One line per predicate for GPT and Jev prompts; optionally only some families."""
    names = (
        [n for fam in families for n in FAMILIES.get(fam, [])] if families else list(PREDICATES)
    )
    lines = []
    for name in names:
        meta = PREDICATES[name]
        params = ", ".join(
            f"{p}: {t}" + (" (optional)" if p in meta["optional"] else "") for p, t in meta["params"].items()
        )
        lines.append(f"- {name}({params}): {meta['meaning']}")
    lines.append("- all([spec, ...]) / any([spec, ...]) / not(spec): combinators")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# local evaluation over a facts dict (offline; Node is the authority in-game)
# --------------------------------------------------------------------------- #
def tool_tier_of(item_name: str, tool: str) -> int | None:
    """Tier index of an item like 'stone_pickaxe' for `tool`, or None if it is not that tool."""
    if not item_name.endswith(f"_{tool}"):
        return None
    material = item_name[: -len(tool) - 1]
    material = TIER_ALIASES.get(material, material)
    return TOOL_TIERS.index(material) if material in TOOL_TIERS else None


def evaluate(spec: Any, facts: dict[str, Any]) -> bool:
    """Evaluate a spec against plain facts.

    ``facts`` may hold: ``inventory`` {name: count}, ``slots`` {slot: item},
    ``nearby_blocks`` {name: distance}, ``placed_by_me`` {name: pos},
    ``nearby_entities`` {name: distance}, ``biome``, ``position`` {x,y,z},
    ``status`` {health, food, xp}, ``time_of_day``, ``sheltered`` (bool),
    ``containers`` {"x,y,z": {name: count}}, ``region_counts`` {name: count}.
    Missing facts make the predicate false.
    """
    s = spec.spec if isinstance(spec, VerifySpec) else spec
    op, body = next(iter(s.items()))
    if op == "all":
        return all(evaluate(c, facts) for c in body)
    if op == "any":
        return any(evaluate(c, facts) for c in body)
    if op == "not":
        return not evaluate(body, facts)
    p = dict(PREDICATES[op]["optional"])
    p.update(body or {})
    inv = facts.get("inventory") or {}
    if op == "has_item":
        return count_matching(inv, p["name"]) >= p["count"]
    if op == "has_equipped":
        return (facts.get("slots") or {}).get(p["slot"]) == p["name"]
    if op == "has_tool_tier":
        want = TOOL_TIERS.index(TIER_ALIASES.get(p["material"], p["material"]))
        held = list(inv.keys()) + [v for v in (facts.get("slots") or {}).values() if v]
        return any((tool_tier_of(n, p["tool"]) or -1) >= want for n in held)
    if op == "near_block":
        d = (facts.get("nearby_blocks") or {}).get(p["name"])
        return d is not None and d <= p["radius"]
    if op == "block_placed_by_me":
        return p["name"] in (facts.get("placed_by_me") or {})
    if op == "near_entity":
        d = (facts.get("nearby_entities") or {}).get(p["name"])
        return d is not None and d <= p["radius"]
    if op == "in_biome":
        return facts.get("biome") == p["name"]
    if op == "at_y_level":
        pos = facts.get("position")
        return bool(pos) and p["min"] <= pos["y"] <= p["max"]
    if op == "position_within":
        pos = facts.get("position")
        return bool(pos) and ((pos["x"] - p["x"]) ** 2 + (pos["z"] - p["z"]) ** 2) ** 0.5 <= p["radius"]
    if op == "status_at_least":
        return (facts.get("status") or {}).get(p["field"], -1) >= p["min"]
    if op == "time_of_day_in":
        t = facts.get("time_of_day")
        return t is not None and p["min"] <= t <= p["max"]
    if op == "is_sheltered":
        return bool(facts.get("sheltered"))
    if op == "container_contains":
        key = f"{p['pos']['x']},{p['pos']['y']},{p['pos']['z']}"
        return (facts.get("containers") or {}).get(key, {}).get(p["name"], 0) >= p["count"]
    if op == "block_count_in_region":
        return (facts.get("region_counts") or {}).get(p["name"], 0) >= p["count"]
    return False


def item_progress(spec: Any, inventory: dict[str, int]) -> dict[str, int] | None:
    """{have, need} for the first has_item predicate in a spec, or None."""
    for name, params in VerifySpec.from_json(spec.spec if isinstance(spec, VerifySpec) else spec).predicates():
        if name == "has_item":
            need = int(params.get("count", 1))
            return {"have": min(count_matching(inventory, params["name"]), need), "need": need}
    return None
