// Dry-run for lib/jevTraversal.js without Minecraft: feeds a canned game-state
// snapshot through the candidate builder and the Jev call, and prints the
// chosen goal. Requires TYPESAFE_API_KEY to exercise the real decision;
// without it, verifies the fallback path returns null.
//
// Usage: node scripts/jev_dryrun.js
const { Vec3 } = require("vec3");
const {
    decideNextMove,
    gatherState,
    askJevChoice,
    MAX_ASK_CALLS_PER_STEP,
} = require("../lib/jevTraversal");

function fakeBlock(name) {
    return { name, type: name === "air" ? 0 : 1 };
}

// A simple world: grass plains, a lava pool to the east, a cow to the north.
const fakeBot = {
    entity: { position: new Vec3(0.5, 64, 0.5) },
    health: 6,
    food: 18,
    inventory: {
        items: () => [
            { name: "oak_log", count: 3 },
            { name: "wooden_pickaxe", count: 1 },
        ],
    },
    time: { timeOfDay: 1000 },
    entities: {
        1: { name: "cow", position: new Vec3(5, 64, -20) },
    },
    blockAt(pos) {
        if (pos.x > 8 && pos.y <= 63) return fakeBlock("lava");
        if (pos.y >= 64) return fakeBlock("air");
        if (pos.y === 63) return fakeBlock("grass_block");
        return fakeBlock("stone");
    },
};
fakeBot.entities[0] = fakeBot.entity;

(async () => {
    const options = {
        objective: "find a cow to collect beef",
        direction: { x: 1, y: 0, z: 1 },
    };
    console.log("--- gathered state ---");
    console.log(
        JSON.stringify(
            gatherState(fakeBot, options.objective, options.direction),
            null,
            2
        )
    );
    console.log("--- decision ---");
    const decision = await decideNextMove(fakeBot, options);
    if (!decision) {
        console.log(
            "decideNextMove returned null (fallback path). " +
                (process.env.TYPESAFE_API_KEY
                    ? "Jev was reachable but unsure or errored - see logs above."
                    : "Expected: TYPESAFE_API_KEY is not set.")
        );
        return;
    }
    console.log(`choice: ${decision.choice}`);
    console.log(`confidence: ${decision.confidence.toFixed(3)}`);
    console.log(`goal: ${decision.goal ? decision.goal.constructor.name : "stay (null)"}`);
})().then(async () => {
    console.log("--- askJev: caller mistakes throw ---");
    for (const bad of [
        () => askJevChoice(fakeBot, "", ["a", "b"]),
        () => askJevChoice(fakeBot, "pick", ["only_one"]),
        () => askJevChoice(fakeBot, "pick", "not options"),
    ]) {
        try {
            await bad();
            console.log("FAIL: expected an error");
        } catch (err) {
            console.log("ok:", err.message);
        }
    }
    console.log("--- askJev: judgment call ---");
    fakeBot.jevAskCalls = 0;
    const answer = await askJevChoice(
        fakeBot,
        "Should the bot keep working here or retreat to safety first?",
        {
            continue: "Surroundings are safe enough to keep working",
            retreat: "Health, hunger, darkness, or hostile mobs make it unsafe",
        },
        { task: "mine 3 more oak logs", lavaSeenToTheEast: true }
    );
    if (!answer) {
        console.log(
            "askJevChoice returned null (fallback path). " +
                (process.env.TYPESAFE_API_KEY
                    ? "Jev was reachable but unsure or errored - see logs above."
                    : "Expected: TYPESAFE_API_KEY is not set.")
        );
    } else {
        console.log(`choice: ${answer.choice}`);
        console.log(`confidence: ${answer.confidence.toFixed(3)}`);
        console.log(`probabilities: ${JSON.stringify(answer.probabilities)}`);
    }
    console.log("--- askJev: per-program call cap ---");
    fakeBot.jevAskCalls = MAX_ASK_CALLS_PER_STEP;
    try {
        await askJevChoice(fakeBot, "pick", ["a", "b"]);
        console.log("FAIL: expected the cap to throw");
    } catch (err) {
        console.log("ok:", err.message);
    }
});
