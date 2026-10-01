"""Offline check for voyager.typesafe.build_state using a recorded events file.

Usage (from the repo root, with the venv interpreter):
    venv/Scripts/python.exe scripts/check_typesafe_state.py [path/to/events.json]

Without an argument the newest file under ckpt/events/ is used. No API key or
Minecraft instance is needed.
"""

from __future__ import annotations

import copy
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from voyager.typesafe.state import build_state, effective_biome, other_blocks  # noqa: E402
from voyager.typesafe.client import JevClient  # noqa: E402


def load_events(path: str | None) -> list:
    if path is None:
        candidates = sorted(glob.glob(os.path.join(ROOT, "ckpt", "events", "*")), key=os.path.getmtime)
        if not candidates:
            sys.exit("No events fixture found under ckpt/events/; pass a path explicitly.")
        path = candidates[-1]
    with open(path, encoding="utf-8") as fh:
        events = json.load(fh)
    print(f"fixture: {path} ({len(events)} events)")
    return events


def main() -> None:
    events = load_events(sys.argv[1] if len(sys.argv) > 1 else None)
    observe = events[-1][1]

    before = copy.deepcopy(observe["inventory"])
    # pretend the task gained two logs and consumed one plank
    before_task = dict(before)
    first_item = next(iter(before_task), None)
    if first_item is not None:
        before_task[first_item] = max(0, before_task[first_item] - 2)
    before_task["oak_planks"] = before_task.get("oak_planks", 0) + 1

    state = build_state(
        events,
        task="Mine 3 wood logs",
        context="Any log type counts.",
        chest_memory={"(1, 2, 3)": {"dirt": 4}, "(4, 5, 6)": {}, "(7, 8, 9)": "Unknown"},
        completed_tasks=["Mine 1 wood log"],
        failed_tasks=[],
        inventory_before=before_task,
    )

    required = [
        "task", "context", "biome", "raw_biome", "time_of_day", "health", "hunger",
        "position", "equipment", "inventory_used", "inventory", "nearby_blocks",
        "other_blocks_recently_seen", "nearby_entities_nearest_first", "chests",
        "chat_log", "execution_errors", "damage_events", "save_events",
        "completed_tasks", "failed_tasks", "inventory_before_task", "inventory_delta",
    ]
    missing = [k for k in required if k not in state]
    assert not missing, f"missing fields: {missing}"

    # JSON-serialisable with the standard encoder (no default=str)
    encoded = json.dumps(state)
    assert len(encoded) > 0

    # underground rule
    assert effective_biome("plains", ["stone", "deepslate"]) == "underground"
    assert effective_biome("plains", ["grass_block", "stone"]) == "plains"
    assert state["biome"] == effective_biome(observe["status"]["biome"], observe["voxels"])

    # other_blocks rule mirrors the curriculum renderer
    expected_other = sorted(
        set(observe["blockRecords"]).difference(set(observe["voxels"]).union(observe["inventory"].keys()))
    )
    assert state["other_blocks_recently_seen"] == expected_other
    assert other_blocks(["a", "b", "c"], ["a"], {"b": 1}) == ["c"]

    # delta: gained 2 of first_item, lost 1 oak_planks (unless the fixture held planks)
    delta = state["inventory_delta"]
    if first_item is not None:
        assert delta.get(first_item, 0) == min(2, before[first_item]), delta
    assert delta.get("oak_planks", 0) == before.get("oak_planks", 0) - before_task["oak_planks"], delta

    # chest rendering semantics
    assert state["chests"] == {
        "(1, 2, 3)": {"dirt": 4},
        "(4, 5, 6)": "Empty",
        "(7, 8, 9)": "Unknown items inside",
    }

    # position rounding
    assert all(isinstance(v, (int, float)) for v in state["position"].values())

    print("build_state: OK")
    print(json.dumps({k: state[k] for k in ("biome", "inventory", "inventory_delta", "nearby_blocks")}, indent=2))

    # disabled-client path must be a clean no-op
    saved = os.environ.pop("TYPESAFE_API_KEY", None)
    try:
        client = JevClient(enabled=True, ckpt_dir=os.path.join(ROOT, "ckpt"))
        assert not client.enabled, "client should be disabled without TYPESAFE_API_KEY"
        assert client.ask("noop", state, {"x": None}) is None
        client.log_only("noop", {"should": "not be written"})
        assert not os.path.exists(os.path.join(ROOT, "ckpt", "typesafe", "noop.jsonl"))
    finally:
        if saved is not None:
            os.environ["TYPESAFE_API_KEY"] = saved
    print("disabled JevClient: OK")


if __name__ == "__main__":
    main()
