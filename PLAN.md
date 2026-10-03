# Plan: goal tree + Jev action loop + action builder

Rebuild the agent around three systems: a **decomposer** that turns a GPT goal
into a tree of verifiable subtasks, a **Jev action loop** that picks one
low-level action at a time from a menu, and an **action builder** that grows
the menu when Jev says the current menu cannot finish the subtask. Everything
below replaces the fast-loop / milestone ladder design on this branch.

Constraint carried through every section: **Jev never sees code.** Every menu
line carries live facts, every state field is a plain observed value, and
code owns the workflow.

---

## 0. Keep / replace / delete

All edits go under `voyager/`. `upgrade-movement/` and
`.claude/worktrees/update-movement/` are stale copies and are not touched.

| Path | Fate | Notes |
| --- | --- | --- |
| `voyager/env/mineflayer/lib/fastLoop.js` | **delete** | 2.7k lines of bounded primitives + menu folding; replaced by `agentLoop.js` |
| `voyager/env/mineflayer/lib/goalPlanner.js` | **delete** | recipe planning moves to Python `decomposer.py` |
| `voyager/fast/brain.py`, `milestones.py`, `skills.py` | **delete** | replaced by `voyager/tree/` |
| `voyager/env/mineflayer/scripts/fastloop_dryrun.js` | **delete** | replaced by `agentloop_dryrun.js` |
| `tests/test_fast.py` | **delete** | replaced by `tests/test_tree.py` and `scripts/verify_test.js` |
| `voyager/env/mineflayer/lib/primitiveRegistry.js` | **keep, edit** | same `menu(snap, loop)` / `execute(bot, loop, ctx)` contract; `snap` schema changes (section 2) |
| `voyager/env/mineflayer/lib/mcKnowledge.js` | **keep** | `ORE_DROPS`, `SMELTABLE`, `FUELS`, `MOB_DROPS`, `NATURAL_BLOCK_RE` feed the decomposer; exported as JSON via `GET /knowledge` so Python reads the same tables |
| `voyager/env/mineflayer/lib/jevTraversal.js` | **keep client bootstrap only** | `getClient`, `choiceFn`, `noulFn`, `nearbyEntitiesOf`; drop the traversal survey and `jevAsk` |
| `voyager/env/mineflayer/index.js` | **keep skeleton** | `/start`, `/stop`, `/pause`, express setup; replace `/fast/*` routes with `/agent/*` (section 7) |
| `voyager/env/bridge.py` | **keep, edit** | same HTTP shape; rename `fast_*` methods to `agent_*` |
| `voyager/typesafe/client.py` | **keep** | `JevClient.ask` with jsonl audit logs is used by the decomposer and builder |
| `voyager/typesafe/gates.py`, `state.py` | **delete** | critic shadow / QA rank belong to the old GPT-program loop |
| `voyager/fast/generator.py` | **replace** | keep static checks + post/retry/persist skeleton; new prompt and new trigger (section 6) |
| `voyager/agents/*`, `voyager/prompts/*`, `voyager.py` | **slim** | keep `Voyager` env lifecycle + `curriculum_agent` for the high goal; drop action/critic/skill agents |
| `run_fast.py`, `scripts/run_bots.py` | **replace** with `run_tree.py` | single bot; multi-bot deferred (section 10) |

New files:

```
voyager/tree/__init__.py
voyager/tree/schema.py        # Node, VerifySpec dataclasses + JSON (section 1)
voyager/tree/catalog.py       # verification predicate catalog, Python side (section 3)
voyager/tree/decomposer.py    # goal -> tree (section 4)
voyager/tree/traversal.py     # post-order runner, done flags, stuck routing (sections 5, 8)
voyager/tree/builder.py       # action builder (section 6)
voyager/tree/llm.py           # thin OpenAI wrapper (section 10)
voyager/env/mineflayer/lib/agentLoop.js     # observation, menu, Jev decision, execution
voyager/env/mineflayer/lib/observe.js       # snapshot (section 2)
voyager/env/mineflayer/lib/verify.js        # predicate implementations (section 3)
voyager/env/mineflayer/lib/baseActions.js   # move:* / look:* (section 2)
voyager/env/mineflayer/scripts/agentloop_dryrun.js
voyager/env/mineflayer/scripts/verify_test.js
tests/test_tree.py
```

---

## 1. Data schemas

Everything else hangs off these. Written as JSON so Node and Python share them.

### 1.1 Tree node

```json
{
  "id": "n3",
  "parent": "n1",
  "children": ["n4", "n5"],
  "description": "obtain a stone pickaxe",
  "kind": "craft | mine | smelt | kill | place | locate | goal",
  "verify": { "...VerifySpec..." },
  "requires": { "cobblestone": 3, "stick": 2 },
  "acquire": null,
  "work": { "kind": "craft", "item": "stone_pickaxe", "count": 1 },
  "done": false,
  "done_at": null,
  "attempts": 0,
  "actions_spent": 0,
  "budget_actions": 60,
  "failed": false,
  "trace": []
}
```

* `requires` are the raw per-recipe inputs for this node (never aggregated).
* `acquire` is set only on **leaf** nodes: the total demand for that resource
  summed across the whole tree. The leaf's `verify` checks `acquire`.
* `failed` is set by traversal when re-decomposition of the node fails; the
  whole parent chain is marked too (section 8).
* `work` is what the node itself does after its children are done. Leaves
  have `work.kind` in `mine | kill | collect | locate`; internal nodes have
  `craft | smelt | place`.
* `done` is **persisted** the moment `verify` passes. It is never re-evaluated,
  so consumption by a later craft cannot flip it back.
* `trace` is the action list that completed the node: the seed of a
  replayable skill.

### 1.2 VerifySpec

A JSON expression over catalog predicates, composable with `all` / `any` / `not`.

```json
{ "all": [
  { "has_item": { "name": "cobblestone", "count": 3 } },
  { "near_block": { "name": "crafting_table", "radius": 4 } }
] }
```

Predicate names and params are fixed (section 3). GPT selects and fills them;
it does not write code for the common case.

### 1.3 Snapshot (`snap`)

Produced by `observe.js` each decision. Sent to Jev as `state` and handed to
primitive `menu()` functions.

```json
{
  "bot": { "position": {"x":0,"y":64,"z":0}, "yaw": "north", "pitch": 0,
           "health": 20, "food": 17, "oxygen": 20, "xp": 0,
           "inWater": false, "inLava": false, "onGround": true,
           "timeOfDay": 2000, "daylight": "day", "biome": "plains" },
  "cursor": { "block": "oak_log", "distance": 3, "entity": null },
  "inventory": { "oak_log": 2, "stick": 4 },
  "slots": { "hand": "wooden_pickaxe", "offhand": null, "head": null, "chest": null, "legs": null, "feet": null },
  "blocks": { "oak_log": {"dx":2,"dy":1,"dz":-10,"count":6}, "stone": {"dx":0,"dy":-2,"dz":0,"count":40} },
  "entities": [ {"name":"cow","distance":14,"direction":"north"} ],
  "dropped": [ {"name":"oak_log","distance":3} ],
  "placed_by_me": { "crafting_table": {"x":1,"y":64,"z":1} },
  "subtask": { "id":"n4", "description":"obtain 3 cobblestone", "verify": {}, "progress": {"have":1,"need":3} },
  "recent": [ {"action":"move:north","outcome":"ok moved 4","delta":{"pos":4}},
              {"action":"x:mine_cursor","outcome":"ok +1 cobblestone","delta":{"inv":{"cobblestone":1}}} ],
  "since_progress": { "actions": 3, "seconds": 11 }
}
```

The Jev request carries this snapshot plus two fields the loop adds:
`menu` (id to description) and `proven_menu` (the ids excluding untested
trial actions), so the `completable` Noul can be asked over the proven menu
only.

Context-rot caps: `blocks` holds the nearest instance per type within radius
16, limited to an **allowed set** plus hazards (`lava`, `water`, `fire`,
`cactus`) plus the cursor block. The allowed set is built in Python from the
tree's leaf items through the inverse of `ORE_DROPS` (cobblestone needs
`stone` visible) and the `FAMILIES` table (`oak_log` needs any `*_log`
visible), and family matches are collapsed to one entry keyed by the family
(`*_log`). `entities` within 24, nearest 8. `recent` is the last 8 actions.
No absolute block positions except `placed_by_me`. Primitives may add
fields under `snap.extra` (section 1.5), capped at 400 characters each.

### 1.4 Action record

```json
{ "action": "move:north", "outcome": "ok moved 4 blocks", "ms": 1200,
  "delta": { "pos": 4, "inv": {}, "health": 0 } }
```

### 1.5 Primitive module contract

Unchanged from `primitiveRegistry.js`: `function menu(snap, loop)` returns a
one-line string with live facts or `null`; `async function execute(bot, loop, ctx)`
returns `"ok ..."` or `"failed: ..."`. `ctx` carries `snap`, `subtask`, and
`tree_items` (all item names in the tree). `loop` helpers shrink to
`mcData`, `nearestBlocks`, `nearestEntity`, `freeSpotNear`, `inventoryCounts`,
`recordPlacement(pos, name)`.

New optional third function: `function observe(bot, loop)` (synchronous)
returns a small plain object that is merged into `snap.extra.<name>`. This is
how new info-adding actions extend the state Jev sees (a light-level reader,
a "blocks under me" probe). The static check allows it, the registry calls it
inside a try/catch each snapshot, and the builder prompt says GPT may add one
when the action needs a fact the snapshot lacks.

---

## 2. Observation and the base menu

### 2.1 `observe.js`

* Position, yaw as a compass word, pitch, health/food/oxygen/xp, water/lava.
* `cursor` from `bot.blockAtCursor(6)` and `bot.entityAtCursor(6)`.
* `blocks` via one `bot.findBlocks` call per allowed type (allowed set passed
  down from Python with the subtask).
* `placed_by_me` is recorded by `loop.recordPlacement`, which every place
  action must call; this is what `block_placed_by_me` verifies against.
* Inventory counts and the six named slots.

### 2.2 Base actions (`baseActions.js`)

The starting menu is movement and looking only. The point is to prove the
builder can add mining, crafting and placing itself.

| Menu id | Effect |
| --- | --- |
| `move:north / south / east / west` | walk up to 4 blocks that way; one jump allowed for a 1-block step up; stops at a water or lava edge; outcome reports blocks moved |
| `move:up` | jump once (reports if nothing changed) |
| `look:north / south / east / west` | set yaw, pitch 0 (mineflayer yaw 0 faces north; `snap.bot.yaw` is reported as a compass word) |
| `look:up / down` | set pitch +60 / -60 degrees |
| `look:at:<block>` | offered per visible block type in `snap.blocks`; `bot.lookAt` the nearest one (this is the bridge from "see a tree" to "mine the cursor block") |
| `wait` | 1 second |

Movement uses `bot.setControlState` with a short timeout, not pathfinder, so
the agent genuinely has to learn to route. Decision: if the in-game acceptance run shows the
agent cannot cross plains terrain this way, swap the body of `move:*` to a
4-block `GoalXZ` pathfinder step while keeping the menu id identical. Jev's
interface does not change either way.

### 2.3 Cadence

The loop decides after each action resolves, with a floor of 500 ms between
decisions. There is no per-tick decision.

---

## 3. Verification catalog

Predicates are fixed Node functions in `verify.js`, mirrored by a Python
table in `catalog.py` that lists name, params and a one-line meaning (for
GPT and Jev prompts). GPT emits a VerifySpec, never code. Raw GPT
verification code is a fallback only, loaded through the same sandbox as
primitives, and only when Jev says no catalog predicate fits.

| Predicate | Params | Reads |
| --- | --- | --- |
| `has_item` | name, count | inventory count >= count |
| `has_equipped` | name, slot | `bot.inventory.slots` by slot name |
| `has_tool_tier` | tool, material | holds tool of material or better (`PICKAXE_TIER`) |
| `near_block` | name, radius | `bot.findBlock` |
| `block_placed_by_me` | name | `placed_by_me` record still present in world |
| `near_entity` | name, radius | entity scan |
| `in_biome` | name | `bot.blockAt(pos).biome.name` |
| `at_y_level` | min, max | position |
| `position_within` | x, z, radius | position |
| `status_at_least` | field (health/food/xp), min | bot fields |
| `time_of_day_in` | min, max | `bot.time.timeOfDay` |
| `is_sheltered` | none | solid block overhead and at most one open side |
| `container_contains` | pos, name, count | opens container; cached result |
| `block_count_in_region` | name, region, count | voxel count |
| `all` / `any` / `not` | specs | combinators |

Dropped on purpose: `near_structure` (not verifiable client-side; use
`near_block(bell)` or `near_entity(villager)`).

That is 14 predicates plus the three combinators. Node exposes
`POST /agent/verify {spec}` returning `{ok, detail}` so Python
can check any node on demand, and the loop checks the current subtask's spec
after every action.

---

## 4. Decomposer

`decomposer.py`. Input: goal text from GPT. Output: a tree (section 1.1)
saved to `<ckpt>/tree/<goal_id>.json`.

### 4.1 Steps

1. **Goal to plan.** GPT reads the goal into a fixed-shape plan:
   `{items: [{item, count}], final: null | {kind, item, slot, verify}, hints: [..]}`.
   `items` are what the goal needs the bot to hold; `final` is the last step after
   obtaining them (equip, place, eat, use, locate, status, build, other) with its
   completion check from the predicate catalog; `hints` are facts recipes cannot
   express (iron ore is underground). Code validates every part: item ids against
   the game knowledge, the check against the catalog, and that the check is not
   already true before the items exist. Problems go back to GPT for a corrected
   plan (up to 3 tries). When GPT says holding the items is enough, Jev
   cross-checks that one judgment ("does holding them finish the goal?") and a no
   sends GPT back for the final step.
   Tree shape: one item and no final step -> that item's node is the root (as
   before). A final step -> a root node of that kind (equip/place/eat/use are work
   steps the builder writes for; others are plain goal nodes) with each item's
   recipe tree beneath it. Several items, no final step -> a goal node checking all
   of them. A root final step is never marked done before its item subtree.
2. **Recipe expansion** (`minecraft-data` `recipes` by result id, pulled once
   from Node via `GET /knowledge`):
   * pick the recipe whose inputs are cheapest by a fixed cost table
     (natural blocks 1, mob drops 3, smelted 4, crafted = sum of inputs);
   * each input becomes a child with `requires`;
   * a 3x3 recipe adds a sibling precondition node
     `near_block(crafting_table, 4)` whose own subtree is "obtain
     crafting_table" then `place`;
   * `SMELTABLE[item]` adds `near_block(furnace)` and a fuel node from
     `FUELS` (coal first, planks fallback);
   * `blocks[name].harvestTools` adds a tool node (`has_tool_tier`) when the
     block needs one.
3. **Leaves.** Stop when the item is a natural block drop (`ORE_DROPS` /
   `NATURAL_BLOCK_RE`), a mob drop (`MOB_DROPS`), or collectable
   (sapling, apple). Leaf `work.kind` follows the source.
4. **Cycle guard.** Visited set keyed by item; an item already on the path
   is not expanded again (ingot to block to ingot).
5. **Aggregation (as built).** There is **one node per item** in a tree,
   sized for whole-tree demand, not just one per leaf. The build iterates
   to a demand fixed point: a planks node needed by both the sticks and the
   crafting table is a single node whose count covers both consumers, placed
   under the **first-visited** consumer in the deterministic child order used
   by traversal (preconditions first, then recipe input order). Craft and
   smelt nodes verify their own output at the total count; gather leaves
   carry `acquire`. Per-node `requires` stay raw. This went beyond the
   leaf-only merge in the first draft because a separate planks node per
   consumer is already satisfied by planks the earlier consumer has not used
   yet, and the tree ends one craft short.
6. **VerifySpec generation.** For item nodes the spec is derived in code
   (`has_item(name, acquire or count)`). For non-item nodes Jev Choice picks
   the predicate family (`has`, `near`, `status`, `location`), then GPT fills
   params from the catalog table with the choice as a hint. The spec is
   validated against the catalog before the node is saved.
7. **Descriptions.** Code-generated ("obtain 3 cobblestone", "craft 1
   stone_pickaxe"); GPT is not asked.

### 4.2 Done-ness at build time

Before saving, every node's spec is checked once via `/agent/verify`. Nodes
already satisfied get `done = true` so the agent does not redo them.

---

## 5. Traversal and the Jev loop

`traversal.py` runs the tree; `agentLoop.js` runs one subtask.

### 5.1 Order

Post-order depth-first. For each node: run children in order, then run the
node's own `work` step with the node as the current subtask. A node is
skipped if `done`. After the work step the node's spec is checked; on pass
`done` is persisted and the parent is notified.

### 5.2 One decision request

Python posts the subtask (`POST /agent/subtask`) with its description,
VerifySpec, allowed block types, and budget. Node then loops:

```
snap = observe()
menu = baseActions.menu(snap) + registry.menuEntries(snap)
answers = jev.systemOne({ state: snap, questions })
run action; append action record; check verify
```

Questions in one request:

| id | primitive | asks |
| --- | --- | --- |
| `action` | Choice over menu | which single action makes the most progress on `subtask.description` now |
| `danger` | Noul | immediate physical danger that must be handled first |
| `progress` | Noul | judging from `recent` and `since_progress`, is the agent making progress |
| `completable` | Noul | can `subtask.description` be completed using only the actions on the menu |

Code reads `completable` on every decision but acts on it only every 10
actions: a `no` at that checkpoint fires the builder. A build cooldown
prevents a second build while a trial action from the last build is still
untested. `progress` is used only by stuck routing (section 8). Confidence
below 0.2 on `action` falls back to the least recently used movement action.

### 5.3 Done

On verify pass: push trigger `subtask_done` with the action trace. Python
records it on the node and moves to the next node.

---

## 6. Action builder

`builder.py`, replacing `generator.py`. Triggered by `completable` low (5.2) or
by stuck routing (section 8).

1. **Kind** (Jev Choice over `craft | smelt | mine | place | use_block |
   equip | collect | attack | goto_nearest | other`) given the subtask, the
   menu, and `recent`.
2. **Target item/block** (Jev Choice) over the union of items in the tree,
   `snap.blocks`, and `snap.inventory`. Includes `none` for untargeted kinds.
3. **GPT writes the module** using the primitive contract, the chosen kind and
   target, the snapshot schema, and the outcome tallies. Prompt reuses the
   structure of `generator.py`'s `CONTRACT` with the new `snap` fields.
   Actions should be **templates**: `mine_cursor` (mine whatever block is at
   the cursor) rather than `mine_oak_log`, so the menu does not explode. GPT
   is told this and shown one template example.
4. **Static check** (existing `static_check`), post to `/agent/primitive`,
   retry once with Node's error.
5. **Trial.** A new primitive is marked `trial` and shown on the menu with a
   `[new action, untested]` note. It counts as part of the menu for the
   `completable` question only after its first `ok`; three failures with no
   success retires it (existing logic).
6. **Persist** under `<ckpt>/primitives/<name>.js` with a sidecar JSON
   (`kind`, `target`, `subtask`, `successes`). This directory is the skill
   library and is reloaded on resume.

Code is never shown to Jev. The menu line a primitive writes is the whole
interface.

---

## 7. Node HTTP surface

These shapes are **frozen** for the parallel build (section 9). Node
implements them, Python consumes them, and neither side changes a field
without updating this table.

| Route | Body | Returns |
| --- | --- | --- |
| `GET /knowledge` | | see below |
| `POST /agent/subtask` | `{id, description, verify, allowed_blocks: [..], budget_actions, kind, work, requires, tree_items: [..]}` | `{ok}` |
| `GET /agent/status` | | see below |
| `POST /agent/verify` | `{spec}` | `{ok: bool, detail: string}` |
| `POST /agent/primitive` | `{name, source}` | `{ok: bool, error: string|null}` |
| `POST /agent/stop` | | `{ok}`; loop idles, no triggers raised |

`GET /knowledge` returns:

```json
{
  "version": "1.19",
  "recipes": { "stone_pickaxe": [ { "inputs": {"cobblestone":3, "stick":2}, "count": 1, "shape": "3x3" } ] },
  "harvest_tools": { "stone": ["wooden_pickaxe","stone_pickaxe","iron_pickaxe","diamond_pickaxe","netherite_pickaxe"] },
  "ore_drops": {}, "smeltable": {}, "fuels": [], "mob_drops": {}, "families": {},
  "natural_block_re": "<source of NATURAL_BLOCK_RE>"
}
```

`recipes` is keyed by result item name; each entry lists inputs as name to
count, the result count, and `shape` as `"2x2"` or `"3x3"` (3x3 means a
crafting table is required). `harvest_tools` lists item names, lowest tier
first, only for blocks that need a tool.

`GET /agent/status` returns:

```json
{
  "snap": { "...section 1.3..." },
  "triggers": [ { "type": "subtask_done", "subtask_id": "n4", "trace": [], "at": 0 } ],
  "recent": [ "...section 1.4..." ],
  "primitives": [ { "name": "mine_cursor", "trials": 2, "successes": 1, "failures": 1, "retired": false, "trial": false } ],
  "menu": { "move:north": "walk up to 4 blocks north (grass ahead)", "x:mine_cursor": "..." },
  "last_decision": { "action": "move:north", "confidence": 0.61, "danger": 0.02, "progress": 0.7, "completable": 0.2 },
  "subtask": { "id": "n4", "actions_spent": 12 },
  "stats": { "decisions": 40, "jev_ms": 12000 }
}
```

Re-posting the **same `id`** is a retry: the loop resets the budget and
counters but keeps `recent` and the action cooldowns so Jev sees what
already failed on this subtask.

Triggers are drained on each status read. Types and detail fields:

| type | detail |
| --- | --- |
| `subtask_done` | `subtask_id`, `trace` |
| `builder_wanted` | `subtask_id`, `completable` (float), `menu` (ids) |
| `stuck` | `subtask_id`, `kind` in `position / inventory / repeat / health / progress` |
| `budget_exhausted` | `subtask_id`, `actions_spent` |
| `danger` | `health`, `threat` |

---

## 8. Stuck detection and routing

Hard triggers in Node (cheap, every action):

* position unchanged over 10 actions
* inventory unchanged over 10 actions **while the subtask is an item node**
* same action id chosen 5 times in a row
* health dropped 6 or more in the last 5 actions with no attack-kind action on the menu

Soft trigger: `progress` Noul below 0.3 on three consecutive decisions.

Routing in `traversal.py`:

| Signal | Route |
| --- | --- |
| `completable` no at the 10-action checkpoint | **builder** (section 6), subject to the build cooldown |
| `completable` high + progress low, hard trigger fired | **retry**: same subtask, `recent` kept in state, budget halved, failed action ids cooled down for 4 decisions |
| budget exhausted twice | **re-decompose** this node only (different recipe, or a `locate` child inserted) |
| re-decompose fails | mark node `failed`, fail parent chain, ask GPT for a new high goal |

---

## 9. Work packages: three parallel agents, then a final pass

The build is split by **file ownership**. No two packages write the same
file. Every cross-package dependency is one of the frozen contracts:
section 1 (schemas), section 3 (predicate table), section 7 (HTTP shapes
and triggers), and section 1.5 (primitive module contract). An agent that
needs a contract change stops and reports it instead of changing the file.

Each package must be testable **without Minecraft and without API keys**.
Node tests are plain `node scripts/<name>.js` scripts over a fake bot;
Python tests go through `tests/run_tests.py` with the venv python. Python
packages talk to Node only through `bridge.py` methods, so they mock the
bridge in tests.

### Package A: Node runtime (`voyager/env/mineflayer/`)

Owns: `lib/observe.js`, `lib/baseActions.js`, `lib/verify.js`,
`lib/agentLoop.js`, `lib/primitiveRegistry.js` (edit), `lib/jevTraversal.js`
(trim), `index.js` (routes), `scripts/agentloop_dryrun.js`,
`scripts/verify_test.js`. Deletes `lib/fastLoop.js`, `lib/goalPlanner.js`,
`scripts/fastloop_dryrun.js`.

Builds:
1. `observe.js` producing the exact snapshot in 1.3, with the allowed-block
   set and family collapsing, cursor from `blockAtCursor`/`entityAtCursor`,
   `placed_by_me` via `loop.recordPlacement`, and `snap.extra` from primitive
   `observe()` functions.
2. `baseActions.js`: the menu table in 2.2 with control-state movement and
   outcomes that report blocks moved.
3. `verify.js`: the 14 predicates and 3 combinators in section 3, one function
   `evaluate(bot, loop, spec) -> {ok, detail}`, and a spec validator that
   rejects unknown predicates or params.
4. `agentLoop.js`: subtask intake, decision loop with the four questions in
   5.2, the completable checkpoint every 10 actions with build cooldown, the
   hard stuck triggers in section 8, per-action verify, action records with
   deltas, and the status/trigger surface in section 7.
5. `primitiveRegistry.js`: add optional `observe`, the `trial` flag, and
   `ctx.tree_items`; keep static checks and retirement.
6. `index.js`: replace `/fast/*` with `/agent/*` and add `GET /knowledge`
   built from `minecraft-data` recipes plus `mcKnowledge.js`.
7. `jevTraversal.js`: keep only `getClient`, `choiceFn`, `noulFn`,
   `nearbyEntitiesOf`, and the compass helpers.

Done when: `node scripts/verify_test.js` passes every predicate against the
fake bot; `node scripts/agentloop_dryrun.js` builds a snapshot from the
canned plains world, prints a movement-only menu, and (with
`TYPESAFE_API_KEY`) completes one real Jev decision; `GET /knowledge` output
for `wooden_pickaxe` matches the shape in section 7.

### Package B: tree core (`voyager/tree/` data side, `tests/`)

Owns: `voyager/tree/__init__.py`, `schema.py`, `catalog.py`,
`decomposer.py`, `llm.py`, `tests/test_tree.py`, `tests/run_tests.py`
(edit module list), `tests/fixtures/knowledge_1_19.json`. Deletes
`tests/test_fast.py`.

Builds:
1. `schema.py`: `Node` and `VerifySpec` dataclasses with `to_json` /
   `from_json` matching 1.1 and 1.2, tree save/load under
   `<ckpt>/tree/<goal_id>.json`, and helpers `leaves()`, `post_order()`,
   `items()`, `allowed_blocks(knowledge)`.
2. `catalog.py`: the predicate table from section 3 as data (name, params,
   types, one-line meaning), a Python validator identical in behaviour to
   the Node one, and `item_spec(name, count)` / `near_spec(block, radius)`
   constructors.
3. `llm.py`: thin wrapper over the `openai` package with `complete(messages)`
   and `complete_json(messages, schema_hint)`; reads `OPENAI_API_KEY`; model
   name from `OPENAI_MODEL` with a sensible default.
4. `decomposer.py`: `Decomposer(knowledge, jev, llm).build(goal_text) -> Tree`
   implementing 4.1 steps 1 to 7, with `jev` and `llm` injectable so tests
   run with stubs. Also the method `Decomposer.redecompose(tree, node_id)
   -> bool` for section 8 (choose the next-cheapest recipe or insert a
   `locate` child); traversal takes it as an injected callable.
5. `tests/fixtures/knowledge_1_19.json`: a captured `GET /knowledge` payload
   covering wooden and stone tools, planks, sticks, crafting table, furnace,
   torches, iron ingot. Hand-written against the shape in section 7 until
   Package A can produce the real one.

Done when `tests/run_tests.py` passes with: wooden and stone pickaxe trees
with crafting_table precondition nodes; merged log leaf under the
first-visited consumer with aggregated `acquire`; furnace plus fuel nodes
for iron_ingot; cycle guard on iron_block; done flags set from a fake
inventory; a non-item goal producing a single root node with a validated
spec from stubbed Jev and GPT.

### Package C: orchestration (`voyager/tree/` runtime side, entrypoints)

Owns: `voyager/tree/traversal.py`, `voyager/tree/builder.py`,
`voyager/env/bridge.py` (edit), `run_tree.py`, `voyager/voyager.py` (slim),
`voyager/agents/` (slim to curriculum), `tests/test_traversal.py`,
`tests/test_builder.py`. Deletes `voyager/fast/`, `voyager/typesafe/gates.py`,
`voyager/typesafe/state.py`, `run_fast.py`, `scripts/run_bots.py`,
`scripts/check_typesafe_gates.py`, `scripts/check_typesafe_state.py`,
`scripts/typesafe_agreement.py`, `scripts/skill_tree.py`.

Builds:
1. `bridge.py`: `knowledge()`, `agent_subtask(node)`, `agent_status()`,
   `agent_verify(spec)`, `agent_primitive(name, source)`, `agent_stop()`
   against section 7; remove `fast_*` and `/step` program execution.
2. `traversal.py`: `Traversal(tree, bridge, jev, builder).run()` doing
   post-order in 5.1, polling status, persisting `done` and `trace`, and the
   routing table in section 8 (builder, retry with halved budget and action
   cooldown, re-decompose via Package B's `redecompose`, fail chain and new
   goal).
3. `builder.py`: steps 1 to 6 of section 6. Port `static_check` and
   `extract_module` from `generator.py`, write the new prompt around the 1.3
   snapshot and the template rule, and persist under `<ckpt>/primitives/`
   with sidecar JSON and reload on resume.
4. `run_tree.py`: start the env, fetch knowledge, propose a goal through the
   curriculum agent, decompose, traverse, repeat; `--resume <ckpt>` reloads
   the tree and primitives.
5. Slim `voyager.py` to env lifecycle plus `curriculum_agent` and `jev`;
   delete action, critic and skill agents and their prompts.

Done when `tests/run_tests.py` passes with a **fake bridge** that replays
scripted status responses: traversal completes a three-node tree in
post-order; a `completable` low trigger calls the builder once and respects
the cooldown; a stuck trigger halves the budget and retries; two budget
exhaustions call `redecompose`; a resumed tree skips done nodes. Builder
tests cover static check rejection, Node error retry, and persistence.
`python run_tree.py --help` runs.

### Final pass: cohesiveness review and integration

One agent, after A, B and C finish, with the whole tree in view.

1. **Contract audit.** Diff every field name in `observe.js` against 1.3,
   `verify.js` against `catalog.py`, `index.js` routes against `bridge.py`,
   trigger names in `agentLoop.js` against `traversal.py`. Fix mismatches on
   the side that deviates from this document.
2. **Dead code sweep.** Grep for `fast`, `fastLoop`, `goalPlanner`,
   `milestone`, `critic`, `qa_rank`, `langchain` outside the curriculum
   agent; remove leftovers in `voyager/__init__.py`, `setup.py`,
   `requirements.txt`, `README.md` references.
3. **Fixture refresh.** Run `GET /knowledge` from the real Node process (no
   Minecraft login needed if `index.js` can serve it before `/start`; else
   generate it with a small node script) and replace the hand-written
   fixture; rerun Package B tests.
4. **Offline end to end.** Run `tests/run_tests.py` and both node scripts.
   Then run `run_tree.py` against a Node process in dryrun mode (Package A
   adds a `--fake-world` flag to `index.js` that serves the canned bot) and
   confirm a wooden pickaxe tree is built, posted node by node, and that a
   `completable` low response from a stubbed Jev reaches the builder.
5. **In-game acceptance** (needs the user's server and keys): from an empty
   inventory in plains, goal "obtain a wooden pickaxe". Pass criteria: the
   builder produces a mine-at-cursor action before any other, then a craft
   action; the tree reaches `done` with every node verified; a run killed
   mid-tree resumes without redoing done nodes or regenerating primitives.
6. **Report** listing contract deviations fixed, anything left stubbed, and
   the in-game result with the log paths.

### Ordering

A, B and C start together. C codes against the frozen contracts with a fake
bridge and does not wait for A. B's fixture is hand-written and replaced in
the final pass. The final pass starts only when all three report done.

---

## 10. Open decisions (recommendation first)

* **GPT handle.** Thin wrapper over the `openai` package in `tree/llm.py`
  with a JSON-mode helper. Drop langchain for new code; `curriculum_agent`
  keeps its handle until Package C replaces it with one `propose_goal` call.
* **Multi-bot.** Drop from `run_tree.py` for now. `bridge.py` keeps
  `server_port` so it can return later.
* **Movement implementation.** Start with raw control states (2.2). Switch to
  a 4-block pathfinder step only if the acceptance run fails on plain terrain.
* **Verification fallback code.** Allowed, sandboxed, logged as
  `verify_code_generated`; expect it to be rare and treat each use as a
  catalog gap to fill.
* **minecraft-data version.** Pin to the server's version through
  `bot.version`, as today; Python never loads minecraft-data itself.

---

## 11. Final pass status (2026-10-02)

Packages A, B and C landed and were integrated. Offline: 32 Python tests
pass (`venv/Scripts/python tests/run_tests.py`), `verify_test.js` and
`agentloop_dryrun.js` pass, and `run_tree.py` against `FAKE_WORLD=1`
builds the wooden pickaxe tree, posts nodes in post-order, exercises the
builder (with keys) or the retry / re-decompose / fail-chain routes (with
`--no-jev`). The knowledge fixture is now real `GET /knowledge` output for
1.19. Still unverified: in-game movement with raw control states, and the
first live GPT-built mine action. Acceptance commands are in README "Run".
