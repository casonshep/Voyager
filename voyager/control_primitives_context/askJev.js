/*
Ask Jev which of several visible trees is the best to walk to, then fall back to the nearest one
const logs = bot.findBlocks({ matching: mcData.blocksByName["oak_log"].id, maxDistance: 32, count: 5 });
let target = logs[0];
if (logs.length > 1) {
    const options = {};
    const context = {};
    logs.forEach((pos, i) => {
        options[`log_${i}`] = null;
        context[`log_${i}`] = { position: pos, distance: Math.round(pos.distanceTo(bot.entity.position)) };
    });
    const answer = await askJev(bot, "Which log is safest and quickest to reach?", options, context);
    if (answer) {
        target = logs[parseInt(answer.choice.split("_")[1])];
    }
}

Ask Jev whether to keep mining or retreat first, with a sensible default when Jev is unsure
const answer = await askJev(bot, "Should the bot keep mining here or retreat to safety first?", {
    continue: "Surroundings are safe enough to keep working",
    retreat: "Health, hunger, darkness, or hostile mobs make it unsafe; move away first",
});
if (answer && answer.choice === "retreat" && answer.confidence > 0.5) {
    bot.chat("Retreating before continuing.");
    const hostile = ["zombie", "skeleton", "creeper", "spider"];
    await exploreUntil(bot, new Vec3(1, 0, 0), 20, () => {
        const threat = bot.nearestEntity((e) => hostile.includes(e.name) && e.position.distanceTo(bot.entity.position) < 16);
        return threat ? null : true;
    }, "move away from hostile mobs to a safe spot");
}
*/
async function askJev(bot, question, options, context = {}) {
    /*
    Implementation of this function is omitted.
    question: string, a short, specific judgment question about the current situation.
    options: either an array of option names, or an object mapping each option name to a
        short description (or null when the name is self-explanatory). Give at least two.
    context: optional object of named facts you gathered for this decision (candidate
        positions, distances, what the skill is trying to do). Jev also sees the bot's
        position, health, food, time of day, inventory counts, nearby blocks and
        nearby entities automatically.

    Return: { choice, confidence, probabilities } where `choice` is one of your option
        names and `confidence` is 0-1, or null when Jev is unavailable or unsure.
        Always handle null with a plain-code default.

    Use askJev only for judgment calls: picking between candidates, deciding whether it
    is safe to continue, choosing where to place something. Never use it to check the
    inventory, whether a block or mob exists, or any other exact fact; use code for those.
    Do not call it inside loops; a program may make at most 20 askJev calls.
    */
}
