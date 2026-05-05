"""Argparse construction for Nipux CLI commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping


CommandHandler = Callable[[argparse.Namespace], None]
CommandHandlers = Mapping[str, CommandHandler]


def _handler(handlers: CommandHandlers, name: str) -> CommandHandler:
    return handlers[name]


def build_arg_parser(
    *,
    handlers: CommandHandlers,
    version: str,
    default_context_length: int,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nipux")
    parser.add_argument("--version", action="version", version=f"nipux {version}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--path")
    init.add_argument("--force", action="store_true")
    init.add_argument("--openrouter", action="store_true", help="Write an OpenRouter config that reads OPENROUTER_API_KEY")
    init.add_argument("--model", help="Model name to write into config.yaml")
    init.add_argument("--base-url", help="OpenAI-compatible API base URL")
    init.add_argument("--api-key-env", help="Environment variable that stores the API key")
    init.add_argument("--context-length", type=int, default=default_context_length)
    init.set_defaults(func=_handler(handlers, "init"))

    update = sub.add_parser("update")
    update.add_argument("--path", help="Git checkout to update. Defaults to the current Nipux install.")
    update.add_argument("--allow-dirty", action="store_true", help="Attempt git pull even when local changes exist")
    update.set_defaults(func=_handler(handlers, "update"))

    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("--yes", action="store_true", help="Confirm removal without an interactive prompt")
    uninstall.add_argument("--dry-run", action="store_true", help="Show what would be removed")
    uninstall.add_argument("--keep-legacy", action="store_true", help="Keep legacy ~/.kneepucks state if present")
    uninstall.add_argument("--remove-tool", action="store_true", help="Also run `uv tool uninstall nipux`")
    uninstall.add_argument("--wait", type=float, default=5.0, help="Seconds to wait for daemon shutdown")
    uninstall.set_defaults(func=_handler(handlers, "uninstall"))

    create = sub.add_parser("create", aliases=["new"])
    create.add_argument("objective")
    create.add_argument("--title")
    create.add_argument("--kind", default="generic")
    create.add_argument("--cadence")
    create.set_defaults(func=_handler(handlers, "create"))

    jobs = sub.add_parser("jobs")
    jobs.set_defaults(func=_handler(handlers, "jobs"))

    ls_cmd = sub.add_parser("ls")
    ls_cmd.set_defaults(func=_handler(handlers, "jobs"))

    focus = sub.add_parser("focus")
    focus.add_argument("query", nargs="*")
    focus.set_defaults(func=_handler(handlers, "focus"))

    rename = sub.add_parser("rename")
    rename.add_argument("job_id", nargs="*")
    rename.add_argument("--title", nargs="+", required=True)
    rename.set_defaults(func=_handler(handlers, "rename"))

    delete = sub.add_parser("delete", aliases=["rm"])
    delete.add_argument("job_id", nargs="*")
    delete.add_argument("--keep-files", action="store_true")
    delete.set_defaults(func=_handler(handlers, "delete"))

    chat = sub.add_parser("chat")
    chat.add_argument("job_id", nargs="*")
    chat.add_argument("--history-limit", type=int, default=12)
    chat.add_argument("--no-history", action="store_true")
    chat.set_defaults(func=_handler(handlers, "chat"))

    shell = sub.add_parser("shell")
    shell.add_argument("--status", action="store_true", help="Render the full dashboard when the shell opens")
    shell.add_argument("--no-status", action="store_true", help=argparse.SUPPRESS)
    shell.add_argument("--limit", type=int, default=8)
    shell.add_argument("--chars", type=int, default=180)
    shell.set_defaults(func=_handler(handlers, "shell"))

    steer = sub.add_parser("steer", aliases=["say"])
    steer.add_argument("--job", dest="job_id")
    steer.add_argument("message", nargs="+")
    steer.set_defaults(func=_handler(handlers, "steer"))

    pause = sub.add_parser("pause")
    pause.add_argument("parts", nargs="*", help="Optional job title/id followed by an optional note")
    pause.set_defaults(func=_handler(handlers, "pause"))

    resume = sub.add_parser("resume")
    resume.add_argument("job_id", nargs="*")
    resume.set_defaults(func=_handler(handlers, "resume"))

    cancel = sub.add_parser("cancel")
    cancel.add_argument("parts", nargs="*", help="Optional job title/id followed by an optional note")
    cancel.set_defaults(func=_handler(handlers, "cancel"))

    status = sub.add_parser("status")
    status.add_argument("job_id", nargs="*")
    status.add_argument("--limit", type=int, default=8)
    status.add_argument("--chars", type=int, default=180)
    status.add_argument("--full", action="store_true", help="Render the full dashboard")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_handler(handlers, "status"))

    health = sub.add_parser("health")
    health.add_argument("--limit", type=int, default=8)
    health.add_argument("--chars", type=int, default=180)
    health.set_defaults(func=_handler(handlers, "health"))

    history = sub.add_parser("history")
    history.add_argument("job_id", nargs="*")
    history.add_argument("--limit", type=int, default=80)
    history.add_argument("--chars", type=int, default=260)
    history.add_argument("--full", action="store_true")
    history.add_argument("--json", action="store_true")
    history.set_defaults(func=_handler(handlers, "history"))

    events = sub.add_parser("events")
    events.add_argument("job_id", nargs="*")
    events.add_argument("--limit", type=int, default=80)
    events.add_argument("--chars", type=int, default=260)
    events.add_argument("--full", action="store_true")
    events.add_argument("--follow", action="store_true")
    events.add_argument("--interval", type=float, default=2.0)
    events.add_argument("--json", action="store_true")
    events.set_defaults(func=_handler(handlers, "events"))

    dashboard = sub.add_parser("dashboard", aliases=["dash"])
    dashboard.add_argument("job_id", nargs="*")
    dashboard.add_argument("--interval", type=float, default=2.0)
    dashboard.add_argument("--limit", type=int, default=12)
    dashboard.add_argument("--chars", type=int, default=260)
    dashboard.add_argument("--no-follow", dest="follow", action="store_false")
    dashboard.add_argument("--no-clear", dest="clear", action="store_false")
    dashboard.set_defaults(func=_handler(handlers, "dashboard"), follow=True, clear=True)

    start = sub.add_parser("start")
    start.add_argument("--poll-seconds", type=float, default=0.0)
    start.add_argument("--fake", action="store_true", help="Use deterministic fake model responses")
    start.add_argument("--quiet", action="store_true", help="Write fewer daemon log lines")
    start.add_argument("--log-file")
    start.set_defaults(func=_handler(handlers, "start"))

    stop = sub.add_parser("stop")
    stop.add_argument("job_id", nargs="*", help="Optional job title/id to pause instead of stopping the daemon")
    stop.add_argument("--wait", type=float, default=5.0)
    stop.set_defaults(func=_handler(handlers, "stop"))

    restart = sub.add_parser("restart")
    restart.add_argument("--poll-seconds", type=float, default=0.0)
    restart.add_argument("--wait", type=float, default=5.0)
    restart.add_argument("--fake", action="store_true", help="Use deterministic fake model responses")
    restart.add_argument("--quiet", action="store_true", help="Write fewer daemon log lines")
    restart.add_argument("--log-file")
    restart.set_defaults(func=_handler(handlers, "restart"))

    browser_dashboard = sub.add_parser("browser-dashboard")
    browser_dashboard.add_argument("--port", type=int, default=4848)
    browser_dashboard.add_argument("--foreground", action="store_true")
    browser_dashboard.add_argument("--stop", action="store_true")
    browser_dashboard.add_argument("--log-file")
    browser_dashboard.set_defaults(func=_handler(handlers, "browser_dashboard"))

    autostart = sub.add_parser("autostart")
    autostart.add_argument("action", choices=["install", "status", "uninstall"])
    autostart.add_argument("--poll-seconds", type=float, default=5.0)
    autostart.add_argument("--quiet", action="store_true")
    autostart.set_defaults(func=_handler(handlers, "autostart"))

    service = sub.add_parser("service")
    service.add_argument("action", choices=["install", "status", "uninstall"])
    service.add_argument("--poll-seconds", type=float, default=0.0)
    service.add_argument("--quiet", action="store_true")
    service.set_defaults(func=_handler(handlers, "service"))

    artifacts = sub.add_parser("artifacts")
    artifacts.add_argument("job_id", nargs="*")
    artifacts.add_argument("--limit", type=int, default=25)
    artifacts.add_argument("--chars", type=int, default=220)
    artifacts.add_argument("--paths", action="store_true", help="Show full artifact paths")
    artifacts.set_defaults(func=_handler(handlers, "artifacts"))

    artifact = sub.add_parser("artifact")
    artifact.add_argument("artifact_id_or_path", nargs="+")
    artifact.add_argument("--job", dest="job_id")
    artifact.add_argument("--chars", type=int, default=12000)
    artifact.set_defaults(func=_handler(handlers, "artifact"))

    lessons = sub.add_parser("lessons")
    lessons.add_argument("job_id", nargs="*")
    lessons.add_argument("--limit", type=int, default=25)
    lessons.add_argument("--chars", type=int, default=220)
    lessons.set_defaults(func=_handler(handlers, "lessons"))

    learn = sub.add_parser("learn")
    learn.add_argument("--job", dest="job_id")
    learn.add_argument("--category", default="operator_preference")
    learn.add_argument("--chars", type=int, default=220)
    learn.add_argument("lesson", nargs="+")
    learn.set_defaults(func=_handler(handlers, "learn"))

    findings = sub.add_parser("findings")
    findings.add_argument("job_id", nargs="*")
    findings.add_argument("--limit", type=int, default=25)
    findings.add_argument("--chars", type=int, default=220)
    findings.add_argument("--json", action="store_true")
    findings.set_defaults(func=_handler(handlers, "findings"))

    tasks = sub.add_parser("tasks")
    tasks.add_argument("job_id", nargs="*")
    tasks.add_argument("--limit", type=int, default=25)
    tasks.add_argument("--chars", type=int, default=220)
    tasks.add_argument("--status", nargs="+")
    tasks.add_argument("--json", action="store_true")
    tasks.set_defaults(func=_handler(handlers, "tasks"))

    roadmap = sub.add_parser("roadmap")
    roadmap.add_argument("job_id", nargs="*")
    roadmap.add_argument("--limit", type=int, default=25)
    roadmap.add_argument("--features", type=int, default=3)
    roadmap.add_argument("--chars", type=int, default=220)
    roadmap.add_argument("--json", action="store_true")
    roadmap.set_defaults(func=_handler(handlers, "roadmap"))

    experiments = sub.add_parser("experiments")
    experiments.add_argument("job_id", nargs="*")
    experiments.add_argument("--limit", type=int, default=25)
    experiments.add_argument("--chars", type=int, default=220)
    experiments.add_argument("--status", nargs="+")
    experiments.add_argument("--json", action="store_true")
    experiments.set_defaults(func=_handler(handlers, "experiments"))

    sources = sub.add_parser("sources")
    sources.add_argument("job_id", nargs="*")
    sources.add_argument("--limit", type=int, default=25)
    sources.add_argument("--chars", type=int, default=220)
    sources.add_argument("--json", action="store_true")
    sources.set_defaults(func=_handler(handlers, "sources"))

    memory = sub.add_parser("memory")
    memory.add_argument("job_id", nargs="*")
    memory.add_argument("--limit", type=int, default=10)
    memory.add_argument("--chars", type=int, default=260)
    memory.set_defaults(func=_handler(handlers, "memory"))

    metrics = sub.add_parser("metrics")
    metrics.add_argument("job_id", nargs="*")
    metrics.add_argument("--chars", type=int, default=220)
    metrics.set_defaults(func=_handler(handlers, "metrics"))

    usage = sub.add_parser("usage")
    usage.add_argument("job_id", nargs="*")
    usage.add_argument("--json", action="store_true")
    usage.set_defaults(func=_handler(handlers, "usage"))

    logs = sub.add_parser("logs", aliases=["outputs", "output"])
    logs.add_argument("job_id", nargs="*")
    logs.add_argument("--limit", type=int, default=25)
    logs.add_argument("--verbose", action="store_true")
    logs.add_argument("--chars", type=int, default=4000)
    logs.set_defaults(func=_handler(handlers, "logs"))

    activity = sub.add_parser("activity", aliases=["feed", "tail"])
    activity.add_argument("job_id", nargs="*")
    activity.add_argument("--limit", type=int, default=20)
    activity.add_argument("--chars", type=int, default=180)
    activity.add_argument("--follow", action="store_true")
    activity.add_argument("--interval", type=float, default=2.0)
    activity.add_argument("--verbose", action="store_true")
    activity.add_argument("--paths", action="store_true", help="Show full artifact paths")
    activity.set_defaults(func=_handler(handlers, "activity"))

    updates = sub.add_parser("updates", aliases=["outcomes", "outcome"])
    updates.add_argument("job_id", nargs="*")
    updates.add_argument("--all", action="store_true", help="Show durable outcome summaries for every job")
    updates.add_argument("--limit", type=int, default=5)
    updates.add_argument("--chars", type=int, default=180)
    updates.add_argument("--paths", action="store_true", help="Show full artifact paths")
    updates.set_defaults(func=_handler(handlers, "updates"))

    watch = sub.add_parser("watch")
    watch.add_argument("job_id", nargs="+")
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument("--limit", type=int, default=20)
    watch.add_argument("--verbose", action="store_true")
    watch.add_argument("--chars", type=int, default=4000)
    watch.add_argument("--no-follow", dest="follow", action="store_false")
    watch.set_defaults(func=_handler(handlers, "watch"), follow=True)

    run_one = sub.add_parser("run-one")
    run_one.add_argument("job_id", nargs="+")
    run_one.add_argument("--fake", action="store_true", help="Use a deterministic fake model response")
    run_one.set_defaults(func=_handler(handlers, "run_one"))

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
    work.set_defaults(func=_handler(handlers, "work"))

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
    run.set_defaults(func=_handler(handlers, "run"))

    digest = sub.add_parser("digest")
    digest.add_argument("job_id", nargs="+")
    digest.set_defaults(func=_handler(handlers, "digest"))

    daily_digest = sub.add_parser("daily-digest")
    daily_digest.add_argument("--day", help="YYYY-MM-DD. Defaults to today.")
    daily_digest.set_defaults(func=_handler(handlers, "daily_digest"))

    daemon = sub.add_parser("daemon")
    daemon.add_argument("--once", action="store_true", help="Run at most one job step and exit")
    daemon.add_argument("--fake", action="store_true", help="Use deterministic fake model responses")
    daemon.add_argument("--poll-seconds", type=float, default=0.0)
    daemon.add_argument("--quiet", action="store_true", help="Do not print foreground progress lines")
    daemon.add_argument("--verbose", action="store_true", help="Print model-visible job state and step results")
    daemon.set_defaults(func=_handler(handlers, "daemon"))

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--check-model", action="store_true", help="Also call the local model /models endpoint")
    doctor.set_defaults(func=_handler(handlers, "doctor"))

    return parser
