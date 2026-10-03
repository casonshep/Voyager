// Bot process: one mineflayer bot behind a small HTTP server (PLAN.md section 7).
//
//   POST /start          spawn the bot (body: port, username, waitTicks, reset, position, spread)
//   POST /reset          in-process cleanup between goals
//   GET  /knowledge      recipes, harvest tools and knowledge tables (works before /start)
//   POST /agent/subtask  set the subtask the Jev loop works on
//   GET  /agent/status   snapshot, drained triggers, recent actions, primitives, menu
//   POST /agent/verify   evaluate a VerifySpec now
//   POST /agent/primitive register a generated action (or remove one)
//   POST /agent/stop     idle the loop
//   POST /stop, /pause
//
//   node index.js [port] [--fake-world]     FAKE_WORLD=1 also works: no Minecraft,
//                                           the canned plains bot from scripts/fakeWorld.js
const express = require("express");
const bodyParser = require("body-parser");
const mineflayer = require("mineflayer");

const { cleanupBot } = require("./lib/botCleanup");
const observeLib = require("./lib/observe");
const obs = require("./lib/observation/base");
const OnChat = require("./lib/observation/onChat");
const OnError = require("./lib/observation/onError");
const { Voxels, BlockRecords } = require("./lib/observation/voxels");
const Status = require("./lib/observation/status");
const Inventory = require("./lib/observation/inventory");
const OnSave = require("./lib/observation/onSave");
const Chests = require("./lib/observation/chests");
const agentLoop = require("./lib/agentLoop");
const knowledge = require("./lib/knowledge");

const args = process.argv.slice(2);
const FAKE_WORLD = process.env.FAKE_WORLD === "1" || args.includes("--fake-world");
const PORT = Number(args.find((a) => /^\d+$/.test(a))) || 3000;

let bot = null;
let lastDisconnect = null; // why the bot's connection ended, until the next /start

const app = express();
app.use(bodyParser.json({ limit: "50mb" }));
app.use(bodyParser.urlencoded({ limit: "50mb", extended: false }));

// A throw inside a packet handler or a timer callback would otherwise end the
// process (the signed-chat crash did exactly that). Log it and keep the bot.
process.on("uncaughtException", (err) => {
    console.log("uncaughtException (bot kept alive):", err && err.stack ? err.stack : err);
});
process.on("unhandledRejection", (err) => {
    console.log("unhandledRejection (ignored):", err && err.stack ? err.stack : err);
});

function errorText(err) {
    return String(err && err.message ? err.message : err);
}

function requireLoop(res) {
    if (!bot || !bot.entity || !bot.agentLoop) {
        res.status(400).json({ error: "Bot not spawned" });
        return null;
    }
    return bot.agentLoop;
}

if (FAKE_WORLD) {
    const { makeFakeBot } = require("./scripts/fakeWorld");
    bot = makeFakeBot(process.env.MC_VERSION || "1.19").bot;
    agentLoop.inject(bot);
    bot.observe = () => ({ fake: true });
    console.log("FAKE_WORLD: serving the canned plains bot; /start is a no-op");
}

app.post("/start", (req, res) => {
    if (FAKE_WORLD) {
        res.json({ fake: true, position: bot.entity.position });
        return;
    }
    lastDisconnect = null;
    if (bot) onDisconnect("Restarting bot");
    bot = null;
    console.log(req.body);
    bot = mineflayer.createBot({
        host: "localhost",
        port: req.body.port,
        username: req.body.username || "bot",
        disableChatSigning: true,
        checkTimeoutInterval: 60 * 60 * 1000,
    });
    bot.once("error", onConnectionFailed);

    bot.waitTicks = req.body.waitTicks || 5;
    bot.on("kicked", onDisconnect);
    // A connection the server drops without a kick (ECONNRESET) only fires "end".
    // Stop the loop so it does not keep deciding against a dead socket, and remember
    // why so /agent/status can tell Python to reconnect.
    bot.once("end", (reason) => {
        lastDisconnect = String(reason || "connection ended");
        console.log(`bot disconnected: ${lastDisconnect}`);
        try {
            if (bot && bot.agentLoop) bot.agentLoop.stop().catch(() => {});
        } catch (err) {
            /* already gone */
        }
        bot = null;
    });
    bot.on("mount", () => bot.dismount()); // mounting stops physicsTick

    bot.once("spawn", async () => {
        try {
            await onSpawn();
        } catch (err) {
            // Never leave /start unanswered: Python waits on it for minutes.
            console.log("spawn setup failed:", err && err.stack ? err.stack : err);
            if (!res.headersSent) res.status(500).json({ error: `spawn setup failed: ${errorText(err)}` });
        }
    });

    async function onSpawn() {
        bot.removeListener("error", onConnectionFailed);
        let itemTicks = 1;
        if (req.body.reset === "hard") {
            bot.chat("/clear @s");
            bot.chat("/kill @s");
            const inventory = req.body.inventory || {};
            const equipment = req.body.equipment || [null, null, null, null, null, null];
            for (const key in inventory) {
                bot.chat(`/give @s minecraft:${key} ${inventory[key]}`);
                itemTicks += 1;
            }
            const equipmentNames = ["armor.head", "armor.chest", "armor.legs", "armor.feet", "weapon.mainhand", "weapon.offhand"];
            for (let i = 0; i < 6; i++) {
                if (i === 4) continue;
                if (equipment[i]) {
                    bot.chat(`/item replace entity @s ${equipmentNames[i]} with minecraft:${equipment[i]}`);
                    itemTicks += 1;
                }
            }
        }
        if (req.body.position) {
            bot.chat(`/tp @s ${req.body.position.x} ${req.body.position.y} ${req.body.position.z}`);
        }

        const { pathfinder, Movements } = require("mineflayer-pathfinder");
        bot.loadPlugin(pathfinder);
        bot.loadPlugin(require("mineflayer-tool").plugin);
        bot.loadPlugin(require("mineflayer-collectblock").plugin);
        bot.loadPlugin(require("mineflayer-pvp").plugin);
        try {
            // generated primitives may use bot.pathfinder.goto; give it sane defaults
            const movements = new Movements(bot, require("minecraft-data")(bot.version));
            movements.canDig = false;
            bot.pathfinder.setMovements(movements);
        } catch (err) {
            console.log("pathfinder movements not set:", errorText(err));
        }

        agentLoop.inject(bot);
        obs.inject(bot, [OnChat, OnError, Voxels, Status, Inventory, OnSave, Chests, BlockRecords]);

        if (req.body.spread) {
            bot.chat(`/spreadplayers ~ ~ 0 300 under 80 false @s`);
            await bot.waitForTicks(bot.waitTicks);
        }
        await bot.waitForTicks(bot.waitTicks * itemTicks);
        res.json(bot.observe());
        bot.chat("/gamerule keepInventory true");
        bot.chat("/gamerule doDaylightCycle false");
    }

    function onConnectionFailed(e) {
        console.log(e);
        bot = null;
        res.status(400).json({ error: e });
    }
    function onDisconnect(message) {
        if (bot.viewer) bot.viewer.close();
        if (bot.agentLoop) bot.agentLoop.stop().catch(() => {});
        bot.end();
        console.log(message);
        bot = null;
    }
});

// In-process reset between goals: stop the loop, clear what actions may have
// left running, optionally teleport. Returns an observation like /start.
app.post("/reset", async (req, res) => {
    if (!bot || !bot.entity) {
        res.status(400).json({ error: "Bot not spawned" });
        return;
    }
    try {
        if (bot.agentLoop) await bot.agentLoop.stop();
        if (!FAKE_WORLD) {
            bot.waitTicks = req.body.waitTicks || bot.waitTicks;
            await cleanupBot(bot);
            if (req.body.position) {
                bot.chat(`/tp @s ${req.body.position.x} ${req.body.position.y} ${req.body.position.z}`);
            }
            if (req.body.spread) {
                bot.chat(`/spreadplayers ~ ~ 0 300 under 80 false @s`);
                await bot.waitForTicks(bot.waitTicks);
            }
            await bot.waitForTicks(bot.waitTicks);
        }
        res.json(bot.observe());
    } catch (err) {
        console.log("reset failed:", err);
        res.status(500).json({ error: errorText(err) });
    }
});

// ---------------------------------------------------------------------------
// Knowledge: recipes and tables for the decomposer. Works before /start.
// ---------------------------------------------------------------------------
app.get("/knowledge", (req, res) => {
    try {
        const version = req.query.version || (bot && bot.version) || process.env.MC_VERSION || "1.19";
        res.json(knowledge.build(version));
    } catch (err) {
        res.status(500).json({ error: errorText(err) });
    }
});

// ---------------------------------------------------------------------------
// Agent loop endpoints (lib/agentLoop.js)
// ---------------------------------------------------------------------------
app.post("/agent/subtask", async (req, res) => {
    const loop = requireLoop(res);
    if (!loop) return;
    try {
        const result = await loop.setSubtask(req.body || {});
        if (!result.ok) res.status(400).json(result);
        else res.json(result);
    } catch (err) {
        console.log("agent/subtask failed:", err);
        res.status(500).json({ ok: false, error: errorText(err) });
    }
});

app.get("/agent/status", (req, res) => {
    if (!bot && lastDisconnect) {
        res.json({ disconnected: true, reason: lastDisconnect });
        return;
    }
    const loop = requireLoop(res);
    if (!loop) return;
    try {
        const drain = req.query.drain !== "0";
        res.json(loop.status({ drain }));
    } catch (err) {
        console.log("agent/status failed:", err);
        res.status(500).json({ error: errorText(err) });
    }
});

app.post("/agent/verify", async (req, res) => {
    const loop = requireLoop(res);
    if (!loop) return;
    try {
        const spec = (req.body || {}).spec;
        res.json(await loop.verifySpec(spec));
    } catch (err) {
        res.status(500).json({ ok: false, detail: errorText(err) });
    }
});

// Register (or replace, or remove with {remove:true}) a runtime action.
// Returns the exact compile or check error so the builder can retry with it.
app.post("/agent/primitive", async (req, res) => {
    const loop = requireLoop(res);
    if (!loop) return;
    const b = req.body || {};
    if (b.remove) {
        res.json({ ok: loop.registry.remove(String(b.name || "")), error: null });
        return;
    }
    let snap = null;
    try {
        snap = loop.snapshot();
    } catch (err) {
        snap = null;
    }
    const name = String(b.name || "");
    // menu() is smoke-tested against the current snapshot only while a subtask is
    // active: before one is posted (reloading the library at startup) snap.subtask is
    // null, and a correct action that reads its progress would be wrongly rejected.
    const smokeSnap = loop.subtask ? snap : null;
    const result = loop.registry.register(name, String(b.source || ""), smokeSnap, b.purpose ? String(b.purpose) : null);
    if (result.ok && b.repaired) loop.registry.markRepaired(name);
    // Applicability check: would menu() offer this action in the world as it is now?
    // An action that is never offered can never be tried, so the builder needs to know
    // at compile time, not after a wasted trial.
    let offeredNow = null;
    let menuLine = null;
    let menuError = null;
    if (result.ok && smokeSnap) {
        const p = loop.registry.get(name);
        try {
            const line = p.menu(snap, loop);
            offeredNow = typeof line === "string" && line.trim().length > 0;
            menuLine = offeredNow ? line.trim().slice(0, 300) : null;
        } catch (err) {
            offeredNow = false;
            menuError = errorText(err);
        }
    }
    // Ground truth for the builder: what the helpers return for each target right now.
    const probe = {};
    if (snap) {
        for (const key of Object.keys(snap.blocks || {})) {
            try {
                const found = loop.nearestBlocks(observeLib.matcherFor(key), 32, 1);
                probe[key] = found.length
                    ? `loop.nearestBlocks(loop.matcher("${key}"), 32, 1) -> ${found[0].name} at ${found[0].position.distanceTo(bot.entity.position).toFixed(1)} blocks`
                    : `loop.nearestBlocks(loop.matcher("${key}"), 32, 1) -> []`;
            } catch (err) {
                probe[key] = `probe failed: ${errorText(err)}`;
            }
        }
    }
    // Does the action's declared achievement already hold? Then the menu would hide it
    // even though the subtask that asked for it is unfinished: the declaration is wrong.
    let purposeHoldsNow = null;
    const reg = result.ok ? loop.registry.get(name) : null;
    if (reg && reg.purposeSpec) {
        try {
            purposeHoldsNow = Boolean((await loop.verifySpec(reg.purposeSpec)).ok);
        } catch (err) {
            purposeHoldsNow = null;
        }
    }
    res.json({
        ok: result.ok,
        error: result.error || null,
        purpose: reg ? reg.purpose : null,
        purpose_spec: reg ? reg.purposeSpec : null,
        purpose_holds_now: purposeHoldsNow,
        probe,
        offered_now: offeredNow,
        menu_line: menuLine,
        menu_error: menuError,
        primitives: loop.registry.summary(),
    });
});

app.post("/agent/stop", async (req, res) => {
    if (!bot || !bot.agentLoop) {
        res.json({ ok: true });
        return;
    }
    try {
        res.json(await bot.agentLoop.stop());
    } catch (err) {
        res.status(500).json({ ok: false, error: errorText(err) });
    }
});

app.post("/stop", (req, res) => {
    if (bot && bot.agentLoop) bot.agentLoop.stop().catch(() => {});
    if (bot && !FAKE_WORLD) bot.end();
    res.json({ message: "Bot stopped" });
});

app.post("/pause", (req, res) => {
    if (!bot) {
        res.status(400).json({ error: "Bot not spawned" });
        return;
    }
    if (FAKE_WORLD) {
        res.json({ message: "Success" });
        return;
    }
    bot.chat("/pause");
    bot.waitForTicks(bot.waitTicks).then(() => res.json({ message: "Success" }));
});

app.listen(PORT, () => {
    console.log(`Server started on port ${PORT}${FAKE_WORLD ? " (fake world)" : ""}`);
});
