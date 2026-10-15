"""Post-goal review: turn one goal's raw decision records into lessons and remedies.

After each goal, one fresh-context GPT call per lesson family reads only what
happened (the decision records Node wrote for that goal, the tree's outcome, the
existing lesson file for the family) and rewrites that family's lesson file. It
also proposes remedies and judges the existing ones against the new evidence.

Sources (see .claude/research/):
* AgentGarten: after each round an agent writes a playbook recording "what it
  tried, what it observed, what it still doubts, and what to test next"; an
  inherited playbook is "another attempt's claims, not ground truth"; lessons
  carry evidence IDs and are tested before they are passed on.
* EmbodiedCLBench: a Markdown skill summary of at most 1000 words, injected as
  "reference guidance", with the current state remaining the source of truth.
* MineEvolve: structured remedies ``{failure_type, repair_action, scope}``
  outperformed free-text reflection.
* Learn2Play: distilled rules can harden one failure into a wrong permanent
  rule, so raw traces stay authoritative and every claim cites its evidence.

Files, all under ``<library>/lessons/``:
* ``<family>.md``: the current lesson for that family (find, acquire, craft,
  place, use, goal), sections Tried / Observed / Still doubt / Test next;
* ``history/<family>.<timestamp>.md``: every earlier version;
* ``remedies.json``: remedies with status ``hypothesis`` (fewer than two goals
  support it), ``confirmed`` (supported by two or more goals, never
  contradicted), or ``disputed`` (contradicted twice).

The builder reads one family file and the matching remedies (``LessonBook.for_builder``);
the curriculum reads the "test next" items (``LessonBook.test_next``). Jev never
sees any of it.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from typing import Any, Callable

from voyager.tree.experience import family_of
from voyager.utils import timing

SECTIONS = ("Tried", "Observed", "Still doubt", "Test next")
MAX_WORDS = 1000
MAX_RECORDS_PER_FAMILY = 80
FAMILIES_PER_REVIEW = 4


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def _section(md: str, title: str) -> list[str]:
    """Bullet lines under '## <title>' in a lesson file."""
    m = re.search(rf"^##\s*{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", md or "", re.M | re.S | re.I)
    if not m:
        return []
    return [ln.strip()[2:].strip() for ln in m.group(1).splitlines() if ln.strip().startswith(("- ", "* "))]


class LessonBook:
    """Read and write the lesson files and the remedy store."""

    def __init__(self, root: str):
        self.dir = os.path.join(root, "lessons")
        self.history = os.path.join(self.dir, "history")
        os.makedirs(self.history, exist_ok=True)
        self.remedies_path = os.path.join(self.dir, "remedies.json")

    # ---- lessons ---------------------------------------------------------- #
    def path(self, family: str) -> str:
        return os.path.join(self.dir, f"{family}.md")

    def read(self, family: str) -> str:
        p = self.path(family)
        if not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    def write(self, family: str, markdown: str, *, goal: str, goal_id: str | None) -> None:
        old = self.read(family)
        if old:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            with open(os.path.join(self.history, f"{family}.{stamp}.md"), "w", encoding="utf-8") as fh:
                fh.write(old)
        header = (
            f"<!-- lesson family: {family}; last reviewed after goal '{goal}' ({goal_id}) on "
            f"{time.strftime('%Y-%m-%d %H:%M')}. Claims written by a reviewer from raw decision records; "
            "ids in braces are decision-record ids in that goal's decisions.jsonl. Reference guidance, not ground truth. -->\n"
        )
        body = re.sub(r"^<!--.*?-->\s*", "", markdown.strip(), flags=re.S)
        with open(self.path(family), "w", encoding="utf-8") as fh:
            fh.write(header + body + "\n")

    def test_next(self, limit: int = 5) -> list[str]:
        out: list[str] = []
        for f in sorted(os.listdir(self.dir)):
            if f.endswith(".md"):
                with open(os.path.join(self.dir, f), encoding="utf-8") as fh:
                    out += [f"[{f[:-3]}] {line}" for line in _section(fh.read(), "Test next")]
        return out[:limit]

    def still_doubt(self, family: str) -> list[str]:
        return _section(self.read(family), "Still doubt")

    # ---- remedies --------------------------------------------------------- #
    def remedies(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.remedies_path):
            return []
        try:
            with open(self.remedies_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def save_remedies(self, items: list[dict[str, Any]]) -> None:
        with open(self.remedies_path, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2)

    def for_builder(self, family: str, failure_labels: list[str] | None = None, max_remedies: int = 6) -> str:
        """Text for the builder prompt: the family's lesson and the remedies that match
        its family or the failing action's labels. Empty when there is nothing."""
        lesson = self.read(family)
        labels = set(failure_labels or [])
        rems = [
            r for r in self.remedies()
            if r.get("status") != "disputed" and (r.get("scope") == family or r.get("failure_type") in labels)
        ]
        rems.sort(key=lambda r: (r.get("status") == "confirmed", len(r.get("support") or [])), reverse=True)
        parts = []
        if lesson:
            body = re.sub(r"^<!--.*?-->\s*", "", lesson, flags=re.S).strip()
            parts.append(
                f"LESSONS FROM EARLIER PLAY for '{family}' steps (written by a reviewer from raw decision records; "
                "they are claims, not ground truth: the evidence and the snapshot above win when they disagree. "
                "Items under 'Still doubt' are assumptions your code should check rather than rely on):\n" + body
            )
        if rems:
            lines = [
                f"- [{r.get('status')}, {len(r.get('support') or [])} goal(s)] when {r.get('failure_type')}: {r.get('repair_action')}"
                for r in rems[:max_remedies]
            ]
            parts.append("REMEDIES THAT WORKED OR ARE BEING TESTED (from earlier reviews):\n" + "\n".join(lines))
        return "\n\n".join(parts)


REVIEW_SCHEMA = (
    '{"lesson_markdown": "## Tried\\n- ...\\n## Observed\\n- ...\\n## Still doubt\\n- ...\\n## Test next\\n- ...", '
    '"remedies": [{"failure_type": "LABEL", "repair_action": "...", "evidence_ids": ["id", "..."]}], '
    '"remedy_verdicts": [{"id": "rm-1", "verdict": "supported|contradicted|untested", "evidence_ids": ["id"]}]}'
)


class GoalReviewer:
    """One review per goal. ``llm`` needs ``complete_json(messages, schema_hint)``."""

    def __init__(self, llm, book: LessonBook, *, log: Callable[[str, dict[str, Any]], None] | None = None):
        self.llm = llm
        self.book = book
        self._log = log

    def review(
        self,
        *,
        goal: str,
        goal_id: str | None,
        success: bool,
        nodes: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Rewrite the lesson file of each family this goal exercised. Returns a summary."""
        summary: dict[str, Any] = {"families": [], "remedies_added": 0, "verdicts": 0, "skipped": []}
        if self.llm is None or not decisions:
            summary["skipped"].append("no LLM" if self.llm is None else "no decision records")
            return summary
        by_node = {str(n.get("id")): n for n in nodes}
        by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rec in decisions:
            node = by_node.get(str(rec.get("subtask_id"))) or {}
            by_family[family_of(node.get("kind"))].append(rec)
        families = sorted(by_family, key=lambda f: len(by_family[f]), reverse=True)[:FAMILIES_PER_REVIEW]
        remedies = self.book.remedies()
        for family in families:
            records = by_family[family]
            if len(records) < 3:
                summary["skipped"].append(f"{family}: only {len(records)} record(s)")
                continue
            try:
                out = self._review_family(goal, goal_id, success, family, records, nodes, remedies)
            except Exception as exc:
                summary["skipped"].append(f"{family}: {type(exc).__name__}: {exc}"[:200])
                continue
            if out is None:
                summary["skipped"].append(f"{family}: invalid review")
                continue
            markdown, new_remedies, verdicts = out
            self.book.write(family, markdown, goal=goal, goal_id=goal_id)
            summary["families"].append(family)
            summary["remedies_added"] += self._add_remedies(remedies, new_remedies, family, goal_id)
            summary["verdicts"] += self._apply_verdicts(remedies, verdicts, goal_id, {r.get("id") for r in records})
        self.book.save_remedies(remedies)
        self._emit("goal_review", {"goal": goal, "goal_id": goal_id, **summary})
        return summary

    # ------------------------------------------------------------------ #
    def _review_family(self, goal, goal_id, success, family, records, nodes, remedies):
        known_ids = {r.get("id") for r in records if r.get("id")}
        prompt = self._prompt(goal, success, family, records, nodes, remedies)
        messages = [
            {"role": "system", "content": (
                "You review one Minecraft bot's play from its raw action records and keep a short lesson file for "
                "one kind of step. You only state what the records show, you cite record ids for every claim, and "
                "you keep what you are unsure about under 'Still doubt' instead of turning it into a rule.")},
            {"role": "user", "content": prompt},
        ]
        problems: list[str] = []
        for _ in range(2):
            if problems:
                messages.append({"role": "user", "content": "Invalid: " + "; ".join(problems) + ". Answer again with the corrected JSON object."})
            with timing.timed("llm.review", family=family):
                reply = self.llm.complete_json(messages, REVIEW_SCHEMA)
            md, new_remedies, verdicts, problems = self._validate(reply, known_ids)
            if not problems:
                return md, new_remedies, verdicts
        self._emit("goal_review_rejected", {"family": family, "problems": problems})
        return None

    def _prompt(self, goal, success, family, records, nodes, remedies) -> str:
        fam_nodes = [n for n in nodes if family_of(n.get("kind")) == family]
        steps = "\n".join(
            f"- {n.get('description')}: {'done' if n.get('done') else 'FAILED' if n.get('failed') else 'not finished'}"
            f" after {n.get('actions_spent') or 0} actions" for n in fam_nodes
        ) or "- (none recorded)"
        # failures and the records around them matter most; keep the newest when trimming
        recs = records[-MAX_RECORDS_PER_FAMILY:]
        lines = []
        for r in recs:
            st = r.get("state") or {}
            lines.append(
                f"{{{r.get('id')}}} [{r.get('subtask')}] {r.get('action')} -> {str(r.get('outcome') or '')[:150]} "
                f"[{r.get('label')}] delta={json.dumps(r.get('delta') or {})} cursor={st.get('cursor')} "
                f"inv={json.dumps(st.get('inventory') or {})[:120]}"
            )
        labels: dict[str, int] = defaultdict(int)
        for r in records:
            labels[str(r.get("label"))] += 1
        current = self.book.read(family) or "(no lesson file yet)"
        rems = [r for r in remedies if r.get("scope") == family]
        rem_text = "\n".join(
            f"- {r.get('id')} [{r.get('status')}] when {r.get('failure_type')}: {r.get('repair_action')}" for r in rems
        ) or "- (none)"
        return (
            f"Goal: {goal} ({'reached' if success else 'NOT reached'})\n"
            f"Lesson family: {family}\n"
            f"Steps of this family in the goal tree:\n{steps}\n\n"
            f"Outcome labels in these records: {dict(labels)}\n"
            "Labels: OK = it did something; NO_CHANGE = reported ok but nothing changed; NO_EFFECT = a learned "
            "action said ok but its purpose did not change; the others are typed failures.\n\n"
            f"RAW ACTION RECORDS for this family, oldest first ({len(recs)} of {len(records)}):\n" + "\n".join(lines) + "\n\n"
            f"CURRENT LESSON FILE for '{family}':\n{current}\n\n"
            f"EXISTING REMEDIES for '{family}':\n{rem_text}\n\n"
            "Rewrite the lesson file for this family. Keep what earlier evidence supports, revise what the new records "
            "contradict, and add what is new. It must have exactly these four sections as Markdown headings: "
            "'## Tried' (approaches and actions used, with how they went), '## Observed' (facts the records show), "
            "'## Still doubt' (beliefs the records do not settle), '## Test next' (concrete things the bot should try "
            f"next time). Every bullet cites at least one record id in braces, like {{{recs[-1].get('id')}}}. "
            f"At most {MAX_WORDS} words. Then list new remedies (a failure label, the repair that the records show "
            "worked or would plausibly work, and the record ids) and a verdict on each existing remedy: supported, "
            "contradicted, or untested by these records."
        )

    @staticmethod
    def _validate(reply: Any, known_ids: set) -> tuple[str, list, list, list[str]]:
        problems: list[str] = []
        if not isinstance(reply, dict):
            return "", [], [], ["the answer must be one JSON object"]
        md = str(reply.get("lesson_markdown") or "").strip()
        missing = [s for s in SECTIONS if not re.search(rf"^##\s*{re.escape(s)}\s*$", md, re.M | re.I)]
        if missing:
            problems.append(f"lesson_markdown lacks the section(s) {missing}")
        if _words(md) > MAX_WORDS:
            problems.append(f"lesson_markdown has {_words(md)} words; the limit is {MAX_WORDS}")
        cited = {i for i in known_ids if i and i in md}
        if md and not cited:
            problems.append("lesson_markdown cites no record id from the records given")
        new_remedies = []
        for r in reply.get("remedies") or []:
            if not isinstance(r, dict) or not r.get("failure_type") or not r.get("repair_action"):
                continue
            ids = [i for i in (r.get("evidence_ids") or []) if i in known_ids]
            if ids:  # a remedy without evidence from these records is not kept
                new_remedies.append({"failure_type": str(r["failure_type"]).upper()[:40],
                                     "repair_action": str(r["repair_action"])[:300], "evidence_ids": ids})
        verdicts = []
        for v in reply.get("remedy_verdicts") or []:
            if isinstance(v, dict) and v.get("id") and v.get("verdict") in ("supported", "contradicted", "untested"):
                verdicts.append({"id": v["id"], "verdict": v["verdict"],
                                 "evidence_ids": [i for i in (v.get("evidence_ids") or []) if i in known_ids]})
        return md, new_remedies, verdicts, problems

    @staticmethod
    def _add_remedies(store: list[dict[str, Any]], new: list[dict[str, Any]], family: str, goal_id: str | None) -> int:
        n = 0
        next_id = 1 + max([int(str(r.get("id", "rm-0")).split("-")[-1]) for r in store] or [0])
        for r in new:
            dup = next((x for x in store if x.get("scope") == family and x.get("failure_type") == r["failure_type"]
                        and x.get("repair_action", "").lower() == r["repair_action"].lower()), None)
            if dup:
                continue
            store.append({
                "id": f"rm-{next_id}", "scope": family, "failure_type": r["failure_type"],
                "repair_action": r["repair_action"], "status": "hypothesis", "support": [goal_id] if goal_id else [],
                "contradicted": 0, "evidence": {goal_id or "?": r["evidence_ids"]}, "created_at": time.time(),
            })
            next_id += 1
            n += 1
        return n

    @staticmethod
    def _apply_verdicts(store: list[dict[str, Any]], verdicts: list[dict[str, Any]], goal_id: str | None, known_ids: set) -> int:
        n = 0
        by_id = {r.get("id"): r for r in store}
        for v in verdicts:
            r = by_id.get(v["id"])
            if r is None or v["verdict"] == "untested" or not v["evidence_ids"]:
                continue  # a verdict must point at records from this goal
            n += 1
            if v["verdict"] == "supported" and goal_id and goal_id not in (r.get("support") or []):
                r.setdefault("support", []).append(goal_id)
                r.setdefault("evidence", {})[goal_id] = v["evidence_ids"]
            elif v["verdict"] == "contradicted":
                r["contradicted"] = int(r.get("contradicted") or 0) + 1
            if int(r.get("contradicted") or 0) >= 2:
                r["status"] = "disputed"
            elif len(r.get("support") or []) >= 2 and not r.get("contradicted"):
                r["status"] = "confirmed"
        return n

    def _emit(self, kind: str, record: dict[str, Any]) -> None:
        print(f"\033[34m[review] {kind} {json.dumps(record, default=str)[:300]}\033[0m")
        if self._log:
            try:
                self._log(kind, record)
            except Exception:
                pass
