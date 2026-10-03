"""FastBrain: a high goal from the curriculum, subgoals chosen by Jev, a skill tree.

Timescales
----------
* **Fast (Node, every action):** ``lib/fastLoop.js`` snapshots the world,
  builds a menu of bounded primitives, lets Jev pick one, executes it, and
  repeats. It raises triggers (subgoal reached, high goal reached, no
  progress, hazard, stuck, subgoal obsolete).
* **Medium (Python, per subgoal):** this class asks Node for candidate
  subgoals derived from recipes and the world for the current high goal,
  annotates each with the skill node already learned for it (if any), and
  asks Jev to pick one. A reached subgoal becomes (or updates) a skill node.
* **Slow (Python, per high goal):** the curriculum (GPT) proposes the next
  high goal. This is the only GPT call in fast mode. A proposal is rejected
  and re-asked when it is already a learned node or already satisfied by the
  world. When a high goal is reached it becomes a node connected to the
  subgoal nodes used on the way (the skill tree).

* **Player chat (any time):** lines typed by players arrive with each status
  poll. Jev judges each one (a new task for this bot, stop, or not a command)
  and whether it should interrupt the current high goal. Tasks go on a
  **directive queue that is disjoint from the ladder**: a directive runs as
  its own high goal, with none of the ladder's failure bookkeeping, and the
  ladder resumes afterwards. A directive is decomposed once by the QA model
  into an ordered list of small steps, each with a target code can verify
  (item count, block nearby, within N blocks of a player, items handed to a
  player) or, failing that, a statement Jev judges. Item steps are planned
  through the recipe graph like ladder targets; the others post as-is.

The Minecraft server is never paused by the brain; ``/pause`` in Minecraft
idles the loop and holds the brain's clocks.
"""

from __future__ import annotations

import copy
import re
import time
import traceback
from typing import Any

import voyager.utils as U
from voyager.typesafe import build_state, critic_shadow
from voyager.utils import timing

from .generator import ActionGenerator
from .milestones import Ladder, target_label
from .skills import SkillMemory, target_key

try:
    from typesafe_sdk import Choice, Noul
except Exception:  # the client is disabled in this case
    Choice = Noul = None  # type: ignore[assignment]

STANDING_GOAL = {
    "id": "standing",
    "text": "Gather useful nearby resources (wood, cobblestone, food) and stay safe",
    "context": (
        "A filler goal while the planner thinks. Prefer wood if there is little, "
        "then stone, eat when hungry, avoid hostiles and lava. With no food at all, "
        "hunt a cow, pig, sheep or chicken whenever one is visible. Put junk in a chest "
        "or throw it away when the bag is nearly full."
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
        long_term_goal: str | None = None,
        failed_task_cooldown: int = 10,
        max_proposal_rejections: int = 3,
        use_curriculum: bool = False,
        milestone_skip_subgoals: int = 6,
        generate_actions: bool = True,
    ):
        # Goals come from the milestone ladder (voyager/fast/milestones.py); the
        # GPT curriculum is only used when use_curriculum=True or the ladder is done.
        self.use_curriculum = use_curriculum
        self.ladder = Ladder()
        self.milestone_skip_subgoals = milestone_skip_subgoals
        self.milestone_failures: dict[str, int] = {}
        self.v = voyager
        self.env = voyager.env
        self.env.pause_server = False
        self.poll_seconds = poll_seconds
        self.goal_timeout_seconds = goal_timeout_seconds
        self.subgoal_failures_before_fail = subgoal_failures_before_fail
        self.reset_env = reset_env
        self.max_proposal_rejections = max_proposal_rejections
        U.f_mkdir(f"{voyager.ckpt_dir}/fast")
        self.skills = SkillMemory(f"{voyager.ckpt_dir}/fast/skills.json")
        # The long-term goal steers the curriculum's proposals and Jev's subgoal picks.
        self.long_term_goal = long_term_goal
        voyager.curriculum_agent.long_term_goal = long_term_goal
        # A failed high goal is hidden from the curriculum until this many more
        # subgoals have been completed; the wait doubles on each repeat failure.
        self.failed_task_cooldown = failed_task_cooldown
        self._failed_path = f"{voyager.ckpt_dir}/fast/failed_tasks.json"
        self.failed_history: dict[str, dict[str, Any]] = (
            U.load_json(self._failed_path) if U.f_exists(self._failed_path) else {}
        )
        self.subgoals_completed = sum(
            s.get("successes", 0) for s in self.skills.skills.values() if s.get("kind") == "sub"
        )
        self.last_events: list = []
        self.last_status: dict[str, Any] = {}
        self.goal_counter = 0
        self._last_chests = None
        self._learned: dict[str, list[dict[str, Any]]] = {}
        self._decision_stats: list[tuple[float | None, int]] = []
        self._deferred: dict[str, float] = {}  # subgoal text -> time it may be offered again (batch cooking)
        self.directives: list[dict[str, Any]] = []  # player commands waiting to run, oldest first
        # Fills action gaps with GPT-written primitives the bot process registers (voyager/fast/generator.py)
        self.generator = ActionGenerator(self, enabled=generate_actions)
        self._interrupt: dict[str, Any] | None = None  # set by chat handling to end the current high goal
        self.chat_interrupt_threshold = 0.45
        self._home_path = f"{voyager.ckpt_dir}/fast/home.json"
        self.home: dict[str, Any] | None = U.load_json(self._home_path) if U.f_exists(self._home_path) else None

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
        if self.home:
            try:
                self.env.fast_home(self.home)
            except Exception as exc:
                print(f"\033[31mFast brain: could not send home base: {exc}\033[0m")
        try:
            self.generator.reload()
        except Exception as exc:
            print(f"\033[31mFast brain: could not reload generated actions: {exc}\033[0m")
        # keep the bot busy from the first second; the status poll seeds the curriculum
        self.env.fast_goal(STANDING_GOAL)
        self.last_status = self.env.fast_status()
        self.last_events = self._merge_events([], self.last_status.get("events") or [])

        while True:
            if v.recorder.iteration > v.max_iterations:
                print("Iteration limit reached")
                break
            v.jev.iteration = v.recorder.iteration
            self._release_cooled_failures()
            milestone = None
            directive = self.directives.pop(0) if self.directives else None
            if directive is not None:
                task = directive["text"]
                context = (
                    f"A player ({directive['from']}) asked for this in chat. Do what they asked, "
                    "nothing more; the ladder resumes afterwards."
                )
                print(f"\033[35mFast brain: player directive '{task}' (from {directive['from']})\033[0m")
                self._say(f"On it: {task}")
                try:
                    info = self.run_high_goal(task, context, source="chat", requester=directive["from"])
                except Exception as exc:
                    print(f"\033[41mDirective '{task}' aborted: {exc}\033[0m")
                    traceback.print_exc()
                    info = {"task": task, "success": False}
                    time.sleep(3)
                # disjoint from the ladder: no milestone bookkeeping, no failed-task cooldown
                if info.get("interrupted"):
                    if (self._interrupt or {}).get("reason") == "new_task":
                        # paused behind the request that interrupted it, not lost
                        self.directives.insert(min(1, len(self.directives)), directive)
                        self._say(f"Paused '{task}'; back to it after the newer request")
                    # a stop was already acknowledged and cleared the queue
                else:
                    self._say(f"{'Done' if info['success'] else 'Could not finish'}: {task}")
                self.env.fast_goal(STANDING_GOAL)
                continue
            if not self.use_curriculum:
                milestone, unmet, lookahead = self._next_milestone()
            if milestone is not None:
                task = milestone.name
                context = milestone.description + (f" {milestone.notes}" if milestone.notes else "")
                print(
                    f"\033[35mFast brain: milestone '{task}' -> {', '.join(target_label(t) for t in unmet)}"
                    f"{' | looking ahead to ' + ', '.join(target_label(t) for t in lookahead) if lookahead else ''}\033[0m"
                )
            else:
                if not self.use_curriculum:
                    print("\033[35mFast brain: the milestone ladder is complete or paused; asking the curriculum\033[0m")
                task, context = self._propose_new_high_goal()
                unmet, lookahead = None, None
                print(f"\033[35mFast brain: high goal '{task}'\033[0m")
            try:
                info = self.run_high_goal(task, context, targets=unmet, lookahead=lookahead, milestone=milestone)
            except Exception as exc:  # keep the lifelong loop alive like Voyager.learn
                print(f"\033[41mHigh goal '{task}' aborted: {exc}\033[0m")
                traceback.print_exc()
                info = {"task": task, "success": False}
                time.sleep(3)
            if info.get("interrupted"):
                print(f"\033[33mFast brain: '{task}' interrupted by a player request; the ladder resumes after it\033[0m")
                self.env.fast_goal(STANDING_GOAL)
                continue
            v.curriculum_agent.update_exploration_progress(info)
            if milestone is not None and not info["success"]:
                n = self.milestone_failures.get(milestone.key, 0) + 1
                self.milestone_failures[milestone.key] = n
                if n >= 5:
                    # five straight failures: let the ladder step one rung past this one for a while
                    self.ladder.skip(milestone.key, self.subgoals_completed, self.milestone_skip_subgoals)
                    print(
                        f"\033[41mFast brain: milestone '{task}' failed {n} times; stepping one rung past it "
                        f"for {self.milestone_skip_subgoals} subgoals, then back\033[0m"
                    )
                elif n >= 3:
                    self._force_kit = True
                    print(f"\033[41mFast brain: milestone '{task}' failed {n} times; running the kit before retrying\033[0m")
                else:
                    print(f"\033[33mFast brain: milestone '{task}' failed ({n}x); retrying\033[0m")
            if milestone is not None and info["success"]:
                self.milestone_failures.pop(milestone.key, None)
            if milestone is None and not info["success"]:
                self._note_failed(task)
            print(f"\033[35mCompleted tasks: {', '.join(v.curriculum_agent.completed_tasks)}\033[0m")
            print(f"\033[35mFailed tasks: {', '.join(v.curriculum_agent.failed_tasks)}\033[0m")
            print(f"\033[35mSkill tree: {len(self.skills.skills)} nodes\033[0m")
            print(f"\033[90m[timing] summary: {timing.summary()}\033[0m")
            self.env.fast_goal(STANDING_GOAL)

        self.env.fast_stop()
        return {
            "completed_tasks": v.curriculum_agent.completed_tasks,
            "failed_tasks": v.curriculum_agent.failed_tasks,
            "skills": self.skills.skills,
        }

    # ------------------------------------------------------------------ #
    # milestone ladder
    # ------------------------------------------------------------------ #
    def _milestone_satisfaction(self) -> dict[str, list[bool]]:
        """Exact per-target checks for every milestone, in one round trip to Node."""
        flat: list[tuple[str, int, dict]] = []
        for m in self.ladder.milestones:
            for i, t in enumerate(m.targets):
                flat.append((m.key, i, t))
        checkable = [t for _, _, t in flat if not t.get("judged")]
        try:
            res = self.env.fast_subgoals("", check=checkable) if checkable else {}
            flags = list(res.get("satisfied") or [])
        except Exception as exc:
            print(f"\033[31mFast brain: milestone check failed: {exc}\033[0m")
            flags = []
        out: dict[str, list[bool]] = {m.key: [False] * len(m.targets) for m in self.ladder.milestones}
        it = iter(flags)
        for key, i, t in flat:
            if t.get("judged"):
                # a judged target counts once the tree has it as a reached high goal
                out[key][i] = self.skills.known(t["judged"], None)
            else:
                out[key][i] = bool(next(it, False))
        return out

    def _reached_milestones(self) -> set[str]:
        return {
            n["id"].split(":", 1)[1]
            for n in self.skills.skills.values()
            if n.get("kind") == "high" and n["id"].startswith("milestone:")
        }

    def _next_milestone(self):
        satisfied = self._milestone_satisfaction()
        reached = self._reached_milestones()
        print(f"\033[35mFast brain: {self.ladder.progress(satisfied, reached)}\033[0m")
        if getattr(self, "_force_kit", False):
            # a rung keeps failing: run the kit in full (all its targets) before retrying
            self._force_kit = False
            kit = self.ladder.by_key("kit")
            if kit is not None:  # restocking never depends on later rungs
                lookahead = self.ladder.upcoming(kit, satisfied, 1, reached)
                return kit, list(kit.targets), lookahead
        milestone, unmet = self.ladder.next_unmet(satisfied, self.subgoals_completed, reached)
        if milestone is None:
            return None, [], []
        lookahead = self.ladder.upcoming(milestone, satisfied, 1, reached)
        return milestone, unmet, lookahead

    def _remember_home(self, status) -> None:
        """The home milestone is reached: the chest's position is home from now on."""
        landmarks = status.get("landmarks") or {}
        spot = landmarks.get("chest") or landmarks.get("crafting_table")
        if not spot:
            return
        self.home = {"x": spot["x"], "y": spot["y"], "z": spot["z"]}
        U.dump_json(self.home, self._home_path)
        try:
            self.env.fast_home(self.home)
        except Exception as exc:
            print(f"\033[31mFast brain: could not send home base: {exc}\033[0m")
        print(f"\033[32mFast brain: home base recorded at {self.home}\033[0m")

    # ------------------------------------------------------------------ #
    # high goal acceptance: a new node, not already learned or satisfied
    # ------------------------------------------------------------------ #
    def _propose_new_high_goal(self) -> tuple[str, str]:
        v = self.v
        rejected: list[str] = []
        task = context = None
        for attempt in range(self.max_proposal_rejections + 1):
            feedback = None
            if rejected:
                feedback = (
                    "These proposals were rejected: " + "; ".join(rejected) + ". "
                    "Propose a different task that is new and not yet satisfied by my inventory. "
                    "Respond in the same format."
                )
            with timing.timed("brain.propose_task"):
                task, context = v.curriculum_agent.propose_next_task(
                    events=self.last_events,
                    chest_observation=v.action_agent.render_chest_observation(),
                    chest_memory=v.action_agent.chest_memory,
                    max_retries=5,
                    extra_feedback=feedback,
                )
            reason = self._reject_reason(task)
            if reason is None or attempt == self.max_proposal_rejections:
                if reason:
                    print(f"\033[33mFast brain: accepting '{task}' anyway ({reason})\033[0m")
                return task, context
            print(f"\033[33mFast brain: rejected '{task}': {reason}\033[0m")
            rejected.append(f'"{task}" because {reason}')
        return task, context

    def _reject_reason(self, task: str) -> str | None:
        """Why a proposed high goal is not a new node, or None when it is acceptable."""
        try:
            derived = self.env.fast_subgoals(task)
        except Exception as exc:
            print(f"\033[31mFast brain: could not inspect '{task}': {exc}\033[0m")
            return None
        summary = derived.get("target")
        target = {"item": summary["item"], "count": summary["need"]} if summary else None
        if summary and summary.get("have", 0) >= summary.get("need", 1):
            return f"my inventory already holds {summary['have']} {summary['item']} (need {summary['need']})"
        if self.skills.known(task, target):
            node = self.skills.node(task, target)
            return f"it is already a learned skill ('{node['goal']}', reached {node['successes']} time(s))"
        return None

    # ------------------------------------------------------------------ #
    # failed-task cooldown
    # ------------------------------------------------------------------ #
    def _note_failed(self, task: str) -> None:
        entry = self.failed_history.get(task) or {"failures": 0}
        entry["failures"] += 1
        entry["failed_at_subgoals"] = self.subgoals_completed
        entry["retry_after_subgoals"] = (
            self.subgoals_completed + self.failed_task_cooldown * (2 ** (entry["failures"] - 1))
        )
        self.failed_history[task] = entry
        U.dump_json(self.failed_history, self._failed_path)
        print(
            f"\033[33mFast brain: '{task}' may be proposed again after "
            f"{entry['retry_after_subgoals'] - self.subgoals_completed} more completed subgoals\033[0m"
        )

    def _release_cooled_failures(self) -> None:
        """Let the curriculum see a failed task again once its cooldown has passed."""
        curriculum = self.v.curriculum_agent
        released = [
            task
            for task in list(curriculum.failed_tasks)
            if (entry := self.failed_history.get(task)) and self.subgoals_completed >= entry["retry_after_subgoals"]
        ]
        if not released:
            return
        curriculum.failed_tasks = [t for t in curriculum.failed_tasks if t not in released]
        U.dump_json(curriculum.failed_tasks, f"{curriculum.ckpt_dir}/curriculum/failed_tasks.json")
        print(f"\033[33mFast brain: eligible to retry: {', '.join(released)}\033[0m")

    # ------------------------------------------------------------------ #
    # one high goal: a sequence of Jev-selected subgoals
    # ------------------------------------------------------------------ #
    def run_high_goal(
        self, task: str, context: str, targets=None, lookahead=None, milestone=None, source: str = "ladder", requester=None
    ) -> dict[str, Any]:
        v = self.v
        self.goal_counter += 1
        high = {"id": f"high-{self.goal_counter}", "text": task, "source": source}
        if requester:
            high["requester"] = requester
        self._interrupt = None
        if targets:
            high["targets"] = [t for t in targets if not t.get("judged")] or None
            high["judged"] = [t["judged"] for t in targets if t.get("judged")]
            if not high["targets"]:
                del high["targets"]
        if lookahead:
            high["lookahead"] = lookahead
        started = time.time()
        failed_subgoals: set[str] = set()
        subgoal_failures = 0
        success = False
        interrupted = False
        events: list = list(self.last_events)
        status = self.last_status
        subgoal_log: list[dict[str, Any]] = []
        used_nodes: list[str] = []  # skill nodes reached on the way: the high goal's children
        high_inventory_before = self._inventory(events)
        high_fingerprint = status.get("fingerprint")
        high_target: dict | None = None
        self._learned[high["id"]] = []  # requirements discovered while executing (see _learn_from_chat)

        subgoal = self._select_subgoal(high, context, status, failed_subgoals)
        run = self._start_subgoal(high, context, subgoal, status)

        paused_since: float | None = None
        while True:
            if subgoal.get("exhausted"):
                print(f"\033[41mFast brain: no step left for '{task}'; failing it now\033[0m")
                subgoal_failures = self.subgoal_failures_before_fail
                break
            time.sleep(self.poll_seconds)
            status = self.env.fast_status()
            self.last_status = status
            events = self._merge_events(events, status.get("events") or [])
            self._remember_chests(events)
            self._learn_from_chat(high, status.get("events") or [])
            self._print_decisions(status.get("decisions") or [])
            try:
                self.generator.on_outcomes(status.get("primitiveOutcomes") or [], status)
            except Exception as exc:
                print(f"\033[31mFast brain: action outcome handling error: {exc}\033[0m")
            for line in self.generator.drain():
                print(f"\033[36mFast brain: {line}\033[0m")
            self._handle_player_chat(status.get("chat") or [], high, subgoal, status)
            if status.get("highTarget") and high_target is None:
                high_target = {"item": status["highTarget"]["item"], "count": status["highTarget"]["need"]}
            # /pause on the Minecraft server: the loop idles, the brain holds its clocks
            if status.get("serverPaused"):
                if paused_since is None:
                    paused_since = time.time()
                    print(
                        "\033[33mFast brain: Minecraft server is paused. The bot is idle; "
                        "Ctrl+C is safe now. Run /pause again in Minecraft to continue.\033[0m"
                    )
                continue
            if paused_since is not None:
                started += time.time() - paused_since  # the pause does not count against the goal
                paused_since = None
                print("\033[33mFast brain: server resumed, continuing\033[0m")
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
                    instant = bool(detail.get("instant")) or not status.get("trace")  # trace resets per goal
                    if instant:
                        # already satisfied when posted: nothing was learned, do not offer it again
                        print(f"\033[33mFast brain: subgoal '{subgoal['text']}' was already satisfied, skipping\033[0m")
                        subgoal_log.append({"subgoal": subgoal["text"], "ok": None, "instant": True})
                        failed_subgoals.add(subgoal["text"])
                        confirmed = False
                    else:
                        confirmed = self._confirm(subgoal["text"], context, events, detail, run["inventory_before"])
                    if confirmed:
                        if self._replayable(subgoal.get("target")):
                            node = self._record_subgoal(high, subgoal, run, status, events)
                            if node["id"] not in used_nodes:
                                used_nodes.append(node["id"])
                            seconds = node["last_seconds"]
                        else:
                            seconds = round(time.time() - run["started"], 1)
                        self.subgoals_completed += 1
                        subgoal_log.append({"subgoal": subgoal["text"], "ok": True, "seconds": seconds})
                        # the world changed: steps that stalled before may work now
                        failed_subgoals.clear()
                        if high.get("source") == "chat" and self._chat_step_reached(high, subgoal):
                            success = True
                    elif not instant:
                        print("\033[33mFast brain: subgoal completion not confirmed\033[0m")
                    if not success:
                        subgoal = self._select_subgoal(high, context, status, failed_subgoals)
                        run = self._start_subgoal(high, context, subgoal, status)
                elif kind == "no_progress":
                    subgoal_failures += 1
                    failed_subgoals.add(subgoal["text"])
                    subgoal_log.append({"subgoal": subgoal["text"], "ok": False})
                    if subgoal.get("skill"):
                        self.skills.note_failure(subgoal["text"], subgoal["target"])
                    print(
                        f"\033[33mFast brain: subgoal '{subgoal['text']}' stalled "
                        f"({subgoal_failures}/{self.subgoal_failures_before_fail})\033[0m"
                    )
                    try:
                        self.generator.on_stall(high, subgoal, status, status.get("menuVerbs"))
                    except Exception as exc:
                        print(f"\033[31mFast brain: action generator error: {exc}\033[0m")
                    if subgoal_failures >= self.subgoal_failures_before_fail:
                        break
                    subgoal = self._select_subgoal(high, context, status, failed_subgoals)
                    run = self._start_subgoal(high, context, subgoal, status)
                elif kind == "subgoal_obsolete":
                    # Jev judged the subgoal no longer needed: pick another, no skill, no failure
                    print(f"\033[33mFast brain: subgoal '{subgoal['text']}' judged obsolete, re-selecting\033[0m")
                    subgoal_log.append({"subgoal": subgoal["text"], "ok": None, "obsolete": True})
                    failed_subgoals.add(subgoal["text"])  # do not offer the same text again this high goal
                    if high.get("source") == "chat":
                        if self._chat_step_reached(high, subgoal, force=True):
                            success = True
                            break
                        subgoal = self._select_subgoal(high, context, status, failed_subgoals)
                        run = self._start_subgoal(high, context, subgoal, status)
                        continue
                    next_subgoal = self._select_subgoal(high, context, status, failed_subgoals)
                    if next_subgoal["text"] == subgoal["text"]:
                        # Nothing else to try. For a high goal without a countable
                        # target ("Place a chest") an obsolete verdict usually means it
                        # is already satisfied: let the critic Nouls decide, otherwise
                        # count a failure so this cannot loop.
                        if status.get("highTarget") is None and self._confirm(
                            task, context, events, {}, high_inventory_before
                        ):
                            print(f"\033[32mFast brain: high goal '{task}' confirmed already satisfied\033[0m")
                            success = True
                            break
                        subgoal_failures += 1
                        print(
                            f"\033[33mFast brain: no alternative subgoal "
                            f"({subgoal_failures}/{self.subgoal_failures_before_fail})\033[0m"
                        )
                        if subgoal_failures >= self.subgoal_failures_before_fail:
                            break
                    subgoal = next_subgoal
                    run = self._start_subgoal(high, context, subgoal, status)
                elif kind == "smelt_queued":
                    # a batch is cooking: this subgoal is parked, work on something else meanwhile
                    print(
                        f"\033[36mFast brain: {detail.get('count')} {detail.get('input')} cooking, ready in "
                        f"~{detail.get('readyInSeconds')}s; parking '{subgoal['text']}' and picking another step\033[0m"
                    )
                    self._deferred[subgoal["text"]] = time.time() + float(detail.get("readyInSeconds") or 0)
                    subgoal_log.append({"subgoal": subgoal["text"], "ok": None, "cooking": detail.get("count")})
                    next_subgoal = self._select_subgoal(high, context, status, failed_subgoals)
                    if next_subgoal["text"] != subgoal["text"]:
                        subgoal = next_subgoal
                        run = self._start_subgoal(high, context, subgoal, status)
                elif kind in ("hazard", "stuck"):
                    print(f"\033[33mFast brain: {kind} {detail}\033[0m")
                elif kind in ("server_paused", "server_resumed"):
                    pass  # handled through status.serverPaused above
            if self._interrupt is not None and not success:
                interrupted = True
                break
            if success or subgoal_failures >= self.subgoal_failures_before_fail:
                break
            if time.time() - started > self.goal_timeout_seconds:
                print(f"\033[33mFast brain: high goal '{task}' timed out\033[0m")
                break

        # A high goal without a parseable target is judged like a subgoal
        if not success and not interrupted and not status.get("highTargets") and status.get("goalReached"):
            success = self._confirm(task, context, events, {}, self._inventory(self.last_events))

        elapsed = time.time() - started
        self.last_events = events
        v.recorder.record(events, task)
        print(
            f"\033[35mFast brain: high goal '{task}' "
            f"{'succeeded' if success else 'interrupted' if interrupted else 'failed'} "
            f"in {elapsed:.0f}s over {len(subgoal_log)} subgoals\033[0m"
        )
        if success:
            if milestone is not None and milestone.key == "home":
                self._remember_home(status)
            # the new node of the skill tree, connected to the nodes used to reach it
            if milestone is not None:
                high_target = {"milestone": milestone.key, "targets": list(milestone.targets)}
            node = self.skills.record(
                goal=task,
                target=high_target,
                kind="high",
                high_goal=None,
                fingerprint=high_fingerprint,
                seconds=elapsed,
                trace=[],
                collapsed=[],
                inventory_before=high_inventory_before,
                inventory_after=status.get("inventory") or self._inventory(events),
                children=used_nodes,
            )
            print(
                f"\033[32mFast brain: skill tree node '{node['goal']}' "
                f"<- {', '.join(used_nodes) or 'no subgoal nodes'}\033[0m"
            )
        v.jev.log_only(
            "fast_goals",
            {
                "task": task,
                "source": source,
                "success": success,
                "interrupted": interrupted,
                "seconds": round(elapsed, 1),
                "high_target": status.get("highTarget"),
                "subgoals": subgoal_log,
                "children": used_nodes,
                "stats": status.get("stats"),
            },
        )
        return {"task": task, "success": success, "interrupted": interrupted}

    # ------------------------------------------------------------------ #
    # player chat: Jev judges each line; commands queue apart from the ladder
    # ------------------------------------------------------------------ #
    def _say(self, text: str) -> None:
        try:
            self.env.fast_say(text)
        except Exception as exc:
            print(f"\033[31mFast brain: could not chat: {exc}\033[0m")

    def _handle_player_chat(self, lines, high, subgoal, status) -> None:
        for line in lines:
            text = str(line.get("text") or "").strip()
            if not text:
                continue
            verdict = self._judge_chat(line, high, subgoal, status)
            print(
                f"\033[36mFast brain: chat from {line.get('from')}: '{text}' -> {verdict['intent']}"
                f"{' (interrupt)' if verdict['interrupt'] else ''}\033[0m"
            )
            self.v.jev.log_only("chat_directive", {"from": line.get("from"), "text": text, **verdict})
            if verdict["intent"] == "stop":
                self.directives.clear()
                if high.get("source") == "chat":
                    self._interrupt = {"reason": "stop", "text": text}
                    self._say("Stopping; back to my own goals")
                else:
                    self._say("No player requests pending; I am on my own goals")
            elif verdict["intent"] == "new_task":
                directive = {"text": text, "from": line.get("from"), "at": time.time()}
                if verdict["interrupt"]:
                    self.directives.insert(0, directive)
                    self._interrupt = {"reason": "new_task", "text": text}
                else:
                    self.directives.append(directive)
                    self._say(f"Queued: {text} (after '{high['text']}')")

    def _judge_chat(self, line, high, subgoal, status) -> dict[str, Any]:
        """Jev's reading of one player line: intent and whether to act on it now."""
        jev = self.v.jev
        text = str(line.get("text") or "")
        bot_name = status.get("botName") or "bot"
        if not jev.enabled or Choice is None or Noul is None:
            # no Jev: an addressed or unaddressed line is a task; "stop" stops
            lowered = text.lower()
            stem = bot_name.rstrip("0123456789")
            m = re.match(rf"^({stem}\d*)\b[:,]?\s*", lowered)
            if m and m.group(1) != bot_name.lower():
                return {"intent": "none", "interrupt": False, "confidence": None}
            intent = "stop" if re.search(r"\b(stop|cancel|never ?mind)\b", lowered) else "new_task"
            return {"intent": intent, "interrupt": intent == "stop", "confidence": None}
        state = {
            "bot_name": bot_name,
            "sibling_bots": "other bots on this server are named like the bot (bot, bot2, bot3, ...)",
            "message": {"from": line.get("from"), "text": text},
            "current_high_goal": high["text"],
            "current_high_goal_source": high.get("source", "ladder"),
            "current_subgoal": subgoal["text"] if subgoal else None,
            "queued_player_requests": [d["text"] for d in self.directives],
            "long_term_goal": self.long_term_goal,
            "inventory": status.get("inventory") or {},
        }
        response = jev.ask(
            "chat_directive",
            state,
            {
                "intent": Choice(
                    instructions=(
                        "A player typed `message.text` in Minecraft chat. The bot reading it is "
                        "`bot_name`; `sibling_bots` explains the other bots' names. A message that "
                        "names another bot and not this one is not for this bot. The bot is working "
                        "on `current_high_goal` (from the ladder of milestones, or an earlier player "
                        "request when `current_high_goal_source` is chat). What does the message ask "
                        "of this bot?"
                    ),
                    criteria={
                        "new_task": "A task for this bot to do: gather, craft, build, go somewhere, fight, bring something.",
                        "stop": "Stop or cancel what the bot is doing for players and go back to its own goals.",
                        "none": "Not a command for this bot: a question, remark, greeting, or a message meant only for another bot.",
                    },
                ),
                "interrupt": Noul(
                    instructions=(
                        "If `message.text` is a task for this bot, should it drop `current_high_goal` "
                        "now to act on it, rather than finish the current goal first? The ladder goal "
                        "is the bot's own routine work and can be resumed later at little cost, while "
                        "a player is waiting on the answer, so lean toward yes for a clear task; no when "
                        "the player says it can wait, or it is a vague group remark rather than a request "
                        "to this bot. When `current_high_goal_source` is chat the bot is already serving a "
                        "player request: then interrupt only for an urgent new one (now, immediately, danger)."
                    )
                ),
            },
            extra_record={"text": text},
        )
        if response is None:
            return {"intent": "none", "interrupt": False, "confidence": None}
        try:
            intent = response.choices["intent"].choice
            conf = response.choices["intent"].confidence
            interrupt = response.nouls["interrupt"].noul
        except Exception:
            return {"intent": "none", "interrupt": False, "confidence": None}
        if intent not in ("new_task", "stop", "none"):
            intent = "none"
        return {
            "intent": intent,
            "interrupt": intent == "new_task" and interrupt >= self.chat_interrupt_threshold,
            "confidence": conf,
            "interrupt_noul": interrupt,
        }

    # ------------------------------------------------------------------ #
    # subgoal selection: derived steps toward the high goal, skills as annotations
    # ------------------------------------------------------------------ #
    def _select_subgoal(self, high, context, status, failed: set[str]) -> dict[str, Any]:
        if high.get("source") == "chat":
            return self._select_chat_step(high, context, status, failed)
        fingerprint = status.get("fingerprint")
        extra = self._learned.get(high["id"]) or None
        derived = self.env.fast_subgoals(
            high["text"], targets=high.get("targets"), extra_targets=extra, lookahead=high.get("lookahead")
        )
        if self._undecomposed(derived, high["text"]):
            # The planner found no item to work from (e.g. "Build a nether portal").
            # Ask the LLM once per high goal for the items the goal needs, plan
            # from those, and let the tree prune them if they do not pan out.
            targets = self._llm_decompose(high, context)
            if targets:
                planned = self.env.fast_subgoals(high["text"], targets=targets, extra_targets=extra, lookahead=high.get("lookahead"))
                if planned.get("candidates") and not self._undecomposed(planned, high["text"]):
                    derived = planned
        self._print_plan(derived.get("plan") or {})
        candidates: list[dict[str, Any]] = []
        now = time.time()
        for c in derived.get("candidates") or []:
            if c["text"] in failed:
                continue
            if self._deferred.get(c["text"], 0) > now and len(derived.get("candidates") or []) > 1:
                continue  # parked while its batch cooks; anything else goes first
            node = self.skills.node(c["text"], c.get("target"))
            if node and node["failures"] > node["successes"] + 1:
                node = None  # a skill that keeps failing is no recommendation
            candidates.append({"text": c["text"], "target": c.get("target"), "why": c.get("why", ""), "skill": node})
        if not candidates:
            return self._fallback_subgoal(high, derived, failed)
        if len(candidates) == 1:
            return candidates[0]
        pick = self._jev_pick(high, context, status, candidates)
        return pick or candidates[0]

    def _fallback_subgoal(self, high, derived, failed: set[str]) -> dict[str, Any]:
        """No candidate survived the failed set. Never post the high goal's bare name
        for a goal that has targets: Jev reads "Home base" as "be at home" and walks
        in circles. Offer a failed candidate again, then the first unmet target itself,
        and otherwise declare the goal exhausted so it fails now, not at the timeout."""
        cands = derived.get("candidates") or []
        retry = [c for c in cands if c["text"] != high["text"]]
        if retry:
            c = retry[0]
            print(f"\033[33mFast brain: every step was tried; offering '{c['text']}' again\033[0m")
            return {"text": c["text"], "target": c.get("target"), "why": "tried before; nothing else left", "skill": None}
        targets = high.get("targets") or []
        if not targets:
            return {"text": high["text"], "target": None, "why": "nothing derived", "skill": None}
        try:
            sat = self.env.fast_subgoals(high["text"], check=targets).get("satisfied") or []
        except Exception:
            sat = []
        for i, t in enumerate(targets):
            if i < len(sat) and sat[i]:
                continue
            if t.get("nearBlock"):
                text = f"Place the {str(t['nearBlock']).replace('_', ' ')}"
            elif t.get("item"):
                text = f"Obtain {t.get('count', 1)} {t['item']}"
            else:
                continue
            if text in failed:
                continue
            print(f"\033[33mFast brain: no plan for '{high['text']}'; posting its unmet target '{text}'\033[0m")
            return {"text": text, "target": t, "why": "the goal's own unmet target", "skill": None}
        return {"text": high["text"], "target": None, "why": "exhausted", "skill": None, "exhausted": True}

    # ------------------------------------------------------------------ #
    # player directives: one LLM decomposition into verifiable steps, run in order
    # ------------------------------------------------------------------ #
    @staticmethod
    def _replayable(target) -> bool:
        """Player-relative steps depend on where a player stands: no skill node."""
        return not (target and (target.get("nearPlayer") or target.get("give")))

    def _chat_decompose(self, high, context, status) -> list[dict[str, Any]]:
        """Ordered steps for a player's request, each with a target code can check.

        Target shapes: {item, count}, {nearBlock}, {nearPlayer, distance},
        {give: {item, count, to}}, or {judged: true} (a Jev Noul decides).
        Anything the model gets wrong is dropped; with nothing left the raw
        request is one judged step, which is what happened before.
        """
        requester = high.get("requester") or "the player"
        players = [p for p in (status.get("players") or []) if p]
        fallback = [{"text": high["text"], "target": {"judged": True}}]
        llm = getattr(self.v.curriculum_agent, "qa_llm", None)
        if llm is None:
            return fallback
        from langchain.schema import HumanMessage, SystemMessage

        prompt = (
            "A Minecraft (Java 1.19) bot received a request from a player in chat. Break it into the "
            "smallest ordered list of steps the bot can do and that code can verify.\n"
            f"Request: {high['text']}\n"
            f"From player: {requester}\n"
            f"Players online: {', '.join(players) or requester}\n"
            f"Bot inventory: {status.get('inventory') or {}}\n"
            "Answer with a JSON array only, at most 6 objects, each {\"text\": <short imperative>, "
            "\"target\": <one of>}:\n"
            '  {"item": "<minecraft item id>", "count": <n>}   the bot must hold n of the item\n'
            '  {"nearBlock": "<block id>"}                        the block must be within ~16 blocks\n'
            '  {"nearPlayer": "<player name>", "distance": <n>}   the bot stands within n blocks of that player\n'
            '  {"give": {"item": "<item id>", "count": <n>, "to": "<player name>"}}   drop n of the item at that player\n'
            "  null                                                 nothing checkable; the bot judges it done\n"
            'Use the player name exactly as given. "come here" is one nearPlayer step. "bring me X" is an item '
            "step, then a nearPlayer step, then a give step. Use item ids like oak_log, cooked_beef, iron_ingot."
        )
        try:
            with timing.timed("brain.chat_decompose"):
                reply = llm([SystemMessage(content="You answer with a JSON array only."), HumanMessage(content=prompt)]).content
        except Exception as exc:
            print(f"\033[31mFast brain: chat decomposition failed: {exc}\033[0m")
            return fallback
        steps = self._parse_steps(str(reply), requester, players)
        print(f"\033[36mFast brain: steps for '{high['text']}': {[s['text'] for s in steps] or 'none (judged as a whole)'}\033[0m")
        self.v.jev.log_only("chat_decompose", {"task": high["text"], "reply": str(reply), "steps": steps})
        return steps or fallback

    @staticmethod
    def _parse_steps(reply: str, requester: str, players: list[str]) -> list[dict[str, Any]]:
        import json

        start, end = reply.find("["), reply.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            raw = json.loads(reply[start : end + 1])
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        known = {p.lower(): p for p in players}
        known.setdefault(requester.lower(), requester)

        def num(value, default):
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                return default

        def player(name):
            return known.get(str(name or "").lower()) or requester

        steps = []
        for s in raw[:6]:
            if not isinstance(s, dict) or not str(s.get("text") or "").strip():
                continue
            t = s.get("target")
            target: dict[str, Any] | None
            if not t:
                target = {"judged": True}
            elif isinstance(t, dict) and t.get("item"):
                target = {"item": str(t["item"]).lower().strip(), "count": num(t.get("count"), 1)}
            elif isinstance(t, dict) and t.get("nearBlock"):
                target = {"nearBlock": str(t["nearBlock"]).lower().strip()}
            elif isinstance(t, dict) and t.get("nearPlayer"):
                target = {"nearPlayer": player(t["nearPlayer"]), "distance": num(t.get("distance"), 3)}
            elif isinstance(t, dict) and isinstance(t.get("give"), dict) and t["give"].get("item"):
                g = t["give"]
                target = {"give": {"item": str(g["item"]).lower().strip(), "count": num(g.get("count"), 1), "to": player(g.get("to"))}}
            else:
                continue
            steps.append({"text": str(s["text"]).strip(), "target": target})
        return steps

    def _chat_step_reached(self, high, subgoal, force: bool = False) -> bool:
        """Mark the current step done when the reached subgoal was the step itself
        (or its item target now holds). Returns True when every step is done."""
        steps = high.get("steps") or []
        i = high.get("step_index")
        if i is None or i >= len(steps):
            return False
        step = steps[i]
        t = step["target"]
        done = high.setdefault("done", [])  # a list: the goal is posted to Node as JSON
        if t.get("item") and not force:
            try:
                sat = self.env.fast_subgoals(high["text"], check=[t]).get("satisfied") or [False]
            except Exception:
                sat = [False]
            if sat[0] and i not in done:
                done.append(i)
        elif (force or subgoal["text"] == step["text"]) and i not in done:
            done.append(i)
        return len(done) == len(steps)

    def _select_chat_step(self, high, context, status, failed: set[str]) -> dict[str, Any]:
        if high.get("steps") is None:
            high["steps"] = self._chat_decompose(high, context, status)
            high["done"] = []
        steps = high["steps"]
        done = high["done"]
        # code-checkable steps may already hold (an item the bot carries, a block in view)
        checkable = [(i, s) for i, s in enumerate(steps) if i not in done and not s["target"].get("judged") and not s["target"].get("give")]
        if checkable:
            try:
                sat = self.env.fast_subgoals(high["text"], check=[s["target"] for _, s in checkable]).get("satisfied") or []
                for (i, _), ok in zip(checkable, sat):
                    if ok and i not in done:
                        done.append(i)
            except Exception as exc:
                print(f"\033[31mFast brain: could not check steps: {exc}\033[0m")
        for i, step in enumerate(steps):
            if i in done:
                continue
            high["step_index"] = i
            why = f"step {i + 1} of {len(steps)} for {high.get('requester') or 'the player'}"
            t = step["target"]
            if t.get("item"):
                # plan the item through recipes, like a ladder target
                derived = self.env.fast_subgoals(step["text"], targets=[t], extra_targets=self._learned.get(high["id"]) or None)
                self._print_plan(derived.get("plan") or {})
                cands = [c for c in derived.get("candidates") or [] if c["text"] not in failed]
                if cands:
                    picks = [
                        {"text": c["text"], "target": c.get("target"), "why": c.get("why", "") or why, "skill": self.skills.node(c["text"], c.get("target"))}
                        for c in cands
                    ]
                    if len(picks) == 1:
                        return picks[0]
                    return self._jev_pick(high, context, status, picks) or picks[0]
            # the step itself: posted as-is, re-offered after a stall (later steps need it)
            return {"text": step["text"], "target": None if t.get("judged") else t, "why": why, "skill": None}
        return {"text": high["text"], "target": None, "why": "all steps done", "skill": None}

    # Chat lines the primitives emit when the world refuses an action. They name
    # a requirement the plan did not know about, so it becomes a planned target.
    _NEED_TOOL_RE = re.compile(r"I need at least an? (\w+) to mine (\w+)")
    _NEED_ITEMS_RE = re.compile(r"I cannot make (\w+) because I need: (.+)")
    _NEED_PART_RE = re.compile(r"(\d+)\s+more\s+(\w+)")

    def _learn_from_chat(self, high, new_events) -> None:
        learned = self._learned.setdefault(high["id"], [])
        known = {t["item"] for t in learned}
        for event_type, event in new_events or []:
            if event_type != "onChat":
                continue
            line = str((event or {}).get("onChat") or "")
            found: list[tuple[str, int]] = []
            m = self._NEED_TOOL_RE.search(line)
            if m:
                found.append((m.group(1), 1))
            m = self._NEED_ITEMS_RE.search(line)
            if m:
                found.extend((item, int(n)) for n, item in self._NEED_PART_RE.findall(m.group(2)))
            for item, count in found:
                if item in known:
                    continue
                known.add(item)
                learned.append({"item": item, "count": count, "from": line[:120]})
                print(f"\033[36mFast brain: learned requirement from the world: {count} {item} ({line[:80]})\033[0m")
                self.v.jev.log_only("learned_requirements", {"task": high["text"], "item": item, "count": count, "chat": line})

    @staticmethod
    def _undecomposed(derived: dict, text: str) -> bool:
        cands = derived.get("candidates") or []
        return not derived.get("targets") or (len(cands) == 1 and cands[0]["text"] == text)

    def _llm_decompose(self, high, context) -> list[dict[str, Any]]:
        """Items the high goal needs, proposed by the curriculum's QA model.

        The answer is a list of {item, count}; Node keeps only real item ids and
        plans from them, so a wrong guess costs one rejected node, not a run.
        """
        cache = self.__dict__.setdefault("_llm_plans", {})
        if high["text"] in cache:
            return cache[high["text"]]
        llm = getattr(self.v.curriculum_agent, "qa_llm", None)
        if llm is None:
            cache[high["text"]] = []
            return []
        from langchain.schema import HumanMessage, SystemMessage

        prompt = (
            "You decompose Minecraft (Java 1.19) goals into the items a player must hold.\n"
            f"Goal: {high['text']}\n"
            f"Context: {context or 'none'}\n"
            "Answer with one line per item in the form `item_id count`, using minecraft item ids "
            "such as obsidian, flint_and_steel, iron_ingot. List only items the player must end up "
            "holding (or placing) to complete the goal, at most 6 lines, no prose."
        )
        try:
            with timing.timed("brain.llm_decompose"):
                reply = llm([SystemMessage(content="You answer with item lines only."), HumanMessage(content=prompt)]).content
        except Exception as exc:
            print(f"\033[31mFast brain: LLM decomposition failed: {exc}\033[0m")
            cache[high["text"]] = []
            return []
        targets = []
        for line in str(reply).splitlines():
            parts = line.strip().strip("-*`").split()
            if not parts:
                continue
            item = parts[0].lower().strip(":,")
            count = 1
            for p in parts[1:]:
                if p.isdigit():
                    count = int(p)
                    break
            if item:
                targets.append({"item": item, "count": count})
        print(f"\033[36mFast brain: LLM decomposition of '{high['text']}': {targets}\033[0m")
        self.v.jev.log_only("llm_decompose", {"task": high["text"], "reply": str(reply), "targets": targets})
        cache[high["text"]] = targets
        return targets

    @staticmethod
    def _print_plan(plan: dict, limit: int = 14) -> None:
        """Compact view of the requirement graph: what is still missing and why."""
        open_nodes = [n for n in plan.values() if n.get("status") != "satisfied" and n.get("id") != "goal"]
        if not open_nodes:
            return
        open_nodes.sort(key=lambda n: (n.get("depth") or 0, n.get("id")))
        lines = []
        for n in open_nodes[:limit]:
            indent = "  " * (n.get("depth") or 0)
            what = n.get("item") or n.get("id")
            qty = f"{n.get('have', 0)}/{n.get('need', 1)}" if n.get("kind") not in ("station", "fuel", "tool", "explore") else ""
            later = " (later)" if n.get("priority") == "later" else ""
            lines.append(f"{indent}{n.get('status', '?'):9} {n.get('kind', '?'):8} {what} {qty}{later}".rstrip())
        more = f"  ... {len(open_nodes) - limit} more" if len(open_nodes) > limit else ""
        print("\033[90mFast brain plan:\n" + "\n".join(lines) + more + "\033[0m")

    def _jev_pick(self, high, context, status, candidates):
        jev = self.v.jev
        if not jev.enabled or Choice is None:
            return None
        fingerprint = status.get("fingerprint")
        criteria = {}
        for i, c in enumerate(candidates):
            label = f"c{i}"
            note = c["why"] or ""
            if c["skill"]:
                note = f"{note}; {self.skills.describe(c['skill'], fingerprint)}" if note else self.skills.describe(c["skill"], fingerprint)
            criteria[label] = {"subgoal": c["text"], "note": note} if note else {"subgoal": c["text"]}
        state = {
            "high_goal": high["text"],
            "long_term_goal": self.long_term_goal,
            "context": context,
            "high_goal_progress": status.get("highTargets") or status.get("highTarget"),
            "upcoming_milestone_targets": [target_label(t) for t in (high.get("lookahead") or [])],
            "inventory": status.get("inventory") or {},
            "fingerprint": fingerprint,
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
                        "`high_goal_progress` is the exact count still needed, when known); "
                        "`long_term_goal` is where the whole run is heading and "
                        "`upcoming_milestone_targets` is what comes right after this goal; prefer steps "
                        "that serve both (mine extra iron while already at the ore). "
                        "`candidates` are the next steps derived for this high goal; each is one "
                        "bounded step the bot can start right now. A 'known skill' note means the "
                        "step was reached before and says how that went. Given `inventory` and "
                        "`recent_actions`, which candidate is the best next step toward the high goal?"
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
        if subgoal.get("exhausted"):
            return {"goal": {"id": None, "text": subgoal["text"]}, "started": time.time(), "fingerprint": None, "inventory_before": {}}
        goal = {
            "id": f"{high['id']}-sub-{int(time.time() * 1000) % 100000000}",
            "text": subgoal["text"],
            "context": context,
            "kind": "task",
            "highGoal": high,
        }
        if subgoal.get("target"):
            goal["target"] = subgoal["target"]
        route = self.skills.route_for(subgoal.get("target"), status.get("fingerprint"), subgoal["text"])
        if route:
            goal["replay"] = route
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

    def _record_subgoal(self, high, subgoal, run, status, events) -> dict[str, Any]:
        node = self.skills.record(
            goal=subgoal["text"],
            target=subgoal.get("target") or (status.get("target") and {"item": status["target"]["item"]}),
            kind="sub",
            high_goal=high["text"],
            fingerprint=run["fingerprint"],
            seconds=time.time() - run["started"],
            trace=status.get("trace") or [],
            collapsed=status.get("collapsedTrace") or [],
            inventory_before=run["inventory_before"],
            inventory_after=status.get("inventory") or self._inventory(events),
        )
        print(
            f"\033[32mFast brain: skill '{subgoal['text']}' in {node['last_seconds']}s "
            f"with {', '.join(node['tools_used']) or 'no tools'} ({node['successes']} success(es))\033[0m"
        )
        return node

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

    def _print_decisions(self, decisions) -> None:
        """One console line per Jev call made by the fast loop, plus a rate summary every 20."""
        for d in decisions:
            since = f"+{d['sinceLast']:.1f}s" if d.get("sinceLast") is not None else "first"
            flags = []
            if d.get("count"):
                flags.append(f"x{d['count']}")
            if d.get("danger", 0) >= 0.7:
                flags.append("DANGER")
            if d.get("stuck", 0) >= 0.7:
                flags.append("STUCK")
            if d.get("valid", 1) <= 0.2:
                flags.append("OBSOLETE")
            if d.get("goalReached") is not None and d["goalReached"] >= 0.8:
                flags.append("DONE?")
            if not d.get("inMenu", True):
                flags.append("NOT-IN-MENU")
            print(
                f"[90m[jev #{d['n']} {since} {d['ms']}ms] {d['action']} ({d['confidence']:.2f}) "
                f"danger {d['danger']:.2f} stuck {d['stuck']:.2f} valid {d['valid']:.2f} | {d['options']} options"
                f"{' | ' + ' '.join(flags) if flags else ''}[0m"
            )
            self._decision_stats.append((d.get("sinceLast"), d["ms"]))
            if len(self._decision_stats) % 20 == 0:
                gaps = [g for g, _ in self._decision_stats[-20:] if g is not None]
                lat = [m for _, m in self._decision_stats[-20:]]
                print(
                    f"[90m[jev rate] last 20 calls: one every {sum(gaps) / len(gaps):.1f}s on average, "
                    f"{sum(lat) / len(lat):.0f}ms latency, {len(self._decision_stats)} calls this run[0m"
                    if gaps else ""
                )

    def _remember_chests(self, events) -> None:
        """Keep the action agent's chest memory (and its checkpoint file) current."""
        for event_type, event in reversed(events or []):
            if event_type == "observe":
                chests = event.get("nearbyChests")
                if chests is not None and chests != self._last_chests:
                    self._last_chests = chests
                    try:
                        self.v.action_agent.update_chest_memory(chests)
                    except Exception as exc:
                        print(f"\033[31mFast brain: chest memory update failed: {exc}\033[0m")
                return

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
