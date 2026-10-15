"""ActionBuilder: grow the agent's menu with one new, code-verified primitive.

PLAN.md section 6. Fired when Jev says the current subtask cannot be completed
with the actions on the menu (the ``completable`` checkpoint) or by stuck
routing. Steps:

1. Jev Choice: what KIND of action is missing (craft, smelt, mine, place, ...).
2. Jev Choice: which TARGET item or block it concerns (tree items, visible
   blocks, inventory, or none).
3. GPT writes one mineflayer module following the primitive contract
   (``menu`` / ``execute`` / optional ``observe``), as a TEMPLATE action such
   as ``mine_cursor`` rather than one bound to a single block name.
4. Static check (mirror of Node's), ``POST /agent/primitive``, one retry with
   Node's exact error.
5. The primitive enters the menu as a trial; the builder does not build again
   while a primitive it built is still untested (build cooldown).
6. Accepted sources are persisted under ``<ckpt>/primitives/<name>.js`` with a
   JSON sidecar and re-registered on resume. That directory is the skill
   library.

Jev never sees code. The one-line description a primitive's ``menu()`` writes
is its whole interface to the agent.
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import threading
import time
from typing import Any

from voyager.tree import catalog
from voyager.tree.experience import TraceStore, family_of, render_trace
from voyager.utils import timing

try:
    from typesafe_sdk import Choice
except Exception:  # the client is disabled in this case
    Choice = None  # type: ignore[assignment]

MAX_SOURCE_CHARS = 12000
FORBIDDEN = [
    (re.compile(r"\bprocess\s*\."), "process access"),
    (re.compile(r"child_process"), "child_process"),
    (re.compile(r"\brequire\s*\(\s*[\"'`]fs[\"'`]"), "fs access"),
    (re.compile(r"\beval\s*\("), "eval"),
    (re.compile(r"\bFunction\s*\("), "Function constructor"),
    (re.compile(r"\bbot\s*\.\s*chat\s*\("), "bot.chat"),
    (re.compile(r"\bbot\s*\.\s*(end|quit)\s*\("), "bot.end/quit"),
    (re.compile(r"\bsetInterval\s*\("), "setInterval"),
    (re.compile(r"\bimport\s*\("), "dynamic import"),
]

WORK_STEP_KINDS = ("craft", "smelt", "place", "equip", "eat", "use")

# What each typed outcome label (lib/outcomes.js) usually means for the code that
# produced it. Shown to the model in a repair prompt next to the evidence.
LABEL_HINTS = {
    "API_MISUSE": "a mineflayer call got the wrong argument type; the outcome text says what was expected. Fix exactly that call",
    "NOT_REACHABLE": "the target was out of reach or the path failed; approach it first (GoalNear within 2-3 blocks) or choose a reachable instance, and check the distance before acting",
    "NO_TARGET": "nothing matching was found; the search is probably too narrow (family names like *_log, the radius) or the target is absent, in which case menu() should return null",
    "NO_PLACE_SURFACE": "placing failed; choose a free air block above a SOLID block near the bot and place against the solid block under it",
    "MISSING_INPUT": "an input item was missing; count the inventory family-aware and make menu() return null while inputs are absent",
    "NO_EFFECT": "it reported ok but its declared purpose (or the world) did not change: it does not do what it claims, or returns ok too early (for example before picking up the drop)",
    "TIMEOUT": "it ran past the 20 s limit; bound every wait and pathfinder call and do one step per call",
    "MOVE_BLOCKED": "a walk could not move; route around or approach from another side",
    "OTHER_FAILURE": "read the failure text",
}  # node kinds whose action kind is known from the tree

KINDS = {
    "mine": "dig the block the bot is looking at (or the nearest block of a type) and pick up the drop",
    "craft": "craft an item from inventory, using a nearby crafting table when the recipe needs one",
    "smelt": "put an input and fuel into a nearby furnace and take the output",
    "place": "place a block from inventory on the ground next to the bot (crafting table, furnace, chest)",
    "use_block": "right-click or open a block in front of the bot (chest, door, bed, furnace)",
    "equip": "move an item from inventory to the hand or an armor slot",
    "collect": "walk to and pick up dropped items lying nearby",
    "attack": "attack the nearest hostile or animal in reach",
    "eat": "eat a food item from the inventory",
    "use": "use (right-click) an item from the inventory or on a block",
    "goto_nearest": "walk to the nearest visible block or entity of a type using the pathfinder",
    "explore": "walk a good distance into unseen terrain to reveal blocks that are not visible yet",
    "other": "something none of the kinds above describes",
}

CONTRACT = """\
Write ONE JavaScript module (no exports; require only "vec3",
"mineflayer-pathfinder" or "minecraft-data") that defines exactly:

    function menu(snap, loop)
        SYNCHRONOUS. Return a one-line description with live facts (distances,
        counts, what is at the cursor) when this action is worth offering right
        now, otherwise null. Jev reads ONLY this line, never your code, so say
        what the action will do in plain words.
        `snap` has:
          bot {position{x,y,z}, yaw (compass word), pitch, health, food, oxygen,
               xp, inWater, inLava, onGround, timeOfDay, daylight, biome}
          cursor {block (name or null), distance, entity (name or null)}
          horizon {north|northeast|...: {trees, water, samples}}  what lies 12,
               24 and 40 blocks away in each of 8 directions, e.g. samples
               "12:land:grass_block+1 24:trees+3 40:water-2"; trees/water count hits
          view [{block, from, to, hit?, occluded?}]  segments along the look
               direction out to 30 blocks; hit = first solid block seen
          inventory {itemName: count}
          slots {hand, offhand, head, chest, legs, feet}  (item names or null)
          blocks {blockName: {dx, dy, dz, count}}  nearest instance per type;
                 dx, dy, dz are SIGNED offsets from the bot (distance is
                 Math.hypot(dx, dy, dz), never dx alone); family keys like
                 "*_log" exist and a block may be listed only under its family
          entities [{name, distance, direction}]  direction is a COMPASS WORD
          dropped [{name, distance}]
          placed_by_me {blockName: {x, y, z}}
          subtask {id, description, verify, progress {have, need}}
          recent [{action, outcome, delta}]
          extra {primitiveName: {...}}  fields other primitives' observe() added
        `loop` helpers: loop.mcData, loop.inventoryCounts(),
          loop.nearestBlocks(matches, maxDistance, count) where `matches` is a
          function of a BLOCK NAME STRING and the result is an array of blocks
          nearest first with .name and .position (a Vec3),
          loop.matcher("*_log") -> (name) => bool for a family or exact name,
          loop.countMatching("*_log", snap.inventory) -> total count across the family
          (snap.inventory has concrete names like oak_log; never index it with "*_log"),
          loop.recipesFor("oak_planks" or "*_planks", tableBlockOrNull) -> array of
          mineflayer recipes craftable NOW from the inventory (empty when inputs are
          missing); a family name tries every member, and each recipe has
          .resultName (the concrete item it makes),
          loop.nearestEntity(["cow","pig"], maxDistance) -> live entity or null,
          loop.freeSpotNear(pos, radius) -> Vec3 of an air block over solid
          ground or null, loop.recordPlacement(pos, name) which EVERY place
          action must call after a successful placeBlock.

    async function execute(bot, loop, ctx)
        Do ONE bounded thing (it is cut off after 20 seconds). Return a string
        starting with "ok" on success (say what happened), otherwise
        "failed: <why>". HOW SUCCESS IS JUDGED: an "ok" counts only if the
        inventory changed, the bot moved at least one block, or a block was
        placed (via loop.recordPlacement). An "ok" with no such effect is scored
        as a failure and marked [NO EFFECT]. So a mining action must also pick
        up the drop (walk onto it: GoalBlock at the item entity's position, then
        wait briefly) before it returns.
        `ctx` has snap, subtask, tree_items (every item name in the goal tree).
        ctx.subtask = {id, description, kind, verify, work, requires, progress}
        where progress is {have, need} for item subtasks and NULL otherwise, so
        guard it (const need = ctx.subtask.progress ? ctx.subtask.progress.need : 1).
        In menu() the same object is snap.subtask.
        The snapshot is ONLY available as `ctx.snap` here
        (a bare `snap` variable does NOT exist inside execute, and there is no
        bot.blocks or bot.snap); for live positions use
        loop.nearestBlocks(...) or bot.blockAtCursor(6) rather than snapshot
        offsets. mineflayer calls return PROMISES: `await bot.dig(block)`,
        `await bot.equip(item, "hand")` (item = an inventory Item object),
        `await bot.placeBlock(referenceBlock, faceVec3)` where referenceBlock is
        a SOLID Block object from bot.blockAt(vec3) that you place against (not a
        position) and faceVec3 is e.g. new Vec3(0, 1, 0) for "on top of it".
        Placing pattern: equip the item; spot = loop.freeSpotNear(bot.entity
        .position, 3); ref = bot.blockAt(spot.offset(0, -1, 0)); await
        bot.placeBlock(ref, new Vec3(0, 1, 0)); loop.recordPlacement(spot, name).
        These calls check their arguments and throw a message saying what was
        expected; read it in the earlier-attempts section when rebuilding.
        `await bot.craft(recipe, count, tableBlock)`, `await bot.attack(entity)`,
        `await bot.toss(id, null, n)`, `await bot.lookAt(vec3)`. Never pass a
        callback to them. `bot.blockAtCursor(6)` returns the targeted block.
        CRAFTING: the name given to loop.recipesFor is the OUTPUT the action
        makes, never one of its inputs. A crafting-table crafter asks for
        "crafting_table"; a stick crafter asks for "stick". Pattern, with OUTPUT
        being the item this action produces (the step's output):
            const OUTPUT = "<the item this action makes>";
            const table = null; // 2x2 recipes (planks, sticks, crafting_table) need no table;
                                // 3x3 ones: loop.nearestBlocks((n) => n === "crafting_table", 4.5, 1)[0]
            const recipes = loop.recipesFor(OUTPUT, table);
            if (!recipes.length) return `failed: no craftable recipe for ${OUTPUT} (missing inputs or table)`;
            await bot.craft(recipes[0], 1, table);   // 1 = one application of the recipe
        When the output is a family (the step says "*_planks"), pass the family
        name as OUTPUT: loop.recipesFor("*_planks", null) returns whichever planks
        the held logs make. Never hard-code one wood type unless the step names it.
        bot.craft(recipe, n, table) applies the recipe n times (one log -> 4
        planks, so n = logs to use, not planks wanted). mcData.recipes is NOT
        indexed by name; do not call .find on it. For CRAFT steps this is
        checked when the action compiles: a recipesFor call that names something
        other than the step's output is rejected.
        Pathfinding: `await bot.pathfinder.goto(new goals.GoalNear(x, y, z, 1))`;
        GoalNear, GoalBlock, GoalLookAtBlock, GoalFollow are in scope.
        Every loop body must contain an await. Never call bot.chat.

    const PURPOSE = { description: "...", achieves: { ...VerifySpec... } }   REQUIRED
        What this action is FOR, declared by you. `description` is one short
        line Jev reads on the menu. `achieves` is the world condition the action
        brings about, written with the predicates listed under PURPOSE
        PREDICATES below and combinable with {all: [...]}, {any: [...]},
        {not: {...}}. The menu HIDES the action while `achieves` already holds,
        so make it the condition under which running the action again would be
        pointless. A crafting-table crafter: {any: [{has_item: {name:
        "crafting_table", count: 1}}, {near_block: {name: "crafting_table",
        radius: 4}}]}. A log miner for a 3-log subtask: {has_item: {name:
        "*_log", count: 3}}. It must NOT already hold right now while the
        subtask is unfinished, or the action would be hidden at once; that is
        checked when it compiles.

    function observe(bot, loop)   OPTIONAL, SYNCHRONOUS
        Return a small plain object (under 400 characters as JSON) that is
        merged into snap.extra.<name> on every snapshot. Add it only when the
        action needs a fact the snapshot lacks (light level, block under the
        bot). Must never throw.

TEMPLATE RULE: write the action for a CLASS of targets, not one block name.
"mine_nearby" (dig the nearest reachable block whose name matches a family the
goal needs) is right; "mine_oak_log" is wrong. The target you are given is a
HINT about the family: match it by suffix (any name ending in "_log" for a log
target, "_ore" for an ore) and also accept any block named in ctx.tree_items.

FAMILY NAMES: an item or block name starting with "*_" is a family. "*_log"
means any log (oak_log, birch_log, dark_oak_log...), "*_planks" any planks.
They appear in ctx.tree_items, snap.blocks keys and the subtask's verify spec.
Match a family by suffix: name === "log" || name.endsWith("_log"). Mixed wood
types are fine in every recipe (planks of any kind make sticks, tables, tools).

REACH RULE (for actions that DIG or USE a block): do NOT gate menu() on
snap.cursor alone. The cursor usually hits leaves, grass or air even when the
target is two blocks away. Find targets with loop.nearestBlocks(matcher, 4.5, 3)
(4.5 blocks is dig reach); in execute, `await bot.lookAt(block.position
.offset(0.5, 0.5, 0.5))` then `await bot.dig(block)`. Digging needs reach, not
line of sight. Use snap.cursor only as extra information.
APPROACH actions are different: offer them whenever the target appears in
snap.blocks at ANY distance (search with loop.nearestBlocks(matcher, 32, 1)),
and walk with bot.pathfinder.goto(new goals.GoalNear(x, y, z, 2)). Never copy
the 4.5 dig-reach gate into an approach action.
EXPLORE actions (for "find X nearby" subtasks) are needed exactly when X is NOT in
snap.blocks, so offer them whenever X is absent and say which way they would go.
They move the bot into unseen terrain toward where X is likely: CHOOSE the
heading at run time from snap.horizon (for logs: the direction with the most
`trees` samples; otherwise land over water), never a fixed direction. Walk with
bot.pathfinder.goto(new goals.GoalXZ(x, z)) (GoalXZ ignores height, which avoids
"No path to the goal") 12-24 blocks that way. Success = the bot moved.
APPLICABILITY CHECK: right after compiling, menu() is called on the current
snapshot shown above. If it returns null there, the action is rejected and you
are asked again, because the bot could never choose it.

In scope: bot, loop, mcData, Vec3, goals, Movements, require (restricted).
Start the module with a comment `// name: <lowercase_name>` and a comment
saying when menu() offers it. Answer with the module in one ```javascript
code block and nothing else.
"""

EXAMPLE = """\
```javascript
// name: mine_nearby
// Offered when a block the goal needs (here: any log) is within dig reach.
const PURPOSE = { description: "mine a needed block within reach and pick up the drop", achieves: { has_item: { name: "*_log", count: 3 } } };
function wanted(name, treeItems) {
    return name.endsWith("_log") || (treeItems || []).includes(name);
}
function menu(snap, loop) {
    const items = Object.keys(snap.blocks || {});
    const near = loop.nearestBlocks((n) => wanted(n, items), 4.5, 1);
    if (!near.length) return null;
    const b = near[0];
    const d = b.position.distanceTo(bot.entity.position).toFixed(1);
    return `Mine the ${b.name} ${d} blocks away by hand and pick up the drop`;
}
async function execute(bot, loop, ctx) {
    const near = loop.nearestBlocks((n) => wanted(n, ctx.tree_items), 4.5, 1);
    if (!near.length) return "failed: no wanted block within reach";
    const block = near[0];
    const before = loop.inventoryCounts();
    try {
        await bot.lookAt(block.position.offset(0.5, 0.5, 0.5));
        await bot.dig(block);
    } catch (e) {
        return `failed: dig threw ${e.message}`;
    }
    const drop = bot.nearestEntity((e) => e.name === "item" && e.position.distanceTo(bot.entity.position) < 6);
    if (drop) {
        try {
            await bot.pathfinder.goto(new goals.GoalNear(drop.position.x, drop.position.y, drop.position.z, 0));
        } catch (e) {
            // the drop may already be collected
        }
    }
    await new Promise((r) => setTimeout(r, 500));
    const after = loop.inventoryCounts();
    const gained = Object.keys(after).filter((k) => (after[k] || 0) > (before[k] || 0));
    return gained.length ? `ok mined ${block.name}, gained ${gained.join(", ")}` : `ok mined ${block.name} (no item picked up yet)`;
}
```
"""


def static_check(name: str, source: str) -> str | None:
    """Mirror of Node's checks so a bad module never leaves Python."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", name or ""):
        return "name must be lowercase [a-z0-9_], 2-32 chars"
    if not source or not source.strip():
        return "source is empty"
    if len(source) > MAX_SOURCE_CHARS:
        return f"source longer than {MAX_SOURCE_CHARS} chars"
    for rx, why in FORBIDDEN:
        if rx.search(source):
            return f"forbidden: {why}"
    if not re.search(r"\bfunction\s+menu\s*\(", source):
        return "missing `function menu(snap, loop)`"
    if re.search(r"\basync\s+function\s+menu\s*\(", source):
        return "menu() must be synchronous"
    if re.search(r"\basync\s+function\s+observe\s*\(", source):
        return "observe() must be synchronous"
    if not re.search(r"\basync\s+function\s+execute\s*\(", source):
        return "missing `async function execute(bot, loop, ctx)`"
    if not re.search(r"\bconst\s+PURPOSE\s*=", source):
        return "missing `const PURPOSE = { description, achieves }` (what the action is for; see the contract)"
    for m in re.finditer(r"\b(while|for)\s*\([^)]*\)\s*\{", source):
        depth, i = 1, m.end()
        start = i
        while i < len(source) and depth:
            depth += source[i] == "{"
            depth -= source[i] == "}"
            i += 1
        if "await" not in source[start : i - 1]:
            return f"{m.group(1)} loop without await"
    return None


def extract_module(reply: str) -> tuple[str | None, str | None]:
    """(name, source) from a GPT reply: the first code block, name from a leading comment."""
    m = re.search(r"```(?:javascript|js)?\s*\n(.*?)```", reply, re.S)
    source = (m.group(1) if m else reply).strip()
    if not source:
        return None, None
    name = None
    head = re.search(r"//\s*name\s*:\s*([a-z][a-z0-9_]{1,31})", source)
    if head:
        name = head.group(1)
    return name, source


def outcomes_by_action(recent) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in recent or []:
        a = str(r.get("action") or "")
        o = str(r.get("outcome") or "")[:40]
        if not a:
            continue
        out.setdefault(a, {})
        out[a][o] = out[a].get(o, 0) + 1
    return out


class ActionBuilder:
    def __init__(
        self,
        bridge,
        jev,
        llm,
        *,
        ckpt_dir: str = "ckpt",
        library_dir: str | None = None,
        enabled: bool = True,
        max_live: int = 12,
        min_seconds_between_builds: float = 5.0,
        unpicked_decisions: int = 6,
        max_repairs: int = 3,
        lessons=None,
    ):
        """``bridge`` has ``agent_primitive``; ``jev`` is a ``JevClient`` (may be disabled);
        ``llm`` has ``complete(messages) -> str`` where messages are ``{"role", "content"}`` dicts."""
        self.bridge = bridge
        self.jev = jev
        self.llm = llm
        self.enabled = enabled
        self.max_live = max_live
        self.min_seconds_between_builds = min_seconds_between_builds
        self.unpicked_decisions = unpicked_decisions  # offered this often without being tried = Jev passed it over
        # a shared library dir carries learned actions across runs; default is per-run
        self.dir = library_dir or os.path.join(ckpt_dir, "primitives")
        self.max_attempts = 3  # GPT replies per build: static check, Node compile and applicability feedback
        os.makedirs(self.dir, exist_ok=True)
        self.registered: dict[str, str] = {}  # name -> source accepted this run or reloaded
        self.meta: dict[str, dict[str, Any]] = {}  # name -> sidecar
        self.last_build_at: float = 0.0
        self.builds = 0
        self.log: list[str] = []
        self.last_refusal: str | None = None  # why the last build() returned None
        self.last_build_subtask: str | None = None  # the subtask the last build was for
        self.stale_kinds: dict[str, set[str]] = {}  # subtask id -> kinds whose trial was never applicable
        # Repair, not rewrite: a failing action is fixed in place under the same name up to
        # this many times (versions 2..max_repairs+1); old versions are kept in versions/.
        self.max_repairs = max_repairs
        self.versions_dir = os.path.join(self.dir, "versions")
        # completed-subtask traces (raw evidence), retrieved one at a time into prompts
        self.traces = TraceStore(self.dir)
        # post-goal lessons (voyager.tree.review.LessonBook) or None
        self.lessons = lessons
        # builds may run on a worker thread while the poll loop records outcomes
        self._lock = threading.RLock()
        self.model_name = getattr(llm, "model", None)

    # ------------------------------------------------------------------ #
    # library
    # ------------------------------------------------------------------ #

    def saved(self) -> list[str]:
        return sorted(f[:-3] for f in os.listdir(self.dir) if f.endswith(".js"))

    def reload(self, proven_only: bool = False) -> int:
        """Re-register saved primitives with the bot process. Returns how many were accepted.

        ``proven_only`` (used for the shared library) loads only actions that have
        succeeded at least once and are not retired: the library is the set of skills
        the agent has actually learned. Their per-run bookkeeping (which subtask they
        were built for) is cleared so it cannot leak into this run's tree, whose node
        ids are reused.
        """
        if proven_only:
            self.prune()
        count = 0
        for name in self.saved():
            with open(os.path.join(self.dir, f"{name}.js"), encoding="utf-8") as fh:
                source = fh.read()
            side = os.path.join(self.dir, f"{name}.json")
            meta: dict[str, Any] = {}
            if os.path.exists(side):
                try:
                    with open(side, encoding="utf-8") as fh:
                        meta = json.load(fh)
                except Exception:
                    meta = {}
            if proven_only and (int(meta.get("successes") or 0) == 0 or meta.get("retired")):
                continue
            trials = int(meta.get("trials") or 0)
            if proven_only and trials >= 10 and int(meta.get("successes") or 0) / trials < 0.2:
                # a skill that has stopped working (under 20% over 10+ tries) leaves the library
                self._say(f"library: dropping x:{name} ({meta.get('successes')}/{trials} successes)")
                continue
            if proven_only:
                meta["subtask_id"] = None
                meta["learned_from"] = meta.get("subtask")
            self.meta[name] = meta
            meta["base"] = {k: int(meta.get(k) or 0) for k in ("trials", "successes", "failures")}
            if not re.search(r"\bconst\s+PURPOSE\s*=", source):
                # learned before actions declared what they are for: have the model
                # read the code and declare it now, so the menu can hide it when done
                source = self.declare_purpose(name, source, meta) or source
            try:
                try:
                    res = self.bridge.agent_primitive(name, source, purpose=meta.get("learned_from") or meta.get("subtask"))
                except TypeError:
                    res = self.bridge.agent_primitive(name, source)
            except Exception as exc:
                self._say(f"reload: could not post x:{name}: {exc}")
                continue
            if res.get("ok"):
                self.registered[name] = source
                count += 1
            else:
                self._say(f"reload: Node rejected x:{name}: {res.get('error')}")
        if count:
            self._say(f"reloaded {count} saved primitive(s) from {self.dir}")
        return count

    def _read_sidecar(self, name: str) -> dict[str, Any]:
        side = os.path.join(self.dir, f"{name}.json")
        if not os.path.exists(side):
            return {}
        try:
            with open(side, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def prune(self, untried_runs: int = 3) -> list[str]:
        """Move duplicate and dead entries out of the library into ``_archive/``.

        * Duplicates: entries with the same (kind, target, declared purpose) are one
          skill written several times; the one with the most successes (then the best
          success rate, then the newest) stays.
        * Dead: an entry that has never been tried after being in the library for
          ``untried_runs`` reloads.
        Nothing is deleted; archived files can be moved back by hand. Returns the
        archived names."""
        archive = os.path.join(self.dir, "_archive")
        entries = []
        for name in self.saved():
            meta = self._read_sidecar(name)
            meta["runs_seen"] = int(meta.get("runs_seen") or 0) + 1
            entries.append((name, meta))
        groups: dict[str, list[tuple[str, dict[str, Any]]]] = collections.defaultdict(list)
        for name, meta in entries:
            key = json.dumps([meta.get("kind"), meta.get("target"), meta.get("purpose_spec")], sort_keys=True, default=str)
            groups[key].append((name, meta))
        drop: dict[str, str] = {}
        for members in groups.values():
            if len(members) < 2:
                continue
            ranked = sorted(
                members,
                key=lambda nm: (
                    int(nm[1].get("successes") or 0),
                    (int(nm[1].get("successes") or 0) / int(nm[1].get("trials") or 1)),
                    float(nm[1].get("created_at") or 0),
                ),
                reverse=True,
            )
            for name, _ in ranked[1:]:
                drop[name] = f"duplicate of x:{ranked[0][0]}"
        for name, meta in entries:
            if name not in drop and int(meta.get("trials") or 0) == 0 and meta["runs_seen"] >= untried_runs:
                drop[name] = f"never tried in {meta['runs_seen']} runs"
        for name, meta in entries:
            if name in drop:
                os.makedirs(archive, exist_ok=True)
                meta["archived"] = drop[name]
                for ext in (".js", ".json"):
                    src = os.path.join(self.dir, name + ext)
                    if os.path.exists(src):
                        shutil.move(src, os.path.join(archive, name + ext))
                with open(os.path.join(archive, f"{name}.json"), "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, indent=2)
                self._say(f"library: archived x:{name} ({drop[name]})")
            else:
                with open(os.path.join(self.dir, f"{name}.json"), "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, indent=2)
        return sorted(drop)

    def remember(self, subtask: dict[str, Any], records: list[dict[str, Any]], goal_id: str | None = None) -> None:
        """Keep the raw action records of a completed subtask as an experience trace."""
        entry = self.traces.save(description=str(subtask.get("description") or ""), kind=subtask.get("kind"),
                                 records=records, goal_id=goal_id, node_id=subtask.get("id"))
        if entry:
            self._say(f"trace kept: '{entry['description']}' done in {entry['actions']} action(s)")

    def declare_purpose(self, name: str, source: str, meta: dict[str, Any]) -> str | None:
        """Ask the model to read an existing action and declare its PURPOSE. Returns the
        source with the declaration prepended (also saved to the library), or None."""
        if self.llm is None:
            return None
        prompt = (
            "This Minecraft bot action was written without a PURPOSE declaration. Read it and declare what it is "
            "for. Answer with ONE JSON object and nothing else: {\"description\": \"<one short line>\", "
            "\"achieves\": <VerifySpec>}. `achieves` is the world condition the action brings about; the menu "
            "HIDES the action while it holds, so make it the condition under which running the action again would be "
            "pointless (a crafting-table crafter: {\"any\": [{\"has_item\": {\"name\": \"crafting_table\", "
            "\"count\": 1}}, {\"near_block\": {\"name\": \"crafting_table\", \"radius\": 4}}]}). Family names "
            "like \"*_log\" are allowed. Combine with {\"all\": [...]}, {\"any\": [...]}, {\"not\": {...}}.\n\n"
            f"It was learned for the step: {meta.get('learned_from') or meta.get('subtask') or 'unknown'}\n\n"
            "Predicates:\n" + catalog.describe_catalog() + "\n\nAction source:\n```javascript\n" + source[:6000] + "\n```"
        )
        messages = [{"role": "user", "content": prompt}]
        for _ in range(2):
            try:
                reply = self.llm.complete(messages)
                reply = getattr(reply, "content", reply)
                text = str(reply)
                start = text.find("{")
                declared = json.JSONDecoder().raw_decode(text[start:])[0] if start >= 0 else None
            except Exception as exc:
                self._say(f"library: could not get a purpose for x:{name}: {exc}")
                return None
            problems = []
            if not isinstance(declared, dict) or not isinstance(declared.get("description"), str) or not declared.get("achieves"):
                problems = ["answer must be {description: string, achieves: VerifySpec}"]
            else:
                problems = catalog.validate(declared["achieves"])
            if not problems:
                break
            messages += [{"role": "assistant", "content": str(reply)}, {"role": "user", "content": f"Invalid: {problems}. Answer with the corrected JSON object only."}]
        else:
            self._say(f"library: x:{name} still has no valid purpose; loading it without one")
            return None
        line = "const PURPOSE = " + json.dumps({"description": declared["description"][:160], "achieves": declared["achieves"]}) + ";\n"
        new_source = line + source
        try:
            with open(os.path.join(self.dir, f"{name}.js"), "w", encoding="utf-8") as fh:
                fh.write(new_source)
        except OSError:
            pass
        meta["purpose"] = declared["description"][:160]
        meta["purpose_spec"] = declared["achieves"]
        self._say(f"library: x:{name} now declares its purpose: {declared['description'][:80]!r}")
        return new_source

    def record_outcomes(self, status: dict[str, Any]) -> None:
        """Copy trial statistics from a status poll into the sidecars.

        Node's counters restart every run, so the saved totals are the counts carried
        in from earlier runs (``base``) plus this run's. Overwriting them would make a
        proven skill look unproven and drop it from the library.

        Runs on the poll thread; when a build on the worker thread holds the lock this
        poll's update is skipped (the next poll carries the same totals)."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._record_outcomes(status)
        finally:
            self._lock.release()

    def _record_outcomes(self, status: dict[str, Any]) -> None:
        for p in status.get("primitives") or []:
            name = p.get("name")
            if not name or name not in self.registered:
                continue
            meta = self.meta.setdefault(name, {})
            base = meta.setdefault("base", {k: int(meta.get(k) or 0) for k in ("trials", "successes", "failures")})
            changed = False
            for key in ("trials", "successes", "failures"):
                if key in p:
                    total = int(base.get(key, 0)) + int(p[key] or 0)
                    if meta.get(key) != total:
                        meta[key] = total
                        changed = True
            if "retired" in p and meta.get("retired") != p["retired"]:
                meta["retired"] = p["retired"]
                changed = True
            if changed:
                self._save_sidecar(name)

    # ------------------------------------------------------------------ #
    # cooldown
    # ------------------------------------------------------------------ #

    def note_stale(self, names: list[str], subtask_id: str | None) -> str:
        """Record trials whose menu() has never fired for this subtask.

        The action stays registered: a mine action that is never offered because
        no log is within reach is not broken, the bot just has not got there.
        Its KIND is excluded from the next design for this subtask, so Jev has to
        pick a different kind (approach the target, collect, craft...) instead of
        asking GPT to rewrite the same action again. Returns a sentence for the prompt.
        """
        notes = []
        bucket = self.stale_kinds.setdefault(subtask_id or "", set())
        for name in names:
            meta = self.meta.get(name) or {}
            kind = meta.get("kind")
            if kind:
                bucket.add(kind)
            source = self.registered.get(name, "")
            menu_src = ""
            m = re.search(r"function\s+menu\s*\([^)]*\)\s*\{(.*?)\n\}", source, re.S)
            if m:
                menu_src = " ".join(m.group(1).split())[:300]
            self._say(f"x:{name} ({kind or 'unknown kind'}) has never been applicable; designing a different kind next")
            notes.append(
                f"The registered action x:{name} (kind {kind}) stays on the menu but has never been offered because its "
                f"condition never held (menu body: {menu_src or 'unavailable'}). Build something that MAKES it applicable "
                "or achieves the subtask another way; do not write another action of the same kind."
            )
        return " ".join(notes)

    # kept for callers written against the old name
    retire_stale = note_stale

    def cooling_down(self, status: dict[str, Any], subtask_id: str | None = None) -> str | None:
        """Reason the builder must not build now, or None.

        An untested trial blocks a new build only when it was built for THIS
        subtask and Jev has actually tried it at least once. A mine action left
        over from the log subtask must not stop the plank subtask from getting a
        craft action, and an action Jev never picks is not being tested at all.
        """
        if not self.enabled:
            return "builder disabled"
        prims = status.get("primitives") or []
        live = [p for p in prims if not p.get("retired")]
        if len(live) >= self.max_live:
            return f"{len(live)} generated actions are live already"
        for p in live:
            name = p.get("name")
            if name not in self.registered or p.get("stale"):
                continue
            meta = self.meta.get(name) or {}
            built_for = meta.get("subtask_id")
            # only a fresh build for THIS subtask can hold the next build back: a library
            # skill (built in an earlier run, built_for None) or one built for another
            # subtask is not on trial here, however it fares now
            if built_for is None or (subtask_id is not None and built_for != subtask_id):
                continue
            if int((meta.get("base") or {}).get("successes", 0)) > 0:
                continue
            attempted = int(p.get("trials", 0))
            if attempted == 1 and p.get("successes", 0) == 0:
                # one failure may be bad luck; from the second the builder repairs it
                return f"x:{name} is still an untested trial ({attempted} attempt(s), no success yet)"
        # Trial-based gate: the newest action built for THIS subtask must get its chance
        # before another is written. It has had it once Jev has tried it, once it has sat
        # on the menu for UNPICKED_DECISIONS decisions without being chosen (Jev does not
        # want it), or once it has never been applicable (stale). A build for a different
        # subtask fills a different gap and does not wait.
        newest = self._newest_for(subtask_id)
        if newest is not None:
            p = next((x for x in prims if x.get("name") == newest), None)
            if p and not p.get("retired") and not p.get("stale"):
                tried = int(p.get("trials") or 0) > 0
                passed_over = int(p.get("offered") or 0) >= self.unpicked_decisions
                if not tried and not passed_over:
                    return f"x:{newest} has not had its chance yet (offered {int(p.get('offered') or 0)}x, not tried)"
        # a short floor only, so two triggers in the same poll do not both build
        if time.time() - self.last_build_at < self.min_seconds_between_builds:
            return "a build finished moments ago"
        return None

    def _newest_for(self, subtask_id: str | None) -> str | None:
        """The most recently built live action for this subtask, if any."""
        if subtask_id is None:
            return None
        best, best_t = None, -1.0
        for name, meta in self.meta.items():
            if name in self.registered and meta.get("subtask_id") == subtask_id:
                t = float(meta.get("created_at") or 0)
                if t > best_t:
                    best, best_t = name, t
        return best

    # ------------------------------------------------------------------ #
    # build
    # ------------------------------------------------------------------ #

    def build(self, subtask: dict[str, Any], status: dict[str, Any], tree_items: list[str], reason: str = "", *, force: bool = False) -> str | None:
        """Repair, or design, write, check, register and persist one primitive. Returns its name or None.

        ``force`` skips the time-based cooldown (not the untested-trial one): used
        when a work step's inputs are all in the inventory and only the action is missing.
        Safe to call from a worker thread: the whole build holds the builder's lock.
        """
        with self._lock:
            return self._build(subtask, status, tree_items, reason, force=force)

    def _build(self, subtask: dict[str, Any], status: dict[str, Any], tree_items: list[str], reason: str = "", *, force: bool = False) -> str | None:
        self.last_refusal = None
        sid = subtask.get("id")
        snap = status.get("snap") or {}
        recent = status.get("recent") or []
        menu = status.get("menu") or {}
        # Repair comes first: an action built for this subtask that keeps failing is
        # fixed in place, under its name, with its full evidence. Before this, a retired
        # action was never repaired and GPT rewrote it from scratch as name_2, name_3 ... (F1).
        repair = self._failing_trial(status, sid)
        if repair is not None:
            rname, rmeta = repair
            if int(rmeta.get("version") or 1) > self.max_repairs:
                restored = self._give_up_on(rname, status)
                if restored:
                    return restored  # an earlier version that worked is back on the menu
            else:
                self.last_build_at = time.time()
                self.last_build_subtask = sid
                self._status = status
                return self._write_and_register(
                    subtask, snap, recent, menu, tree_items,
                    rmeta.get("kind") or "other", rmeta.get("target") or "none", reason, repair_of=rname,
                )
        why_not = self.cooling_down(status, sid)
        if why_not and "live already" in why_not:
            # the menu is full: retire the weakest generated action instead of refusing,
            # so a full menu never blocks the step that needs something new
            evicted = self._evict_weakest(status, sid)
            why_not = None if evicted else why_not
            if evicted:
                status = {**status, "primitives": [p for p in status.get("primitives") or [] if p.get("name") != evicted]}
                why_not = self.cooling_down(status, sid)
        if why_not and force and why_not != "builder disabled" and "live already" not in why_not:
            # inputs are in hand and only the work step is missing: neither the build
            # spacing nor an unrelated half-tested trial may hold this up
            self._say(f"forced build for '{subtask.get('description')}' (ignoring: {why_not})")
            why_not = None
        if why_not:
            self.last_refusal = why_not
            self._say(f"not building for '{subtask.get('description')}': {why_not}")
            return None
        self.last_build_at = time.time()
        self.last_build_subtask = sid
        self._status = status
        work = subtask.get("work") or {}
        if subtask.get("kind") in WORK_STEP_KINDS and work.get("item"):
            # The tree already knows what this step is: a craft, smelt or place of a
            # named item. Asking Jev which kind of action is missing only invites a
            # wrong guess (a collect action for a craft step), so skip the design.
            kind, target = subtask["kind"], work["item"]
            self._say(f"work step '{subtask.get('description')}' is a {kind} of {target}; building that directly")
        elif subtask.get("kind") == "locate" and work.get("block"):
            # a find-it step whose target is not in view: what is missing is exploring
            kind, target = "explore", work["block"]
            self._say(f"locate step '{subtask.get('description')}': building an explore action for {target}")
        else:
            kind, target = self._design(subtask, snap, recent, menu, tree_items)
        # A design that matches an action already built for this subtask is that action
        # again: repair it if it can still be repaired, rather than minting a twin.
        twin = self._same_design(sid, kind, target)
        if twin is not None and int((self.meta.get(twin) or {}).get("version") or 1) <= self.max_repairs:
            self._say(f"design {kind}/{target} matches x:{twin}; repairing it instead of writing a duplicate")
            return self._write_and_register(subtask, snap, recent, menu, tree_items, kind, target, reason, repair_of=twin)
        return self._write_and_register(subtask, snap, recent, menu, tree_items, kind, target, reason)

    def untested_for(self, subtask_id: str | None, status: dict[str, Any]) -> list[str]:
        """Live actions built for this subtask whose current version has not been tried
        yet (and is not merely never applicable). Traversal does not give up on a node
        while one of these is waiting for its first try."""
        out = []
        for p in status.get("primitives") or []:
            name = p.get("name")
            meta = self.meta.get(name) or {}
            if name not in self.registered or meta.get("subtask_id") != subtask_id or meta.get("final_retired"):
                continue
            if p.get("retired") or p.get("stale") or int(p.get("trials") or 0) > 0:
                continue
            out.append(name)
        return out

    def _same_design(self, subtask_id: str | None, kind: str, target: str) -> str | None:
        """A live action built for this subtask with the same kind and target that has
        not succeeded in its current version, or None."""
        for name, meta in self.meta.items():
            if name not in self.registered or meta.get("subtask_id") != subtask_id or meta.get("final_retired"):
                continue
            if meta.get("kind") == kind and meta.get("target") == target and int(meta.get("successes") or 0) == 0:
                return name
        return None

    def _failing_trial(self, status: dict[str, Any], subtask_id: str | None):
        """An action built for this subtask whose current version has run at least twice
        without a success, including one Node has taken off the menu (retired) after
        three failures: the one to repair. Never-applicable (stale) actions are not
        failing, they are waiting. Returns (name, meta) for the most recent, or None."""
        best = None
        for p in status.get("primitives") or []:
            name = p.get("name")
            meta = self.meta.get(name) or {}
            if name not in self.registered or p.get("stale") or meta.get("final_retired"):
                continue
            if meta.get("subtask_id") != subtask_id:
                continue
            if int(p.get("trials") or 0) >= 2 and int(p.get("successes") or 0) == 0:
                if best is None or float(meta.get("created_at") or 0) > float(best[1].get("created_at") or 0):
                    best = (name, meta)
        return best

    def _give_up_on(self, name: str, status: dict[str, Any]) -> str | None:
        """The repair budget is spent. Restore the best earlier version if one ever
        succeeded (returns its name); otherwise retire the action for good so its kind is
        designed afresh (returns None)."""
        meta = self.meta.setdefault(name, {})
        good = [v for v in meta.get("versions") or [] if int(v.get("successes") or 0) > 0 and v.get("path")]
        if good:
            best = max(good, key=lambda v: (int(v["successes"]), int(v["successes"]) / max(1, int(v.get("trials") or 1))))
            try:
                with open(best["path"], encoding="utf-8") as fh:
                    source = fh.read()
                res = self.bridge.agent_primitive(name, source, purpose=meta.get("subtask"), repair=True, version=best["version"])
            except Exception as exc:
                res = {"ok": False, "error": str(exc)}
            if res.get("ok"):
                self.registered[name] = source
                with open(os.path.join(self.dir, f"{name}.js"), "w", encoding="utf-8") as fh:
                    fh.write(source)
                meta.update({"version": best["version"], "restored_from": best["version"], "trials": 0, "successes": 0,
                             "failures": 0, "retired": False, "base": {"trials": 0, "successes": 0, "failures": 0}})
                self._save_sidecar(name)
                self._say(f"repair budget spent for x:{name}; restored version {best['version']} "
                          f"({best['successes']}/{best.get('trials')} successes)")
                self._log("action_restored", {"name": name, "version": best["version"]})
                return name
        meta["final_retired"] = True
        meta["retired"] = True
        self._save_sidecar(name)
        self._say(f"repair budget spent for x:{name} ({meta.get('version')} versions, none succeeded); retired for good")
        self._log("action_given_up", {"name": name, "versions": meta.get("versions"), "kind": meta.get("kind"), "target": meta.get("target")})
        return None

    def _evict_weakest(self, status: dict[str, Any], subtask_id: str | None) -> str | None:
        """Retire one generated action to make room on a full menu. Returns its name.

        Weakest first: never succeeded before proven ones; then the lowest success
        rate; then the least often offered (an action the world rarely makes
        applicable); then the oldest. A fresh trial built for the current subtask is
        kept, since it has not had its chance yet."""
        candidates = []
        for p in status.get("primitives") or []:
            name = p.get("name")
            if p.get("retired") or name not in self.registered:
                continue
            meta = self.meta.get(name) or {}
            if meta.get("subtask_id") == subtask_id and int(p.get("trials") or 0) == 0:
                continue
            base = meta.get("base") or {}
            trials = int(base.get("trials", 0)) + int(p.get("trials") or 0)
            successes = int(base.get("successes", 0)) + int(p.get("successes") or 0)
            rate = successes / trials if trials else 0.0
            key = (successes > 0, rate, int(p.get("offered") or 0), float(meta.get("created_at") or 0))
            candidates.append((key, name, successes, trials))
        if not candidates:
            return None
        _, name, successes, trials = min(candidates)
        try:
            self.bridge.agent_primitive_remove(name)
        except Exception as exc:
            self._say(f"could not evict x:{name}: {exc}")
            return None
        self.registered.pop(name, None)
        meta = self.meta.setdefault(name, {})
        meta["evicted"] = True
        if successes == 0:
            meta["retired"] = True
        self._save_sidecar(name)
        self._say(f"menu full: evicted x:{name} ({successes}/{trials} successes) to make room")
        return name

    def _unique_name(self, name: str) -> str:
        """Never reuse a registered name: replacing an action in place merges its trial
        counts with the new code and resets its offered counter, so one action can look
        both never-offered and half-tested at once. A reused name gets a numeric suffix."""
        if name not in self.registered and name not in self.meta:
            return name
        stem = re.sub(r"_\d+$", "", name)[:29]
        n = 2
        while f"{stem}_{n}" in self.registered or f"{stem}_{n}" in self.meta:
            n += 1
        fresh = f"{stem}_{n}"
        self._say(f"builder: name '{name}' already exists; registering as '{fresh}'")
        return fresh

    def _open_kinds(self, subtask_id: str | None = None) -> dict[str, str]:
        """Kinds Jev may choose for this subtask.

        Every kind stays open except, for this subtask only, kinds whose action was
        never applicable (stale) or was retired for good after its repair budget. An
        earlier version also removed any kind that had a proven action anywhere and any
        kind already tried here; that hid `mine` from the cobblestone step because a log
        miner existed, and Jev was left choosing between wrong kinds (F2). Duplicates are
        now prevented by repairing the existing action instead.
        """
        closed = set(self.stale_kinds.get(subtask_id or "", set()))
        if subtask_id is not None:
            for meta in self.meta.values():
                if meta.get("subtask_id") == subtask_id and meta.get("final_retired") and meta.get("kind"):
                    closed.add(meta["kind"])
        open_kinds = {k: v for k, v in KINDS.items() if k not in closed or k == "other"}
        return open_kinds if len(open_kinds) > 1 else dict(KINDS)

    def _learned_by_kind(self, menu: dict[str, str]) -> dict[str, list[str]]:
        """For the design question: the actions the bot already has, by kind, with their
        record and whether the menu offers them right now."""
        out: dict[str, list[str]] = {}
        stats = {p.get("name"): p for p in ((getattr(self, "_status", None) or {}).get("primitives") or [])}
        for name, meta in self.meta.items():
            if name not in self.registered or meta.get("final_retired"):
                continue
            p = stats.get(name) or {}
            trials = int((meta.get("base") or {}).get("trials", 0)) + int(p.get("trials") or 0)
            wins = int((meta.get("base") or {}).get("successes", 0)) + int(p.get("successes") or 0)
            where = "offered on the menu now" if f"x:{name}" in (menu or {}) else "NOT offered now (its condition does not hold here)"
            what = meta.get("purpose") or meta.get("learned_from") or meta.get("subtask") or ""
            out.setdefault(meta.get("kind") or "other", []).append(f"x:{name}: {what} ({wins}/{trials} successes; {where})")
        return out

    def _design(self, subtask, snap, recent, menu, tree_items) -> tuple[str, str]:
        """Jev picks the kind of action and its target. Falls back to ('other', 'none')."""
        if self.jev is None or not getattr(self.jev, "enabled", False) or Choice is None:
            return "other", "none"
        candidates: dict[str, str | None] = {}
        for it in tree_items or []:
            candidates[it] = "item in the goal tree"
        for b in (snap.get("blocks") or {}).keys():
            candidates.setdefault(b, "block type visible nearby")
        for it in (snap.get("inventory") or {}).keys():
            candidates.setdefault(it, "item in the inventory")
        candidates = dict(list(candidates.items())[:40])
        candidates["none"] = "the action has no single item or block target"
        state = {
            "subtask": subtask.get("description"),
            "subtask_verify": subtask.get("verify"),
            "menu": menu,
            "recent_actions": recent,
            "outcomes_by_action": outcomes_by_action(recent),
            "inventory": snap.get("inventory") or {},
            "cursor": snap.get("cursor"),
            "nearby_blocks": snap.get("blocks") or {},
            "nearby_entities": snap.get("entities") or [],
            "tree_items": tree_items,
            "learned_actions": self._learned_by_kind(menu),
        }
        response = self.jev.ask(
            "builder_design",
            state,
            {
                "kind": Choice(
                    instructions=(
                        "A Minecraft bot cannot finish `subtask` with the actions in `menu` (each key is an "
                        "action id, each value says what it does). `recent_actions` shows what it tried. "
                        "Which KIND of action is missing from the menu and would let it make progress? "
                        "Pick the kind whose description matches the first thing the bot cannot do yet. "
                        "`learned_actions` lists, by kind, the actions the bot already has and whether the menu "
                        "offers each one right now. When the needed kind already has an action that is not "
                        "offered now, what is missing is whatever makes it applicable (getting within reach, a "
                        "tool, a free spot), so pick the kind that provides that."
                    ),
                    criteria=self._open_kinds(subtask.get("id")),
                ),
                "target": Choice(
                    instructions=(
                        "If one new action were added to the menu to progress `subtask`, which item or block "
                        "should it be about? Consider `tree_items` (what the goal needs), `nearby_blocks` and "
                        "`cursor` (what is visible) and `inventory` (what is held). Choose `none` when the "
                        "action is not about one specific item."
                    ),
                    criteria=candidates,
                ),
            },
            extra_record={"subtask": subtask.get("description")},
        )
        if response is None:
            return "other", "none"
        try:
            kind = response.choices["kind"].choice
            target = response.choices["target"].choice
        except Exception:
            return "other", "none"
        if kind not in KINDS:
            kind = "other"
        if target not in candidates:
            target = "none"
        self._say(f"Jev designed a {kind} action about {target} for '{subtask.get('description')}'")
        return kind, target

    def _prompt(self, subtask, snap, recent, menu, tree_items, kind, target, reason) -> str:
        return (
            f"Subtask the bot is stuck on: {subtask.get('description')}\n"
            + self._gap_line(subtask, snap)
            + (f"Planning hints: {'; '.join((subtask.get('work') or {}).get('hints') or [])}\n" if (subtask.get('work') or {}).get('hints') else "")
            + f"Its completion check (a VerifySpec): {json.dumps(subtask.get('verify'))}\n"
            f"Why a new action is wanted: {reason or 'Jev judged the subtask not completable with the current menu'}\n"
            f"Kind of action to build: {kind} ({KINDS.get(kind, '')})\n"
            f"Target it concerns: {target}\n"
            f"Items in the goal tree: {tree_items}\n"
            f"Current menu (id -> what it does): {json.dumps(menu)[:1500]}\n"
            f"Snapshot now: {json.dumps(snap)[:2500]}\n"
            f"Recent actions and outcomes: {json.dumps(recent)[:1500]}\n"
            f"Outcome tallies by action: {outcomes_by_action(recent)}\n"
            f"Generated actions already registered: {sorted(self.registered)}\n\n"
            + self._attempt_history(subtask)
            + self._experience_block(subtask)
            + "Design ONE new action of that kind that the bot can pick from its menu. It must do something "
            "no current menu entry does. Do not re-implement an existing registered action.\n\n"
            + CONTRACT
            + "\nPURPOSE PREDICATES (for PURPOSE.achieves; '*_log' style family names are allowed):\n"
            + catalog.describe_catalog() + "\n"
            + "\nExample of the expected style:\n"
            + EXAMPLE
        )

    def _experience_block(self, subtask: dict[str, Any], failure_labels: list[str] | None = None) -> str:
        """Raw evidence and lessons from earlier play, labelled as guidance:
        * the single closest completed trace of a similar step (raw action records);
        * the lesson file for this step's family and the remedies that match it, when the
          post-goal review has written any. Raw traces stay the source of truth."""
        parts = []
        try:
            prior = self.traces.most_similar(str(subtask.get("description") or ""), subtask.get("kind"))
        except Exception:
            prior = None
        if prior:
            parts.append(
                f"A PREVIOUS COMPLETED ATTEMPT AT A SIMILAR STEP ('{prior.get('description')}', "
                f"{prior.get('actions')} actions). These are raw records of what the bot did and what happened; "
                "use them as evidence of what works, not as a script:\n" + render_trace(prior.get("records") or [], limit=25)
            )
        if self.lessons is not None:
            try:
                text = self.lessons.for_builder(family_of(subtask.get("kind")), failure_labels or [])
            except Exception:
                text = ""
            if text:
                parts.append(text)
        return ("\n\n".join(parts) + "\n\n") if parts else ""

    def _repair_prompt(self, subtask, snap, menu, tree_items, name: str, meta: dict[str, Any], prim: dict[str, Any], reason: str) -> str:
        """Everything a local fix needs: the full source, every outcome of this version with
        its typed label and decision-record id, what the labels usually mean, earlier
        versions' results, and the situation now."""
        source = self.registered.get(name, "")
        log = prim.get("outcome_log") or [{"ok": False, "label": None, "outcome": o, "id": None} for o in prim.get("last_outcomes") or []]
        lines = []
        for o in log:
            tag = o.get("label") or ("OK" if o.get("ok") else "FAILED")
            lines.append(f"- [{tag}] {o.get('outcome')}" + (f" {{{o.get('id')}}}" if o.get("id") else ""))
        labels = collections.Counter(o.get("label") for o in log if o.get("label") and not o.get("ok"))
        hints = "\n".join(f"- {lab} x{n}: {LABEL_HINTS.get(lab, '')}" for lab, n in labels.most_common())
        versions = "\n".join(
            f"- v{v.get('version')}: {v.get('successes', 0)}/{v.get('trials', 0)} successes; labels {v.get('labels') or {}}"
            for v in meta.get("versions") or []
        )
        version = int(meta.get("version") or 1)
        return (
            f"REPAIR the existing action x:{name} (version {version}; kind {meta.get('kind')}; target {meta.get('target')}).\n"
            f"It was written for the step: {subtask.get('description')}\n"
            + self._gap_line(subtask, snap)
            + f"That step's completion check (a VerifySpec): {json.dumps(subtask.get('verify'))}\n"
            f"Its declared purpose: {meta.get('purpose') or 'none'} (achieves {json.dumps(meta.get('purpose_spec'))})\n\n"
            f"Its full source (version {version}):\n```javascript\n{source[:9000]}\n```\n\n"
            "What happened each time this version ran, oldest first ([label] outcome {decision-record id}):\n"
            + ("\n".join(lines) or "- (no outcomes recorded)") + "\n\n"
            + (f"What those failure labels usually mean:\n{hints}\n\n" if hints else "")
            + (f"Earlier versions of this action:\n{versions}\n\n" if versions else "")
            + f"Current menu (id -> what it does): {json.dumps(menu)[:1200]}\n"
            f"Snapshot now: {json.dumps(snap)[:2000]}\n"
            f"Items in the goal tree: {tree_items}\n"
            + (f"Note from the caller: {reason}\n" if reason else "")
            + "\n"
            + self._experience_block(subtask, list(labels))
            + f"Find the root cause in the code from the evidence above and make the SMALLEST change that fixes it. "
            f"Keep the action's name (`// name: {name}`), its job and its PURPOSE unless the evidence shows the "
            "PURPOSE itself is wrong. Answer with the full corrected module.\n\n"
            + CONTRACT
            + "\nPURPOSE PREDICATES (for PURPOSE.achieves; '*_log' style family names are allowed):\n"
            + catalog.describe_catalog() + "\n"
        )

    @staticmethod
    def _craft_target_problem(subtask: dict[str, Any], source: str) -> str | None:
        """For a craft step, every literal name passed to recipesFor must be the step's
        output (or its family). Asking for an input's recipe (planks, when the step
        makes a crafting table) silently crafts the wrong thing."""
        work = subtask.get("work") or {}
        if subtask.get("kind") != "craft" or not work.get("item"):
            return None
        out = str(work["item"])

        def matches(name: str) -> bool:
            if name == out:
                return True
            if out.startswith("*_"):  # family output: any member, or the family itself
                return name == out[2:] or name.endswith("_" + out[2:])
            if name.startswith("*_"):
                return out == name[2:] or out.endswith("_" + name[2:])
            return False

        named = re.findall(r"recipesFor\(\s*[\"'`]([^\"'`]+)[\"'`]", source)
        wrong = [n for n in named if not matches(n)]
        if wrong:
            return (
                f"this step crafts {out!r}, but the action calls recipesFor({wrong[0]!r}). The name passed to "
                f"loop.recipesFor is the OUTPUT the action makes, never an input: use loop.recipesFor({out!r}, table)"
            )
        return None

    @staticmethod
    def _gap_line(subtask: dict[str, Any], snap: dict[str, Any]) -> str:
        """One line saying which inputs of this step are already in hand and which
        output is still missing, so the model builds the step itself rather than
        another action for an input it already has."""
        inv = snap.get("inventory") or {}
        work = subtask.get("work") or {}
        requires = subtask.get("requires") or {}
        parts = []
        for name, need in requires.items():
            have = catalog.count_matching(inv, name)
            parts.append(f"{name} need {int(need)}, have {have} ({'enough' if have >= int(need) else 'MISSING ' + str(int(need) - have)})")
        out = work.get("item")
        if not out and not parts:
            return ""
        line = ""
        if parts:
            line += "Inputs for this step: " + "; ".join(parts) + ". "
        if out:
            want = int(work.get("count") or 1)
            have_out = catalog.count_matching(inv, out)
            line += f"Output still missing: {out} (need {want}, have {have_out}). "
            if parts and all("enough" in p for p in parts):
                line += f"Every input is already in hand: build the action that {work.get('kind', 'makes')}s {out} ITSELF, not one for its inputs. "
        return line + "\n"

    def _not_applicable_feedback(self, res: dict[str, Any], snap: dict[str, Any], subtask: dict[str, Any]) -> str:
        why = f"menu() threw: {res.get('menu_error')}" if res.get("menu_error") else "menu() returned null"
        targets = {k: v for k, v in (snap.get("blocks") or {}).items()}
        return (
            f"it compiled, but {why} for the CURRENT snapshot, so the bot could never choose it. "
            f"Right now: nearest relevant blocks (signed offsets dx,dy,dz) = {json.dumps(targets)[:600]}; "
            f"cursor = {json.dumps(snap.get('cursor'))}; inventory = {json.dumps(snap.get('inventory') or {})}; "
            f"subtask = {subtask.get('description')!r}. "
            f"Live helper results right now: {json.dumps(res.get('probe') or {})[:700]}. "
            "If a target is listed there within range, the helper works and YOUR menu() condition is wrong: note that "
            "snap.blocks keys can be FAMILY keys like '*_log' (never 'oak_log'), and positions in snap.blocks are "
            "offsets, not coordinates. An action must be offerable in situations like this one. "
            "If its real precondition is not met yet (target out of reach, input missing), write the action that "
            "makes progress from HERE instead (for example approach the target with the pathfinder when it is "
            "farther than dig reach), and offer it whenever that step is possible"
        )

    def _attempt_history(self, subtask: dict[str, Any]) -> str:
        """Earlier actions built for this same subtask: their execute() code and what
        happened when the bot ran them. This is the feedback that lets a rebuild learn
        from the last failure instead of repeating it."""
        sid = subtask.get("id")
        stats = {p.get("name"): p for p in ((getattr(self, "_status", None) or {}).get("primitives") or [])}
        blocks = []
        for name, meta in self.meta.items():
            if sid is None or meta.get("subtask_id") != sid or name not in self.registered:
                continue
            p = stats.get(name) or {}
            m = re.search(r"async\s+function\s+execute[\s\S]*", self.registered[name])
            code = (m.group(0) if m else self.registered[name])[:900]
            outcomes = p.get("last_outcomes") or []
            blocks.append(
                f"--- x:{name}: tried {p.get('trials', 0)} time(s), {p.get('successes', 0)} success(es); "
                f"last outcomes: {outcomes or 'never run'}" + chr(10) + code + chr(10)
            )
        if not blocks:
            return ""
        nl = chr(10)
        return (
            "EARLIER ATTEMPTS AT THIS SUBTASK (they did not finish it). Read each outcome, find the root cause in "
            "the code (a hard-coded name that does not match the inventory, a wrong API, a wrong count), and write "
            "an action that avoids it:" + nl + nl.join(blocks[-4:]) + nl
        )

    def _write_and_register(self, subtask, snap, recent, menu, tree_items, kind, target, reason, *, repair_of: str | None = None) -> str | None:
        """Ask GPT for a module, check it, register it, persist it. With ``repair_of`` the
        draft is a new version of that action: it keeps the name, compiles under a
        temporary draft name until it passes every check (so a rejected draft never
        clobbers the registered one), then replaces it with fresh trial counts."""
        if self.llm is None:
            self._say("builder: no LLM handle; cannot write an action")
            self.last_refusal = "no LLM handle; cannot write an action"
            self._log("build_failed", {"subtask": subtask.get("description"), "why": self.last_refusal})
            return None
        prim = {}
        if repair_of is not None:
            prim = next((p for p in (getattr(self, "_status", None) or {}).get("primitives") or [] if p.get("name") == repair_of), {})
            prompt = self._repair_prompt(subtask, snap, menu, tree_items, repair_of, self.meta.get(repair_of) or {}, prim, reason)
            self._say(f"repairing x:{repair_of} (v{int((self.meta.get(repair_of) or {}).get('version') or 1)}, {kind} for {target}) "
                      f"for '{subtask.get('description')}'")
        else:
            prompt = self._prompt(subtask, snap, recent, menu, tree_items, kind, target, reason)
        messages = [
            {"role": "system", "content": "You write small, careful mineflayer primitives."},
            {"role": "user", "content": prompt},
        ]
        error_feedback = None
        for attempt in range(self.max_attempts):
            if error_feedback:
                messages.append(
                    {"role": "user", "content": f"The previous module was rejected: {error_feedback}. Fix it and answer with the full module again."}
                )
            try:
                with timing.timed("llm.builder", attempt=attempt, kind=kind):
                    reply = self.llm.complete(messages)
            except Exception as exc:
                self._say(f"builder: GPT call failed: {exc}")
                self.last_refusal = f"GPT call failed: {type(exc).__name__}: {exc}"[:300]
                self._log("build_failed", {"subtask": subtask.get("description"), "why": self.last_refusal})
                return None
            if hasattr(reply, "content"):
                reply = reply.content
            name, source = extract_module(str(reply))
            if not source:
                error_feedback = "no code block found"
                continue
            if repair_of is not None:
                name = f"{repair_of[:26]}_draft"  # checked under a draft name; the real one is untouched
            else:
                if not name:
                    base = re.sub(r"[^a-z0-9_]", "_", f"{kind}_{target}".lower()).strip("_")[:28] or "action"
                    name = f"gen_{base}"[:32]
                name = self._unique_name(name)
            err = static_check(name, source)
            if not err:
                err = self._craft_target_problem(subtask, source)
            if err:
                error_feedback = err
                self._say(f"builder: '{name}' failed static check ({err})" + ("; retrying" if attempt == 0 else "; giving up"))
                if self.jev is not None and hasattr(self.jev, "log_only"):
                    self.jev.log_only("action_rejected", {"name": name, "why": f"static check: {err}", "source": source})
                continue
            messages.append({"role": "assistant", "content": str(reply)})
            try:
                res = self.bridge.agent_primitive(name, source, purpose=subtask.get("description"))
            except TypeError:  # a bridge stub without the purpose keyword
                res = self.bridge.agent_primitive(name, source)
            except Exception as exc:
                self._say(f"builder: could not post '{name}': {exc}")
                self.last_refusal = f"could not post '{name}' to the bot: {type(exc).__name__}: {exc}"[:300]
                self._log("build_failed", {"subtask": subtask.get("description"), "name": name, "why": self.last_refusal})
                return None
            if res.get("ok") and res.get("purpose_holds_now") is True and attempt < self.max_attempts - 1:
                try:
                    self.bridge.agent_primitive_remove(name)
                except Exception:
                    pass
                error_feedback = (
                    f"it compiled, but its PURPOSE.achieves {json.dumps(res.get('purpose_spec'))} ALREADY HOLDS in the "
                    f"current world while the subtask {subtask.get('description')!r} (check: "
                    f"{json.dumps(subtask.get('verify'))}) is unfinished, so the menu would hide the action at once. "
                    f"Inventory now: {json.dumps(snap.get('inventory') or {})}. Declare what the action brings about "
                    "that is NOT yet true, e.g. the subtask's own target count. "
                    + self._gap_line(subtask, snap)
                )
                if self.jev is not None and hasattr(self.jev, "log_only"):
                    self.jev.log_only("action_rejected", {"name": name, "why": "purpose already holds", "purpose_spec": res.get("purpose_spec"), "source": source})
                self._say(f"builder: '{name}' declares a purpose that already holds; asking for a revision")
                continue
            if res.get("ok") and res.get("offered_now") is False and attempt < self.max_attempts - 1:
                # compiled, but menu() would not offer it in the world as it is now:
                # the bot could never pick it. Withdraw it and tell GPT exactly that.
                try:
                    self.bridge.agent_primitive_remove(name)
                except Exception:
                    pass
                error_feedback = self._not_applicable_feedback(res, snap, subtask)
                if self.jev is not None and hasattr(self.jev, "log_only"):
                    self.jev.log_only("action_rejected", {"name": name, "why": "not applicable now", "probe": res.get("probe"), "menu_error": res.get("menu_error"), "source": source})
                self._say(f"builder: '{name}' compiled but is not applicable right now; asking for a revision")
                continue
            if res.get("ok") and repair_of is not None:
                return self._accept_repair(repair_of, name, source, subtask, prim, reason)
            if res.get("ok"):
                self.registered[name] = source
                self.meta[name] = {
                    "version": 1,
                    "versions": [],
                    "model": self.model_name,
                    "purpose": res.get("purpose"),
                    "purpose_spec": res.get("purpose_spec"),
                    "kind": kind,
                    "target": target,
                    "subtask": subtask.get("description"),
                    "subtask_id": subtask.get("id"),
                    "reason": reason,
                    "created_at": time.time(),
                    "trials": 0,
                    "successes": 0,
                    "failures": 0,
                    "retired": False,
                }
                with open(os.path.join(self.dir, f"{name}.js"), "w", encoding="utf-8") as fh:
                    fh.write(source)
                self._save_sidecar(name)
                self.builds += 1
                if self.jev is not None and hasattr(self.jev, "log_only"):
                    self.jev.log_only("action_generated", {"name": name, "kind": kind, "target": target, "subtask": subtask.get("description"),
                                                           "model": self.model_name, "version": 1, "source": source})
                self._say(f"new action x:{name} registered for '{subtask.get('description')}' (saved to {self.dir}); it is on the menu as a trial")
                return name
            error_feedback = res.get("error") or "rejected by the bot process"
            if self.jev is not None and hasattr(self.jev, "log_only"):
                self.jev.log_only("action_rejected", {"name": name, "why": error_feedback, "source": source})
            self._say(f"builder: Node rejected '{name}': {error_feedback}" + ("; retrying" if attempt == 0 else ""))
        if self.jev is not None and hasattr(self.jev, "log_only"):
            self.jev.log_only("action_generation_failed", {"subtask": subtask.get("description"), "error": error_feedback})
        self.last_refusal = f"all {self.max_attempts} GPT drafts rejected; last: {str(error_feedback)[:160]}"
        return None

    def _accept_repair(self, name: str, draft: str, source: str, subtask: dict[str, Any], prim: dict[str, Any], reason: str) -> str | None:
        """A repair draft passed every check: archive the current version with its record,
        register the new source over the real name with fresh counts, and persist it."""
        try:
            self.bridge.agent_primitive_remove(draft)
        except Exception:
            pass
        meta = self.meta.setdefault(name, {})
        old_version = int(meta.get("version") or 1)
        new_version = old_version + 1
        try:
            res = self.bridge.agent_primitive(name, source, purpose=subtask.get("description"), repair=True, version=new_version)
        except Exception as exc:
            self.last_refusal = f"could not register repaired x:{name}: {type(exc).__name__}: {exc}"[:300]
            self._log("build_failed", {"subtask": subtask.get("description"), "name": name, "why": self.last_refusal})
            return None
        if not res.get("ok"):
            self.last_refusal = f"repaired x:{name} compiled as a draft but Node rejected it: {res.get('error')}"[:300]
            self._log("build_failed", {"subtask": subtask.get("description"), "name": name, "why": self.last_refusal})
            return None
        os.makedirs(self.versions_dir, exist_ok=True)
        old_path = os.path.join(self.versions_dir, f"{name}.v{old_version}.js")
        with open(old_path, "w", encoding="utf-8") as fh:
            fh.write(self.registered.get(name, ""))
        log = prim.get("outcome_log") or []
        labels = dict(collections.Counter(o.get("label") for o in log if o.get("label") and not o.get("ok")))
        base = meta.get("base") or {}
        meta.setdefault("versions", []).append({
            "version": old_version,
            "path": old_path,
            "trials": int(base.get("trials", 0)) + int(prim.get("trials") or 0),
            "successes": int(base.get("successes", 0)) + int(prim.get("successes") or 0),
            "failures": int(base.get("failures", 0)) + int(prim.get("failures") or 0),
            "labels": labels,
            "evidence": [o.get("id") for o in log if o.get("id")],
            "model": meta.get("model"),
        })
        meta.update({
            "version": new_version, "trials": 0, "successes": 0, "failures": 0, "retired": False,
            "base": {"trials": 0, "successes": 0, "failures": 0}, "created_at": time.time(),
            "purpose": res.get("purpose") or meta.get("purpose"), "purpose_spec": res.get("purpose_spec") or meta.get("purpose_spec"),
            "model": self.model_name, "repair_reason": reason or None,
        })
        self.registered[name] = source
        with open(os.path.join(self.dir, f"{name}.js"), "w", encoding="utf-8") as fh:
            fh.write(source)
        self._save_sidecar(name)
        self.builds += 1
        self._log("action_repaired", {"name": name, "version": new_version, "labels": labels, "subtask": subtask.get("description"),
                                      "model": self.model_name, "source": source})
        self._say(f"repaired x:{name}: version {new_version} replaces v{old_version} (failures {labels or 'unlabelled'})")
        return name

    # ------------------------------------------------------------------ #

    def _save_sidecar(self, name: str) -> None:
        try:
            with open(os.path.join(self.dir, f"{name}.json"), "w", encoding="utf-8") as fh:
                json.dump(self.meta.get(name, {}), fh, indent=2)
        except Exception as exc:
            self._say(f"builder: sidecar write failed for {name}: {exc}")

    def _log(self, gate: str, record: dict[str, Any]) -> None:
        """Append an audit record through the Jev client's jsonl log (no-op without one)."""
        if self.jev is not None and hasattr(self.jev, "log_only"):
            try:
                self.jev.log_only(gate, record)
            except Exception:
                pass

    def _say(self, text: str) -> None:
        self.log.append(text)
        print(f"\033[36m[builder] {text}\033[0m")
