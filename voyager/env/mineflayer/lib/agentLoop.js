// The Jev action loop (PLAN.md sections 5.2, 7, 8). Python posts one subtask
// (description + VerifySpec + allowed blocks + budget); this loop then
// repeats: observe -> menu -> one Jev request (action Choice plus danger,
// progress and completable Nouls) -> execute one bounded action -> record the
// outcome and state delta -> check the subtask's spec. It raises triggers for
// Python (subtask_done, builder_wanted, stuck, budget_exhausted, danger) that
// are drained by GET /agent/status. Jev never sees code: the menu lines are
// the whole interface to an action.
const { Vec3 } = require("vec3");
const jev = require("./jevTraversal");
const observe = require("./observe");
const base = require("./baseActions");
const verify = require("./verify");
const { PrimitiveRegistry, guardedBot } = require("./primitiveRegistry");

const DECISION_FLOOR_MS = 500; // minimum gap between decisions
const ACTION_TIMEOUT_MS = 20 * 1000;
const COMPLETABLE_EVERY = 10; // actions between "completable with this menu?" checkpoints
const COMPLETABLE_EVERY_BARE = 5; // ... when no learned action has been on the menu for that long
const COMPLETABLE_NO = 0.5; // Noul below this at a checkpoint = "no"
const DANGER_YES = 0.7;
const PROGRESS_LOW = 0.3;
const PROGRESS_LOW_STREAK = 3;
const STUCK_WINDOW = 10; // position / inventory unchanged over this many actions
const REPEAT_WINDOW = 5; // same action id this many times in a row
const HEALTH_WINDOW = 5;
const HEALTH_DROP = 6;
const ACTION_COOLDOWN_DECISIONS = 4; // an action that just failed sits out this many decisions
const LOOK_MEMORY = 6; // actions a look's revealed block / a repeated look is remembered for
const NO_CHANGE_REPEATS = 3; // same action this many times with no position/inventory/cursor change -> cooldown
const SAMPLE_TEMPERATURE = 0.6; // < 1 sharpens Jev's distribution: mostly the top pick, sometimes the runner-up
const SAMPLE_TEMPERATURE_STUCK = 1.5; // > 1 flattens it when the bot is repeating itself

// "*_log 9.4" : the nearest non-hazard block the subtask cares about, for the log line
function nearestTargetText(snap) {
    let best = null;
    for (const [k, b] of Object.entries((snap && snap.blocks) || {})) {
        if (["lava", "water", "fire", "cactus"].includes(k)) continue;
        const d = Math.hypot(b.dx, b.dy, b.dz);
        if (!best || d < best.d) best = { k, d };
    }
    return best ? `${best.k} ${best.d.toFixed(1)}` : "none";
}

// Item names a declared purpose says the action produces: has_item and has_equipped
// predicates (not under a `not`). Empty when the purpose is about places or status.
function purposeItems(spec, out = []) {
    if (!spec || typeof spec !== "object") return out;
    for (const [k, v] of Object.entries(spec)) {
        if (k === "all" || k === "any") (Array.isArray(v) ? v : []).forEach((s) => purposeItems(s, out));
        else if ((k === "has_item" || k === "has_equipped") && v && typeof v.name === "string" && !out.includes(v.name)) out.push(v.name);
    }
    return out;
}

// Grounded success for a generated action: it must change the inventory, move
// the bot at least a block, or place a block. Turning the head does not count.
function hadEffect(record, placedBefore, placedAfter) {
    const d = record.delta || {};
    if (d.inv && Object.keys(d.inv).length) return true;
    if ((d.pos || 0) >= 1) return true;
    return placedAfter > placedBefore;
}

// Did this action record change anything observable?
function changedWorld(record) {
    const d = record.delta || {};
    if ((d.pos || 0) >= 1) return true;
    if (d.inv && Object.keys(d.inv).length) return true;
    if (d.cursor) return true;
    return /^failed/i.test(record.outcome || "") ? false : /moved [1-9]|mined|crafted|placed|gained|collected|picked/i.test(record.outcome || "");
}

// Sample one menu id from Jev's probabilities at the given temperature.
// Options not on the menu (cooled down since) are dropped; null when nothing usable.
function sampleChoice(probabilities, menu, temperature) {
    const entries = Object.entries(probabilities || {}).filter(([id, p]) => id in menu && p > 0);
    if (!entries.length) return null;
    const t = Math.max(0.05, temperature || 1);
    const weights = entries.map(([, p]) => Math.pow(p, 1 / t));
    const total = weights.reduce((s, w) => s + w, 0);
    let r = Math.random() * total;
    for (let i = 0; i < entries.length; i++) {
        r -= weights[i];
        if (r <= 0) return entries[i][0];
    }
    return entries[entries.length - 1][0];
}
const DANGER_TRIGGER_GAP_MS = 10 * 1000;
const HOSTILE = /^(zombie|skeleton|creeper|spider|cave_spider|enderman|witch|drowned|husk|stray|pillager|phantom|slime|zombie_villager|piglin|blaze|ghast|magma_cube|hoglin|vindicator|evoker|ravager|warden)$/;
const ATTACK_LIKE = /attack|fight|kill|hit|strike|defend/;
const ITEM_KINDS = new Set(["craft", "mine", "smelt", "kill", "collect"]);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function withTimeout(promise, ms, label) {
    let timer;
    const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms / 1000}s`)), ms);
    });
    Promise.resolve(promise).catch(() => {});
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function diffCounts(before, after) {
    const out = {};
    for (const k of new Set([...Object.keys(before), ...Object.keys(after)])) {
        const d = (after[k] || 0) - (before[k] || 0);
        if (d) out[k] = d;
    }
    return out;
}

class AgentLoop {
    constructor(bot) {
        this.bot = bot;
        this.mcData = require("minecraft-data")(bot.version);
        this.registry = new PrimitiveRegistry(this);
        this.placedByMe = {}; // block name -> {x,y,z}
        this.recent = []; // action records, oldest first (capped)
        this.subtask = null;
        this.allowedBlocks = [];
        this.treeItems = [];
        this.running = false;
        this.idle = false; // budget exhausted: keep the subtask, stop acting
        this._runPromise = null;
        this.pending = []; // triggers for Python
        this.stats = { decisions: 0, jev_ms: 0 };
        this.lastDecision = null;
        this.lastMenu = {};
        this.cooldowns = new Map(); // action id -> decisions left
        this.actionsSpent = 0;
        this.actionsSinceCheckpoint = 0;
        this.lastCompletable = null;
        this.progressLowStreak = 0;
        this.lastProgressAt = Date.now();
        this.actionsSinceProgress = 0;
        this.lastProgressPos = null;
        this.lastDangerAt = 0;
        this.stuckFiredAt = { position: -1, inventory: -1, repeat: -1, health: -1, progress: -1 };
    }

    // ---- helpers available to primitives as loop.* -------------------------
    inventoryCounts() {
        return observe.inventoryCounts(this.bot);
    }
    freeSlots() {
        const slots = this.bot.inventory && this.bot.inventory.slots ? this.bot.inventory.slots : [];
        let used = 0;
        for (let i = 9; i <= 44; i++) if (slots[i]) used++;
        return 36 - used;
    }
    nearestBlocks(matches, maxDistance, count) {
        return observe.nearestBlocks(this.bot, this.mcData, matches, maxDistance, count);
    }
    // Family-aware helpers for generated primitives: "*_log" matches any log.
    matcher(key) {
        return observe.matcherFor(key);
    }
    countMatching(key, inventory) {
        const inv = inventory || this.inventoryCounts();
        const m = observe.matcherFor(key);
        let n = 0;
        for (const [name, c] of Object.entries(inv)) if (m(name)) n += c;
        return n;
    }
    // Recipes the bot can craft right now for an item name ("stick", "oak_planks");
    // pass the crafting table block for 3x3 recipes, null for 2x2 ones.
    // Recipes craftable NOW for an item name. A family name ("*_planks") tries
    // every member (oak_planks, spruce_planks, ...) so the caller need not know
    // which wood the inventory holds. Each recipe carries .resultName.
    recipesFor(itemName, tableBlock) {
        const names = String(itemName || "").startsWith("*_")
            ? Object.keys(this.mcData.itemsByName).filter(observe.matcherFor(itemName))
            : [itemName];
        const out = [];
        for (const name of names) {
            const item = this.mcData.itemsByName[name];
            if (!item) continue;
            for (const r of this.bot.recipesFor(item.id, null, 1, tableBlock || null) || []) {
                r.resultName = name;
                out.push(r);
            }
        }
        return out;
    }
    nearestEntity(names, maxDistance) {
        return observe.nearestEntity(this.bot, names, maxDistance);
    }
    freeSpotNear(pos, radius) {
        return observe.freeSpotNear(this.bot, pos, radius);
    }
    recordPlacement(pos, name) {
        if (!pos || !name) return;
        this.placedByMe[name] = { x: Math.floor(pos.x), y: Math.floor(pos.y), z: Math.floor(pos.z) };
    }
    get sinceProgress() {
        return { actions: this.actionsSinceProgress, seconds: Math.round((Date.now() - this.lastProgressAt) / 1000) };
    }

    // ---- observation / verification ---------------------------------------
    snapshot() {
        return observe.snapshot(this.bot, this);
    }

    verifySpec(spec) {
        return verify.evaluate(this.bot, this, spec);
    }

    async verifyCurrent() {
        if (!this.subtask) return { ok: false, detail: "no subtask" };
        return this.verifySpec(this.subtask.verify);
    }

    // ---- subtask lifecycle --------------------------------------------------
    async setSubtask(body) {
        const b = body || {};
        const err = verify.validate(b.verify);
        if (err) return { ok: false, error: `verify: ${err}` };
        if (!b.id || !b.description) return { ok: false, error: "id and description are required" };
        // A re-post of the same id is a RETRY (PLAN.md section 8): the budget and
        // counters reset, but `recent` stays so Jev sees what already failed here.
        const isRetry = Boolean(this.subtask && this.subtask.id === String(b.id));
        this.subtask = {
            id: String(b.id),
            description: String(b.description),
            verify: b.verify,
            kind: b.kind || "goal",
            budget_actions: Number(b.budget_actions) > 0 ? Number(b.budget_actions) : 60,
        };
        const allowed = new Set(Array.isArray(b.allowed_blocks) ? b.allowed_blocks.map(String) : []);
        for (const n of verify.itemsIn(b.verify)) if (this.mcData.blocksByName[n] || n.startsWith("*_")) allowed.add(n);
        this.allowedBlocks = [...allowed];
        this.treeItems = Array.isArray(b.tree_items) ? b.tree_items.map(String) : [];
        if (!isRetry) {
            this.recent = [];
            this.cooldowns.clear();
        }
        this.actionsSpent = 0;
        this.actionsSinceCheckpoint = 0;
        this.lastCompletable = null;
        this.progressLowStreak = 0;
        this.lastProgressAt = Date.now();
        this.actionsSinceProgress = 0;
        this.lastProgressPos = this.bot.entity ? this.bot.entity.position.clone() : null;
        this.idle = false;
        for (const k of Object.keys(this.stuckFiredAt)) this.stuckFiredAt[k] = -1;
        console.log(`agentLoop: ${isRetry ? "retry of " : ""}subtask ${this.subtask.id} "${this.subtask.description}" budget ${this.subtask.budget_actions}`);
        if (!this.running) {
            this.running = true;
            this._runPromise = this.run().catch((e) => console.log("agentLoop: run crashed:", e && e.stack ? e.stack : e));
        }
        return { ok: true };
    }

    async stop() {
        this.running = false;
        this.subtask = null;
        if (this._runPromise) {
            try {
                await withTimeout(this._runPromise, 5000, "agentLoop.stop");
            } catch (e) {
                /* keep going */
            }
        }
        try {
            this.bot.clearControlStates();
        } catch (e) {
            /* fake bots */
        }
        return { ok: true };
    }

    // ---- menu ----------------------------------------------------------------
    // A compact line of what the agent sees, for reading runs back afterwards.
    logState(snap) {
        try {
            const b = snap.bot || {};
            const hit = (snap.view || []).find((v) => v.hit);
            const blocks = Object.entries(snap.blocks || {}).map(([k, v]) => `${k}@${Math.round(Math.hypot(v.dx, v.dy, v.dz))}`).join(",");
            console.log(
                `agentLoop: state pos ${b.position.x},${b.position.y},${b.position.z} biome ${b.biome} ` +
                    `${b.inWater ? "IN_WATER " : ""}${b.onGround ? "" : "AIRBORNE "}hp ${b.health} food ${b.food} ` +
                    `facing ${b.yaw}/${b.pitch} sees ${hit ? `${hit.block}@${hit.from}` : "nothing"} blocks [${blocks}] ` +
                    `inv ${JSON.stringify(snap.inventory)}`
            );
        } catch (err) {
            /* logging only */
        }
    }

    // Which learned actions have nothing left to do: their purpose spec holds now.
    async refreshPurposes() {
        const done = new Set();
        for (const p of this.registry.items.values()) {
            if (!p.purposeSpec || p.retired) continue;
            try {
                const r = await this.verifySpec(p.purposeSpec);
                if (r && r.ok) done.add(p.name);
            } catch (err) {
                /* unknown: keep offering it */
            }
        }
        this.satisfiedPurposes = done;
    }

    buildMenu(snap) {
        const menu = { ...base.menu(snap, this.bot, this), ...this.registry.menuEntries(snap) };
        for (const [id, left] of this.cooldowns) {
            if (left <= 0) this.cooldowns.delete(id);
            else if (id in menu && !id.startsWith("move:")) delete menu[id]; // movement always stays available
        }
        this.lastMenu = menu;
        return menu;
    }

    provenMenuIds(menu) {
        return Object.keys(menu).filter((id) => {
            if (!id.startsWith("x:")) return true;
            const p = this.registry.get(id.slice(2));
            return p && !this.registry.isTrial(p);
        });
    }

    // The last few actions are the same id and none of them changed anything.
    repeatingWithoutChange() {
        const r = this.recent.slice(-NO_CHANGE_REPEATS);
        if (r.length < NO_CHANGE_REPEATS) return false;
        return r.every((x) => x.action === r[0].action && !changedWorld(x));
    }

    // Least recently used movement action: the no-signal fallback.
    fallbackAction(menu) {
        const moves = Object.keys(menu).filter((id) => id.startsWith("move:") && id !== "move:up");
        if (!moves.length) return Object.keys(menu)[0] || "wait";
        const lastUse = new Map(moves.map((m) => [m, -1]));
        this.recent.forEach((r, i) => {
            if (lastUse.has(r.action)) lastUse.set(r.action, i);
        });
        return [...lastUse.entries()].sort((a, b) => a[1] - b[1])[0][0];
    }

    // ---- the one Jev request per decision ----------------------------------
    async decide(snap, menu) {
        const client = jev.getClient();
        const started = Date.now();
        const result = { action: null, confidence: null, danger: null, progress: null, completable: null, source: "fallback" };
        if (client && Object.keys(menu).length >= 2) {
            const proven = this.provenMenuIds(menu);
            const state = {
                ...snap,
                menu,
                proven_menu: proven,
            };
            const questions = {
                action: jev.choiceFn(
                    "A Minecraft agent is working on the subtask `subtask.description`; `subtask.verify` is the exact " +
                        "condition that will mark it done and `subtask.progress` (when present) is {have, need} for the item it " +
                        "asks for. The agent only sees what is in this state: `bot` (position, facing `yaw`, `pitch` in degrees " +
                        "with positive up, health and food out of 20, `daylight`), `cursor` (the block or entity the agent is " +
                        "looking at within 6 blocks), `view` (what lies along the look direction out to 30 blocks, as segments " +
                        "{block, from, to} in blocks from the eyes; `hit` marks the first solid block actually seen, `occluded` " +
                        "segments are behind it), `horizon` (for each compass direction, what lies 12, 24 and 40 blocks away: " +
                        "trees, water, or land:<block> with height change; `trees`/`water` count samples), `inventory` counts, " +
                        "equipment `slots`, `blocks` (nearest instance of each " +
                        "visible relevant block type as an offset dx,dy,dz from the agent, with a count within 32 blocks; a key " +
                        "like '*_log' means any kind of log), `entities` (mobs nearest first), `dropped` items on the ground, " +
                        "`placed_by_me`, `recent` (the last actions with their outcomes and state deltas; do not repeat an action " +
                        "that failed or changed nothing unless the situation changed), `since_progress`, and `extra` (facts " +
                        "added by learned actions). Each option is one bounded action the code will execute next; options " +
                        "starting with 'x:' are learned actions and the ones marked untested have never succeeded yet. " +
                        "A learned action states its purpose; pick it when that purpose serves the current subtask. " +
                        "Which option makes the most progress toward the subtask right now while keeping the agent safe " +
                        "(avoid walking into lava or water, handle hostiles, eat when starving)?",
                    menu
                ),
                danger: jev.noulFn(
                    "Is the agent in immediate physical danger that must be handled before working on the subtask: " +
                        "a hostile mob within a few blocks in `entities`, health at or below 6, standing in or next to lava " +
                        "(`bot.inLava`, or lava within 2 blocks in `blocks`), or drowning (`bot.inWater` with low `bot.oxygen`)?"
                ),
                progress: jev.noulFn(
                    "Judging from `recent` (each entry is an action, its outcome text, and the resulting state delta: blocks " +
                        "moved, inventory change, health change) and `since_progress` (actions and seconds since the last gain), " +
                        "is the agent making progress toward `subtask.description`? Walking toward a needed block, turning to " +
                        "put it under the cursor, or gaining needed items all count; repeated failures, timeouts, and actions " +
                        "that change nothing do not."
                ),
                completable: jev.noulFn(
                    "Consider only the actions whose ids are listed in `proven_menu` (their descriptions are in `menu`). " +
                        "Can `subtask.description` be completed, so that `subtask.verify` becomes true, using only those " +
                        "actions? For example, a subtask that needs an item to be mined, crafted, placed, or picked up cannot " +
                        "be completed with movement and looking alone. Answer no when a needed capability is missing from the menu."
                ),
            };
            try {
                const { answers } = await client.systemOne({ state, questions }, { timeout: jev.DECISION_TIMEOUT_MS });
                const ms = Date.now() - started;
                this.stats.decisions++;
                this.stats.jev_ms += ms;
                const a = answers.action;
                result.confidence = typeof a.confidence === "number" ? a.confidence : null;
                result.danger = answers.danger ? answers.danger.noul : null;
                result.progress = answers.progress ? answers.progress.noul : null;
                result.completable = answers.completable ? answers.completable.noul : null;
                if (a && typeof a.choice === "string" && a.choice in menu && result.confidence >= jev.MIN_CONFIDENCE) {
                    // Jev is deterministic: the same state gives the same top choice, so a
                    // pointless pick (turn to look at the log, again) can repeat forever.
                    // Sample from its distribution instead of taking the argmax; sharpen
                    // normally, flatten when the recent actions are a repeat with no change.
                    const temperature = this.repeatingWithoutChange() ? SAMPLE_TEMPERATURE_STUCK : SAMPLE_TEMPERATURE;
                    const sampled = sampleChoice(a.probabilities, menu, temperature);
                    result.action = sampled || a.choice;
                    result.source = sampled && sampled !== a.choice ? "jev_sampled" : "jev";
                } else {
                    result.action = this.fallbackAction(menu);
                    result.source = "low_confidence";
                }
                console.log(
                    `agentLoop: jev ${ms}ms -> ${result.action} (${(result.confidence || 0).toFixed(2)}) [nearest ${nearestTargetText(snap)}] danger ${(result.danger || 0).toFixed(2)} ` +
                        `progress ${(result.progress || 0).toFixed(2)} completable ${(result.completable || 0).toFixed(2)}`
                );
            } catch (err) {
                console.log("agentLoop: jev request failed:", err.message);
                result.action = this.fallbackAction(menu);
                result.source = "error";
            }
        } else {
            result.action = this.fallbackAction(menu);
        }
        this.lastDecision = {
            action: result.action,
            confidence: result.confidence,
            danger: result.danger,
            progress: result.progress,
            completable: result.completable,
            source: result.source,
        };
        return result;
    }

    // ---- execution -----------------------------------------------------------
    async execute(id, snap) {
        if (base.isBase(id)) {
            try {
                return await withTimeout(base.execute(this.bot, this, id), ACTION_TIMEOUT_MS, id);
            } catch (err) {
                return `failed: ${err.message}`;
            } finally {
                this.safeClearControls();
            }
        }
        if (id.startsWith("x:")) {
            const p = this.registry.get(id.slice(2));
            if (!p) return `failed: unknown action ${id}`;
            // one subtask object everywhere: the posted subtask plus live progress {have, need}
            const subtask = { ...(this.subtask || {}), ...(snap.subtask || {}) };
            const ctx = { snap, subtask, tree_items: this.treeItems };
            let outcome;
            try {
                // the guarded bot: execute's `bot` parameter would otherwise shadow the guarded one
                outcome = await withTimeout(p.execute(guardedBot(this.bot), this, ctx), ACTION_TIMEOUT_MS, id);
                if (typeof outcome !== "string") outcome = outcome ? "ok" : "failed: returned nothing";
            } catch (err) {
                outcome = `failed: ${err.message}`;
            } finally {
                this.safeClearControls();
                // stop() only latches a "stop at next node" flag; setGoal(null) afterwards
                // drains it. Without the drain the NEXT action's goto is cancelled at once
                // with "Path was stopped" (see botCleanup.js for the same ordering).
                try {
                    if (this.bot.pathfinder && typeof this.bot.pathfinder.stop === "function") this.bot.pathfinder.stop();
                } catch (e) {
                    /* ignore */
                }
                try {
                    if (this.bot.pathfinder && typeof this.bot.pathfinder.setGoal === "function") this.bot.pathfinder.setGoal(null);
                } catch (e) {
                    /* ignore */
                }
            }
            // the trial is scored after the world is re-read (see the main loop), so an
            // "ok" that changed nothing does not count as a success
            return outcome;
        }
        return `failed: unknown action ${id}`;
    }

    safeClearControls() {
        try {
            this.bot.clearControlStates();
        } catch (e) {
            /* fake bots */
        }
    }

    // ---- triggers --------------------------------------------------------------
    pushTrigger(type, detail) {
        const t = { type, at: Date.now(), ...(detail || {}) };
        this.pending.push(t);
        console.log(`agentLoop: trigger ${type} ${JSON.stringify(detail || {}).slice(0, 200)}`);
    }

    fireStuck(kind, extra) {
        if (this.stuckFiredAt[kind] >= 0 && this.actionsSpent - this.stuckFiredAt[kind] < STUCK_WINDOW) return;
        this.stuckFiredAt[kind] = this.actionsSpent;
        this.pushTrigger("stuck", { subtask_id: this.subtask ? this.subtask.id : null, kind, ...(extra || {}) });
    }

    checkHardTriggers(menu) {
        const r = this.recent;
        const isItem = this.subtask && (ITEM_KINDS.has(this.subtask.kind) || observe.progressOf(this.subtask.verify, {}) !== null);
        if (r.length >= STUCK_WINDOW) {
            const w = r.slice(-STUCK_WINDOW);
            if (w.every((x) => (x.delta.pos || 0) < 0.5)) this.fireStuck("position", { actions: STUCK_WINDOW });
            if (isItem && w.every((x) => !Object.keys(x.delta.inv || {}).length)) this.fireStuck("inventory", { actions: STUCK_WINDOW });
        }
        if (r.length >= REPEAT_WINDOW) {
            const w = r.slice(-REPEAT_WINDOW);
            if (w.every((x) => x.action === w[0].action)) this.fireStuck("repeat", { action: w[0].action, times: REPEAT_WINDOW });
        }
        if (r.length >= HEALTH_WINDOW) {
            const drop = -r.slice(-HEALTH_WINDOW).reduce((s, x) => s + (x.delta.health || 0), 0);
            const canFight = Object.keys(menu).some((id) => ATTACK_LIKE.test(id) || ATTACK_LIKE.test(menu[id]));
            if (drop >= HEALTH_DROP && !canFight) this.fireStuck("health", { drop, health: this.bot.health });
        }
    }

    // ---- the loop -----------------------------------------------------------------
    async run() {
        let lastDecisionAt = 0;
        while (this.running) {
            if (!this.subtask || this.idle) {
                await sleep(250);
                if (!this.subtask) break;
                continue;
            }
            const gap = DECISION_FLOOR_MS - (Date.now() - lastDecisionAt);
            if (gap > 0) await sleep(gap);
            lastDecisionAt = Date.now();

            let snap;
            try {
                snap = this.snapshot();
            } catch (err) {
                console.log("agentLoop: snapshot failed:", err.message);
                await sleep(1000);
                continue;
            }

            // done?
            const v = await this.verifyCurrent();
            if (!this.running || !this.subtask) break; // stop() may have run during the await
            if (v.ok) {
                const done = this.subtask;
                this.pushTrigger("subtask_done", { subtask_id: done.id, trace: this.recent.map((x) => x.action), detail: v.detail, actions: this.actionsSpent });
                this.subtask = null;
                break;
            }
            if (this.actionsSpent >= this.subtask.budget_actions) {
                this.pushTrigger("budget_exhausted", { subtask_id: this.subtask.id, actions_spent: this.actionsSpent });
                this.idle = true;
                continue;
            }

            await this.refreshPurposes();
            if (this.stats.decisions % 10 === 0) this.logState(snap);
            const menu = this.buildMenu(snap);
            // how long the menu has offered nothing but base moves and looks
            this.bareMenuStreak = Object.keys(menu).some((id) => id.startsWith("x:")) ? 0 : (this.bareMenuStreak || 0) + 1;
            const decision = await this.decide(snap, menu);
            if (!this.running || !this.subtask) break;
            for (const [id, left] of this.cooldowns) this.cooldowns.set(id, left - 1);

            if (decision.danger !== null && decision.danger >= DANGER_YES && Date.now() - this.lastDangerAt > DANGER_TRIGGER_GAP_MS) {
                this.lastDangerAt = Date.now();
                const threat = (snap.entities || []).find((e) => HOSTILE.test(e.name));
                this.pushTrigger("danger", { health: snap.bot.health, threat: threat ? `${threat.name} ${threat.distance} blocks ${threat.direction}` : snap.bot.inLava ? "lava" : snap.bot.inWater ? "water" : "unknown" });
            }

            // act
            this.placedCountBefore = Object.keys(this.placedByMe || {}).length;
            const before = {
                inv: snap.inventory,
                pos: this.bot.entity.position.clone(),
                health: this.bot.health,
                cursor: snap.cursor ? snap.cursor.block : null,
                yaw: this.bot.entity.yaw,
                pitch: this.bot.entity.pitch,
            };
            const t0 = Date.now();
            const outcome = await this.execute(decision.action, snap);
            const after = this.inventoryCounts();
            const record = {
                action: decision.action,
                outcome: String(outcome).slice(0, 200),
                ms: Date.now() - t0,
                delta: {
                    pos: Math.round(this.bot.entity.position.distanceTo(before.pos) * 10) / 10,
                    inv: diffCounts(before.inv, after),
                    health: (this.bot.health || 0) - (before.health || 0),
                },
            };
            // A look is a change only if it REVEALS something: the cursor lands on a block
            // type not seen at the cursor in the last few actions. Turning toward a target
            // that is already in view (the bot already knows where the log is) is not.
            try {
                const cur = this.bot.blockAtCursor(6);
                const curName = cur ? cur.name : null;
                const recentlySeen = new Set(
                    this.recent.slice(-LOOK_MEMORY).map((r) => r.seen).filter(Boolean).concat(before.cursor ? [before.cursor] : [])
                );
                record.seen = curName;
                if (curName && !recentlySeen.has(curName)) record.delta.cursor = curName;
            } catch (err) {
                /* no cursor info */
            }
            if (record.action.startsWith("x:")) {
                const claimedOk = /^ok/i.test(record.outcome);
                const prim = this.registry.get(record.action.slice(2));
                const wanted = purposeItems(prim && prim.purposeSpec);
                let success;
                if (wanted.length) {
                    // the action declared which items it brings about: it succeeded only if
                    // one of THOSE went up, not merely if something changed
                    const gained = Object.entries(record.delta.inv || {}).filter(([, n]) => n > 0).map(([k]) => k);
                    const progressed = gained.some((g) => wanted.some((w) => observe.matcherFor(w)(g)));
                    success = claimedOk && progressed;
                    if (claimedOk && !progressed) {
                        const got = gained.length ? `; it changed ${gained.join(", ")} instead` : "";
                        record.outcome = `${record.outcome} [NO PROGRESS toward its declared purpose: ${wanted.join("/")} unchanged${got}]`.slice(0, 240);
                    }
                } else {
                    const effect = hadEffect(record, this.placedCountBefore, Object.keys(this.placedByMe || {}).length);
                    success = claimedOk && effect;
                    if (claimedOk && !effect) {
                        record.outcome = `${record.outcome} [NO EFFECT: inventory, position and placed blocks unchanged]`.slice(0, 220);
                    }
                }
                this.registry.recordOutcome(record.action.slice(2), success, record.outcome);
            }
            this.recent.push(record);
            if (this.recent.length > 40) this.recent.shift();
            if (!/^(move|look):|^wait$/.test(record.action) || /^failed/i.test(record.outcome)) {
                console.log(`agentLoop: outcome ${record.action} -> ${record.outcome} inv ${JSON.stringify(record.delta.inv)}`);
            }
            this.actionsSpent++;
            this.actionsSinceCheckpoint++;
            if (/^failed/i.test(record.outcome)) this.cooldowns.set(decision.action, ACTION_COOLDOWN_DECISIONS);
            // An action repeated with nothing changing (same look, same spot) is a
            // no-op here: take it off the menu for a few decisions so Jev must pick
            // something else, the same way a failed action sits out.
            if (this.repeatingWithoutChange()) {
                this.cooldowns.set(decision.action, ACTION_COOLDOWN_DECISIONS);
                console.log(`agentLoop: ${decision.action} repeated ${NO_CHANGE_REPEATS}x with no change; cooling it down`);
            } else if (/^look:/.test(decision.action) && !changedWorld(record)) {
                // a look that revealed nothing, used before in the last few actions (even
                // with moves in between): it is not helping, so it sits out a few decisions
                const earlier = this.recent.slice(-LOOK_MEMORY - 1, -1).some((r) => r.action === decision.action);
                if (earlier) {
                    this.cooldowns.set(decision.action, ACTION_COOLDOWN_DECISIONS);
                    console.log(`agentLoop: ${decision.action} revealed nothing new again; cooling it down`);
                }
            }

            // progress bookkeeping
            if (this.madeProgress(snap, record)) {
                this.lastProgressAt = Date.now();
                this.actionsSinceProgress = 0;
                this.lastProgressPos = this.bot.entity.position.clone();
            } else {
                this.actionsSinceProgress++;
            }
            if (decision.progress !== null) {
                this.progressLowStreak = decision.progress < PROGRESS_LOW ? this.progressLowStreak + 1 : 0;
                if (this.progressLowStreak >= PROGRESS_LOW_STREAK) {
                    this.fireStuck("progress", { progress: decision.progress, streak: this.progressLowStreak });
                    this.progressLowStreak = 0;
                }
            }
            this.checkHardTriggers(menu);

            // completable checkpoint (section 5.2)
            if (decision.completable !== null) this.lastCompletable = decision.completable;
            const every = (this.bareMenuStreak || 0) >= COMPLETABLE_EVERY_BARE ? COMPLETABLE_EVERY_BARE : COMPLETABLE_EVERY;
            if (this.actionsSinceCheckpoint >= every) {
                this.actionsSinceCheckpoint = 0;
                if (this.lastCompletable !== null && this.lastCompletable < COMPLETABLE_NO) {
                    const stale = this.registry.staleTrials();
                    if (this.registry.hasUntestedTrial()) {
                        console.log("agentLoop: completable=no but a trial action is untested; build cooldown");
                    } else {
                        if (stale.length) console.log(`agentLoop: trial(s) never offered, asking for a replacement: ${stale.join(", ")}`);
                        this.pushTrigger("builder_wanted", {
                            subtask_id: this.subtask.id,
                            completable: this.lastCompletable,
                            menu: Object.keys(menu),
                            stale,
                        });
                    }
                }
            }
        }
        this.running = false;
        this.safeClearControls();
    }

    madeProgress(snap, record) {
        if (!this.subtask) return false;
        const prog = snap.subtask && snap.subtask.progress;
        if (prog) {
            const now = observe.progressOf(this.subtask.verify, this.inventoryCounts());
            return Boolean(now && now.have > prog.have);
        }
        if (ITEM_KINDS.has(this.subtask.kind)) return Object.keys(record.delta.inv || {}).some((k) => record.delta.inv[k] > 0);
        if (!this.lastProgressPos) return record.delta.pos >= 1;
        return this.bot.entity.position.distanceTo(this.lastProgressPos) >= 3;
    }

    // ---- status -----------------------------------------------------------------
    status({ drain = true } = {}) {
        let snap = null;
        try {
            snap = this.bot.entity ? this.snapshot() : null;
        } catch (err) {
            snap = null;
        }
        const triggers = drain ? this.pending.splice(0) : [...this.pending];
        return {
            snap,
            triggers,
            recent: this.recent.slice(-8),
            primitives: this.registry.summary(),
            menu: this.lastMenu,
            last_decision: this.lastDecision,
            subtask: this.subtask ? { id: this.subtask.id, actions_spent: this.actionsSpent, budget_actions: this.subtask.budget_actions, idle: this.idle } : null,
            stats: { ...this.stats, running: this.running },
        };
    }
}

function inject(bot) {
    bot.agentLoop = new AgentLoop(bot);
    return bot.agentLoop;
}

module.exports = { AgentLoop, inject, withTimeout, diffCounts, purposeItems, COMPLETABLE_EVERY, STUCK_WINDOW, REPEAT_WINDOW };
