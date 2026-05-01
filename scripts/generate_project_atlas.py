#!/usr/bin/env python3
"""Generate docs/project-atlas.html from the tracked Nipux source tree."""

from __future__ import annotations

import ast
import html
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "project-atlas.html"
SOURCE_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml"}
EXCLUDED = {
    "docs/project-atlas.html",
    "uv.lock",
}


@dataclass
class SourceFile:
    path: str
    text: str
    lines: list[str]
    tree: ast.AST | None
    error: str = ""


@dataclass
class Symbol:
    path: str
    kind: str
    name: str
    line: int
    end_line: int
    doc: str
    calls: list[str]


@dataclass
class Prompt:
    path: str
    name: str
    line: int
    text: str
    context: str


def main() -> None:
    files = load_source_files()
    symbols = extract_symbols(files)
    prompts = extract_prompts(files)
    tools = extract_tools(files)
    tables = extract_tables(files)
    commit = git(["rev-parse", "--short", "HEAD"]) or "working-tree"
    html_text = render(files, symbols, prompts, tools, tables, commit=commit)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html_text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(html_text):,} chars)")


def load_source_files() -> list[SourceFile]:
    paths = tracked_paths()
    files: list[SourceFile] = []
    for path in paths:
        if path in EXCLUDED:
            continue
        full = ROOT / path
        if full.suffix not in SOURCE_SUFFIXES or not full.is_file():
            continue
        text = full.read_text(encoding="utf-8", errors="replace")
        tree = None
        error = ""
        if full.suffix == ".py":
            try:
                tree = ast.parse(text, filename=path)
            except SyntaxError as exc:
                error = str(exc)
        files.append(SourceFile(path=path, text=text, lines=text.splitlines(), tree=tree, error=error))
    return files


def tracked_paths() -> list[str]:
    output = git(["ls-files"])
    if not output:
        return []
    return sorted(line.strip() for line in output.splitlines() if line.strip())


def git(args: list[str]) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, check=False, capture_output=True, text=True)
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def extract_symbols(files: list[SourceFile]) -> list[Symbol]:
    symbols: list[Symbol] = []
    for source in files:
        if source.tree is None:
            continue
        for node in ast.walk(source.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                calls = sorted(call_names(node))[:16]
                symbols.append(
                    Symbol(
                        path=source.path,
                        kind="class" if isinstance(node, ast.ClassDef) else "function",
                        name=node.name,
                        line=getattr(node, "lineno", 0),
                        end_line=getattr(node, "end_lineno", getattr(node, "lineno", 0)),
                        doc=ast.get_docstring(node) or "",
                        calls=calls,
                    )
                )
    return sorted(symbols, key=lambda item: (item.path, item.line, item.name))


def call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = dotted_name(child.func)
        if name:
            names.add(name)
    return names


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def extract_prompts(files: list[SourceFile]) -> list[Prompt]:
    prompts: list[Prompt] = []
    for source in files:
        if source.tree is None:
            continue
        for node in ast.walk(source.tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                names = assignment_names(node)
                value = getattr(node, "value", None)
                text = literal_string(value)
                if not text:
                    continue
                if any(is_prompt_name(name) for name in names) or is_prompt_text(text):
                    prompts.append(
                        Prompt(
                            path=source.path,
                            name=", ".join(names) or "string",
                            line=getattr(node, "lineno", 0),
                            text=text,
                            context="assignment",
                        )
                    )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and is_prompt_text(node.value):
                prompts.append(
                    Prompt(
                        path=source.path,
                        name="inline string",
                        line=getattr(node, "lineno", 0),
                        text=node.value,
                        context="inline",
                    )
                )
    deduped: dict[tuple[str, int, str], Prompt] = {}
    for prompt in prompts:
        deduped[(prompt.path, prompt.line, prompt.text[:120])] = prompt
    return sorted(deduped.values(), key=lambda item: (item.path, item.line))


def assignment_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
    return names


def literal_string(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
    return ""


def is_prompt_name(name: str) -> bool:
    upper = name.upper()
    return any(term in upper for term in ("PROMPT", "SYSTEM", "INSTRUCTION", "GUIDANCE")) and not upper.endswith("_PATH")


def is_prompt_text(text: str) -> bool:
    clean = " ".join(text.split())
    if len(clean) < 240:
        return False
    lowered = clean.lower()
    return (
        "you are" in lowered
        or "do not" in lowered and "use " in lowered
        or "operator" in lowered and "context" in lowered and "prompt" in lowered
        or "next-action" in lowered
    )


def extract_tools(files: list[SourceFile]) -> list[dict[str, str]]:
    tools_text = next((source.text for source in files if source.path == "nipux_cli/tools.py"), "")
    tools: list[dict[str, str]] = []
    pattern = re.compile(r"ToolSpec\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", re.S)
    for match in pattern.finditer(tools_text):
        line = tools_text[: match.start()].count("\n") + 1
        tools.append({"name": match.group(1), "description": " ".join(match.group(2).split()), "line": str(line)})
    return tools


def extract_tables(files: list[SourceFile]) -> list[dict[str, Any]]:
    db_text = next((source.text for source in files if source.path == "nipux_cli/db.py"), "")
    tables: list[dict[str, Any]] = []
    for match in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*?)\)", db_text, re.S):
        raw_columns = [line.strip().rstrip(",") for line in match.group(2).splitlines()]
        columns = [line for line in raw_columns if line and not line.upper().startswith(("FOREIGN", "UNIQUE", "PRIMARY KEY"))]
        tables.append({"name": match.group(1), "columns": columns, "line": db_text[: match.start()].count("\n") + 1})
    return tables


def render(
    files: list[SourceFile],
    symbols: list[Symbol],
    prompts: list[Prompt],
    tools: list[dict[str, str]],
    tables: list[dict[str, Any]],
    *,
    commit: str,
) -> str:
    python_files = [source for source in files if source.path.endswith(".py")]
    total_lines = sum(len(source.lines) for source in files)
    file_cards = "\n".join(render_file_card(source, symbols) for source in files)
    source_browser = "\n".join(render_source_file(source) for source in files)
    symbol_cards = "\n".join(render_symbol(symbol) for symbol in symbols)
    prompt_cards = "\n".join(render_prompt(prompt) for prompt in prompts[:80])
    tool_rows = "\n".join(
        f"<tr><td><code>{esc(tool['name'])}</code></td><td>{esc(tool['description'])}</td><td>{tool['line']}</td></tr>"
        for tool in tools
    )
    table_cards = "\n".join(render_table(table) for table in tables)
    risk_cards = render_review_points(files, symbols, prompts, tools)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nipux Project Atlas</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #080909; --panel: #101112; --panel-2: #151717; --text: #ecebe6;
  --muted: #9b9b96; --faint: #5f615e; --line: #303332; --accent: #9ad6d1;
  --accent-2: #d8d06d; --warn: #ee9b66; --bad: #e36d78; --green: #9fca7f;
  --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  --sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: radial-gradient(circle at 70% 0%, rgba(154,214,209,.10), transparent 36%), var(--bg); color: var(--text); font: 15px/1.55 var(--sans); }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.shell {{ display: grid; grid-template-columns: 292px minmax(0, 1fr); min-height: 100vh; }}
.sidebar {{ position: sticky; top: 0; height: 100vh; overflow: auto; border-right: 1px solid var(--line); padding: 24px 22px; background: rgba(8,9,9,.94); }}
.logo {{ font: 750 22px var(--mono); letter-spacing: .08em; color: var(--accent); }}
.subtitle {{ color: var(--muted); margin: 6px 0 22px; }}
.search {{ width: 100%; background: #050606; border: 1px solid var(--line); color: var(--text); border-radius: 8px; padding: 11px 12px; font: 14px var(--mono); outline: none; }}
.search:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px rgba(154,214,209,.12); }}
.nav {{ margin: 22px 0; display: grid; gap: 7px; }}
.nav a {{ color: var(--muted); padding: 6px 0; font: 13px var(--mono); text-transform: uppercase; letter-spacing: .12em; }}
.stats {{ margin-top: 24px; display: grid; gap: 10px; }}
.stat {{ border: 1px solid var(--line); background: var(--panel); border-radius: 10px; padding: 12px; }}
.stat b {{ display: block; font: 650 24px/1 var(--mono); }}
.stat span {{ color: var(--muted); font: 12px var(--mono); text-transform: uppercase; letter-spacing: .12em; }}
main {{ min-width: 0; padding: 34px 38px 80px; }}
.hero {{ border-bottom: 1px solid var(--line); padding-bottom: 28px; margin-bottom: 28px; }}
.eyebrow {{ color: var(--accent); font: 12px var(--mono); text-transform: uppercase; letter-spacing: .22em; }}
h1 {{ font-size: clamp(38px, 6vw, 86px); line-height: .9; margin: 14px 0 18px; letter-spacing: -.04em; }}
h2 {{ margin: 0; font-size: 28px; letter-spacing: -.02em; }}
h3 {{ margin: 0 0 6px; font-size: 16px; }}
.lede {{ max-width: 980px; color: #c7c6bf; font-size: 19px; }}
.section {{ margin: 44px 0; scroll-margin-top: 20px; }}
.section > header {{ display: flex; align-items: end; justify-content: space-between; gap: 20px; border-bottom: 1px solid var(--line); margin-bottom: 18px; padding-bottom: 10px; }}
.kicker {{ color: var(--muted); font: 12px var(--mono); text-transform: uppercase; letter-spacing: .16em; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }}
.card, .file-card, .prompt, .tool, .db-card, .symbol {{ border: 1px solid var(--line); background: linear-gradient(180deg, rgba(255,255,255,.035), rgba(255,255,255,.012)); border-radius: 12px; padding: 16px; overflow: hidden; }}
.file-card header, .prompt header, .tool header {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; border-bottom: 1px solid rgba(255,255,255,.06); margin: -2px 0 10px; padding-bottom: 8px; }}
.muted, .file-card header span, .prompt header span, .tool header span {{ color: var(--muted); }}
.meta {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }}
.meta span, .pill {{ border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px; color: var(--muted); font: 12px var(--mono); }}
.arch {{ display: grid; grid-template-columns: repeat(3, minmax(210px, 1fr)); gap: 12px; }}
.node {{ text-align: left; min-height: 126px; cursor: pointer; border: 1px solid var(--line); color: var(--text); background: var(--panel); border-radius: 14px; padding: 15px; font: inherit; transition: .15s ease; }}
.node:hover {{ transform: translateY(-2px); border-color: var(--accent); background: var(--panel-2); }}
.node strong {{ display: block; font-size: 17px; }}
.node span {{ display: block; color: var(--accent); font: 12px var(--mono); margin: 4px 0 8px; }}
.node em {{ display: block; color: var(--muted); font-style: normal; }}
.flow {{ counter-reset: flow; display: grid; gap: 10px; padding: 0; list-style: none; }}
.flow li {{ counter-increment: flow; border-left: 2px solid var(--accent); background: var(--panel); padding: 12px 14px 12px 48px; position: relative; border-radius: 8px; }}
.flow li:before {{ content: counter(flow); position: absolute; left: 14px; top: 12px; color: var(--accent-2); font: 700 16px var(--mono); }}
.flow li span {{ display: block; color: var(--muted); }}
details {{ margin-top: 10px; }}
summary {{ cursor: pointer; color: var(--accent); font: 13px var(--mono); }}
table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }}
th, td {{ text-align: left; border-bottom: 1px solid rgba(255,255,255,.07); padding: 7px 8px; vertical-align: top; }}
th {{ color: var(--muted); font: 12px var(--mono); text-transform: uppercase; letter-spacing: .08em; }}
code, pre {{ font-family: var(--mono); }}
pre {{ max-height: 540px; overflow: auto; background: #050606; border: 1px solid var(--line); border-radius: 10px; padding: 14px; color: #dad8cf; white-space: pre-wrap; }}
.mini-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }}
.symbol p {{ margin: 8px 0; }}
.calls {{ color: var(--muted); }}
.warning {{ border-color: rgba(238,155,102,.45); background: rgba(238,155,102,.08); }}
.hidden {{ display: none !important; }}
.source-list {{ display: grid; gap: 10px; }}
.source-file {{ border: 1px solid var(--line); border-radius: 10px; background: var(--panel); padding: 0 12px 10px; }}
.source-file summary {{ padding: 12px 0; color: var(--text); }}
.source-file summary span {{ color: var(--muted); margin-left: 8px; }}
.source-code {{ max-height: 620px; font-size: 12px; line-height: 1.45; white-space: pre; }}
.src-line {{ display: grid; grid-template-columns: 52px minmax(0, 1fr); min-height: 17px; }}
.src-line b {{ color: var(--faint); user-select: none; font-weight: 500; }}
.src-line code {{ color: #d6d3ca; }}
@media (max-width: 980px) {{ .shell {{ grid-template-columns: 1fr; }} .sidebar {{ position: relative; height: auto; }} main {{ padding: 24px 18px 60px; }} .arch {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <div class="logo">NIPUX ATLAS</div>
    <div class="subtitle">Generated from tracked source on {esc(commit)}.</div>
    <input id="search" class="search" placeholder="filter files, prompts, tools..." autocomplete="off">
    <nav class="nav">
      <a href="#architecture">Architecture</a>
      <a href="#runtime-flow">Runtime Flow</a>
      <a href="#prompts">Prompt Surfaces</a>
      <a href="#tools">Tool Registry</a>
      <a href="#database">Database</a>
      <a href="#files">Files</a>
      <a href="#symbols">Symbols</a>
      <a href="#source-browser">Source Browser</a>
      <a href="#tests">Tests</a>
      <a href="#risks">Review Points</a>
    </nav>
    <div class="stats">
      <div class="stat"><b>{len(files)}</b><span>tracked files</span></div>
      <div class="stat"><b>{total_lines:,}</b><span>tracked lines mapped</span></div>
      <div class="stat"><b>{len(symbols)}</b><span>python symbols</span></div>
      <div class="stat"><b>{len(tools)}</b><span>runtime tools</span></div>
    </div>
  </aside>
  <main>
    <section class="hero">
      <div class="eyebrow">Backend map / prompt audit / source index</div>
      <h1>Nipux Project Atlas</h1>
      <p class="lede">A self-contained visual map of the current backend: entrypoints, daemon loop, worker prompt assembly, durable memory, tools, SQLite schema, UI control plane, tests, and every tracked source file with parsed functions/classes and line references.</p>
    </section>

    <section id="architecture" class="section">
      <header><div><div class="kicker">Mind map</div><h2>Architecture</h2></div><span class="muted">Click a node to jump into related detail.</span></header>
      <div class="arch">{architecture_nodes()}</div>
    </section>

    <section id="runtime-flow" class="section">
      <header><div><div class="kicker">Lifecycle</div><h2>Runtime Flow</h2></div><span class="muted">What happens from terminal input to durable progress.</span></header>
      <ol class="flow">{runtime_flow()}</ol>
    </section>

    <section id="prompts" class="section">
      <header><div><div class="kicker">Exact text</div><h2>Prompt Surfaces</h2></div><span class="muted">System/program prompts and instruction-like strings extracted from source.</span></header>
      <div class="grid">{prompt_cards}</div>
    </section>

    <section id="tools" class="section">
      <header><div><div class="kicker">Tools</div><h2>Tool Registry</h2></div><span class="muted">Static ToolSpec definitions from nipux_cli/tools.py.</span></header>
      <table><thead><tr><th>Name</th><th>Description</th><th>Line</th></tr></thead><tbody>{tool_rows}</tbody></table>
    </section>

    <section id="database" class="section">
      <header><div><div class="kicker">Persistence</div><h2>SQLite Tables</h2></div><span class="muted">CREATE TABLE blocks found in nipux_cli/db.py.</span></header>
      <div class="grid">{table_cards}</div>
    </section>

    <section id="files" class="section">
      <header><div><div class="kicker">Source index</div><h2>Important Files</h2></div><span class="muted">{len(python_files)} Python modules plus docs/config files.</span></header>
      <div class="grid">{file_cards}</div>
    </section>

    <section id="symbols" class="section">
      <header><div><div class="kicker">Functions and classes</div><h2>Symbol Map</h2></div><span class="muted">Parsed with Python AST.</span></header>
      <div class="mini-grid">{symbol_cards}</div>
    </section>

    <section id="source-browser" class="section">
      <header><div><div class="kicker">Line-by-line</div><h2>Source Browser</h2></div><span class="muted">Collapsed raw tracked source so the backend can be inspected directly in this page.</span></header>
      <div class="source-list">{source_browser}</div>
    </section>

    <section id="tests" class="section">
      <header><div><div class="kicker">Verification</div><h2>Test Coverage Map</h2></div><span class="muted">Test files included in the source index.</span></header>
      <div class="grid">{test_cards(files)}</div>
    </section>

    <section id="risks" class="section">
      <header><div><div class="kicker">Audit cues</div><h2>Review Points</h2></div><span class="muted">Generated signals for where to inspect next.</span></header>
      <div class="grid">{risk_cards}</div>
    </section>
  </main>
</div>
<script>
const search = document.getElementById('search');
search?.addEventListener('input', () => {{
  const term = search.value.toLowerCase().trim();
  document.querySelectorAll('.searchable').forEach((node) => {{
    const hay = (node.getAttribute('data-search') || node.textContent || '').toLowerCase();
    node.classList.toggle('hidden', term && !hay.includes(term));
  }});
}});
document.querySelectorAll('.node[data-target]').forEach((node) => {{
  node.addEventListener('click', () => {{
    const target = document.getElementById(node.getAttribute('data-target'));
    if (target) target.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  }});
}});
</script>
</body>
</html>
"""


def architecture_nodes() -> str:
    nodes = [
        ("cli-tui", "CLI / TUI", "nipux_cli/cli.py", "Chat-first terminal UI, first-run menu, slash commands, job switching, event panes."),
        ("sqlite-state", "SQLite state", "nipux_cli/db.py", "Jobs, runs, steps, artifacts, events, ledgers, usage, and memory index."),
        ("daemon", "Daemon", "nipux_cli/daemon.py", "Single-instance forever loop, stale runtime fingerprint, heartbeat, work scheduling."),
        ("worker-loop", "Worker loop", "nipux_cli/worker.py", "Builds prompts, chooses one tool step, guards loops, records durable progress."),
        ("llm-adapter", "LLM adapter", "nipux_cli/llm.py", "OpenAI-compatible chat calls, usage/cost tracking, tool call parsing."),
        ("tool-registry", "Tool registry", "nipux_cli/tools.py", "Browser, web, shell, artifact, ledger, task, experiment, digest tools."),
        ("browser-web", "Browser/web", "nipux_cli/browser.py / web.py", "Visible browsing, snapshots, search/extract helpers, anti-bot source scoring."),
        ("artifacts-files", "Artifacts/files", "nipux_cli/artifacts.py", "Saved outputs and concrete workspace file writing."),
        ("memory", "Memory", "compression.py / operator_context.py", "Compact rolling memory and durable operator context."),
    ]
    return "".join(
        f"<button id='{esc(anchor)}' class='node searchable' data-target='source-browser' data-search='{esc(title + ' ' + path + ' ' + desc)}'>"
        f"<strong>{esc(title)}</strong><span>{esc(path)}</span><em>{esc(desc)}</em></button>"
        for anchor, title, path, desc in nodes
    )


def runtime_flow() -> str:
    steps = [
        ("Startup", "pyproject entrypoint calls nipux_cli.cli:main. With no args, the chat/TUI opens on the focused job or first-run workspace."),
        ("Operator input", "Plain chat is stored as visible events and, when relevant, durable operator context for future worker prompts."),
        ("Daemon scheduling", "The daemon claims runnable jobs, keeps a lock/heartbeat, starts runs, and calls one bounded worker step repeatedly."),
        ("Prompt assembly", "worker.build_messages layers system prompt, program template, operator context, roadmaps, tasks, ledgers, experiments, memory, timeline, and recent steps."),
        ("Tool call", "The LLM selects one OpenAI-style tool. The registry executes it with ToolContext and stores input/output in steps/events."),
        ("Progress accounting", "Guards require artifacts, findings, tasks, experiments, or milestone validation when evidence or measurements appear."),
        ("Persistence", "Artifacts go to the job output directory. SQLite stores steps, events, ledgers, runtime state, and usage/cost metadata."),
        ("UI refresh", "The TUI reads timeline/events and compact job metrics, splitting chat from worker activity and status."),
    ]
    return "".join(f"<li><strong>{esc(title)}</strong><span>{esc(body)}</span></li>" for title, body in steps)


def render_file_card(source: SourceFile, symbols: list[Symbol]) -> str:
    local_symbols = [symbol for symbol in symbols if symbol.path == source.path]
    top_names = ", ".join(symbol.name for symbol in local_symbols[:10]) or "none"
    doc = module_doc(source) or source.error or "No module docstring."
    imports = ", ".join(module_imports(source)[:12]) or "none"
    return f"""<article class="file-card searchable" data-search="{esc(source.path + ' ' + doc + ' ' + top_names)}">
<header><h3>{esc(source.path)}</h3><span>{len(source.lines)} lines</span></header>
<p>{esc(short(doc, 260))}</p>
<div class="meta"><span>{len(local_symbols)} symbols</span><span>{source.text.count('TODO')} TODOs</span></div>
<details><summary>Imports and top symbols</summary><p><strong>Imports:</strong> {esc(imports)}</p><p><strong>Symbols:</strong> {esc(top_names)}</p></details>
</article>"""


def module_doc(source: SourceFile) -> str:
    if source.tree is None:
        for line in source.lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped
        return ""
    return ast.get_docstring(source.tree) or ""


def module_imports(source: SourceFile) -> list[str]:
    if source.tree is None:
        return []
    names: list[str] = []
    for node in source.tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            names.append(module)
    return names


def render_source_file(source: SourceFile) -> str:
    code = "\n".join(
        f"<span class='src-line'><b>{index:>4}</b><code>{esc(line)}</code></span>"
        for index, line in enumerate(source.lines, start=1)
    )
    return f"""<details class="source-file searchable" data-search="{esc(source.path + ' ' + source.text[:4000])}">
<summary>{esc(source.path)} <span>{len(source.lines)} lines</span></summary>
<pre class="source-code">{code}</pre>
</details>"""


def render_symbol(symbol: Symbol) -> str:
    doc = short(symbol.doc or "No docstring.", 180)
    calls = ", ".join(symbol.calls) or "none"
    return f"""<article class="symbol searchable" data-search="{esc(symbol.path + ' ' + symbol.name + ' ' + doc + ' ' + calls)}">
<h3>{esc(symbol.name)}</h3>
<p><span class="pill">{esc(symbol.kind)}</span> <span class="pill">{esc(symbol.path)}:{symbol.line}</span></p>
<p>{esc(doc)}</p>
<p class="calls"><strong>Calls:</strong> {esc(short(calls, 280))}</p>
</article>"""


def render_prompt(prompt: Prompt) -> str:
    title = prompt.name or "prompt"
    return f"""<article class="prompt searchable" data-search="{esc(prompt.path + ' ' + title + ' ' + prompt.text)}">
<header><h3>{esc(title)}</h3><span>{esc(prompt.path)}:{prompt.line}</span></header>
<p class="muted">Context: <code>{esc(prompt.context)}</code> · {len(prompt.text):,} chars</p>
<pre><code>{esc(prompt.text)}</code></pre>
</article>"""


def render_table(table: dict[str, Any]) -> str:
    columns = "".join(f"<li><code>{esc(column)}</code></li>" for column in table["columns"][:40])
    return f"""<article class="db-card searchable" data-search="{esc(table['name'] + ' ' + ' '.join(table['columns']))}">
<h3>{esc(table['name'])}</h3>
<p class="muted">nipux_cli/db.py:{table['line']}</p>
<ul>{columns}</ul>
</article>"""


def test_cards(files: list[SourceFile]) -> str:
    tests = [source for source in files if source.path.startswith("tests/")]
    cards = []
    for source in tests:
        names = []
        if source.tree:
            names = [node.name for node in ast.walk(source.tree) if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
        cards.append(
            f"""<article class="test-card searchable" data-search="{esc(source.path + ' ' + ' '.join(names))}">
<h3>{esc(source.path)}</h3><p><strong>{len(names)}</strong> tests · {len(source.lines)} lines</p>
<p class="muted">{esc(short(', '.join(names), 320))}</p></article>"""
        )
    return "\n".join(cards)


def render_review_points(
    files: list[SourceFile],
    symbols: list[Symbol],
    prompts: list[Prompt],
    tools: list[dict[str, str]],
) -> str:
    largest = sorted(files, key=lambda source: len(source.lines), reverse=True)[:5]
    large_text = ", ".join(f"{source.path} ({len(source.lines)} lines)" for source in largest)
    prompt_text = f"{len(prompts)} prompt/instruction-like strings were extracted. Inspect this section after any agent-behavior change."
    tool_text = f"{len(tools)} tools are exposed to the worker. Review descriptions whenever generic behavior changes."
    symbol_text = f"{len(symbols)} symbols were parsed. Large modules are candidates for refactoring once behavior stabilizes."
    cards = [
        ("Large modules", large_text),
        ("Prompt surfaces", prompt_text),
        ("Tool surface", tool_text),
        ("Symbol map", symbol_text),
    ]
    return "\n".join(
        f"<article class='card warning searchable' data-search='{esc(title + ' ' + body)}'><h3>{esc(title)}</h3><p>{esc(body)}</p></article>"
        for title, body in cards
    )


def short(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)] + "..."


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


if __name__ == "__main__":
    main()
