"""Thin CLI for the Nipux agent runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import termios
import threading
import textwrap
import time
import tty
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from nipux_cli import __version__
from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import (
    DEFAULT_BASE_URL,
    DEFAULT_API_KEY_ENV,
    DEFAULT_CONTEXT_LENGTH,
    DEFAULT_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    default_config_yaml,
    get_agent_home,
    load_config,
)
from nipux_cli.daemon import Daemon, DaemonAlreadyRunning, daemon_lock_status, read_daemon_events
from nipux_cli.dashboard import collect_dashboard_state, render_dashboard, render_overview
from nipux_cli.db import AgentDB
from nipux_cli.digest import render_job_digest, write_daily_digest
from nipux_cli.doctor import run_doctor
from nipux_cli.operator_context import active_prompt_operator_entries
from nipux_cli.planning import (
    format_initial_plan,
    initial_plan_for_objective,
    initial_roadmap_for_objective,
    initial_task_contract,
)
from nipux_cli.templates import program_for_job
from nipux_cli.tui_commands import (
    CHAT_SETTING_COMMANDS,
    CHAT_SLASH_COMMANDS,
    FIRST_RUN_ACTIONS,
    FIRST_RUN_SLASH_COMMANDS,
    SETTINGS_FIELD_TYPES,
    autocomplete_slash as _autocomplete_slash,
    slash_suggestion_lines as _slash_suggestion_lines,
)
from nipux_cli.tui_events import (
    CHAT_RIGHT_PAGES,
    append_chat_output as _append_chat_output,
    chat_event_parts as _chat_event_parts,
    clean_step_summary as _clean_step_summary,
    experiment_metric_text as _experiment_metric_text,
    friendly_error_text as _friendly_error_text,
    generic_display_text as _generic_display_text,
    live_badge as _live_badge,
    minimal_live_event_line as _minimal_live_event_line,
    model_update_event_parts as _model_update_event_parts,
    worker_activity_lines as _worker_activity_lines,
)
from nipux_cli.tui_layout import (
    _compose_bar,
    _metric_strip,
    _top_bar,
    _two_col_line,
    _two_col_title,
)
from nipux_cli.tui_style import (
    _accent,
    _bold,
    _center_ansi,
    _event_badge,
    _fancy_ui,
    _fit_ansi,
    _muted,
    _one_line,
    _page_indicator,
    _status_badge,
    _strip_ansi,
    _style,
    _themed_lines,
)


SHELL_BUILTINS = {"help", "?", "commands", "exit", "quit", ":q", "clear"}
SHELL_COMMAND_NAMES = {
    "init",
    "create",
    "jobs",
    "ls",
    "focus",
    "rename",
    "delete",
    "rm",
    "chat",
    "shell",
    "status",
    "health",
    "history",
    "events",
    "activity",
    "feed",
    "tail",
    "updates",
    "findings",
    "tasks",
    "roadmap",
    "experiments",
    "update",
    "dashboard",
    "dash",
    "start",
    "stop",
    "restart",
    "browser-dashboard",
    "artifacts",
    "artifact",
    "lessons",
    "learn",
    "findings",
    "sources",
    "memory",
    "metrics",
    "logs",
    "outputs",
    "output",
    "watch",
    "run-one",
    "run",
    "work",
    "steer",
    "say",
    "pause",
    "resume",
    "cancel",
    "digest",
    "daily-digest",
    "daemon",
    "doctor",
    "autostart",
    "service",
}

NIPUX_BANNER = r"""
 _   _ _                  ____ _     ___
| \ | (_)_ __  _   ___  _/ ___| |   |_ _|
|  \| | | '_ \| | | \ \/ / |   | |    | |
| |\  | | |_) | |_| |>  <| |___| |___ | |
|_| \_|_| .__/ \__,_/_/\_\\____|_____|___|
        |_|
""".strip("\n")

NATURAL_COMMANDS = {
    "tell me updates": "updates",
    "show updates": "updates",
    "show history": "history",
    "what happened": "history",
    "show events": "events",
    "what did it find": "updates",
    "what did you find": "updates",
    "what has it found": "updates",
    "findings": "findings",
    "tasks": "tasks",
    "roadmap": "roadmap",
    "show roadmap": "roadmap",
    "show artifacts": "artifacts",
    "where are artifacts": "artifacts",
    "show lessons": "lessons",
    "what did it learn": "lessons",
    "show findings": "findings",
    "show tasks": "tasks",
    "show experiments": "experiments",
    "show sources": "sources",
    "show memory": "memory",
    "show metrics": "metrics",
    "what is going on": "status",
    "whats going on": "status",
    "what's going on": "status",
    "what are you doing": "status",
    "what is it doing": "status",
    "how is it going": "status",
    "how are things going": "status",
    "check up on things": "status",
    "is it running": "health",
    "is the daemon running": "health",
    "daemon health": "health",
    "show health": "health",
    "show activity": "activity",
    "show tool calls": "activity",
}

def _db() -> tuple[AgentDB, object]:
    config = load_config()
    config.ensure_dirs()
    return AgentDB(config.runtime.state_db_path), config


def cmd_init(args: argparse.Namespace) -> None:
    config = load_config()
    config.ensure_dirs()
    path = Path(args.path).expanduser() if args.path else config.runtime.home / "config.yaml"
    if path.exists() and not args.force:
        print(f"Config already exists: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    model = args.model or DEFAULT_MODEL
    base_url = args.base_url or DEFAULT_BASE_URL
    api_key_env = args.api_key_env or DEFAULT_API_KEY_ENV
    if args.openrouter:
        base_url = args.base_url or "https://openrouter.ai/api/v1"
        api_key_env = args.api_key_env or DEFAULT_API_KEY_ENV
        model = args.model or DEFAULT_OPENROUTER_MODEL
    path.write_text(
        default_config_yaml(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            context_length=args.context_length,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {path}")
    env_path = config.runtime.home / ".env"
    if not env_path.exists():
        env_path.write_text(
            f"# Optional local secrets for Nipux. This file stays outside the git repo.\n{api_key_env}=\n",
            encoding="utf-8",
        )
        try:
            env_path.chmod(0o600)
        except OSError:
            pass
        print(f"Wrote {env_path} (fill {api_key_env}; do not commit secrets)")


def cmd_create(args: argparse.Namespace) -> None:
    job_id, title = _create_job(
        objective=args.objective,
        title=args.title,
        kind=args.kind,
        cadence=args.cadence,
    )
    print(f"created {title}")


def _create_job(
    *, objective: str, title: str | None = None, kind: str = "generic", cadence: str | None = None
) -> tuple[str, str]:
    db, config = _db()
    try:
        title = title or objective.strip().splitlines()[0][:80] or "Untitled job"
        plan = initial_plan_for_objective(objective)
        job_id = db.create_job(
            objective,
            title=title,
            kind=kind,
            cadence=cadence,
            metadata={"planning": plan},
        )
        db.update_job_status(job_id, "queued", metadata_patch={"planning": plan, "planning_status": "auto_accepted"})
        db.append_agent_update(job_id, format_initial_plan(plan), category="plan", metadata={"planning": plan})
        db.append_agent_update(job_id, "Plan accepted automatically. I will start working from the planned tasks.", category="plan")
        db.append_roadmap_record(job_id, **initial_roadmap_for_objective(title=title, objective=objective))
        for index, task in enumerate(plan["tasks"], start=1):
            task_contract = initial_task_contract(str(task))
            db.append_task_record(
                job_id,
                title=str(task),
                status="open",
                priority=max(0, 10 - index),
                goal=objective,
                output_contract=task_contract["output_contract"],
                acceptance_criteria=task_contract["acceptance_criteria"],
                evidence_needed=task_contract["evidence_needed"],
                stall_behavior=task_contract["stall_behavior"],
                metadata={"phase": "initial_plan"},
            )
        program = config.runtime.jobs_dir / job_id / "program.md"
        program.parent.mkdir(parents=True, exist_ok=True)
        program.write_text(
            program_for_job(kind=kind, title=title, objective=objective),
            encoding="utf-8",
        )
        _write_shell_state({"focus_job_id": job_id})
        return job_id, title
    finally:
        db.close()


def cmd_jobs(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        jobs = db.list_jobs()
        if not jobs:
            print('No jobs yet. Create one with: nipux create "objective"')
            return
        focused = _configured_focus_job_id(db)
        daemon_running = daemon_lock_status(load_config().runtime.home / "agentd.lock")["running"]
        _print_jobs_panel(jobs, focused_job_id=str(focused or ""), daemon_running=bool(daemon_running))
    finally:
        db.close()


def cmd_focus(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        if not args.query:
            job_id = _default_job_id(db)
            if not job_id:
                print("No focused job. Create one first.")
                return
            job = db.get_job(job_id)
            daemon = daemon_lock_status(load_config().runtime.home / "agentd.lock")
            print(f"focus: {job['title']} | job {_job_display_state(job, bool(daemon['running']))}")
            return
        job = _find_job(db, " ".join(args.query))
        if not job:
            print(f"No job matched: {' '.join(args.query)}")
            return
        _write_shell_state({"focus_job_id": job["id"]})
        daemon = daemon_lock_status(load_config().runtime.home / "agentd.lock")
        print(f"focus set: {job['title']} | job {_job_display_state(job, bool(daemon['running']))}")
    finally:
        db.close()


def cmd_rename(args: argparse.Namespace) -> None:
    db, config = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        old = db.get_job(job_id)
        renamed = db.rename_job(job_id, _job_ref_text(args.title))
        program = config.runtime.jobs_dir / job_id / "program.md"
        if program.exists():
            try:
                content = program.read_text(encoding="utf-8")
                lines = content.splitlines()
                if lines and lines[0].startswith("# "):
                    lines[0] = f"# {renamed['title']}"
                    program.write_text("\n".join(lines) + ("\n" if content.endswith("\n") else ""), encoding="utf-8")
            except OSError:
                pass
        _write_shell_state({"focus_job_id": job_id})
        print(f"renamed {old['title']} -> {renamed['title']}")
    finally:
        db.close()


def cmd_delete(args: argparse.Namespace) -> None:
    db, config = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "usage: delete JOB_TITLE")
            return
        result = db.delete_job(job_id)
        job = result["job"]
    finally:
        db.close()

    removed_files = 0
    if not args.keep_files:
        job_dir = config.runtime.jobs_dir / job_id
        for path_text in result.get("artifact_paths") or []:
            path = Path(path_text)
            try:
                if path.exists() and job_dir in path.parents:
                    path.unlink()
                    removed_files += 1
            except OSError:
                pass
        try:
            if job_dir.exists():
                shutil.rmtree(job_dir)
        except OSError:
            pass
    state = _read_shell_state()
    if state.get("focus_job_id") == job_id:
        _write_shell_state({"focus_job_id": ""})
    counts = result.get("counts") or {}
    file_text = "kept files" if args.keep_files else f"removed files={removed_files}"
    print(
        f"deleted {job['title']} | steps={counts.get('steps', 0)} "
        f"artifacts={counts.get('artifacts', 0)} runs={counts.get('runs', 0)} | {file_text}"
    )


def cmd_chat(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found. Create one first.")
            return
        _write_shell_state({"focus_job_id": job_id})
    finally:
        db.close()

    _enter_chat(job_id, show_history=not args.no_history, history_limit=args.history_limit)


def cmd_home(args: argparse.Namespace) -> None:
    _install_readline_history()
    db, _ = _db()
    try:
        job_id = _default_job_id(db)
    finally:
        db.close()
    if job_id:
        _enter_chat(job_id, show_history=True, history_limit=args.history_limit)
        return

    _enter_first_run_menu(history_limit=args.history_limit)


def _enter_first_run_menu(*, history_limit: int = 12) -> None:
    if _frame_chat_enabled():
        _enter_first_run_frame(history_limit=history_limit)
        return

    print("Nipux CLI")
    print(_rule("="))
    _print_first_run_menu()
    print(_rule("="))
    while True:
        try:
            line = input("nipux > ").strip()
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print()
            continue
        if not _handle_first_run_menu_line(line, history_limit=history_limit):
            return


def _print_first_run_menu() -> None:
    config = load_config()
    daemon = daemon_lock_status(config.runtime.home / "agentd.lock")
    print("Start")
    print(f"  model   {config.model.model}")
    print(f"  daemon  {'running' if daemon['running'] else 'stopped'}")
    print(f"  home    {_short_path(config.runtime.home)}")
    print()
    print("Actions")
    print("  1  new       create a long-running job")
    print("  2  jobs      list saved jobs")
    print("  3  doctor    check local setup")
    print("  4  init      write config/env template")
    print("  5  exit      leave")
    print()
    print('Type an objective, `new OBJECTIVE`, or a command.')


def _handle_first_run_menu_line(line: str, *, history_limit: int = 12) -> bool:
    line = line.strip()
    if not line:
        _print_first_run_menu()
        return True
    if line.startswith("/"):
        line = line[1:].strip()
    lowered = line.lower()
    if lowered in {"exit", "quit", ":q", "5"}:
        return False
    if lowered in {"help", "?", "commands"}:
        _print_first_run_menu()
        return True
    if lowered in {"1", "new"}:
        objective = _prompt_first_run_value("objective")
        if not objective:
            print("No job created.")
            return True
        _first_run_create_and_open(objective, history_limit=history_limit)
        return False
    if lowered.startswith("new "):
        objective = line[4:].strip()
        if not objective:
            print("usage: new OBJECTIVE")
            return True
        _first_run_create_and_open(objective, history_limit=history_limit)
        return False
    if lowered in {"2", "jobs", "ls"}:
        cmd_jobs(argparse.Namespace())
        return True
    if lowered in {"3", "doctor"}:
        try:
            cmd_doctor(argparse.Namespace(check_model=False))
        except SystemExit:
            pass
        return True
    if lowered in {"4", "init"}:
        cmd_init(argparse.Namespace(path=None, force=False))
        return True
    first = _first_token(line)
    if first in SHELL_COMMAND_NAMES:
        before_job_id = None
        if first == "create":
            db, _ = _db()
            try:
                before_job_id = _default_job_id(db)
            finally:
                db.close()
        _run_shell_line(line)
        if first == "create":
            db, _ = _db()
            try:
                after_job_id = _default_job_id(db)
            finally:
                db.close()
            if after_job_id and after_job_id != before_job_id:
                _enter_chat(after_job_id, show_history=True, history_limit=history_limit)
                return False
        return True
    objective = _extract_job_objective_from_message(line)
    if objective:
        _first_run_create_and_open(objective, history_limit=history_limit)
        return False
    print(_first_run_chat_reply(line))
    return True


def _handle_chat_setting_command(command: str, rest: list[str]) -> bool:
    if command in {"key", "api-key"}:
        if not rest:
            config = load_config()
            state = "set" if config.model.api_key else "missing"
            print(f"API key is {state} via {config.model.api_key_env}. Use /api-key KEY to save a new one.")
            return True
        print(_inline_setting_notice("secret:model.api_key", " ".join(rest)))
        return True
    if command not in CHAT_SETTING_COMMANDS:
        return False
    field, placeholder = CHAT_SETTING_COMMANDS[command]
    if not rest:
        current = _config_field_value(field)
        print(f"{field} = {current}")
        print(f"usage: /{command} {placeholder}")
        return True
    print(_inline_setting_notice(field, " ".join(rest)))
    return True


def _capture_setting_command(line: str) -> list[str]:
    try:
        parts = shlex.split(line[1:] if line.startswith("/") else line)
    except ValueError as exc:
        return [f"parse error: {exc}"]
    if not parts:
        return []
    stream = StringIO()
    with redirect_stdout(stream):
        if not _handle_chat_setting_command(parts[0], parts[1:]):
            print(f"unknown config command: /{parts[0]}")
    lines = [" ".join(item.split()) for item in stream.getvalue().splitlines() if item.strip()]
    return lines[-12:] or ["done"]


def _prompt_first_run_value(label: str) -> str:
    try:
        return input(f"{label} > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _first_run_create_and_open(objective: str, *, history_limit: int = 12) -> None:
    job_id, title = _create_job(objective=objective, title=None, kind="generic", cadence=None)
    print(f"created {title}")
    print("Opening workspace. Use the right-side controls to run, switch jobs, or inspect output.")
    _enter_chat(job_id, show_history=True, history_limit=history_limit)


def _first_token(line: str) -> str:
    try:
        parts = shlex.split(line)
    except ValueError:
        parts = line.split()
    return parts[0].lower() if parts else ""


def _enter_first_run_frame(*, history_limit: int = 12) -> None:
    buffer = ""
    notices: list[str] = []
    next_job_id: str | None = None
    view = "start"
    selected = 0
    editing_field: str | None = None
    old_attrs = termios.tcgetattr(sys.stdin)
    print("\033[?1049h\033[H\033[?25l\033[?1000h\033[?1002h\033[?1006h", end="", flush=True)
    try:
        stdin_fd = sys.stdin.fileno()
        tty.setcbreak(stdin_fd)
        needs_render = True
        last_render = 0.0
        while next_job_id is None:
            now = time.monotonic()
            if needs_render or now - last_render >= 1.0:
                selected = _clamp_first_run_selection(selected, view)
                _render_first_run_frame(buffer, notices, selected=selected, view=view, editing_field=editing_field)
                needs_render = False
                last_render = now
            readable, _, _ = select.select([stdin_fd], [], [], 0.05)
            if not readable:
                continue
            char = _read_terminal_char(stdin_fd)
            if editing_field is not None:
                if char in {"\r", "\n"}:
                    notices.append(_inline_setting_notice(editing_field, buffer))
                    notices[:] = notices[-10:]
                    editing_field = None
                    buffer = ""
                    needs_render = True
                    continue
                if char in {"\x04"}:
                    return
                if char == "\x03":
                    notices.append("cancelled edit")
                    notices[:] = notices[-10:]
                    editing_field = None
                    buffer = ""
                    needs_render = True
                    continue
                if char in {"\x7f", "\b"}:
                    buffer = buffer[:-1]
                    needs_render = True
                    continue
                if char == "\x1b":
                    key, _payload = _decode_terminal_escape(_read_escape_sequence(char, fd=stdin_fd))
                    if key == "unknown":
                        notices.append("cancelled edit")
                        notices[:] = notices[-10:]
                        editing_field = None
                        buffer = ""
                    needs_render = True
                    continue
                if char.isprintable():
                    buffer += char
                    needs_render = True
                continue
            if char in {"\r", "\n"}:
                line = buffer.strip()
                buffer = ""
                if not line:
                    action, payload = _handle_first_run_action(_first_run_actions(view)[selected][0])
                else:
                    action, payload = _handle_first_run_frame_line(line)
                if action == "view":
                    view = str(payload or "start")
                    selected = 0
                    notices.clear()
                    needs_render = True
                    continue
                if action == "exit":
                    return
                if action == "clear":
                    notices.clear()
                    needs_render = True
                    continue
                if action == "open":
                    next_job_id = str(payload)
                    break
                if action == "edit":
                    editing_field = str(payload)
                    notices.append(f"editing {editing_field}; enter saves, escape cancels")
                    notices[:] = notices[-10:]
                    needs_render = True
                    continue
                if isinstance(payload, list):
                    notices.extend(str(item) for item in payload if str(item).strip())
                elif payload:
                    notices.append(str(payload))
                notices[:] = notices[-10:]
                needs_render = True
                continue
            if char in {"\x04"}:
                return
            if char == "\x03":
                buffer = ""
                notices.append("cancelled input")
                notices[:] = notices[-10:]
                needs_render = True
                continue
            if char in {"\x7f", "\b"}:
                buffer = buffer[:-1]
                needs_render = True
                continue
            if char == "\t":
                buffer = _autocomplete_slash(buffer, FIRST_RUN_SLASH_COMMANDS)
                needs_render = True
                continue
            if char == "\x1b":
                key, payload = _decode_terminal_escape(_read_escape_sequence(char, fd=stdin_fd))
                if key == "up":
                    selected = (selected - 1) % len(_first_run_actions(view))
                elif key == "down":
                    selected = (selected + 1) % len(_first_run_actions(view))
                elif key in {"left", "right"}:
                    selected = 0
                elif key == "click" and isinstance(payload, tuple):
                    clicked = _first_run_click_action(payload[0], payload[1], view=view)
                    if clicked is not None:
                        selected = clicked
                        action, action_payload = _handle_first_run_action(_first_run_actions(view)[selected][0])
                        if action == "view":
                            view = str(action_payload or "start")
                            selected = 0
                            notices.clear()
                        elif action == "exit":
                            return
                        elif action == "open":
                            next_job_id = str(action_payload)
                            break
                        elif action == "edit":
                            editing_field = str(action_payload)
                            notices.append(f"editing {editing_field}; enter saves, escape cancels")
                        elif isinstance(action_payload, list):
                            notices.extend(str(item) for item in action_payload if str(item).strip())
                        elif action_payload:
                            notices.append(str(action_payload))
                        notices[:] = notices[-10:]
                else:
                    _drain_pending_input(stdin_fd)
                needs_render = True
                continue
            if char.isprintable():
                buffer += char
                needs_render = True
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
        print("\033[?1002l\033[?1000l\033[?1006l\033[?25h\033[0m\033[?1049l", flush=True)
    if next_job_id:
        _enter_chat(next_job_id, show_history=True, history_limit=history_limit)


def _config_path() -> Path:
    return get_agent_home() / "config.yaml"


def _load_config_yaml() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        loaded = yaml.safe_load(default_config_yaml()) or {}
        return loaded if isinstance(loaded, dict) else {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _save_config_yaml(data: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _config_field_value(field: str, config: Any | None = None) -> Any:
    config = load_config() if config is None else config
    values = {
        "model.name": config.model.model,
        "model.base_url": config.model.base_url,
        "model.api_key_env": config.model.api_key_env,
        "model.context_length": config.model.context_length,
        "model.request_timeout_seconds": config.model.request_timeout_seconds,
        "runtime.home": str(config.runtime.home),
        "runtime.max_step_seconds": config.runtime.max_step_seconds,
        "runtime.artifact_inline_char_limit": config.runtime.artifact_inline_char_limit,
        "runtime.daily_digest_enabled": config.runtime.daily_digest_enabled,
        "runtime.daily_digest_time": config.runtime.daily_digest_time,
    }
    return values.get(field, "")


def _save_config_field(field: str, raw_value: str) -> Any:
    value = _coerce_config_value(field, raw_value)
    data = _load_config_yaml()
    section, key = field.split(".", 1)
    target = data.setdefault(section, {})
    if not isinstance(target, dict):
        target = {}
        data[section] = target
    target[key] = value
    _save_config_yaml(data)
    return value


def _inline_setting_notice(field: str, raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return f"kept {field}"
    if field == "secret:model.api_key":
        config = load_config()
        name = config.model.api_key_env
        _save_env_secret(name, value)
        return f"saved {name} in {_short_path(get_agent_home() / '.env')}"
    try:
        saved = _save_config_field(field, value)
    except ValueError as exc:
        return f"{field}: {exc}"
    return f"saved {field} = {saved}"


def _save_env_secret(name: str, value: str) -> None:
    env_path = get_agent_home() / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" not in raw or raw.strip().startswith("#"):
                continue
            key, current = raw.split("=", 1)
            if key.strip():
                existing[key.strip()] = current.strip()
    existing[name] = value
    env_path.write_text("\n".join(f"{key}={current}" for key, current in existing.items()) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    os.environ[name] = value


def _edit_target_label(field: str) -> str:
    if field == "secret:model.api_key":
        return "API key"
    return field


def _edit_target_hint(field: str, config: Any | None = None) -> str:
    config = config or load_config()
    if field == "secret:model.api_key":
        state = "set" if config.model.api_key else "missing"
        return f"Editing API key ({state}). Enter saves, Esc cancels. Input is hidden."
    current = _config_field_value(field, config)
    return f"Editing {field}. Current: {current}. Enter saves, Esc cancels, empty keeps current."


def _edit_target_masks_input(field: str | None) -> bool:
    return field == "secret:model.api_key"


def _coerce_config_value(field: str, raw_value: str) -> Any:
    kind = SETTINGS_FIELD_TYPES.get(field, "str")
    value = raw_value.strip()
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "bool":
        lowered = value.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError("use true or false")
    if kind == "path":
        return str(Path(value).expanduser())
    return value


def _first_run_actions(view: str) -> list[tuple[str, str, str]]:
    return FIRST_RUN_ACTIONS


def _clamp_first_run_selection(selected: int, view: str) -> int:
    actions = _first_run_actions(view)
    if not actions:
        return 0
    return max(0, min(selected, len(actions) - 1))


def _handle_first_run_action(action: str) -> tuple[str, str | list[str] | None]:
    if action.startswith("edit:"):
        return "edit", action.split(":", 1)[1]
    if action.startswith("secret:"):
        return "edit", action
    if action == "new":
        return "notice", "Type the goal in the input line, then press Enter."
    if action == "back":
        return "view", "start"
    if action == "jobs":
        return "notice", _capture_first_run_command("jobs")
    if action == "doctor":
        return "notice", _capture_first_run_command("doctor")
    if action == "init":
        return "notice", _capture_first_run_command("init")
    if action == "exit":
        return "exit", None
    return "notice", f"Unknown action: {action}"


def _read_terminal_char(fd: int) -> str:
    data = os.read(fd, 1)
    return data.decode("latin1", errors="ignore")


def _read_escape_sequence(first: str, *, fd: int | None = None) -> str:
    fd = sys.stdin.fileno() if fd is None else fd
    sequence = first
    deadline = time.monotonic() + 0.12
    while len(sequence) < 96:
        timeout = max(0.0, min(0.04, deadline - time.monotonic()))
        if timeout <= 0:
            break
        readable, _, _ = select.select([fd], [], [], timeout)
        if not readable:
            break
        sequence += _read_terminal_char(fd)
        if _terminal_escape_complete(sequence):
            break
    return sequence


def _terminal_escape_complete(sequence: str) -> bool:
    if sequence in {"\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D", "\x1bOA", "\x1bOB", "\x1bOC", "\x1bOD"}:
        return True
    if re.match(r"^\x1b\[[0-9;?]*[ABCD]$", sequence):
        return True
    if re.match(r"^\x1b\[<\d+;\d+;\d+[mM]$", sequence):
        return True
    if sequence.startswith("\x1b[M") and len(sequence) >= 6:
        return True
    return False


def _decode_terminal_escape(sequence: str) -> tuple[str, tuple[int, int] | None]:
    arrows = {
        "\x1b[A": "up",
        "\x1b[B": "down",
        "\x1b[C": "right",
        "\x1b[D": "left",
        "\x1bOA": "up",
        "\x1bOB": "down",
        "\x1bOC": "right",
        "\x1bOD": "left",
    }
    if sequence in arrows:
        return arrows[sequence], None
    csi_arrow = re.match(r"^\x1b\[[0-9;?]*([ABCD])$", sequence)
    if csi_arrow:
        return {"A": "up", "B": "down", "C": "right", "D": "left"}[csi_arrow.group(1)], None
    match = re.match(r"^\x1b\[<(\d+);(\d+);(\d+)([mM])$", sequence)
    if match and match.group(4) == "M":
        button = int(match.group(1))
        if button == 0:
            return "click", (int(match.group(2)), int(match.group(3)))
    if sequence.startswith("\x1b[M") and len(sequence) >= 6:
        button = ord(sequence[3]) - 32
        if button == 0:
            return "click", (ord(sequence[4]) - 32, ord(sequence[5]) - 32)
    return "unknown", None


def _first_run_click_action(x: int, y: int, *, view: str) -> int | None:
    width, _height = shutil.get_terminal_size((100, 30))
    left_width, _right_width = _first_run_columns(max(92, width))
    right_start = left_width + 4
    if x < right_start:
        return None
    body_start_y = 4
    action_body_index = 7
    index = y - (body_start_y + action_body_index)
    actions = _first_run_actions(view)
    return index if 0 <= index < len(actions) else None


def _chat_page_click(x: int, y: int, *, right_view: str) -> str | None:
    del right_view
    width, _height = shutil.get_terminal_size((100, 30))
    width = max(92, width)
    left_width = max(56, int(width * 0.64))
    right_width = max(34, width - left_width - 3)
    if right_width < 34:
        left_width = max(48, width - right_width - 3)
    right_start = left_width + 4
    if x < right_start or y > 8:
        return None
    relative = max(0, x - right_start)
    third = max(1, right_width // 3)
    if relative < third:
        return "status"
    if relative < third * 2:
        return "updates"
    return "work"


def _handle_first_run_frame_line(line: str) -> tuple[str, str | list[str] | None]:
    original = line.strip()
    if original.startswith("/"):
        original = original[1:].strip()
    lowered = original.lower()
    if lowered in {"exit", "quit", ":q", "5"}:
        return "exit", None
    if lowered in {"clear"}:
        return "clear", None
    if lowered in {"help", "?", "commands"}:
        return "notice", [
            "Talk normally here, or ask Nipux to create a job with a concrete goal.",
            "Use the right-side controls for jobs, setup checks, and exit.",
            "When a job exists, the left pane becomes its chat and output stream.",
        ]
    if lowered in {"1", "new"}:
        return "notice", "Type `new OBJECTIVE` or paste the objective directly."
    if lowered.startswith("new "):
        return "open", _create_first_run_job(original[4:].strip())
    if lowered in {"2", "jobs", "ls"}:
        return "notice", _capture_first_run_command("jobs")
    if lowered == "settings":
        return "notice", "Config is changed with slash commands: /model, /api-key, /base-url, /context."
    if lowered in {"back"}:
        return "view", "start"
    if lowered in {"3", "doctor"}:
        return "notice", _capture_first_run_command("doctor")
    if lowered in {"4", "init"}:
        return "notice", _capture_first_run_command("init")
    if lowered == "shell":
        return "notice", "The old console is only available as `nipux shell` from your terminal."
    first = _first_token(original)
    if first == "shell":
        return "notice", "The old console is only available as `nipux shell` from your terminal."
    if first in CHAT_SETTING_COMMANDS or first in {"api-key", "key"}:
        return "notice", _capture_setting_command(original)
    if first in SHELL_COMMAND_NAMES:
        before_job_id = _current_default_job_id()
        output = _capture_first_run_command(original)
        after_job_id = _current_default_job_id()
        if first == "create" and after_job_id and after_job_id != before_job_id:
            return "open", after_job_id
        return "notice", output
    objective = _extract_job_objective_from_message(original)
    if objective:
        return "open", _create_first_run_job(objective)
    return "notice", _first_run_chat_reply(original)


def _first_run_chat_reply(message: str) -> str:
    lowered = message.strip().lower()
    if lowered in {"hi", "hello", "hey", "yo"}:
        return "Hi. Tell me what long-running work you want, or use the controls on the right to create a job."
    if "what can" in lowered or "help" in lowered:
        return "I can spin up long-running jobs, keep their output on the left, and let you monitor work from the right."
    return "I can chat here, but I only create a job when you give me a concrete goal like 'create a job to monitor nightly benchmarks'."


def _create_first_run_job(objective: str) -> str | list[str]:
    objective = objective.strip()
    if not objective:
        return ["No job created. Type an objective first."]
    job_id, _title = _create_job(objective=objective, title=None, kind="generic", cadence=None)
    return job_id


def _capture_first_run_command(line: str) -> list[str]:
    stream = StringIO()
    with redirect_stdout(stream):
        try:
            _run_shell_line(line)
        except SystemExit as exc:
            if exc.code not in (None, 0):
                print(f"command exited with status {exc.code}")
    lines = [" ".join(item.split()) for item in stream.getvalue().splitlines() if item.strip()]
    return lines[-8:] or ["done"]


def _current_default_job_id() -> str | None:
    db, _ = _db()
    try:
        return _default_job_id(db)
    finally:
        db.close()


def _render_first_run_frame(
    input_buffer: str,
    notices: list[str],
    *,
    selected: int = 0,
    view: str = "start",
    editing_field: str | None = None,
) -> None:
    width, height = shutil.get_terminal_size((100, 30))
    frame = _build_first_run_frame(
        input_buffer,
        notices,
        width=width,
        height=height,
        selected=selected,
        view=view,
        editing_field=editing_field,
    )
    print("\033[H" + frame, end="", flush=True)


def _build_first_run_frame(
    input_buffer: str,
    notices: list[str],
    *,
    width: int,
    height: int,
    selected: int = 0,
    view: str = "start",
    editing_field: str | None = None,
) -> str:
    width = max(92, width)
    height = max(22, height)
    config = load_config()
    daemon = daemon_lock_status(config.runtime.home / "agentd.lock")
    jobs: list[dict[str, Any]] = []
    db, _ = _db()
    try:
        jobs = db.list_jobs()
    finally:
        db.close()
    daemon_text = _daemon_state_line(daemon)
    header = _top_bar(width, state="setup", daemon=daemon_text, model=config.model.model)
    if editing_field:
        hint = _edit_target_hint(editing_field, config)
        prompt_label = _edit_target_label(editing_field)
    else:
        hint = "Enter selects the highlighted control. ↑↓ moves. ←→ switches pages. Click selects."
        prompt_label = "❯"
    suggestions = [] if editing_field else _slash_suggestion_lines(input_buffer, FIRST_RUN_SLASH_COMMANDS, width=width)
    compose_lines = _compose_bar(
        input_buffer,
        width=width,
        hint=hint,
        suggestions=suggestions,
        prompt_label=prompt_label,
        mask_input=_edit_target_masks_input(editing_field),
    )
    footer_rows = len(compose_lines)
    body_rows = max(10, height - len(header) - 1 - footer_rows)
    left_width, right_width = _first_run_columns(width)
    left_lines = _first_run_left_lines(
        notices,
        width=left_width,
        rows=body_rows,
        view=view,
        selected=selected,
    )
    right_lines = _first_run_right_lines(
        jobs=jobs,
        daemon_text=daemon_text,
        model=config.model.model,
        home=_short_path(config.runtime.home, max_width=max(20, right_width - 8)),
        config_path=_short_path(config.runtime.home / "config.yaml", max_width=max(20, right_width - 8)),
        selected=selected,
        view=view,
        width=right_width,
        rows=body_rows,
    )
    left_title = "Nipux Chat"
    right_title = "Control"
    lines = [*header, _two_col_title(left_width, right_width, left_title, right_title)]
    for index in range(body_rows):
        left = left_lines[index] if index < len(left_lines) else ""
        right = right_lines[index] if index < len(right_lines) else ""
        lines.append(_two_col_line(left, right, left_width=left_width, right_width=right_width))
    lines.extend(compose_lines)
    return "\n".join(_first_run_themed_lines(lines[:height], width=width))


def _first_run_columns(width: int) -> tuple[int, int]:
    left_width = max(56, int(width * 0.60))
    right_width = max(34, width - left_width - 3)
    if right_width < 34:
        right_width = 34
        left_width = max(56, width - right_width - 3)
    return left_width, right_width


def _first_run_themed_lines(lines: list[str], *, width: int) -> list[str]:
    return _themed_lines(lines, width=width)


def _first_run_left_lines(
    notices: list[str],
    *,
    width: int,
    rows: int,
    view: str,
    selected: int,
) -> list[str]:
    selected_label = _first_run_actions(view)[_clamp_first_run_selection(selected, view)][1]
    if not notices:
        content = [
            *[_center_ansi(_style(line, "37;1"), width) for line in NIPUX_HERO],
            "",
            _center_ansi(_muted("A long-running agent harness."), width),
            _center_ansi(_muted("Type an objective, or use the controls on the right."), width),
            "",
            _center_ansi(f"{_muted('Selected')} {_accent(selected_label)}", width),
        ]
        top_pad = max(0, (rows - len(content)) // 2 - 1)
        return ([""] * top_pad + content)[:rows]
    lines = [
        _bold("No agent output yet."),
        _muted("Create a job from the control pane or type a goal in the input line."),
        "",
        f"{_muted('Selected')} {_accent(selected_label)}",
        "",
        f"{_muted('Mode')} {_accent('Start')}",
        "",
        _muted("After a job exists, this side becomes the agent conversation and output stream."),
        "",
        _muted("Use arrows, Enter, click, or / commands. Controls stay on the right."),
    ]
    if notices:
        lines.extend(["", _muted("Recent")])
        for notice in notices[-6:]:
            for wrapped in textwrap.wrap(" ".join(str(notice).split()), width=max(20, width - 4))[:3]:
                lines.append(f"{_accent('›')} {wrapped}")
    return [_fit_ansi(line, width) for line in lines[:rows]]


def _first_run_right_lines(
    *,
    jobs: list[dict[str, Any]],
    daemon_text: str,
    model: str,
    home: str,
    config_path: str,
    selected: int,
    view: str,
    width: int,
    rows: int,
) -> list[str]:
    profile_lines = _first_run_profile_lines(
        view=view,
        model=model,
        daemon_text=daemon_text,
        home=home,
        config_path=config_path,
        width=width,
    )
    lines = [
        *profile_lines,
        _bold("Actions"),
        *_first_run_action_lines(_first_run_actions(view), selected, width=width),
        "",
        _bold("Jobs"),
    ]
    if jobs:
        lines.extend(_frame_jobs_lines(jobs, focused_job_id="", daemon_running=False, width=width)[:5])
    else:
        lines.append(_muted("No saved jobs in this profile."))
    return [_fit_ansi(line, width) for line in lines[:rows]]


def _first_run_profile_lines(
    *,
    view: str,
    model: str,
    daemon_text: str,
    home: str,
    config_path: str,
    width: int,
) -> list[str]:
    return [
        f"{_muted('Mode')}   {_accent('Start')}",
        f"{_muted('Model')}  {_one_line(model, width - 8)}",
        f"{_muted('Daemon')} {_one_line(daemon_text, width - 8)}",
        f"{_muted('Home')}   {_one_line(home, width - 8)}",
        f"{_muted('Config')} {_one_line(config_path, width - 8)}",
        "",
    ]


def _first_run_action_lines(actions: list[tuple[str, str, str]], selected: int, *, width: int) -> list[str]:
    lines: list[str] = []
    selected = max(0, min(selected, len(actions) - 1)) if actions else 0
    for index, (_key, label, detail) in enumerate(actions):
        marker = _accent("›") if index == selected else _muted(" ")
        name = _bold(label) if index == selected else label
        if _key.startswith("edit:"):
            field = _key.split(":", 1)[1]
            detail = _one_line(str(_config_field_value(field)), max(10, width - 18))
        elif _key == "secret:model.api_key":
            config = load_config()
            state = "set" if config.model.api_key else "missing"
            detail = state
        lines.append(_fit_ansi(f"{marker} {_fit_ansi(name, 14)} {_muted(detail)}", width))
    return lines


def _enter_chat(job_id: str, *, show_history: bool, history_limit: int = 12) -> None:
    _install_readline_history()
    if _frame_chat_enabled():
        _enter_chat_frame(job_id, history_limit=history_limit)
        return
    db, _ = _db()
    try:
        job = db.get_job(job_id)
        _write_shell_state({"focus_job_id": job_id})
    finally:
        db.close()

    if _fancy_ui():
        print("\033[2J\033[H", end="")
    print(NIPUX_BANNER)
    print(_rule("="))
    print(_shell_summary())
    print(_rule("="))
    if show_history:
        _print_startup_history(job_id, limit=history_limit, chars=180)
        print()
    _print_chat_composer(job)
    live_stop, live_thread = _start_chat_live_feed(job_id)
    try:
        while True:
            db, _ = _db()
            try:
                refreshed = _default_job_id(db)
                if refreshed:
                    job_id = refreshed
                    job = db.get_job(job_id)
            finally:
                db.close()
            try:
                line = input(_chat_prompt(job))
            except EOFError:
                print()
                return
            except KeyboardInterrupt:
                print()
                continue
            if not _chat_handle_line(job_id, line):
                return
    finally:
        if live_stop is not None:
            live_stop.set()
        if live_thread is not None:
            live_thread.join(timeout=1.0)


def _frame_chat_enabled() -> bool:
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and not os.environ.get("NIPUX_APPEND_LIVE")
        and not os.environ.get("NIPUX_NO_FRAME")
    )


def _enter_chat_frame(job_id: str, *, history_limit: int = 12) -> None:
    _write_shell_state({"focus_job_id": job_id})
    buffer = ""
    notices: list[str] = []
    right_view = "status"
    selected_control = 0
    editing_field: str | None = None
    snapshot = _load_frame_snapshot(job_id, history_limit=history_limit)
    job_id = str(snapshot["job_id"])
    old_attrs = termios.tcgetattr(sys.stdin)
    print("\033[?1049h\033[H\033[?25l\033[?1000h\033[?1002h\033[?1006h", end="", flush=True)
    try:
        stdin_fd = sys.stdin.fileno()
        tty.setcbreak(stdin_fd)
        last_snapshot = 0.0
        needs_render = True
        while True:
            now = time.monotonic()
            if now - last_snapshot >= 0.75:
                try:
                    snapshot = _load_frame_snapshot(job_id, history_limit=history_limit)
                    job_id = str(snapshot["job_id"])
                    last_snapshot = now
                    needs_render = True
                except Exception as exc:
                    notices.append(f"frame refresh failed: {type(exc).__name__}")
                    notices[:] = notices[-12:]
            if needs_render:
                selected_control = 0
                _render_chat_frame(
                    snapshot,
                    buffer,
                    notices,
                    right_view=right_view,
                    selected_control=selected_control,
                    editing_field=editing_field,
                )
                needs_render = False
            readable, _, _ = select.select([stdin_fd], [], [], 0.05)
            if not readable:
                continue
            char = _read_terminal_char(stdin_fd)
            if editing_field is not None:
                if char in {"\r", "\n"}:
                    notices.append(_inline_setting_notice(editing_field, buffer))
                    notices[:] = notices[-12:]
                    editing_field = None
                    buffer = ""
                    needs_render = True
                    continue
                if char in {"\x04"}:
                    return
                if char == "\x03":
                    notices.append("cancelled edit")
                    notices[:] = notices[-12:]
                    editing_field = None
                    buffer = ""
                    needs_render = True
                    continue
                if char in {"\x7f", "\b"}:
                    buffer = buffer[:-1]
                    needs_render = True
                    continue
                if char == "\x1b":
                    key, _payload = _decode_terminal_escape(_read_escape_sequence(char, fd=stdin_fd))
                    if key == "unknown":
                        notices.append("cancelled edit")
                        notices[:] = notices[-12:]
                        editing_field = None
                        buffer = ""
                    needs_render = True
                    continue
                if char.isprintable():
                    buffer += char
                    needs_render = True
                continue
            if char in {"\r", "\n"}:
                line = buffer.strip()
                buffer = ""
                if not line:
                    needs_render = True
                    continue
                if line in {"clear", "/clear"}:
                    notices.clear()
                    needs_render = True
                    continue
                notices.append(f"> {line}")
                notices[:] = notices[-12:]
                _render_chat_frame(
                    snapshot,
                    buffer,
                    notices,
                    right_view=right_view,
                    selected_control=selected_control,
                    editing_field=editing_field,
                )
                if _is_plain_chat_line(line):
                    keep_running, message = _handle_chat_message(job_id, line, quiet=True)
                    notices = [notice for notice in notices if notice != f"> {line}"]
                    if message:
                        notices.append(message)
                        notices[:] = notices[-12:]
                else:
                    keep_running, output = _capture_chat_command(job_id, line)
                    for output_line in _compact_command_output(output):
                        notices.append(output_line)
                    notices[:] = notices[-12:]
                snapshot = _load_frame_snapshot(job_id, history_limit=history_limit)
                job_id = str(snapshot["job_id"])
                needs_render = True
                if not keep_running:
                    return
                continue
            if char in {"\x04"}:
                return
            if char == "\x03":
                buffer = ""
                notices.append("cancelled input")
                notices[:] = notices[-12:]
                needs_render = True
                continue
            if char in {"\x7f", "\b"}:
                buffer = buffer[:-1]
                needs_render = True
                continue
            if char == "\t":
                buffer = _autocomplete_slash(buffer, CHAT_SLASH_COMMANDS)
                needs_render = True
                continue
            if char == "\x1b":
                key, payload = _decode_terminal_escape(_read_escape_sequence(char, fd=stdin_fd))
                if key == "right" and not buffer:
                    right_view = _next_chat_right_view(right_view, 1)
                    selected_control = 0
                elif key == "left" and not buffer:
                    right_view = _next_chat_right_view(right_view, -1)
                    selected_control = 0
                elif key in {"up", "down"} and not buffer:
                    next_focus = _frame_next_job_id(snapshot, job_id, direction=-1 if key == "up" else 1)
                    if next_focus and next_focus != job_id:
                        job_id = next_focus
                        _write_shell_state({"focus_job_id": job_id})
                        snapshot = _load_frame_snapshot(job_id, history_limit=history_limit)
                        title = snapshot["job"].get("title") or job_id
                        notices.append(f"focus {title}")
                        notices[:] = notices[-12:]
                elif key == "click" and isinstance(payload, tuple):
                    clicked_view = _chat_page_click(payload[0], payload[1], right_view=right_view)
                    if clicked_view:
                        right_view = clicked_view
                        selected_control = 0
                else:
                    _drain_pending_input(stdin_fd)
                needs_render = True
                continue
            if char.isprintable():
                buffer += char
                needs_render = True
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
        print("\033[?1006l\033[?1002l\033[?1000l\033[?25h\033[0m\033[?1049l", flush=True)


def _capture_chat_command(job_id: str, line: str) -> tuple[bool, str]:
    stream = StringIO()
    with redirect_stdout(stream):
        keep_running = _chat_handle_line(job_id, line)
    return keep_running, stream.getvalue()


def _compact_command_output(output: str) -> list[str]:
    lines = [" ".join(line.split()) for line in output.splitlines() if line.strip()]
    compacted: list[str] = []
    for line in lines:
        if line.startswith("\033[2J"):
            continue
        compacted.append(_one_line(line, 120))
    return compacted[-8:]


def _drain_pending_input(fd: int | None = None) -> None:
    fd = sys.stdin.fileno() if fd is None else fd
    while True:
        readable, _, _ = select.select([fd], [], [], 0)
        if not readable:
            return
        os.read(fd, 1)


def _frame_next_job_id(snapshot: dict[str, Any], current_job_id: str, *, direction: int) -> str | None:
    jobs = snapshot.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return None
    ids = [str(job.get("id")) for job in jobs if job.get("id")]
    if not ids:
        return None
    try:
        index = ids.index(str(current_job_id))
    except ValueError:
        index = 0
    return ids[(index + direction) % len(ids)]


def _next_chat_right_view(current: str, direction: int) -> str:
    keys = [key for key, _label in CHAT_RIGHT_PAGES]
    try:
        index = keys.index(current)
    except ValueError:
        index = 0
    return keys[(index + direction) % len(keys)]


def _is_plain_chat_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("/"):
        return False
    lowered = stripped.lower()
    if lowered in {"help", "jobs", "ls", "clear", "exit", "quit"}:
        return False
    try:
        first = shlex.split(stripped)[0].lower()
    except (IndexError, ValueError):
        first = lowered.split(maxsplit=1)[0]
    return first not in {"chat", "focus", "switch", "jobs", "ls", "help", "clear", "exit", "quit"}


def _load_frame_snapshot(job_id: str, *, history_limit: int = 12) -> dict[str, Any]:
    db, config = _db()
    try:
        job_id = _default_job_id(db) or job_id
        job = db.get_job(job_id)
        jobs = db.list_jobs()
        counts = db.job_record_counts(job_id)
        steps = db.list_steps(job_id=job_id, limit=80)
        artifacts = db.list_artifacts(job_id, limit=8)
        job_artifacts = {
            str(item["id"]): db.list_artifacts(str(item["id"]), limit=3)
            for item in jobs[:6]
            if item.get("id")
        }
        memory_entries = db.list_memory(job_id)[:8]
        events = db.list_events(job_id=job_id, limit=max(history_limit * 16, 240))
        summary_events = db.list_events(job_id=job_id, limit=max(history_limit * 80, 1000))
        token_usage = db.job_token_usage(job_id)
        daemon = daemon_lock_status(config.runtime.home / "agentd.lock")
    finally:
        db.close()
    return {
        "job_id": job_id,
        "job": job,
        "jobs": jobs,
        "steps": steps,
        "artifacts": artifacts,
        "job_artifacts": job_artifacts,
        "memory_entries": memory_entries,
        "events": events,
        "summary_events": summary_events,
        "daemon": daemon,
        "model": config.model.model,
        "base_url": config.model.base_url,
        "context_length": config.model.context_length,
        "token_usage": token_usage,
        "counts": counts,
    }


def _render_chat_frame(
    snapshot: dict[str, Any],
    input_buffer: str,
    notices: list[str],
    *,
    right_view: str = "status",
    selected_control: int = 0,
    editing_field: str | None = None,
) -> None:
    width, height = shutil.get_terminal_size((100, 30))
    frame = _build_chat_frame(
        snapshot,
        input_buffer,
        notices,
        width=width,
        height=height,
        right_view=right_view,
        selected_control=selected_control,
        editing_field=editing_field,
    )
    print("\033[H" + frame, end="", flush=True)


def _build_chat_frame(
    snapshot: dict[str, Any],
    input_buffer: str,
    notices: list[str],
    *,
    width: int,
    height: int,
    right_view: str = "status",
    selected_control: int = 0,
    editing_field: str | None = None,
) -> str:
    width = max(92, width)
    height = max(22, height)
    job = snapshot["job"]
    jobs = snapshot["jobs"]
    steps = snapshot["steps"]
    artifacts = snapshot["artifacts"]
    job_id = str(snapshot["job_id"])
    job_artifacts = snapshot.get("job_artifacts") if isinstance(snapshot.get("job_artifacts"), dict) else {}
    if artifacts:
        job_artifacts.setdefault(job_id, artifacts)
    memory_entries = snapshot["memory_entries"]
    events = snapshot["events"]
    summary_events = snapshot.get("summary_events") if isinstance(snapshot.get("summary_events"), list) else events
    daemon = snapshot["daemon"]
    model = str(snapshot["model"])
    base_url = str(snapshot.get("base_url") or "")
    token_usage = snapshot.get("token_usage") if isinstance(snapshot.get("token_usage"), dict) else {}
    context_length = int(snapshot.get("context_length") or 0)
    counts = snapshot.get("counts") if isinstance(snapshot.get("counts"), dict) else {}
    findings = _metadata_records(job, "finding_ledger")
    sources = _metadata_records(job, "source_ledger")
    tasks = _metadata_records(job, "task_queue")
    experiments = _metadata_records(job, "experiment_ledger")
    lessons = _metadata_records(job, "lessons")
    roadmap = job.get("metadata", {}).get("roadmap") if isinstance(job.get("metadata"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap, dict) and isinstance(roadmap.get("milestones"), list) else []
    open_tasks = sum(1 for task in tasks if str(task.get("status") or "open") in {"open", "active"})
    state = _job_display_state(job, bool(daemon["running"]))
    worker = _worker_label(job, bool(daemon["running"]))
    latest_step = steps[-1] if steps else None
    left_width = max(56, int(width * 0.64))
    right_width = max(34, width - left_width - 3)
    if right_width < 34:
        right_width = 34
        left_width = max(48, width - right_width - 3)
    latest_text = _step_line(latest_step, chars=right_width - 6) if latest_step else "no worker steps yet"
    daemon_text = _daemon_state_line(daemon)
    goal_text = " ".join(str(job.get("objective") or "").split())
    metrics = [
        ("actions", counts.get("steps", _step_count(steps))),
        ("outputs", counts.get("artifacts", len(artifacts))),
        ("findings", len(findings)),
        ("sources", len(sources)),
        ("tasks", f"{len(tasks)}/{open_tasks} open"),
        ("roadmap", len(milestones)),
        ("experiments", len(experiments)),
        ("lessons", len(lessons)),
        ("memory", counts.get("memory", len(memory_entries))),
    ]

    header = _top_bar(
        width,
        state=state,
        daemon=daemon_text,
        model=model,
        token_usage=token_usage,
        context_length=context_length,
        base_url=base_url,
    )
    if editing_field:
        hint = _edit_target_hint(editing_field)
        prompt_label = _edit_target_label(editing_field)
    else:
        hint = "Enter sends  ·  / commands  ·  ←→ panels  ·  ↑↓ jobs"
        prompt_label = "❯"
    suggestions = [] if editing_field else _slash_suggestion_lines(input_buffer, CHAT_SLASH_COMMANDS, width=width)
    compose_lines = _compose_bar(
        input_buffer,
        width=width,
        hint=hint,
        suggestions=suggestions,
        prompt_label=prompt_label,
        mask_input=_edit_target_masks_input(editing_field),
    )
    footer_rows = len(compose_lines)
    body_rows = max(10, height - len(header) - 1 - footer_rows)
    chat_rows = body_rows
    right_rows = body_rows
    chat_lines = _chat_pane_lines(events, notices, width=left_width, rows=chat_rows)
    if right_view == "updates":
        right_lines = _chat_updates_pane_lines(
            job=job,
            events=summary_events,
            width=right_width,
            rows=right_rows,
        )
        right_title = "Progress"
    elif right_view == "work":
        right_lines = _chat_work_pane_lines(
            job=job,
            events=events,
            tasks=tasks,
            experiments=experiments,
            width=right_width,
            rows=right_rows,
        )
        right_title = "Work"
    else:
        right_lines = _right_pane_lines(
            job=job,
            jobs=jobs,
            job_artifacts=job_artifacts,
            job_id=job_id,
            daemon_running=bool(daemon["running"]),
            state=state,
            worker=worker,
            daemon_text=daemon_text,
            model=model,
            goal_text=goal_text,
            latest_text=latest_text,
            metrics=metrics,
            events=summary_events,
            width=right_width,
            rows=right_rows,
            right_view=right_view,
        )
        right_title = "Status"
    lines = [*header, _two_col_title(left_width, right_width, "Chat", right_title)]
    for index in range(body_rows):
        left = chat_lines[index] if index < len(chat_lines) else ""
        right = right_lines[index] if index < len(right_lines) else ""
        lines.append(_two_col_line(left, right, left_width=left_width, right_width=right_width))
    lines.extend(compose_lines)
    if len(lines) > height:
        keep_top = min(4, len(header) + 1)
        keep_bottom = footer_rows
        middle_budget = max(0, height - keep_top - keep_bottom)
        lines = lines[:keep_top] + lines[-(middle_budget + keep_bottom) : -keep_bottom] + lines[-keep_bottom:]
    return "\n".join(_first_run_themed_lines(lines[:height], width=width))


def _chat_pane_lines(events: list[dict[str, Any]], notices: list[str], *, width: int, rows: int) -> list[str]:
    items: list[tuple[str, str, str]] = []
    for event in events:
        rendered = _chat_event_parts(event)
        if not rendered:
            continue
        label, body, clock = rendered
        items.append((label, body, clock))
    for notice in notices:
        if notice.startswith("> "):
            items.append(("YOU", notice[2:], ""))
        else:
            items.append(("NIPUX", notice, ""))
    if not items:
        return _chat_empty_state_lines(width=width, rows=rows)
    output_rows: list[str] = []
    for label, body, clock in items[-max(4, rows) :]:
        _append_chat_output(output_rows, label, body, clock=clock, width=width)
    return output_rows[-rows:]


NIPUX_HERO = [
    " _   _ ___ ____  _   ___  __",
    "| \\ | |_ _|  _ \\| | | \\ \\/ /",
    "|  \\| || || |_) | | | |>  < ",
    "| |\\  || ||  __/| |_| /_/\\_\\",
    "|_| \\_|___|_|    \\___/      ",
]


def _chat_empty_state_lines(*, width: int, rows: int) -> list[str]:
    if width < 48:
        content = [
            _center_ansi(_bold(_accent("NIPUX")), width),
            "",
            _center_ansi(_muted("Type normally to talk."), width),
        ]
        return content[:rows]
    content = [
        *[_center_ansi(_style(line, "37;1"), width) for line in NIPUX_HERO],
        "",
        _center_ansi(_muted("A persistent agent workspace."), width),
        _center_ansi(_muted("Enter sends  ·  / opens commands  ·  arrows switch panels"), width),
    ]
    top_pad = max(0, (rows - len(content)) // 2 - 1)
    return ([""] * top_pad + content)[:rows]


def _right_pane_lines(
    *,
    job: dict[str, Any],
    jobs: list[dict[str, Any]],
    job_artifacts: dict[str, list[dict[str, Any]]],
    job_id: str,
    daemon_running: bool,
    state: str,
    worker: str,
    daemon_text: str,
    model: str,
    goal_text: str,
    latest_text: str,
    metrics: list[tuple[str, Any]],
    events: list[dict[str, Any]],
    width: int,
    rows: int,
    right_view: str = "status",
) -> list[str]:
    info_lines = _chat_workspace_lines(
        right_view=right_view,
        job=job,
        state=state,
        worker=worker,
        daemon_text=daemon_text,
        model=model,
        goal_text=goal_text,
        latest_text=latest_text,
        metrics=metrics,
        width=width,
    )
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    active_operator = _active_operator_messages(metadata)
    pending_measurement = (
        metadata.get("pending_measurement_obligation")
        if isinstance(metadata.get("pending_measurement_obligation"), dict)
        else {}
    )
    if active_operator:
        info_lines.append(f"{_muted('Operator')} {len(active_operator)} active")
        info_lines.append(f"{_muted('Context')} {_one_line(active_operator[-1].get('message') or '', width - 8)}")
    if pending_measurement:
        info_lines.append(f"{_muted('Measure')} pending step #{pending_measurement.get('source_step_no') or '?'}")
    latest_outcome = _latest_durable_outcome_line(events, width=width)
    if latest_outcome:
        info_lines.append(latest_outcome)
    info_lines.append("")
    info_lines.append(_bold("Jobs"))
    info_lines.extend(
        _frame_jobs_lines(
            jobs[:5],
            focused_job_id=job_id,
            daemon_running=daemon_running,
            width=width,
            job_artifacts=job_artifacts,
            show_outputs=True,
        )
    )
    current_outputs = job_artifacts.get(job_id) or []
    info_lines.append("")
    info_lines.append(_bold("Saved outputs"))
    if current_outputs:
        for index, artifact in enumerate(current_outputs[:4], start=1):
            title = _one_line(str(artifact.get("title") or artifact.get("id") or "output"), max(10, width - 8))
            info_lines.append(_fit_ansi(f"{index}. {_event_badge('SAVE')} {title}", width))
    else:
        info_lines.append(_muted("No saved outputs yet."))
    return info_lines[:rows]


def _latest_durable_outcome_line(events: list[dict[str, Any]], *, width: int) -> str:
    fallback: tuple[str, str, str] | None = None
    for event in reversed(events):
        parsed = _model_update_event_parts(event, width=width)
        if not parsed:
            continue
        label, text, clock = parsed
        if label == "DONE":
            fallback = fallback or parsed
            continue
        prefix = f"{_muted('Outcome')} {_event_badge(label)} "
        return _fit_ansi(prefix + _one_line(text, max(12, width - len(_strip_ansi(prefix)))), width)
    if fallback:
        label, text, clock = fallback
        prefix = f"{_muted('Outcome')} {_event_badge(label)} "
        return _fit_ansi(prefix + _one_line(text, max(12, width - len(_strip_ansi(prefix)))), width)
    return ""


def _chat_work_pane_lines(
    *,
    job: dict[str, Any],
    events: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    width: int,
    rows: int,
) -> list[str]:
    lines = [
        f"{_muted('Page')}   {_page_indicator('work', CHAT_RIGHT_PAGES)}",
        f"{_muted('Focus')}  {_bold(_one_line(job.get('title') or 'untitled', width - 8))}",
        "",
        _bold("Tool / console"),
    ]
    tool_lines = _worker_activity_lines(events, width=width, limit=max(4, rows // 2))
    if tool_lines:
        lines.extend(tool_lines)
    else:
        lines.append(_muted("No recent tool calls."))
    remaining = max(0, rows - len(lines))
    if remaining > 4:
        lines.append("")
        lines.append(_bold("Tasks"))
        for task in _rank_visible_tasks(tasks)[: max(1, remaining // 2)]:
            status = str(task.get("status") or "open")
            title = _one_line(str(task.get("title") or "task"), max(10, width - 15))
            lines.append(_fit_ansi(f"{_status_badge(status)} {title}", width))
    remaining = max(0, rows - len(lines))
    if remaining > 3 and experiments:
        lines.append("")
        lines.append(_bold("Measurements"))
        for experiment in experiments[-max(1, remaining - 2) :]:
            metric = _experiment_metric_text(experiment)
            title = _one_line(str(experiment.get("title") or "experiment"), max(10, width - 16))
            suffix = f" {_muted(metric)}" if metric else ""
            lines.append(_fit_ansi(f"{_event_badge('TEST')} {title}{suffix}", width))
    return [_fit_ansi(line, width) for line in lines[:rows]]


def _rank_visible_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status_order = {"active": 0, "open": 1, "blocked": 2, "validating": 3, "done": 4, "skipped": 5}
    return sorted(
        [task for task in tasks if isinstance(task, dict)],
        key=lambda task: (
            status_order.get(str(task.get("status") or "open"), 9),
            -int(task.get("priority") or 0),
            str(task.get("title") or ""),
        ),
    )


def _chat_updates_pane_lines(
    *,
    job: dict[str, Any],
    events: list[dict[str, Any]],
    width: int,
    rows: int,
) -> list[str]:
    lines = [
        f"{_muted('Page')}   {_page_indicator('updates', CHAT_RIGHT_PAGES)}",
        f"{_muted('Focus')}  {_bold(_one_line(job.get('title') or 'untitled', width - 8))}",
        "",
        _bold("Progress by hour"),
        _muted("Summaries of durable output, findings, measurements, decisions, and files."),
        "",
    ]
    update_lines = _hourly_update_lines(events, width=width, limit=max(4, rows - len(lines)))
    if update_lines:
        lines.extend(update_lines)
    else:
        lines.append(_muted("No durable model updates yet. Tool calls are on Work."))
    return [_fit_ansi(line, width) for line in lines[:rows]]


def _hourly_update_lines(events: list[dict[str, Any]], *, width: int, limit: int) -> list[str]:
    if limit <= 0:
        return []
    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        parsed = _model_update_event_parts(event, width=max(width, 220))
        if not parsed:
            continue
        label, text, clock = parsed
        hour = _event_hour(event)
        if hour not in buckets:
            buckets[hour] = {"counts": {}, "items": [], "clock": clock}
            order.append(hour)
        bucket = buckets[hour]
        counts = bucket["counts"]
        counts[label] = int(counts.get(label) or 0) + 1
        item = (label, text)
        if item not in bucket["items"]:
            bucket["items"].append(item)
    rendered: list[str] = []
    recent_hours = order[-max(1, min(len(order), limit)) :]
    per_bucket = max(2, min(8, (limit // max(1, len(recent_hours))) - 1))
    for hour in recent_hours:
        bucket = buckets[hour]
        counts = bucket["counts"]
        summary = " ".join(f"{count} {label.lower()}" for label, count in sorted(counts.items()))
        rendered.append(_fit_ansi(f"{_muted(hour)} {_bold(summary or 'activity')}", width))
        for label, text in bucket["items"][-per_bucket:]:
            prefix = f"  {_event_badge(label)} "
            available = max(16, width - len(_strip_ansi(prefix)))
            parts = textwrap.wrap(text, width=available) or [""]
            rendered.append(_fit_ansi(prefix + parts[0], width))
            for part in parts[1:3]:
                rendered.append(_fit_ansi(" " * len(_strip_ansi(prefix)) + part, width))
                if len(rendered) >= limit:
                    return rendered[:limit]
        if len(rendered) >= limit:
            return rendered[:limit]
    return rendered[-limit:]


def _event_hour(event: dict[str, Any]) -> str:
    compact = _compact_time(str(event.get("created_at") or ""))
    if len(compact) >= 13 and compact[10:11] == " ":
        return f"{compact[:13]}:00"
    if len(compact) >= 2:
        return compact
    return "recent"


def _chat_workspace_lines(
    *,
    right_view: str,
    job: dict[str, Any],
    state: str,
    worker: str,
    daemon_text: str,
    model: str,
    goal_text: str,
    latest_text: str,
    metrics: list[tuple[str, Any]],
    width: int,
) -> list[str]:
    goal_lines = textwrap.wrap(goal_text, width=max(20, width - 8))[:2] or [""]
    while len(goal_lines) < 2:
        goal_lines.append("")
    title = _one_line(str(job.get("title") or "untitled"), max(10, width))
    return [
        f"{_muted('Page')}   {_page_indicator(right_view, CHAT_RIGHT_PAGES)}",
        _bold(title),
        f"{_status_badge(state)} {_muted('worker')} {_status_badge(worker)}  {_muted(_one_line(daemon_text, max(8, width - 28)))}",
        f"{_muted('Goal')}   {goal_lines[0]}",
        f"{_muted('       ')}{goal_lines[1]}",
        f"{_muted('Latest')} {_one_line(latest_text, width - 8)}",
        *_metrics_grid_lines(metrics, width=width),
        "",
    ]


def _metrics_grid_lines(metrics: list[tuple[str, Any]], *, width: int) -> list[str]:
    wanted = ["actions", "outputs", "findings", "sources", "tasks", "experiments", "memory"]
    lookup = {label: value for label, value in metrics}
    items = [(label, lookup[label]) for label in wanted if label in lookup]
    if width < 40:
        return [_metric_strip(items, width=width)]
    lines: list[str] = []
    col_width = max(16, (width - 2) // 2)
    for index in range(0, len(items), 2):
        left = _metric_cell(items[index], width=col_width)
        right = _metric_cell(items[index + 1], width=col_width) if index + 1 < len(items) else ""
        lines.append(_fit_ansi(left + "  " + right, width))
    return lines


def _metric_cell(item: tuple[str, Any], *, width: int) -> str:
    label, value = item
    return _fit_ansi(f"{_muted(label)} {_bold(value)}", width)


def _activity_text(event: dict[str, Any], *, width: int) -> str:
    text = _minimal_live_event_line(event, chars=max(16, width - 10))
    if not text:
        return ""
    return f"{_live_badge(text)} {_one_line(text, max(16, width - 9))}"


def _frame_jobs_lines(
    jobs: list[dict[str, Any]],
    *,
    focused_job_id: str,
    daemon_running: bool,
    width: int,
    job_artifacts: dict[str, list[dict[str, Any]]] | None = None,
    show_outputs: bool = False,
) -> list[str]:
    rendered = []
    for index, item in enumerate(jobs[:5], start=1):
        item_id = str(item.get("id") or "")
        marker = _accent("●") if item_id == focused_job_id else _muted("○")
        title_width = max(14, min(30, width - 34))
        title = _one_line(str(item.get("title") or item.get("id") or "job"), title_width)
        state = _status_badge(_job_display_state(item, daemon_running))
        worker = _status_badge(_worker_label(item, daemon_running))
        kind = _one_line(item.get("kind") or "", max(0, width - title_width - 33))
        rendered.append(
            _fit_ansi(
                f"{marker} {index:<2} {_fit_ansi(title, title_width)} "
                f"{_fit_ansi(state, 10)} {_fit_ansi(worker, 10)} {kind}",
                width,
            )
        )
        outputs = (job_artifacts or {}).get(item_id) or []
        if show_outputs and outputs:
            latest = outputs[0]
            output_title = _one_line(str(latest.get("title") or latest.get("id") or "saved output"), max(8, width - 15))
            rendered.append(_fit_ansi(f"   {_event_badge('SAVE')} {output_title}", width))
    return rendered


def _frame_event_line(event: dict[str, Any], *, width: int) -> str:
    text = _minimal_live_event_line(event, chars=width - 18)
    if not text:
        return ""
    badge = _live_badge(text)
    return _frame_line(width, f"{badge} {text}")


def _frame_top(width: int, title: str) -> str:
    clean = f" {title} "
    return _muted("╭─") + _bold(_accent(clean)) + _muted("─" * max(0, width - len(clean) - 3) + "╮")


def _frame_divider(width: int, title: str) -> str:
    clean = f" {title} "
    return _muted("├─") + _bold(clean) + _muted("─" * max(0, width - len(clean) - 3) + "┤")


def _frame_bottom(width: int) -> str:
    return _muted("╰" + "─" * max(0, width - 2) + "╯")


def _frame_line(width: int, text: str, *, colorize: bool = True) -> str:
    content = str(text)
    visible = _strip_ansi(content)
    if len(visible) > width - 4:
        content = _one_line(visible, width - 4)
        visible = content
    border = _muted("│")
    return border + " " + content + " " * max(0, width - 4 - len(visible)) + " " + border


def _resolve_job_id(db: AgentDB, requested: Any = None) -> str | None:
    requested = _job_ref_text(requested)
    if requested:
        job = _find_job(db, requested)
        return str(job["id"]) if job else None
    return _default_job_id(db)


def _activate_job_if_planning(db: AgentDB, job_id: str) -> bool:
    job = db.get_job(job_id)
    if job.get("status") != "planning":
        return False
    db.update_job_status(job_id, "queued", metadata_patch={"planning_status": "accepted"})
    db.append_agent_update(job_id, "Plan accepted. I will start working from the planned tasks.", category="plan")
    return True


def _ensure_job_runnable(db: AgentDB, job_id: str) -> None:
    if _activate_job_if_planning(db, job_id):
        return
    job = db.get_job(job_id)
    status = str(job.get("status") or "")
    if status in {"completed", "paused", "cancelled", "failed"}:
        db.update_job_status(
            job_id,
            "queued",
            metadata_patch={"last_note": f"reopened from {status} by operator run command"},
        )
        db.append_agent_update(
            job_id,
            f"Reopened from {status}; continuing as a long-running job.",
            category="progress",
            metadata={"previous_status": status},
        )


def cmd_steer(args: argparse.Namespace) -> None:
    message = " ".join(args.message).strip()
    if not message:
        print("No steering message provided.")
        return
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found. Create one first, then send steering.")
            return
        entry = db.append_operator_message(job_id, message, source="operator")
        job = db.get_job(job_id)
        print(f"waiting for {job['title']}: {entry['message']}")
        print("The next worker step will include this in model-visible context.")
    finally:
        db.close()


def cmd_pause(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id, note, ref = _resolve_control_job_and_note(db, args)
        if not job_id:
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        patch = {"last_note": note} if note else None
        db.update_job_status(job_id, "paused", metadata_patch=patch)
        job = db.get_job(job_id)
        print(f"paused {job['title']}" + (f": {note}" if note else ""))
    finally:
        db.close()


def cmd_resume(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        db.update_job_status(job_id, "queued")
        job = db.get_job(job_id)
        print(f"resumed {job['title']}")
    finally:
        db.close()


def cmd_cancel(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id, note, ref = _resolve_control_job_and_note(db, args)
        if not job_id:
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        patch = {"last_note": note} if note else None
        db.update_job_status(job_id, "cancelled", metadata_patch=patch)
        job = db.get_job(job_id)
        print(f"cancelled {job['title']}" + (f": {note}" if note else ""))
    finally:
        db.close()


def cmd_status(args: argparse.Namespace) -> None:
    db, config = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if _job_ref_text(args.job_id) and not job_id:
            print(f"No job matched: {_job_ref_text(args.job_id)}")
            return
        state = collect_dashboard_state(db, config, job_id=job_id, limit=args.limit)
        if args.json:
            print(json.dumps(state, ensure_ascii=False, indent=2, default=_json_default))
            return
        if args.full:
            print(render_dashboard(state, width=_terminal_width(), chars=args.chars), end="")
        else:
            print(render_overview(state, width=_terminal_width()), end="")
    finally:
        db.close()


def cmd_health(args: argparse.Namespace) -> None:
    db, config = _db()
    try:
        config.ensure_dirs()
        lock = daemon_lock_status(config.runtime.home / "agentd.lock")
        metadata = lock.get("metadata") if isinstance(lock.get("metadata"), dict) else {}
        events = read_daemon_events(config, limit=args.limit)
        job_id = _default_job_id(db)
        print("Nipux Health")
        print(_rule("="))
        print(f"daemon: {_daemon_state_line(lock)}")
        if metadata.get("last_heartbeat"):
            print(f"heartbeat: {metadata['last_heartbeat']}")
        if metadata.get("last_state"):
            print(f"state: {metadata['last_state']}")
        if metadata.get("last_status") or metadata.get("last_tool"):
            print(f"last step: {metadata.get('last_status') or '?'} {metadata.get('last_tool') or '-'}")
        if metadata.get("consecutive_failures"):
            print(f"consecutive failures: {metadata['consecutive_failures']}")
        if metadata.get("last_error"):
            print(
                f"last error: {metadata.get('last_error_type') or 'error'}: {_one_line(metadata['last_error'], args.chars)}"
            )
        print(f"model: {config.model.model}")
        print(f"state db: {config.runtime.state_db_path}")
        print(f"daemon log: {config.runtime.logs_dir / 'daemon.log'}")
        print(f"event log: {config.runtime.logs_dir / 'daemon-events.jsonl'}")
        print(f"autostart: {'installed' if _launch_agent_path().exists() else 'not installed'}")
        if job_id:
            job = db.get_job(job_id)
            steps = db.list_steps(job_id=job_id)
            artifacts = db.list_artifacts(job_id, limit=1)
            print()
            print(f"focus: {job['title']}")
            state = _job_display_state(job, bool(lock["running"]))
            print(
                f"state: {state} | worker: {_worker_label(job, bool(lock['running']))} | "
                f"steps: {_step_count(steps)} | latest artifacts: {len(artifacts)}"
            )
            if steps:
                print(f"latest: {_step_line(steps[-1], chars=args.chars)}")
        else:
            print()
            print("focus: no jobs")
        if events:
            print()
            print("recent daemon events:")
            job_titles = {job["id"]: job["title"] for job in db.list_jobs()}
            for event in events[-args.limit :]:
                print(f"  {_daemon_event_line(event, chars=args.chars, job_titles=job_titles)}")
        else:
            print()
            print("recent daemon events: none")
    finally:
        db.close()


def cmd_history(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        events = db.list_timeline_events(job_id, limit=args.limit)
        if args.json:
            print(
                json.dumps(
                    [_public_event(event) for event in events], ensure_ascii=False, indent=2, default=_json_default
                )
            )
            return
        print(f"history {job['title']}")
        print(_rule("="))
        if not events:
            print("No visible history yet.")
            return
        for event in events:
            if args.full:
                print(_event_line(event, chars=max(args.chars, 1200), full=True))
            else:
                _print_event_card(event, chars=args.chars)
    finally:
        db.close()


def cmd_events(args: argparse.Namespace) -> None:
    db, _ = _db()
    seen: set[str] = set()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        if not args.json:
            print(f"events {job['title']}")
            print(_rule("="))

        def emit() -> None:
            events = db.list_timeline_events(job_id, limit=args.limit)
            printed = False
            for event in events:
                event_id = str(event.get("id") or "")
                if event_id in seen:
                    continue
                seen.add(event_id)
                if args.json:
                    print(json.dumps(_public_event(event), ensure_ascii=False, default=_json_default), flush=True)
                else:
                    if args.full:
                        print(_event_line(event, chars=args.chars, full=True), flush=True)
                    else:
                        _print_event_card(event, chars=args.chars)
                printed = True
            if printed and not args.json:
                print(_rule("-"), flush=True)

        emit()
        while args.follow:
            time.sleep(args.interval)
            emit()
    except KeyboardInterrupt:
        print("\nevents stopped")
    finally:
        db.close()


def cmd_dashboard(args: argparse.Namespace) -> None:
    db, config = _db()
    try:
        while True:
            job_id = _resolve_job_id(db, args.job_id)
            if _job_ref_text(args.job_id) and not job_id:
                print(f"No job matched: {_job_ref_text(args.job_id)}")
                return
            state = collect_dashboard_state(db, config, job_id=job_id, limit=args.limit)
            if args.clear:
                print("\033[2J\033[H", end="")
            print(render_dashboard(state, width=_terminal_width(), chars=args.chars), end="", flush=True)
            if not args.follow:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ndashboard stopped")
    finally:
        db.close()


def cmd_artifacts(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        artifacts = db.list_artifacts(job_id, limit=args.limit)
        if not artifacts:
            print(f"No saved outputs recorded for {job['title']}.")
            return
        print(f"saved outputs {job['title']} (newest first)")
        print(_rule("-"))
        print("Open one with: artifact NUMBER, artifact latest, or artifact TITLE")
        for index, artifact in enumerate(artifacts, start=1):
            title = artifact.get("title") or artifact["id"]
            print(f"{index:>2}. {_one_line(title, 72)}")
            meta = f"{artifact['created_at']} | {artifact['type']} | id {artifact['id']}"
            print(f"    {meta}")
            if artifact.get("summary"):
                print(f"    {_one_line(_generic_display_text(artifact['summary']), args.chars)}")
            print(f"    view: artifact {index}")
            if args.paths:
                print(f"    path: {artifact['path']}")
    finally:
        db.close()


def cmd_artifact(args: argparse.Namespace) -> None:
    db, config = _db()
    try:
        store = ArtifactStore(config.runtime.home, db=db)
        ref = _job_ref_text(args.artifact_id_or_path)
        resolved = _resolve_artifact_ref(db, config, ref, job_id=_resolve_job_id(db, getattr(args, "job_id", None)))
        if not resolved:
            print(f"No artifact matched: {ref}")
            return
        content = store.read_text(resolved["id"] if resolved.get("id") else resolved["path"])
        if resolved.get("title"):
            print(f"artifact: {resolved['title']}")
            if resolved.get("summary"):
                print(f"summary: {resolved['summary']}")
            print(_rule("-"))
        if args.chars and len(content) > args.chars:
            content = content[: args.chars] + f"\n... truncated {len(content) - args.chars} chars\n"
        print(content, end="" if content.endswith("\n") else "\n")
    finally:
        db.close()


def cmd_lessons(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        _print_lessons(job, limit=args.limit, chars=args.chars)
    finally:
        db.close()


def cmd_learn(args: argparse.Namespace) -> None:
    lesson = " ".join(args.lesson).strip()
    if not lesson:
        print("usage: learn [--job JOB_TITLE] [--category CATEGORY] LESSON")
        return
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        entry = db.append_lesson(
            job_id, lesson, category=args.category or "operator_preference", metadata={"source": "operator"}
        )
        job = db.get_job(job_id)
        print(f"learned for {job['title']}: {_one_line(entry['lesson'], args.chars)}")
    finally:
        db.close()


def cmd_findings(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        findings = _metadata_records(job, "finding_ledger")
        if args.json:
            print(json.dumps(findings, ensure_ascii=False, indent=2, default=_json_default))
            return
        print(f"findings {job['title']} | {len(findings)} unique")
        print(_rule("="))
        if not findings:
            print("none yet")
            return
        ranked = sorted(findings, key=lambda finding: float(finding.get("score") or 0), reverse=True)
        for index, finding in enumerate(ranked[: args.limit], start=1):
            score = finding.get("score")
            score_text = f" score={score:g}" if isinstance(score, (int, float)) else ""
            print(f"{index:>2}. {_one_line(finding.get('name') or 'unknown', 54)}{score_text}")
            details = " | ".join(
                value
                for value in [
                    str(finding.get("location") or "").strip(),
                    str(finding.get("category") or "").strip(),
                    str(finding.get("status") or "").strip(),
                ]
                if value
            )
            if details:
                print(f"    {details}")
            if finding.get("url") or finding.get("source_url"):
                print(f"    {finding.get('url') or finding.get('source_url')}")
            if finding.get("reason"):
                print(f"    {_one_line(finding['reason'], args.chars)}")
    finally:
        db.close()


def cmd_tasks(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        tasks = _metadata_records(job, "task_queue")
        if args.status:
            wanted = {status.strip().lower() for status in args.status}
            tasks = [task for task in tasks if str(task.get("status") or "open").lower() in wanted]
        if args.json:
            print(json.dumps(tasks, ensure_ascii=False, indent=2, default=_json_default))
            return
        status_order = {"active": 0, "open": 1, "blocked": 2, "done": 3, "skipped": 4}
        ranked = sorted(
            tasks,
            key=lambda task: (
                status_order.get(str(task.get("status") or "open"), 9),
                -int(task.get("priority") or 0),
                str(task.get("title") or ""),
            ),
        )
        print(f"tasks {job['title']} | {len(ranked)} tracked")
        print(_rule("="))
        if not ranked:
            print("none yet")
            return
        for index, task in enumerate(ranked[: args.limit], start=1):
            status = str(task.get("status") or "open")
            priority = int(task.get("priority") or 0)
            print(f"{index:>2}. {status:<7} p={priority:<3} {_one_line(task.get('title') or 'untitled', 54)}")
            details = " | ".join(
                value
                for value in [
                    f"contract={task.get('output_contract')}" if task.get("output_contract") else "",
                    f"accept={task.get('acceptance_criteria')}" if task.get("acceptance_criteria") else "",
                    f"evidence={task.get('evidence_needed')}" if task.get("evidence_needed") else "",
                    f"stall={task.get('stall_behavior')}" if task.get("stall_behavior") else "",
                    str(task.get("goal") or "").strip(),
                    str(task.get("source_hint") or "").strip(),
                    str(task.get("result") or "").strip(),
                ]
                if value
            )
            if details:
                print(f"    {_one_line(details, args.chars)}")
    finally:
        db.close()


def cmd_roadmap(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
        if args.json:
            print(json.dumps(roadmap, ensure_ascii=False, indent=2, default=_json_default))
            return
        print(f"roadmap {job['title']}")
        print(_rule("="))
        if not roadmap:
            print("none yet")
            print("the worker can create one with record_roadmap when broad work needs milestones")
            return
        milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
        print(f"title: {roadmap.get('title') or 'Roadmap'}")
        print(f"status: {roadmap.get('status') or 'planned'} | milestones: {len(milestones)}")
        if roadmap.get("current_milestone"):
            print(f"current: {_one_line(roadmap.get('current_milestone') or '', args.chars)}")
        if roadmap.get("scope"):
            print(f"scope: {_one_line(roadmap.get('scope') or '', args.chars)}")
        if roadmap.get("validation_contract"):
            print(f"validation: {_one_line(roadmap.get('validation_contract') or '', args.chars)}")
        if not milestones:
            return
        print()
        status_order = {"active": 0, "validating": 1, "planned": 2, "blocked": 3, "done": 4, "skipped": 5}
        ranked = sorted(
            [milestone for milestone in milestones if isinstance(milestone, dict)],
            key=lambda milestone: (
                status_order.get(str(milestone.get("status") or "planned"), 9),
                -int(milestone.get("priority") or 0),
                str(milestone.get("title") or ""),
            ),
        )
        for index, milestone in enumerate(ranked[: args.limit], start=1):
            status = str(milestone.get("status") or "planned")
            validation = str(milestone.get("validation_status") or "not_started")
            features = milestone.get("features") if isinstance(milestone.get("features"), list) else []
            open_features = sum(
                1 for feature in features
                if isinstance(feature, dict) and str(feature.get("status") or "planned") in {"planned", "active"}
            )
            print(
                f"{index:>2}. {status:<10} validation={validation:<11} "
                f"p={int(milestone.get('priority') or 0):<3} {_one_line(milestone.get('title') or 'milestone', 54)}"
            )
            details = " | ".join(
                value
                for value in [
                    f"features={len(features)}/{open_features} open" if features else "",
                    f"accept={milestone.get('acceptance_criteria')}" if milestone.get("acceptance_criteria") else "",
                    f"evidence={milestone.get('evidence_needed')}" if milestone.get("evidence_needed") else "",
                    f"result={milestone.get('validation_result')}" if milestone.get("validation_result") else "",
                    f"next={milestone.get('next_action')}" if milestone.get("next_action") else "",
                ]
                if value
            )
            if details:
                print(f"    {_one_line(details, args.chars)}")
            for feature in features[: min(3, args.features)]:
                if not isinstance(feature, dict):
                    continue
                print(
                    f"    - {str(feature.get('status') or 'planned'):<7} "
                    f"{_one_line(feature.get('title') or 'feature', max(30, args.chars - 16))}"
                )
    finally:
        db.close()


def cmd_experiments(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        experiments = _metadata_records(job, "experiment_ledger")
        if args.status:
            wanted = {status.strip().lower() for status in args.status}
            experiments = [
                experiment for experiment in experiments if str(experiment.get("status") or "planned").lower() in wanted
            ]
        if args.json:
            print(json.dumps(experiments, ensure_ascii=False, indent=2, default=_json_default))
            return
        status_order = {"running": 0, "planned": 1, "measured": 2, "blocked": 3, "failed": 4, "skipped": 5}
        ranked = sorted(
            experiments,
            key=lambda experiment: (
                not bool(experiment.get("best_observed")),
                status_order.get(str(experiment.get("status") or "planned"), 9),
                str(experiment.get("updated_at") or experiment.get("created_at") or ""),
            ),
        )
        print(f"experiments {job['title']} | {len(ranked)} tracked")
        print(_rule("="))
        if not ranked:
            print("none yet")
            return
        for index, experiment in enumerate(ranked[: args.limit], start=1):
            status = str(experiment.get("status") or "planned")
            best = " *best*" if experiment.get("best_observed") else ""
            metric = ""
            if experiment.get("metric_value") is not None:
                metric = f" {experiment.get('metric_name') or 'metric'}={experiment.get('metric_value')}{experiment.get('metric_unit') or ''}"
            print(f"{index:>2}. {status:<8} {_one_line(experiment.get('title') or 'experiment', 54)}{metric}{best}")
            details = " | ".join(
                value
                for value in [
                    str(experiment.get("result") or "").strip(),
                    f"next: {experiment.get('next_action')}" if experiment.get("next_action") else "",
                    f"delta: {experiment.get('delta_from_previous_best')}"
                    if experiment.get("delta_from_previous_best") is not None
                    else "",
                ]
                if value
            )
            if details:
                print(f"    {_one_line(details, args.chars)}")
    finally:
        db.close()


def cmd_sources(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        sources = _metadata_records(job, "source_ledger")
        if args.json:
            print(json.dumps(sources, ensure_ascii=False, indent=2, default=_json_default))
            return
        ranked = sorted(
            sources,
            key=lambda source: (float(source.get("usefulness_score") or 0), int(source.get("yield_count") or 0)),
            reverse=True,
        )
        print(f"sources {job['title']} | {len(sources)} scored")
        print(_rule("="))
        if not ranked:
            print("none yet")
            return
        for index, source in enumerate(ranked[: args.limit], start=1):
            score = float(source.get("usefulness_score") or 0)
            print(
                f"{index:>2}. {_one_line(source.get('source') or 'unknown', 58)} "
                f"score={score:g} findings={source.get('yield_count') or 0} fails={source.get('fail_count') or 0}"
            )
            detail = " | ".join(
                value
                for value in [
                    str(source.get("source_type") or "").strip(),
                    str(source.get("last_outcome") or "").strip(),
                ]
                if value
            )
            if detail:
                print(f"    {_one_line(detail, args.chars)}")
            warnings = source.get("warnings") if isinstance(source.get("warnings"), list) else []
            if warnings:
                print(f"    warnings: {_one_line(', '.join(str(item) for item in warnings[-3:]), args.chars)}")
    finally:
        db.close()


def cmd_memory(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        lessons = _metadata_records(job, "lessons")
        reflections = _metadata_records(job, "reflections")
        compact = db.list_memory(job_id)
        active_operator = _active_operator_messages(metadata)
        pending_measurement = metadata.get("pending_measurement_obligation") if isinstance(metadata.get("pending_measurement_obligation"), dict) else {}
        print(f"memory {job['title']}")
        print(_rule("="))
        print(f"lessons={len(lessons)} reflections={len(reflections)} compact_entries={len(compact)}")
        if active_operator:
            print()
            print("active operator context:")
            for entry in active_operator[-min(args.limit, 8):]:
                marker = entry.get("event_id") or "operator"
                print(f"  {marker}: {_one_line(entry.get('message') or '', args.chars)}")
        if pending_measurement:
            print()
            print(f"pending measurement: step #{pending_measurement.get('source_step_no') or '?'}")
            candidates = pending_measurement.get("metric_candidates") if isinstance(pending_measurement.get("metric_candidates"), list) else []
            if candidates:
                print(f"  candidates: {_one_line(', '.join(str(item) for item in candidates[:5]), args.chars)}")
        if reflections:
            print()
            print("latest reflection:")
            reflection = reflections[-1]
            print(f"  {_one_line(reflection.get('summary') or '', args.chars)}")
            if reflection.get("strategy"):
                print(f"  strategy: {_one_line(reflection['strategy'], args.chars)}")
        if lessons:
            print()
            print("latest lessons:")
            for lesson in lessons[-min(args.limit, 8) :]:
                print(f"  {lesson.get('category') or 'memory'}: {_one_line(lesson.get('lesson') or '', args.chars)}")
        if compact:
            print()
            print("compact memory:")
            for entry in compact[: min(args.limit, 3)]:
                print(f"  {entry.get('key')}: {_one_line(entry.get('summary') or '', args.chars)}")
    finally:
        db.close()


def cmd_metrics(args: argparse.Namespace) -> None:
    db, config = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        steps = db.list_steps(job_id=job_id)
        artifacts = db.list_artifacts(job_id, limit=1000)
        findings = _metadata_records(job, "finding_ledger")
        sources = _metadata_records(job, "source_ledger")
        tasks = _metadata_records(job, "task_queue")
        experiments = _metadata_records(job, "experiment_ledger")
        lessons = _metadata_records(job, "lessons")
        reflections = _metadata_records(job, "reflections")
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
        milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
        daemon = daemon_lock_status(config.runtime.home / "agentd.lock")
        finding_batches = [
            artifact
            for artifact in artifacts
            if "finding" in str(artifact.get("title") or artifact.get("summary") or "").lower()
        ]
        blocked = [step for step in steps if step.get("status") == "blocked"]
        failed = [step for step in steps if step.get("status") == "failed"]
        print(f"metrics {job['title']}")
        print(_rule("="))
        print(
            f"daemon: {'running' if daemon['running'] else 'stopped'} | worker: {_worker_label(job, bool(daemon['running']))}"
        )
        print(f"steps: {_step_count(steps)} | failed: {len(failed)} | blocked/recovered: {len(blocked)}")
        print(f"artifacts: {len(artifacts)} | finding_batches: {len(finding_batches)}")
        print(
            f"findings: {len(findings)} | sources: {len(sources)} | tasks: {len(tasks)} | "
            f"milestones: {len(milestones)} | experiments: {len(experiments)} | "
            f"lessons: {len(lessons)} | reflections: {len(reflections)}"
        )
        if sources:
            best = max(sources, key=lambda source: float(source.get("usefulness_score") or 0))
            print(
                f"best source: {_one_line(best.get('source') or '', args.chars)} score={best.get('usefulness_score')}"
            )
        if findings:
            best_finding = max(findings, key=lambda finding: float(finding.get("score") or 0))
            print(
                f"best finding: {_one_line(best_finding.get('name') or '', args.chars)} score={best_finding.get('score')}"
            )
        measured = [experiment for experiment in experiments if experiment.get("metric_value") is not None]
        best_experiments = [experiment for experiment in measured if experiment.get("best_observed")]
        if best_experiments:
            best_experiment = best_experiments[-1]
            metric = f"{best_experiment.get('metric_name') or 'metric'}={best_experiment.get('metric_value')}{best_experiment.get('metric_unit') or ''}"
            print(f"best experiment: {_one_line(best_experiment.get('title') or '', args.chars)} {metric}")
    finally:
        db.close()


def _remote_model_preflight_failures(config) -> list[str]:
    host = (urlparse(config.model.base_url).hostname or "").lower()
    local_hosts = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if host in local_hosts or host.endswith(".local"):
        return []
    blocking = {"model_config", "model_auth", "model_endpoint", "model_generation"}
    checks = run_doctor(config=config, check_model=True)
    return [f"{check.name}: {check.detail}" for check in checks if not check.ok and check.name in blocking]


def _ensure_remote_model_ready_for_worker(config, *, fake: bool) -> bool:
    if fake:
        return True
    failures = _remote_model_preflight_failures(config)
    if not failures:
        return True
    print("model is not ready; daemon not started")
    for failure in failures:
        print(f"  fail {failure}")
    print("Run `nipux doctor --check-model` after fixing the model configuration.")
    return False


def cmd_start(args: argparse.Namespace) -> None:
    config = load_config()
    config.ensure_dirs()
    status = daemon_lock_status(config.runtime.home / "agentd.lock")
    if status["running"]:
        metadata = status.get("metadata") or {}
        if status.get("stale"):
            print(f"nipux daemon stale pid={metadata.get('pid', 'unknown')}; restarting")
            _stop_daemon_process(config, wait=5.0, quiet=True)
            time.sleep(0.5)
        else:
            print(f"nipux daemon already running pid={metadata.get('pid', 'unknown')}")
            return
    if not _ensure_remote_model_ready_for_worker(config, fake=args.fake):
        return
    log_path = Path(args.log_file).expanduser() if args.log_file else config.runtime.logs_dir / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "nipux_cli.cli",
        "daemon",
        "--poll-seconds",
        str(args.poll_seconds),
    ]
    if args.fake:
        command.append("--fake")
    if args.quiet:
        command.append("--quiet")
    else:
        command.append("--verbose")
    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(Path.cwd()),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(0.5)
    status = daemon_lock_status(config.runtime.home / "agentd.lock")
    if status["running"]:
        metadata = status.get("metadata") or {}
        print(f"nipux daemon started pid={metadata.get('pid') or process.pid}")
        print(f"log: {log_path}")
        return
    if process.poll() is None:
        print(f"nipux daemon process started pid={process.pid}, waiting for lock")
        print(f"log: {log_path}")
        return
    raise SystemExit(f"nipux daemon exited immediately with code {process.returncode}; see {log_path}")


def _start_daemon_if_needed(
    *, poll_seconds: float, fake: bool = False, quiet: bool = False, log_file: str | None = None
) -> None:
    config = load_config()
    config.ensure_dirs()
    status = daemon_lock_status(config.runtime.home / "agentd.lock")
    if status["running"]:
        metadata = status.get("metadata") or {}
        if status.get("stale"):
            print(f"daemon stale pid={metadata.get('pid', 'unknown')}; restarting")
            _stop_daemon_process(config, wait=5.0, quiet=True)
            time.sleep(0.5)
            cmd_start(argparse.Namespace(poll_seconds=poll_seconds, fake=fake, quiet=quiet, log_file=log_file))
            return
        print(f"daemon already running pid={metadata.get('pid', 'unknown')}")
        return
    cmd_start(argparse.Namespace(poll_seconds=poll_seconds, fake=fake, quiet=quiet, log_file=log_file))


def cmd_restart(args: argparse.Namespace) -> None:
    config = load_config()
    config.ensure_dirs()
    stopped = _stop_daemon_process(config, wait=args.wait, quiet=False)
    if stopped:
        time.sleep(0.5)
    cmd_start(argparse.Namespace(poll_seconds=args.poll_seconds, fake=args.fake, quiet=args.quiet, log_file=args.log_file))


def _stop_daemon_process(config, *, wait: float, quiet: bool) -> bool:
    status = daemon_lock_status(config.runtime.home / "agentd.lock")
    if not status["running"]:
        if not quiet:
            print("nipux daemon is not running")
        return False
    metadata = status.get("metadata") or {}
    pid = metadata.get("pid")
    if not isinstance(pid, int):
        raise SystemExit("daemon is running but lock file has no pid; stop it from the terminal that owns it")
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + wait
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            if not quiet:
                print(f"nipux daemon stopped pid={pid}")
            return True
        time.sleep(0.2)
    if not quiet:
        print(f"sent SIGTERM to nipux daemon pid={pid}; it may still be shutting down")
    return False


def cmd_stop(args: argparse.Namespace) -> None:
    requested_job = _job_ref_text(getattr(args, "job_id", None))
    if requested_job:
        db, _ = _db()
        try:
            job_id = _resolve_job_id(db, requested_job)
            if not job_id:
                print(f"No job matched: {requested_job}")
                return
            db.update_job_status(job_id, "paused", metadata_patch={"last_note": "stopped by operator"})
            job = db.get_job(job_id)
            print(f"stopped {job['title']} (paused job)")
            print("Use resume/run to start it again. Plain 'stop' still stops the daemon.")
            return
        finally:
            db.close()

    config = load_config()
    _stop_daemon_process(config, wait=args.wait, quiet=False)


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.nipux.agent.plist"


def _launch_agent_plist(*, poll_seconds: float, quiet: bool) -> str:
    config = load_config()
    config.ensure_dirs()
    command = [
        sys.executable,
        "-m",
        "nipux_cli.cli",
        "daemon",
        "--poll-seconds",
        str(poll_seconds),
    ]
    command.append("--quiet" if quiet else "--verbose")
    args_xml = "\n".join(f"        <string>{_xml_escape(part)}</string>" for part in command)
    log_path = config.runtime.logs_dir / "launchd-daemon.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.nipux.agent</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
      <key>NIPUX_HOME</key>
      <string>{_xml_escape(str(config.runtime.home))}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{_xml_escape(str(log_path))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(str(log_path))}</string>
    <key>WorkingDirectory</key>
    <string>{_xml_escape(str(Path.cwd()))}</string>
  </dict>
</plist>
"""


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cmd_autostart(args: argparse.Namespace) -> None:
    path = _launch_agent_path()
    label = "gui/" + str(os.getuid()) + "/com.nipux.agent"
    if args.action == "status":
        status = "installed" if path.exists() else "not installed"
        print(f"autostart: {status}")
        print(f"plist: {path}")
        if path.exists():
            result = subprocess.run(
                ["launchctl", "print", label], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            print("launchd: loaded" if result.returncode == 0 else "launchd: not loaded")
        return
    if args.action == "install":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_launch_agent_plist(poll_seconds=args.poll_seconds, quiet=args.quiet), encoding="utf-8")
        subprocess.run(
            ["launchctl", "bootout", label], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        result = subprocess.run(["launchctl", "bootstrap", "gui/" + str(os.getuid()), str(path)], check=False)
        if result.returncode:
            raise SystemExit(result.returncode)
        subprocess.run(["launchctl", "enable", label], check=False)
        print(f"autostart installed: {path}")
        print("daemon will start at login and launchd will keep it alive")
        return
    if args.action == "uninstall":
        subprocess.run(
            ["launchctl", "bootout", label], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if path.exists():
            path.unlink()
        print("autostart uninstalled")
        return
    raise SystemExit(f"unknown autostart action: {args.action}")


def _systemd_service_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "nipux.service"


def _systemd_service_text(*, poll_seconds: float, quiet: bool) -> str:
    config = load_config()
    config.ensure_dirs()
    command = [
        sys.executable,
        "-m",
        "nipux_cli.cli",
        "daemon",
        "--poll-seconds",
        str(poll_seconds),
    ]
    command.append("--quiet" if quiet else "--verbose")
    return "\n".join(
        [
            "[Unit]",
            "Description=Nipux 24/7 autonomous worker",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={Path.cwd()}",
            f"Environment=NIPUX_HOME={config.runtime.home}",
            f"ExecStart={' '.join(shlex.quote(part) for part in command)}",
            "Restart=always",
            "RestartSec=3",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def cmd_service(args: argparse.Namespace) -> None:
    path = _systemd_service_path()
    systemctl = shutil.which("systemctl")
    user_cmd = [systemctl, "--user"] if systemctl else None
    if args.action == "status":
        print(f"service: {'installed' if path.exists() else 'not installed'}")
        print(f"unit: {path}")
        if user_cmd:
            result = subprocess.run(
                [*user_cmd, "is-active", "nipux.service"], check=False, capture_output=True, text=True
            )
            print(f"systemd: {result.stdout.strip() or result.stderr.strip() or 'unknown'}")
        else:
            print("systemd: unavailable on this machine")
        return
    if args.action == "install":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_systemd_service_text(poll_seconds=args.poll_seconds, quiet=args.quiet), encoding="utf-8")
        print(f"service file written: {path}")
        if user_cmd:
            subprocess.run([*user_cmd, "daemon-reload"], check=False)
            subprocess.run([*user_cmd, "enable", "--now", "nipux.service"], check=False)
            print("systemd user service enabled and started")
        else:
            print(
                "systemd not found; copy this service to a Linux server or run: systemctl --user enable --now nipux.service"
            )
        return
    if args.action == "uninstall":
        if user_cmd:
            subprocess.run([*user_cmd, "disable", "--now", "nipux.service"], check=False)
            subprocess.run([*user_cmd, "daemon-reload"], check=False)
        if path.exists():
            path.unlink()
        print("service uninstalled")
        return
    raise SystemExit(f"unknown service action: {args.action}")


def cmd_browser_dashboard(args: argparse.Namespace) -> None:
    from nipux_cli.browser import _find_agent_browser

    config = load_config()
    config.ensure_dirs()
    if args.stop:
        result = subprocess.run([*_find_agent_browser(), "dashboard", "stop"], check=False)
        if result.returncode:
            raise SystemExit(result.returncode)
        print("agent-browser dashboard stopped")
        return

    command = [*_find_agent_browser(), "dashboard", "start", "--port", str(args.port)]
    if args.foreground:
        raise SystemExit(subprocess.call(command))

    log_path = Path(args.log_file).expanduser() if args.log_file else config.runtime.logs_dir / "browser-dashboard.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(Path.cwd()),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"agent-browser dashboard started pid={process.pid}")
    print(f"url: http://127.0.0.1:{args.port}")
    print(f"log: {log_path}")


def _clip_json(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated {len(text) - limit} chars"


def _print_step(step: dict[str, Any], *, verbose: bool = False, chars: int = 4000) -> None:
    tool = step.get("tool_name") or "-"
    summary = _one_line(_clean_step_summary(step.get("summary") or ""), chars)
    error = _one_line(step["error"], chars) if step.get("error") else ""
    print(f"step #{step['step_no']} {step['started_at']} {step['status']} {step['kind']} {tool}")
    if summary:
        print(f"  summary: {summary}")
    if error:
        print(f"  error: {error}")
    output_data = step.get("output") or {}
    if not verbose and isinstance(output_data, dict):
        artifact_id = output_data.get("artifact_id")
        if artifact_id:
            print(f"  artifact: {artifact_id} (view with: artifact {artifact_id})")
        lesson = output_data.get("lesson") if isinstance(output_data.get("lesson"), dict) else None
        if lesson:
            print(f"  lesson: {_one_line(lesson.get('lesson') or '', chars)}")
        update = output_data.get("update") if isinstance(output_data.get("update"), dict) else None
        if update:
            print(f"  update: {_one_line(update.get('message') or '', chars)}")
        source = output_data.get("source") if isinstance(output_data.get("source"), dict) else None
        if source:
            print(f"  source: {_one_line(source.get('source') or '', chars)} score={source.get('usefulness_score')}")
        if isinstance(output_data.get("findings"), list):
            print(f"  findings: {output_data.get('added', 0)} new, {output_data.get('updated', 0)} updated")
        checkpoint = (
            output_data.get("auto_checkpoint") if isinstance(output_data.get("auto_checkpoint"), dict) else None
        )
        if checkpoint:
            print(f"  auto checkpoint: {checkpoint.get('artifact_id')}")
    if verbose:
        input_data = step.get("input") or {}
        if input_data:
            print("  input:")
            print(_clip_json(input_data, chars))
        if output_data:
            print("  output:")
            print(_clip_json(output_data, chars))


def _print_artifact(artifact: dict[str, Any]) -> None:
    title = artifact.get("title") or artifact["id"]
    print(f"artifact {artifact['created_at']} {artifact['type']} {title}")
    print(f"  {artifact['path']}")


def _print_run(run: dict[str, Any]) -> None:
    print(f"run {run['started_at']} {run['status']} {run['id']} {run.get('model') or ''}")
    if run.get("error"):
        print(f"  error: {run['error']}")


def _print_startup_history(job_id: str, *, limit: int, chars: int) -> None:
    db, config = _db()
    try:
        job = db.get_job(job_id)
        jobs = db.list_jobs()
        steps = db.list_steps(job_id=job_id)
        artifacts = db.list_artifacts(job_id, limit=1000)
        memory_entries = db.list_memory(job_id)
        events = db.list_timeline_events(job_id, limit=limit)
        daemon = daemon_lock_status(config.runtime.home / "agentd.lock")
    finally:
        db.close()
    print()
    _print_session_overview(
        job,
        steps=steps,
        artifacts=artifacts,
        memory_entries=memory_entries,
        daemon_running=bool(daemon["running"]),
        model=config.model.model,
        artifacts_dir=config.runtime.jobs_dir / job_id / "artifacts",
        jobs=jobs,
        chars=chars,
    )
    print()
    print(_section_title("Recent activity", f"{job['title']}"))
    if not events:
        print("  No visible history yet.")
        return
    display_events = _important_startup_events(events, limit=min(limit, 8))
    artifact_indexes = {str(artifact["id"]): index for index, artifact in enumerate(artifacts, start=1)}
    for event in display_events:
        _print_event_card(event, chars=min(chars, 140), artifact_indexes=artifact_indexes)
    if len(events) > len(display_events):
        print(f"  ... {len(events) - len(display_events)} older events hidden. Use /history for the full timeline.")


def _print_session_overview(
    job: dict[str, Any],
    *,
    steps: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    memory_entries: list[dict[str, Any]],
    daemon_running: bool,
    model: str,
    artifacts_dir: Path,
    jobs: list[dict[str, Any]],
    chars: int,
) -> None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    findings = _metadata_records(job, "finding_ledger")
    sources = _metadata_records(job, "source_ledger")
    tasks = _metadata_records(job, "task_queue")
    experiments = _metadata_records(job, "experiment_ledger")
    lessons = _metadata_records(job, "lessons")
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    open_tasks = sum(1 for task in tasks if str(task.get("status") or "open") in {"open", "active"})
    state = _job_display_state(job, daemon_running)
    worker = _worker_label(job, daemon_running)
    print(_section_title("Workspace"))
    print(f"  model      {model}")
    print(f"  focus      {job['title']}")
    print(f"  state      {_status_badge(state)}   worker {_status_badge(worker)}   kind {job['kind']}")
    next_action = _next_operator_action(job, daemon_running)
    if next_action:
        print(f"  next       {next_action}")

    print()
    _print_jobs_panel(jobs, focused_job_id=str(job["id"]), daemon_running=daemon_running)

    print()
    print(_section_title("Focus"))
    _print_wrapped(
        "  goal       ", job.get("objective") or "", width=_terminal_width(), subsequent_indent="             "
    )
    planning = metadata.get("planning") if isinstance(metadata.get("planning"), dict) else {}
    if job.get("status") == "planning" and planning:
        print("  plan       waiting for your answers or /run")
        questions = planning.get("questions") if isinstance(planning.get("questions"), list) else []
        for question in questions[:3]:
            _print_wrapped("  question   ", question, width=_terminal_width(), subsequent_indent="             ")

    print()
    print(_section_title("Progress"))
    _print_metric_grid(
        [
            ("actions", _step_count(steps)),
            ("outputs", len(artifacts)),
            ("findings", len(findings)),
            ("sources", len(sources)),
            ("tasks", f"{len(tasks)} ({open_tasks} open)"),
            ("roadmap", len(milestones)),
            ("experiments", len(experiments)),
            ("lessons", len(lessons)),
            ("memory", len(memory_entries)),
        ]
    )
    print(f"  output dir {_short_path(artifacts_dir, max_width=min(_terminal_width() - 13, 84))}")


def _print_chat_composer(job: dict[str, Any]) -> None:
    width = min(_terminal_width(), 96)
    if _fancy_ui():
        print(_accent("╭─ Message " + "─" * max(0, width - 11)))
        print("│ Type normally to chat. Live steps stream above. /jobs switches workspaces. /help shows commands.")
        print("╰─" + "─" * max(0, width - 2))
        return
    print(_section_title("Message"))
    print("  Type normally to chat. Live steps stream above. /jobs switches workspaces. /help shows commands.")


def _chat_prompt(job: dict[str, Any]) -> str:
    return f"{_accent('nipux')} > "


def _start_chat_live_feed(job_id: str) -> tuple[threading.Event | None, threading.Thread | None]:
    if (
        not sys.stdin.isatty()
        or not sys.stdout.isatty()
        or os.environ.get("NIPUX_NO_LIVE")
        or os.environ.get("NIPUX_PLAIN")
    ):
        return None, None
    stop = threading.Event()
    thread = threading.Thread(target=_chat_live_feed_loop, args=(job_id, stop), daemon=True)
    thread.start()
    return stop, thread


def _chat_live_feed_loop(initial_job_id: str, stop: threading.Event) -> None:
    seen_by_job: dict[str, set[str]] = {}
    initialized_jobs: set[str] = set()
    active_job_id = initial_job_id
    while not stop.wait(1.0):
        try:
            db, _ = _db()
            try:
                focused = _default_job_id(db) or active_job_id
                active_job_id = focused
                seen = seen_by_job.setdefault(focused, set())
                events = db.list_events(job_id=focused, limit=40)
                if focused not in initialized_jobs:
                    initialized_jobs.add(focused)
                    seen.update(str(event.get("id") or "") for event in events)
                    continue
                for event in events:
                    event_id = str(event.get("id") or "")
                    if not event_id or event_id in seen:
                        continue
                    seen.add(event_id)
                    line = _minimal_live_event_line(event)
                    if line:
                        _print_live_line(line)
            finally:
                db.close()
        except Exception:
            continue


def _print_live_line(line: str) -> None:
    try:
        if _fancy_ui():
            print(f"\r\033[K{_live_badge(line)} {line}\n{_chat_prompt({})}", end="", flush=True)
        else:
            print(f"\n· {line}", flush=True)
    except Exception:
        return


def _print_wrapped(prefix: str, text: Any, *, width: int, subsequent_indent: str = "") -> None:
    content = " ".join(str(text).split())
    if not content:
        print(prefix.rstrip())
        return
    available = max(20, min(width, 96) - len(prefix))
    wrapped = textwrap.wrap(content, width=available) or [content]
    print(prefix + wrapped[0])
    for line in wrapped[1:]:
        print(subsequent_indent + line)


def _section_title(title: str, subtitle: str = "") -> str:
    text = title.upper()
    if subtitle:
        text = f"{text} - {_one_line(subtitle, 52)}"
    width = min(_terminal_width(), 96)
    if len(text) >= width - 2:
        return text[:width]
    if _fancy_ui():
        return _accent(f"╭─ {text} " + "─" * max(0, width - len(text) - 4))
    return f"{text} " + "-" * max(0, width - len(text) - 1)


def _print_metric_grid(items: list[tuple[str, Any]]) -> None:
    width = min(_terminal_width(), 96)
    cell_width = 24 if width >= 80 else 18
    cells = [f"{label:<12} {value}"[:cell_width].ljust(cell_width) for label, value in items]
    columns = max(1, width // cell_width)
    for start in range(0, len(cells), columns):
        print("  " + "  ".join(cells[start : start + columns]).rstrip())


def _print_command_grid(items: list[tuple[str, str]]) -> None:
    width = min(_terminal_width(), 96)
    cell_width = 38 if width >= 90 else 30
    cells = [f"{command:<15} {label}"[:cell_width].ljust(cell_width) for command, label in items]
    columns = max(1, width // cell_width)
    for start in range(0, len(cells), columns):
        print("  " + "  ".join(cells[start : start + columns]).rstrip())


def _short_path(path: Path | str, *, max_width: int = 80) -> str:
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + os.sep):
        text = "~" + text[len(home) :]
    if len(text) <= max_width:
        return text
    keep = max(12, max_width - 4)
    return "..." + text[-keep:]


def _print_jobs_panel(jobs: list[dict[str, Any]], *, focused_job_id: str, daemon_running: bool) -> None:
    print(_section_title("Jobs"))
    if not jobs:
        print("  No jobs yet. Type an objective or use /new OBJECTIVE.")
        return
    print("  #  job                         state       worker      kind")
    for index, item in enumerate(jobs[:8], start=1):
        marker = "*" if str(item.get("id")) == focused_job_id else " "
        state = _job_display_state(item, daemon_running)
        worker = _worker_label(item, daemon_running)
        title = _one_line(item.get("title") or item.get("id") or "job", 27)
        print(
            f"  {marker}{index:<2} {title:<27} {_status_badge(state):<11} {_status_badge(worker):<11} {item.get('kind') or ''}"
        )
    if len(jobs) > 8:
        print(f"  ... {len(jobs) - 8} more. Use /jobs for the full list.")
    print("  switch: /focus JOB_TITLE")


def _next_operator_action(job: dict[str, Any], daemon_running: bool) -> str:
    status = str(job.get("status") or "")
    if status == "planning":
        return "answer the plan questions, or use Run when ready"
    if status == "cancelled":
        return "resume to reopen this job, or delete it"
    if status == "paused":
        return "resume, then run to continue"
    if status in {"queued", "running"} and not daemon_running:
        return "run to start background work"
    if status in {"queued", "running"} and daemon_running:
        return "daemon is active; live steps will stream here"
    if status == "completed":
        return "inspect history or artifacts"
    if status == "failed":
        return "resume, then run one worker step to test recovery"
    return ""


def _important_startup_events(events: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    if len(events) <= limit:
        return events
    important_types = {
        "operator_message",
        "agent_message",
        "artifact",
        "finding",
        "task",
        "experiment",
        "lesson",
        "reflection",
        "error",
        "compaction",
    }
    selected: list[dict[str, Any]] = []
    for event in reversed(events):
        if event.get("event_type") in important_types:
            selected.append(event)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for event in reversed(events):
            if event not in selected:
                selected.append(event)
            if len(selected) >= limit:
                break
    selected.sort(key=lambda event: (str(event.get("created_at") or ""), str(event.get("id") or "")))
    return selected


def _event_line(event: dict[str, Any], *, chars: int, full: bool = False) -> str:
    when, label, detail, access = _event_display_parts(event, chars=chars, full=full)
    suffix = f" | {access}" if access and full else ""
    return f"{when:<16} {label:<8} {_one_line(detail + suffix, chars)}"


def _print_event_card(event: dict[str, Any], *, chars: int, artifact_indexes: dict[str, int] | None = None) -> None:
    when, label, detail, access = _event_display_parts(event, chars=chars, full=False)
    artifact_indexes = artifact_indexes or {}
    artifact_index = artifact_indexes.get(str(event.get("ref_id") or ""))
    if artifact_index and event.get("event_type") == "artifact":
        access = f"open: /artifact {artifact_index}"
    print(f"  {_event_badge(label):<8} {_muted(when):<16} {_one_line(detail, chars)}")
    if access:
        print(f"  {'':<8} {'':<16} {access}")


def _event_display_parts(event: dict[str, Any], *, chars: int, full: bool = False) -> tuple[str, str, str, str]:
    when = str(event.get("created_at") or "?")
    when = _compact_time(when)
    kind = str(event.get("event_type") or "event")
    title = str(event.get("title") or "").strip()
    body = _generic_display_text(event.get("body") or "")
    ref_table = str(event.get("ref_table") or "")
    ref_id = str(event.get("ref_id") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    label = _event_label(kind, metadata)
    access = ""
    if kind == "tool_result" and metadata.get("status"):
        label = _event_label(f"{kind}:{metadata.get('status')}", metadata)
    if kind == "error":
        label = "ERROR"
    if kind.startswith("tool_result") or kind == "error":
        body = _clean_step_summary(body)
    if kind == "artifact":
        title = title or ref_id
        if body.startswith("/") or "/.nipux/jobs/" in body or "/jobs/job_" in body:
            body = _generic_display_text(metadata.get("summary") or "saved output")
        if title:
            access = f"open: /artifact {shlex.quote(title)}"
    if kind == "operator_message" and metadata.get("mode"):
        title = f"{title or 'operator'} {metadata.get('mode')}"
    if kind == "operator_context":
        body = body or f"{metadata.get('count') or 0} message(s)"
    if kind in {"tool_call", "tool_result", "error"} and metadata.get("step_no"):
        title = f"#{metadata.get('step_no')} {title}".strip()
    if not body and kind == "artifact" and metadata.get("path"):
        body = str(metadata.get("type") or "saved artifact")
    if not body and kind == "finding" and metadata.get("category"):
        body = str(metadata.get("category") or "")
    if not body and kind == "task" and metadata.get("status"):
        body = str(metadata.get("status") or "")
    if not body and kind == "roadmap" and metadata.get("status"):
        body = str(metadata.get("status") or "")
    if not body and kind == "milestone_validation" and metadata.get("validation_status"):
        body = str(metadata.get("validation_status") or "")
    if not body and kind == "experiment":
        metric_value = metadata.get("metric_value")
        if metric_value is not None:
            body = f"{metadata.get('metric_name') or 'metric'}={metric_value}{metadata.get('metric_unit') or ''}"
    if kind == "compaction":
        body = _one_line(body, min(chars, 140))
    if kind == "daemon" and title == "run started":
        body = body or str(metadata.get("model") or "")
    detail = title if title else kind
    if body:
        detail = f"{detail} - {body}"
    if ref_table and ref_id and full:
        detail = f"{detail} [{ref_table}:{ref_id}]"
    return when, label, detail, access


def _event_label(kind: str, metadata: dict[str, Any]) -> str:
    if kind == "operator_message":
        mode = str(metadata.get("mode") or "")
        return "FOLLOW" if mode == "follow_up" else "USER"
    if kind == "operator_context":
        return "ACK"
    if kind == "agent_message":
        return "AGENT"
    if kind == "roadmap":
        return "ROAD"
    if kind == "milestone_validation":
        return "VALID"
    if kind == "tool_call":
        return "TOOL"
    if kind.startswith("tool_result"):
        status = str(metadata.get("status") or "")
        if status == "blocked":
            return "BLOCK"
        if status == "failed" or kind.endswith(":failed"):
            return "ERROR"
        return "DONE"
    if kind == "artifact":
        return "OUTPUT"
    if kind == "finding":
        return "FIND"
    if kind == "source":
        return "SOURCE"
    if kind == "task":
        return "TASK"
    if kind == "experiment":
        return "TEST"
    if kind == "lesson":
        return "LEARN"
    if kind == "reflection":
        return "PLAN"
    if kind == "digest":
        return "DIGEST"
    if kind == "compaction":
        return "MEMORY"
    if kind == "error":
        return "ERROR"
    if kind == "daemon":
        return "SYSTEM"
    return kind.upper()[:8]


def _compact_time(value: str) -> str:
    text = value.replace("T", " ")
    if len(text) >= 16 and text[4:5] == "-" and text[13:14] == ":":
        return text[:16]
    return _one_line(text, 16)


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    public = dict(event)
    public.pop("metadata_json", None)
    return public


def _print_event_details(event: dict[str, Any], *, chars: int) -> None:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    if not metadata:
        return
    compact = {
        key: value
        for key, value in metadata.items()
        if key not in {"input", "output"} and value not in (None, "", [], {})
    }
    if compact:
        print(f"     meta: {_one_line(json.dumps(compact, ensure_ascii=False, sort_keys=True, default=str), chars)}")
    if isinstance(metadata.get("input"), dict):
        print(
            f"     input: {_one_line(json.dumps(metadata['input'], ensure_ascii=False, sort_keys=True, default=str), chars)}"
        )
    if isinstance(metadata.get("output"), dict):
        print(
            f"     output: {_one_line(json.dumps(metadata['output'], ensure_ascii=False, sort_keys=True, default=str), chars)}"
        )


def _step_line(step: dict[str, Any], *, chars: int = 180) -> str:
    tool = step.get("tool_name") or step.get("kind") or "-"
    summary = _clean_step_summary(step.get("summary") or step.get("error") or "-")
    error = " ERROR" if step.get("error") else ""
    return f"#{step['step_no']:<4} {step['status']:<9} {tool:<18} {_one_line(summary, chars)}{error}"


def _terminal_width() -> int:
    return shutil.get_terminal_size((120, 40)).columns


def _rule(char: str = "-", width: int | None = None) -> str:
    return char * min(width or _terminal_width(), 96)


def _json_default(value: Any) -> str:
    return str(value)


def _daemon_state_line(lock: dict[str, Any]) -> str:
    metadata = lock.get("metadata") if isinstance(lock.get("metadata"), dict) else {}
    if lock.get("running"):
        pid = metadata.get("pid") or "unknown"
        stale = " stale-runtime" if lock.get("stale") else ""
        return f"running pid={pid}{stale}"
    return "stopped (start with: nipux start)"


def _worker_label(job: dict[str, Any], daemon_running: bool) -> str:
    status = str(job.get("status") or "")
    if status == "planning":
        return "waiting"
    if status in {"paused", "completed", "cancelled", "failed"}:
        return status
    return "active" if daemon_running and status in {"running", "queued"} else "idle"


def _job_display_state(job: dict[str, Any], daemon_running: bool) -> str:
    status = str(job.get("status") or "")
    if status in {"running", "queued"}:
        return "advancing" if daemon_running else "open"
    return status or "unknown"


def _daemon_event_line(event: dict[str, Any], *, chars: int, job_titles: dict[str, str] | None = None) -> str:
    at = str(event.get("at") or "?")
    name = str(event.get("event") or "?")
    pieces = []
    job_titles = job_titles or {}
    for key in ("status", "tool", "job_id", "step_id", "error_type", "detail", "error"):
        value = event.get(key)
        if value not in (None, ""):
            label = "job" if key == "job_id" else key
            if key == "job_id":
                value = job_titles.get(str(value), value)
            pieces.append(f"{label}={value}")
    suffix = " ".join(pieces)
    return _one_line(f"{at} {name} {suffix}".strip(), chars)


def _job_ref_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text or None


def _note_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(item) for item in value).strip()
    return str(value).strip()


def _resolve_control_job_and_note(db: AgentDB, args: argparse.Namespace) -> tuple[str | None, str, str | None]:
    if hasattr(args, "parts"):
        parts = [str(part) for part in getattr(args, "parts") or []]
        if not parts:
            return _default_job_id(db), "", None
        for end in range(len(parts), 0, -1):
            ref = " ".join(parts[:end])
            job = _find_job(db, ref)
            if job:
                return str(job["id"]), " ".join(parts[end:]).strip(), ref
        return None, "", " ".join(parts)
    job_ref = _job_ref_text(getattr(args, "job_id", None))
    return _resolve_job_id(db, job_ref), _note_text(getattr(args, "note", None)), job_ref


def _default_job_id(db: AgentDB) -> str | None:
    configured = _configured_focus_job_id(db)
    if configured:
        return configured
    jobs = db.list_jobs()
    for status in ("running", "queued", "planning", "paused", "failed", "completed"):
        for job in jobs:
            if job.get("status") == status:
                return str(job["id"])
    return str(jobs[0]["id"]) if jobs else None


def _configured_focus_job_id(db: AgentDB) -> str | None:
    job_id = _read_shell_state().get("focus_job_id")
    if not isinstance(job_id, str) or not job_id:
        return None
    try:
        db.get_job(job_id)
    except KeyError:
        return None
    return job_id


def _find_job(db: AgentDB, query: str) -> dict[str, Any] | None:
    needle = " ".join(query.split()).lower()
    if not needle:
        return None
    jobs = db.list_jobs()
    for job in jobs:
        if str(job["id"]).lower() == needle:
            return job
    for job in jobs:
        if str(job.get("title") or "").lower() == needle:
            return job
    for job in jobs:
        if needle in str(job.get("title") or "").lower():
            return job
    return None


def _shell_state_path() -> Path:
    config = load_config()
    config.ensure_dirs()
    return config.runtime.home / "shell_state.json"


def _read_shell_state() -> dict[str, Any]:
    path = _shell_state_path()
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_shell_state(patch: dict[str, Any]) -> None:
    state = _read_shell_state()
    state.update(patch)
    _shell_state_path().write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _step_by_id(db: AgentDB, job_id: str, step_id: str) -> dict[str, Any] | None:
    for step in db.list_steps(job_id=job_id):
        if step["id"] == step_id:
            return step
    return None


def _step_count(steps: list[dict[str, Any]]) -> int:
    numbers = [int(step.get("step_no") or 0) for step in steps]
    return max(numbers, default=0)


def _job_lessons(job: dict[str, Any]) -> list[dict[str, Any]]:
    return _metadata_records(job, "lessons")


def _metadata_records(job: dict[str, Any], key: str) -> list[dict[str, Any]]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    values = metadata.get(key) if isinstance(metadata.get(key), list) else []
    return [entry for entry in values if isinstance(entry, dict)]


def _active_operator_messages(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    return [
        entry for entry in messages
        if isinstance(entry, dict)
        and entry in active_prompt_operator_entries(messages)
        and str(entry.get("mode") or "steer") in {"steer", "follow_up"}
    ]


def _print_lessons(job: dict[str, Any], *, limit: int, chars: int) -> None:
    lessons = _job_lessons(job)
    print(f"lessons {job['title']}")
    print(_rule("="))
    if not lessons:
        print("none yet")
        print("add one with: learn this source is not useful for the current objective")
        return
    for index, lesson in enumerate(lessons[-limit:], start=max(1, len(lessons) - limit + 1)):
        category = lesson.get("category") or "memory"
        confidence = lesson.get("confidence")
        suffix = f" | confidence {confidence:g}" if isinstance(confidence, (int, float)) else ""
        print(f"{index:>2}. {category}{suffix}")
        print(f"    {_one_line(lesson.get('lesson') or '', chars)}")


def _resolve_artifact_ref(
    db: AgentDB,
    config: Any,
    query: str | None,
    *,
    job_id: str | None = None,
) -> dict[str, Any] | None:
    if not query:
        return None
    ref = query.strip()
    path = Path(ref).expanduser()
    if path.exists():
        return {"path": str(path), "title": path.name, "summary": ""}

    ref_lower = ref.lower()
    focused_artifacts = db.list_artifacts(job_id, limit=250) if job_id else []
    if focused_artifacts and ref_lower in {"latest", "last", "newest"}:
        return focused_artifacts[0]
    index_ref = ref_lower[1:] if ref_lower.startswith("#") else ref_lower
    if focused_artifacts and index_ref.isdigit():
        index = int(index_ref)
        if 1 <= index <= len(focused_artifacts):
            return focused_artifacts[index - 1]

    jobs = db.list_jobs()
    ordered_jobs = []
    if job_id:
        try:
            selected = db.get_job(job_id)
            ordered_jobs.append(selected)
        except KeyError:
            pass
    ordered_jobs.extend(job for job in jobs if not job_id or job["id"] != job_id)
    artifacts: list[dict[str, Any]] = []
    for job in ordered_jobs:
        artifacts.extend(db.list_artifacts(job["id"], limit=250))

    for artifact in artifacts:
        if str(artifact["id"]).lower() == ref_lower:
            return artifact
    for artifact in artifacts:
        title = str(artifact.get("title") or "")
        if title.lower() == ref_lower:
            return artifact
    for artifact in artifacts:
        haystack = " ".join(str(artifact.get(key) or "") for key in ("title", "summary", "type")).lower()
        if ref_lower in haystack:
            return artifact

    store = ArtifactStore(config.runtime.home, db=db)
    search_job_ids = [job_id] if job_id else [str(job["id"]) for job in ordered_jobs]
    for candidate_job_id in search_job_ids:
        if not candidate_job_id:
            continue
        for result in store.search_text(job_id=candidate_job_id, query=ref, limit=1):
            try:
                return db.get_artifact(str(result["id"]))
            except KeyError:
                continue
    return None


def cmd_logs(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            ref = _job_ref_text(args.job_id)
            print(f"No job matched: {ref}" if ref else "No jobs found.")
            return
        job = db.get_job(job_id)
        daemon = daemon_lock_status(load_config().runtime.home / "agentd.lock")
        print(f"{job['title']}\tstate {_job_display_state(job, bool(daemon['running']))}\t{job['kind']}")
        print()
        print("Runs")
        for run in db.list_runs(job_id, limit=args.limit):
            error = f"\tERROR {run['error']}" if run.get("error") else ""
            print(f"{run['started_at']}\t{run['status']}\t{run['id']}\t{run.get('model') or ''}{error}")
        print()
        print("Steps")
        steps = db.list_steps(job_id=job_id)[-args.limit :]
        if not steps:
            print("No steps recorded.")
        for step in steps:
            if args.verbose:
                _print_step(step, verbose=True, chars=args.chars)
            else:
                tool = step.get("tool_name") or "-"
                summary = _one_line(_clean_step_summary(step.get("summary") or ""), args.chars)
                error = f"\tERROR {step['error']}" if step.get("error") else ""
                print(
                    f"#{step['step_no']}\t{step['started_at']}\t{step['status']}\t{step['kind']}\t{tool}\t{summary}{error}"
                )
        print()
        print("Artifacts")
        artifacts = db.list_artifacts(job_id, limit=args.limit)
        if not artifacts:
            print("No artifacts recorded.")
        for artifact in artifacts:
            print(
                f"{artifact['created_at']}\t{artifact['type']}\t{artifact.get('title') or artifact['id']}\t{artifact['path']}"
            )
    finally:
        db.close()


def cmd_activity(args: argparse.Namespace) -> None:
    db, _ = _db()
    seen_events: set[str] = set()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            print("No jobs found.")
            return
        job = db.get_job(job_id)
        daemon = daemon_lock_status(load_config().runtime.home / "agentd.lock")
        print(f"activity {job['title']} | state {_job_display_state(job, bool(daemon['running']))}")
        print("tool calls, artifacts, learning, and messages, oldest to newest")
        print(_rule("-"))

        def emit() -> None:
            events = db.list_timeline_events(job_id, limit=args.limit)
            printed = False
            for event in events:
                event_id = str(event.get("id") or "")
                if event_id in seen_events:
                    continue
                print(_event_line(event, chars=args.chars, full=args.verbose))
                if args.verbose:
                    _print_event_details(event, chars=args.chars)
                if args.paths and event.get("event_type") == "artifact":
                    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
                    if metadata.get("path"):
                        print(f"     path: {metadata['path']}")
                seen_events.add(event_id)
                printed = True
            if printed:
                print(_rule("-"))

        emit()
        while args.follow:
            time.sleep(args.interval)
            emit()
    except KeyboardInterrupt:
        print("\nactivity stopped")
    finally:
        db.close()


def cmd_updates(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            print("No jobs found.")
            return
        job = db.get_job(job_id)
        steps = db.list_steps(job_id=job_id)
        artifacts = db.list_artifacts(job_id, limit=args.limit)
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        operator_messages = (
            metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
        )
        agent_updates = metadata.get("agent_updates") if isinstance(metadata.get("agent_updates"), list) else []
        lessons = metadata.get("lessons") if isinstance(metadata.get("lessons"), list) else []
        daemon = daemon_lock_status(load_config().runtime.home / "agentd.lock")
        print(f"updates {job['title']} | state {_job_display_state(job, bool(daemon['running']))}")
        print(_rule("="))
        if operator_messages:
            latest = operator_messages[-1]
            print(f"last steering: {_one_line(latest.get('message') or '', args.chars)}")
        if agent_updates:
            print("latest agent notes:")
            for update in agent_updates[-min(args.limit, 5) :]:
                category = update.get("category") or "progress"
                print(f"  {category}: {_one_line(update.get('message') or '', args.chars)}")
        if lessons:
            print("latest lessons:")
            for lesson in lessons[-min(args.limit, 5) :]:
                if not isinstance(lesson, dict):
                    continue
                category = lesson.get("category") or "memory"
                print(f"  {category}: {_one_line(lesson.get('lesson') or '', args.chars)}")
        print("recent tool calls:")
        for step in steps[-min(args.limit, 8) :]:
            print(f"  {_step_line(step, chars=args.chars)}")
        print()
        print("latest findings/artifacts:")
        if not artifacts:
            print("  none yet")
        for artifact in artifacts:
            title = artifact.get("title") or artifact["id"]
            summary = f" - {_one_line(artifact['summary'], args.chars)}" if artifact.get("summary") else ""
            print(f"  {artifact['created_at']} {title}{summary}")
            print(f"    view: artifact {shlex.quote(title)}")
            if args.paths:
                print(f"    {artifact['path']}")
    finally:
        db.close()


def cmd_watch(args: argparse.Namespace) -> None:
    db, _ = _db()
    seen_runs: set[str] = set()
    seen_steps: set[str] = set()
    seen_artifacts: set[str] = set()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            print(f"No job matched: {_job_ref_text(args.job_id)}")
            return
        job = db.get_job(job_id)
        daemon = daemon_lock_status(load_config().runtime.home / "agentd.lock")
        print(f"watching {job['title']} | state {_job_display_state(job, bool(daemon['running']))} | {job['kind']}")
        print(f"objective: {job['objective']}")
        print(
            "Note: this shows model-visible state, tool calls, outputs, and errors. It does not expose hidden chain-of-thought."
        )
        print()

        def emit_snapshot(*, initial: bool = False) -> None:
            nonlocal job
            job = db.get_job(job_id)
            runs = list(reversed(db.list_runs(job_id, limit=args.limit)))
            steps = db.list_steps(job_id=job_id)[-args.limit :]
            artifacts = list(reversed(db.list_artifacts(job_id, limit=args.limit)))
            printed = False
            for run in runs:
                if run["id"] in seen_runs:
                    continue
                if not initial:
                    print()
                _print_run(run)
                seen_runs.add(run["id"])
                printed = True
            for step in steps:
                if step["id"] in seen_steps:
                    continue
                if not initial and not printed:
                    print()
                _print_step(step, verbose=args.verbose, chars=args.chars)
                seen_steps.add(step["id"])
                printed = True
            for artifact in artifacts:
                if artifact["id"] in seen_artifacts:
                    continue
                if not initial and not printed:
                    print()
                _print_artifact(artifact)
                seen_artifacts.add(artifact["id"])
                printed = True
            if printed:
                print(f"status: {job['status']}")

        emit_snapshot(initial=True)
        while args.follow:
            time.sleep(args.interval)
            emit_snapshot()
    except KeyboardInterrupt:
        print("\nwatch stopped")
    finally:
        db.close()


def cmd_run_one(args: argparse.Namespace) -> None:
    from nipux_cli.worker import run_one_step

    db, config = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            print(f"No job matched: {_job_ref_text(args.job_id)}")
            return
        _activate_job_if_planning(db, job_id)
        llm = None
        if args.fake:
            from nipux_cli.llm import LLMResponse, ScriptedLLM, ToolCall

            llm = ScriptedLLM(
                [
                    LLMResponse(
                        tool_calls=[
                            ToolCall(
                                name="write_artifact",
                                arguments={
                                    "title": "fake-step",
                                    "type": "text",
                                    "summary": "Fake one-step smoke artifact",
                                    "content": "This is a fake bounded worker step.",
                                },
                            )
                        ]
                    )
                ]
            )
        result = run_one_step(job_id, config=config, db=db, llm=llm)
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    finally:
        db.close()


def cmd_work(args: argparse.Namespace) -> None:
    from nipux_cli.worker import run_one_step

    db, config = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            print('No jobs found. Create one with: nipux create "objective"')
            return
        _activate_job_if_planning(db, job_id)
        job = db.get_job(job_id)
        print(f"working {job['title']} | state foreground | {job['kind']}")
        print(
            "Note: this shows model-visible state, tool calls, outputs, and errors. It does not expose hidden chain-of-thought."
        )
        print()
        for index in range(1, args.steps + 1):
            llm = None
            if args.fake:
                from nipux_cli.llm import LLMResponse, ScriptedLLM, ToolCall

                llm = ScriptedLLM(
                    [
                        LLMResponse(
                            tool_calls=[
                                ToolCall(
                                    name="write_artifact",
                                    arguments={
                                        "title": f"fake-work-step-{index}",
                                        "type": "text",
                                        "summary": "Fake foreground work step",
                                        "content": f"This is fake foreground work step {index}.",
                                    },
                                )
                            ]
                        )
                    ]
                )
            print(f"work step {index}/{args.steps}", flush=True)
            result = run_one_step(job_id, config=config, db=db, llm=llm)
            step = _step_by_id(db, job_id, result.step_id)
            if step:
                _print_step(step, verbose=args.verbose, chars=args.chars)
            else:
                print(json.dumps(result.__dict__, ensure_ascii=False, indent=2, default=_json_default))
            if args.dashboard:
                state = collect_dashboard_state(db, config, job_id=job_id, limit=args.limit)
                print()
                print(render_dashboard(state, width=_terminal_width(), chars=args.chars), end="")
            if result.status == "failed" and not args.continue_on_error:
                print("stopped after failed step; pass --continue-on-error to keep going")
                return
            if index < args.steps and args.poll_seconds > 0:
                time.sleep(args.poll_seconds)
    finally:
        db.close()


def cmd_run(args: argparse.Namespace) -> None:
    requested = _job_ref_text(args.job_id)
    if requested:
        db, _ = _db()
        try:
            job = _find_job(db, requested)
            if not job:
                print(f"No job matched: {requested}")
                return
            args.job_id = job["id"]
            _write_shell_state({"focus_job_id": job["id"]})
            _ensure_job_runnable(db, job["id"])
            job = db.get_job(job["id"])
            daemon = daemon_lock_status(load_config().runtime.home / "agentd.lock")
            print(f"focus set: {job['title']} | job {_job_display_state(job, bool(daemon['running']))}")
        finally:
            db.close()
    else:
        db, _ = _db()
        try:
            job_id = _default_job_id(db)
            if job_id:
                _ensure_job_runnable(db, job_id)
        finally:
            db.close()
    _start_daemon_if_needed(
        poll_seconds=args.poll_seconds,
        fake=args.fake,
        quiet=args.quiet,
        log_file=args.log_file,
    )
    if args.no_follow:
        return
    cmd_activity(
        argparse.Namespace(
            job_id=args.job_id,
            limit=args.limit,
            chars=args.chars,
            follow=True,
            interval=args.interval,
            verbose=args.verbose,
            paths=args.paths,
        )
    )


def cmd_digest(args: argparse.Namespace) -> None:
    db, _ = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            print(f"No job matched: {_job_ref_text(args.job_id)}")
            return
        print(render_job_digest(db, job_id), end="")
    finally:
        db.close()


def cmd_daily_digest(args: argparse.Namespace) -> None:
    db, config = _db()
    try:
        result = write_daily_digest(config, db, day=args.day)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()


def cmd_daemon(args: argparse.Namespace) -> None:
    config = load_config()
    if not _ensure_remote_model_ready_for_worker(config, fake=args.fake):
        raise SystemExit(2)
    daemon = Daemon.open(config=config)
    try:
        if args.once:
            result = daemon.run_once(fake=args.fake, verbose=args.verbose)
            print(json.dumps(result.__dict__ if result else None, ensure_ascii=False, indent=2))
            return
        daemon.run_forever(fake=args.fake, poll_seconds=args.poll_seconds, quiet=args.quiet, verbose=args.verbose)
    except DaemonAlreadyRunning as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        daemon.close()


def cmd_doctor(args: argparse.Namespace) -> None:
    checks = run_doctor(check_model=args.check_model)
    for check in checks:
        status = "ok" if check.ok else "fail"
        print(f"{status}\t{check.name}\t{check.detail}")
    if not all(check.ok for check in checks):
        raise SystemExit(1)


def _chat_handle_line(job_id: str, line: str, *, reply_fn=None) -> bool:
    line = line.strip()
    if not line:
        return True
    if line.startswith("chat "):
        db, _ = _db()
        try:
            job = db.get_job(job_id)
            print(f"already chatting with {job['title']}; type your message, /run, or /exit")
            return True
        finally:
            db.close()
    if line in {"/exit", "/quit", "exit", "quit"}:
        return False
    if line in {"/help", "help"}:
        print("Chat commands:")
        print("  /jobs /focus JOB_TITLE /switch JOB_TITLE /new OBJECTIVE /delete [JOB_TITLE]")
        print("  /history /events /activity /outputs /updates /status /health")
        print("  /artifacts /artifact QUERY /findings /tasks /roadmap /experiments /sources /memory /metrics /lessons")
        print("  /model MODEL /base-url URL /api-key KEY /api-key-env ENV /context TOKENS")
        print("  /timeout SECONDS /home PATH /step-limit SECONDS /output-chars CHARS /daily-digest BOOL /digest-time HH:MM /doctor")
        print("  /run /start /restart /work N /work-verbose N /stop /pause [note] /resume /cancel [note]")
        print("  /learn LESSON /note MESSAGE /follow MESSAGE /digest /clear /exit")
        print("Plain text gets a model reply and is saved as model-visible steering.")
        return True
    if line in {"clear", "/clear"}:
        print("\033[2J\033[H", end="")
        return True
    if line == "jobs" or line == "ls" or line.startswith("jobs "):
        cmd_jobs(argparse.Namespace())
        return True
    if line.startswith(("focus ", "switch ")):
        parts = shlex.split(line)
        cmd_focus(argparse.Namespace(query=parts[1:]))
        return True
    if line.startswith("/"):
        parts = shlex.split(line[1:])
        if not parts:
            return True
        command = parts[0]
        rest = parts[1:]
        if command in {"jobs", "ls"}:
            cmd_jobs(argparse.Namespace())
            return True
        if command == "history":
            cmd_history(
                argparse.Namespace(
                    job_id=job_id,
                    limit=int(rest[0]) if rest and rest[0].isdigit() else 40,
                    chars=220,
                    full=False,
                    json=False,
                )
            )
            return True
        if command == "events":
            cmd_events(
                argparse.Namespace(
                    job_id=job_id,
                    limit=int(rest[0]) if rest and rest[0].isdigit() else 40,
                    chars=220,
                    full=False,
                    json=False,
                    follow=False,
                    interval=2.0,
                )
            )
            return True
        if command == "outputs":
            cmd_logs(
                argparse.Namespace(
                    job_id=[job_id], limit=int(rest[0]) if rest and rest[0].isdigit() else 25, verbose=False, chars=260
                )
            )
            return True
        if command == "updates":
            cmd_updates(argparse.Namespace(job_id=job_id, limit=5, chars=180, paths=False))
            return True
        if command == "artifacts":
            cmd_artifacts(argparse.Namespace(job_id=job_id, limit=10, chars=220, paths=False))
            return True
        if command == "artifact":
            query = " ".join(rest).strip()
            if not query:
                print("usage: /artifact QUERY_OR_ID")
                return True
            cmd_artifact(argparse.Namespace(artifact_id_or_path=[query], job_id=job_id, chars=12000))
            return True
        if command == "lessons":
            cmd_lessons(argparse.Namespace(job_id=job_id, limit=10, chars=220))
            return True
        if command == "findings":
            cmd_findings(argparse.Namespace(job_id=job_id, limit=20, chars=220, json=False))
            return True
        if command == "tasks":
            cmd_tasks(argparse.Namespace(job_id=job_id, limit=20, chars=220, status=None, json=False))
            return True
        if command == "roadmap":
            cmd_roadmap(argparse.Namespace(job_id=job_id, limit=20, features=3, chars=220, json=False))
            return True
        if command == "experiments":
            cmd_experiments(argparse.Namespace(job_id=job_id, limit=20, chars=220, status=None, json=False))
            return True
        if command == "sources":
            cmd_sources(argparse.Namespace(job_id=job_id, limit=20, chars=220, json=False))
            return True
        if command == "memory":
            cmd_memory(argparse.Namespace(job_id=job_id, limit=10, chars=220))
            return True
        if command == "metrics":
            cmd_metrics(argparse.Namespace(job_id=job_id, chars=220))
            return True
        if command == "learn":
            lesson = " ".join(rest).strip()
            if not lesson:
                print("usage: /learn LESSON")
                return True
            db, _ = _db()
            try:
                entry = db.append_lesson(job_id, lesson, category="operator_preference", metadata={"source": "chat"})
                job = db.get_job(job_id)
                print(f"learned for {job['title']}: {_one_line(entry['lesson'], 220)}")
            finally:
                db.close()
            return True
        if command == "activity":
            cmd_activity(
                argparse.Namespace(
                    job_id=job_id, limit=20, chars=180, follow=False, interval=2.0, verbose=False, paths=False
                )
            )
            return True
        if command == "digest":
            cmd_digest(argparse.Namespace(job_id=[job_id]))
            return True
        if command == "status":
            cmd_status(argparse.Namespace(job_id=job_id, limit=8, chars=180, full=False, json=False))
            return True
        if _handle_chat_setting_command(command, rest):
            return True
        if command == "doctor":
            try:
                cmd_doctor(argparse.Namespace(check_model=False))
            except SystemExit:
                pass
            return True
        if command == "init":
            cmd_init(argparse.Namespace(path=None, force=False, model=None, base_url=None, api_key_env=None, openrouter=False))
            return True
        if command == "health":
            cmd_health(argparse.Namespace(limit=8, chars=180))
            return True
        if command == "start":
            cmd_start(argparse.Namespace(poll_seconds=0.0, fake=False, quiet=False, log_file=None))
            return True
        if command == "run":
            db, _ = _db()
            try:
                _ensure_job_runnable(db, job_id)
            finally:
                db.close()
            cmd_run(
                argparse.Namespace(
                    job_id=job_id,
                    poll_seconds=0.0,
                    interval=2.0,
                    limit=20,
                    chars=180,
                    verbose=False,
                    paths=False,
                    fake=False,
                    quiet=False,
                    log_file=None,
                    no_follow=True,
                )
            )
            return True
        if command == "restart":
            cmd_restart(argparse.Namespace(
                poll_seconds=0.0,
                wait=5.0,
                fake=False,
                quiet=False,
                log_file=None,
            ))
            return True
        if command in {"work", "work-verbose"}:
            steps = int(rest[0]) if rest and rest[0].isdigit() else 1
            cmd_work(
                argparse.Namespace(
                    job_id=job_id,
                    steps=steps,
                    poll_seconds=0.5,
                    fake=False,
                    verbose=command == "work-verbose",
                    dashboard=False,
                    limit=12,
                    chars=260 if command == "work" else 4000,
                    continue_on_error=False,
                )
            )
            return True
        if command in {"pause", "stop"}:
            cmd_pause(argparse.Namespace(job_id=job_id, note=rest))
            return True
        if command == "resume":
            cmd_resume(argparse.Namespace(job_id=job_id))
            return True
        if command == "cancel":
            cmd_cancel(argparse.Namespace(job_id=job_id, note=rest))
            return True
        if command == "note":
            message = " ".join(rest).strip()
            if not message:
                print("usage: /note MESSAGE")
                return True
            _queue_chat_note(job_id, message, mode="note")
            return True
        if command == "follow":
            message = " ".join(rest).strip()
            if not message:
                print("usage: /follow MESSAGE")
                return True
            _queue_chat_note(job_id, message, mode="follow_up")
            return True
        if command == "new":
            objective = " ".join(rest).strip()
            if not objective:
                print("usage: /new OBJECTIVE")
                return True
            created_id, title = _create_job(objective=objective, title=None, kind="generic", cadence=None)
            print(f"created {title}")
            print(f"focus set to {title}; answer the plan questions, then use /run when ready.")
            return True
        if command in {"focus", "switch"}:
            query = " ".join(rest).strip()
            if not query:
                cmd_focus(argparse.Namespace(query=[]))
                return True
            cmd_focus(argparse.Namespace(query=rest))
            return True
        if command == "delete":
            target = rest if rest else [job_id]
            cmd_delete(argparse.Namespace(job_id=target, keep_files=False))
            if not rest:
                return False
            return True
        print(f"unknown chat command: /{command}")
        return True
    if reply_fn is None:
        reply_fn = _reply_to_chat
    _handle_chat_message(job_id, line, reply_fn=reply_fn)
    return True


def _handle_chat_message(job_id: str, line: str, *, reply_fn=None, quiet: bool = False) -> tuple[bool, str]:
    if reply_fn is None:
        reply_fn = _reply_to_chat
    spawned = _maybe_spawn_job_from_chat(job_id, line, quiet=quiet)
    if spawned:
        return True, spawned
    controlled = _handle_chat_control_intent(job_id, line, quiet=quiet)
    if controlled is not None:
        return controlled
    _queue_chat_note(job_id, line, mode="steer", quiet=quiet)
    try:
        reply = reply_fn(job_id, line)
    except Exception as exc:
        detail = _friendly_error_text(f"{type(exc).__name__}: {exc}")
        message = f"{detail}; message saved for the worker"
        if not quiet:
            print(detail)
            print("Your message is still saved for the next worker step.")
        return True, message
    if reply.strip():
        db, _ = _db()
        try:
            db.append_agent_update(job_id, reply.strip(), category="chat")
        finally:
            db.close()
        if not quiet:
            print()
            print(reply.strip())
            print()
        return True, ""
    else:
        message = "model returned an empty reply; message is queued"
        if not quiet:
            print("model returned an empty reply; your message is still queued.")
        return True, message


def _handle_chat_control_intent(job_id: str, line: str, *, quiet: bool = False) -> tuple[bool, str] | None:
    command = _chat_control_command(line)
    if not command:
        return None
    keep_running, output = _capture_chat_command(job_id, command)
    compact = _compact_command_output(output)
    message = " | ".join(compact[-4:]) if compact else f"{command.lstrip('/')} done"
    if not quiet:
        print(message)
    return keep_running, message


def _chat_control_command(line: str) -> str:
    text = " ".join(line.strip().split())
    if not text:
        return ""
    lowered = text.lower().rstrip("?.!")
    natural = NATURAL_COMMANDS.get(lowered)
    if natural:
        return f"/{natural}"
    if lowered in {"jobs", "show jobs", "list jobs", "switch jobs", "change jobs"}:
        return "/jobs"
    if lowered in {"settings", "show settings"}:
        return "/model"
    if lowered in {"model settings", "change model", "edit settings"}:
        return "/model"
    if lowered in {
        "run",
        "start",
        "start working",
        "start work",
        "run this",
        "run this job",
        "start this job",
        "continue",
        "keep going",
        "keep working",
        "resume work",
    }:
        return "/run"
    if lowered in {"pause", "pause work", "pause this job", "stop", "stop work", "stop working", "stop this job"}:
        return "/pause"
    if lowered in {"resume", "resume this job", "reopen this job"}:
        return "/resume"
    if lowered in {"history", "show history", "timeline", "show timeline"}:
        return "/history"
    if lowered in {"artifacts", "outputs", "saved outputs", "show artifacts", "show outputs"}:
        return "/artifacts"
    if lowered in {"memory", "show memory", "learning", "show learning"}:
        return "/memory"
    return ""


def _maybe_spawn_job_from_chat(job_id: str, message: str, *, quiet: bool = False) -> str:
    objective = _extract_job_objective_from_message(message)
    if not objective:
        return ""
    created_id, title = _create_job(objective=objective, title=None, kind="generic", cadence=None)
    _write_shell_state({"focus_job_id": created_id})
    db, _ = _db()
    try:
        db.append_operator_message(created_id, message, source="chat", mode="steer")
        run_now = _message_requests_immediate_run(message)
        update = "Created this job from chat and drafted its initial plan."
        if run_now:
            update += " Starting the daemon so it can begin work."
        else:
            update += " Use the right-side controls to run it."
        db.append_agent_update(created_id, update, category="chat")
        db.append_agent_update(
            job_id,
            f"Created job '{title}' from your chat request and switched focus to it.",
            category="chat",
        )
    finally:
        db.close()
    run_now = _message_requests_immediate_run(message)
    text = f"Created job: {title}. Focus switched to it."
    if run_now:
        _start_daemon_if_needed(poll_seconds=0.0, quiet=True)
        text += " Started worker."
    if not quiet:
        print(text)
    return text


def _message_requests_immediate_run(message: str) -> bool:
    lowered = " ".join(message.strip().lower().split())
    return bool(re.match(r"^(?:please\s+)?(?:start|launch|run|spin\s+off)\b", lowered))


def _extract_job_objective_from_message(message: str) -> str:
    text = " ".join(message.strip().split())
    if not text:
        return ""
    lowered = text.lower()
    patterns = [
        r"^(?:please\s+)?(?:create|start|spin\s+off|make|launch)\s+(?:a\s+)?(?:new\s+)?job\s+(?:to|for|that|which)?\s*(.+)$",
        r"^(?:please\s+)?(?:send|queue)\s+(?:off\s+)?(?:a\s+)?(?:new\s+)?job\s+(?:to|for|that|which)?\s*(.+)$",
        r"^(?:please\s+)?(?:new|job)\s+(.+)$",
        r"^(?:please\s+)?(?:can\s+you|could\s+you|i\s+need\s+you\s+to|i\s+want\s+you\s+to)\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            objective = match.group(1).strip(" .")
            return objective if _looks_like_job_objective(objective) else ""
    if _looks_like_job_objective(text) and not _looks_like_smalltalk(lowered):
        return text
    return ""


def _looks_like_smalltalk(lowered: str) -> bool:
    return lowered in {"hi", "hello", "hey", "yo", "sup", "thanks", "thank you"} or lowered.endswith("?")


def _looks_like_job_objective(text: str) -> bool:
    lowered = text.lower()
    if len(text.split()) < 3:
        return False
    action_words = {
        "research",
        "monitor",
        "optimize",
        "build",
        "find",
        "test",
        "deploy",
        "fix",
        "write",
        "analyze",
        "track",
        "benchmark",
        "scrape",
        "watch",
        "automate",
        "summarize",
        "compare",
        "investigate",
        "improve",
    }
    return any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in action_words)


def _queue_chat_note(job_id: str, message: str, *, mode: str = "steer", quiet: bool = False) -> None:
    db, _ = _db()
    try:
        entry = db.append_operator_message(job_id, message, source="chat", mode=mode)
        if not quiet:
            if entry.get("mode") == "follow_up":
                print(f"waiting after current branch: {entry['message']}")
            else:
                print(f"waiting: {entry['message']}")
    finally:
        db.close()


def _reply_to_chat(job_id: str, message: str) -> str:
    from nipux_cli.llm import OpenAIChatLLM

    db, config = _db()
    try:
        job = db.get_job(job_id)
        messages = _build_chat_messages(db, job, message)
    finally:
        db.close()
    return OpenAIChatLLM(config.model).complete(messages=messages)


def _build_chat_messages(db: AgentDB, job: dict[str, Any], message: str) -> list[dict[str, str]]:
    steps = db.list_steps(job_id=job["id"])[-10:]
    artifacts = db.list_artifacts(job["id"], limit=5)
    timeline_events = db.list_timeline_events(job["id"], limit=18)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    operator_messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    agent_updates = metadata.get("agent_updates") if isinstance(metadata.get("agent_updates"), list) else []
    lessons = metadata.get("lessons") if isinstance(metadata.get("lessons"), list) else []
    findings = metadata.get("finding_ledger") if isinstance(metadata.get("finding_ledger"), list) else []
    sources = metadata.get("source_ledger") if isinstance(metadata.get("source_ledger"), list) else []
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    experiments = metadata.get("experiment_ledger") if isinstance(metadata.get("experiment_ledger"), list) else []
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    step_lines = "\n".join(
        f"- #{step['step_no']} {step['status']} {step.get('tool_name') or step['kind']}: {_clean_step_summary(step.get('summary') or step.get('error') or '')}"
        for step in steps
    )
    artifact_lines = "\n".join(
        f"- #{index} {artifact.get('title') or artifact['id']}: {artifact.get('summary') or ''} "
        f"(view with /artifact {index})"
        for index, artifact in enumerate(artifacts, start=1)
    )
    steering_lines = "\n".join(
        f"- {entry.get('source', 'operator')} {entry.get('mode', 'steer')}: {entry.get('message', '')}"
        for entry in active_prompt_operator_entries(operator_messages)[-6:]
        if isinstance(entry, dict)
    )
    update_lines = "\n".join(
        f"- {entry.get('category', 'progress')}: {entry.get('message', '')}"
        for entry in agent_updates[-5:]
        if isinstance(entry, dict)
    )
    lesson_lines = "\n".join(
        f"- {entry.get('category', 'memory')}: {entry.get('lesson', '')}"
        for entry in lessons[-8:]
        if isinstance(entry, dict)
    )
    finding_lines = "\n".join(
        f"- {entry.get('name')}: {entry.get('category') or ''} {entry.get('location') or ''} score={entry.get('score')}"
        for entry in findings[-8:]
        if isinstance(entry, dict)
    )
    task_lines = "\n".join(
        f"- {entry.get('status') or 'open'} p={entry.get('priority') or 0}: {entry.get('title')}"
        for entry in tasks[-10:]
        if isinstance(entry, dict)
    )
    milestone_lines = ""
    if roadmap:
        milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
        milestone_lines = "\n".join(
            (
                f"- {entry.get('status') or 'planned'} validation={entry.get('validation_status') or 'not_started'} "
                f"p={entry.get('priority') or 0}: {entry.get('title')}"
            )
            for entry in milestones[-8:]
            if isinstance(entry, dict)
        )
        roadmap_header = (
            f"{roadmap.get('status') or 'planned'}: {roadmap.get('title') or 'Roadmap'}"
            + (f" current={roadmap.get('current_milestone')}" if roadmap.get("current_milestone") else "")
        )
        milestone_lines = f"{roadmap_header}\n{milestone_lines}".strip()
    experiment_lines = "\n".join(
        (
            f"- {entry.get('status') or 'planned'}: {entry.get('title')}"
            f" {entry.get('metric_name') or 'metric'}={entry.get('metric_value')}{entry.get('metric_unit') or ''}"
            f"{' best' if entry.get('best_observed') else ''}"
        )
        if entry.get("metric_value") is not None
        else f"- {entry.get('status') or 'planned'}: {entry.get('title')}"
        for entry in experiments[-10:]
        if isinstance(entry, dict)
    )
    source_lines = "\n".join(
        f"- {entry.get('source')}: score={entry.get('usefulness_score')} findings={entry.get('yield_count') or 0} outcome={entry.get('last_outcome') or ''}"
        for entry in sources[-8:]
        if isinstance(entry, dict)
    )
    timeline_lines = "\n".join(_event_line(event, chars=700, full=False) for event in timeline_events[-12:])
    return [
        {
            "role": "system",
            "content": (
                "You are Nipux, the chat model that controls a generic long-running agent workspace. "
                "You know the visible CLI state, focused job, job list, task queue, artifacts, memory, metrics, and recent activity. "
                "Answer directly from the visible job state. Do not claim hidden chain-of-thought. "
                "If the operator asks for work to be done, explain the concrete job/control action Nipux will take or how to run it from the Jobs/Status panel. "
                "If the operator asks where saved work is, explain that artifacts and history are visible from the Jobs/Status panel or direct CLI commands. "
                "Do not start replies with an introduction. Keep replies concise and useful."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Job title: {job['title']}\n"
                f"Job status: {job['status']}\n"
                f"Kind: {job['kind']}\n"
                f"Objective: {job['objective']}\n\n"
                f"Recent tool calls:\n{step_lines or 'None yet.'}\n\n"
                f"Latest artifacts:\n{artifact_lines or 'None yet.'}\n\n"
                f"Finding ledger:\n{finding_lines or 'None yet.'}\n\n"
                f"Task queue:\n{task_lines or 'None yet.'}\n\n"
                f"Roadmap:\n{milestone_lines or 'None yet.'}\n\n"
                f"Experiment ledger:\n{experiment_lines or 'None yet.'}\n\n"
                f"Source ledger:\n{source_lines or 'None yet.'}\n\n"
                f"Lessons learned:\n{lesson_lines or 'None yet.'}\n\n"
                f"Recent operator steering:\n{steering_lines or 'None.'}\n\n"
                f"Recent agent notes:\n{update_lines or 'None.'}\n\n"
                f"Recent visible timeline:\n{timeline_lines or 'None.'}\n\n"
                f"Operator message:\n{message}"
            ),
        },
    ]


def cmd_shell(args: argparse.Namespace) -> None:
    _install_readline_history()
    _print_shell_header()
    print()
    if args.status:
        _print_shell_status(limit=args.limit, chars=args.chars)
    while True:
        try:
            line = input(_shell_prompt())
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print()
            continue
        if not _run_shell_line(line):
            return


def _print_shell_header() -> None:
    print(NIPUX_BANNER)
    print(_rule("="))
    print(_shell_summary())
    print("Type 'chat' to talk, 'history' or 'artifacts' to inspect output, or plain text to steer.")
    print("Trace output is observable state and tool I/O, not hidden chain-of-thought.")
    print(_rule("="))


def _shell_summary() -> str:
    db, config = _db()
    try:
        daemon = daemon_lock_status(config.runtime.home / "agentd.lock")
        job_id = _default_job_id(db)
        if not job_id:
            focus = "no jobs"
        else:
            job = db.get_job(job_id)
            state = _job_display_state(job, bool(daemon["running"]))
            focus = f"{job['title']} [job {state} | worker {_worker_label(job, bool(daemon['running']))}]"
        daemon_text = "running" if daemon["running"] else "stopped"
        return f"daemon: {daemon_text} | model: {config.model.model} | focus: {focus}"
    finally:
        db.close()


def _shell_prompt() -> str:
    db, _ = _db()
    try:
        job_id = _default_job_id(db)
        if not job_id:
            return "nipux> "
        job = db.get_job(job_id)
        title = str(job.get("title") or job_id).strip()[:22]
        daemon = daemon_lock_status(load_config().runtime.home / "agentd.lock")
        worker = _worker_label(job, bool(daemon["running"]))
        return f"nipux[{title}:{worker}]> "
    except Exception:
        return "nipux> "
    finally:
        db.close()


def _install_readline_history() -> None:
    try:
        import atexit
        import readline
    except ImportError:
        return
    config = load_config()
    config.ensure_dirs()
    history_path = config.runtime.home / "shell_history"
    try:
        readline.read_history_file(history_path)
    except OSError:
        pass
    atexit.register(readline.write_history_file, history_path)


def _print_shell_status(*, limit: int, chars: int) -> None:
    db, config = _db()
    try:
        state = collect_dashboard_state(db, config, limit=limit)
        print(render_dashboard(state, width=_terminal_width(), chars=chars), end="")
        print()
    finally:
        db.close()


def _print_shell_help() -> None:
    print(NIPUX_BANNER)
    print(_rule("="))
    print("Jobs")
    for command in (
        'create "objective" --title TITLE',
        "ls",
        "focus [JOB_TITLE]",
        "rename JOB_TITLE --title NEW_TITLE",
        "delete JOB_TITLE",
        "chat [JOB_TITLE]",
        "steer [--job JOB_TITLE] MESSAGE",
        "pause [JOB_TITLE] [note...]",
        "resume [JOB_TITLE]",
        "cancel [JOB_TITLE] [note...]",
    ):
        print(f"  {command}")
    print()
    print("Inspect")
    for command in (
        "status [JOB_TITLE]",
        "health",
        "history [JOB_TITLE]",
        "events [JOB_TITLE] [--follow] [--json]",
        "activity [JOB_TITLE] [--follow]",
        "updates [JOB_TITLE]",
        "outputs [JOB_TITLE] --verbose",
        "findings [JOB_TITLE]",
        "tasks [JOB_TITLE]",
        "roadmap [JOB_TITLE]",
        "experiments [JOB_TITLE]",
        "sources [JOB_TITLE]",
        "memory [JOB_TITLE]",
        "metrics [JOB_TITLE]",
        "artifacts [JOB_TITLE]",
        "artifact QUERY_OR_TITLE",
        "lessons [JOB_TITLE]",
    ):
        print(f"  {command}")
    print()
    print("Worker")
    for command in (
        "work [JOB_TITLE] --steps N [--verbose]",
        "run [JOB_TITLE] --poll-seconds N",
        "start --poll-seconds N",
        "restart --poll-seconds N",
        "stop  # daemon",
        "stop [JOB_TITLE]  # pause job",
    ):
        print(f"  {command}")
    print()
    print("System")
    for command in (
        "learn [--job JOB_TITLE] LESSON",
        "digest JOB_TITLE",
        "daily-digest",
        "service install|status|uninstall",
        "autostart install|status|uninstall",
        "dashboard [JOB_TITLE] --no-follow",
        "doctor --check-model",
        "browser-dashboard --port 4848",
        "help",
        "exit",
    ):
        print(f"  {command}")


def _run_shell_line(line: str) -> bool:
    line = line.strip()
    if not line:
        return True
    if line in {"exit", "quit", ":q"}:
        return False
    if line in {"help", "?", "commands"}:
        _print_shell_help()
        return True
    if line == "clear":
        print("\033[2J\033[H", end="")
        return True
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        print(f"parse error: {exc}")
        return True
    if tokens and tokens[0] == "nipux":
        tokens = tokens[1:]
    if not tokens:
        return True
    natural = NATURAL_COMMANDS.get(" ".join(tokens).lower())
    if natural:
        tokens = [natural]
    if tokens[0] == "ls":
        tokens[0] = "jobs"
    if tokens[0] == "focus" and len(tokens) > 1 and tokens[1].lower() in {"on", "more", "only"}:
        _steer_default_job(line)
        return True
    if tokens[0] not in SHELL_COMMAND_NAMES and tokens[0] not in SHELL_BUILTINS:
        _steer_default_job(line)
        return True
    try:
        parser = build_parser()
        parsed = parser.parse_args(tokens)
        if parsed.func is cmd_shell:
            print("already in nipux shell")
            return True
        parsed.func(parsed)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code:
            print(f"command exited with status {code}")
    return True


def _steer_default_job(message: str) -> None:
    db, _ = _db()
    try:
        job_id = _default_job_id(db)
        if not job_id:
            print('No focused job. Create one first, or run: create "objective"')
            return
        job = db.get_job(job_id)
        entry = db.append_operator_message(job_id, message, source="shell")
        print(f"waiting for {job['title']}: {entry['message']}")
        print("Waiting for the next worker step.")
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nipux")
    parser.add_argument("--version", action="version", version=f"nipux {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--path")
    init.add_argument("--force", action="store_true")
    init.add_argument("--openrouter", action="store_true", help="Write an OpenRouter config that reads OPENROUTER_API_KEY")
    init.add_argument("--model", help="Model name to write into config.yaml")
    init.add_argument("--base-url", help="OpenAI-compatible API base URL")
    init.add_argument("--api-key-env", help="Environment variable that stores the API key")
    init.add_argument("--context-length", type=int, default=DEFAULT_CONTEXT_LENGTH)
    init.set_defaults(func=cmd_init)

    create = sub.add_parser("create")
    create.add_argument("objective")
    create.add_argument("--title")
    create.add_argument("--kind", default="generic")
    create.add_argument("--cadence")
    create.set_defaults(func=cmd_create)

    jobs = sub.add_parser("jobs")
    jobs.set_defaults(func=cmd_jobs)

    ls_cmd = sub.add_parser("ls")
    ls_cmd.set_defaults(func=cmd_jobs)

    focus = sub.add_parser("focus")
    focus.add_argument("query", nargs="*")
    focus.set_defaults(func=cmd_focus)

    rename = sub.add_parser("rename")
    rename.add_argument("job_id", nargs="*")
    rename.add_argument("--title", nargs="+", required=True)
    rename.set_defaults(func=cmd_rename)

    delete = sub.add_parser("delete", aliases=["rm"])
    delete.add_argument("job_id", nargs="*")
    delete.add_argument("--keep-files", action="store_true")
    delete.set_defaults(func=cmd_delete)

    chat = sub.add_parser("chat")
    chat.add_argument("job_id", nargs="*")
    chat.add_argument("--history-limit", type=int, default=12)
    chat.add_argument("--no-history", action="store_true")
    chat.set_defaults(func=cmd_chat)

    shell = sub.add_parser("shell")
    shell.add_argument("--status", action="store_true", help="Render the full dashboard when the shell opens")
    shell.add_argument("--no-status", action="store_true", help=argparse.SUPPRESS)
    shell.add_argument("--limit", type=int, default=8)
    shell.add_argument("--chars", type=int, default=180)
    shell.set_defaults(func=cmd_shell)

    steer = sub.add_parser("steer", aliases=["say"])
    steer.add_argument("--job", dest="job_id")
    steer.add_argument("message", nargs="+")
    steer.set_defaults(func=cmd_steer)

    pause = sub.add_parser("pause")
    pause.add_argument("parts", nargs="*", help="Optional job title/id followed by an optional note")
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser("resume")
    resume.add_argument("job_id", nargs="*")
    resume.set_defaults(func=cmd_resume)

    cancel = sub.add_parser("cancel")
    cancel.add_argument("parts", nargs="*", help="Optional job title/id followed by an optional note")
    cancel.set_defaults(func=cmd_cancel)

    status = sub.add_parser("status")
    status.add_argument("job_id", nargs="*")
    status.add_argument("--limit", type=int, default=8)
    status.add_argument("--chars", type=int, default=180)
    status.add_argument("--full", action="store_true", help="Render the full dashboard")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    health = sub.add_parser("health")
    health.add_argument("--limit", type=int, default=8)
    health.add_argument("--chars", type=int, default=180)
    health.set_defaults(func=cmd_health)

    history = sub.add_parser("history")
    history.add_argument("job_id", nargs="*")
    history.add_argument("--limit", type=int, default=80)
    history.add_argument("--chars", type=int, default=260)
    history.add_argument("--full", action="store_true")
    history.add_argument("--json", action="store_true")
    history.set_defaults(func=cmd_history)

    events = sub.add_parser("events")
    events.add_argument("job_id", nargs="*")
    events.add_argument("--limit", type=int, default=80)
    events.add_argument("--chars", type=int, default=260)
    events.add_argument("--full", action="store_true")
    events.add_argument("--follow", action="store_true")
    events.add_argument("--interval", type=float, default=2.0)
    events.add_argument("--json", action="store_true")
    events.set_defaults(func=cmd_events)

    dashboard = sub.add_parser("dashboard", aliases=["dash"])
    dashboard.add_argument("job_id", nargs="*")
    dashboard.add_argument("--interval", type=float, default=2.0)
    dashboard.add_argument("--limit", type=int, default=12)
    dashboard.add_argument("--chars", type=int, default=260)
    dashboard.add_argument("--no-follow", dest="follow", action="store_false")
    dashboard.add_argument("--no-clear", dest="clear", action="store_false")
    dashboard.set_defaults(func=cmd_dashboard, follow=True, clear=True)

    start = sub.add_parser("start")
    start.add_argument("--poll-seconds", type=float, default=0.0)
    start.add_argument("--fake", action="store_true", help="Use deterministic fake model responses")
    start.add_argument("--quiet", action="store_true", help="Write fewer daemon log lines")
    start.add_argument("--log-file")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop")
    stop.add_argument("job_id", nargs="*", help="Optional job title/id to pause instead of stopping the daemon")
    stop.add_argument("--wait", type=float, default=5.0)
    stop.set_defaults(func=cmd_stop)

    restart = sub.add_parser("restart")
    restart.add_argument("--poll-seconds", type=float, default=0.0)
    restart.add_argument("--wait", type=float, default=5.0)
    restart.add_argument("--fake", action="store_true", help="Use deterministic fake model responses")
    restart.add_argument("--quiet", action="store_true", help="Write fewer daemon log lines")
    restart.add_argument("--log-file")
    restart.set_defaults(func=cmd_restart)

    browser_dashboard = sub.add_parser("browser-dashboard")
    browser_dashboard.add_argument("--port", type=int, default=4848)
    browser_dashboard.add_argument("--foreground", action="store_true")
    browser_dashboard.add_argument("--stop", action="store_true")
    browser_dashboard.add_argument("--log-file")
    browser_dashboard.set_defaults(func=cmd_browser_dashboard)

    autostart = sub.add_parser("autostart")
    autostart.add_argument("action", choices=["install", "status", "uninstall"])
    autostart.add_argument("--poll-seconds", type=float, default=5.0)
    autostart.add_argument("--quiet", action="store_true")
    autostart.set_defaults(func=cmd_autostart)

    service = sub.add_parser("service")
    service.add_argument("action", choices=["install", "status", "uninstall"])
    service.add_argument("--poll-seconds", type=float, default=0.0)
    service.add_argument("--quiet", action="store_true")
    service.set_defaults(func=cmd_service)

    artifacts = sub.add_parser("artifacts")
    artifacts.add_argument("job_id", nargs="*")
    artifacts.add_argument("--limit", type=int, default=25)
    artifacts.add_argument("--chars", type=int, default=220)
    artifacts.add_argument("--paths", action="store_true", help="Show full artifact paths")
    artifacts.set_defaults(func=cmd_artifacts)

    artifact = sub.add_parser("artifact")
    artifact.add_argument("artifact_id_or_path", nargs="+")
    artifact.add_argument("--job", dest="job_id")
    artifact.add_argument("--chars", type=int, default=12000)
    artifact.set_defaults(func=cmd_artifact)

    lessons = sub.add_parser("lessons")
    lessons.add_argument("job_id", nargs="*")
    lessons.add_argument("--limit", type=int, default=25)
    lessons.add_argument("--chars", type=int, default=220)
    lessons.set_defaults(func=cmd_lessons)

    learn = sub.add_parser("learn")
    learn.add_argument("--job", dest="job_id")
    learn.add_argument("--category", default="operator_preference")
    learn.add_argument("--chars", type=int, default=220)
    learn.add_argument("lesson", nargs="+")
    learn.set_defaults(func=cmd_learn)

    findings = sub.add_parser("findings")
    findings.add_argument("job_id", nargs="*")
    findings.add_argument("--limit", type=int, default=25)
    findings.add_argument("--chars", type=int, default=220)
    findings.add_argument("--json", action="store_true")
    findings.set_defaults(func=cmd_findings)

    tasks = sub.add_parser("tasks")
    tasks.add_argument("job_id", nargs="*")
    tasks.add_argument("--limit", type=int, default=25)
    tasks.add_argument("--chars", type=int, default=220)
    tasks.add_argument("--status", nargs="+")
    tasks.add_argument("--json", action="store_true")
    tasks.set_defaults(func=cmd_tasks)

    roadmap = sub.add_parser("roadmap")
    roadmap.add_argument("job_id", nargs="*")
    roadmap.add_argument("--limit", type=int, default=25)
    roadmap.add_argument("--features", type=int, default=3)
    roadmap.add_argument("--chars", type=int, default=220)
    roadmap.add_argument("--json", action="store_true")
    roadmap.set_defaults(func=cmd_roadmap)

    experiments = sub.add_parser("experiments")
    experiments.add_argument("job_id", nargs="*")
    experiments.add_argument("--limit", type=int, default=25)
    experiments.add_argument("--chars", type=int, default=220)
    experiments.add_argument("--status", nargs="+")
    experiments.add_argument("--json", action="store_true")
    experiments.set_defaults(func=cmd_experiments)

    sources = sub.add_parser("sources")
    sources.add_argument("job_id", nargs="*")
    sources.add_argument("--limit", type=int, default=25)
    sources.add_argument("--chars", type=int, default=220)
    sources.add_argument("--json", action="store_true")
    sources.set_defaults(func=cmd_sources)

    memory = sub.add_parser("memory")
    memory.add_argument("job_id", nargs="*")
    memory.add_argument("--limit", type=int, default=10)
    memory.add_argument("--chars", type=int, default=260)
    memory.set_defaults(func=cmd_memory)

    metrics = sub.add_parser("metrics")
    metrics.add_argument("job_id", nargs="*")
    metrics.add_argument("--chars", type=int, default=220)
    metrics.set_defaults(func=cmd_metrics)

    logs = sub.add_parser("logs", aliases=["outputs", "output"])
    logs.add_argument("job_id", nargs="*")
    logs.add_argument("--limit", type=int, default=25)
    logs.add_argument("--verbose", action="store_true")
    logs.add_argument("--chars", type=int, default=4000)
    logs.set_defaults(func=cmd_logs)

    activity = sub.add_parser("activity", aliases=["feed", "tail"])
    activity.add_argument("job_id", nargs="*")
    activity.add_argument("--limit", type=int, default=20)
    activity.add_argument("--chars", type=int, default=180)
    activity.add_argument("--follow", action="store_true")
    activity.add_argument("--interval", type=float, default=2.0)
    activity.add_argument("--verbose", action="store_true")
    activity.add_argument("--paths", action="store_true", help="Show full artifact paths")
    activity.set_defaults(func=cmd_activity)

    updates = sub.add_parser("updates", aliases=["update"])
    updates.add_argument("job_id", nargs="*")
    updates.add_argument("--limit", type=int, default=5)
    updates.add_argument("--chars", type=int, default=180)
    updates.add_argument("--paths", action="store_true", help="Show full artifact paths")
    updates.set_defaults(func=cmd_updates)

    watch = sub.add_parser("watch")
    watch.add_argument("job_id", nargs="+")
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument("--limit", type=int, default=20)
    watch.add_argument("--verbose", action="store_true")
    watch.add_argument("--chars", type=int, default=4000)
    watch.add_argument("--no-follow", dest="follow", action="store_false")
    watch.set_defaults(func=cmd_watch, follow=True)

    run_one = sub.add_parser("run-one")
    run_one.add_argument("job_id", nargs="+")
    run_one.add_argument("--fake", action="store_true", help="Use a deterministic fake model response")
    run_one.set_defaults(func=cmd_run_one)

    work = sub.add_parser("work")
    work.add_argument("job_id", nargs="*")
    work.add_argument("--steps", type=int, default=5)
    work.add_argument("--poll-seconds", type=float, default=0.5)
    work.add_argument("--fake", action="store_true", help="Use deterministic fake model responses")
    work.add_argument("--verbose", action="store_true", help="Print step inputs and outputs")
    work.add_argument("--dashboard", action="store_true", help="Render a dashboard snapshot after each step")
    work.add_argument("--limit", type=int, default=12)
    work.add_argument("--chars", type=int, default=4000)
    work.add_argument("--continue-on-error", action="store_true")
    work.set_defaults(func=cmd_work)

    run = sub.add_parser("run")
    run.add_argument("job_id", nargs="*")
    run.add_argument("--poll-seconds", type=float, default=0.0)
    run.add_argument("--interval", type=float, default=2.0)
    run.add_argument("--limit", type=int, default=20)
    run.add_argument("--chars", type=int, default=180)
    run.add_argument("--verbose", action="store_true")
    run.add_argument("--paths", action="store_true")
    run.add_argument("--fake", action="store_true", help="Use deterministic fake model responses")
    run.add_argument("--quiet", action="store_true", help="Write fewer daemon log lines")
    run.add_argument("--log-file")
    run.add_argument("--no-follow", action="store_true", help="Start daemon and return without tailing activity")
    run.set_defaults(func=cmd_run)

    digest = sub.add_parser("digest")
    digest.add_argument("job_id", nargs="+")
    digest.set_defaults(func=cmd_digest)

    daily_digest = sub.add_parser("daily-digest")
    daily_digest.add_argument("--day", help="YYYY-MM-DD. Defaults to today.")
    daily_digest.set_defaults(func=cmd_daily_digest)

    daemon = sub.add_parser("daemon")
    daemon.add_argument("--once", action="store_true", help="Run at most one job step and exit")
    daemon.add_argument("--fake", action="store_true", help="Use deterministic fake model responses")
    daemon.add_argument("--poll-seconds", type=float, default=0.0)
    daemon.add_argument("--quiet", action="store_true", help="Do not print foreground progress lines")
    daemon.add_argument("--verbose", action="store_true", help="Print model-visible job state and step results")
    daemon.set_defaults(func=cmd_daemon)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--check-model", action="store_true", help="Also call the local model /models endpoint")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        cmd_home(argparse.Namespace(history_limit=12))
        return
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
