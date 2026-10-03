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
        let prefetched = null; // next Jev decision, requested before the leg ends
        const DECISION_INTERVAL_MS = 10000;
        const PREFETCH_DISTANCE = 5;

        const cleanUp = () => {
            finished = true;
            clearInterval(explorationInterval);
            clearTimeout(maxTimeTimeout);
            bot.removeListener("goal_reached", onLegDone);
            bot.removeListener("path_update", onPathUpdate);
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

        const applyDecision = (decision) => {
            // The decision may resolve after exploration ended; a
            // late setGoal would steer the bot during later code.
            if (finished) return;
            if (decision && decision.goal) {
                bot.pathfinder.setGoal(decision.goal);
            } else if (!decision || decision.choice !== "stay") {
                bot.pathfinder.setGoal(randomGoal());
            }
        };

        const requestDecision = () =>
            bot
                .jevNextGoal({
                    objective,
                    direction: { x: dx, y: dy, z: dz },
                })
                .catch(() => null);

        const decide = (force = false) => {
            // Each decision is a billed API call: re-decide when a leg ends
            // (goal_reached / no path), when a prefetched answer is ready, or
            // when the current leg has run a while.
            const now = Date.now();
            if (
                deciding ||
                (!force &&
                    bot.pathfinder.isMoving() &&
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
            const pending = prefetched || requestDecision();
            prefetched = null;
            pending.then(applyDecision).finally(() => {
                deciding = false;
            });
        };

        // Event-driven re-decide: no idle gap between legs.
        const onLegDone = () => {
            if (finished) return;
            decide(true);
        };
        const onPathUpdate = (results) => {
            if (finished || deciding) return;
            if (results.status === "noPath" || results.status === "timeout") {
                decide(true);
                return;
            }
            // Start the next decision while the last few blocks are walked.
            if (
                !prefetched &&
                typeof bot.jevNextGoal === "function" &&
                results.path &&
                results.path.length > 0 &&
                results.path.length <= PREFETCH_DISTANCE
            ) {
                prefetched = requestDecision();
            }
        };
        bot.on("goal_reached", onLegDone);
        bot.on("path_update", onPathUpdate);

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
