// Jev-based traversal: TypeSafe's Jev model (a System One model returning
// typed judgments) picks the next waypoint from a snapshot of game state;
// mineflayer-pathfinder still executes the actual movement. If the SDK or
// TYPESAFE_API_KEY is unavailable, or Jev is unsure, callers fall back to
// the old random-walk behavior.
const { Vec3 } = require("vec3");

const SURVEY_DISTANCE = 16;
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

function getClient() {
    if (client !== undefined) return client;
    if (!process.env.TYPESAFE_API_KEY) {
        console.log(
            "jevTraversal: TYPESAFE_API_KEY not set, falling back to random exploration"
        );
        client = null;
        return client;
    }
    try {
        const sdk = require("@typesafe-ai/sdk");
        client = new sdk.TypeSafeClient();
        choiceFn = sdk.choice;
    } catch (err) {
        console.log("jevTraversal: TypeSafe SDK unavailable:", err.message);
        client = null;
    }
    return client;
}

// Sample the column of blocks around the point `dist` blocks away in a
// direction: surface block, elevation change, and any lava/water seen.
function surveyCandidate(bot, dx, dz, dist) {
    const pos = bot.entity.position;
    const x = Math.floor(pos.x + dx * dist);
    const z = Math.floor(pos.z + dz * dist);
    const y0 = Math.floor(pos.y);
    const hazards = new Set();
    let surfaceBlock = "unknown";
    let elevationDelta = null;
    let aboveWasAir = false;
    for (let dy = 8; dy >= -8; dy--) {
        const block = bot.blockAt(new Vec3(x, y0 + dy, z));
        if (!block) continue;
        if (block.name === "lava" || block.name === "flowing_lava") {
            hazards.add("lava");
        }
        if (block.name === "water" || block.name === "flowing_water") {
            hazards.add("water");
        }
        if (block.name === "air" || block.name === "cave_air") {
            aboveWasAir = true;
        } else if (aboveWasAir && elevationDelta === null) {
            surfaceBlock = block.name;
            elevationDelta = dy;
        }
    }
    return {
        surfaceBlock,
        elevationDelta: elevationDelta === null ? "unknown" : elevationDelta,
        hazards: [...hazards],
    };
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

function gatherState(bot, objective, directionHint) {
    const pos = bot.entity.position;
    const candidates = {};
    for (const [name, { dx, dz }] of Object.entries(COMPASS)) {
        candidates[name] = surveyCandidate(bot, dx, dz, SURVEY_DISTANCE);
    }
    const above = bot.blockAt(pos.offset(0, 2, 0));
    const below = bot.blockAt(pos.offset(0, -1, 0));
    candidates.up = { blockOverhead: above ? above.name : "unknown" };
    candidates.down = { blockUnderfoot: below ? below.name : "unknown" };

    const nearbyEntities = Object.values(bot.entities)
        .filter(
            (e) =>
                e !== bot.entity &&
                e.position &&
                e.position.distanceTo(pos) < 32
        )
        .slice(0, 10)
        .map((e) => ({
            name: e.name || e.username || "unknown",
            distance: Math.round(e.position.distanceTo(pos)),
            direction: compassNameOf(
                e.position.x - pos.x,
                e.position.z - pos.z
            ),
        }));

    let hint = "none";
    if (directionHint && (directionHint.x || directionHint.z)) {
        hint = compassNameOf(directionHint.x, directionHint.z);
    }
    if (directionHint && directionHint.y) {
        hint =
            hint === "none"
                ? directionHint.y > 0
                    ? "up"
                    : "down"
                : `${hint}, and ${directionHint.y > 0 ? "upward" : "downward"}`;
    }

    return {
        objective: objective || "explore to find useful resources",
        directionHint: hint,
        bot: {
            position: {
                x: Math.floor(pos.x),
                y: Math.floor(pos.y),
                z: Math.floor(pos.z),
            },
            health: bot.health,
            food: bot.food,
            timeOfDay: bot.time ? bot.time.timeOfDay : "unknown",
        },
        nearbyEntities,
        candidates,
    };
}

async function askJev(bot, objective, directionHint) {
    const c = getClient();
    if (!c) return null;
    const state = gatherState(bot, objective, directionHint);
    const request = c.systemOne({
        state,
        questions: {
            move: choiceFn(
                "A Minecraft bot is exploring to accomplish `objective`. " +
                    "Each compass option in `candidates` describes the terrain " +
                    `about ${SURVEY_DISTANCE} blocks away in that direction: ` +
                    "the surface block, the elevation change (positive means " +
                    "uphill), and any lava or water hazards seen. " +
                    "`directionHint` is a suggestion from the planner, not a " +
                    "command. `nearbyEntities` lists mobs and players within " +
                    "32 blocks. Which travel direction makes the most " +
                    "progress toward the objective while avoiding hazards?",
                {
                    north: null,
                    northeast: null,
                    east: null,
                    southeast: null,
                    south: null,
                    southwest: null,
                    west: null,
                    northwest: null,
                    up: "Climb or build upward, e.g. to reach the surface or higher terrain",
                    down: "Dig downward, e.g. to reach ores or caves below",
                    stay: "Remain here; the surroundings already suit the objective",
                }
            ),
        },
    });
    const timeout = new Promise((_, reject) =>
        setTimeout(
            () => reject(new Error("Jev decision timed out")),
            DECISION_TIMEOUT_MS
        )
    );
    const response = await Promise.race([request, timeout]);
    return response.answers.move;
}

// Decide the next pathfinder goal. Resolves to:
//   { choice, confidence, goal } — goal is null for "stay"
//   null — Jev unavailable, unsure, or errored; caller should fall back
async function decideNextMove(bot, { objective, direction } = {}) {
    const {
        goals: { GoalNear, GoalNearXZ, GoalY },
    } = require("mineflayer-pathfinder");
    let answer;
    try {
        answer = await askJev(bot, objective, direction);
    } catch (err) {
        console.log("jevTraversal: decision failed:", err.message);
        return null;
    }
    if (!answer) return null;
    if (answer.confidence < MIN_CONFIDENCE) {
        console.log(
            `jevTraversal: low confidence ${answer.confidence.toFixed(2)} ` +
                `for "${answer.choice}", falling back`
        );
        return null;
    }
    const pos = bot.entity.position;
    const dist = Math.floor(Math.random() * 20 + 10);
    let goal;
    if (answer.choice === "stay") {
        goal = null;
    } else if (answer.choice === "up") {
        goal = new GoalY(Math.floor(pos.y) + 10);
    } else if (answer.choice === "down") {
        goal = new GoalY(Math.floor(pos.y) - 12);
    } else {
        const { dx, dz } = COMPASS[answer.choice];
        goal = new GoalNearXZ(
            Math.floor(pos.x + dx * dist),
            Math.floor(pos.z + dz * dist),
            3
        );
    }
    console.log(
        `jevTraversal: moving ${answer.choice} ` +
            `(confidence ${answer.confidence.toFixed(2)})`
    );
    return { choice: answer.choice, confidence: answer.confidence, goal };
}

function inject(bot) {
    bot.jevNextGoal = (options) => decideNextMove(bot, options);
}

module.exports = { inject, decideNextMove, gatherState };
