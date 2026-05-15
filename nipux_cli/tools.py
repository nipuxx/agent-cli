"""Static tool registry for the Nipux agent."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import AppConfig
from nipux_cli.db import AgentDB
from nipux_cli.metric_format import format_metric_value
from nipux_cli.digest import send_digest_email
from nipux_cli.memory_graph import memory_graph_from_job, search_memory_graph
from nipux_cli.shell_tools import shell_exec as _shell_exec
from nipux_cli.shell_tools import write_file as _write_file


@dataclass(frozen=True)
class ToolContext:
    config: AppConfig
    db: AgentDB
    artifacts: ArtifactStore
    job_id: str
    run_id: str | None = None
    step_id: str | None = None
    task_id: str | None = None


Handler = Callable[[dict[str, Any], ToolContext], str]

EVIDENCE_OUTPUT_TERMS = {
    "audit",
    "checkpoint",
    "evidence",
    "extract",
    "extracted",
    "notes",
    "source",
    "sources",
}
DELIVERABLE_OUTPUT_TERMS = {
    "compiled",
    "deliverable",
    "draft",
    "final",
    "revision",
    "updated",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _missing_argument(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return True
        lowered = stripped.lower()
        if lowered in {"...", "…", "<...>", "{...}", "{{...}}", "placeholder", "todo", "tbd"}:
            return True
        if re.fullmatch(r"[.<{\[(\s]*\.{3,}[\s>}\])]*", stripped):
            return True
        return False
    if isinstance(value, (list, dict, tuple, set)):
        return not value
    return False


REFERENCE_LIKE_FIELD_PATTERN = re.compile(r"(?i)(?:^|_)(?:artifact|id|path|ref|source|url)(?:$|_)")


def _placeholder_argument(value: Any) -> bool:
    if _missing_argument(value):
        return True
    if not isinstance(value, str):
        return False
    stripped = value.strip().strip("'\"")
    if not stripped:
        return True
    if re.search(r"\s", stripped):
        return False
    return bool(re.search(r"(?:\.{3,}|…)$", stripped))


def _schema_placeholder_arguments(schema: dict[str, Any], value: Any, *, path: str = "") -> list[str]:
    schema_type = schema.get("type")
    placeholders: list[str] = []
    if schema_type == "object" and isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for name, child_schema in properties.items():
            if name not in value or not isinstance(child_schema, dict):
                continue
            child_path = f"{path}.{name}" if path else str(name)
            if REFERENCE_LIKE_FIELD_PATTERN.search(str(name)) and _placeholder_argument(value.get(name)):
                placeholders.append(child_path)
                continue
            placeholders.extend(_schema_placeholder_arguments(child_schema, value.get(name), path=child_path))
    elif schema_type == "array" and isinstance(value, list):
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        for index, item in enumerate(value[:50]):
            placeholders.extend(_schema_placeholder_arguments(item_schema, item, path=f"{path}[{index}]"))
    return placeholders


def _schema_missing_arguments(schema: dict[str, Any], value: Any, *, path: str = "") -> list[str]:
    schema_type = schema.get("type")
    missing: list[str] = []
    if schema_type == "object" and isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for required in schema.get("required") or []:
            name = str(required)
            if _missing_argument(value.get(name)):
                missing.append(f"{path}.{name}" if path else name)
        for name, child_schema in properties.items():
            if name in value and isinstance(child_schema, dict) and not _missing_argument(value.get(name)):
                child_path = f"{path}.{name}" if path else str(name)
                missing.extend(_schema_missing_arguments(child_schema, value.get(name), path=child_path))
    elif schema_type == "array" and isinstance(value, list):
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        for index, item in enumerate(value[:50]):
            missing.extend(_schema_missing_arguments(item_schema, item, path=f"{path}[{index}]"))
    return missing


REQUIRED_ARGUMENT_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "record_experiment": {
        "title": ("title", "name", "metric_name", "hypothesis", "result", "outcome"),
    },
}

REQUIRED_ARGUMENT_GROUPS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "read_artifact": (("artifact reference", ("artifact_id", "path", "title", "ref")),),
    "record_memory_graph": (("nodes or edges", ("nodes", "edges")),),
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_artifact(args: dict[str, Any], ctx: ToolContext) -> str:
    content = str(args.get("content") or "")
    if not content:
        return _json({"success": False, "error": "content is required"})
    stored = ctx.artifacts.write_text(
        job_id=ctx.job_id,
        run_id=ctx.run_id,
        step_id=ctx.step_id,
        content=content,
        title=args.get("title"),
        summary=args.get("summary"),
        artifact_type=str(args.get("type") or "text"),
        metadata=args.get("metadata") if isinstance(args.get("metadata"), dict) else None,
    )
    return _json({
        "success": True,
        "artifact_id": stored.id,
        "path": str(stored.path),
        "sha256": stored.sha256,
    })


def _read_artifact(args: dict[str, Any], ctx: ToolContext) -> str:
    artifact_ref = str(args.get("artifact_id") or args.get("path") or args.get("title") or args.get("ref") or "")
    if not artifact_ref:
        return _json({"success": False, "error": "artifact_id is required"})
    resolved = _resolve_artifact_ref(ctx, artifact_ref)
    if not resolved:
        recent = _recent_artifact_refs(ctx)
        return _json({
            "success": False,
            "error": f"artifact not found: {artifact_ref}",
            "guidance": (
                "The requested artifact reference does not exist. Use one of the recent_artifacts refs, "
                "call search_artifacts with a concrete query, or continue from already observed evidence."
            ),
            "recent_artifacts": recent,
        })
    try:
        content = ctx.artifacts.read_text(resolved["id"])
    except (OSError, KeyError, ValueError) as exc:
        return _json({"success": False, "artifact_id": resolved["id"], "error": str(exc)})
    return _json({"success": True, "artifact_id": resolved["id"], "title": resolved.get("title"), "path": resolved.get("path"), "content": content})


def _recent_artifact_refs(ctx: ToolContext, limit: int = 8) -> list[dict[str, str]]:
    artifacts = ctx.db.list_artifacts(ctx.job_id, limit=limit)
    refs: list[dict[str, str]] = []
    for index, artifact in enumerate(artifacts, start=1):
        refs.append({
            "number": str(index),
            "id": str(artifact.get("id") or ""),
            "title": str(artifact.get("title") or ""),
            "path": str(artifact.get("path") or ""),
        })
    return refs


def _resolve_artifact_ref(ctx: ToolContext, artifact_ref: str) -> dict[str, Any] | None:
    ref = artifact_ref.strip().strip("'\"")
    if not ref:
        return None
    artifacts = ctx.db.list_artifacts(ctx.job_id, limit=250)
    for artifact in artifacts:
        if ref == artifact.get("id") or ref == str(artifact.get("path") or ""):
            return artifact
    if ref.isdigit():
        index = int(ref) - 1
        if 0 <= index < len(artifacts):
            return artifacts[index]
    lowered = ref.lower()
    for artifact in artifacts:
        title = str(artifact.get("title") or "").lower()
        if lowered == title:
            return artifact
    for artifact in artifacts:
        haystack = " ".join(str(artifact.get(key) or "") for key in ("title", "summary", "path")).lower()
        if lowered and lowered in haystack:
            return artifact
    return None


def _search_artifacts(args: dict[str, Any], ctx: ToolContext) -> str:
    query = str(args.get("query") or "")
    limit = int(args.get("limit") or 10)
    return _json({"success": True, "results": ctx.artifacts.search_text(job_id=ctx.job_id, query=query, limit=limit)})


def _update_job_state(args: dict[str, Any], ctx: ToolContext) -> str:
    status = str(args.get("status") or "").strip().lower()
    if status in {"paused", "cancelled", "completed", "failed"}:
        note = str(args.get("note") or "")
        follow_up_task = None
        if status == "completed":
            follow_up_task = _append_completion_audit_task(
                ctx,
                source="update_job_state",
                requested_status=status,
                claimed_message=note,
            )
        metadata = {"requested_status": status, "kept_running": True}
        if follow_up_task is not None:
            metadata["follow_up_task"] = follow_up_task.get("key")
        ctx.db.append_agent_update(
            ctx.job_id,
            f"Worker requested {status}; job remains running. {note}".strip(),
            category="progress" if status == "completed" else "blocked",
            metadata=metadata,
        )
        result = {
            "success": True,
            "job_id": ctx.job_id,
            "status": "running",
            "requested_status": status,
            "kept_running": True,
            "guidance": (
                "Jobs are perpetual by default. Do not mark the job complete or failed. "
                "Save the current result, create follow-up tasks, report a checkpoint, and continue."
            ),
        }
        if follow_up_task is not None:
            result["follow_up_task"] = follow_up_task
        return _json(result)
    if status not in {"queued", "running"}:
        return _json({"success": False, "error": f"invalid status: {status}"})
    note = str(args.get("note") or "")
    patch = {"last_note": note} if note else None
    ctx.db.update_job_status(ctx.job_id, status, metadata_patch=patch)
    return _json({"success": True, "job_id": ctx.job_id, "status": status})


def _defer_job(args: dict[str, Any], ctx: ToolContext) -> str:
    until = _defer_until(args)
    reason = str(args.get("reason") or "").strip()
    next_action = str(args.get("next_action") or "").strip()
    patch = {
        "defer_until": until.isoformat(),
        "defer_reason": reason,
        "defer_next_action": next_action,
    }
    job = ctx.db.get_job(ctx.job_id)
    status = str(job.get("status") or "queued")
    if status not in {"queued", "running"}:
        status = "queued"
    ctx.db.update_job_status(ctx.job_id, status, metadata_patch=patch)
    message = f"Deferred until {until.isoformat()}"
    if reason:
        message += f": {reason}"
    if next_action:
        message += f" Next: {next_action}"
    ctx.db.append_agent_update(
        ctx.job_id,
        message,
        category="progress",
        metadata={"defer_until": until.isoformat(), "reason": reason, "next_action": next_action},
    )
    return _json({
        "success": True,
        "job_id": ctx.job_id,
        "status": status,
        "defer_until": until.isoformat(),
        "reason": reason,
        "next_action": next_action,
    })


def _defer_until(args: dict[str, Any]) -> datetime:
    raw_until = str(args.get("until") or "").strip()
    if raw_until:
        try:
            parsed = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    seconds = args.get("seconds", args.get("delay_seconds", 300))
    try:
        delay = max(1.0, float(seconds))
    except (TypeError, ValueError):
        delay = 300.0
    return datetime.now(timezone.utc) + timedelta(seconds=delay)


def _report_update(args: dict[str, Any], ctx: ToolContext) -> str:
    message = str(args.get("message") or args.get("summary") or "").strip()
    if not message:
        return _json({"success": False, "error": "message is required"})
    category = str(args.get("category") or "progress").strip().lower()
    metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
    normalized_message = _perpetual_checkpoint_message(message)
    if normalized_message != message:
        metadata = {**metadata, "original_message": message, "rewritten_completion_claim": True}
        message = normalized_message
        follow_up_task = _append_completion_audit_task(
            ctx,
            source="report_update",
            requested_status="completed",
            claimed_message=str(metadata.get("original_message") or ""),
        )
        metadata["follow_up_task"] = follow_up_task.get("key")
    entry = ctx.db.append_agent_update(ctx.job_id, message, category=category, metadata=metadata)
    return _json({"success": True, "job_id": ctx.job_id, "update": entry})


def _append_completion_audit_task(
    ctx: ToolContext,
    *,
    source: str,
    requested_status: str,
    claimed_message: str = "",
) -> dict[str, Any]:
    return ctx.db.append_task_record(
        ctx.job_id,
        title="Audit latest checkpoint against objective",
        status="open",
        priority=7,
        goal=(
            "Before treating the latest checkpoint as sufficient, compare the objective and operator context "
            "against concrete artifacts, files, findings, measurements, validations, and task results."
        ),
        output_contract="decision",
        acceptance_criteria=(
            "A prompt-to-artifact checklist maps explicit requirements to evidence, identifies uncovered gaps, "
            "and opens or continues the next branch from those gaps."
        ),
        evidence_needed=(
            "Objective text, active operator context, latest durable outputs, recent tool/test results, "
            "task queue state, roadmap validations, and measured results when applicable."
        ),
        stall_behavior=(
            "If evidence is missing, mark the checkpoint incomplete, record the gap, and create the smallest "
            "follow-up task instead of claiming completion."
        ),
        metadata={
            "source": source,
            "requested_status": requested_status,
            "completion_audit_required": True,
            "claimed_message": claimed_message[:1000],
        },
    )


def _perpetual_checkpoint_message(message: str) -> str:
    """Keep worker reports checkpoint-oriented without hiding the underlying audit trail."""

    text = " ".join(str(message or "").split())
    if not text:
        return ""
    leading_claim = re.compile(
        r"(?i)^\s*(?:the\s+)?(?:job|objective|run|work)\s+"
        r"(?:is\s+|was\s+)?(?:complete|completed|done|finished)\b[.!:,\-\s]*"
    )
    if leading_claim.search(text):
        rest = leading_claim.sub("", text, count=1).strip()
        if rest:
            return f"Checkpoint reported; continuing work. {rest}"
        return "Checkpoint reported; continuing work."
    whole_job_claim = re.compile(
        r"(?i)\b(?:completed|finished|done\s+with)\s+(?:the\s+)?(?:job|objective|run|work)\b"
    )
    if whole_job_claim.search(text):
        return "Checkpoint reported; continuing work. " + whole_job_claim.sub("reached a checkpoint for the work", text, count=1)
    return text


def _record_lesson(args: dict[str, Any], ctx: ToolContext) -> str:
    lesson = str(args.get("lesson") or args.get("memory") or "").strip()
    if not lesson:
        return _json({"success": False, "error": "lesson is required"})
    category = str(args.get("category") or "memory").strip().lower()
    confidence_arg = args.get("confidence")
    confidence = float(confidence_arg) if isinstance(confidence_arg, (int, float)) else None
    metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
    entry = ctx.db.append_lesson(ctx.job_id, lesson, category=category, confidence=confidence, metadata=metadata)
    if _pending_measurement(ctx) and category in {"constraint", "mistake", "strategy", "memory"}:
        _resolve_measurement_obligation(
            ctx,
            status="explained",
            reason=lesson,
            via_tool="record_lesson",
        )
    return _json({"success": True, "job_id": ctx.job_id, "lesson": entry})


def _record_memory_graph(args: dict[str, Any], ctx: ToolContext) -> str:
    nodes = args.get("nodes") if isinstance(args.get("nodes"), list) else []
    edges = args.get("edges") if isinstance(args.get("edges"), list) else []
    if not nodes and not edges:
        return _json({"success": False, "error": "nodes or edges are required"})
    record = ctx.db.append_memory_graph_records(ctx.job_id, nodes=nodes, edges=edges)
    ctx.db.append_agent_update(
        ctx.job_id,
        (
            "Memory graph updated: "
            f"{record.get('added_nodes')} new nodes, {record.get('updated_nodes')} updated, "
            f"{record.get('added_edges')} new links."
        ),
        category="progress",
        metadata={
            "memory_graph_event_id": record.get("event_id"),
            "added_nodes": record.get("added_nodes"),
            "updated_nodes": record.get("updated_nodes"),
            "added_edges": record.get("added_edges"),
        },
    )
    return _json({"success": True, "job_id": ctx.job_id, **record})


def _search_memory_graph(args: dict[str, Any], ctx: ToolContext) -> str:
    query = str(args.get("query") or "")
    limit = int(args.get("limit") or 10)
    job = ctx.db.get_job(ctx.job_id)
    graph = memory_graph_from_job(job)
    results = search_memory_graph(graph, query=query, limit=limit)
    return _json({"success": True, "job_id": ctx.job_id, "query": query, **results})


def _acknowledge_operator_context(args: dict[str, Any], ctx: ToolContext) -> str:
    raw_ids = args.get("message_ids")
    message_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
    summary = str(args.get("summary") or args.get("reason") or "").strip()
    status = str(args.get("status") or "acknowledged").strip().lower()
    pending = _acknowledgeable_operator_messages(ctx.db.get_job(ctx.job_id), message_ids=message_ids)
    if not pending:
        return _json({
            "success": False,
            "error": "no active operator context to acknowledge",
            "message_ids": message_ids,
            "guidance": "Use acknowledge_operator_context only after incorporating claimed operator steering. Use report_update, record_lesson, record_tasks, or record_experiment for ordinary progress.",
        })
    result = ctx.db.acknowledge_operator_messages(
        ctx.job_id,
        message_ids=message_ids,
        summary=summary,
        status=status,
    )
    message = summary or f"Operator context {result.get('status')}."
    ctx.db.append_agent_update(
        ctx.job_id,
        message,
        category="progress",
        metadata={
            "operator_context_status": result.get("status"),
            "operator_message_count": result.get("count"),
            "operator_message_ids": [
                entry.get("event_id")
                for entry in result.get("messages", [])
                if isinstance(entry, dict) and entry.get("event_id")
            ],
        },
    )
    return _json({"success": True, "job_id": ctx.job_id, **result})


def _acknowledgeable_operator_messages(job: dict[str, Any], *, message_ids: list[str]) -> list[dict[str, Any]]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    wanted = {str(message_id).strip() for message_id in message_ids if str(message_id).strip()}
    pending = []
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
        pending.append(entry)
    return pending


def _record_source(args: dict[str, Any], ctx: ToolContext) -> str:
    source = str(args.get("source") or args.get("url") or args.get("domain") or "").strip()
    if not source:
        return _json({"success": False, "error": "source is required"})
    warnings_raw = args.get("warnings")
    warnings = [str(item) for item in warnings_raw] if isinstance(warnings_raw, list) else []
    score_arg = args.get("usefulness_score")
    usefulness_score = float(score_arg) if isinstance(score_arg, (int, float)) else None
    yield_count = int(args.get("yield_count") or 0)
    fail_count_delta = int(args.get("fail_count_delta") or 0)
    metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
    entry = ctx.db.append_source_record(
        ctx.job_id,
        source,
        source_type=str(args.get("source_type") or ""),
        usefulness_score=usefulness_score,
        yield_count=yield_count,
        fail_count_delta=fail_count_delta,
        warnings=warnings,
        outcome=str(args.get("outcome") or ""),
        metadata=metadata,
    )
    return _json({"success": True, "job_id": ctx.job_id, "source": entry})


def _record_findings(args: dict[str, Any], ctx: ToolContext) -> str:
    raw_findings = args.get("findings")
    if isinstance(raw_findings, list):
        findings = [item for item in raw_findings if isinstance(item, dict)]
    else:
        findings = [args]
    if not findings:
        return _json({"success": False, "error": "findings are required"})
    evidence_artifact = str(args.get("evidence_artifact") or args.get("artifact_id") or "")
    stored = []
    added = 0
    updated = 0
    unchanged = 0
    source_yields: dict[str, int] = {}
    for finding in findings[:50]:
        name = str(finding.get("name") or finding.get("title") or "").strip()
        if not name:
            continue
        source_url = str(finding.get("source_url") or finding.get("source") or args.get("source_url") or args.get("source") or "")
        score_arg = finding.get("score")
        score = float(score_arg) if isinstance(score_arg, (int, float)) else None
        entry = ctx.db.append_finding_record(
            ctx.job_id,
            name=name,
            url=str(finding.get("url") or ""),
            source_url=source_url,
            category=str(finding.get("category") or finding.get("type") or ""),
            location=str(finding.get("location") or ""),
            contact=str(finding.get("contact") or ""),
            reason=str(finding.get("reason") or finding.get("rationale") or ""),
            status=str(finding.get("status") or "new"),
            score=score,
            evidence_artifact=str(finding.get("evidence_artifact") or evidence_artifact),
            metadata=finding.get("metadata") if isinstance(finding.get("metadata"), dict) else {},
        )
        if entry.get("created"):
            added += 1
            if source_url:
                source_yields[source_url] = source_yields.get(source_url, 0) + 1
        elif entry.get("substantive_update"):
            updated += 1
        else:
            unchanged += 1
        stored.append(entry)
    if not stored:
        return _json({"success": False, "error": "no valid finding with name/title was provided"})
    source_records = []
    for source_url, count in source_yields.items():
        score = round(min(1.0, 0.55 + min(count, 10) * 0.04), 2)
        source_records.append(
            ctx.db.append_source_record(
                ctx.job_id,
                source_url,
                source_type=str(args.get("source_type") or "finding_source"),
                usefulness_score=score,
                yield_count=count,
                outcome=f"record_findings yielded {count} new candidate(s)",
                metadata={"auto_from_record_findings": True, "evidence_artifact": evidence_artifact},
            )
        )
    if added or updated or source_records:
        message = f"Finding ledger updated: {added} new, {updated} changed. Source ledger updated: {len(source_records)}."
        if unchanged:
            message += f" {unchanged} unchanged."
        ctx.db.append_agent_update(
            ctx.job_id,
            message,
            category="finding",
            metadata={"added": added, "updated": updated, "unchanged": unchanged, "sources_updated": len(source_records)},
        )
    return _json({
        "success": True,
        "job_id": ctx.job_id,
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "sources_updated": len(source_records),
        "sources": source_records,
        "findings": stored,
    })


def _record_tasks(args: dict[str, Any], ctx: ToolContext) -> str:
    raw_tasks = args.get("tasks")
    if isinstance(raw_tasks, list):
        tasks = [item for item in raw_tasks if isinstance(item, dict)]
    else:
        tasks = [args]
    if not tasks:
        return _json({"success": False, "error": "tasks are required"})

    stored = []
    added = 0
    updated = 0
    unchanged = 0
    for task in tasks[:50]:
        title = str(task.get("title") or task.get("name") or "").strip()
        if not title:
            continue
        status = str(task.get("status") or "open")
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        output_contract = str(
            task.get("output_contract")
            or task.get("contract")
            or metadata.get("output_contract")
            or metadata.get("contract")
            or ""
        )
        result_text = str(task.get("result") or task.get("outcome") or "")
        status, metadata = _validated_task_status(
            ctx,
            status=status,
            output_contract=output_contract,
            result=result_text,
            metadata=metadata,
        )
        priority_arg = task.get("priority")
        priority = int(priority_arg) if isinstance(priority_arg, (int, float)) else 0
        entry = ctx.db.append_task_record(
            ctx.job_id,
            title=title,
            status=status,
            priority=priority,
            goal=str(task.get("goal") or task.get("description") or ""),
            source_hint=str(task.get("source_hint") or task.get("source") or ""),
            result=result_text,
            parent=str(task.get("parent") or ""),
            output_contract=output_contract,
            acceptance_criteria=str(task.get("acceptance_criteria") or ""),
            evidence_needed=str(task.get("evidence_needed") or ""),
            stall_behavior=str(task.get("stall_behavior") or ""),
            metadata=metadata,
        )
        if entry.get("created"):
            added += 1
        elif entry.get("substantive_update"):
            updated += 1
        else:
            unchanged += 1
        stored.append(entry)
    if not stored:
        return _json({"success": False, "error": "no valid task with title/name was provided"})

    if added or updated:
        message = f"Task queue updated: {added} new, {updated} changed."
        if unchanged:
            message += f" {unchanged} unchanged."
        ctx.db.append_agent_update(
            ctx.job_id,
            message,
            category="plan",
            metadata={"added": added, "updated": updated, "unchanged": unchanged},
        )
    if (added or updated) and _pending_measurement(ctx):
        _resolve_measurement_obligation(
            ctx,
            status="deferred",
            reason="Created or updated task branch to obtain or handle the pending measurement.",
            via_tool="record_tasks",
        )
    return _json({"success": True, "job_id": ctx.job_id, "added": added, "updated": updated, "unchanged": unchanged, "tasks": stored})


def _validated_task_status(
    ctx: ToolContext,
    *,
    status: str,
    output_contract: str,
    result: str,
    metadata: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    normalized_status = status.strip().lower().replace(" ", "_") or "open"
    contract = output_contract.strip().lower().replace(" ", "_")
    if normalized_status == "done" and not result.strip() and not _task_metadata_has_completion_evidence(metadata, contract=contract):
        updated = dict(metadata)
        updated["completion_validation"] = "missing_result_evidence"
        return "active", updated
    if normalized_status != "done":
        return status, metadata
    if contract in {"artifact", "report"}:
        if _recent_deliverable_evidence(ctx):
            return status, metadata
        updated = dict(metadata)
        updated["completion_validation"] = "missing_recent_deliverable_evidence"
        if result:
            updated["claimed_result"] = result
        return "active", updated
    if _task_contract_completion_has_evidence(ctx, contract=contract, metadata=metadata):
        return status, metadata
    updated = dict(metadata)
    updated["completion_validation"] = f"missing_{contract}_evidence" if contract else "missing_contract_evidence"
    if result:
        updated["claimed_result"] = result
    return "active", updated


def _task_contract_completion_has_evidence(ctx: ToolContext, *, contract: str, metadata: dict[str, Any]) -> bool:
    if not contract or contract == "decision":
        return True
    if _task_metadata_has_completion_evidence(metadata, contract=contract):
        return True
    recent_evidence_tools = {
        "research": {"record_source", "record_findings"},
        "experiment": {"record_experiment"},
        "action": {"shell_exec", "write_file", "write_artifact"},
        "monitor": {"defer_job"},
        "validation": {"record_milestone_validation", "shell_exec"},
    }
    tools = recent_evidence_tools.get(contract)
    if not tools:
        return True
    for step in reversed(ctx.db.list_steps(job_id=ctx.job_id, limit=12)):
        if step.get("id") == ctx.step_id or step.get("status") != "completed":
            continue
        tool_name = str(step.get("tool_name") or "")
        if contract == "action" and tool_name == "shell_exec":
            input_data = step.get("input") if isinstance(step.get("input"), dict) else {}
            args = input_data.get("arguments") if isinstance(input_data.get("arguments"), dict) else {}
            if _shell_command_counts_as_action_evidence(str(args.get("command") or "")):
                return True
            continue
        if tool_name in tools:
            return True
    return False


def _task_metadata_has_completion_evidence(metadata: dict[str, Any], *, contract: str = "") -> bool:
    evidence_keys = {
        "artifact_id",
        "evidence_artifact",
        "experiment_key",
        "file_path",
        "output_path",
        "validation_event_id",
    }
    if contract == "research":
        evidence_keys.update({"finding_key", "source_key", "source_url", "finding_id", "source_id"})
    elif contract == "experiment":
        evidence_keys.update({"metric_name", "metric_value", "measurement_id"})
    elif contract == "action":
        evidence_keys.update({"step_id", "command", "action_id"})
    elif contract == "monitor":
        evidence_keys.update({"defer_until", "monitor_id", "check_at"})
    return any(str(metadata.get(key) or "").strip() for key in evidence_keys)


def _recent_deliverable_evidence(ctx: ToolContext, *, limit: int = 12) -> bool:
    for step in reversed(ctx.db.list_steps(job_id=ctx.job_id, limit=limit)):
        if step.get("id") == ctx.step_id:
            continue
        if step.get("status") != "completed":
            continue
        tool_name = str(step.get("tool_name") or "")
        input_data = step.get("input") if isinstance(step.get("input"), dict) else {}
        args = input_data.get("arguments") if isinstance(input_data.get("arguments"), dict) else {}
        if tool_name == "write_artifact" and _artifact_args_look_like_deliverable(args):
            return True
        if tool_name == "write_file":
            return True
        if tool_name == "shell_exec" and _shell_command_looks_like_write(str(args.get("command") or "")):
            return True
    return False


def _artifact_args_look_like_deliverable(args: dict[str, Any]) -> bool:
    text = " ".join(str(args.get(key) or "") for key in ("title", "summary", "type")).lower()
    if not text:
        return False
    evidence_like = any(term in text for term in EVIDENCE_OUTPUT_TERMS)
    deliverable_like = any(term in text for term in DELIVERABLE_OUTPUT_TERMS)
    return deliverable_like and not evidence_like


def _shell_command_looks_like_write(command: str) -> bool:
    text = command.strip()
    if not text:
        return False
    write_patterns = [
        r"(?<!\d)>>?\s*[^&]",
        r"\b1>>?\s*[^&]",
        r"\btee\b",
        r"\bcat\s+>\b",
        r"\bpython[0-9.]*\b.*\bwrite_text\b",
        r"\bpython[0-9.]*\b.*\bopen\([^)]*,\s*['\"]w",
        r"\bsed\s+-i\b",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in write_patterns)


def _shell_command_counts_as_action_evidence(command: str) -> bool:
    text = command.strip()
    if not text:
        return False
    if _shell_command_looks_like_write(text):
        return True
    read_only = re.compile(
        r"(?is)^\s*(?:"
        r"awk\b|cat\b|df\b|du\b|echo\b|find\b|git\s+(?:diff|grep|log|ls-files|show|status)\b|"
        r"grep\b|head\b|ls\b|pwd\b|rg\b|sed\s+-n\b|stat\b|tail\b|tree\b|wc\b"
        r")"
    )
    if read_only.search(text):
        return False
    if re.match(r"(?is)^curl\b", text):
        mutating_flags = (
            r"\b-X\s*(?:POST|PUT|PATCH|DELETE)\b|--request\s+(?:POST|PUT|PATCH|DELETE)\b|"
            r"(?:^|\s)(?:-d|--data|--form|-F|-T|--upload-file)\b"
        )
        return bool(re.search(mutating_flags, text))
    return True


def _record_roadmap(args: dict[str, Any], ctx: ToolContext) -> str:
    title = str(args.get("title") or args.get("name") or "").strip()
    if not title:
        return _json({"success": False, "error": "title is required"})
    milestones_arg = args.get("milestones")
    milestones = [item for item in milestones_arg if isinstance(item, dict)] if isinstance(milestones_arg, list) else []
    roadmap = ctx.db.append_roadmap_record(
        ctx.job_id,
        title=title,
        status=str(args.get("status") or "planned"),
        objective=str(args.get("objective") or ""),
        scope=str(args.get("scope") or ""),
        current_milestone=str(args.get("current_milestone") or ""),
        validation_contract=str(args.get("validation_contract") or ""),
        milestones=milestones,
        metadata=args.get("metadata") if isinstance(args.get("metadata"), dict) else {},
    )
    ctx.db.append_agent_update(
        ctx.job_id,
        (
            f"Roadmap updated: {roadmap.get('status')} with "
            f"{len(roadmap.get('milestones') or [])} milestones."
        ),
        category="plan",
        metadata={
            "roadmap_title": roadmap.get("title"),
            "roadmap_status": roadmap.get("status"),
            "milestone_count": len(roadmap.get("milestones") or []),
            "current_milestone": roadmap.get("current_milestone"),
        },
    )
    return _json({"success": True, "job_id": ctx.job_id, "roadmap": roadmap})


def _record_milestone_validation(args: dict[str, Any], ctx: ToolContext) -> str:
    milestone = str(args.get("milestone") or args.get("milestone_title") or "").strip()
    if not milestone:
        return _json({"success": False, "error": "milestone is required"})
    raw_issues = args.get("issues")
    issues = [str(item) for item in raw_issues if str(item).strip()] if isinstance(raw_issues, list) else []
    follow_up_items = args.get("follow_up_tasks") if isinstance(args.get("follow_up_tasks"), list) else []
    validation_status = str(args.get("validation_status") or args.get("status") or "pending").strip().lower().replace(" ", "_")
    if validation_status not in {"pending", "passed", "failed", "blocked"}:
        validation_status = "pending"
    result_text = str(args.get("result") or args.get("summary") or "").strip()
    evidence_text = str(args.get("evidence") or args.get("evidence_artifact") or "").strip()
    next_action = str(args.get("next_action") or "").strip()
    metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
    if validation_status == "passed" and not _validation_has_positive_evidence(result_text, evidence_text, metadata):
        return _json({
            "success": False,
            "error": "passed milestone validation requires evidence or result",
            "guidance": (
                "Use validation_status=passed only after concrete evidence or a validation result proves the milestone. "
                "Use pending, failed, or blocked when validation is incomplete or missing evidence."
            ),
        })
    if validation_status in {"failed", "blocked"} and not (
        result_text or evidence_text or issues or follow_up_items or next_action
    ):
        return _json({
            "success": False,
            "error": f"{validation_status} milestone validation requires a gap, issue, evidence, next_action, or follow-up task",
            "guidance": (
                "Failed or blocked validation must say what is missing or what should happen next, "
                "so the worker can continue from a concrete gap instead of logging a vague checkpoint."
            ),
        })
    validation = ctx.db.append_milestone_validation_record(
        ctx.job_id,
        milestone=milestone,
        validation_status=validation_status,
        result=result_text,
        evidence=evidence_text,
        issues=issues,
        next_action=next_action,
        metadata=metadata,
    )
    follow_up_tasks = []
    for task in follow_up_items[:25]:
        if not isinstance(task, dict):
            continue
        title = str(task.get("title") or task.get("name") or "").strip()
        if not title:
            continue
        priority_arg = task.get("priority")
        priority = int(priority_arg) if isinstance(priority_arg, (int, float)) else 0
        follow_up_tasks.append(ctx.db.append_task_record(
            ctx.job_id,
            title=title,
            status=str(task.get("status") or "open"),
            priority=priority,
            goal=str(task.get("goal") or task.get("description") or ""),
            source_hint=str(task.get("source_hint") or task.get("source") or ""),
            result=str(task.get("result") or task.get("outcome") or ""),
            parent=str(task.get("parent") or milestone),
            output_contract=str(
                task.get("output_contract")
                or task.get("contract")
                or (task.get("metadata") if isinstance(task.get("metadata"), dict) else {}).get("output_contract")
                or (task.get("metadata") if isinstance(task.get("metadata"), dict) else {}).get("contract")
                or "action"
            ),
            acceptance_criteria=str(task.get("acceptance_criteria") or ""),
            evidence_needed=str(task.get("evidence_needed") or ""),
            stall_behavior=str(task.get("stall_behavior") or ""),
            metadata=task.get("metadata") if isinstance(task.get("metadata"), dict) else {"source": "milestone_validation"},
        ))
    ctx.db.append_agent_update(
        ctx.job_id,
        (
            f"Milestone validation {validation.get('validation_status')}: "
            f"{validation.get('title') or milestone}; follow-up tasks {len(follow_up_tasks)}."
        ),
        category="plan",
        metadata={
            "milestone": validation.get("title") or milestone,
            "validation_status": validation.get("validation_status"),
            "follow_up_tasks": len(follow_up_tasks),
        },
    )
    return _json({
        "success": True,
        "job_id": ctx.job_id,
        "validation": validation,
        "follow_up_tasks": follow_up_tasks,
    })


def _validation_has_positive_evidence(result: str, evidence: str, metadata: dict[str, Any]) -> bool:
    if result.strip() or evidence.strip():
        return True
    evidence_keys = {
        "artifact_id",
        "evidence_artifact",
        "experiment_key",
        "file_path",
        "output_path",
        "source_url",
        "finding_key",
        "validation_event_id",
    }
    return any(str(metadata.get(key) or "").strip() for key in evidence_keys)


def _record_experiment(args: dict[str, Any], ctx: ToolContext) -> str:
    title = str(
        args.get("title")
        or args.get("name")
        or args.get("metric_name")
        or args.get("hypothesis")
        or args.get("result")
        or args.get("outcome")
        or "Experiment checkpoint"
    ).strip()
    metric_name = str(args.get("metric_name") or "").strip()
    metric_value = _optional_float(args.get("metric_value"))
    baseline_value = _optional_float(args.get("baseline_value"))
    status = str(args.get("status") or "planned").strip().lower() or "planned"
    next_action = str(args.get("next_action") or "").strip()
    if status == "measured" and (not metric_name or metric_value is None):
        return _json({
            "success": False,
            "error": "measured experiments require metric_name and numeric metric_value",
            "guidance": (
                "Use status=measured only for a real measurement with a metric name and numeric value. "
                "Use status=failed, blocked, skipped, running, or planned when the trial did not produce a valid metric."
            ),
        })
    if status in {"measured", "failed", "blocked", "skipped"} and not next_action:
        return _json({
            "success": False,
            "error": "next_action is required for measured, failed, blocked, or skipped experiments",
            "guidance": (
                "Experiment records that close out a trial must leave a concrete next action, "
                "such as the next experiment, action branch, monitor branch, pivot, or blocked condition."
            ),
        })
    record = ctx.db.append_experiment_record(
        ctx.job_id,
        title=title,
        hypothesis=str(args.get("hypothesis") or ""),
        status=status,
        metric_name=metric_name,
        metric_value=metric_value,
        metric_unit=str(args.get("metric_unit") or ""),
        higher_is_better=bool(args.get("higher_is_better", True)),
        baseline_value=baseline_value,
        config=args.get("config") if isinstance(args.get("config"), dict) else {},
        result=str(args.get("result") or args.get("outcome") or ""),
        evidence_artifact=str(args.get("evidence_artifact") or args.get("artifact_id") or ""),
        next_action=next_action,
        metadata=args.get("metadata") if isinstance(args.get("metadata"), dict) else {},
    )
    metric = ""
    if record.get("metric_value") is not None:
        metric = " " + format_metric_value(
            record.get("metric_name") or "metric",
            record.get("metric_value"),
            record.get("metric_unit") or "",
        )
    best = " best" if record.get("best_observed") else ""
    ctx.db.append_agent_update(
        ctx.job_id,
        f"Experiment {record.get('status')}: {record.get('title')}{metric}{best}.",
        category="progress",
        metadata={
            "experiment_key": record.get("key"),
            "metric_name": record.get("metric_name"),
            "metric_value": record.get("metric_value"),
            "best_observed": record.get("best_observed"),
            "delta_from_previous_best": record.get("delta_from_previous_best"),
        },
    )
    if record.get("metric_value") is not None or str(record.get("status") or "") in {"measured", "failed", "blocked", "skipped"}:
        _resolve_measurement_obligation(
            ctx,
            status="recorded",
            reason=f"Recorded experiment {record.get('title')}.",
            via_tool="record_experiment",
            experiment_key=str(record.get("key") or ""),
        )
    return _json({"success": True, "job_id": ctx.job_id, "experiment": record})


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _pending_measurement(ctx: ToolContext) -> dict[str, Any] | None:
    try:
        job = ctx.db.get_job(ctx.job_id)
    except KeyError:
        return None
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    obligation = metadata.get("pending_measurement_obligation")
    if isinstance(obligation, dict) and not obligation.get("resolved_at"):
        return obligation
    return None


def _resolve_measurement_obligation(
    ctx: ToolContext,
    *,
    status: str,
    reason: str,
    via_tool: str,
    experiment_key: str = "",
) -> None:
    obligation = _pending_measurement(ctx)
    if not obligation:
        return
    resolved = dict(obligation)
    resolved.update({
        "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "resolution_status": status,
        "resolution_reason": reason[:1000],
        "resolution_tool": via_tool,
    })
    if experiment_key:
        resolved["experiment_key"] = experiment_key
    ctx.db.update_job_metadata(
        ctx.job_id,
        {
            "pending_measurement_obligation": {},
            "last_measurement_obligation": resolved,
        },
    )
    ctx.db.append_agent_update(
        ctx.job_id,
        f"Measurement obligation {status}: {reason[:220]}",
        category="progress" if status == "recorded" else "blocked",
        metadata={"measurement_obligation": resolved},
    )


def _send_digest_email(args: dict[str, Any], ctx: ToolContext) -> str:
    subject = str(args.get("subject") or "Agent digest")
    body = str(args.get("body") or "")
    if not body:
        return _json({"success": False, "error": "body is required"})
    result = send_digest_email(ctx.config.email, subject=subject, body=body, to_addr=args.get("to_addr"))
    stored = ctx.artifacts.write_text(
        job_id=ctx.job_id,
        run_id=ctx.run_id,
        step_id=ctx.step_id,
        content=body,
        title=subject,
        summary="Digest email body",
        artifact_type="digest",
        metadata={"email": result},
    )
    return _json({"success": True, "email": result, "artifact_id": stored.id, "path": str(stored.path)})


def _browser_call(name: str, args: dict[str, Any], ctx: ToolContext) -> str:
    from nipux_cli import browser

    task_id = ctx.task_id or ctx.job_id
    if name == "browser_navigate":
        return _json(browser.navigate(ctx.config, task_id=task_id, url=str(args.get("url") or "")))
    if name == "browser_snapshot":
        return _json(browser.snapshot(ctx.config, task_id=task_id, full=bool(args.get("full", False))))
    if name == "browser_click":
        return _json(browser.click(ctx.config, task_id=task_id, ref=str(args.get("ref") or "")))
    if name == "browser_type":
        return _json(browser.fill(ctx.config, task_id=task_id, ref=str(args.get("ref") or ""), text=str(args.get("text") or "")))
    if name == "browser_scroll":
        return _json(browser.scroll(ctx.config, task_id=task_id, direction=str(args.get("direction") or "down")))
    if name == "browser_back":
        return _json(browser.back(ctx.config, task_id=task_id))
    if name == "browser_press":
        return _json(browser.press(ctx.config, task_id=task_id, key=str(args.get("key") or "")))
    if name == "browser_console":
        return _json(browser.console(ctx.config, task_id=task_id, clear=bool(args.get("clear", False)), expression=args.get("expression")))
    raise KeyError(name)


def _web_call(name: str, args: dict[str, Any], ctx: ToolContext) -> str:
    del ctx
    from nipux_cli.web import web_extract, web_search

    if name == "web_search":
        return _json(web_search(str(args.get("query") or ""), limit=int(args.get("limit") or 5)))
    if name == "web_extract":
        urls = args.get("urls") if isinstance(args.get("urls"), list) else []
        return _json(web_extract(urls[:5]))
    raise KeyError(name)


def _browser_handler(name: str) -> Handler:
    return lambda args, ctx: _browser_call(name, args, ctx)


def _web_handler(name: str) -> Handler:
    return lambda args, ctx: _web_call(name, args, ctx)


BROWSER_SCHEMAS: list[ToolSpec] = [
    ToolSpec("browser_navigate", "Navigate to a URL and return a compact browser snapshot.", {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }, _browser_handler("browser_navigate")),
    ToolSpec("browser_snapshot", "Refresh the current page accessibility snapshot.", {
        "type": "object",
        "properties": {"full": {"type": "boolean", "default": False}},
        "required": [],
    }, _browser_handler("browser_snapshot")),
    ToolSpec("browser_click", "Click an element by snapshot ref, for example @e5.", {
        "type": "object",
        "properties": {"ref": {"type": "string"}},
        "required": ["ref"],
    }, _browser_handler("browser_click")),
    ToolSpec("browser_type", "Fill an input by snapshot ref.", {
        "type": "object",
        "properties": {"ref": {"type": "string"}, "text": {"type": "string"}},
        "required": ["ref", "text"],
    }, _browser_handler("browser_type")),
    ToolSpec("browser_scroll", "Scroll the current page up or down.", {
        "type": "object",
        "properties": {"direction": {"type": "string", "enum": ["up", "down"]}},
        "required": ["direction"],
    }, _browser_handler("browser_scroll")),
    ToolSpec("browser_back", "Navigate back in browser history.", {"type": "object", "properties": {}, "required": []}, _browser_handler("browser_back")),
    ToolSpec("browser_press", "Press a keyboard key in the browser.", {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    }, _browser_handler("browser_press")),
    ToolSpec("browser_console", "Read console errors or evaluate JavaScript in the current page.", {
        "type": "object",
        "properties": {"clear": {"type": "boolean", "default": False}, "expression": {"type": "string"}},
        "required": [],
    }, _browser_handler("browser_console")),
]


SUPPORT_SCHEMAS: list[ToolSpec] = [
    ToolSpec("web_search", "Search the web for candidate sources.", {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
        "required": ["query"],
    }, _web_handler("web_search")),
    ToolSpec("web_extract", "Extract markdown text from up to five URLs.", {
        "type": "object",
        "properties": {"urls": {"type": "array", "items": {"type": "string"}, "maxItems": 5}},
        "required": ["urls"],
    }, _web_handler("web_extract")),
    ToolSpec("shell_exec", "Run a local shell command for CLI work. Use small read-only probes first. For long downloads, builds, training, crawls, or benchmarks, set a meaningful timeout, prefer resumable commands, and record or defer monitoring instead of repeatedly restarting short timed-out commands. Do not run destructive or high-risk cyber commands.", {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number", "default": 60},
            "max_output_chars": {"type": "integer", "default": 12000},
        },
        "required": ["command"],
    }, _shell_exec),
    ToolSpec("write_file", "Create, overwrite, or append a concrete workspace/local file for deliverables, code, documents, configs, or other file outputs.", {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
            "create_parents": {"type": "boolean", "default": True},
        },
        "required": ["path", "content"],
    }, _write_file),
    ToolSpec("write_artifact", "Persist important findings, evidence, reports, or checkpoints to the job artifact store.", {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "type": {"type": "string", "default": "text"},
            "summary": {"type": "string"},
            "content": {"type": "string"},
            "metadata": {"type": "object"},
        },
        "required": ["content"],
    }, _write_artifact),
    ToolSpec("read_artifact", "Read a saved artifact by artifact_id, visible number, exact saved path, or title.", {
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string", "description": "Artifact id, visible number, saved path, or title."},
            "path": {"type": "string"},
            "title": {"type": "string"},
            "ref": {"type": "string"},
        },
        "required": [],
    }, _read_artifact),
    ToolSpec("search_artifacts", "Search stored artifacts for exact evidence from prior steps.", {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
        "required": ["query"],
    }, _search_artifacts),
    ToolSpec("update_job_state", "Keep the current job runnable. Completion, failure, pausing, and cancellation are operator-only; workers should report checkpoints and continue.", {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["queued", "running"]},
            "note": {"type": "string"},
        },
        "required": ["status"],
    }, _update_job_state),
    ToolSpec("defer_job", "Wait before the next worker turn for this job. Use for long external processes, monitor/check-later tasks, cooldowns, or scheduled follow-up without completing or pausing the job.", {
        "type": "object",
        "properties": {
            "seconds": {"type": "number", "description": "Delay in seconds before this job is runnable again.", "default": 300},
            "until": {"type": "string", "description": "Optional ISO timestamp to resume after."},
            "reason": {"type": "string"},
            "next_action": {"type": "string"},
        },
        "required": [],
    }, _defer_job),
    ToolSpec("report_update", "Leave a short operator-readable progress note. Do not use this instead of write_artifact for durable evidence.", {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "category": {"type": "string", "enum": ["progress", "finding", "blocked", "plan"], "default": "progress"},
            "metadata": {"type": "object"},
        },
        "required": ["message"],
    }, _report_update),
    ToolSpec("record_lesson", "Save durable learning for this job: bad source patterns, success criteria, strategy changes, mistakes to avoid, or operator preferences.", {
        "type": "object",
        "properties": {
            "lesson": {"type": "string"},
            "category": {
                "type": "string",
                "enum": [
                    "source_quality",
                    "task_profile",
                    "strategy",
                    "mistake",
                    "constraint",
                    "operator_preference",
                    "memory",
                ],
                "default": "memory",
            },
            "confidence": {"type": "number"},
            "metadata": {"type": "object"},
        },
        "required": ["lesson"],
    }, _record_lesson),
    ToolSpec("record_memory_graph", "Create or update the job's connected memory graph: reusable episodes, facts, strategies, skills, questions, decisions, constraints, and links between them. Use this to build a durable brain for long-running work instead of relying on raw history.", {
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "title": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "artifact",
                                "constraint",
                                "decision",
                                "episode",
                                "experiment",
                                "fact",
                                "milestone",
                                "question",
                                "skill",
                                "source",
                                "strategy",
                                "task",
                            ],
                        },
                        "status": {
                            "type": "string",
                            "enum": ["active", "blocked", "deprecated", "open", "resolved", "stable"],
                            "default": "active",
                        },
                        "summary": {"type": "string"},
                        "salience": {"type": "number"},
                        "confidence": {"type": "number"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "parent_key": {"type": "string"},
                        "links": {"type": "array", "items": {"type": "string"}},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object"},
                    },
                    "required": ["title"],
                },
            },
            "edges": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": {
                        "from_key": {"type": "string"},
                        "to_key": {"type": "string"},
                        "relation": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object"},
                    },
                    "required": ["from_key", "to_key", "relation"],
                },
            },
        },
        "required": [],
    }, _record_memory_graph),
    ToolSpec("search_memory_graph", "Search the job's connected memory graph for reusable facts, decisions, strategies, skills, questions, constraints, and related links.", {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    }, _search_memory_graph),
    ToolSpec("acknowledge_operator_context", "Acknowledge that durable operator steering has been incorporated or superseded. Use this after acting on a chat correction so it can leave the active context while remaining in history.", {
        "type": "object",
        "properties": {
            "message_ids": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
            "status": {"type": "string", "enum": ["acknowledged", "superseded"], "default": "acknowledged"},
        },
        "required": ["summary"],
    }, _acknowledge_operator_context),
    ToolSpec("record_source", "Update the source ledger with source quality, finding yield, failures, warnings, and last outcome.", {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "source_type": {"type": "string"},
            "usefulness_score": {"type": "number"},
            "yield_count": {"type": "integer", "default": 0},
            "fail_count_delta": {"type": "integer", "default": 0},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "outcome": {"type": "string"},
            "metadata": {"type": "object"},
        },
        "required": ["source"],
    }, _record_source),
    ToolSpec("record_findings", "Update the finding ledger with one or more useful results. Use after saving an evidence artifact or identifying durable candidates, facts, opportunities, experiments, or other reusable outputs.", {
        "type": "object",
        "properties": {
            "evidence_artifact": {"type": "string"},
            "findings": {
                "type": "array",
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "url": {"type": "string"},
                        "source_url": {"type": "string"},
                        "category": {"type": "string"},
                        "location": {"type": "string"},
                        "contact": {"type": "string"},
                        "reason": {"type": "string"},
                        "status": {"type": "string"},
                        "score": {"type": "number"},
                        "evidence_artifact": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["name"],
                },
            },
        },
        "required": ["findings"],
    }, _record_findings),
    ToolSpec("record_tasks", "Create or update a durable queue of objective-neutral work branches. Use this to split long jobs into next actions, mark blocked branches, and keep the agent from cycling on one path.", {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "status": {"type": "string", "enum": ["open", "active", "done", "blocked", "skipped"], "default": "open"},
                        "priority": {"type": "integer", "default": 0},
                        "goal": {"type": "string"},
                        "source_hint": {"type": "string"},
                        "result": {"type": "string"},
                        "parent": {"type": "string"},
                        "output_contract": {
                            "type": "string",
                            "enum": ["research", "artifact", "experiment", "action", "monitor", "decision", "report"],
                        },
                        "acceptance_criteria": {"type": "string"},
                        "evidence_needed": {"type": "string"},
                        "stall_behavior": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["title"],
                },
            },
        },
        "required": ["tasks"],
    }, _record_tasks),
    ToolSpec("record_roadmap", "Create or update a generic roadmap for broad work: milestones, features, success criteria, validation contract, scope, and current roadmap state. Use this before or during long-running work when task lists need higher-level structure.", {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "status": {"type": "string", "enum": ["planned", "active", "validating", "done", "blocked", "paused"], "default": "planned"},
            "objective": {"type": "string"},
            "scope": {"type": "string"},
            "current_milestone": {"type": "string"},
            "validation_contract": {"type": "string"},
            "milestones": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "title": {"type": "string"},
                        "status": {"type": "string", "enum": ["planned", "active", "validating", "done", "blocked", "skipped"], "default": "planned"},
                        "priority": {"type": "integer", "default": 0},
                        "goal": {"type": "string"},
                        "acceptance_criteria": {"type": "string"},
                        "evidence_needed": {"type": "string"},
                        "validation_status": {"type": "string", "enum": ["not_started", "pending", "passed", "failed", "blocked"], "default": "not_started"},
                        "validation_result": {"type": "string"},
                        "next_action": {"type": "string"},
                        "features": {
                            "type": "array",
                            "maxItems": 100,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string"},
                                    "title": {"type": "string"},
                                    "status": {"type": "string", "enum": ["planned", "active", "done", "blocked", "skipped"], "default": "planned"},
                                    "goal": {"type": "string"},
                                    "output_contract": {"type": "string", "enum": ["research", "artifact", "experiment", "action", "monitor", "decision", "report", "validation"]},
                                    "acceptance_criteria": {"type": "string"},
                                    "evidence_needed": {"type": "string"},
                                    "result": {"type": "string"},
                                    "metadata": {"type": "object"},
                                },
                                "required": ["title"],
                            },
                        },
                        "metadata": {"type": "object"},
                    },
                    "required": ["title"],
                },
            },
            "metadata": {"type": "object"},
        },
        "required": ["title"],
    }, _record_roadmap),
    ToolSpec("record_milestone_validation", "Record validation for a roadmap milestone and optionally create follow-up tasks for gaps. Use fresh evidence, acceptance criteria, and clear pass/fail/blocker reasons.", {
        "type": "object",
        "properties": {
            "milestone": {"type": "string"},
            "validation_status": {"type": "string", "enum": ["pending", "passed", "failed", "blocked"], "default": "pending"},
            "result": {"type": "string"},
            "evidence": {"type": "string"},
            "issues": {"type": "array", "items": {"type": "string"}},
            "next_action": {"type": "string"},
            "follow_up_tasks": {
                "type": "array",
                "maxItems": 25,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "status": {"type": "string", "enum": ["open", "active", "done", "blocked", "skipped"], "default": "open"},
                        "priority": {"type": "integer", "default": 0},
                        "goal": {"type": "string"},
                        "source_hint": {"type": "string"},
                        "result": {"type": "string"},
                        "parent": {"type": "string"},
                        "output_contract": {"type": "string", "enum": ["research", "artifact", "experiment", "action", "monitor", "decision", "report"]},
                        "acceptance_criteria": {"type": "string"},
                        "evidence_needed": {"type": "string"},
                        "stall_behavior": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["title"],
                },
            },
            "metadata": {"type": "object"},
        },
        "required": ["milestone", "validation_status"],
    }, _record_milestone_validation),
    ToolSpec("record_experiment", "Track a measurable trial, benchmark, comparison, hypothesis test, or optimization attempt. Use this after any command or source produces a concrete result so future steps compare against the best observed result instead of treating notes as progress. Closed trials must include next_action so long-running work can continue from the result.", {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "hypothesis": {"type": "string"},
            "status": {"type": "string", "enum": ["planned", "running", "measured", "failed", "blocked", "skipped"], "default": "planned"},
            "metric_name": {"type": "string"},
            "metric_value": {"type": "number"},
            "metric_unit": {"type": "string"},
            "higher_is_better": {"type": "boolean", "default": True},
            "baseline_value": {"type": "number"},
            "config": {"type": "object"},
            "result": {"type": "string"},
            "evidence_artifact": {"type": "string"},
            "next_action": {"type": "string", "description": "Concrete next experiment, action, monitor branch, pivot, or blocked condition. Required when status is measured, failed, blocked, or skipped."},
            "metadata": {"type": "object"},
        },
        "required": ["title"],
    }, _record_experiment),
    ToolSpec("send_digest_email", "Send or dry-run a digest email and save the body as an artifact.", {
        "type": "object",
        "properties": {"subject": {"type": "string"}, "body": {"type": "string"}, "to_addr": {"type": "string"}},
        "required": ["body"],
    }, _send_digest_email),
]


APPROVED_TOOL_NAMES = tuple(spec.name for spec in [*BROWSER_SCHEMAS, *SUPPORT_SCHEMAS])


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None):
        self._specs = {spec.name: spec for spec in (specs or [*BROWSER_SCHEMAS, *SUPPORT_SCHEMAS])}

    def names(self) -> list[str]:
        return sorted(self._specs)

    def openai_tools(self, config: AppConfig | None = None) -> list[dict[str, Any]]:
        return [self._specs[name].as_openai_tool() for name in self.names() if _tool_enabled(name, config)]

    def validate_arguments(self, name: str, args: dict[str, Any], config: AppConfig | None = None) -> dict[str, Any] | None:
        if name not in self._specs or not _tool_enabled(name, config):
            return None
        args = args if isinstance(args, dict) else {}
        spec = self._specs[name]
        missing: list[str] = []
        aliases = REQUIRED_ARGUMENT_ALIASES.get(name, {})
        for required in spec.parameters.get("required") or []:
            candidates = aliases.get(str(required), (str(required),))
            if all(_missing_argument(args.get(candidate)) for candidate in candidates):
                missing.append(" or ".join(candidates))
        for label, fields in REQUIRED_ARGUMENT_GROUPS.get(name, ()):
            if all(_missing_argument(args.get(field)) for field in fields):
                missing.append(label)
        nested_schema = dict(spec.parameters)
        nested_schema["required"] = []
        missing.extend(item for item in _schema_missing_arguments(nested_schema, args) if item not in missing)
        placeholders = [] if missing else [item for item in _schema_placeholder_arguments(nested_schema, args) if item not in missing]
        if not missing and not placeholders:
            return None
        concrete_fields = [*missing, *placeholders]
        return {
            "success": True,
            "recoverable": True,
            "error": "missing required tool arguments" if missing else "placeholder tool arguments",
            "missing_arguments": missing,
            "placeholder_arguments": placeholders,
            "blocked_tool": name,
            "guidance": (
                f"Retry {name} with concrete values for: {', '.join(concrete_fields)}. "
                "Do not call a tool with placeholder or empty arguments."
            ),
        }

    def handle(self, name: str, args: dict[str, Any], ctx: ToolContext) -> str:
        if name not in self._specs:
            return _json({"success": False, "error": f"unknown tool: {name}"})
        if not _tool_enabled(name, ctx.config):
            group = _tool_access_group(name) or "tool"
            return _json({
                "success": False,
                "error": f"{name} is disabled by tool access config",
                "tool_access": group,
            })
        return self._specs[name].handler(args, ctx)


def _tool_enabled(name: str, config: AppConfig | None) -> bool:
    if config is None:
        return True
    group = _tool_access_group(name)
    if group is None:
        return True
    return bool(getattr(config.tools, group))


def _tool_access_group(name: str) -> str | None:
    if name.startswith("browser_"):
        return "browser"
    if name.startswith("web_"):
        return "web"
    if name == "shell_exec":
        return "shell"
    if name == "write_file":
        return "files"
    return None


DEFAULT_REGISTRY = ToolRegistry()
