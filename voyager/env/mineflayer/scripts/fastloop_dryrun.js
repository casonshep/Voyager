// Dry-run for lib/fastLoop.js without Minecraft: compiles the control
// primitives, parses goal targets, builds a decision menu from a canned
// world, and (when TYPESAFE_API_KEY is set) runs one real Jev decision.
//
// Usage: node scripts/fastloop_dryrun.js
const { Vec3 } = require("vec3");
const { EventEmitter } = require("events");
const { FastLoop, findTargetCandidates, countMatching, matcherFor } = require("../lib/fastLoop");

const mcData = require("minecraft-data")("1.19");

function fakeBlock(name, pos) {
    const def = mcData.blocksByName[name];
    return {
        name,
        type: def ? def.id : 0,
        position: pos,
        biome: { name: "plains" },
        boundingBox: name === "air" || name === "cave_air" || name === "lava" || name === "water" ? "empty" : "block",
    };
}

// World: grass plains at y=63, oak logs in a clump to the north, stone below,
// a lava pool to the east, a cow north, a zombie far south-west.
const logs = [];
for (let i = 0; i < 6; i++) logs.push(new Vec3(2, 64 + (i % 3), -10 - Math.floor(i / 3)));
const bot = {
    version: "1.19",
    registry: mcData,
    entity: { position: new Vec3(0.5, 64, 0.5), isInLava: false, isInWater: false },
    health: 20,
    food: 17,
    oxygenLevel: 20,
    time: { timeOfDay: 2000 },
    inventory: {
        items: () => [
            { name: "oak_log", count: 2 },
            { name: "oak_planks", count: 4 },
            { name: "apple", count: 1 },
        ],
    },
    entities: {
        1: { name: "cow", position: new Vec3(3, 64, -14) },
        2: { name: "zombie", position: new Vec3(-20, 64, 20) },
    },
    blockAt(pos) {
        for (const [name, p] of Object.entries(this._blocksNearby || {})) {
            if (p.floored().equals(pos.floored())) return fakeBlock(name, pos);
        }
        if (logs.some((l) => l.equals(pos.floored()))) return fakeBlock("oak_log", pos);
        if (pos.x > 8 && pos.y <= 63) return fakeBlock("lava", pos);
        if (pos.y >= 64) return fakeBlock("air", pos);
        if (pos.y === 63) return fakeBlock("grass_block", pos);
        return fakeBlock("stone", pos);
    },
    findBlocks({ matching, maxDistance, count }) {
        const ids = new Set(Array.isArray(matching) ? matching : [matching]);
        const out = [];
        for (const [name, p] of Object.entries(this._blocksNearby)) {
            const def = mcData.blocksByName[name];
            if (def && ids.has(def.id) && p.distanceTo(this.entity.position) <= maxDistance) out.push(p);
        }
        const here = this.entity.position;
        for (let x = -maxDistance; x <= maxDistance; x += 2) {
            for (let z = -maxDistance; z <= maxDistance; z += 2) {
                for (let y = 60; y <= 66; y++) {
                    const p = new Vec3(Math.floor(here.x) + x, y, Math.floor(here.z) + z);
                    const b = this.blockAt(p);
                    if (ids.has(b.type) && p.distanceTo(here) <= maxDistance) out.push(p);
                    if (out.length >= count) return out;
                }
            }
        }
        return out;
    },
    _blocksNearby: {}, // name -> Vec3, set by tests to simulate findBlock hits
    _dug: [],
    canDigBlock(block) { return block && block.name !== "bedrock"; },
    async dig(block) { this._dug.push(block.position.floored()); },
    tool: { async equipForBlock() {} },
    findBlock({ matching }) {
        for (const [name, p] of Object.entries(this._blocksNearby)) {
            const def = mcData.blocksByName[name];
            if (def && (matching === def.id || (Array.isArray(matching) && matching.includes(def.id)))) {
                return { name, position: p, type: def.id };
            }
        }
        return null;
    },
    inventoryUsed() {
        return this.inventory.items().length;
    },
    heldItem: null,
    _placed: [],
    _controls: {},
    _equipped: [],
    async equip(item, dest) {
        this._equipped.push([item.name, dest || "hand"]);
        if (!dest || dest === "hand") this.heldItem = item;
    },
    _tossed: [],
    async toss(id, meta, count) {
        this._tossed.push([mcData.items[id].name, count]);
    },
    async lookAt() {},
    setControlState(name, value) {
        this._controls[name] = value;
    },
    async _placeBlockWithOptions(ref, face) {
        this._placed.push(ref.position.plus(face));
    },
    recipesFor(id, meta, min, table) {
        // satisfiable now: planks from logs, sticks from planks, crafting table
        const ok = new Set([
            mcData.itemsByName.oak_planks.id,
            mcData.itemsByName.stick.id,
            mcData.itemsByName.crafting_table.id,
        ]);
        return ok.has(id) ? [{ id }] : [];
    },
    pathfinder: { setGoal() {}, goto: async () => {}, setMovements(m) { this.movements = m; } },
    world: {},
    _furnace: { slots: [null, null, null], puts: [], takes: 0,
        inputItem() { return this.slots[0]; }, fuelItem() { return this.slots[1]; }, outputItem() { return this.slots[2]; },
        async putInput(id, meta, n) { this.puts.push(["input", mcData.items[id].name, n]); this.slots[0] = { name: mcData.items[id].name, count: n }; },
        async putFuel(id, meta, n) { this.puts.push(["fuel", mcData.items[id].name, n]); this.slots[1] = { name: mcData.items[id].name, count: n }; },
        async takeOutput() { this.takes++; this.slots[2] = null; }, close() {} },
    async openFurnace() { return this._furnace; },
    clearControlStates() {},
};
Object.assign(bot, EventEmitter.prototype);
EventEmitter.call(bot);
bot.entities[0] = bot.entity;

function withTimeoutP(p, ms) {
    return Promise.race([p, new Promise((r) => setTimeout(() => r("timed out"), ms))]);
}

function check(label, ok, detail) {
    console.log(`${ok ? "PASS" : "FAIL"}: ${label}${detail ? ` -> ${detail}` : ""}`);
    if (!ok) process.exitCode = 1;
}

(async () => {
    console.log("--- goal target parsing ---");
    const cases = [
        ["Mine 1 wood log", "*_log", 1],
        ["Mine 3 wood logs", "*_log", 3],
        ["Craft 4 oak planks", "oak_planks", 4],
        ["Craft a wooden pickaxe", "wooden_pickaxe", 1],
        ["Craft 1 crafting table", "crafting_table", 1],
        ["Mine 5 cobblestone", "cobblestone", 5],
        ["Mine 3 iron ore", "raw_iron", 3],
        ["Kill 1 cow", null, 1],
        ["Smelt 1 raw iron", "iron_ingot", 1],
        ["Cook 2 beef", "cooked_beef", 2],
        ["Place a crafting table", null, 1],
        ["Equip the stone sword", null, 1],
        ["Deposit useless items into the chest at x=1, y=2, z=3", null, 1],
        ["Mine 1 stone", "cobblestone", 1],
    ];
    const loop = new FastLoop(bot);
    for (const [text, expectKey, expectNeed] of cases) {
        const cands = findTargetCandidates(text, mcData);
        if (cands.length > 1) {
            console.log(`  (${text}: ${cands.length} candidates ${cands.map((c) => c.key).join(", ")})`);
        }
        const target = await loop.parseTarget(text);
        const key = target ? target.key : null;
        check(
            `"${text}"`,
            key === expectKey && (!target || target.need === expectNeed),
            `${key} x${target ? target.need : "-"}`
        );
    }
    check("class matcher counts oak+spruce logs", countMatching({ oak_log: 2, spruce_log: 1, stick: 4 }, matcherFor("*_log")) === 3);

    console.log("--- primitives compiled ---");
    const primNames = Object.keys(loop.prims).sort();
    check("mineBlock/craftItem/smeltItem/placeItem/killMob present",
        ["mineBlock", "craftItem", "smeltItem", "placeItem", "killMob"].every((n) => typeof loop.prims[n] === "function"),
        primNames.join(", "));

    console.log("--- menu from a snapshot ---");
    loop.goal = { id: "g1", text: "Mine 3 wood logs", context: "", kind: "task", noProgressSeconds: 75, hint: null };
    loop.target = await loop.parseTarget(loop.goal.text);
    const snap = loop.snapshot();
    const menu = loop.buildMenu(snap);
    console.log(JSON.stringify(menu, null, 2));
    check("menu offers mine:oak_log", "mine:oak_log" in menu);
    bot._blocksNearby = { chest: new Vec3(2, 64, 0), crafting_table: new Vec3(-2, 64, 0) };
    const menuL = loop.buildMenu(loop.snapshot());
    check("never offers to mine its own chest or crafting table", !Object.keys(menuL).some((k) => /^mine:(chest|crafting_table|furnace)$/.test(k)));
    bot._blocksNearby = { iron_ore: new Vec3(3, 63, 3), coal_ore: new Vec3(-3, 63, 3) };
    const menuNoPick = loop.buildMenu(loop.snapshot());
    check("no pickaxe -> neither ore is offered to mine", !("mine:iron_ore" in menuNoPick) && !("mine:coal_ore" in menuNoPick));
    bot.inventory.items = () => [{ name: "wooden_pickaxe", count: 1 }];
    const menuWood = loop.buildMenu(loop.snapshot());
    check("wooden pickaxe -> coal yes, iron no", ("mine:coal_ore" in menuWood) && !("mine:iron_ore" in menuWood));
    bot.inventory.items = () => [{ name: "stone_pickaxe", count: 1 }];
    check("stone pickaxe -> iron offered", "mine:iron_ore" in loop.buildMenu(loop.snapshot()));
    bot.inventory.items = () => [{ name: "oak_log", count: 2 }, { name: "oak_planks", count: 4 }, { name: "apple", count: 1 }];
    bot._blocksNearby = {};
    bot.inventory.items = () => [{ name: "wooden_pickaxe", count: 1 }];
    const learned = await loop.deriveSubgoals("Mine 3 iron ore", undefined, [{ item: "stone_pickaxe", count: 1 }, { item: "not_an_item", count: 1 }]);
    check("learned requirement is planned as an extra target", learned.targets.some((t) => t.item === "stone_pickaxe") && !learned.targets.some((t) => t.item === "not_an_item"));
    bot.inventory.items = () => [{ name: "oak_log", count: 2 }, { name: "oak_planks", count: 4 }, { name: "apple", count: 1 }];
    check("menu offers craft:oak_planks", "craft:oak_planks" in menu);
    check("menu offers attack:cow", "attack:cow" in menu);
    check("menu flags lava to the east", /hazards lava/.test(menu["walk:east"] || ""));
    check("menu size is bounded", Object.keys(menu).length <= 24, `${Object.keys(menu).length} options`);
    check("no wait on the surface menu", !("wait" in menu));

    console.log("--- triggers ---");
    loop.checkTriggers(snap);
    check("no goal_reached yet", !loop.goalReached);
    bot.inventory.items = () => [{ name: "oak_log", count: 5 }];
    loop.checkTriggers(loop.snapshot());
    check("goal_reached after +3 logs", loop.goalReached && loop.pending.some((t) => t.type === "goal_reached"));
    bot.inventory.items = () => [
        { name: "oak_log", count: 2 },
        { name: "oak_planks", count: 4 },
        { name: "apple", count: 1 },
    ];

    console.log("--- heuristic policy ---");
    loop.goalReached = false;
    check("heuristic mines the target", loop.heuristic(snap, menu) === "mine:oak_log");

    console.log("--- subgoal derivation (planner) ---");
    const names = (r) => r.candidates.map((c) => c.text);
    const show = (label, r) => console.log(`  ${label}: ${names(r).join(" | ")}`);
    bot.inventory.items = () => [];
    let derived = await loop.deriveSubgoals("Craft a wooden pickaxe");
    show("pickaxe, empty inventory", derived);
    check("empty inventory -> the only ready leaf is mining logs", names(derived).length === 1 && /^Mine \d+ oak_log$/.test(names(derived)[0]));
    check("plan aggregates planks for pickaxe + sticks + table", derived.plan.oak_planks && derived.plan.oak_planks.need >= 9 && derived.plan.oak_planks.status === "blocked");
    check("high goal itself is NOT offered when steps were derived", !names(derived).includes("Craft a wooden pickaxe"));
    const kill = await loop.deriveSubgoals("Kill 1 cow");
    check("undecomposable goal -> offered directly", names(kill).includes("Kill 1 cow"));
    bot.inventory.items = () => [{ name: "oak_planks", count: 3 }, { name: "stick", count: 2 }];
    derived = await loop.deriveSubgoals("Craft a wooden pickaxe");
    show("pickaxe, planks+sticks, no table", derived);
    check("needs 4 more planks for the table -> mine 1 log", /^Mine 1 oak_log$/.test(names(derived)[0]));
    bot.inventory.items = () => [{ name: "oak_planks", count: 7 }, { name: "stick", count: 2 }];
    derived = await loop.deriveSubgoals("Craft a wooden pickaxe");
    show("pickaxe, 7 planks+sticks, no table", derived);
    check("enough planks -> craft the table first", names(derived)[0] === "Craft 1 crafting_table");
    bot.inventory.items = () => [{ name: "oak_planks", count: 3 }, { name: "stick", count: 2 }, { name: "crafting_table", count: 1 }];
    derived = await loop.deriveSubgoals("Craft a wooden pickaxe");
    check("table in inventory -> place it (nearBlock target)", derived.candidates[0].text === "Place the crafting table" && derived.candidates[0].target.nearBlock === "crafting_table");
    bot._blocksNearby = { crafting_table: new Vec3(2, 64, 2) };
    derived = await loop.deriveSubgoals("Craft a wooden pickaxe");
    check("table placed and ingredients held -> craft the pickaxe", names(derived)[0] === "Craft 1 wooden_pickaxe" && derived.plan.wooden_pickaxe.status === "ready");
    bot._blocksNearby = {};
    bot.inventory.items = () => [{ name: "wooden_pickaxe", count: 1 }];
    derived = await loop.deriveSubgoals("Mine 3 iron ore");
    show("iron ore, wooden pickaxe", derived);
    check("iron needs a stone pickaxe: cobblestone is a ready leaf", names(derived).some((t) => /^Mine 3 cobblestone$/.test(t)));
    check("no iron nearby -> explore with nearBlock target", derived.candidates.some((c) => c.target && c.target.nearBlock === "*_iron_ore"));
    check("raw_iron is mined, not crafted from raw_iron_block", !names(derived).some((t) => /raw_iron_block/.test(t)));
    check("the stone pickaxe requirement is in the plan", derived.plan["tool:stone_pickaxe"] && derived.plan.stone_pickaxe.need === 1);
    bot.inventory.items = () => [];
    derived = await loop.deriveSubgoals("Mine 3 wood logs");
    check("logs nearby -> mine them", /^Mine 3 \*_log$/.test(names(derived)[0]));
    bot.inventory.items = () => [{ name: "raw_iron", count: 2 }];
    derived = await loop.deriveSubgoals("Smelt 5 raw iron into iron ingots");
    show("smelt, 2 raw iron, nothing else", derived);
    check("smelt plan: furnace, fuel and raw iron are requirements", derived.plan["station:furnace"] && derived.plan.fuel && derived.plan.raw_iron.need === 5 && derived.plan.raw_iron.have === 2);
    check("smelt leaves: explore for coal and mine logs for tools", derived.candidates.some((c) => c.target && c.target.nearBlock === "*_coal_ore") && names(derived).some((t) => /oak_log/.test(t)));
    check("no block/nugget recipes for ingots", !names(derived).some((t) => /block|nugget/.test(t)));
    bot.inventory.items = () => [{ name: "raw_iron", count: 5 }, { name: "coal", count: 2 }];
    bot._blocksNearby = { furnace: new Vec3(3, 63, 3) };
    derived = await loop.deriveSubgoals("Smelt 5 raw iron into iron ingots");
    show("smelt with everything at hand", derived);
    check("requirements under a satisfied node are moot", derived.plan["tool:stone_pickaxe"].status === "moot" && names(derived).length === 1);
    check("smelt with everything at hand -> Smelt 5 raw_iron", names(derived)[0] === "Smelt 5 raw_iron" && derived.candidates[0].target.item === "iron_ingot");
    bot._blocksNearby = {};
    bot.inventory.items = () => [{ name: "iron_ingot", count: 10 }, { name: "stone_pickaxe", count: 1 }];
    derived = await loop.deriveSubgoals("Craft a set of iron armor");
    show("iron armor, 10 ingots, stone pickaxe", derived);
    check("set expands to four pieces", derived.targets.length === 4 && derived.targets.every((t) => /^iron_(helmet|chestplate|leggings|boots)$/.test(t.item)));
    check("iron aggregates to 24 ingots (5+8+7+4), 14 still missing", derived.plan.iron_ingot.need === 24 && derived.plan.iron_ingot.remaining === 14);
    check("14 raw iron to mine, stone pickaxe already satisfies the tool gate", derived.plan.raw_iron.need === 14 && derived.plan["tool:stone_pickaxe"].status === "satisfied");
    check("set target reports pieces", derived.target && derived.target.pieces && derived.target.pieces.length === 4);
    bot.inventory.items = () => [];
    derived = await loop.deriveSubgoals("Craft a set of stone tools (stone pickaxe, stone axe, stone shovel)");
    check("tool set expands to three items", derived.targets.length === 3);
    bot.inventory.items = () => [];

    console.log("--- plural parsing, place spot, staircase ---");
    bot.inventory.items = () => [{ name: "coal", count: 3 }, { name: "stick", count: 3 }];
    let torches = await loop.deriveSubgoals("Craft 12 torches");
    console.log("   torches, 3 coal + 3 sticks:", names(torches).join(" | "));
    check("a moot branch adds no demand (sticks need 3, not 5)", torches.plan.stick.need === 3 && torches.plan.stick.status === "satisfied");
    check("'torches' parses to torch and plans from the recipe", torches.targets.length === 1 && torches.targets[0].item === "torch" && names(torches).some((t) => /^Craft 12 torch$/.test(t)));
    check("'Craft 2 buckets' -> bucket", (await loop.deriveSubgoals("Craft 2 buckets")).targets[0].item === "bucket");
    bot.inventory.items = () => [];
    // on the surface at (0.5, 64, 0.5): the diagonal is air with grass below -> a spot exists
    const spotSurface = loop.findPlaceSpot();
    check("surface: a place spot next to the bot with a solid neighbour", spotSurface && spotSurface.y === 64 && bot.blockAt(spotSurface).name === "air");
    // underground in a 1x1 shaft: everything around is stone -> no spot, dig a pocket
    const shaft = new Vec3(0, 40, 0);
    const prevBlockAt = bot.blockAt;
    bot.blockAt = (pos) => {
        const f = pos.floored();
        if (bot._dug.some((d) => d.equals(f))) return { ...fakeBlock("air", pos), boundingBox: "empty" };
        if (f.x === 0 && f.z === 0 && (f.y === 40 || f.y === 41)) return { ...fakeBlock("air", pos), boundingBox: "empty" };
        if (f.y < 63) return { ...fakeBlock("stone", pos), boundingBox: "block" };
        if (f.y === 63) return fakeBlock("grass_block", pos); // no lava rim in the shaft test
        return fakeBlock("air", pos);
    };
    bot.entity.position = shaft.offset(0.5, 0, 0.5);
    check("shaft: no free spot around", loop.findPlaceSpot() === null);
    const pocket = await loop.digPocket();
    check("shaft: a pocket is dug at feet level and becomes the spot", pocket && pocket.y === 40 && bot._dug.length === 1 && loop.findPlaceSpot() && loop.findPlaceSpot().equals(pocket));
    check("underground fingerprint and solid overhead counted", loop.fingerprint().biome === "underground" && loop.solidOverhead(shaft) >= 20 && !loop.skyAbove(shaft));
    bot.inventory.items = () => [{ name: "stone_pickaxe", count: 1 }];
    const menuU = loop.buildMenu(loop.snapshot());
    check("underground with a pickaxe -> surface:up offered, pillar not (rock overhead)", ("surface:up" in menuU) && !("pillar:up" in menuU));
    check("wait is never on the menu", !("wait" in loop.buildMenu(loop.snapshot())));
    check("the heuristic never waits either", loop.heuristic(loop.snapshot(), loop.buildMenu(loop.snapshot())) !== "wait");
    bot.inventory.items = () => [];
    check("underground without a pickaxe -> no surface:up", !("surface:up" in loop.buildMenu(loop.snapshot())));
    bot.inventory.items = () => [{ name: "stone_pickaxe", count: 1 }];
    loop.target = await loop.parseTarget("Obtain 2 stick", { item: "stick", count: 2 });
    check("heuristic underground wanting wood -> surface:up", loop.heuristic(loop.snapshot(), menuU) === "surface:up");
    const planU = await loop.deriveSubgoals("Craft a wooden pickaxe");
    check("planner underground: logs are a 'return to the surface' step", names(planU).some((t) => /^Return to the surface to find .*log/.test(t)));
    // staircase: pathfinder stub cannot move, so the manual fallback runs; it digs headroom and the next tread each step
    loop.active = true;
    let moved = 0;
    const origGoto = bot.pathfinder.goto;
    bot.pathfinder.goto = async () => { moved++; bot.entity.position = bot.entity.position.offset(1, 1, 0); };
    const stair = await withTimeoutP(loop.surfaceUp(() => false), 20000);
    loop.active = false;
    bot.pathfinder.goto = origGoto;
    check("staircase digs upward until sky is visible", /^ok, reached the surface/.test(stair) && moved >= 20 && bot._dug.length > 40, `${stair}, ${moved} steps, ${bot._dug.length} blocks dug`);
    bot.blockAt = prevBlockAt;
    bot._dug = [];
    bot.entity.position = new Vec3(0.5, 64, 0.5);
    bot.inventory.items = () => [];

    console.log("--- milestone targets: families, dimension, multi-target, lookahead ---");
    bot.inventory.items = () => [{ name: "apple", count: 3 }, { name: "bread", count: 6 }, { name: "wooden_pickaxe", count: 1 }];
    const food = await loop.parseTarget("", { item: "family:food", count: 8 });
    check("family:food counts apples and bread together", loop.summarise(food).have === 9);
    const picks = await loop.parseTarget("", { item: "family:pickaxe", count: 2 });
    check("family:pickaxe counts one pickaxe", loop.summarise(picks).have === 1 && loop.summarise(picks).need === 2);
    bot.game = { dimension: "minecraft:overworld" };
    const dim = await loop.parseTarget("", { dimension: "the_nether" });
    check("dimension target unmet in the overworld", loop.summarise(dim).have === 0);
    bot.game.dimension = "the_nether";
    check("dimension target met in the nether", loop.summarise(dim).have === 1);
    bot.game.dimension = "minecraft:overworld";
    const sat = await loop.checkSatisfied([{ item: "family:food", count: 8 }, { item: "torch", count: 16 }, { dimension: "the_nether" }, { nearBlock: "furnace" }]);
    check("milestone check: food yes, torches no, nether no, furnace no", JSON.stringify(sat) === "[true,false,false,false]", JSON.stringify(sat));
    check("pathfinder movements: no towers, no scaffolding, digging allowed", loop.movements && loop.movements.allow1by1towers === false && loop.movements.scafoldingBlocks.length === 0 && loop.movements.canDig === true);
    // multi-target high goal: all pieces must hold
    loop.active = true; loop.run = async () => {};
    bot.inventory.items = () => [{ name: "stone_pickaxe", count: 1 }];
    await loop.setGoal({ id: "m1", text: "Obtain 1 stone_axe", target: { item: "stone_axe", count: 1 }, highGoal: { id: "ms-stone", text: "Stone tools", targets: [{ item: "stone_pickaxe", count: 1 }, { item: "stone_axe", count: 1 }, { item: "stone_sword", count: 1 }] } });
    check("multi-target high goal parsed into three targets", loop.highTargets.length === 3);
    bot.inventory.items = () => [{ name: "stone_pickaxe", count: 1 }, { name: "stone_axe", count: 1 }];
    loop.pending = []; loop.checkTriggers(loop.snapshot());
    check("two of three held -> high goal not reached", !loop.pending.some((t) => t.type === "high_goal_reached"));
    bot.inventory.items = () => [{ name: "stone_pickaxe", count: 1 }, { name: "stone_axe", count: 1 }, { name: "stone_sword", count: 1 }];
    loop.pending = []; loop.checkTriggers(loop.snapshot());
    check("all three held -> high_goal_reached", loop.pending.some((t) => t.type === "high_goal_reached"));
    loop.active = false;
    // lookahead: iron for the pickaxe and the armor is one aggregated requirement
    bot.inventory.items = () => [{ name: "stone_pickaxe", count: 1 }, { name: "stick", count: 4 }];
    const ahead = await loop.deriveSubgoals("Iron tools", { targets: [{ item: "iron_pickaxe", count: 1 }] }, undefined, [{ item: "iron_chestplate", count: 1 }, { item: "iron_boots", count: 1 }]);
    console.log("   lookahead plan:", names(ahead).join(" | "));
    check("iron aggregates across current goal and lookahead (3 + 8 + 4 = 15)", ahead.plan.iron_ingot && ahead.plan.iron_ingot.need === 15);
    check("lookahead reported separately", ahead.lookahead.length === 2 && ahead.targets.length === 1);
    // the furnace milestone follows stone tools: a block target as lookahead must not crash the planner
    let blockAhead = null;
    try {
        blockAhead = await loop.deriveSubgoals("Stone tools", { targets: [{ item: "stone_sword", count: 1 }] }, undefined, [{ nearBlock: "furnace" }, { item: "coal", count: 16 }]);
    } catch (err) {
        blockAhead = { error: err.message };
    }
    check("nearBlock lookahead target plans as a station to build", blockAhead && !blockAhead.error && blockAhead.plan["station:furnace"] && blockAhead.plan["station:furnace"].status !== "satisfied", blockAhead && blockAhead.error);
    check("shared requirements are explained by the current goal", /iron_pickaxe/.test(ahead.candidates[1].why) && !/next milestone/.test(ahead.candidates[1].why));
    check("current-goal leaves come before lookahead-only leaves", !/upcoming milestone/.test(ahead.candidates[0].why));
    bot.inventory.items = () => [];
    bot._blocksNearby = {};

    console.log("--- inventory policy: keep table, discard, withdraw, space, chunking, home, auto-armor ---");
    bot._blocksNearby = {};
    bot.inventory.slots = [];
    const bigBag = [{ name: "cobblestone", count: 505 }, { name: "andesite", count: 124 }, { name: "clay_ball", count: 104 }, { name: "iron_pickaxe", count: 1 }, { name: "apple", count: 3 }, { name: "raw_iron", count: 3 }, { name: "torch", count: 20 }, { name: "spruce_log", count: 4 }];
    for (let i = 0; i < 25; i++) bigBag.push({ name: "stick", count: 1 }); // 33 stacks used, 3 free
    bot.inventory.items = () => bigBag;
    const junk = loop.junkToDeposit(loop.inventoryCounts());
    check("keep table: cobble excess over 32, all andesite and clay; tools, ore, food, torches, logs kept",
        junk.cobblestone === 473 && junk.andesite === 124 && junk.clay_ball === 104 && !("iron_pickaxe" in junk) && !("apple" in junk) && !("raw_iron" in junk) && !("torch" in junk) && !("spruce_log" in junk), JSON.stringify(junk));
    let menuInv = loop.buildMenu(loop.snapshot());
    check("31 slots used, no chest -> discard:junk offered", /Throw away/.test(menuInv["discard:junk"] || ""));
    check("no chest near -> no chest:deposit", !("chest:deposit" in menuInv));
    const disc = await loop.execute("discard:junk");
    check("discard tosses the junk kinds", /^ok, tossed 3 kind/.test(disc) && bot._tossed.some(([n, c]) => n === "cobblestone" && c === 473), disc);
    bot._tossed = [];
    bot._blocksNearby = { chest: new Vec3(3, 64, 3) };
    menuInv = loop.buildMenu(loop.snapshot());
    check("chest near -> deposit offered, discard not", ("chest:deposit" in menuInv) && !("discard:junk" in menuInv));
    // free-slot target and the planner's space requirement
    const free = await loop.parseTarget("", { freeSlots: 8 });
    check("freeSlots target counts empty slots", loop.summarise(free).have === 36 - bigBag.length && loop.summarise(free).need === 8);
    bot._blocksNearby = {};
    const plan = await loop.deriveSubgoals("Mine 36 raw_iron", { targets: [{ item: "raw_iron", count: 36 }] });
    check("planner hangs a space requirement under mining when the bag is nearly full", plan.plan.space && plan.plan.space.status === "ready" && names(plan).some((t) => /^Free up 4 inventory slots$/.test(t)), names(plan).join(" | "));
    bot.inventory.items = () => [{ name: "stone_pickaxe", count: 1 }];
    const chunked = await loop.deriveSubgoals("Mine 36 raw_iron", { targets: [{ item: "raw_iron", count: 36 }] });
    bot._blocksNearby = { iron_ore: new Vec3(4, 63, 0) };
    const chunked2 = await loop.deriveSubgoals("Mine 36 raw_iron", { targets: [{ item: "raw_iron", count: 36 }] });
    check("mining subgoals are chunked to 8 while the plan keeps the full 36", names(chunked2).includes("Mine 8 raw_iron") && chunked2.plan.raw_iron.need === 36, names(chunked2).join(" | "));
    bot._blocksNearby = {};
    // withdraw from a known chest instead of mining
    bot.obsList = [{ name: "nearbyChests", chestsItems: { "(6, 64, 2)": { iron_ingot: 12, cobblestone: 64 }, "(40, 64, 40)": "Unknown" } }];
    const fromChest = await loop.deriveSubgoals("Craft an iron pickaxe", { targets: [{ item: "iron_pickaxe", count: 1 }] });
    check("planner takes ingots from the known chest instead of smelting", names(fromChest).some((t) => /^Take 3 iron_ingot from the chest$/.test(t)), names(fromChest).join(" | "));
    loop.goal = { id: "w", text: "Obtain 3 iron_ingot", context: "", kind: "task", noProgressSeconds: 30, noProgressActions: 5 };
    loop.target = await loop.parseTarget("", { item: "iron_ingot", count: 3 });
    const menuChest = loop.buildMenu(loop.snapshot());
    check("menu offers withdraw:iron_ingot from the known chest", /Take 12 iron_ingot from the chest/.test(menuChest["withdraw:iron_ingot"] || ""));
    bot.obsList = [];
    // food family plans as hunting animals
    bot.inventory.items = () => [];
    const foodPlan = await loop.deriveSubgoals("Basic kit", { targets: [{ item: "family:food", count: 8 }] });
    check("food family -> hunt animals candidate", names(foodPlan).some((t) => /^Hunt animals \(cow, pig, sheep or chicken\) for 8 food$/.test(t)), names(foodPlan).join(" | "));
    // home base
    loop.setHome({ x: 100, y: 64, z: 0 });
    const menuHome = loop.buildMenu(loop.snapshot());
    check("home set and far -> return:home offered with distance", /home base 100 blocks away/.test(menuHome["return:home"] || ""));
    loop.setHome(null);
    check("home cleared -> no return:home", !("return:home" in loop.buildMenu(loop.snapshot())));
    // armor is worn by code, not offered to Jev
    bot.inventory.items = () => [{ name: "iron_helmet", count: 1 }, { name: "iron_boots", count: 1 }];
    bot.inventory.slots = [];
    bot._equipped = [];
    await loop.autoEquipArmor();
    check("auto-equip wears helmet and boots into their slots", JSON.stringify(bot._equipped.sort()) === JSON.stringify([["iron_boots", "feet"], ["iron_helmet", "head"]]), JSON.stringify(bot._equipped));
    check("armor is not a Jev equip option any more", !Object.keys(loop.buildMenu(loop.snapshot())).some((k) => /equip:iron_(helmet|boots)/.test(k)));
    bot.inventory.items = () => [];
    bot.inventory.slots = [];

    console.log("--- home base: placeable block targets decompose to obtain-then-place ---");
    bot._blocksNearby = {};
    loop.landmarks = {};
    bot.inventory.items = () => [{ name: "oak_planks", count: 2 }];
    let homePlan = await loop.deriveSubgoals("Home base", { targets: [{ nearBlock: "crafting_table" }, { nearBlock: "furnace" }, { nearBlock: "chest" }] });
    console.log("   home, 2 planks:", names(homePlan).join(" | "));
    check("no station is 'explored for'", !names(homePlan).some((t) => /^Explore to find (chest|furnace|crafting_table)/.test(t)));
    check("chest and table are obtained through planks -> logs", homePlan.plan.chest && homePlan.plan.chest.kind === "craft" && homePlan.plan.oak_planks && homePlan.plan.oak_planks.need >= 12 && names(homePlan).some((t) => /oak_log/.test(t)));
    check("furnace needs cobblestone -> mine stone with a pickaxe", homePlan.plan.furnace && homePlan.plan.furnace.kind === "craft" && homePlan.plan.cobblestone);
    bot.inventory.items = () => [{ name: "chest", count: 1 }, { name: "furnace", count: 1 }, { name: "crafting_table", count: 1 }];
    homePlan = await loop.deriveSubgoals("Home base", { targets: [{ nearBlock: "crafting_table" }, { nearBlock: "furnace" }, { nearBlock: "chest" }] });
    check("all three held -> place them", ["Place the crafting table", "Place the furnace", "Place the chest"].every((t) => names(homePlan).includes(t)), names(homePlan).join(" | "));
    bot.inventory.items = () => [{ name: "chest", count: 1 }];
    loop.landmarks = { crafting_table: { x: 60, y: 64, z: 0 } };
    homePlan = await loop.deriveSubgoals("Home base", { targets: [{ nearBlock: "crafting_table" }, { nearBlock: "chest" }] });
    check("table placed far away -> walk back to it instead of crafting another", names(homePlan).includes("Walk back to the crafting table"), names(homePlan).join(" | "));
    loop.landmarks = { crafting_table: { x: 30, y: 64, z: 0 } }; // beyond the 24-block forget radius, within the 32-block craft rule
    const menuStation = loop.buildMenu(loop.snapshot());
    check("a station already carried or standing nearby is not offered to craft", !("craft:chest" in menuStation) && !("craft:crafting_table" in menuStation));
    const stationJunk = loop.junkToDeposit({ crafting_table: 20, furnace: 4, chest: 3, iron_pickaxe: 1 });
    check("stations are capped: 19 tables, 3 furnaces, 1 chest are junk", stationJunk.crafting_table === 19 && stationJunk.furnace === 3 && stationJunk.chest === 1 && !("iron_pickaxe" in stationJunk), JSON.stringify(stationJunk));
    loop.landmarks = {};
    bot.inventory.items = () => [];

    console.log("--- batch smelting ---");
    bot._blocksNearby = { furnace: new Vec3(2, 64, 0) };
    bot.inventory.items = () => [{ name: "raw_iron", count: 13 }, { name: "coal", count: 5 }];
    loop.goal = { id: "sm", text: "Smelt 13 raw_iron", context: "", kind: "task", noProgressSeconds: 120, noProgressActions: 12 };
    loop.target = await loop.parseTarget("", { item: "iron_ingot", count: 13 });
    const menuSmelt = loop.buildMenu(loop.snapshot());
    check("smelt option loads the whole batch", /Load the furnace with 13 raw_iron/.test(menuSmelt["smelt:raw_iron"] || ""), menuSmelt["smelt:raw_iron"]);
    loop.pending = [];
    const smelted = await loop.execute("smelt:raw_iron");
    check("batch load puts 13 inputs and 2 coal, then leaves", /^ok, 13 raw_iron cooking \(130s\)/.test(smelted) && JSON.stringify(bot._furnace.puts) === JSON.stringify([["fuel", "coal", 2], ["input", "raw_iron", 13]]), `${smelted} ${JSON.stringify(bot._furnace.puts)}`);
    check("a smelt_queued trigger tells the brain to park the subgoal", loop.pending.some((t) => t.type === "smelt_queued" && t.detail.count === 13 && t.detail.readyInSeconds === 130));
    check("the job is tracked", loop.furnaceJobs.length === 1 && loop.pendingOutput((x) => x === "iron_ingot") === 13);
    const menuCook = loop.buildMenu(loop.snapshot());
    check("furnace:collect is offered while cooking", /Collect 13 iron_ingot from the furnace .*ready in about 1[23]\ds/.test(menuCook["furnace:collect"] || ""), menuCook["furnace:collect"]);
    loop.pending = []; loop.lastProgressAt = 0;
    loop.checkTriggers(loop.snapshot());
    check("cooking counts as progress: no stall while the batch cooks", !loop.pending.some((t) => t.type === "no_progress"));
    const cookPlan = await loop.deriveSubgoals("Iron tools", { targets: [{ item: "iron_pickaxe", count: 1 }] });
    check("planner offers collecting the batch instead of loading more", names(cookPlan).some((t) => /^Collect 3 iron_ingot from the furnace$/.test(t)) && !names(cookPlan).some((t) => /^Smelt/.test(t)), names(cookPlan).join(" | "));
    loop.furnaceJobs[0].readyAt = Date.now() - 1000;
    bot._furnace.slots = [null, null, { name: "iron_ingot", count: 13 }];
    const collected = await loop.execute("furnace:collect");
    check("collect takes the finished output and clears the job", /^ok, collected 13 iron_ingot/.test(collected) && loop.furnaceJobs.length === 0, collected);
    bot._blocksNearby = {};
    bot.inventory.items = () => [];
    loop.target = null;

    console.log("--- nearBlock and high goal targets ---");
    const near = await loop.parseTarget("Explore to find lava", { nearBlock: "lava" });
    check("lava is within scan -> nearBlock reached", loop.summarise(near).gained === 1);
    const far = await loop.parseTarget("Explore to find iron ore", { nearBlock: "iron_ore" });
    check("iron ore is not -> nearBlock pending", loop.summarise(far).gained === 0);
    loop.active = true; // setGoal must not start the run loop in this dry run
    loop.run = async () => {};
    await loop.setGoal({ id: "s1", text: "Obtain 2 oak_log", target: { item: "oak_log", count: 2 }, highGoal: { id: "h1", text: "Mine 3 wood logs" } });
    check("high goal parsed once", loop.highGoal && loop.highGoal.id === "h1" && loop.highTarget.key === "*_log");
    bot.inventory.items = () => [{ name: "oak_log", count: 2 }];
    loop.pending = [];
    loop.checkTriggers(loop.snapshot());
    check("subgoal reached, high goal not yet", loop.pending.some((t) => t.type === "goal_reached") && !loop.pending.some((t) => t.type === "high_goal_reached"));
    await loop.setGoal({ id: "s2", text: "Obtain 1 oak_log", target: { item: "oak_log", count: 1 }, highGoal: { id: "h1", text: "Mine 3 wood logs" } });
    check("baseline kept across subgoals", loop.highTarget.startHave === 0);
    bot.inventory.items = () => [{ name: "oak_log", count: 3 }];
    loop.pending = [];
    loop.checkTriggers(loop.snapshot());
    check("high_goal_reached fires at 3 logs held", loop.pending.some((t) => t.type === "high_goal_reached"));
    bot.inventory.items = () => [{ name: "oak_log", count: 7 }];
    await loop.setGoal({ id: "s3", text: "Obtain 1 oak_log", target: { item: "oak_log", count: 1 }, highGoal: { id: "h2", text: "Mine 3 wood logs" } });
    loop.pending = [];
    loop.checkTriggers(loop.snapshot());
    check("high goal counts what is held, not what was gained", loop.pending.some((t) => t.type === "high_goal_reached"));
    loop.active = false;
    bot.inventory.items = () => [{ name: "oak_log", count: 2 }, { name: "oak_planks", count: 4 }, { name: "apple", count: 1 }];
    loop.goal = { id: "g1", text: "Mine 3 wood logs", context: "", kind: "task", noProgressSeconds: 75, noProgressActions: 5, hint: null };
    loop.target = await loop.parseTarget(loop.goal.text);
    loop.goalReached = false;

    console.log("--- collect / pillar / equip / chest / return options ---");
    bot.entities[3] = { name: "item", position: new Vec3(4, 64, 2) };
    bot.inventory.items = () => [{ name: "dirt", count: 12 }, { name: "stone_sword", count: 1 }];
    bot.entities[2].position = new Vec3(-6, 64, 6); // zombie now 8 blocks away
    let snap2 = loop.snapshot();
    let menu2 = loop.buildMenu(snap2);
    check("dropped item -> collect:items", /nearest 4 blocks/.test(menu2["collect:items"] || ""));
    check("dirt in inventory and air above -> pillar:up", /tower of dirt/.test(menu2["pillar:up"] || ""));
    check("hostile near and sword not held -> equip:stone_sword", "equip:stone_sword" in menu2);
    bot.heldItem = { name: "stone_sword" };
    check("sword already held -> no equip option", !("equip:stone_sword" in loop.buildMenu(loop.snapshot())));
    bot.inventory.items = () => [{ name: "iron_helmet", count: 1 }, { name: "leather_boots", count: 1 }];
    bot.inventory.slots = []; bot.inventory.slots[8] = { name: "iron_boots" };
    const upgrades = loop.armorUpgrades(loop.inventoryCounts());
    check("carried helmet with empty head slot is an upgrade", upgrades.iron_helmet === null);
    check("leather boots do not replace worn iron boots", !("leather_boots" in upgrades));
    bot.inventory.slots[5] = { name: "leather_helmet" };
    check("iron helmet beats worn leather helmet", loop.armorUpgrades(loop.inventoryCounts()).iron_helmet === "leather_helmet");
    bot.inventory.slots = [];
    bot.heldItem = null;
    bot.entities[2].position = new Vec3(-20, 64, 20);
    delete bot.entities[3];
    const bag = [{ name: "dirt", count: 60 }, { name: "cobblestone", count: 40 }];
    for (let i = 0; i < 29; i++) bag.push({ name: "stick", count: 1 });
    bot.inventory.items = () => bag;
    bot._blocksNearby = { chest: new Vec3(5, 63, 5) };
    snap2 = loop.snapshot();
    menu2 = loop.buildMenu(snap2);
    check("bag nearly full and chest near -> chest:deposit of all junk and the cobble excess", /Put 60 dirt, 8 cobblestone/.test(menu2["chest:deposit"] || ""), menu2["chest:deposit"]);
    check("chest remembered as a landmark", loop.landmarks.chest && loop.landmarks.chest.x === 5);
    check("landmark next door -> no return option", !("return:chest" in menu2));
    bot._blocksNearby = {};
    bot.entity.position = new Vec3(40.5, 64, 40.5);
    menu2 = loop.buildMenu(loop.snapshot());
    check("chest out of sight and far -> return:chest", /Walk back to the last chest/.test(menu2["return:chest"] || ""), menu2["return:chest"]);
    bot.entity.position = new Vec3(0.5, 64, 0.5);
    loop.snapshot();
    check("standing where the chest was, none seen -> landmark forgotten", !loop.landmarks.chest);
    bot.inventory.items = () => [{ name: "oak_log", count: 2 }, { name: "oak_planks", count: 4 }, { name: "apple", count: 1 }];

    console.log("--- pillar timing (simulated physics) ---");
    bot.inventory.items = () => [{ name: "dirt", count: 3 }];
    bot._placed = [];
    bot.entity.position = new Vec3(0.5, 64, 0.5);
    bot.entity.onGround = true;
    let airborneTicks = 0;
    const sim = setInterval(() => {
        // crude jump arc: rise 0.3/tick to +1.2 then fall back to the new ground
        if (bot._controls.jump || airborneTicks) {
            airborneTicks++;
            const base = 64 + bot._placed.length;
            const y = airborneTicks <= 4 ? base + airborneTicks * 0.3 : Math.max(base, base + 1.2 - (airborneTicks - 4) * 0.4);
            bot.entity.position = new Vec3(0.5, y, 0.5);
            bot.entity.onGround = y <= base + 0.001 && airborneTicks > 4;
            if (bot.entity.onGround) airborneTicks = 0;
        }
        bot.emit("physicsTick");
    }, 20);
    const solidBelow = (pos) => bot._placed.some((p) => p.equals(pos.floored())) || pos.y <= 63;
    const origBlockAt = bot.blockAt.bind(bot);
    bot.blockAt = (pos) => {
        if (pos.y >= 64 && solidBelow(pos)) return { ...fakeBlock("dirt", pos), boundingBox: "block" };
        const b = origBlockAt(pos);
        return { ...b, boundingBox: b.name === "air" ? "empty" : "block" };
    };
    const okWait = await loop.waitFor(() => true, 500);
    const badWait = await loop.waitFor(() => false, 100);
    check("waitFor resolves on tick / times out", okWait === true && badWait === false);
    loop.active = true; // pillarUp stops when the loop is inactive
    const pillarResult = await loop.pillarUp(3, () => false);
    loop.active = false;
    clearInterval(sim);
    bot.blockAt = origBlockAt;
    check("pillar placed 3 blocks under the feet", /^ok, pillared 3/.test(pillarResult) && bot._placed.length === 3, pillarResult);
    check("blocks went at y=64,65,66", bot._placed.map((p) => p.y).join(",") === "64,65,66", bot._placed.map((p) => p.y).join(","));
    check("jump released after pillaring", bot._controls.jump === false);
    bot.entity.position = new Vec3(0.5, 64, 0.5);
    bot.inventory.items = () => [{ name: "oak_log", count: 2 }, { name: "oak_planks", count: 4 }, { name: "apple", count: 1 }];

    console.log("--- server pause detection ---");
    check("no world age known -> never paused", loop.checkServerPaused() === false && !loop.serverPaused);
    bot.time.age = 1000;
    loop.checkServerPaused();
    loop._lastAgeChangeAt = Date.now() - 3000; // the clock has not moved for 3 s
    loop.pending = [];
    check("frozen world clock -> paused", loop.checkServerPaused() === true && loop.pending.some((t) => t.type === "server_paused"));
    check("status reports it", loop.status({ drain: false }).serverPaused === true);
    loop.lastProgressAt = 0;
    bot.time.age = 1020;
    check("clock moves -> resumed and no-progress clock reset", loop.checkServerPaused() === false && Date.now() - loop.lastProgressAt < 1000 && loop.pending.some((t) => t.type === "server_resumed"));
    delete bot.time.age;

    console.log("--- one Jev decision ---");
    if (!process.env.TYPESAFE_API_KEY) {
        console.log("TYPESAFE_API_KEY not set; skipping the live decision (heuristic fallback is used in that case)");
        return;
    }
    const action = await loop.decide(snap, menu);
    console.log("decision:", JSON.stringify(loop.lastDecision, null, 2));
    check("Jev returned a menu option", action in menu, action);
    const st = loop.status();
    check("decision log is reported and drained through status", st.decisions.length === 1 && st.decisions[0].action === action && typeof st.decisions[0].ms === "number" && loop.status().decisions.length === 0);
    check("stuck and subgoalValid Nouls answered", typeof loop.lastDecision.stuck === "number" && typeof loop.lastDecision.subgoalValid === "number", `stuck=${loop.lastDecision.stuck} valid=${loop.lastDecision.subgoalValid}`);
    check("Jev used the API, not the heuristic", loop.stats.decisions === 1 && loop.stats.heuristic === 0);
})().catch((err) => {
    console.error(err);
    process.exitCode = 1;
});
