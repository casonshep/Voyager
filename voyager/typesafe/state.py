"""Shared structured state for Jev judgments.

The three prose renderers (action, critic, curriculum) each flatten the same
observation into a different string. This module builds one JSON-serialisable
dict from the same raw ``events`` so every Jev call sees identical, named
fields. The prose renderers are left untouched so GPT prompts stay
byte-identical for shadow comparisons.
"""

from __future__ import annotations

import copy
from typing import Any

# Blocks whose presence in the nearby voxels means the bot is on the surface.
# Copied from CurriculumAgent.render_observation.
_SURFACE_MARKERS = ("dirt", "log", "grass", "sand", "snow")


def _round_position(position: dict | None) -> dict | None:
    if not isinstance(position, dict):
        return None
    out = {}
    for axis in ("x", "y", "z"):
        value = position.get(axis)
        out[axis] = round(float(value), 1) if isinstance(value, (int, float)) else value
    return out


def _inventory_delta(before: dict | None, after: dict) -> dict:
    before = before or {}
    delta = {}
    for item in sorted(set(before) | set(after)):
        change = int(after.get(item, 0)) - int(before.get(item, 0))
        if change != 0:
            delta[item] = change
    return delta


def effective_biome(biome: str, voxels: list[str]) -> str:
    """Apply the curriculum's 'underground' override."""
    if not any(marker in block for block in voxels for marker in _SURFACE_MARKERS):
        return "underground"
    return biome


def other_blocks(block_records: list[str], voxels: list[str], inventory: dict) -> list[str]:
    """Recently seen blocks that are neither nearby nor already held."""
    return sorted(set(block_records).difference(set(voxels).union(inventory.keys())))


def build_state(
    events: list,
    *,
    task: str = "",
    context: str = "",
    chest_memory: dict | None = None,
    completed_tasks: list[str] | None = None,
    failed_tasks: list[str] | None = None,
    inventory_before: dict | None = None,
) -> dict[str, Any]:
    """Build a JSON-serialisable state dict from a Voyager ``events`` list.

    ``events`` is the list of ``(event_type, payload)`` pairs returned by
    ``VoyagerEnv.step``; the last entry must be an ``observe`` event.
    ``inventory_before`` is the inventory snapshot taken at task start; when
    given, ``inventory_delta`` holds the per-item change over the whole task
    (all retries), not a single attempt.
    """
    assert events and events[-1][0] == "observe", "Last event must be observe"
    observe = events[-1][1]
    status = observe.get("status", {})

    voxels = list(observe.get("voxels") or [])
    block_records = list(observe.get("blockRecords") or [])
    inventory = dict(observe.get("inventory") or {})
    entities = dict(status.get("entities") or {})

    chat_log: list[str] = []
    errors: list[str] = []
    damage: list[Any] = []
    saves: list[str] = []
    for event_type, event in events:
        if event_type == "onChat":
            chat_log.append(event.get("onChat"))
        elif event_type == "onError":
            errors.append(event.get("onError"))
        elif event_type == "onDamage":
            damage.append(event.get("onDamage"))
        elif event_type == "onSave":
            saves.append(event.get("onSave"))

    chests: dict[str, Any] = {}
    for position, chest in (chest_memory or {}).items():
        # Mirror ActionAgent.render_chest_observation semantics.
        if isinstance(chest, dict):
            chests[str(position)] = chest if chest else "Empty"
        elif chest == "Unknown":
            chests[str(position)] = "Unknown items inside"

    state: dict[str, Any] = {
        "task": task or None,
        "context": context or None,
        "biome": effective_biome(status.get("biome", "None"), voxels),
        "raw_biome": status.get("biome", "None"),
        "time_of_day": status.get("timeOfDay"),
        "health": status.get("health"),
        "hunger": status.get("food"),
        "position": _round_position(status.get("position")),
        "equipment": list(status.get("equipment") or []),
        "inventory_used": status.get("inventoryUsed"),
        "inventory_capacity": 36,
        "inventory": inventory,
        "nearby_blocks": voxels,
        "other_blocks_recently_seen": other_blocks(block_records, voxels, inventory),
        "nearby_entities_nearest_first": [
            name for name, _ in sorted(entities.items(), key=lambda kv: kv[1])
        ],
        "chests": chests,
        "chat_log": [m for m in chat_log if m],
        "execution_errors": [e for e in errors if e],
        "damage_events": damage,
        "save_events": [s for s in saves if s],
        "completed_tasks": list(completed_tasks or []),
        "failed_tasks": list(failed_tasks or []),
    }

    if inventory_before is not None:
        state["inventory_before_task"] = copy.deepcopy(inventory_before)
        state["inventory_delta"] = _inventory_delta(inventory_before, inventory)

    return state
