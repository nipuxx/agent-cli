"""Thin CLI for the Nipux agent runtime."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from nipux_cli import __version__
from nipux_cli.artifacts import ArtifactStore
from nipux_cli.chat_intent import (
    chat_control_command,
    extract_job_objective_from_message as _extract_job_objective_from_message,
    natural_command_for,
)
from nipux_cli.cli_state import (
    configured_focus_job_id as _configured_focus_job_id,
    default_job_id as _default_job_id,
    find_job as _find_job,
    clear_model_setup_verified as _clear_model_setup_verified,
    mark_model_setup_verified as _mark_model_setup_verified,
    model_setup_verified as _model_setup_verified,
    read_shell_state as _read_shell_state,
    write_shell_state as _write_shell_state,
)
from nipux_cli.cli_render import (
    daemon_event_line as _daemon_event_line,
    daemon_state_line as _daemon_state_line,
    important_startup_events as _important_startup_events,
    job_ref_text as _job_ref_text,
    json_default as _json_default,
    next_operator_action as _next_operator_action,
    note_text as _note_text,
    print_artifact as _print_artifact,
    print_event_card as _print_event_card,
    print_event_details as _print_event_details,
    print_jobs_panel as _print_jobs_panel,
    print_metric_grid as _print_metric_grid,
    print_run as _print_run,
    print_step as _print_step,
    print_wrapped as _print_wrapped,
    public_event as _public_event,
    rule as _rule,
    section_title as _section_title,
    short_path as _short_path,
    step_line as _step_line,
    terminal_width as _terminal_width,
)
from nipux_cli.chat_context import build_chat_messages as _build_chat_messages
from nipux_cli.chat_commands import ChatCommandDeps, handle_chat_slash_command as _handle_chat_slash_command
from nipux_cli.chat_controller import (
    ChatControllerDeps,
    chat_reply_text_and_metadata as _controller_reply_text_and_metadata,
    handle_chat_control_intent as _controller_handle_chat_control_intent,
    handle_chat_message as _controller_handle_chat_message,
    maybe_spawn_job_from_chat as _controller_maybe_spawn_job_from_chat,
    queue_chat_note as _controller_queue_chat_note,
)
from nipux_cli.chat_frame_runtime import (
    ChatFrameDeps,
    compact_command_output as _compact_command_output,
    emit_frame_if_changed as _emit_frame_if_changed,
    run_chat_frame as _run_chat_frame,
)
from nipux_cli.chat_tui import build_chat_frame as _build_chat_tui_frame
from nipux_cli.cli_help import NIPUX_BANNER, print_shell_help as _render_shell_help
from nipux_cli.config import (
    DEFAULT_BASE_URL,
    DEFAULT_API_KEY_ENV,
    DEFAULT_CONTEXT_LENGTH,
    DEFAULT_MODEL,
    DEFAULT_OPENROUTER_API_KEY_ENV,
    DEFAULT_OPENROUTER_MODEL,
    default_config_yaml,
    load_config,
    write_private_text,
)
from nipux_cli.daemon_control import cmd_restart_impl as _cmd_restart_impl
from nipux_cli.daemon_control import cmd_start_impl as _cmd_start_impl
from nipux_cli.daemon_control import ensure_remote_model_ready_for_worker as _daemon_ensure_remote_model_ready
from nipux_cli.daemon_control import remote_model_preflight_failures as _daemon_remote_model_preflight_failures
from nipux_cli.daemon_control import start_daemon_if_needed_impl as _start_daemon_if_needed_impl
from nipux_cli.daemon_control import stop_daemon_process_impl as _stop_daemon_process_impl
from nipux_cli.daemon import Daemon, DaemonAlreadyRunning, daemon_lock_status, read_daemon_events
from nipux_cli.dashboard import collect_dashboard_state, render_dashboard, render_overview
from nipux_cli.db import AgentDB
from nipux_cli.digest import render_job_digest, write_daily_digest
from nipux_cli.doctor import run_doctor
from nipux_cli.first_run_tui import (
    build_first_run_frame as _build_first_run_tui_frame,
    first_run_actions as _first_run_tui_actions,
    first_run_columns as _first_run_columns,
)
from nipux_cli.first_run_controller import (
    FirstRunFrameDeps,
    capture_first_run_command as _controller_capture_first_run_command,
    create_first_run_job as _controller_create_first_run_job,
    first_run_chat_reply as _controller_first_run_chat_reply,
    first_token as _controller_first_token,
    handle_first_run_action as _controller_handle_first_run_action,
    handle_first_run_frame_line as _controller_handle_first_run_frame_line,
)
from nipux_cli.first_run_frame_runtime import (
    FirstRunRuntimeDeps,
    clamp_selection as _clamp_first_run_runtime_selection,
    run_first_run_frame as _run_first_run_frame,
)
from nipux_cli.event_render import event_line as _event_line
from nipux_cli.frame_snapshot import load_frame_snapshot
from nipux_cli.parser_builder import build_arg_parser
from nipux_cli.planning import (
    format_initial_plan,
    initial_plan_for_objective,
    initial_roadmap_for_objective,
    initial_task_contract,
)
from nipux_cli.scheduling import job_provider_blocked, provider_retry_metadata
from nipux_cli.record_commands import (
    RecordCommandDeps,
    cmd_experiments_impl,
    cmd_findings_impl,
    cmd_memory_impl,
    cmd_metrics_impl,
    cmd_roadmap_impl,
    cmd_sources_impl,
    cmd_tasks_impl,
    cmd_usage_impl,
)
from nipux_cli.service_install import cmd_autostart, cmd_service
from nipux_cli.service_install import launch_agent_path as _launch_agent_path
from nipux_cli.service_install import launch_agent_plist as _service_launch_agent_plist
from nipux_cli.service_install import systemd_service_text as _service_systemd_service_text
from nipux_cli.templates import program_for_job
from nipux_cli.tui_commands import slash_suggestion_lines
from nipux_cli.settings import (
    config_field_value,
    save_config_field,
)
from nipux_cli.settings_commands import (
    capture_setting_command as _capture_setting_command,
    handle_chat_setting_command as _handle_chat_setting_command,
)
from nipux_cli.tui_event_format import (
    clean_step_summary as _clean_step_summary,
    friendly_error_text as _friendly_error_text,
    generic_display_text as _generic_display_text,
)
from nipux_cli.tui_events import (
    live_badge as _live_badge,
    minimal_live_event_line as _minimal_live_event_line,
)
from nipux_cli.tui_status import (
    job_display_state as _job_display_state,
    worker_label as _worker_label,
)
from nipux_cli.tui_style import (
    _accent,
    _fancy_ui,
    _one_line,
    _status_badge,
)
from nipux_cli.uninstall import build_uninstall_plan, uninstall_runtime
from nipux_cli.updater import update_checkout
from nipux_cli.updates import render_all_updates_report, render_updates_report

_save_config_field = save_config_field
_config_field_value = config_field_value
_slash_suggestion_lines = slash_suggestion_lines
_chat_control_command = chat_control_command


def _launch_agent_plist(*, poll_seconds: float, quiet: bool) -> str:
    return _service_launch_agent_plist(poll_seconds=poll_seconds, quiet=quiet)


def _systemd_service_text(*, poll_seconds: float, quiet: bool) -> str:
    return _service_systemd_service_text(poll_seconds=poll_seconds, quiet=quiet)


SHELL_BUILTINS = {"help", "?", "commands", "exit", "quit", ":q", "clear"}
SHELL_COMMAND_NAMES = {
    "init",
    "uninstall",
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
    "usage",
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

def _db() -> tuple[AgentDB, object]:
    config = load_config()
    config.ensure_dirs()
    return AgentDB(config.runtime.state_db_path), config


def _record_command_deps() -> RecordCommandDeps:
    return RecordCommandDeps(
        db_factory=_db,
        resolve_job_id=_resolve_job_id,
        job_ref_text=_job_ref_text,
    )


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
        api_key_env = args.api_key_env or DEFAULT_OPENROUTER_API_KEY_ENV
        model = args.model or DEFAULT_OPENROUTER_MODEL
    write_private_text(
        path,
        default_config_yaml(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            context_length=args.context_length,
        ),
    )
    print(f"Wrote {path}")
    env_path = config.runtime.home / ".env"
    if not env_path.exists():
        write_private_text(
            env_path,
            f"# Optional local secrets for Nipux. This file stays outside the git repo.\n{api_key_env}=\n",
        )
        print(f"Wrote {env_path} (fill {api_key_env}; do not commit secrets)")


def cmd_update(args: argparse.Namespace) -> None:
    code, lines = update_checkout(path=args.path, allow_dirty=args.allow_dirty)
    for line in lines:
        print(line)
    if code:
        raise SystemExit(code)


def cmd_uninstall(args: argparse.Namespace) -> None:
    config = load_config()
    plan = build_uninstall_plan(runtime_home=config.runtime.home, include_legacy=not args.keep_legacy)
    if not args.yes and not args.dry_run:
        print("This will stop Nipux and remove local runtime state:")
        for path in (*plan.service_paths, *plan.paths):
            print(f"  {path.expanduser()}")
        if args.remove_tool:
            print("It will also run: uv tool uninstall nipux")
        try:
            answer = input("Type 'uninstall' to continue: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print("uninstall aborted")
            return
        if answer != "uninstall":
            print("uninstall aborted")
            return
    if not args.dry_run:
        try:
            _stop_daemon_process(config, wait=float(args.wait), quiet=True)
        except (OSError, SystemExit) as exc:
            print(f"daemon stop skipped: {exc}")
    for line in uninstall_runtime(
        runtime_home=config.runtime.home,
        dry_run=bool(args.dry_run),
        include_legacy=not args.keep_legacy,
    ):
        print(line)
    if args.remove_tool and not args.dry_run:
        uv = shutil.which("uv")
        if not uv:
            print("uv not found; remove the installed CLI with your package manager")
            return
        result = subprocess.run([uv, "tool", "uninstall", "nipux"], check=False, capture_output=True, text=True)
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                print(line)
        if result.returncode:
            raise SystemExit(result.returncode)
    elif not args.dry_run:
        print("runtime removed. If installed with uv tool, remove the CLI binary with: uv tool uninstall nipux")


def cmd_create(args: argparse.Namespace) -> None:
    if not _ensure_model_setup_verified_for_workspace():
        raise SystemExit(1)
    job_id, title = _create_job(
        objective=args.objective,
        title=args.title,
        kind=args.kind,
        cadence=args.cadence,
    )
    print(f"created {title}")


def _ensure_model_setup_verified_for_workspace() -> bool:
    config = load_config()
    if _model_setup_verified(config):
        return True
    if _workspace_has_model_config(config) and _auto_verify_model_setup(config):
        return True
    print("Model setup is not verified.")
    print("Run `nipux` and finish setup, or run `nipux doctor --check-model` after configuring a provider.")
    print("Jobs and chat stay locked until the configured model accepts a chat request.")
    return False


def _workspace_has_model_config(config: Any) -> bool:
    return bool(_read_shell_state().get("setup_completed")) or (config.runtime.home / "config.yaml").exists()


def _auto_verify_model_setup(config: Any) -> bool:
    checks = run_doctor(config=config, check_model=True)
    ok = all(check.ok for check in checks)
    if ok:
        _mark_model_setup_verified(config)
        return True
    _clear_model_setup_verified()
    return False


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
    if not _ensure_model_setup_verified_for_workspace():
        return
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
    config = load_config()
    if not _model_setup_verified(config) and _workspace_has_model_config(config):
        _auto_verify_model_setup(config)
    if not _model_setup_verified(load_config()):
        _enter_first_run_setup(history_limit=args.history_limit)
        return
    db, _ = _db()
    try:
        job_id = _default_job_id(db)
    finally:
        db.close()
    if job_id:
        _enter_chat(job_id, show_history=True, history_limit=args.history_limit)
        return

    _enter_empty_workspace(history_limit=args.history_limit)


def _enter_first_run_setup(*, history_limit: int = 12) -> None:
    if _frame_chat_enabled():
        _enter_first_run_frame(history_limit=history_limit)
        return

    print("Nipux setup requires an interactive terminal.")
    print("Run `nipux` in a terminal window to choose model, endpoint, tools, and first job.")


def _enter_empty_workspace(*, history_limit: int = 12) -> None:
    del history_limit
    print("Nipux")
    print(_rule("="))
    print("No jobs are saved in this profile.")
    print("Create a job with: nipux create \"objective\"")
    print("Edit settings with slash commands inside a job, or use: nipux init --force")
    print("Check setup with: nipux doctor")


def _print_first_run_menu() -> None:
    config = load_config()
    print("Start")
    print(f"  model   {config.model.model}")
    print("  status  ready when work starts")
    print(f"  home    {_short_path(config.runtime.home)}")
    print()
    print("Commands")
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


def _prompt_first_run_value(label: str) -> str:
    try:
        return input(f"{label} > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _first_run_create_and_open(objective: str, *, history_limit: int = 12) -> None:
    if not _ensure_model_setup_verified_for_workspace():
        return
    job_id, title = _create_job(objective=objective, title=None, kind="generic", cadence=None)
    print(f"created {title}")
    _start_interactive_daemon_if_possible()
    print("Opening workspace.")
    _enter_chat(job_id, show_history=True, history_limit=history_limit)


def _first_token(line: str) -> str:
    return _controller_first_token(line)


def _enter_first_run_frame(*, history_limit: int = 12) -> None:
    next_job_id = _run_first_run_frame(deps=_first_run_runtime_deps())
    if next_job_id:
        _start_interactive_daemon_if_possible()
        _enter_chat(next_job_id, show_history=True, history_limit=history_limit)


def _first_run_runtime_deps() -> FirstRunRuntimeDeps:
    return FirstRunRuntimeDeps(
        render_frame=lambda buffer, notices, selected, view, editing_field, previous: _render_first_run_frame(
            buffer,
            notices,
            selected=selected,
            view=view,
            editing_field=editing_field,
            previous_frame=previous,
        ),
        actions=_first_run_actions,
        handle_action=_handle_first_run_action,
        handle_line=_handle_first_run_frame_line,
        click_action=lambda x, y, view: _first_run_click_action(x, y, view=view),
    )


def _first_run_actions(view: str) -> list[tuple[str, str, str]]:
    return _first_run_tui_actions(view)


def _clamp_first_run_selection(selected: int, view: str) -> int:
    return _clamp_first_run_runtime_selection(selected, _first_run_actions(view))


def _handle_first_run_action(action: str) -> tuple[str, str | list[str] | None]:
    return _controller_handle_first_run_action(action, deps=_first_run_frame_deps())


def _first_run_click_action(x: int, y: int, *, view: str) -> int | str | None:
    width, height = shutil.get_terminal_size((100, 30))
    width = max(92, width)
    actions = _first_run_actions(view)
    if not actions or y < 10 or y > max(10, height - 4):
        return None
    gap = 2
    card_width = max(18, min(34, (width - (len(actions) - 1) * gap - 4) // len(actions)))
    total_width = len(actions) * card_width + (len(actions) - 1) * gap
    start_x = max(1, (width - total_width) // 2 + 1)
    relative = x - start_x
    if relative < 0 or relative >= total_width:
        return None
    span = card_width + gap
    index = relative // span
    within_card = relative % span < card_width
    if not within_card:
        return None
    return index if 0 <= index < len(actions) else None


def _chat_page_click(x: int, y: int, *, right_view: str) -> str | None:
    del right_view
    width, _height = shutil.get_terminal_size((100, 30))
    width = max(92, width)
    right_width = min(max(50, int(width * 0.34)), 72)
    left_width = max(48, width - right_width - 3)
    if left_width < 48:
        left_width = 48
        right_width = max(34, width - left_width - 3)
    right_start = left_width + 4
    if x < right_start or y > 8:
        return None
    relative = max(0, x - right_start)
    third = max(1, right_width // 3)
    return ["status", "updates", "work"][min(2, relative // third)]


def _handle_first_run_frame_line(line: str) -> tuple[str, str | list[str] | None]:
    return _controller_handle_first_run_frame_line(line, deps=_first_run_frame_deps())


def _first_run_chat_reply(message: str) -> str:
    return _controller_first_run_chat_reply(message)


def _create_first_run_job(objective: str) -> str | list[str]:
    return _controller_create_first_run_job(objective, deps=_first_run_frame_deps())


def _capture_first_run_command(line: str) -> list[str]:
    return _controller_capture_first_run_command(line, _run_shell_line)


def _first_run_frame_deps() -> FirstRunFrameDeps:
    return FirstRunFrameDeps(
        capture_command=_capture_first_run_command,
        capture_setting_command=_capture_setting_command,
        create_job=_create_job,
        current_default_job_id=_current_default_job_id,
        extract_objective=_extract_job_objective_from_message,
        model_setup_verified=lambda: _model_setup_verified(load_config()),
        verify_model_setup=_verify_model_setup_from_first_run,
        shell_command_names=SHELL_COMMAND_NAMES,
    )


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
    previous_frame: str = "",
) -> str:
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
    return _emit_frame_if_changed(frame, previous_frame)


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
    right_width = _first_run_columns(width)[1]
    return _build_first_run_tui_frame(
        input_buffer,
        notices,
        width=width,
        height=height,
        selected=selected,
        view=view,
        editing_field=editing_field,
        config=config,
        jobs=jobs,
        daemon_text=_daemon_state_line(daemon),
        home=_short_path(config.runtime.home, max_width=max(20, right_width - 8)),
        config_path=_short_path(config.runtime.home / "config.yaml", max_width=max(20, right_width - 8)),
    )


def _enter_chat(job_id: str, *, show_history: bool, history_limit: int = 12) -> None:
    if not _ensure_model_setup_verified_for_workspace():
        return
    _install_readline_history()
    startup_note = _start_interactive_daemon_if_possible()
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
    if startup_note:
        print(_one_line(startup_note, 180))
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
    _run_chat_frame(job_id, history_limit=history_limit, deps=_chat_frame_deps())


def _chat_frame_deps() -> ChatFrameDeps:
    return ChatFrameDeps(
        load_snapshot=lambda job_id, history_limit: _load_frame_snapshot(job_id, history_limit=history_limit),
        render_frame=lambda snapshot, buffer, notices, right_view, selected, editing_field, modal_view, previous: _render_chat_frame(
            snapshot,
            buffer,
            notices,
            right_view=right_view,
            selected_control=selected,
            editing_field=editing_field,
            modal_view=modal_view,
            previous_frame=previous,
        ),
        handle_chat_message=lambda job_id, line: _handle_chat_message(job_id, line, quiet=True),
        capture_chat_command=_capture_chat_command,
        write_shell_state=_write_shell_state,
        is_plain_chat_line=_is_plain_chat_line,
        page_click=lambda x, y, right_view: _chat_page_click(x, y, right_view=right_view),
    )


def _capture_chat_command(job_id: str, line: str) -> tuple[bool, str]:
    stream = StringIO()
    with redirect_stdout(stream):
        keep_running = _chat_handle_line(job_id, line)
    return keep_running, stream.getvalue()


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
        return load_frame_snapshot(
            db,
            config,
            job_id,
            default_job_id=_default_job_id(db),
            history_limit=history_limit,
        )
    finally:
        db.close()


def _render_chat_frame(
    snapshot: dict[str, Any],
    input_buffer: str,
    notices: list[str],
    *,
    right_view: str = "status",
    selected_control: int = 0,
    editing_field: str | None = None,
    modal_view: str | None = None,
    previous_frame: str = "",
) -> str:
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
        modal_view=modal_view,
    )
    return _emit_frame_if_changed(frame, previous_frame)


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
    modal_view: str | None = None,
) -> str:
    return _build_chat_tui_frame(
        snapshot,
        input_buffer,
        notices,
        width=width,
        height=height,
        right_view=right_view,
        selected_control=selected_control,
        editing_field=editing_field,
        modal_view=modal_view,
    )


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
    if status in {"completed", "paused", "cancelled", "failed"} or job_provider_blocked(job):
        patch = provider_retry_metadata()
        patch["last_note"] = f"reopened from {status} by operator run command"
        db.update_job_status(
            job_id,
            "queued",
            metadata_patch=patch,
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
        db.update_job_status(job_id, "queued", metadata_patch=provider_retry_metadata())
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
    return cmd_findings_impl(args, _record_command_deps())


def cmd_tasks(args: argparse.Namespace) -> None:
    return cmd_tasks_impl(args, _record_command_deps())


def cmd_roadmap(args: argparse.Namespace) -> None:
    return cmd_roadmap_impl(args, _record_command_deps())


def cmd_experiments(args: argparse.Namespace) -> None:
    return cmd_experiments_impl(args, _record_command_deps())


def cmd_sources(args: argparse.Namespace) -> None:
    return cmd_sources_impl(args, _record_command_deps())


def cmd_memory(args: argparse.Namespace) -> None:
    return cmd_memory_impl(args, _record_command_deps())


def cmd_metrics(args: argparse.Namespace) -> None:
    return cmd_metrics_impl(args, _record_command_deps())


def cmd_usage(args: argparse.Namespace) -> None:
    return cmd_usage_impl(args, _record_command_deps())


def _remote_model_preflight_failures(config) -> list[str]:
    return _daemon_remote_model_preflight_failures(config, doctor_fn=run_doctor)


def _ensure_remote_model_ready_for_worker(config, *, fake: bool) -> bool:
    return _daemon_ensure_remote_model_ready(config, fake=fake, doctor_fn=run_doctor)


def cmd_start(args: argparse.Namespace) -> None:
    return _cmd_start_impl(
        args,
        ready_fn=lambda config, fake: _ensure_remote_model_ready_for_worker(config, fake=fake),
        stop_fn=lambda config, wait, quiet: _stop_daemon_process(config, wait=wait, quiet=quiet),
    )


def _start_daemon_if_needed(
    *, poll_seconds: float, fake: bool = False, quiet: bool = False, log_file: str | None = None
) -> None:
    return _start_daemon_if_needed_impl(
        poll_seconds=poll_seconds,
        fake=fake,
        quiet=quiet,
        log_file=log_file,
        start_fn=cmd_start,
        stop_fn=lambda config, wait, quiet: _stop_daemon_process(config, wait=wait, quiet=quiet),
    )


def _start_interactive_daemon_if_possible() -> str:
    """Best-effort daemon start for the full-screen UI without printing over the frame."""

    stream = StringIO()
    with redirect_stdout(stream):
        try:
            _start_daemon_if_needed(poll_seconds=0.0, quiet=True)
        except SystemExit:
            pass
    return stream.getvalue()


def cmd_restart(args: argparse.Namespace) -> None:
    return _cmd_restart_impl(
        args,
        start_fn=cmd_start,
        stop_fn=lambda config, wait, quiet: _stop_daemon_process(config, wait=wait, quiet=quiet),
    )


def _stop_daemon_process(config, *, wait: float, quiet: bool) -> bool:
    return _stop_daemon_process_impl(config, wait=wait, quiet=quiet, pid_alive=_pid_is_alive)


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
    db, config = _db()
    try:
        if getattr(args, "all", False):
            print(
                "\n".join(
                    render_all_updates_report(
                        db,
                        config,
                        limit=args.limit,
                        chars=args.chars,
                        paths=args.paths,
                    )
                )
            )
            return
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            print("No jobs found.")
            return
        print(
            "\n".join(
                render_updates_report(
                    db,
                    config,
                    job_id,
                    limit=args.limit,
                    chars=args.chars,
                    paths=args.paths,
                )
            )
        )
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
        if not args.fake and not _model_setup_verified(config):
            _ensure_model_setup_verified_for_workspace()
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
        if not args.fake and not _model_setup_verified(config):
            _ensure_model_setup_verified_for_workspace()
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
    if not args.fake and not _model_setup_verified(load_config()):
        _ensure_model_setup_verified_for_workspace()
        return
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
    db, config = _db()
    try:
        job_id = _resolve_job_id(db, args.job_id)
        if not job_id:
            print(f"No job matched: {_job_ref_text(args.job_id)}")
            return
        print(
            render_job_digest(
                db,
                job_id,
                model=config.model.model,
                base_url=config.model.base_url,
                context_length=config.model.context_length,
                input_cost_per_million=config.model.input_cost_per_million,
                output_cost_per_million=config.model.output_cost_per_million,
            ),
            end="",
        )
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
    config = load_config()
    checks = run_doctor(config=config, check_model=args.check_model)
    for check in checks:
        status = "ok" if check.ok else "fail"
        print(f"{status}\t{check.name}\t{check.detail}")
    ok = all(check.ok for check in checks)
    if args.check_model:
        if ok:
            _mark_model_setup_verified(config)
            print("ok\tmodel_setup\tverified for workspace and chat")
        else:
            _clear_model_setup_verified()
    if not ok:
        raise SystemExit(1)


def _verify_model_setup_from_first_run() -> list[str]:
    stream = StringIO()
    with redirect_stdout(stream):
        try:
            cmd_doctor(argparse.Namespace(check_model=True))
        except SystemExit as exc:
            if exc.code not in (None, 0):
                print("Model setup is not ready. Fix the failed check above before creating a job.")
    lines = [" ".join(item.split()) for item in stream.getvalue().splitlines() if item.strip()]
    return lines[-12:] or ["done"]


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
        print("  /history /events /activity /outputs /updates /outcomes [all] /status /usage /config /settings /health")
        print("  /artifacts /artifact QUERY /findings /tasks /roadmap /experiments /sources /memory /metrics /lessons")
        print("  /model MODEL /base-url URL /api-key KEY /api-key-env ENV /context TOKENS")
        print("  /input-cost DOLLARS_PER_1M_INPUT_TOKENS /output-cost DOLLARS_PER_1M_OUTPUT_TOKENS")
        print("  /browser true|false /web true|false /cli-access true|false /file-access true|false")
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
        return _handle_chat_slash_command(job_id, parts[0], parts[1:], deps=_chat_command_deps())
    if reply_fn is None:
        reply_fn = _reply_to_chat
    _handle_chat_message(job_id, line, reply_fn=reply_fn)
    return True


def _handle_chat_message(job_id: str, line: str, *, reply_fn=None, quiet: bool = False) -> tuple[bool, str]:
    if not _model_setup_verified(load_config()):
        message = (
            "Model setup is not verified. Complete setup or run /doctor after configuring a working provider."
        )
        if not quiet:
            print(message)
        return True, message
    return _controller_handle_chat_message(
        job_id,
        line,
        deps=_chat_controller_deps(),
        reply_fn=reply_fn,
        quiet=quiet,
    )


def _chat_reply_text_and_metadata(reply: Any) -> tuple[str, dict[str, Any]]:
    return _controller_reply_text_and_metadata(reply)


def _handle_chat_control_intent(job_id: str, line: str, *, quiet: bool = False) -> tuple[bool, str] | None:
    return _controller_handle_chat_control_intent(job_id, line, deps=_chat_controller_deps(), quiet=quiet)


def _maybe_spawn_job_from_chat(job_id: str, message: str, *, quiet: bool = False) -> str:
    return _controller_maybe_spawn_job_from_chat(job_id, message, deps=_chat_controller_deps(), quiet=quiet)


def _queue_chat_note(job_id: str, message: str, *, mode: str = "steer", quiet: bool = False) -> None:
    _controller_queue_chat_note(job_id, message, deps=_chat_controller_deps(), mode=mode, quiet=quiet)


def _chat_controller_deps() -> ChatControllerDeps:
    return ChatControllerDeps(
        db_factory=_db,
        reply_fn=_reply_to_chat,
        create_job=_create_job,
        write_shell_state=_write_shell_state,
        start_daemon=_start_daemon_if_needed,
        capture_command=_capture_chat_command,
        compact_command_output=_compact_command_output,
        friendly_error_text=_friendly_error_text,
    )


def _chat_command_deps() -> ChatCommandDeps:
    return ChatCommandDeps(
        db_factory=_db,
        jobs=cmd_jobs,
        history=cmd_history,
        events=cmd_events,
        logs=cmd_logs,
        updates=cmd_updates,
        artifacts=cmd_artifacts,
        artifact=cmd_artifact,
        lessons=cmd_lessons,
        findings=cmd_findings,
        tasks=cmd_tasks,
        roadmap=cmd_roadmap,
        experiments=cmd_experiments,
        sources=cmd_sources,
        memory=cmd_memory,
        metrics=cmd_metrics,
        activity=cmd_activity,
        digest=cmd_digest,
        status=cmd_status,
        usage=cmd_usage,
        handle_setting=_handle_chat_setting_command,
        doctor=cmd_doctor,
        init=cmd_init,
        health=cmd_health,
        start=cmd_start,
        ensure_job_runnable=_ensure_job_runnable,
        run=cmd_run,
        restart=cmd_restart,
        work=cmd_work,
        pause=cmd_pause,
        resume=cmd_resume,
        cancel=cmd_cancel,
        queue_note=_queue_chat_note,
        create_job=_create_job,
        focus=cmd_focus,
        delete=cmd_delete,
    )


def _reply_to_chat(job_id: str, message: str) -> Any:
    from nipux_cli.llm import OpenAIChatLLM

    db, config = _db()
    try:
        job = db.get_job(job_id)
        messages = _build_chat_messages(db, job, message)
    finally:
        db.close()
    return OpenAIChatLLM(config.model).complete_response(messages=messages)


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
    _render_shell_help(rule=_rule)


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
    natural = natural_command_for(" ".join(tokens))
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
    return build_arg_parser(
        handlers={
            "init": cmd_init,
            "update": cmd_update,
            "uninstall": cmd_uninstall,
            "create": cmd_create,
            "jobs": cmd_jobs,
            "focus": cmd_focus,
            "rename": cmd_rename,
            "delete": cmd_delete,
            "chat": cmd_chat,
            "shell": cmd_shell,
            "steer": cmd_steer,
            "pause": cmd_pause,
            "resume": cmd_resume,
            "cancel": cmd_cancel,
            "status": cmd_status,
            "health": cmd_health,
            "history": cmd_history,
            "events": cmd_events,
            "dashboard": cmd_dashboard,
            "start": cmd_start,
            "stop": cmd_stop,
            "restart": cmd_restart,
            "browser_dashboard": cmd_browser_dashboard,
            "autostart": cmd_autostart,
            "service": cmd_service,
            "artifacts": cmd_artifacts,
            "artifact": cmd_artifact,
            "lessons": cmd_lessons,
            "learn": cmd_learn,
            "findings": cmd_findings,
            "tasks": cmd_tasks,
            "roadmap": cmd_roadmap,
            "experiments": cmd_experiments,
            "sources": cmd_sources,
            "memory": cmd_memory,
            "metrics": cmd_metrics,
            "usage": cmd_usage,
            "logs": cmd_logs,
            "activity": cmd_activity,
            "updates": cmd_updates,
            "watch": cmd_watch,
            "run_one": cmd_run_one,
            "work": cmd_work,
            "run": cmd_run,
            "digest": cmd_digest,
            "daily_digest": cmd_daily_digest,
            "daemon": cmd_daemon,
            "doctor": cmd_doctor,
        },
        version=__version__,
        default_context_length=DEFAULT_CONTEXT_LENGTH,
    )


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
