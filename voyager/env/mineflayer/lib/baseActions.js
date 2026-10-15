// The starting menu: movement and looking only (PLAN.md section 2.2). Every
// other capability (mining, crafting, placing) is meant to be added by the
// action builder as a registered primitive. Movement uses raw control states
// with a timeout, not the pathfinder, so routing is something the agent has to
// learn.
//
// menu(snap, bot, loop) -> { id: "one-line description with live facts" }
// execute(bot, loop, id) -> "ok ..." | "failed: ..."
const { Vec3 } = require("vec3");
const { COMPASS } = require("./jevTraversal");
const { matcherFor, nearestBlocks, HAZARDS: HAZARD_LIST } = require("./observe");
const HAZARDS = new Set(HAZARD_LIST);

const STEP_BLOCKS = 4;
const MOVE_TIMEOUT_MS = 2500;
const POLL_MS = 100;
const STALL_MS = 500; // no horizontal movement for this long -> try a jump, then give up
const PITCH_UP = (60 * Math.PI) / 180;
const DIRECTIONS = ["north", "south", "east", "west"];
// mineflayer yaw: 0 = north (-z), pi/2 = west (-x), pi = south (+z), -pi/2 = east
// (view direction is (-sin yaw, 0, -cos yaw); mineflayer converts from the protocol's south-based yaw)
const YAW = { north: 0, west: Math.PI / 2, south: Math.PI, east: -Math.PI / 2 };
const LIQUID = /^(water|lava|flowing_water|flowing_lava)$/;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Look along a cardinal direction from the feet: for each of the next
// STEP_BLOCKS columns report the first thing that matters (a wall, a drop,
// liquid), else "open".
function survey(bot, dir) {
    const { dx, dz } = COMPASS[dir];
    const feet = bot.entity.position.floored();
    const notes = [];
    let groundName = null;
    for (let i = 1; i <= STEP_BLOCKS; i++) {
        const col = feet.offset(dx * i, 0, dz * i);
        const atFeet = bot.blockAt(col);
        const atHead = bot.blockAt(col.offset(0, 1, 0));
        const ground = bot.blockAt(col.offset(0, -1, 0));
        const stepUp = bot.blockAt(col.offset(0, 2, 0));
        const n = (b) => (b ? b.name : "unloaded");
        if (atFeet && LIQUID.test(atFeet.name)) return `${atFeet.name.replace("flowing_", "")} ahead at ${i} block${i > 1 ? "s" : ""}`;
        if (ground && LIQUID.test(ground.name)) return `${ground.name.replace("flowing_", "")} ahead at ${i} block${i > 1 ? "s" : ""}`;
        if (atHead && atHead.boundingBox === "block") return `blocked by ${n(atHead)} after ${i - 1} block${i - 1 === 1 ? "" : "s"}`;
        if (atFeet && atFeet.boundingBox === "block") {
            // a one-block step up is walkable if head room above it is clear
            if (stepUp && stepUp.boundingBox === "block") return `blocked by ${n(atFeet)} after ${i - 1} block${i - 1 === 1 ? "" : "s"}`;
            notes.push(`step up onto ${n(atFeet)} at ${i}`);
            continue;
        }
        if (ground && ground.boundingBox !== "block") {
            // how deep is the drop?
            let depth = 1;
            while (depth < 6) {
                const b = bot.blockAt(col.offset(0, -1 - depth, 0));
                if (!b || b.boundingBox === "block") break;
                if (LIQUID.test(b.name)) return `${b.name.replace("flowing_", "")} below a drop at ${i} blocks`;
                depth++;
            }
            if (depth >= 3) return `drop of ${depth}+ blocks at ${i}`;
            notes.push(`drop of ${depth} at ${i}`);
            continue;
        }
        if (!groundName && ground) groundName = ground.name;
    }
    const base = groundName ? `ground: ${groundName}` : "ground: unknown";
    return notes.length ? `${base}, ${notes[0]}` : `${base}, open for ${STEP_BLOCKS} blocks`;
}

function menu(snap, bot, loop) {
    const out = {};
    // Which of the blocks the subtask cares about lie in each direction: Jev
    // should not have to turn signed dx/dz offsets into a compass heading.
    const toward = {};
    for (const [key, b] of Object.entries(snap.blocks || {})) {
        if (HAZARDS.has(key)) continue;
        const dist = Math.round(Math.hypot(b.dx, b.dz));
        if (dist < 2) continue;
        const label = key.startsWith("*_") ? key.slice(2) : key;
        const dirs = [];
        if (Math.abs(b.dz) >= 2) dirs.push(b.dz < 0 ? "north" : "south");
        if (Math.abs(b.dx) >= 2) dirs.push(b.dx < 0 ? "west" : "east");
        for (const d of dirs) (toward[d] = toward[d] || []).push(`${label} ${dist} blocks`);
    }
    for (const dir of DIRECTIONS) {
        let note = "terrain unknown";
        try {
            note = survey(bot, dir);
        } catch (err) {
            /* keep default */
        }
        const hint = toward[dir] ? `; toward ${toward[dir].slice(0, 3).join(", ")}` : "";
        out[`move:${dir}`] = `walk up to ${STEP_BLOCKS} blocks ${dir} (${note}${hint})`;
    }
    out["move:up"] = `jump once in place (standing on ${blockUnder(bot)})`;
    for (const dir of DIRECTIONS) {
        out[`look:${dir}`] = snap.bot.yaw === dir ? `already facing ${dir}; keep facing ${dir}, level` : `turn to face ${dir}, level`;
    }
    out["look:up"] = `tilt the view up 60 degrees (now ${snap.bot.pitch} degrees)`;
    out["look:down"] = `tilt the view down 60 degrees (now ${snap.bot.pitch} degrees)`;
    for (const [key, b] of Object.entries(snap.blocks || {})) {
        const d = Math.round(Math.hypot(b.dx, b.dy, b.dz));
        const label = key.startsWith("*_") ? `nearest ${key.slice(2)} (any kind)` : `nearest ${key}`;
        out[`look:at:${key}`] = `turn to look at the ${label}, ${d} blocks away (dx=${b.dx} dy=${b.dy} dz=${b.dz}); cursor now: ${snap.cursor.block || "nothing"}`;
    }
    out.wait = "stand still for 1 second";
    return out;
}

function blockUnder(bot) {
    try {
        const b = bot.blockAt(bot.entity.position.offset(0, -1, 0));
        return b ? b.name : "unknown";
    } catch (err) {
        return "unknown";
    }
}

function isBase(id) {
    return id === "wait" || id.startsWith("move:") || id.startsWith("look:");
}

async function execute(bot, loop, id) {
    if (id === "wait") {
        await sleep(1000);
        return "ok waited 1 second";
    }
    if (id === "move:up") return jumpOnce(bot);
    if (id.startsWith("move:")) {
        const dir = id.slice(5);
        if (!DIRECTIONS.includes(dir)) return `failed: unknown direction ${dir}`;
        return walk(bot, dir);
    }
    if (id.startsWith("look:at:")) return lookAtNearest(bot, loop, id.slice(8));
    if (id.startsWith("look:")) {
        const what = id.slice(5);
        if (what === "up" || what === "down") {
            await bot.look(bot.entity.yaw, what === "up" ? PITCH_UP : -PITCH_UP, true);
            return `ok looking ${what} (pitch ${what === "up" ? 60 : -60} degrees)`;
        }
        if (!(what in YAW)) return `failed: unknown look target ${what}`;
        await bot.look(YAW[what], 0, true);
        return `ok facing ${what}`;
    }
    return `failed: unknown base action ${id}`;
}

async function jumpOnce(bot) {
    const y0 = bot.entity.position.y;
    bot.setControlState("jump", true);
    await sleep(150);
    bot.setControlState("jump", false);
    await sleep(600);
    const dy = Math.round((bot.entity.position.y - y0) * 10) / 10;
    return dy > 0.3 ? `ok jumped, now ${dy} higher` : "ok jumped in place, landed at the same height";
}

async function walk(bot, dir) {
    const { dx, dz } = COMPASS[dir];
    const start = bot.entity.position.clone();
    const moved = () => (bot.entity.position.x - start.x) * dx + (bot.entity.position.z - start.z) * dz;
    const liquidAhead = () => {
        const feet = bot.entity.position.floored();
        for (const dy of [0, -1]) {
            const b = bot.blockAt(feet.offset(dx, dy, dz));
            if (b && LIQUID.test(b.name)) return b.name.replace("flowing_", "");
        }
        return null;
    };
    await bot.look(YAW[dir], 0, true);
    const inLiquidAtStart = bot.entity.isInWater || bot.entity.isInLava;
    bot.setControlState("forward", true);
    const t0 = Date.now();
    let lastProgress = moved();
    let lastProgressAt = t0;
    let jumped = false;
    let stop = null;
    try {
        while (Date.now() - t0 < MOVE_TIMEOUT_MS) {
            await sleep(POLL_MS);
            const m = moved();
            if (m >= STEP_BLOCKS) break;
            if (!inLiquidAtStart) {
                const liq = liquidAhead();
                if (liq) {
                    stop = `stopped at the edge of ${liq}`;
                    break;
                }
            }
            if (m > lastProgress + 0.05) {
                lastProgress = m;
                lastProgressAt = Date.now();
                if (jumped) {
                    bot.setControlState("jump", false);
                    jumped = false;
                }
            } else if (Date.now() - lastProgressAt > STALL_MS) {
                if (!jumped) {
                    bot.setControlState("jump", true); // one step up allowed
                    jumped = true;
                    lastProgressAt = Date.now();
                } else {
                    stop = "blocked";
                    break;
                }
            }
        }
    } finally {
        bot.setControlState("forward", false);
        bot.setControlState("jump", false);
    }
    const n = Math.max(0, Math.round(moved()));
    const dy = Math.round(bot.entity.position.y - start.y);
    const climb = dy > 0 ? `, climbed ${dy}` : dy < 0 ? `, dropped ${-dy}` : "";
    if (n === 0) return `failed: could not move ${dir} (${stop || "no movement"}; something is in the way)`;
    if (stop) return `ok moved ${n} block${n === 1 ? "" : "s"} ${dir} then ${stop}${climb}`;
    return `ok moved ${n} block${n === 1 ? "" : "s"} ${dir}${climb}`;
}

async function lookAtNearest(bot, loop, key) {
    const blocks = nearestBlocks(bot, loop.mcData, matcherFor(key), 16, 1);
    if (!blocks.length) return `failed: no ${key} within 16 blocks to look at`;
    const b = blocks[0];
    await bot.lookAt(b.position.offset(0.5, 0.5, 0.5), true);
    const d = Math.round(b.position.offset(0.5, 0.5, 0.5).distanceTo(bot.entity.position));
    let cursor = null;
    try {
        const c = typeof bot.blockAtCursor === "function" ? bot.blockAtCursor(6) : null;
        cursor = c ? c.name : null;
    } catch (err) {
        cursor = null;
    }
    const cursorNote = cursor === b.name ? `; cursor is on it` : cursor ? `; cursor hits ${cursor} first` : d > 6 ? `; too far for the cursor (over 6 blocks)` : "";
    return `ok looking at ${b.name} ${d} blocks away${cursorNote}`;
}

module.exports = { menu, execute, isBase, survey, DIRECTIONS, STEP_BLOCKS, YAW };
