// Verification predicates (PLAN.md section 3). A VerifySpec is a JSON
// expression over these fixed predicates composed with all / any / not:
//
//   { "all": [ { "has_item": { "name": "cobblestone", "count": 3 } },
//              { "near_block": { "name": "crafting_table", "radius": 4 } } ] }
//
// evaluate(bot, loop, spec) -> { ok, detail }   (async: container_contains opens a chest)
// validate(spec) -> null | "error text"         (unknown predicate / bad params)
//
// Every predicate reads the bot client locally; nothing uses server commands.
const { Vec3 } = require("vec3");
const { PICKAXE_TIER } = require("./mcKnowledge");
const { matcherFor, blockIdsMatching, inventoryCounts, slotsOf, nearestEntity } = require("./observe");

const CONTAINER_CACHE_MS = 10 * 1000;
const REGION_MAX_VOLUME = 32 * 32 * 32;
const SLOT_NAMES = ["hand", "offhand", "head", "chest", "legs", "feet"];
const STATUS_FIELDS = ["health", "food", "xp", "oxygen"];

// name -> { params: { name: type }, required: [..], meaning }
// type: "string" | "number" | "pos" | "region"
const PREDICATES = {
    has_item: {
        params: { name: "string", count: "number" },
        required: ["name"],
        meaning: "inventory holds at least `count` of item `name` ('*_log' matches any log)",
    },
    has_equipped: {
        params: { name: "string", slot: "string" },
        required: ["name", "slot"],
        meaning: "item `name` is in equipment slot `slot` (hand, offhand, head, chest, legs, feet)",
    },
    has_tool_tier: {
        params: { tool: "string", material: "string" },
        required: ["tool", "material"],
        meaning: "holds a `tool` (pickaxe, axe, shovel, sword, hoe) of `material` tier or better (wooden < stone < iron < diamond < netherite)",
    },
    near_block: {
        params: { name: "string", radius: "number" },
        required: ["name"],
        meaning: "a block `name` exists within `radius` blocks (default 4)",
    },
    block_placed_by_me: {
        params: { name: "string" },
        required: ["name"],
        meaning: "the agent placed a `name` block and it is still there",
    },
    near_entity: {
        params: { name: "string", radius: "number" },
        required: ["name"],
        meaning: "an entity `name` (cow, zombie, villager, player name) is within `radius` blocks (default 8)",
    },
    in_biome: {
        params: { name: "string" },
        required: ["name"],
        meaning: "the agent stands in biome `name` (substring match, e.g. 'forest')",
    },
    at_y_level: {
        params: { min: "number", max: "number" },
        required: [],
        meaning: "the agent's y is within [min, max]",
    },
    position_within: {
        params: { x: "number", z: "number", radius: "number" },
        required: ["x", "z"],
        meaning: "the agent is within `radius` blocks (horizontal, default 3) of x,z",
    },
    status_at_least: {
        params: { field: "string", min: "number" },
        required: ["field", "min"],
        meaning: "`field` (health, food, xp, oxygen) is at least `min`",
    },
    time_of_day_in: {
        params: { min: "number", max: "number" },
        required: ["min", "max"],
        meaning: "time of day (0-24000) is within [min, max]; wraps when min > max",
    },
    is_sheltered: {
        params: {},
        required: [],
        meaning: "a solid block is overhead within 4 blocks and at most one side at feet/head level is open",
    },
    container_contains: {
        params: { pos: "pos", name: "string", count: "number" },
        required: ["pos", "name"],
        meaning: "the container at `pos` {x,y,z} holds at least `count` of `name`",
    },
    block_count_in_region: {
        params: { name: "string", region: "region", count: "number" },
        required: ["name", "region"],
        meaning: "at least `count` blocks `name` inside `region` ({x1,y1,z1,x2,y2,z2} or {center:{x,y,z}, radius})",
    },
};
const COMBINATORS = ["all", "any", "not"];

function isPos(v) {
    return v && typeof v === "object" && ["x", "y", "z"].every((k) => typeof v[k] === "number");
}

function isRegion(v) {
    if (!v || typeof v !== "object") return false;
    if (["x1", "y1", "z1", "x2", "y2", "z2"].every((k) => typeof v[k] === "number")) return true;
    return isPos(v.center) && typeof v.radius === "number";
}

function validate(spec, path = "spec") {
    if (!spec || typeof spec !== "object" || Array.isArray(spec)) return `${path}: must be an object`;
    const keys = Object.keys(spec);
    if (keys.length !== 1) return `${path}: exactly one key expected, got [${keys.join(", ")}]`;
    const key = keys[0];
    const body = spec[key];
    if (key === "not") return validate(body, `${path}.not`);
    if (key === "all" || key === "any") {
        if (!Array.isArray(body) || !body.length) return `${path}.${key}: must be a non-empty array`;
        for (let i = 0; i < body.length; i++) {
            const err = validate(body[i], `${path}.${key}[${i}]`);
            if (err) return err;
        }
        return null;
    }
    const def = PREDICATES[key];
    if (!def) return `${path}: unknown predicate '${key}'`;
    const params = body || {};
    if (typeof params !== "object" || Array.isArray(params)) return `${path}.${key}: params must be an object`;
    for (const p of def.required) if (params[p] === undefined) return `${path}.${key}: missing param '${p}'`;
    for (const [p, v] of Object.entries(params)) {
        const type = def.params[p];
        if (!type) return `${path}.${key}: unknown param '${p}'`;
        if (type === "pos" ? !isPos(v) : type === "region" ? !isRegion(v) : typeof v !== type) {
            return `${path}.${key}: param '${p}' must be ${type}`;
        }
    }
    if (key === "has_equipped" && !SLOT_NAMES.includes(params.slot)) return `${path}.has_equipped: slot must be one of ${SLOT_NAMES.join(", ")}`;
    if (key === "status_at_least" && !STATUS_FIELDS.includes(params.field)) return `${path}.status_at_least: field must be one of ${STATUS_FIELDS.join(", ")}`;
    if (key === "has_tool_tier" && !PICKAXE_TIER.includes(params.material)) return `${path}.has_tool_tier: material must be one of ${PICKAXE_TIER.join(", ")}`;
    return null;
}

function tierIndex(material) {
    if (material === "golden") return 0; // gold harvests like wood
    return PICKAXE_TIER.indexOf(material);
}

const impl = {
    has_item(bot, loop, { name, count = 1 }) {
        const m = matcherFor(name);
        let have = 0;
        for (const [n, c] of Object.entries(inventoryCounts(bot))) if (m(n)) have += c;
        return { ok: have >= count, detail: `has_item ${name}: ${have}/${count}` };
    },
    has_equipped(bot, loop, { name, slot }) {
        const slots = slotsOf(bot);
        const got = slots[slot];
        const ok = got !== null && got !== undefined && matcherFor(name)(got);
        return { ok, detail: `has_equipped ${slot}: ${got || "empty"} (want ${name})` };
    },
    has_tool_tier(bot, loop, { tool, material }) {
        const want = tierIndex(material);
        let best = -1;
        let bestName = null;
        const re = new RegExp(`^(\\w+?)_${tool}$`);
        for (const n of Object.keys(inventoryCounts(bot))) {
            const m = n.match(re);
            if (!m) continue;
            const t = tierIndex(m[1]);
            if (t > best) {
                best = t;
                bestName = n;
            }
        }
        return { ok: best >= want, detail: `has_tool_tier ${tool}: best ${bestName || "none"} (want ${material}+)` };
    },
    near_block(bot, loop, { name, radius = 4 }) {
        const ids = blockIdsMatching(loop.mcData, matcherFor(name));
        if (!ids.length) return { ok: false, detail: `near_block: unknown block '${name}'` };
        let found = [];
        try {
            found = bot.findBlocks({ matching: ids, maxDistance: radius, count: 1 });
        } catch (err) {
            found = [];
        }
        if (!found.length) return { ok: false, detail: `near_block ${name}: none within ${radius}` };
        const d = Math.round(found[0].offset(0.5, 0.5, 0.5).distanceTo(bot.entity.position));
        return { ok: true, detail: `near_block ${name}: ${d} blocks away` };
    },
    block_placed_by_me(bot, loop, { name }) {
        const p = (loop.placedByMe || {})[name];
        if (!p) return { ok: false, detail: `block_placed_by_me ${name}: never placed` };
        const b = bot.blockAt(new Vec3(p.x, p.y, p.z));
        const ok = Boolean(b && b.name === name);
        return { ok, detail: `block_placed_by_me ${name}: at ${p.x},${p.y},${p.z} ${ok ? "still there" : "gone"}` };
    },
    near_entity(bot, loop, { name, radius = 8 }) {
        let e = nearestEntity(bot, name, radius);
        if (!e) {
            // player by username
            for (const other of Object.values(bot.entities || {})) {
                if (other !== bot.entity && other.username === name && other.position && other.position.distanceTo(bot.entity.position) <= radius) e = other;
            }
        }
        if (!e) return { ok: false, detail: `near_entity ${name}: none within ${radius}` };
        return { ok: true, detail: `near_entity ${name}: ${Math.round(e.position.distanceTo(bot.entity.position))} blocks away` };
    },
    in_biome(bot, loop, { name }) {
        let biome = "unknown";
        try {
            const block = bot.blockAt(bot.entity.position);
            if (block && block.biome) {
                const byId = loop.mcData.biomes && loop.mcData.biomes[block.biome.id];
                biome = block.biome.name || (byId && byId.name) || "unknown";
            }
        } catch (err) {
            /* unknown */
        }
        return { ok: biome.includes(name), detail: `in_biome: ${biome} (want ${name})` };
    },
    at_y_level(bot, loop, { min = -64, max = 320 }) {
        const y = Math.floor(bot.entity.position.y);
        return { ok: y >= min && y <= max, detail: `at_y_level: y=${y} (want ${min}..${max})` };
    },
    position_within(bot, loop, { x, z, radius = 3 }) {
        const p = bot.entity.position;
        const d = Math.hypot(p.x - (x + 0.5), p.z - (z + 0.5));
        return { ok: d <= radius, detail: `position_within: ${Math.round(d)} blocks from ${x},${z} (want <= ${radius})` };
    },
    status_at_least(bot, loop, { field, min }) {
        const value = field === "xp" ? (bot.experience ? bot.experience.level : 0) : field === "oxygen" ? bot.oxygenLevel : bot[field];
        return { ok: typeof value === "number" && value >= min, detail: `status_at_least ${field}: ${value} (want >= ${min})` };
    },
    time_of_day_in(bot, loop, { min, max }) {
        const t = bot.time ? bot.time.timeOfDay : 0;
        const ok = min <= max ? t >= min && t <= max : t >= min || t <= max;
        return { ok, detail: `time_of_day_in: ${t} (want ${min}..${max})` };
    },
    is_sheltered(bot) {
        const feet = bot.entity.position.floored();
        let roof = false;
        for (let dy = 2; dy <= 5 && !roof; dy++) {
            const b = bot.blockAt(feet.offset(0, dy, 0));
            if (b && b.boundingBox === "block") roof = true;
        }
        let open = 0;
        for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
            const a = bot.blockAt(feet.offset(dx, 0, dz));
            const b = bot.blockAt(feet.offset(dx, 1, dz));
            if ((a && a.boundingBox !== "block") || (b && b.boundingBox !== "block")) open++;
        }
        return { ok: roof && open <= 1, detail: `is_sheltered: roof=${roof} open sides=${open}` };
    },
    async container_contains(bot, loop, { pos, name, count = 1 }) {
        const key = `${pos.x},${pos.y},${pos.z}`;
        const cache = (loop.containerCache = loop.containerCache || {});
        let items = null;
        const hit = cache[key];
        if (hit && Date.now() - hit.at < CONTAINER_CACHE_MS) items = hit.items;
        if (!items) {
            const block = bot.blockAt(new Vec3(pos.x, pos.y, pos.z));
            if (!block || !/chest|barrel|shulker/.test(block.name)) return { ok: false, detail: `container_contains: no container at ${key} (${block ? block.name : "unloaded"})` };
            if (block.position.distanceTo(bot.entity.position) > 5) return { ok: false, detail: `container_contains: container at ${key} is too far to open` };
            try {
                const c = await bot.openContainer(block);
                items = c.items().map((i) => ({ name: i.name, count: i.count }));
                c.close();
            } catch (err) {
                return { ok: false, detail: `container_contains: open failed: ${err.message}` };
            }
            cache[key] = { at: Date.now(), items };
        }
        const m = matcherFor(name);
        let have = 0;
        for (const i of items) if (m(i.name)) have += i.count;
        return { ok: have >= count, detail: `container_contains ${name} at ${key}: ${have}/${count}` };
    },
    block_count_in_region(bot, loop, { name, region, count = 1 }) {
        let x1, y1, z1, x2, y2, z2;
        if (region.center) {
            const c = region.center;
            const r = region.radius;
            [x1, y1, z1, x2, y2, z2] = [c.x - r, c.y - r, c.z - r, c.x + r, c.y + r, c.z + r];
        } else {
            [x1, y1, z1, x2, y2, z2] = [region.x1, region.y1, region.z1, region.x2, region.y2, region.z2];
        }
        const [ax, bx] = [Math.min(x1, x2), Math.max(x1, x2)];
        const [ay, by] = [Math.min(y1, y2), Math.max(y1, y2)];
        const [az, bz] = [Math.min(z1, z2), Math.max(z1, z2)];
        const volume = (bx - ax + 1) * (by - ay + 1) * (bz - az + 1);
        if (volume > REGION_MAX_VOLUME) return { ok: false, detail: `block_count_in_region: region too large (${volume} blocks)` };
        const m = matcherFor(name);
        let n = 0;
        for (let x = ax; x <= bx; x++)
            for (let y = ay; y <= by; y++)
                for (let z = az; z <= bz; z++) {
                    const b = bot.blockAt(new Vec3(x, y, z));
                    if (b && m(b.name)) n++;
                }
        return { ok: n >= count, detail: `block_count_in_region ${name}: ${n}/${count}` };
    },
};

async function evaluate(bot, loop, spec) {
    const err = validate(spec);
    if (err) return { ok: false, detail: `invalid spec: ${err}` };
    return evalNode(bot, loop, spec);
}

async function evalNode(bot, loop, spec) {
    const key = Object.keys(spec)[0];
    const body = spec[key];
    if (key === "not") {
        const r = await evalNode(bot, loop, body);
        return { ok: !r.ok, detail: `not(${r.detail})` };
    }
    if (key === "all" || key === "any") {
        const results = [];
        for (const s of body) results.push(await evalNode(bot, loop, s));
        const ok = key === "all" ? results.every((r) => r.ok) : results.some((r) => r.ok);
        return { ok, detail: `${key}[${results.map((r) => `${r.ok ? "+" : "-"}${r.detail}`).join("; ")}]` };
    }
    try {
        return await impl[key](bot, loop, body || {});
    } catch (e) {
        return { ok: false, detail: `${key} threw: ${e.message}` };
    }
}

// Item names mentioned anywhere in a spec (for the allowed-block set).
function itemsIn(spec, out = new Set()) {
    if (!spec || typeof spec !== "object") return out;
    for (const [k, v] of Object.entries(spec)) {
        if (k === "all" || k === "any") for (const s of v) itemsIn(s, out);
        else if (k === "not") itemsIn(v, out);
        else if (v && typeof v.name === "string") out.add(v.name);
    }
    return out;
}

module.exports = { PREDICATES, COMBINATORS, validate, evaluate, itemsIn, SLOT_NAMES, STATUS_FIELDS };
