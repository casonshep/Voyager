// Explore downward for 60 seconds: exploreUntil(bot, new Vec3(0, -1, 0), 60);
// Waypoints are chosen by Jev (TypeSafe) from the game state when available;
// `direction` is used as a hint for Jev and as the random-walk fallback.
async function exploreUntil(
    bot,
    direction,
    maxTime = 60,
    callback = () => {
        return false;
    },
    objective = ""
) {
    if (typeof maxTime !== "number") {
        throw new Error("maxTime must be a number");
    }
    if (typeof callback !== "function") {
        throw new Error("callback must be a function");
    }
    const test = callback();
    if (test) {
        bot.chat("Explore success.");
        return Promise.resolve(test);
    }
    if (direction.x === 0 && direction.y === 0 && direction.z === 0) {
        throw new Error("direction cannot be 0, 0, 0");
    }
    if (
        !(
            (direction.x === 0 || direction.x === 1 || direction.x === -1) &&
            (direction.y === 0 || direction.y === 1 || direction.y === -1) &&
            (direction.z === 0 || direction.z === 1 || direction.z === -1)
        )
    ) {
        throw new Error(
            "direction must be a Vec3 only with value of -1, 0 or 1"
        );
    }
    maxTime = Math.min(maxTime, 1200);
    return new Promise((resolve, reject) => {
        const dx = direction.x;
        const dy = direction.y;
        const dz = direction.z;

        let explorationInterval;
        let maxTimeTimeout;
        let deciding = false;
        let finished = false;
        let lastDecisionTime = 0;
        const DECISION_INTERVAL_MS = 10000;

        const cleanUp = () => {
            finished = true;
            clearInterval(explorationInterval);
            clearTimeout(maxTimeTimeout);
            bot.pathfinder.setGoal(null);
        };

        const randomGoal = () => {
            const x =
                bot.entity.position.x +
                Math.floor(Math.random() * 20 + 10) * dx;
            const y =
                bot.entity.position.y +
                Math.floor(Math.random() * 20 + 10) * dy;
            const z =
                bot.entity.position.z +
                Math.floor(Math.random() * 20 + 10) * dz;
            if (dy === 0) {
                return new GoalNearXZ(x, z);
            }
            return new GoalNear(x, y, z);
        };

        const decide = () => {
            // Each decision is a billed API call: only re-decide when the
            // bot has stopped moving or the current leg has run a while.
            const now = Date.now();
            if (
                deciding ||
                (bot.pathfinder.isMoving() &&
                    now - lastDecisionTime < DECISION_INTERVAL_MS)
            ) {
                return;
            }
            deciding = true;
            lastDecisionTime = now;
            if (typeof bot.jevNextGoal !== "function") {
                bot.pathfinder.setGoal(randomGoal());
                deciding = false;
                return;
            }
            bot.jevNextGoal({
                objective,
                direction: { x: dx, y: dy, z: dz },
            })
                .then((decision) => {
                    // The decision may resolve after exploration ended; a
                    // late setGoal would steer the bot during later code.
                    if (finished) return;
                    if (decision && decision.goal) {
                        bot.pathfinder.setGoal(decision.goal);
                    } else if (!decision || decision.choice !== "stay") {
                        bot.pathfinder.setGoal(randomGoal());
                    }
                })
                .catch(() => {
                    if (finished) return;
                    bot.pathfinder.setGoal(randomGoal());
                })
                .finally(() => {
                    deciding = false;
                });
        };

        const explore = () => {
            try {
                const result = callback();
                if (result) {
                    cleanUp();
                    bot.chat("Explore success.");
                    resolve(result);
                    return;
                }
            } catch (err) {
                cleanUp();
                reject(err);
                return;
            }
            decide();
        };

        explore();
        explorationInterval = setInterval(explore, 2000);

        maxTimeTimeout = setTimeout(() => {
            cleanUp();
            bot.chat("Max exploration time reached");
            resolve(null);
        }, maxTime * 1000);
    });
}
