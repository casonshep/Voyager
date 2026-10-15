"""Render the goal-tree agent's learning progress as one self-contained HTML page.

Reads every run checkpoint (``ckpt_*/tree`` and ``ckpt_*/typesafe``) and the shared
skill library (``skill_library/tree``), then writes ``reports/progress.html``:

* summary: runs, goals reached, furthest progress, skills in the library;
* runs: one row per run with each subtask as a coloured chip, expandable to the
  goal tree with actions spent and the learned skills that finished each step;
* skills: every learned action with its declared purpose, when it was learned,
  its success rate across runs, which runs used it, and whether it is still loaded;
* builder: accepted and rejected builds per run, with rejection reasons.

Usage (from the repo root):
    venv/Scripts/python scripts/progress_report.py [--out reports/progress.html] [--library skill_library/tree]
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import time
from collections import Counter, defaultdict
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def load_runs() -> list[dict[str, Any]]:
    runs = []
    for ckpt in sorted(glob.glob(os.path.join(ROOT, "ckpt_*"))):
        tree_dir = os.path.join(ckpt, "tree")
        if not os.path.isdir(tree_dir):
            continue
        trees = [p for p in glob.glob(os.path.join(tree_dir, "*.json")) if not p.endswith((".run.json", "current.json"))]
        for tree_path in sorted(trees):
            tree = _read_json(tree_path)
            if not tree or not isinstance(tree.get("nodes"), dict):
                continue
            ts = os.path.join(ckpt, "typesafe")
            events = _read_jsonl(os.path.join(ts, "traversal.jsonl"))
            runs.append({
                "ckpt": os.path.basename(ckpt),
                "tree": tree,
                "run_state": _read_json(tree_path[:-5] + ".run.json") or {},
                "events": events,
                "generated": _read_jsonl(os.path.join(ts, "action_generated.jsonl")),
                "rejected": _read_jsonl(os.path.join(ts, "action_rejected.jsonl")),
                "started": float(tree.get("created_at") or os.path.getmtime(tree_path)),
                "ended": max([float(e.get("ts") or 0) for e in events] + [os.path.getmtime(tree_path)]),
            })
    runs.sort(key=lambda r: r["started"])
    return runs


def load_skills(library: str) -> dict[str, dict[str, Any]]:
    skills = {}
    for side in sorted(glob.glob(os.path.join(library, "*.json"))):
        name = os.path.basename(side)[:-5]
        meta = _read_json(side) or {}
        if not os.path.exists(os.path.join(library, f"{name}.js")):
            continue
        skills[name] = meta
    return skills


def skill_status(meta: dict[str, Any]) -> str:
    """Mirror of ActionBuilder.reload(proven_only=True)."""
    trials = int(meta.get("trials") or 0)
    successes = int(meta.get("successes") or 0)
    if meta.get("retired"):
        return "retired"
    if successes == 0:
        return "unproven"
    if trials >= 10 and successes / trials < 0.2:
        return "pruned"
    return "active"


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #
def post_order(tree: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = tree["nodes"]
    out: list[dict[str, Any]] = []

    def walk(nid: str) -> None:
        n = nodes.get(nid)
        if not n:
            return
        for c in n.get("children") or []:
            walk(c)
        out.append(n)

    if tree.get("root") in nodes:
        walk(tree["root"])
    return out


def node_state(n: dict[str, Any], failed: set[str]) -> str:
    if n.get("done"):
        return "done"
    if n.get("failed") or n.get("id") in failed:
        return "failed"
    return "pending"


def analyse(runs: list[dict[str, Any]], skills: dict[str, dict[str, Any]]) -> dict[str, Any]:
    used_in: dict[str, set[int]] = defaultdict(set)
    finished_by: dict[str, Counter] = defaultdict(Counter)
    for i, r in enumerate(runs):
        failed = set(r["run_state"].get("failed") or [])
        order = post_order(r["tree"])
        r["order"] = order
        r["done"] = sum(1 for n in order if n.get("done"))
        r["total"] = len(order)
        r["failed"] = failed
        r["reached"] = bool(order and order[-1].get("done"))
        r["actions"] = sum(int(n.get("actions_spent") or 0) for n in order)
        for n in order:
            learned = [a[2:] for a in (n.get("trace") or []) if isinstance(a, str) and a.startswith("x:")]
            n["_skills"] = Counter(learned)
            for s in set(learned):
                used_in[s].add(i)
                if n.get("done"):
                    finished_by[s][n.get("description", "")] += 1
    for i, r in enumerate(runs):
        nxt = runs[i + 1]["started"] if i + 1 < len(runs) else float("inf")
        r["window"] = (r["started"] - 60, nxt)
    for name, meta in skills.items():
        created = float(meta.get("created_at") or 0)
        meta["_run"] = next((i for i, r in enumerate(runs) if r["window"][0] <= created < r["window"][1]), None)
        meta["_used_in"] = sorted(used_in.get(name, set()))
        meta["_finished"] = finished_by.get(name, Counter())
        meta["_status"] = skill_status(meta)
    return {"used_in": used_in}


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#f7f7f5;--panel:#ffffff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e3de;--done:#2f8f5b;--done-bg:#e3f3ea;
--fail:#c2410c;--fail-bg:#fbe7dc;--pend:#8a8a84;--pend-bg:#efefeb;--accent:#3b5bdb;--accent-bg:#e7ecfd;--warn:#a16207;--warn-bg:#fbf1d6}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#151514;--panel:#1e1e1c;--ink:#ecece8;--muted:#9b9b95;--line:#30302d;
--done:#6fcf97;--done-bg:#1f3a2a;--fail:#f4a261;--fail-bg:#3d2617;--pend:#9b9b95;--pend-bg:#2a2a27;--accent:#8ea6ff;--accent-bg:#232a45;--warn:#e9c46a;--warn-bg:#3a3018}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 10px}
.sub{color:var(--muted);margin:0 0 20px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}.card b{display:block;font-size:22px}
.card span{color:var(--muted);font-size:12px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}tr:last-child td{border-bottom:none}
.chips{display:flex;flex-wrap:wrap;gap:3px}.chip{width:14px;height:14px;border-radius:3px;display:inline-block}
.chip.done{background:var(--done)}.chip.failed{background:var(--fail)}.chip.pending{background:var(--pend-bg);border:1px solid var(--line)}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:12px;white-space:nowrap}
.tag.done,.tag.active{background:var(--done-bg);color:var(--done)}.tag.failed,.tag.retired{background:var(--fail-bg);color:var(--fail)}
.tag.pending,.tag.unproven{background:var(--pend-bg);color:var(--pend)}.tag.pruned{background:var(--warn-bg);color:var(--warn)}
.tag.kind{background:var(--accent-bg);color:var(--accent)}.muted{color:var(--muted)}code{font:12px ui-monospace,Consolas,monospace}
.bar{height:6px;background:var(--pend-bg);border-radius:3px;width:90px;display:inline-block;vertical-align:middle;margin-right:6px}
.bar i{display:block;height:6px;border-radius:3px;background:var(--done)}details{border-top:1px solid var(--line)}
details summary{cursor:pointer;padding:8px 10px;list-style:none}details summary::-webkit-details-marker{display:none}
.tree{padding:4px 10px 12px}.node{display:flex;gap:8px;align-items:baseline;padding:2px 0}.node .d{flex:1}
.reasons{margin:0;padding-left:18px}.legend{display:flex;gap:14px;color:var(--muted);font-size:12px;margin:6px 0 0}
.legend span{display:inline-flex;gap:5px;align-items:center}
"""


def esc(x: Any) -> str:
    return html.escape(str(x if x is not None else ""))


def when(ts: float) -> str:
    return time.strftime("%b %d %H:%M", time.localtime(ts)) if ts else ""


def render(runs: list[dict[str, Any]], skills: dict[str, dict[str, Any]], library: str) -> str:
    reached = [r for r in runs if r["reached"]]
    active = [s for s in skills.values() if s["_status"] == "active"]
    best = max(runs, key=lambda r: (r["done"] / max(1, r["total"]), r["started"]), default=None)
    first_win = reached[0] if reached else None

    cards = [
        (len(runs), "runs recorded"),
        (len(reached), "goals reached"),
        (f"{best['done']}/{best['total']}" if best else "-", f"best run ({best['tree'].get('goal_text', '')})" if best else "best run"),
        (len(active), "skills in the library"),
        (sum(len(r["generated"]) for r in runs), "actions built (all runs)"),
        (sum(len(r["rejected"]) for r in runs), "builds rejected at compile time"),
    ]
    out = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Agent Learning Progress</title><style>", CSS, "</style></head><body><main>",
        "<h1>Agent learning progress</h1>",
        f"<p class='sub'>Generated {esc(time.strftime('%Y-%m-%d %H:%M'))} from {len(runs)} run checkpoints and <code>{esc(os.path.relpath(library, ROOT))}</code>."
        + (f" First goal reached in run {runs.index(first_win) + 1} ({esc(first_win['tree'].get('goal_text'))})." if first_win else "")
        + "</p>",
        "<div class='cards'>" + "".join(f"<div class='card'><b>{esc(v)}</b><span>{esc(k)}</span></div>" for v, k in cards) + "</div>",
    ]

    # ---- runs ----
    out.append("<h2>Runs</h2><div class='panel'><table><thead><tr><th>#</th><th>Started</th><th>Goal</th><th>Subtasks (post-order)</th>"
               "<th>Done</th><th>Actions</th><th>Built</th><th>Rejected</th><th>Result</th></tr></thead><tbody>")
    for i, r in enumerate(runs):
        chips = "".join(f"<span class='chip {node_state(n, r['failed'])}' title='{esc(n.get('description'))}'></span>" for n in r["order"])
        result = "done" if r["reached"] else ("failed" if r["failed"] else "pending")
        label = {"done": "reached", "failed": "failed", "pending": "stopped"}[result]
        out.append(
            f"<tr><td>{i + 1}</td><td class='muted'>{esc(when(r['started']))}</td><td>{esc(r['tree'].get('goal_text'))}</td>"
            f"<td><div class='chips'>{chips}</div></td><td>{r['done']}/{r['total']}</td><td>{r['actions']}</td>"
            f"<td>{len(r['generated'])}</td><td>{len(r['rejected'])}</td><td><span class='tag {result}'>{label}</span></td></tr>"
        )
    out.append("</tbody></table>")
    out.append("<div class='legend' style='padding:0 10px 10px'><span><i class='chip done'></i>done</span>"
               "<span><i class='chip failed'></i>failed</span><span><i class='chip pending'></i>not finished</span></div>")
    for i, r in reversed(list(enumerate(runs))):
        nodes = r["tree"]["nodes"]
        rows = []

        def depth(n: dict[str, Any]) -> int:
            d, p = 0, n.get("parent")
            while p and p in nodes:
                d, p = d + 1, nodes[p].get("parent")
            return d

        for n in r["order"]:
            st = node_state(n, r["failed"])
            used = ", ".join(f"x:{k}" + (f" ×{v}" if v > 1 else "") for k, v in n["_skills"].most_common(4))
            rows.append(
                f"<div class='node' style='padding-left:{depth(n) * 18}px'><span class='tag {st}'>{st}</span>"
                f"<span class='d'>{esc(n.get('description'))} <span class='muted'>· {int(n.get('actions_spent') or 0)} actions</span></span>"
                f"<span class='muted'><code>{esc(used)}</code></span></div>"
            )
        out.append(f"<details><summary>Run {i + 1} · {esc(r['tree'].get('goal_text'))} · {r['done']}/{r['total']} · "
                   f"<span class='muted'>{esc(r['ckpt'])}</span></summary><div class='tree'>{''.join(rows)}</div></details>")
    out.append("</div>")

    # ---- skills ----
    out.append("<h2>Skills learned</h2><div class='panel'><table><thead><tr><th>Skill</th><th>Kind</th><th>Declared purpose</th>"
               "<th>Learned in</th><th>Success</th><th>Used in runs</th><th>Finished steps</th><th>Library</th></tr></thead><tbody>")
    order = {"active": 0, "pruned": 1, "unproven": 2, "retired": 3}
    for name, m in sorted(skills.items(), key=lambda kv: (order.get(kv[1]["_status"], 9), float(kv[1].get("created_at") or 0))):
        trials, succ = int(m.get("trials") or 0), int(m.get("successes") or 0)
        rate = succ / trials if trials else 0
        run_no = f"run {m['_run'] + 1}" if m["_run"] is not None else "-"
        finished = "; ".join(f"{esc(k)} ×{v}" for k, v in m["_finished"].most_common(2))
        out.append(
            f"<tr><td><code>x:{esc(name)}</code></td><td><span class='tag kind'>{esc(m.get('kind') or '?')}</span></td>"
            f"<td>{esc(m.get('purpose') or '')}<div class='muted'>learned for: {esc(m.get('learned_from') or m.get('subtask') or '')}</div></td>"
            f"<td class='muted'>{esc(run_no)}</td>"
            f"<td><span class='bar'><i style='width:{rate * 100:.0f}%'></i></span>{succ}/{trials}</td>"
            f"<td class='muted'>{esc(', '.join(str(x + 1) for x in m['_used_in']) or '-')}</td>"
            f"<td class='muted'>{finished or '-'}</td><td><span class='tag {m['_status']}'>{m['_status']}</span></td></tr>"
        )
    out.append("</tbody></table></div>")

    # ---- builder ----
    out.append("<h2>Builder feedback loop</h2><div class='panel'><table><thead><tr><th>#</th><th>Goal</th><th>Accepted</th>"
               "<th>Rejected</th><th>Rejection reasons</th><th>Refused</th></tr></thead><tbody>")
    for i, r in enumerate(runs):
        reasons = Counter()
        for rej in r["rejected"]:
            why = str(rej.get("why") or "")
            reasons[why.split(":")[0][:90] if not why.startswith(("not applicable", "purpose")) else why] += 1
        accepted = ", ".join(f"x:{g.get('name')}" for g in r["generated"])
        # builds the builder declined to start (cap, cooldown, trial gate), logged since the refusal event exists
        refusals = Counter(str(e.get("why") or "")[:80] for e in r["events"] if e.get("type") == "build_refused")
        out.append(
            f"<tr><td>{i + 1}</td><td>{esc(r['tree'].get('goal_text'))}</td><td><code>{esc(accepted) or '-'}</code></td>"
            f"<td>{len(r['rejected'])}</td><td>" + ("<ul class='reasons'>" + "".join(f"<li>{esc(k)} ×{v}</li>" for k, v in reasons.most_common(4)) + "</ul>" if reasons else "-") + "</td>"
            + "<td>" + (("<ul class='reasons'>" + "".join(f"<li>{esc(k)} ×{v}</li>" for k, v in refusals.most_common(3)) + "</ul>") if refusals else "-") + "</td></tr>"
        )
    out.append("</tbody></table></div></main></body></html>")
    return "".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(ROOT, "reports", "progress.html"))
    ap.add_argument("--library", default=os.path.join(ROOT, "skill_library", "tree"))
    args = ap.parse_args()
    runs = load_runs()
    skills = load_skills(args.library)
    analyse(runs, skills)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(render(runs, skills, args.library))
    reached = sum(1 for r in runs if r["reached"])
    active = sum(1 for s in skills.values() if s["_status"] == "active")
    print(f"wrote {args.out}: {len(runs)} runs, {reached} goals reached, {active} active skills")


if __name__ == "__main__":
    main()
