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

    def agent_primitive(self, name, source, purpose=None, repair=False, version=None):
        self.posted.append((name, source))
        self.flags = getattr(self, "flags", [])
        self.flags.append({"name": name, "repair": repair, "version": version})
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
    assert not [f for f in os.listdir(os.path.join(d, "primitives")) if f.endswith(".js")]
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
    # The same design (kind and target) as the failing action already built for this
    # subtask is that action again: it is repaired in place, not minted as mine_cursor_2.
    assert forced == "mine_cursor" and set(builder.registered) == {"mine_cursor"}
    assert bridge.posted[1][0] == "mine_cursor_draft" and bridge.posted[2][0] == "mine_cursor"
    assert bridge.flags[2] == {"name": "mine_cursor", "repair": True, "version": 2}
    assert builder.meta["mine_cursor"]["version"] == 2 and builder.meta["mine_cursor"]["versions"][0]["version"] == 1


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
    # trying a kind no longer hides it from the next design (that hid `mine` from the
    # cobblestone step, F2); only a kind retired for good after its repair budget is closed
    assert "craft" in builder._open_kinds("n3")
    builder.meta[name]["final_retired"] = True
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
    assert second == first  # repaired in place under the same name
    prompt = llm.prompts[1][1]["content"]
    assert f"REPAIR the existing action x:{first}" in prompt and "Took to long" in prompt and "kind goto_nearest" in prompt
    assert "Its full source (version 1)" in prompt and "await bot.dig(block)" in prompt  # the whole module, not 900 chars
    assert builder.meta[first]["version"] == 2 and builder.meta[first]["kind"] == "goto_nearest"
    # Node resets the repaired action's counts; with fresh counts it is not failing any more
    fresh = status([{"name": first, "trials": 0, "successes": 0, "failures": 0, "retired": False, "trial": True}])
    assert builder._failing_trial(fresh, subtask()["id"]) is None


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



def labelled(name, n, label="API_MISUSE", retired=False):
    log = [{"id": f"r-{i}", "ok": False, "label": label, "outcome": f"failed: bot.equip(item, destination): item must be an Item OBJECT ({i})"} for i in range(n)]
    return {"name": name, "trials": n, "successes": 0, "failures": n, "retired": retired, "trial": not retired,
            "outcome_log": log, "last_outcomes": [o["outcome"] for o in log][-3:]}


def test_retired_action_is_repaired_with_its_labelled_evidence():
    # F1: Node retires an action after 3 failures; the builder used to skip retired actions,
    # so repair never happened and GPT rewrote it from scratch under a new name
    llm = FakeLLM([GOOD, GOOD])
    bridge = FakeBridge([])
    builder, d = make(bridge, llm)
    first = builder.build(subtask(), status(), [], reason="first")
    name = builder.build(subtask(), status([labelled(first, 3, retired=True)]), [], reason="stuck")
    assert name == first
    prompt = llm.prompts[1][1]["content"]
    assert "[API_MISUSE]" in prompt and "{r-2}" in prompt and "Item OBJECT" in prompt
    assert "API_MISUSE x3" in prompt  # the label hint block
    assert os.path.exists(os.path.join(d, "primitives", "versions", f"{first}.v1.js"))
    v1 = builder.meta[first]["versions"][0]
    assert v1["labels"] == {"API_MISUSE": 3} and v1["evidence"] == ["r-0", "r-1", "r-2"]


def test_rejected_repair_draft_never_touches_the_registered_action():
    llm = FakeLLM([GOOD, GOOD, GOOD, GOOD])
    bridge = FakeBridge([{"ok": True, "error": None},
                         {"ok": False, "error": "compile error: a"}, {"ok": False, "error": "compile error: b"},
                         {"ok": False, "error": "compile error: c"}])
    builder, _ = make(bridge, llm)
    first = builder.build(subtask(), status(), [])
    assert builder.build(subtask(), status([labelled(first, 2)]), []) is None
    assert [n for n, _ in bridge.posted[1:]] == [f"{first}_draft"] * 3  # drafts only; the real one untouched
    assert builder.meta[first]["version"] == 1 and "drafts rejected" in builder.last_refusal


def test_repair_budget_restores_the_best_version_or_retires_for_good():
    llm = FakeLLM([GOOD] * 6)
    bridge = FakeBridge([])
    builder, d = make(bridge, llm, max_repairs=1)
    first = builder.build(subtask(), status(), [])
    builder.build(subtask(), status([labelled(first, 2)]), [])
    builder.meta[first]["versions"][0]["successes"] = 2  # pretend v1 had worked before
    assert builder.meta[first]["version"] == 2
    # v2 fails too: the budget (1 repair) is spent, so v1 comes back instead of a v3
    builder.build(subtask(), status([labelled(first, 2)]), [])
    assert builder.meta[first]["version"] == 1 and builder.meta[first]["restored_from"] == 1
    assert bridge.flags[-1]["repair"] is True and bridge.flags[-1]["version"] == 1
    # with no version that ever worked, the action is retired for good and its kind closes
    builder2, _ = make(FakeBridge([]), FakeLLM([GOOD] * 4), max_repairs=1)
    a = builder2.build(subtask(), status(), [])
    builder2.build(subtask(), status([labelled(a, 2)]), [])
    builder2.build(subtask(), status([labelled(a, 2)]), [])
    assert builder2.meta[a]["final_retired"] and "other" in builder2._open_kinds(subtask()["id"])


def test_prune_archives_duplicates_and_long_untried_entries():
    d = tempfile.mkdtemp()
    lib = os.path.join(d, "lib")
    os.makedirs(lib)
    def put(name, **meta):
        with open(os.path.join(lib, f"{name}.js"), "w") as fh:
            fh.write("// x")
        with open(os.path.join(lib, f"{name}.json"), "w") as fh:
            json.dump(meta, fh)
    spec = {"has_item": {"name": "*_log", "count": 3}}
    put("approach_logs", kind="other", target="*_log", purpose_spec=spec, trials=10, successes=7)
    put("approach_logs_2", kind="other", target="*_log", purpose_spec=spec, trials=0, successes=0)
    put("approach_logs_3", kind="other", target="*_log", purpose_spec=spec, trials=3, successes=1)
    put("craft_planks", kind="craft", target="*_planks", purpose_spec={}, trials=0, successes=0, runs_seen=2)
    put("craft_sticks", kind="craft", target="stick", purpose_spec={}, trials=4, successes=4)
    builder = ActionBuilder(FakeBridge([]), DisabledJev(), None, ckpt_dir=d, library_dir=lib)
    archived = builder.prune()
    assert archived == ["approach_logs_2", "approach_logs_3", "craft_planks"]
    assert sorted(builder.saved()) == ["approach_logs", "craft_sticks"]
    with open(os.path.join(lib, "_archive", "approach_logs_3.json")) as fh:
        assert "duplicate of x:approach_logs" in json.load(fh)["archived"]


def test_completed_trace_is_retrieved_into_the_next_prompt():
    llm = FakeLLM([GOOD])
    builder, _ = make(FakeBridge([]), llm)
    builder.remember({"id": "n5", "description": "obtain 3 cobblestone by mining stone", "kind": "mine"},
                     [{"id": "a-1", "action": "x:approach_stone", "outcome": "ok moved 6", "label": "OK", "delta": {"pos": 6}},
                      {"id": "a-2", "action": "x:mine_stone", "outcome": "ok mined stone", "label": "OK", "delta": {"inv": {"cobblestone": 1}}}],
                     goal_id="g1")
    builder.build({**subtask(), "kind": "mine"}, status(), [])
    prompt = llm.prompts[0][1]["content"]
    assert "PREVIOUS COMPLETED ATTEMPT" in prompt and "x:mine_stone -> ok mined stone [OK] (+cobblestone 1)" in prompt


def test_record_outcomes_skips_while_a_build_holds_the_lock():
    builder, d = make(FakeBridge([]), FakeLLM([GOOD]))
    name = builder.build(subtask(), status(), [])
    with builder._lock:
        import threading
        t = threading.Thread(target=builder.record_outcomes, args=(status([{"name": name, "trials": 5, "successes": 5, "failures": 0}]),))
        t.start()
        t.join(timeout=2)
        assert not t.is_alive()  # returned at once instead of waiting for the build
    assert builder.meta[name]["successes"] == 0
    builder.record_outcomes(status([{"name": name, "trials": 5, "successes": 5, "failures": 0}]))
    assert builder.meta[name]["successes"] == 5



def test_untested_for_lists_fresh_actions_of_this_subtask_only():
    builder, _ = make(FakeBridge([]), FakeLLM([GOOD]))
    name = builder.build(subtask(), status(), [])
    fresh = status([{"name": name, "trials": 0, "successes": 0, "failures": 0, "retired": False}])
    assert builder.untested_for(subtask()["id"], fresh) == [name]
    assert builder.untested_for("n99", fresh) == []
    tried = status([{"name": name, "trials": 1, "successes": 0, "failures": 1, "retired": False}])
    assert builder.untested_for(subtask()["id"], tried) == []
    stale = status([{"name": name, "trials": 0, "stale": True, "retired": False}])
    assert builder.untested_for(subtask()["id"], stale) == []
