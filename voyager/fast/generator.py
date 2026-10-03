"""ActionGenerator: fill an action gap with a new, code-verified primitive.

When a subgoal keeps stalling and the recent actions repeat the same few
verbs, the bot may simply lack an action that fits. This module, running
beside the brain's poll loop:

1. asks Jev whether the stall looks like an action gap (a Noul over the
   subgoal, the recent actions and outcomes, and the verbs on offer);
2. asks GPT, in a worker thread, for one mineflayer primitive that follows the
   fast loop's module contract (a synchronous ``menu(snap, loop)`` and an
   ``async execute(bot, loop, ctx)``);
3. runs the same static checks Node applies, posts the source to the bot
   process (``POST /fast/primitive``), and retries once with Node's exact error;
4. saves accepted sources under ``<ckpt>/fast/primitives`` so they are
   registered again on resume.

Node offers an accepted primitive as ``x:<name>`` with a trial note, measures
its outcome like any other action, and retires it after three failures with
no success. Jev never sees code, only the menu line the primitive writes.
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from typing import Any

import voyager.utils as U
from voyager.utils import timing

try:
    from typesafe_sdk import Noul
except Exception:  # the client is disabled in this case
    Noul = None  # type: ignore[assignment]

MAX_SOURCE_CHARS = 12000
FORBIDDEN = [
    (re.compile(r"\bprocess\s*\."), "process access"),
    (re.compile(r"child_process"), "child_process"),
    (re.compile(r"\brequire\s*\(\s*[\"'`]fs[\"'`]"), "fs access"),
    (re.compile(r"\beval\s*\("), "eval"),
    (re.compile(r"\bFunction\s*\("), "Function constructor"),
    (re.compile(r"\bbot\s*\.\s*chat\s*\("), "bot.chat"),
    (re.compile(r"\bbot\s*\.\s*(end|quit)\s*\("), "bot.end/quit"),
    (re.compile(r"\bsetInterval\s*\("), "setInterval"),
    (re.compile(r"\bimport\s*\("), "dynamic import"),
]

CONTRACT = """\
Write ONE JavaScript module (no exports, no require of anything but "vec3",
"mineflayer-pathfinder" or "minecraft-data") that defines exactly:

    function menu(snap, loop)
        SYNCHRONOUS. Return a one-line description with live facts (distances,
        counts) when this action is worth offering right now, otherwise null.
        `snap` has: position {x,y,z}, health, food, inventory {name: count},
        resources {blockName: {count, nearest}}, entities [{name, distance,
        direction}] where direction is a COMPASS WORD such as "north" (not a
        vector; to reach an entity use loop.nearestEntity and its .position),
        landmarks {crafting_table|furnace|chest: {x,y,z,distance}},
        target {item, need, have} or null (need is the total to hold),
        heldItem, inLava, inWater.
        `loop` has helpers: loop.mcData, loop.inventoryCounts(), loop.freeSlots(),
        loop.nearestBlocks(matches, maxDistance, count) where `matches` is a
        function of a BLOCK NAME STRING (e.g. (name) => name === "sand") and the
        result is an array of blocks nearest first, each with .name and .position
        (a Vec3; use .distanceTo(bot.entity.position) for distance),
        loop.playerEntity(name), loop.homeDistance(), loop.landmarks,
        loop.nearestEntity(["cow", "pig"], maxDistance) -> the live entity (with
        .position, .id) or null, loop.freeSpotNear(pos, radius) -> a Vec3 of an
        air block with solid ground under it, or null.
        To test solidity use bot.blockAt(pos).boundingBox === "block".
        mineflayer calls return PROMISES: `await bot.toss(id, null, n)`,
        `await bot.dig(block)`, `await bot.equip(item, "hand")`,
        `await bot.placeBlock(refBlock, faceVec3)`, `await bot.attack(entity)`.
        Never pass a callback to them (it is ignored and the call never resolves).
        Do not call bot.pathfinder.setMovements; the loop's movements already
        allow swimming, digging and horizontal bridging.

    async function execute(bot, loop, ctx)
        Do ONE bounded thing (it is cut off after 20 seconds). Return a string
        starting with "ok" on success (say what happened), otherwise
        "failed: <why>". `ctx` has snap, target, goal. Use bot.pathfinder.goto
        with `goals` (GoalNear, GoalBlock, GoalLookAtBlock, GoalFollow are in
        scope), bot.dig, bot.placeBlock, bot.equip, bot.lookAt, bot.toss,
        bot.setControlState. Every loop body must contain an await. Never call
        bot.chat. Do not define helpers outside these two functions except
        small pure ones.

In scope: bot, loop, mcData, Vec3, goals, Movements, require (restricted).
Answer with the module in one ```javascript code block and nothing else.
"""

EXAMPLE = """\
```javascript
// Example of the style: swim to the nearest shore when the bot is in water.
function menu(snap, loop) {
    if (!snap.inWater) return null;
    return `Swim toward the nearest solid ground (bot is in water at y=${snap.position.y})`;
}
async function execute(bot, loop, ctx) {
    const here = bot.entity.position;
    let best = null;
    for (let r = 2; r <= 12 && !best; r += 2) {
        await new Promise((res) => setTimeout(res, 0));
        for (const [dx, dz] of [[r, 0], [-r, 0], [0, r], [0, -r]]) {
            const p = here.offset(dx, 0, dz).floored();
            const b = bot.blockAt(p.offset(0, -1, 0));
            if (b && b.boundingBox === "block") { best = p; break; }
        }
    }
    if (!best) return "failed: no shore within 12 blocks";
    await bot.pathfinder.goto(new goals.GoalNear(best.x, best.y, best.z, 1));
    return `ok, reached ground at x=${best.x} z=${best.z}`;
}
```
"""


def static_check(name: str, source: str) -> str | None:
    """Mirror of Node's checks so a bad module never leaves Python."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", name or ""):
        return "name must be lowercase [a-z0-9_], 2-32 chars"
    if not source or not source.strip():
        return "source is empty"
    if len(source) > MAX_SOURCE_CHARS:
        return f"source longer than {MAX_SOURCE_CHARS} chars"
    for rx, why in FORBIDDEN:
        if rx.search(source):
            return f"forbidden: {why}"
    if not re.search(r"\bfunction\s+menu\s*\(", source):
        return "missing `function menu(snap, loop)`"
    if re.search(r"\basync\s+function\s+menu\s*\(", source):
        return "menu() must be synchronous"
    if not re.search(r"\basync\s+function\s+execute\s*\(", source):
        return "missing `async function execute(bot, loop, ctx)`"
    for m in re.finditer(r"\b(while|for)\s*\([^)]*\)\s*\{", source):
        depth, i = 1, m.end()
        start = i
        while i < len(source) and depth:
            depth += source[i] == "{"
            depth -= source[i] == "}"
            i += 1
        if "await" not in source[start : i - 1]:
            return f"{m.group(1)} loop without await"
    return None


def extract_module(reply: str) -> tuple[str | None, str | None]:
    """(name, source) from a GPT reply: the first code block, name from a leading comment or derived."""
    m = re.search(r"```(?:javascript|js)?\s*\n(.*?)```", reply, re.S)
    source = (m.group(1) if m else reply).strip()
    if not source:
        return None, None
    name = None
    head = re.search(r"//\s*name\s*:\s*([a-z][a-z0-9_]{1,31})", source)
    if head:
        name = head.group(1)
    return name, source


class ActionGenerator:
    def __init__(
        self,
        brain,
        *,
        enabled: bool = True,
        stalls_before: int = 2,
        max_live: int = 8,
        cooldown_seconds: float = 600,
        gap_threshold: float = 0.65,
    ):
        self.brain = brain
        self.enabled = enabled
        self.stalls_before = stalls_before
        self.max_live = max_live
        self.cooldown_seconds = cooldown_seconds
        self.gap_threshold = gap_threshold
        self.messages: "queue.Queue[str]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._last_attempt: dict[str, float] = {}  # subgoal text -> time
        self._stalls: dict[str, int] = {}
        self.dir = f"{brain.v.ckpt_dir}/fast/primitives"
        U.f_mkdir(self.dir)
        self.registered: dict[str, str] = {}  # name -> source (accepted this run or reloaded)

    # ------------------------------------------------------------------ #
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def drain(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self.messages.get_nowait())
            except queue.Empty:
                return out

    def reload(self) -> int:
        """Register every saved primitive again (after a restart or resume)."""
        n = 0
        for fn in sorted(os.listdir(self.dir)) if os.path.isdir(self.dir) else []:
            if not fn.endswith(".js"):
                continue
            name = fn[:-3]
            source = open(os.path.join(self.dir, fn), encoding="utf-8").read()
            if static_check(name, source):
                continue
            try:
                res = self.brain.env.fast_primitive(name, source)
            except Exception as exc:
                print(f"\033[31mAction generator: could not re-register {name}: {exc}\033[0m")
                continue
            if res.get("ok"):
                self.registered[name] = source
                n += 1
            else:
                print(f"\033[33mAction generator: saved primitive {name} rejected: {res.get('error')}\033[0m")
        if n:
            print(f"\033[36mAction generator: re-registered {n} saved action(s)\033[0m")
        return n

    # ------------------------------------------------------------------ #
    def on_stall(self, high, subgoal, status, menu_verbs: list[str] | None = None) -> bool:
        """Called on a no_progress trigger. Starts a generation when the stall looks like an action gap."""
        if not self.enabled or self.busy:
            return False
        text = subgoal["text"]
        self._stalls[text] = self._stalls.get(text, 0) + 1
        if self._stalls[text] < self.stalls_before:
            return False
        now = time.time()
        if now - self._last_attempt.get(text, 0) < self.cooldown_seconds:
            return False
        live = [p for p in (status.get("primitives") or []) if not p.get("retired")]
        if len(live) >= self.max_live:
            self.messages.put(f"action gap on '{text}' but {len(live)} generated actions are live already; not generating")
            return False
        recent = status.get("recentActions") or []
        verbs = sorted({str(r.get("action", "")).split(":")[0] for r in recent if r.get("action")})
        gap = self._judge_gap(high, subgoal, status, recent, verbs, menu_verbs or status.get("menuVerbs") or [])
        if not gap:
            return False
        self._last_attempt[text] = now
        self._thread = threading.Thread(
            target=self._generate, args=(high, subgoal, status, recent), name="action-generator", daemon=True
        )
        self._thread.start()
        self.messages.put(f"action gap on '{text}' (verbs tried: {', '.join(verbs) or 'none'}); asking GPT for a new action")
        return True

    def _judge_gap(self, high, subgoal, status, recent, verbs, menu_verbs) -> bool:
        jev = self.brain.v.jev
        if not jev.enabled or Noul is None:
            # no Jev: a stall that only ever used one or two verbs is treated as a gap
            return len(verbs) <= 2
        state = {
            "high_goal": high["text"],
            "subgoal": subgoal["text"],
            "target": status.get("target"),
            "recent_actions": recent,
            "outcomes_by_action": self._outcomes_by_action(recent),
            "surroundings": status.get("fingerprint") or {},
            "verbs_tried": verbs,
            "actions_available": menu_verbs,
            "inventory": status.get("inventory") or {},
            "generated_actions_live": [p["name"] for p in (status.get("primitives") or []) if not p.get("retired")],
        }
        response = jev.ask(
            "action_gap",
            state,
            {
                "gap": Noul(
                    instructions=(
                        "A Minecraft bot keeps stalling on `subgoal` (part of `high_goal`); `recent_actions` "
                        "shows what it tried and how each ended and `outcomes_by_action` tallies them; "
                        "`actions_available` lists every action kind it can choose from. Is the stall best "
                        "explained by a MISSING ACTION, one that no available action kind can do (for example "
                        "crossing water, reaching a block the pathfinder cannot route to, using an item, "
                        "opening something), rather than by the world (nothing to mine here), by an existing "
                        "action that merely failed (a timeout or 'no item gained' on an action that fits the "
                        "job and the terrain is NOT a missing action), or by picking badly among actions "
                        "that could work? `surroundings` gives the biome: when the terrain itself defeats "
                        "every tried action (walks time out in an ocean, the target is across water or "
                        "up a cliff the pathfinder cannot climb), that IS a missing action."
                    )
                )
            },
            extra_record={"subgoal": subgoal["text"]},
        )
        if response is None:
            return False
        try:
            noul = response.nouls["gap"].noul
        except Exception:
            return False
        return noul >= self.gap_threshold

    @staticmethod
    def _outcomes_by_action(recent) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in recent or []:
            a = str(r.get("action") or "")
            o = str(r.get("outcome") or "")[:40]
            if not a:
                continue
            out.setdefault(a, {})
            out[a][o] = out[a].get(o, 0) + 1
        return out

    # ------------------------------------------------------------------ #
    # outcomes of generated actions: log them, repair once before retirement
    # ------------------------------------------------------------------ #
    def on_outcomes(self, outcomes, status) -> None:
        for o in outcomes or []:
            name = o.get("name")
            if not name:
                continue
            self.brain.v.jev.log_only("action_outcome", {**o, "subgoal": o.get("goal")})
            self.messages.put(f"generated action x:{name}: {o.get('outcome')} ({'ok' if o.get('ok') else 'failed'}, {o.get('ms', 0)} ms)")
            if o.get("ok") or not self.enabled or self.busy:
                continue
            stats = next((p for p in (status.get("primitives") or []) if p.get("name") == name), None)
            if stats is None or stats.get("successes") or stats.get("repaired") or stats.get("retired"):
                continue
            source = self.registered.get(name)
            if not source:
                continue
            self._thread = threading.Thread(target=self._repair, args=(name, source, o, status), name="action-repair", daemon=True)
            self._thread.start()
            self.messages.put(f"generated action x:{name} failed ('{o.get('outcome')}'); asking GPT to repair it once before it is retired")

    def _repair(self, name, source, outcome, status) -> None:
        llm = getattr(self.brain.v.action_agent, "llm", None)
        if llm is None:
            return
        from langchain.schema import HumanMessage, SystemMessage

        prompt = (
            f"This mineflayer primitive (registered as x:{name}) ran and returned: {outcome.get('outcome')!r} "
            f"after {outcome.get('ms', 0)} ms with no progress.\n"
            f"Bot inventory now: {status.get('inventory') or {}}\nTarget: {status.get('target')}\n"
            f"Recent actions and outcomes: {status.get('recentActions') or []}\n\n"
            "Find the bug and answer with the full corrected module (same name, same contract). "
            "Common causes: passing a callback to a mineflayer call that returns a promise, treating "
            "an entity's compass `direction` word as a vector, placing or digging a block out of reach, "
            "an unbounded pathfinder goto (give it a nearby goal and expect it to throw on no path).\n\n"
            + CONTRACT
            + "\nThe module to fix:\n```javascript\n"
            + source
            + "\n```"
        )
        try:
            with timing.timed("generator.repair"):
                reply = llm([SystemMessage(content="You fix small mineflayer primitives."), HumanMessage(content=prompt)]).content
        except Exception as exc:
            self.messages.put(f"action repair: GPT call failed: {exc}")
            return
        _, fixed = extract_module(str(reply))
        if not fixed:
            self.messages.put(f"action repair: no code block for x:{name}")
            return
        err = static_check(name, fixed)
        if err:
            self.messages.put(f"action repair: x:{name} failed static check ({err})")
            return
        try:
            res = self.brain.env.fast_primitive(name, fixed, repaired=True)
        except Exception as exc:
            self.messages.put(f"action repair: could not post x:{name}: {exc}")
            return
        if res.get("ok"):
            with open(os.path.join(self.dir, f"{name}.js"), "w", encoding="utf-8") as fh:
                fh.write(fixed)
            self.registered[name] = fixed
            self.brain.v.jev.log_only("action_repaired", {"name": name, "outcome": outcome.get("outcome"), "source": fixed})
            self.messages.put(f"generated action x:{name} repaired and re-registered; it keeps its remaining trials")
        else:
            self.messages.put(f"action repair: Node rejected x:{name}: {res.get('error')}")

    # ------------------------------------------------------------------ #
    def _generate(self, high, subgoal, status, recent) -> None:
        llm = getattr(self.brain.v.action_agent, "llm", None)
        if llm is None:
            self.messages.put("action generator: no GPT handle on the action agent")
            return
        from langchain.schema import HumanMessage, SystemMessage

        existing = [p["name"] for p in (status.get("primitives") or [])]
        prompt = (
            f"Stalled subgoal: {subgoal['text']}\n"
            f"High goal: {high['text']}\n"
            f"Target progress: {status.get('target')}\n"
            f"Surroundings: {(status.get('fingerprint') or {})}\n"
            f"Inventory: {status.get('inventory') or {}}\n"
            f"Landmarks: {status.get('landmarks') or {}}\n"
            f"Recent actions and outcomes: {recent}\n"
            f"Outcome tallies by action: {self._outcomes_by_action(recent)}\n"
            f"Action kinds already available: walk, climb, dig, mine, craft, smelt, furnace, place, surface, "
            f"attack, eat, collect, pillar, equip, chest, return, goto(player), give, bridge, discard, withdraw"
            f"{', ' + ', '.join('x:' + n for n in existing) if existing else ''}\n\n"
            "Design ONE new action that would let the bot make progress here. It must do something the kinds "
            "above cannot: read the outcome tallies, work out WHY the tried actions failed (a timeout on mine "
            "usually means the block is unreachable by walking; 'no item gained' means the drop was not picked "
            "up), and build the action that removes that obstacle. Do not re-implement an existing kind. "
            "Start the module with a comment `// name: <lowercase_name>` and a comment saying when menu() offers it.\n\n"
            + CONTRACT
            + "\nExample of the expected style:\n"
            + EXAMPLE
        )
        error_feedback = None
        for attempt in range(2):
            messages = [SystemMessage(content="You write small, careful mineflayer primitives."), HumanMessage(content=prompt)]
            if error_feedback:
                messages.append(HumanMessage(content=f"The previous module was rejected: {error_feedback}. Fix it and answer with the full module again."))
            try:
                with timing.timed("generator.gpt"):
                    reply = llm(messages).content
            except Exception as exc:
                self.messages.put(f"action generator: GPT call failed: {exc}")
                return
            name, source = extract_module(str(reply))
            if not source:
                error_feedback = "no code block found"
                continue
            if not name:
                name = re.sub(r"[^a-z0-9_]", "_", subgoal["text"].lower())[:24].strip("_") or "action"
                name = f"gen_{name}"[:32]
            err = static_check(name, source)
            if err:
                error_feedback = err
                self.messages.put(f"action generator: '{name}' failed static check ({err}); retrying" if attempt == 0 else f"action generator: gave up on '{name}': {err}")
                continue
            try:
                res = self.brain.env.fast_primitive(name, source)
            except Exception as exc:
                self.messages.put(f"action generator: could not post '{name}': {exc}")
                return
            if res.get("ok"):
                path = os.path.join(self.dir, f"{name}.js")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(source)
                self.registered[name] = source
                self.brain.v.jev.log_only("action_generated", {"name": name, "subgoal": subgoal["text"], "high": high["text"], "source": source})
                self.messages.put(f"new action x:{name} registered for '{subgoal['text']}' (saved to {path}); it is on the menu as a trial")
                return
            error_feedback = res.get("error") or "rejected by the bot process"
            self.messages.put(f"action generator: Node rejected '{name}': {error_feedback}" + ("; retrying" if attempt == 0 else ""))
        self.brain.v.jev.log_only("action_generation_failed", {"subgoal": subgoal["text"], "error": error_feedback})
