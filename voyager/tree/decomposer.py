"""Decomposer: a goal becomes a tree of verifiable subtasks (PLAN.md section 4).

Inputs
------
* ``knowledge``: the dict ``GET /knowledge`` returns (recipes keyed by result
  item, harvest tools per block, and the mcKnowledge tables).
* ``jev``: a ``JevClient`` (``.ask(gate, state, questions)``) or any stub with
  that method. Used for two small judgments: is the goal item-driven, and
  which predicate family fits a non-item goal. Optional.
* ``llm``: an ``LLM`` (``.complete_json(messages, schema_hint)``) or stub. Used
  to parse the goal into a target and to fill a VerifySpec for non-item
  goals. Optional when ``build`` is given an explicit target.

Algorithm (4.1): choose the cheapest source for an item (mine, kill, smelt,
craft, collect) with a cycle guard, expand recipe inputs as children with
preconditions first (crafting table, furnace, fuel, harvest tool), stop at
natural blocks and mob drops. Every item gets ONE node, placed under the
first consumer that asks for it and sized for the whole tree's demand (the
build iterates to a demand fixed point). Gather leaves carry that total in
``acquire``; craft and smelt nodes carry it in ``work.count`` and verify
their own output at that count. This goes one step past PLAN.md 4.1 step 5
(which merged only leaves): with separate plank nodes per consumer, a later
plank node is satisfied by planks an earlier consumer has not yet used up,
and the tree finishes one craft short.
"""

from __future__ import annotations

import math
import json
import re
from typing import Any, Callable

from . import catalog
from .schema import Node, Tree, VerifySpec, goal_id_for

try:
    from typesafe_sdk import Choice, Noul
except Exception:  # SDK optional; stubs in tests return canned answers
    Choice = Noul = None  # type: ignore[assignment]

INF = float("inf")
GATHER_KINDS = ("mine", "kill", "collect")
PRECONDITION_RADIUS = 4
LOCATE_RADIUS = 16

# Base costs for the recipe choice (section 4.1 step 2)
COST_NATURAL = 1.0
COST_TOOL_NEEDED = 2.0
COST_MOB = 3.0
COST_SMELT = 4.0
COST_COLLECT = 6.0  # unknown source: last resort


class DecomposeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# knowledge
# --------------------------------------------------------------------------- #
class Knowledge:
    """Typed reads over the ``GET /knowledge`` payload."""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.recipes_by_item: dict[str, list[dict[str, Any]]] = data.get("recipes") or {}
        self.harvest: dict[str, list[str]] = data.get("harvest_tools") or {}
        self.ore_drops: dict[str, str] = data.get("ore_drops") or {}
        self.smeltable: dict[str, str] = data.get("smeltable") or {}
        self.fuels: list[str] = list(data.get("fuels") or [])
        self.mob_drops: dict[str, str] = data.get("mob_drops") or {}
        self.families: dict[str, list[str]] = data.get("families") or {}
        self.natural = re.compile(data.get("natural_block_re") or r"$^")
        self._drops_inverse: dict[str, list[str]] = {}
        for block, drop in self.ore_drops.items():
            self._drops_inverse.setdefault(drop, []).append(block)
        # Wood is interchangeable: any log makes planks, any planks make sticks,
        # tables and tools. Members of these families are replaced by the
        # wildcard everywhere so the tree never binds to the oak the recipe
        # table happens to list first while the bot stands in a birch forest.
        self._canon: dict[str, str] = {}
        for fam, wild in WILDCARD_FAMILIES.items():
            for member in self.families.get(fam) or []:
                self._canon[member] = wild

    def canon(self, item: str) -> str:
        return self._canon.get(item, item)

    def representative(self, item: str) -> str:
        """A concrete member for a wildcard ('*_log' -> 'oak_log'); other names unchanged."""
        for fam, wild in WILDCARD_FAMILIES.items():
            if item == wild:
                members = self.families.get(fam) or []
                return members[0] if members else item
        return item

    def canon_inputs(self, inputs: dict[str, Any]) -> dict[str, int]:
        out: dict[str, int] = {}
        for name, n in (inputs or {}).items():
            key = self.canon(name)
            out[key] = out.get(key, 0) + int(n)
        return out

    def recipes(self, item: str) -> list[dict[str, Any]]:
        """Recipes for ``item`` with wildcard inputs; a wildcard item uses its representative's
        recipes, and variants that collapse to the same inputs are listed once."""
        raw = self.recipes_by_item.get(self.representative(item)) or []
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for recipe in raw:
            inputs = self.canon_inputs(recipe.get("inputs") or {})
            key = json.dumps(inputs, sort_keys=True) + str(recipe.get("count")) + str(recipe.get("shape"))
            if key in seen:
                continue
            seen.add(key)
            out.append({**recipe, "inputs": inputs})
        return out

    def mine_blocks(self, item: str) -> list[str]:
        """Blocks to mine for ``item``: the ones that drop it, else the item itself when natural.
        A wildcard item ('*_log') is mined as that wildcard; the bot matches any member."""
        if item in WILDCARD_FAMILIES.values():
            rep = self.representative(item)
            return [item] if self.natural.search(rep) or self._drops_inverse.get(rep) else []
        blocks = list(self._drops_inverse.get(item, []))
        if blocks:
            return blocks
        # stripped logs/wood match the natural-block pattern but never generate
        # in the world; they come from using an axe on a log
        if self.natural.search(item) and not item.startswith("stripped_"):
            return [item]
        return []

    def harvest_tools(self, block: str) -> list[str]:
        return list(self.harvest.get(self.representative(block)) or [])

    def lowest_harvest_tool(self, blocks: list[str]) -> str | None:
        """The weakest tool that mines all the given blocks, or None when hands do."""
        best: str | None = None
        best_tier = -1
        for block in blocks:
            tools = self.harvest_tools(block)
            if not tools:
                continue
            tool = tools[0]
            tier = _tier_of(tool)
            if tier > best_tier:
                best, best_tier = tool, tier
        return best

    def smelt_inputs(self, item: str) -> list[str]:
        out: list[str] = []
        for src, result in self.smeltable.items():
            if result == self.representative(item):
                c = self.canon(src)
                if c not in out:
                    out.append(c)
        return out

    def mob_for(self, item: str) -> str | None:
        return self.mob_drops.get(self.representative(item))

    def family_of(self, item: str) -> str | None:
        for fam, members in self.families.items():
            if item in members:
                return fam
        return None


FINAL_KINDS = {"equip", "place", "eat", "use", "locate", "status", "build", "other"}
FINAL_WORK_KINDS = {"equip", "place", "eat", "use"}  # final steps the builder writes a work action for
PLAN_SCHEMA_HINT = (
    '{"items": [{"item": "snake_case_id", "count": 1}], "final": null or {"kind": "...", "item": "id or null", '
    '"slot": "slot or null", "verify": {...}}, "hints": ["..."]}'
)

WILDCARD_FAMILIES = {"log": "*_log", "planks": "*_planks"}
WILDCARD_LABELS = {"*_log": "logs (any wood)", "*_planks": "planks (any wood)"}


def label(item: str) -> str:
    """Human wording for descriptions: wildcards read as 'logs (any wood)'."""
    return WILDCARD_LABELS.get(item, item)


def _tier_of(tool_item: str) -> int:
    material = tool_item.split("_", 1)[0]
    material = catalog.TIER_ALIASES.get(material, material)
    return catalog.TOOL_TIERS.index(material) if material in catalog.TOOL_TIERS else 0


def _tool_parts(tool_item: str) -> tuple[str, str]:
    """'wooden_pickaxe' -> ('pickaxe', 'wooden')."""
    material, _, tool = tool_item.partition("_")
    return tool, catalog.TIER_ALIASES.get(material, material)


# --------------------------------------------------------------------------- #
# decomposer
# --------------------------------------------------------------------------- #
class Decomposer:
    def __init__(
        self,
        knowledge: dict[str, Any] | Knowledge,
        jev: Any = None,
        llm: Any = None,
        *,
        budget_actions: int = 60,
        verify_fn: Callable[[dict[str, Any]], bool] | None = None,
        fuel_preference: list[str] | None = None,
        item_driven_threshold: float = 0.35,
    ):
        self.k = knowledge if isinstance(knowledge, Knowledge) else Knowledge(knowledge)
        self.jev = jev
        self.llm = llm
        self.budget_actions = budget_actions
        # verify_fn(spec_dict) -> bool: checks a spec against the live world (bridge.agent_verify)
        self.verify_fn = verify_fn
        self.fuel_preference = fuel_preference or ["coal", "charcoal", "oak_planks", "birch_planks", "spruce_planks"]
        self.item_driven_threshold = item_driven_threshold
        # item -> set of source keys that failed and must not be chosen again (redecompose)
        self.exclude: dict[str, set[str]] = {}
        self._cost_memo: dict[tuple[str, tuple[str, ...]], float] = {}
        # per-build dedupe of precondition and tool nodes
        self._preconditions: dict[str, str] = {}
        self._tool_nodes: dict[str, str] = {}
        self._built: dict[str, str] = {}  # item -> node id already in the tree (one node per item)
        self._totals: dict[str, int] = {}  # item -> whole-tree demand from the previous pass
        self._demand: dict[str, int] = {}  # item -> demand summed during this pass

    # ---- public ---------------------------------------------------------- #
    def build(self, goal_text: str, target: dict[str, Any] | None = None) -> Tree:
        """Decompose ``goal_text`` into a tree.

        The goal is read into a PLAN (see ``parse_plan``): items to obtain, an optional
        final step with its completion check, and hints. Items are expanded by the
        recipe code; a final step becomes the root with the item trees beneath it.
        ``target`` skips the GPT reading: an old-style {"item", "count"} target or a
        plan dict.
        """
        plan = self._plan_from_target(target) if target else self.parse_plan(goal_text)
        items = plan["items"]
        final = plan.get("final")
        tree_target = self._tree_target(plan)

        if final is None and len(items) == 1:
            # holding one item finishes the goal: that item's node is the root
            tree = self._expand_fixed_point(goal_text, tree_target, items, root_node=None)
            root = tree.get(tree.root)
            root.description = f"{root.description} (goal: {goal_text})"
        elif final is None:
            # several items, held together: a goal node checking all of them
            spec = {"all": [catalog.item_spec(i["item"], i["count"]).to_json() for i in items]}
            root = Node(id="", description=goal_text, kind="goal", verify=spec,
                        work={"kind": "obtain", "items": [i["item"] for i in items]},
                        budget_actions=self.budget_actions)
            tree = self._expand_fixed_point(goal_text, tree_target, items, root_node=root)
        else:
            kind = final["kind"]
            work = {"kind": kind, **{k: v for k, v in final.items() if k not in ("kind", "verify") and v is not None}}
            if plan.get("hints"):
                work["hints"] = plan["hints"]
            root = Node(
                id="",
                description=goal_text,
                # equip/place/eat/use are work steps the builder knows how to build for;
                # anything else (reach a place, survive the night) is a plain goal node
                kind=kind if kind in FINAL_WORK_KINDS else "goal",
                verify=final["verify"],
                work=work,
                budget_actions=self.budget_actions * (1 if items else 2),
            )
            tree = self._expand_fixed_point(goal_text, tree_target, items, root_node=root)
        self.aggregate(tree)
        self.mark_satisfied(tree)
        return tree

    def _expand_fixed_point(self, goal_text: str, target: dict[str, Any], items: list[dict[str, Any]], root_node: Node | None) -> Tree:
        """One node per item, sized for the whole tree: build, total the demand per item,
        rebuild with those totals, until the totals stop changing. ``root_node`` (a goal
        or final-step node) is re-created each pass with the items beneath it."""
        totals: dict[str, int] = {}
        tree = Tree(goal_id=goal_id_for(goal_text), goal_text=goal_text, target=target)
        for _ in range(8):
            tree = Tree(goal_id=tree.goal_id, goal_text=goal_text, target=target)
            self._reset_dedupe()
            self._totals = totals
            self._demand = {}
            parent = None
            if root_node is not None:
                fresh = Node.from_json({**root_node.to_json(), "id": tree.new_id(), "children": [], "parent": None})
                tree.add(fresh)
                parent = fresh.id
            for it in items:
                self._expand(tree, it["item"], int(it["count"]), parent=parent, path=())
            if self._demand == totals:
                break
            totals = dict(self._demand)
        return tree

    @staticmethod
    def _tree_target(plan: dict[str, Any]) -> dict[str, Any]:
        """What the tree records as its target. A single held item keeps the old
        {"item", "count", "kind": "item"} form; anything else records the plan."""
        items, final = plan["items"], plan.get("final")
        if final is None and len(items) == 1:
            return {"item": items[0]["item"], "count": items[0]["count"], "kind": "item"}
        return {"item": None, "kind": (final or {}).get("kind", "obtain"), "plan": plan}

    def _plan_from_target(self, target: dict[str, Any]) -> dict[str, Any]:
        if "items" in target:
            return target
        if target.get("item"):
            return {"items": [{"item": self.k.canon(str(target["item"])), "count": max(1, int(target.get("count") or 1))}],
                    "final": None, "hints": []}
        raise DecomposeError(f"target has no item and no plan: {target}")

    def redecompose(self, tree: Tree, node_id: str) -> bool:
        """Rebuild one node with a different source, or insert a locate child (section 8).

        Returns True when the tree changed. Keeps the node's id and parent.
        """
        node = tree.get(node_id)
        item = node.item
        source_key = (node.work or {}).get("source")
        changed = False
        if item and source_key:
            self.exclude.setdefault(item, set()).add(source_key)
            self._cost_memo.clear()
            path = tuple(n.item for n in tree.path_to_root(node_id)[1:] if n.item)
            alt = self._choose_source(item, path)
            if alt is not None:
                for child in list(node.children):
                    tree.remove(child)
                self._reset_dedupe(tree, skip={node.id})
                self._totals = {}
                self._demand = {}
                self._fill_node(tree, node, item, node.count, alt, path)
                changed = True
        if not changed and (node.work or {}).get("kind") == "mine":
            blocks = (node.work or {}).get("blocks") or []
            block = blocks[0] if blocks else item
            if not any(tree.get(c).kind == "locate" for c in node.children):
                locate = Node(
                    id=tree.new_id(),
                    description=f"find {block} nearby",
                    kind="locate",
                    verify=catalog.near_spec(block, LOCATE_RADIUS).to_json(),
                    work={"kind": "locate", "block": block},
                    budget_actions=self.budget_actions,
                )
                tree.add(locate, parent=node.id)
                # a locate child must run before the mine step: put it first
                node.children.remove(locate.id)
                node.children.insert(0, locate.id)
                changed = True
        if changed:
            node.attempts += 1
            node.actions_spent = 0
            node.done = False
            node.failed = False
            self.aggregate(tree)
            self.mark_satisfied(tree)
        return changed

    def parse_target(self, goal_text: str) -> dict[str, Any]:
        """Old-style reading kept for callers: the tree target of the plan."""
        return self._tree_target(self.parse_plan(goal_text))

    def parse_plan(self, goal_text: str) -> dict[str, Any]:
        """Read a goal into a validated PLAN with a fixed shape:

            {"items": [{"item": id, "count": n}, ...],     # expanded by recipe code
             "final": null | {"kind": equip|place|eat|use|locate|status|build|other,
                              "item": id?, "slot": slot?, "verify": VerifySpec},
             "hints": ["short facts recipes cannot express", ...]}

        GPT writes it; code checks every item name against the game knowledge and the
        final check against the predicate catalog, and sends any problem back for a
        corrected plan. Jev cross-checks one judgment: whether holding the items alone
        finishes the goal.
        """
        if self.llm is None:
            guess = self._guess_item(goal_text)
            if guess:
                return {"items": [{"item": self.k.canon(guess["item"]), "count": guess["count"]}], "final": None, "hints": []}
            raise DecomposeError("no LLM to read the goal")
        messages = [
            {"role": "system", "content": "You turn a Minecraft goal for a bot into a small structured plan. Use exact Minecraft ids in snake_case (wooden_pickaxe, iron_helmet, cooked_beef); '*_log' and '*_planks' mean any wood."},
            {"role": "user", "content": self._plan_prompt(goal_text)},
        ]
        problems: list[str] = []
        checked_by_jev = False
        for _ in range(3):
            if problems:
                messages.append({"role": "user", "content": "That plan was rejected: " + "; ".join(problems) + ". Answer with the corrected plan JSON only."})
            reply = self.llm.complete_json(list(messages), schema_hint=PLAN_SCHEMA_HINT)
            messages.append({"role": "assistant", "content": json.dumps(reply)})
            plan, problems = self._validate_plan(reply)
            if problems:
                continue
            if plan["final"] is None and plan["items"] and not checked_by_jev:
                # GPT says holding the items is enough; Jev double-checks that one judgment
                checked_by_jev = True
                held = self._jev_item_driven(goal_text, plan)
                if held is not None and held < self.item_driven_threshold:
                    problems = ["a checker judged that holding the items does NOT finish this goal; add the final step "
                                "(equip, place, eat, reach, ...) with its verify condition"]
                    continue
            if plan["final"] is not None and plan["final"].get("verify") is None:
                plan["final"]["verify"] = self.nonitem_spec(goal_text, {**plan["final"], "summary": goal_text}).to_json()
            return plan
        raise DecomposeError(f"could not read '{goal_text}' into a valid plan: {'; '.join(problems)}")

    def _plan_prompt(self, goal_text: str) -> str:
        return (
            f"Goal: {goal_text}\n\n"
            "Return JSON with exactly these keys:\n"
            '- "items": every item the bot must HAVE for this goal, as [{"item": id, "count": n}]. Only list what the '
            "goal itself names or directly needs; do not list recipe ingredients (those are worked out separately). "
            "Empty for goals that need no item (reach a biome, survive the night).\n"
            '- "final": null when holding those items finishes the goal. Otherwise the last step after obtaining them: '
            '{"kind": one of equip|place|eat|use|locate|status|build|other, "item": the item it acts on or null, '
            '"slot": hand|offhand|head|chest|legs|feet when equipping, "verify": the completion condition as a verify '
            "spec using ONLY these predicates (one key per object; combine with all/any/not):\n"
            f"{catalog.describe_catalog()}\n"
            '- "hints": up to 3 short facts a recipe table cannot express that will matter (e.g. "iron ore is usually '
            'found underground, below y=64"). Empty list if none.\n\n'
            'Example: "get an iron helmet and put it on" -> {"items": [{"item": "iron_helmet", "count": 1}], '
            '"final": {"kind": "equip", "item": "iron_helmet", "slot": "head", "verify": {"has_equipped": '
            '{"name": "iron_helmet", "slot": "head"}}}, "hints": ["iron ore is usually underground"]}'
        )

    def _validate_plan(self, reply: Any) -> tuple[dict[str, Any], list[str]]:
        """Check a GPT plan; returns (normalized plan, problems)."""
        problems: list[str] = []
        if not isinstance(reply, dict):
            return {}, ["the answer must be one JSON object"]
        items: list[dict[str, Any]] = []
        for raw in reply.get("items") or []:
            if not isinstance(raw, dict) or not raw.get("item"):
                problems.append(f"bad item entry {raw!r}: use {{'item': id, 'count': n}}")
                continue
            name = re.sub(r"[^a-z0-9_*]", "_", str(raw["item"]).strip().lower()).strip("_")
            name = self.k.canon(name)
            try:
                count = max(1, int(raw.get("count") or 1))
            except (TypeError, ValueError):
                count = 1
            if not (self._known(name) or name in WILDCARD_FAMILIES.values()):
                problems.append(f"unknown item id {raw['item']!r}: use the exact Minecraft id")
                continue
            items.append({"item": name, "count": count})
        final = reply.get("final")
        if final is not None:
            if not isinstance(final, dict):
                problems.append("final must be null or an object")
                final = None
            else:
                kind = str(final.get("kind") or "other").lower()
                if kind not in FINAL_KINDS:
                    problems.append(f"final.kind {kind!r} must be one of {sorted(FINAL_KINDS)}")
                item = final.get("item")
                if item:
                    item = self.k.canon(re.sub(r"[^a-z0-9_*]", "_", str(item).strip().lower()).strip("_"))
                slot = final.get("slot")
                verify = final.get("verify")
                if verify is None and kind == "equip" and item:
                    verify = {"has_equipped": {"name": item, "slot": slot or "hand"}}
                if verify is not None:
                    bad = catalog.validate(verify)
                    if bad:
                        problems.append(f"final.verify is invalid: {'; '.join(bad)}")
                    else:
                        verify = catalog.normalize(verify).to_json()
                if kind == "equip" and item and not any(i["item"] == item for i in items):
                    items.append({"item": item, "count": 1})  # you cannot equip what you never obtained
                final = {"kind": kind, "item": item or None, "slot": slot or None, "verify": verify}
        if not items and final is None:
            problems.append("the plan needs at least one item or a final step")
        if final is not None and items and final.get("verify") and self.verify_fn is not None and not problems:
            # a final check that is already true before the items exist cannot show the
            # step happened (and would mark the whole tree done at once)
            try:
                holds_now = bool(self.verify_fn(final["verify"]))
            except Exception:
                holds_now = False
            if holds_now:
                problems.append(
                    f"final.verify {json.dumps(final['verify'])} is ALREADY TRUE now, before the items are obtained, so it "
                    "cannot show the final step happened. Use a condition that only becomes true after the step: "
                    "status_at_least on food for eating, has_equipped for wearing, block_placed_by_me for placing"
                )
        hints = [str(h)[:160] for h in (reply.get("hints") or []) if isinstance(h, (str, int, float))][:3]
        return {"items": items, "final": final, "hints": hints}, problems

    def _jev_item_driven(self, goal_text: str, parsed: dict[str, Any]) -> float | None:
        if self.jev is None or Noul is None or not getattr(self.jev, "enabled", True):
            return None
        response = self.jev.ask(
            "goal_kind",
            {"goal": goal_text, "parsed": parsed},
            {
                "item_driven": Noul(
                    instructions=(
                        "`goal` is a Minecraft goal for a bot and `parsed` is a plan for it. Is the goal "
                        "finished exactly when the bot HOLDS the items in `parsed.items` (obtain, collect, craft, "
                        "mine, cook, smelt X)? Answer no when something must still be done with them or "
                        "elsewhere: equipping or wearing, placing, eating, reaching a place or biome, surviving, "
                        "building, or anything the inventory alone cannot prove."
                    )
                )
            },
            extra_record={"goal": goal_text},
        )
        if response is None:
            return None
        try:
            return float(response.nouls["item_driven"].noul)
        except Exception:
            return None

    def nonitem_spec(self, goal_text: str, target: dict[str, Any]) -> VerifySpec:
        """Step 6 for non-item goals: Jev picks the predicate family, GPT fills params."""
        if self.llm is None:
            raise DecomposeError("no LLM to build a verify spec for a non-item goal")
        family = self._jev_family(goal_text, target)
        families = [family] if family else None
        errors: list[str] = []
        for attempt in range(3):
            catalog_text = catalog.describe_catalog(families if attempt < 2 else None)
            content = (
                f"Goal: {goal_text}\nParsed target: {target}\n\n"
                "Write the completion condition for this goal as a JSON verify spec using ONLY these "
                "predicates (one object with exactly one key: a predicate name whose value is its params, "
                "or all/any/not):\n"
                f"{catalog_text}\n"
                "Example: {\"all\": [{\"in_biome\": {\"name\": \"desert\"}}, {\"status_at_least\": {\"field\": \"food\", \"min\": 10}}]}"
            )
            if errors:
                content += f"\n\nThe previous spec was rejected: {'; '.join(errors)}. Fix it."
            reply = self.llm.complete_json(
                [{"role": "system", "content": "You write machine-checkable completion conditions for Minecraft bot tasks."},
                 {"role": "user", "content": content}],
                schema_hint="a verify spec object",
            )
            spec = reply.get("spec") if isinstance(reply.get("spec"), dict) else reply
            errors = catalog.validate(spec)
            if not errors:
                return catalog.normalize(spec)
        raise DecomposeError(f"could not build a valid verify spec for '{goal_text}': {'; '.join(errors)}")

    def _jev_family(self, goal_text: str, target: dict[str, Any]) -> str | None:
        if self.jev is None or Choice is None or not getattr(self.jev, "enabled", True):
            return None
        response = self.jev.ask(
            "verify_family",
            {"goal": goal_text, "parsed": target},
            {
                "family": Choice(
                    instructions=(
                        "`goal` is a Minecraft bot goal that is not about holding an item. Which kind of "
                        "check proves it is finished?"
                    ),
                    criteria={
                        "has": "something held, equipped or a tool tier reached",
                        "near": "a block, placed block, container contents or creature near the bot",
                        "status": "the bot's health, food, xp, shelter or the time of day",
                        "location": "where the bot stands: a biome, a depth, or a position",
                    },
                )
            },
            extra_record={"goal": goal_text},
        )
        if response is None:
            return None
        try:
            answer = response.choices["family"]
            return answer.choice if answer.confidence is None or answer.confidence >= 0.2 else None
        except Exception:
            return None

    @staticmethod
    def _guess_item(goal_text: str) -> dict[str, Any] | None:
        """Offline fallback: 'obtain 3 oak_log' / 'craft a stone pickaxe'."""
        m = re.search(r"(?:obtain|get|collect|craft|mine|make|gather)\s+(?:an?\s+|(\d+)\s+)?([a-z][a-z_ ]+?)\s*$", goal_text.lower())
        if not m:
            return None
        count = int(m.group(1)) if m.group(1) else 1
        item = re.sub(r"\s+", "_", m.group(2).strip())
        item = re.sub(r"s$", "", item) if count == 1 and item.endswith("s") and not item.endswith("ss") else item
        return {"item": item, "count": count, "kind": "item"}

    # ---- expansion ------------------------------------------------------- #
    def _reset_dedupe(self, tree: Tree | None = None, skip: set[str] | None = None) -> None:
        self._preconditions = {}
        self._tool_nodes = {}
        self._built = {}
        if tree is not None:
            for node in tree.nodes.values():
                if skip and node.id in skip:
                    continue
                if node.kind == "place" and node.item:
                    self._preconditions[node.item] = node.id
                    continue
                if (node.work or {}).get("role") == "tool" and node.item:
                    self._tool_nodes[node.item] = node.id
                if node.item and node.kind != "goal":
                    self._built.setdefault(node.item, node.id)

    def _expand(self, tree: Tree, item: str, count: int, parent: str | None, path: tuple[str, ...]) -> Node | None:
        """Add the node for ``item`` under ``parent``, or return the existing one.

        Demand is summed per item across the pass; the node itself is sized by
        the previous pass total so one node covers every consumer (section
        4.1 step 5) and sits under the first consumer that asked for it.
        """
        item = self.k.canon(item)
        self._demand[item] = self._demand.get(item, 0) + count
        if item in self._built:
            return tree.nodes.get(self._built[item])
        source = self._choose_source(item, path)
        if source is None:
            source = {"kind": "collect", "cost": COST_COLLECT, "key": "collect"}
        node = tree.add(
            Node(id=tree.new_id(), description="", kind=source["kind"], verify={}, budget_actions=self.budget_actions),
            parent=parent,
        )
        self._built[item] = node.id
        self._fill_node(tree, node, item, max(count, self._totals.get(item, 0)), source, path)
        return node

    def _fill_node(self, tree: Tree, node: Node, item: str, count: int, source: dict[str, Any], path: tuple[str, ...]) -> None:
        kind = source["kind"]
        node.kind = kind
        node.verify = catalog.item_spec(item, count).to_json()
        node.work = {"kind": kind, "item": item, "count": count, "source": source["key"]}
        node.requires = {}
        path = path + (item,)
        if kind == "craft":
            recipe = source["recipe"]
            batches = max(1, math.ceil(count / max(1, int(recipe.get("count") or 1))))
            node.requires = {name: int(n) * batches for name, n in recipe["inputs"].items()}
            node.work["batches"] = batches
            node.work["shape"] = recipe.get("shape", "2x2")
            node.description = f"craft {count} {label(item)}"
            if recipe.get("shape") == "3x3":
                self._precondition(tree, node, "crafting_table", path)
            for name, n in node.requires.items():
                self._expand(tree, name, n, parent=node.id, path=path)
        elif kind == "smelt":
            src = source["input"]
            node.requires = {src: count}
            node.work["input"] = src
            node.description = f"smelt {count} {label(item)} from {label(src)}"
            self._precondition(tree, node, "furnace", path)
            self._expand(tree, src, count, parent=node.id, path=path)
            fuel = self._pick_fuel(path)
            if fuel:
                fuel_count = max(1, math.ceil(count / 8))
                node.requires[fuel] = fuel_count
                node.work["fuel"] = fuel
                self._expand(tree, fuel, fuel_count, parent=node.id, path=path)
        elif kind == "mine":
            blocks = source["blocks"]
            node.work["blocks"] = blocks
            node.acquire = count
            node.description = f"obtain {count} {label(item)} by mining {label(blocks[0])}"
            tool = self.k.lowest_harvest_tool(blocks)
            if tool:
                node.work["tool"] = tool
                self._tool_node(tree, node, tool, path)
            # Finding the block is its own verifiable step. When one is already in
            # view it is satisfied at once and costs nothing; when none is, it gives
            # the agent a clear subtask (and the builder a reason to learn to explore)
            # instead of a mining step that no action can ever apply to.
            self._locate_child(tree, node, blocks[0])
        elif kind == "kill":
            node.work["mob"] = source["mob"]
            node.acquire = count
            node.description = f"obtain {count} {label(item)} from {source['mob']}"
        else:
            node.acquire = count
            node.description = f"collect {count} {label(item)}"

    def _locate_child(self, tree: Tree, miner: Node, block: str) -> Node:
        """A 'find <block> nearby' child that runs before the mining work step (after any tool)."""
        for c in miner.children:
            if tree.get(c).kind == "locate":
                return tree.get(c)
        locate = Node(
            id=tree.new_id(),
            description=f"find {label(block)} nearby",
            kind="locate",
            verify=catalog.near_spec(block, LOCATE_RADIUS).to_json(),
            work={"kind": "locate", "block": block},
            budget_actions=self.budget_actions,
        )
        tree.add(locate, parent=miner.id)
        return locate

    def _precondition(self, tree: Tree, consumer: Node, block: str, path: tuple[str, ...]) -> None:
        """One place node per tree for a crafting table or furnace, first child of its first consumer."""
        if block in self._preconditions or block in path:
            return
        pre = tree.add(
            Node(
                id=tree.new_id(),
                description=f"place a {block} nearby",
                kind="place",
                verify=catalog.near_spec(block, PRECONDITION_RADIUS).to_json(),
                requires={block: 1},
                work={"kind": "place", "item": block, "count": 1},
                budget_actions=self.budget_actions,
            ),
            parent=consumer.id,
        )
        self._preconditions[block] = pre.id
        self._expand(tree, block, 1, parent=pre.id, path=path)

    def _tool_node(self, tree: Tree, miner: Node, tool_item: str, path: tuple[str, ...]) -> None:
        """One node per tool item; verified by tier so a better tool also satisfies it."""
        if tool_item in self._tool_nodes or tool_item in path:
            return
        tool, material = _tool_parts(tool_item)
        if tool_item in self._built:
            return
        node = self._expand(tree, tool_item, 1, parent=miner.id, path=path)
        if node is None:
            return
        node.verify = catalog.tool_tier_spec(tool, material).to_json()
        node.description = f"obtain a {material} {tool} or better"
        node.work["role"] = "tool"
        self._tool_nodes[tool_item] = node.id

    def _pick_fuel(self, path: tuple[str, ...]) -> str | None:
        for fuel in self.fuel_preference + self.k.fuels:
            if fuel in path:
                continue
            if self._cost(fuel, path) < INF:
                return fuel
        return None

    # ---- sources and cost ------------------------------------------------- #
    def _sources(self, item: str, path: tuple[str, ...]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        excluded = self.exclude.get(item, set())
        blocks = self.k.mine_blocks(item)
        if blocks:
            key = f"mine:{blocks[0]}"
            if key not in excluded:
                tool = self.k.lowest_harvest_tool(blocks)
                cost = COST_NATURAL + (COST_TOOL_NEEDED if tool else 0)
                out.append({"kind": "mine", "blocks": blocks, "cost": cost, "key": key})
        mob = self.k.mob_for(item)
        if mob and f"kill:{mob}" not in excluded:
            out.append({"kind": "kill", "mob": mob, "cost": COST_MOB, "key": f"kill:{mob}"})
        for src in self.k.smelt_inputs(item):
            key = f"smelt:{src}"
            if key in excluded or src in path:
                continue
            c = self._cost(src, path + (item,))
            if c < INF:
                out.append({"kind": "smelt", "input": src, "cost": COST_SMELT + c, "key": key})
        for recipe in self.k.recipes(item):
            inputs = recipe.get("inputs") or {}
            key = "craft:" + "+".join(sorted(inputs))
            if key in excluded or not inputs:
                continue
            total = 0.0
            for name, n in inputs.items():
                c = self._cost(name, path + (item,))
                if c == INF:
                    total = INF
                    break
                total += c * int(n)
            if total < INF:
                out.append({"kind": "craft", "recipe": recipe, "cost": total / max(1, int(recipe.get("count") or 1)), "key": key})
        out.sort(key=lambda s: s["cost"])
        return out

    def _choose_source(self, item: str, path: tuple[str, ...]) -> dict[str, Any] | None:
        if item in path:
            return None
        sources = self._sources(item, path)
        return sources[0] if sources else None

    def _cost(self, item: str, path: tuple[str, ...]) -> float:
        if item in path:
            return INF
        memo_key = (item, path)
        if memo_key in self._cost_memo:
            return self._cost_memo[memo_key]
        self._cost_memo[memo_key] = INF  # guard re-entry
        sources = self._sources(item, path)
        if sources:
            cost = sources[0]["cost"]
        elif self._known(item):
            cost = INF  # every known source is blocked on this path (a cycle) or excluded
        else:
            cost = COST_COLLECT  # nothing known: a collectable like a sapling or apple
        self._cost_memo[memo_key] = cost
        return cost

    def _known(self, item: str) -> bool:
        return bool(self.k.recipes(item) or self.k.mine_blocks(item) or self.k.mob_for(item) or self.k.smelt_inputs(item))

    # ---- post-processing -------------------------------------------------- #
    def aggregate(self, tree: Tree) -> None:
        """Step 5: one gather leaf per item, under its first-visited consumer, with summed demand."""
        first: dict[tuple[str, str], Node] = {}
        demand: dict[tuple[str, str], int] = {}
        for node in tree.post_order():
            work = node.work or {}
            if work.get("kind") not in GATHER_KINDS or not node.item or node.done:
                continue
            key = (work["kind"], node.item)
            demand[key] = demand.get(key, 0) + int(work.get("count", 1))
            if key not in first:
                first[key] = node
        for node in list(tree.nodes.values()):
            work = node.work or {}
            if work.get("kind") not in GATHER_KINDS or not node.item or node.done:
                continue
            key = (work["kind"], node.item)
            keeper = first[key]
            if node.id != keeper.id and node.id in tree.nodes:
                # move any children (a tool node) to the keeper before dropping the duplicate
                for child in list(node.children):
                    if not any(tree.get(c).item == tree.get(child).item for c in keeper.children):
                        tree.get(child).parent = keeper.id
                        keeper.children.insert(0, child)
                        node.children.remove(child)
                tree.remove(node.id)
        for key, node in first.items():
            node.acquire = demand[key]
            node.verify = catalog.item_spec(node.item, node.acquire).to_json()
            if node.acquire != node.work.get("count"):
                node.description = re.sub(r"^(obtain|collect) \d+", rf"\g<1> {node.acquire}", node.description)

    def mark_satisfied(self, tree: Tree) -> None:
        """4.2: nodes whose spec already holds are done, together with their subtrees."""
        if self.verify_fn is None:
            return
        for node in tree.post_order():
            if node.done:
                continue
            if node.parent is None and node.children and (node.kind in FINAL_WORK_KINDS or node.kind == "goal"):
                # a final step acts on items its subtree obtains: it can only be done after them
                if not all(tree.get(c).done for c in node.children):
                    continue
            try:
                ok = bool(self.verify_fn(node.verify))
            except Exception:
                ok = False
            if ok:
                for sub in tree.post_order(node.id):
                    sub.done = True
                    sub.done_at = sub.done_at or 0.0


def verify_fn_from_facts(facts: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    """A verify_fn over a facts dict (see catalog.evaluate); for tests and offline runs."""
    return lambda spec: catalog.evaluate(spec, facts)
