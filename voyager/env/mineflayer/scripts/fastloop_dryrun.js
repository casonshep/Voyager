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
    };
}

// World: grass plains at y=63, oak logs in a clump to the north, stone below,
// a lava pool to the east, a cow north, a zombie far south-west.
const logs = [];
for (let i = 0; i < 6; i++) logs.push(new Vec3(2, 64 + (i % 3), -10 - Math.floor(i / 3)));
const bot = {
    version: "1.19",
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
        if (logs.some((l) => l.equals(pos.floored()))) return fakeBlock("oak_log", pos);
        if (pos.x > 8 && pos.y <= 63) return fakeBlock("lava", pos);
        if (pos.y >= 64) return fakeBlock("air", pos);
        if (pos.y === 63) return fakeBlock("grass_block", pos);
        return fakeBlock("stone", pos);
    },
    findBlocks({ matching, maxDistance, count }) {
        const ids = new Set(matching);
        const out = [];
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
    async equip(item) {
        this.heldItem = item;
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
    pathfinder: { setGoal() {}, goto: async () => {} },
    clearControlStates() {},
};
Object.assign(bot, EventEmitter.prototype);
EventEmitter.call(bot);
bot.entities[0] = bot.entity;

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
    check("menu offers craft:oak_planks", "craft:oak_planks" in menu);
    check("menu offers attack:cow", "attack:cow" in menu);
    check("menu flags lava to the east", /hazards lava/.test(menu["walk:east"] || ""));
    check("menu size is bounded", Object.keys(menu).length <= 24, `${Object.keys(menu).length} options`);

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

    console.log("--- subgoal derivation ---");
    const names = (r) => r.candidates.map((c) => c.text);
    bot.inventory.items = () => [];
    let derived = await loop.deriveSubgoals("Craft a wooden pickaxe");
    console.log("  empty inventory:", names(derived).join(" | "));
    check("empty inventory -> obtain planks and sticks", /Obtain 3 \w+_planks/.test(names(derived).join()) && names(derived).some((t) => /Obtain 2 stick/.test(t)));
    check("high goal itself is offered", names(derived).includes("Craft a wooden pickaxe"));
    bot.inventory.items = () => [{ name: "oak_planks", count: 3 }, { name: "stick", count: 2 }];
    derived = await loop.deriveSubgoals("Craft a wooden pickaxe");
    console.log("  planks+sticks:", names(derived).join(" | "));
    check("ingredients present -> needs a crafting table first", names(derived)[0] === "Obtain 1 crafting_table");
    bot.inventory.items = () => [{ name: "oak_planks", count: 3 }, { name: "stick", count: 2 }, { name: "crafting_table", count: 1 }];
    derived = await loop.deriveSubgoals("Craft a wooden pickaxe");
    check("table in inventory -> place it (nearBlock target)", derived.candidates[0].text === "Place the crafting table" && derived.candidates[0].target.nearBlock === "crafting_table");
    check("...then craft", names(derived).includes("Craft 1 wooden_pickaxe"));
    bot.inventory.items = () => [{ name: "wooden_pickaxe", count: 1 }];
    derived = await loop.deriveSubgoals("Mine 3 iron ore");
    console.log("  iron ore, wooden pickaxe:", names(derived).join(" | "));
    check("iron ore needs a stone pickaxe", names(derived).includes("Obtain 1 stone_pickaxe"));
    check("no iron nearby -> explore with nearBlock target", derived.candidates.some((c) => c.target && c.target.nearBlock === "*_iron_ore"));
    check("raw_iron is mined, not crafted from raw_iron_block", !names(derived).some((t) => /raw_iron_block/.test(t)));
    bot.inventory.items = () => [];
    derived = await loop.deriveSubgoals("Mine 3 wood logs");
    check("logs nearby -> mine them", names(derived).includes("Mine 3 *_log"));

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
    check("high_goal_reached fires at 3 logs", loop.pending.some((t) => t.type === "high_goal_reached"));
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
    bot.heldItem = null;
    bot.entities[2].position = new Vec3(-20, 64, 20);
    delete bot.entities[3];
    const bag = [{ name: "dirt", count: 60 }, { name: "cobblestone", count: 40 }];
    for (let i = 0; i < 29; i++) bag.push({ name: "stick", count: 1 });
    bot.inventory.items = () => bag;
    bot._blocksNearby = { chest: new Vec3(5, 63, 5) };
    snap2 = loop.snapshot();
    menu2 = loop.buildMenu(snap2);
    check("bag nearly full and chest near -> chest:deposit", /Put 44 dirt, 24 cobblestone/.test(menu2["chest:deposit"] || ""), menu2["chest:deposit"]);
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

    console.log("--- one Jev decision ---");
    if (!process.env.TYPESAFE_API_KEY) {
        console.log("TYPESAFE_API_KEY not set; skipping the live decision (heuristic fallback is used in that case)");
        return;
    }
    const action = await loop.decide(snap, menu);
    console.log("decision:", JSON.stringify(loop.lastDecision, null, 2));
    check("Jev returned a menu option", action in menu, action);
    check("stuck and subgoalValid Nouls answered", typeof loop.lastDecision.stuck === "number" && typeof loop.lastDecision.subgoalValid === "number", `stuck=${loop.lastDecision.stuck} valid=${loop.lastDecision.subgoalValid}`);
    check("Jev used the API, not the heuristic", loop.stats.decisions === 1 && loop.stats.heuristic === 0);
})().catch((err) => {
    console.error(err);
    process.exitCode = 1;
});
