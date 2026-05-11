"""Self-contained HTML view for a job-local memory graph."""

from __future__ import annotations

import json
from html import escape
from typing import Any

from nipux_cli.memory_graph import memory_graph_from_job


def render_memory_graph_html(job: dict[str, Any]) -> str:
    """Return a standalone clickable graph page for a job's memory graph."""

    graph = memory_graph_from_job(job)
    nodes = [_view_node(node) for node in graph["nodes"]]
    edges = [_view_edge(edge) for edge in graph["edges"]]
    data = json.dumps(
        {
            "job": {
                "id": str(job.get("id") or ""),
                "title": str(job.get("title") or ""),
                "objective": str(job.get("objective") or ""),
            },
            "updated_at": graph.get("updated_at") or "",
            "nodes": nodes,
            "edges": edges,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    title = escape(str(job.get("title") or "Nipux memory graph"))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nipux Memory Graph - {title}</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #101112;
  --panel: #17191b;
  --line: #303336;
  --text: #eeeeea;
  --muted: #9a9a94;
  --accent: #82e6e1;
  --gold: #e6d06f;
  --purple: #c99ce8;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; min-height: 100%; background: var(--bg); color: var(--text); font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
body {{ overflow: hidden; }}
.shell {{ display: grid; grid-template-columns: minmax(0, 1fr) 360px; height: 100vh; }}
.stage {{ position: relative; min-width: 0; border-right: 1px solid var(--line); }}
.top {{ position: absolute; top: 22px; left: 26px; right: 26px; z-index: 2; display: flex; align-items: start; justify-content: space-between; gap: 24px; }}
.eyebrow {{ color: var(--muted); letter-spacing: .22em; text-transform: uppercase; font-size: 12px; }}
h1 {{ margin: 8px 0 0; font-size: clamp(30px, 4vw, 64px); line-height: .95; letter-spacing: -.04em; }}
.stats {{ display: flex; gap: 18px; color: var(--muted); white-space: nowrap; }}
.stats b {{ color: var(--text); font-size: 20px; }}
canvas {{ display: block; width: 100%; height: 100%; }}
.help {{ position: absolute; left: 26px; bottom: 22px; color: var(--muted); z-index: 2; }}
aside {{ min-width: 0; background: var(--panel); padding: 26px; overflow: auto; }}
.card {{ border: 1px solid var(--line); border-radius: 18px; padding: 18px; margin-top: 18px; background: rgba(255,255,255,.018); }}
.label {{ color: var(--muted); font-size: 12px; letter-spacing: .18em; text-transform: uppercase; }}
.node-title {{ margin-top: 10px; font-size: 22px; line-height: 1.12; }}
.row {{ display: grid; grid-template-columns: 88px minmax(0, 1fr); gap: 12px; margin-top: 12px; }}
.row span:first-child {{ color: var(--muted); }}
.pill {{ display: inline-block; margin: 5px 6px 0 0; padding: 3px 8px; border: 1px solid var(--line); border-radius: 999px; color: var(--accent); }}
.list {{ margin: 8px 0 0; padding-left: 18px; color: var(--muted); }}
.empty {{ color: var(--muted); margin-top: 12px; }}
.search {{ width: 100%; margin-top: 18px; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--line); background: #0d0e0f; color: var(--text); font: inherit; outline: none; }}
.search:focus {{ border-color: var(--accent); }}
@media (max-width: 900px) {{
  .shell {{ grid-template-columns: 1fr; grid-template-rows: 62vh 38vh; }}
  .stage {{ border-right: 0; border-bottom: 1px solid var(--line); }}
}}
</style>
</head>
<body>
<main class="shell">
  <section class="stage">
    <div class="top">
      <div>
        <div class="eyebrow">Nipux memory graph</div>
        <h1>{title}</h1>
      </div>
      <div class="stats">
        <div><b id="node-count">0</b><br>nodes</div>
        <div><b id="edge-count">0</b><br>links</div>
      </div>
    </div>
    <canvas id="graph"></canvas>
    <div class="help">drag to rotate · scroll to zoom · click a node</div>
  </section>
  <aside>
    <div class="label">inspect</div>
    <input id="search" class="search" placeholder="search nodes">
    <div id="details" class="card">
      <div class="label">selected node</div>
      <div class="empty">Click a node to inspect its summary, evidence, and links.</div>
    </div>
    <div id="results" class="card">
      <div class="label">visible nodes</div>
      <div class="empty">No nodes yet. The worker can create graph memory with record_memory_graph.</div>
    </div>
  </aside>
</main>
<script id="graph-data" type="application/json">{data}</script>
<script>
const data = JSON.parse(document.getElementById("graph-data").textContent);
const canvas = document.getElementById("graph");
const ctx = canvas.getContext("2d");
const details = document.getElementById("details");
const results = document.getElementById("results");
const search = document.getElementById("search");
document.getElementById("node-count").textContent = data.nodes.length;
document.getElementById("edge-count").textContent = data.edges.length;

let width = 0, height = 0, zoom = 1, rotX = -0.35, rotY = 0.65, dragging = false, last = [0, 0], selected = null;
let lastResultsSignature = "";
const nodeByKey = new Map(data.nodes.map((node, index) => [node.key, {{ ...node, index }}]));
const nodes = data.nodes.map((node, index) => {{
  const a = index * 2.399963229728653;
  const r = 110 + (index % 7) * 24;
  const z = ((index * 53) % 240) - 120;
  return {{ ...node, x: Math.cos(a) * r, y: Math.sin(a) * r, z, vx: 0, vy: 0, vz: 0, screen: [0, 0], visible: true }};
}});
const nodeLookup = new Map(nodes.map(node => [node.key, node]));
const edges = data.edges.map(edge => ({{ ...edge, from: nodeLookup.get(edge.from_key), to: nodeLookup.get(edge.to_key) }})).filter(edge => edge.from && edge.to);

function resize() {{
  const ratio = window.devicePixelRatio || 1;
  width = canvas.clientWidth;
  height = canvas.clientHeight;
  canvas.width = Math.max(1, width * ratio);
  canvas.height = Math.max(1, height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
}}
window.addEventListener("resize", resize);
resize();

function project(node) {{
  const cy = Math.cos(rotY), sy = Math.sin(rotY), cx = Math.cos(rotX), sx = Math.sin(rotX);
  let x = node.x * cy - node.z * sy;
  let z = node.x * sy + node.z * cy;
  let y = node.y * cx - z * sx;
  z = node.y * sx + z * cx;
  const scale = zoom * 520 / (520 + z);
  return [width / 2 + x * scale, height / 2 + y * scale, scale, z];
}}

function color(node) {{
  if (node.status === "deprecated") return "#7d7d77";
  if (node.kind === "question") return "#e6d06f";
  if (node.kind === "skill" || node.kind === "strategy") return "#82e6e1";
  if (node.kind === "decision" || node.kind === "constraint") return "#c99ce8";
  return "#eeeeea";
}}

function draw() {{
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#101112";
  ctx.fillRect(0, 0, width, height);
  const q = search.value.trim().toLowerCase();
  for (const node of nodes) {{
    const hay = [node.key, node.title, node.kind, node.status, node.summary, ...(node.tags || [])].join(" ").toLowerCase();
    node.visible = !q || hay.includes(q);
  }}
  for (const edge of edges) {{
    if (!edge.from.visible || !edge.to.visible) continue;
    const a = project(edge.from), b = project(edge.to);
    ctx.strokeStyle = "rgba(154,154,148,.26)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    ctx.stroke();
  }}
  const sorted = [...nodes].map(node => [node, project(node)]).sort((a, b) => a[1][3] - b[1][3]);
  for (const [node, p] of sorted) {{
    node.screen = p;
    if (!node.visible) continue;
    const radius = Math.max(5, 9 * p[2]);
    ctx.fillStyle = color(node);
    ctx.globalAlpha = selected && selected.key !== node.key ? .55 : 1;
    ctx.beginPath();
    ctx.arc(p[0], p[1], radius, 0, Math.PI * 2);
    ctx.fill();
    if (selected && selected.key === node.key) {{
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 2;
      ctx.stroke();
    }}
  }}
  ctx.globalAlpha = 1;
  renderResults(q);
  requestAnimationFrame(draw);
}}

function renderResults(query) {{
  const visible = nodes.filter(node => node.visible).slice(0, 18);
  const signature = query + "|" + visible.map(node => node.key).join(",");
  if (signature === lastResultsSignature) return;
  lastResultsSignature = signature;
  results.innerHTML = '<div class="label">visible nodes</div>' + (
    visible.length
      ? visible.map(node => `<div class="row"><span>${{escapeHtml(node.kind)}}</span><a href="#" data-key="${{escapeHtml(node.key)}}">${{escapeHtml(node.title || node.key)}}</a></div>`).join("")
      : '<div class="empty">No nodes match the current search.</div>'
  );
  for (const link of results.querySelectorAll("a[data-key]")) {{
    link.addEventListener("click", event => {{
      event.preventDefault();
      selectNode(nodeLookup.get(link.dataset.key));
    }});
  }}
}}

function selectNode(node) {{
  selected = node;
  if (!node) return;
  const linked = edges.filter(edge => edge.from.key === node.key || edge.to.key === node.key).slice(0, 12);
  details.innerHTML = `
    <div class="label">${{escapeHtml(node.kind)}} · ${{escapeHtml(node.status)}}</div>
    <div class="node-title">${{escapeHtml(node.title || node.key)}}</div>
    <div class="row"><span>key</span><div>${{escapeHtml(node.key)}}</div></div>
    <div class="row"><span>summary</span><div>${{escapeHtml(node.summary || "No summary recorded.")}}</div></div>
    <div class="row"><span>score</span><div>salience ${{node.salience ?? "n/a"}} · confidence ${{node.confidence ?? "n/a"}}</div></div>
    <div class="row"><span>tags</span><div>${{(node.tags || []).map(tag => `<span class="pill">${{escapeHtml(tag)}}</span>`).join("") || "none"}}</div></div>
    <div class="row"><span>evidence</span><ul class="list">${{(node.evidence_refs || []).map(ref => `<li>${{escapeHtml(ref)}}</li>`).join("") || "<li>none</li>"}}</ul></div>
    <div class="row"><span>links</span><ul class="list">${{linked.map(edge => `<li>${{escapeHtml(edge.from.key === node.key ? edge.relation + " → " + edge.to.key : edge.relation + " ← " + edge.from.key)}}</li>`).join("") || "<li>none</li>"}}</ul></div>
  `;
}}

function escapeHtml(value) {{
  return String(value ?? "").replace(/[&<>"']/g, char => ({{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }}[char]));
}}

canvas.addEventListener("mousedown", event => {{ dragging = true; last = [event.clientX, event.clientY]; }});
window.addEventListener("mouseup", () => dragging = false);
window.addEventListener("mousemove", event => {{
  if (!dragging) return;
  rotY += (event.clientX - last[0]) * 0.006;
  rotX += (event.clientY - last[1]) * 0.006;
  last = [event.clientX, event.clientY];
}});
canvas.addEventListener("wheel", event => {{
  event.preventDefault();
  zoom = Math.max(.35, Math.min(3.2, zoom * (event.deltaY > 0 ? .92 : 1.08)));
}}, {{ passive: false }});
canvas.addEventListener("click", event => {{
  const rect = canvas.getBoundingClientRect();
  const x = event.clientX - rect.left, y = event.clientY - rect.top;
  let best = null, bestDistance = 18;
  for (const node of nodes) {{
    if (!node.visible) continue;
    const dx = node.screen[0] - x, dy = node.screen[1] - y;
    const distance = Math.hypot(dx, dy);
    if (distance < bestDistance) {{ best = node; bestDistance = distance; }}
  }}
  if (best) selectNode(best);
}});
search.addEventListener("input", () => {{}});
if (nodes[0]) selectNode(nodes[0]);
draw();
</script>
</body>
</html>
"""


def _view_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": str(node.get("key") or node.get("title") or "memory"),
        "title": str(node.get("title") or node.get("key") or "memory"),
        "kind": str(node.get("kind") or "fact"),
        "status": str(node.get("status") or "active"),
        "summary": str(node.get("summary") or ""),
        "salience": node.get("salience"),
        "confidence": node.get("confidence"),
        "tags": _string_list(node.get("tags")),
        "evidence_refs": _string_list(node.get("evidence_refs")),
        "created_at": str(node.get("created_at") or ""),
        "updated_at": str(node.get("updated_at") or ""),
    }


def _view_edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "from_key": str(edge.get("from_key") or ""),
        "to_key": str(edge.get("to_key") or ""),
        "relation": str(edge.get("relation") or "related_to"),
        "summary": str(edge.get("summary") or ""),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [" ".join(str(item).split()) for item in value if str(item).strip()]
