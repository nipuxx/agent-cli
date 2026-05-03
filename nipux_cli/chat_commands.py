"""Slash-command dispatch for focused chat sessions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Callable

from nipux_cli.tui_style import _one_line


@dataclass(frozen=True)
class ChatCommandDeps:
    db_factory: Callable[[], tuple[Any, Any]]
    jobs: Callable[[argparse.Namespace], None]
    history: Callable[[argparse.Namespace], None]
    events: Callable[[argparse.Namespace], None]
    logs: Callable[[argparse.Namespace], None]
    updates: Callable[[argparse.Namespace], None]
    artifacts: Callable[[argparse.Namespace], None]
    artifact: Callable[[argparse.Namespace], None]
    lessons: Callable[[argparse.Namespace], None]
    findings: Callable[[argparse.Namespace], None]
    tasks: Callable[[argparse.Namespace], None]
    roadmap: Callable[[argparse.Namespace], None]
    experiments: Callable[[argparse.Namespace], None]
    sources: Callable[[argparse.Namespace], None]
    memory: Callable[[argparse.Namespace], None]
    metrics: Callable[[argparse.Namespace], None]
    activity: Callable[[argparse.Namespace], None]
    digest: Callable[[argparse.Namespace], None]
    status: Callable[[argparse.Namespace], None]
    usage: Callable[[argparse.Namespace], None]
    handle_setting: Callable[[str, list[str]], bool]
    doctor: Callable[[argparse.Namespace], None]
    init: Callable[[argparse.Namespace], None]
    health: Callable[[argparse.Namespace], None]
    start: Callable[[argparse.Namespace], None]
    ensure_job_runnable: Callable[[Any, str], None]
    run: Callable[[argparse.Namespace], None]
    restart: Callable[[argparse.Namespace], None]
    work: Callable[[argparse.Namespace], None]
    pause: Callable[[argparse.Namespace], None]
    resume: Callable[[argparse.Namespace], None]
    cancel: Callable[[argparse.Namespace], None]
    queue_note: Callable[..., None]
    create_job: Callable[..., tuple[str, str]]
    focus: Callable[[argparse.Namespace], None]
    delete: Callable[[argparse.Namespace], None]


def handle_chat_slash_command(job_id: str, command: str, rest: list[str], *, deps: ChatCommandDeps) -> bool:
    if command in {"jobs", "ls"}:
        deps.jobs(argparse.Namespace())
        return True
    if command == "history":
        deps.history(
            argparse.Namespace(
                job_id=job_id,
                limit=_optional_int(rest, default=40),
                chars=220,
                full=False,
                json=False,
            )
        )
        return True
    if command == "events":
        deps.events(
            argparse.Namespace(
                job_id=job_id,
                limit=_optional_int(rest, default=40),
                chars=220,
                full=False,
                json=False,
                follow=False,
                interval=2.0,
            )
        )
        return True
    if command == "outputs":
        deps.logs(
            argparse.Namespace(
                job_id=[job_id],
                limit=_optional_int(rest, default=25),
                verbose=False,
                chars=260,
            )
        )
        return True
    if command in {"updates", "outcomes", "outcome"}:
        all_jobs = bool(rest and rest[0].lower() == "all")
        deps.updates(argparse.Namespace(job_id=job_id, all=all_jobs, limit=5, chars=180, paths=False))
        return True
    if command == "artifacts":
        deps.artifacts(argparse.Namespace(job_id=job_id, limit=10, chars=220, paths=False))
        return True
    if command == "artifact":
        query = " ".join(rest).strip()
        if not query:
            print("usage: /artifact QUERY_OR_ID")
            return True
        deps.artifact(argparse.Namespace(artifact_id_or_path=[query], job_id=job_id, chars=12000))
        return True
    if command == "lessons":
        deps.lessons(argparse.Namespace(job_id=job_id, limit=10, chars=220))
        return True
    if command == "findings":
        deps.findings(argparse.Namespace(job_id=job_id, limit=20, chars=220, json=False))
        return True
    if command == "tasks":
        deps.tasks(argparse.Namespace(job_id=job_id, limit=20, chars=220, status=None, json=False))
        return True
    if command == "roadmap":
        deps.roadmap(argparse.Namespace(job_id=job_id, limit=20, features=3, chars=220, json=False))
        return True
    if command == "experiments":
        deps.experiments(argparse.Namespace(job_id=job_id, limit=20, chars=220, status=None, json=False))
        return True
    if command == "sources":
        deps.sources(argparse.Namespace(job_id=job_id, limit=20, chars=220, json=False))
        return True
    if command == "memory":
        deps.memory(argparse.Namespace(job_id=job_id, limit=10, chars=220))
        return True
    if command == "metrics":
        deps.metrics(argparse.Namespace(job_id=job_id, chars=220))
        return True
    if command == "learn":
        lesson = " ".join(rest).strip()
        if not lesson:
            print("usage: /learn LESSON")
            return True
        db, _config = deps.db_factory()
        try:
            entry = db.append_lesson(job_id, lesson, category="operator_preference", metadata={"source": "chat"})
            job = db.get_job(job_id)
            print(f"learned for {job['title']}: {_one_line(entry['lesson'], 220)}")
        finally:
            db.close()
        return True
    if command == "activity":
        deps.activity(
            argparse.Namespace(job_id=job_id, limit=20, chars=180, follow=False, interval=2.0, verbose=False, paths=False)
        )
        return True
    if command == "digest":
        deps.digest(argparse.Namespace(job_id=[job_id]))
        return True
    if command == "status":
        deps.status(argparse.Namespace(job_id=job_id, limit=8, chars=180, full=False, json=False))
        return True
    if command == "usage":
        deps.usage(argparse.Namespace(job_id=job_id, json=False))
        return True
    if command == "settings":
        deps.handle_setting("config", [])
        return True
    if deps.handle_setting(command, rest):
        return True
    if command == "doctor":
        try:
            deps.doctor(argparse.Namespace(check_model=False))
        except SystemExit:
            pass
        return True
    if command == "init":
        deps.init(argparse.Namespace(path=None, force=False, model=None, base_url=None, api_key_env=None, openrouter=False))
        return True
    if command == "health":
        deps.health(argparse.Namespace(limit=8, chars=180))
        return True
    if command == "start":
        deps.start(argparse.Namespace(poll_seconds=0.0, fake=False, quiet=False, log_file=None))
        return True
    if command == "run":
        db, _config = deps.db_factory()
        try:
            deps.ensure_job_runnable(db, job_id)
        finally:
            db.close()
        deps.run(
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
        deps.restart(argparse.Namespace(poll_seconds=0.0, wait=5.0, fake=False, quiet=False, log_file=None))
        return True
    if command in {"work", "work-verbose"}:
        deps.work(
            argparse.Namespace(
                job_id=job_id,
                steps=_optional_int(rest, default=1),
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
        deps.pause(argparse.Namespace(job_id=job_id, note=rest))
        return True
    if command == "resume":
        deps.resume(argparse.Namespace(job_id=job_id))
        return True
    if command == "cancel":
        deps.cancel(argparse.Namespace(job_id=job_id, note=rest))
        return True
    if command == "note":
        message = " ".join(rest).strip()
        if not message:
            print("usage: /note MESSAGE")
            return True
        deps.queue_note(job_id, message, mode="note")
        return True
    if command == "follow":
        message = " ".join(rest).strip()
        if not message:
            print("usage: /follow MESSAGE")
            return True
        deps.queue_note(job_id, message, mode="follow_up")
        return True
    if command == "new":
        objective = " ".join(rest).strip()
        if not objective:
            print("usage: /new OBJECTIVE")
            return True
        _created_id, title = deps.create_job(objective=objective, title=None, kind="generic", cadence=None)
        print(f"created {title}")
        deps.start(argparse.Namespace(poll_seconds=0.0, fake=False, quiet=True, log_file=None))
        print(f"focus set to {title}; initial plan accepted and worker started.")
        return True
    if command in {"focus", "switch"}:
        if not " ".join(rest).strip():
            deps.focus(argparse.Namespace(query=[]))
            return True
        deps.focus(argparse.Namespace(query=rest))
        return True
    if command == "delete":
        target = rest if rest else [job_id]
        deps.delete(argparse.Namespace(job_id=target, keep_files=False))
        return bool(rest)
    print(f"unknown chat command: /{command}")
    return True


def _optional_int(values: list[str], *, default: int) -> int:
    return int(values[0]) if values and values[0].isdigit() else default
