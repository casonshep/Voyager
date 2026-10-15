// The state Jev reads for each decision: the snapshot (observe.js) with its
// numbers pre-digested into words. The TypeSafe docs are explicit that Jev
// "will perform better on semantic questions than mathematical ones", that
// "counting is unreliable", and that "accuracy falls as the state grows with
// content unrelated to the decision". So signed dx/dy/dz offsets become
// "6 blocks north-east, 1 above, out of reach", the 30-block view ray becomes
// what the agent is looking at, horizon samples become "trees at 24 blocks",
// and action records become one line each with their outcome label.
//
// Generated primitives keep reading the raw snapshot (their contract documents
// it); only the Jev request uses this digest. Every field is still an observed
// value: nothing here is inferred or decided.
const { compassNameOf } = require("./jevTraversal");

const REACH = 4.5; // dig / use reach in blocks
const NEAR = 12;
const HAZARDS = new Set(["lava", "water", "fire", "cactus"]);

function direction(dx, dz) {
    if (Math.abs(dx) < 1 && Math.abs(dz) < 1) return "right here";
    return compassNameOf(dx, dz);
}

function vertical(dy) {
    if (!dy) return "level";
    return `${Math.abs(dy)} ${dy > 0 ? "above" : "below"}`;
}

function reachWord(dist) {
    if (dist <= REACH) return "within reach";
    if (dist <= NEAR) return "near, out of reach";
    return "far";
}

function blockLine(b) {
    const dist = Math.round(Math.hypot(b.dx, b.dy, b.dz));
    const parts = [`nearest is ${dist} block${dist === 1 ? "" : "s"} away (${reachWord(dist)})`, direction(b.dx, b.dz), vertical(b.dy)];
    if (b.count > 1) parts.push(b.count >= 32 ? "many more around" : "a few more around");
    return parts.join(", ");
}

function lookingAt(snap) {
    const c = snap.cursor || {};
    const seen = (snap.view || []).find((v) => v.hit);
    if (c.block) {
        const reach = c.distance !== null && c.distance !== undefined && c.distance <= REACH ? "within reach" : "out of reach";
        return `${c.block}, ${c.distance} blocks away (${reach})`;
    }
    if (seen) return `${seen.block} at ${seen.from} blocks (too far to touch)`;
    return "open air or sky";
}

function facing(snap) {
    const b = snap.bot || {};
    const p = b.pitch || 0;
    const tilt = p > 20 ? `looking up ${p} degrees` : p < -20 ? `looking down ${-p} degrees` : "looking level";
    return `${b.yaw}, ${tilt}`;
}

// "12:land:grass_block+1 24:trees+3 40:water-2" -> "grass_block at 12, trees at 24 (3 up), water at 40"
function horizonLine(h) {
    if (!h || !h.samples) return "unknown";
    const out = [];
    for (const sample of String(h.samples).split(/\s+/)) {
        const m = sample.match(/^(\d+):(land:)?([a-z_]+)([+-]\d+)?$/);
        if (!m) continue;
        const [, dist, , what, riseText] = m;
        if (what === "unloaded") continue;
        const rise = riseText ? Number(riseText) : 0;
        const climb = Math.abs(rise) >= 3 ? ` (${Math.abs(rise)} ${rise > 0 ? "up" : "down"})` : "";
        out.push(`${what} at ${dist}${climb}`);
    }
    return out.join(", ") || "unknown";
}

function deltaText(d) {
    const parts = [];
    if (d && d.pos >= 1) parts.push(`moved ${d.pos}`);
    if (d && d.inv) for (const [k, n] of Object.entries(d.inv)) parts.push(`${n > 0 ? "+" : ""}${n} ${k}`);
    if (d && d.health) parts.push(`health ${d.health > 0 ? "+" : ""}${d.health}`);
    if (d && d.cursor) parts.push(`now sees ${d.cursor}`);
    return parts.length ? parts.join(", ") : "no change";
}

function recentLines(recent) {
    return (recent || []).map((r) => `${r.action} -> ${String(r.outcome || "").slice(0, 140)} [${r.label || "?"}; ${deltaText(r.delta)}]`);
}

function relative(snap, p) {
    const me = (snap.bot || {}).position || { x: 0, y: 0, z: 0 };
    const dx = p.x - me.x;
    const dy = p.y - me.y;
    const dz = p.z - me.z;
    const dist = Math.round(Math.hypot(dx, dy, dz));
    return `${dist} blocks ${direction(dx, dz)}, ${vertical(dy)} (${reachWord(dist)})`;
}

function digest(snap) {
    const s = snap || {};
    const b = s.bot || {};
    const nearby = {};
    const hazards = {};
    for (const [k, v] of Object.entries(s.blocks || {})) {
        const label = k.startsWith("*_") ? `${k.slice(2)} (any kind)` : k;
        if (HAZARDS.has(k)) hazards[label] = blockLine(v);
        else nearby[label] = blockLine(v);
    }
    const horizon = {};
    for (const [dir, h] of Object.entries(s.horizon || {})) horizon[dir] = horizonLine(h);
    const equipment = {};
    for (const [slot, item] of Object.entries(s.slots || {})) if (item) equipment[slot] = item;
    const placed = {};
    for (const [name, p] of Object.entries(s.placed_by_me || {})) placed[name] = relative(s, p);
    const sub = s.subtask || null;
    const out = {
        agent: {
            health: `${b.health}/20`,
            food: `${b.food}/20`,
            oxygen: b.inWater ? `${b.oxygen}/20` : undefined,
            in_water: b.inWater || undefined,
            in_lava: b.inLava || undefined,
            on_ground: b.onGround === false ? false : undefined,
            daylight: b.daylight,
            biome: b.biome,
            facing: facing(s),
        },
        looking_at: lookingAt(s),
        nearby_blocks: nearby,
        hazards,
        horizon,
        entities: (s.entities || []).map((e) => `${e.name}, ${e.distance} blocks ${e.direction}`),
        dropped_items: (s.dropped || []).map((d) => `${d.name}, ${d.distance} blocks away`),
        inventory: s.inventory || {},
        equipment,
        placed_by_me: placed,
        subtask: sub
            ? {
                  description: sub.description,
                  done_when: sub.verify,
                  progress: sub.progress ? `has ${sub.progress.have} of ${sub.progress.need}` : undefined,
              }
            : null,
        recent: recentLines(s.recent),
        since_progress: s.since_progress ? `${s.since_progress.actions} actions and ${s.since_progress.seconds} s since the last gain` : undefined,
        extra: s.extra && Object.keys(s.extra).length ? s.extra : undefined,
    };
    return JSON.parse(JSON.stringify(out)); // drop undefined fields
}

// Fisher-Yates over the option entries: current Jev "leans toward the option that
// comes first" (TypeSafe docs), so a fixed order would bias every decision.
function shuffled(obj, rand = Math.random) {
    const entries = Object.entries(obj);
    for (let i = entries.length - 1; i > 0; i--) {
        const j = Math.floor(rand() * (i + 1));
        [entries[i], entries[j]] = [entries[j], entries[i]];
    }
    return Object.fromEntries(entries);
}

module.exports = { digest, shuffled, horizonLine, blockLine, recentLines, REACH };
