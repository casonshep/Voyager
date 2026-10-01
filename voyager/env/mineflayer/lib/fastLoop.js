// Fast loop: a goal-driven controller that runs inside the mineflayer
// process so the bot never waits for Python between actions.
//
//   Python (slow brain)                    Node (fast loop)
//   ------------------                     ----------------
//   POST /fast/goal {text, ...}   ---->    parse target, start loop
//                                          loop: snapshot -> triggers ->
//                                                menu -> Jev picks one bounded
//                                                primitive -> execute -> repeat
//   GET  /fast/status             <----    drained triggers, observations,
//                                          trace of (fingerprint, action, gain)
//
// Jev (TypeSafe's System One model) selects from a small menu of primitives
// that code builds from the live world state; code owns the exact checks
// (target counts, timeouts, hazards) and the execution of every primitive.
// If Jev is unavailable the loop falls back to a plain heuristic policy.
const fs = require("fs");
const path = require("path");
const { Vec3 } = require("vec3");
const { getSurroundingBlocks } = require("./observation/voxels");
const traversal = require("./jevTraversal");

const DECISION_TIMEOUT_MS = 4000;
const WALK_DISTANCE = 12;
const WALK_TIMEOUT_MS = 9000;
const MINE_TIMEOUT_MS = 30000;
const CRAFT_TIMEOUT_MS = 20000;
const ATTACK_TIMEOUT_S = 20;
// No-progress fires on whichever comes first: wall clock or a run of
// actions without a target gain (decisions are fast, so count them too).
const NO_PROGRESS_DEFAULT_S = 30;
const NO_PROGRESS_DEFAULT_ACTIONS = 5;
const MAX_SUBGOAL_CANDIDATES = 8;
const STUCK_WINDOW_ACTIONS = 4;
const STUCK_DISTANCE = 1.5;
const RECENT_ACTIONS = 5;
const RESOURCE_SCAN_DISTANCE = 24;
const MAX_MENU_MINE = 5;
const MAX_MENU_CRAFT = 4;
const MAX_MENU_ATTACK = 3;

const HOSTILE = new Set([
    "zombie", "skeleton", "creeper", "spider", "cave_spider", "enderman",
    "witch", "drowned", "husk", "stray", "slime", "phantom", "pillager",
    "vindicator", "zombie_villager", "silverfish", "blaze", "ghast",
]);
const FOOD_MOBS = new Set(["cow", "pig", "sheep", "chicken", "rabbit"]);
const EDIBLE = [
    "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken",
    "bread", "apple", "baked_potato", "cooked_cod", "cooked_salmon",
    "beef", "porkchop", "mutton", "chicken", "carrot", "potato", "melon_slice",
    "sweet_berries", "rotten_flesh",
];
// Items worth offering as craft options when their recipe is currently
// satisfiable. The goal target is always added when craftable.
const CRAFT_LADDER = [
    "oak_planks", "spruce_planks", "birch_planks", "jungle_planks",
    "acacia_planks", "dark_oak_planks", "mangrove_planks",
    "stick", "crafting_table", "wooden_pickaxe", "wooden_axe", "wooden_sword",
    "stone_pickaxe", "stone_axe", "stone_sword", "stone_shovel", "furnace",
    "torch", "iron_pickaxe", "iron_sword", "iron_axe", "shield", "chest",
    "bucket", "shears",
];
const ORE_DROPS = {
    coal_ore: "coal", deepslate_coal_ore: "coal",
    iron_ore: "raw_iron", deepslate_iron_ore: "raw_iron",
    copper_ore: "raw_copper", deepslate_copper_ore: "raw_copper",
    gold_ore: "raw_gold", deepslate_gold_ore: "raw_gold",
    diamond_ore: "diamond", deepslate_diamond_ore: "diamond",
    lapis_ore: "lapis_lazuli", deepslate_lapis_ore: "lapis_lazuli",
    redstone_ore: "redstone", deepslate_redstone_ore: "redstone",
    emerald_ore: "emerald", deepslate_emerald_ore: "emerald",
    stone: "cobblestone", deepslate: "cobbled_deepslate", grass_block: "dirt",
};
const PICKAXE_TIER = ["wooden", "stone", "iron", "diamond", "netherite"];
const FUELS = ["coal", "charcoal", "oak_planks", "spruce_planks", "birch_planks",
    "jungle_planks", "acacia_planks", "dark_oak_planks", "oak_log", "spruce_log",
    "birch_log", "jungle_log", "acacia_log", "dark_oak_log", "stick"];
const SMELTABLE = { raw_iron: "iron_ingot", raw_copper: "copper_ingot", raw_gold: "gold_ingot",
    beef: "cooked_beef", porkchop: "cooked_porkchop", mutton: "cooked_mutton",
    chicken: "cooked_chicken", cobblestone: "stone", sand: "glass" };
// Blocks worth pillaring with, cheapest first.
const PILLAR_BLOCKS = ["dirt", "cobblestone", "netherrack", "cobbled_deepslate", "andesite", "diorite",
    "granite", "oak_planks", "spruce_planks", "birch_planks", "stone", "sand", "gravel"];
const PILLAR_HEIGHT = 6;
// Things nobody needs more than a stack of; deposited when the bag is nearly full.
const JUNK = ["dirt", "cobblestone", "gravel", "sand", "andesite", "diorite", "granite", "cobbled_deepslate",
    "wheat_seeds", "rotten_flesh", "flint", "string", "bone", "spider_eye", "gunpowder", "oak_sapling",
    "birch_sapling", "spruce_sapling", "stick", "tuff", "netherrack", "clay_ball", "kelp", "seagrass"];
const JUNK_KEEP = 16; // how many of a junk item to keep when depositing
const INVENTORY_FULL = 30; // used slots (of 36) that make chest:deposit worth offering
const LANDMARK_BLOCKS = ["crafting_table", "furnace", "chest"];
const COLLECT_DISTANCE = 12;
const NO_TARGET_VERB_RE =
    /^\s*(place|put|equip|wear|deposit|store|eat|consume|kill|hunt|explore|find|build|use|sleep|plant|drink)\b/i;
const RESOURCE_BLOCK_RE =
    /_log$|_ore$|^stone$|^cobblestone$|^dirt$|^sand$|^gravel$|^deepslate$|^grass_block$|_leaves$|^crafting_table$|^furnace$|^chest$|^obsidian$|^clay$|^sugar_cane$|^bamboo$|^cactus$|_wool$|^pumpkin$|^melon$|^sweet_berry_bush$|^wheat$/;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function withTimeout(promise, ms, label) {
    let timer;
    const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timed out`)), ms);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

// ---------------------------------------------------------------------------
// Control primitives as callables. /step evals the same sources per request;
// here they are compiled once with an equivalent scope.
// ---------------------------------------------------------------------------
function loadPrimitives(bot, mcData) {
    const dir = path.join(__dirname, "..", "..", "..", "control_primitives");
    const sources = fs
        .readdirSync(dir)
        .filter((f) => f.endsWith(".js"))
        .map((f) => fs.readFileSync(path.join(dir, f), "utf8"))
        .join("\n");
    // top-level declarations only: nested helpers are not in the return scope
    const names = [...sources.matchAll(/^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/gm)].map(
        (m) => m[1]
    );
    const { goals, Movements } = require("mineflayer-pathfinder");
    const body =
        "let _craftItemFailCount = 0, _killMobFailCount = 0, _mineBlockFailCount = 0, " +
        "_placeItemFailCount = 0, _smeltItemFailCount = 0;\n" +
        sources +
        "\nreturn { resetCounters() { _craftItemFailCount = _killMobFailCount = _mineBlockFailCount = " +
        "_placeItemFailCount = _smeltItemFailCount = 0; }, " +
        [...new Set(names)].join(", ") +
        " };";
    const goalNames = Object.keys(goals);
    const factory = new Function(
        "bot", "mcData", "Vec3", "Movements", "require", ...goalNames, body
    );
    return factory(bot, mcData, Vec3, Movements, require, ...goalNames.map((n) => goals[n]));
}

// ---------------------------------------------------------------------------
// Goal target parsing: substring-match the task text against item and block
// names; suffix classes (log, planks, pickaxe) match a family. Several
// equally good candidates are resolved by a Jev Choice; code owns the count.
// ---------------------------------------------------------------------------
function normaliseText(text) {
    return ` ${String(text)
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, " ")
        .replace(/\b([a-z]+)s\b/g, "$1")} `;
}

function findTargetCandidates(text, mcData) {
    const norm = normaliseText(text);
    const words = new Set(norm.trim().split(/\s+/));
    const exact = [];
    const classes = new Map();
    const allNames = new Set([
        ...Object.keys(mcData.itemsByName),
        ...Object.keys(mcData.blocksByName),
    ]);
    for (const name of allNames) {
        const phrase = ` ${name.replace(/_/g, " ").replace(/\b([a-z]+)s\b/g, "$1")} `;
        if (norm.includes(phrase)) exact.push(name);
        const suffix = name.split("_").pop().replace(/s$/, "");
        if (name.includes("_") && words.has(suffix)) {
            if (!classes.has(suffix)) classes.set(suffix, []);
            classes.get(suffix).push(name);
        }
    }
    const candidates = [];
    if (exact.length) {
        const longest = Math.max(...exact.map((n) => n.length));
        for (const n of exact.filter((n) => n.length === longest)) {
            candidates.push({ kind: "exact", key: n, matches: (x) => x === n });
        }
    } else {
        for (const [suffix, members] of classes) {
            // "wood" alone must not match "wooden_pickaxe"; require a real family
            if (members.length < 2) continue;
            candidates.push({
                kind: "class",
                key: `*_${suffix}`,
                matches: (x) => x === suffix || x.endsWith(`_${suffix}`),
                members,
            });
        }
    }
    return candidates;
}

function countMatching(inventory, matches) {
    let total = 0;
    for (const [name, count] of Object.entries(inventory)) {
        if (matches(name)) total += count;
    }
    return total;
}

// Longest shared underscore-suffix: iron_ore + deepslate_iron_ore -> "iron_ore".
function commonSuffix(names) {
    const parts = names.map((n) => n.split("_"));
    const out = [];
    for (let i = 1; ; i++) {
        const seg = parts[0][parts[0].length - i];
        if (seg === undefined || !parts.every((p) => p[p.length - i] === seg)) break;
        out.unshift(seg);
    }
    return out.join("_") || names[0];
}

function matcherFor(key) {
    if (key.startsWith("*_")) {
        const suffix = key.slice(2);
        return (x) => x === suffix || x.endsWith(`_${suffix}`);
    }
    return (x) => x === key;
}

// ---------------------------------------------------------------------------
class FastLoop {
    constructor(bot) {
        this.bot = bot;
        this.mcData = require("minecraft-data")(bot.version);
        this.prims = loadPrimitives(bot, this.mcData);
        this.active = false;
        this.runPromise = null;
        this.goal = null;
        this.target = null;
        this.pending = []; // triggers for the brain, drained by status()
        this.trace = [];
        this.recent = [];
        this.replay = [];
        this.lastDecision = null;
        this.stats = { decisions: 0, jevMs: 0, actions: 0, heuristic: 0 };
        this.goalReached = false;
        this.lastHealth = bot.health;
        this.positions = [];
        this.lastProgressAt = Date.now();
        this.startedAt = Date.now();
        this.abortCurrent = null;
        this.actionsSinceProgress = 0;
        this.highGoal = null;
        this.highTarget = null;
        this.highGoalReached = false;
        this._recipes = null;
        this.landmarks = {}; // block name -> {x, y, z} of the last one seen; kept across goals
    }

    // Remember where useful blocks were last seen so the bot can walk back.
    updateLandmarks() {
        const bot = this.bot;
        const here = bot.entity.position;
        for (const name of LANDMARK_BLOCKS) {
            const def = this.mcData.blocksByName[name];
            if (!def) continue;
            const block = bot.findBlock({ matching: def.id, maxDistance: 32 });
            if (block) {
                this.landmarks[name] = { x: block.position.x, y: block.position.y, z: block.position.z };
            } else if (this.landmarks[name]) {
                const l = this.landmarks[name];
                // forget a landmark the bot is standing next to but can no longer see: it is gone
                if (Math.hypot(l.x - here.x, l.y - here.y, l.z - here.z) < 24) delete this.landmarks[name];
            }
        }
    }

    droppedItems() {
        const here = this.bot.entity.position;
        return Object.values(this.bot.entities)
            .filter((e) => e.name === "item" && e.position && e.position.distanceTo(here) <= COLLECT_DISTANCE)
            .sort((a, b) => a.position.distanceTo(here) - b.position.distanceTo(here));
    }

    // Poll a condition on each physics tick; resolves true when met, false on timeout.
    waitFor(condition, ms) {
        const bot = this.bot;
        return new Promise((resolve) => {
            const timer = setTimeout(() => {
                bot.removeListener("physicsTick", tick);
                resolve(false);
            }, ms);
            const tick = () => {
                let ok = false;
                try {
                    ok = condition();
                } catch (err) {
                    ok = false;
                }
                if (ok) {
                    clearTimeout(timer);
                    bot.removeListener("physicsTick", tick);
                    resolve(true);
                }
            };
            bot.on("physicsTick", tick);
        });
    }

    // Jump-and-place tower: look down, jump, place the block under the feet
    // once the bot has risen clear of its old position, land, repeat.
    async pillarUp(height, isAborted) {
        const bot = this.bot;
        const blockName = PILLAR_BLOCKS.find((n) => this.inventoryCounts()[n]);
        if (!blockName) return "failed: no blocks to pillar with";
        const item = bot.inventory.items().find((i) => i.name === blockName);
        await bot.equip(item, "hand");
        let placed = 0;
        for (let i = 0; i < height; i++) {
            if (!this.active || isAborted()) break;
            if (!this.inventoryCounts()[blockName]) break;
            const feet = bot.entity.position.floored();
            const ref = bot.blockAt(feet.offset(0, -1, 0));
            if (!ref || ref.boundingBox !== "block") {
                return placed ? `ok, pillared ${placed} (nothing solid below)` : "failed: nothing solid below";
            }
            const headroom = bot.blockAt(feet.offset(0, 2, 0));
            if (headroom && headroom.boundingBox === "block") {
                return placed ? `ok, pillared ${placed} (ceiling)` : "failed: ceiling overhead";
            }
            await bot.lookAt(feet.offset(0.5, -0.5, 0.5), true); // straight down, instantly
            bot.setControlState("jump", true);
            // the block goes where the feet were, so wait until the bot is well above that
            const rose = await this.waitFor(() => bot.entity.position.y > feet.y + 0.8, 800);
            if (!rose) {
                bot.setControlState("jump", false);
                return placed ? `ok, pillared ${placed} (could not jump)` : "failed: could not jump";
            }
            try {
                await withTimeout(
                    bot._placeBlockWithOptions(ref, new Vec3(0, 1, 0), { forceLook: "ignore", swingArm: "right" }),
                    1500,
                    "pillar place"
                );
                placed++;
            } catch (err) {
                bot.setControlState("jump", false);
                return placed ? `ok, pillared ${placed} (${err.message.slice(0, 40)})` : `failed: ${err.message.slice(0, 60)}`;
            } finally {
                bot.setControlState("jump", false);
            }
            await this.waitFor(() => bot.entity.onGround, 1500);
        }
        return placed ? `ok, pillared ${placed}` : "failed: could not place";
    }

    // Items to put away: junk above a small keep count, never tools or the goal target.
    junkToDeposit(inventory) {
        const out = {};
        for (const name of JUNK) {
            const count = inventory[name] || 0;
            if (count <= JUNK_KEEP) continue;
            if (this.target && this.target.matches(name)) continue;
            if (this.highTarget && this.highTarget.matches(name)) continue;
            out[name] = count - JUNK_KEEP;
        }
        return out;
    }

    // Tools worth switching to right now: a sword when hostiles are near, the
    // right pickaxe or axe for the blocks the goal is about.
    equipOptions(snap) {
        const held = this.bot.heldItem ? this.bot.heldItem.name : null;
        const options = {};
        const best = (kind) => {
            let pick = null;
            for (const name of Object.keys(snap.inventory)) {
                const m = name.match(/^(\w+)_(sword|pickaxe|axe)$/);
                if (!m || m[2] !== kind) continue;
                if (!pick || PICKAXE_TIER.indexOf(m[1]) > PICKAXE_TIER.indexOf(pick.split("_")[0])) pick = name;
            }
            return pick;
        };
        const hostileNear = snap.entities.some((e) => HOSTILE.has(e.name) && e.distance <= 12);
        const sword = best("sword");
        if (hostileNear && sword && held !== sword) options[sword] = `Equip the ${sword} (a hostile mob is within 12 blocks)`;
        const wantsLogs = this.target && Object.keys(snap.resources).some((b) => /_log$/.test(b) && this.target.matches(b));
        const axe = best("axe");
        if (wantsLogs && axe && held !== axe) options[axe] = `Equip the ${axe} for chopping logs`;
        const wantsStone = this.target && Object.keys(snap.resources).some(
            (b) => (/_ore$|^stone$|^deepslate$|^cobblestone$/.test(b)) && (this.target.matches(b) || this.target.matches(ORE_DROPS[b] || ""))
        );
        const pickaxe = best("pickaxe");
        if (wantsStone && pickaxe && held !== pickaxe) options[pickaxe] = `Equip the ${pickaxe} for mining stone and ore`;
        return options;
    }

    // ---- subgoal derivation ---------------------------------------------
    // Candidate subgoals for a high goal, derived from recipes and the world.
    // Each carries an explicit target so the chosen text never needs to be
    // re-parsed. One level per call: the brain re-derives after each subgoal.
    async deriveSubgoals(text, override) {
        const target = await this.parseTarget(text, override);
        const inventory = this.inventoryCounts();
        const out = [];
        const seen = new Set();
        const add = (c) => {
            if (seen.has(c.text) || out.length >= MAX_SUBGOAL_CANDIDATES) return;
            seen.add(c.text);
            out.push(c);
        };
        if (target && target.kind !== "nearBlock") {
            const summary = this.summarise(target);
            const remaining = Math.max(1, summary.need - summary.gained);
            const itemDef = target.key.startsWith("*_") ? null : this.mcData.itemsByName[target.key];
            const blocks = Object.keys(this.mcData.blocksByName).filter(
                (b) => target.matches(b) || target.matches(ORE_DROPS[b] || "")
            );
            // Items that drop from a block are mined, not crafted (raw_iron has a
            // recipe from raw_iron_block, which is never the sensible route).
            if (itemDef && !blocks.length) {
                const plan = this.bestRecipe(itemDef.id, remaining, inventory);
                if (plan) {
                    const table = this.bot.findBlock({ matching: this.mcData.blocksByName.crafting_table.id, maxDistance: 32 });
                    if (plan.requiresTable && !table) {
                        if (inventory.crafting_table) {
                            add({ text: "Place the crafting table", target: { nearBlock: "crafting_table" }, why: "the recipe needs a crafting table and one is in the inventory" });
                        } else {
                            add({ text: "Obtain 1 crafting_table", target: { item: "crafting_table", count: 1 }, why: "the recipe needs a crafting table" });
                        }
                    }
                    for (const [name, missing] of Object.entries(plan.missing)) {
                        add({ text: `Obtain ${missing} ${name}`, target: { item: name, count: missing }, why: `ingredient for ${target.key}` });
                    }
                    if (!Object.keys(plan.missing).length) {
                        add({ text: `Craft ${remaining} ${target.key}`, target: { item: target.key, count: remaining }, why: "all ingredients are in the inventory" });
                    }
                }
            }
            if (blocks.length) {
                const nearby = this.blockNearby((b) => blocks.includes(b));
                const tier = this.requiredPickaxe(blocks);
                if (tier && PICKAXE_TIER.indexOf(tier) > PICKAXE_TIER.indexOf(this.toolTier())) {
                    add({ text: `Obtain 1 ${tier}_pickaxe`, target: { item: `${tier}_pickaxe`, count: 1 }, why: `${blocks[0]} needs at least a ${tier} pickaxe` });
                }
                const family = target.key.startsWith("*_") ? target.key : blocks.length === 1 ? blocks[0] : `*_${commonSuffix(blocks)}`;
                if (nearby) {
                    add({ text: `Mine ${remaining} ${target.key}`, target: { item: target.key, count: remaining }, why: "a matching block is within reach" });
                } else {
                    add({ text: `Explore to find ${family}`, target: { nearBlock: family }, why: "no matching block is within scan range" });
                }
            }
        }
        if (this.bot.food < 10 && !EDIBLE.some((f) => inventory[f])) {
            add({ text: "Obtain 1 cooked_beef", target: { item: "*_beef", count: 1 }, why: "hunger is low and there is no food" });
        }
        add({ text, target: override || (target ? { item: target.key, count: target.need } : { none: true }), why: "work on the high goal directly" });
        return { target: this.summarise(target), candidates: out };
    }

    // Recipe for `itemId` that leaves the fewest ingredients missing, scaled to `need`.
    bestRecipe(itemId, need, inventory) {
        if (!this._recipes) this._recipes = require("prismarine-recipe")(this.bot.version).Recipe;
        let best = null;
        for (const recipe of this._recipes.find(itemId, null)) {
            const crafts = Math.ceil(need / Math.max(1, recipe.result.count));
            const missing = {};
            let total = 0;
            for (const d of recipe.delta) {
                if (d.count >= 0) continue;
                const item = this.mcData.items[d.id];
                if (!item) continue;
                const short = -d.count * crafts - (inventory[item.name] || 0);
                if (short > 0) {
                    missing[item.name] = short;
                    total += short;
                }
            }
            if (!best || total < best.total) best = { total, missing, requiresTable: recipe.requiresTable };
        }
        return best;
    }

    // Lowest pickaxe tier that harvests any of `blocks`, or null when hands do.
    requiredPickaxe(blocks) {
        let tier = null;
        for (const name of blocks) {
            const tools = this.mcData.blocksByName[name] && this.mcData.blocksByName[name].harvestTools;
            if (!tools) continue;
            let lowest = null;
            for (const id of Object.keys(tools)) {
                const item = this.mcData.items[id];
                const m = item && item.name.match(/^(\w+)_pickaxe$/);
                if (m && (lowest === null || PICKAXE_TIER.indexOf(m[1]) < lowest)) lowest = PICKAXE_TIER.indexOf(m[1]);
            }
            if (lowest !== null && (tier === null || lowest < tier)) tier = lowest;
        }
        return tier === null ? null : PICKAXE_TIER[tier];
    }

    // ---- lifecycle -------------------------------------------------------
    async setGoal(goal) {
        if (!goal || typeof goal.text !== "string" || !goal.text.trim()) {
            throw new Error("goal.text must be a non-empty string");
        }
        await this.interrupt();
        this.goal = {
            id: goal.id || `goal-${Date.now()}`,
            text: goal.text.trim(),
            context: goal.context || "",
            kind: goal.kind || "task", // "task" | "standing"
            noProgressSeconds: goal.noProgressSeconds || NO_PROGRESS_DEFAULT_S,
            noProgressActions: goal.noProgressActions || NO_PROGRESS_DEFAULT_ACTIONS,
            hint: goal.hint || null,
            highGoalId: goal.highGoal ? goal.highGoal.id || goal.highGoal.text : null,
        };
        this.target = await this.parseTarget(this.goal.text, goal.target);
        // The high goal is parsed once per id so its baseline survives subgoal changes.
        if (goal.highGoal && goal.highGoal.text) {
            const hid = goal.highGoal.id || goal.highGoal.text;
            if (!this.highGoal || this.highGoal.id !== hid) {
                this.highGoal = { id: hid, text: goal.highGoal.text };
                this.highTarget = await this.parseTarget(goal.highGoal.text, goal.highGoal.target);
                this.highGoalReached = false;
                console.log(`fastLoop: high goal "${this.highGoal.text}" target=${JSON.stringify(this.summarise(this.highTarget))}`);
            }
        } else if (this.goal.kind !== "standing") {
            this.highGoal = null;
            this.highTarget = null;
            this.highGoalReached = false;
        }
        this.actionsSinceProgress = 0;
        // replay: an explicit list, or the entry for the parsed target in a
        // library of {targetKey: [actions]} the brain built for this situation
        this.replay = Array.isArray(goal.replay) ? [...goal.replay] : [];
        if (!this.replay.length && goal.sequenceLibrary && this.target) {
            const seq = goal.sequenceLibrary[this.target.key];
            if (Array.isArray(seq)) this.replay = [...seq];
        }
        this.replaySource = this.replay.length ? this.target && this.target.key : null;
        this.prims.resetCounters();
        this.trace = [];
        this.recent = [];
        this.positions = [];
        this.goalReached = false;
        this._lastGained = 0;
        this.lastProgressAt = Date.now();
        this.startedAt = Date.now();
        this.lastHealth = this.bot.health;
        console.log(
            `fastLoop: goal "${this.goal.text}" target=${JSON.stringify(this.targetSummary())} ` +
                `replay=${this.replay.length} steps`
        );
        if (!this.active) {
            this.active = true;
            this.runPromise = this.run().catch((err) => {
                console.log("fastLoop: run loop crashed:", err);
                this.active = false;
            });
        }
        return this.status({ drain: false });
    }

    // Stop the current primitive but keep the loop alive (used by setGoal).
    async interrupt() {
        if (this.abortCurrent) {
            try {
                this.abortCurrent();
            } catch (err) {
                /* ignore */
            }
        }
        this.goal = null; // loop idles until the new goal is installed
        await sleep(50);
    }

    async stop() {
        if (!this.active) return;
        this.active = false;
        await this.interrupt();
        try {
            await withTimeout(this.runPromise || Promise.resolve(), 15000, "fastLoop.stop");
        } catch (err) {
            console.log("fastLoop:", err.message);
        }
        this.runPromise = null;
        try {
            this.bot.pathfinder.setGoal(null);
            this.bot.clearControlStates();
        } catch (err) {
            /* ignore */
        }
    }

    // ---- target ----------------------------------------------------------
    async parseTarget(text, override) {
        const inventory = this.inventoryCounts();
        if (override && override.none) return null; // e.g. a standing goal
        if (override && override.nearBlock) {
            // reached when a matching block is within scan range (exploration subgoals)
            const key = String(override.nearBlock);
            return { kind: "nearBlock", key: `near:${key}`, block: key, need: 1, matches: matcherFor(key), startHave: 0 };
        }
        if (override && override.item) {
            const key = String(override.item);
            const matches = matcherFor(key);
            return {
                key,
                need: override.count || 1,
                matches,
                startHave: countMatching(inventory, matches),
            };
        }
        // Tasks whose item leaves the inventory (place, equip, deposit, eat)
        // or that have no item at all (kill, explore) have no countable
        // target: the Jev goalReached Noul decides completion instead.
        if (NO_TARGET_VERB_RE.test(text)) return null;
        const candidates = findTargetCandidates(text, this.mcData);
        const numbers = text.match(/\b(\d+)\b/);
        const need = numbers ? parseInt(numbers[1], 10) : 1;
        let chosen = null;
        if (candidates.length === 1) {
            chosen = candidates[0];
        } else if (candidates.length > 1) {
            chosen = await this.jevPickTarget(text, candidates);
        }
        if (!chosen) return null;
        const resolved = { key: chosen.key, need, matches: chosen.matches };
        // Smelting or cooking counts by the output item, not the consumed input.
        if (chosen.kind === "exact" && SMELTABLE[chosen.key] && /\b(smelt|cook|bake)\b/i.test(text)) {
            const out = SMELTABLE[chosen.key];
            resolved.key = out;
            resolved.matches = (x) => x === out;
        }
        // Mining an ore counts by its drop; mining stone counts by cobblestone.
        else if (
            chosen.kind === "exact" &&
            ORE_DROPS[chosen.key] &&
            /\b(mine|collect|gather|get|obtain|dig)\b/i.test(text)
        ) {
            const drop = ORE_DROPS[chosen.key];
            resolved.key = drop;
            resolved.matches = (x) => x === drop;
        }
        resolved.startHave = countMatching(inventory, resolved.matches);
        return resolved;
    }

    async jevPickTarget(text, candidates) {
        const client = traversal.getClient();
        if (!client) return candidates[0];
        const criteria = { none: "The task does not ask to obtain any of these items" };
        for (const c of candidates) {
            criteria[c.key] = c.members ? `Any of: ${c.members.slice(0, 8).join(", ")}` : null;
        }
        try {
            const { answers } = await client.systemOne(
                {
                    state: { task: text },
                    questions: {
                        target: traversal.choiceFn(
                            "Which item (or item family) does `task` ask the Minecraft player to " +
                                "end up holding more of? Pick the family for 'any kind of' wording.",
                            criteria
                        ),
                    },
                },
                { timeout: DECISION_TIMEOUT_MS }
            );
            return candidates.find((c) => c.key === answers.target.choice) || null;
        } catch (err) {
            console.log("fastLoop: target pick failed:", err.message);
            return candidates[0];
        }
    }

    targetSummary() {
        return this.summarise(this.target);
    }

    summarise(target) {
        if (!target) return null;
        if (target.kind === "nearBlock") {
            const have = this.blockNearby(target.matches) ? 1 : 0;
            return { item: target.key, need: 1, have, gained: have };
        }
        const have = countMatching(this.inventoryCounts(), target.matches);
        return {
            item: target.key,
            need: target.need,
            have,
            gained: have - target.startHave,
        };
    }

    blockNearby(matches) {
        const ids = [];
        for (const [name, block] of Object.entries(this.mcData.blocksByName)) {
            if (matches(name)) ids.push(block.id);
        }
        if (!ids.length) return false;
        return this.bot.findBlocks({ matching: ids, maxDistance: RESOURCE_SCAN_DISTANCE, count: 1 }).length > 0;
    }

    // ---- observation helpers --------------------------------------------
    inventoryCounts() {
        const counts = {};
        for (const item of this.bot.inventory.items()) {
            if (item) counts[item.name] = (counts[item.name] || 0) + item.count;
        }
        return counts;
    }

    toolTier() {
        let best = -1;
        for (const n of Object.keys(this.inventoryCounts())) {
            const m = n.match(/^(\w+)_pickaxe$/);
            if (m) best = Math.max(best, PICKAXE_TIER.indexOf(m[1]));
        }
        return best < 0 ? "none" : PICKAXE_TIER[best];
    }

    fingerprint() {
        const bot = this.bot;
        const voxels = Array.from(getSurroundingBlocks(bot, 8, 2, 8));
        const surface = voxels.some((b) => /dirt|log|grass|sand|snow/.test(b));
        const block = bot.blockAt(bot.entity.position);
        const biome = block && block.biome ? block.biome.name : "unknown";
        const t = bot.time ? bot.time.timeOfDay : 0;
        return {
            biome: surface ? biome : "underground",
            toolTier: this.toolTier(),
            daylight: t >= 13000 && t < 23000 ? "night" : "day",
        };
    }

    nearbyResources() {
        const bot = this.bot;
        // Goal-relevant blocks get their own scan: a capped scan of every
        // resource type would otherwise fill up with stone and dirt.
        const wantTarget = new Set();
        const wantGeneric = new Set();
        for (const [name, block] of Object.entries(this.mcData.blocksByName)) {
            if (this.target && (this.target.matches(name) || this.target.matches(ORE_DROPS[name] || ""))) {
                wantTarget.add(block.id);
            } else if (RESOURCE_BLOCK_RE.test(name)) {
                wantGeneric.add(block.id);
            }
        }
        const positions = [];
        if (wantTarget.size) {
            positions.push(
                ...bot.findBlocks({ matching: [...wantTarget], maxDistance: RESOURCE_SCAN_DISTANCE, count: 64 })
            );
        }
        positions.push(
            ...bot.findBlocks({ matching: [...wantGeneric], maxDistance: RESOURCE_SCAN_DISTANCE, count: 400 })
        );
        const byName = {};
        const here = bot.entity.position;
        for (const p of positions) {
            const b = bot.blockAt(p);
            if (!b) continue;
            const d = Math.round(p.distanceTo(here));
            const e = byName[b.name] || (byName[b.name] = { count: 0, nearest: d });
            e.count++;
            if (d < e.nearest) e.nearest = d;
        }
        return byName;
    }

    nearbyEntities() {
        return traversal.nearbyEntitiesOf(this.bot);
    }

    snapshot() {
        const bot = this.bot;
        const pos = bot.entity.position;
        this.updateLandmarks();
        const dropped = this.droppedItems();
        const landmarks = {};
        for (const [name, l] of Object.entries(this.landmarks)) {
            landmarks[name] = { ...l, distance: Math.round(Math.hypot(l.x - pos.x, l.y - pos.y, l.z - pos.z)) };
        }
        return {
            position: { x: Math.floor(pos.x), y: Math.floor(pos.y), z: Math.floor(pos.z) },
            health: bot.health,
            food: bot.food,
            oxygen: bot.oxygenLevel,
            inLava: bot.entity.isInLava,
            inWater: bot.entity.isInWater,
            timeOfDay: bot.time ? bot.time.timeOfDay : 0,
            inventory: this.inventoryCounts(),
            inventoryUsed: typeof bot.inventoryUsed === "function" ? bot.inventoryUsed() : null,
            heldItem: bot.heldItem ? bot.heldItem.name : null,
            resources: this.nearbyResources(),
            entities: this.nearbyEntities(),
            droppedItems: {
                count: dropped.length,
                nearest: dropped.length ? Math.round(dropped[0].position.distanceTo(pos)) : null,
            },
            landmarks,
            target: this.targetSummary(),
        };
    }

    // ---- triggers --------------------------------------------------------
    pushTrigger(type, detail) {
        const last = this.pending[this.pending.length - 1];
        if (last && last.type === type && Date.now() - last.at < 5000) return;
        this.pending.push({
            type,
            at: Date.now(),
            detail: detail || {},
            goalId: this.goal && this.goal.id,
        });
        console.log(`fastLoop: trigger ${type} ${JSON.stringify(detail || {}).slice(0, 200)}`);
    }

    checkTriggers(snap) {
        const now = Date.now();
        // 1. task-relevant delta: exact count on the parsed target
        if (snap.target) {
            if (snap.target.gained > this._lastGained) this.lastProgressAt = now;
            this._lastGained = snap.target.gained;
            if (snap.target.gained >= snap.target.need && !this.goalReached) {
                this.goalReached = true;
                this.pushTrigger("goal_reached", { target: snap.target, trace: this.collapsedTrace() });
            }
        } else {
            // without a parseable target any inventory change counts as progress
            const sig = JSON.stringify(snap.inventory);
            if (sig !== this._lastInventorySig) this.lastProgressAt = now;
            this._lastInventorySig = sig;
        }
        // 1b. the high goal (parsed once; survives subgoal changes)
        if (this.highTarget && !this.highGoalReached) {
            const high = this.summarise(this.highTarget);
            if (high.gained >= high.need) {
                this.highGoalReached = true;
                this.pushTrigger("high_goal_reached", { target: high, highGoalId: this.highGoal.id });
            }
        }
        // 2. no progress for too long: by wall clock or by action count
        const idle = (now - this.lastProgressAt) / 1000;
        if (
            !this.goalReached &&
            (idle > this.goal.noProgressSeconds || this.actionsSinceProgress >= this.goal.noProgressActions)
        ) {
            this.pushTrigger("no_progress", {
                seconds: Math.round(idle),
                actions: this.actionsSinceProgress,
                target: snap.target,
            });
            this.lastProgressAt = now; // re-arm
            this.actionsSinceProgress = 0;
        }
        // 3. hazards
        const hostiles = snap.entities.filter((e) => HOSTILE.has(e.name) && e.distance <= 6);
        const healthDrop = this.lastHealth - snap.health;
        this.lastHealth = snap.health;
        if (healthDrop >= 2 || snap.inLava || snap.oxygen < 8 || hostiles.length || snap.health <= 6) {
            this.pushTrigger("hazard", {
                healthDrop,
                health: snap.health,
                inLava: snap.inLava,
                oxygen: snap.oxygen,
                hostiles: hostiles.map((h) => h.name),
            });
        }
    }

    isStuck(snap) {
        this.positions.push(snap.position);
        if (this.positions.length > STUCK_WINDOW_ACTIONS) this.positions.shift();
        const moves = this.recent
            .slice(-STUCK_WINDOW_ACTIONS)
            .filter((r) => /^(walk|climb|dig|flee)/.test(r.action));
        if (moves.length < STUCK_WINDOW_ACTIONS || this.positions.length < STUCK_WINDOW_ACTIONS) {
            return false;
        }
        const a = this.positions[0];
        const b = snap.position;
        return Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z) < STUCK_DISTANCE;
    }

    // ---- menu ------------------------------------------------------------
    buildMenu(snap) {
        const bot = this.bot;
        const menu = {}; // id -> description (Jev criteria); ids are executed by code
        const pos = bot.entity.position;
        for (const [name, { dx, dz }] of Object.entries(traversal.COMPASS)) {
            const s = traversal.surveyCandidate(bot, dx, dz, WALK_DISTANCE);
            menu[`walk:${name}`] =
                `Walk about ${WALK_DISTANCE} blocks ${name}: surface ${s.surfaceBlock}, ` +
                `elevation ${s.elevationDelta}` +
                (s.hazards.length ? `, hazards ${s.hazards.join("/")}` : "");
        }
        const above = bot.blockAt(pos.offset(0, 2, 0));
        if (!above || above.name === "air" || above.name === "cave_air") {
            menu["climb:up"] = "Climb or pillar upward about 6 blocks (reach the surface or higher ground)";
        }
        if (pos.y > 8) menu["dig:down"] = "Dig straight down about 4 blocks (toward caves and ores)";

        // mining: goal-relevant first, then ores, then everything else
        const resources = Object.entries(snap.resources);
        const relevance = ([name]) =>
            this.target && (this.target.matches(name) || this.target.matches(ORE_DROPS[name] || ""))
                ? 0
                : /_ore$/.test(name)
                ? 1
                : 2;
        resources.sort((a, b) => relevance(a) - relevance(b) || a[1].nearest - b[1].nearest);
        for (const [name, info] of resources.slice(0, MAX_MENU_MINE)) {
            const drop = ORE_DROPS[name] ? ` (drops ${ORE_DROPS[name]})` : "";
            menu[`mine:${name}`] =
                `Mine the nearest ${name}${drop}: ${info.count} within ${RESOURCE_SCAN_DISTANCE} blocks, ` +
                `nearest ${info.nearest} blocks away`;
        }

        // crafting: recipes that are satisfiable right now
        const table = bot.findBlock({
            matching: this.mcData.blocksByName.crafting_table.id,
            maxDistance: 32,
        });
        const craftable = [];
        const ladder = [...CRAFT_LADDER];
        if (this.target && this.mcData.itemsByName[this.target.key]) ladder.unshift(this.target.key);
        for (const item of new Set(ladder)) {
            const def = this.mcData.itemsByName[item];
            if (!def) continue;
            let recipes = [];
            try {
                recipes = bot.recipesFor(def.id, null, 1, table);
            } catch (err) {
                continue;
            }
            if (recipes.length) craftable.push(item);
            if (craftable.length >= MAX_MENU_CRAFT) break;
        }
        for (const item of craftable) {
            menu[`craft:${item}`] =
                `Craft ${item} once${table ? " at the nearby crafting table" : " in the inventory grid"}`;
        }
        if (snap.inventory.crafting_table && !table) {
            menu["place:crafting_table"] = "Place the crafting table from inventory next to the bot";
        }
        const furnace = bot.findBlock({
            matching: this.mcData.blocksByName.furnace.id,
            maxDistance: 32,
        });
        if (snap.inventory.furnace && !furnace) {
            menu["place:furnace"] = "Place the furnace from inventory next to the bot";
        }
        if (furnace) {
            const fuel = FUELS.find((f) => snap.inventory[f]);
            for (const [raw, out] of Object.entries(SMELTABLE)) {
                if (snap.inventory[raw] && fuel) {
                    menu[`smelt:${raw}`] = `Smelt one ${raw} into ${out} at the nearby furnace using ${fuel}`;
                }
            }
        }

        // mobs
        const mobs = snap.entities.filter(
            (e) => (HOSTILE.has(e.name) || FOOD_MOBS.has(e.name)) && e.distance <= 16
        );
        for (const m of mobs.slice(0, MAX_MENU_ATTACK)) {
            menu[`attack:${m.name}`] =
                `Attack the ${m.name} ${m.distance} blocks ${m.direction}` +
                (HOSTILE.has(m.name) ? " (hostile)" : " (food/leather source)");
        }
        if (snap.entities.some((e) => HOSTILE.has(e.name) && e.distance <= 12)) {
            menu["flee"] = "Run away from the nearest hostile mob for about 16 blocks";
        }
        const food = EDIBLE.find((f) => snap.inventory[f]);
        if (food && snap.food < 16) menu["eat"] = `Eat ${food} (hunger is ${snap.food}/20)`;

        // dropped items, pillaring, tools, storage, landmarks
        if (snap.droppedItems.count) {
            menu["collect:items"] =
                `Walk over and pick up ${snap.droppedItems.count} dropped item stack(s), ` +
                `nearest ${snap.droppedItems.nearest} blocks away`;
        }
        const pillarBlock = PILLAR_BLOCKS.find((n) => snap.inventory[n]);
        if (pillarBlock && (!above || above.name === "air" || above.name === "cave_air")) {
            menu["pillar:up"] =
                `Jump-and-place a ${PILLAR_HEIGHT}-block tower of ${pillarBlock} straight up ` +
                `(${snap.inventory[pillarBlock]} available; escapes pits and water, reaches the surface)`;
        }
        for (const [tool, description] of Object.entries(this.equipOptions(snap))) {
            menu[`equip:${tool}`] = description;
        }
        if (snap.inventoryUsed !== null && snap.inventoryUsed >= INVENTORY_FULL) {
            const junk = this.junkToDeposit(snap.inventory);
            const chestNear = bot.findBlock({ matching: this.mcData.blocksByName.chest.id, maxDistance: 32 });
            if (Object.keys(junk).length && chestNear) {
                menu["chest:deposit"] =
                    `Put ${Object.entries(junk).map(([n, c]) => `${c} ${n}`).join(", ")} into the chest ` +
                    `${Math.round(chestNear.position.distanceTo(pos))} blocks away (inventory ${snap.inventoryUsed}/36 slots used)`;
            } else if (Object.keys(junk).length && snap.inventory.chest) {
                menu["place:chest"] = `Place the chest from inventory (inventory ${snap.inventoryUsed}/36 slots used, nothing to store in nearby)`;
            }
        }
        for (const [name, l] of Object.entries(snap.landmarks)) {
            if (l.distance > WALK_DISTANCE) {
                menu[`return:${name}`] = `Walk back to the last ${name} seen, ${l.distance} blocks away at x=${l.x} y=${l.y} z=${l.z}`;
            }
        }
        menu["wait"] = "Do nothing for two seconds (only if nothing above is useful)";
        return menu;
    }

    // ---- decision --------------------------------------------------------
    async decide(snap, menu) {
        const client = traversal.getClient();
        const started = Date.now();
        if (client) {
            try {
                const state = {
                    goal: {
                        text: this.goal.text,
                        context: this.goal.context,
                        kind: this.goal.kind,
                        target: snap.target,
                        hint: this.goal.hint,
                    },
                    bot: {
                        position: snap.position,
                        health: snap.health,
                        food: snap.food,
                        timeOfDay: snap.timeOfDay,
                        inLava: snap.inLava,
                        inWater: snap.inWater,
                        bestPickaxe: this.toolTier(),
                    },
                    inventory: snap.inventory,
                    inventoryUsedSlots: snap.inventoryUsed,
                    heldItem: snap.heldItem,
                    nearbyResources: snap.resources,
                    nearbyEntities: snap.entities,
                    droppedItems: snap.droppedItems,
                    landmarks: snap.landmarks,
                    recentActions: this.recent,
                    secondsSinceProgress: Math.round((Date.now() - this.lastProgressAt) / 1000),
                    actionsSinceProgress: this.actionsSinceProgress,
                };
                const questions = {
                    action: traversal.choiceFn(
                        "A Minecraft bot works toward `goal.text` (`goal.context` explains it; " +
                            "`goal.target` is the exact item count still needed, when known). " +
                            "`inventory` maps item names to counts, `nearbyResources` lists " +
                            "block types with counts and nearest distance, `nearbyEntities` lists " +
                            "mobs nearest first, `droppedItems` are item stacks lying on the ground, " +
                            "`landmarks` are the last seen crafting table, furnace and chest with " +
                            "distances, `heldItem` is what is in the hand, and `recentActions` shows " +
                            "the last few actions with their outcomes (avoid repeating ones that " +
                            "failed or made no progress). `bot.timeOfDay` is 0-24000 and night " +
                            "starts near 13000. " +
                            "Each option is one bounded primitive the code will execute next. " +
                            "Which option makes the most progress toward the goal right now " +
                            "while keeping the bot safe (eat when hungry, deal with hostiles, " +
                            "avoid lava and water)?",
                        menu
                    ),
                    danger: traversal.noulFn(
                        "Is the bot in immediate physical danger that must be handled before " +
                            "working on the goal (hostile mob within a few blocks, health at or " +
                            "below 6, standing in or beside lava, drowning)?"
                    ),
                    stuck: traversal.noulFn(
                        "Judging from `recentActions`, `actionsSinceProgress` and " +
                            "`secondsSinceProgress`, is the bot stuck: repeating actions that time " +
                            "out, fail, or change nothing, so that continuing the same way will " +
                            "not reach the goal?"
                    ),
                    subgoalValid: traversal.noulFn(
                        "Given `inventory`, `nearbyResources` and `landmarks` as they are now, does " +
                            "`goal.text` still need doing? Answer no if it is already satisfied or " +
                            "has become pointless (for example the item it asks for is already in " +
                            "hand, or the block it asks to place is already placed nearby)."
                    ),
                };
                if (!snap.target) {
                    questions.goalReached = traversal.noulFn(
                        "Looking at `inventory`, `nearbyResources` (placed blocks such as a " +
                            "crafting table or chest appear here), `nearbyEntities` and `bot`, " +
                            "has `goal.text` already been accomplished so no further action is needed?"
                    );
                }
                const { answers } = await client.systemOne(
                    { state, questions },
                    { timeout: DECISION_TIMEOUT_MS }
                );
                const ms = Date.now() - started;
                this.stats.decisions++;
                this.stats.jevMs += ms;
                const a = answers.action;
                this.lastDecision = {
                    choice: a.choice,
                    confidence: a.confidence,
                    probabilities: a.probabilities,
                    danger: answers.danger.noul,
                    stuck: answers.stuck.noul,
                    subgoalValid: answers.subgoalValid.noul,
                    goalReached: answers.goalReached ? answers.goalReached.noul : null,
                    ms,
                };
                if (answers.danger.noul >= 0.7) {
                    this.pushTrigger("hazard", { jevDanger: answers.danger.noul });
                }
                if (answers.stuck.noul >= 0.7 && this.recent.length >= 3) {
                    this.pushTrigger("stuck", { jevStuck: answers.stuck.noul, recent: this.recent.map((r) => r.action) });
                    this.recent.push({ action: "nudge", outcome: "Jev judged the bot stuck; jumped in place" });
                    nudge(this.bot);
                }
                if (answers.subgoalValid.noul <= 0.2 && this.goal.kind === "task" && !this.goalReached) {
                    // not a failure: the brain re-selects without recording a skill
                    this.pushTrigger("subgoal_obsolete", { subgoalValid: answers.subgoalValid.noul, target: snap.target });
                }
                if (answers.goalReached && answers.goalReached.noul >= 0.8 && !this.goalReached) {
                    this.goalReached = true;
                    this.pushTrigger("goal_reached", {
                        judged: answers.goalReached.noul,
                        trace: this.collapsedTrace(),
                    });
                }
                // A flat spread over equivalent headings is still a usable
                // preference, so the top choice is taken at any confidence.
                if (menu[a.choice]) return a.choice;
                console.log("fastLoop: Jev chose an option not in the menu:", a.choice);
            } catch (err) {
                console.log("fastLoop: Jev decision failed:", err.message);
            }
        }
        this.stats.heuristic++;
        return this.heuristic(snap, menu);
    }

    // Plain-code fallback used when Jev is unavailable or errored.
    heuristic(snap, menu) {
        if (menu.eat && snap.food < 10) return "eat";
        if (menu.flee && snap.health < 10) return "flee";
        const mine = Object.keys(menu).find(
            (k) => k.startsWith("mine:") && this.target && this.target.matches(k.slice(5))
        );
        if (mine) return mine;
        if (this.target && menu[`craft:${this.target.key}`]) return `craft:${this.target.key}`;
        if (this.goal.hint && this.goal.hint.direction && menu[`walk:${this.goal.hint.direction}`]) {
            return `walk:${this.goal.hint.direction}`;
        }
        const walks = Object.keys(menu).filter((k) => k.startsWith("walk:") && !/hazards/.test(menu[k]));
        return walks.length ? walks[Math.floor(Math.random() * walks.length)] : "wait";
    }

    nextReplay(menu) {
        if (!this.replay.length) return null;
        const next = this.replay[0];
        if (!menu[next]) {
            console.log(`fastLoop: replay step "${next}" not available now, dropping replay`);
            this.replay = [];
            return null;
        }
        this.replay.shift();
        return next;
    }

    // ---- execution -------------------------------------------------------
    async execute(action) {
        const bot = this.bot;
        const { goals } = require("mineflayer-pathfinder");
        const [verb, arg] = action.split(":");
        const pos = bot.entity.position.clone();
        const gotoBounded = async (goal, ms) => {
            let aborted = false;
            this.abortCurrent = () => {
                aborted = true;
                bot.pathfinder.setGoal(null);
            };
            try {
                await withTimeout(bot.pathfinder.goto(goal), ms, action);
                return "ok";
            } catch (err) {
                if (aborted) return "interrupted";
                return /timed out/.test(err.message)
                    ? "timeout"
                    : `failed: ${err.message.split("\n")[0].slice(0, 80)}`;
            } finally {
                this.abortCurrent = null;
                bot.pathfinder.setGoal(null);
            }
        };
        try {
            switch (verb) {
                case "walk": {
                    const { dx, dz } = traversal.COMPASS[arg];
                    return await gotoBounded(
                        new goals.GoalNearXZ(
                            Math.floor(pos.x + dx * WALK_DISTANCE),
                            Math.floor(pos.z + dz * WALK_DISTANCE),
                            2
                        ),
                        WALK_TIMEOUT_MS
                    );
                }
                case "climb":
                    return await gotoBounded(new goals.GoalY(Math.floor(pos.y) + 6), WALK_TIMEOUT_MS);
                case "dig":
                    return await gotoBounded(new goals.GoalY(Math.floor(pos.y) - 4), WALK_TIMEOUT_MS);
                case "flee": {
                    const hostile = this.nearbyEntities().find((e) => HOSTILE.has(e.name));
                    let dx = 1;
                    let dz = 0;
                    if (hostile) {
                        const c = traversal.COMPASS[hostile.direction];
                        dx = -c.dx;
                        dz = -c.dz;
                    }
                    return await gotoBounded(
                        new goals.GoalNearXZ(Math.floor(pos.x + dx * 16), Math.floor(pos.z + dz * 16), 2),
                        WALK_TIMEOUT_MS
                    );
                }
                case "mine": {
                    this.abortCurrent = () => {
                        try {
                            bot.collectBlock.cancelTask();
                        } catch (e) {
                            /* ignore */
                        }
                        bot.pathfinder.setGoal(null);
                    };
                    const before = this.inventoryCounts();
                    await withTimeout(this.prims.mineBlock(bot, arg, 1), MINE_TIMEOUT_MS, action);
                    const after = this.inventoryCounts();
                    const gained = Object.keys(after).some((k) => (after[k] || 0) > (before[k] || 0));
                    return gained ? "ok" : "no item gained";
                }
                case "craft": {
                    const before = this.inventoryCounts();
                    await withTimeout(this.prims.craftItem(bot, arg, 1), CRAFT_TIMEOUT_MS, action);
                    return (this.inventoryCounts()[arg] || 0) > (before[arg] || 0)
                        ? "ok"
                        : "recipe did not complete";
                }
                case "smelt": {
                    const fuel = FUELS.find((f) => this.inventoryCounts()[f]);
                    await withTimeout(this.prims.smeltItem(bot, arg, fuel, 1), CRAFT_TIMEOUT_MS * 2, action);
                    return "ok";
                }
                case "place": {
                    const spot = pos.offset(1, 0, 1).floored();
                    await withTimeout(this.prims.placeItem(bot, arg, spot), CRAFT_TIMEOUT_MS, action);
                    return "ok";
                }
                case "attack":
                    await withTimeout(
                        this.prims.killMob(bot, arg, ATTACK_TIMEOUT_S),
                        (ATTACK_TIMEOUT_S + 10) * 1000,
                        action
                    );
                    return "ok";
                case "eat": {
                    const food = EDIBLE.find((f) => this.inventoryCounts()[f]);
                    const item = bot.inventory.items().find((i) => i.name === food);
                    await bot.equip(item, "hand");
                    await withTimeout(bot.consume(), 8000, action);
                    return "ok";
                }
                case "collect": {
                    const drops = this.droppedItems();
                    if (!drops.length) return "failed: nothing to pick up";
                    const slots = typeof bot.inventoryUsed === "function" ? bot.inventoryUsed() : 0;
                    const p = drops[0].position;
                    const r = await gotoBounded(new goals.GoalNear(p.x, p.y, p.z, 0.5), WALK_TIMEOUT_MS);
                    if (r !== "ok") return r;
                    await sleep(300); // pickup happens on contact a tick or two later
                    const after = typeof bot.inventoryUsed === "function" ? bot.inventoryUsed() : 0;
                    return after > slots || this.droppedItems().length < drops.length ? "ok" : "ok, nothing picked up";
                }
                case "pillar": {
                    let aborted = false;
                    this.abortCurrent = () => {
                        aborted = true;
                        bot.setControlState("jump", false);
                    };
                    return await withTimeout(this.pillarUp(PILLAR_HEIGHT, () => aborted), 25000, action);
                }
                case "equip": {
                    const item = bot.inventory.items().find((i) => i.name === arg);
                    if (!item) return `failed: no ${arg} in inventory`;
                    await withTimeout(bot.equip(item, "hand"), 5000, action);
                    return "ok";
                }
                case "chest": {
                    const chest = bot.findBlock({ matching: this.mcData.blocksByName.chest.id, maxDistance: 32 });
                    if (!chest) return "failed: no chest nearby";
                    const junk = this.junkToDeposit(this.inventoryCounts());
                    if (!Object.keys(junk).length) return "ok, nothing to deposit";
                    const slots = bot.inventoryUsed();
                    await withTimeout(this.prims.depositItemIntoChest(bot, chest.position, junk), CRAFT_TIMEOUT_MS * 2, action);
                    return bot.inventoryUsed() < slots ? "ok" : "deposited nothing";
                }
                case "return": {
                    const l = this.landmarks[arg];
                    if (!l) return `failed: no ${arg} remembered`;
                    return await gotoBounded(new goals.GoalNear(l.x, l.y, l.z, 2), WALK_TIMEOUT_MS * 3);
                }
                case "wait":
                default:
                    await sleep(2000);
                    return "ok";
            }
        } catch (err) {
            const msg = err && err.message ? err.message.split("\n")[0].slice(0, 100) : String(err);
            return /timed out/.test(msg) ? "timeout" : `failed: ${msg}`;
        } finally {
            this.abortCurrent = null;
            try {
                bot.pathfinder.setGoal(null);
            } catch (e) {
                /* ignore */
            }
        }
    }

    // ---- main loop -------------------------------------------------------
    async run() {
        while (this.active) {
            if (!this.goal) {
                await sleep(100);
                continue;
            }
            const goalId = this.goal.id;
            let snap;
            try {
                snap = this.snapshot();
                this.checkTriggers(snap);
            } catch (err) {
                console.log("fastLoop: snapshot failed:", err.message);
                await sleep(500);
                continue;
            }
            if (this.goalReached && this.goal.kind === "task") {
                await sleep(250); // the brain will replace the goal
                continue;
            }
            if (this.isStuck(snap)) {
                this.pushTrigger("stuck", { position: snap.position });
                this.recent.push({ action: "nudge", outcome: "jumped in place to get unstuck" });
                nudge(this.bot);
                this.positions = [];
            }
            const menu = this.buildMenu(snap);
            const replayed = this.nextReplay(menu);
            const action = replayed || (await this.decide(snap, menu));
            if (!this.active || !this.goal || this.goal.id !== goalId) continue;
            const before = this.targetSummary();
            const t0 = Date.now();
            const outcome = await this.execute(action);
            // a new goal may have landed mid-action: its trace must not inherit this outcome
            if (!this.goal || this.goal.id !== goalId) continue;
            if (replayed && !/^ok/.test(outcome)) {
                console.log(`fastLoop: replay step "${action}" ${outcome}, handing control to Jev`);
                this.replay = [];
            }
            if (!/^ok/.test(outcome)) await sleep(400); // no hot loop on instant failures
            const after = this.targetSummary();
            const gain = before && after ? after.gained - before.gained : 0;
            this.stats.actions++;
            this.actionsSinceProgress = gain > 0 ? 0 : this.actionsSinceProgress + 1;
            this.recent.push({
                action,
                outcome: gain > 0 ? `${outcome}, +${gain} ${after.item}` : outcome,
            });
            if (this.recent.length > RECENT_ACTIONS) this.recent.shift();
            const held = this.bot.heldItem;
            this.trace.push({ action, outcome, gain, ms: Date.now() - t0, tool: held ? held.name : null });
        }
    }

    collapsedTrace() {
        const out = [];
        for (const step of this.trace) {
            if (!/^ok/.test(step.outcome)) continue; // only keep steps that worked
            const last = out[out.length - 1];
            if (last && last.action === step.action) last.times++;
            else out.push({ action: step.action, times: 1 });
        }
        return out;
    }

    // ---- status ----------------------------------------------------------
    status({ drain = true } = {}) {
        const triggers = drain ? this.pending.splice(0) : [...this.pending];
        let fingerprint = null;
        try {
            fingerprint = this.bot.entity ? this.fingerprint() : null;
        } catch (err) {
            /* ignore */
        }
        return {
            running: this.active,
            goal: this.goal,
            target: this.targetSummary(),
            goalReached: this.goalReached,
            highGoal: this.highGoal || null,
            highTarget: this.summarise(this.highTarget),
            highGoalReached: Boolean(this.highGoalReached),
            inventory: this.bot.entity ? this.inventoryCounts() : {},
            triggers,
            recentActions: this.recent,
            trace: this.trace,
            collapsedTrace: this.collapsedTrace(),
            fingerprint,
            lastDecision: this.lastDecision,
            stats: this.stats,
            replayRemaining: this.replay.length,
            replaySource: this.replaySource || null,
            elapsedSeconds: Math.round((Date.now() - this.startedAt) / 1000),
        };
    }
}

function nudge(bot) {
    try {
        bot.setControlState("jump", true);
        setTimeout(() => bot.setControlState("jump", false), 300);
    } catch (err) {
        /* ignore */
    }
}

function inject(bot) {
    bot.fastLoop = new FastLoop(bot);
    return bot.fastLoop;
}

module.exports = { inject, FastLoop, findTargetCandidates, countMatching, matcherFor, loadPrimitives };
