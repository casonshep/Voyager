// Snapshot of the world for Jev and for primitive menu() functions
// (PLAN.md section 1.3). Everything here is an observed value; nothing is
// inferred. The block list is capped to an allowed set so the prompt does not
// fill with stone and dirt: the allowed set is what the current subtask tree
// needs (sent by Python), plus hazards, plus whatever the cursor points at.
const { Vec3 } = require("vec3");
const { compassNameOf, nearbyEntitiesOf } = require("./jevTraversal");

const BLOCK_RADIUS = 32; // how far the snapshot lists nearest blocks of each relevant type
const BLOCK_SCAN_COUNT = 64;
const ENTITY_RADIUS = 24;
const ENTITY_LIMIT = 8;
const RECENT_LIMIT = 8;
const EXTRA_CHARS = 400;
const HAZARDS = ["lava", "water", "fire", "cactus"];
const SLOT_INDEX = { head: 5, chest: 6, legs: 7, feet: 8, offhand: 45 };

// '*_log' matches any *_log name; a plain name matches itself.
function matcherFor(key) {
    if (typeof key !== "string") return () => false;
    if (key.startsWith("*_")) {
        const suffix = key.slice(2);
        return (name) => name === suffix || name.endsWith(`_${suffix}`);
    }
    return (name) => name === key;
}

function blockIdsMatching(mcData, matches) {
    const ids = [];
    for (const [name, block] of Object.entries(mcData.blocksByName)) if (matches(name)) ids.push(block.id);
    return ids;
}

function inventoryCounts(bot) {
    const counts = {};
    if (!bot.inventory) return counts;
    for (const item of bot.inventory.items()) {
        if (item) counts[item.name] = (counts[item.name] || 0) + item.count;
    }
    return counts;
}

function slotsOf(bot) {
    const slots = bot.inventory && bot.inventory.slots ? bot.inventory.slots : [];
    const nameAt = (i) => (slots[i] ? slots[i].name : null);
    return {
        hand: bot.heldItem ? bot.heldItem.name : null,
        offhand: nameAt(SLOT_INDEX.offhand),
        head: nameAt(SLOT_INDEX.head),
        chest: nameAt(SLOT_INDEX.chest),
        legs: nameAt(SLOT_INDEX.legs),
        feet: nameAt(SLOT_INDEX.feet),
    };
}

// mineflayer view direction is (-sin yaw, 0, -cos yaw): yaw 0 = north, pi/2 = west, pi = south, -pi/2 = east
function yawToCompass(yaw) {
    return compassNameOf(-Math.sin(yaw), -Math.cos(yaw));
}

function daylightOf(t) {
    return t >= 13000 && t < 23000 ? "night" : "day";
}

function biomeOf(bot, mcData) {
    try {
        const block = bot.blockAt(bot.entity.position);
        if (block && block.biome) {
            const byId = mcData.biomes && mcData.biomes[block.biome.id];
            return block.biome.name || (byId && byId.name) || "unknown";
        }
    } catch (err) {
        /* fall through */
    }
    return "unknown";
}

function cursorOf(bot) {
    const out = { block: null, distance: null, entity: null };
    try {
        const b = typeof bot.blockAtCursor === "function" ? bot.blockAtCursor(6) : null;
        if (b) {
            out.block = b.name;
            out.distance = Math.round(b.position.offset(0.5, 0.5, 0.5).distanceTo(bot.entity.position.offset(0, 1.62, 0)));
        }
    } catch (err) {
        /* no cursor block */
    }
    try {
        const e = typeof bot.entityAtCursor === "function" ? bot.entityAtCursor(6) : null;
        if (e) out.entity = e.name || e.username || "unknown";
    } catch (err) {
        /* no cursor entity */
    }
    return out;
}

const HORIZON_DIRS = {
    north: [0, -1], northeast: [0.7071, -0.7071], east: [1, 0], southeast: [0.7071, 0.7071],
    south: [0, 1], southwest: [-0.7071, 0.7071], west: [-1, 0], northwest: [-0.7071, -0.7071],
};
const HORIZON_DISTANCES = [12, 24, 40];

// The top block of the column at (x, z), scanning down from well above the bot.
function surfaceAt(bot, x, z, y0) {
    for (let y = y0 + 20; y >= y0 - 24; y--) {
        const b = bot.blockAt(new Vec3(x, y, z));
        if (!b) return null; // not loaded
        if (b.name === "air" || b.name === "cave_air" || b.name === "void_air" || b.name === "snow") continue;
        return { block: b, y };
    }
    return null;
}

// What lies in each compass direction beyond the 32-block block scan: for each of
// eight headings and distances 12/24/40, the surface ("water", "land:<block>",
// "trees", or "unloaded") and the height change. Trees are any column topped by
// leaves or logs. Lets exploration choose a heading instead of guessing one.
function horizonOf(bot) {
    const out = {};
    try {
        const pos = bot.entity.position;
        const y0 = Math.floor(pos.y);
        for (const [dir, [dx, dz]] of Object.entries(HORIZON_DIRS)) {
            const samples = [];
            let trees = 0;
            let water = 0;
            for (const d of HORIZON_DISTANCES) {
                let label = "unloaded";
                let rise = null;
                // a 3-column fan at each distance so one gap does not hide a forest
                for (const off of [-2, 0, 2]) {
                    const x = Math.floor(pos.x + dx * d - dz * off);
                    const z = Math.floor(pos.z + dz * d + dx * off);
                    const s = surfaceAt(bot, x, z, y0);
                    if (!s) continue;
                    const n = s.block.name;
                    let l;
                    if (n.endsWith("_leaves") || n.endsWith("_log")) {
                        l = "trees";
                        trees++;
                    } else if (n === "lava") {
                        l = "lava";
                    } else if (n === "water" || n === "ice" || n === "packed_ice" || n === "blue_ice") {
                        l = "water";
                        water++;
                    } else l = `land:${n}`;
                    if (label === "unloaded" || l === "trees" || (label.startsWith("land") && l === "water")) label = l;
                    if (rise === null) rise = s.y - y0;
                }
                samples.push(`${d}:${label}${rise !== null && label !== "unloaded" ? (rise >= 0 ? `+${rise}` : `${rise}`) : ""}`);
            }
            out[dir] = { trees, water, samples: samples.join(" ") };
        }
    } catch (err) {
        /* no horizon */
    }
    return out;
}

const VIEW_RAY_LENGTH = 30;
const VIEW_RAY_STEP = 0.25;
const VIEW_RAY_MAX_SEGMENTS = 8;
const SEE_THROUGH = new Set(["air", "cave_air", "void_air", "water", "glass", "short_grass", "grass", "tall_grass", "fern"]);

// What lies along the look direction, out to 30 blocks: consecutive blocks of
// the same name collapse into one segment {block, from, to} (distances from the
// eyes). Air is skipped. The first solid block is the one the bot actually sees
// ("hit": true); segments after it are behind it ("occluded": true). Leaves and
// water are listed but do not count as the hit.
function viewRayOf(bot) {
    const out = [];
    try {
        const eye = bot.entity.position.offset(0, bot.entity.height || 1.62, 0);
        const yaw = bot.entity.yaw || 0;
        const pitch = bot.entity.pitch || 0;
        const dir = { x: -Math.sin(yaw) * Math.cos(pitch), y: Math.sin(pitch), z: -Math.cos(yaw) * Math.cos(pitch) };
        let last = null;
        let seg = null;
        let hit = false;
        for (let d = VIEW_RAY_STEP; d <= VIEW_RAY_LENGTH; d += VIEW_RAY_STEP) {
            const p = eye.offset(dir.x * d, dir.y * d, dir.z * d).floored();
            const key = `${p.x},${p.y},${p.z}`;
            if (key === last) continue;
            last = key;
            const b = bot.blockAt(p);
            const name = b ? b.name : "unloaded";
            if (name === "air" || name === "cave_air" || name === "void_air") {
                seg = null;
                continue;
            }
            const dist = Math.round(d);
            if (seg && seg.block === name) {
                seg.to = dist;
                continue;
            }
            if (out.length >= VIEW_RAY_MAX_SEGMENTS) break;
            seg = { block: name, from: dist, to: dist };
            if (hit) seg.occluded = true;
            const solid = b && b.boundingBox === "block" && !SEE_THROUGH.has(name) && !name.endsWith("_leaves");
            if (!hit && solid) {
                seg.hit = true;
                hit = true;
            }
            out.push(seg);
        }
    } catch (err) {
        /* no view ray */
    }
    return out;
}

// Nearest instance per allowed key with a count within radius. Family keys
// ('*_log') collapse every matching block type into one entry under the key.
function blocksOf(bot, mcData, allowed) {
    const here = bot.entity.position;
    const out = {};
    for (const key of allowed) {
        const ids = blockIdsMatching(mcData, matcherFor(key));
        if (!ids.length) continue;
        let positions = [];
        try {
            positions = bot.findBlocks({ matching: ids, maxDistance: BLOCK_RADIUS, count: BLOCK_SCAN_COUNT });
        } catch (err) {
            continue;
        }
        if (!positions.length) continue;
        let nearest = null;
        let nd = Infinity;
        for (const p of positions) {
            const d = p.offset(0.5, 0.5, 0.5).distanceTo(here);
            if (d < nd) {
                nd = d;
                nearest = p;
            }
        }
        const base = here.floored();
        out[key] = {
            dx: nearest.x - base.x,
            dy: nearest.y - base.y,
            dz: nearest.z - base.z,
            count: positions.length,
        };
    }
    return out;
}

function droppedOf(bot) {
    const here = bot.entity.position;
    const out = [];
    for (const e of Object.values(bot.entities || {})) {
        if (!e || e.name !== "item" || !e.position) continue;
        const d = e.position.distanceTo(here);
        if (d > ENTITY_RADIUS) continue;
        let name = "item";
        try {
            const it = typeof e.getDroppedItem === "function" ? e.getDroppedItem() : null;
            if (it && it.name) name = it.name;
        } catch (err) {
            /* unknown item */
        }
        out.push({ name, distance: Math.round(d) });
    }
    return out.sort((a, b) => a.distance - b.distance).slice(0, ENTITY_LIMIT);
}

// Only placements whose block is still what we placed.
function placedByMeOf(bot, placed) {
    const out = {};
    for (const [name, p] of Object.entries(placed || {})) {
        try {
            const b = bot.blockAt(new Vec3(p.x, p.y, p.z));
            if (b && b.name === name) out[name] = { x: p.x, y: p.y, z: p.z };
        } catch (err) {
            /* chunk unloaded */
        }
    }
    return out;
}

// {have, need} from the first has_item predicate in a verify spec, else null.
function progressOf(spec, inventory) {
    const found = firstHasItem(spec);
    if (!found) return null;
    const m = matcherFor(found.name);
    let have = 0;
    for (const [n, c] of Object.entries(inventory)) if (m(n)) have += c;
    return { have, need: found.count || 1 };
}

function firstHasItem(spec) {
    if (!spec || typeof spec !== "object") return null;
    if (spec.has_item) return spec.has_item;
    for (const k of ["all", "any"]) {
        if (Array.isArray(spec[k])) for (const s of spec[k]) {
            const f = firstHasItem(s);
            if (f) return f;
        }
    }
    return null;
}

function extraOf(bot, loop) {
    const out = {};
    if (!loop.registry || typeof loop.registry.observeEntries !== "function") return out;
    for (const [name, value] of Object.entries(loop.registry.observeEntries(bot, loop))) {
        let text = JSON.stringify(value);
        if (text === undefined) continue;
        if (text.length > EXTRA_CHARS) {
            out[name] = { truncated: text.slice(0, EXTRA_CHARS) };
        } else {
            out[name] = value;
        }
    }
    return out;
}

// loop: { mcData, allowedBlocks: Set|Array, placedByMe, subtask, recent,
//         sinceProgress: {actions, seconds}, registry }
function snapshot(bot, loop) {
    const mcData = loop.mcData;
    const pos = bot.entity.position;
    const t = bot.time ? bot.time.timeOfDay : 0;
    const cursor = cursorOf(bot);
    const allowed = new Set([...(loop.allowedBlocks || []), ...HAZARDS]);
    if (cursor.block) allowed.add(cursor.block);
    const inventory = inventoryCounts(bot);
    const subtask = loop.subtask
        ? {
              id: loop.subtask.id,
              description: loop.subtask.description,
              verify: loop.subtask.verify,
              progress: progressOf(loop.subtask.verify, inventory),
          }
        : null;
    return {
        bot: {
            position: { x: Math.floor(pos.x), y: Math.floor(pos.y), z: Math.floor(pos.z) },
            yaw: yawToCompass(bot.entity.yaw || 0),
            pitch: Math.round(((bot.entity.pitch || 0) * 180) / Math.PI),
            health: bot.health,
            food: bot.food,
            oxygen: bot.oxygenLevel,
            xp: bot.experience ? bot.experience.level : 0,
            inWater: Boolean(bot.entity.isInWater),
            inLava: Boolean(bot.entity.isInLava),
            onGround: bot.entity.onGround !== false,
            timeOfDay: t,
            daylight: daylightOf(t),
            biome: biomeOf(bot, mcData),
        },
        cursor,
        view: viewRayOf(bot),
        horizon: horizonOf(bot),
        inventory,
        slots: slotsOf(bot),
        blocks: blocksOf(bot, mcData, allowed),
        entities: nearbyEntitiesOf(bot, ENTITY_RADIUS, ENTITY_LIMIT),
        dropped: droppedOf(bot),
        placed_by_me: placedByMeOf(bot, loop.placedByMe),
        subtask,
        recent: (loop.recent || []).slice(-RECENT_LIMIT),
        since_progress: loop.sinceProgress || { actions: 0, seconds: 0 },
        extra: extraOf(bot, loop),
    };
}

// ---- helpers shared with primitives (loop.*) --------------------------------

function nearestBlocks(bot, mcData, matches, maxDistance = BLOCK_RADIUS, count = 8) {
    const ids = blockIdsMatching(mcData, matches);
    if (!ids.length) return [];
    const here = bot.entity.position;
    return bot
        .findBlocks({ matching: ids, maxDistance, count })
        .map((p) => bot.blockAt(p))
        .filter(Boolean)
        .sort((a, b) => a.position.distanceTo(here) - b.position.distanceTo(here));
}

function nearestEntity(bot, names, maxDistance = 32) {
    const here = bot.entity.position;
    const set = new Set(Array.isArray(names) ? names : [names]);
    let best = null;
    let bestD = maxDistance;
    for (const e of Object.values(bot.entities || {})) {
        if (e === bot.entity || !e.position || !set.has(e.name)) continue;
        const d = e.position.distanceTo(here);
        if (d <= bestD) {
            best = e;
            bestD = d;
        }
    }
    return best;
}

// An air block with solid ground under it within `radius` of `pos`, nearest first.
function freeSpotNear(bot, pos, radius = 2) {
    const base = new Vec3(Math.floor(pos.x), Math.floor(pos.y), Math.floor(pos.z));
    let best = null;
    for (let dx = -radius; dx <= radius; dx++)
        for (let dz = -radius; dz <= radius; dz++)
            for (let dy = -1; dy <= 1; dy++) {
                if (!dx && !dz && !dy) continue;
                const p = base.offset(dx, dy, dz);
                const b = bot.blockAt(p);
                const below = bot.blockAt(p.offset(0, -1, 0));
                const above = bot.blockAt(p.offset(0, 1, 0));
                if (!b || !below || b.boundingBox !== "empty" || below.boundingBox !== "block") continue;
                if (above && above.boundingBox === "block") continue;
                const d = Math.abs(dx) + Math.abs(dz) + Math.abs(dy);
                if (!best || d < best.d) best = { p, d };
            }
    return best ? best.p : null;
}

module.exports = {
    snapshot,
    matcherFor,
    blockIdsMatching,
    inventoryCounts,
    slotsOf,
    yawToCompass,
    daylightOf,
    progressOf,
    nearestBlocks,
    nearestEntity,
    freeSpotNear,
    HAZARDS,
    viewRayOf,
    horizonOf,
    BLOCK_RADIUS,
};
