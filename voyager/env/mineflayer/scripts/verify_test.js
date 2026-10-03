// Offline checks for lib/verify.js: every predicate against the canned bot.
//
//   node scripts/verify_test.js
const { Vec3 } = require("vec3");
const { makeFakeBot } = require("./fakeWorld");
const verify = require("../lib/verify");

const { bot, mcData } = makeFakeBot("1.19");
const loop = { bot, mcData, placedByMe: {} };

let failed = 0;
function check(label, cond, detail) {
    console.log(`${cond ? "PASS" : "FAIL"}: ${label}${detail ? ` -> ${detail}` : ""}`);
    if (!cond) failed++;
}
async function expect(label, spec, want) {
    const r = await verify.evaluate(bot, loop, spec);
    check(label, r.ok === want, r.detail);
}

(async () => {
    console.log("--- validate ---");
    check("valid has_item", verify.validate({ has_item: { name: "oak_log", count: 2 } }) === null);
    check("unknown predicate rejected", /unknown predicate/.test(verify.validate({ near_structure: { name: "village" } }) || ""));
    check("missing param rejected", /missing param/.test(verify.validate({ near_block: {} }) || ""));
    check("bad type rejected", /must be number/.test(verify.validate({ has_item: { name: "oak_log", count: "2" } }) || ""));
    check("unknown param rejected", /unknown param/.test(verify.validate({ has_item: { name: "oak_log", radius: 2 } }) || ""));
    check("bad slot rejected", /slot must be/.test(verify.validate({ has_equipped: { name: "x", slot: "hat" } }) || ""));
    check("two keys rejected", /exactly one key/.test(verify.validate({ has_item: { name: "a" }, near_block: { name: "b" } }) || ""));
    check("empty all rejected", /non-empty/.test(verify.validate({ all: [] }) || ""));
    check("nested valid", verify.validate({ all: [{ not: { has_item: { name: "dirt" } } }, { any: [{ at_y_level: { min: 60 } }] }] }) === null);
    check("every predicate in PREDICATES has a meaning", Object.values(verify.PREDICATES).every((p) => typeof p.meaning === "string" && p.meaning));
    check("15 predicates listed", Object.keys(verify.PREDICATES).length === 14, `${Object.keys(verify.PREDICATES).length} predicates + ${verify.COMBINATORS.length} combinators`);

    console.log("--- has_item ---");
    await expect("has 2 oak_log", { has_item: { name: "oak_log", count: 2 } }, true);
    await expect("not 3 oak_log", { has_item: { name: "oak_log", count: 3 } }, false);
    await expect("family *_log counts oak", { has_item: { name: "*_log", count: 2 } }, true);
    await expect("default count 1", { has_item: { name: "apple" } }, true);

    console.log("--- has_equipped / has_tool_tier ---");
    await expect("nothing in hand", { has_equipped: { name: "wooden_pickaxe", slot: "hand" } }, false);
    bot.heldItem = { name: "wooden_pickaxe", count: 1 };
    bot._items.push({ name: "wooden_pickaxe", count: 1 });
    await expect("wooden pickaxe in hand", { has_equipped: { name: "wooden_pickaxe", slot: "hand" } }, true);
    bot.inventory.slots[6] = { name: "leather_chestplate" };
    await expect("chestplate in chest slot", { has_equipped: { name: "leather_chestplate", slot: "chest" } }, true);
    await expect("wooden tier ok for wooden", { has_tool_tier: { tool: "pickaxe", material: "wooden" } }, true);
    await expect("wooden not enough for stone", { has_tool_tier: { tool: "pickaxe", material: "stone" } }, false);
    bot._items.push({ name: "iron_pickaxe", count: 1 });
    await expect("iron beats stone", { has_tool_tier: { tool: "pickaxe", material: "stone" } }, true);
    await expect("no axe at all", { has_tool_tier: { tool: "axe", material: "wooden" } }, false);

    console.log("--- near_block / block_placed_by_me ---");
    await expect("oak_log within 16", { near_block: { name: "oak_log", radius: 16 } }, true);
    await expect("oak_log not within 4", { near_block: { name: "oak_log", radius: 4 } }, false);
    await expect("family *_log within 16", { near_block: { name: "*_log", radius: 16 } }, true);
    await expect("unknown block", { near_block: { name: "not_a_block" } }, false);
    await expect("no crafting table placed", { block_placed_by_me: { name: "crafting_table" } }, false);
    bot._blocksNearby.crafting_table = new Vec3(1, 64, 1);
    loop.placedByMe.crafting_table = { x: 1, y: 64, z: 1 };
    await expect("placed table still there", { block_placed_by_me: { name: "crafting_table" } }, true);
    delete bot._blocksNearby.crafting_table;
    await expect("placed table gone", { block_placed_by_me: { name: "crafting_table" } }, false);

    console.log("--- near_entity / in_biome / at_y_level / position_within ---");
    await expect("cow within 16", { near_entity: { name: "cow", radius: 16 } }, true);
    await expect("cow not within 8 (default)", { near_entity: { name: "cow" } }, false);
    await expect("zombie far", { near_entity: { name: "zombie", radius: 10 } }, false);
    await expect("in plains", { in_biome: { name: "plains" } }, true);
    await expect("not in desert", { in_biome: { name: "desert" } }, false);
    await expect("y in 60..70", { at_y_level: { min: 60, max: 70 } }, true);
    await expect("y not below 20", { at_y_level: { max: 20 } }, false);
    await expect("position within 3 of 0,0", { position_within: { x: 0, z: 0 } }, true);
    await expect("position not within 3 of 20,0", { position_within: { x: 20, z: 0, radius: 3 } }, false);
    await expect("position within 25 of 20,0", { position_within: { x: 20, z: 0, radius: 25 } }, true);

    console.log("--- status / time / sheltered ---");
    await expect("health >= 20", { status_at_least: { field: "health", min: 20 } }, true);
    await expect("food >= 18 false", { status_at_least: { field: "food", min: 18 } }, false);
    await expect("xp >= 0", { status_at_least: { field: "xp", min: 0 } }, true);
    await expect("time 2000 in day", { time_of_day_in: { min: 0, max: 12000 } }, true);
    await expect("time 2000 not night", { time_of_day_in: { min: 13000, max: 23000 } }, false);
    await expect("wrapping range 23000..3000 includes 2000", { time_of_day_in: { min: 23000, max: 3000 } }, true);
    await expect("open plains not sheltered", { is_sheltered: {} }, false);
    for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1]]) {
        bot._blocksNearby[`stone_${dx}_${dz}`] = null; // placeholder keys ignored (no def)
    }
    bot._blocksNearby = { stone: new Vec3(0, 66, 0), cobblestone: new Vec3(1, 64, 0), dirt: new Vec3(-1, 64, 0), gravel: new Vec3(0, 64, 1), oak_planks: new Vec3(1, 65, 0), spruce_planks: new Vec3(-1, 65, 0), birch_planks: new Vec3(0, 65, 1) };
    await expect("roof and three walls = sheltered", { is_sheltered: {} }, true);
    bot._blocksNearby = {};

    console.log("--- container_contains / block_count_in_region ---");
    bot._blocksNearby.chest = new Vec3(2, 64, 0);
    bot._containers[new Vec3(2, 64, 0).toString()] = [{ name: "cobblestone", count: 5 }];
    await expect("chest has 5 cobblestone", { container_contains: { pos: { x: 2, y: 64, z: 0 }, name: "cobblestone", count: 5 } }, true);
    await expect("chest lacks 6 cobblestone (cached)", { container_contains: { pos: { x: 2, y: 64, z: 0 }, name: "cobblestone", count: 6 } }, false);
    await expect("no container at 5,64,5", { container_contains: { pos: { x: 5, y: 64, z: 5 }, name: "cobblestone" } }, false);
    await expect("6 oak logs in the clump", { block_count_in_region: { name: "oak_log", region: { x1: 0, y1: 63, z1: -12, x2: 4, y2: 67, z2: -9 }, count: 6 } }, true);
    await expect("not 7 oak logs", { block_count_in_region: { name: "oak_log", region: { center: { x: 2, y: 65, z: -10 }, radius: 3 }, count: 7 } }, false);
    await expect("region too large fails", { block_count_in_region: { name: "stone", region: { x1: -50, y1: 0, z1: -50, x2: 50, y2: 100, z2: 50 } } }, false);

    console.log("--- combinators ---");
    await expect("all true", { all: [{ has_item: { name: "oak_log", count: 2 } }, { in_biome: { name: "plains" } }] }, true);
    await expect("all with one false", { all: [{ has_item: { name: "oak_log", count: 2 } }, { in_biome: { name: "desert" } }] }, false);
    await expect("any with one true", { any: [{ in_biome: { name: "desert" } }, { has_item: { name: "apple" } }] }, true);
    await expect("not", { not: { in_biome: { name: "desert" } } }, true);
    await expect("invalid spec evaluates false", { bogus: {} }, false);
    check("itemsIn collects names", [...verify.itemsIn({ all: [{ has_item: { name: "cobblestone" } }, { near_block: { name: "crafting_table" } }] })].sort().join(",") === "cobblestone,crafting_table");

    console.log(failed ? `\n${failed} check(s) FAILED` : "\nall checks passed");
    process.exit(failed ? 1 : 0);
})();
