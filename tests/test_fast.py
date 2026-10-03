"""Offline tests for fast mode (no Minecraft, no API keys).

Run with the venv python:  venv/Scripts/python tests/run_tests.py
"""
import json
import os
import tempfile

from voyager.fast.skills import SkillMemory, expand, fingerprint_key, target_key, verify_spec
from voyager.fast.brain import FastBrain, _matcher

FP = {"biome": "plains", "toolTier": "none", "daylight": "day"}
NIGHT = {**FP, "daylight": "night"}


def test_skill_node_fields_routes_and_tree():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fast", "skills.json")
        mem = SkillMemory(path)
        trace = [
            {"action": "walk:north", "outcome": "ok", "gain": 0, "ms": 3000, "tool": None},
            {"action": "mine:oak_log", "outcome": "ok", "gain": 1, "ms": 4000, "tool": "wooden_axe"},
        ]
        node = mem.record(
            goal="Obtain 2 oak_log", target={"item": "oak_log", "count": 2}, kind="sub",
            high_goal="Craft a wooden pickaxe", fingerprint=FP, seconds=12.3, trace=trace,
            collapsed=[{"action": "walk:north", "times": 1}, {"action": "mine:oak_log", "times": 2}],
            inventory_before={"stick": 1}, inventory_after={"stick": 1, "oak_log": 2},
        )
        # what the user asked a skill to carry
        assert node["id"] == "oak_log" and node["goal"] == "Obtain 2 oak_log"
        assert node["high_goals"] == ["Craft a wooden pickaxe"]
        assert node["seconds"] == 12.3 and node["tools_used"] == ["wooden_axe"]
        assert node["verify"] == {"kind": "item", "item": "oak_log", "count": 2}
        assert node["inventory_delta"] == {"oak_log": 2}
        assert mem.route_for({"item": "oak_log", "count": 2}, FP) == ["walk:north", "mine:oak_log", "mine:oak_log"]
        assert mem.route_for({"item": "oak_log", "count": 2}, NIGHT) is None
        # a shorter route in the same situation replaces the old one; another high goal is linked
        mem.record(goal="Obtain 2 oak_log", target={"item": "oak_log", "count": 2}, kind="sub",
                   high_goal="Mine 3 wood logs", fingerprint=FP, seconds=8.0, trace=[],
                   collapsed=[{"action": "mine:oak_log", "times": 2}], inventory_before={}, inventory_after={})
        node = mem.node("Obtain 2 oak_log", {"item": "oak_log", "count": 2})
        assert node["successes"] == 2 and sorted(node["high_goals"]) == ["Craft a wooden pickaxe", "Mine 3 wood logs"]
        assert node["routes"][fingerprint_key(FP)]["actions"] == ["mine:oak_log", "mine:oak_log"]
        # the high goal becomes a node connected to the nodes used to reach it
        mem.record(goal="Obtain 2 stick", target={"item": "stick", "count": 2}, kind="sub", high_goal="Craft a wooden pickaxe",
                   fingerprint=FP, seconds=2.0, trace=[], collapsed=[], inventory_before={}, inventory_after={})
        high = mem.record(goal="Craft a wooden pickaxe", target={"item": "wooden_pickaxe", "count": 1}, kind="high",
                          high_goal=None, fingerprint=FP, seconds=40.0, trace=[], collapsed=[],
                          inventory_before={}, inventory_after={"wooden_pickaxe": 1}, children=["oak_log", "stick"])
        assert high["kind"] == "high" and high["children"] == ["oak_log", "stick"]
        assert mem.skills["oak_log"]["parents"] == ["wooden_pickaxe"]
        assert mem.known("Craft a wooden pickaxe", {"item": "wooden_pickaxe", "count": 1})
        assert not mem.known("Obtain 2 stick", {"item": "stick", "count": 2})  # a sub node is not a learned high goal
        mem.note_failure("Obtain 2 oak_log", {"item": "oak_log", "count": 2})
        assert mem.skills["oak_log"]["failures"] == 1
        assert "known skill" in mem.describe(mem.skills["oak_log"], FP)
        assert SkillMemory(path).skills["wooden_pickaxe"]["children"] == ["oak_log", "stick"]
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh)


def test_keys_and_helpers():
    assert expand([{"action": "a", "times": 2}, {"action": "b"}]) == ["a", "a", "b"]
    assert fingerprint_key(None) == "?|?|?"
    assert target_key({"item": "*_log", "count": 3}) == "*_log"
    assert target_key({"nearBlock": "iron_ore"}) == "near:iron_ore"
    assert target_key({"none": True}) == "none" and target_key(None) == "none"
    assert verify_spec({"nearBlock": "furnace"}) == {"kind": "nearBlock", "block": "furnace"}
    assert verify_spec(None) == {"kind": "judged"}
    assert SkillMemory.node_id("Place a chest", None) == "judged:place a chest"
    assert _matcher("*_log")("spruce_log") and not _matcher("*_log")("log_cabin")


def test_merge_events_keeps_trailing_observe():
    obs = ("observe", {"inventory": {"oak_log": 1}})
    events = [("onChat", {"onChat": "hi"}), obs]
    merged = FastBrain._merge_events(events, [["onSave", {"onSave": "oak_log_mined"}]])
    assert [e[0] for e in merged] == ["onChat", "onSave", "observe"]
    merged2 = FastBrain._merge_events(merged, [["onChat", {}], ["observe", {"inventory": {}}]])
    assert merged2[-1][1] == {"inventory": {}}
    assert FastBrain._inventory(merged) == {"oak_log": 1}


class _StubEnv:
    """Stand-in for VoyagerEnv in fast mode: a wooden pickaxe needs planks first."""

    OBSERVE = ["observe", {"inventory": {}, "status": {"position": {"x": 0, "z": 0}}}]

    def __init__(self):
        self.goals = []
        self.subgoal_requests = []
        self.polls = 0
        self.pause_server = True
        self.inventory = {"oak_log": 1}

    def fast_goal(self, goal):
        self.goals.append(goal)
        return {"replayRemaining": len(goal.get("replay") or []), "replaySource": None}

    def fast_subgoals(self, text, target=None, **kw):
        self.subgoal_requests.append(text)
        if "oak_planks" in self.inventory:
            cands = [{"text": "Craft 1 wooden_pickaxe", "target": {"item": "wooden_pickaxe", "count": 1}, "why": "all ingredients present"}]
        else:
            cands = [
                {"text": "Obtain 3 oak_planks", "target": {"item": "oak_planks", "count": 3}, "why": "ingredient"},
                {"text": "Obtain 2 stick", "target": {"item": "stick", "count": 2}, "why": "ingredient"},
            ]
        item = "wooden_axe" if "axe" in text and "pickaxe" not in text else "wooden_pickaxe"
        have = self.inventory.get(item, 0)
        return {"target": {"item": item, "need": 1, "have": have, "gained": 0}, "candidates": cands}

    def _status(self, **kw):
        base = {
            "triggers": [], "events": [self.OBSERVE], "inventory": dict(self.inventory),
            "fingerprint": FP, "recentActions": [], "trace": [], "collapsedTrace": [],
            "target": None, "highTarget": {"item": "wooden_pickaxe", "need": 1, "have": 0, "gained": 0},
            "highGoalReached": False, "stats": {"actions": 1, "decisions": 1},
        }
        base.update(kw)
        return base

    def fast_status(self):
        self.polls += 1
        goal = self.goals[-1]
        if self.polls == 1:
            return self._status()
        if self.polls == 2:  # first subgoal (planks) reached
            self.inventory = {"oak_planks": 4}
            return self._status(
                triggers=[{"type": "goal_reached", "goalId": goal["id"], "detail": {"target": {"item": "oak_planks"}}}],
                target={"item": "oak_planks", "need": 3, "have": 4, "gained": 4},
                trace=[{"action": "craft:oak_planks", "outcome": "ok", "gain": 4, "ms": 900, "tool": None}],
                collapsedTrace=[{"action": "craft:oak_planks", "times": 1}],
            )
        if self.polls == 3:  # the pickaxe subgoal stalls once
            return self._status(triggers=[{"type": "no_progress", "goalId": goal["id"], "detail": {"actions": 5}}])
        # then the high goal is reached
        self.inventory = {"oak_planks": 1, "wooden_pickaxe": 1}
        return self._status(
            triggers=[
                {"type": "goal_reached", "goalId": goal["id"], "detail": {"target": {"item": "wooden_pickaxe"}}},
                {"type": "high_goal_reached", "goalId": goal["id"], "detail": {}},
            ],
            highGoalReached=True,
            highTarget={"item": "wooden_pickaxe", "need": 1, "have": 1, "gained": 1},
            trace=[{"action": "craft:wooden_pickaxe", "outcome": "ok", "gain": 1, "ms": 1200, "tool": "oak_planks"}],
            collapsedTrace=[{"action": "craft:wooden_pickaxe", "times": 1}],
        )

    def fast_stop(self):
        return {}


class _Stub:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _voyager(env, ckpt):
    recorded, logged = [], []
    v = _Stub(
        env=env, ckpt_dir=ckpt, resume=True,
        recorder=_Stub(iteration=0, record=lambda events, task: recorded.append((task, events))),
        jev=_Stub(enabled=False, iteration=0, log_only=lambda gate, rec: logged.append((gate, rec))),
        typesafe_critic_threshold=0.8,
        action_agent=_Stub(chest_memory={}, update_chest_memory=lambda chests: None,
                           render_chest_observation=lambda: "Chests: None\n\n"),
        curriculum_agent=_Stub(completed_tasks=[], failed_tasks=[], ckpt_dir=ckpt),
    )
    return v, recorded, logged


def test_high_goal_builds_skill_tree():
    with tempfile.TemporaryDirectory() as d:
        env = _StubEnv()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0)
        assert env.pause_server is False
        brain.last_status = env._status()
        info = brain.run_high_goal("Craft a wooden pickaxe", "needs planks and sticks")
        assert info == {"task": "Craft a wooden pickaxe", "success": True, "interrupted": False}
        assert all(g["highGoal"]["text"] == "Craft a wooden pickaxe" for g in env.goals)
        assert env.goals[0]["text"] == "Obtain 3 oak_planks" and env.goals[0]["target"] == {"item": "oak_planks", "count": 3}
        # after the stall the only derived candidate is offered again; the high goal's bare name is never posted
        assert [g["text"] for g in env.goals[1:]] == ["Craft 1 wooden_pickaxe", "Craft 1 wooden_pickaxe"]
        # the tree: high node connected to the sub nodes used
        high = brain.skills.node("Craft a wooden pickaxe", {"item": "wooden_pickaxe", "count": 1})
        assert high and high["kind"] == "high"
        assert "oak_planks" in high["children"]
        planks = brain.skills.skills["oak_planks"]
        assert planks["kind"] == "sub" and planks["high_goals"] == ["Craft a wooden pickaxe"]
        assert planks["routes"][fingerprint_key(FP)]["actions"] == ["craft:oak_planks"]
        assert isinstance(planks["seconds"], float) and planks["tools_used"] == []
        assert os.path.exists(os.path.join(d, "fast", "skills.json"))
        assert recorded and recorded[0][0] == "Craft a wooden pickaxe"
        assert logged[-1][0] == "fast_goals" and logged[-1][1]["success"] and "oak_planks" in logged[-1][1]["children"]
        # a later high goal needing planks in the same situation replays the learned route
        env2 = _StubEnv()
        env2.inventory = {}
        brain.env = env2
        brain.last_status = env2._status()
        sub = brain._select_subgoal({"id": "h2", "text": "Craft a wooden axe"}, "", env2._status(), set())
        assert sub["text"] == "Obtain 3 oak_planks" and sub["skill"]["id"] == "oak_planks"
        brain._start_subgoal({"id": "h2", "text": "Craft a wooden axe"}, "", sub, env2._status())
        assert env2.goals[-1]["replay"] == ["craft:oak_planks"]


def test_new_high_goal_rejects_learned_or_satisfied():
    with tempfile.TemporaryDirectory() as d:
        env = _StubEnv()
        v, recorded, logged = _voyager(env, d)
        proposals = iter([
            ("Craft a wooden pickaxe", "c1"),  # already a learned high node
            ("Craft a wooden pickaxe", "c2"),  # again, while the stub says the bag holds one -> satisfied
            ("Craft a wooden axe", "c3"),
        ])
        feedbacks = []

        def propose(**kw):
            feedbacks.append(kw.get("extra_feedback"))
            return next(proposals)

        v.curriculum_agent.propose_next_task = propose
        brain = FastBrain(v, poll_seconds=0.0)
        brain.skills.record(goal="Craft a wooden pickaxe", target={"item": "wooden_pickaxe", "count": 1}, kind="high",
                            high_goal=None, fingerprint=FP, seconds=1, trace=[], collapsed=[],
                            inventory_before={}, inventory_after={})
        task, _ = brain._propose_new_high_goal()
        assert task == "Craft a wooden axe"
        assert feedbacks[0] is None
        assert "already a learned skill" in feedbacks[1]
        assert "rejected" in feedbacks[2]


def test_failed_task_cooldown_and_long_term_goal():
    with tempfile.TemporaryDirectory() as d:
        env = _StubEnv()
        v, recorded, logged = _voyager(env, d)
        os.makedirs(os.path.join(d, "curriculum"), exist_ok=True)
        brain = FastBrain(v, poll_seconds=0.0, long_term_goal="Beat the game", failed_task_cooldown=3)
        assert v.curriculum_agent.long_term_goal == "Beat the game"
        v.curriculum_agent.failed_tasks.append("Mine 3 iron ore")
        brain._note_failed("Mine 3 iron ore")
        assert brain.failed_history["Mine 3 iron ore"]["retry_after_subgoals"] == 3
        brain._release_cooled_failures()
        assert v.curriculum_agent.failed_tasks == ["Mine 3 iron ore"]
        brain.subgoals_completed = 3
        brain._release_cooled_failures()
        assert v.curriculum_agent.failed_tasks == []
        v.curriculum_agent.failed_tasks.append("Mine 3 iron ore")
        brain._note_failed("Mine 3 iron ore")
        assert brain.failed_history["Mine 3 iron ore"]["retry_after_subgoals"] == 3 + 6
        assert os.path.exists(os.path.join(d, "fast", "failed_tasks.json"))


def test_obsolete_targetless_high_goal_ends():
    """'Place a chest' with a chest already nearby: Jev says obsolete, nothing else to try -> done."""

    class _ChestEnv(_StubEnv):
        def fast_subgoals(self, text, target=None, **kw):
            return {"target": None, "candidates": [{"text": text, "target": {"none": True}, "why": "direct"}]}

        def fast_status(self):
            self.polls += 1
            goal = self.goals[-1]
            trig = [] if self.polls == 1 else [{"type": "subgoal_obsolete", "goalId": goal["id"], "detail": {"subgoalValid": 0.05}}]
            return self._status(triggers=trig, highTarget=None, target=None)

    with tempfile.TemporaryDirectory() as d:
        env = _ChestEnv()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0)
        brain.last_status = env._status(highTarget=None)
        info = brain.run_high_goal("Place a chest", "a chest nearby counts")
        assert info["success"] is True
        assert env.polls <= 3 and len(env.goals) == 1
        assert brain.skills.node("Place a chest", None)["verify"] == {"kind": "judged"}


def test_instant_subgoal_is_not_a_skill():
    class _InstantEnv(_StubEnv):
        def fast_status(self):
            self.polls += 1
            goal = self.goals[-1]
            if self.polls == 1:
                return self._status(triggers=[{"type": "goal_reached", "goalId": goal["id"],
                                               "detail": {"target": {"item": "oak_planks"}, "instant": True}}])
            return self._status(triggers=[{"type": "no_progress", "goalId": goal["id"], "detail": {}}])

    with tempfile.TemporaryDirectory() as d:
        env = _InstantEnv()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0, subgoal_failures_before_fail=1)
        brain.last_status = env._status()
        info = brain.run_high_goal("Craft a wooden pickaxe", "")
        assert info["success"] is False
        assert brain.skills.skills == {}  # the instant completion recorded nothing
        assert env.goals[1]["text"] == "Obtain 2 stick"  # and the instant one was not offered again


def test_world_refusals_become_planned_requirements():
    class _RefuseEnv(_StubEnv):
        def __init__(self):
            super().__init__()
            self.extra_seen = []

        def fast_subgoals(self, text, target=None, extra_targets=None, **kw):
            self.extra_seen.append(extra_targets)
            cands = [{"text": "Mine 3 raw_iron", "target": {"item": "raw_iron", "count": 3}, "why": "the goal itself"}]
            if extra_targets:
                cands.insert(0, {"text": "Obtain 1 stone_pickaxe", "target": {"item": "stone_pickaxe", "count": 1}, "why": "learned"})
            return {"target": {"item": "raw_iron", "need": 3, "have": 0, "gained": 0}, "candidates": cands,
                    "targets": [{"item": "raw_iron", "need": 3}]}

        def fast_status(self):
            self.polls += 1
            goal = self.goals[-1]
            if self.polls == 1:
                return self._status(events=[["onChat", {"onChat": "I need at least a stone_pickaxe to mine iron_ore!  Skip it!"}], self.OBSERVE])
            if self.polls == 2:
                return self._status(triggers=[{"type": "no_progress", "goalId": goal["id"], "detail": {}}],
                                    events=[["onChat", {"onChat": "I cannot make iron_pickaxe because I need: 2 more stick, 1 more iron_ingot"}], self.OBSERVE])
            return self._status(triggers=[{"type": "no_progress", "goalId": goal["id"], "detail": {}}])

    with tempfile.TemporaryDirectory() as d:
        env = _RefuseEnv()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0, subgoal_failures_before_fail=2)
        brain.last_status = env._status()
        brain.run_high_goal("Mine 3 iron ore", "")
        learned = brain._learned["high-1"]
        assert [(t["item"], t["count"]) for t in learned] == [("stone_pickaxe", 1), ("stick", 2), ("iron_ingot", 1)]
        # the re-plan after the stall carried the learned requirement and the planner offered the pickaxe
        assert env.extra_seen[0] is None and env.extra_seen[1][0]["item"] == "stone_pickaxe"
        assert env.goals[1]["text"] == "Obtain 1 stone_pickaxe"
        assert any(g == "learned_requirements" for g, _ in logged)


def test_skill_tools_only_records_real_tools():
    with tempfile.TemporaryDirectory() as d:
        mem = SkillMemory(os.path.join(d, "fast", "skills.json"))
        trace = [{"action": "mine:stone", "outcome": "ok", "gain": 1, "ms": 1, "tool": t} for t in ("cobblestone", "raw_iron", "stone_pickaxe", "shears", "iron_axe", None)]
        node = mem.record(goal="Obtain 3 cobblestone", target={"item": "cobblestone", "count": 3}, kind="sub", high_goal="x",
                          fingerprint=FP, seconds=1, trace=trace, collapsed=[], inventory_before={}, inventory_after={})
        assert node["tools_used"] == ["iron_axe", "shears", "stone_pickaxe"]


def test_ladder_is_strict_remembers_rungs_and_kits_restock():
    from voyager.fast.milestones import Ladder, target_label

    ladder = Ladder()
    keys = [m.key for m in ladder.milestones]
    assert keys[:5] == ["wood", "wooden_tools", "home", "kit", "stone_tools"] and keys[-1] == "dragon"
    nothing = {m.key: [False] * len(m.targets) for m in ladder.milestones}
    m, unmet = ladder.next_unmet(nothing, 0)
    assert m.key == "wood" and target_label(unmet[0]) == "8 *_log"
    sat = dict(nothing)
    sat["wood"] = [True]
    assert ladder.next_unmet(sat, 0)[0].key == "wooden_tools"
    sat["wooden_tools"] = [True, True]
    assert ladder.next_unmet(sat, 0)[0].key == "home"  # the kit waits for the home base
    sat["home"] = [True] * 3
    m, unmet = ladder.next_unmet(sat, 0)
    assert m.key == "kit" and len(unmet) == 6 and target_label(unmet[-1]) == "8 free inventory slots"
    sat["kit"] = [True] * 6
    assert ladder.next_unmet(sat, 0)[0].key == "stone_tools"
    # a rung set aside after repeated failures yields to the next rung only, then comes back
    ladder.skip("stone_tools", 0, 6)
    assert ladder.next_unmet(sat, 2)[0].key == "furnace"
    assert ladder.next_unmet(sat, 7)[0].key == "stone_tools"
    ladder.skipped_until = {}
    # remembered: logs used up no longer send the bot back to "wood"
    sat["wood"] = [False]
    assert ladder.next_unmet(sat, 0, reached={"wood"})[0].key == "stone_tools"
    assert ladder.next_unmet(sat, 0)[0].key == "wood"
    # a kit becomes unmet again and comes first
    sat["kit"] = [True, False, True, True, True, True]
    m, unmet = ladder.next_unmet(sat, 0, reached={"wood"})
    assert m.key == "kit" and unmet == [{"item": "family:food", "count": 8}]
    sat["kit"] = [True] * 6
    # lookahead skips free-slot, dimension and judged targets and reached rungs
    ahead = ladder.upcoming(ladder.by_key("stone_tools"), sat, 1, reached={"wood"})
    assert ahead and all(not t.get("freeSlots") for t in ahead)
    assert ladder.upcoming(ladder.by_key("kit"), sat, 1, reached={"wood"})[0]["item"] == "stone_pickaxe"
    assert "1/" in ladder.progress(nothing, reached={"wood"})
    end_sat = {m.key: [True] * len(m.targets) for m in ladder.milestones}
    end_sat["dragon"] = [False]
    m, unmet = ladder.next_unmet(end_sat, 0)
    assert m.key == "dragon" and unmet[0].get("judged")


def test_brain_runs_milestones_from_the_ladder():
    """The brain asks Node which targets hold, takes the first unmet milestone, and records it as a node."""

    class _LadderEnv(_StubEnv):
        def __init__(self):
            super().__init__()
            self.inventory = {"oak_log": 8, "crafting_table": 1, "wooden_pickaxe": 1}
            self.checks = []

        def fast_subgoals(self, text, target=None, check=None, targets=None, lookahead=None, **kw):
            if check is not None:
                self.checks.append(len(check))

                def ok(t):
                    if t.get("nearBlock"):
                        return True
                    if t.get("freeSlots"):
                        return True
                    if not t.get("item"):
                        return False
                    key = t["item"]
                    if key == "family:pickaxe":
                        have = sum(c for n, c in self.inventory.items() if n.endswith("_pickaxe"))
                    elif key.startswith("*_"):
                        have = sum(c for n, c in self.inventory.items() if n.endswith(key[1:]))
                    elif key.startswith("family:"):
                        have = 0
                    else:
                        have = self.inventory.get(key, 0)
                    return have >= t["count"]

                return {"satisfied": [ok(t) for t in check], "candidates": [], "targets": []}
            self.subgoal_requests.append((text, targets, lookahead))
            return {"target": {"item": "+".join(t["item"] for t in targets or []), "need": 1, "have": 0, "gained": 0},
                    "targets": [{"item": t["item"], "need": t["count"]} for t in (targets or [])],
                    "candidates": [{"text": "Mine 8 oak_log", "target": {"item": "oak_log", "count": 8}, "why": "kit"}]}

        def fast_status(self):
            self.polls += 1
            goal = self.goals[-1]
            if self.polls == 1:
                return self._status(highTargets=[{"item": "*_log", "need": 16, "have": 8, "gained": 0}])
            return self._status(
                triggers=[{"type": "goal_reached", "goalId": goal["id"], "detail": {"target": {"item": "oak_log"}}},
                          {"type": "high_goal_reached", "goalId": goal["id"], "detail": {}}],
                highGoalReached=True, highTargets=[{"item": "*_log", "need": 16, "have": 16, "gained": 8}],
                trace=[{"action": "mine:oak_log", "outcome": "ok", "gain": 1, "ms": 500, "tool": None}],
                collapsedTrace=[{"action": "mine:oak_log", "times": 8}],
            )

    with tempfile.TemporaryDirectory() as d:
        env = _LadderEnv()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0)
        brain.last_status = env._status()
        milestone, unmet, lookahead = brain._next_milestone()
        assert milestone.key == "kit"  # wood, wooden tools and home hold, so the kit comes first
        assert env.checks and env.checks[0] > 20
        assert [t["item"] for t in lookahead] == ["stone_pickaxe", "stone_axe", "stone_sword"]
        info = brain.run_high_goal(milestone.name, milestone.description, targets=unmet, lookahead=lookahead, milestone=milestone)
        assert info["success"] is True
        text, targets, ahead = env.subgoal_requests[0]
        assert text == "Basic kit" and targets and ahead and ahead[0]["item"] == "stone_pickaxe"
        assert env.goals[0]["highGoal"]["targets"] == targets
        node = brain.skills.skills["milestone:kit"]
        assert node["kind"] == "high" and node["verify"]["kind"] == "milestone" and "oak_log" in node["children"]


def test_home_milestone_records_and_sends_home():
    class _HomeEnv(_StubEnv):
        def __init__(self):
            super().__init__()
            self.home_sent = []

        def fast_home(self, pos=None):
            self.home_sent.append(pos)
            return {"home": pos}

        def fast_subgoals(self, text, target=None, **kw):
            return {"target": None, "targets": [{"item": "near:chest", "need": 1}],
                    "candidates": [{"text": "Place the chest", "target": {"nearBlock": "chest"}, "why": "home"}]}

        def fast_status(self):
            self.polls += 1
            goal = self.goals[-1]
            if self.polls == 1:
                return self._status(highTargets=[{"item": "near:chest", "need": 1, "have": 0, "gained": 0}])
            return self._status(
                triggers=[{"type": "goal_reached", "goalId": goal["id"], "detail": {"target": {"item": "near:chest"}}},
                          {"type": "high_goal_reached", "goalId": goal["id"], "detail": {}}],
                highGoalReached=True, highTargets=[{"item": "near:chest", "need": 1, "have": 1, "gained": 1}],
                landmarks={"chest": {"x": 10, "y": 64, "z": -5, "distance": 2}, "crafting_table": {"x": 11, "y": 64, "z": -5, "distance": 3}},
                trace=[{"action": "place:chest", "outcome": "ok", "gain": 1, "ms": 900, "tool": None}],
                collapsedTrace=[{"action": "place:chest", "times": 1}],
            )

    from voyager.fast.milestones import Ladder

    with tempfile.TemporaryDirectory() as d:
        env = _HomeEnv()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0)
        brain.last_status = env._status()
        home = Ladder().by_key("home")
        info = brain.run_high_goal(home.name, home.description, targets=home.targets, milestone=home)
        assert info["success"] is True
        assert brain.home == {"x": 10, "y": 64, "z": -5}
        assert env.home_sent == [{"x": 10, "y": 64, "z": -5}]
        assert os.path.exists(os.path.join(d, "fast", "home.json"))
        assert "milestone:home" in brain.skills.skills and "home" in brain._reached_milestones()
        # a new brain on the same checkpoint sends the remembered home on start
        env2 = _HomeEnv()
        v2, _, _ = _voyager(env2, d)
        brain2 = FastBrain(v2, poll_seconds=0.0)
        assert brain2.home == {"x": 10, "y": 64, "z": -5}


def test_failed_rung_escalates_kit_then_one_step():
    from voyager.fast.milestones import Ladder

    ladder = Ladder()
    sat = {m.key: [False] * len(m.targets) for m in ladder.milestones}
    reached = {"wood", "wooden_tools"}
    assert ladder.next_unmet(sat, 0, reached)[0].key == "home"
    # five failures: the brain skips the rung; the ladder steps exactly one rung past it, then comes back
    ladder.skip("home", 0, 6)
    assert ladder.next_unmet(sat, 2, reached)[0].key == "stone_tools"
    assert ladder.next_unmet(sat, 7, reached)[0].key == "home"
    # never two rungs past: with stone tools also skipped, stone tools is returned (one step only)
    ladder.skip("stone_tools", 0, 6)
    assert ladder.next_unmet(sat, 2, reached)[0].key == "stone_tools"


def test_brain_forces_kit_after_three_failures():
    class _KitEnv(_StubEnv):
        def fast_subgoals(self, text, target=None, check=None, targets=None, lookahead=None, **kw):
            if check is not None:
                def ok(t):
                    if t.get("nearBlock"):
                        return False  # home never holds
                    if t.get("freeSlots"):
                        return True
                    return bool(t.get("item")) and t["item"] in ("*_log", "wooden_pickaxe", "crafting_table", "family:pickaxe")
                return {"satisfied": [ok(t) for t in check], "candidates": [], "targets": []}
            return {"target": None, "targets": [], "candidates": [{"text": text, "target": {"none": True}, "why": "direct"}]}

    with tempfile.TemporaryDirectory() as d:
        env = _KitEnv()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0)
        brain.skills.record(goal="Wood", target={"milestone": "wood", "targets": []}, kind="high", high_goal=None, fingerprint=FP, seconds=1, trace=[], collapsed=[], inventory_before={}, inventory_after={})
        brain.skills.record(goal="Wooden tools", target={"milestone": "wooden_tools", "targets": []}, kind="high", high_goal=None, fingerprint=FP, seconds=1, trace=[], collapsed=[], inventory_before={}, inventory_after={})
        m, unmet, ahead = brain._next_milestone()
        assert m.key == "home"
        brain.milestone_failures["home"] = 3
        brain._force_kit = True
        m, unmet, ahead = brain._next_milestone()
        assert m.key == "kit" and len(unmet) == len(m.targets)  # the whole kit, not only the unmet part
        m, _, _ = brain._next_milestone()
        assert m.key == "home"  # back to the rung afterwards


def test_smelt_queued_parks_the_subgoal_and_picks_another():
    class _SmeltEnv(_StubEnv):
        def fast_subgoals(self, text, target=None, **kw):
            self.subgoal_requests.append(text)
            return {"target": {"item": "iron_pickaxe", "need": 1, "have": 0, "gained": 0},
                    "targets": [{"item": "iron_pickaxe", "need": 1}],
                    "candidates": [
                        {"text": "Smelt 3 raw_iron", "target": {"item": "iron_ingot", "count": 3}, "why": "ingots"},
                        {"text": "Obtain 2 stick", "target": {"item": "stick", "count": 2}, "why": "handle"},
                    ]}

        def fast_status(self):
            self.polls += 1
            goal = self.goals[-1]
            if self.polls == 1:
                return self._status(triggers=[{"type": "smelt_queued", "goalId": goal["id"], "detail": {"input": "raw_iron", "output": "iron_ingot", "count": 3, "readyInSeconds": 30}}])
            return self._status(triggers=[{"type": "no_progress", "goalId": goal["id"], "detail": {}}])

    with tempfile.TemporaryDirectory() as d:
        env = _SmeltEnv()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0, subgoal_failures_before_fail=1)
        brain.last_status = env._status()
        brain.run_high_goal("Iron tools", "", targets=[{"item": "iron_pickaxe", "count": 1}])
        # first pick was the smelt (jev disabled -> first candidate); after the batch is queued the sticks come next
        assert [g["text"] for g in env.goals[:2]] == ["Smelt 3 raw_iron", "Obtain 2 stick"]
        assert brain._deferred["Smelt 3 raw_iron"] > 0


def test_player_chat_queues_directives_disjoint_from_the_ladder():
    """A player line during a ladder goal queues a directive (Jev disabled: heuristic);
    an urgent 'stop' during a chat goal interrupts it without any ladder bookkeeping."""

    class _ChatEnv(_StubEnv):
        def __init__(self):
            super().__init__()
            self.said = []
            self.script = []  # per poll: chat lines to deliver

        def fast_say(self, text):
            self.said.append(text)

        def fast_subgoals(self, text, target=None, **kw):
            self.subgoal_requests.append(text)
            return {"target": {"item": "oak_log", "need": 4, "have": 0, "gained": 0},
                    "targets": [{"item": "oak_log", "need": 4}],
                    "candidates": [{"text": "Mine 4 oak_log", "target": {"item": "oak_log", "count": 4}, "why": "asked"}]}

        def fast_status(self):
            self.polls += 1
            goal = self.goals[-1]
            chat = self.script.pop(0) if self.script else []
            if chat:
                return self._status(chat=chat, botName="bot2")
            return self._status(
                triggers=[{"type": "goal_reached", "goalId": goal["id"], "detail": {"target": {"item": "oak_log"}}},
                          {"type": "high_goal_reached", "goalId": goal["id"], "detail": {}}],
                highGoalReached=True, botName="bot2",
                trace=[{"action": "mine:oak_log", "outcome": "ok", "gain": 1, "ms": 500, "tool": None}],
                collapsedTrace=[{"action": "mine:oak_log", "times": 4}],
            )

    with tempfile.TemporaryDirectory() as d:
        env = _ChatEnv()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0)
        brain.last_status = env._status()
        # poll 1: two player lines; one is addressed to another bot and must be ignored
        env.script = [[{"from": "cason", "text": "bot3: come here"}, {"from": "cason", "text": "get me 4 oak logs"}]]
        info = brain.run_high_goal("Wood", "ladder rung", targets=[{"item": "oak_log", "count": 4}])
        assert info["success"] is True and info["interrupted"] is False
        assert [dd["text"] for dd in brain.directives] == ["get me 4 oak logs"]
        assert any(s.startswith("Queued: get me 4 oak logs") for s in env.said)
        assert logged and logged[-1][0] in ("fast_goals", "chat_directive")
        # the directive runs as its own high goal; a 'stop' line interrupts it
        directive = brain.directives.pop(0)
        env.script = [[{"from": "cason", "text": "stop"}]]
        info = brain.run_high_goal(directive["text"], "player asked", source="chat")
        assert info["interrupted"] is True and info["success"] is False
        assert brain.directives == [] and "Stopping; back to my own goals" in env.said
        assert env.goals[-1]["highGoal"]["source"] == "chat"
        # an interrupted goal is not a skill-tree node and not a failed task
        assert all(n["goal"] != "get me 4 oak logs" for n in brain.skills.skills.values())
        assert brain.failed_history == {}


def test_chat_stop_on_a_ladder_goal_does_not_interrupt():
    class _ChatEnv(_StubEnv):
        def __init__(self):
            super().__init__()
            self.said = []

        def fast_say(self, text):
            self.said.append(text)

        def fast_status(self):
            self.polls += 1
            goal = self.goals[-1]
            if self.polls == 1:
                return self._status(chat=[{"from": "cason", "text": "never mind, stop"}])
            self.inventory = {"oak_planks": 1, "wooden_pickaxe": 1}
            return self._status(
                triggers=[{"type": "goal_reached", "goalId": goal["id"], "detail": {"target": {"item": "wooden_pickaxe"}}},
                          {"type": "high_goal_reached", "goalId": goal["id"], "detail": {}}],
                highGoalReached=True,
                trace=[{"action": "craft:wooden_pickaxe", "outcome": "ok", "gain": 1, "ms": 1200, "tool": None}],
                collapsedTrace=[{"action": "craft:wooden_pickaxe", "times": 1}],
            )

    with tempfile.TemporaryDirectory() as d:
        env = _ChatEnv()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0)
        brain.last_status = env._status()
        brain.directives.append({"text": "old request", "from": "cason", "at": 0})
        info = brain.run_high_goal("Craft a wooden pickaxe", "ladder")
        assert info["success"] is True and info["interrupted"] is False
        assert brain.directives == []  # stop clears the queue even while on the ladder
        assert "No player requests pending; I am on my own goals" in env.said


def test_chat_directive_is_decomposed_into_ordered_verifiable_steps():
    """'bring me 4 oak logs' -> item step (planned through recipes), nearPlayer step, give step;
    steps run in order, player-relative steps are not skill nodes, the last step ends the goal."""

    class _FakeLLM:
        def __call__(self, messages):
            return _Stub(content='here you go:\n[{"text": "Collect 4 oak logs", "target": {"item": "oak_log", "count": 4}},'
                                 ' {"text": "Walk to Cason", "target": {"nearPlayer": "cason", "distance": 3}},'
                                 ' {"text": "Hand over the logs", "target": {"give": {"item": "oak_log", "count": 4, "to": "cason"}}},'
                                 ' {"text": "bogus", "target": {"teleport": true}}]')

    class _ChatEnv(_StubEnv):
        def __init__(self):
            super().__init__()
            self.inventory = {}
            self.said = []
            self.checks = []

        def fast_say(self, text):
            self.said.append(text)

        def fast_subgoals(self, text, target=None, check=None, targets=None, **kw):
            if check is not None:
                self.checks.append(check)
                return {"satisfied": [bool(t.get("item")) and self.inventory.get(t["item"], 0) >= t["count"] for t in check]}
            self.subgoal_requests.append((text, targets))
            return {"target": {"item": "oak_log", "need": 4, "have": self.inventory.get("oak_log", 0), "gained": 0},
                    "targets": [{"item": "oak_log", "need": 4}],
                    "candidates": [{"text": "Mine 4 oak_log", "target": {"item": "oak_log", "count": 4}, "why": "trees nearby"}]}

        def fast_status(self):
            self.polls += 1
            goal = self.goals[-1]
            if self.polls == 1:
                self.inventory = {"oak_log": 4}
                return self._status(triggers=[{"type": "goal_reached", "goalId": goal["id"], "detail": {"target": {"item": "oak_log"}}}],
                                    trace=[{"action": "mine:oak_log", "outcome": "ok", "gain": 1, "ms": 500, "tool": None}],
                                    collapsedTrace=[{"action": "mine:oak_log", "times": 4}], players=["cason"])
            return self._status(triggers=[{"type": "goal_reached", "goalId": goal["id"], "detail": {"target": {"item": goal["target"]["nearPlayer"] if goal.get("target", {}).get("nearPlayer") else "give"}}}],
                                trace=[{"action": "goto:player", "outcome": "ok", "gain": 0, "ms": 500, "tool": None}],
                                collapsedTrace=[{"action": "goto:player", "times": 1}], players=["cason"])

    with tempfile.TemporaryDirectory() as d:
        env = _ChatEnv()
        v, recorded, logged = _voyager(env, d)
        v.curriculum_agent.qa_llm = _FakeLLM()
        brain = FastBrain(v, poll_seconds=0.0)
        brain.last_status = env._status(players=["cason"])
        info = brain.run_high_goal("bring me 4 oak logs", "player asked", source="chat", requester="cason")
        assert info["success"] is True and info["interrupted"] is False
        posted = [(g["text"], g.get("target")) for g in env.goals]
        assert posted == [
            ("Mine 4 oak_log", {"item": "oak_log", "count": 4}),  # the item step went through the planner
            ("Walk to Cason", {"nearPlayer": "cason", "distance": 3}),
            ("Hand over the logs", {"give": {"item": "oak_log", "count": 4, "to": "cason"}}),
        ]
        assert env.subgoal_requests[0] == ("Collect 4 oak logs", [{"item": "oak_log", "count": 4}])
        assert [s[0] for s in logged if s[0] == "chat_decompose"]
        goals = {n["goal"] for n in brain.skills.skills.values()}
        assert "Mine 4 oak_log" in goals and "Walk to Cason" not in goals and "Hand over the logs" not in goals


def test_parse_steps_falls_back_and_normalises_players():
    steps = FastBrain._parse_steps('[{"text": "come", "target": {"nearPlayer": "CASON", "distance": 2}}, {"text": "x", "target": null}]', "cason", [])
    assert steps == [{"text": "come", "target": {"nearPlayer": "cason", "distance": 2}}, {"text": "x", "target": {"judged": True}}]
    assert FastBrain._parse_steps("no json here", "cason", []) == []
    assert FastBrain._parse_steps('[{"text": "y", "target": {"nearPlayer": "nobody"}}]', "cason", ["ann"]) == [{"text": "y", "target": {"nearPlayer": "cason", "distance": 3}}]


def test_interrupted_directive_is_requeued_behind_the_newer_one():
    """Jev disabled: the heuristic never interrupts, so drive learn()'s directive branch directly."""

    class _Env(_StubEnv):
        def __init__(self):
            super().__init__()
            self.said = []

        def fast_say(self, text):
            self.said.append(text)

    with tempfile.TemporaryDirectory() as d:
        env = _Env()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0)
        brain.directives = [{"text": "get me stone", "from": "cason", "at": 1}]
        brain._interrupt = {"reason": "new_task", "text": "get me stone"}
        directive = {"text": "get me wood", "from": "cason", "at": 0}
        # the same bookkeeping learn() does after run_high_goal returned interrupted
        info = {"task": directive["text"], "success": False, "interrupted": True}
        if info.get("interrupted") and (brain._interrupt or {}).get("reason") == "new_task":
            brain.directives.insert(min(1, len(brain.directives)), directive)
        assert [dd["text"] for dd in brain.directives] == ["get me stone", "get me wood"]
    # malformed numbers from the model fall back to defaults instead of aborting the directive
    steps = FastBrain._parse_steps('[{"text": "a", "target": {"item": "oak_log", "count": "lots"}}, {"text": "b", "target": {"nearPlayer": "cason", "distance": null}}]', "cason", [])
    assert steps[0]["target"] == {"item": "oak_log", "count": 1} and steps[1]["target"]["distance"] == 3


def test_action_generator_static_checks_and_module_parsing():
    from voyager.fast.generator import extract_module, static_check

    good = "// name: swim_ashore\nfunction menu(snap, loop) { return snap.inWater ? 'swim' : null; }\n" \
           "async function execute(bot, loop, ctx) { for (let i = 0; i < 2; i++) { await bot.lookAt(bot.entity.position); } return 'ok'; }"
    assert static_check("swim_ashore", good) is None
    assert "forbidden" in static_check("x1", good.replace("return 'ok'", "process.exit(); return 'ok'"))
    assert "without await" in static_check("x1", good.replace("await bot.lookAt(bot.entity.position);", "i++;"))
    assert "synchronous" in static_check("x1", good.replace("function menu", "async function menu"))
    assert "name" in static_check("Bad-Name", good)
    assert "missing" in static_check("x1", "async function execute() { return 'ok' }")
    name, source = extract_module("Here you go:\n```javascript\n" + good + "\n```\nenjoy")
    assert name == "swim_ashore" and source.startswith("// name: swim_ashore") and source.endswith("return 'ok'; }")
    assert extract_module("no code at all")[0] is None


def test_action_generator_posts_a_primitive_and_persists_it():
    """A stall with Jev disabled and few verbs tried counts as a gap; GPT's module is checked, posted, saved."""
    import time as _time
    from voyager.fast.generator import ActionGenerator

    module = "// name: hop_gap\nfunction menu(snap, loop) { return 'Hop the gap'; }\nasync function execute(bot, loop, ctx) { await bot.lookAt(bot.entity.position); return 'ok, hopped'; }"

    class _LLM:
        def __init__(self):
            self.calls = 0

        def __call__(self, messages):
            self.calls += 1
            return _Stub(content="```javascript\n" + module + "\n```")

    class _Env(_StubEnv):
        def __init__(self):
            super().__init__()
            self.primitives = []

        def fast_primitive(self, name, source, remove=False):
            self.primitives.append((name, source))
            return {"ok": True, "primitives": [{"name": name, "trials": 0, "successes": 0, "failures": 0, "retired": False}]}

    with tempfile.TemporaryDirectory() as d:
        env = _Env()
        v, recorded, logged = _voyager(env, d)
        v.action_agent.llm = _LLM()
        brain = FastBrain(v, poll_seconds=0.0, generate_actions=True)
        gen = brain.generator
        high = {"id": "high-1", "text": "Wood"}
        sub = {"text": "Mine 8 oak_log"}
        status = env._status(recentActions=[{"action": "mine:oak_log", "outcome": "timeout"}] * 3)
        assert gen.on_stall(high, sub, status) is False  # first stall: not yet
        assert gen.on_stall(high, sub, status) is True  # second stall: a gap, generation starts
        for _ in range(100):
            if not gen.busy:
                break
            _time.sleep(0.02)
        assert env.primitives and env.primitives[0][0] == "hop_gap"
        assert os.path.exists(os.path.join(d, "fast", "primitives", "hop_gap.js"))
        assert any("x:hop_gap registered" in m for m in gen.drain())
        assert any(g == "action_generated" for g, _ in logged)
        # cooldown: the same subgoal does not trigger again right away
        assert gen.on_stall(high, sub, status) is False
        # a fresh brain on the same checkpoint re-registers the saved action
        env2 = _Env()
        v2, _, _ = _voyager(env2, d)
        brain2 = FastBrain(v2, poll_seconds=0.0)
        assert brain2.generator.reload() == 1 and env2.primitives[0][0] == "hop_gap"


def test_action_generator_respects_live_cap_and_rejections():
    from voyager.fast.generator import ActionGenerator

    class _Env(_StubEnv):
        def fast_primitive(self, name, source, remove=False):
            return {"ok": False, "error": "compile error: oops"}

    class _LLM:
        def __call__(self, messages):
            return _Stub(content="```js\nfunction menu(){ return 'x' }\nasync function execute(){ return 'ok' }\n```")

    with tempfile.TemporaryDirectory() as d:
        env = _Env()
        v, recorded, logged = _voyager(env, d)
        v.action_agent.llm = _LLM()
        brain = FastBrain(v, poll_seconds=0.0)
        gen = brain.generator
        gen.stalls_before = 1
        live = [{"name": f"p{i}", "retired": False} for i in range(8)]
        assert gen.on_stall({"id": "h", "text": "Wood"}, {"text": "Mine 8 oak_log"}, env._status(primitives=live)) is False
        assert any("live already" in m for m in gen.drain())
        # a Node rejection is retried once with the error, then given up
        assert gen.on_stall({"id": "h", "text": "Wood"}, {"text": "Obtain 2 stick"}, env._status()) is True
        import time as _time
        for _ in range(100):
            if not gen.busy:
                break
            _time.sleep(0.02)
        msgs = gen.drain()
        assert sum("rejected" in m for m in msgs) == 2
        assert any(g == "action_generation_failed" for g, _ in logged)


def test_no_candidates_never_posts_the_high_goal_name():
    """Every planner step in the failed set: offer one again; with no steps at all,
    post the first unmet target; a target already failed means the goal is exhausted."""

    class _Env(_StubEnv):
        def __init__(self):
            super().__init__()
            self.cands = [{"text": "Craft 3 oak_planks", "target": {"item": "oak_planks", "count": 8}, "why": "chest"}]
            self.checks = []

        def fast_subgoals(self, text, target=None, check=None, targets=None, **kw):
            if check is not None:
                self.checks.append(check)
                return {"satisfied": [False for _ in check]}
            return {"target": {"item": "near:chest", "need": 1, "have": 0, "gained": 0}, "targets": [{"item": "near:chest", "need": 1}], "candidates": self.cands}

    with tempfile.TemporaryDirectory() as d:
        env = _Env()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0)
        high = {"id": "high-1", "text": "Home base", "targets": [{"nearBlock": "chest"}]}
        status = env._status()
        pick = brain._select_subgoal(high, "ctx", status, {"Craft 3 oak_planks"})
        assert pick["text"] == "Craft 3 oak_planks"  # offered again rather than the bare goal name
        env.cands = []
        pick = brain._select_subgoal(high, "ctx", status, set())
        assert pick["text"] == "Place the chest" and pick["target"] == {"nearBlock": "chest"}
        pick = brain._select_subgoal(high, "ctx", status, {"Place the chest"})
        assert pick.get("exhausted") is True
        # run_high_goal ends at once on an exhausted selection instead of running the name to a timeout
        brain._select_subgoal = lambda *a, **k: {"text": "Home base", "target": None, "why": "exhausted", "skill": None, "exhausted": True}
        info = brain.run_high_goal("Home base", "ctx", targets=[{"nearBlock": "chest"}])
        assert info["success"] is False and env.polls == 0
        assert all(g["text"] != "Home base" for g in env.goals)


def test_generated_action_outcomes_are_logged_and_repaired_once():
    """A failed generated action is sent back to GPT once with its outcome; a repaired or
    successful one is left alone; outcomes are logged for review."""
    import time as _time

    class _LLM:
        def __init__(self):
            self.prompts = []

        def __call__(self, messages):
            self.prompts.append(messages[-1].content)
            return _Stub(content="```javascript\n// name: hop\nfunction menu(snap, loop) { return 'Hop'; }\nasync function execute(bot, loop, ctx) { await bot.toss(1, null, 1); return 'ok'; }\n```")

    class _Env(_StubEnv):
        def __init__(self):
            super().__init__()
            self.posted = []

        def fast_primitive(self, name, source, remove=False, repaired=False):
            self.posted.append((name, repaired))
            return {"ok": True, "primitives": []}

    with tempfile.TemporaryDirectory() as d:
        env = _Env()
        v, recorded, logged = _voyager(env, d)
        v.action_agent.llm = _LLM()
        brain = FastBrain(v, poll_seconds=0.0)
        gen = brain.generator
        gen.registered["hop"] = "function menu(){ return 'x' }\nasync function execute(bot){ bot.toss(1, null, 1, () => {}); return 'ok'; }"
        prims = [{"name": "hop", "trials": 1, "successes": 0, "failures": 1, "retired": False, "repaired": False}]
        gen.on_outcomes([{"name": "hop", "outcome": "timeout", "gain": 0, "ms": 20000, "ok": False, "goal": "Free up 4 inventory slots"}], env._status(primitives=prims))
        for _ in range(100):
            if not gen.busy:
                break
            _time.sleep(0.02)
        assert env.posted == [("hop", True)]
        assert "timeout" in v.action_agent.llm.prompts[0] and "callback" in v.action_agent.llm.prompts[0]
        assert any(g == "action_outcome" for g, _ in logged) and any(g == "action_repaired" for g, _ in logged)
        assert os.path.exists(os.path.join(d, "fast", "primitives", "hop.js"))
        # already repaired: no second attempt; a success never triggers one
        prims[0]["repaired"] = True
        gen.on_outcomes([{"name": "hop", "outcome": "failed: x", "gain": 0, "ms": 10, "ok": False}], env._status(primitives=prims))
        gen.on_outcomes([{"name": "hop", "outcome": "ok", "gain": 1, "ms": 10, "ok": True}], env._status(primitives=prims))
        assert len(env.posted) == 1
        tallies = gen._outcomes_by_action([{"action": "mine:oak_log", "outcome": "timeout"}, {"action": "mine:oak_log", "outcome": "timeout"}, {"action": "walk:north", "outcome": "ok"}])
        assert tallies == {"mine:oak_log": {"timeout": 2}, "walk:north": {"ok": 1}}
