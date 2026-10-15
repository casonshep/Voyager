"""Offline tests for the post-goal review (voyager.tree.review) and the experience
store (voyager.tree.experience). No Minecraft, no API keys."""
from __future__ import annotations

import json
import os
import tempfile

from voyager.tree.experience import TraceStore, family_of, read_decisions, similarity
from voyager.tree.review import GoalReviewer, LessonBook


def records(prefix="mg1", n=6, subtask_id="n3", goal_id="g1"):
    out = []
    for i in range(1, n + 1):
        failed = i < n
        out.append({
            "id": f"{prefix}-{i}", "ts": 1000 * i, "goal_id": goal_id, "subtask_id": subtask_id,
            "subtask": "place a crafting_table nearby", "action": "x:place_crafting_table",
            "outcome": "failed: bot.equip(item, destination): item must be an Item OBJECT" if failed else "ok placed crafting_table",
            "label": "API_MISUSE" if failed else "OK", "delta": {} if failed else {"inv": {"crafting_table": -1}},
            "state": {"cursor": "grass_block", "inventory": {"crafting_table": 1}},
        })
    return out


NODES = [{"id": "n3", "kind": "place", "description": "place a crafting_table nearby", "done": True, "actions_spent": 6}]

GOOD_MD = """## Tried
- x:place_crafting_table, failing five times then working {mg1-6}
## Observed
- equip with a name string fails with API_MISUSE {mg1-1}
## Still doubt
- whether placing works on sand {mg1-6}
## Test next
- place a table on a slope {mg1-6}
"""


class StubLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete_json(self, messages, schema_hint=None):
        self.calls.append(messages)
        return self.replies.pop(0)


def test_review_writes_lesson_sections_and_evidence_backed_remedies():
    root = tempfile.mkdtemp()
    book = LessonBook(root)
    llm = StubLLM([{
        "lesson_markdown": GOOD_MD,
        "remedies": [
            {"failure_type": "api_misuse", "repair_action": "find the Item object in bot.inventory.items() before equip", "evidence_ids": ["mg1-1", "mg1-6"]},
            {"failure_type": "NO_TARGET", "repair_action": "made up", "evidence_ids": ["zz-99"]},  # no evidence here: dropped
        ],
        "remedy_verdicts": [],
    }])
    summary = GoalReviewer(llm, book).review(goal="obtain a wooden pickaxe", goal_id="g1", success=True, nodes=NODES, decisions=records())
    assert summary["families"] == ["place"] and summary["remedies_added"] == 1
    text = book.read("place")
    assert "## Still doubt" in text and "reference guidance, not ground truth" in text.lower()
    rems = book.remedies()
    assert rems[0]["status"] == "hypothesis" and rems[0]["failure_type"] == "API_MISUSE" and rems[0]["support"] == ["g1"]
    prompt = llm.calls[0][1]["content"]
    assert "{mg1-1}" in prompt and "API_MISUSE" in prompt and "(no lesson file yet)" in prompt
    assert book.test_next() == ["[place] place a table on a slope {mg1-6}"]
    assert book.still_doubt("place") == ["whether placing works on sand {mg1-6}"]


def test_invalid_review_is_sent_back_then_previous_version_is_archived():
    root = tempfile.mkdtemp()
    book = LessonBook(root)
    book.write("place", GOOD_MD, goal="earlier", goal_id="g0")
    bad = {"lesson_markdown": "## Tried\n- something", "remedies": [], "remedy_verdicts": []}
    good = {"lesson_markdown": GOOD_MD.replace("slope", "hill"), "remedies": [], "remedy_verdicts": []}
    llm = StubLLM([bad, good])
    summary = GoalReviewer(llm, book).review(goal="g", goal_id="g1", success=False, nodes=NODES, decisions=records())
    assert summary["families"] == ["place"]
    assert "lacks the section" in llm.calls[1][-1]["content"]
    assert "hill" in book.read("place") and len(os.listdir(book.history)) == 1
    assert "slope" in open(os.path.join(book.history, os.listdir(book.history)[0])).read()


def test_remedy_is_confirmed_by_two_goals_and_disputed_by_two_contradictions():
    root = tempfile.mkdtemp()
    book = LessonBook(root)
    book.save_remedies([{"id": "rm-1", "scope": "place", "failure_type": "API_MISUSE", "repair_action": "x",
                         "status": "hypothesis", "support": ["g1"], "contradicted": 0, "evidence": {}}])
    reply = lambda verdict, ids: {"lesson_markdown": GOOD_MD, "remedies": [], "remedy_verdicts": [{"id": "rm-1", "verdict": verdict, "evidence_ids": ids}]}
    GoalReviewer(StubLLM([reply("supported", ["mg1-2"])]), book).review(goal="g", goal_id="g2", success=True, nodes=NODES, decisions=records())
    assert book.remedies()[0]["status"] == "confirmed"
    # a verdict that cites no record from this goal changes nothing
    GoalReviewer(StubLLM([reply("contradicted", ["nope-1"])]), book).review(goal="g", goal_id="g3", success=True, nodes=NODES, decisions=records())
    assert book.remedies()[0]["contradicted"] == 0
    for gid in ("g4", "g5"):
        GoalReviewer(StubLLM([reply("contradicted", ["mg1-3"])]), book).review(goal="g", goal_id=gid, success=True, nodes=NODES, decisions=records())
    assert book.remedies()[0]["status"] == "disputed"
    assert book.for_builder("place", ["API_MISUSE"]).count("REMEDIES") == 0  # disputed remedies are not shown


def test_for_builder_text_and_small_families_are_skipped():
    root = tempfile.mkdtemp()
    book = LessonBook(root)
    assert book.for_builder("place") == ""
    book.write("place", GOOD_MD, goal="g", goal_id="g1")
    book.save_remedies([{"id": "rm-1", "scope": "craft", "failure_type": "API_MISUSE", "repair_action": "use Item objects",
                         "status": "confirmed", "support": ["g1", "g2"]}])
    text = book.for_builder("place", ["API_MISUSE"])
    assert "LESSONS FROM EARLIER PLAY for 'place'" in text and "claims, not ground truth" in text
    assert "[confirmed, 2 goal(s)] when API_MISUSE: use Item objects" in text
    summary = GoalReviewer(StubLLM([]), book).review(goal="g", goal_id="g1", success=True, nodes=NODES, decisions=records(n=2))
    assert summary["families"] == [] and "only 2 record(s)" in summary["skipped"][0]


def test_decision_log_and_trace_store():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "decisions.jsonl")
    with open(path, "w") as fh:
        for r in records(goal_id="g1") + records(prefix="mg2", goal_id="g2"):
            fh.write(json.dumps(r) + "\n")
        fh.write("not json\n")
    assert len(read_decisions(path)) == 12 and [r["id"] for r in read_decisions(path, goal_id="g2")][:2] == ["mg2-1", "mg2-2"]
    assert family_of("smelt") == "craft" and family_of("locate") == "find" and family_of(None) == "goal"
    assert similarity("obtain 3 logs (any wood) by mining logs", "obtain 11 cobblestone by mining stone") == 0.0
    store = TraceStore(d)
    store.save(description="craft 4 stick", kind="craft", records=records(n=3), goal_id="g1")
    store.save(description="craft 9 planks (any wood)", kind="craft", records=records(n=2), goal_id="g1")
    assert store.most_similar("craft 2 stick", "craft")["description"] == "craft 4 stick"
    assert store.most_similar("place a furnace nearby", "place") is None


def test_builder_prompt_carries_the_family_lesson():
    from voyager.tree.builder import ActionBuilder
    from test_builder import GOOD, DisabledJev, FakeBridge, FakeLLM, status

    root = tempfile.mkdtemp()
    book = LessonBook(root)
    book.write("place", GOOD_MD, goal="g", goal_id="g1")
    llm = FakeLLM([GOOD])
    builder = ActionBuilder(FakeBridge([]), DisabledJev(), llm, ckpt_dir=root, lessons=book, min_seconds_between_builds=0)
    place = {"id": "n2", "description": "place a crafting_table nearby", "kind": "place",
             "work": {"kind": "place", "item": "crafting_table"}, "verify": {"near_block": {"name": "crafting_table", "radius": 4}}}
    builder.build(place, status(), ["crafting_table"])
    assert "LESSONS FROM EARLIER PLAY for 'place'" in llm.prompts[0][1]["content"]
