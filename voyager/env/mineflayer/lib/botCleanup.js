// Reset a long-lived bot between tasks without restarting the Node process.
// Clears everything generated code may have left running on the bot and the
// per-task bookkeeping that a process restart used to reset implicitly.
//
// Ordering matters for mineflayer-pathfinder: `pathfinder.stop()` only sets a
// "stop at next node" flag, and that flag is cleared when `resetPath` runs,
// which `setGoal` triggers. If `setGoal(null)` ran first the path would already
// be empty, the flag would stay latched, and the next task's first `setGoal`
// would be cancelled with "Path was stopped". So: request stops first, then
// `setGoal(null)` last to drain the flag and emit `path_stop`.

const PLUGIN_STOP_TIMEOUT_MS = 5000;

function withTimeout(promise, ms, label) {
    let timer;
    const timeout = new Promise((resolve) => {
        timer = setTimeout(() => {
            console.log(`cleanup: ${label} did not finish within ${ms}ms`);
            resolve();
        }, ms);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function cleanupBot(bot) {
    // 0. the fast loop is the only other controller of the bot
    if (bot.fastLoop) {
        try {
            await withTimeout(bot.fastLoop.stop(), PLUGIN_STOP_TIMEOUT_MS * 3, "fastLoop.stop");
        } catch (err) {
            console.log("cleanup: fastLoop", err.message);
        }
    }
    // 1. plugins that drive the pathfinder: ask them to stop first
    let pvpStop = null;
    try {
        if (bot.pvp) pvpStop = Promise.resolve(bot.pvp.stop());
    } catch (err) {
        console.log("cleanup: pvp", err.message);
    }
    try {
        if (bot.collectBlock) {
            await withTimeout(
                Promise.resolve(bot.collectBlock.cancelTask()),
                PLUGIN_STOP_TIMEOUT_MS,
                "collectBlock.cancelTask"
            );
        }
    } catch (err) {
        console.log("cleanup: collectBlock", err.message);
    }
    // 2. the pathfinder itself: flag the stop, then clear the goal, which runs
    //    resetPath and drains the flag (emitting path_stop for pvp.stop above)
    try {
        bot.pathfinder.stop();
    } catch (err) {
        console.log("cleanup: pathfinder.stop", err.message);
    }
    try {
        bot.pathfinder.setGoal(null);
    } catch (err) {
        console.log("cleanup: pathfinder.setGoal", err.message);
    }
    if (pvpStop) {
        await withTimeout(
            pvpStop.catch((err) => console.log("cleanup: pvp", err.message)),
            PLUGIN_STOP_TIMEOUT_MS,
            "pvp.stop"
        );
    }
    // 3. anything still held on the bot body
    try {
        bot.clearControlStates();
    } catch (err) {
        console.log("cleanup: controls", err.message);
    }
    try {
        bot.stopDigging();
    } catch (err) {
        // not digging
    }
    // 4. per-task bookkeeping
    bot.globalTickCounter = 0;
    bot.stuckTickCounter = 0;
    bot.stuckPosList = [];
    bot.jevAskCalls = 0;
    bot.cumulativeObs = [];
    if (Array.isArray(bot.obsList)) {
        for (const o of bot.obsList) {
            try {
                o.reset();
            } catch (err) {
                console.log(`cleanup: observation ${o.name}`, err.message);
            }
        }
    }
}

module.exports = { cleanupBot };
