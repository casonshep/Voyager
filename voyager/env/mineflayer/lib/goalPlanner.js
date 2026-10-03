// Goal planner: decompose a high goal into a requirement graph and report
// which leaves can be worked on right now.
//
//   Craft a set of iron armor
//     iron_helmet 1, iron_chestplate 1, iron_leggings 1, iron_boots 1   (craft)
//       iron_ingot 24 (5+8+7+4, minus what is held)                      (smelt)
//         raw_iron 24                                                    (mine)
//           stone_pickaxe 1  <- iron ore needs at least stone            (craft)
//             cobblestone 3, stick 2 ...
//         station:furnace, fuel
//       station:crafting_table
//
// Quantities aggregate per item across every branch (iron for all four
// pieces is one requirement). Each node is `satisfied` (held), `ready`
// (every child satisfied: it can be executed now) or `blocked`. The ready,
// unsatisfied nodes are the candidate subgoals; the deepest ones are the
// focus. Items that drop from blocks are mined (never crafted from their
// block form); smelt outputs come from a furnace; mob drops come from mobs;
// anything else is a judged goal.
const {
    ORE_DROPS, SMELTABLE, FUELS, PICKAXE_TIER, MOB_DROPS, NATURAL_BLOCK_RE,
    SET_EXPANSIONS, SET_MATERIALS, SYNONYMS,
} = require("./mcKnowledge");

// a block that is crafted and placed (crafting table, furnace, chest, torch, ...)
// as opposed to one found in the world (logs, ores)
const isPlaceable = (mcData, block) =>
    Boolean(mcData.itemsByName[block]) && !NATURAL_BLOCK_RE.test(block) && !ORE_DROPS[block];

// a block the item can be mined from: natural blocks only
const sourceBlocks = (mcData, item, matches) =>
    Object.keys(mcData.blocksByName).filter(
        (b) => (NATURAL_BLOCK_RE.test(b) || ORE_DROPS[b]) && ((matches && matches(b)) || item === b || item === ORE_DROPS[b])
    );

const MAX_DEPTH = 12;
const CHUNK = 8; // most of one item asked for in a single subgoal: progress lands in the tree every chunk
const FURNACE_MAX_BATCH_PLAN = 64; // smelting is batched: load everything at once and come back
const MIN_FREE_SLOTS = 4; // below this, gathering steps first need inventory space
const FOOD_MOBS = "cow, pig, sheep or chicken";
const MAX_CANDIDATES = 8;
const FUEL_UNITS = 4; // coal to ask for when there is no fuel at all

// ---- target parsing ---------------------------------------------------------
const { textTokens, wordForms, phraseIn } = require("./fastLoop");

// tokens with synonyms applied; every plural form of each token
function normaliseWords(text) {
    return textTokens(text).map((w) => SYNONYMS[w] || w);
}
function tokenForms(words) {
    return new Set(words.flatMap((w) => [...wordForms(w)]));
}
const SURFACE_FAMILY_RE = /_log$|grass_block|^sand$|_leaves$|sugar_cane|pumpkin|melon|bamboo|cactus|sweet_berry|_sapling$/;

// Every item the goal asks for, with counts. "a set of iron armor" expands to
// the four pieces; "iron pickaxe, iron axe and iron shovel" lists three; a
// single item falls back to the loop's own parser.
async function parseTargets(loop, text, override) {
    if (override && Array.isArray(override.targets)) {
        // an explicit decomposition (e.g. proposed by an LLM): known items only
        const out = [];
        for (const t of override.targets) {
            if (t.freeSlots || t.nearBlock || t.dimension || t.nearPlayer || t.give) {
                out.push(await loop.parseTarget("", t));
                continue;
            }
            const item = String(t.item || "").toLowerCase().trim();
            const known = loop.mcData.itemsByName[item] || loop.mcData.blocksByName[item] || item.startsWith("family:") || item.startsWith("*_");
            if (!known) continue;
            out.push(await loop.parseTarget(item, { item, count: Math.max(1, parseInt(t.count, 10) || 1) }));
        }
        return out;
    }
    if (override) {
        const t = await loop.parseTarget(text, override);
        return t ? [t] : [];
    }
    const words = normaliseWords(text);
    const forms = tokenForms(words);
    const material = SET_MATERIALS.find((m) => forms.has(m));
    const setWord = Object.keys(SET_EXPANSIONS).find((s) => forms.has(s) || forms.has(s.replace(/s$/, "")));
    if (material && setWord) {
        const pieces = SET_EXPANSIONS[setWord]
            .filter((p) => !(setWord === "tools" && material === "leather"))
            .map((p) => `${material}_${p}`)
            .filter((n) => loop.mcData.itemsByName[n]);
        if (forms.has("sword") && setWord === "tools") pieces.push(`${material}_sword`);
        return Promise.all(pieces.map((n) => loop.parseTarget(n, { item: n, count: 1 })));
    }
    // several explicit items: all exact matches that are not part of a longer match
    const exact = Object.keys(loop.mcData.itemsByName).filter((name) => phraseIn(words, name));
    const maximal = exact.filter((a) => !exact.some((b) => b !== a && b.includes(a)));
    if (maximal.length >= 2 && /,| and |\(/.test(text)) {
        return Promise.all(maximal.map((n) => loop.parseTarget(n, { item: n, count: countFor(text, n) })));
    }
    const t = await loop.parseTarget(text, undefined);
    return t ? [t] : [];
}

function countFor(text, itemName) {
    // "3 iron pickaxes" -> 3; a bare item in a list -> 1
    const phrase = itemName.replace(/_/g, " ");
    const m = new RegExp(`(\\d+)\\s+${phrase}`, "i").exec(text);
    return m ? parseInt(m[1], 10) : 1;
}

// ---- requirement graph --------------------------------------------------------
class Planner {
    constructor(loop) {
        this.loop = loop;
        this.mcData = loop.mcData;
        this.inventory = loop.inventoryCounts();
        this.nodes = new Map(); // id -> node
    }

    node(id, init) {
        if (!this.nodes.has(id)) this.nodes.set(id, { id, need: 0, children: [], parents: [], ...init });
        return this.nodes.get(id);
    }

    link(parent, child) {
        if (!parent.children.includes(child.id)) parent.children.push(child.id);
        if (!child.parents.includes(parent.id)) child.parents.push(parent.id);
    }

    have(node) {
        if (node.kind === "station") return this.loop.blockNearby((b) => b === node.block) ? 1 : 0;
        if (node.kind === "explore") return this.loop.blockNearby(node.matches) ? 1 : 0;
        if (node.kind === "fuel") return FUELS.some((f) => this.inventory[f]) ? 1 : 0;
        if (node.kind === "space") return this.loop.freeSlots() >= MIN_FREE_SLOTS ? 1 : 0;
        if (node.kind === "tool") return this.toolTierIndex() >= PICKAXE_TIER.indexOf(node.tier) ? 1 : 0;
        if (node.matches) {
            let total = 0;
            for (const [n, c] of Object.entries(this.inventory)) if (node.matches(n)) total += c;
            return total;
        }
        return this.inventory[node.item] || 0;
    }

    toolTierIndex() {
        return PICKAXE_TIER.indexOf(this.loop.toolTier());
    }

    // Shape pass: how an item is obtained and what it depends on (unit quantities).
    // Depth is the longest path from a root, so every parent is expanded first.
    expand(node, depth, stack) {
        node.depth = Math.max(node.depth || 0, depth);
        if (node.expanded) return;
        if (depth >= MAX_DEPTH || stack.has(node.id)) return; // may still expand via a shorter path
        node.expanded = true;
        const next = new Set(stack);
        next.add(node.id);
        const item = node.item;
        if (!item || node.kind === "station" || node.kind === "fuel" || node.kind === "explore" || node.kind === "judged" || node.kind === "space") return;
        if (item === "family:food" || item === "family:cooked_food") {
            node.kind = "hunt";
            node.mob = FOOD_MOBS;
            this.needSpace(node, depth);
            return;
        }

        const blocks = sourceBlocks(this.mcData, item, node.matches);
        const rawInput = Object.keys(SMELTABLE).find((raw) => SMELTABLE[raw] === item);
        const itemDef = node.matches ? null : this.mcData.itemsByName[item];

        if (rawInput && !blocks.length) {
            node.kind = "smelt";
            node.rawInput = rawInput;
            const raw = this.node(rawInput, { item: rawInput, kind: "obtain" });
            this.link(node, raw);
            this.expand(raw, depth + 1, next);
            const furnace = this.node("station:furnace", { kind: "station", block: "furnace", item: null });
            this.link(node, furnace);
            this.expand(furnace, depth + 1, next);
            const furnaceItem = this.node("furnace", { item: "furnace", kind: "obtain" });
            this.link(furnace, furnaceItem);
            this.expand(furnaceItem, depth + 2, next);
            const fuel = this.node("fuel", { kind: "fuel", item: null });
            this.link(node, fuel);
            const coal = this.node("coal", { item: "coal", kind: "obtain" });
            this.link(fuel, coal);
            this.expand(coal, depth + 2, next);
            return;
        }
        if (blocks.length) {
            node.kind = "mine";
            node.blocks = blocks;
            this.needSpace(node, depth);
            const tier = this.loop.requiredPickaxe(blocks);
            if (tier) {
                const tool = this.node(`tool:${tier}_pickaxe`, { kind: "tool", tier, item: null });
                this.link(node, tool);
                tool.depth = Math.max(tool.depth || 0, depth + 1);
                const pick = this.node(`${tier}_pickaxe`, { item: `${tier}_pickaxe`, kind: "obtain" });
                this.link(tool, pick);
                this.expand(pick, depth + 2, next);
            }
            const explore = this.node(`explore:${item}`, {
                kind: "explore",
                item: null,
                blocks,
                matches: (b) => blocks.includes(b),
                family: blocks.length === 1 ? blocks[0] : `*_${commonSuffix(blocks)}`,
            });
            this.link(node, explore);
            explore.depth = Math.max(explore.depth || 0, depth + 1);
            return;
        }
        if (itemDef) {
            const recipes = this.loop.recipesFor(itemDef.id);
            const usable = recipes.filter((r) => !r.delta.some((d) => d.count < 0 && next.has(this.itemName(d.id))));
            if (usable.length) {
                node.kind = "craft";
                node.recipe = this.pickRecipe(usable);
                if (node.recipe.requiresTable) {
                    const table = this.node("station:crafting_table", { kind: "station", block: "crafting_table", item: null });
                    this.link(node, table);
                    table.depth = Math.max(table.depth || 0, depth + 1);
                    const tableItem = this.node("crafting_table", { item: "crafting_table", kind: "obtain" });
                    this.link(table, tableItem);
                    this.expand(tableItem, depth + 2, next);
                }
                for (const d of node.recipe.delta) {
                    if (d.count >= 0) continue;
                    const name = this.itemName(d.id);
                    if (!name) continue;
                    const child = this.node(name, { item: name, kind: "obtain" });
                    this.link(node, child);
                    this.expand(child, depth + 1, next);
                }
                return;
            }
        }
        if (MOB_DROPS[item]) {
            node.kind = "hunt";
            node.mob = MOB_DROPS[item];
            this.needSpace(node, depth);
            return;
        }
        node.kind = "judged";
    }

    // Gathering only works with room in the bag: hang a space requirement under
    // the node when free slots are low, so a deposit/discard step comes first.
    needSpace(node, depth) {
        if (this.loop.freeSlots() >= MIN_FREE_SLOTS) return;
        const space = this.node("space", { kind: "space", item: null, need: 1 });
        this.link(node, space);
        space.depth = Math.max(space.depth || 0, depth + 1);
    }

    // Items a known chest holds that this node needs: take them instead of making them.
    chestSupply(item, matches) {
        let total = 0;
        for (const chest of this.loop.chestsKnown()) {
            for (const [n, c] of Object.entries(chest.items)) if ((matches && matches(n)) || n === item) total += c;
        }
        return total;
    }

    itemName(id) {
        const it = this.mcData.items[id];
        return it ? it.name : null;
    }

    pickRecipe(recipes) {
        let best = null;
        let bestMissing = Infinity;
        for (const r of recipes) {
            let missing = 0;
            for (const d of r.delta) if (d.count < 0) missing += Math.max(0, -d.count - (this.inventory[this.itemName(d.id)] || 0));
            if (missing < bestMissing) {
                bestMissing = missing;
                best = r;
            }
        }
        return best;
    }

    // Longest-path depths along every edge, so each parent is processed
    // before its children regardless of the order the DFS found them.
    relaxDepths() {
        for (let i = 0; i < 50; i++) {
            let changed = false;
            for (const n of this.nodes.values()) {
                for (const c of n.children) {
                    const child = this.nodes.get(c);
                    if (child && (child.depth || 0) < (n.depth || 0) + 1 && child.id !== n.id) {
                        child.depth = (n.depth || 0) + 1;
                        changed = true;
                    }
                }
            }
            if (!changed) break;
        }
    }

    // Quantity pass in depth order: a node's total need is known once every
    // parent (all shallower) has been processed; its children then receive
    // their share from what is still missing.
    quantify() {
        this.relaxDepths();
        const order = [...this.nodes.values()].sort((a, b) => (a.depth || 0) - (b.depth || 0));
        for (const n of order) {
            // live = reachable from the goal through unsatisfied nodes; a branch
            // under something already held must not add to anyone's demand
            // (the pickaxe under held coal must not ask for sticks)
            n.live = n.id === "goal" || n.id === "later" || n.parents.some((p) => {
                const parent = this.nodes.get(p);
                return parent && parent.live && parent.remaining > 0;
            });
            n.have = this.have(n);
            n.remaining = Math.max(0, (n.need || 0) - n.have);
            if (n.kind === "station" || n.kind === "fuel" || n.kind === "tool" || n.kind === "explore" || n.kind === "space") {
                n.need = 1;
                n.remaining = n.have ? 0 : 1;
            }
            if (!n.live || n.remaining <= 0) continue;
            if (n.kind === "craft" && n.recipe) {
                const crafts = Math.ceil(n.remaining / Math.max(1, n.recipe.result.count));
                for (const d of n.recipe.delta) {
                    if (d.count >= 0) continue;
                    const child = this.nodes.get(this.itemName(d.id));
                    if (child) child.need += -d.count * crafts;
                }
            } else if (n.kind === "smelt") {
                const raw = this.nodes.get(n.rawInput);
                if (raw) raw.need += n.remaining;
            } else if (n.kind === "station") {
                const it = this.nodes.get(n.block);
                if (it) it.need += 1;
            } else if (n.kind === "fuel") {
                const coal = this.nodes.get("coal");
                if (coal) coal.need += FUEL_UNITS;
            } else if (n.kind === "tool") {
                const pick = this.nodes.get(`${n.tier}_pickaxe`);
                if (pick) pick.need += 1;
            }
        }
        // children's needs may have grown after they were visited (shared items
        // at different depths): one more pass for have/remaining/status
        for (const n of order) {
            n.have = this.have(n);
            n.remaining = ["station", "fuel", "tool", "explore", "space"].includes(n.kind) ? (n.have ? 0 : 1) : Math.max(0, n.need - n.have);
        }
        for (const n of [...order].reverse()) {
            // a known chest holding the item makes the node ready at once: take it
            // rather than make it, and nothing below it matters
            n.fromChest = Boolean(n.item && ["craft", "smelt", "mine", "obtain", "hunt"].includes(n.kind) && n.remaining > 0 && this.chestSupply(n.item, n.matches) > 0);
            // a station that already stands somewhere known is walked back to, not rebuilt
            const far = n.kind === "station" && !this.inventory[n.block] && this.loop.landmarkDistance ? this.loop.landmarkDistance(n.block) : null;
            n.fromLandmark = Boolean(n.kind === "station" && n.remaining > 0 && far !== null && far > 24);
            if (n.remaining <= 0) n.status = "satisfied";
            else if (n.fromChest || n.fromLandmark || n.children.every((c) => this.nodes.get(c).status === "satisfied")) n.status = "ready";
            else n.status = "blocked";
        }
        // Requirements below a satisfied node do not matter (the pickaxe gate
        // under raw iron that is already held): mark them moot so they are
        // neither candidates nor shown as open work.
        const reach = (start) => {
            const seen = new Set();
            const stack = [start];
            while (stack.length) {
                const id = stack.pop();
                const n = this.nodes.get(id);
                if (!n || seen.has(id)) continue;
                seen.add(id);
                if ((n.status === "satisfied" || n.fromChest || n.fromLandmark) && id !== start) continue;
                stack.push(...n.children);
            }
            return seen;
        };
        const now = reach("goal");
        const laterSet = this.nodes.has("later") ? reach("later") : new Set();
        for (const n of this.nodes.values()) {
            if (n.id === "goal" || n.id === "later") continue;
            if (now.has(n.id)) n.priority = "now";
            else if (laterSet.has(n.id)) n.priority = "later";
            else if (n.status !== "satisfied") n.status = "moot";
        }
    }

    // ---- candidates: ready, unsatisfied nodes as subgoals ------------------------
    candidate(n) {
        const why = this.pathUp(n);
        const chunk = Math.min(n.remaining || 1, CHUNK);
        // anything a known chest holds is taken from it rather than made
        if (n.item && ["craft", "smelt", "mine", "obtain", "hunt"].includes(n.kind)) {
            const inChest = this.chestSupply(n.item, n.matches);
            if (inChest > 0) {
                const take = Math.min(inChest, n.remaining || 1);
                return { text: `Take ${take} ${n.item} from the chest`, target: { item: n.item, count: (n.have || 0) + take }, why: `${why}; a known chest holds ${inChest}` };
            }
        }
        switch (n.kind) {
            case "space":
                return { text: `Free up ${MIN_FREE_SLOTS} inventory slots`, target: { freeSlots: MIN_FREE_SLOTS }, why: `${why}; the bag is nearly full (deposit into a chest or throw junk away)` };
            case "craft":
                // target counts are absolute holdings: have + what this step adds
                return { text: `Craft ${n.remaining} ${n.item}`, target: { item: n.item, count: (n.have || 0) + n.remaining }, why };
            case "smelt": {
                // a batch already cooking covers this: collect it when ready rather than loading more
                const pending = this.loop.pendingOutput ? this.loop.pendingOutput((x) => x === n.item || (n.matches && n.matches(x))) : 0;
                if (pending > 0) {
                    const jobs = this.loop.jobsFor((x) => x === n.item);
                    const left = Math.max(0, ...jobs.map((j) => Math.round((j.readyAt - Date.now()) / 1000)));
                    return { text: `Collect ${Math.min(pending, n.remaining)} ${n.item} from the furnace`, target: { item: n.item, count: (n.have || 0) + Math.min(pending, n.remaining) }, why: `${why}; ${pending} cooking, ready in about ${left}s`, cooking: left };
                }
                const batch = Math.min(n.remaining, FURNACE_MAX_BATCH_PLAN);
                return { text: `Smelt ${batch} ${n.rawInput}`, target: { item: n.item, count: (n.have || 0) + batch }, why };
            }
            case "mine":
                return { text: `Mine ${chunk} ${n.item}`, target: { item: n.item, count: (n.have || 0) + chunk }, why };
            case "explore": {
                // a surface resource wanted from underground: go up, do not wander the caves
                const underground = this.loop.fingerprint().biome === "underground";
                if (underground && SURFACE_FAMILY_RE.test(n.family)) {
                    return { text: `Return to the surface to find ${n.family}`, target: { nearBlock: n.family }, why: `${why}; the bot is underground and ${n.family} is a surface block` };
                }
                return { text: `Explore to find ${n.family}`, target: { nearBlock: n.family }, why };
            }
            case "station": {
                const held = this.inventory[n.block] || 0;
                if (held) return { text: `Place the ${n.block.replace(/_/g, " ")}`, target: { nearBlock: n.block }, why };
                // one was placed before but is out of range: go back to it rather than make another
                const far = this.loop.landmarkDistance ? this.loop.landmarkDistance(n.block) : null;
                if (far !== null && far > 24) {
                    return { text: `Walk back to the ${n.block.replace(/_/g, " ")}`, target: { nearBlock: n.block }, why: `${why}; one stands ${far} blocks away` };
                }
                return null; // its item child is the real step
            }
            case "hunt":
                if (n.item === "family:food" || n.item === "family:cooked_food") {
                    return { text: `Hunt animals (${n.mob}) for ${chunk} food`, target: { item: n.item, count: (n.have || 0) + chunk }, why: `${why}; animals live on the surface in daylight` };
                }
                return { text: `Kill a ${n.mob} for ${chunk} ${n.item}`, target: { item: n.item, count: chunk }, why };
            case "fuel":
            case "tool":
                return null; // their item children are the steps
            case "obtain":
            case "judged":
            default:
                return { text: `Obtain ${chunk} ${n.item}`, target: { item: n.item, count: (n.have || 0) + chunk }, why };
        }
    }

    pathUp(n) {
        // follow parents that belong to the current goal before any that only
        // serve the lookahead, so a shared requirement is explained by the goal
        const names = [];
        let cur = n;
        const seen = new Set();
        while (cur && cur.parents.length && !seen.has(cur.id)) {
            seen.add(cur.id);
            const parents = cur.parents.map((p) => this.nodes.get(p)).filter(Boolean);
            cur = parents.find((p) => p.priority === "now" || p.id === "goal") || parents[0];
            if (cur) names.push(cur.label || cur.item || cur.id);
        }
        return names.length ? `needed for ${names.join(" <- ")}` : "the goal itself";
    }

    summary() {
        const out = {};
        for (const n of this.nodes.values()) {
            out[n.id] = {
                id: n.id,
                kind: n.kind,
                priority: n.priority || null,
                item: n.item,
                need: n.need,
                have: n.have,
                remaining: n.remaining,
                status: n.status,
                depth: n.depth,
                children: n.children,
            };
        }
        return out;
    }
}

function commonSuffix(names) {
    const parts = names.map((n) => n.split("_"));
    const out = [];
    for (let i = 1; ; i++) {
        const seg = parts[0][parts[0].length - i];
        if (seg === undefined || !parts.every((p) => p[p.length - i] === seg)) break;
        out.unshift(seg);
    }
    return out.join("_") || names[0];
}

// Build the plan for a high goal. Returns { target, targets, plan, candidates }.
// `extraTargets` are requirements learned while executing (the collect plugin
// refusing a block for want of a stone pickaxe, a craft refused for missing
// sticks); they are planned as additional children of the goal.
// `lookahead` are the next milestone's targets: planned in the same graph so
// shared requirements aggregate (mine iron for the pickaxe AND the armor in one
// trip), but their leaves are offered after the current goal's own leaves.
async function planGoal(loop, text, override, extraTargets, lookahead) {
    // player-relative targets (near a player, hand items over) are steps the loop runs as-is; recipes do not apply
    const targets = (await parseTargets(loop, text, override)).filter((t) => t && t.kind !== "nearPlayer" && t.kind !== "give");
    for (const t of extraTargets || []) {
        const item = String(t.item || "").toLowerCase().trim();
        if (!item || !loop.mcData.itemsByName[item]) continue;
        if (targets.some((x) => x.key === item)) continue;
        targets.push(await loop.parseTarget(item, { item, count: Math.max(1, parseInt(t.count, 10) || 1) }));
    }
    const later = [];
    for (const spec of lookahead || []) {
        if (!spec || spec.none || spec.judged) continue;
        const t = await loop.parseTarget("", spec);
        if (t && !targets.some((x) => x.key === t.key)) later.push(t);
    }
    const planner = new Planner(loop);
    if (!targets.length) {
        return {
            target: null,
            targets: [],
            plan: {},
            candidates: [{ text, target: override || { none: true }, why: "the goal itself (no item target could be read from it)" }],
        };
    }
    const root = planner.node("goal", { kind: "root", item: null, label: text, depth: 0, need: 1 });
    const laterRoot = later.length ? planner.node("later", { kind: "root", item: null, label: "the next milestone", depth: 0, need: 1, later: true }) : null;
    for (const t of later) {
        const key = t.kind === "nearBlock" ? `near:${t.block}` : t.key;
        if (t.kind === "nearBlock" && isPlaceable(loop.mcData, t.block)) {
            const station = planner.node(`station:${t.block}`, { kind: "station", block: t.block, item: null, need: 1 });
            planner.link(laterRoot, station);
            station.depth = Math.max(station.depth || 0, 1);
            const stationItem = planner.node(t.block, { item: t.block, kind: "obtain" });
            planner.link(station, stationItem);
            planner.expand(stationItem, 2, new Set(["later"]));
            continue;
        }
        const node = planner.node(key, {
            item: t.key,
            kind: t.kind === "nearBlock" ? "explore" : "obtain",
            matches: t.kind === "nearBlock" || t.key.startsWith("*_") || t.key.startsWith("family:") ? t.matches : undefined,
            blocks: t.kind === "nearBlock" ? Object.keys(loop.mcData.blocksByName).filter((b) => t.matches(b)) : undefined,
            family: t.kind === "nearBlock" ? t.block : undefined,
        });
        node.need += t.need;
        planner.link(laterRoot, node);
        planner.expand(node, 1, new Set(["later"]));
    }
    if (laterRoot) laterRoot.expanded = true;
    for (const t of targets) {
        const key = t.kind === "nearBlock" ? `near:${t.block}` : t.key;
        if (t.kind === "freeSlots") {
            const space = planner.node("space", { kind: "space", item: null, need: 1 });
            planner.link(root, space);
            space.depth = 1;
            space.expanded = true;
            continue;
        }
        if (t.kind === "nearBlock" && isPlaceable(loop.mcData, t.block)) {
            // a crafting table, furnace or chest "nearby" is placed, never searched for
            const station = planner.node(`station:${t.block}`, { kind: "station", block: t.block, item: null, need: 1 });
            planner.link(root, station);
            station.depth = Math.max(station.depth || 0, 1);
            const stationItem = planner.node(t.block, { item: t.block, kind: "obtain" });
            planner.link(station, stationItem);
            planner.expand(stationItem, 2, new Set(["goal"]));
            continue;
        }
        const node = planner.node(key, {
            item: t.key,
            kind: t.kind === "nearBlock" ? "explore" : "obtain",
            matches: t.kind === "nearBlock" || t.key.startsWith("*_") || t.key.startsWith("family:") ? t.matches : undefined,
            blocks: t.kind === "nearBlock" ? Object.keys(loop.mcData.blocksByName).filter((b) => t.matches(b)) : undefined,
            family: t.kind === "nearBlock" ? t.block : undefined,
        });
        node.need += t.need;
        // high goals count what is held; the root's own baseline is the inventory
        planner.link(root, node);
        planner.expand(node, 1, new Set(["goal"]));
    }
    root.expanded = true;
    planner.quantify();
    root.status = root.children.every((c) => planner.nodes.get(c).status === "satisfied") ? "satisfied" : "blocked";

    const rank = (n) => (n.priority === "later" ? 0 : 1000) + (n.depth || 0) - (n.kind === "smelt" && planner.loop.pendingOutput && planner.loop.pendingOutput((x) => x === n.item) > 0 ? 500 : 0);
    const ready = [...planner.nodes.values()]
        .filter((n) => n.status === "ready" && n.id !== "goal" && n.id !== "later")
        .sort((a, b) => rank(b) - rank(a)); // current goal first, deepest first: the focus
    const candidates = [];
    const seen = new Set();
    for (const n of ready) {
        const c = planner.candidate(n);
        if (!c || seen.has(c.text)) continue;
        seen.add(c.text);
        candidates.push(c);
        if (candidates.length >= MAX_CANDIDATES) break;
    }
    for (const c of candidates) {
        const node = [...planner.nodes.values()].find((n) => planner.candidate(n) && planner.candidate(n).text === c.text);
        if (node && node.priority === "later") c.why = `${c.why}; for an upcoming milestone, not the current goal`;
    }
    if (!candidates.length && root.status !== "satisfied") {
        // everything blocked by something unexpandable: offer the goal directly
        const t = targets[0];
        candidates.push({ text, target: { item: t.key, count: t.need }, why: "work on the high goal directly" });
    }
    const first = targets[0];
    const target = loop.summarise(first);
    if (targets.length > 1 && target) {
        // report the whole set: satisfied when every piece is held
        const allHave = targets.every((t) => loop.summarise(t).have >= t.need);
        target.item = targets.map((t) => t.key).join("+");
        target.need = targets.reduce((s, t) => s + t.need, 0);
        target.have = targets.reduce((s, t) => s + Math.min(t.need, loop.summarise(t).have), 0);
        target.gained = allHave ? target.need : target.have;
        target.pieces = targets.map((t) => ({ item: t.key, need: t.need, have: loop.summarise(t).have }));
    }
    return {
        target,
        targets: targets.map((t) => ({ item: t.key, need: t.need })),
        lookahead: later.map((t) => ({ item: t.key, need: t.need })),
        plan: planner.summary(),
        candidates,
    };
}

module.exports = { planGoal, parseTargets, Planner };
