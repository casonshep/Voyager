"""Offline tests for the goal tree core (no Minecraft, no API keys).

Run with the venv python:  venv/Scripts/python tests/run_tests.py
"""
import importlib
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


tree_pkg = importlib.import_module("voyager.tree")
schema = importlib.import_module("voyager.tree.schema")
catalog = importlib.import_module("voyager.tree.catalog")
decomposer_mod = importlib.import_module("voyager.tree.decomposer")
llm_mod = importlib.import_module("voyager.tree.llm")

Node, Tree, VerifySpec = schema.Node, schema.Tree, schema.VerifySpec
Decomposer, Knowledge, verify_fn_from_facts = (
    decomposer_mod.Decomposer, decomposer_mod.Knowledge, decomposer_mod.verify_fn_from_facts,
)

with open(os.path.join(ROOT, "tests", "fixtures", "knowledge_1_19.json"), encoding="utf-8") as fh:
    KNOWLEDGE = json.load(fh)


# --------------------------------------------------------------------------- #
# stubs
# --------------------------------------------------------------------------- #
class _Answer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Response:
    def __init__(self, nouls=None, choices=None):
        self.nouls = {k: _Answer(noul=v) for k, v in (nouls or {}).items()}
        self.choices = {k: _Answer(choice=v, confidence=0.9, probabilities={v: 0.9}) for k, v in (choices or {}).items()}


class StubJev:
    enabled = True

    def __init__(self, nouls=None, choices=None):
        self.nouls, self.choices, self.calls = nouls or {}, choices or {}, []

    def ask(self, gate, state, questions, extra_record=None):
        self.calls.append((gate, list(questions)))
        return _Response(
            {k: self.nouls[k] for k in questions if k in self.nouls},
            {k: self.choices[k] for k in questions if k in self.choices},
        )


class StubLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def complete_json(self, messages, schema_hint=None):
        self.calls.append(messages)
        return self.replies.pop(0)


def _by_item(tree, item, kind=None):
    return [n for n in tree.nodes.values() if n.item == item and (kind is None or n.kind == kind)]


def _one(tree, item, kind=None):
    nodes = _by_item(tree, item, kind)
    assert len(nodes) == 1, f"expected one {kind or ''} node for {item}, got {[n.id for n in nodes]}"
    return nodes[0]


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #
def test_node_and_tree_round_trip():
    tree = Tree(goal_id="g1", goal_text="obtain a stick")
    root = tree.add(Node(id=tree.new_id(), description="craft 2 stick", kind="craft",
                         verify=catalog.item_spec("stick", 2).to_json(), requires={"oak_planks": 2},
                         work={"kind": "craft", "item": "stick", "count": 2}))
    leaf = tree.add(Node(id=tree.new_id(), description="obtain 1 oak_log", kind="mine",
                         verify=catalog.item_spec("oak_log", 1).to_json(),
                         work={"kind": "mine", "item": "oak_log", "count": 1, "blocks": ["oak_log"]}), parent=root.id)
    assert root.children == [leaf.id] and leaf.parent == root.id
    assert [n.id for n in tree.post_order()] == [leaf.id, root.id]
    assert tree.next_pending() is leaf
    leaf.mark_done([{"action": "move:north", "outcome": "ok", "ms": 10, "delta": {}}])
    assert leaf.done and leaf.done_at and leaf.trace[0]["action"] == "move:north"
    assert tree.next_pending() is root
    data = json.loads(json.dumps(tree.to_json()))
    back = Tree.from_json(data)
    assert back.get(leaf.id).done and back.get(root.id).requires == {"oak_planks": 2}
    assert back.new_id() == "n3"  # counter restored
    # the frozen node fields (section 1.1) are all present
    for key in ("id", "parent", "children", "description", "kind", "verify", "requires", "acquire",
                "work", "done", "done_at", "attempts", "actions_spent", "budget_actions", "trace"):
        assert key in data["nodes"][root.id], key
    assert tree.items() >= {"stick", "oak_planks", "oak_log"}
    with tempfile.TemporaryDirectory() as d:
        path = tree.save(d)
        assert path.endswith(os.path.join("tree", "g1.json"))
        assert Tree.load(path).goal_text == "obtain a stick"
        assert Tree.latest(d).goal_id == "g1"


def test_verify_spec_helpers():
    spec = VerifySpec.all_of(catalog.item_spec("cobblestone", 3), catalog.near_spec("crafting_table"))
    assert spec.op == "all"
    assert sorted(name for name, _ in spec.predicates()) == ["has_item", "near_block"]
    assert spec.items_mentioned() == {"cobblestone", "crafting_table"}
    assert spec.negate().op == "not"
    assert VerifySpec.all_of(catalog.item_spec("x")).op == "has_item"  # single child collapses


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
def test_catalog_validation_and_defaults():
    assert catalog.validate({"has_item": {"name": "stick", "count": 2}}) == []
    assert catalog.validate({"near_block": {"name": "furnace"}}) == []  # radius optional
    assert catalog.normalize({"near_block": {"name": "furnace"}}).spec == {"near_block": {"name": "furnace", "radius": 4}}
    assert "unknown predicate" in catalog.validate({"near_structure": {"name": "village"}})[0]
    assert "unknown param" in catalog.validate({"has_item": {"name": "stick", "qty": 2}})[0]
    assert "missing param" in catalog.validate({"has_equipped": {"name": "iron_helmet"}})[0]
    assert "must be an integer" in catalog.validate({"has_item": {"name": "stick", "count": "2"}})[0]
    assert catalog.validate({"has_equipped": {"name": "iron_helmet", "slot": "hat"}})
    assert catalog.validate({"all": []})
    assert catalog.validate({"all": [{"has_item": {"name": "a"}}, {"bogus": {}}]})
    assert catalog.validate({"not": {"in_biome": {"name": "desert"}}}) == []
    assert catalog.validate({"has_item": {"name": "stick"}, "near_block": {"name": "x"}})  # two keys
    assert catalog.validate({"block_count_in_region": {"name": "oak_planks", "count": 4,
                                                       "region": {"min": {"x": 0, "y": 60, "z": 0}, "max": {"x": 4, "y": 64, "z": 4}}}}) == []
    for name in catalog.PREDICATES:
        assert name in catalog.describe_catalog()
    assert len(catalog.PREDICATES) == 14
    assert "has_item" in catalog.describe_catalog(["has"]) and "in_biome" not in catalog.describe_catalog(["has"])


def test_catalog_local_evaluation():
    facts = {
        "inventory": {"stone_pickaxe": 1, "cobblestone": 5},
        "slots": {"hand": "stone_pickaxe", "head": None},
        "nearby_blocks": {"crafting_table": 3, "furnace": 9},
        "placed_by_me": {"crafting_table": {"x": 1, "y": 64, "z": 1}},
        "nearby_entities": {"cow": 6},
        "biome": "plains", "position": {"x": 10, "y": 64, "z": -3},
        "status": {"health": 20, "food": 12}, "time_of_day": 1000, "sheltered": False,
        "containers": {"1,64,2": {"oak_log": 3}}, "region_counts": {"oak_planks": 9},
    }
    ev = lambda spec: catalog.evaluate(spec, facts)
    assert ev({"has_item": {"name": "cobblestone", "count": 5}}) and not ev({"has_item": {"name": "cobblestone", "count": 6}})
    assert ev({"has_equipped": {"name": "stone_pickaxe", "slot": "hand"}})
    assert ev({"has_tool_tier": {"tool": "pickaxe", "material": "wooden"}})  # stone is better than wooden
    assert ev({"has_tool_tier": {"tool": "pickaxe", "material": "stone"}})
    assert not ev({"has_tool_tier": {"tool": "pickaxe", "material": "iron"}})
    assert not ev({"has_tool_tier": {"tool": "axe", "material": "wooden"}})
    assert ev({"near_block": {"name": "crafting_table"}}) and not ev({"near_block": {"name": "furnace"}})
    assert ev({"near_block": {"name": "furnace", "radius": 10}})
    assert ev({"block_placed_by_me": {"name": "crafting_table"}}) and not ev({"block_placed_by_me": {"name": "furnace"}})
    assert ev({"near_entity": {"name": "cow"}}) and not ev({"near_entity": {"name": "pig"}})
    assert ev({"in_biome": {"name": "plains"}}) and ev({"at_y_level": {"min": 60, "max": 70}})
    assert ev({"position_within": {"x": 12, "z": -3, "radius": 3}}) and not ev({"position_within": {"x": 20, "z": 0}})
    assert ev({"status_at_least": {"field": "food", "min": 10}}) and not ev({"status_at_least": {"field": "food", "min": 13}})
    assert ev({"time_of_day_in": {"min": 0, "max": 12000}}) and not ev({"is_sheltered": {}})
    assert ev({"container_contains": {"pos": {"x": 1, "y": 64, "z": 2}, "name": "oak_log", "count": 3}})
    assert ev({"block_count_in_region": {"name": "oak_planks", "count": 9, "region": {"min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 1, "z": 1}}}})
    assert ev({"all": [{"in_biome": {"name": "plains"}}, {"not": {"is_sheltered": {}}}]})
    assert ev({"any": [{"in_biome": {"name": "desert"}}, {"in_biome": {"name": "plains"}}]})
    assert catalog.item_progress({"has_item": {"name": "cobblestone", "count": 8}}, facts["inventory"]) == {"have": 5, "need": 8}
    assert catalog.item_progress({"in_biome": {"name": "plains"}}, {}) is None


# --------------------------------------------------------------------------- #
# decomposer
# --------------------------------------------------------------------------- #
def test_wooden_pickaxe_tree():
    tree = Decomposer(KNOWLEDGE).build("obtain a wooden pickaxe", target={"item": "wooden_pickaxe", "count": 1})
    root = tree.get(tree.root)
    assert root.kind == "craft" and root.item == "wooden_pickaxe"
    assert root.requires == {"*_planks": 3, "stick": 2}
    assert root.verify == {"has_item": {"name": "wooden_pickaxe", "count": 1}}
    # 3x3 recipe: the crafting table precondition is the first child, placed once
    table = _one(tree, "crafting_table", "place")
    assert root.children[0] == table.id
    assert table.verify == {"near_block": {"name": "crafting_table", "radius": 4}}
    assert table.requires == {"crafting_table": 1}
    craft_table = _one(tree, "crafting_table", "craft")
    assert craft_table.parent == table.id and craft_table.requires == {"*_planks": 4}
    # sticks are crafted from planks, planks from logs; one node per item
    stick = _one(tree, "stick")
    assert stick.kind == "craft" and stick.requires == {"*_planks": 2} and stick.work["batches"] == 1
    # planks: 4 for the table + 3 for the pickaxe + 2 for the sticks, in ONE node under the first consumer
    planks = _one(tree, "*_planks")
    assert planks.parent == craft_table.id and planks.work["count"] == 9
    assert planks.requires == {"*_log": 3} and planks.work["batches"] == 3
    assert planks.verify == {"has_item": {"name": "*_planks", "count": 9}}
    assert stick.children == []  # its planks come from the shared node
    log = _one(tree, "*_log")
    assert log.kind == "mine" and log.parent == planks.id
    # finding the block is its own verifiable step, run before mining
    (find,) = [tree.get(c) for c in log.children]
    assert find.kind == "locate" and find.verify == {"near_block": {"name": "*_log", "radius": 16}}
    assert log.acquire == 3 and log.verify == {"has_item": {"name": "*_log", "count": 3}}
    assert log.description.startswith("obtain 3 logs (any wood)")
    # the shared nodes come before every consumer in traversal order
    order = [n.id for n in tree.post_order()]
    assert order.index(log.id) < order.index(planks.id) < order.index(craft_table.id)
    assert order.index(planks.id) < order.index(stick.id) < order.index(root.id)
    assert len(tree.nodes) == 7
    # post-order: every child before its parent, root last
    for n in tree.nodes.values():
        for c in n.children:
            assert order.index(c) < order.index(n.id)
    assert order[-1] == root.id
    # logs need no tool: no tool node anywhere
    assert not any((n.work or {}).get("role") == "tool" for n in tree.nodes.values())
    # allowed blocks: the log family, plus the crafting table landmark
    assert tree.allowed_blocks(KNOWLEDGE) == ["*_log", "crafting_table"]
    assert all(catalog.validate(n.verify) == [] for n in tree.nodes.values())


def test_stone_pickaxe_tree_has_tool_node_and_aggregated_cobblestone():
    tree = Decomposer(KNOWLEDGE).build("craft a stone pickaxe", target={"item": "stone_pickaxe", "count": 1})
    root = tree.get(tree.root)
    assert root.requires == {"cobblestone": 3, "stick": 2}
    cobble = _one(tree, "cobblestone")
    assert cobble.kind == "mine" and cobble.work["blocks"][0] == "stone" and cobble.acquire == 3
    assert cobble.work["tool"] == "wooden_pickaxe"
    tool = _one(tree, "wooden_pickaxe")
    assert tool.parent == cobble.id and tool.work["role"] == "tool"
    assert tool.verify == {"has_tool_tier": {"tool": "pickaxe", "material": "wooden"}}
    assert tool.description == "obtain a wooden pickaxe or better"
    # the wooden pickaxe subtree shares the single crafting table node and the single log leaf
    _one(tree, "crafting_table", "place")
    # sticks: 2 for the wooden pickaxe + 2 for the stone pickaxe, one batch of 4
    stick = _one(tree, "stick")
    assert stick.work["count"] == 4 and stick.requires == {"*_planks": 2}
    # planks: 4 (table) + 3 (wooden pickaxe) + 2 (sticks) = 9 -> 3 logs
    assert _one(tree, "*_planks").work["count"] == 9
    assert _one(tree, "*_log").acquire == 3
    assert "stone" in tree.allowed_blocks(KNOWLEDGE) and "*_log" in tree.allowed_blocks(KNOWLEDGE)


def test_iron_ingot_smelts_with_furnace_and_fuel():
    tree = Decomposer(KNOWLEDGE).build("get 3 iron ingots", target={"item": "iron_ingot", "count": 3})
    root = tree.get(tree.root)
    assert root.kind == "smelt" and root.work["input"] == "raw_iron" and root.work["fuel"] == "coal"
    assert root.requires == {"raw_iron": 3, "coal": 1}
    furnace_pre = _one(tree, "furnace", "place")
    assert root.children[0] == furnace_pre.id
    furnace = _one(tree, "furnace", "craft")
    assert furnace.requires == {"cobblestone": 8} and furnace.work["shape"] == "3x3"
    cobble = _one(tree, "cobblestone")
    assert cobble.acquire == 11  # 8 for the furnace + 3 for the stone pickaxe that mines iron ore
    raw = _one(tree, "raw_iron")
    assert raw.kind == "mine" and raw.work["blocks"][0] == "iron_ore" and raw.work["tool"] == "stone_pickaxe"
    stone_pick = _one(tree, "stone_pickaxe")
    assert stone_pick.work["role"] == "tool" and stone_pick.parent == raw.id
    coal = _one(tree, "coal")
    assert coal.kind == "mine" and coal.work["blocks"][0] == "coal_ore" and coal.acquire == 1
    # both tool nodes exist exactly once (wooden under cobblestone, stone under raw iron)
    _one(tree, "wooden_pickaxe")
    assert all(catalog.validate(n.verify) == [] for n in tree.nodes.values())


def test_cycle_guard_iron_block():
    tree = Decomposer(KNOWLEDGE).build("x", target={"item": "iron_block", "count": 1})
    root = tree.get(tree.root)
    assert root.kind == "craft" and root.requires == {"iron_ingot": 9}
    ingot = _one(tree, "iron_ingot")
    # ingot must not be crafted back from iron_block or nuggets (both cycle); it is smelted
    assert ingot.kind == "smelt" and ingot.work["input"] == "raw_iron"
    assert _by_item(tree, "iron_nugget") == []
    assert len(_by_item(tree, "iron_block")) == 1


def test_done_flags_from_inventory_skip_subtrees():
    facts = {"inventory": {"stone_pickaxe": 1, "stick": 2}, "nearby_blocks": {"crafting_table": 2}}
    d = Decomposer(KNOWLEDGE, verify_fn=verify_fn_from_facts(facts))
    tree = d.build("x", target={"item": "furnace", "count": 1})
    # the harvest tool is satisfied by a better pickaxe: its whole subtree is done
    tool = _one(tree, "wooden_pickaxe")
    assert tool.done and all(n.done for n in tree.post_order(tool.id))
    assert _one(tree, "crafting_table", "place").done
    cobble = _one(tree, "cobblestone")
    assert not cobble.done and cobble.acquire == 8
    assert not tree.get(tree.root).done
    find_stone = [tree.get(c) for c in cobble.children if tree.get(c).kind == "locate"][0]
    assert tree.next_pending() is find_stone  # find stone, then mine it, then craft the furnace
    assert sum(1 for n in tree.pending()) == 3


def test_redecompose_switches_source_then_inserts_locate():
    d = Decomposer(KNOWLEDGE)
    tree = d.build("x", target={"item": "*_planks", "count": 4})
    root = tree.get(tree.root)
    log = _one(tree, "*_log")
    assert root.work["source"] == "craft:*_log"
    # first failure: a different recipe (oak_wood) replaces the subtree
    assert d.redecompose(tree, root.id)
    assert root.work["source"] != "craft:*_log" and root.attempts == 1
    # the alternative (oak_wood) still bottoms out in logs, but the old subtree is rebuilt
    assert log.id not in tree.nodes
    # a mine leaf is built with its locate child already; with no other source there is nothing left to try
    tree2 = Decomposer(KNOWLEDGE).build("x", target={"item": "cobblestone", "count": 2})
    d2 = Decomposer(KNOWLEDGE)
    cobble = tree2.get(tree2.root)
    locate = [tree2.get(c) for c in cobble.children if tree2.get(c).kind == "locate"][0]
    assert locate.verify == {"near_block": {"name": "stone", "radius": 16}}
    assert not d2.redecompose(tree2, cobble.id)  # nothing more to try
    # a place precondition cannot be re-decomposed
    tree3 = Decomposer(KNOWLEDGE).build("x", target={"item": "wooden_pickaxe", "count": 1})
    assert not Decomposer(KNOWLEDGE).redecompose(tree3, _one(tree3, "crafting_table", "place").id)


def test_non_item_goal_plan_is_validated_and_retried():
    jev = StubJev(nouls={"item_driven": 0.05}, choices={"family": "location"})
    llm = StubLLM([
        {"items": [], "final": {"kind": "locate", "item": None, "verify": {"near_structure": {"name": "village"}}}, "hints": []},
        {"items": [], "final": {"kind": "locate", "item": None, "verify": {"all": [{"in_biome": {"name": "desert"}}, {"status_at_least": {"field": "food", "min": 6}}]}}, "hints": []},
    ])
    tree = Decomposer(KNOWLEDGE, jev=jev, llm=llm).build("walk until you reach a desert")
    assert len(tree.nodes) == 1
    root = tree.get(tree.root)
    assert root.kind == "goal" and root.work["kind"] == "locate"
    assert root.verify == {"all": [{"in_biome": {"name": "desert"}}, {"status_at_least": {"field": "food", "min": 6}}]}
    assert catalog.validate(root.verify) == []
    assert jev.calls == []  # a plan with a final step needs no "is holding enough?" check
    assert len(llm.calls) == 2 and "rejected" in llm.calls[1][-1]["content"] and "near_structure" in llm.calls[1][-1]["content"]


def test_item_goal_plan_and_jev_cross_check():
    jev = StubJev(nouls={"item_driven": 0.9})
    llm = StubLLM([{"items": [{"item": "Wooden Pickaxe", "count": 1}], "final": None, "hints": []}])
    tree = Decomposer(KNOWLEDGE, jev=jev, llm=llm).build("make yourself a wooden pickaxe")
    assert tree.target == {"item": "wooden_pickaxe", "count": 1, "kind": "item"}
    assert tree.get(tree.root).item == "wooden_pickaxe"
    # Jev judges that holding a bed does not finish "sleep": GPT is asked for the final step
    jev2 = StubJev(nouls={"item_driven": 0.1})
    llm2 = StubLLM([
        {"items": [{"item": "red_bed", "count": 1}], "final": None, "hints": []},
        {"items": [{"item": "red_bed", "count": 1}], "final": {"kind": "use", "item": "red_bed", "verify": {"near_block": {"name": "red_bed", "radius": 3}}}, "hints": []},
    ])
    tree2 = Decomposer(KNOWLEDGE, jev=jev2, llm=llm2).build("sleep through the night")
    root2 = tree2.get(tree2.root)
    assert root2.kind == "use" and root2.verify == {"near_block": {"name": "red_bed", "radius": 3}}
    assert root2.children and tree2.get(root2.children[0]).item == "red_bed"  # the bed is still obtained first
    assert "does NOT finish" in llm2.calls[1][-1]["content"]
    # offline guess when there is no LLM at all
    tree3 = Decomposer(KNOWLEDGE).build("obtain 3 oak_log")
    assert tree3.target == {"item": "*_log", "count": 3, "kind": "item"}


def test_compound_goal_keeps_its_item_tree_under_the_final_step():
    llm = StubLLM([
        {"items": [{"item": "magic_helmet", "count": 1}], "final": {"kind": "equip", "item": "iron_helmet", "slot": "head"}, "hints": []},
        {"items": [{"item": "iron_helmet", "count": 1}], "final": {"kind": "equip", "item": "iron_helmet", "slot": "head"},
         "hints": ["iron ore is usually underground, below y=64"]},
    ])
    tree = Decomposer(KNOWLEDGE, jev=StubJev(), llm=llm).build("obtain and equip an iron helmet")
    root = tree.get(tree.root)
    assert root.kind == "equip" and root.verify == {"has_equipped": {"name": "iron_helmet", "slot": "head"}}
    assert root.work["slot"] == "head" and root.work["hints"] == ["iron ore is usually underground, below y=64"]
    helmet = tree.get(root.children[0])
    assert helmet.kind == "craft" and helmet.item == "iron_helmet" and helmet.children
    assert _one(tree, "iron_ingot").kind == "smelt" and _one(tree, "raw_iron").kind == "mine"
    assert tree.target["plan"]["final"]["kind"] == "equip"
    assert "unknown item id 'magic_helmet'" in llm.calls[1][-1]["content"]
    order = [n.id for n in tree.post_order()]
    assert order[-1] == root.id and order.index(helmet.id) < order.index(root.id)


def test_llm_json_parsing():
    parse = llm_mod.parse_json_object
    assert parse('{"a": 1}') == {"a": 1}
    assert parse('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse('Sure: {"item": "stick", "count": 2} done') == {"item": "stick", "count": 2}
    try:
        parse("no json here")
        assert False
    except llm_mod.LLMError:
        pass
    llm = llm_mod.LLM(api_key=None)  # construction never touches the network
    try:
        llm.complete([{"role": "user", "content": "hi"}])
        assert False
    except llm_mod.LLMError as exc:
        assert "OPENAI_API_KEY" in str(exc)


def test_vacuous_final_check_is_rejected_and_final_step_is_not_premarked():
    from voyager.tree.decomposer import verify_fn_from_facts
    llm = StubLLM([
        {"items": [{"item": "cooked_beef", "count": 1}], "final": {"kind": "eat", "item": "cooked_beef",
         "verify": {"not": {"has_item": {"name": "cooked_beef", "count": 1}}}}, "hints": []},
        {"items": [{"item": "cooked_beef", "count": 1}], "final": {"kind": "eat", "item": "cooked_beef",
         "verify": {"status_at_least": {"field": "food", "min": 20}}}, "hints": []},
    ])
    d = Decomposer(KNOWLEDGE, jev=StubJev(), llm=llm, verify_fn=verify_fn_from_facts({"inventory": {}, "status": {"food": 12}}))
    tree = d.build("cook some beef and eat it")
    root = tree.get(tree.root)
    assert root.kind == "eat" and root.verify == {"status_at_least": {"field": "food", "min": 20}}
    assert "ALREADY TRUE now" in llm.calls[1][-1]["content"]
    assert not root.done and not any(n.done for n in tree.nodes.values())
