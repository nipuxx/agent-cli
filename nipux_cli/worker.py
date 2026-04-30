"""Bounded worker loop for one restartable agent step."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import AppConfig, load_config
from nipux_cli.compression import refresh_memory_index
from nipux_cli.db import AgentDB
from nipux_cli.llm import LLMResponse, LLMResponseError, OpenAIChatLLM, StepLLM
from nipux_cli.operator_context import (
    active_prompt_operator_entries,
    inactive_prompt_operator_ids,
    operator_entry_is_prompt_relevant,
)
from nipux_cli.source_quality import anti_bot_reason
from nipux_cli.tools import DEFAULT_REGISTRY, ToolContext, ToolRegistry


REFLECTION_INTERVAL_STEPS = 12
WORKER_PROTOCOL_VERSION = "2026-04-26-measured-progress-v1"

SYSTEM_PROMPT = """You are a long-running local work agent.

Operate as a bounded worker, not a chat assistant. Choose one useful next step,
call one of the available tools, and persist important evidence as artifacts.
Do not claim the whole job is complete. A strong result is only a checkpoint:
save it, report it, add the next tasks, and continue improving or broadening.

Use this durable cycle: discover one source, extract or browse it, save useful
evidence, update finding/source ledgers, record lessons, then continue with the
next branch. Keep moving forever until the operator pauses or cancels the job.
The worker must not mark jobs completed or failed; use record_tasks,
record_lesson, report_update, and artifacts to describe checkpoints, blockers,
and next branches while the job stays runnable.

Avoid loops. Do not repeat the same search query or the same exact tool call.
If search results already exist, move forward by extracting source pages,
opening a useful site in the browser, or saving a finding/evidence artifact.
If a page has already been extracted and contains useful evidence, save that
evidence with write_artifact before doing more searching or browsing.
Only click or type browser refs from the most recent successful browser snapshot
or navigation result. If a click/type fails with an unknown ref, use the fresh
recovery snapshot or call browser_snapshot before retrying.
If a source shows Cloudflare, login, paywall, or anti-bot verification, keep it
visible in the trace. Do not bypass protections. Continue with normal visible
browser actions when possible, persist what you have, or use alternate public
sources if stuck.
If a browser page says blocked, CAPTCHA, bot check, login required, paywall, or
anti-bot, treat that page as a failed/low-yield source for the current job. Do
not write an artifact that claims usable evidence exists unless the evidence is
actually visible. Record the source outcome or pivot to another public source.
Use report_update for short operator-readable progress notes when you need to
say what you found or why you are blocked. Do not use report_update instead of
write_artifact when you have durable evidence, findings, or report content to save.
Use record_lesson when you learn something that should change future behavior:
bad source patterns, task-specific success criteria, repeated mistakes, operator
preferences, or a better strategy. Keep lessons short and reusable.
Use record_source when a source is high-yield, low-yield, blocked, repetitive,
or otherwise useful to score for future behavior.
Use record_findings after finding durable candidates, facts, opportunities,
experiments, files, bugs, sources, or other reusable outputs. Dedupe against the
finding ledger and artifacts before saving.
Use record_tasks to maintain a durable queue of objective-neutral branches:
open work, active branch, blocked branch, completed branch, and skipped branch.
Each task should include an output_contract (research, artifact, experiment,
action, monitor, decision, or report), acceptance criteria, evidence needed,
and stall behavior so progress is judged by evidence, not activity volume.
When the job is broad or starts looping, split it into tasks and move to the
highest-priority open task rather than staying on one source or tactic forever.
Use record_experiment for measurable trials, benchmarks, comparisons,
optimization attempts, or hypothesis tests. A saved note, source, or artifact is
not enough progress for a measurable objective: record the exact configuration,
metric, result, whether higher or lower is better, and the next experiment. Keep
improving against the best observed result instead of declaring victory after a
single measurement.
Use shell_exec for command-line work, repository inspection, diagnostics,
benchmarks, repeatable experiments, and other command execution that the
objective requires. Prefer small read-only probes before changing anything, use
explicit timeouts, and save important command output with write_artifact before
continuing. Do not run destructive or high-risk cyber commands.
read_artifact only reads saved Nipux artifacts. Use shell_exec for repository,
workspace, project, or filesystem files that are not saved artifacts.
Operator messages are durable context from the human operator. Messages marked
steer are active constraints until acknowledged or superseded. Messages marked
follow_up are lower-priority queued work; keep them in the task queue and act on
them after the current active branch has a durable checkpoint. Messages marked
note are durable preferences. Use acknowledge_operator_context only after you
have incorporated or intentionally superseded a steer/follow_up message.
"""

INFORMATION_GATHERING_TOOLS = {
    "browser_back",
    "browser_click",
    "browser_console",
    "browser_navigate",
    "browser_press",
    "browser_scroll",
    "browser_snapshot",
    "browser_type",
    "web_extract",
    "web_search",
}

BRANCH_WORK_TOOLS = INFORMATION_GATHERING_TOOLS | {"shell_exec"}
LEDGER_PROGRESS_TOOLS = {"record_findings", "record_source", "record_tasks", "record_experiment", "record_lesson"}
MEASUREMENT_RESOLUTION_TOOLS = {"record_experiment", "record_lesson", "record_tasks", "acknowledge_operator_context"}
MEASUREMENT_BLOCKED_TOOLS = INFORMATION_GATHERING_TOOLS | {
    "shell_exec",
    "write_artifact",
    "record_findings",
    "record_source",
    "report_update",
}
CHURN_TOOLS = INFORMATION_GATHERING_TOOLS | {"shell_exec"}
MEASURABLE_RESEARCH_BLOCKED_TOOLS = INFORMATION_GATHERING_TOOLS | {
    "write_artifact",
    "record_findings",
    "record_source",
    "report_update",
}
MEASURABLE_PROGRESS_PATTERN = re.compile(
    r"(?i)\b("
    r"benchmark|baseline|compare|comparison|experiment|improv(?:e|ing|ement)|increase|latency|"
    r"measure|metric|minimi[sz]e|maximi[sz]e|optim(?:ize|ise|ization|isation)|performance|"
    r"rate|reduce|score|speed|throughput|tune|tuning"
    r")\b"
)
MEASURABLE_RESEARCH_BUDGET_STEPS = 18
MEASURABLE_ACTION_BUDGET_STEPS = 4
PROGRAM_PROMPT_CHARS = 2000
MEMORY_ENTRY_PROMPT_CHARS = 700
MEMORY_PROMPT_CHARS = 1800
RECENT_STATE_STEPS = 5
RECENT_STATE_PROMPT_CHARS = 3000
TIMELINE_PROMPT_EVENTS = 8
SECTION_ITEM_CHARS = 420

QUERY_STOPWORDS = {
    "and",
    "are",
    "does",
    "for",
    "from",
    "how",
    "offer",
    "product",
    "service",
    "services",
    "the",
    "they",
    "what",
    "with",
}

BROWSER_REF_IGNORE_NAMES = {
    "about us",
    "back to top",
    "careers",
    "click here",
    "clutch rating",
    "organization name",
    "contact",
    "contact us",
    "go",
    "headquarters",
    "help",
    "latest links",
    "learn more",
    "privacy",
    "read more",
    "readmore",
    "services",
    "submit",
    "top hits",
}

ANTI_BOT_ACK_TERMS = (
    "anti-bot",
    "blocked",
    "bot check",
    "captcha",
    "not usable",
    "verification",
)


@dataclass(frozen=True)
class StepExecution:
    job_id: str
    run_id: str
    step_id: str
    tool_name: str | None
    status: str
    result: dict[str, Any]


def build_messages(
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    memory_entries: list[dict[str, Any]] | None = None,
    program_text: str = "",
    timeline_events: list[dict[str, Any]] | None = None,
    active_operator_messages: list[dict[str, Any]] | None = None,
    include_unclaimed_operator_messages: bool = True,
) -> list[dict[str, Any]]:
    step_lines = []
    for step in recent_steps[-RECENT_STATE_STEPS:]:
        step_lines.append(_clip_text(_format_step_for_prompt(step), 720))
    state = _clip_text("\n".join(step_lines), RECENT_STATE_PROMPT_CHARS) if step_lines else "No prior steps."
    memory_lines = []
    for entry in (memory_entries or [])[:2]:
        refs = ", ".join((entry.get("artifact_refs") or [])[:8])
        suffix = f"\nArtifact refs: {refs}" if refs else ""
        memory_lines.append(
            _clip_text(f"### {entry['key']}\n{entry.get('summary') or ''}{suffix}", MEMORY_ENTRY_PROMPT_CHARS)
        )
    memory_text = _clip_text("\n\n".join(memory_lines), MEMORY_PROMPT_CHARS) if memory_lines else "No compact memory yet."
    program = _clip_text(program_text.strip(), PROGRAM_PROMPT_CHARS) if program_text else "No program.md saved yet."
    operator_messages = _operator_messages_for_prompt(
        job,
        active_messages=active_operator_messages or [],
        include_unclaimed=include_unclaimed_operator_messages,
    )
    measurement_obligation = _measurement_obligation_for_prompt(job)
    measured_progress_guard = _measured_progress_guard_for_prompt(job, recent_steps)
    lessons = _lessons_for_prompt(job)
    tasks = _tasks_for_prompt(job)
    ledgers = _ledgers_for_prompt(job)
    experiments = _experiments_for_prompt(job)
    reflections = _reflections_for_prompt(job)
    timeline = _timeline_for_prompt(timeline_events or [])
    next_constraint = _next_action_constraint(job, recent_steps)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Job: {job['title']}\n"
                f"Kind: {job['kind']}\n"
                f"Objective:\n{job['objective']}\n\n"
                f"Workspace:\n"
                f"- shell_exec default cwd: {os.getcwd()}\n"
                f"- saved artifacts are separate Nipux outputs; read_artifact is only for those saved outputs\n"
                f"- use shell_exec for workspace/project files unless the file is a saved artifact\n\n"
                f"Operator context:\n{operator_messages}\n\n"
                f"Pending measurement obligation:\n{measurement_obligation}\n\n"
                f"Measured progress guard:\n{measured_progress_guard}\n\n"
                f"Program:\n{program}\n\n"
                f"Lessons learned:\n{lessons}\n\n"
                f"Task queue:\n{tasks}\n\n"
                f"Ledgers:\n{ledgers}\n\n"
                f"Experiment ledger:\n{experiments}\n\n"
                f"Reflections:\n{reflections}\n\n"
                f"Compact memory:\n{memory_text}\n\n"
                f"Recent visible timeline:\n{timeline}\n\n"
                f"Recent state:\n{state}\n\n"
                f"Next-action constraint:\n{next_constraint}\n\n"
                "Take exactly one bounded next action. If recent state contains search results, do not search the same query again. "
                "If recent state contains extracted page evidence, write an artifact before doing more search or browsing."
            ),
        },
    ]


def _operator_messages_for_prompt(
    job: dict[str, Any],
    *,
    active_messages: list[dict[str, Any]] | None = None,
    include_unclaimed: bool = True,
) -> str:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    lines = []
    active_messages = active_prompt_operator_entries(active_messages or [])
    active_ids = {active.get("event_id") for active in active_messages if isinstance(active, dict)}
    if active_messages:
        lines.append("Newly delivered operator messages for this turn:")
    for entry in active_messages:
        line = _operator_message_line(entry)
        if line:
            lines.append(line)
    active_context = [
        entry
        for entry in messages
        if isinstance(entry, dict)
        and operator_entry_is_prompt_relevant(entry)
        and (include_unclaimed or entry.get("claimed_at") or str(entry.get("mode") or "steer") == "note")
        and entry.get("event_id") not in active_ids
    ]
    if active_context:
        if lines:
            lines.append("Still-active durable operator context:")
        for entry in active_context[-6:]:
            line = _operator_message_line(entry)
            if line:
                lines.append(line)
    return "\n".join(lines) if lines else "No active operator context."


def _acknowledge_non_prompt_operator_context(db: AgentDB, job_id: str) -> int:
    job = db.get_job(job_id)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    message_ids = inactive_prompt_operator_ids(messages)
    if not message_ids:
        return 0
    result = db.acknowledge_operator_messages(
        job_id,
        message_ids=message_ids,
        summary="conversation-only message retained in history, not used as worker constraint",
    )
    return int(result.get("count") or 0)


def _operator_message_line(entry: dict[str, Any]) -> str:
    if not isinstance(entry, dict):
        return ""
    at = str(entry.get("at") or "")
    source = str(entry.get("source") or "operator")
    mode = str(entry.get("mode") or "steer")
    event_id = str(entry.get("event_id") or "")
    message = " ".join(str(entry.get("message") or "").split())
    if message:
        states = []
        if entry.get("claimed_at"):
            states.append("delivered")
        if entry.get("acknowledged_at"):
            states.append("acknowledged")
        if entry.get("superseded_at"):
            states.append("superseded")
        state_text = f" ({', '.join(states)})" if states else ""
        id_text = f" id={event_id}" if event_id else ""
        return f"-{id_text} {at} {source} {mode}{state_text}: {_clip_text(message, 420)}"
    return ""


def _lessons_for_prompt(job: dict[str, Any]) -> str:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    lessons = metadata.get("lessons") if isinstance(metadata.get("lessons"), list) else []
    if not lessons:
        return "No durable lessons yet."
    lines = []
    for entry in lessons[-5:]:
        if not isinstance(entry, dict):
            continue
        category = str(entry.get("category") or "memory")
        lesson = " ".join(str(entry.get("lesson") or "").split())
        if lesson:
            lines.append(f"- {category}: {_clip_text(lesson, SECTION_ITEM_CHARS)}")
    return "\n".join(lines) if lines else "No durable lessons yet."


def _metadata_list(job: dict[str, Any], key: str) -> list[dict[str, Any]]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    values = metadata.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _tasks_for_prompt(job: dict[str, Any]) -> str:
    tasks = _metadata_list(job, "task_queue")
    if not tasks:
        return (
            "No durable task queue yet. If the objective is broad, use record_tasks "
            "to create a few concrete open branches with output contracts and acceptance criteria before continuing."
        )
    status_rank = {"active": 0, "open": 1, "blocked": 2, "done": 3, "skipped": 4}
    ranked = sorted(
        tasks,
        key=lambda task: (status_rank.get(str(task.get("status") or "open"), 9), -_as_int(task.get("priority"))),
    )
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "open")
        counts[status] = counts.get(status, 0) + 1
    lines = ["Task counts: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))]
    selected = [task for task in ranked if str(task.get("status") or "open") in {"active", "open"}][:6]
    if len(selected) < 6:
        selected.extend([task for task in ranked if str(task.get("status") or "open") == "blocked"][: 6 - len(selected)])
    if len(selected) < 6:
        selected.extend([task for task in ranked if task not in selected][: 6 - len(selected)])
    for task in selected[:6]:
        bits = [
            str(task.get("status") or "open"),
            f"priority={task.get('priority') or 0}",
            str(task.get("title") or "untitled"),
        ]
        if task.get("output_contract"):
            bits.append(f"contract={task.get('output_contract')}")
        detail = " | ".join(bit for bit in bits if bit)
        if task.get("goal"):
            detail += f" | goal={task.get('goal')}"
        if task.get("acceptance_criteria"):
            detail += f" | accept={task.get('acceptance_criteria')}"
        if task.get("evidence_needed"):
            detail += f" | evidence={task.get('evidence_needed')}"
        if task.get("stall_behavior"):
            detail += f" | stall={task.get('stall_behavior')}"
        if task.get("source_hint"):
            detail += f" | source_hint={task.get('source_hint')}"
        if task.get("result"):
            detail += f" | result={task.get('result')}"
        lines.append("- " + _clip_text(detail, 520))
    return "\n".join(lines)


def _timeline_for_prompt(events: list[dict[str, Any]]) -> str:
    if not events:
        return "No timeline events yet."
    lines = []
    for event in events[-TIMELINE_PROMPT_EVENTS:]:
        event_type = str(event.get("event_type") or "event")
        if event_type == "operator_message":
            continue
        title = " ".join(str(event.get("title") or "").split())
        body = " ".join(str(event.get("body") or "").split())
        at = str(event.get("created_at") or "")
        detail = title if title else event_type
        if body:
            detail = f"{detail}: {body}"
        lines.append(f"- {at} {event_type}: {_clip_text(detail, SECTION_ITEM_CHARS)}")
    return "\n".join(lines)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ledgers_for_prompt(job: dict[str, Any]) -> str:
    findings = _metadata_list(job, "finding_ledger")
    sources = _metadata_list(job, "source_ledger")
    lines = [
        f"Finding ledger: {len(findings)} unique candidates.",
        f"Source ledger: {len(sources)} scored sources.",
    ]
    if findings:
        lines.append("Recent findings:")
        for finding in findings[-5:]:
            bits = [
                str(finding.get("name") or "unknown"),
                str(finding.get("category") or "").strip(),
                str(finding.get("location") or "").strip(),
                f"score={finding.get('score')}" if finding.get("score") is not None else "",
            ]
            lines.append("- " + _clip_text(" | ".join(bit for bit in bits if bit), 360))
    if sources:
        usable_sources = [
            source
            for source in sources
            if _as_float(source.get("usefulness_score")) >= 0.2
            or _as_int(source.get("yield_count")) > 0
        ]
        low_quality_sources = [
            source
            for source in sources
            if _as_float(source.get("usefulness_score")) < 0.2
            and _as_int(source.get("yield_count")) <= 0
            and (_as_int(source.get("fail_count")) > 0 or source.get("warnings"))
        ]
        ranked = sorted(
            usable_sources,
            key=lambda item: (_as_float(item.get("usefulness_score")), _as_int(item.get("yield_count"))),
            reverse=True,
        )
        if ranked:
            lines.append("High-yield/current sources:")
            for source in ranked[:4]:
                lines.append(
                    "- "
                    + _clip_text(
                        f"{source.get('source')} type={source.get('source_type') or 'unknown'} "
                        f"score={source.get('usefulness_score')} findings={source.get('yield_count') or 0} "
                        f"fails={source.get('fail_count') or 0} outcome={source.get('last_outcome') or ''}",
                        420,
                    )
                )
        if low_quality_sources:
            lines.append("Low-yield/blocked source patterns to avoid:")
            for source in low_quality_sources[-3:]:
                lines.append(
                    "- "
                    + _clip_text(
                        f"{source.get('source')} type={source.get('source_type') or 'unknown'} "
                        f"score={source.get('usefulness_score')} fails={source.get('fail_count') or 0} "
                        f"warnings={', '.join(source.get('warnings') or [])} outcome={source.get('last_outcome') or ''}",
                        420,
                    )
                )
    return "\n".join(lines)


def _experiments_for_prompt(job: dict[str, Any]) -> str:
    experiments = _metadata_list(job, "experiment_ledger")
    if not experiments:
        return (
            "No experiments tracked yet. If this objective involves improving, "
            "comparing, benchmarking, reducing, increasing, or otherwise measuring something, "
            "turn candidate ideas into record_experiment entries with exact config, metric, result, and next action."
        )
    measured = [experiment for experiment in experiments if experiment.get("metric_value") is not None]
    best = [
        experiment
        for experiment in measured
        if bool(experiment.get("best_observed"))
    ]
    status_counts: dict[str, int] = {}
    for experiment in experiments:
        status = str(experiment.get("status") or "planned")
        status_counts[status] = status_counts.get(status, 0) + 1
    lines = [
        f"Experiment counts: {', '.join(f'{key}={value}' for key, value in sorted(status_counts.items()))}.",
        f"Measured results: {len(measured)}.",
    ]
    if best:
        lines.append("Best observed results:")
        for experiment in best[-3:]:
            metric = (
                f"{experiment.get('metric_name') or 'metric'}={experiment.get('metric_value')}"
                f"{experiment.get('metric_unit') or ''}"
            )
            lines.append(
                "- "
                + _clip_text(" | ".join(
                    bit
                    for bit in [
                        str(experiment.get("title") or "experiment"),
                        metric,
                        f"result={experiment.get('result')}" if experiment.get("result") else "",
                        f"next={experiment.get('next_action')}" if experiment.get("next_action") else "",
                    ]
                    if bit
                ), 520)
            )
    recent = experiments[-4:]
    if recent:
        lines.append("Recent experiments:")
        for experiment in recent:
            metric = ""
            if experiment.get("metric_value") is not None:
                metric = (
                    f"{experiment.get('metric_name') or 'metric'}={experiment.get('metric_value')}"
                    f"{experiment.get('metric_unit') or ''}"
                )
            delta = ""
            if experiment.get("delta_from_previous_best") is not None:
                delta = f"delta={experiment.get('delta_from_previous_best')}"
            lines.append(
                "- "
                + _clip_text(" | ".join(
                    bit
                    for bit in [
                        str(experiment.get("status") or "planned"),
                        str(experiment.get("title") or "experiment"),
                        metric,
                        delta,
                        f"next={experiment.get('next_action')}" if experiment.get("next_action") else "",
                    ]
                    if bit
                ), 520)
            )
    return "\n".join(lines)


def _measured_progress_guard_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _measured_progress_guard_context(job, recent_steps)
    if not context:
        return "None."
    if _as_int(context.get("shell_actions_since_last_experiment")) >= _as_int(context.get("shell_action_budget")):
        return (
            "This objective or active task is measurably framed, and the shell/action budget since the last experiment "
            f"is exhausted. completed_since_last_experiment={context.get('completed_since_last_experiment')} "
            f"shell_actions={context.get('shell_actions_since_last_experiment')} shell_budget={context.get('shell_action_budget')} "
            f"reason={context.get('reason')}. Do not call shell_exec or do more research next. Use record_experiment "
            "for a known result, record_tasks to create a missing experiment/monitor branch, or record_lesson if the "
            "branch is blocked or the recent outputs were not valid measurements."
        )
    return (
        "This objective or active task is measurably framed, but recent work has not produced "
        f"new experiment records. completed_since_last_experiment={context.get('completed_since_last_experiment')} "
        f"research_budget={context.get('research_budget')} shell_actions={context.get('shell_actions_since_last_experiment')} "
        f"shell_budget={context.get('shell_action_budget')} reason={context.get('reason')}. "
        "Next useful actions: run a small measuring action, call record_experiment for a known result, "
        "or use record_tasks to create an experiment/action/monitor task with acceptance criteria and evidence."
    )


def _measurement_obligation_for_prompt(job: dict[str, Any]) -> str:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    obligation = metadata.get("pending_measurement_obligation")
    if not isinstance(obligation, dict) or not obligation or obligation.get("resolved_at"):
        return "None."
    candidates = obligation.get("metric_candidates") if isinstance(obligation.get("metric_candidates"), list) else []
    lines = [
        f"source_step=#{obligation.get('source_step_no') or '?'} tool={obligation.get('tool') or ''}",
        f"summary={obligation.get('summary') or ''}",
    ]
    command = str(obligation.get("command") or "")
    if command:
        lines.append(f"command={_clip_text(command, 360)}")
    if candidates:
        lines.append("metric_candidates=" + "; ".join(str(item) for item in candidates[:6]))
    lines.append(
        "Before more research or artifact churn, call record_experiment with the measured result, "
        "record_lesson explaining why it is not a valid measurement, or record_tasks to create the missing measurement branch."
    )
    return "\n".join(lines)


def _reflections_for_prompt(job: dict[str, Any]) -> str:
    reflections = _metadata_list(job, "reflections")
    if not reflections:
        return "No reflection checkpoints yet."
    lines = []
    for reflection in reflections[-2:]:
        strategy = f" strategy={reflection.get('strategy')}" if reflection.get("strategy") else ""
        lines.append("- " + _clip_text(f"{reflection.get('summary')}{strategy}", 520))
    return "\n".join(lines)


def _next_action_constraint(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    measurement_obligation = _pending_measurement_obligation(job)
    if measurement_obligation:
        return (
            "A pending measurement obligation is active from "
            f"step #{measurement_obligation.get('source_step_no') or '?'}. "
            "Resolve it with record_experiment, record_lesson explaining why it is invalid, "
            "or record_tasks creating the missing measurement branch before more research/artifact churn."
        )
    measured_guard = _measured_progress_guard_context(job, recent_steps)
    if measured_guard:
        return (
            "This job needs measured progress, not more research-only activity. "
            "Do one of: run a small measuring command/action, call record_experiment for a known measurement, "
            "record_tasks with an experiment/action/monitor contract, or record_lesson if measurement is blocked."
        )
    evidence_step = _unpersisted_evidence_step(recent_steps)
    if evidence_step:
        return (
            f"You have unsaved evidence from step #{evidence_step['step_no']} "
            f"({evidence_step.get('tool_name') or evidence_step['kind']}). "
            "Your next tool call should usually be write_artifact. If this evidence taught a durable rule, record_lesson after saving it."
        )
    if _task_queue_exhausted(job):
        return (
            "All durable task branches are done, skipped, or blocked. Before more research or execution, "
            "use record_tasks to open the next concrete branch, or report_update if the operator needs a checkpoint."
        )
    for step in reversed(recent_steps[-5:]):
        error = str(step.get("error") or "")
        if error == "artifact required before more research":
            return "The last blocked action needs write_artifact, not another search or browser action."
        if error == "task branch required before more work":
            return "Create or reopen a task branch with record_tasks before doing more research or execution."
        if error in {"duplicate tool call blocked", "similar search query blocked", "search loop blocked"}:
            output = step.get("output") if isinstance(step.get("output"), dict) else {}
            blocked_tool = str(output.get("blocked_tool") or "")
            if blocked_tool == "read_artifact":
                return "Do not read the same artifact again. Use its content to choose a concrete next action: inspect a specific item, record findings/tasks, or write a report artifact."
            if blocked_tool == "shell_exec":
                return "Do not rerun the same shell discovery command. Use the prior output to inspect a specific file/item, save it, or update findings/tasks."
            return "Change source, extract an existing result, save an artifact, or record a lesson about the failed strategy."
    return "No special constraint beyond taking one bounded useful action."


def _task_queue_exhausted(job: dict[str, Any]) -> bool:
    tasks = _metadata_list(job, "task_queue")
    if not tasks:
        return False
    runnable = {"open", "active"}
    return not any(str(task.get("status") or "open").strip().lower() in runnable for task in tasks)


def _parse_tool_result(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    except json.JSONDecodeError:
        return {"result": raw}


def _load_program_text(config: AppConfig, job_id: str) -> str:
    path = config.runtime.jobs_dir / job_id / "program.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _compact(value: Any, limit: int = 500) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _clip_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _format_step_for_prompt(step: dict[str, Any]) -> str:
    tool = f" tool={step['tool_name']}" if step.get("tool_name") else ""
    summary = step.get("summary") or step.get("error") or ""
    pieces = [f"- #{step['step_no']} {step['kind']} {step['status']}{tool}: {summary}"]
    input_data = step.get("input") or {}
    args = input_data.get("arguments") if isinstance(input_data, dict) else None
    if args:
        pieces.append(f"  args: {_compact(args, 320)}")
    output = step.get("output") or {}
    observation = _observation_for_prompt(step.get("tool_name"), output)
    if observation:
        pieces.append(f"  observed: {observation}")
    return "\n".join(pieces)


def _observation_for_prompt(tool_name: str | None, output: dict[str, Any]) -> str:
    if not output:
        return ""
    if tool_name == "web_search":
        results = output.get("results") if isinstance(output.get("results"), list) else []
        titles = []
        for result in results[:5]:
            title = result.get("title") or "untitled"
            url = result.get("url") or ""
            titles.append(f"{title} <{url}>")
        return _clip_text(f"query={output.get('query')!r}; results={'; '.join(titles)}", 650)
    if tool_name == "web_extract":
        pages = output.get("pages") if isinstance(output.get("pages"), list) else []
        parts = []
        for page in pages[:3]:
            if page.get("error"):
                parts.append(f"{page.get('url')}: ERROR {page.get('error')}")
            else:
                text = str(page.get("text") or "")
                parts.append(f"{page.get('url')}: {_clip_text(text, 160)}")
        return _clip_text("; ".join(parts), 650)
    if tool_name == "shell_exec":
        stdout = str(output.get("stdout") or "")
        stderr = str(output.get("stderr") or "")
        excerpt = stdout.strip() or stderr.strip()
        return (
            f"command={output.get('command')!r}; rc={output.get('returncode')}; "
            f"duration={output.get('duration_seconds')}s; output={_clip_text(excerpt, 360)}"
        )[:650]
    if tool_name == "write_artifact":
        return f"saved artifact={output.get('artifact_id')} path={output.get('path')}"
    if tool_name == "report_update":
        update = output.get("update") if isinstance(output.get("update"), dict) else {}
        return _clip_text(f"agent_update={update.get('message') or ''}", 420)
    if tool_name == "record_lesson":
        lesson = output.get("lesson") if isinstance(output.get("lesson"), dict) else {}
        return _clip_text(f"lesson={lesson.get('category') or 'memory'}: {lesson.get('lesson') or ''}", 420)
    if tool_name == "record_source":
        source = output.get("source") if isinstance(output.get("source"), dict) else {}
        return (
            f"source={source.get('source')} score={source.get('usefulness_score')} "
            f"findings={source.get('yield_count')} fails={source.get('fail_count')} outcome={source.get('last_outcome')}"
        )[:420]
    if tool_name == "record_findings":
        return f"finding ledger updated added={output.get('added')} updated={output.get('updated')}"[:700]
    if tool_name == "record_experiment":
        experiment = output.get("experiment") if isinstance(output.get("experiment"), dict) else {}
        metric = ""
        if experiment.get("metric_value") is not None:
            metric = f"{experiment.get('metric_name') or 'metric'}={experiment.get('metric_value')}{experiment.get('metric_unit') or ''}"
        delta = f" delta={experiment.get('delta_from_previous_best')}" if experiment.get("delta_from_previous_best") is not None else ""
        best = " best_observed" if experiment.get("best_observed") else ""
        return _clip_text(f"experiment={experiment.get('title')} status={experiment.get('status')} {metric}{delta}{best}", 520)
    if tool_name == "acknowledge_operator_context":
        return f"operator_context {output.get('status')} count={output.get('count')}"[:700]
    if tool_name in {"browser_click", "browser_type"} and output.get("error"):
        recovery = output.get("recovery_snapshot") if isinstance(output.get("recovery_snapshot"), dict) else {}
        candidates = _browser_candidates_for_prompt(recovery)
        suffix = f"; recovery_candidates={candidates}" if candidates else ""
        return _clip_text(f"error={output.get('error')}; guidance={output.get('recovery_guidance', '')}{suffix}", 700)
    if tool_name == "browser_navigate":
        data = output.get("data") if isinstance(output.get("data"), dict) else {}
        title = data.get("title") or ""
        url = data.get("url") or ""
        snapshot = str(output.get("snapshot") or "")
        warning = anti_bot_reason(title, url, snapshot)
        suffix = f"; source_warning={warning}" if warning else ""
        candidates = _browser_candidates_for_prompt(output)
        candidate_suffix = f"; candidates={candidates}" if candidates else ""
        return _clip_text(f"opened {title} <{url}>; snapshot_chars={len(snapshot)}{suffix}{candidate_suffix}", 700)
    if tool_name == "browser_snapshot":
        data = output.get("data") if isinstance(output.get("data"), dict) else {}
        snapshot = str(output.get("snapshot") or data.get("snapshot") or output.get("data") or "")
        warning = anti_bot_reason(snapshot)
        suffix = f"; source_warning={warning}" if warning else ""
        candidates = _browser_candidates_for_prompt(output)
        candidate_suffix = f"; candidates={candidates}" if candidates else ""
        return _clip_text(f"snapshot_chars={len(snapshot)}{suffix}{candidate_suffix}", 700)
    if output.get("error"):
        return f"error={output.get('error')}"
    return _compact(output, 700)


def _browser_warning_context(output: dict[str, Any]) -> dict[str, str] | None:
    data = output.get("data") if isinstance(output.get("data"), dict) else {}
    title = str(data.get("title") or "")
    url = str(data.get("url") or data.get("origin") or output.get("url") or "")
    snapshot = str(output.get("snapshot") or data.get("snapshot") or output.get("data") or "")
    reason = anti_bot_reason(title, url, snapshot)
    if not reason:
        return None
    return {"reason": reason, "url": url, "title": title}


def _recent_anti_bot_context(recent_steps: list[dict[str, Any]], *, window: int = 8) -> dict[str, Any] | None:
    for step in reversed(recent_steps[-window:]):
        if step.get("status") != "completed" or step.get("tool_name") not in {"browser_navigate", "browser_snapshot"}:
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        warning = _browser_warning_context(output)
        if warning:
            return {**warning, "step_id": step.get("id"), "step_no": step.get("step_no")}
    return None


def _artifact_args_acknowledge_block(args: dict[str, Any]) -> bool:
    text = " ".join(str(args.get(key) or "") for key in ("title", "summary", "content")).lower()
    return any(term in text for term in ANTI_BOT_ACK_TERMS)


def _same_source_url(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left.split("#", 1)[0].rstrip("/") == right.split("#", 1)[0].rstrip("/")


def _normalized_source_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        return f"https://{value}"
    return value


def _source_host(value: str) -> str:
    parsed = urlparse(_normalized_source_url(value))
    return parsed.netloc.lower().removeprefix("www.")


def _source_matches(left: str, right: str) -> bool:
    if _same_source_url(left, right):
        return True
    left_host = _source_host(left)
    right_host = _source_host(right)
    return bool(left_host and right_host and left_host == right_host)


def _known_bad_sources(job: dict[str, Any]) -> list[dict[str, Any]]:
    bad_sources = []
    for source in _metadata_list(job, "source_ledger"):
        if (
            _as_float(source.get("usefulness_score")) < 0.2
            and _as_int(source.get("yield_count")) <= 0
            and (_as_int(source.get("fail_count")) > 0 or source.get("warnings"))
        ):
            bad_sources.append(source)
    return bad_sources


def _known_bad_source_for_call(name: str, args: dict[str, Any], job: dict[str, Any]) -> dict[str, Any] | None:
    if name not in {"browser_navigate", "web_extract"}:
        return None
    bad_sources = _known_bad_sources(job)
    if not bad_sources:
        return None
    urls: list[str] = []
    if name == "browser_navigate":
        urls = [str(args.get("url") or "")]
    elif isinstance(args.get("urls"), list):
        urls = [str(url) for url in args["urls"]]
    for url in [url for url in urls if url.strip()]:
        for source in bad_sources:
            source_value = str(source.get("source") or "")
            if source_value and _source_matches(url, source_value):
                return source
    return None


def _tool_signature(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"


def _browser_candidates_for_prompt(output: dict[str, Any], *, limit: int = 18) -> str:
    refs = output.get("refs") if isinstance(output.get("refs"), dict) else None
    if refs is None:
        data = output.get("data") if isinstance(output.get("data"), dict) else {}
        refs = data.get("refs") if isinstance(data.get("refs"), dict) else {}
    candidates = []
    seen = set()
    for ref, item in refs.items():
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        if role not in {"link", "heading", "cell"}:
            continue
        name = " ".join(str(item.get("name") or "").split())
        key = name.lower().strip()
        if not name or key in BROWSER_REF_IGNORE_NAMES:
            continue
        if len(name) < 3 or len(name) > 90 or key in seen:
            continue
        if role == "cell" and (_looks_like_metric_cell(name) or _looks_like_service_description(name)):
            continue
        seen.add(key)
        candidates.append(f"{name} (@{ref})")
        if len(candidates) >= limit:
            break
    return "; ".join(candidates)


def _looks_like_metric_cell(name: str) -> bool:
    text = name.strip()
    return bool(re.fullmatch(r"(?:n/?a|na|[-+]?\d+(?:\.\d+)?(?:/5)?|[$€£]?\d[\d,]*(?:\.\d+)?%?)", text, re.I))


def _looks_like_service_description(name: str) -> bool:
    text = name.lower()
    if "," in text and len(text.split()) >= 6:
        return True
    service_terms = ("custom ecommerce", "ux/ui", "payment integration", "mobile responsiveness", "headless commerce")
    return any(term in text for term in service_terms) and len(text.split()) >= 5


def _duplicate_recent_tool_call(
    name: str,
    args: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    window: int = 24,
) -> dict[str, Any] | None:
    if name == "browser_snapshot":
        return None
    signature = _tool_signature(name, args)
    for step in reversed(recent_steps[-window:]):
        if step.get("status") != "completed" or step.get("tool_name") != name:
            continue
        input_data = step.get("input") or {}
        previous_args = input_data.get("arguments") if isinstance(input_data, dict) else None
        if isinstance(previous_args, dict) and _tool_signature(name, previous_args) == signature:
            return step
    return None


def _completed_recent_steps(recent_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in recent_steps if step.get("status") == "completed"]


def _step_has_evidence(step: dict[str, Any]) -> bool:
    tool_name = step.get("tool_name")
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    if tool_name == "web_extract":
        pages = output.get("pages") if isinstance(output.get("pages"), list) else []
        for page in pages:
            if page.get("error"):
                continue
            if str(page.get("text") or "").strip():
                return True
    if tool_name in {"browser_navigate", "browser_snapshot"}:
        data = output.get("data") if isinstance(output.get("data"), dict) else {}
        snapshot = str(output.get("snapshot") or data.get("snapshot") or "")
        if anti_bot_reason(str(data.get("title") or ""), str(data.get("url") or data.get("origin") or ""), snapshot):
            return False
        return len(snapshot.strip()) >= 500
    if tool_name == "shell_exec":
        text = "\n".join(str(output.get(key) or "") for key in ("stdout", "stderr"))
        return len(text.strip()) >= 1000
    return False


def _unpersisted_evidence_step(recent_steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    for step in reversed(recent_steps):
        if step.get("status") not in {"completed", "blocked"}:
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        if step.get("tool_name") == "write_artifact":
            return None
        if isinstance(output.get("auto_checkpoint"), dict):
            return None
        if step.get("status") == "completed" and _step_has_evidence(step):
            return step
    return None


def _recent_search_streak(recent_steps: list[dict[str, Any]]) -> int:
    streak = 0
    for step in reversed(_completed_recent_steps(recent_steps)):
        tool_name = step.get("tool_name")
        if tool_name == "web_search":
            streak += 1
            continue
        if tool_name:
            break
    return streak


def _pending_measurement_obligation(job: dict[str, Any]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    obligation = metadata.get("pending_measurement_obligation")
    if isinstance(obligation, dict) and obligation and not obligation.get("resolved_at"):
        return obligation
    return None


def _clear_invalid_measurement_obligation(db: AgentDB, job_id: str) -> bool:
    job = db.get_job(job_id)
    obligation = _pending_measurement_obligation(job)
    if not obligation:
        return False
    candidates = obligation.get("metric_candidates") if isinstance(obligation.get("metric_candidates"), list) else []
    if not candidates:
        return False
    command = str(obligation.get("command") or "")
    has_intent = bool(MEASUREMENT_INTENT_PATTERN.search(command))
    if not all(_candidate_is_diagnostic_only(str(candidate), has_intent) for candidate in candidates):
        return False
    db.update_job_metadata(job_id, {"pending_measurement_obligation": {}})
    db.append_agent_update(
        job_id,
        "Cleared measurement obligation because the output was diagnostic context, not a trial result.",
        category="progress",
        metadata={"cleared_measurement_obligation": obligation},
    )
    return True


def _progress_churn_context(recent_steps: list[dict[str, Any]], *, window: int = 10) -> dict[str, Any] | None:
    completed = [step for step in recent_steps if step.get("status") == "completed"]
    tail = completed[-window:]
    if len(tail) < 8:
        return None
    if any(step.get("tool_name") in LEDGER_PROGRESS_TOOLS for step in tail):
        return None
    churn_count = sum(1 for step in tail if step.get("tool_name") in CHURN_TOOLS)
    if churn_count < 7:
        return None
    return {
        "window": len(tail),
        "churn_count": churn_count,
        "since_step": tail[0].get("step_no"),
        "tools": [step.get("tool_name") or step.get("kind") for step in tail],
    }


def _job_requires_measured_progress(job: dict[str, Any]) -> bool:
    text_parts = [
        str(job.get("title") or ""),
        str(job.get("objective") or ""),
        str(job.get("kind") or ""),
    ]
    tasks = _metadata_list(job, "task_queue")
    for task in tasks:
        status = str(task.get("status") or "open")
        if status in {"done", "skipped"}:
            continue
        contract = str(task.get("output_contract") or "")
        if contract in {"experiment", "monitor"}:
            return True
        if contract == "action" and _task_text_requires_measurement(task):
            return True
        text_parts.extend(
            str(task.get(key) or "")
            for key in ("title", "goal", "acceptance_criteria", "evidence_needed", "stall_behavior")
        )
    return any(MEASURABLE_PROGRESS_PATTERN.search(part) for part in text_parts if part)


def _task_text_requires_measurement(task: dict[str, Any]) -> bool:
    return any(
        MEASURABLE_PROGRESS_PATTERN.search(str(task.get(key) or ""))
        for key in ("title", "goal", "acceptance_criteria", "evidence_needed", "stall_behavior")
    )


def _measured_progress_guard_context(
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    budget: int = MEASURABLE_RESEARCH_BUDGET_STEPS,
) -> dict[str, Any] | None:
    if not _job_requires_measured_progress(job):
        return None
    if _pending_measurement_obligation(job):
        return None
    completed = [step for step in recent_steps if step.get("status") == "completed"]
    if not completed:
        return None
    last_experiment_index = -1
    for index, step in enumerate(completed):
        if step.get("tool_name") == "record_experiment":
            last_experiment_index = index
    tail = completed[last_experiment_index + 1 :]
    branch_activity = [step for step in tail if step.get("tool_name") in BRANCH_WORK_TOOLS | {"write_artifact"}]
    shell_actions = [step for step in tail if step.get("tool_name") == "shell_exec"]
    if len(branch_activity) < budget and len(shell_actions) < MEASURABLE_ACTION_BUDGET_STEPS:
        return None
    if any(step.get("tool_name") in {"record_tasks", "record_lesson"} for step in tail[-6:]):
        return None
    experiments = _metadata_list(job, "experiment_ledger")
    reason = "no experiment records yet" if not experiments else "no recent experiment update"
    return {
        "reason": reason,
        "research_budget": budget,
        "shell_action_budget": MEASURABLE_ACTION_BUDGET_STEPS,
        "completed_since_last_experiment": len(tail),
        "branch_activity": len(branch_activity),
        "shell_actions_since_last_experiment": len(shell_actions),
        "since_step": branch_activity[0].get("step_no") if branch_activity else None,
        "tools": [step.get("tool_name") or step.get("kind") for step in branch_activity[-10:]],
    }


MEASUREMENT_PATTERN = re.compile(
    r"(?i)(?:"
    r"\b\d+(?:\.\d+)?\s*(?:%|ms|s|sec|secs|seconds|msec|us|hz|khz|mhz|ghz|kb/s|mb/s|gb/s|tb/s|"
    r"it/s|ops/s|req/s|qps|rps|samples/s|items/s|units/s|tokens/s|tok/s|t/s)\b"
    r"|(?:score|rate|speed|throughput|latency|accuracy|loss|error|duration|runtime|time|memory|cpu|gpu|ram)\D{0,40}\d+(?:\.\d+)?"
    r")"
)
MEASUREMENT_INTENT_PATTERN = re.compile(
    r"(?i)\b(bench(?:mark)?|compare|duration|eval(?:uate)?|experiment|hyperfine|latency|measure|metric|perf|"
    r"profile|rate|runtime|speed|test|throughput|time|trial)\b"
)
DIAGNOSTIC_MEASUREMENT_PATTERN = re.compile(r"(?i)^\s*(?:cpu|gpu|memory|mem|ram)\b")
ACTION_MEASUREMENT_PATTERN = re.compile(
    r"(?i)^\s*(?:score|rate|speed|throughput|latency|accuracy|loss|error|duration|runtime|time)\b"
)
LABELED_MEASUREMENT_PATTERN = re.compile(
    r"(?i)^\s*(?:score|rate|speed|throughput|latency|accuracy|loss|error|duration|runtime|time)\s*(?:=|:)\s*[-+]?\d"
)
EXPLICIT_RESULT_UNIT_PATTERN = re.compile(
    r"(?i)\b\d+(?:\.\d+)?\s*(?:ms|msec|sec|secs|seconds|it/s|ops/s|req/s|qps|rps|samples/s|items/s|units/s|"
    r"tokens/s|tok/s|t/s|kb/s|mb/s|gb/s|tb/s)\b"
)


def _measurement_candidates(output: dict[str, Any], *, command: str = "", limit: int = 8) -> list[str]:
    text = "\n".join(
        str(output.get(key) or "")
        for key in ("stdout", "stderr", "result", "content")
        if output.get(key) is not None
    )
    if not text.strip():
        return []
    command_has_measurement_intent = bool(MEASUREMENT_INTENT_PATTERN.search(command))
    candidates: list[str] = []
    for match in MEASUREMENT_PATTERN.finditer(text[:20000]):
        candidate = " ".join(match.group(0).split())
        if _candidate_is_diagnostic_only(candidate, command_has_measurement_intent):
            continue
        if candidate not in candidates:
            candidates.append(candidate[:140])
        if len(candidates) >= limit:
            break
    return candidates


def _candidate_is_diagnostic_only(candidate: str, command_has_measurement_intent: bool) -> bool:
    if command_has_measurement_intent:
        return False
    if DIAGNOSTIC_MEASUREMENT_PATTERN.search(candidate):
        return True
    if EXPLICIT_RESULT_UNIT_PATTERN.search(candidate) and not re.search(r"(?i)\b(?:cpu|gpu|ram|mem|memory)\b", candidate):
        return False
    if ACTION_MEASUREMENT_PATTERN.search(candidate):
        return not bool(LABELED_MEASUREMENT_PATTERN.search(candidate))
    return True


def _maybe_create_measurement_obligation(
    *,
    db: AgentDB,
    job_id: str,
    step: dict[str, Any] | None,
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    if tool_name != "shell_exec":
        return
    command = str(args.get("command") or result.get("command") or "")
    candidates = _measurement_candidates(result, command=command)
    if not candidates:
        return
    metadata = db.get_job(job_id).get("metadata")
    if isinstance(metadata, dict):
        existing = metadata.get("pending_measurement_obligation")
        if isinstance(existing, dict) and existing and not existing.get("resolved_at"):
            return
    obligation = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_step_id": step.get("id") if step else "",
        "source_step_no": step.get("step_no") if step else None,
        "tool": tool_name,
        "summary": "Tool output contains measurable-looking results that need experiment accounting.",
        "metric_candidates": candidates,
        "command": command[:1000],
    }
    db.update_job_metadata(job_id, {"pending_measurement_obligation": obligation})
    db.append_agent_update(
        job_id,
        f"Measured output needs accounting: {', '.join(candidates[:3])}.",
        category="blocked",
        metadata={"pending_measurement_obligation": obligation},
    )


def _step_by_id(db: AgentDB, job_id: str, step_id: str) -> dict[str, Any] | None:
    for step in db.list_steps(job_id=job_id):
        if str(step.get("id") or "") == step_id:
            return step
    return None


def _search_query(args: dict[str, Any]) -> str:
    return str(args.get("query") or "").strip()


def _query_tokens(query: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", query.lower())
        if len(token) > 2 and token not in QUERY_STOPWORDS
    }


def _similar_recent_search(
    args: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    window: int = 12,
) -> dict[str, Any] | None:
    query = _search_query(args)
    tokens = _query_tokens(query)
    if len(tokens) < 2:
        return None
    for step in reversed(_completed_recent_steps(recent_steps)[-window:]):
        if step.get("tool_name") != "web_search":
            continue
        input_data = step.get("input") or {}
        previous_args = input_data.get("arguments") if isinstance(input_data, dict) else None
        if not isinstance(previous_args, dict):
            continue
        previous_query = _search_query(previous_args)
        previous_tokens = _query_tokens(previous_query)
        if len(previous_tokens) < 2:
            continue
        overlap = len(tokens & previous_tokens) / max(len(tokens), len(previous_tokens))
        if overlap >= 0.72:
            return step
    return None


def _blocked_tool_call_result(
    name: str,
    args: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    job: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    duplicate_step = _duplicate_recent_tool_call(name, args, recent_steps)
    if duplicate_step:
        guidance = "Use a different query, extract one of the prior result URLs, open a result in the browser, or write an artifact."
        if name == "read_artifact":
            guidance = (
                "This artifact was already read. Do not read it again; use its content to inspect a concrete item, "
                "record findings/tasks, or write a report artifact."
            )
        elif name == "shell_exec":
            guidance = (
                "This shell command was already run. Do not rerun discovery; use the previous output to inspect a "
                "specific file/item, write an artifact, or update findings/tasks."
            )
        result = {
            "success": False,
            "error": "duplicate tool call blocked",
            "blocked_tool": name,
            "blocked_arguments": args,
            "previous_step": duplicate_step["id"],
            "guidance": guidance,
        }
        return result, f"blocked duplicate {name}; previous step #{duplicate_step['step_no']}"

    measurement_obligation = _pending_measurement_obligation(job)
    if measurement_obligation and name in MEASUREMENT_BLOCKED_TOOLS and name not in MEASUREMENT_RESOLUTION_TOOLS:
        result = {
            "success": False,
            "error": "measurement obligation pending",
            "blocked_tool": name,
            "blocked_arguments": args,
            "pending_measurement_obligation": measurement_obligation,
            "guidance": (
                "A recent action produced measurable output. Record it with record_experiment, "
                "explain why it is invalid with record_lesson, or create the missing measurement branch with record_tasks "
                "before doing more research, artifact writing, or finding/source updates."
            ),
        }
        return result, f"blocked {name}; record_experiment required after measured output"

    measured_progress_guard = _measured_progress_guard_context(job, recent_steps)
    progress_churn = _progress_churn_context(recent_steps)
    if progress_churn and not measured_progress_guard and name in CHURN_TOOLS:
        result = {
            "success": False,
            "error": "progress ledger update required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "progress_churn": progress_churn,
            "guidance": (
                "Recent activity has not changed findings, experiments, tasks, lessons, or sources. "
                "Use a ledger tool to record progress, reject the branch, or create a pivot task before continuing."
            ),
        }
        return result, f"blocked {name}; progress ledger update required"

    anti_bot_context = _recent_anti_bot_context(recent_steps)
    if anti_bot_context:
        blocked_browser_followups = {"browser_click", "browser_console", "browser_press", "browser_scroll", "browser_snapshot", "browser_type"}
        if name in blocked_browser_followups:
            result = {
                "success": False,
                "error": "anti-bot source loop blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "anti_bot_source": anti_bot_context,
                "guidance": "This page is blocked by anti-bot/CAPTCHA. Record the source as blocked and pivot to a different public source.",
            }
            return result, f"blocked {name}; anti-bot source at step #{anti_bot_context.get('step_no')}"
        if name == "browser_navigate" and _same_source_url(str(args.get("url") or ""), str(anti_bot_context.get("url") or "")):
            result = {
                "success": False,
                "error": "anti-bot source loop blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "anti_bot_source": anti_bot_context,
                "guidance": "Do not reopen the same blocked source. Pivot to another source.",
            }
            return result, f"blocked {name}; repeated blocked source from step #{anti_bot_context.get('step_no')}"
        if name == "web_extract":
            urls = args.get("urls") if isinstance(args.get("urls"), list) else []
            if any(_same_source_url(str(url), str(anti_bot_context.get("url") or "")) for url in urls):
                result = {
                    "success": False,
                    "error": "anti-bot source loop blocked",
                    "blocked_tool": name,
                    "blocked_arguments": args,
                    "anti_bot_source": anti_bot_context,
                    "guidance": "Do not extract the same blocked source. Record it as low-yield and pivot.",
                }
                return result, f"blocked {name}; blocked source from step #{anti_bot_context.get('step_no')}"
        if name == "write_artifact" and not _artifact_args_acknowledge_block(args):
            result = {
                "success": False,
                "error": "misleading blocked-source artifact blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "anti_bot_source": anti_bot_context,
                "guidance": "The latest browser evidence is an anti-bot/CAPTCHA block. Write only a blocked-source note or pivot.",
            }
            return result, f"blocked misleading write_artifact; anti-bot source at step #{anti_bot_context.get('step_no')}"

    unpersisted_evidence = _unpersisted_evidence_step(recent_steps)
    if unpersisted_evidence and name in BRANCH_WORK_TOOLS:
        result = {
            "success": False,
            "error": "artifact required before more research",
            "blocked_tool": name,
            "blocked_arguments": args,
            "previous_step": unpersisted_evidence["id"],
            "guidance": "Write an artifact summarizing the browser, extracted, or shell evidence before doing more search, browsing, or shell work.",
        }
        return result, f"blocked {name}; write_artifact required after evidence step #{unpersisted_evidence['step_no']}"

    shell_budget_exhausted = (
        name == "shell_exec"
        and _as_int(measured_progress_guard.get("shell_actions_since_last_experiment")) >= MEASURABLE_ACTION_BUDGET_STEPS
    ) if measured_progress_guard else False
    if measured_progress_guard and (name in MEASURABLE_RESEARCH_BLOCKED_TOOLS or shell_budget_exhausted):
        result = {
            "success": False,
            "error": "measured progress required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "measured_progress_guard": measured_progress_guard,
            "guidance": (
                "This job is measurably framed and has exhausted its research budget without new experiment records. "
                "If the shell/action budget is exhausted, do not call shell_exec again; call record_experiment for a "
                "known measurement, record_tasks with an experiment/action/monitor contract, or record_lesson if "
                "measurement is blocked."
            ),
        }
        return result, f"blocked {name}; measured progress required"

    if name in BRANCH_WORK_TOOLS and _task_queue_exhausted(job):
        result = {
            "success": False,
            "error": "task branch required before more work",
            "blocked_tool": name,
            "blocked_arguments": args,
            "guidance": (
                "The durable task queue has no open or active branch. Use record_tasks to open the next concrete "
                "branch before doing more research or execution, or report_update if the operator needs a checkpoint."
            ),
        }
        return result, f"blocked {name}; no open task branch"

    known_bad_source = _known_bad_source_for_call(name, args, job)
    if known_bad_source:
        result = {
            "success": False,
            "error": "known bad source blocked",
            "blocked_tool": name,
            "blocked_arguments": args,
            "known_bad_source": known_bad_source,
            "guidance": (
                "The source ledger marks this source as blocked or low-yield for this job. "
                "Choose a different source, or record a fresh operator reason before retrying it."
            ),
        }
        return result, f"blocked {name}; known bad source {known_bad_source.get('source')}"

    if name == "web_search":
        similar_step = _similar_recent_search(args, recent_steps)
        if similar_step:
            result = {
                "success": False,
                "error": "similar search query blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "previous_step": similar_step["id"],
                "guidance": "Use an existing result URL, extract a page, or search a clearly different topic/location/source.",
            }
            return result, f"blocked similar web_search; previous step #{similar_step['step_no']}"
        streak = _recent_search_streak(recent_steps)
        if streak >= 3:
            result = {
                "success": False,
                "error": "search loop blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "recent_search_streak": streak,
                "guidance": "Stop searching. Extract or open one of the prior results, then write an artifact.",
            }
            return result, f"blocked web_search after {streak} consecutive searches"

    return None


def _summarize_tool_result(name: str, args: dict[str, Any], result: dict[str, Any], *, ok: bool) -> str:
    if not ok:
        return f"{name} failed: {result.get('error') or 'unknown error'}"
    if name == "web_search":
        results = result.get("results") if isinstance(result.get("results"), list) else []
        top = "; ".join((item.get("title") or "untitled") for item in results[:3])
        return f"web_search query={args.get('query')!r} returned {len(results)} results: {top}"
    if name == "web_extract":
        pages = result.get("pages") if isinstance(result.get("pages"), list) else []
        ok_pages = [page for page in pages if not page.get("error")]
        return f"web_extract fetched {len(ok_pages)}/{len(pages)} pages"
    if name == "shell_exec":
        command = str(result.get("command") or args.get("command") or "")
        return (
            f"shell_exec rc={result.get('returncode')} "
            f"duration={result.get('duration_seconds')}s cmd={command!r}"
        )
    if name == "write_artifact":
        return f"write_artifact saved {result.get('artifact_id')} at {result.get('path')}"
    if name == "report_update":
        update = result.get("update") if isinstance(result.get("update"), dict) else {}
        return f"report_update saved: {str(update.get('message') or '')[:160]}"
    if name == "record_lesson":
        lesson = result.get("lesson") if isinstance(result.get("lesson"), dict) else {}
        category = lesson.get("category") or "memory"
        text = str(lesson.get("lesson") or "")[:160]
        return f"record_lesson saved {category}: {text}"
    if name == "record_source":
        source = result.get("source") if isinstance(result.get("source"), dict) else {}
        return f"record_source updated {source.get('source')} score={source.get('usefulness_score')} yield={source.get('yield_count')}"
    if name == "record_findings":
        return (
            f"record_findings updated ledger: {result.get('added', 0)} new, "
            f"{result.get('updated', 0)} updated, {result.get('sources_updated', 0)} sources"
        )
    if name == "record_tasks":
        return f"record_tasks updated queue: {result.get('added', 0)} new, {result.get('updated', 0)} updated"
    if name == "record_experiment":
        experiment = result.get("experiment") if isinstance(result.get("experiment"), dict) else {}
        metric = ""
        if experiment.get("metric_value") is not None:
            metric = f" {experiment.get('metric_name') or 'metric'}={experiment.get('metric_value')}{experiment.get('metric_unit') or ''}"
        best = " best" if experiment.get("best_observed") else ""
        return f"record_experiment {experiment.get('status')}: {experiment.get('title')}{metric}{best}"
    if name == "acknowledge_operator_context":
        return f"acknowledge_operator_context {result.get('status')} count={result.get('count', 0)}"
    if name == "browser_navigate":
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        title = data.get("title") or ""
        url = data.get("url") or ""
        warning = f" | warning={result.get('source_warning')}" if result.get("source_warning") else ""
        return f"browser_navigate opened {title} <{url}>{warning}"
    if name == "browser_snapshot":
        snapshot = str(result.get("snapshot") or result.get("data") or "")
        warning = f" | warning={result.get('source_warning')}" if result.get("source_warning") else ""
        return f"browser_snapshot returned {len(snapshot)} chars{warning}"
    if name == "read_artifact":
        return f"read_artifact read {result.get('artifact_id')}"
    if name == "search_artifacts":
        results = result.get("results") if isinstance(result.get("results"), list) else []
        return f"search_artifacts returned {len(results)} results for {args.get('query')!r}"
    return f"{name} completed"


def _error_result(exc: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }
    if isinstance(exc, LLMResponseError) and exc.payload:
        result["provider_payload"] = exc.payload
    return result


def _max_step_no(steps: list[dict[str, Any]]) -> int:
    return max((int(step.get("step_no") or 0) for step in steps), default=0)


def _should_reflect(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> bool:
    if not recent_steps:
        return False
    if recent_steps[-1].get("kind") == "reflection":
        return False
    step_no = _max_step_no(recent_steps)
    if step_no == 0 or step_no % REFLECTION_INTERVAL_STEPS != 0:
        return False
    reflections = _metadata_list(job, "reflections")
    if not reflections:
        return True
    last_reflected = 0
    metadata = reflections[-1].get("metadata") if isinstance(reflections[-1].get("metadata"), dict) else {}
    if isinstance(metadata.get("through_step"), int):
        last_reflected = metadata["through_step"]
    return step_no > last_reflected


def _claim_operator_queue(db: AgentDB, job_id: str) -> list[dict[str, Any]]:
    steering = db.claim_operator_messages(job_id, modes=("steer",), limit=1)
    if steering:
        return steering
    return db.claim_operator_messages(job_id, modes=("follow_up",), limit=1)


def _emit_loop_start(db: AgentDB, job_id: str, run_id: str) -> None:
    db.append_event(
        job_id,
        event_type="loop",
        title="agent_start",
        ref_table="job_runs",
        ref_id=run_id,
        metadata={"run_id": run_id},
    )
    db.append_event(
        job_id,
        event_type="loop",
        title="turn_start",
        ref_table="job_runs",
        ref_id=run_id,
        metadata={"run_id": run_id},
    )


def _emit_assistant_message_event(
    db: AgentDB,
    job_id: str,
    run_id: str,
    response: LLMResponse,
) -> None:
    if response.tool_calls:
        body = ", ".join(call.name for call in response.tool_calls)
        metadata = {"run_id": run_id, "tool_calls": [call.name for call in response.tool_calls]}
    else:
        body = response.content[:1000]
        metadata = {"run_id": run_id, "tool_calls": []}
    db.append_event(
        job_id,
        event_type="loop",
        title="message_end",
        body=body,
        ref_table="job_runs",
        ref_id=run_id,
        metadata=metadata,
    )


def _emit_loop_end(
    db: AgentDB,
    job_id: str,
    run_id: str,
    *,
    status: str,
    step_id: str | None = None,
    tool_name: str | None = None,
    detail: str = "",
) -> None:
    metadata = {"run_id": run_id, "status": status, "step_id": step_id or "", "tool": tool_name or ""}
    db.append_event(
        job_id,
        event_type="loop",
        title="turn_end",
        body=detail[:1000],
        ref_table="job_runs",
        ref_id=run_id,
        metadata=metadata,
    )
    db.append_event(
        job_id,
        event_type="loop",
        title="agent_end",
        body=status,
        ref_table="job_runs",
        ref_id=run_id,
        metadata=metadata,
    )


def _run_reflection_step(
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    db: AgentDB,
    job_id: str,
    run_id: str,
) -> StepExecution:
    step_id = db.add_step(job_id=job_id, run_id=run_id, kind="reflection", tool_name="reflect")
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    findings = metadata.get("finding_ledger") if isinstance(metadata.get("finding_ledger"), list) else []
    sources = metadata.get("source_ledger") if isinstance(metadata.get("source_ledger"), list) else []
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    experiments = metadata.get("experiment_ledger") if isinstance(metadata.get("experiment_ledger"), list) else []
    lessons = metadata.get("lessons") if isinstance(metadata.get("lessons"), list) else []
    operator_messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    active_operator_messages = [
        entry for entry in operator_messages
        if isinstance(entry, dict)
        and str(entry.get("mode") or "steer") in {"steer", "follow_up"}
        and not entry.get("acknowledged_at")
        and not entry.get("superseded_at")
    ]
    pending_measurement = _pending_measurement_obligation(job)
    artifacts = db.list_artifacts(job_id, limit=12)
    failures = [step for step in recent_steps[-REFLECTION_INTERVAL_STEPS:] if step.get("status") == "failed" or step.get("status") == "blocked"]
    step_no = _max_step_no(recent_steps)
    finding_batches = [artifact for artifact in artifacts if "finding" in str(artifact.get("title") or artifact.get("summary") or "").lower()]
    best_sources = sorted(
        [
            source for source in sources
            if isinstance(source, dict)
            and (
                _as_int(source.get("yield_count")) > 0
                or _as_float(source.get("usefulness_score")) >= 0.2
            )
            and _as_int(source.get("fail_count")) <= max(0, _as_int(source.get("yield_count")))
        ],
        key=lambda source: (_as_float(source.get("usefulness_score")), _as_int(source.get("yield_count"))),
        reverse=True,
    )[:3]
    source_text = ", ".join(str(source.get("source") or "") for source in best_sources) or "no high-yield source yet"
    measured_experiments = [experiment for experiment in experiments if isinstance(experiment, dict) and experiment.get("metric_value") is not None]
    best_experiments = [experiment for experiment in measured_experiments if experiment.get("best_observed")]
    best_experiment_text = "no measured experiment yet"
    if best_experiments:
        best_experiment_text = "; ".join(
            (
                f"{experiment.get('title')} {experiment.get('metric_name') or 'metric'}="
                f"{experiment.get('metric_value')}{experiment.get('metric_unit') or ''}"
            )
            for experiment in best_experiments[-3:]
        )
    summary = (
        f"Reflection through step #{step_no}: {len(findings)} findings, {len(sources)} sources, "
        f"{len(tasks)} tasks, {len(experiments)} experiments, {len(lessons)} lessons, "
        f"{len(active_operator_messages)} active operator messages, "
        f"{len(finding_batches)} recent finding artifacts, {len(failures)} recent blocked/failed steps. "
        f"Best source direction: {source_text}. Best measured result: {best_experiment_text}."
        + (" Pending measurement obligation needs resolution." if pending_measurement else "")
    )
    strategy = (
        "Prioritize source types that have yielded durable findings or artifacts; "
        "downgrade repetitive, blocked, or low-evidence paths that do not advance the objective. "
        "For measurable work, convert ideas into record_experiment trials and choose the next branch from the best observed result."
    )
    reflection = db.append_reflection(
        job_id,
        summary,
        strategy=strategy,
        metadata={
            "through_step": step_no,
            "finding_count": len(findings),
            "source_count": len(sources),
            "task_count": len(tasks),
            "experiment_count": len(experiments),
            "measured_experiment_count": len(measured_experiments),
            "active_operator_message_count": len(active_operator_messages),
            "pending_measurement_obligation": bool(pending_measurement),
        },
    )
    db.append_lesson(job_id, strategy, category="strategy", confidence=0.75, metadata={"source": "reflection", "through_step": step_no})
    db.append_agent_update(job_id, summary, category="plan", metadata={"reflection": reflection})
    result = {"success": True, "reflection": reflection}
    db.finish_step(step_id, status="completed", summary=summary, output_data=result)
    db.finish_run(run_id, "completed")
    _emit_loop_end(db, job_id, run_id, status="completed", step_id=step_id, tool_name="reflect", detail=summary)
    refresh_memory_index(db, job_id)
    return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name="reflect", status="completed", result=result)


def _evidence_checkpoint_content(evidence_step: dict[str, Any]) -> str:
    output = evidence_step.get("output") if isinstance(evidence_step.get("output"), dict) else {}
    input_data = evidence_step.get("input") if isinstance(evidence_step.get("input"), dict) else {}
    observation = _observation_for_prompt(evidence_step.get("tool_name"), output)
    return "\n\n".join([
        "# Auto Evidence Checkpoint",
        f"Source step: #{evidence_step.get('step_no')} {evidence_step.get('tool_name') or evidence_step.get('kind')}",
        f"Summary: {evidence_step.get('summary') or ''}",
        f"Arguments:\n```json\n{json.dumps(input_data.get('arguments') or {}, ensure_ascii=False, indent=2)[:3000]}\n```",
        f"Observed:\n{observation or 'No compact observation available.'}",
        f"Raw output excerpt:\n```json\n{json.dumps(output, ensure_ascii=False, indent=2)[:9000]}\n```",
    ])


def _auto_persist_evidence(
    *,
    db: AgentDB,
    artifacts: ArtifactStore,
    job_id: str,
    run_id: str,
    step_id: str,
    blocked_tool: str,
    evidence_step: dict[str, Any],
) -> dict[str, Any]:
    stored = artifacts.write_text(
        job_id=job_id,
        run_id=run_id,
        step_id=step_id,
        title=f"Auto Evidence Checkpoint after step {evidence_step.get('step_no')}",
        summary=f"Auto-saved evidence before allowing more research; blocked tool was {blocked_tool}.",
        content=_evidence_checkpoint_content(evidence_step),
        artifact_type="text",
        metadata={"auto_checkpoint": True, "evidence_step": evidence_step.get("id"), "blocked_tool": blocked_tool},
    )
    lesson = db.append_lesson(
        job_id,
        (
            f"Evidence from step #{evidence_step.get('step_no')} must be persisted before more research; "
            f"auto-saved checkpoint {stored.id} after blocked {blocked_tool}."
        ),
        category="mistake",
        confidence=0.8,
        metadata={"artifact_id": stored.id, "blocked_tool": blocked_tool},
    )
    db.append_agent_update(
        job_id,
        f"Auto-saved evidence checkpoint {stored.id} after the model tried {blocked_tool} before persisting evidence.",
        category="blocked",
        metadata={"artifact_id": stored.id, "blocked_tool": blocked_tool},
    )
    return {"artifact_id": stored.id, "path": str(stored.path), "lesson": lesson}


def _auto_record_blocked_source(
    *,
    db: AgentDB,
    job_id: str,
    context: dict[str, Any],
    blocked_tool: str,
) -> dict[str, Any]:
    source = str(context.get("url") or context.get("title") or "unknown blocked browser source")
    reason = str(context.get("reason") or "anti-bot challenge")
    record = db.append_source_record(
        job_id,
        source,
        source_type="blocked_browser_source",
        usefulness_score=0.02,
        fail_count_delta=1,
        warnings=[reason],
        outcome=f"blocked by {reason}; pivot to an alternate source for the current objective",
        metadata={"blocked_tool": blocked_tool, "source_step": context.get("step_id")},
    )
    lesson = None
    if int(record.get("fail_count") or 0) <= 2:
        lesson = db.append_lesson(
            job_id,
            "Blocked, CAPTCHA, login, paywall, or anti-bot pages are not usable evidence for any long-running task; record the source outcome and pivot instead of repeating browser actions.",
            category="source_quality",
            confidence=0.9,
            metadata={"source": source, "blocked_tool": blocked_tool},
        )
    db.append_agent_update(
        job_id,
        f"Blocked source guard: current source is {reason}; pivoting away instead of looping.",
        category="blocked",
        metadata={"source": source, "blocked_tool": blocked_tool, "reason": reason},
    )
    return {"source": record, "lesson": lesson}


def _auto_record_tool_source_quality(
    *,
    db: AgentDB,
    job_id: str,
    tool_name: str | None,
    result: dict[str, Any],
) -> None:
    if tool_name not in {"browser_navigate", "browser_snapshot"}:
        return
    context = _browser_warning_context(result)
    if not context:
        return
    result["source_warning"] = context["reason"]
    result["source_url"] = context.get("url") or ""
    _auto_record_blocked_source(db=db, job_id=job_id, context=context, blocked_tool=tool_name or "browser")


def _auto_checkpoint_update(
    *,
    db: AgentDB,
    job_id: str,
    step_no: int,
    tool_name: str | None,
    args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    title_text = " ".join(str(args.get(key) or "") for key in ("title", "summary", "type")).lower()
    is_finding_batch = tool_name == "write_artifact" and "finding" in title_text
    if not is_finding_batch and step_no % 10 != 0:
        return
    job = db.get_job(job_id)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    findings = metadata.get("finding_ledger") if isinstance(metadata.get("finding_ledger"), list) else []
    sources = metadata.get("source_ledger") if isinstance(metadata.get("source_ledger"), list) else []
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    experiments = metadata.get("experiment_ledger") if isinstance(metadata.get("experiment_ledger"), list) else []
    lessons = metadata.get("lessons") if isinstance(metadata.get("lessons"), list) else []
    artifact_id = result.get("artifact_id") or ""
    if is_finding_batch:
        message = f"Saved a finding-related artifact {artifact_id}; ledger now has {len(findings)} findings, {len(sources)} sources, {len(tasks)} tasks, and {len(experiments)} experiments."
        category = "finding"
    else:
        message = f"Checkpoint at step #{step_no}: {len(findings)} findings, {len(sources)} sources, {len(tasks)} tasks, {len(experiments)} experiments, {len(lessons)} lessons recorded."
        category = "progress"
    db.append_agent_update(job_id, message, category=category, metadata={"step_no": step_no, "tool": tool_name})


def _execute_tool_call(
    call: Any,
    *,
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    config: AppConfig,
    db: AgentDB,
    artifacts: ArtifactStore,
    registry: ToolRegistry,
    job_id: str,
    run_id: str,
) -> tuple[StepExecution, bool, str, str | None]:
    step_id = db.add_step(
        job_id=job_id,
        run_id=run_id,
        kind="tool",
        tool_name=call.name,
        input_data={"tool_call_id": call.id, "arguments": call.arguments},
    )
    blocked = _blocked_tool_call_result(call.name, call.arguments, recent_steps, job)
    if blocked:
        result, summary = blocked
        result = {**result, "success": True, "recoverable": True}
        evidence_checkpoint = None
        if result.get("error") == "artifact required before more research":
            evidence_step = next(
                (step for step in recent_steps if step.get("id") == result.get("previous_step")),
                None,
            )
            if evidence_step:
                evidence_checkpoint = _auto_persist_evidence(
                    db=db,
                    artifacts=artifacts,
                    job_id=job_id,
                    run_id=run_id,
                    step_id=step_id,
                    blocked_tool=call.name,
                    evidence_step=evidence_step,
                )
                result["auto_checkpoint"] = evidence_checkpoint
                summary = f"blocked {call.name}; auto-saved evidence checkpoint {evidence_checkpoint['artifact_id']}"
        anti_bot_source = result.get("anti_bot_source") if isinstance(result.get("anti_bot_source"), dict) else None
        if anti_bot_source:
            result["auto_source_record"] = _auto_record_blocked_source(
                db=db,
                job_id=job_id,
                context=anti_bot_source,
                blocked_tool=call.name,
            )
        known_bad_source = result.get("known_bad_source") if isinstance(result.get("known_bad_source"), dict) else None
        if known_bad_source:
            db.append_agent_update(
                job_id,
                f"Source ledger blocked retry of {known_bad_source.get('source')}; choosing a different route next.",
                category="blocked",
                metadata={"source": known_bad_source, "blocked_tool": call.name},
            )
        db.finish_step(
            step_id,
            status="blocked",
            summary=summary,
            output_data=result,
            error=None,
        )
        return (
            StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=call.name, status="blocked", result=result),
            True,
            summary,
            None,
        )

    ctx = ToolContext(
        config=config,
        db=db,
        artifacts=artifacts,
        job_id=job_id,
        run_id=run_id,
        step_id=step_id,
        task_id=job_id,
    )
    try:
        raw_result = registry.handle(call.name, call.arguments, ctx)
        result = _parse_tool_result(raw_result)
        ok = bool(result.get("success", True)) and not result.get("error")
        status = "completed" if ok else "failed"
        if ok:
            _auto_record_tool_source_quality(db=db, job_id=job_id, tool_name=call.name, result=result)
        summary = _summarize_tool_result(call.name, call.arguments, result, ok=ok)
        db.finish_step(step_id, status=status, summary=summary, output_data=result, error=result.get("error"))
        if ok:
            finished_step = _step_by_id(db, job_id, step_id)
            _maybe_create_measurement_obligation(
                db=db,
                job_id=job_id,
                step=finished_step,
                tool_name=call.name,
                args=call.arguments,
                result=result,
            )
            _auto_checkpoint_update(
                db=db,
                job_id=job_id,
                step_no=(finished_step or db.list_steps(job_id=job_id)[-1])["step_no"],
                tool_name=call.name,
                args=call.arguments,
                result=result,
            )
        return (
            StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=call.name, status=status, result=result),
            status != "completed",
            summary,
            result.get("error") if status == "failed" else None,
        )
    except Exception as exc:
        result = _error_result(exc)
        db.finish_step(step_id, status="failed", summary=f"{call.name} raised", output_data=result, error=str(exc))
        return (
            StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=call.name, status="failed", result=result),
            True,
            str(exc),
            str(exc),
        )


def run_one_step(
    job_id: str,
    *,
    config: AppConfig | None = None,
    db: AgentDB | None = None,
    llm: StepLLM | None = None,
    registry: ToolRegistry = DEFAULT_REGISTRY,
) -> StepExecution:
    config = config or load_config()
    config.ensure_dirs()
    owns_db = db is None
    db = db or AgentDB(config.runtime.state_db_path)
    try:
        artifacts = ArtifactStore(config.runtime.home, db=db)
        job = db.get_job(job_id)
        if _acknowledge_non_prompt_operator_context(db, job_id):
            job = db.get_job(job_id)
        if _clear_invalid_measurement_obligation(db, job_id):
            job = db.get_job(job_id)
        run_id = db.start_run(job_id, model=config.model.model)
        _emit_loop_start(db, job_id, run_id)
        recent_steps = db.list_steps(job_id=job_id)
        if _should_reflect(job, recent_steps):
            return _run_reflection_step(job, recent_steps, db=db, job_id=job_id, run_id=run_id)
        active_operator_messages = _claim_operator_queue(db, job_id)
        if active_operator_messages:
            job = db.get_job(job_id)
        messages = build_messages(
            job,
            recent_steps,
            memory_entries=db.list_memory(job_id),
            program_text=_load_program_text(config, job_id),
            timeline_events=db.list_timeline_events(job_id, limit=30),
            active_operator_messages=active_operator_messages,
            include_unclaimed_operator_messages=False,
        )
        llm = llm or OpenAIChatLLM(config.model)
        try:
            response: LLMResponse = llm.next_action(messages=messages, tools=registry.openai_tools())
        except Exception as exc:
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="llm",
                status="failed",
                summary=f"model call failed: {type(exc).__name__}",
                input_data={"model": config.model.model},
            )
            result = _error_result(exc)
            db.finish_step(step_id, status="failed", output_data=result, error=str(exc))
            db.finish_run(run_id, "failed", error=str(exc))
            _emit_loop_end(db, job_id, run_id, status="failed", step_id=step_id, detail=str(exc))
            refresh_memory_index(db, job_id)
            return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=None, status="failed", result=result)

        _emit_assistant_message_event(db, job_id, run_id, response)

        if response.tool_calls:
            executions: list[StepExecution] = []
            details: list[str] = []
            run_error: str | None = None
            for call in response.tool_calls:
                current_job = db.get_job(job_id)
                current_recent_steps = db.list_steps(job_id=job_id)
                execution, stop_batch, detail, error = _execute_tool_call(
                    call,
                    job=current_job,
                    recent_steps=current_recent_steps,
                    config=config,
                    db=db,
                    artifacts=artifacts,
                    registry=registry,
                    job_id=job_id,
                    run_id=run_id,
                )
                executions.append(execution)
                details.append(detail)
                if error:
                    run_error = error
                if stop_batch:
                    break

            final_execution = executions[-1]
            run_status = "failed" if any(item.status == "failed" for item in executions) else "completed"
            db.finish_run(run_id, run_status, error=run_error)
            detail = f"executed {len(executions)}/{len(response.tool_calls)} tool calls"
            if details:
                detail = f"{detail}; last: {details[-1]}"
            _emit_loop_end(
                db,
                job_id,
                run_id,
                status=final_execution.status,
                step_id=final_execution.step_id,
                tool_name=final_execution.tool_name,
                detail=detail,
            )
            refresh_memory_index(db, job_id)
            return final_execution

        step_id = db.add_step(
            job_id=job_id,
            run_id=run_id,
            kind="assistant",
            status="completed",
            summary=response.content[:500],
            input_data={},
        )
        result = {"success": True, "content": response.content}
        db.finish_step(step_id, status="completed", output_data=result)
        db.finish_run(run_id, "completed")
        _emit_loop_end(db, job_id, run_id, status="completed", step_id=step_id, detail=response.content[:1000])
        refresh_memory_index(db, job_id)
        return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=None, status="completed", result=result)
    finally:
        if owns_db:
            db.close()
