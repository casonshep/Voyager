"""CurriculumAgent: propose the next high goal (one GPT call).

This is the only place GPT chooses *what* to do. It reads the live snapshot
(PLAN.md 1.3), the goals completed and failed so far, and returns one short,
beginner-level, usually item-driven goal such as "Obtain a stone pickaxe".
The decomposer turns that goal into a verifiable subtask tree.

The LLM is injected: any object with ``complete(messages) -> str`` where
``messages`` is a list of ``{"role", "content"}`` dicts (``voyager.tree.llm``
provides one). Nothing here depends on langchain.
"""

from __future__ import annotations

import json
import re
from typing import Any

import voyager.utils as U
from voyager.prompts import load_prompt
from voyager.utils import timing


class CurriculumAgent:
    def __init__(self, llm=None, *, ckpt_dir: str = "ckpt", resume: bool = False, long_term_goal: str | None = None):
        self.llm = llm
        self.ckpt_dir = ckpt_dir
        self.long_term_goal = long_term_goal
        U.f_mkdir(f"{ckpt_dir}/curriculum")
        if resume:
            self.completed_tasks: list[str] = U.json_load_or([], f"{ckpt_dir}/curriculum/completed_tasks.json")
            self.failed_tasks: list[str] = U.json_load_or([], f"{ckpt_dir}/curriculum/failed_tasks.json")
        else:
            self.completed_tasks = []
            self.failed_tasks = []

    # ------------------------------------------------------------------ #

    @property
    def progress(self) -> int:
        return len(self.completed_tasks)

    def render_observation(self, snap: dict[str, Any] | None, hints: list[str] | None = None) -> str:
        snap = snap or {}
        bot = snap.get("bot") or {}
        inv = snap.get("inventory") or {}
        blocks = snap.get("blocks") or {}
        ents = snap.get("entities") or []
        lines = [
            f"Biome: {bot.get('biome', 'unknown')}",
            f"Time: {bot.get('daylight', 'unknown')}",
            f"Nearby blocks: {', '.join(sorted(blocks)) or 'none'}",
            f"Nearby entities (nearest to farthest): {', '.join(e.get('name', '?') for e in ents) or 'none'}",
            f"Health: {bot.get('health', '?')}/20",
            f"Hunger: {bot.get('food', '?')}/20",
            f"Position: {json.dumps(bot.get('position')) if bot.get('position') else 'unknown'}",
            f"Equipment: {json.dumps(snap.get('slots') or {})}",
            f"Inventory ({len(inv)} kinds): {json.dumps(inv) if inv else 'Empty'}",
            f"Completed goals so far: {', '.join(self.completed_tasks) or 'None'}",
            f"Failed goals that are too hard: {', '.join(self.failed_tasks) or 'None'}",
        ]
        if self.long_term_goal:
            lines.append(f"Long-term direction: {self.long_term_goal}")
        if hints:
            # "test next" items from the post-goal review lesson files: open questions the
            # bot's own play raised. Suggestions, not orders.
            lines.append("Open questions from earlier play (a goal that tests one is useful): " + "; ".join(h[:160] for h in hints[:5]))
        return "\n".join(lines)

    def propose_goal(self, snap: dict[str, Any] | None, *, rejected: list[str] | None = None, max_retries: int = 3,
                     hints: list[str] | None = None) -> str:
        """One GPT call; returns the goal text. The first goal of a fresh run is fixed."""
        if self.progress == 0 and not self.failed_tasks and not rejected:
            return "Obtain 1 wood log"
        if self.llm is None:
            raise RuntimeError("CurriculumAgent has no LLM handle")
        messages = [
            {"role": "system", "content": load_prompt("curriculum")},
            {"role": "user", "content": self.render_observation(snap, hints)},
        ]
        if rejected:
            messages.append({"role": "user", "content": "Do not propose any of these again: " + "; ".join(rejected)})
        last_error = None
        for _ in range(max_retries):
            with timing.timed("llm.curriculum"):
                reply = self.llm.complete(messages)
            if hasattr(reply, "content"):
                reply = reply.content
            try:
                return self.parse(str(reply))
            except ValueError as exc:
                last_error = exc
                messages.append({"role": "user", "content": f"{exc}. Answer again in the required format."})
        raise RuntimeError(f"curriculum: no task in {max_retries} replies: {last_error}")

    @staticmethod
    def parse(reply: str) -> str:
        for line in reply.splitlines():
            if line.strip().lower().startswith("task:"):
                task = line.split(":", 1)[1].strip().rstrip(".")
                task = re.sub(r"\s+", " ", task)
                if task:
                    return task
        raise ValueError("Task not found in Curriculum Agent response")

    # ------------------------------------------------------------------ #

    def record(self, goal: str, success: bool) -> None:
        if success:
            print(f"\033[35mCompleted goal {goal}.\033[0m")
            self.completed_tasks.append(goal)
        else:
            print(f"\033[35mFailed goal {goal}.\033[0m")
            self.failed_tasks.append(goal)
        self.clean_up_tasks()

    def clean_up_tasks(self) -> None:
        completed: list[str] = []
        for task in self.completed_tasks:
            if task not in completed:
                completed.append(task)
        failed = [t for t in self.failed_tasks if t not in completed]
        self.completed_tasks = completed
        self.failed_tasks = failed
        U.dump_json(self.completed_tasks, f"{self.ckpt_dir}/curriculum/completed_tasks.json")
        U.dump_json(self.failed_tasks, f"{self.ckpt_dir}/curriculum/failed_tasks.json")
