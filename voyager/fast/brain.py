"""FastBrain: the slow-timescale goal loop that drives the Node fast loop.

Timescales
----------
* **Fast (Node, ~every action):** ``lib/fastLoop.js`` snapshots the world,
  builds a menu of bounded primitives, lets Jev pick one, executes it, and
  repeats. It raises triggers (goal reached, no progress, hazard, stuck).
* **Slow (Python, per goal):** this class asks the curriculum for the next
  task, hands it to the loop as a goal, and polls status about once a second,
  draining triggers. The Minecraft server is never paused: while GPT proposes
  the next task the bot works on a standing goal.

Success is decided in code (exact target count from the loop) with Jev's
critic judgment as the check for goals without a parseable target. The GPT
action agent is the rare path: after repeated no-progress triggers it writes
one program, which runs through the ordinary ``/step`` route, then the goal
resumes in the fast loop.
"""

from __future__ import annotations

import copy
import time
from typing import Any

import voyager.utils as U
from voyager.typesafe import build_state, critic_shadow
from voyager.utils import timing

from .sequences import SequenceMemory

def _matcher(key: str):
    """Mirror of matcherFor in fastLoop.js: '*_log' matches any *_log item."""
    if key.startswith("*_"):
        suffix = key[2:]
        return lambda name: name == suffix or name.endswith(f"_{suffix}")
    return lambda name: name == key


STANDING_GOAL = {
    "id": "standing",
    "text": "Gather useful nearby resources (wood, cobblestone, food) and stay safe",
    "context": (
        "A filler goal while the planner thinks. Prefer wood if there is little, "
        "then stone, eat when hungry, avoid hostiles and lava."
    ),
    "kind": "standing",
    "target": {"none": True},
    "noProgressSeconds": 600,
}


class FastBrain:
    def __init__(
        self,
        voyager,
        *,
        poll_seconds: float = 1.0,
        goal_timeout_seconds: float = 300,
        stalls_before_gpt: int = 2,
        stalls_before_fail: int = 4,
        gpt_fallback: bool = True,
        reset_env: bool = True,
    ):
        self.v = voyager
        self.env = voyager.env
        self.env.pause_server = False
        self.poll_seconds = poll_seconds
        self.goal_timeout_seconds = goal_timeout_seconds
        self.stalls_before_gpt = stalls_before_gpt
        self.stalls_before_fail = stalls_before_fail
        self.gpt_fallback = gpt_fallback
        self.reset_env = reset_env
        self.sequences = SequenceMemory(f"{voyager.ckpt_dir}/fast/sequences.json")
        U.f_mkdir(f"{voyager.ckpt_dir}/fast")
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
        self.last_events = self.env.step("")  # one observation to seed the curriculum
        self.env.fast_goal(STANDING_GOAL)  # keep the bot busy while the first task is proposed

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
            print(f"\033[35mFast brain: starting goal '{task}'\033[0m")
            try:
                info = self.run_task(task, context)
            except Exception as exc:  # keep the lifelong loop alive like Voyager.learn
                print(f"\033[41mGoal '{task}' aborted: {exc}\033[0m")
                info = {"task": task, "success": False}
                time.sleep(3)
                try:
                    self.env.fast_stop()
                except Exception:
                    pass
            if info["success"] and info.get("program_code"):
                v.skill_manager.add_new_skill(info)
            v.curriculum_agent.update_exploration_progress(info)
            print(f"\033[35mCompleted tasks: {', '.join(v.curriculum_agent.completed_tasks)}\033[0m")
            print(f"\033[35mFailed tasks: {', '.join(v.curriculum_agent.failed_tasks)}\033[0m")
            print(f"\033[90m[timing] summary: {timing.summary()}\033[0m")
            # back to the standing goal while the curriculum picks the next task
            self.env.fast_goal(STANDING_GOAL)

        self.env.fast_stop()
        return {
            "completed_tasks": v.curriculum_agent.completed_tasks,
            "failed_tasks": v.curriculum_agent.failed_tasks,
            "skills": v.skill_manager.skills,
            "sequences": self.sequences.entries,
        }

    # ------------------------------------------------------------------ #
    # one goal
    # ------------------------------------------------------------------ #
    def run_task(self, task: str, context: str) -> dict[str, Any]:
        v = self.v
        self.goal_counter += 1
        goal_id = f"goal-{self.goal_counter}"
        start_fp = self.last_status.get("fingerprint")
        goal = {
            "id": goal_id,
            "text": task,
            "context": context,
            "kind": "task",
            "sequenceLibrary": self.sequences.library_for(start_fp),
        }
        status = self.env.fast_goal(goal)
        if status.get("replayRemaining"):
            print(
                f"\033[36mFast brain: replaying {status['replayRemaining']} remembered steps "
                f"for {status.get('replaySource')}\033[0m"
            )
        inventory_before = self._inventory(self.last_events)
        started = time.time()
        stalls = 0
        success = False
        program: dict[str, Any] | None = None
        events: list = list(self.last_events)

        while True:
            time.sleep(self.poll_seconds)
            status = self.env.fast_status()
            self.last_status = status
            events = self._merge_events(events, status.get("events") or [])
            for trig in status.get("triggers") or []:
                if trig.get("goalId") not in (goal_id, None):
                    continue
                kind = trig["type"]
                detail = trig.get("detail") or {}
                if kind == "goal_reached":
                    success = self._confirm_success(task, context, events, status, detail, inventory_before)
                    if success:
                        break
                    # Jev disagreed with a judged (target-less) completion: keep going
                    print("\033[33mFast brain: completion not confirmed, continuing\033[0m")
                    self.env.fast_goal({**goal, "id": goal_id, "sequenceLibrary": {}})
                elif kind == "no_progress":
                    stalls += 1
                    print(f"\033[33mFast brain: no progress ({stalls}) on '{task}'\033[0m")
                    if stalls >= self.stalls_before_fail:
                        break
                    if self.gpt_fallback and stalls >= self.stalls_before_gpt:
                        program, events, done = self._gpt_assist(goal, task, context, events, status)
                        if done:
                            success = True
                            break
                elif kind in ("hazard", "stuck"):
                    print(f"\033[33mFast brain: {kind} {detail}\033[0m")
            if success or stalls >= self.stalls_before_fail:
                break
            if time.time() - started > self.goal_timeout_seconds:
                print(f"\033[33mFast brain: goal '{task}' timed out\033[0m")
                break

        elapsed = time.time() - started
        self.last_events = events
        v.recorder.record(events, task)
        target = status.get("target") or {}
        print(
            f"\033[35mFast brain: '{task}' {'succeeded' if success else 'failed'} in {elapsed:.0f}s, "
            f"{(status.get('stats') or {}).get('actions', 0)} actions, "
            f"{(status.get('stats') or {}).get('decisions', 0)} Jev decisions\033[0m"
        )
        if success and target.get("item"):
            if status.get("replaySource") and not status.get("replayRemaining"):
                self.sequences.note_replay(target["item"], start_fp)
            self.sequences.store(target["item"], start_fp, status.get("collapsedTrace") or [], elapsed)
        v.jev.log_only(
            "fast_goals",
            {
                "task": task,
                "success": success,
                "seconds": round(elapsed, 1),
                "target": target,
                "stats": status.get("stats"),
                "trace": status.get("trace"),
                "fingerprint": start_fp,
                "stalls": stalls,
                "gpt_program": bool(program),
            },
        )
        info = {"task": task, "success": success}
        if success and program:
            info.update(program)
        return info

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _confirm_success(self, task, context, events, status, detail, inventory_before) -> bool:
        if detail.get("target"):
            return True  # exact count in code
        # Judged completion (no parseable target): confirm with Jev's critic
        if not self.v.jev.enabled or not events:
            return True
        try:
            state = build_state(
                events,
                task=task,
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

    def _gpt_assist(self, goal, task, context, events, status):
        """Rare path: one GPT-written program through /step, then resume the goal.

        Returns ``(program_info | None, events, done)``.
        """
        v = self.v
        target = status.get("target") or {}
        print(f"\033[34mFast brain: asking the action agent for a program for '{task}'\033[0m")
        skills = v.skill_manager.retrieve_skills(query=context)
        system_message = v.action_agent.render_system_message(skills=skills)
        human_message = v.action_agent.render_human_message(
            events=events, code="", task=task, context=context, critique=""
        )
        with timing.timed("brain.gpt_program"):
            ai_message = v.action_agent.llm([system_message, human_message])
        parsed = v.action_agent.process_ai_message(message=ai_message)
        if not isinstance(parsed, dict):
            print(f"\033[34m{parsed}\033[0m")
            self.env.fast_goal({**goal, "sequenceLibrary": {}, "target": self._remaining_target(target)})
            return None, events, False
        code = parsed["program_code"] + "\n" + parsed["exec_code"]
        # /step stops the fast loop itself; the server is not paused in fast mode
        new_events = self.env.step(code, programs=v.skill_manager.programs)
        v.action_agent.update_chest_memory(new_events[-1][1]["nearbyChests"])
        events = self._merge_events(events, new_events)
        program = {"program_code": parsed["program_code"], "program_name": parsed["program_name"]}
        remaining = self._remaining_target(target, self._inventory(new_events))
        if remaining is not None and remaining["count"] <= 0:
            return program, events, True
        self.env.fast_goal({**goal, "sequenceLibrary": {}, **({"target": remaining} if remaining else {})})
        return program, events, False

    @staticmethod
    def _remaining_target(target, inventory=None):
        """Target override for re-posting a goal so progress made so far is kept."""
        if not target or not target.get("item"):
            return None
        need = int(target.get("need", 1))
        gained = int(target.get("gained", 0))
        if inventory is not None:
            matches = _matcher(target["item"])
            have_now = sum(c for n, c in inventory.items() if matches(n))
            start_have = int(target.get("have", 0)) - gained
            gained = have_now - start_have
        return {"item": target["item"], "count": need - gained}

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
        # cap memory: keep the last 200 non-observe events plus the final observe
        if len(merged) > 201:
            merged = merged[-201:]
        return merged
