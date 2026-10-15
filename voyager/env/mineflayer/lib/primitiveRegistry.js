// Runtime-registered actions for the agent loop (PLAN.md section 1.5).
//
// A primitive is a JavaScript module source that defines:
//
//     function menu(snap, loop)            // SYNCHRONOUS: a one-line description
//                                          // with live facts when the action applies
//                                          // right now, or null to stay off the menu
//     async function execute(bot, loop, ctx) // does one bounded thing; returns a string
//                                            // starting with "ok" on success, otherwise
//                                            // "failed: <why>"
//     function observe(bot, loop)          // OPTIONAL, synchronous: a small plain object
//                                          // merged into snap.extra.<name> so the action
//                                          // can add facts Jev needs that the snapshot lacks
//
// The source is compiled once with a restricted scope (bot, loop, mcData, Vec3,
// goals, Movements, a require limited to a few packages). Registered actions
// are offered on the menu as `x:<name>`, executed under a timeout, and retired
// after repeated failure with no success. A primitive with no success yet is a
// `trial`: it is on the menu, but the loop does not count it as a capability
// for the "completable with this menu" question, and no new build starts
// while a trial is untested. The builder (Python) generates and posts them.
const { Vec3 } = require("vec3");

const MAX_SOURCE_CHARS = 12000;
const RETIRE_AFTER_FAILURES = 3;
const STALE_AFTER_MENU_BUILDS = 15;
const ALLOWED_REQUIRES = new Set(["vec3", "mineflayer-pathfinder", "minecraft-data"]);

const FORBIDDEN = [
    [/\bprocess\s*\./, "process access"],
    [/child_process/, "child_process"],
    [/\brequire\s*\(\s*["'`]fs["'`]/, "fs access"],
    [/\beval\s*\(/, "eval"],
    [/\bFunction\s*\(/, "Function constructor"],
    [/\bbot\s*\.\s*chat\s*\(/, "bot.chat (chat lines are parsed by the brain)"],
    [/\bbot\s*\.\s*end\s*\(/, "bot.end"],
    [/\bbot\s*\.\s*quit\s*\(/, "bot.quit"],
    [/\bsetInterval\s*\(/, "setInterval"],
    [/\bimport\s*\(/, "dynamic import"],
];

// Node cannot preempt a synchronous loop: every loop body must contain an await.
function loopsWithoutAwait(source) {
    const problems = [];
    const re = /\b(while|for)\s*\([^)]*\)\s*\{/g;
    let m;
    while ((m = re.exec(source))) {
        let depth = 1;
        let i = m.index + m[0].length;
        const start = i;
        while (i < source.length && depth > 0) {
            if (source[i] === "{") depth++;
            else if (source[i] === "}") depth--;
            i++;
        }
        const body = source.slice(start, i - 1);
        if (!/\bawait\b/.test(body)) problems.push(`${m[1]} loop without await at offset ${m.index}`);
    }
    return problems;
}

function staticCheck(name, source) {
    if (!/^[a-z][a-z0-9_]{1,31}$/.test(String(name || ""))) return "name must be lowercase [a-z0-9_], 2-32 chars";
    if (typeof source !== "string" || !source.trim()) return "source is empty";
    if (source.length > MAX_SOURCE_CHARS) return `source longer than ${MAX_SOURCE_CHARS} chars`;
    for (const [re, why] of FORBIDDEN) if (re.test(source)) return `forbidden: ${why}`;
    if (!/\bfunction\s+menu\s*\(/.test(source)) return "missing `function menu(snap, loop)`";
    if (!/\basync\s+function\s+execute\s*\(/.test(source)) return "missing `async function execute(bot, loop, ctx)`";
    if (/\basync\s+function\s+observe\s*\(/.test(source)) return "`observe` must be synchronous (not async)";
    const loops = loopsWithoutAwait(source);
    if (loops.length) return loops[0];
    return null;
}

function restrictedRequire(name) {
    if (!ALLOWED_REQUIRES.has(name)) throw new Error(`require("${name}") is not allowed in a primitive`);
    return require(name);
}

// Generated code gets the bot through this proxy. The common mineflayer calls
// check their arguments and throw an error that says what was expected, because
// mineflayer's own errors ("Cannot read properties of undefined (reading
// 'plus')") give the builder nothing to learn from. Everything else passes through.
const isBlock = (b) => b && typeof b === "object" && typeof b.name === "string" && b.position && typeof b.position.offset === "function";
const isVec3 = (v) => v && typeof v === "object" && typeof v.x === "number" && typeof v.offset === "function";
const kindOf = (v) => (v === null ? "null" : v === undefined ? "undefined" : isBlock(v) ? `Block(${v.name})` : isVec3(v) ? "Vec3" : Array.isArray(v) ? "array" : typeof v === "object" ? `object{${Object.keys(v).slice(0, 4).join(",")}}` : typeof v);
const CHECKS = {
    placeBlock(args) {
        const [ref, face] = args;
        if (!isBlock(ref)) return `bot.placeBlock(referenceBlock, faceVector): referenceBlock must be a Block from bot.blockAt(vec3) (the solid block you place AGAINST), got ${kindOf(ref)}`;
        if (!isVec3(face)) return `bot.placeBlock(referenceBlock, faceVector): faceVector must be a Vec3 such as new Vec3(0, 1, 0) (place on top), got ${kindOf(face)}`;
        if (ref.boundingBox !== "block") return `bot.placeBlock: the reference block is ${ref.name}, which is not solid; place against a solid block (e.g. the ground under a free spot)`;
        return null;
    },
    dig(args) {
        return isBlock(args[0]) ? null : `bot.dig(block): block must be a Block from bot.blockAt(vec3) or loop.nearestBlocks(...), got ${kindOf(args[0])}`;
    },
    equip(args) {
        const [item, dest] = args;
        const okItem = item && typeof item === "object" && typeof item.name === "string";
        if (!okItem) {
            const hint = typeof item === "string" ? ` "${item}"` : "";
            return `bot.equip(item, destination): item must be an Item OBJECT from the inventory, e.g. const it = bot.inventory.items().find(i => i.name === ${hint ? `"${item}"` : '"crafting_table"'}); if (!it) return "failed: no ${typeof item === "string" ? item : "item"} in inventory"; await bot.equip(it, "hand"). Got ${kindOf(item)}${hint} (a name string or id is not accepted)`;
        }
        if (dest !== undefined && !["hand", "off-hand", "head", "torso", "legs", "feet"].includes(dest)) return `bot.equip: destination must be "hand", "off-hand", "head", "torso", "legs" or "feet", got ${JSON.stringify(dest)}`;
        return null;
    },
    craft(args) {
        const [recipe, count, table] = args;
        if (!recipe || typeof recipe !== "object" || !("result" in recipe || "delta" in recipe)) return `bot.craft(recipe, count, table): recipe must come from loop.recipesFor(...)/bot.recipesFor(...), got ${kindOf(recipe)}`;
        if (count !== undefined && count !== null && !(Number.isInteger(count) && count > 0)) return `bot.craft: count is how many times to apply the recipe (a positive integer), got ${JSON.stringify(count)}`;
        if (table !== undefined && table !== null && !isBlock(table)) return `bot.craft: table must be a crafting_table Block (from loop.nearestBlocks) or null, got ${kindOf(table)}`;
        return null;
    },
    lookAt(args) {
        return isVec3(args[0]) ? null : `bot.lookAt(point): point must be a Vec3 (e.g. block.position.offset(0.5, 0.5, 0.5)), got ${kindOf(args[0])}`;
    },
};

function guardedBot(bot) {
    return new Proxy(bot, {
        get(target, prop, receiver) {
            const value = Reflect.get(target, prop, receiver);
            const check = CHECKS[prop];
            if (!check || typeof value !== "function") return value;
            return function (...args) {
                const problem = check(args);
                if (problem) return Promise.reject(new Error(problem));
                return value.apply(target, args);
            };
        },
    });
}

class PrimitiveRegistry {
    constructor(loop) {
        this.loop = loop;
        this.items = new Map(); // name -> {name, source, menu, execute, observe, trials, successes, failures, retired, registeredAt}
    }

    // Compile and register; returns {ok, error}. A snapshot (optional) lets the
    // menu function run once so an immediate throw is reported, not hidden.
    // `opts.repair`: this source is a repaired version of an action already registered
    // under `name`: its trial counts start fresh and it comes back on the menu.
    // `opts.version`: the version number the builder assigned (kept for the summary).
    register(name, source, snap, purpose = null, opts = {}) {
        const err = staticCheck(name, source);
        if (err) return { ok: false, error: err };
        let mod;
        try {
            const { goals, Movements } = require("mineflayer-pathfinder");
            const factory = new Function(
                "bot", "loop", "mcData", "Vec3", "goals", "Movements", "require",
                source + "\nreturn { menu, execute, observe: typeof observe === 'function' ? observe : null, PURPOSE: typeof PURPOSE !== 'undefined' ? PURPOSE : null };"
            );
            mod = factory(guardedBot(this.loop.bot), this.loop, this.loop.mcData, Vec3, goals, Movements, restrictedRequire);
        } catch (e) {
            return { ok: false, error: `compile error: ${e.message}` };
        }
        if (typeof mod.menu !== "function" || typeof mod.execute !== "function") {
            return { ok: false, error: "menu and execute must be functions" };
        }
        // The action's own declaration of what it is for: {description, achieves}, where
        // achieves is a VerifySpec over the predicate catalog. It is written by the model
        // that wrote the action; the menu hides the action while `achieves` already holds.
        let declared = null;
        if (mod.PURPOSE !== null && mod.PURPOSE !== undefined) {
            const P = mod.PURPOSE;
            if (typeof P !== "object" || Array.isArray(P)) return { ok: false, error: "PURPOSE must be an object {description, achieves}" };
            if (typeof P.description !== "string" || !P.description.trim()) return { ok: false, error: "PURPOSE.description must be a non-empty string" };
            if (!P.achieves || typeof P.achieves !== "object") return { ok: false, error: 'PURPOSE.achieves must be a VerifySpec object such as {has_item: {name: "*_planks", count: 4}}' };
            const bad = require("./verify").validate(P.achieves, "PURPOSE.achieves");
            if (bad) return { ok: false, error: `PURPOSE.achieves is not a valid VerifySpec: ${bad}` };
            declared = { description: P.description.trim().slice(0, 160), achieves: P.achieves };
        }
        if (snap) {
            try {
                const d = mod.menu(snap, this.loop);
                if (d && typeof d.then === "function") return { ok: false, error: "menu() must be synchronous (it returned a Promise)" };
                if (d !== null && d !== undefined && typeof d !== "string") return { ok: false, error: "menu() must return a string or null" };
            } catch (e) {
                return { ok: false, error: `menu() threw: ${e.message}` };
            }
            if (mod.observe) {
                try {
                    const o = mod.observe(this.loop.bot, this.loop);
                    if (o && typeof o.then === "function") return { ok: false, error: "observe() must be synchronous (it returned a Promise)" };
                    if (o !== null && o !== undefined && (typeof o !== "object" || Array.isArray(o))) return { ok: false, error: "observe() must return a plain object or null" };
                } catch (e) {
                    return { ok: false, error: `observe() threw: ${e.message}` };
                }
            }
        }
        const prev = this.items.get(name);
        const fresh = !prev || Boolean(opts.repair);
        this.items.set(name, {
            name,
            source,
            menu: mod.menu,
            execute: mod.execute,
            observe: mod.observe,
            trials: fresh ? 0 : prev.trials,
            successes: fresh ? 0 : prev.successes,
            failures: fresh ? 0 : prev.failures,
            outcomeLog: fresh ? [] : prev.outcomeLog || [],
            lastOutcomes: fresh ? [] : prev.lastOutcomes || [],
            retired: false,
            repaired: Boolean(opts.repair) || (prev ? prev.repaired : false),
            version: Number(opts.version) > 0 ? Number(opts.version) : prev ? prev.version || 1 : 1,
            registeredAt: Date.now(),
            learnedFor: purpose || (prev ? prev.learnedFor : null), // the subtask it was built for
            purpose: declared ? declared.description : null, // what the action says it is for
            purposeSpec: declared ? declared.achieves : null, // what it says it achieves; hidden while this holds
            menuBuilds: 0, // snapshots since registration
            offered: 0, // snapshots where menu() returned a line
        });
        if (opts.repair && Array.isArray(this.loop.recent)) {
            // the failures in `recent` belong to the previous version: say so, so neither
            // Jev nor its effect questions hold them against the repaired one
            for (const r of this.loop.recent) {
                if (r.action === `x:${name}` && !/\(earlier version; repaired since\)/.test(r.outcome || "")) {
                    r.outcome = `${r.outcome} (earlier version; repaired since)`;
                }
            }
        }
        console.log(`primitives: registered x:${name}${opts.repair ? ` (repaired, v${this.items.get(name).version})` : prev ? " (replaced)" : ""}`);
        return { ok: true };
    }

    remove(name) {
        return this.items.delete(name);
    }

    get(name) {
        return this.items.get(name) || null;
    }

    isTrial(p) {
        return !p.retired && p.successes === 0;
    }

    // A trial that has gone this many snapshots without ever being offered is
    // judged inapplicable: its menu() condition never matches the world, so it
    // can never be tested and must not block the builder.
    isStale(p) {
        return this.isTrial(p) && p.offered === 0 && p.menuBuilds >= STALE_AFTER_MENU_BUILDS;
    }

    // A registered action that has never succeeded, is not retired, and has
    // actually been offered (or is too new to judge).
    hasUntestedTrial() {
        for (const p of this.items.values()) if (this.isTrial(p) && !this.isStale(p)) return true;
        return false;
    }

    staleTrials() {
        return [...this.items.values()].filter((p) => this.isStale(p)).map((p) => p.name);
    }

    // Menu entries for the current snapshot: id -> description.
    menuEntries(snap) {
        const out = {};
        for (const p of this.items.values()) {
            if (p.retired) continue;
            // its reason to exist is already met (e.g. a table is in the bag or placed nearby)
            if ((this.loop.satisfiedPurposes || new Set()).has(p.name)) continue;
            let d = null;
            try {
                d = p.menu(snap, this.loop);
            } catch (e) {
                d = null;
            }
            p.menuBuilds++;
            if (typeof d === "string" && d.trim()) {
                p.offered++;
                const trial = this.isTrial(p)
                    ? p.repaired && p.trials === 0
                        ? " [repaired since its last failure; this version is untested]"
                        : ` [new action, untested${p.trials ? `, ${p.trials} failed trial(s)` : ""}]`
                    : "";
                let purpose = "";
                if (p.purpose) purpose = ` [purpose: ${p.purpose}]`;
                else if (p.learnedFor) purpose = ` [learned for: ${p.learnedFor}]`;
                out[`x:${p.name}`] = d.trim().slice(0, 300) + trial + purpose;
            }
        }
        return out;
    }

    // Extra snapshot fields from primitives that define observe().
    observeEntries(bot, loop) {
        const out = {};
        for (const p of this.items.values()) {
            if (p.retired || !p.observe) continue;
            try {
                const o = p.observe(guardedBot(bot), loop);
                if (o && typeof o === "object" && !Array.isArray(o) && Object.keys(o).length) out[p.name] = o;
            } catch (e) {
                /* a broken observe must not break the snapshot */
            }
        }
        return out;
    }

    // `meta` = {id, label}: the decision-record id and the typed outcome label
    recordOutcome(name, ok, outcome, meta = {}) {
        const p = this.items.get(name);
        if (!p) return;
        p.trials++;
        const text = String(outcome || (ok ? "ok" : "failed"));
        // the last few outcome texts go back to the builder so a rebuild can see WHY it failed
        p.lastOutcomes = [...(p.lastOutcomes || []), text.slice(0, 160)].slice(-3);
        // ...and the full evidence for a repair: every outcome of this version with its
        // decision-record id and failure label (capped)
        p.outcomeLog = [...(p.outcomeLog || []), { id: meta.id || null, ok: Boolean(ok), label: meta.label || null, outcome: text.slice(0, 300) }].slice(-12);
        if (ok) p.successes++;
        else p.failures++;
        if (!p.retired && p.successes === 0 && p.failures >= RETIRE_AFTER_FAILURES) {
            p.retired = true;
            console.log(`primitives: retired x:${name} after ${p.failures} failures`);
        }
    }

    markRepaired(name) {
        const p = this.items.get(name);
        if (p) p.repaired = true;
    }

    summary() {
        return [...this.items.values()].map((p) => ({
            name: p.name,
            trials: p.trials,
            successes: p.successes,
            failures: p.failures,
            retired: p.retired,
            version: p.version || 1,
            outcome_log: p.outcomeLog || [],
            trial: this.isTrial(p),
            stale: this.isStale(p),
            offered: p.offered,
            last_outcomes: p.lastOutcomes || [],
            repaired: p.repaired,
        }));
    }
}

module.exports = { guardedBot, PrimitiveRegistry, staticCheck, loopsWithoutAwait, RETIRE_AFTER_FAILURES };
