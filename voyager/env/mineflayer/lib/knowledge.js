// GET /knowledge payload (PLAN.md section 7): recipes keyed by result item
// name, harvest tools per block, and the hand-written tables from
// mcKnowledge.js. Built from minecraft-data for one version and cached, so it
// works before a bot has spawned.

const K = require("./mcKnowledge");

const cache = new Map();

function idOf(cell) {
    if (cell === null || cell === undefined) return null;
    if (typeof cell === "number") return cell < 0 ? null : cell;
    if (typeof cell === "object" && typeof cell.id === "number") return cell.id < 0 ? null : cell.id;
    return null;
}

function recipeEntry(mcData, recipe) {
    const inputs = {};
    let shape = "2x2";
    const add = (id) => {
        if (id === null) return;
        const item = mcData.items[id];
        if (!item) return;
        inputs[item.name] = (inputs[item.name] || 0) + 1;
    };
    if (Array.isArray(recipe.inShape)) {
        const rows = recipe.inShape.length;
        const cols = Math.max(0, ...recipe.inShape.map((r) => (Array.isArray(r) ? r.length : 0)));
        if (rows > 2 || cols > 2) shape = "3x3";
        for (const row of recipe.inShape) for (const cell of row || []) add(idOf(cell));
    } else if (Array.isArray(recipe.ingredients)) {
        if (recipe.ingredients.length > 4) shape = "3x3";
        for (const cell of recipe.ingredients) add(idOf(cell));
    }
    if (!Object.keys(inputs).length) return null;
    return { inputs, count: recipe.result ? recipe.result.count || 1 : 1, shape };
}

function build(version) {
    const v = version || process.env.MC_VERSION || "1.19";
    if (cache.has(v)) return cache.get(v);
    const mcData = require("minecraft-data")(v);
    if (!mcData) throw new Error(`minecraft-data has no data for version ${v}`);

    const recipes = {};
    for (const [resultId, list] of Object.entries(mcData.recipes || {})) {
        const item = mcData.items[Number(resultId)];
        if (!item || !Array.isArray(list)) continue;
        const seen = new Set();
        const out = [];
        for (const r of list) {
            const e = recipeEntry(mcData, r);
            if (!e) continue;
            const key = JSON.stringify([e.inputs, e.count, e.shape]);
            if (seen.has(key)) continue;
            seen.add(key);
            out.push(e);
        }
        if (out.length) recipes[item.name] = out;
    }

    const tierOf = (name) => {
        const m = name.match(/^(\w+?)_(pickaxe|axe|shovel|hoe|sword)$/);
        const t = m ? K.PICKAXE_TIER.indexOf(m[1]) : -1;
        return t < 0 ? (m && m[1] === "golden" ? 0.5 : 99) : t;
    };
    const harvest_tools = {};
    for (const block of mcData.blocksArray || Object.values(mcData.blocksByName)) {
        if (!block.harvestTools) continue;
        const names = Object.keys(block.harvestTools)
            .map((id) => mcData.items[Number(id)])
            .filter(Boolean)
            .map((i) => i.name)
            .sort((a, b) => tierOf(a) - tierOf(b));
        if (names.length) harvest_tools[block.name] = names;
    }

    const payload = {
        version: mcData.version.minecraftVersion,
        recipes,
        harvest_tools,
        ore_drops: K.ORE_DROPS,
        smeltable: K.SMELTABLE,
        fuels: K.FUELS,
        mob_drops: K.MOB_DROPS,
        families: K.FAMILIES,
        natural_block_re: K.NATURAL_BLOCK_RE.source,
    };
    cache.set(v, payload);
    return payload;
}

module.exports = { build };
