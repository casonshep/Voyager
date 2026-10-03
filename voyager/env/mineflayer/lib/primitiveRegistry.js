// Runtime-registered actions for the fast loop.
//
// A primitive is a JavaScript module source that defines two functions:
//
//     function menu(snap, loop)            // SYNCHRONOUS: a one-line description
//                                          // with live facts when the action applies
//                                          // right now, or null to stay off the menu
//     async function execute(bot, loop, ctx) // does one bounded thing; returns a string
//                                            // starting with "ok" on success, otherwise
//                                            // "failed: <why>"
//
// The source is compiled once with a restricted scope (bot, loop, mcData, Vec3,
// goals, Movements, a require limited to a few packages). Registered actions
// are offered on the menu as `x:<name>`, executed under a timeout, and retired
// after repeated failure. The brain generates and posts them (voyager/fast/generator.py).
const { Vec3 } = require("vec3");

const MAX_SOURCE_CHARS = 12000;
const RETIRE_AFTER_FAILURES = 3;
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
    const loops = loopsWithoutAwait(source);
    if (loops.length) return loops[0];
    return null;
}

function restrictedRequire(name) {
    if (!ALLOWED_REQUIRES.has(name)) throw new Error(`require("${name}") is not allowed in a primitive`);
    return require(name);
}

class PrimitiveRegistry {
    constructor(loop) {
        this.loop = loop;
        this.items = new Map(); // name -> {name, source, menu, execute, trials, successes, failures, retired, registeredAt}
    }

    // Compile and register; returns {ok, error}. A snapshot (optional) lets the
    // menu function run once so an immediate throw is reported, not hidden.
    register(name, source, snap) {
        const err = staticCheck(name, source);
        if (err) return { ok: false, error: err };
        let mod;
        try {
            const { goals, Movements } = require("mineflayer-pathfinder");
            const factory = new Function(
                "bot", "loop", "mcData", "Vec3", "goals", "Movements", "require",
                source + "\nreturn { menu, execute };"
            );
            mod = factory(this.loop.bot, this.loop, this.loop.mcData, Vec3, goals, Movements, restrictedRequire);
        } catch (e) {
            return { ok: false, error: `compile error: ${e.message}` };
        }
        if (typeof mod.menu !== "function" || typeof mod.execute !== "function") {
            return { ok: false, error: "menu and execute must be functions" };
        }
        if (snap) {
            try {
                const d = mod.menu(snap, this.loop);
                if (d && typeof d.then === "function") return { ok: false, error: "menu() must be synchronous (it returned a Promise)" };
                if (d !== null && d !== undefined && typeof d !== "string") return { ok: false, error: "menu() must return a string or null" };
            } catch (e) {
                return { ok: false, error: `menu() threw: ${e.message}` };
            }
        }
        const prev = this.items.get(name);
        this.items.set(name, {
            name,
            source,
            menu: mod.menu,
            execute: mod.execute,
            trials: prev ? prev.trials : 0,
            successes: prev ? prev.successes : 0,
            failures: prev ? prev.failures : 0,
            retired: false,
            repaired: prev ? prev.repaired : false,
            registeredAt: Date.now(),
        });
        console.log(`primitives: registered x:${name}${prev ? " (replaced)" : ""}`);
        return { ok: true };
    }

    remove(name) {
        return this.items.delete(name);
    }

    get(name) {
        return this.items.get(name) || null;
    }

    // Menu entries for the current snapshot: id -> description.
    menuEntries(snap) {
        const out = {};
        for (const p of this.items.values()) {
            if (p.retired) continue;
            let d = null;
            try {
                d = p.menu(snap, this.loop);
            } catch (e) {
                d = null;
            }
            if (typeof d === "string" && d.trim()) {
                const trial = p.successes === 0 ? ` [new action, ${p.trials} trial(s) so far]` : "";
                out[`x:${p.name}`] = d.trim().slice(0, 300) + trial;
            }
        }
        return out;
    }

    recordOutcome(name, ok) {
        const p = this.items.get(name);
        if (!p) return;
        p.trials++;
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
            repaired: p.repaired,
        }));
    }
}

module.exports = { PrimitiveRegistry, staticCheck, loopsWithoutAwait, RETIRE_AFTER_FAILURES };
