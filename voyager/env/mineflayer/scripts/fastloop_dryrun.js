// Dry-run for lib/fastLoop.js without Minecraft: compiles the control
// primitives, parses goal targets, builds a decision menu from a canned
// world, and (when TYPESAFE_API_KEY is set) runs one real Jev decision.
//
// Usage: node scripts/fastloop_dryrun.js
const { Vec3 } = require("vec3");
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
    findBlock() {
        return null;
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
    setControlState() {},
};
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

    console.log("--- one Jev decision ---");
    if (!process.env.TYPESAFE_API_KEY) {
        console.log("TYPESAFE_API_KEY not set; skipping the live decision (heuristic fallback is used in that case)");
        return;
    }
    const action = await loop.decide(snap, menu);
    console.log("decision:", JSON.stringify(loop.lastDecision, null, 2));
    check("Jev returned a menu option", action in menu, action);
    check("Jev used the API, not the heuristic", loop.stats.decisions === 1 && loop.stats.heuristic === 0);
})().catch((err) => {
    console.error(err);
    process.exitCode = 1;
});
