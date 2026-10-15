// The Jev action loop (PLAN.md sections 5.2, 7, 8). Python posts one subtask
// (description + VerifySpec + allowed blocks + budget); this loop then
// repeats: observe -> menu -> one Jev request (action Choice plus danger,
// progress and completable Nouls) -> execute one bounded action -> record the
// outcome and state delta -> check the subtask's spec. It raises triggers for
// Python (subtask_done, builder_wanted, stuck, budget_exhausted, danger) that
// are drained by GET /agent/status. Jev never sees code: the menu lines are
// the whole interface to an action.
const fs = require("fs");
const { Vec3 } = require("vec3");
const jev = require("./jevTraversal");
const { labelOf } = require("./outcomes");
const jevState = require("./jevState");
const observe = require("./observe");
const base = require("./baseActions");
const verify = require("./verify");
const { PrimitiveRegistry, guardedBot } = require("./primitiveRegistry");

const DECISION_FLOOR_MS = 500; // minimum gap between decisions
const ACTION_TIMEOUT_MS = 20 * 1000;
const COMPLETABLE_EVERY = 10; // actions between "completable with this menu?" checkpoints
const COMPLETABLE_EVERY_BARE = 5; // ... when no learned action has been on the menu for that long
const COMPLETABLE_NO = 0.5; // mean `completable` Noul below this at a checkpoint = the menu cannot do it
// The action Choice carries this extra option (TypeSafe docs: "Include an 'other' or
// 'none of the above' option when the list might not cover every input"). It answers
// a step-level question (does any option help RIGHT NOW) and is used only to keep the
// sampled action honest; it is never executed. The plan-level question (can this menu
// EVER finish the subtask) stays a separate `completable` Noul, now asked over the
// whole menu including untested trials: the old one was asked over proven actions only
// and sat near 0.1 whenever a trial existed, firing the builder every checkpoint (F4).
const NONE_OPTION = "none_of_these";
const NONE_TEXT =
    "No option on this menu can move the subtask forward from here: what it needs next (for example mining, " +
    "crafting, placing or picking something up) is not among the options.";
// When Jev's top answer is none_of_these the loop HOLDS instead of running a leftover
// option: it stands still for HOLD_MS, does not spend the action budget, records no
// action, and asks Python for a build at once (then every HOLD_REQUEST_EVERY holds).
// Before this, the leftover was sampled from the remaining probability, mostly looks
// and turns that changed nothing, and it burned the budget the build needed (run
// 2026-10-13 20:27: none_of_these won 100 of 120 decisions on the cobblestone step).
// Holding is bounded per posting of a subtask (HOLD_BUDGET_MS) so a build that never
// comes cannot freeze the agent; danger always overrides a hold.
const NONE_HOLD_MIN = 0.5; // P(none_of_these) needed, besides being the top choice
const HOLD_MS = 1500;
const HOLD_REQUEST_EVERY = 6;
const HOLD_BUDGET_MS = 120 * 1000;
const EFFECT_QUESTIONS_MAX = 14; // per-option "would this change anything" Nouls (non-move options)
const EFFECT_FLOOR = 0.15; // options judged this unlikely to change anything are not sampled
// Progress as situations, not degrees (TypeSafe docs on Score). Level 0 three times in a
// row is the soft "stuck" signal.
const PROGRESS_LEVELS = [
    "The last several actions changed nothing that matters: the same actions repeat, fail, or leave position, inventory and what the agent sees unchanged.",
    "Things change (the agent moves or turns, new blocks come into view) but it is not getting closer to what the subtask needs.",
    "The agent is getting closer to what the subtask needs: approaching or facing a needed block or item, or making a needed tool, station or item available.",
    "The subtask's own target is advancing: a needed item was gained or the completion condition got closer in the last few actions.",
];
const DANGER_YES = 0.7;
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

// Sample one menu id from Jev's probabilities at the given temperature. The
// none_of_these option, options no longer on the menu, and options Jev judged very
// unlikely to change anything (effect below EFFECT_FLOOR) are left out; the rest are
// renormalised. Null when nothing usable remains.
function sampleChoice(probabilities, menu, temperature, effects = null, exempt = null) {
    let entries = Object.entries(probabilities || {}).filter(([id, p]) => id in menu && id !== NONE_OPTION && p > 0);
    if (effects) {
        const useful = entries.filter(([id]) => !(id in effects) || effects[id] >= EFFECT_FLOOR || (exempt && exempt.has(id)));
        if (useful.length) entries = useful;
    }
    if (!entries.length) return null;
    const t = Math.max(0.05, temperature || 1);
    const weights = entries.map(([, p]) => Math.pow(p, 1 / t));
    const total = weights.reduce((acc, w) => acc + w, 0);
    let r = Math.random() * total;
    for (let i = 0; i < entries.length; i++) {
        r -= weights[i];
        if (r <= 0) return entries[i][0];
    }
    return entries[entries.length - 1][0];
}

// The rubric level with the highest probability (the docs: use the level, not the mean).
function scoreLevel(answer) {
    if (!answer || !answer.probabilities) return null;
    let best = null;
    let bestP = -1;
    for (const [k, p] of Object.entries(answer.probabilities)) {
        if (p > bestP) {
            bestP = p;
            best = Number(k);
        }
    }
    return best;
}
const DANGER_TRIGGER_GAP_MS = 10 * 1000;
const HOSTILE = /^(zombie|skeleton|creeper|spider|cave_spider|enderman|witch|drowned|husk|stray|pillager|phantom|slime|zombie_villager|piglin|blaze|ghast|magma_cube|hoglin|vindicator|evoker|ravager|warden)$/;
const ATTACK_LIKE = /attack|fight|kill|hit|strike|defend/;
const ITEM_KINDS = new Set(["craft", "mine", "smelt", "kill", "collect"]);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The parts of a snapshot worth keeping in the decision log: enough to see what the
// agent saw without storing the full ray and horizon samples every action.
function stateDigest(snap) {
    const s = snap || {};
    const b = s.bot || {};
    const blocks = {};
    for (const [k, v] of Object.entries(s.blocks || {})) blocks[k] = Math.round(Math.hypot(v.dx, v.dy, v.dz));
    return {
        pos: b.position,
        yaw: b.yaw,
        pitch: b.pitch,
        health: b.health,
        food: b.food,
        in_water: b.inWater || undefined,
        cursor: s.cursor ? s.cursor.block : null,
        inventory: s.inventory || {},
        hand: s.slots ? s.slots.hand : null,
        blocks,
        entities: (s.entities || []).slice(0, 3).map((e) => `${e.name}@${e.distance}${e.direction ? " " + e.direction : ""}`),
        progress: s.subtask ? s.subtask.progress : null,
    };
}

// The highest-probability options of a Choice answer, rounded, for the log.
function topProbabilities(probabilities, n = 6) {
    return Object.fromEntries(
        Object.entries(probabilities || {})
            .sort((a, b) => b[1] - a[1])
            .slice(0, n)
            .map(([k, v]) => [k, Math.round(v * 1000) / 1000])
    );
}

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
        // decision log (one JSON line per action); the path comes with each subtask
        this.decisionLog = null;
        this.goalId = null;
        this.holdMsUsed = 0; // per posting of a subtask
        this.holdStreak = 0;
        this.holding = false;
        this.runTag = Date.now().toString(36);
        this.decisionSeq = 0;
    }

    // Append one record to the decision log. Never throws and never blocks the loop.
    logDecision(entry) {
        if (!this.decisionLog) return;
        try {
            fs.appendFile(this.decisionLog, JSON.stringify(entry) + "\n", (err) => {
                if (err && !this._logErrorShown) {
                    this._logErrorShown = true;
                    console.log("agentLoop: decision log write failed:", err.message);
                }
            });
        } catch (err) {
            /* logging must never break the loop */
        }
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
        if (b.decision_log) this.decisionLog = String(b.decision_log);
        if (b.goal_id) this.goalId = String(b.goal_id);
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
        this.holdMsUsed = 0; // each posting (retries included) may hold again: a new build may be on its way
        this.holdStreak = 0;
        this.holding = false;
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
    // One System One request per decision, all questions over the same digested state:
    //   action    Choice over the (shuffled) menu plus none_of_these
    //   danger    Noul: immediate physical danger
    //   progress  Score: which situation describes the last few actions
    //   effect:N  Noul per non-move option: would it change anything observable now
    // Questions over one state cost little extra time (TypeSafe docs); code decides
    // which answers to use.
    buildQuestions(menu) {
        // (completable, danger, action, progress, effect_N; see the comment above)
        const sc = jev.scoreFn;
        const options = {};
        for (const [id, text] of Object.entries(menu)) options[id] = text;
        const questions = {
            action: jev.choiceFn(
                "A Minecraft agent is working on the subtask in `subtask` (`subtask.done_when` is the exact condition " +
                    "that marks it done; `subtask.progress` counts the item it asks for). The state says where the agent " +
                    "is and what it sees: `agent`, `looking_at` (the block under the crosshair; digging and using need it " +
                    "within reach, about 4.5 blocks), `nearby_blocks` (nearest of each relevant block: distance, compass " +
                    "direction, height), `hazards`, `horizon` (what lies 12 to 40 blocks away in each direction), " +
                    "`entities`, `dropped_items`, `inventory`, `equipment`, `placed_by_me`, `recent` (the last actions, " +
                    "each with its outcome, a label and what changed) and `extra` (facts added by learned actions). " +
                    "Each option is one bounded action the code will run next; options starting with 'x:' are learned " +
                    "actions and say what they are for. Which option makes the most progress on the subtask right now " +
                    "while keeping the agent safe? Do not pick an action whose recent attempts failed or changed nothing " +
                    "unless the situation has changed since. If no option can move the subtask forward, choose " +
                    `${NONE_OPTION}.`,
                jevState.shuffled({ ...menu, [NONE_OPTION]: NONE_TEXT })
            ),
            completable: jev.noulFn(
                "`menu` lists every action the agent can take, each with what it does (learned actions marked " +
                    "untested are included). Taking these actions one after another, as many times as needed, can the " +
                    "subtask in `subtask` be completed so that `subtask.done_when` becomes true? Walking and looking " +
                    "alone cannot mine, craft, place or pick anything up. Answer no when a needed capability is missing " +
                    "from `menu`, even if some option helps right now."
            ),
            danger: jev.noulFn(
                "Is the agent in immediate physical danger that must be handled before the subtask: a hostile mob " +
                    "within a few blocks in `entities`, health at 6/20 or lower in `agent.health`, standing in or right " +
                    "next to lava (`agent.in_lava`, or lava within 2 blocks in `hazards`), or drowning (`agent.in_water` " +
                    "with low `agent.oxygen`)?"
            ),
        };
        if (sc) {
            questions.progress = sc(
                "Judging only from `recent` and `since_progress`, which situation describes the agent's last few " +
                    "actions with respect to `subtask.description`?",
                PROGRESS_LEVELS
            );
        }
        const effectIds = Object.keys(menu).filter((id) => !id.startsWith("move:")).slice(0, EFFECT_QUESTIONS_MAX);
        effectIds.forEach((id, i) => {
            questions[`effect_${i}`] = jev.noulFn(
                `Consider one option: "${String(menu[id]).slice(0, 220)}". If the agent took this action right now, ` +
                    "would something observable change: the agent moves, the inventory changes, a block is broken or " +
                    "placed, or a block not seen in `recent` comes under the crosshair? Answer no if `recent` shows " +
                    "the same action just failed or changed nothing in the same situation."
            );
        });
        return { questions, effectIds };
    }

    async decide(snap, menu) {
        const client = jev.getClient();
        const started = Date.now();
        const result = {
            action: null, confidence: null, danger: null, progress: null, progressLevel: null, completable: null,
            none: null, effects: null, source: "fallback", probabilities: null, jev_ms: null, top: null,
        };
        if (client && Object.keys(menu).length >= 2) {
            const state = { ...jevState.digest(snap), menu };
            const { questions, effectIds } = this.buildQuestions(menu);
            try {
                const { answers } = await client.systemOne({ state, questions }, { timeout: jev.DECISION_TIMEOUT_MS });
                const ms = Date.now() - started;
                this.stats.decisions++;
                this.stats.jev_ms += ms;
                const a = answers.action;
                result.jev_ms = ms;
                result.top = a ? a.choice : null;
                result.probabilities = a ? a.probabilities : null;
                result.confidence = typeof a.confidence === "number" ? a.confidence : null;
                result.danger = answers.danger ? answers.danger.noul : null;
                if (answers.progress) {
                    result.progressLevel = scoreLevel(answers.progress);
                    result.progress = typeof answers.progress.score === "number" ? answers.progress.score / (PROGRESS_LEVELS.length - 1) : null;
                }
                result.none = a && a.probabilities && typeof a.probabilities[NONE_OPTION] === "number" ? a.probabilities[NONE_OPTION] : null;
                result.completable = answers.completable ? answers.completable.noul : null;
                const effects = {};
                effectIds.forEach((id, i) => {
                    const e = answers[`effect_${i}`];
                    if (e && typeof e.noul === "number") effects[id] = Math.round(e.noul * 1000) / 1000;
                });
                result.effects = effects;
                if (a && typeof a.choice === "string" && result.confidence >= jev.MIN_CONFIDENCE) {
                    // Jev is deterministic: the same state gives the same top choice, so a
                    // pointless pick (turn to look at the log, again) can repeat forever.
                    // Sample from its distribution instead of taking the argmax; sharpen
                    // normally, flatten when the recent actions are a repeat with no change.
                    const temperature = this.repeatingWithoutChange() ? SAMPLE_TEMPERATURE_STUCK : SAMPLE_TEMPERATURE;
                    // a freshly repaired action keeps its predecessor's failures in `recent`;
                    // the effect filter must not hide it before it has been tried once
                    const fresh = new Set(
                        [...this.registry.items.values()].filter((p) => p.repaired && p.trials === 0).map((p) => `x:${p.name}`)
                    );
                    const sampled = sampleChoice(a.probabilities, menu, temperature, effects, fresh);
                    result.action = sampled || (a.choice in menu ? a.choice : this.fallbackAction(menu));
                    result.source = a.choice === NONE_OPTION ? "jev_none" : sampled && sampled !== a.choice ? "jev_sampled" : "jev";
                } else {
                    result.action = this.fallbackAction(menu);
                    result.source = "low_confidence";
                }
                console.log(
                    `agentLoop: jev ${ms}ms -> ${result.action} (${(result.confidence || 0).toFixed(2)}) [nearest ${nearestTargetText(snap)}] danger ${(result.danger || 0).toFixed(2)} ` +
                        `progress L${result.progressLevel} none ${(result.none || 0).toFixed(2)} completable ${(result.completable || 0).toFixed(2)}`
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
            progress_level: result.progressLevel,
            completable: result.completable,
            none: result.none,
            source: result.source,
            jev_ms: result.jev_ms,
        };
        return result;
    }

    // ---- holding: nothing on the menu helps ------------------------------------
    shouldHold(decision) {
        if (decision.top !== NONE_OPTION || decision.none === null || decision.none < NONE_HOLD_MIN) return false;
        if (decision.danger !== null && decision.danger >= DANGER_YES) return false; // act, do not stand still
        return this.holdMsUsed + HOLD_MS <= HOLD_BUDGET_MS;
    }

    // Stand still for HOLD_MS without spending budget or recording an action, and ask
    // for a build at the start of a hold streak and every HOLD_REQUEST_EVERY holds.
    async hold(decision, snap, menu) {
        this.holding = true;
        if (this.holdStreak % HOLD_REQUEST_EVERY === 0) {
            this.pushTrigger("builder_wanted", {
                subtask_id: this.subtask.id,
                completable: decision.completable,
                none: decision.none,
                reason: "none_of_these",
                menu: Object.keys(menu),
                stale: this.registry.staleTrials(),
            });
        }
        this.holdStreak++;
        this.logDecision({
            id: `${this.runTag}-h${this.decisionSeq}-${this.holdStreak}`,
            ts: Date.now(),
            goal_id: this.goalId,
            subtask_id: this.subtask.id,
            subtask: this.subtask.description,
            state: stateDigest(snap),
            menu: Object.keys(menu),
            choice: { source: "hold", confidence: decision.confidence, top: topProbabilities(decision.probabilities) },
            completable: decision.completable,
            none: decision.none,
            progress_level: decision.progressLevel,
            danger: decision.danger,
            action: "hold",
            outcome: `held ${HOLD_MS} ms: nothing on the menu helps (none_of_these ${decision.none.toFixed(2)})`,
            label: "HOLD",
            jev_ms: decision.jev_ms,
            act_ms: HOLD_MS,
        });
        if (this.lastDecision) this.lastDecision.source = "hold";
        this.safeClearControls();
        await sleep(HOLD_MS);
        this.holdMsUsed += HOLD_MS;
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
                this.pushTrigger("subtask_done", {
                    subtask_id: done.id,
                    trace: this.recent.map((x) => x.action),
                    // the raw evidence of how it was done: kept by Python as an experience trace
                    records: this.recent.map((x) => ({ id: x.id, action: x.action, outcome: x.outcome, label: x.label, delta: x.delta })),
                    detail: v.detail,
                    actions: this.actionsSpent,
                });
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

            if (this.shouldHold(decision)) {
                await this.hold(decision, snap, menu);
                continue;
            }
            if (this.holding) {
                console.log(`agentLoop: acting again after holding ${this.holdStreak} decision(s)`);
                this.holding = false;
                this.holdStreak = 0;
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
                id: `${this.runTag}-${++this.decisionSeq}`,
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
                record.label = labelOf(record.outcome, success || changedWorld(record));
                this.registry.recordOutcome(record.action.slice(2), success, record.outcome, { id: record.id, label: record.label });
            }
            if (!record.label) record.label = labelOf(record.outcome, changedWorld(record));
            this.logDecision({
                id: record.id,
                ts: Date.now(),
                goal_id: this.goalId,
                subtask_id: this.subtask ? this.subtask.id : null,
                subtask: this.subtask ? this.subtask.description : null,
                state: stateDigest(snap),
                menu: Object.keys(menu),
                choice: {
                    source: decision.source,
                    confidence: decision.confidence,
                    top: topProbabilities(decision.probabilities),
                },
                completable: decision.completable,
                none: decision.none,
                progress_level: decision.progressLevel,
                danger: decision.danger,
                effects: decision.effects,
                action: record.action,
                outcome: record.outcome,
                label: record.label,
                delta: record.delta,
                seen_after: record.seen || null,
                jev_ms: decision.jev_ms,
                act_ms: record.ms,
            });
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
            if (decision.progressLevel !== null && this.recent.length >= 3) {
                // with fewer than three records there is nothing to judge progress from
                this.progressLowStreak = decision.progressLevel === 0 ? this.progressLowStreak + 1 : 0;
                if (this.progressLowStreak >= PROGRESS_LOW_STREAK) {
                    this.fireStuck("progress", { progress: decision.progress, streak: this.progressLowStreak });
                    this.progressLowStreak = 0;
                }
            }
            this.checkHardTriggers(menu);

            // completable checkpoint (section 5.2): the mean of the `completable` Noul over
            // the decisions since the last checkpoint
            if (decision.completable !== null) {
                this.lastCompletable = decision.completable;
                (this.completableWindow = this.completableWindow || []).push(decision.completable);
            }
            const every = (this.bareMenuStreak || 0) >= COMPLETABLE_EVERY_BARE ? COMPLETABLE_EVERY_BARE : COMPLETABLE_EVERY;
            if (this.actionsSinceCheckpoint >= every) {
                this.actionsSinceCheckpoint = 0;
                const win = this.completableWindow || [];
                this.completableWindow = [];
                const meanCompletable = win.length ? win.reduce((acc, x) => acc + x, 0) / win.length : null;
                if (meanCompletable !== null && meanCompletable < COMPLETABLE_NO) {
                    const stale = this.registry.staleTrials();
                    if (this.registry.hasUntestedTrial()) {
                        console.log("agentLoop: completable=no but a trial action is untested; build cooldown");
                    } else {
                        if (stale.length) console.log(`agentLoop: trial(s) never offered, asking for a replacement: ${stale.join(", ")}`);
                        this.pushTrigger("builder_wanted", {
                            subtask_id: this.subtask.id,
                            completable: Math.round(meanCompletable * 1000) / 1000,
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
            subtask: this.subtask
                ? { id: this.subtask.id, actions_spent: this.actionsSpent, budget_actions: this.subtask.budget_actions, idle: this.idle,
                    holding: this.holding, hold_ms_used: this.holdMsUsed }
                : null,
            stats: { ...this.stats, running: this.running },
        };
    }
}

function inject(bot) {
    bot.agentLoop = new AgentLoop(bot);
    return bot.agentLoop;
}

module.exports = { AgentLoop, inject, withTimeout, diffCounts, purposeItems, COMPLETABLE_EVERY, STUCK_WINDOW, REPEAT_WINDOW };
