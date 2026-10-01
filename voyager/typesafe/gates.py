"""Jev judgment gates for Voyager.

Each gate is a pure function: it takes a ``JevClient`` and a state dict from
``build_state``, asks one System One request, and returns a small result
object (or ``None`` when the client is disabled or the request failed).
Callers decide what to do with the numbers; gates never change control flow
themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import JevClient, answers_to_dict

try:
    from typesafe_sdk import Choice, Noul, Score
except Exception:  # pragma: no cover - the client is disabled in this case
    Choice = Noul = Score = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Critic shadow                                                               #
# --------------------------------------------------------------------------- #

_SUCCESS_TRUE = (
    "The final game state shows the task's goal reached or exceeded. "
    "For mining and crafting tasks check `inventory`: counts of the requested "
    "item class add up (e.g. 2 oak_log + 2 spruce_log satisfies 'Mine 3 wood logs'); "
    "mining an ore yields its drop (iron_ore -> raw_iron, diamond_ore -> diamond). "
    "For eating tasks `hunger` is 20. For kill tasks the mob's drop is in inventory "
    "(zombie -> rotten_flesh, cow -> beef or leather, sheep -> mutton or wool). "
    "For equip tasks the item appears in `equipment`. For deposit tasks "
    "`inventory_used` is 20 or fewer and the chest contains the items. "
    "For planting or placing tasks `nearby_blocks` shows the placed block."
)
_SUCCESS_FALSE = (
    "The goal item or quantity is absent; materials to craft the item are present "
    "but the item itself was not crafted; the wrong variant was obtained "
    "(cobblestone instead of stone, raw_iron when iron_ingot was required); "
    "hunger is below 20 for an eating task; more than 20 inventory slots remain "
    "used after a deposit task."
)


@dataclass
class CriticShadowResult:
    success: float
    success_by_delta: float | None
    target_item_missing: float
    quantity_short: float
    wrong_variant: float
    already_had: float
    verification_channel: str
    verification_confidence: float
    raw: dict[str, Any] = field(default_factory=dict)


def critic_shadow(
    client: JevClient,
    state: dict[str, Any],
    *,
    gpt_success: bool | None = None,
    gpt_critique: str = "",
    threshold: float = 0.8,
) -> CriticShadowResult | None:
    """Judge task completion from the final state. Logged next to GPT's verdict."""
    if not client.enabled:
        return None

    has_delta = "inventory_delta" in state
    questions: dict[str, Any] = {
        "success": Noul(
            instructions=(
                "Based on the final game state, did the player meet or exceed the "
                "requirements of `task`? Exceeding the requirement counts as success. "
                "Use `context` only to understand what the task means."
            ),
            criteria={"true": _SUCCESS_TRUE, "false": _SUCCESS_FALSE},
        ),
        "target_item_missing": Noul(
            instructions=(
                "Is the item class that `task` asks for (or its natural drop) completely "
                "absent from `inventory`, `equipment`, and `nearby_blocks`?"
            )
        ),
        "quantity_short": Noul(
            instructions=(
                "Does `inventory` contain the right item class for `task` but fewer "
                "than the quantity the task requires?"
            )
        ),
        "wrong_variant": Noul(
            instructions=(
                "Did the player obtain a related but wrong variant of what `task` asks for "
                "(e.g. cobblestone for stone, raw_iron for iron_ingot, planks for logs)?"
            )
        ),
        "already_had": Noul(
            instructions=(
                "Was the requirement of `task` already satisfied by `inventory_before_task` "
                "so that nothing new had to be obtained? If `inventory_before_task` is "
                "absent answer no."
            )
        ),
        "verification_channel": Choice(
            instructions=(
                "Which part of the state is the primary evidence for whether `task` "
                "was completed?"
            ),
            criteria={
                "inventory": "Mining, crafting, smelting, cooking, and kill tasks",
                "equipment": "Equip tasks",
                "hunger": "Eating tasks",
                "nearby_blocks": "Placing or planting tasks verified by blocks in the world",
                "chest": "Deposit or take-from-chest tasks",
                "none": "The task cannot be verified from this state",
            },
        ),
    }
    if has_delta:
        questions["success_by_delta"] = Noul(
            instructions=(
                "Counting only items gained during this task (`inventory_delta`, "
                "positive values are gains), did the player acquire what `task` requires? "
                "Items already held before the task do not count."
            ),
            criteria={
                "true": "The gained items alone satisfy the task's item class and quantity.",
                "false": "The gains fall short even if the total inventory satisfies the task.",
            },
        )

    extra: dict[str, Any] = {
        "gpt_success": gpt_success,
        "gpt_critique": gpt_critique,
        "threshold": threshold,
        "delta_spans_whole_task": True,
    }
    response = client.ask("critic_shadow", state, questions, extra_record=extra)
    if response is None:
        return None

    answers = answers_to_dict(response)
    result = CriticShadowResult(
        success=answers["success"]["noul"],
        success_by_delta=answers.get("success_by_delta", {}).get("noul"),
        target_item_missing=answers["target_item_missing"]["noul"],
        quantity_short=answers["quantity_short"]["noul"],
        wrong_variant=answers["wrong_variant"]["noul"],
        already_had=answers["already_had"]["noul"],
        verification_channel=answers["verification_channel"]["choice"],
        verification_confidence=answers["verification_channel"]["confidence"],
        raw=answers,
    )
    jev_would_say = result.success >= threshold
    client.log_only(
        "critic_shadow_decisions",
        {
            "task": state.get("task"),
            "gpt_success": gpt_success,
            "jev_success": result.success,
            "jev_success_by_delta": result.success_by_delta,
            "jev_would_say": jev_would_say,
            "agree": (gpt_success == jev_would_say) if gpt_success is not None else None,
            "verification_channel": result.verification_channel,
        },
    )
    return result


# --------------------------------------------------------------------------- #
# QA ranking                                                                  #
# --------------------------------------------------------------------------- #

_QA_LEVELS = [
    "Generic, or already answered by the current inventory and progress; "
    "would not change which task to pick next.",
    "Marginally useful; background knowledge that might matter later.",
    "Directly actionable for choosing the next task given the current inventory, "
    "equipment, biome, and completed tasks.",
]


@dataclass
class QARankResult:
    scores: list[float]
    same_as_cached: dict[int, float]  # question index -> probability
    raw: dict[str, Any] = field(default_factory=dict)


def qa_rank(
    client: JevClient,
    state: dict[str, Any],
    questions: list[str],
    *,
    cached_neighbours: dict[int, tuple[str, float]] | None = None,
) -> QARankResult | None:
    """Score each candidate question's usefulness; optionally test cache equivalence.

    ``cached_neighbours`` maps a question index to ``(cached_question, distance)``
    for questions whose nearest cached neighbour lies in the gray band.
    """
    if not client.enabled or not questions:
        return None

    jev_questions: dict[str, Any] = {}
    for i, question in enumerate(questions):
        jev_questions[f"q{i}"] = Score(
            instructions={
                "question": question,
                "ask": (
                    "How useful is answering `question` for choosing the player's next "
                    "task, given the current inventory, equipment, biome, completed "
                    "tasks, and failed tasks in the state?"
                ),
            },
            criteria=_QA_LEVELS,
        )
    for i, (cached_question, _distance) in (cached_neighbours or {}).items():
        jev_questions[f"same{i}"] = Noul(
            instructions={
                "candidate": questions[i],
                "cached": cached_question,
                "ask": (
                    "Do `candidate` and `cached` ask for the same information, so that "
                    "an answer to `cached` fully answers `candidate`?"
                ),
            }
        )

    response = client.ask(
        "qa_rank",
        state,
        jev_questions,
        extra_record={
            "questions": questions,
            "cached_neighbours": {
                str(i): {"cached": q, "distance": d}
                for i, (q, d) in (cached_neighbours or {}).items()
            },
        },
    )
    if response is None:
        return None

    answers = answers_to_dict(response)
    scores = [answers[f"q{i}"]["score"] for i in range(len(questions))]
    same = {
        i: answers[f"same{i}"]["noul"]
        for i in (cached_neighbours or {})
        if f"same{i}" in answers
    }
    return QARankResult(scores=scores, same_as_cached=same, raw=answers)


# --------------------------------------------------------------------------- #
# Proposed-task fan-out                                                       #
# --------------------------------------------------------------------------- #

FEASIBILITY_LEVELS = [
    "Already satisfied by the current inventory or equipment; nothing needs doing.",
    "One skill call away: all required items are in hand (e.g. craft with materials held).",
    "Needs gathering items that are visible in nearby blocks or entities first.",
    "Needs a tool tier, block, or mob that has not been seen yet; exploration required.",
    "Multi-stage: requires several unlocks (new tools, smelting chain, rare resources).",
]

VETO_IDS = (
    "requires_placing_building_planting_trading",
    "pointless_repeat",
    "in_failed_tasks_unchanged",
)


@dataclass
class TaskFanoutResult:
    vetoes: dict[str, float]
    requires_missing_tool_tier: float
    feasibility: float
    feasibility_confidence: float
    feasibility_probabilities: list[float]
    verb_class: str
    verb_confidence: float
    raw: dict[str, Any] = field(default_factory=dict)

    def fired(self, threshold: float) -> list[str]:
        return [name for name, p in self.vetoes.items() if p >= threshold]


def task_fanout(client: JevClient, state: dict[str, Any]) -> TaskFanoutResult | None:
    """Judge a GPT-proposed task (held in ``state['task']``) in one request."""
    if not client.enabled:
        return None

    questions: dict[str, Any] = {
        "requires_placing_building_planting_trading": Noul(
            instructions=(
                "Does `task` require placing blocks, building a structure, digging a "
                "shaped hole, planting crops, or trading with villagers? These cannot be "
                "verified from the player's inventory and status."
            ),
            criteria={
                "true": "Tasks starting with or centred on place, build, dig a shelter/hole, plant, farm, trade.",
                "false": "Mine, craft, smelt, cook, kill, equip, collect, explore-and-obtain tasks.",
            },
        ),
        "pointless_repeat": Noul(
            instructions=(
                "Is `task` the same objective as an entry in `completed_tasks` AND is its "
                "requested quantity already satisfied by `inventory`, so repeating it "
                "gains nothing? Gathering more of a resource that is needed for a later "
                "craft is NOT pointless."
            ),
            criteria={
                "true": "Same item class and the inventory already holds at least the requested count.",
                "false": "A new objective, or a repeat that would add resources the player still needs.",
            },
        ),
        "in_failed_tasks_unchanged": Noul(
            instructions=(
                "Is `task` the same objective as an entry in `failed_tasks`, with no new "
                "tools, materials, or nearby resources in the state that would make it "
                "achievable now?"
            )
        ),
        "requires_missing_tool_tier": Noul(
            instructions=(
                "Does `task` require a tool tier the player does not have in `inventory` "
                "or `equipment` (e.g. iron_ore needs a stone pickaxe or better, diamond_ore "
                "needs an iron pickaxe or better, obsidian needs a diamond pickaxe)?"
            )
        ),
        "feasibility": Score(
            instructions=(
                "How far is the player from completing `task` given `inventory`, "
                "`equipment`, `nearby_blocks`, `other_blocks_recently_seen`, and "
                "`nearby_entities_nearest_first`?"
            ),
            criteria=FEASIBILITY_LEVELS,
        ),
        "verb_class": Choice(
            instructions="What kind of action does `task` primarily ask for?",
            criteria={
                "mine": "Mine or collect blocks",
                "craft": "Craft at a crafting table or in inventory",
                "smelt": "Smelt in a furnace",
                "kill": "Kill a mob",
                "cook": "Cook food",
                "equip": "Equip armor or a tool",
                "explore": "Find or reach something",
                "other": None,
            },
        ),
    }

    response = client.ask("task_fanout", state, questions)
    if response is None:
        return None

    answers = answers_to_dict(response)
    return TaskFanoutResult(
        vetoes={name: answers[name]["noul"] for name in VETO_IDS},
        requires_missing_tool_tier=answers["requires_missing_tool_tier"]["noul"],
        feasibility=answers["feasibility"]["score"],
        feasibility_confidence=answers["feasibility"]["confidence"],
        feasibility_probabilities=answers["feasibility"]["probabilities"],
        verb_class=answers["verb_class"]["choice"],
        verb_confidence=answers["verb_class"]["confidence"],
        raw=answers,
    )


VETO_MESSAGES = {
    "requires_placing_building_planting_trading": (
        "placing, building, planting, and trading tasks cannot be verified from my "
        "status and inventory (criterion 8)"
    ),
    "pointless_repeat": (
        "it repeats a completed task whose quantity my inventory already satisfies "
        "(criteria 5 and 6)"
    ),
    "in_failed_tasks_unchanged": (
        "it is in my failed tasks and nothing in my inventory or surroundings has "
        "changed to make it achievable (criterion 4)"
    ),
}
