// TypeSafe (Jev) client bootstrap shared by the agent loop. Jev is a System
// One model returning typed judgments (Choice, Noul). Code owns the workflow;
// Jev only answers the questions the loop asks over an observed snapshot.
//
// If the SDK or TYPESAFE_API_KEY is unavailable, getClient() returns null and
// callers fall back to plain code paths.

const DECISION_TIMEOUT_MS = 10 * 1000;
// Below this Choice confidence the distribution is close to flat over the
// candidates, so the answer carries no real signal.
const MIN_CONFIDENCE = 0.2;

const COMPASS = {
    north: { dx: 0, dz: -1 },
    northeast: { dx: 1, dz: -1 },
    east: { dx: 1, dz: 0 },
    southeast: { dx: 1, dz: 1 },
    south: { dx: 0, dz: 1 },
    southwest: { dx: -1, dz: 1 },
    west: { dx: -1, dz: 0 },
    northwest: { dx: -1, dz: -1 },
};

let client; // undefined = not initialized, null = unavailable
let choiceFn;
let noulFn;
let scoreFn;

// The question builders (choice, noul, score) are plain functions: load them
// whenever the package is installed, key or not, so questions can be built and
// inspected offline.
let sdkLoaded = false;
function loadSdk() {
    if (sdkLoaded) return;
    sdkLoaded = true;
    try {
        const sdk = require("@typesafe-ai/sdk");
        choiceFn = sdk.choice;
        noulFn = sdk.noul;
        scoreFn = sdk.score;
    } catch (err) {
        /* no SDK: callers fall back to code paths */
    }
}

function getClient() {
    if (client !== undefined) return client;
    if (!process.env.TYPESAFE_API_KEY) {
        console.log("jev: TYPESAFE_API_KEY not set, Jev decisions disabled (code fallbacks only)");
        client = null;
        return client;
    }
    try {
        const sdk = require("@typesafe-ai/sdk");
        client = new sdk.TypeSafeClient();
        choiceFn = sdk.choice;
        noulFn = sdk.noul;
        scoreFn = sdk.score;
    } catch (err) {
        console.log("jev: TypeSafe SDK unavailable:", err.message);
        client = null;
    }
    return client;
}

function compassNameOf(dx, dz) {
    let best = null;
    let bestDot = -Infinity;
    const len = Math.sqrt(dx * dx + dz * dz) || 1;
    for (const [name, d] of Object.entries(COMPASS)) {
        const dlen = Math.sqrt(d.dx * d.dx + d.dz * d.dz);
        const dot = (dx / len) * (d.dx / dlen) + (dz / len) * (d.dz / dlen);
        if (dot > bestDot) {
            bestDot = dot;
            best = name;
        }
    }
    return best;
}

// Entities within `maxDistance` blocks, nearest first, as Jev-friendly records.
function nearbyEntitiesOf(bot, maxDistance = 24, limit = 8) {
    const pos = bot.entity.position;
    return Object.values(bot.entities || {})
        .filter((e) => e !== bot.entity && e.position && e.name !== "item" && e.position.distanceTo(pos) < maxDistance)
        .sort((a, b) => a.position.distanceTo(pos) - b.position.distanceTo(pos))
        .slice(0, limit)
        .map((e) => ({
            name: e.name || e.username || "unknown",
            distance: Math.round(e.position.distanceTo(pos)),
            direction: compassNameOf(e.position.x - pos.x, e.position.z - pos.z),
        }));
}

module.exports = {
    getClient,
    COMPASS,
    compassNameOf,
    nearbyEntitiesOf,
    DECISION_TIMEOUT_MS,
    MIN_CONFIDENCE,
    get choiceFn() {
        loadSdk();
        return choiceFn;
    },
    get noulFn() {
        loadSdk();
        return noulFn;
    },
    get scoreFn() {
        loadSdk();
        return scoreFn;
    },
};
