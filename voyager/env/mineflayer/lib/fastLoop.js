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
const NO_PROGRESS_DEFAULT_S = 75;
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
            hint: goal.hint || null,
        };
        this.target = await this.parseTarget(this.goal.text, goal.target);
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
        if (!this.target) return null;
        const have = countMatching(this.inventoryCounts(), this.target.matches);
        return {
            item: this.target.key,
            need: this.target.need,
            have,
            gained: have - this.target.startHave,
        };
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
        return {
            position: { x: Math.floor(pos.x), y: Math.floor(pos.y), z: Math.floor(pos.z) },
            health: bot.health,
            food: bot.food,
            oxygen: bot.oxygenLevel,
            inLava: bot.entity.isInLava,
            inWater: bot.entity.isInWater,
            timeOfDay: bot.time ? bot.time.timeOfDay : 0,
            inventory: this.inventoryCounts(),
            resources: this.nearbyResources(),
            entities: this.nearbyEntities(),
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
        // 2. no progress for too long
        const idle = (now - this.lastProgressAt) / 1000;
        if (!this.goalReached && idle > this.goal.noProgressSeconds) {
            this.pushTrigger("no_progress", { seconds: Math.round(idle), target: snap.target });
            this.lastProgressAt = now; // re-arm
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
                    nearbyResources: snap.resources,
                    nearbyEntities: snap.entities,
                    recentActions: this.recent,
                    secondsSinceProgress: Math.round((Date.now() - this.lastProgressAt) / 1000),
                };
                const questions = {
                    action: traversal.choiceFn(
                        "A Minecraft bot works toward `goal.text` (`goal.context` explains it; " +
                            "`goal.target` is the exact item count still needed, when known). " +
                            "`inventory` maps item names to counts, `nearbyResources` lists " +
                            "block types with counts and nearest distance, `nearbyEntities` lists " +
                            "mobs nearest first, and `recentActions` shows the last few actions " +
                            "with their outcomes (avoid repeating ones that failed or made no " +
                            "progress). `bot.timeOfDay` is 0-24000 and night starts near 13000. " +
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
                    goalReached: answers.goalReached ? answers.goalReached.noul : null,
                    ms,
                };
                if (answers.danger.noul >= 0.7) {
                    this.pushTrigger("hazard", { jevDanger: answers.danger.noul });
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
            this.recent.push({
                action,
                outcome: gain > 0 ? `${outcome}, +${gain} ${after.item}` : outcome,
            });
            if (this.recent.length > RECENT_ACTIONS) this.recent.shift();
            this.trace.push({ action, outcome, gain, ms: Date.now() - t0 });
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
