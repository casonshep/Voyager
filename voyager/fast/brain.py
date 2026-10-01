"""FastBrain: a high goal from the curriculum, a subgoal chosen by Jev.

Timescales
----------
* **Fast (Node, every action):** ``lib/fastLoop.js`` snapshots the world,
  builds a menu of bounded primitives, lets Jev pick one, executes it, and
  repeats. It raises triggers (subgoal reached, high goal reached, no
  progress, hazard, stuck).
* **Medium (Python, per subgoal):** this class asks Node for candidate
  subgoals derived from recipes and the world, merges in remembered skills
  for this situation, and asks Jev to pick one. The pick becomes the fast
  loop's goal. A reached subgoal is recorded as a skill.
* **Slow (Python, per high goal):** the curriculum (GPT) proposes the next
  high goal. This is the only GPT call in fast mode. While it runs the bot
  works on a standing goal, and the server is never paused.

The old ``/step`` route (GPT-written programs, GPT critic) is not used here.
"""

from __future__ import annotations

import copy
import time
from typing import Any

import voyager.utils as U
from voyager.typesafe import build_state, critic_shadow
from voyager.utils import timing

from .skills import SkillMemory, target_key

try:
    from typesafe_sdk import Choice
except Exception:  # the client is disabled in this case
    Choice = None  # type: ignore[assignment]

STANDING_GOAL = {
    "id": "standing",
    "text": "Gather useful nearby resources (wood, cobblestone, food) and stay safe",
    "context": (
        "A filler goal while the planner thinks. Prefer wood if there is little, "
        "then stone, eat when hungry, avoid hostiles and lava."
    ),
    "kind": "standing",
    "target": {"none": True},
    "noProgressSeconds": 3600,
    "noProgressActions": 100000,
}


def _matcher(key: str):
    """Mirror of matcherFor in fastLoop.js: '*_log' matches any *_log item."""
    if key.startswith("*_"):
        suffix = key[2:]
        return lambda name: name == suffix or name.endswith(f"_{suffix}")
    return lambda name: name == key


class FastBrain:
    def __init__(
        self,
        voyager,
        *,
        poll_seconds: float = 1.0,
        goal_timeout_seconds: float = 300,
        subgoal_failures_before_fail: int = 3,
        reset_env: bool = True,
    ):
        self.v = voyager
        self.env = voyager.env
        self.env.pause_server = False
        self.poll_seconds = poll_seconds
        self.goal_timeout_seconds = goal_timeout_seconds
        self.subgoal_failures_before_fail = subgoal_failures_before_fail
        self.reset_env = reset_env
        U.f_mkdir(f"{voyager.ckpt_dir}/fast")
        self.skills = SkillMemory(f"{voyager.ckpt_dir}/fast/skills.json")
        self.last_events: list = []
        self.last_status: dict[str, Any] = {}
        self.goal_counter = 0

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def learn(self):
        v = self.v
        if v.resume:
            v.env.reset(options={"mode": "soft", "wait_ticks": v.env_wait_ticks})
        else:
            v.env.reset(options={"mode": "hard", "wait_ticks": v.env_wait_ticks})
            v.resume = True
        # keep the bot busy from the first second; the status poll seeds the curriculum
        self.env.fast_goal(STANDING_GOAL)
        self.last_status = self.env.fast_status()
        self.last_events = self._merge_events([], self.last_status.get("events") or [])

        while True:
            if v.recorder.iteration > v.max_iterations:
                print("Iteration limit reached")
                break
            v.jev.iteration = v.recorder.iteration
            with timing.timed("brain.propose_task"):
                task, context = v.curriculum_agent.propose_next_task(
                    events=self.last_events,
                    chest_observation=v.action_agent.render_chest_observation(),
                    chest_memory=v.action_agent.chest_memory,
                    max_retries=5,
                )
            print(f"\033[35mFast brain: high goal '{task}'\033[0m")
            try:
                info = self.run_high_goal(task, context)
            except Exception as exc:  # keep the lifelong loop alive like Voyager.learn
                print(f"\033[41mHigh goal '{task}' aborted: {exc}\033[0m")
                info = {"task": task, "success": False}
                time.sleep(3)
            v.curriculum_agent.update_exploration_progress(info)
            print(f"\033[35mCompleted tasks: {', '.join(v.curriculum_agent.completed_tasks)}\033[0m")
            print(f"\033[35mFailed tasks: {', '.join(v.curriculum_agent.failed_tasks)}\033[0m")
            print(f"\033[35mSkills known: {len(self.skills.skills)}\033[0m")
            print(f"\033[90m[timing] summary: {timing.summary()}\033[0m")
            self.env.fast_goal(STANDING_GOAL)

        self.env.fast_stop()
        return {
            "completed_tasks": v.curriculum_agent.completed_tasks,
            "failed_tasks": v.curriculum_agent.failed_tasks,
            "skills": self.skills.skills,
        }

    # ------------------------------------------------------------------ #
    # one high goal: a sequence of Jev-selected subgoals
    # ------------------------------------------------------------------ #
    def run_high_goal(self, task: str, context: str) -> dict[str, Any]:
        v = self.v
        self.goal_counter += 1
        high = {"id": f"high-{self.goal_counter}", "text": task}
        started = time.time()
        failed_subgoals: set[str] = set()
        subgoal_failures = 0
        success = False
        events: list = list(self.last_events)
        status = self.last_status
        subgoal_log: list[dict[str, Any]] = []

        subgoal = self._select_subgoal(high, context, status, failed_subgoals)
        run = self._start_subgoal(high, context, subgoal, status)

        while True:
            time.sleep(self.poll_seconds)
            status = self.env.fast_status()
            self.last_status = status
            events = self._merge_events(events, status.get("events") or [])
            if status.get("highGoalReached") or any(
                t["type"] == "high_goal_reached" for t in status.get("triggers") or []
            ):
                success = True
            for trig in status.get("triggers") or []:
                if trig.get("goalId") not in (run["goal"]["id"], None):
                    continue
                kind = trig["type"]
                detail = trig.get("detail") or {}
                if kind == "goal_reached":
                    confirmed = self._confirm(subgoal["text"], context, events, detail, run["inventory_before"])
                    if confirmed:
                        skill = self._record_skill(high, subgoal, run, status, events)
                        subgoal_log.append({"subgoal": subgoal["text"], "ok": True, "seconds": skill["last_seconds"]})
                    else:
                        print("\033[33mFast brain: subgoal completion not confirmed\033[0m")
                    if not success:
                        subgoal = self._select_subgoal(high, context, status, failed_subgoals)
                        run = self._start_subgoal(high, context, subgoal, status)
                elif kind == "no_progress":
                    subgoal_failures += 1
                    failed_subgoals.add(subgoal["text"])
                    subgoal_log.append({"subgoal": subgoal["text"], "ok": False})
                    if subgoal.get("skill"):
                        self.skills.note_failure(subgoal["target"], run["fingerprint"])
                    print(
                        f"\033[33mFast brain: subgoal '{subgoal['text']}' stalled "
                        f"({subgoal_failures}/{self.subgoal_failures_before_fail})\033[0m"
                    )
                    if subgoal_failures >= self.subgoal_failures_before_fail:
                        break
                    subgoal = self._select_subgoal(high, context, status, failed_subgoals)
                    run = self._start_subgoal(high, context, subgoal, status)
                elif kind in ("hazard", "stuck"):
                    print(f"\033[33mFast brain: {kind} {detail}\033[0m")
            if success or subgoal_failures >= self.subgoal_failures_before_fail:
                break
            if time.time() - started > self.goal_timeout_seconds:
                print(f"\033[33mFast brain: high goal '{task}' timed out\033[0m")
                break

        # A high goal without a parseable target is judged like a subgoal
        if not success and status.get("highTarget") is None and status.get("goalReached"):
            success = self._confirm(task, context, events, {}, self._inventory(self.last_events))

        elapsed = time.time() - started
        self.last_events = events
        v.recorder.record(events, task)
        print(
            f"\033[35mFast brain: high goal '{task}' {'succeeded' if success else 'failed'} "
            f"in {elapsed:.0f}s over {len(subgoal_log)} subgoals\033[0m"
        )
        v.jev.log_only(
            "fast_goals",
            {
                "task": task,
                "success": success,
                "seconds": round(elapsed, 1),
                "high_target": status.get("highTarget"),
                "subgoals": subgoal_log,
                "stats": status.get("stats"),
            },
        )
        return {"task": task, "success": success}

    # ------------------------------------------------------------------ #
    # subgoal selection
    # ------------------------------------------------------------------ #
    def _select_subgoal(self, high, context, status, failed: set[str]) -> dict[str, Any]:
        """Derived candidates from Node plus remembered skills; Jev picks one."""
        fingerprint = status.get("fingerprint")
        derived = self.env.fast_subgoals(high["text"])
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for c in derived.get("candidates") or []:
            if c["text"] in failed:
                continue
            candidates.append({"text": c["text"], "target": c.get("target"), "why": c.get("why", ""), "skill": None})
            seen.add(target_key(c.get("target")))
        for s in self.skills.matching(fingerprint):
            if s["subgoal"] in failed or s["failures"] > s["successes"]:
                continue
            tk = target_key(s.get("target"))
            # a remembered skill for a derived target replaces the derived entry's replay
            for c in candidates:
                if target_key(c["target"]) == tk and c["skill"] is None:
                    c["skill"] = s
                    break
            else:
                if tk not in seen and self._skill_relevant(s, high["text"]):
                    candidates.append({"text": s["subgoal"], "target": s.get("target"), "why": "remembered", "skill": s})
                    seen.add(tk)
        if not candidates:
            return {"text": high["text"], "target": None, "why": "nothing derived", "skill": None}
        if len(candidates) == 1:
            return candidates[0]
        pick = self._jev_pick(high, context, status, candidates)
        return pick or candidates[0]

    @staticmethod
    def _skill_relevant(skill, high_text: str) -> bool:
        """A remembered skill from another high goal is offered only when it shares a word."""
        words = {w for w in high_text.lower().replace("_", " ").split() if len(w) > 3}
        skill_words = set(" ".join([skill["subgoal"], *skill.get("high_goals", [])]).lower().replace("_", " ").split())
        return bool(words & skill_words)

    def _jev_pick(self, high, context, status, candidates):
        jev = self.v.jev
        if not jev.enabled or Choice is None:
            return None
        criteria = {}
        for i, c in enumerate(candidates):
            label = f"c{i}"
            desc = c["why"] or None
            if c["skill"]:
                s = c["skill"]
                desc = (
                    f"{desc + '; ' if desc else ''}remembered skill: done {s['successes']} time(s) "
                    f"in about {s['seconds']}s using {', '.join(s['tools_used']) or 'no tools'}"
                )
            criteria[label] = {"subgoal": c["text"], "note": desc} if desc else {"subgoal": c["text"]}
        state = {
            "high_goal": high["text"],
            "context": context,
            "high_goal_progress": status.get("highTarget"),
            "inventory": status.get("inventory") or {},
            "fingerprint": status.get("fingerprint"),
            "recent_actions": status.get("recentActions") or [],
            "candidates": {k: v for k, v in criteria.items()},
        }
        response = jev.ask(
            "subgoal_select",
            state,
            {
                "subgoal": Choice(
                    instructions=(
                        "A Minecraft bot pursues `high_goal` (`context` explains it; "
                        "`high_goal_progress` is the exact count still needed, when known). "
                        "`candidates` are the subgoals it could work on next; each is one "
                        "bounded step the bot can start right now, and remembered skills "
                        "note how they went before. Given `inventory` and `recent_actions`, "
                        "which candidate is the best next step toward the high goal?"
                    ),
                    criteria=criteria,
                )
            },
            extra_record={"candidates": [c["text"] for c in candidates]},
        )
        if response is None:
            return None
        try:
            label = response.choices["subgoal"].choice
            conf = response.choices["subgoal"].confidence
        except Exception:
            return None
        idx = int(label[1:]) if label.startswith("c") and label[1:].isdigit() else -1
        if not 0 <= idx < len(candidates):
            return None
        print(f"\033[36mFast brain: Jev picked subgoal '{candidates[idx]['text']}' (confidence {conf:.2f})\033[0m")
        return candidates[idx]

    def _start_subgoal(self, high, context, subgoal, status) -> dict[str, Any]:
        goal = {
            "id": f"{high['id']}-sub-{int(time.time() * 1000) % 100000000}",
            "text": subgoal["text"],
            "context": context,
            "kind": "task",
            "highGoal": high,
        }
        if subgoal.get("target"):
            goal["target"] = subgoal["target"]
        if subgoal.get("skill") and subgoal["skill"].get("actions"):
            goal["replay"] = list(subgoal["skill"]["actions"])
        posted = self.env.fast_goal(goal)
        print(
            f"\033[36mFast brain: subgoal '{subgoal['text']}'"
            f"{' (replaying ' + str(posted.get('replayRemaining')) + ' steps)' if posted.get('replayRemaining') else ''}\033[0m"
        )
        return {
            "goal": goal,
            "started": time.time(),
            "fingerprint": status.get("fingerprint"),
            "inventory_before": status.get("inventory") or self._inventory(self.last_events),
        }

    def _record_skill(self, high, subgoal, run, status, events) -> dict[str, Any]:
        skill = self.skills.record(
            high_goal=high["text"],
            subgoal=subgoal["text"],
            target=subgoal.get("target") or (status.get("target") and {"item": status["target"]["item"]}),
            fingerprint=run["fingerprint"],
            seconds=time.time() - run["started"],
            trace=status.get("trace") or [],
            collapsed=status.get("collapsedTrace") or [],
            inventory_before=run["inventory_before"],
            inventory_after=status.get("inventory") or self._inventory(events),
        )
        print(
            f"\033[32mFast brain: skill recorded '{subgoal['text']}' in {skill['last_seconds']}s "
            f"with {', '.join(skill['tools_used']) or 'no tools'} ({skill['successes']} success(es))\033[0m"
        )
        return skill

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _confirm(self, text, context, events, detail, inventory_before) -> bool:
        if detail.get("target"):
            return True  # exact count in code
        if not self.v.jev.enabled or not events:
            return True
        try:
            state = build_state(
                events,
                task=text,
                context=context,
                chest_memory=self.v.action_agent.chest_memory,
                completed_tasks=self.v.curriculum_agent.completed_tasks,
                failed_tasks=self.v.curriculum_agent.failed_tasks,
                inventory_before=inventory_before,
            )
            result = critic_shadow(self.v.jev, state, threshold=self.v.typesafe_critic_threshold)
        except Exception as exc:
            print(f"\033[31mFast brain: critic check failed: {exc}\033[0m")
            return True
        if result is None:
            return True
        return result.success >= self.v.typesafe_critic_threshold

    @staticmethod
    def _inventory(events) -> dict:
        for event_type, event in reversed(events or []):
            if event_type == "observe":
                return copy.deepcopy(event.get("inventory") or {})
        return {}

    @staticmethod
    def _merge_events(events, new_events) -> list:
        """Append new events; the critic/curriculum need the last entry to be 'observe'."""
        merged = [e for e in events if e[0] != "observe"]
        merged.extend(tuple(e) for e in new_events)
        if not merged or merged[-1][0] != "observe":
            for e in reversed(events or []):
                if e[0] == "observe":
                    merged.append(e)
                    break
        if len(merged) > 201:
            merged = merged[-201:]
        return merged
