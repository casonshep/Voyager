// Dry-run for lib/agentLoop.js without Minecraft: builds a snapshot from the
// canned plains world, prints the movement-only menu, registers a sample
// primitive (with observe()) and, when TYPESAFE_API_KEY is set, runs one real
// Jev decision; otherwise prints the request that would be sent.
//
//   node scripts/agentloop_dryrun.js
const { makeFakeBot } = require("./fakeWorld");
const { AgentLoop } = require("../lib/agentLoop");
const base = require("../lib/baseActions");
const jev = require("../lib/jevTraversal");

let failed = 0;
function check(label, cond, detail) {
    console.log(`${cond ? "PASS" : "FAIL"}: ${label}${detail ? ` -> ${detail}` : ""}`);
    if (!cond) failed++;
}

const SAMPLE_PRIMITIVE = `
// name: mine_cursor
// offered when the cursor is on a block within reach
function menu(snap, loop) {
    if (!snap.cursor.block || snap.cursor.block === "air") return null;
    return \`mine the \${snap.cursor.block} under the cursor, \${snap.cursor.distance} blocks away (held: \${snap.slots.hand || "nothing"})\`;
}
function observe(bot, loop) {
    const b = bot.blockAt(bot.entity.position.offset(0, -1, 0));
    return { block_under_feet: b ? b.name : "unknown" };
}
async function execute(bot, loop, ctx) {
    const block = bot.blockAtCursor(6);
    if (!block) return "failed: nothing under the cursor";
    await bot.dig(block);
    return \`ok mined \${block.name}\`;
}
`;

(async () => {
    const { bot } = makeFakeBot("1.19");
    const loop = new AgentLoop(bot);

    console.log("--- subtask ---");
    const bad = await loop.setSubtask({ id: "x", description: "bad", verify: { near_structure: { name: "village" } } });
    check("invalid spec rejected", !bad.ok, bad.error);
    const res = await loop.setSubtask({
        id: "n1",
        description: "walk to x=20, z=0",
        verify: { position_within: { x: 20, z: 0, radius: 3 } },
        allowed_blocks: ["*_log", "stone"],
        budget_actions: 30,
        kind: "locate",
    });
    check("subtask accepted", res.ok);
    await loop.stop(); // the dryrun drives decisions by hand
    loop.subtask = { id: "n1", description: "walk to x=20, z=0", verify: { position_within: { x: 20, z: 0, radius: 3 } }, kind: "locate", budget_actions: 30 };

    console.log("--- snapshot ---");
    const snap = loop.snapshot();
    console.log(JSON.stringify(snap, null, 2));
    const top = ["bot", "cursor", "inventory", "slots", "blocks", "entities", "dropped", "placed_by_me", "subtask", "recent", "since_progress", "extra"];
    check("snapshot has every contract field", top.every((k) => k in snap), Object.keys(snap).join(","));
    check("bot facing north", snap.bot.yaw === "north");
    check("*_log family collapsed to one entry", "*_log" in snap.blocks && !("oak_log" in snap.blocks), JSON.stringify(snap.blocks["*_log"]));
    check("stone visible (allowed)", "stone" in snap.blocks);
    check("lava visible (hazard)", "lava" in snap.blocks);
    check("water visible (hazard)", "water" in snap.blocks);
    check("grass_block hidden (not allowed)", !("grass_block" in snap.blocks));
    check("cow in entities, item entity excluded", snap.entities.some((e) => e.name === "cow") && !snap.entities.some((e) => e.name === "item"));
    check("dropped stick listed", snap.dropped.some((d) => d.name === "stick"));
    check("no progress for a position subtask", snap.subtask.progress === null);

    console.log("--- menu ---");
    const menu = loop.buildMenu(snap);
    console.log(JSON.stringify(menu, null, 2));
    const ids = Object.keys(menu);
    check("movement-only menu", ids.every((id) => base.isBase(id)), ids.join(", "));
    check("four walk directions", base.DIRECTIONS.every((d) => `move:${d}` in menu));
    check("look:at per visible block", ids.includes("look:at:*_log") && ids.includes("look:at:stone"));
    check("menu lines carry live facts", /ground: grass_block/.test(menu["move:north"]) && /lava ahead/.test(menu["move:east"]), `${menu["move:north"]} | ${menu["move:east"]}`);
    check("west warns about water", /water/.test(menu["move:west"]), menu["move:west"]);

    console.log("--- base actions execute on the fake bot ---");
    const look = await base.execute(bot, loop, "look:at:*_log");
    check("look:at turns toward the log clump", /^ok looking at oak_log/.test(look), look);
    const snap2 = loop.snapshot();
    check("cursor or facing changed after look", snap2.bot.yaw !== "south", `yaw ${snap2.bot.yaw} pitch ${snap2.bot.pitch} cursor ${snap2.cursor.block}`);
    const faceEast = await base.execute(bot, loop, "look:east");
    check("look:east", faceEast === "ok facing east" && loop.snapshot().bot.yaw === "east");
    const walk = await base.execute(bot, loop, "move:north");
    check("walk on a static fake bot reports failure (no movement)", /^failed: could not move north/.test(walk), walk);
    check("controls released", !bot._controls.forward && !bot._controls.jump);

    console.log("--- registry ---");
    const reg = loop.registry.register("mine_cursor", SAMPLE_PRIMITIVE, snap);
    check("sample primitive registers", reg.ok, reg.error);
    await bot.look(Math.PI, -0.9); // look down at the grass
    const snap3 = loop.snapshot();
    check("observe() merged into snap.extra", snap3.extra.mine_cursor && snap3.extra.mine_cursor.block_under_feet === "grass_block", JSON.stringify(snap3.extra));
    const menu3 = loop.buildMenu(snap3);
    check("x:mine_cursor offered with untested note", /untested/.test(menu3["x:mine_cursor"] || ""), menu3["x:mine_cursor"]);
    check("untested trial excluded from proven menu", !loop.provenMenuIds(menu3).includes("x:mine_cursor"));
    check("build cooldown while trial untested", loop.registry.hasUntestedTrial());
    const out = await loop.execute("x:mine_cursor", snap3);
    check("primitive executes", /^ok mined/.test(out), out);
    // scoring is grounded in effect: an "ok" with no inventory/position/placement change is a failure
    loop.registry.recordOutcome("mine_cursor", false, out + " [NO EFFECT]");
    check("ok without effect does not lift the trial", loop.registry.hasUntestedTrial());
    check("outcome text kept for the builder", (loop.registry.summary()[0].last_outcomes || []).some((o) => /NO EFFECT/.test(o)));
    loop.registry.recordOutcome("mine_cursor", true, out);
    check("trial lifted after first ok with effect", !loop.registry.hasUntestedTrial());

    console.log("--- purpose gating ---");
    // an action learned for "have 2 apples" is offered while the bag holds 1, hidden once it holds 2
    loop.registry.register("pg_probe", "const PURPOSE = { description: 'get a second apple', achieves: { has_item: { name: 'apple', count: 2 } } };\nfunction menu(snap, loop) { return 'probe action'; }\nasync function execute(bot, loop, ctx) { return 'ok'; }", null, "obtain 2 apple");
    await loop.refreshPurposes();
    check("purpose unmet -> offered", "x:pg_probe" in loop.buildMenu(loop.snapshot()));
    check("menu line shows the declared purpose", /purpose: get a second apple/.test(loop.buildMenu(loop.snapshot())["x:pg_probe"] || ""));
    loop.registry.register("pg_probe2", "const PURPOSE = { description: 'get an apple', achieves: { has_item: { name: 'apple', count: 1 } } };\nfunction menu(snap, loop) { return 'probe action'; }\nasync function execute(bot, loop, ctx) { return 'ok'; }", null, "obtain 1 apple");
    await loop.refreshPurposes();
    check("purpose already met -> hidden", !("x:pg_probe2" in loop.buildMenu(loop.snapshot())));
    loop.registry.remove("pg_probe");
    loop.registry.remove("pg_probe2");

    const badPurpose = loop.registry.register("pg_bad", "const PURPOSE = { description: 'x', achieves: { has_itm: {} } };\nfunction menu(snap, loop) { return null; }\nasync function execute(bot, loop, ctx) { return 'ok'; }", null, null);
    check("invalid PURPOSE.achieves is a compile error", !badPurpose.ok && /PURPOSE.achieves/.test(badPurpose.error || ""), badPurpose.error);

    const { purposeItems } = require("../lib/agentLoop");
    check("purpose items: has_item names drive scoring", JSON.stringify(purposeItems({ any: [{ has_item: { name: "crafting_table", count: 1 } }, { near_block: { name: "crafting_table", radius: 4 } }] })) === '["crafting_table"]');
    check("purpose items: place-only purpose falls back to effect scoring", purposeItems({ near_block: { name: "crafting_table", radius: 4 } }).length === 0);

    console.log("--- status shape ---");
    const status = loop.status();
    check("status has contract fields", ["snap", "triggers", "recent", "primitives", "menu", "last_decision", "subtask", "stats"].every((k) => k in status), Object.keys(status).join(","));
    check("primitive summary has trial flag", status.primitives[0] && status.primitives[0].trial === false);

    console.log("--- jev decision ---");
    if (process.env.TYPESAFE_API_KEY) {
        const d = await loop.decide(snap, menu);
        console.log(JSON.stringify(d, null, 2));
        check("jev chose a menu action", d.action in menu && d.source === "jev", `${d.action} (${d.source})`);
        check("completable answered", typeof d.completable === "number");
    } else {
        console.log("TYPESAFE_API_KEY not set; the request would carry:");
        console.log(`  state keys: ${Object.keys({ ...snap, menu, proven_menu: [] }).join(", ")}`);
        console.log(`  questions: action (Choice over ${ids.length} menu ids), danger (Noul), progress (Noul), completable (Noul)`);
        const d = await loop.decide(snap, menu);
        check("fallback picks a movement action", d.action.startsWith("move:") && d.source === "fallback", d.action);
    }

    console.log(failed ? `\n${failed} check(s) FAILED` : "\nall checks passed");
    process.exit(failed ? 1 : 0);
})().catch((e) => {
    console.log("dryrun crashed:", e && e.stack ? e.stack : e);
    process.exit(1);
});
