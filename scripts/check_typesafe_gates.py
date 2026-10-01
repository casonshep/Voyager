"""Offline check of the gate logic with a fake Jev client (no API key, no Minecraft).

Usage:
    venv/Scripts/python.exe scripts/check_typesafe_gates.py

Verifies that:
  * critic_shadow / qa_rank / task_fanout parse a response into their result objects
  * CurriculumAgent._select_qa_questions keeps the 3 fixed questions plus top-k by score
    and reuses gray-band cache entries only when Jev says they match
  * CurriculumAgent.propose_next_ai_task re-asks GPT on a veto and stops after the cap
"""

from __future__ import annotations

import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from voyager.typesafe import gates  # noqa: E402
from voyager.typesafe.client import JevClient  # noqa: E402


# --------------------------------------------------------------------------- #
# Fake SDK response                                                           #
# --------------------------------------------------------------------------- #

class _Ans(types.SimpleNamespace):
    pass


class FakeResponse:
    def __init__(self, nouls=None, choices=None, scores=None):
        self.nouls = {k: _Ans(noul=v) for k, v in (nouls or {}).items()}
        self.choices = {
            k: _Ans(choice=v[0], confidence=v[1], probabilities=v[2]) for k, v in (choices or {}).items()
        }
        self.scores = {
            k: _Ans(score=v[0], confidence=v[1], probabilities=v[2]) for k, v in (scores or {}).items()
        }
        self.model = "fake"
        self.request_id = "req_fake"
        self.usage = _Ans(input_tokens=1, output_tokens=1)


class FakeJev(JevClient):
    """A JevClient that is 'enabled' but answers from a scripted function."""

    def __init__(self, scripted, ckpt_dir):
        super().__init__(enabled=False, ckpt_dir=ckpt_dir)
        self._scripted = scripted
        self._client = object()  # makes .enabled True
        os.makedirs(self.log_dir, exist_ok=True)
        self.calls = []

    def ask(self, gate, state, questions, extra_record=None):
        self.calls.append((gate, dict(state), list(questions)))
        response = self._scripted(gate, state, questions)
        self._append(gate, {"gate": gate, "answers": gates.answers_to_dict(response)})
        return response


def scripted(gate, state, questions):
    if gate == "critic_shadow":
        nouls = {
            "success": 0.93,
            "target_item_missing": 0.02,
            "quantity_short": 0.05,
            "wrong_variant": 0.01,
            "already_had": 0.1,
        }
        if "success_by_delta" in questions:
            nouls["success_by_delta"] = 0.9
        return FakeResponse(nouls=nouls, choices={"verification_channel": ("inventory", 0.95, {"inventory": 0.96})})
    if gate == "qa_rank":
        n = sum(1 for q in questions if q.startswith("q"))
        # descending usefulness by index so the ordering is easy to assert
        scores = {f"q{i}": (2.0 - i * 0.2, 0.8, [0, 0, 1]) for i in range(n)}
        nouls = {k: (0.95 if k == "same4" else 0.2) for k in questions if k.startswith("same")}
        return FakeResponse(nouls=nouls, scores=scores)
    if gate == "task_fanout":
        task = state["task"].lower()
        veto = 0.95 if task.startswith("place") else 0.05
        return FakeResponse(
            nouls={
                "requires_placing_building_planting_trading": veto,
                "pointless_repeat": 0.05,
                "in_failed_tasks_unchanged": 0.05,
                "requires_missing_tool_tier": 0.1,
            },
            scores={"feasibility": (1.4, 0.6, [0.1, 0.5, 0.3, 0.1, 0.0])},
            choices={"verb_class": ("mine", 0.9, {"mine": 0.9})},
        )
    raise AssertionError(gate)


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="typesafe_check_")
    jev = FakeJev(scripted, tmp)
    assert jev.enabled

    state = {"task": "Mine 3 wood logs", "inventory": {"oak_log": 4}, "inventory_delta": {"oak_log": 3}}

    # ---- gates parse responses -------------------------------------------
    cs = gates.critic_shadow(jev, state, gpt_success=True, threshold=0.8)
    assert cs is not None and cs.success == 0.93 and cs.success_by_delta == 0.9
    assert cs.verification_channel == "inventory"
    assert os.path.exists(os.path.join(tmp, "typesafe", "critic_shadow.jsonl"))
    assert os.path.exists(os.path.join(tmp, "typesafe", "critic_shadow_decisions.jsonl"))

    questions = [f"Q{i}?" for i in range(9)]
    qr = gates.qa_rank(jev, state, questions, cached_neighbours={4: ("cached Q4", 0.1), 5: ("cached Q5", 0.2)})
    assert qr is not None and len(qr.scores) == 9
    assert qr.same_as_cached == {4: 0.95, 5: 0.2}

    tf = gates.task_fanout(jev, {**state, "task": "Place 4 torches"})
    assert tf is not None and tf.fired(0.7) == ["requires_placing_building_planting_trading"]
    assert gates.task_fanout(jev, {**state, "task": "Mine 1 iron_ore"}).fired(0.7) == []
    print("gates: OK")

    # ---- curriculum wiring, without constructing the real agent ----------
    from voyager.agents.curriculum import CurriculumAgent
    from langchain.schema import HumanMessage, SystemMessage

    agent = CurriculumAgent.__new__(CurriculumAgent)
    agent.jev = jev
    agent.veto_threshold = 0.7
    agent.qa_keep = 3
    agent.max_vetoes_per_proposal = 2
    agent.completed_tasks = ["Mine 1 wood log"]
    agent.failed_tasks = []
    agent.qa_cache = {"cached Q4": "Answer: four", "cached Q5": "Answer: five"}

    # fake vector db: Q4 lands in the gray band, Q5 too, Q8 is an exact-ish hit
    distances = {"Q4?": ("cached Q4", 0.1), "Q5?": ("cached Q5", 0.2), "Q8?": ("cached Q4", 0.01)}

    def nearest(q):
        return distances.get(q, (None, None))

    agent._nearest_cached_question = nearest

    events = [("observe", {"status": {"biome": "plains", "entities": {}, "position": {"x": 0, "y": 0, "z": 0},
                                      "equipment": [None] * 6, "inventoryUsed": 1, "health": 20, "food": 20,
                                      "timeOfDay": "day"},
                           "voxels": ["grass_block"], "blockRecords": [], "inventory": {"oak_log": 4},
                           "nearbyChests": {}})]
    keep, reuse = agent._select_qa_questions(questions, events=events, chest_memory={})
    # fixed 0,1,2 always; generated ranked by score desc => 3,4,5 for qa_keep=3
    assert keep == [0, 1, 2, 3, 4, 5], keep
    # Q8 exact hit is recorded but Q8 was dropped; Q4 gray-band accepted (0.95), Q5 rejected (0.2)
    assert reuse == {4: "cached Q4", 8: "cached Q4"}, reuse
    print("qa selection: OK")

    # veto loop: GPT proposes a placing task twice, then a mining task
    proposals = iter(["Reasoning: x\nTask: Place 4 torches.", "Reasoning: y\nTask: Place a torch.", "Reasoning: z\nTask: Mine 1 iron_ore."])
    seen_messages = []

    def fake_llm(messages):
        seen_messages.append(len(messages))
        return types.SimpleNamespace(content=next(proposals))

    agent.llm = fake_llm
    agent.get_task_context = lambda task: f"ctx for {task}"
    messages = [SystemMessage(content="sys"), HumanMessage(content="obs")]
    task, context = agent.propose_next_ai_task(messages=messages, state=dict(state), max_retries=5)
    assert task == "Mine 1 iron_ore", task
    assert seen_messages == [2, 3, 4], seen_messages  # one feedback message appended per veto
    assert len(messages) == 2  # original list not mutated

    # cap: after two vetoes the third proposal is accepted even if it would be vetoed
    proposals = iter(["Reasoning: a\nTask: Place 1 torch.", "Reasoning: b\nTask: Place 2 torches.", "Reasoning: c\nTask: Place 3 torches."])
    seen_messages.clear()
    task, _ = agent.propose_next_ai_task(messages=messages, state=dict(state), max_retries=5)
    assert task == "Place 3 torches" and seen_messages == [2, 3, 4], (task, seen_messages)

    # disabled / no state: no judgment, no re-ask
    proposals = iter(["Reasoning: d\nTask: Place 9 torches."])
    task, _ = agent.propose_next_ai_task(messages=messages, state=None, max_retries=5)
    assert task == "Place 9 torches"
    print("veto loop: OK")

    # a transient failure fetching task context is retried, not propagated
    proposals = iter(["Reasoning: e\nTask: Mine 1 coal_ore.", "Reasoning: f\nTask: Mine 2 coal_ore."])
    attempts = {"n": 0}

    def flaky_context(task):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("simulated QA network error")
        return f"ctx for {task}"

    agent.get_task_context = flaky_context
    task, context = agent.propose_next_ai_task(messages=messages, state=dict(state), max_retries=5)
    assert task == "Mine 2 coal_ore" and context == "ctx for Mine 2 coal_ore", (task, context)
    assert attempts["n"] == 2
    print("context retry: OK")
    print(f"logs written under {tmp}/typesafe")


if __name__ == "__main__":
    main()
