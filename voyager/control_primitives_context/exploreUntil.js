/*
Explore until find an iron_ore, use Vec3(0, -1, 0) because iron ores are usually underground
await exploreUntil(bot, new Vec3(0, -1, 0), 60, () => {
    const iron_ore = bot.findBlock({
        matching: mcData.blocksByName["iron_ore"].id,
        maxDistance: 32,
    });
    return iron_ore;
}, "find iron ore underground");

Explore until find a pig, use Vec3(1, 0, 1) because pigs are usually on the surface
let pig = await exploreUntil(bot, new Vec3(1, 0, 1), 60, () => {
    const pig = bot.nearestEntity((entity) => {
        return (
            entity.name === "pig" &&
            entity.position.distanceTo(bot.entity.position) < 32
        );
    });
    return pig;
}, "find a pig on the surface");
*/
async function exploreUntil(bot, direction, maxTime = 60, callback, objective = "") {
    /*
    Implementation of this function is omitted.
    direction: Vec3, can only contain value of -1, 0 or 1. Used as a hint for
        the traversal model and as the fallback exploration direction.
    maxTime: number, the max time for exploration
    callback: function, early stop condition, will be called each second, exploration will stop if return value is not null
    objective: string, a short description of what the exploration is trying to
        find or achieve. Always pass this; it lets the traversal model pick
        smarter waypoints from the game state.

    Return: null if explore timeout, otherwise return the return value of callback
    */
}
