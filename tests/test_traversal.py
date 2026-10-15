"""Offline tests for voyager.tree.traversal (no Minecraft, no API keys).

A fake bridge replays scripted ``agent_status`` responses. Nodes and the tree
are minimal local stubs following PLAN.md 1.1; the real ``voyager.tree.schema``
is used when it is importable and exposes the same names.
"""
from __future__ import annotations

import json
import os
import tempfile

from voyager.tree.traversal import Traversal


# ---------------------------------------------------------------------- #
# stubs (PLAN.md 1.1)
# ---------------------------------------------------------------------- #

class Node:
    def __init__(self, id, description, verify, *, parent=None, children=None, kind="craft", requires=None,
                 acquire=None, work=None, budget_actions=20):
        self.id = id
        self.parent = parent
        self.children = list(children or [])
        self.description = description
        self.kind = kind
        self.verify = verify
        self.requires = requires or {}
        self.acquire = acquire
        self.work = work or {"kind": kind, "item": id, "count": 1}
        self.done = False
        self.done_at = None
        self.attempts = 0
        self.actions_spent = 0
        self.budget_actions = budget_actions
        self.trace = []

    def to_json(self):
        return dict(self.__dict__)


class Tree:
    def __init__(self, goal, nodes):
        self.goal = goal
        self.goal_id = goal.lower().replace(" ", "_")
        self.nodes = {n.id: n for n in nodes}

    def post_order(self):
        roots = [n for n in self.nodes.values() if not n.parent]
        out = []

        def visit(n):
            for cid in n.children:
                visit(self.nodes[cid])
            out.append(n)

        for r in roots:
            visit(r)
        return out

    def items(self):
        return [n.work["item"] for n in self.post_order()]

    def to_json(self):
        return {"goal": self.goal, "nodes": [n.to_json() for n in self.post_order()]}


def pickaxe_tree():
    log = Node("oak_log", "obtain 3 oak_log", {"has_item": {"name": "oak_log", "count": 3}}, parent="planks", kind="mine", acquire=3)
    planks = Node("planks", "craft 12 oak_planks", {"has_item": {"name": "oak_planks", "count": 12}}, parent="pickaxe", children=["oak_log"])
    pick = Node("pickaxe", "craft 1 wooden_pickaxe", {"has_item": {"name": "wooden_pickaxe", "count": 1}}, children=["planks"])
    return Tree("Obtain a wooden pickaxe", [pick, planks, log])


# ---------------------------------------------------------------------- #
# fake bridge
# ---------------------------------------------------------------------- #

def status(triggers=(), subtask_id=None, completable=None, primitives=(), spent=0):
    return {
        "snap": {"bot": {"position": {"x": 0, "y": 64, "z": 0}}, "inventory": {}, "blocks": {"*_log": {"dx": 2, "dy": 0, "dz": -5, "count": 3}}},
        "triggers": list(triggers),
        "recent": [],
        "primitives": list(primitives),
        "menu": {"move:north": "walk 4 north"},
        "last_decision": {"action": "move:north", "confidence": 0.5, "danger": 0.0, "progress": 0.5, "completable": completable},
        "subtask": {"id": subtask_id, "actions_spent": spent},
        "stats": {"decisions": 1, "jev_ms": 100},
    }


class FakeBridge:
    """Replays one scripted status per poll; records every call."""

    def __init__(self, statuses, verify_ok=None):
        self.statuses = list(statuses)
        self.verify_ok = verify_ok or (lambda spec: False)
        self.posted = []
        self.verified = []
        self.primitives = []
        self.stopped = False

    def agent_subtask(self, node):
        self.posted.append(dict(node))
        return {"ok": True}

    def agent_status(self):
        if not self.statuses:
            raise AssertionError("fake bridge ran out of scripted statuses; traversal did not finish")
        return self.statuses.pop(0)

    def agent_verify(self, spec):
        self.verified.append(spec)
        return {"ok": bool(self.verify_ok(spec)), "detail": "fake"}

    def agent_primitive(self, name, source):
        self.primitives.append(name)
        return {"ok": True, "error": None}

    def agent_stop(self):
        self.stopped = True
        return {"ok": True}


class FakeBuilder:
    def __init__(self):
        self.calls = []
        self.outcomes = 0
        self.untested = False

    def build(self, subtask, status, tree_items, reason=""):
        self.calls.append((subtask["id"], reason))
        return f"x_{len(self.calls)}"

    def record_outcomes(self, status):
        self.outcomes += 1


def make(tree, bridge, builder=None, **kw):
    d = tempfile.mkdtemp()
    kw.setdefault("sleep", lambda s: None)
    return Traversal(tree, bridge, None, builder or FakeBuilder(), ckpt_dir=d, **kw), d


# ---------------------------------------------------------------------- #
# tests
# ---------------------------------------------------------------------- #

def test_post_order_completes_three_nodes_and_persists():
    tree = pickaxe_tree()
    bridge = FakeBridge([
        status(subtask_id="oak_log", spent=3),
        status([{"type": "subtask_done", "subtask_id": "oak_log", "trace": [{"action": "move:north", "outcome": "ok"}, {"action": "x:mine_cursor", "outcome": "ok +1"}]}], "oak_log", spent=5),
        status([{"type": "subtask_done", "subtask_id": "planks", "trace": [{"action": "x:craft", "outcome": "ok"}]}], "planks"),
        status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe"),
    ])
    trav, d = make(tree, bridge)
    result = trav.run()
    assert result["status"] == "done" and result["nodes_done"] == 3
    assert [p["id"] for p in bridge.posted] == ["oak_log", "planks", "pickaxe"]
    post = bridge.posted[0]
    assert set(post) == {"id", "description", "verify", "allowed_blocks", "budget_actions", "kind", "work", "requires", "tree_items",
                         "goal_id", "decision_log"}
    assert post["goal_id"] == tree.goal_id and post["decision_log"].endswith("decisions.jsonl")
    assert "lava" in post["allowed_blocks"] and post["budget_actions"] == 20
    assert tree.nodes["oak_log"].done and tree.nodes["oak_log"].actions_spent == 5
    assert tree.nodes["oak_log"].trace[1]["action"] == "x:mine_cursor"
    assert bridge.stopped
    with open(os.path.join(d, "tree", f"{tree.goal_id}.json"), encoding="utf-8") as fh:
        saved = json.load(fh)
    assert all(n["done"] for n in saved["nodes"])


def test_already_satisfied_node_is_skipped_without_posting():
    tree = pickaxe_tree()
    bridge = FakeBridge(
        [status([{"type": "subtask_done", "subtask_id": "planks", "trace": []}], "planks"),
         status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe")],
        verify_ok=lambda spec: spec.get("has_item", {}).get("name") == "oak_log",
    )
    trav, _ = make(tree, bridge)
    assert trav.run()["status"] == "done"
    assert [p["id"] for p in bridge.posted] == ["planks", "pickaxe"]
    assert tree.nodes["oak_log"].done


def test_resumed_tree_skips_done_nodes():
    tree = pickaxe_tree()
    tree.nodes["oak_log"].done = True
    tree.nodes["planks"].done = True
    bridge = FakeBridge([status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe")])
    trav, _ = make(tree, bridge)
    assert trav.run()["status"] == "done"
    assert [p["id"] for p in bridge.posted] == ["pickaxe"]
    assert not bridge.verified[:-1]  # done nodes are never re-verified


def test_builder_wanted_calls_builder_once_with_reason_and_cooldown_is_builders():
    tree = pickaxe_tree()
    builder = FakeBuilder()
    bridge = FakeBridge([
        status([{"type": "builder_wanted", "subtask_id": "oak_log", "completable": 0.1, "menu": ["move:north"]}], "oak_log", completable=0.1),
        status(subtask_id="oak_log"),
        status([{"type": "subtask_done", "subtask_id": "oak_log", "trace": []}], "oak_log"),
        status([{"type": "subtask_done", "subtask_id": "planks", "trace": []}], "planks"),
        status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe"),
    ])
    trav, _ = make(tree, bridge, builder)
    assert trav.run()["status"] == "done"
    assert len(builder.calls) == 1 and builder.calls[0][0] == "oak_log" and "completable" in builder.calls[0][1]
    assert builder.outcomes == 5  # every poll feeds trial statistics back to the builder
    assert len(bridge.posted) == 3  # a build does not re-post the subtask


def test_stuck_with_completable_menu_retries_with_half_budget():
    tree = pickaxe_tree()
    builder = FakeBuilder()
    bridge = FakeBridge([
        status([{"type": "stuck", "subtask_id": "oak_log", "kind": "position"}], "oak_log", completable=0.9),
        status([{"type": "subtask_done", "subtask_id": "oak_log", "trace": []}], "oak_log"),
        status([{"type": "subtask_done", "subtask_id": "planks", "trace": []}], "planks"),
        status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe"),
    ])
    trav, _ = make(tree, bridge, builder)
    assert trav.run()["status"] == "done"
    assert [p["id"] for p in bridge.posted[:2]] == ["oak_log", "oak_log"]
    assert bridge.posted[0]["budget_actions"] == 20 and bridge.posted[1]["budget_actions"] == 10
    assert tree.nodes["oak_log"].attempts == 1
    assert builder.calls == []


def test_stuck_with_insufficient_menu_builds_instead_of_retrying():
    tree = pickaxe_tree()
    builder = FakeBuilder()
    bridge = FakeBridge([
        status([{"type": "stuck", "subtask_id": "oak_log", "kind": "inventory"}], "oak_log", completable=0.2),
        status([{"type": "subtask_done", "subtask_id": "oak_log", "trace": []}], "oak_log"),
        status([{"type": "subtask_done", "subtask_id": "planks", "trace": []}], "planks"),
        status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe"),
    ])
    trav, _ = make(tree, bridge, builder)
    trav.run()
    assert len(builder.calls) == 1 and "stuck" in builder.calls[0][1]
    assert len([p for p in bridge.posted if p["id"] == "oak_log"]) == 1


def test_two_budget_exhaustions_redecompose_then_continue():
    tree = pickaxe_tree()
    calls = []

    def redecompose(t, node_id):
        calls.append(node_id)
        # swap the exhausted leaf for an easier one
        new = Node("birch_log", "obtain 3 birch_log", {"has_item": {"name": "birch_log", "count": 3}}, parent="planks", kind="mine", acquire=3)
        del t.nodes["oak_log"]
        t.nodes["birch_log"] = new
        t.nodes["planks"].children = ["birch_log"]
        return True

    bridge = FakeBridge([
        status([{"type": "budget_exhausted", "subtask_id": "oak_log", "actions_spent": 20}], "oak_log"),
        status([{"type": "budget_exhausted", "subtask_id": "oak_log", "actions_spent": 20}], "oak_log"),
        status([{"type": "subtask_done", "subtask_id": "birch_log", "trace": []}], "birch_log"),
        status([{"type": "subtask_done", "subtask_id": "planks", "trace": []}], "planks"),
        status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe"),
    ])
    trav, _ = make(tree, bridge, redecompose=redecompose)
    assert trav.run()["status"] == "done"
    assert calls == ["oak_log"]
    assert [p["id"] for p in bridge.posted] == ["oak_log", "oak_log", "birch_log", "planks", "pickaxe"]
    assert bridge.posted[1]["budget_actions"] == 20  # first exhaustion re-posts with the full budget


def test_failed_redecompose_fails_the_parent_chain_and_stops():
    tree = pickaxe_tree()
    bridge = FakeBridge([
        status([{"type": "budget_exhausted", "subtask_id": "oak_log", "actions_spent": 20}], "oak_log"),
        status([{"type": "budget_exhausted", "subtask_id": "oak_log", "actions_spent": 20}], "oak_log"),
    ])
    trav, d = make(tree, bridge, redecompose=lambda t, n: None)
    result = trav.run()
    assert result["status"] == "failed"
    assert result["failed"] == ["oak_log", "pickaxe", "planks"]
    assert len(bridge.posted) == 2 and bridge.stopped
    with open(os.path.join(d, "tree", f"{tree.goal_id}.run.json"), encoding="utf-8") as fh:
        assert set(json.load(fh)["failed"]) == {"oak_log", "planks", "pickaxe"}


def test_wall_clock_limit_counts_as_exhaustion():
    tree = pickaxe_tree()
    clock = [0.0]

    def now():
        clock[0] += 100.0
        return clock[0]

    bridge = FakeBridge([
        status(subtask_id="oak_log"),  # time passes past the limit on this poll
        status(subtask_id="oak_log"),
        status([{"type": "subtask_done", "subtask_id": "oak_log", "trace": []}], "oak_log"),
        status([{"type": "subtask_done", "subtask_id": "planks", "trace": []}], "planks"),
        status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe"),
    ])
    trav, _ = make(tree, bridge, now=now, node_seconds_limit=150.0, exhaustions_before_redecompose=5)
    assert trav.run()["status"] == "done"
    assert trav.exhaustions["oak_log"] >= 1
    assert [p["id"] for p in bridge.posted[:2]] == ["oak_log", "oak_log"]


def test_stale_triggers_from_other_subtasks_are_ignored_and_danger_is_logged():
    tree = pickaxe_tree()
    bridge = FakeBridge([
        status([{"type": "subtask_done", "subtask_id": "planks", "trace": []}, {"type": "danger", "subtask_id": "oak_log", "health": 5, "threat": "zombie"}], "oak_log"),
        status([{"type": "subtask_done", "subtask_id": "oak_log", "trace": []}], "oak_log"),
        status([{"type": "subtask_done", "subtask_id": "planks", "trace": []}], "planks"),
        status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe"),
    ])
    trav, _ = make(tree, bridge)
    assert trav.run()["status"] == "done"
    assert not tree.nodes["planks"].done or bridge.posted[1]["id"] == "planks"
    assert any(e["type"] == "danger" and e.get("threat") == "zombie" for e in trav.events)


def test_allowed_blocks_from_knowledge_tables():
    tree = pickaxe_tree()
    tree.nodes["oak_log"].work = {"kind": "mine", "item": "cobblestone", "count": 3}
    knowledge = {"ore_drops": {"stone": "cobblestone", "deepslate": "cobbled_deepslate"}, "families": {"log": ["oak_log", "birch_log"]}}
    trav, _ = make(tree, FakeBridge([]), knowledge=knowledge)
    blocks = trav.allowed_blocks()
    assert "stone" in blocks and "cobblestone" in blocks and "lava" in blocks and "deepslate" not in blocks


def test_dict_nodes_are_supported():
    class DictTree:
        goal = "dict goal"

        def __init__(self):
            self.n = [{"id": "a", "parent": None, "children": [], "description": "do a", "kind": "mine",
                       "verify": {"has_item": {"name": "dirt", "count": 1}}, "requires": {}, "acquire": 1,
                       "work": {"kind": "mine", "item": "dirt", "count": 1}, "done": False, "done_at": None,
                       "attempts": 0, "actions_spent": 0, "budget_actions": 10, "trace": []}]

        def post_order(self):
            return list(self.n)

        def to_json(self):
            return {"nodes": self.n}

    tree = DictTree()
    bridge = FakeBridge([status([{"type": "subtask_done", "subtask_id": "a", "trace": [1, 2]}], "a")])
    trav, _ = make(tree, bridge)
    assert trav.run()["status"] == "done" and tree.n[0]["done"] and tree.n[0]["trace"] == [1, 2]


def test_builder_refusal_is_logged_with_its_reason():
    class RefusingBuilder(FakeBuilder):
        last_refusal = None

        def build(self, subtask, status, tree_items, reason=""):
            self.calls.append((subtask["id"], reason))
            self.last_refusal = "12 generated actions are live already"
            return None

    tree = pickaxe_tree()
    builder = RefusingBuilder()
    bridge = FakeBridge([
        status([{"type": "builder_wanted", "subtask_id": "oak_log", "completable": 0.1, "menu": ["move:north"]}], "oak_log", completable=0.1),
        status([{"type": "subtask_done", "subtask_id": "oak_log", "trace": []}], "oak_log"),
        status([{"type": "subtask_done", "subtask_id": "planks", "trace": []}], "planks"),
        status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe"),
    ])
    trav, d = make(tree, bridge, builder)
    assert trav.run()["status"] == "done"
    refused = [e for e in trav.events if e.get("type") == "build_refused"]
    assert refused and "live already" in refused[0]["why"] and refused[0]["node"] == "oak_log"


def test_forced_builds_that_produce_nothing_are_capped_then_redecompose():
    # F3: a craft node with its inputs in hand used to cycle exhaust -> forced build(None)
    # -> re-post with a halved budget every few seconds. Now: full budget after a forced
    # build, and two empty forced builds in a row hand over to re-decomposition.
    class EmptyBuilder(FakeBuilder):
        last_refusal = None

        def build(self, subtask, status, tree_items, reason="", force=False):
            self.calls.append((subtask["id"], reason, force))
            self.last_refusal = "GPT call failed: timeout"
            return None

    tree = pickaxe_tree()
    tree.nodes["oak_log"].done = True
    tree.nodes["planks"].requires = {"oak_log": 1}
    redecomposed = []
    exhausted = lambda: status([{"type": "budget_exhausted", "subtask_id": "planks", "actions_spent": 20}], "planks")
    in_hand = lambda s: {**s, "snap": {**s["snap"], "inventory": {"oak_log": 3}}}
    bridge = FakeBridge([in_hand(exhausted()) for _ in range(6)])
    builder = EmptyBuilder()
    trav, _ = make(tree, bridge, builder, redecompose=lambda t, n: redecomposed.append(n) or None)
    result = trav.run()
    forced = [c for c in builder.calls if c[2]]
    assert len(forced) == 2, builder.calls
    assert redecomposed == ["planks"] and result["status"] == "failed"
    assert all(p["budget_actions"] == 20 for p in bridge.posted), [p["budget_actions"] for p in bridge.posted]
    refused = [e for e in trav.events if e["type"] == "build_refused"]
    assert refused and "timeout" in refused[0]["why"]
    assert any(e["type"] == "forced_builds_exhausted" for e in trav.events)


def test_retries_exhausted_reposts_with_the_full_budget():
    tree = pickaxe_tree()
    stuck = lambda: status([{"type": "stuck", "subtask_id": "oak_log", "kind": "position"}], "oak_log", completable=0.9)
    bridge = FakeBridge([stuck(), stuck(), stuck(), stuck(),
                         status([{"type": "subtask_done", "subtask_id": "oak_log", "trace": []}], "oak_log"),
                         status([{"type": "subtask_done", "subtask_id": "planks", "trace": []}], "planks"),
                         status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe")])
    trav, _ = make(tree, bridge, max_retries_per_node=3, exhaustions_before_redecompose=5)
    assert trav.run()["status"] == "done"
    budgets = [p["budget_actions"] for p in bridge.posted if p["id"] == "oak_log"]
    assert budgets == [20, 10, 5, 5, 20], budgets



def test_async_builds_keep_polling_and_hold_one_slot():
    import threading
    import time as _time

    class SlowBuilder(FakeBuilder):
        last_refusal = None

        def __init__(self):
            super().__init__()
            self.release = threading.Event()

        def build(self, subtask, status, tree_items, reason="", force=False):
            self.calls.append((subtask["id"], reason))
            self.release.wait(timeout=5)  # a slow GPT conversation
            return "x_slow"

    tree = pickaxe_tree()
    builder = SlowBuilder()
    want = lambda: status([{"type": "builder_wanted", "subtask_id": "oak_log", "completable": 0.1, "menu": []}], "oak_log", completable=0.1)
    polls = []

    class CountingBridge(FakeBridge):
        def agent_status(self):
            polls.append(1)
            if len(polls) == 4:
                builder.release.set()  # the build finishes while the loop is still polling
                _time.sleep(0.05)
            return super().agent_status()

    bridge = CountingBridge([want(), want(), want(), status(subtask_id="oak_log"),
                             status([{"type": "subtask_done", "subtask_id": "oak_log", "trace": []}], "oak_log"),
                             status([{"type": "subtask_done", "subtask_id": "planks", "trace": []}], "planks"),
                             status([{"type": "subtask_done", "subtask_id": "pickaxe", "trace": []}], "pickaxe")])
    trav, _ = make(tree, bridge, builder, async_builds=True)
    assert trav.run()["status"] == "done"
    assert len(builder.calls) == 1  # one slot: the two later requests were skipped, not queued
    kinds = [e["type"] for e in trav.events]
    assert kinds.count("build_skipped") == 2 and "build_started" in kinds
    assert any(e["type"] == "build" and e.get("name") == "x_slow" for e in trav.events)
    assert len(polls) == 7  # polling never stopped for the build



def test_node_is_not_failed_while_its_build_runs_or_its_new_action_is_untested():
    # 2026-10-13: both repaired actions registered seconds after their node had failed.
    import threading

    class SlowBuilder(FakeBuilder):
        last_refusal = None

        def __init__(self):
            super().__init__()
            self.release = threading.Event()
            self.untested = []

        def build(self, subtask, status, tree_items, reason="", force=False):
            self.calls.append((subtask["id"], reason))
            self.release.wait(timeout=5)
            self.untested = ["fresh_miner"]  # registered, not yet tried
            return "fresh_miner"

        def untested_for(self, subtask_id, status):
            return list(self.untested)

    tree = pickaxe_tree()
    builder = SlowBuilder()
    exhausted = lambda: status([{"type": "budget_exhausted", "subtask_id": "oak_log", "actions_spent": 20}], "oak_log")
    want = status([{"type": "builder_wanted", "subtask_id": "oak_log", "completable": 0.1, "none": 0.8, "reason": "none_of_these", "menu": []}], "oak_log")
    calls = {"n": 0}

    class Bridge(FakeBridge):
        def agent_status(self):
            calls["n"] += 1
            if calls["n"] == 3:
                builder.release.set()  # the build finishes while traversal waits on it
            return super().agent_status()

    redecomposed = []
    bridge = Bridge([want, exhausted(), exhausted(),  # 2nd exhaustion: build running -> wait, grace
                     status(subtask_id="oak_log"),     # fresh status read after the build
                     exhausted(),                      # untested action -> second grace round
                     exhausted(), exhausted()])        # grace used up -> re-decompose (fails here)
    trav, _ = make(tree, bridge, builder, async_builds=True, redecompose=lambda t, n: redecomposed.append(n) or None)
    result = trav.run()
    kinds = [e["type"] for e in trav.events]
    assert "waiting_for_build" in kinds and kinds.count("grace_round") == 2, kinds
    assert any(e["type"] == "build" and e.get("name") == "fresh_miner" for e in trav.events)
    assert kinds.index("build") < kinds.index("node_failed")  # the action existed before the node was given up
    assert redecomposed == ["oak_log"] and result["status"] == "failed"
    assert "no action on the menu can move the subtask forward" in builder.calls[0][1]
    assert all(p["budget_actions"] == 20 for p in bridge.posted)
