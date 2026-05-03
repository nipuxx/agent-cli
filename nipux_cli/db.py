"""SQLite state store for the Nipux agent."""

from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from nipux_cli.metric_format import format_metric_value

T = TypeVar("T")

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'generic',
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    cadence TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS job_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    model TEXT,
    config_hash TEXT,
    score REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    run_id TEXT NOT NULL REFERENCES job_runs(id),
    step_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    tool_name TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    summary TEXT,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    run_id TEXT,
    step_id TEXT,
    type TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    title TEXT,
    summary TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    url_or_source TEXT NOT NULL,
    artifact_id TEXT REFERENCES artifacts(id),
    extracted_text_path TEXT,
    summary TEXT,
    score_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_index (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    key TEXT NOT NULL,
    summary TEXT NOT NULL,
    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, key)
);

CREATE TABLE IF NOT EXISTS digests (
    id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    target TEXT,
    subject TEXT,
    body_path TEXT,
    sent_at TEXT,
    status TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id),
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    title TEXT,
    body TEXT,
    ref_table TEXT,
    ref_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON jobs(status, priority DESC, updated_at);
CREATE INDEX IF NOT EXISTS idx_runs_job ON job_runs(job_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, step_no);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_job_time ON events(job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_ref ON events(ref_table, ref_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:72].strip("-") or new_id("job")


def _unique_job_id(conn: sqlite3.Connection, seed: str) -> str:
    base = _slugify(seed)
    candidate = base
    suffix = 2
    while conn.execute("SELECT 1 FROM jobs WHERE id = ? LIMIT 1", (candidate,)).fetchone():
        candidate = f"{base[:68]}-{suffix}"
        suffix += 1
    return candidate


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nested_value(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _metadata_list(metadata: dict[str, Any], key: str) -> list[dict[str, Any]]:
    values = metadata.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:120]


def _clean_status(value: str, allowed: set[str], default: str) -> str:
    status = (value.strip().lower() or default).replace(" ", "_")
    return status if status in allowed else default


def _experiment_metric_value(entry: dict[str, Any]) -> float | None:
    try:
        value = entry.get("metric_value")
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _same_metric_group(
    entry: dict[str, Any],
    *,
    metric_name: str,
    metric_unit: str,
    higher_is_better: bool,
) -> bool:
    return (
        str(entry.get("metric_name") or "").strip().lower() == metric_name.strip().lower()
        and str(entry.get("metric_unit") or "").strip().lower() == metric_unit.strip().lower()
        and bool(entry.get("higher_is_better", True)) == bool(higher_is_better)
        and _experiment_metric_value(entry) is not None
    )


def _best_experiment_for_metric(
    experiments: list[dict[str, Any]],
    *,
    metric_name: str,
    metric_unit: str,
    higher_is_better: bool,
    exclude_key: str = "",
) -> dict[str, Any] | None:
    candidates = [
        experiment
        for experiment in experiments
        if experiment.get("key") != exclude_key
        and _same_metric_group(
            experiment,
            metric_name=metric_name,
            metric_unit=metric_unit,
            higher_is_better=higher_is_better,
        )
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: _experiment_metric_value(item) or 0.0) if higher_is_better else min(candidates, key=lambda item: _experiment_metric_value(item) or 0.0)


def _metric_delta(
    *,
    metric_value: Any,
    previous_best: dict[str, Any] | None,
    higher_is_better: bool,
) -> float | None:
    try:
        current = float(metric_value)
    except (TypeError, ValueError):
        return None
    if previous_best is None:
        return None
    previous = _experiment_metric_value(previous_best)
    if previous is None:
        return None
    delta = current - previous if higher_is_better else previous - current
    return round(delta, 6)


def _mark_best_experiments(experiments: list[dict[str, Any]]) -> dict[str, Any] | None:
    groups: dict[tuple[str, str, bool], list[dict[str, Any]]] = {}
    for experiment in experiments:
        metric_name = str(experiment.get("metric_name") or "").strip().lower()
        if _experiment_metric_value(experiment) is None or not metric_name:
            experiment["best_observed"] = False
            continue
        key = (
            metric_name,
            str(experiment.get("metric_unit") or "").strip().lower(),
            bool(experiment.get("higher_is_better", True)),
        )
        groups.setdefault(key, []).append(experiment)
    winners: list[dict[str, Any]] = []
    for (_metric_name, _metric_unit, higher_is_better), entries in groups.items():
        winner = max(entries, key=lambda item: _experiment_metric_value(item) or 0.0) if higher_is_better else min(entries, key=lambda item: _experiment_metric_value(item) or 0.0)
        for entry in entries:
            entry["best_observed"] = entry is winner
        winners.append(winner)
    if not winners:
        return None
    return max(winners, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("metadata_json", "input_json", "output_json", "score_json", "artifact_refs_json"):
        if key in result:
            try:
                result[key.removesuffix("_json")] = json.loads(result[key] or "{}")
            except json.JSONDecodeError:
                result[key.removesuffix("_json")] = {}
    return result


def _insert_event(
    conn: sqlite3.Connection,
    *,
    job_id: str | None,
    event_type: str,
    title: str = "",
    body: str = "",
    ref_table: str = "",
    ref_id: str = "",
    metadata: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    event_id = new_id("evt")
    when = created_at or utc_now()
    conn.execute(
        """
        INSERT INTO events(id, job_id, event_type, created_at, title, body, ref_table, ref_id, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            job_id,
            event_type.strip().lower() or "event",
            when,
            title.strip(),
            body.strip(),
            ref_table.strip(),
            ref_id.strip(),
            _json_dumps(metadata or {}),
        ),
    )
    return {
        "id": event_id,
        "job_id": job_id,
        "event_type": event_type.strip().lower() or "event",
        "created_at": when,
        "title": title.strip(),
        "body": body.strip(),
        "ref_table": ref_table.strip(),
        "ref_id": ref_id.strip(),
        "metadata": metadata or {},
    }


def _projected_event(
    *,
    event_id: str,
    job_id: str,
    event_type: str,
    created_at: str,
    title: str = "",
    body: str = "",
    ref_table: str = "",
    ref_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "job_id": job_id,
        "event_type": event_type,
        "created_at": created_at,
        "title": title,
        "body": body,
        "ref_table": ref_table,
        "ref_id": ref_id,
        "metadata": metadata or {},
        "projected": True,
    }


class AgentDB:
    """Small SQLite wrapper with WAL and jittered write retries."""

    _WRITE_RETRIES = 12

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                finally:
                    self._conn.close()
                    self._conn = None

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            row = self._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif int(row["version"]) != SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported nipux schema version: {row['version']}")

    def _write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        last_error: Exception | None = None
        for attempt in range(self._WRITE_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                        return result
                    except BaseException:
                        self._conn.rollback()
                        raise
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                last_error = exc
                if attempt < self._WRITE_RETRIES - 1:
                    time.sleep(random.uniform(0.02, 0.15))
        raise last_error or sqlite3.OperationalError("database is locked")

    def append_event(
        self,
        job_id: str | None = None,
        *,
        event_type: str,
        title: str = "",
        body: str = "",
        ref_table: str = "",
        ref_id: str = "",
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            return _insert_event(
                conn,
                job_id=job_id,
                event_type=event_type,
                title=title,
                body=body,
                ref_table=ref_table,
                ref_id=ref_id,
                metadata=metadata,
                created_at=created_at,
            )

        return self._write(op)

    def list_events(
        self,
        *,
        job_id: str | None = None,
        limit: int = 100,
        event_types: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        filters = []
        params: list[Any] = []
        if job_id is not None:
            filters.append("job_id = ?")
            params.append(job_id)
        if event_types:
            values = [str(value).strip().lower() for value in event_types if str(value).strip()]
            if values:
                filters.append(f"event_type IN ({','.join('?' for _ in values)})")
                params.extend(values)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = self._conn.execute(
            f"""
            SELECT * FROM (
                SELECT * FROM events
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            )
            ORDER BY created_at ASC, id ASC
            """,
            [*params, int(limit)],
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_timeline_events(self, job_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return visible job history, combining durable events with old projected state."""

        actual = self.list_events(job_id=job_id, limit=max(limit * 4, 250))
        actual_ids = {str(event.get("id")) for event in actual}
        actual_refs = {
            (str(event.get("ref_table") or ""), str(event.get("ref_id") or ""))
            for event in actual
            if event.get("ref_table") and event.get("ref_id")
        }
        timeline: list[dict[str, Any]] = list(actual)
        job = self.get_job(job_id)
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}

        for index, entry in enumerate(_metadata_list(metadata, "operator_messages")):
            if entry.get("event_id") in actual_ids:
                continue
            timeline.append(_projected_event(
                event_id=f"projected_operator_{index}",
                job_id=job_id,
                event_type="operator_message",
                created_at=str(entry.get("at") or job.get("updated_at") or job.get("created_at")),
                title=str(entry.get("source") or "operator"),
                body=str(entry.get("message") or ""),
                metadata={
                    "source": entry.get("source") or "operator",
                    "mode": entry.get("mode") or "steer",
                    "claimed_at": entry.get("claimed_at"),
                    "acknowledged_at": entry.get("acknowledged_at"),
                    "superseded_at": entry.get("superseded_at"),
                },
            ))

        for index, entry in enumerate(_metadata_list(metadata, "agent_updates")):
            if entry.get("event_id") in actual_ids:
                continue
            timeline.append(_projected_event(
                event_id=f"projected_agent_{index}",
                job_id=job_id,
                event_type="agent_message",
                created_at=str(entry.get("at") or job.get("updated_at") or job.get("created_at")),
                title=str(entry.get("category") or "progress"),
                body=str(entry.get("message") or ""),
                metadata=entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {},
            ))

        for index, lesson in enumerate(_metadata_list(metadata, "lessons")):
            if lesson.get("event_id") in actual_ids:
                continue
            timeline.append(_projected_event(
                event_id=f"projected_lesson_{index}",
                job_id=job_id,
                event_type="lesson",
                created_at=str(lesson.get("at") or lesson.get("last_seen") or job.get("updated_at") or job.get("created_at")),
                title=str(lesson.get("category") or "memory"),
                body=str(lesson.get("lesson") or ""),
                metadata={"confidence": lesson.get("confidence"), **(lesson.get("metadata") if isinstance(lesson.get("metadata"), dict) else {})},
            ))

        for index, source in enumerate(_metadata_list(metadata, "source_ledger")):
            if source.get("event_id") in actual_ids:
                continue
            timeline.append(_projected_event(
                event_id=f"projected_source_{index}",
                job_id=job_id,
                event_type="source",
                created_at=str(source.get("last_seen") or source.get("first_seen") or job.get("updated_at") or job.get("created_at")),
                title=str(source.get("source") or "source"),
                body=str(source.get("last_outcome") or ""),
                metadata=source,
            ))

        for index, finding in enumerate(_metadata_list(metadata, "finding_ledger")):
            if finding.get("event_id") in actual_ids:
                continue
            timeline.append(_projected_event(
                event_id=f"projected_finding_{index}",
                job_id=job_id,
                event_type="finding",
                created_at=str(finding.get("updated_at") or finding.get("created_at") or job.get("updated_at") or job.get("created_at")),
                title=str(finding.get("name") or "finding"),
                body=str(finding.get("reason") or finding.get("category") or ""),
                metadata=finding,
            ))

        for index, task in enumerate(_metadata_list(metadata, "task_queue")):
            if task.get("event_id") in actual_ids:
                continue
            timeline.append(_projected_event(
                event_id=f"projected_task_{index}",
                job_id=job_id,
                event_type="task",
                created_at=str(task.get("updated_at") or task.get("created_at") or job.get("updated_at") or job.get("created_at")),
                title=str(task.get("title") or "task"),
                body=str(task.get("result") or task.get("goal") or ""),
                metadata=task,
            ))

        for index, experiment in enumerate(_metadata_list(metadata, "experiment_ledger")):
            if experiment.get("event_id") in actual_ids:
                continue
            metric = ""
            if experiment.get("metric_value") is not None:
                metric = format_metric_value(
                    experiment.get("metric_name") or "metric",
                    experiment.get("metric_value"),
                    experiment.get("metric_unit") or "",
                )
            timeline.append(_projected_event(
                event_id=f"projected_experiment_{index}",
                job_id=job_id,
                event_type="experiment",
                created_at=str(experiment.get("updated_at") or experiment.get("created_at") or job.get("updated_at") or job.get("created_at")),
                title=str(experiment.get("title") or "experiment"),
                body=str(experiment.get("result") or metric or experiment.get("hypothesis") or ""),
                metadata=experiment,
            ))

        for index, reflection in enumerate(_metadata_list(metadata, "reflections")):
            if reflection.get("event_id") in actual_ids:
                continue
            timeline.append(_projected_event(
                event_id=f"projected_reflection_{index}",
                job_id=job_id,
                event_type="reflection",
                created_at=str(reflection.get("at") or job.get("updated_at") or job.get("created_at")),
                title="reflection",
                body=str(reflection.get("summary") or reflection.get("strategy") or ""),
                metadata=reflection.get("metadata") if isinstance(reflection.get("metadata"), dict) else {},
            ))

        for step in self.list_steps(job_id=job_id):
            ref = ("steps", str(step["id"]))
            if ref in actual_refs:
                continue
            event_type = "error" if step.get("status") == "failed" or step.get("error") else "tool_result"
            title = str(step.get("tool_name") or step.get("kind") or "step")
            body = str(step.get("summary") or step.get("error") or "")
            timeline.append(_projected_event(
                event_id=f"projected_step_{step['id']}",
                job_id=job_id,
                event_type=event_type,
                created_at=str(step.get("ended_at") or step.get("started_at")),
                title=title,
                body=body,
                ref_table="steps",
                ref_id=str(step["id"]),
                metadata={"step_no": step.get("step_no"), "status": step.get("status"), "kind": step.get("kind")},
            ))

        for artifact in self.list_artifacts(job_id, limit=10000):
            ref = ("artifacts", str(artifact["id"]))
            if ref in actual_refs:
                continue
            timeline.append(_projected_event(
                event_id=f"projected_artifact_{artifact['id']}",
                job_id=job_id,
                event_type="artifact",
                created_at=str(artifact.get("created_at")),
                title=str(artifact.get("title") or artifact["id"]),
                body=str(artifact.get("summary") or artifact.get("path") or ""),
                ref_table="artifacts",
                ref_id=str(artifact["id"]),
                metadata={"type": artifact.get("type"), "path": artifact.get("path")},
            ))

        for memory in self.list_memory(job_id):
            ref = ("memory_index", str(memory["id"]))
            if ref in actual_refs:
                continue
            timeline.append(_projected_event(
                event_id=f"projected_memory_{memory['id']}",
                job_id=job_id,
                event_type="compaction",
                created_at=str(memory.get("updated_at")),
                title=str(memory.get("key") or "compact memory"),
                body=str(memory.get("summary") or ""),
                ref_table="memory_index",
                ref_id=str(memory["id"]),
                metadata={"artifact_refs": memory.get("artifact_refs") or []},
            ))

        timeline = [event for event in timeline if event.get("created_at")]
        timeline.sort(key=lambda event: (str(event.get("created_at") or ""), str(event.get("id") or "")))
        return timeline[-int(limit):]

    def create_job(
        self,
        objective: str,
        *,
        title: str | None = None,
        kind: str = "generic",
        priority: int = 0,
        cadence: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        now = utc_now()
        title = title or objective.strip().splitlines()[0][:80] or "Untitled job"

        def op(conn: sqlite3.Connection) -> str:
            job_id = _unique_job_id(conn, title)
            conn.execute(
                """
                INSERT INTO jobs(id, title, objective, kind, status, priority, cadence, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (job_id, title, objective, kind, priority, cadence, now, now, _json_dumps(metadata)),
            )
            _insert_event(
                conn,
                job_id=job_id,
                event_type="daemon",
                title="job created",
                body=objective,
                metadata={"title": title, "kind": kind, "cadence": cadence},
                created_at=now,
            )
            return job_id

        return self._write(op)

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        job = _row_to_dict(row)
        if job is None:
            raise KeyError(f"Job not found: {job_id}")
        return job

    def list_jobs(self, *, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if statuses:
            values = list(statuses)
            placeholders = ",".join("?" for _ in values)
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY priority DESC, updated_at",
                values,
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC").fetchall()
        return [_row_to_dict(row) for row in rows]

    def update_job_status(self, job_id: str, status: str, *, metadata_patch: dict[str, Any] | None = None) -> None:
        now = utc_now()

        def op(conn: sqlite3.Connection) -> None:
            metadata_json = None
            if metadata_patch:
                row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise KeyError(f"Job not found: {job_id}")
                current = json.loads(row["metadata_json"] or "{}")
                current.update(metadata_patch)
                metadata_json = _json_dumps(current)
            if metadata_json is None:
                conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, now, job_id))
            else:
                conn.execute(
                    "UPDATE jobs SET status = ?, updated_at = ?, metadata_json = ? WHERE id = ?",
                    (status, now, metadata_json, job_id),
                )
            _insert_event(
                conn,
                job_id=job_id,
                event_type="daemon",
                title=f"job {status}",
                body=str((metadata_patch or {}).get("last_note") or ""),
                metadata={"status": status, "metadata_patch": metadata_patch or {}},
                created_at=now,
            )

        self._write(op)

    def update_job_metadata(self, job_id: str, metadata_patch: dict[str, Any]) -> None:
        now = utc_now()

        def op(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            current = json.loads(row["metadata_json"] or "{}")
            current.update(metadata_patch)
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(current), job_id),
            )

        self._write(op)

    def claim_operator_messages(
        self,
        job_id: str,
        *,
        modes: Iterable[str] = ("steer",),
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        now = utc_now()
        allowed = {mode.strip().lower().replace("-", "_") for mode in modes}

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            metadata = json.loads(row["metadata_json"] or "{}")
            messages = metadata.get("operator_messages")
            if not isinstance(messages, list):
                return []
            claimed: list[dict[str, Any]] = []
            for entry in messages:
                if len(claimed) >= limit:
                    break
                if not isinstance(entry, dict):
                    continue
                mode = str(entry.get("mode") or "steer").strip().lower().replace("-", "_")
                if mode not in allowed or entry.get("claimed_at"):
                    continue
                if entry.get("acknowledged_at") or entry.get("superseded_at"):
                    continue
                entry["claimed_at"] = now
                entry["delivered_at"] = now
                claimed.append(dict(entry))
            if not claimed:
                return []
            metadata["operator_messages"] = messages[-200:]
            metadata["last_claimed_operator_messages"] = claimed
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(metadata), job_id),
            )
            for entry in claimed:
                _insert_event(
                    conn,
                    job_id=job_id,
                    event_type="loop",
                    title="steering claimed",
                    body=str(entry.get("message") or ""),
                    metadata={
                        "source": entry.get("source"),
                        "mode": entry.get("mode"),
                        "operator_event_id": entry.get("event_id"),
                    },
                    created_at=now,
                )
            return claimed

        return self._write(op)

    def acknowledge_operator_messages(
        self,
        job_id: str,
        *,
        message_ids: Iterable[str] | None = None,
        summary: str = "",
        status: str = "acknowledged",
    ) -> dict[str, Any]:
        now = utc_now()
        wanted = {str(message_id).strip() for message_id in (message_ids or []) if str(message_id).strip()}
        status = status.strip().lower().replace("-", "_") or "acknowledged"
        if status not in {"acknowledged", "superseded"}:
            status = "acknowledged"

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            metadata = json.loads(row["metadata_json"] or "{}")
            messages = metadata.get("operator_messages")
            if not isinstance(messages, list):
                messages = []
            acknowledged: list[dict[str, Any]] = []
            for entry in messages:
                if not isinstance(entry, dict):
                    continue
                mode = str(entry.get("mode") or "steer").strip().lower().replace("-", "_")
                if mode not in {"steer", "follow_up"}:
                    continue
                event_id = str(entry.get("event_id") or "")
                if wanted and event_id not in wanted:
                    continue
                if not wanted and not entry.get("claimed_at"):
                    continue
                if entry.get("acknowledged_at") or entry.get("superseded_at"):
                    continue
                if status == "superseded":
                    entry["superseded_at"] = now
                else:
                    entry["acknowledged_at"] = now
                if summary:
                    entry["acknowledgement_summary"] = summary.strip()
                acknowledged.append(dict(entry))
            metadata["operator_messages"] = messages[-200:]
            metadata["last_operator_context_ack"] = {
                "at": now,
                "status": status,
                "summary": summary.strip(),
                "message_ids": [entry.get("event_id") for entry in acknowledged if entry.get("event_id")],
                "count": len(acknowledged),
            }
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(metadata), job_id),
            )
            event = _insert_event(
                conn,
                job_id=job_id,
                event_type="operator_context",
                title=f"operator {status}",
                body=summary.strip() or f"{len(acknowledged)} operator message(s) {status}",
                metadata={
                    "status": status,
                    "message_ids": [entry.get("event_id") for entry in acknowledged if entry.get("event_id")],
                    "count": len(acknowledged),
                },
                created_at=now,
            )
            return {"event": event, "messages": acknowledged, "count": len(acknowledged), "status": status}

        return self._write(op)

    def rename_job(self, job_id: str, title: str) -> dict[str, Any]:
        now = utc_now()
        new_title = title.strip()
        if not new_title:
            raise ValueError("title is required")

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            conn.execute("UPDATE jobs SET title = ?, updated_at = ? WHERE id = ?", (new_title, now, job_id))
            _insert_event(
                conn,
                job_id=job_id,
                event_type="daemon",
                title="job renamed",
                body=f"{row['title']} -> {new_title}",
                metadata={"old_title": row["title"], "new_title": new_title},
                created_at=now,
            )
            updated = dict(row)
            updated["title"] = new_title
            updated["updated_at"] = now
            return _row_to_dict(updated)

        return self._write(op)

    def delete_job(self, job_id: str) -> dict[str, Any]:
        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            artifact_rows = conn.execute("SELECT path FROM artifacts WHERE job_id = ?", (job_id,)).fetchall()
            artifact_paths = [str(artifact["path"]) for artifact in artifact_rows if artifact["path"]]
            counts = {
                "evidence": conn.execute("SELECT COUNT(*) AS n FROM evidence WHERE job_id = ?", (job_id,)).fetchone()["n"],
                "artifacts": conn.execute("SELECT COUNT(*) AS n FROM artifacts WHERE job_id = ?", (job_id,)).fetchone()["n"],
                "memory": conn.execute("SELECT COUNT(*) AS n FROM memory_index WHERE job_id = ?", (job_id,)).fetchone()["n"],
                "steps": conn.execute("SELECT COUNT(*) AS n FROM steps WHERE job_id = ?", (job_id,)).fetchone()["n"],
                "runs": conn.execute("SELECT COUNT(*) AS n FROM job_runs WHERE job_id = ?", (job_id,)).fetchone()["n"],
                "events": conn.execute("SELECT COUNT(*) AS n FROM events WHERE job_id = ?", (job_id,)).fetchone()["n"],
            }
            conn.execute("DELETE FROM evidence WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM memory_index WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM steps WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM job_runs WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            return {
                "job": _row_to_dict(row),
                "artifact_paths": artifact_paths,
                "counts": counts,
            }

        return self._write(op)

    def append_operator_message(
        self,
        job_id: str,
        message: str,
        *,
        source: str = "operator",
        mode: str = "steer",
    ) -> dict[str, Any]:
        now = utc_now()
        text = message.strip()
        if not text:
            raise ValueError("message is required")
        mode = mode.strip().lower().replace("-", "_") or "steer"
        if mode not in {"steer", "follow_up", "note"}:
            mode = "steer"
        entry = {"at": now, "source": source, "mode": mode, "message": text}

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            event = _insert_event(
                conn,
                job_id=job_id,
                event_type="operator_message",
                title=source,
                body=text,
                metadata={"source": source, "mode": mode},
                created_at=now,
            )
            entry["event_id"] = event["id"]
            metadata = json.loads(row["metadata_json"] or "{}")
            messages = metadata.get("operator_messages")
            if not isinstance(messages, list):
                messages = []
            messages.append(entry)
            metadata["operator_messages"] = messages[-200:]
            metadata["last_operator_message"] = entry
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(metadata), job_id),
            )
            return entry

        return self._write(op)

    def append_agent_update(
        self,
        job_id: str,
        message: str,
        *,
        category: str = "progress",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        text = message.strip()
        if not text:
            raise ValueError("message is required")
        entry = {
            "at": now,
            "category": category.strip() or "progress",
            "message": text,
            "metadata": metadata or {},
        }

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            event = _insert_event(
                conn,
                job_id=job_id,
                event_type="agent_message",
                title=entry["category"],
                body=text,
                metadata=entry["metadata"],
                created_at=now,
            )
            entry["event_id"] = event["id"]
            job_metadata = json.loads(row["metadata_json"] or "{}")
            updates = job_metadata.get("agent_updates")
            if not isinstance(updates, list):
                updates = []
            updates.append(entry)
            job_metadata["agent_updates"] = updates[-100:]
            job_metadata["last_agent_update"] = entry
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(job_metadata), job_id),
            )
            return entry

        return self._write(op)

    def append_lesson(
        self,
        job_id: str,
        lesson: str,
        *,
        category: str = "memory",
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        text = lesson.strip()
        if not text:
            raise ValueError("lesson is required")
        entry = {
            "at": now,
            "category": category.strip().lower() or "memory",
            "key": _norm_key(f"{category}:{text}"),
            "lesson": text,
            "confidence": confidence,
            "metadata": metadata or {},
        }

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            job_metadata = json.loads(row["metadata_json"] or "{}")
            lessons = job_metadata.get("lessons")
            if not isinstance(lessons, list):
                lessons = []
            existing = next(
                (
                    item
                    for item in lessons
                    if isinstance(item, dict)
                    and (item.get("key") or _norm_key(f"{item.get('category', 'memory')}:{item.get('lesson', '')}"))
                    == entry["key"]
                ),
                None,
            )
            if existing is None:
                lessons.append(entry)
                current = entry
            else:
                existing["last_seen"] = now
                existing["seen_count"] = int(existing.get("seen_count") or 1) + 1
                if confidence is not None:
                    existing["confidence"] = confidence
                if metadata:
                    merged = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
                    merged.update(metadata)
                    existing["metadata"] = merged
                existing["key"] = entry["key"]
                current = existing
            event = _insert_event(
                conn,
                job_id=job_id,
                event_type="lesson",
                title=current.get("category") or "memory",
                body=current.get("lesson") or text,
                metadata={
                    "confidence": current.get("confidence"),
                    "seen_count": current.get("seen_count"),
                    **(current.get("metadata") if isinstance(current.get("metadata"), dict) else {}),
                },
                created_at=now,
            )
            current["event_id"] = event["id"]
            job_metadata["lessons"] = lessons[-200:]
            job_metadata["last_lesson"] = current
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(job_metadata), job_id),
            )
            return current

        return self._write(op)

    def append_source_record(
        self,
        job_id: str,
        source: str,
        *,
        source_type: str = "",
        usefulness_score: float | None = None,
        yield_count: int = 0,
        fail_count_delta: int = 0,
        warnings: list[str] | None = None,
        outcome: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        text = source.strip()
        if not text:
            raise ValueError("source is required")
        key = _norm_key(text)

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            job_metadata = json.loads(row["metadata_json"] or "{}")
            sources = _metadata_list(job_metadata, "source_ledger")
            current = next((entry for entry in sources if entry.get("key") == key), None)
            created = current is None
            if current is None:
                current = {
                    "key": key,
                    "source": text,
                    "source_type": source_type.strip() or "unknown",
                    "usefulness_score": 0.0,
                    "fail_count": 0,
                    "yield_count": 0,
                    "warnings": [],
                    "last_outcome": "",
                    "metadata": {},
                    "first_seen": now,
                }
                sources.append(current)
            if source_type:
                current["source_type"] = source_type.strip()
            if usefulness_score is not None:
                current["usefulness_score"] = float(usefulness_score)
            if yield_count:
                current["yield_count"] = int(current.get("yield_count") or 0) + int(yield_count)
            if fail_count_delta:
                current["fail_count"] = int(current.get("fail_count") or 0) + int(fail_count_delta)
            if warnings:
                merged = list(dict.fromkeys([*current.get("warnings", []), *[str(warning) for warning in warnings]]))
                current["warnings"] = merged[-20:]
            if outcome:
                current["last_outcome"] = outcome.strip()
            if metadata:
                merged_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
                merged_metadata.update(metadata)
                current["metadata"] = merged_metadata
            current["created"] = created
            current["last_seen"] = now
            event = _insert_event(
                conn,
                job_id=job_id,
                event_type="source",
                title=current.get("source") or text,
                body=current.get("last_outcome") or outcome,
                metadata={
                    "created": created,
                    "source_type": current.get("source_type"),
                    "usefulness_score": current.get("usefulness_score"),
                    "yield_count": current.get("yield_count"),
                    "fail_count": current.get("fail_count"),
                    "warnings": current.get("warnings") or [],
                    **(current.get("metadata") if isinstance(current.get("metadata"), dict) else {}),
                },
                created_at=now,
            )
            current["event_id"] = event["id"]
            job_metadata["source_ledger"] = sources[-250:]
            job_metadata["last_source_record"] = current
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(job_metadata), job_id),
            )
            return current

        return self._write(op)

    def append_finding_record(
        self,
        job_id: str,
        *,
        name: str,
        url: str = "",
        source_url: str = "",
        category: str = "",
        location: str = "",
        contact: str = "",
        reason: str = "",
        status: str = "new",
        score: float | None = None,
        evidence_artifact: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        url = url.strip()
        source_url = source_url.strip()
        key = _norm_key(f"{name}|{url or source_url}")

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            job_metadata = json.loads(row["metadata_json"] or "{}")
            findings = _metadata_list(job_metadata, "finding_ledger")
            current = next((entry for entry in findings if entry.get("key") == key), None)
            created = current is None
            if current is None:
                current = {
                    "key": key,
                    "name": name,
                    "url": url,
                    "source_url": source_url,
                    "category": category.strip(),
                    "location": location.strip(),
                    "contact": contact.strip(),
                    "reason": reason.strip(),
                    "status": status.strip() or "new",
                    "score": score,
                    "evidence_artifact": evidence_artifact.strip(),
                    "metadata": metadata or {},
                    "created_at": now,
                }
                findings.append(current)
            else:
                for field, value in {
                    "url": url,
                    "source_url": source_url,
                    "category": category.strip(),
                    "location": location.strip(),
                    "contact": contact.strip(),
                    "reason": reason.strip(),
                    "status": status.strip(),
                    "evidence_artifact": evidence_artifact.strip(),
                }.items():
                    if value:
                        current[field] = value
                if score is not None:
                    current["score"] = score
                if metadata:
                    merged_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
                    merged_metadata.update(metadata)
                    current["metadata"] = merged_metadata
            current["updated_at"] = now
            current["created"] = created
            event = _insert_event(
                conn,
                job_id=job_id,
                event_type="finding",
                title=current.get("name") or name,
                body=current.get("reason") or current.get("category") or "",
                metadata={
                    "created": created,
                    "score": current.get("score"),
                    "status": current.get("status"),
                    "source_url": current.get("source_url"),
                    "evidence_artifact": current.get("evidence_artifact"),
                    **(current.get("metadata") if isinstance(current.get("metadata"), dict) else {}),
                },
                created_at=now,
            )
            current["event_id"] = event["id"]
            job_metadata["finding_ledger"] = findings[-1000:]
            job_metadata["last_finding_record"] = current
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(job_metadata), job_id),
            )
            return current

        return self._write(op)

    def append_roadmap_record(
        self,
        job_id: str,
        *,
        title: str,
        status: str = "planned",
        objective: str = "",
        scope: str = "",
        current_milestone: str = "",
        validation_contract: str = "",
        milestones: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        title = title.strip()
        if not title:
            raise ValueError("title is required")
        status = _clean_status(status, {"planned", "active", "validating", "done", "blocked", "paused"}, "planned")
        milestone_items = milestones if isinstance(milestones, list) else []

        def merge_feature(existing_features: list[dict[str, Any]], feature: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
            feature_title = str(feature.get("title") or feature.get("name") or "").strip()
            if not feature_title:
                return None, False
            feature_key = _norm_key(str(feature.get("key") or feature_title))
            feature_title_key = _norm_key(feature_title)
            current = next(
                (
                    entry for entry in existing_features
                    if entry.get("key") == feature_key
                    or _norm_key(str(entry.get("title") or "")) == feature_title_key
                ),
                None,
            )
            created = current is None
            if current is None:
                current = {
                    "key": feature_key,
                    "title": feature_title,
                    "status": _clean_status(str(feature.get("status") or "planned"), {"planned", "active", "done", "blocked", "skipped"}, "planned"),
                    "goal": str(feature.get("goal") or feature.get("description") or "").strip(),
                    "output_contract": str(feature.get("output_contract") or feature.get("contract") or "").strip().lower().replace(" ", "_"),
                    "acceptance_criteria": str(feature.get("acceptance_criteria") or "").strip(),
                    "evidence_needed": str(feature.get("evidence_needed") or "").strip(),
                    "result": str(feature.get("result") or feature.get("outcome") or "").strip(),
                    "metadata": feature.get("metadata") if isinstance(feature.get("metadata"), dict) else {},
                    "created_at": now,
                }
                existing_features.append(current)
            else:
                current["status"] = _clean_status(str(feature.get("status") or current.get("status") or "planned"), {"planned", "active", "done", "blocked", "skipped"}, "planned")
                for field, value in {
                    "title": feature_title,
                    "goal": str(feature.get("goal") or feature.get("description") or "").strip(),
                    "output_contract": str(feature.get("output_contract") or feature.get("contract") or "").strip().lower().replace(" ", "_"),
                    "acceptance_criteria": str(feature.get("acceptance_criteria") or "").strip(),
                    "evidence_needed": str(feature.get("evidence_needed") or "").strip(),
                    "result": str(feature.get("result") or feature.get("outcome") or "").strip(),
                }.items():
                    if value:
                        current[field] = value
                if isinstance(feature.get("metadata"), dict):
                    merged = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
                    merged.update(feature["metadata"])
                    current["metadata"] = merged
            if current.get("output_contract") not in {"research", "artifact", "experiment", "action", "monitor", "decision", "report", "validation"}:
                current["output_contract"] = ""
            current["updated_at"] = now
            current["created"] = created
            return current, created

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT objective, metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            job_metadata = json.loads(row["metadata_json"] or "{}")
            roadmap = job_metadata.get("roadmap")
            created = not isinstance(roadmap, dict)
            if created:
                roadmap = {
                    "key": _norm_key(title),
                    "title": title,
                    "status": status,
                    "objective": objective.strip() or str(row["objective"] or "").strip(),
                    "scope": scope.strip(),
                    "validation_contract": validation_contract.strip(),
                    "current_milestone": current_milestone.strip(),
                    "milestones": [],
                    "metadata": metadata or {},
                    "created_at": now,
                }
            else:
                roadmap["title"] = title or roadmap.get("title") or "Roadmap"
                roadmap["status"] = status
                for field, value in {
                    "objective": objective.strip(),
                    "scope": scope.strip(),
                    "validation_contract": validation_contract.strip(),
                    "current_milestone": current_milestone.strip(),
                }.items():
                    if value:
                        roadmap[field] = value
                if metadata:
                    merged_metadata = roadmap.get("metadata") if isinstance(roadmap.get("metadata"), dict) else {}
                    merged_metadata.update(metadata)
                    roadmap["metadata"] = merged_metadata

            stored_milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
            added_milestones = 0
            updated_milestones = 0
            added_features = 0
            updated_features = 0
            touched: list[dict[str, Any]] = []
            for milestone in milestone_items[:100]:
                if not isinstance(milestone, dict):
                    continue
                milestone_title = str(milestone.get("title") or milestone.get("name") or "").strip()
                if not milestone_title:
                    continue
                milestone_key = _norm_key(str(milestone.get("key") or milestone_title))
                milestone_title_key = _norm_key(milestone_title)
                current = next(
                    (
                        entry for entry in stored_milestones
                        if entry.get("key") == milestone_key
                        or _norm_key(str(entry.get("title") or "")) == milestone_title_key
                    ),
                    None,
                )
                milestone_created = current is None
                if current is None:
                    current = {
                        "key": milestone_key,
                        "title": milestone_title,
                        "status": _clean_status(str(milestone.get("status") or "planned"), {"planned", "active", "validating", "done", "blocked", "skipped"}, "planned"),
                        "priority": int(milestone.get("priority") or 0),
                        "goal": str(milestone.get("goal") or milestone.get("description") or "").strip(),
                        "acceptance_criteria": str(milestone.get("acceptance_criteria") or "").strip(),
                        "evidence_needed": str(milestone.get("evidence_needed") or "").strip(),
                        "validation_status": _clean_status(str(milestone.get("validation_status") or "not_started"), {"not_started", "pending", "passed", "failed", "blocked"}, "not_started"),
                        "validation_result": str(milestone.get("validation_result") or "").strip(),
                        "next_action": str(milestone.get("next_action") or "").strip(),
                        "features": [],
                        "metadata": milestone.get("metadata") if isinstance(milestone.get("metadata"), dict) else {},
                        "created_at": now,
                    }
                    stored_milestones.append(current)
                    added_milestones += 1
                else:
                    updated_milestones += 1
                    current["status"] = _clean_status(str(milestone.get("status") or current.get("status") or "planned"), {"planned", "active", "validating", "done", "blocked", "skipped"}, "planned")
                    if "priority" in milestone:
                        current["priority"] = int(milestone.get("priority") or 0)
                    for field, value in {
                        "title": milestone_title,
                        "goal": str(milestone.get("goal") or milestone.get("description") or "").strip(),
                        "acceptance_criteria": str(milestone.get("acceptance_criteria") or "").strip(),
                        "evidence_needed": str(milestone.get("evidence_needed") or "").strip(),
                        "validation_status": _clean_status(str(milestone.get("validation_status") or ""), {"not_started", "pending", "passed", "failed", "blocked"}, ""),
                        "validation_result": str(milestone.get("validation_result") or "").strip(),
                        "next_action": str(milestone.get("next_action") or "").strip(),
                    }.items():
                        if value:
                            current[field] = value
                    if isinstance(milestone.get("metadata"), dict):
                        merged_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
                        merged_metadata.update(milestone["metadata"])
                        current["metadata"] = merged_metadata
                feature_items = milestone.get("features") if isinstance(milestone.get("features"), list) else []
                features = current.get("features") if isinstance(current.get("features"), list) else []
                for feature in feature_items[:100]:
                    if not isinstance(feature, dict):
                        continue
                    stored_feature, feature_created = merge_feature(features, feature)
                    if stored_feature is None:
                        continue
                    if feature_created:
                        added_features += 1
                    else:
                        updated_features += 1
                current["features"] = features[-500:]
                current["updated_at"] = now
                current["created"] = milestone_created
                touched.append(current)

            roadmap["milestones"] = stored_milestones[-500:]
            roadmap["updated_at"] = now
            roadmap["created"] = created
            roadmap["added_milestones"] = added_milestones
            roadmap["updated_milestones"] = updated_milestones
            roadmap["added_features"] = added_features
            roadmap["updated_features"] = updated_features
            event = _insert_event(
                conn,
                job_id=job_id,
                event_type="roadmap",
                title=roadmap.get("title") or title,
                body=f"{roadmap.get('status')} | milestones +{added_milestones}/~{updated_milestones} | features +{added_features}/~{updated_features}",
                metadata={
                    "created": created,
                    "status": roadmap.get("status"),
                    "current_milestone": roadmap.get("current_milestone"),
                    "milestone_count": len(roadmap.get("milestones") or []),
                    "added_milestones": added_milestones,
                    "updated_milestones": updated_milestones,
                    "added_features": added_features,
                    "updated_features": updated_features,
                },
                created_at=now,
            )
            roadmap["event_id"] = event["id"]
            job_metadata["roadmap"] = roadmap
            job_metadata["last_roadmap_record"] = {
                "at": now,
                "event_id": event["id"],
                "title": roadmap.get("title"),
                "status": roadmap.get("status"),
                "milestones": touched[-10:],
            }
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(job_metadata), job_id),
            )
            return roadmap

        return self._write(op)

    def append_milestone_validation_record(
        self,
        job_id: str,
        *,
        milestone: str,
        validation_status: str = "pending",
        result: str = "",
        evidence: str = "",
        issues: list[str] | None = None,
        next_action: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        milestone = milestone.strip()
        if not milestone:
            raise ValueError("milestone is required")
        validation_status = _clean_status(validation_status, {"pending", "passed", "failed", "blocked"}, "pending")
        issue_values = [str(issue).strip() for issue in (issues or []) if str(issue).strip()]
        milestone_key = _norm_key(milestone)

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT objective, metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            job_metadata = json.loads(row["metadata_json"] or "{}")
            roadmap = job_metadata.get("roadmap")
            if not isinstance(roadmap, dict):
                roadmap = {
                    "key": _norm_key(str(row["objective"] or "roadmap")),
                    "title": "Roadmap",
                    "status": "active",
                    "objective": str(row["objective"] or ""),
                    "scope": "",
                    "validation_contract": "",
                    "current_milestone": milestone,
                    "milestones": [],
                    "metadata": {},
                    "created_at": now,
                }
            milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
            current = next(
                (
                    entry for entry in milestones
                    if entry.get("key") == milestone_key
                    or _norm_key(str(entry.get("title") or "")) == milestone_key
                ),
                None,
            )
            created = current is None
            if current is None:
                current = {
                    "key": milestone_key,
                    "title": milestone,
                    "status": "validating" if validation_status == "pending" else ("done" if validation_status == "passed" else "blocked"),
                    "priority": 0,
                    "goal": "",
                    "acceptance_criteria": "",
                    "evidence_needed": "",
                    "features": [],
                    "metadata": {},
                    "created_at": now,
                }
                milestones.append(current)
            current["validation_status"] = validation_status
            current["validation_result"] = result.strip()
            current["validation_evidence"] = evidence.strip()
            current["validation_issues"] = issue_values
            current["next_action"] = next_action.strip()
            if validation_status == "passed":
                current["status"] = "done"
            elif validation_status == "pending":
                current["status"] = "validating"
            elif validation_status in {"failed", "blocked"}:
                current["status"] = "blocked"
            if metadata:
                merged_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
                merged_metadata.update(metadata)
                current["metadata"] = merged_metadata
            current["updated_at"] = now
            current["created"] = created
            roadmap["milestones"] = milestones[-500:]
            roadmap["status"] = "active" if validation_status in {"failed", "blocked"} else ("validating" if validation_status == "pending" else roadmap.get("status") or "active")
            roadmap["current_milestone"] = current.get("title") or milestone
            roadmap["updated_at"] = now
            event = _insert_event(
                conn,
                job_id=job_id,
                event_type="milestone_validation",
                title=current.get("title") or milestone,
                body=result.strip() or validation_status,
                metadata={
                    "created": created,
                    "validation_status": validation_status,
                    "evidence": evidence.strip(),
                    "issues": issue_values,
                    "next_action": next_action.strip(),
                    **(metadata or {}),
                },
                created_at=now,
            )
            current["validation_event_id"] = event["id"]
            job_metadata["roadmap"] = roadmap
            job_metadata["last_milestone_validation"] = {
                "at": now,
                "event_id": event["id"],
                "milestone": current.get("title"),
                "validation_status": validation_status,
                "result": result.strip(),
                "issues": issue_values,
                "next_action": next_action.strip(),
            }
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(job_metadata), job_id),
            )
            return current

        return self._write(op)

    def append_task_record(
        self,
        job_id: str,
        *,
        title: str,
        status: str = "open",
        priority: int = 0,
        goal: str = "",
        source_hint: str = "",
        result: str = "",
        parent: str = "",
        output_contract: str = "",
        acceptance_criteria: str = "",
        evidence_needed: str = "",
        stall_behavior: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        title = title.strip()
        if not title:
            raise ValueError("title is required")
        status = (status.strip().lower() or "open").replace(" ", "_")
        if status not in {"open", "active", "done", "blocked", "skipped"}:
            status = "open"
        output_contract = output_contract.strip().lower().replace(" ", "_")
        if output_contract not in {"research", "artifact", "experiment", "action", "monitor", "decision", "report"}:
            output_contract = ""
        key = _norm_key(f"{parent}|{title}")

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            job_metadata = json.loads(row["metadata_json"] or "{}")
            tasks = _metadata_list(job_metadata, "task_queue")
            current = next(
                (
                    entry
                    for entry in tasks
                    if entry.get("key") == key
                    or (
                        not entry.get("key")
                        and _norm_key(f"{entry.get('parent') or ''}|{entry.get('title') or ''}") == key
                    )
                ),
                None,
            )
            created = current is None
            if current is None:
                current = {
                    "key": key,
                    "title": title,
                    "status": status,
                    "priority": int(priority),
                    "goal": goal.strip(),
                    "source_hint": source_hint.strip(),
                    "result": result.strip(),
                    "parent": parent.strip(),
                    "output_contract": output_contract,
                    "acceptance_criteria": acceptance_criteria.strip(),
                    "evidence_needed": evidence_needed.strip(),
                    "stall_behavior": stall_behavior.strip(),
                    "metadata": metadata or {},
                    "created_at": now,
                }
                tasks.append(current)
            else:
                current["status"] = status
                current["priority"] = int(priority)
                for field, value in {
                    "goal": goal.strip(),
                    "source_hint": source_hint.strip(),
                    "result": result.strip(),
                    "parent": parent.strip(),
                    "output_contract": output_contract,
                    "acceptance_criteria": acceptance_criteria.strip(),
                    "evidence_needed": evidence_needed.strip(),
                    "stall_behavior": stall_behavior.strip(),
                }.items():
                    if value:
                        current[field] = value
                if metadata:
                    merged_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
                    merged_metadata.update(metadata)
                    current["metadata"] = merged_metadata
            current["updated_at"] = now
            current["created"] = created
            event = _insert_event(
                conn,
                job_id=job_id,
                event_type="task",
                title=current.get("title") or title,
                body=current.get("result") or current.get("goal") or "",
                metadata={
                    "created": created,
                    "status": current.get("status"),
                    "priority": current.get("priority"),
                    "parent": current.get("parent"),
                    "source_hint": current.get("source_hint"),
                    "output_contract": current.get("output_contract"),
                    "acceptance_criteria": current.get("acceptance_criteria"),
                    "evidence_needed": current.get("evidence_needed"),
                    "stall_behavior": current.get("stall_behavior"),
                    **(current.get("metadata") if isinstance(current.get("metadata"), dict) else {}),
                },
                created_at=now,
            )
            current["event_id"] = event["id"]
            job_metadata["task_queue"] = tasks[-500:]
            job_metadata["last_task_record"] = current
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(job_metadata), job_id),
            )
            return current

        return self._write(op)

    def append_experiment_record(
        self,
        job_id: str,
        *,
        title: str,
        hypothesis: str = "",
        status: str = "planned",
        metric_name: str = "",
        metric_value: float | None = None,
        metric_unit: str = "",
        higher_is_better: bool = True,
        baseline_value: float | None = None,
        config: dict[str, Any] | None = None,
        result: str = "",
        evidence_artifact: str = "",
        next_action: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        title = title.strip()
        if not title:
            raise ValueError("title is required")
        status = (status.strip().lower() or "planned").replace(" ", "_")
        if status not in {"planned", "running", "measured", "failed", "blocked", "skipped"}:
            status = "planned"
        config_value = config if isinstance(config, dict) else {}
        key = _norm_key(f"{title}|{_json_dumps(config_value)}")

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            job_metadata = json.loads(row["metadata_json"] or "{}")
            experiments = _metadata_list(job_metadata, "experiment_ledger")
            current = next((entry for entry in experiments if entry.get("key") == key), None)
            created = current is None
            previous_best = _best_experiment_for_metric(
                experiments,
                metric_name=metric_name,
                metric_unit=metric_unit,
                higher_is_better=higher_is_better,
                exclude_key=key,
            )
            if current is None:
                current = {
                    "key": key,
                    "title": title,
                    "hypothesis": hypothesis.strip(),
                    "status": status,
                    "metric_name": metric_name.strip(),
                    "metric_value": metric_value,
                    "metric_unit": metric_unit.strip(),
                    "higher_is_better": bool(higher_is_better),
                    "baseline_value": baseline_value,
                    "config": config_value,
                    "result": result.strip(),
                    "evidence_artifact": evidence_artifact.strip(),
                    "next_action": next_action.strip(),
                    "metadata": metadata or {},
                    "created_at": now,
                }
                experiments.append(current)
            else:
                current["status"] = status
                for field, value in {
                    "hypothesis": hypothesis.strip(),
                    "metric_name": metric_name.strip(),
                    "metric_unit": metric_unit.strip(),
                    "result": result.strip(),
                    "evidence_artifact": evidence_artifact.strip(),
                    "next_action": next_action.strip(),
                }.items():
                    if value:
                        current[field] = value
                current["higher_is_better"] = bool(higher_is_better)
                if metric_value is not None:
                    current["metric_value"] = metric_value
                if baseline_value is not None:
                    current["baseline_value"] = baseline_value
                if config_value:
                    current["config"] = config_value
                if metadata:
                    merged_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
                    merged_metadata.update(metadata)
                    current["metadata"] = merged_metadata
            current["updated_at"] = now
            current["created"] = created
            current["delta_from_previous_best"] = _metric_delta(
                metric_value=current.get("metric_value"),
                previous_best=previous_best,
                higher_is_better=bool(current.get("higher_is_better", True)),
            )
            best = _mark_best_experiments(experiments)
            event_body = current.get("result") or ""
            if current.get("metric_value") is not None:
                event_body = format_metric_value(
                    current.get("metric_name") or "metric",
                    current.get("metric_value"),
                    current.get("metric_unit") or "",
                )
                if current.get("delta_from_previous_best") is not None:
                    event_body += f" delta={current.get('delta_from_previous_best')}"
                if current.get("result"):
                    event_body += f" | {current.get('result')}"
            event = _insert_event(
                conn,
                job_id=job_id,
                event_type="experiment",
                title=current.get("title") or title,
                body=event_body,
                metadata={
                    "created": created,
                    "status": current.get("status"),
                    "metric_name": current.get("metric_name"),
                    "metric_value": current.get("metric_value"),
                    "metric_unit": current.get("metric_unit"),
                    "higher_is_better": current.get("higher_is_better"),
                    "best_observed": current.get("best_observed"),
                    "delta_from_previous_best": current.get("delta_from_previous_best"),
                    "evidence_artifact": current.get("evidence_artifact"),
                    **(current.get("metadata") if isinstance(current.get("metadata"), dict) else {}),
                },
                created_at=now,
            )
            current["event_id"] = event["id"]
            job_metadata["experiment_ledger"] = experiments[-1000:]
            job_metadata["last_experiment_record"] = current
            if best:
                job_metadata["best_experiment_record"] = best
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(job_metadata), job_id),
            )
            return current

        return self._write(op)

    def append_reflection(
        self,
        job_id: str,
        summary: str,
        *,
        strategy: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        text = summary.strip()
        if not text:
            raise ValueError("summary is required")
        entry = {
            "at": now,
            "summary": text,
            "strategy": strategy.strip(),
            "metadata": metadata or {},
        }

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            event = _insert_event(
                conn,
                job_id=job_id,
                event_type="reflection",
                title="reflection",
                body=text,
                metadata={"strategy": strategy.strip(), **(metadata or {})},
                created_at=now,
            )
            entry["event_id"] = event["id"]
            job_metadata = json.loads(row["metadata_json"] or "{}")
            reflections = _metadata_list(job_metadata, "reflections")
            reflections.append(entry)
            job_metadata["reflections"] = reflections[-100:]
            job_metadata["last_reflection"] = entry
            conn.execute(
                "UPDATE jobs SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (now, _json_dumps(job_metadata), job_id),
            )
            return entry

        return self._write(op)

    def start_run(self, job_id: str, *, model: str = "", config_hash: str = "") -> str:
        run_id = new_id("run")
        now = utc_now()

        def op(conn: sqlite3.Connection) -> str:
            conn.execute(
                """
                INSERT INTO job_runs(id, job_id, status, started_at, model, config_hash)
                VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (run_id, job_id, now, model, config_hash),
            )
            conn.execute("UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?", (now, job_id))
            _insert_event(
                conn,
                job_id=job_id,
                event_type="daemon",
                title="run started",
                body=f"model={model}" if model else "",
                ref_table="job_runs",
                ref_id=run_id,
                metadata={"model": model, "config_hash": config_hash},
                created_at=now,
            )
            return run_id

        return self._write(op)

    def finish_run(self, run_id: str, status: str, *, score: float | None = None, error: str | None = None) -> None:
        now = utc_now()

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE job_runs SET status = ?, ended_at = ?, score = ?, error = ? WHERE id = ?",
                (status, now, score, error, run_id),
            )

        self._write(op)

    def mark_interrupted_running(self, *, reason: str = "daemon interrupted active work") -> dict[str, int]:
        now = utc_now()
        output = {"success": False, "error": reason, "error_type": "Interrupted"}

        def op(conn: sqlite3.Connection) -> dict[str, int]:
            step_result = conn.execute(
                """
                UPDATE steps
                SET status = 'failed',
                    ended_at = ?,
                    summary = COALESCE(summary, ?),
                    output_json = ?,
                    error = ?
                WHERE status = 'running'
                """,
                (now, reason, _json_dumps(output), reason),
            )
            run_result = conn.execute(
                """
                UPDATE job_runs
                SET status = 'failed',
                    ended_at = ?,
                    error = ?
                WHERE status = 'running'
                """,
                (now, reason),
            )
            return {"steps": int(step_result.rowcount or 0), "runs": int(run_result.rowcount or 0)}

        return self._write(op)

    def next_step_no(self, job_id: str) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(step_no), 0) + 1 AS next_step FROM steps WHERE job_id = ?", (job_id,)).fetchone()
        return int(row["next_step"])

    def add_step(
        self,
        *,
        job_id: str,
        run_id: str,
        kind: str,
        status: str = "running",
        tool_name: str | None = None,
        summary: str | None = None,
        input_data: dict[str, Any] | None = None,
    ) -> str:
        step_id = new_id("step")
        step_no = self.next_step_no(job_id)
        now = utc_now()

        def op(conn: sqlite3.Connection) -> str:
            conn.execute(
                """
                INSERT INTO steps(id, job_id, run_id, step_no, kind, status, tool_name, started_at, summary, input_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (step_id, job_id, run_id, step_no, kind, status, tool_name, now, summary, _json_dumps(input_data)),
            )
            _insert_event(
                conn,
                job_id=job_id,
                event_type="tool_call" if tool_name else kind,
                title=tool_name or kind,
                body=summary or "",
                ref_table="steps",
                ref_id=step_id,
                metadata={"run_id": run_id, "step_no": step_no, "kind": kind, "status": status, "input": input_data or {}},
                created_at=now,
            )
            return step_id

        return self._write(op)

    def finish_step(
        self,
        step_id: str,
        *,
        status: str,
        summary: str | None = None,
        output_data: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()

        def op(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT job_id, run_id, step_no, kind, tool_name FROM steps WHERE id = ?", (step_id,)).fetchone()
            conn.execute(
                """
                UPDATE steps
                SET status = ?, ended_at = ?, summary = COALESCE(?, summary), output_json = ?, error = ?
                WHERE id = ?
                """,
                (status, now, summary, _json_dumps(output_data), error, step_id),
            )
            if row is not None:
                event_type = "error" if status == "failed" or error else "tool_result"
                if row["kind"] == "reflection" and not error:
                    event_type = "reflection"
                _insert_event(
                    conn,
                    job_id=row["job_id"],
                    event_type=event_type,
                    title=row["tool_name"] or row["kind"],
                    body=summary or error or "",
                    ref_table="steps",
                    ref_id=step_id,
                    metadata={
                        "run_id": row["run_id"],
                        "step_no": row["step_no"],
                        "kind": row["kind"],
                        "status": status,
                        "output": output_data or {},
                        "error": error,
                    },
                    created_at=now,
                )

        self._write(op)

    def list_steps(self, *, job_id: str | None = None, run_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        if run_id:
            if limit:
                rows = self._conn.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM steps WHERE run_id = ? ORDER BY step_no DESC LIMIT ?
                    ) ORDER BY step_no
                    """,
                    (run_id, int(limit)),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY step_no", (run_id,)).fetchall()
        elif job_id:
            if limit:
                rows = self._conn.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM steps WHERE job_id = ? ORDER BY step_no DESC LIMIT ?
                    ) ORDER BY step_no
                    """,
                    (job_id, int(limit)),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM steps WHERE job_id = ? ORDER BY started_at", (job_id,)).fetchall()
        else:
            if limit:
                rows = self._conn.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM steps ORDER BY started_at DESC LIMIT ?
                    ) ORDER BY started_at
                    """,
                    (int(limit),),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM steps ORDER BY started_at").fetchall()
        return [_row_to_dict(row) for row in rows]

    def job_record_counts(self, job_id: str) -> dict[str, int]:
        row = self._conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM steps WHERE job_id = ?) AS steps,
                (SELECT COUNT(*) FROM artifacts WHERE job_id = ?) AS artifacts,
                (SELECT COUNT(*) FROM memory_index WHERE job_id = ?) AS memory,
                (SELECT COUNT(*) FROM events WHERE job_id = ?) AS events
            """,
            (job_id, job_id, job_id, job_id),
        ).fetchone()
        return {
            "steps": int(row["steps"] or 0),
            "artifacts": int(row["artifacts"] or 0),
            "memory": int(row["memory"] or 0),
            "events": int(row["events"] or 0),
        }

    def job_token_usage(self, job_id: str) -> dict[str, Any]:
        rows = self._conn.execute(
            """
            SELECT created_at, metadata_json
            FROM events
            WHERE job_id = ? AND event_type = 'loop' AND title = 'message_end'
            ORDER BY created_at ASC, id ASC
            """,
            (job_id,),
        ).fetchall()
        totals: dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cost": 0.0,
            "calls": 0,
            "estimated_calls": 0,
            "latest_prompt_tokens": 0,
            "latest_completion_tokens": 0,
            "latest_total_tokens": 0,
            "latest_context_length": 0,
            "latest_context_fraction": 0.0,
            "latest_at": "",
            "has_cost": False,
        }
        for row in rows:
            metadata = _json_loads(row["metadata_json"])
            usage = metadata.get("usage")
            if not isinstance(usage, dict):
                continue
            prompt = _as_int(usage.get("prompt_tokens"))
            completion = _as_int(usage.get("completion_tokens"))
            total = _as_int(usage.get("total_tokens")) or prompt + completion
            totals["prompt_tokens"] += prompt
            totals["completion_tokens"] += completion
            totals["total_tokens"] += total
            totals["reasoning_tokens"] += _as_int(_nested_value(usage, "completion_tokens_details", "reasoning_tokens"))
            totals["cached_tokens"] += _as_int(_nested_value(usage, "prompt_tokens_details", "cached_tokens"))
            cost = _as_float(usage.get("cost"))
            if cost is not None:
                totals["cost"] += cost
                totals["has_cost"] = True
            totals["calls"] += 1
            if bool(usage.get("estimated")):
                totals["estimated_calls"] += 1
            totals["latest_prompt_tokens"] = prompt
            totals["latest_completion_tokens"] = completion
            totals["latest_total_tokens"] = total
            totals["latest_context_length"] = _as_int(usage.get("context_length"))
            totals["latest_context_fraction"] = _as_float(usage.get("context_fraction")) or 0.0
            totals["latest_at"] = str(row["created_at"] or "")
        return totals

    def list_runs(self, job_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM job_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def add_artifact(
        self,
        *,
        job_id: str,
        path: str | Path,
        sha256: str,
        artifact_type: str,
        run_id: str | None = None,
        step_id: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        artifact_id = new_id("art")
        now = utc_now()

        def op(conn: sqlite3.Connection) -> str:
            conn.execute(
                """
                INSERT INTO artifacts(id, job_id, run_id, step_id, type, path, sha256, title, summary, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    job_id,
                    run_id,
                    step_id,
                    artifact_type,
                    str(path),
                    sha256,
                    title,
                    summary,
                    _json_dumps(metadata),
                    now,
                ),
            )
            _insert_event(
                conn,
                job_id=job_id,
                event_type="artifact",
                title=title or artifact_id,
                body=summary or str(path),
                ref_table="artifacts",
                ref_id=artifact_id,
                metadata={"type": artifact_type, "path": str(path), "sha256": sha256, **(metadata or {})},
                created_at=now,
            )
            return artifact_id

        return self._write(op)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        artifact = _row_to_dict(row)
        if artifact is None:
            raise KeyError(f"Artifact not found: {artifact_id}")
        return artifact

    def list_artifacts(self, job_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE job_id = ? ORDER BY created_at DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def upsert_memory(
        self,
        *,
        job_id: str,
        key: str,
        summary: str,
        artifact_refs: list[str] | None = None,
    ) -> str:
        memory_id = new_id("mem")
        now = utc_now()

        def op(conn: sqlite3.Connection) -> str:
            conn.execute(
                """
                INSERT INTO memory_index(id, job_id, key, summary, artifact_refs_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, key) DO UPDATE SET
                    summary = excluded.summary,
                    artifact_refs_json = excluded.artifact_refs_json,
                    updated_at = excluded.updated_at
                """,
                (memory_id, job_id, key, summary, _json_dumps(artifact_refs or []), now),
            )
            row = conn.execute("SELECT id FROM memory_index WHERE job_id = ? AND key = ?", (job_id, key)).fetchone()
            current_id = str(row["id"])
            _insert_event(
                conn,
                job_id=job_id,
                event_type="compaction",
                title=key,
                body=summary,
                ref_table="memory_index",
                ref_id=current_id,
                metadata={"artifact_refs": artifact_refs or []},
                created_at=now,
            )
            return current_id

        return self._write(op)

    def list_memory(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM memory_index WHERE job_id = ? ORDER BY updated_at DESC",
            (job_id,),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def digest_exists(self, *, day: str, target: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM digests WHERE day = ? AND target = ? AND status IN ('sent', 'dry_run') LIMIT 1",
            (day, target),
        ).fetchone()
        return row is not None

    def record_digest(
        self,
        *,
        day: str,
        target: str,
        subject: str,
        body_path: str | Path,
        status: str,
        error: str | None = None,
    ) -> str:
        digest_id = new_id("dig")
        sent_at = utc_now() if status in {"sent", "dry_run"} else None

        def op(conn: sqlite3.Connection) -> str:
            conn.execute(
                """
                INSERT INTO digests(id, day, target, subject, body_path, sent_at, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (digest_id, day, target, subject, str(body_path), sent_at, status, error),
            )
            _insert_event(
                conn,
                job_id=None,
                event_type="digest",
                title=subject,
                body=str(body_path),
                ref_table="digests",
                ref_id=digest_id,
                metadata={"day": day, "target": target, "status": status, "error": error},
                created_at=sent_at or utc_now(),
            )
            return digest_id

        return self._write(op)
