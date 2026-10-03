"""The milestone ladder: the long horizon the fast brain plans along.

Instead of a curriculum model proposing "diverse" tasks, fast mode works up a
fixed ladder of milestones toward beating the game. Every milestone is a set
of **verifiable targets** (item counts, a block nearby, the current
dimension, or a judged statement), so the planner can expand it, the fast
loop can check it exactly, and the skill tree can record it as a node.

Two things make this "planning ahead" rather than task-at-a-time:

* **Lookahead.** The next milestone's targets are planned together with the
  current one, so shared requirements aggregate (iron for the pickaxe and the
  armor is mined in one trip) and the bot prefers steps that serve both.
* **Recurring kit milestones.** "Carry 16 logs, 8 food, a spare pickaxe" is
  checked before anything else and becomes unmet again as supplies are used,
  so the bot restocks on the surface instead of running dry underground.

Targets use the fast loop's override form:
    {"item": "iron_ingot", "count": 24}      item count held (families: "family:food", "*_log")
    {"nearBlock": "nether_portal"}           a block of that kind within scan range
    {"dimension": "the_nether"}              the bot is in that dimension
    {"judged": "the ender dragon is dead"}   decided by a Jev Noul when the loop reports it
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Milestone:
    key: str
    name: str
    description: str
    targets: list[dict[str, Any]]
    recurring: bool = False  # re-arms when its targets stop holding (a kit)
    notes: str = ""  # hints for Jev and the plan printout

    def label(self) -> str:
        return self.name


LADDER: list[Milestone] = [
    Milestone(
        "wood", "Wood",
        "Punch trees for logs; everything starts here.",
        [{"item": "*_log", "count": 8}],
    ),
    Milestone(
        "wooden_tools", "Wooden tools",
        "Planks, sticks, a crafting table and a wooden pickaxe.",
        [{"item": "crafting_table", "count": 1}, {"item": "wooden_pickaxe", "count": 1}],
    ),
    Milestone(
        "home", "Home base",
        "Build the home base: place a crafting table, a furnace and a chest next to each other on the surface. "
        "Craft whichever is missing first (a chest needs 8 planks, a furnace 8 cobblestone).",
        [{"nearBlock": "crafting_table"}, {"nearBlock": "furnace"}, {"nearBlock": "chest"}],
        notes="Place the three blocks next to each other; this spot becomes 'home' and return:home leads back to it.",
    ),
    Milestone(
        "kit", "Basic kit",
        "Carry wood, food, torches and cobblestone before going underground; restock on the surface when low.",
        [
            {"item": "*_log", "count": 16},
            {"item": "family:food", "count": 8},
            {"item": "torch", "count": 16},
            {"item": "cobblestone", "count": 32},
            {"item": "family:pickaxe", "count": 2},
            {"freeSlots": 8},
        ],
        recurring=True,
        notes="Logs and food come from the surface: go up before they run out. Junk goes in the home chest or on the ground.",
    ),
    Milestone(
        "stone_tools", "Stone tools",
        "Cobblestone and a full stone toolset.",
        [{"item": "stone_pickaxe", "count": 1}, {"item": "stone_axe", "count": 1}, {"item": "stone_sword", "count": 1}],
    ),
    Milestone(
        "furnace", "Furnace and coal",
        "A furnace placed and fuel to run it.",
        [{"nearBlock": "furnace"}, {"item": "coal", "count": 16}],
    ),
    Milestone(
        "iron_tools", "Iron tools",
        "Iron pickaxe, sword and axe, a shield and a bucket.",
        [
            {"item": "iron_pickaxe", "count": 1}, {"item": "iron_sword", "count": 1},
            {"item": "iron_axe", "count": 1}, {"item": "shield", "count": 1}, {"item": "bucket", "count": 1},
        ],
    ),
    Milestone(
        "iron_armor", "Iron armor",
        "A full set of iron armor, worn.",
        [
            {"item": "iron_helmet", "count": 1}, {"item": "iron_chestplate", "count": 1},
            {"item": "iron_leggings", "count": 1}, {"item": "iron_boots", "count": 1},
        ],
        notes="Wear each piece once crafted (equip options appear in the action menu).",
    ),
    Milestone(
        "diamonds", "Diamonds",
        "Diamonds from deep underground (y below 16), then a diamond pickaxe.",
        [{"item": "diamond", "count": 8}, {"item": "diamond_pickaxe", "count": 1}],
        notes="Mine at y 16 and below in a straight tunnel; bring the kit.",
    ),
    Milestone(
        "diamond_gear", "Diamond gear",
        "Diamond sword and armor for the nether.",
        [
            {"item": "diamond_sword", "count": 1}, {"item": "diamond_chestplate", "count": 1},
            {"item": "diamond_leggings", "count": 1}, {"item": "diamond_boots", "count": 1}, {"item": "diamond_helmet", "count": 1},
        ],
    ),
    Milestone(
        "portal", "Nether portal",
        "Ten obsidian (water on lava, or mined with a diamond pickaxe), flint and steel, a lit portal.",
        [{"item": "obsidian", "count": 10}, {"item": "flint_and_steel", "count": 1}, {"nearBlock": "nether_portal"}],
        notes="Obsidian forms where water meets a lava source; a bucket of water makes it from lava pools underground.",
    ),
    Milestone(
        "nether", "Enter the nether",
        "Step through the portal.",
        [{"dimension": "the_nether"}],
    ),
    Milestone(
        "blaze", "Blaze rods",
        "Find a nether fortress and kill blazes for rods.",
        [{"item": "blaze_rod", "count": 7}],
        notes="Fortresses are long dark-brick structures; blazes spawn at their spawners.",
    ),
    Milestone(
        "pearls", "Ender pearls",
        "Ender pearls from endermen (overworld at night, or warped forests in the nether).",
        [{"item": "ender_pearl", "count": 12}],
    ),
    Milestone(
        "eyes", "Eyes of ender",
        "Blaze powder plus ender pearls make eyes of ender.",
        [{"item": "ender_eye", "count": 12}],
    ),
    Milestone(
        "stronghold", "Stronghold",
        "Throw eyes of ender to locate the stronghold and find the end portal room.",
        [{"nearBlock": "end_portal_frame"}],
        notes="Eyes fly toward the stronghold; dig down where they hover.",
    ),
    Milestone(
        "end", "The End",
        "Fill the portal frame with eyes and enter the End.",
        [{"dimension": "the_end"}],
    ),
    Milestone(
        "dragon", "Ender dragon",
        "Destroy the end crystals and kill the dragon.",
        [{"judged": "the ender dragon has been defeated and the exit portal is active"}],
    ),
]


def target_label(t: dict[str, Any]) -> str:
    if t.get("item"):
        return f"{t['count']} {t['item']}"
    if t.get("nearBlock"):
        return f"a {t['nearBlock']} nearby"
    if t.get("dimension"):
        return f"be in {t['dimension']}"
    if t.get("freeSlots"):
        return f"{t['freeSlots']} free inventory slots"
    if t.get("judged"):
        return t["judged"]
    return str(t)


class Ladder:
    """Order of play over the milestones, with per-milestone skip cooldowns."""

    def __init__(self, milestones: list[Milestone] | None = None):
        self.milestones = milestones or LADDER
        self.skipped_until: dict[str, int] = {}  # milestone key -> subgoals_completed when it may be retried

    def by_key(self, key: str) -> Milestone | None:
        return next((m for m in self.milestones if m.key == key), None)

    def next_unmet(
        self, satisfied: dict[str, list[bool]], subgoals_completed: int, reached: set[str] | None = None
    ) -> tuple[Milestone | None, list[dict]]:
        """The milestone to work on: an unmet active kit first, else the next rung.

        ``satisfied`` maps milestone key to per-target booleans from the fast
        loop's exact checks. ``reached`` holds keys of non-recurring milestones
        recorded in the skill tree: once reached they stay reached even when the
        logs that proved it are later used up (the kits handle restocking). The
        ladder is strict: the next rung is the first non-recurring milestone not
        reached, never a later one. A rung set aside after a failure yields to an
        unmet kit (restock, then retry) and is otherwise simply retried.
        """
        reached = reached or set()
        done = lambda m: m.key in reached or (satisfied.get(m.key) and all(satisfied[m.key]))
        stepped_past = False
        for kit in (m for m in self.milestones if m.recurring and self._kit_relevant(m, satisfied, reached)):
            flags = satisfied.get(kit.key) or [False] * len(kit.targets)
            unmet = [t for t, ok in zip(kit.targets, flags) if not ok]
            if unmet:
                return kit, unmet
        for m in self.milestones:
            if m.recurring or done(m):
                continue
            flags = satisfied.get(m.key) or [False] * len(m.targets)
            unmet = [t for t, ok in zip(m.targets, flags) if not ok] or list(m.targets)
            until = self.skipped_until.get(m.key)
            if until is not None and subgoals_completed < until and not stepped_past:
                # after repeated failures the ladder may step exactly one rung past
                # the stuck one, then comes back to it
                stepped_past = True
                continue
            return m, unmet
        return None, []

    def _kit_relevant(self, kit: Milestone, satisfied: dict[str, list[bool]], reached: set[str] | None = None) -> bool:
        reached = reached or set()
        for m in self.milestones:
            if m.key == kit.key:
                return True
            if m.recurring:
                continue
            flags = satisfied.get(m.key)
            if not (m.key in reached or (flags and all(flags))):
                return False
        return True

    def skip(self, key: str, subgoals_completed: int, for_subgoals: int) -> None:
        self.skipped_until[key] = subgoals_completed + for_subgoals

    def upcoming(self, current: Milestone, satisfied: dict[str, list[bool]], n: int = 1, reached: set[str] | None = None) -> list[dict]:
        """Unmet targets of the next ``n`` non-recurring milestones after ``current``."""
        reached = reached or set()
        out: list[dict] = []
        seen_current = current.recurring  # a kit looks ahead to the first unmet real milestone
        for m in self.milestones:
            if m.recurring:
                continue
            if not seen_current:
                if m.key == current.key:
                    seen_current = True
                continue
            if m.key in reached:
                continue
            flags = satisfied.get(m.key) or [False] * len(m.targets)
            unmet = [t for t, ok in zip(m.targets, flags) if not ok and not t.get("judged") and not t.get("dimension") and not t.get("freeSlots")]
            if unmet:
                out.extend(unmet)
                n -= 1
                if n <= 0:
                    break
        return out

    def progress(self, satisfied: dict[str, list[bool]], reached: set[str] | None = None) -> str:
        reached = reached or set()
        done = [m.key for m in self.milestones if not m.recurring and (m.key in reached or (satisfied.get(m.key) and all(satisfied[m.key])))]
        return f"{len(done)}/{sum(1 for m in self.milestones if not m.recurring)} milestones: {', '.join(done) or 'none yet'}"
