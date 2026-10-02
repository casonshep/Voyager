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
const { ORE_DROPS, SMELTABLE, FUELS, PICKAXE_TIER, EDIBLE, FAMILIES } = require("./mcKnowledge");
// Jump-and-place pillaring. The server only accepts a placement while the bot
// is a full block above the target and the packet arrives inside that window
// (about four ticks near the top of the jump), so the placement is re-sent on
// every tick of the window rather than once per jump. The pathfinder's own
// towering sends once at lift-off and succeeds by luck after many jumps, so
// it is switched off (see configurePathfinder) and this pillar is used instead.
const ENABLE_PILLAR = true;
const PILLAR_WINDOW_Y = 1.0; // blocks above the old feet level before a placement is valid
const PILLAR_SENDS_PER_JUMP = 4;

const DECISION_TIMEOUT_MS = 4000;
const WALK_DISTANCE = 12;
const WALK_TIMEOUT_MS = 9000;
const MINE_TIMEOUT_MS = 30000;
const CRAFT_TIMEOUT_MS = 20000;
const ATTACK_TIMEOUT_S = 20;
// No-progress fires on whichever comes first: wall clock or a run of
// actions without a target gain (decisions are fast, so count them too).
// Generous on purpose: exploring, surfacing and far mining produce no target
// gain for a while even when they are going well. Three stalls fail a high goal.
const NO_PROGRESS_DEFAULT_S = 120;
const NO_PROGRESS_DEFAULT_ACTIONS = 12;
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
// Blocks worth pillaring with, cheapest first.
const PILLAR_BLOCKS = ["dirt", "cobblestone", "netherrack", "cobbled_deepslate", "andesite", "diorite",
    "granite", "oak_planks", "spruce_planks", "birch_planks", "stone", "sand", "gravel"];
const PILLAR_HEIGHT = 6;
const PILLAR_RETRY_MS = 90 * 1000; // after a pillar that placed nothing, do not offer it again for a while
// Inventory policy, in code: what is worth carrying and how much. Everything
// else is junk and goes into a chest when one is reachable, or on the ground
// when the bag is nearly full underground.
const KEEP_ALL_RE =
    /_(pickaxe|axe|sword|shovel|hoe|helmet|chestplate|leggings|boots)$|^raw_|_ingot$|_nugget$|^diamond$|^emerald$|^lapis_lazuli$|^redstone$|^torch$|^bucket$|^water_bucket$|^lava_bucket$|^flint$|^flint_and_steel$|^obsidian$|^ender_pearl$|^ender_eye$|^blaze_rod$|^blaze_powder$|^string$|^bone$|^arrow$|^bow$|^shield$|_bed$|^gunpowder$|^leather$|_wool$|^shears$|^book$|^paper$|^sugar_cane$|^glass$|^iron_block$|^gold_block$|^diamond_block$/;
const KEEP_CAP = { cobblestone: 32, coal: 64, charcoal: 32, stick: 32, "family:log": 32, "family:planks": 32, crafting_table: 1, furnace: 1, chest: 2 };
const INVENTORY_DEPOSIT_AT = 24; // used slots (of 36) from which a chest deposit is offered
const INVENTORY_DISCARD_AT = 30; // used slots from which junk is tossed when no chest is near
const INVENTORY_SLOTS = 36;
const CHEST_REACH = 48; // how far a known chest may be to offer withdraw/deposit
// Batch smelting: load the furnace with everything to cook plus fuel, leave,
// do something else, come back for the output. Vanilla: 10 s per item.
const SMELT_SECONDS_PER_ITEM = 10;
const FUEL_ITEMS_PER_UNIT = { coal: 8, charcoal: 8, lava_bucket: 100, blaze_rod: 12, oak_log: 1.5, spruce_log: 1.5, birch_log: 1.5,
    jungle_log: 1.5, acacia_log: 1.5, dark_oak_log: 1.5, oak_planks: 1.5, spruce_planks: 1.5, birch_planks: 1.5,
    jungle_planks: 1.5, acacia_planks: 1.5, dark_oak_planks: 1.5, stick: 0.5 };
const FURNACE_MAX_BATCH = 64;
const LANDMARK_BLOCKS = ["crafting_table", "furnace", "chest"];
const COLLECT_DISTANCE = 12;
// The server sends the world age every 20 ticks; when it stops changing for
// this long the server is paused (/pause from the Fabric mod) and the loop idles.
const SERVER_PAUSE_DETECT_MS = 2500;
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
    // a primitive that loses the race keeps running until cancelled; its
    // eventual rejection (usually "Path was stopped") is not an error of ours
    Promise.resolve(promise).catch(() => {});
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
// Plural handling: "torches" must match torch, "pickaxes" pickaxe, "boots" boots.
// Text words are tried in several forms; item words are compared with a
// trailing s removed, so both sides meet in the middle.
function wordForms(w) {
    const forms = new Set([w]);
    if (w.endsWith("s")) forms.add(w.slice(0, -1));
    if (w.endsWith("es")) forms.add(w.slice(0, -2));
    if (w.endsWith("ies")) forms.add(w.slice(0, -3) + "y");
    return forms;
}

function textTokens(text) {
    return String(text).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim().split(/\s+/).filter(Boolean);
}

// Does the item/block `name` (oak_log, iron_boots) appear as consecutive words in `tokens`?
function phraseIn(tokens, name) {
    const words = name.split("_").map((w) => w.replace(/s$/, ""));
    outer: for (let i = 0; i + words.length <= tokens.length; i++) {
        for (let j = 0; j < words.length; j++) {
            if (!wordForms(tokens[i + j]).has(words[j])) continue outer;
        }
        return true;
    }
    return false;
}

function findTargetCandidates(text, mcData) {
    const tokens = textTokens(text);
    const words = new Set(tokens.flatMap((t) => [...wordForms(t)]));
    const exact = [];
    const classes = new Map();
    const allNames = new Set([
        ...Object.keys(mcData.itemsByName),
        ...Object.keys(mcData.blocksByName),
    ]);
    for (const name of allNames) {
        if (phraseIn(tokens, name)) exact.push(name);
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
    if (key.startsWith("family:")) {
        const members = new Set(FAMILIES[key.slice(7)] || []);
        return (x) => members.has(x);
    }
    return (x) => x === key;
}

// ---------------------------------------------------------------------------
class FastLoop {
    constructor(bot) {
        this.bot = bot;
        this.mcData = require("minecraft-data")(bot.version);
        this.prims = loadPrimitives(bot, this.mcData);
        this.configurePathfinder();
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
        this.serverPaused = false;
        this._lastAge = null;
        this._lastAgeChangeAt = Date.now();
        this.decisionLog = []; // recent Jev decisions, drained by status()
        this._lastDecisionAt = null;
        this.home = null; // {x, y, z} of the home base (crafting table, furnace, chest), set by the brain
        this.furnaceJobs = []; // batches cooking: {pos, input, output, count, loadedAt, readyAt}
        this.playerChat = []; // chat lines from players since the last status poll, drained by status()
        this.listenForPlayers();
    }

    // ---- player chat -------------------------------------------------------
    // Lines typed by players reach the brain through status().chat. Lines from
    // this bot and from sibling bots (same name stem, e.g. bot, bot2, bot3) are
    // dropped: a sibling's "I need a stone pickaxe" must not look like a command.
    isBotName(name) {
        const own = String(this.bot.username || "bot");
        const stem = own.replace(/\d+$/, "") || own;
        return name === own || new RegExp("^" + stem + "[0-9]*$").test(String(name || ""));
    }

    listenForPlayers() {
        const client = this.bot._client;
        if (!client || typeof client.on !== "function") return; // dry-run stubs without a protocol client
        // The protocol-level player_chat packet carries only real player messages.
        // mineflayer's "chat" event also fires on server feedback such as
        // "[name: Teleported bot to name]", which must not look like a command.
        client.on("playerChat", (data) => {
            const text = String(data.plainMessage || "").trim();
            if (!text || text.startsWith("/")) return;
            const username = this.playerNameByUuid(data.sender) || data.senderName || "";
            if (!username || this.isBotName(username)) return;
            this.playerChat.push({ from: username, text, at: Date.now() });
            if (this.playerChat.length > 20) this.playerChat.shift();
            console.log(`fastLoop: chat from ${username}: ${text}`);
        });
    }

    playerNameByUuid(uuid) {
        if (!uuid) return null;
        for (const [name, p] of Object.entries(this.bot.players || {})) {
            if (p && p.uuid === uuid) return name;
        }
        return null;
    }

    say(text) {
        const line = String(text || "").slice(0, 240);
        if (!line) return;
        try {
            this.bot.chat(line);
        } catch (err) {
            console.log("fastLoop: chat failed:", err.message);
        }
    }

    // ---- batch smelting ----------------------------------------------------
    pendingOutput(matches) {
        return this.furnaceJobs.filter((j) => matches(j.output)).reduce((s, j) => s + j.count, 0);
    }

    jobsFor(matches) {
        return this.furnaceJobs.filter((j) => !matches || matches(j.output));
    }

    // Load a furnace with up to `count` of `raw` and enough fuel, then leave it cooking.
    async smeltBatch(raw, count) {
        const bot = this.bot;
        const { goals } = require("mineflayer-pathfinder");
        const furnaceBlock = bot.findBlock({ matching: this.mcData.blocksByName.furnace.id, maxDistance: 32 });
        if (!furnaceBlock) return "failed: no furnace nearby";
        const inv = this.inventoryCounts();
        const have = inv[raw] || 0;
        if (!have) return `failed: no ${raw} to smelt`;
        const n = Math.min(have, count, FURNACE_MAX_BATCH);
        const fuelName = Object.keys(FUEL_ITEMS_PER_UNIT).sort((a, b) => FUEL_ITEMS_PER_UNIT[b] - FUEL_ITEMS_PER_UNIT[a]).find((f) => inv[f]);
        if (!fuelName) return "failed: no fuel";
        try {
            await withTimeout(bot.pathfinder.goto(new goals.GoalLookAtBlock(furnaceBlock.position, bot.world)), WALK_TIMEOUT_MS * 2, "walk to furnace");
        } catch (err) {
            return `failed: could not reach the furnace (${err.message.split("\n")[0].slice(0, 40)})`;
        }
        const furnace = await withTimeout(bot.openFurnace(furnaceBlock), 8000, "open furnace");
        try {
            if (furnace.outputItem && furnace.outputItem()) await furnace.takeOutput(); // whatever finished earlier
            const existing = furnace.inputItem ? furnace.inputItem() : null;
            if (existing && existing.name !== raw) return `failed: furnace is busy with ${existing.name}`;
            const fuelNeeded = Math.max(0, Math.ceil(n / FUEL_ITEMS_PER_UNIT[fuelName]) - ((furnace.fuelItem && furnace.fuelItem()) ? furnace.fuelItem().count : 0));
            if (fuelNeeded > 0) await furnace.putFuel(this.mcData.itemsByName[fuelName].id, null, Math.min(fuelNeeded, inv[fuelName]));
            await furnace.putInput(this.mcData.itemsByName[raw].id, null, n);
        } finally {
            try {
                furnace.close();
            } catch (err) {
                /* ignore */
            }
        }
        const output = SMELTABLE[raw];
        const pos = furnaceBlock.position.clone();
        const job = this.furnaceJobs.find((j) => j.pos.equals(pos));
        const now = Date.now();
        if (job) {
            job.count += n;
            job.readyAt = Math.max(job.readyAt, now) + n * SMELT_SECONDS_PER_ITEM * 1000;
        } else {
            this.furnaceJobs.push({ pos, input: raw, output, count: n, loadedAt: now, readyAt: now + n * SMELT_SECONDS_PER_ITEM * 1000 });
        }
        const readyIn = Math.round(n * SMELT_SECONDS_PER_ITEM);
        this.pushTrigger("smelt_queued", { input: raw, output, count: n, readyInSeconds: readyIn, position: { x: pos.x, y: pos.y, z: pos.z } });
        return `ok, ${n} ${raw} cooking (${readyIn}s)`;
    }

    // Take finished output from a furnace with a job; waits a bounded time if it is not done yet.
    async collectFurnace(isAborted) {
        const bot = this.bot;
        const { goals } = require("mineflayer-pathfinder");
        const here = bot.entity.position;
        const job = [...this.furnaceJobs].sort((a, b) => a.pos.distanceTo(here) - b.pos.distanceTo(here))[0];
        if (!job) return "failed: nothing is cooking";
        const block = bot.blockAt(job.pos);
        if (!block || block.name !== "furnace") {
            this.furnaceJobs = this.furnaceJobs.filter((j) => j !== job);
            return "failed: the furnace is gone";
        }
        try {
            await withTimeout(bot.pathfinder.goto(new goals.GoalLookAtBlock(job.pos, bot.world)), WALK_TIMEOUT_MS * 3, "walk to furnace");
        } catch (err) {
            return `failed: could not reach the furnace (${err.message.split("\n")[0].slice(0, 40)})`;
        }
        const wait = Math.min(Math.max(0, job.readyAt - Date.now()), 45000);
        if (wait > 0 && !isAborted()) await sleep(wait);
        const furnace = await withTimeout(bot.openFurnace(block), 8000, "open furnace");
        let taken = 0;
        try {
            for (let i = 0; i < 8; i++) {
                const out = furnace.outputItem ? furnace.outputItem() : null;
                if (!out) break;
                taken += out.count;
                await furnace.takeOutput();
                await sleep(100);
            }
            const left = furnace.inputItem ? furnace.inputItem() : null;
            if (!left) this.furnaceJobs = this.furnaceJobs.filter((j) => j !== job);
            else {
                job.count = left.count;
                job.readyAt = Date.now() + left.count * SMELT_SECONDS_PER_ITEM * 1000;
            }
        } finally {
            try {
                furnace.close();
            } catch (err) {
                /* ignore */
            }
        }
        return taken ? `ok, collected ${taken} ${job.output}` : "took nothing (still cooking)";
    }

    // Detect /pause on the Minecraft server from the world clock. While paused
    // the loop runs no actions and raises no triggers; on resume the
    // no-progress clock restarts so the pause is not counted against the goal.
    checkServerPaused() {
        const age = this.bot.time ? this.bot.time.age : null;
        const now = Date.now();
        if (age === null || age === undefined) return false; // unknown: never treat as paused
        if (age !== this._lastAge) {
            this._lastAge = age;
            this._lastAgeChangeAt = now;
            if (this.serverPaused) {
                this.serverPaused = false;
                this.lastProgressAt = now;
                this.actionsSinceProgress = 0;
                this.positions = [];
                console.log("fastLoop: server resumed");
                this.pushTrigger("server_resumed", {});
            }
        } else if (!this.serverPaused && now - this._lastAgeChangeAt > SERVER_PAUSE_DETECT_MS) {
            this.serverPaused = true;
            console.log("fastLoop: server paused (world clock stopped); idling until it resumes");
            this.pushTrigger("server_paused", {});
        }
        return this.serverPaused;
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

    // Jump-and-place tower. Jump is held the whole time (the bot re-jumps on
    // every landing); on each physics tick while the bot is at least one full
    // block above where its feet were, one placement is sent under the feet.
    // A level is done when the block under the feet turns solid; a level gets
    // several jumps before giving up, and the whole thing stops cleanly on a
    // ceiling, a missing floor, no blocks, or an abort.
    async pillarUp(height, isAborted) {
        const bot = this.bot;
        const blockName = PILLAR_BLOCKS.find((n) => this.inventoryCounts()[n]);
        if (!blockName) return "failed: no blocks to pillar with";
        const item = bot.inventory.items().find((i) => i.name === blockName);
        await bot.equip(item, "hand");
        const solid = (pos) => {
            const b = bot.blockAt(pos);
            return Boolean(b && b.boundingBox === "block");
        };
        let placed = 0;
        const finish = (why) => {
            bot.setControlState("jump", false);
            if (!placed) this.pillarFailedAt = Date.now();
            return placed ? `ok, pillared ${placed}${why ? ` (${why})` : ""}` : `failed: ${why}`;
        };
        // stand in the middle of the block so the tower is under the bot, not beside it
        const feet0 = bot.entity.position.floored();
        const off = bot.entity.position.minus(feet0.offset(0.5, 0, 0.5));
        if (Math.abs(off.x) > 0.25 || Math.abs(off.z) > 0.25) {
            try {
                const { goals } = require("mineflayer-pathfinder");
                await withTimeout(bot.pathfinder.goto(new goals.GoalBlock(feet0.x, feet0.y, feet0.z)), 4000, "pillar centre");
            } catch (err) {
                /* try from where we are */
            }
        }
        if (!(await this.waitFor(() => bot.entity.onGround, 1000))) return finish("not on the ground");

        for (let level = 0; level < height; level++) {
            if (!this.active || isAborted()) return finish("interrupted");
            if (!this.inventoryCounts()[blockName]) return finish("out of blocks");
            const feet = bot.entity.position.floored();
            const ref = bot.blockAt(feet.offset(0, -1, 0));
            if (!ref || ref.boundingBox !== "block") return finish("nothing solid below");
            // Underground the way up is blocked: dig the block overhead first
            // (the jump needs two blocks of air above the feet).
            for (const dy of [2, 3]) {
                const head = bot.blockAt(feet.offset(0, dy, 0));
                if (head && head.boundingBox === "block") {
                    if (!bot.canDigBlock(head) || /bedrock|obsidian|lava|water/.test(head.name)) {
                        return finish(`cannot dig ${head.name} overhead`);
                    }
                    try {
                        await withTimeout(bot.dig(head, true), 12000, "pillar dig");
                    } catch (err) {
                        return finish(`could not dig ${head.name} overhead`);
                    }
                    await bot.equip(bot.inventory.items().find((i) => i.name === blockName), "hand");
                }
            }
            await bot.lookAt(feet.offset(0.5, -0.5, 0.5), true); // straight down, instantly

            bot.setControlState("jump", true);
            let sends = 0;
            let jumps = 0;
            let wasAirborne = false;
            const ok = await this.waitFor(() => {
                if (isAborted() || !this.active) return true;
                if (solid(feet)) return true; // the level is built
                const airborne = !bot.entity.onGround;
                if (airborne && !wasAirborne) jumps++;
                if (!airborne && wasAirborne) sends = 0; // landed: a fresh set of sends next jump
                wasAirborne = airborne;
                if (jumps > 6) return true; // give up on this level
                // Inside the window (a full block above the old feet) send the
                // placement on every tick, several per jump: one of them lands
                // while the server also sees the bot high enough.
                if (sends < PILLAR_SENDS_PER_JUMP && bot.entity.position.y > feet.y + PILLAR_WINDOW_Y) {
                    sends++;
                    bot._placeBlockWithOptions(ref, new Vec3(0, 1, 0), { forceLook: "ignore", swingArm: "right" })
                        .catch(() => {}); // rejections are expected; solid() is the confirmation
                }
                return false;
            }, 9000);
            if (!ok || !solid(feet)) {
                console.log(`fastLoop: pillar level ${level + 1} failed after ${jumps} jump(s) at y=${feet.y}`);
                return finish(ok ? "placement rejected" : "timed out");
            }
            placed++;
            // let the bot settle on the new block before the next level
            await this.waitFor(() => bot.entity.onGround && bot.entity.position.y >= feet.y + 1, 1500);
        }
        return finish("");
    }

    // An air block next to the bot with a solid neighbour to place against.
    // Underground the diagonal is usually rock, so search around and, failing
    // that, dig a pocket at feet level.
    findPlaceSpot() {
        const bot = this.bot;
        const feet = bot.entity.position.floored();
        const isAir = (p) => {
            const b = bot.blockAt(p);
            return Boolean(b && (b.name === "air" || b.name === "cave_air"));
        };
        const isSolid = (p) => {
            const b = bot.blockAt(p);
            return Boolean(b && b.boundingBox === "block");
        };
        const around = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [-1, 1], [1, -1], [-1, -1], [2, 0], [-2, 0], [0, 2], [0, -2]];
        const faces = [[0, -1, 0], [0, 1, 0], [1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1]];
        for (const dy of [0, -1, 1]) {
            for (const [dx, dz] of around) {
                const p = feet.offset(dx, dy, dz);
                if (!isAir(p)) continue;
                if (faces.some(([a, b, c]) => isSolid(p.offset(a, b, c)))) return p;
            }
        }
        return null;
    }

    async digPocket() {
        const bot = this.bot;
        const feet = bot.entity.position.floored();
        for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
            const p = feet.offset(dx, 0, dz);
            const b = bot.blockAt(p);
            if (!b || b.boundingBox !== "block" || /bedrock|obsidian/.test(b.name) || !bot.canDigBlock(b)) continue;
            try {
                if (bot.tool) await bot.tool.equipForBlock(b);
                await withTimeout(bot.dig(b, true), 12000, "pocket dig");
                return p;
            } catch (err) {
                /* try the next side */
            }
        }
        return null;
    }

    // Is there open sky above these feet (no solid block overhead)?
    skyAbove(feet) {
        for (let dy = 2; dy < 90 && feet.y + dy < 320; dy++) {
            const b = this.bot.blockAt(feet.offset(0, dy, 0));
            if (b && b.boundingBox === "block") return false;
        }
        return true;
    }

    solidOverhead(feet) {
        let n = 0;
        for (let dy = 2; dy < 90 && feet.y + dy < 320; dy++) {
            const b = this.bot.blockAt(feet.offset(0, dy, 0));
            if (b && b.boundingBox === "block") n++;
        }
        return n;
    }

    // Dig a staircase up to the surface: per step clear the jump headroom and
    // the two blocks of the next tread, then step up. Needs a pickaxe and no
    // block placement at all, which is why it works where pillaring does not.
    async surfaceUp(isAborted) {
        const bot = this.bot;
        const { goals } = require("mineflayer-pathfinder");
        const dirs = [[1, 0], [0, 1], [-1, 0], [0, -1]];
        let di = ((Math.round((bot.entity.yaw || 0) / (Math.PI / 2)) % 4) + 4) % 4;
        let steps = 0;
        let stalled = 0;
        for (; steps < 48; steps++) {
            if (!this.active || isAborted()) return `interrupted after ${steps} steps`;
            const feet = bot.entity.position.floored();
            if (this.skyAbove(feet)) return `ok, reached the surface after ${steps} steps`;
            const [dx, dz] = dirs[di];
            const toClear = [feet.offset(0, 2, 0), feet.offset(dx, 1, dz), feet.offset(dx, 2, dz), feet.offset(dx, 3, dz)];
            const danger = toClear.concat([feet.offset(dx, 0, dz)]).some((p) => /lava|water/.test(((bot.blockAt(p) || {}).name) || ""));
            if (danger) {
                di = (di + 1) % 4; // turn away from liquids
                continue;
            }
            for (const p of toClear) {
                const b = bot.blockAt(p);
                if (!b || b.boundingBox !== "block") continue;
                if (/bedrock/.test(b.name) || !bot.canDigBlock(b)) return `failed: cannot dig ${b.name} after ${steps} steps`;
                try {
                    if (bot.tool) await bot.tool.equipForBlock(b);
                } catch (err) {
                    /* dig with whatever is held */
                }
                await withTimeout(bot.dig(b, true), 15000, "staircase dig");
            }
            const tread = bot.blockAt(feet.offset(dx, 0, dz));
            if (!tread || tread.boundingBox !== "block") {
                di = (di + 1) % 4; // nothing to stand on ahead: turn
                continue;
            }
            try {
                await withTimeout(bot.pathfinder.goto(new goals.GoalBlock(feet.x + dx, feet.y + 1, feet.z + dz)), 6000, "staircase step");
            } catch (err) {
                bot.setControlState("forward", true);
                bot.setControlState("jump", true);
                await sleep(700);
                bot.clearControlStates();
            }
            if (bot.entity.position.floored().y <= feet.y) {
                stalled++;
                if (stalled > 2) {
                    di = (di + 1) % 4;
                    stalled = 0;
                }
            } else {
                stalled = 0;
            }
        }
        return `ok, climbed ${steps} steps, surface not yet reached`;
    }

    // Items to put away: everything the keep table does not want, and the
    // excess above a cap for bulk items; never the goal targets or food.
    junkToDeposit(inventory) {
        const out = {};
        const wanted = (name) =>
            (this.target && this.target.matches(name)) ||
            (this.highTargets || []).some((t) => t.matches && t.matches(name)) ||
            EDIBLE.includes(name);
        for (const [name, count] of Object.entries(inventory)) {
            if (wanted(name)) continue;
            let cap = KEEP_CAP[name];
            if (cap === undefined) {
                for (const [fam, c] of Object.entries(KEEP_CAP)) {
                    if (fam.startsWith("family:") && (FAMILIES[fam.slice(7)] || []).includes(name)) cap = c;
                }
            }
            if (cap !== undefined) {
                if (count > cap) out[name] = count - cap;
                continue;
            }
            if (KEEP_ALL_RE.test(name)) continue;
            out[name] = count;
        }
        return out;
    }

    freeSlots() {
        const bot = this.bot;
        const used = typeof bot.inventoryUsed === "function" ? bot.inventoryUsed() : 0;
        return INVENTORY_SLOTS - used;
    }

    // Chests whose contents are known (opened before), nearest first.
    chestsKnown(maxDistance = CHEST_REACH) {
        const bot = this.bot;
        const obs = (bot.obsList || []).find((o) => o.name === "nearbyChests");
        const out = [];
        if (!obs || !obs.chestsItems) return out;
        const here = bot.entity.position;
        for (const [key, items] of Object.entries(obs.chestsItems)) {
            if (!items || typeof items !== "object") continue;
            const m = String(key).match(/\(?\s*(-?\d+)[, ]+(-?\d+)[, ]+(-?\d+)/);
            if (!m) continue;
            const pos = new Vec3(parseInt(m[1], 10), parseInt(m[2], 10), parseInt(m[3], 10));
            const d = pos.distanceTo(here);
            if (d <= maxDistance) out.push({ pos, items, distance: Math.round(d) });
        }
        return out.sort((a, b) => a.distance - b.distance);
    }

    // Equipping strictly better armor is not a judgment call: do it in code.
    async autoEquipArmor() {
        const bot = this.bot;
        for (const piece of Object.keys(this.armorUpgrades(this.inventoryCounts()))) {
            const item = bot.inventory.items().find((i) => i.name === piece);
            const slot = FastLoop.ARMOR_SLOTS.find(([, suffix]) => piece.endsWith(`_${suffix}`));
            if (!item || !slot) continue;
            try {
                await withTimeout(bot.equip(item, slot[2]), 5000, "auto equip");
                console.log(`fastLoop: wearing ${piece}`);
                this.recent.push({ action: `equip:${piece}`, outcome: "ok (automatic)" });
                if (this.recent.length > RECENT_ACTIONS) this.recent.shift();
            } catch (err) {
                /* try again after the next action */
            }
        }
    }

    landmarkDistance(name) {
        const l = this.landmarks[name];
        if (!l || !this.bot.entity) return null;
        const p = this.bot.entity.position;
        return Math.round(Math.hypot(l.x - p.x, l.y - p.y, l.z - p.z));
    }

    setHome(pos) {
        this.home = pos ? { x: Math.floor(pos.x), y: Math.floor(pos.y), z: Math.floor(pos.z) } : null;
        if (this.home) console.log(`fastLoop: home base at ${this.home.x} ${this.home.y} ${this.home.z}`);
    }

    homeDistance() {
        if (!this.home || !this.bot.entity) return null;
        const p = this.bot.entity.position;
        return Math.round(Math.hypot(this.home.x - p.x, this.home.y - p.y, this.home.z - p.z));
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
        // armor is worn automatically by code (see autoEquipArmor)
        return options;
    }

    // slot -> [armor slot index in the inventory window, name suffix, equip destination]
    static get ARMOR_SLOTS() {
        return [[5, "helmet", "head"], [6, "chestplate", "torso"], [7, "leggings", "legs"], [8, "boots", "feet"]];
    }

    static armorTier(name) {
        const m = name && name.match(/^(leather|golden|chainmail|iron|diamond|netherite|turtle)_/);
        return m ? ["leather", "golden", "chainmail", "turtle", "iron", "diamond", "netherite"].indexOf(m[1]) : -1;
    }

    // { carried piece name -> currently worn piece name | null } for every upgrade available
    armorUpgrades(inventory) {
        const out = {};
        for (const [slot, suffix] of FastLoop.ARMOR_SLOTS) {
            const wornItem = this.bot.inventory && this.bot.inventory.slots ? this.bot.inventory.slots[slot] : null;
            const wornTier = wornItem ? FastLoop.armorTier(wornItem.name) : -1;
            let best = null;
            for (const name of Object.keys(inventory)) {
                if (!name.endsWith(`_${suffix}`)) continue;
                if (wornItem && wornItem.name === name && inventory[name] <= 1) continue; // only the worn one
                const tier = FastLoop.armorTier(name);
                if (tier > wornTier && (!best || tier > FastLoop.armorTier(best))) best = name;
            }
            if (best) out[best] = wornItem ? wornItem.name : null;
        }
        return out;
    }

    // ---- subgoal derivation (see goalPlanner.js) --------------------------
    // Decompose the goal into a requirement graph (recipes, smelting, block
    // drops, mob drops, tool tiers, stations, fuel) with quantities aggregated
    // per item, and offer the ready leaves as subgoals, deepest first.
    async deriveSubgoals(text, override, extraTargets, lookahead) {
        const { planGoal } = require("./goalPlanner");
        const result = await planGoal(this, text, override, extraTargets, lookahead);
        // never offer a step that is already done
        const live = [];
        for (const c of result.candidates) {
            if (c.text === text || !(await this.targetSatisfied(c.target))) live.push(c);
        }
        result.candidates = live;
        return result;
    }

    recipesFor(itemId) {
        if (!this._recipes) this._recipes = require("prismarine-recipe")(this.bot.version).Recipe;
        return this._recipes.find(itemId, null);
    }

    // Is a goal target (override form) already met right now?
    async targetSatisfied(override) {
        if (!override || override.none) return false;
        const t = await this.parseTarget("", override);
        if (!t) return false;
        const s = this.summarise(t);
        return s.have >= s.need; // what is held now, not what was gained since parsing
    }

    // For the brain: which of these candidate targets are already satisfied.
    async checkSatisfied(targets) {
        const out = [];
        for (const t of targets || []) out.push(await this.targetSatisfied(t));
        return out;
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
                if (!m) continue;
                // golden pickaxes harvest like wooden ones and are not a tier the bot tracks
                const idx = PICKAXE_TIER.indexOf(m[1] === "golden" ? "wooden" : m[1]);
                if (idx >= 0 && (lowest === null || idx < lowest)) lowest = idx;
            }
            if (lowest !== null && (tier === null || lowest < tier)) tier = lowest;
        }
        return tier === null ? null : PICKAXE_TIER[tier];
    }

    // The pathfinder may dig but never places blocks: its tower and bridge
    // moves send one placement at lift-off and bounce for many jumps before
    // one lands. Vertical movement is the pillar action and the staircase.
    configurePathfinder() {
        try {
            const { Movements } = require("mineflayer-pathfinder");
            const m = new Movements(this.bot, this.mcData);
            m.allow1by1towers = false;
            m.scafoldingBlocks = [];
            m.canDig = true;
            this.bot.pathfinder.setMovements(m);
            this.movements = m;
        } catch (err) {
            console.log("fastLoop: could not configure pathfinder movements:", err.message);
        }
    }

    // ---- lifecycle -------------------------------------------------------
    async setGoal(goal) {
        if (this.movements && this.bot.pathfinder) this.bot.pathfinder.setMovements(this.movements); // /step resets them
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
                // a milestone is several targets at once; all must hold to finish
                const specs = Array.isArray(goal.highGoal.targets) && goal.highGoal.targets.length
                    ? goal.highGoal.targets
                    : [goal.highGoal.target];
                this.highTargets = [];
                for (const spec of specs) {
                    const t = await this.parseTarget(goal.highGoal.text, spec || undefined);
                    if (t) this.highTargets.push(t);
                }
                this.highTarget = this.highTargets[0] || null;
                this.highGoalReached = false;
                console.log(
                    `fastLoop: high goal "${this.highGoal.text}" targets=${JSON.stringify(this.highTargets.map((t) => this.summarise(t)))}`
                );
            }
        } else if (this.goal.kind !== "standing") {
            this.highGoal = null;
            this.highTarget = null;
            this.highTargets = [];
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
        // A goal that is already met the moment it is posted is not a skill;
        // tell the brain so it re-selects without recording one.
        const initial = this.summarise(this.target);
        if (initial && initial.gained >= initial.need && this.goal.kind === "task") {
            this.goalReached = true;
            this.pushTrigger("goal_reached", { target: initial, instant: true, trace: [] });
        }
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
        await this.cancelCurrentTask();
    }

    // A primitive that timed out (collectBlock, a craft walking to its table,
    // a goto) is still driving the pathfinder. Starting the next primitive on
    // top of it is what produces "Path was stopped before it could be
    // completed": stop everything first, in the order botCleanup uses.
    async cancelCurrentTask() {
        const bot = this.bot;
        let cancelled = true;
        try {
            if (bot.collectBlock) await withTimeout(Promise.resolve(bot.collectBlock.cancelTask()), 3000, "cancelTask");
        } catch (err) {
            cancelled = false;
        }
        if (!cancelled && bot.collectBlock) {
            // The collect task did not answer pathfinder.stop() (a dig waiting on the
            // server, an equip that never returned). Left alone, every later collect()
            // queues behind it and each mine action times out in turn. Reset it by hand.
            console.log("fastLoop: collect task did not cancel in time; resetting collectBlock state");
            try {
                bot.stopDigging();
            } catch (err) {
                /* ignore */
            }
            try {
                if (bot.collectBlock.targets) bot.collectBlock.targets.clear();
            } catch (err) {
                /* ignore */
            }
            try {
                bot.emit("collectBlock_finished"); // releases every cancelTask() waiter
            } catch (err) {
                /* ignore */
            }
        }
        try {
            if (bot.pvp && bot.pvp.stop) await withTimeout(Promise.resolve(bot.pvp.stop()), 2000, "pvp.stop");
        } catch (err) {
            /* ignore */
        }
        try {
            bot.pathfinder.stop();
        } catch (err) {
            /* ignore */
        }
        try {
            bot.pathfinder.setGoal(null);
        } catch (err) {
            /* ignore */
        }
        try {
            bot.clearControlStates();
        } catch (err) {
            /* ignore */
        }
        try {
            bot.stopDigging();
        } catch (err) {
            /* not digging */
        }
        await sleep(150); // let the stopped tasks settle before the next primitive
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
        if (override && override.freeSlots) {
            // reached when at least this many inventory slots are empty
            const n = Math.max(1, parseInt(override.freeSlots, 10) || 1);
            return { kind: "freeSlots", key: `free:${n}`, need: n, matches: () => false, startHave: 0 };
        }
        if (override && override.nearPlayer) {
            // reached when the bot stands within `distance` blocks of that player
            const name = String(override.nearPlayer);
            const distance = Math.max(1, parseInt(override.distance, 10) || 3);
            return { kind: "nearPlayer", key: `player:${name}`, player: name, distance, need: 1, matches: () => false, startHave: 0 };
        }
        if (override && override.give) {
            // reached when `count` of the item have been dropped at a player's feet (counted in code)
            const g = override.give;
            const item = String(g.item || "");
            const to = String(g.to || "");
            const need = Math.max(1, parseInt(g.count, 10) || 1);
            return { kind: "give", key: `give:${item}:${to}`, item, player: to, need, given: 0, matches: matcherFor(item), startHave: 0 };
        }
        if (override && override.dimension) {
            // reached when the bot is in that dimension (overworld, the_nether, the_end)
            const dim = String(override.dimension);
            return { kind: "dimension", key: `dimension:${dim}`, dimension: dim, need: 1, matches: () => false, startHave: 0 };
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
        if (target.kind === "freeSlots") {
            const have = this.freeSlots();
            return { item: target.key, need: target.need, have, gained: have };
        }
        if (target.kind === "nearPlayer") {
            const d = this.playerDistance(target.player);
            const have = d !== null && d <= target.distance ? 1 : 0;
            return { item: target.key, need: 1, have, gained: have, playerDistance: d };
        }
        if (target.kind === "give") {
            const held = countMatching(this.inventoryCounts(), target.matches);
            return { item: target.key, need: target.need, have: target.given, gained: target.given, holding: held, playerDistance: this.playerDistance(target.player) };
        }
        if (target.kind === "dimension") {
            const current = (this.bot.game && this.bot.game.dimension) || "overworld";
            const have = String(current).replace(/^minecraft:/, "") === target.dimension ? 1 : 0;
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

    // Players are only visible inside render distance: entity may be missing.
    playerEntity(name) {
        const p = (this.bot.players || {})[name];
        return p && p.entity && p.entity.position ? p.entity : null;
    }

    playerDistance(name) {
        const e = this.playerEntity(name);
        if (!e || !this.bot.entity) return null;
        return Math.round(e.position.distanceTo(this.bot.entity.position));
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
        // Underground means rock overhead, not "no dirt nearby": caves have
        // dirt and gravel, and the biome name alone says nothing about depth.
        const feet = bot.entity.position.floored();
        const underground = !this.skyAbove(feet);
        const block = bot.blockAt(bot.entity.position);
        let biome = "unknown";
        if (block && block.biome) {
            const byId = this.mcData.biomes && this.mcData.biomes[block.biome.id];
            biome = block.biome.name || (byId && byId.name) || "unknown";
        }
        const t = bot.time ? bot.time.timeOfDay : 0;
        return {
            biome: underground ? "underground" : biome,
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
        // 1b. the high goal (parsed once; survives subgoal changes). A high goal
        // counts by what the bag holds, like the original critic: "Mine 5 iron
        // ore" is done when 5 raw iron are held, not when 5 more are mined.
        if (this.highTargets && this.highTargets.length && !this.highGoalReached) {
            const highs = this.highTargets.map((t) => this.summarise(t));
            if (highs.every((h) => h.have >= h.need)) {
                this.highGoalReached = true;
                this.pushTrigger("high_goal_reached", { target: highs[0], targets: highs, highGoalId: this.highGoal.id });
            }
        }
        // 1c. a furnace cooking the target is progress in the making
        if (this.target && this.target.matches && this.furnaceJobs.some((j) => this.target.matches(j.output) && j.readyAt > now)) {
            this.lastProgressAt = now;
            this.actionsSinceProgress = 0;
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
        // never offer to mine the bot's own crafting table, furnace or chest
        // ...nor blocks the current pickaxe cannot harvest (the collect plugin
        // would refuse at the block: "I need at least a stone_pickaxe")
        const tier = PICKAXE_TIER.indexOf(this.toolTier());
        const mineable = resources.filter(([name]) => {
            if (LANDMARK_BLOCKS.includes(name)) return false;
            const needed = this.requiredPickaxe([name]);
            return !needed || PICKAXE_TIER.indexOf(needed) <= tier;
        });
        for (const [name, info] of mineable.slice(0, MAX_MENU_MINE)) {
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
            // a station already carried or standing nearby is not worth crafting again
            if (LANDMARK_BLOCKS.includes(item) && (snap.inventory[item] || this.landmarkDistance(item) !== null && this.landmarkDistance(item) <= 32)) continue;
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
                    const n = Math.min(snap.inventory[raw], FURNACE_MAX_BATCH);
                    menu[`smelt:${raw}`] =
                        `Load the furnace with ${n} ${raw} (-> ${out}) and ${fuel}, then leave it cooking ` +
                        `(about ${n * SMELT_SECONDS_PER_ITEM}s; come back with furnace:collect)`;
                }
            }
        }
        for (const job of this.furnaceJobs) {
            const d = Math.round(job.pos.distanceTo(pos));
            if (d > CHEST_REACH) continue;
            const left = Math.max(0, Math.round((job.readyAt - Date.now()) / 1000));
            menu["furnace:collect"] =
                `Collect ${job.count} ${job.output} from the furnace ${d} blocks away ` +
                (left ? `(ready in about ${left}s; waits if early)` : "(ready now)");
            break;
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
        const pillarCooling = this.pillarFailedAt && Date.now() - this.pillarFailedAt < PILLAR_RETRY_MS;
        if (ENABLE_PILLAR && pillarBlock && !pillarCooling && (!above || above.name === "air" || above.name === "cave_air")) {
            menu["pillar:up"] =
                `Jump-and-place a ${PILLAR_HEIGHT}-block tower of ${pillarBlock} straight up ` +
                `(${snap.inventory[pillarBlock]} available; escapes pits and water, reaches the surface)`;
        }
        for (const [tool, description] of Object.entries(this.equipOptions(snap))) {
            menu[`equip:${tool}`] = description;
        }
        const junk = this.junkToDeposit(snap.inventory);
        const junkStacks = Object.keys(junk).length;
        const chestNear = bot.findBlock({ matching: this.mcData.blocksByName.chest.id, maxDistance: 32 });
        const used = snap.inventoryUsed === null ? 0 : snap.inventoryUsed;
        const wantsSpace = this.target && this.target.kind === "freeSlots";
        if (junkStacks && (used >= INVENTORY_DEPOSIT_AT || wantsSpace)) {
            const summary = Object.entries(junk).slice(0, 6).map(([n, c]) => `${c} ${n}`).join(", ") + (junkStacks > 6 ? ", ..." : "");
            if (chestNear) {
                menu["chest:deposit"] =
                    `Put ${summary} into the chest ${Math.round(chestNear.position.distanceTo(pos))} blocks away ` +
                    `(${used}/36 slots used; keeps tools, ore, ingots, food, torches and a stack of building blocks)`;
            } else if (snap.inventory.chest) {
                menu["place:chest"] = `Place the chest from inventory to store junk in (${used}/36 slots used, no chest nearby)`;
            }
            if (!chestNear && (used >= INVENTORY_DISCARD_AT || wantsSpace)) {
                menu["discard:junk"] = `Throw away ${summary} (${used}/36 slots used, no chest within reach; frees the bag to keep mining)`;
            }
        }
        // take instead of mine: a known chest holds what the goal needs
        if (this.target && this.target.kind !== "nearBlock" && this.target.kind !== "freeSlots") {
            for (const chest of this.chestsKnown()) {
                const held = Object.entries(chest.items).filter(([n]) => this.target.matches(n));
                if (!held.length) continue;
                const total = held.reduce((s, [, c]) => s + c, 0);
                menu[`withdraw:${held[0][0]}`] =
                    `Take ${held.map(([n, c]) => `${c} ${n}`).join(", ")} from the chest ${chest.distance} blocks away ` +
                    `(${total} available there, no mining needed)`;
                break;
            }
        }
        if (this.target && (this.target.kind === "nearPlayer" || this.target.kind === "give")) {
            const name = this.target.player;
            const ent = this.playerEntity(name);
            if (ent) {
                const d = Math.round(ent.position.distanceTo(pos));
                const dir = traversal.compassNameOf(ent.position.x - pos.x, ent.position.z - pos.z);
                if (this.target.kind === "give" && d <= 4) {
                    const left = this.target.need - this.target.given;
                    const held = countMatching(this.inventoryCounts(), this.target.matches);
                    menu[`give:${this.target.item}`] =
                        `Drop ${Math.min(left, held)} ${this.target.item} at the feet of player ${name} (${d} blocks away; ` +
                        `${held} in the bag, ${left} still to hand over)`;
                }
                if (d > 2) {
                    menu["goto:player"] = `Walk to player ${name}, ${d} blocks ${dir} at x=${Math.floor(ent.position.x)} y=${Math.floor(ent.position.y)} z=${Math.floor(ent.position.z)}`;
                }
            }
        }
        const homeDist = this.homeDistance();
        if (homeDist !== null && homeDist > 16) {
            menu["return:home"] =
                `Walk back to the home base ${homeDist} blocks away (crafting table, furnace and chest; ` +
                `deposit junk, restock, craft)`;
        }
        for (const [name, l] of Object.entries(snap.landmarks)) {
            if (l.distance > WALK_DISTANCE) {
                menu[`return:${name}`] = `Walk back to the last ${name} seen, ${l.distance} blocks away at x=${l.x} y=${l.y} z=${l.z}`;
            }
        }
        if (this.fingerprint().biome === "underground" && this.toolTier() !== "none") {
            const overhead = this.solidOverhead(pos.floored());
            menu["surface:up"] =
                `Dig a staircase up to the surface (about ${overhead} solid blocks overhead; needs no block placing). ` +
                "Use this to get wood, food or daylight when underground";
        }
        // Waiting twice in a row achieves nothing: take it off the menu so the
        // next pick has to be a move (the loop was seen idling in wait/nudge).
        // no "wait": standing still never reaches a goal, and the menu always has moves
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
                // One line per Jev call, kept for the brain to print on its console
                // (Node's stdout only reaches the log file) and echoed here.
                const sinceLast = this._lastDecisionAt ? (started - this._lastDecisionAt) / 1000 : null;
                this._lastDecisionAt = started;
                const entry = {
                    n: this.stats.decisions,
                    at: started,
                    sinceLast,
                    ms,
                    action: a.choice,
                    confidence: a.confidence,
                    danger: answers.danger.noul,
                    stuck: answers.stuck.noul,
                    valid: answers.subgoalValid.noul,
                    goalReached: this.lastDecision.goalReached,
                    options: Object.keys(menu).length,
                    goal: this.goal ? this.goal.text : null,
                    inMenu: Boolean(menu[a.choice]),
                };
                this.decisionLog.push(entry);
                if (this.decisionLog.length > 200) this.decisionLog.shift();
                console.log(
                    `fastLoop: jev #${entry.n} ${sinceLast === null ? "" : `+${sinceLast.toFixed(1)}s `}${ms}ms -> ${a.choice} ` +
                        `(${a.confidence.toFixed(2)}) danger ${entry.danger.toFixed(2)} stuck ${entry.stuck.toFixed(2)} ` +
                        `valid ${entry.valid.toFixed(2)} | ${entry.options} options | ${entry.goal}`
                );
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
        if (menu["surface:up"] && this.target && /log|planks|stick|wood/.test(this.target.key)) return "surface:up";
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
        if (walks.length) return walks[Math.floor(Math.random() * walks.length)];
        const any = Object.keys(menu);
        return any.length ? any[0] : "walk:north";
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
                    const summary = this.targetSummary();
                    const want = summary && this.target && this.target.matches(SMELTABLE[arg] || "") ? Math.max(1, summary.need - summary.have) : FURNACE_MAX_BATCH;
                    return await withTimeout(this.smeltBatch(arg, want), CRAFT_TIMEOUT_MS * 2, action);
                }
                case "furnace": {
                    let aborted = false;
                    this.abortCurrent = () => {
                        aborted = true;
                        bot.pathfinder.setGoal(null);
                    };
                    return await withTimeout(this.collectFurnace(() => aborted), 90000, action);
                }
                case "place": {
                    // a furnace or chest goes next to the crafting table when one is known nearby
                    if ((arg === "furnace" || arg === "chest") && this.landmarks.crafting_table) {
                        const d = this.landmarkDistance("crafting_table");
                        if (d !== null && d > 3 && d <= 48) {
                            const t = this.landmarks.crafting_table;
                            const r = await gotoBounded(new goals.GoalNear(t.x, t.y, t.z, 2), WALK_TIMEOUT_MS * 3);
                            if (r !== "ok") return `failed: could not reach the crafting table (${r})`;
                        }
                    }
                    let spot = this.findPlaceSpot();
                    if (!spot) spot = await this.digPocket();
                    if (!spot) return "failed: no space to place a block";
                    await withTimeout(this.prims.placeItem(bot, arg, spot), CRAFT_TIMEOUT_MS, action);
                    return this.blockNearby((b) => b === arg) ? "ok" : "failed: block was not placed";
                }
                case "surface": {
                    let aborted = false;
                    this.abortCurrent = () => {
                        aborted = true;
                        bot.pathfinder.setGoal(null);
                        bot.clearControlStates();
                    };
                    return await withTimeout(this.surfaceUp(() => aborted), 120000, action);
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
                    const armor = FastLoop.ARMOR_SLOTS.find(([, suffix]) => arg.endsWith(`_${suffix}`));
                    await withTimeout(bot.equip(item, armor ? armor[2] : "hand"), 5000, action);
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
                    const l = arg === "home" ? this.home : this.landmarks[arg];
                    if (!l) return `failed: no ${arg} remembered`;
                    const dist = Math.hypot(l.x - pos.x, l.y - pos.y, l.z - pos.z);
                    if (dist > 80) {
                        // a long way: one leg of 60 blocks toward it, the loop will pick again
                        const f = 60 / dist;
                        const r = await gotoBounded(
                            new goals.GoalNearXZ(Math.floor(pos.x + (l.x - pos.x) * f), Math.floor(pos.z + (l.z - pos.z) * f), 3),
                            WALK_TIMEOUT_MS * 4
                        );
                        return r === "ok" ? `ok, ${Math.round(dist - 60)} blocks still to go` : r;
                    }
                    return await gotoBounded(new goals.GoalNear(l.x, l.y, l.z, 2), WALK_TIMEOUT_MS * 4);
                }
                case "goto": {
                    const name = this.target && this.target.player;
                    const ent = name ? this.playerEntity(name) : null;
                    if (!ent) return `failed: player ${name || "?"} not in view`;
                    const p = ent.position;
                    const dist = Math.hypot(p.x - pos.x, p.y - pos.y, p.z - pos.z);
                    if (dist > 80) {
                        const f = 60 / dist;
                        const r = await gotoBounded(
                            new goals.GoalNearXZ(Math.floor(pos.x + (p.x - pos.x) * f), Math.floor(pos.z + (p.z - pos.z) * f), 3),
                            WALK_TIMEOUT_MS * 4
                        );
                        return r === "ok" ? `ok, ${Math.round(dist - 60)} blocks still to go` : r;
                    }
                    return await gotoBounded(new goals.GoalNear(p.x, p.y, p.z, 2), WALK_TIMEOUT_MS * 4);
                }
                case "give": {
                    const t = this.target;
                    if (!t || t.kind !== "give") return "failed: no give target";
                    const ent = this.playerEntity(t.player);
                    if (!ent) return `failed: player ${t.player} not in view`;
                    if (ent.position.distanceTo(pos) > 6) return "failed: too far from the player, walk closer first";
                    const def = this.mcData.itemsByName[t.item];
                    if (!def) return `failed: unknown item ${t.item}`;
                    const before = countMatching(this.inventoryCounts(), t.matches);
                    const count = Math.min(t.need - t.given, before);
                    if (count <= 0) return before ? "ok" : `failed: no ${t.item} in the bag`;
                    try {
                        await bot.lookAt(ent.position.offset(0, 1, 0), true);
                        await withTimeout(bot.toss(def.id, null, count), 4000, "toss");
                    } catch (err) {
                        return `failed: ${err.message.split("\n")[0].slice(0, 60)}`;
                    }
                    const dropped = before - countMatching(this.inventoryCounts(), t.matches);
                    t.given += Math.max(0, dropped);
                    return dropped > 0 ? `ok, handed ${dropped} ${t.item} to ${t.player}` : "failed: nothing dropped";
                }
                case "discard": {
                    const junk = this.junkToDeposit(this.inventoryCounts());
                    let tossed = 0;
                    for (const [name, count] of Object.entries(junk)) {
                        const def = this.mcData.itemsByName[name];
                        if (!def) continue;
                        try {
                            await withTimeout(bot.toss(def.id, null, count), 4000, "toss");
                            tossed++;
                            await sleep(150);
                        } catch (err) {
                            /* next item */
                        }
                    }
                    return tossed ? `ok, tossed ${tossed} kind(s) of junk, ${this.freeSlots()} slots free` : "failed: nothing tossed";
                }
                case "withdraw": {
                    const chest = this.chestsKnown().find((c) => Object.keys(c.items).some((n) => this.target && this.target.matches(n)));
                    if (!chest) return "failed: no known chest holds that";
                    const want = {};
                    const summary = this.targetSummary();
                    let remaining = summary ? Math.max(1, summary.need - summary.have) : 8;
                    for (const [n, c] of Object.entries(chest.items)) {
                        if (!this.target.matches(n) || remaining <= 0) continue;
                        want[n] = Math.min(c, remaining);
                        remaining -= want[n];
                    }
                    const before = this.freeSlots();
                    await withTimeout(this.prims.getItemFromChest(bot, chest.pos, want), CRAFT_TIMEOUT_MS * 2, action);
                    return this.freeSlots() <= before ? "ok" : "took nothing";
                }
                case "wait":
                default:
                    await sleep(2000);
                    return "ok";
            }
        } catch (err) {
            const msg = err && err.message ? err.message.split("\n")[0].slice(0, 100) : String(err);
            if (/timed out/.test(msg)) {
                await this.cancelCurrentTask(); // the primitive is still running: stop it
                return "timeout";
            }
            return `failed: ${msg}`;
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
            if (this.checkServerPaused() || !this.goal) {
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
            // the server may have been paused mid-action: that outcome is noise
            if (this.checkServerPaused()) continue;
            if (replayed && !/^ok/.test(outcome)) {
                console.log(`fastLoop: replay step "${action}" ${outcome}, handing control to Jev`);
                this.replay = [];
            }
            if (!/^ok/.test(outcome)) await sleep(400); // no hot loop on instant failures
            const after = this.targetSummary();
            const gain = before && after ? after.gained - before.gained : 0;
            this.stats.actions++;
            this.actionsSinceProgress = gain > 0 ? 0 : this.actionsSinceProgress + 1;
            await this.autoEquipArmor();
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
        const decisions = drain ? this.decisionLog.splice(0) : [...this.decisionLog];
        const chat = drain ? this.playerChat.splice(0) : [...this.playerChat];
        let fingerprint = null;
        try {
            fingerprint = this.bot.entity ? this.fingerprint() : null;
        } catch (err) {
            /* ignore */
        }
        return {
            running: this.active,
            botName: this.bot.username || null,
            players: Object.keys(this.bot.players || {}).filter((n) => !this.isBotName(n)),
            chat,
            serverPaused: this.serverPaused,
            goal: this.goal,
            target: this.targetSummary(),
            goalReached: this.goalReached,
            highGoal: this.highGoal || null,
            highTarget: this.summarise(this.highTarget),
            highTargets: (this.highTargets || []).map((t) => this.summarise(t)),
            highGoalReached: Boolean(this.highGoalReached),
            inventory: this.bot.entity ? this.inventoryCounts() : {},
            freeSlots: this.bot.entity ? this.freeSlots() : null,
            furnaceJobs: this.furnaceJobs.map((j) => ({ input: j.input, output: j.output, count: j.count, readyInSeconds: Math.max(0, Math.round((j.readyAt - Date.now()) / 1000)), position: { x: j.pos.x, y: j.pos.y, z: j.pos.z } })),
            home: this.home || null,
            homeDistance: this.homeDistance(),
            landmarks: this.landmarks,
            triggers,
            recentActions: this.recent,
            trace: this.trace,
            collapsedTrace: this.collapsedTrace(),
            fingerprint,
            lastDecision: this.lastDecision,
            stats: this.stats,
            decisions,
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

module.exports = {
    inject, FastLoop, findTargetCandidates, countMatching, matcherFor, loadPrimitives,
    textTokens, wordForms, phraseIn,
};
