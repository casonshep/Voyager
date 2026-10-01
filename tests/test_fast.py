"""Offline tests for fast mode (no Minecraft, no API keys).

Run with the venv python:  venv/Scripts/python tests/run_tests.py
"""
import json
import os
import tempfile

from voyager.fast.skills import SkillMemory, expand, fingerprint_key, target_key
from voyager.fast.brain import FastBrain, _matcher

FP = {"biome": "plains", "toolTier": "none", "daylight": "day"}


def test_skill_record_fields_and_replay_choice():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fast", "skills.json")
        mem = SkillMemory(path)
        trace = [
            {"action": "walk:north", "outcome": "ok", "gain": 0, "ms": 3000, "tool": None},
            {"action": "mine:oak_log", "outcome": "ok", "gain": 1, "ms": 4000, "tool": "wooden_axe"},
            {"action": "mine:oak_log", "outcome": "ok", "gain": 1, "ms": 4000, "tool": "wooden_axe"},
        ]
        skill = mem.record(
            high_goal="Craft a wooden pickaxe",
            subgoal="Obtain 2 oak_log",
            target={"item": "oak_log", "count": 2},
            fingerprint=FP,
            seconds=12.3,
            trace=trace,
            collapsed=[{"action": "walk:north", "times": 1}, {"action": "mine:oak_log", "times": 2}],
            inventory_before={"stick": 1},
            inventory_after={"stick": 1, "oak_log": 2},
        )
        # the fields the user asked for
        assert skill["high_goal"] == "Craft a wooden pickaxe"
        assert skill["subgoal"] == "Obtain 2 oak_log"
        assert skill["seconds"] == 12.3
        assert skill["tools_used"] == ["wooden_axe"]
        assert skill["actions"] == ["walk:north", "mine:oak_log", "mine:oak_log"]
        assert skill["inventory_delta"] == {"oak_log": 2}
        assert skill["fingerprint"] == FP and skill["successes"] == 1
        # same target, another high goal, shorter sequence -> merged and replay shortened
        mem.record(
            high_goal="Mine 3 wood logs", subgoal="Obtain 2 oak_log", target={"item": "oak_log", "count": 2},
            fingerprint=FP, seconds=8.0, trace=[], collapsed=[{"action": "mine:oak_log", "times": 2}],
            inventory_before={}, inventory_after={"oak_log": 2},
        )
        s = mem.matching(FP)[0]
        assert s["successes"] == 2 and sorted(s["high_goals"]) == ["Craft a wooden pickaxe", "Mine 3 wood logs"]
        assert s["actions"] == ["mine:oak_log", "mine:oak_log"] and s["seconds"] == 8.0
        assert mem.matching({**FP, "daylight": "night"}) == []
        mem.note_failure({"item": "oak_log", "count": 2}, FP)
        assert mem.skills[s["id"]]["failures"] == 1 and mem.skills[s["id"]]["attempts"] == 3
        assert SkillMemory(path).matching(FP)[0]["id"] == s["id"]
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh)


def test_keys_and_helpers():
    assert expand([{"action": "a", "times": 2}, {"action": "b"}]) == ["a", "a", "b"]
    assert fingerprint_key(None) == "?|?|?"
    assert target_key({"item": "*_log", "count": 3}) == "*_log"
    assert target_key({"nearBlock": "iron_ore"}) == "near:iron_ore"
    assert target_key({"none": True}) == "none" and target_key(None) == "none"
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

    def fast_subgoals(self, text, target=None):
        self.subgoal_requests.append(text)
        if "oak_planks" in self.inventory:
            cands = [{"text": "Craft 1 wooden_pickaxe", "target": {"item": "wooden_pickaxe", "count": 1}, "why": "all ingredients present"}]
        else:
            cands = [
                {"text": "Obtain 3 oak_planks", "target": {"item": "oak_planks", "count": 3}, "why": "ingredient"},
                {"text": "Obtain 2 stick", "target": {"item": "stick", "count": 2}, "why": "ingredient"},
            ]
        cands.append({"text": text, "target": {"item": "wooden_pickaxe", "count": 1}, "why": "work on the high goal directly"})
        return {"target": {"item": "wooden_pickaxe", "need": 1, "have": 0, "gained": 0}, "candidates": cands}

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
        action_agent=_Stub(chest_memory={}),
        curriculum_agent=_Stub(completed_tasks=[], failed_tasks=[]),
    )
    return v, recorded, logged


def test_high_goal_runs_subgoals_and_records_skills():
    with tempfile.TemporaryDirectory() as d:
        env = _StubEnv()
        v, recorded, logged = _voyager(env, d)
        brain = FastBrain(v, poll_seconds=0.0)
        assert env.pause_server is False
        brain.last_status = env._status()
        info = brain.run_high_goal("Craft a wooden pickaxe", "needs planks and sticks")
        assert info == {"task": "Craft a wooden pickaxe", "success": True}
        # subgoal selection happened per subgoal and every posted goal carried the high goal
        assert env.subgoal_requests == ["Craft a wooden pickaxe"] * 3
        assert all(g["highGoal"]["text"] == "Craft a wooden pickaxe" for g in env.goals)
        assert env.goals[0]["text"] == "Obtain 3 oak_planks" and env.goals[0]["target"] == {"item": "oak_planks", "count": 3}
        # the stalled subgoal is not offered again
        assert env.goals[1]["text"] == "Craft 1 wooden_pickaxe" and env.goals[2]["text"] == "Craft a wooden pickaxe"
        # two completed subgoals became skills with the requested fields
        skills = list(brain.skills.skills.values())
        assert {s["subgoal"] for s in skills} == {"Obtain 3 oak_planks", "Craft a wooden pickaxe"}
        planks = next(s for s in skills if s["subgoal"] == "Obtain 3 oak_planks")
        assert planks["high_goal"] == "Craft a wooden pickaxe" and planks["actions"] == ["craft:oak_planks"]
        assert isinstance(planks["seconds"], float) and planks["tools_used"] == []
        assert os.path.exists(os.path.join(d, "fast", "skills.json"))
        assert recorded and recorded[0][0] == "Craft a wooden pickaxe"
        assert logged[-1][0] == "fast_goals" and logged[-1][1]["success"] and len(logged[-1][1]["subgoals"]) == 3
        # a later high goal in the same situation offers the remembered skill as a replay
        env2 = _StubEnv()
        env2.inventory = {}
        brain.env = env2
        brain.last_status = env2._status()
        sub = brain._select_subgoal({"id": "h2", "text": "Craft a wooden pickaxe"}, "", env2._status(), set())
        assert sub["text"] == "Obtain 3 oak_planks" and sub["skill"]["actions"] == ["craft:oak_planks"]
        run = brain._start_subgoal({"id": "h2", "text": "Craft a wooden pickaxe"}, "", sub, env2._status())
        assert env2.goals[-1]["replay"] == ["craft:oak_planks"] and run["fingerprint"] == FP
