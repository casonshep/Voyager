"""Offline tests for voyager.tree.builder (no Minecraft, no API keys)."""
from __future__ import annotations

import json
import os
import tempfile

from voyager.tree.builder import ActionBuilder, extract_module, static_check

GOOD = """```javascript
// name: mine_cursor
// Offered when a block is at the cursor.
const PURPOSE = { description: "mine a needed block within reach and pick up the drop", achieves: { has_item: { name: "*_log", count: 3 } } };
function menu(snap, loop) {
    if (!snap.cursor || !snap.cursor.block) return null;
    return `Mine the ${snap.cursor.block} at the cursor`;
}
async function execute(bot, loop, ctx) {
    const block = bot.blockAtCursor(6);
    if (!block) return "failed: nothing at cursor";
    await bot.dig(block);
    return "ok mined " + block.name;
}
function observe(bot, loop) {
    return { lookingAt: bot.blockAtCursor(6) ? bot.blockAtCursor(6).name : null };
}
```"""

NO_AWAIT_LOOP = GOOD.replace("await bot.dig(block);", "for (let i = 0; i < 3; i++) { bot.dig(block); }\n    await bot.dig(block);")
CHATTY = GOOD.replace("await bot.dig(block);", "bot.chat('hi');\n    await bot.dig(block);")


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, messages):
        self.prompts.append(list(messages))
        return self.replies.pop(0)


class FakeBridge:
    def __init__(self, results):
        self.results = list(results)
        self.posted = []
        self.removed = []

    def agent_primitive_remove(self, name):
        self.removed.append(name)
        return {"ok": True}

    def agent_primitive(self, name, source):
        self.posted.append((name, source))
        return self.results.pop(0) if self.results else {"ok": True, "error": None}


class DisabledJev:
    enabled = False

    def __init__(self):
        self.logged = []

    def log_only(self, gate, record):
        self.logged.append((gate, record))


def subtask():
    return {"id": "n4", "description": "obtain 3 cobblestone", "verify": {"has_item": {"name": "cobblestone", "count": 3}}}


def status(primitives=()):
    return {
        "snap": {"cursor": {"block": "stone", "distance": 2, "entity": None}, "inventory": {"oak_log": 2}, "blocks": {"stone": {"dx": 0, "dy": -1, "dz": 1, "count": 30}}},
        "recent": [{"action": "move:north", "outcome": "ok moved 4", "delta": {"pos": 4}}] * 3,
        "menu": {"move:north": "walk up to 4 blocks north"},
        "primitives": list(primitives),
    }


def make(bridge, llm, **kw):
    d = tempfile.mkdtemp()
    kw.setdefault("min_seconds_between_builds", 0)
    return ActionBuilder(bridge, DisabledJev(), llm, ckpt_dir=d, **kw), d


# ---------------------------------------------------------------------- #

def test_static_check_and_extract_module():
    name, source = extract_module(GOOD)
    assert name == "mine_cursor" and static_check(name, source) is None
    assert static_check("Bad-Name", source)
    assert "without await" in static_check("xx", extract_module(NO_AWAIT_LOOP)[1])
    assert "bot.chat" in static_check("xx", extract_module(CHATTY)[1])
    assert "observe() must be synchronous" in static_check("xx", source.replace("function observe", "async function observe"))
    assert "missing `function menu" in static_check("xx", "async function execute(bot, loop, ctx) { return 'ok'; }")
    assert extract_module("no code here")[0] is None and extract_module("")[1] is None


def test_build_registers_persists_and_counts_as_trial():
    llm = FakeLLM([GOOD])
    bridge = FakeBridge([{"ok": True, "error": None}])
    builder, d = make(bridge, llm)
    name = builder.build(subtask(), status(), ["cobblestone", "stone_pickaxe"], reason="completable low")
    assert name == "mine_cursor" and bridge.posted[0][0] == "mine_cursor"
    assert os.path.exists(os.path.join(d, "primitives", "mine_cursor.js"))
    with open(os.path.join(d, "primitives", "mine_cursor.json"), encoding="utf-8") as fh:
        side = json.load(fh)
    assert side["kind"] == "other" and side["target"] == "none" and side["subtask"] == "obtain 3 cobblestone"
    prompt = llm.prompts[0][1]["content"]
    assert "TEMPLATE RULE" in prompt and "cobblestone" in prompt and "move:north" in prompt and "function observe" in prompt
    # a trial Jev has never picked does not block (it is not being tested); one that was tried and failed does
    assert builder.cooling_down(status([{"name": "mine_cursor", "trials": 0, "successes": 0, "failures": 0, "retired": False, "trial": True}])) is None
    why = builder.cooling_down(status([{"name": "mine_cursor", "trials": 1, "successes": 0, "failures": 1, "retired": False, "trial": True}]))
    assert "untested" in why
    assert builder.build(subtask(), status([{"name": "mine_cursor", "trials": 1, "successes": 0, "failures": 1, "retired": False, "trial": True}]), []) is None
    # once it succeeded the cooldown lifts
    assert builder.cooling_down(status([{"name": "mine_cursor", "trials": 2, "successes": 1, "failures": 1, "retired": False, "trial": False}])) is None


def test_static_check_rejection_retries_once_with_feedback():
    llm = FakeLLM([CHATTY, GOOD])
    bridge = FakeBridge([{"ok": True, "error": None}])
    builder, _ = make(bridge, llm)
    assert builder.build(subtask(), status(), []) == "mine_cursor"
    assert len(llm.prompts) == 2 and "rejected" in llm.prompts[1][-1]["content"] and "bot.chat" in llm.prompts[1][-1]["content"]
    assert len(bridge.posted) == 1


def test_node_rejection_retries_once_then_gives_up():
    llm = FakeLLM([GOOD, GOOD, GOOD])
    bridge = FakeBridge([{"ok": False, "error": "compile error: x"}, {"ok": False, "error": "compile error: y"}, {"ok": False, "error": "compile error: z"}])
    builder, d = make(bridge, llm)
    assert builder.build(subtask(), status(), []) is None
    assert len(bridge.posted) == 3 and len(llm.prompts) == 3
    assert "compile error: x" in llm.prompts[1][-1]["content"]
    assert not os.listdir(os.path.join(d, "primitives"))
    assert any(g == "action_generation_failed" for g, _ in builder.jev.logged)


def test_name_is_derived_when_missing_and_bad_reply_is_handled():
    unnamed = GOOD.replace("// name: mine_cursor\n", "")
    llm = FakeLLM(["no code at all", unnamed])
    builder, _ = make(FakeBridge([]), llm)
    name = builder.build(subtask(), status(), [])
    assert name == "gen_other_none"


def test_reload_reposts_saved_primitives_and_record_outcomes_updates_sidecar():
    llm = FakeLLM([GOOD])
    builder, d = make(FakeBridge([]), llm)
    builder.build(subtask(), status(), [])
    bridge2 = FakeBridge([{"ok": True, "error": None}])
    builder2 = ActionBuilder(bridge2, DisabledJev(), None, ckpt_dir=d)
    assert builder2.reload() == 1 and bridge2.posted[0][0] == "mine_cursor"
    assert builder2.meta["mine_cursor"]["subtask"] == "obtain 3 cobblestone"
    builder2.record_outcomes(status([{"name": "mine_cursor", "trials": 3, "successes": 2, "failures": 1, "retired": False, "trial": False}]))
    with open(os.path.join(d, "primitives", "mine_cursor.json"), encoding="utf-8") as fh:
        assert json.load(fh)["successes"] == 2


def test_max_live_and_disabled():
    builder, _ = make(FakeBridge([]), FakeLLM([GOOD]), max_live=1)
    assert "live already" in builder.cooling_down(status([{"name": "other", "successes": 1, "retired": False}]))
    builder.enabled = False
    assert builder.build(subtask(), status(), []) is None


def test_forced_build_bypasses_trial_gate_and_names_stay_unique():
    # a half-tested trial from this subtask normally blocks; a forced build (inputs in hand) does not
    llm = FakeLLM([GOOD, GOOD])
    bridge = FakeBridge([])
    builder, _ = make(bridge, llm)
    first = builder.build(subtask(), status(), [], reason="completable low")
    assert first == "mine_cursor"
    tried = status([{"name": "mine_cursor", "trials": 1, "successes": 0, "failures": 1, "retired": False, "trial": True}])
    assert builder.build(subtask(), tried, [], reason="again") is None
    forced = builder.build(subtask(), tried, [], reason="inputs in hand", force=True)
    # GPT reused the name; the second action gets a suffix instead of replacing the first
    assert forced == "mine_cursor_2" and bridge.posted[1][0] == "mine_cursor_2"
    assert set(builder.registered) == {"mine_cursor", "mine_cursor_2"}


def test_work_step_skips_design_and_attempted_kinds_are_excluded():
    llm = FakeLLM([GOOD])
    bridge = FakeBridge([])
    builder, _ = make(bridge, llm)
    craft = {"id": "n3", "description": "craft 1 crafting_table", "kind": "craft",
             "work": {"kind": "craft", "item": "crafting_table", "count": 1}, "requires": {"*_planks": 4},
             "verify": {"has_item": {"name": "crafting_table", "count": 1}}}
    name = builder.build(craft, status(), ["crafting_table", "*_planks"], reason="inputs in hand")
    assert name == "mine_cursor"  # the module's own name; what matters is the design that was skipped
    assert builder.meta[name]["kind"] == "craft" and builder.meta[name]["target"] == "crafting_table"
    prompt = llm.prompts[0][1]["content"]
    assert "Kind of action to build: craft" in prompt and "crafting_table" in prompt
    # that kind is now excluded from the next design for this subtask, but not for another one
    assert "craft" not in builder._open_kinds("n3") and "craft" in builder._open_kinds("n9")


def test_not_applicable_now_is_withdrawn_and_revised():
    llm = FakeLLM([GOOD, GOOD])
    bridge = FakeBridge([
        {"ok": True, "error": None, "offered_now": False, "menu_line": None, "menu_error": None},
        {"ok": True, "error": None, "offered_now": True, "menu_line": "Mine the stone"},
    ])
    builder, _ = make(bridge, llm)
    name = builder.build(subtask(), status(), [])
    assert name is not None and bridge.removed == ["mine_cursor"]
    feedback = llm.prompts[1][-1]["content"]
    assert "could never choose it" in feedback and "stone" in feedback


def test_full_menu_evicts_weakest_instead_of_refusing():
    llm = FakeLLM([GOOD])
    bridge = FakeBridge([])
    builder, _ = make(bridge, llm, max_live=2)
    # two live generated actions: one proven, one that never succeeded and is rarely offered
    for name, sid in (("proven", "n1"), ("dud", "n1")):
        builder.registered[name] = "src"
        builder.meta[name] = {"subtask_id": sid, "kind": "mine", "created_at": 1.0}
    st = status([
        {"name": "proven", "trials": 4, "successes": 3, "failures": 1, "retired": False, "offered": 9},
        {"name": "dud", "trials": 3, "successes": 0, "failures": 3, "retired": False, "offered": 1},
    ])
    name = builder.build(subtask(), st, [], reason="needs a new action")
    assert name == "mine_cursor"
    assert bridge.removed == ["dud"] and "dud" not in builder.registered and "proven" in builder.registered
    assert builder.meta["dud"]["evicted"] and builder.meta["dud"]["retired"]


def test_gap_line_says_inputs_in_hand_and_missing_output():
    snap = {"inventory": {"spruce_planks": 16}}
    sub = {"requires": {"*_planks": 4}, "work": {"kind": "craft", "item": "crafting_table", "count": 1}}
    line = ActionBuilder._gap_line(sub, snap)
    assert "*_planks need 4, have 16 (enough)" in line
    assert "Output still missing: crafting_table (need 1, have 0)" in line
    assert "ITSELF" in line
    short = ActionBuilder._gap_line({"requires": {"*_log": 3}, "work": {"kind": "craft", "item": "*_planks", "count": 9}}, {"inventory": {"oak_log": 1}})
    assert "MISSING 2" in short and "ITSELF" not in short


def test_refusal_reason_is_recorded():
    builder, _ = make(FakeBridge([]), FakeLLM([]), enabled=False)
    assert builder.build(subtask(), status(), []) is None
    assert builder.last_refusal == "builder disabled"


def test_craft_step_rejects_recipe_lookup_for_an_input():
    craft_table = {"kind": "craft", "work": {"item": "crafting_table"}}
    assert "OUTPUT" in ActionBuilder._craft_target_problem(craft_table, 'const r = loop.recipesFor("*_planks", null);')
    assert ActionBuilder._craft_target_problem(craft_table, "loop.recipesFor('crafting_table', null)") is None
    assert ActionBuilder._craft_target_problem({"kind": "craft", "work": {"item": "*_planks"}}, 'loop.recipesFor("spruce_planks", null)') is None
    assert ActionBuilder._craft_target_problem({"kind": "place", "work": {"item": "crafting_table"}}, 'loop.recipesFor("stick", null)') is None


def test_failing_trial_is_repaired_with_same_kind_and_target():
    llm = FakeLLM([GOOD, GOOD])
    bridge = FakeBridge([])
    builder, _ = make(bridge, llm)
    first = builder.build(subtask(), status(), [], reason="first")
    builder.meta[first]["kind"], builder.meta[first]["target"] = "goto_nearest", "*_log"
    failing = status([{"name": first, "trials": 2, "successes": 0, "failures": 2, "retired": False, "trial": True,
                       "last_outcomes": ["failed: Took to long to decide path to goal!"] * 2}])
    second = builder.build(subtask(), failing, [], reason="stuck")
    assert second and second != first
    prompt = llm.prompts[1][1]["content"]
    assert "REPAIR x:" in prompt and "Kind of action to build: goto_nearest" in prompt and "Took to long" in prompt
    assert builder.meta[first]["repaired_by"] == second
    assert builder._failing_trial(failing, subtask()["id"]) is None  # not repaired twice


def test_trial_gate_waits_for_the_newest_action_to_get_its_chance():
    llm = FakeLLM([GOOD, GOOD, GOOD, GOOD])
    builder, _ = make(FakeBridge([]), llm)
    first = builder.build(subtask(), status(), [], reason="first")
    # just registered, offered twice, never tried: the next build for this subtask waits
    fresh = status([{"name": first, "trials": 0, "successes": 0, "offered": 2, "retired": False}])
    assert "has not had its chance" in builder.cooling_down(fresh, subtask()["id"])
    # released once Jev has tried it ...
    assert builder.cooling_down(status([{"name": first, "trials": 1, "successes": 1, "offered": 3, "retired": False}]), subtask()["id"]) is None
    # ... or once it sat on the menu unpicked for long enough ...
    assert builder.cooling_down(status([{"name": first, "trials": 0, "successes": 0, "offered": 6, "retired": False}]), subtask()["id"]) is None
    # ... or when it was never applicable
    assert builder.cooling_down(status([{"name": first, "trials": 0, "successes": 0, "offered": 0, "stale": True, "retired": False}]), subtask()["id"]) is None
    # a different subtask never waits on it
    assert builder.cooling_down(fresh, "n99") is None
