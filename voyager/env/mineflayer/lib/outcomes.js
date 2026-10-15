// Typed labels for action outcomes. An outcome is the "ok ..." / "failed: ..."
// string an action returns, plus the loop's own annotations ([NO EFFECT],
// [NO PROGRESS ...]). The label is an observed fact about what happened, read
// off that text with fixed patterns: it is what the builder's repair prompt, the
// decision log and the learning metrics group failures by. It never decides
// what the agent does next.
//
//   OK                the action did something observable
//   NO_CHANGE         reported ok, but nothing observable changed (a look that revealed nothing)
//   NO_EFFECT         a learned action said ok but its declared purpose or the world did not change
//   API_MISUSE        the code called mineflayer wrongly (wrong argument type, undefined property)
//   NOT_REACHABLE     pathfinding failed or the target was out of reach
//   NO_TARGET         nothing of the wanted kind was found
//   NO_PLACE_SURFACE  no free spot or solid block to place against
//   MISSING_INPUT     a recipe or step lacked an input item
//   MOVE_BLOCKED      a walk could not move
//   TIMEOUT           the action ran past its time limit
//   OTHER_FAILURE     any other "failed: ..."

const LABELS = [
    "OK", "NO_CHANGE", "NO_EFFECT", "API_MISUSE", "NOT_REACHABLE", "NO_TARGET",
    "NO_PLACE_SURFACE", "MISSING_INPUT", "MOVE_BLOCKED", "TIMEOUT", "OTHER_FAILURE",
];

const PATTERNS = [
    ["TIMEOUT", /timed out after/i],
    ["API_MISUSE", /must be an Item OBJECT|must be a Block|must be a Vec3|must come from loop\.recipesFor|is not a function|Cannot read propert|is not defined|is not iterable|TypeError|ReferenceError|undefined \(reading|must be a positive integer|must return/i],
    ["NO_PLACE_SURFACE", /no free spot|not solid|no (solid )?(ground|surface|block) to place|place against|could not place|placeBlock/i],
    ["NOT_REACHABLE", /no path|path was stopped|goal was changed|too far|out of reach|cannot reach|unreachable|not reachable|beyond reach/i],
    ["MISSING_INPUT", /no craftable recipe|missing input|not enough|no .{1,40} in (the )?inventory|need(s)? .{1,30} first|requires? a crafting.table|no (crafting.table|furnace) (nearby|within)/i],
    ["NO_TARGET", /no .{1,40} within|nothing (at|under) (the )?cursor|not found|no wanted|none visible|no target|no .{1,30} (nearby|visible|in view)/i],
    ["MOVE_BLOCKED", /could not move|something is in the way/i],
];

// label for one action record {outcome, delta}; `changed` says whether the world changed
function labelOf(outcome, changed = true) {
    const text = String(outcome || "");
    if (/\[NO EFFECT|\[NO PROGRESS/.test(text)) return "NO_EFFECT";
    if (/^failed/i.test(text)) {
        for (const [label, re] of PATTERNS) if (re.test(text)) return label;
        return "OTHER_FAILURE";
    }
    return changed ? "OK" : "NO_CHANGE";
}

function isFailureLabel(label) {
    return label !== "OK" && label !== "NO_CHANGE";
}

module.exports = { labelOf, isFailureLabel, LABELS };
