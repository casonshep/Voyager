// Ask Jev (TypeSafe) a judgment question about the current game state.
// Returns { choice, confidence, probabilities } or null when Jev is
// unavailable or unsure, so callers must always keep a plain-code fallback.
// Use for judgment calls only (which target, whether to continue, where to
// place); exact facts such as inventory contents belong in ordinary code.
async function askJev(bot, question, options, context = {}) {
    if (typeof bot.jevAsk !== "function") {
        return null;
    }
    return bot.jevAsk(question, options, context);
}
