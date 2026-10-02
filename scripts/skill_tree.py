"""Render the fast-mode skill tree (ckpt/fast/skills.json) as a standalone HTML page.

    venv/Scripts/python scripts/skill_tree.py                    # newest ckpt_*/fast/skills.json
    venv/Scripts/python scripts/skill_tree.py ckpt_x/fast/skills.json --open

Writes skill_tree.html next to the JSON (or --out PATH) and, with --open, opens it
in the default browser. Re-run any time; the page is a snapshot of the file.

The page: high goals (blue) on top, the subgoal nodes used to reach them (orange)
below, edges from a goal to the nodes it was reached through. Hover a node for
its record (verify spec, seconds, tools, routes, successes/failures); click to
highlight everything it depends on and everything that depends on it. A table
view lists the same records. Light and dark follow the system.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import webbrowser

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skill Tree</title>
<style>
  :root {
    color-scheme: light;
    --surface: #fcfcfb; --panel: #f3f2ef; --border: #d9d8d3;
    --text: #0b0b0b; --text-2: #52514e; --text-3: #7a7873;
    --high: #2a78d6; --sub: #eb6834; --fail: #e34948; --edge: #b9b8b2;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface: #1a1a19; --panel: #232322; --border: #3a3a37;
      --text: #ffffff; --text-2: #c3c2b7; --text-3: #8f8e86;
      --high: #3987e5; --sub: #d95926; --fail: #e66767; --edge: #4b4b47;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface: #1a1a19; --panel: #232322; --border: #3a3a37;
    --text: #ffffff; --text-2: #c3c2b7; --text-3: #8f8e86;
    --high: #3987e5; --sub: #d95926; --fail: #e66767; --edge: #4b4b47;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; background: var(--surface); color: var(--text); font: 14px/1.45 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; display: flex; flex-direction: column; overflow: hidden; }
  header { display: flex; flex-wrap: wrap; gap: 12px 20px; align-items: baseline; padding: 14px 16px 8px; border-bottom: 1px solid var(--border); }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  .meta { color: var(--text-2); }
  .legend { display: flex; flex-wrap: wrap; gap: 14px; margin-left: auto; color: var(--text-2); font-size: 13px; }
  .legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; background: var(--c); }
  .legend .judged::before { background: transparent; border: 2px dashed var(--text-3); width: 8px; height: 8px; }
  .legend .failing::before { background: transparent; border: 2px solid var(--fail); width: 8px; height: 8px; }
  .controls { display: flex; gap: 8px; padding: 8px 16px; border-bottom: 1px solid var(--border); align-items: center; }
  button { background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 5px 10px; font: inherit; cursor: pointer; }
  button[aria-pressed="true"] { border-color: var(--text-2); }
  .hint { color: var(--text-3); font-size: 13px; margin-left: auto; }
  #viz { position: relative; flex: 1; min-height: 360px; overflow: hidden; }
  svg { width: 100%; height: 100%; display: block; cursor: grab; }
  svg.dragging { cursor: grabbing; }
  .edge { fill: none; stroke: var(--edge); stroke-width: 2; }
  .edge.dim, .node.dim { opacity: 0.18; }
  .edge.lit { stroke: var(--text-2); }
  .node circle { stroke: var(--surface); stroke-width: 2; }
  .node.high circle { fill: var(--high); }
  .node.sub circle { fill: var(--sub); }
  .node.judged circle { fill: var(--surface); stroke: var(--text-2); stroke-dasharray: 3 2; }
  .node.judged.high circle { stroke: var(--high); }
  .node.judged.sub circle { stroke: var(--sub); }
  .node .ring { fill: none; stroke: var(--fail); stroke-width: 2; }
  .node text { fill: var(--text); font-size: 12px; paint-order: stroke; stroke: var(--surface); stroke-width: 3px; }
  .node .small { fill: var(--text-2); font-size: 11px; }
  .node:hover circle { stroke: var(--text); }
  #tip { position: absolute; pointer-events: none; background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; max-width: 360px; font-size: 13px; box-shadow: 0 4px 16px rgba(0,0,0,.15); display: none; }
  #tip b { font-weight: 600; }
  #tip dl { margin: 6px 0 0; display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; }
  #tip dt { color: var(--text-2); }
  #tip dd { margin: 0; }
  #table { display: none; padding: 16px; overflow: auto; flex: 1; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--text-2); font-weight: 600; position: sticky; top: 0; background: var(--surface); }
  .empty { padding: 40px 16px; color: var(--text-2); text-align: center; }
  @media (max-width: 640px) { header, .controls { padding-left: 16px; padding-right: 16px; } .legend { margin-left: 0; } }
</style>
</head>
<body>
<header>
  <h1>Skill tree</h1>
  <span class="meta" id="meta"></span>
  <div class="legend">
    <span style="--c: var(--high)">High goal</span>
    <span style="--c: var(--sub)">Subgoal</span>
    <span class="judged">Judged by Jev</span>
    <span class="failing">More failures than successes</span>
  </div>
</header>
<div class="controls">
  <button id="btnTree" aria-pressed="true">Tree</button>
  <button id="btnTable" aria-pressed="false">Table</button>
  <button id="btnFit">Fit</button>
  <span class="hint">Scroll to zoom, drag to pan, click a node to trace its path</span>
</div>
<div id="viz"><svg id="svg" role="img" aria-label="Skill tree graph"><g id="root"></g></svg><div id="tip"></div></div>
<div id="table"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
(() => {
  const raw = JSON.parse(document.getElementById('data').textContent);
  const nodes = Object.values(raw.skills || {}).filter(n => n && n.id);
  const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
  document.getElementById('meta').textContent =
    `${raw.source} · ${nodes.length} nodes · ${nodes.filter(n => n.kind === 'high').length} high goals`;
  const viz = document.getElementById('viz'), svg = document.getElementById('svg'), root = document.getElementById('root'), tip = document.getElementById('tip');
  if (!nodes.length) { viz.innerHTML = '<div class="empty">No skills recorded yet. The tree fills in as the bot reaches goals.</div>'; return; }

  // ---- layering: roots (no parents) at the top, each node below its deepest parent
  const depth = {};
  const parentsOf = n => (n.parents || []).filter(p => byId[p]);
  const childrenOf = n => (n.children || []).filter(c => byId[c]);
  const visiting = new Set();
  function d(n) {
    if (depth[n.id] !== undefined) return depth[n.id];
    if (visiting.has(n.id)) return 0;
    visiting.add(n.id);
    const ps = parentsOf(n);
    depth[n.id] = ps.length ? 1 + Math.max(...ps.map(p => d(byId[p]))) : 0;
    visiting.delete(n.id);
    return depth[n.id];
  }
  nodes.forEach(d);
  // standalone sub nodes (never used by a high goal yet) go to the bottom layer
  const maxDepth = Math.max(0, ...Object.values(depth));
  nodes.forEach(n => { if (!parentsOf(n).length && n.kind !== 'high') depth[n.id] = maxDepth + 1; });
  const layers = [];
  nodes.forEach(n => { (layers[depth[n.id]] ||= []).push(n); });
  // order within a layer by the mean x of parents, then name
  const x = {}, y = {};
  const colW = 190, rowH = 120;
  layers.forEach((layer, li) => {
    layer.sort((a, b) => {
      const ma = parentsOf(a).length ? parentsOf(a).reduce((s, p) => s + (x[p] ?? 0), 0) / parentsOf(a).length : Infinity;
      const mb = parentsOf(b).length ? parentsOf(b).reduce((s, p) => s + (x[p] ?? 0), 0) / parentsOf(b).length : Infinity;
      return ma - mb || a.goal.localeCompare(b.goal);
    });
    const width = (layer.length - 1) * colW;
    layer.forEach((n, i) => { x[n.id] = i * colW - width / 2; y[n.id] = li * rowH; });
  });
  const edges = [];
  nodes.forEach(n => childrenOf(n).forEach(c => edges.push([n.id, c])));

  // ---- draw
  const ns = 'http://www.w3.org/2000/svg';
  const el = (t, a = {}) => { const e = document.createElementNS(ns, t); for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); return e; };
  const gEdges = el('g'), gNodes = el('g');
  root.append(gEdges, gNodes);
  const edgeEls = edges.map(([a, b]) => {
    const p = el('path', { class: 'edge', d: `M${x[a]},${y[a] + 14} C${x[a]},${(y[a] + y[b]) / 2} ${x[b]},${(y[a] + y[b]) / 2} ${x[b]},${y[b] - 14}` });
    p.dataset.a = a; p.dataset.b = b; gEdges.appendChild(p); return p;
  });
  const radius = n => Math.min(22, 10 + 2 * Math.sqrt(n.successes || 0));
  const short = s => s.length > 26 ? s.slice(0, 25) + '…' : s;
  const nodeEls = {};
  nodes.forEach(n => {
    const judged = (n.verify || {}).kind === 'judged';
    const failing = (n.failures || 0) > (n.successes || 0);
    const g = el('g', { class: `node ${n.kind || 'sub'}${judged ? ' judged' : ''}`, transform: `translate(${x[n.id]},${y[n.id]})`, tabindex: 0 });
    const r = radius(n);
    g.appendChild(el('circle', { r }));
    if (failing) g.appendChild(el('circle', { class: 'ring', r: r + 4 }));
    const t = el('text', { y: r + 15, 'text-anchor': 'middle' }); t.textContent = short(n.goal); g.appendChild(t);
    const s = el('text', { class: 'small', y: r + 29, 'text-anchor': 'middle' });
    s.textContent = `${n.successes || 0}✓ ${n.failures || 0}✗ · ${n.seconds ?? '?'}s`; g.appendChild(s);
    g.addEventListener('mousemove', ev => showTip(n, ev));
    g.addEventListener('mouseleave', () => tip.style.display = 'none');
    g.addEventListener('click', ev => { ev.stopPropagation(); trace(n.id); });
    gNodes.appendChild(g); nodeEls[n.id] = g;
  });
  function showTip(n, ev) {
    const routes = Object.values(n.routes || {});
    const rows = [
      ['kind', n.kind], ['verify', JSON.stringify(n.verify)], ['seconds', `${n.seconds} (last ${n.last_seconds})`],
      ['tools', (n.tools_used || []).join(', ') || 'none'], ['reached / failed', `${n.successes} / ${n.failures}`],
      ['for high goals', (n.high_goals || []).join('; ') || '—'],
      ['routes', routes.length ? routes.map(r => `${(r.fingerprint||{}).biome}/${(r.fingerprint||{}).toolTier}/${(r.fingerprint||{}).daylight}: ${r.actions.join(' → ')}`).join('<br>') : 'none'],
      ['inventory delta', JSON.stringify(n.inventory_delta || {})],
    ];
    tip.innerHTML = `<b>${n.goal}</b><dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`;
    tip.style.display = 'block';
    const b = viz.getBoundingClientRect();
    let left = ev.clientX - b.left + 14, top = ev.clientY - b.top + 14;
    if (left + 380 > b.width) left = Math.max(0, ev.clientX - b.left - 380);
    if (top + tip.offsetHeight > b.height) top = Math.max(0, ev.clientY - b.top - tip.offsetHeight - 10);
    tip.style.left = left + 'px'; tip.style.top = top + 'px';
  }
  // ---- click: light the node, its ancestors and descendants
  function trace(id) {
    if (!id) { Object.values(nodeEls).forEach(g => g.classList.remove('dim')); edgeEls.forEach(e => e.classList.remove('dim', 'lit')); return; }
    const keep = new Set([id]);
    const walk = (start, next) => { const st = [start]; while (st.length) { const c = st.pop(); for (const m of next(byId[c])) if (!keep.has(m)) { keep.add(m); st.push(m); } } };
    walk(id, parentsOf); walk(id, childrenOf);
    Object.entries(nodeEls).forEach(([k, g]) => g.classList.toggle('dim', !keep.has(k)));
    edgeEls.forEach(e => { const on = keep.has(e.dataset.a) && keep.has(e.dataset.b); e.classList.toggle('dim', !on); e.classList.toggle('lit', on); });
  }
  svg.addEventListener('click', () => trace(null));

  // ---- pan & zoom
  let tx = 0, ty = 0, k = 1;
  const apply = () => root.setAttribute('transform', `translate(${tx},${ty}) scale(${k})`);
  function fit() {
    const xs = Object.values(x), ys = Object.values(y);
    const minX = Math.min(...xs) - 100, maxX = Math.max(...xs) + 100, minY = Math.min(...ys) - 40, maxY = Math.max(...ys) + 70;
    const w = svg.clientWidth, h = svg.clientHeight;
    k = Math.min(w / (maxX - minX), h / (maxY - minY), 1.6);
    tx = (w - (minX + maxX) * k) / 2; ty = (h - (minY + maxY) * k) / 2; apply();
  }
  svg.addEventListener('wheel', ev => { ev.preventDefault(); const f = Math.exp(-ev.deltaY * 0.0015); const b = svg.getBoundingClientRect(); const px = ev.clientX - b.left, py = ev.clientY - b.top; tx = px - (px - tx) * f; ty = py - (py - ty) * f; k *= f; apply(); }, { passive: false });
  let drag = null;
  svg.addEventListener('pointerdown', ev => { drag = { x: ev.clientX - tx, y: ev.clientY - ty }; svg.classList.add('dragging'); svg.setPointerCapture(ev.pointerId); });
  svg.addEventListener('pointermove', ev => { if (drag) { tx = ev.clientX - drag.x; ty = ev.clientY - drag.y; apply(); } });
  svg.addEventListener('pointerup', () => { drag = null; svg.classList.remove('dragging'); });
  window.addEventListener('resize', fit);
  fit();

  // ---- table view
  const tbl = document.getElementById('table');
  const esc = s => String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  tbl.innerHTML = `<table><thead><tr><th>Goal</th><th>Kind</th><th>Verify</th><th>Reached</th><th>Failed</th><th>Seconds</th><th>Tools</th><th>Used by</th><th>Uses</th></tr></thead><tbody>` +
    nodes.sort((a, b) => depth[a.id] - depth[b.id] || a.goal.localeCompare(b.goal)).map(n => `<tr><td>${esc(n.goal)}</td><td>${esc(n.kind)}</td><td>${esc(JSON.stringify(n.verify))}</td><td>${n.successes}</td><td>${n.failures}</td><td>${n.seconds}</td><td>${esc((n.tools_used || []).join(', '))}</td><td>${esc(parentsOf(n).map(p => byId[p].goal).join('; '))}</td><td>${esc(childrenOf(n).map(c => byId[c].goal).join('; '))}</td></tr>`).join('') + '</tbody></table>';
  const btnTree = document.getElementById('btnTree'), btnTable = document.getElementById('btnTable');
  const show = tree => { viz.style.display = tree ? 'block' : 'none'; tbl.style.display = tree ? 'none' : 'block'; btnTree.setAttribute('aria-pressed', tree); btnTable.setAttribute('aria-pressed', !tree); if (tree) fit(); };
  btnTree.onclick = () => show(true); btnTable.onclick = () => show(false);
  document.getElementById('btnFit').onclick = fit;
})();
</script>
</body>
</html>
"""


def newest_skills_file() -> str | None:
    candidates = glob.glob(os.path.join("ckpt*", "fast", "skills.json"))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def normalise(skills: dict) -> dict:
    """Accept the tree format; skip entries from the older flat skill list."""
    out = {}
    for key, node in (skills or {}).items():
        if not isinstance(node, dict) or "goal" not in node or "kind" not in node:
            continue  # pre-tree record: no verify spec, routes or edges
        node = dict(node)
        node.setdefault("id", key)
        node.setdefault("children", [])
        node.setdefault("parents", [])
        node.setdefault("routes", {})
        node.setdefault("tools_used", [])
        node.setdefault("successes", 0)
        node.setdefault("failures", 0)
        out[node["id"]] = node
    return out


def render(skills_path: str, out_path: str) -> int:
    with open(skills_path, encoding="utf-8") as fh:
        skills = json.load(fh)
    nodes = normalise(skills)
    skipped = len(skills) - len(nodes)
    data = {"source": os.path.relpath(skills_path).replace("\\", "/"), "skills": nodes}
    payload = json.dumps(data).replace("</", "<\\/")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(HTML.replace("__DATA__", payload))
    print(f"{len(nodes)} nodes -> {out_path}" + (f" ({skipped} old-format entries skipped)" if skipped else ""))
    return len(nodes)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("skills", nargs="?", help="path to fast/skills.json (default: newest ckpt_*/fast/skills.json)")
    ap.add_argument("--out", help="output HTML path (default: skill_tree.html next to the JSON)")
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = ap.parse_args()
    skills_path = args.skills or newest_skills_file()
    if not skills_path or not os.path.exists(skills_path):
        print("no skills.json found; pass its path", file=sys.stderr)
        return 1
    out_path = args.out or os.path.join(os.path.dirname(skills_path), "skill_tree.html")
    render(skills_path, out_path)
    if args.open:
        webbrowser.open("file://" + os.path.abspath(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
