"""Bounded worker loop for one restartable agent step."""

from __future__ import annotations

import json
import re
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import AppConfig, load_config
from nipux_cli.compression import refresh_memory_index
from nipux_cli.context_pressure import (
    context_pressure_for_prompt,
    emit_context_pressure_update,
    emit_usage_pressure_update,
    usage_pressure_for_prompt,
)
from nipux_cli.db import AgentDB
from nipux_cli.llm import LLMResponse, LLMResponseError, OpenAIChatLLM, StepLLM, ToolCall
from nipux_cli.measurement import measurement_candidates, measurement_candidates_are_diagnostic_only
from nipux_cli.memory_graph import memory_graph_from_job
from nipux_cli.metric_format import format_metric_value
from nipux_cli.operator_context import (
    inactive_prompt_operator_ids,
)
from nipux_cli.progress import build_progress_checkpoint
from nipux_cli.provider_errors import provider_action_required_note
from nipux_cli.source_quality import anti_bot_reason
from nipux_cli.task_match import find_semantic_task_match, task_key
from nipux_cli.tools import DEFAULT_REGISTRY, ToolContext, ToolRegistry
from nipux_cli.worker_policy import (
    ACTIVITY_STAGNATION_BLOCKED_TOOLS,
    ACTIVITY_STAGNATION_CHECKPOINTS,
    ANTI_BOT_ACK_TERMS,
    ARTIFACT_ACCOUNTING_BLOCKED_TOOLS,
    ARTIFACT_ACCOUNTING_RESOLUTION_TOOLS,
    BRANCH_WORK_TOOLS,
    CHURN_TOOLS,
    DELIVERABLE_ARTIFACT_TERMS,
    DELIVERABLE_PROGRESS_BLOCKED_TOOLS,
    DELIVERABLE_RESEARCH_BUDGET_STEPS,
    EVIDENCE_ARTIFACT_TERMS,
    EXPERIMENT_DELIVERY_ACTION_TERMS,
    EXPERIMENT_INFORMATION_ACTION_TERMS,
    EXPERIMENT_NEXT_ACTION_BLOCKED_TOOLS,
    FILE_VALIDATION_BLOCKED_TOOLS,
    FILE_VALIDATION_RESOLUTION_TOOLS,
    LEDGER_PROGRESS_TOOLS,
    MAX_WORKER_PROMPT_CHARS,
    MEASURABLE_ACTION_BUDGET_STEPS,
    MEASURABLE_PROGRESS_PATTERN,
    MEASURABLE_RESEARCH_BLOCKED_TOOLS,
    MEASURABLE_RESEARCH_BUDGET_STEPS,
    MEASUREMENT_BLOCKED_TOOLS,
    MEASUREMENT_RESOLUTION_TOOLS,
    MEMORY_CONSOLIDATION_BLOCKED_TOOLS,
    MEMORY_ENTRY_PROMPT_CHARS,
    MEMORY_PROMPT_CHARS,
    MILESTONE_VALIDATION_BLOCKED_TOOLS,
    PROGRAM_PROMPT_CHARS,
    QUERY_STOPWORDS,
    READ_ONLY_SHELL_COMMAND_PATTERN,
    RECENT_STATE_PROMPT_CHARS,
    RECENT_STATE_STEPS,
    RECOVERABLE_GUARD_ERRORS,
    REFLECTION_INTERVAL_STEPS,
    RESEARCH_BALANCE_BLOCKED_TOOLS,
    ROADMAP_STALENESS_BLOCKED_TOOLS,
    SOURCE_YIELD_BLOCKED_TOOLS,
    SYSTEM_PROMPT,
    TASK_DELIVERABLE_ACTION_TERMS,
    TASK_PLANNING_STAGNATION_CHECKPOINTS,
    TASK_QUEUE_SATURATION_OPEN_TASKS,
    TASK_QUEUE_TOTAL_SOFT_LIMIT,
    TEXT_TOKEN_STOPWORDS,
)
from nipux_cli.worker_prompt_context import (
    _as_float,
    _as_int,
    _experiments_for_prompt,
    _ledgers_for_prompt,
    _lessons_for_prompt,
    _memory_graph_for_prompt,
    _memory_entries_for_prompt,
    _metadata_list,
    _operator_messages_for_prompt,
    _outcomes_for_prompt,
    _render_worker_prompt,
    _roadmap_for_prompt,
    _tasks_for_prompt,
    _timeline_for_prompt,
)
from nipux_cli.worker_prompt_format import (
    clip_text as _clip_text,
    format_step_for_prompt as _format_step_for_prompt,
    observation_for_prompt as _observation_for_prompt,
)
from nipux_cli.worker_tool_summary import summarize_tool_result as _summarize_tool_result
from nipux_cli.worker_usage import turn_usage_metadata


__all__ = ["MAX_WORKER_PROMPT_CHARS", "_render_worker_prompt", "build_messages", "run_one_step"]


LESSON_SPRAWL_MIN_LESSONS = 30
LESSON_SPRAWL_RECENT_LESSONS = 3
EXPERIMENT_STAGNATION_MIN_TRIALS = 6
EXPERIMENT_STAGNATION_NON_IMPROVING = 4
SOURCE_YIELD_MIN_SOURCES = 12
SOURCE_YIELD_MIN_RECENT_GATHERING = 5


@dataclass(frozen=True)
class StepExecution:
    job_id: str
    run_id: str
    step_id: str
    tool_name: str | None
    status: str
    result: dict[str, Any]


EXPERIMENT_NEXT_ACTION_VERIFY_SHELL_PATTERN = re.compile(
    r"(?is)^\s*(?:command\s+-v\b|which\b|type\b|test\b|ls\b|find\b|stat\b|file\b)"
)
EXPERIMENT_NEXT_ACTION_VERIFY_STOPWORDS = {
    "action",
    "after",
    "before",
    "from",
    "into",
    "next",
    "real",
    "then",
    "using",
    "with",
}
MILESTONE_MATCH_STOPWORDS = {
    "acceptance",
    "blocked",
    "criteria",
    "current",
    "done",
    "evidence",
    "failed",
    "issue",
    "issues",
    "milestone",
    "needed",
    "pending",
    "passed",
    "result",
    "roadmap",
    "status",
    "title",
    "validating",
    "validation",
    "validate",
}


def build_messages(
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    memory_entries: list[dict[str, Any]] | None = None,
    program_text: str = "",
    timeline_events: list[dict[str, Any]] | None = None,
    active_operator_messages: list[dict[str, Any]] | None = None,
    include_unclaimed_operator_messages: bool = True,
    token_usage: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    step_lines = []
    for step in recent_steps[-RECENT_STATE_STEPS:]:
        step_lines.append(_clip_text(_format_step_for_prompt(step), 720))
    state = _clip_text("\n".join(step_lines), RECENT_STATE_PROMPT_CHARS) if step_lines else "No prior steps."
    memory_lines = []
    for entry in _memory_entries_for_prompt(memory_entries or []):
        refs = ", ".join((entry.get("artifact_refs") or [])[:8])
        suffix = f"\nArtifact refs: {refs}" if refs else ""
        memory_lines.append(
            _clip_text(f"### {entry.get('key') or 'memory'}\n{entry.get('summary') or ''}{suffix}", MEMORY_ENTRY_PROMPT_CHARS)
        )
    memory_text = _clip_text("\n\n".join(memory_lines), MEMORY_PROMPT_CHARS) if memory_lines else "No compact memory yet."
    program = _clip_text(program_text.strip(), PROGRAM_PROMPT_CHARS) if program_text else "No program.md saved yet."
    operator_messages = _operator_messages_for_prompt(
        job,
        active_messages=active_operator_messages or [],
        include_unclaimed=include_unclaimed_operator_messages,
    )
    measurement_obligation = _measurement_obligation_for_prompt(job)
    file_validation_obligation = _file_validation_obligation_for_prompt(job)
    candidate_file_discovery = _candidate_file_discovery_for_prompt(job, recent_steps)
    shell_path_recovery = _shell_path_recovery_for_prompt(recent_steps)
    shell_permission_recovery = _shell_permission_recovery_for_prompt(recent_steps)
    measured_progress_guard = _measured_progress_guard_for_prompt(job, recent_steps)
    experiment_stagnation_guard = _experiment_stagnation_guard_for_prompt(job, recent_steps)
    research_balance_guard = _research_balance_guard_for_prompt(job, recent_steps)
    source_yield_guard = _source_yield_guard_for_prompt(job, recent_steps)
    deliverable_progress_guard = _deliverable_progress_guard_for_prompt(job, recent_steps)
    progress_accounting_guard = _progress_accounting_for_prompt(recent_steps)
    evidence_checkpoint_guard = _evidence_checkpoint_accounting_for_prompt(job, recent_steps)
    activity_stagnation = _activity_stagnation_for_prompt(job)
    task_planning_guard = _task_planning_guard_for_prompt(job)
    task_queue_saturation = _task_queue_saturation_for_prompt(job, recent_steps)
    memory_consolidation_guard = _memory_consolidation_guard_for_prompt(job, recent_steps)
    lesson_consolidation_guard = _lesson_consolidation_guard_for_prompt(job, recent_steps)
    durable_yield = _durable_yield_for_prompt(job, recent_steps)
    context_pressure = context_pressure_for_prompt(job)
    usage_pressure = usage_pressure_for_prompt(job, token_usage)
    lessons = _lessons_for_prompt(job)
    memory_graph = _memory_graph_for_prompt(job)
    roadmap = _roadmap_for_prompt(job)
    tasks = _tasks_for_prompt(job)
    ledgers = _ledgers_for_prompt(job)
    experiments = _experiments_for_prompt(job)
    reflections = _reflections_for_prompt(job)
    timeline = _timeline_for_prompt(timeline_events or [])
    outcomes = _outcomes_for_prompt(timeline_events or [])
    next_constraint = _next_action_constraint(job, recent_steps)
    content = _render_worker_prompt(
        job,
        sections=[
            (
                "Workspace",
                "\n".join([
                    "- shell_exec runs on the machine hosting this Nipux worker, in the current worker directory unless the command changes it",
                    "- saved artifacts are separate Nipux outputs; read_artifact is only for those saved outputs",
                    "- use shell_exec for workspace/project files unless the file is a saved artifact",
                ]),
            ),
            ("Operator context", operator_messages),
            ("Pending measurement obligation", measurement_obligation),
            ("Pending file validation obligation", file_validation_obligation),
            ("Candidate file discovery", candidate_file_discovery),
            ("Shell path recovery", shell_path_recovery),
            ("Shell permission recovery", shell_permission_recovery),
            ("Measured progress guard", measured_progress_guard),
            ("Experiment stagnation guard", experiment_stagnation_guard),
            ("Research balance guard", research_balance_guard),
            ("Source yield guard", source_yield_guard),
            ("Deliverable progress guard", deliverable_progress_guard),
            ("Progress accounting guard", progress_accounting_guard),
            ("Evidence checkpoint accounting guard", evidence_checkpoint_guard),
            ("Activity stagnation", activity_stagnation),
            ("Task planning guard", task_planning_guard),
            ("Task queue saturation", task_queue_saturation),
            ("Memory consolidation guard", memory_consolidation_guard),
            ("Lesson consolidation guard", lesson_consolidation_guard),
            ("Durable progress yield", durable_yield),
            ("Context pressure", context_pressure),
            ("Usage pressure", usage_pressure),
            ("Program", program),
            ("Lessons learned", lessons),
            ("Memory graph", memory_graph),
            ("Roadmap", roadmap),
            ("Task queue", tasks),
            ("Durable outcomes", outcomes),
            ("Ledgers", ledgers),
            ("Experiment ledger", experiments),
            ("Reflections", reflections),
            ("Compact memory", memory_text),
            ("Recent visible timeline", timeline),
            ("Recent state", state),
            ("Next-action constraint", next_constraint),
        ],
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


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


def _measured_progress_guard_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _measured_progress_guard_context(job, recent_steps)
    if not context:
        return "None."
    if _as_int(context.get("shell_actions_since_last_experiment")) >= _as_int(context.get("shell_action_budget")):
        candidate_context = _candidate_file_discovery_context(job, recent_steps)
        shell_guidance = "Do not call shell_exec or do more research next."
        if candidate_context:
            shell_guidance = (
                "Do not call broad shell_exec or do more research next. A single bounded shell_exec is allowed only "
                "when it validates one exact candidate path already listed in Candidate file discovery."
            )
        return (
            "This objective or active task is measurably framed, and the shell/action budget since the last experiment "
            f"is exhausted. completed_since_last_experiment={context.get('completed_since_last_experiment')} "
            f"shell_actions={context.get('shell_actions_since_last_experiment')} shell_budget={context.get('shell_action_budget')} "
            f"reason={context.get('reason')}. {shell_guidance} Use record_experiment "
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


def _deliverable_progress_guard_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _deliverable_progress_guard_context(job, recent_steps)
    if not context:
        return "None."
    return (
        "This objective or active task expects a durable deliverable, but recent branch work has not produced a "
        "draft/report/file checkpoint. "
        f"completed_since_last_deliverable={context.get('completed_since_last_deliverable')} "
        f"research_budget={context.get('research_budget')} reason={context.get('reason')}. "
        "Next useful actions: write_file or write_artifact for a partial deliverable, record_tasks for a smaller "
        "deliverable branch, record_roadmap/record_milestone_validation for validation, or record_lesson if the "
        "deliverable is blocked."
    )


def _experiment_stagnation_guard_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _experiment_stagnation_context(job, recent_steps)
    if not context:
        return "None."
    return (
        "Recent measured trials have not improved the best observed result. "
        f"metric={context.get('metric_name')} unit={context.get('metric_unit')} "
        f"best={context.get('best_value')} latest={context.get('latest_value')} "
        f"non_improving={context.get('non_improving_count')} recent_trials={context.get('recent_trials')}. "
        "Before more experiments, shell execution, research, or output churn, record a decision: reject or block the "
        "stale branch, pivot to a materially different branch, update the roadmap/task queue, or explain why the "
        "stagnant measurements are still useful."
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


def _file_validation_obligation_for_prompt(job: dict[str, Any]) -> str:
    obligation = _pending_file_validation_obligation(job)
    if not obligation:
        return "None."
    lines = [
        f"path={obligation.get('path') or ''}",
        f"source_step=#{obligation.get('source_step_no') or '?'}",
        f"reason={obligation.get('reason') or 'recent file output needs validation'}",
    ]
    suggested = str(obligation.get("suggested_validation") or "").strip()
    if suggested:
        lines.append(f"suggested_validation={suggested}")
    lines.append(
        "Before more research/output churn, validate the file with shell_exec, "
        "corroborating any `file` output with header/signature bytes, checksum/size, or a parser/loader when the expected "
        "format matters, "
        "or use record_tasks/record_lesson/record_experiment to explain the blocked or deferred validation."
    )
    return "\n".join(lines)


def _candidate_file_discovery_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _candidate_file_discovery_context(job, recent_steps)
    if not context:
        return "None."
    paths = context["paths"]
    source_text = context["source_text"]
    lines = [
        f"{source_text} while open work depends on file/path validation.",
        "Candidate paths:",
    ]
    for path in paths[:8]:
        lines.append(f"- {path}")
    invalid_paths = context.get("invalid_paths") if isinstance(context.get("invalid_paths"), list) else []
    if invalid_paths:
        lines.append(
            "Recently invalid or stub-like candidates: "
            + "; ".join(str(path) for path in invalid_paths[:5])
            + ". Prefer higher-confidence candidates before retrying these."
        )
    lines.append(
        "Validate likely candidates with shell_exec before recording a no-file/no-progress claim or searching for alternatives. "
        "Do not reject a non-empty candidate binary from `file` output alone; corroborate with header/signature bytes, "
        "checksum/size, or a parser/loader for the expected format, or record uncertainty. "
        "Treat durable-record candidates as candidates until revalidated. This supersedes stale no-candidate/no-file memory "
        "until validation proves those candidates are irrelevant."
    )
    lines.append(f"Relevant open work: {_clip_text(context['task_text'], 500)}")
    return "\n".join(lines)


def _shell_path_recovery_for_prompt(recent_steps: list[dict[str, Any]]) -> str:
    context = _shell_path_recovery_context(recent_steps)
    if not context:
        return "None."
    paths = context.get("missing_paths") if isinstance(context.get("missing_paths"), list) else []
    commands = context.get("missing_commands") if isinstance(context.get("missing_commands"), list) else []
    candidate_executables = (
        context.get("candidate_executables") if isinstance(context.get("candidate_executables"), dict) else {}
    )
    observed_executables = (
        context.get("observed_executables") if isinstance(context.get("observed_executables"), list) else []
    )
    lines = [
        f"Recent shell step #{context.get('step_no') or '?'} reported a missing command or path.",
    ]
    if commands:
        lines.append("Missing commands: " + ", ".join(str(command) for command in commands[:6]))
    if candidate_executables:
        for command, command_paths in list(candidate_executables.items())[:6]:
            if not isinstance(command_paths, list) or not command_paths:
                continue
            lines.append(
                f"Observed candidate executable for {command}: "
                + ", ".join(str(path) for path in command_paths[:4])
            )
        lines.append("Recovery priority: try the exact candidate path or add its directory to PATH before package-manager/install retries.")
    if paths:
        lines.append("Missing paths: " + ", ".join(str(path) for path in paths[:6]))
    if observed_executables:
        lines.append("Observed executable paths in partial shell output: " + ", ".join(str(path) for path in observed_executables[:8]))
    if not commands and not paths:
        lines.append("Missing command/path was not parsed.")
    command = str(context.get("command") or "")
    if command:
        lines.append(f"Failed command: {_clip_text(command, 420)}")
    excerpt = str(context.get("excerpt") or "")
    if excerpt:
        lines.append(f"Observed output: {_clip_text(excerpt, 360)}")
    lines.append(
        "Do not treat this output as a successful measurement or deliverable. Next, locate or verify the real "
        "executable/file path with a bounded shell probe such as command -v, find, ls, or an equivalent platform "
        "tool. Retry using only a validated path, or record the branch as blocked/skipped with the observed reason."
    )
    return "\n".join(lines)


def _shell_path_recovery_context(recent_steps: list[dict[str, Any]], *, window: int = 16) -> dict[str, Any] | None:
    for step in reversed(_completed_or_failed_recent_steps(recent_steps)[-window:]):
        if step.get("tool_name") != "shell_exec":
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        text = "\n".join(str(output.get(key) or "") for key in ("stdout", "stderr", "error"))
        if not text.strip():
            continue
        missing_paths = _missing_paths_from_shell_output(text)
        if not missing_paths and not _shell_output_has_missing_command(text):
            continue
        commands = _missing_commands_from_shell_output(text)
        return {
            "step_no": step.get("step_no"),
            "command": output.get("command"),
            "missing_commands": commands,
            "candidate_executables": _candidate_executable_paths_for_missing_commands(recent_steps, commands),
            "observed_executables": _observed_executable_paths_from_recent_shell(
                recent_steps,
                exclude_paths=missing_paths,
            ),
            "missing_paths": missing_paths,
            "excerpt": text.strip(),
        }
    return None


def _shell_permission_recovery_for_prompt(recent_steps: list[dict[str, Any]]) -> str:
    context = _recent_privileged_shell_failure_context(recent_steps)
    if not context:
        return "None."
    lines = [
        f"Recent shell step #{context.get('step_no') or '?'} failed because a privileged/package-manager command lacked permission.",
    ]
    command = str(context.get("command") or "")
    if command:
        lines.append(f"Failed command: {_clip_text(command, 420)}")
    excerpt = str(context.get("excerpt") or "")
    if excerpt:
        lines.append(f"Observed output: {_clip_text(excerpt, 360)}")
    lines.append("Recovery priority: try non-privileged alternatives first; record when operator credentials are required.")
    lines.append(
        "Do not retry the same privileged/package-manager path. Prefer observed executables, user-writable installs, "
        "existing project files, or other non-privileged alternatives; otherwise record the branch as blocked, skipped, "
        "or needing operator credentials."
    )
    return "\n".join(lines)


def _shell_step_failure_text(step: dict[str, Any]) -> str:
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    return "\n".join(str(output.get(key) or "") for key in ("stdout", "stderr", "error"))


def _shell_output_has_missing_command(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("command not found", ": not found", "no such file or directory"))


def _missing_paths_from_shell_output(text: str) -> list[str]:
    patterns = [
        r"(?:^|\n)(?:/bin/sh:\s*\d+:\s*)?(?P<path>/[^\s:'\"]+):\s*(?:not found|No such file or directory|command not found)",
        r"(?:cannot access|cannot stat|can't stat|stat: cannot statx?) ['\"](?P<quoted>[^'\"]+)['\"]:\s*No such file or directory",
        r"(?:^|\n)(?P<plain>/[^\s:'\"]+):\s*No such file or directory",
    ]
    paths: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            path = str(match.groupdict().get("path") or match.groupdict().get("quoted") or match.groupdict().get("plain") or "").strip()
            if not path or path in seen:
                continue
            seen.add(path)
            paths.append(path)
            if len(paths) >= 12:
                return paths
    return paths


def _missing_commands_from_shell_output(text: str) -> list[str]:
    patterns = [
        r"(?:^|\n)(?:/bin/sh:\s*\d+:\s*)?(?P<cmd>[A-Za-z0-9_.+-]+):\s*(?:not found|command not found)",
        r"(?:^|\n)(?:sh|bash|zsh):\s*(?P<shell_cmd>[A-Za-z0-9_.+-]+):\s*command not found",
        r"command not found:\s*(?P<suffix_cmd>[A-Za-z0-9_.+-]+)",
    ]
    commands: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            command = str(
                match.groupdict().get("cmd")
                or match.groupdict().get("shell_cmd")
                or match.groupdict().get("suffix_cmd")
                or ""
            ).strip()
            if not command or "/" in command or command in seen:
                continue
            seen.add(command)
            commands.append(command)
            if len(commands) >= 12:
                return commands
    return commands


def _candidate_executable_paths_for_missing_commands(
    recent_steps: list[dict[str, Any]], missing_commands: list[str], *, window: int = 20, max_paths_per_command: int = 6
) -> dict[str, list[str]]:
    command_names = {str(command or "").strip().lower() for command in missing_commands}
    command_names = {command for command in command_names if command}
    if not command_names:
        return {}
    matches: dict[str, list[str]] = {command: [] for command in command_names}
    seen: set[tuple[str, str]] = set()
    for path in _observed_executable_paths_from_recent_shell(recent_steps, command_names=command_names, window=window):
        name = Path(path).name.lower()
        if name not in command_names:
            continue
        key = (name, path.lower())
        if key in seen or len(matches.get(name, [])) >= max_paths_per_command:
            continue
        seen.add(key)
        matches.setdefault(name, []).append(path)
    return {command: paths for command, paths in matches.items() if paths}


def _observed_executable_paths_from_recent_shell(
    recent_steps: list[dict[str, Any]],
    *,
    command_names: set[str] | None = None,
    exclude_paths: list[str] | None = None,
    window: int = 20,
    max_paths: int = 12,
) -> list[str]:
    excluded = {str(path or "").lower() for path in (exclude_paths or []) if path}
    paths: list[str] = []
    seen: set[str] = set()
    for step in _completed_or_failed_recent_steps(recent_steps)[-window:]:
        if step.get("tool_name") != "shell_exec":
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        text = "\n".join(str(output.get(key) or "") for key in ("stdout", "stderr", "error"))
        for line in text.splitlines():
            if _shell_line_reports_missing_candidate(line):
                continue
            for path in _extract_candidate_executable_paths(line, command_names):
                key = path.lower()
                if key in excluded or key in seen:
                    continue
                seen.add(key)
                paths.append(path)
                if len(paths) >= max_paths:
                    return paths
    return paths


def _shell_line_reports_missing_candidate(line: str) -> bool:
    lowered = str(line or "").lower()
    return any(
        marker in lowered
        for marker in (
            "not found",
            "no such file or directory",
            "cannot access",
            "cannot stat",
            "can't stat",
            "missing",
        )
    )


def _extract_candidate_executable_paths(text: str, command_names: set[str] | None = None) -> list[str]:
    commands = {command.lower() for command in (command_names or set()) if command}
    paths: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?<![A-Za-z0-9])(?:~|/)[^\s'\"<>|;&]{2,}", text or ""):
        raw = _clean_candidate_file_path(match.group(0))
        if not _looks_like_candidate_executable_path(raw):
            continue
        name = Path(raw).name.lower()
        if commands and name not in commands:
            continue
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        paths.append(raw)
    return paths


def _looks_like_candidate_executable_path(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw or len(raw) > 500:
        return False
    if "://" in raw or raw.startswith("//") or "..." in raw or "…" in raw or "*" in raw:
        return False
    if not raw.startswith(("/", "~")):
        return False
    name = Path(raw).name
    if not name or name.startswith(".") or name in {".", ".."}:
        return False
    if any(char in name for char in ("$", "{", "}", "`")):
        return False
    return True


PACKAGE_MANAGER_WRITE_COMMAND_PATTERN = re.compile(
    r"(?is)(?:^|[;&|]{1,2}\s*)(?:sudo\s+|doas\s+|pkexec\s+)?"
    r"(?:apt-get|apt|dnf|yum|apk|pacman|zypper|brew|port)\s+"
    r"(?:install|upgrade|update|remove|erase|add|sync|build-dep)\b"
)
PRIVILEGED_COMMAND_PATTERN = re.compile(r"(?is)(?:^|[;&|]{1,2}\s*)(?:sudo|doas|pkexec)\b")


def _shell_command_looks_privileged_or_package_manager(command: str) -> bool:
    text = str(command or "").strip()
    if not text:
        return False
    return bool(PRIVILEGED_COMMAND_PATTERN.search(text) or PACKAGE_MANAGER_WRITE_COMMAND_PATTERN.search(text))


def _shell_output_has_permission_failure(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "permission denied",
            "not permitted",
            "operation not permitted",
            "authentication",
            "authorization",
            "are you root",
            "sudo:",
            "password is required",
            "unable to acquire the dpkg frontend lock",
            "could not open lock file",
        )
    )


def _recent_privileged_shell_failure_context(recent_steps: list[dict[str, Any]], *, window: int = 12) -> dict[str, Any] | None:
    accounting_tools = {"record_experiment", "record_tasks", "record_lesson", "record_roadmap", "record_milestone_validation"}
    latest_accounting_step = max(
        (
            _as_int(step.get("step_no"))
            for step in recent_steps[-window:]
            if step.get("status") == "completed" and step.get("tool_name") in accounting_tools
        ),
        default=0,
    )
    for step in reversed(_completed_or_failed_recent_steps(recent_steps)[-window:]):
        step_no = _as_int(step.get("step_no"))
        if latest_accounting_step and step_no <= latest_accounting_step:
            continue
        if step.get("tool_name") != "shell_exec":
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        command = _step_command(step) or str(output.get("command") or "")
        text = _shell_step_failure_text(step)
        if not _shell_output_has_permission_failure(text):
            continue
        if not _shell_command_looks_privileged_or_package_manager(command):
            continue
        return {
            "step_no": step.get("step_no"),
            "command": command,
            "excerpt": text.strip(),
        }
    return None


def _observed_candidate_recovery_required_context(recent_steps: list[dict[str, Any]], args: dict[str, Any]) -> dict[str, Any] | None:
    command = str(args.get("command") or "")
    if not command.strip():
        return None
    context = _shell_path_recovery_context(recent_steps)
    if not context:
        return None
    candidate_executables = (
        context.get("candidate_executables") if isinstance(context.get("candidate_executables"), dict) else {}
    )
    if not candidate_executables:
        return None
    for missing_command, paths in candidate_executables.items():
        if not isinstance(paths, list) or not paths:
            continue
        missing_name = str(missing_command or "").strip()
        if not missing_name:
            continue
        if not _shell_command_invokes_bare_executable(command, missing_name):
            continue
        if _shell_command_mentions_candidate_path(command, paths):
            continue
        return {
            "step_no": context.get("step_no"),
            "missing_command": missing_name,
            "candidate_executables": paths[:6],
            "blocked_command": command,
        }
    return None


def _shell_command_invokes_bare_executable(command: str, executable_name: str) -> bool:
    name = str(executable_name or "").strip()
    if not name:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9_./-]){re.escape(name)}(?![A-Za-z0-9_.-])", command))


def _shell_command_mentions_candidate_path(command: str, candidate_paths: list[Any]) -> bool:
    text = str(command or "")
    for path_value in candidate_paths:
        path = str(path_value or "").strip()
        if not path:
            continue
        if path in text:
            return True
        parent = str(Path(path).parent)
        if parent and parent not in {".", "/"} and parent in text:
            return True
    return False


def _candidate_file_discovery_context(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    task_text = _open_file_dependent_task_text(job)
    if not task_text:
        return None
    recent_paths = [
        *_candidate_file_paths_from_recent_shell(recent_steps),
        *_candidate_file_paths_from_recent_grounding_blocks(recent_steps),
    ]
    durable_paths = _candidate_file_paths_from_durable_records(job)
    paths: list[str] = []
    seen: set[str] = set()
    for path in [*recent_paths, *durable_paths]:
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    if not paths:
        return None
    source_text = "Recent shell output or durable records listed candidate file paths"
    if recent_paths and not durable_paths:
        source_text = "Recent shell output listed candidate file paths"
    elif durable_paths and not recent_paths:
        source_text = "Durable records mention candidate file paths"
    return {
        "paths": _rank_candidate_file_paths(job, task_text, paths, recent_steps=recent_steps),
        "invalid_paths": _invalid_candidate_file_paths(paths, recent_steps),
        "source_text": source_text,
        "task_text": task_text,
    }


def _shell_exec_targets_candidate_file(job: dict[str, Any], recent_steps: list[dict[str, Any]], args: dict[str, Any]) -> bool:
    command = str(args.get("command") or "")
    if not command.strip():
        return False
    context = _candidate_file_discovery_context(job, recent_steps)
    if not context:
        return False
    command_text = command.replace("\\ ", " ")
    return any(path and path in command_text for path in context.get("paths", [])[:12])


def _rank_candidate_file_paths(
    job: dict[str, Any],
    task_text: str,
    paths: list[str],
    *,
    recent_steps: list[dict[str, Any]] | None = None,
) -> list[str]:
    context_tokens = _candidate_context_tokens(job, task_text)
    indexed = list(enumerate(paths))
    ranked = sorted(
        indexed,
        key=lambda item: _candidate_file_path_score(
            item[1],
            context_tokens,
            item[0],
            recent_steps=recent_steps,
        ),
        reverse=True,
    )
    return [path for _, path in ranked]


def _candidate_context_tokens(job: dict[str, Any], task_text: str) -> set[str]:
    text = " ".join(str(job.get(key) or "") for key in ("title", "objective", "kind")) + " " + task_text
    tokens = set()
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}", text.lower()):
        cleaned = token.strip("._-")
        if not cleaned or cleaned in QUERY_STOPWORDS or cleaned in TEXT_TOKEN_STOPWORDS:
            continue
        tokens.add(cleaned)
        for part in re.split(r"[._-]+", cleaned):
            if len(part) >= 3 and part not in QUERY_STOPWORDS and part not in TEXT_TOKEN_STOPWORDS:
                tokens.add(part)
    return tokens


def _candidate_file_path_score(
    path: str,
    context_tokens: set[str],
    original_index: int,
    *,
    recent_steps: list[dict[str, Any]] | None = None,
) -> float:
    lowered_path = path.lower()
    name = Path(path).name.lower()
    stem = Path(name).stem.lower()
    path_tokens = set()
    for token in re.findall(r"[a-z0-9][a-z0-9._-]{1,}", lowered_path):
        path_tokens.add(token.strip("._-"))
        path_tokens.update(part for part in re.split(r"[._-]+", token) if len(part) >= 2)
    score = 0.0
    matches = context_tokens & {token for token in path_tokens if token}
    score += len(matches) * 8.0
    if any(token and token in stem for token in context_tokens):
        score += 6.0
    if "/" in path:
        score += min(path.count("/"), 8) * 0.15
    auxiliary_markers = (
        "vocab",
        "tokenizer",
        "tokeniser",
        "mmproj",
        "adapter",
        "config",
        "readme",
        "license",
        "metadata",
        "sample",
        "example",
        "stub",
    )
    if any(marker in name for marker in auxiliary_markers):
        score -= 18.0
    if name.startswith("."):
        score -= 20.0
    suffix = Path(name).suffix.lower()
    if suffix:
        score += 1.0
    score += _candidate_file_observation_score(path, recent_steps or [])
    score -= original_index * 0.01
    return score


def _invalid_candidate_file_paths(paths: list[str], recent_steps: list[dict[str, Any]]) -> list[str]:
    invalid: list[str] = []
    for path in paths:
        if _candidate_file_observation_score(path, recent_steps) <= -30:
            invalid.append(path)
    return invalid


def _candidate_file_observation_score(path: str, recent_steps: list[dict[str, Any]], *, window: int = 12) -> float:
    if not path:
        return 0.0
    path_key = path.lower()
    score = 0.0
    for step in _completed_recent_steps(recent_steps)[-window:]:
        if step.get("tool_name") != "shell_exec":
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        text = "\n".join(str(output.get(key) or "") for key in ("stdout", "stderr"))
        for line in text.splitlines():
            lowered = line.lower()
            if path_key not in lowered:
                continue
            if _shell_line_reports_missing_candidate(line):
                score -= 70.0
            if any(marker in lowered for marker in ("ascii text", "html document", "json data", "with no line terminators")):
                score -= 45.0
            score += _candidate_file_size_score_from_line(line)
    return score


def _candidate_file_size_score_from_line(line: str) -> float:
    lowered = str(line or "").lower()
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:t|tb|tib|g|gb|gib)\b", lowered):
        return 55.0
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:m|mb|mib)\b", lowered):
        return 18.0
    integers = [int(match) for match in re.findall(r"(?<![\w.])\d{1,15}(?![\w.])", lowered)]
    if any(value >= 1_000_000_000 for value in integers):
        return 55.0
    if any(value >= 1_000_000 for value in integers):
        return 18.0
    return 0.0


def _open_file_dependent_task_text(job: dict[str, Any]) -> str:
    tasks = _metadata_list(job, "task_queue")
    parts: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "open").lower()
        if status not in {"open", "active", "waiting", "blocked"}:
            continue
        text = " ".join(
            str(task.get(key) or "")
            for key in ("title", "description", "acceptance_criteria", "evidence_needed", "stall_behavior", "contract")
        )
        lowered = text.lower()
        if any(term in lowered for term in ("file", "path", "download", "artifact", "validate", "benchmark", "script", "config")):
            parts.append(" ".join(text.split()))
        if len(parts) >= 4:
            break
    return " | ".join(parts)


def _candidate_file_paths_from_recent_shell(
    recent_steps: list[dict[str, Any]], *, window: int = 8, max_paths: int = 80
) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for step in _completed_recent_steps(recent_steps)[-window:]:
        if step.get("tool_name") != "shell_exec":
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        text = "\n".join(str(output.get(key) or "") for key in ("stdout", "stderr"))
        for path in _extract_candidate_file_paths(text):
            key = path.lower()
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
            if len(paths) >= max_paths:
                return paths
    return paths


def _candidate_file_paths_from_recent_grounding_blocks(
    recent_steps: list[dict[str, Any]], *, window: int = 8, max_paths: int = 80
) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for step in recent_steps[-window:]:
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        grounding = output.get("evidence_grounding") if isinstance(output.get("evidence_grounding"), dict) else {}
        candidates = grounding.get("missing_candidate_paths")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            path = _clean_candidate_file_path(str(candidate or ""))
            if not _looks_like_exact_candidate_file_path(path):
                continue
            key = path.lower()
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
            if len(paths) >= max_paths:
                return paths
    return paths


def _candidate_file_paths_from_durable_records(
    job: dict[str, Any], *, max_records: int = 80, max_paths: int = 80
) -> list[str]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    paths: list[str] = []
    seen: set[str] = set()
    record_groups = [
        _metadata_list(job, "experiment_ledger"),
        _metadata_list(job, "finding_ledger"),
        _metadata_list(job, "lessons"),
        _metadata_list(job, "source_ledger"),
        _metadata_list(job, "task_queue"),
    ]
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    record_groups.append([item for item in milestones if isinstance(item, dict)])
    checked = 0
    for records in record_groups:
        for record in reversed(records[-max_records:]):
            if not isinstance(record, dict):
                continue
            checked += 1
            try:
                text = json.dumps(record, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                text = str(record)
            for path in _extract_candidate_file_paths(text):
                key = path.lower()
                if key in seen:
                    continue
                seen.add(key)
                paths.append(path)
                if len(paths) >= max_paths:
                    return paths
            if checked >= max_records * len(record_groups):
                return paths
    return paths


def _extract_candidate_file_paths(text: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"(?<![A-Za-z0-9])(?:~|/)[^\s'\"<>|;&]{2,}", text or ""):
        raw = _clean_candidate_file_path(match.group(0))
        if not _looks_like_exact_candidate_file_path(raw):
            continue
        paths.append(raw)
    for match in re.finditer(r'"path"\s*:\s*"([^"]+\.[A-Za-z0-9][A-Za-z0-9_-]{1,12})"', text or ""):
        raw = _clean_candidate_file_path(match.group(1))
        if not _looks_like_exact_candidate_file_path(raw, allow_relative=True):
            continue
        paths.append(raw)
    return paths


def _looks_like_exact_candidate_file_path(value: str, *, allow_relative: bool = False) -> bool:
    raw = str(value or "").strip()
    if not raw or len(raw) > 500:
        return False
    if "://" in raw or raw.startswith("//") or "..." in raw or "…" in raw or "*" in raw:
        return False
    if not allow_relative and not raw.startswith(("/", "~")):
        return False
    name = Path(raw).name
    if not name or name.startswith("."):
        return False
    suffix = Path(name).suffix
    if not suffix or not re.match(r"^\.[A-Za-z0-9][A-Za-z0-9_]{1,12}$", suffix) or not any(ch.isalpha() for ch in suffix):
        return False
    return True


def _clean_candidate_file_path(value: str) -> str:
    raw = str(value or "").strip().rstrip(".,:;)")
    for separator in ("\\n", "\\r", "\\t", "\n", "\r", "\t"):
        raw = raw.split(separator, 1)[0]
    return raw.strip().rstrip(".,:;)")


def _progress_accounting_for_prompt(recent_steps: list[dict[str, Any]]) -> str:
    context = _artifact_accounting_context(recent_steps)
    if not context:
        return "None."
    return (
        "Recent saved outputs need accounting before more output/research. "
        f"artifact_count={context.get('artifact_count')} since_step={context.get('since_step')} "
        f"artifact_titles={'; '.join(str(title) for title in context.get('artifact_titles', [])[:4])}. "
        "Next use record_tasks or record_roadmap to mark progress/reopen branches, "
        "record_findings or record_source for reusable evidence, record_experiment for measured results, "
        "record_milestone_validation for milestone checks, or record_lesson if these outputs are not useful."
    )


def _activity_stagnation_for_prompt(job: dict[str, Any]) -> str:
    context = _activity_stagnation_context(job)
    if not context:
        return "None."
    return (
        "Recent checkpoints have reported activity without durable progress. "
        f"activity_checkpoint_streak={context.get('streak')} threshold={context.get('threshold')} "
        f"last_counts={context.get('counts')}. "
        "Next classify the branch with record_findings, record_source, record_experiment, record_tasks, "
        "record_roadmap, record_milestone_validation, or record_lesson. If the branch is low-yield, mark it "
        "blocked/skipped and pivot before doing more read-only work or saving more outputs."
    )


def _research_balance_guard_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _research_balance_context(job, recent_steps)
    if not context:
        return "None."
    return (
        "Recent work is execution-heavy but has little source-backed research recorded. "
        f"completed_window={context.get('completed_window')} execution_actions={context.get('execution_actions')} "
        f"research_actions={context.get('research_actions')} sources={context.get('sources')} findings={context.get('findings')} "
        f"experiments={context.get('experiments')} files={context.get('files')}. "
        "Before another deep execution/testing loop, use available research, browser, source, documentation, or local-inspection tools "
        "to gather evidence and record it with record_source, record_findings, record_lesson, record_tasks, or an artifact. "
        "If external research is not relevant or tools are unavailable, explicitly record why and what evidence substitutes for it."
    )


def _source_yield_guard_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _source_yield_context(job, recent_steps)
    if not context:
        return "None."
    return (
        "Many sources have been gathered without enough durable synthesis. "
        f"sources={context.get('sources')} findings={context.get('findings')} "
        f"yielded_sources={context.get('yielded_sources')} recent_gathering={context.get('recent_gathering')} "
        f"recent_source_titles={'; '.join(str(title) for title in context.get('recent_source_titles', [])[:4])}. "
        "Before more search, extraction, browsing, shell work, file/output writing, or report chatter, distill the "
        "source set into record_findings with evidence, update record_source with yield/fail outcomes, or update "
        "tasks/roadmap/lessons to reject or pivot the low-yield source branch."
    )


def _task_planning_guard_for_prompt(job: dict[str, Any]) -> str:
    context = _task_planning_stagnation_context(job)
    if not context:
        return "None."
    return (
        "Recent checkpoints only added or updated tasks without durable evidence, measurements, validations, "
        f"or lessons. task_only_checkpoints={context.get('task_only_checkpoints')} "
        f"open_tasks={context.get('open_tasks')} total_tasks={context.get('total_tasks')}. "
        "Do not create more new open tasks next. Execute, measure, validate, write a checkpoint, mark existing "
        "tasks done/blocked/skipped, or record a lesson from the branch."
    )


def _task_queue_saturation_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _recent_task_queue_saturation_context(recent_steps)
    persistent_pressure = False
    if not context:
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        pressure = metadata.get("task_backlog_pressure") if isinstance(metadata.get("task_backlog_pressure"), dict) else {}
        current_pressure = _current_task_backlog_pressure_context(job)
        if not pressure and not current_pressure:
            return "None."
        if current_pressure:
            guard_recovery = pressure.get("guard_recovery") if isinstance(pressure.get("guard_recovery"), dict) else {}
            task_queue = guard_recovery.get("task_queue") if isinstance(guard_recovery.get("task_queue"), dict) else {}
            context = {
                "step_no": pressure.get("latest_step_no") or guard_recovery.get("latest_step_no") or "current",
                "source": pressure.get("source") or ("guard_recovery" if guard_recovery else "current_queue"),
                "reason": pressure.get("reason") or task_queue.get("reason") or current_pressure.get("reason"),
                "open_count": current_pressure.get("open_count"),
                "total_count": current_pressure.get("total_count"),
                "open_titles": current_pressure.get("open_titles") or [],
            }
        else:
            return "None."
        persistent_pressure = True
    counts = []
    if context.get("open_count") is not None:
        counts.append(f"open_tasks={context.get('open_count')}")
    if context.get("total_count") is not None:
        counts.append(f"total_tasks={context.get('total_count')}")
    count_text = " ".join(counts) or "queue is saturated"
    open_titles = [str(title).strip() for title in context.get("open_titles") or [] if str(title).strip()]
    title_text = f" Existing runnable task titles: {json.dumps(open_titles[:8], ensure_ascii=False)}." if open_titles else ""
    if context.get("source") == "blocked_record_tasks":
        source_label = "record_tasks block"
    elif context.get("source") == "current_queue":
        source_label = "current queue"
    else:
        source_label = "guard recovery"
    opening = (
        f"Task backlog pressure remains active from {source_label} #{context.get('step_no')}: "
        if persistent_pressure
        else f"Task queue saturation was just hit at step #{context.get('step_no')}: "
    )
    return (
        opening
        + f"{context.get('reason') or 'task queue saturated'} ({count_text}). "
        f"{title_text} "
        "Do not create new task branches. Either execute an existing high-priority branch, "
        "or use record_tasks only to update existing task titles to active, done, blocked, or skipped "
        "with concise result/evidence. If you have a near-duplicate task, update the closest existing "
        "task instead of inventing a fresh title. Consolidate branch sprawl into roadmap/milestones when useful. "
        "If this repeats, record_tasks is temporarily withheld so the worker must use a non-planning action."
    )


def _current_task_backlog_pressure_context(job: dict[str, Any]) -> dict[str, Any] | None:
    tasks = _metadata_list(job, "task_queue")
    objective_tasks = [task for task in tasks if not _is_guard_recovery_task(task)]
    open_tasks = [
        task
        for task in objective_tasks
        if str(task.get("status") or "open").strip().lower().replace(" ", "_") in {"open", "active"}
    ]
    if len(objective_tasks) <= TASK_QUEUE_TOTAL_SOFT_LIMIT and len(open_tasks) < TASK_QUEUE_SATURATION_OPEN_TASKS:
        return None
    reason = "total task queue is too large" if len(objective_tasks) > TASK_QUEUE_TOTAL_SOFT_LIMIT else "too many open tasks"
    return {
        "reason": reason,
        "open_count": len(open_tasks),
        "total_count": len(objective_tasks),
        "open_titles": [
            str(task.get("title") or "").strip()
            for task in open_tasks[:8]
            if str(task.get("title") or "").strip()
        ],
    }


def _memory_consolidation_guard_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _memory_graph_consolidation_context(job, recent_steps)
    if not context:
        return "None."
    return (
        "Durable job memory is growing faster than the connected memory graph. "
        f"durable_records={context.get('durable_records')} graph_nodes={context.get('graph_nodes')} "
        f"graph_edges={context.get('graph_edges')} reason={context.get('reason')}. "
        "Before more branch work, use record_memory_graph to consolidate the most reusable facts, strategies, "
        "decisions, questions, skills, constraints, episodes, and evidence links. If there is truly nothing "
        "reusable, record a lesson explaining why this branch should not become graph memory."
    )


def _lesson_consolidation_guard_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _lesson_sprawl_context(job, recent_steps)
    if not context:
        return "None."
    return (
        "Raw lessons are accumulating faster than consolidated memory. "
        f"lessons={context.get('lessons')} recent_lessons={context.get('recent_lessons')} "
        f"graph_nodes={context.get('graph_nodes')} reason={context.get('reason')}. "
        "Do not add another raw lesson next. Consolidate the reusable strategy, mistake, constraint, decision, "
        "or question into record_memory_graph, or update existing tasks/roadmap state if the lesson only describes "
        "branch status."
    )


def _memory_graph_consolidation_context(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    if any(step.get("tool_name") == "record_memory_graph" and step.get("status") == "completed" for step in recent_steps[-8:]):
        return None
    graph = memory_graph_from_job(job)
    node_count = len(graph["nodes"])
    edge_count = len(graph["edges"])
    durable_records = _durable_memory_signal_count(job)
    if durable_records < 6:
        return None
    reason = ""
    if node_count == 0:
        reason = "durable ledgers exist but no graph nodes have been consolidated"
    elif durable_records >= 12 and node_count * 5 < durable_records:
        reason = "graph is sparse relative to reusable durable records"
    elif node_count >= 3 and edge_count == 0 and durable_records >= 10:
        reason = "graph nodes exist but have no links"
    if not reason:
        return None
    return {
        "durable_records": durable_records,
        "graph_nodes": node_count,
        "graph_edges": edge_count,
        "reason": reason,
    }


def _lesson_sprawl_context(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    memory_context = _memory_graph_consolidation_context(job, recent_steps)
    if not memory_context:
        return None
    lessons = _metadata_list(job, "lessons")
    lesson_count = len(lessons)
    if lesson_count < LESSON_SPRAWL_MIN_LESSONS:
        return None
    recent_lessons = [
        step
        for step in recent_steps[-12:]
        if step.get("tool_name") == "record_lesson" and str(step.get("status") or "").lower() == "completed"
    ]
    if len(recent_lessons) < LESSON_SPRAWL_RECENT_LESSONS and lesson_count < LESSON_SPRAWL_MIN_LESSONS * 2:
        return None
    return {
        "lessons": lesson_count,
        "recent_lessons": len(recent_lessons),
        "graph_nodes": memory_context.get("graph_nodes"),
        "graph_edges": memory_context.get("graph_edges"),
        "durable_records": memory_context.get("durable_records"),
        "reason": "raw lesson backlog needs graph consolidation",
    }


def _durable_memory_signal_count(job: dict[str, Any]) -> int:
    count = (
        len(_metadata_list(job, "finding_ledger"))
        + len(_metadata_list(job, "source_ledger"))
        + len(_metadata_list(job, "experiment_ledger"))
        + len(_metadata_list(job, "lessons"))
    )
    tasks = _metadata_list(job, "task_queue")
    count += sum(
        1
        for task in tasks
        if str(task.get("status") or "open").lower() in {"done", "blocked", "skipped"}
        and (task.get("result") or task.get("evidence_needed") or task.get("acceptance_criteria"))
    )
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    count += sum(
        1
        for milestone in milestones
        if isinstance(milestone, dict)
        and str(milestone.get("status") or "planned").lower() in {"active", "validating", "done", "blocked", "skipped"}
    )
    return count


def _durable_yield_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    completed = [step for step in recent_steps if step.get("status") == "completed"]
    if len(completed) < 20:
        return "None."
    durable_tools = LEDGER_PROGRESS_TOOLS | {"write_artifact", "write_file"}
    durable_indexes = [
        index
        for index, step in enumerate(completed)
        if step.get("tool_name") in durable_tools
    ]
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    durable_records = (
        len(_metadata_list(job, "finding_ledger"))
        + len(_metadata_list(job, "source_ledger"))
        + len(_metadata_list(job, "experiment_ledger"))
        + len(_metadata_list(job, "lessons"))
    )
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    durable_records += len(milestones)
    if not durable_indexes and durable_records <= 0:
        return (
            f"No durable progress records after {len(completed)} completed actions. "
            "Next action should save an output, record findings/source/experiment/lesson/roadmap progress, "
            "or mark the branch blocked/skipped before more read-only work."
        )
    last_durable_index = durable_indexes[-1] if durable_indexes else -1
    actions_since = len(completed) - last_durable_index - 1
    durable_steps = len(durable_indexes)
    actions_per_durable = len(completed) / max(1, durable_steps + durable_records)
    if actions_since < 25 and actions_per_durable < 30:
        return "None."
    return (
        f"Durable yield is low: completed_actions={len(completed)} durable_steps={durable_steps} "
        f"durable_records={durable_records} actions_since_last_durable={actions_since} "
        f"actions_per_durable~{actions_per_durable:.1f}. "
        "Prefer a concrete checkpoint next: write/save output, record measured or reusable evidence, validate a milestone, "
        "or reject/pivot the branch with a lesson."
    )


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
    file_validation = _pending_file_validation_obligation(job)
    if file_validation:
        return (
            "A recently written file needs validation before more branch work. "
            f"File: {_clip_text(str(file_validation.get('path') or ''), 260)}. "
            f"Suggested validation: {_clip_text(str(file_validation.get('suggested_validation') or ''), 360)}. "
            "Use shell_exec to validate it, or record_tasks/record_lesson/record_experiment if validation is blocked or deferred."
        )
    artifact_accounting = _artifact_accounting_context(recent_steps)
    if artifact_accounting:
        return (
            "Recent saved outputs need durable accounting. Before more artifact writing, reading, research, browsing, "
            "or shell work, use record_tasks, record_roadmap, record_milestone_validation, record_findings, record_source, record_experiment, or record_lesson "
            "to explain what changed and what branch is next."
        )
    checkpoint_accounting = _auto_checkpoint_accounting_context(job, recent_steps)
    if checkpoint_accounting:
        if not checkpoint_accounting.get("checkpoint_read"):
            return (
                "An auto-saved evidence checkpoint is pending. Read that specific checkpoint artifact, or use a durable "
                "ledger tool to account for the checkpoint from existing evidence before more branch work."
            )
        return (
            "An already-read evidence checkpoint is pending durable accounting. Use record_findings, record_source, "
            "record_experiment, record_tasks, record_roadmap, record_milestone_validation, or record_lesson before "
            "more shell, search, file, report, or artifact work."
        )
    grounding_block = _latest_evidence_grounding_block(recent_steps)
    if grounding_block:
        raw_missing_paths = grounding_block.get("missing_candidate_paths") if isinstance(grounding_block.get("missing_candidate_paths"), list) else []
        missing_paths = [
            path
            for path in (_clean_candidate_file_path(str(item or "")) for item in raw_missing_paths)
            if _looks_like_exact_candidate_file_path(path)
        ]
        path_text = "; ".join(str(path) for path in missing_paths[:6])
        detail = f" Missing exact paths: {path_text}." if path_text else ""
        candidate_files = _candidate_file_discovery_context(job, recent_steps)
        if candidate_files:
            paths = candidate_files.get("paths") if isinstance(candidate_files.get("paths"), list) else []
            current_path_text = "; ".join(str(path) for path in paths[:4])
            if current_path_text:
                detail += (
                    " Current ranked candidate paths from recent/durable evidence: "
                    f"{_clip_text(current_path_text, 520)}."
                )
        return (
            "Recent evidence grounding blocked a durable record. Next, rewrite the record using only observed evidence, "
            "include the exact observed paths/tokens when claiming candidates or files, or explicitly record why they "
            f"are irrelevant/invalid.{detail}"
        )
    action_failure = _experiment_next_action_failure_context(job, recent_steps)
    if action_failure:
        return (
            "The latest experiment next action was attempted, but the observed shell output reports a missing "
            f"command/path/prerequisite at step #{action_failure.get('step_no') or '?'}. "
            f"Observed output: {_clip_text(str(action_failure.get('excerpt') or ''), 260)}. "
            "Next, account for this attempted action with record_experiment, record_tasks, or record_lesson: "
            "mark the branch failed/blocked or create the concrete recovery branch. Do not run more read-only probes "
            "until the failed action is durable."
        )
    measured_guard = _measured_progress_guard_context(job, recent_steps)
    if measured_guard:
        return (
            "This job needs measured progress, not more research-only activity. "
            "Do one of: run a small measuring command/action, call record_experiment for a known measurement, "
            "record_tasks with an experiment/action/monitor contract, or record_lesson if measurement is blocked."
        )
    activity_stagnation = _activity_stagnation_context(job)
    if activity_stagnation:
        return (
            "Recent checkpoints show activity without durable progress. "
            "Use a ledger or planning tool to classify what changed, reject the low-yield branch, or open a better branch "
            "before more read-only work or output churn."
        )
    task_planning_guard = _task_planning_stagnation_context(job)
    if task_planning_guard:
        return (
            "Recent progress is only task planning. Do not create more new open tasks next. Execute an existing task, "
            "record evidence/measurements/validation, write a checkpoint, mark tasks done/blocked/skipped, or record "
            "a lesson before expanding the queue again."
        )
    task_queue_saturation = _recent_task_queue_saturation_context(recent_steps)
    if task_queue_saturation:
        return (
            "The durable task queue is saturated. Do not create new task branches. Execute a current task, "
            "or use record_tasks only to update existing task titles to active/done/blocked/skipped with evidence."
        )
    memory_consolidation = _memory_graph_consolidation_context(job, recent_steps)
    if memory_consolidation:
        return (
            "Consolidate durable progress into the job memory graph before more branch work. "
            "Use record_memory_graph for connected reusable knowledge, or record_lesson if the recent branch has no "
            "reusable memory value."
        )
    deliverable_guard = _deliverable_progress_guard_context(job, recent_steps)
    if deliverable_guard:
        return (
            "This job needs a durable deliverable checkpoint, not more background collection. "
            "Use write_file or write_artifact to save a partial draft/report/file, or use record_tasks, "
            "record_roadmap, record_milestone_validation, or record_lesson to explain the specific blocker "
            "and the next deliverable branch."
        )
    research_balance = _research_balance_context(job, recent_steps)
    if research_balance:
        return (
            "Balance execution with research before the next deep action loop. "
            "Gather source-backed evidence with available web/browser/documentation/local-inspection tools and record it, "
            "or record why research is not applicable and what evidence replaces it."
        )
    candidate_files = _candidate_file_discovery_context(job, recent_steps)
    if candidate_files:
        paths = candidate_files.get("paths") if isinstance(candidate_files.get("paths"), list) else []
        path_text = "; ".join(str(path) for path in paths[:4])
        return (
            "Concrete candidate file paths are available while file/path-dependent work is open. "
            f"Validate likely candidates next with shell_exec before retrying downloads, searching for alternatives, "
            f"or recording no-file/no-progress claims. Candidate paths: {_clip_text(path_text, 520)}."
        )
    experiment_next_action = _latest_experiment_next_action_context(job)
    if experiment_next_action:
        return (
            "The latest measured experiment selected a concrete next action. "
            f"Next action: {_clip_text(experiment_next_action.get('next_action') or '', 520)}. "
            "Act on it with the appropriate tool, or use record_tasks/record_lesson if it is invalid or blocked. "
            "Do not bury it under more checkpoints or unrelated research."
        )
    milestone_validation = _milestone_validation_needed(job)
    if milestone_validation:
        return (
            f"Roadmap milestone '{milestone_validation.get('title')}' is ready for validation or is marked validating. "
            "Use record_milestone_validation with evidence and pass/fail/blocker status, then create follow-up tasks for gaps."
        )
    roadmap_staleness = _roadmap_staleness_context(job, recent_steps)
    if roadmap_staleness:
        return (
            "The roadmap has not advanced despite durable task/artifact activity. "
            "Use record_roadmap to mark the current milestone active/done/blocked, or record_milestone_validation "
            "if acceptance criteria can be judged from existing evidence, before more branch work."
        )
    if _roadmap_missing_for_broad_job(job):
        return (
            "The objective is broad enough to benefit from roadmap control. Use record_roadmap to define compact milestones, "
            "features, acceptance criteria, and validation checkpoints before expanding the task queue further."
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
        if step.get("status") == "failed" and step.get("tool_name") == "read_artifact":
            output = step.get("output") if isinstance(step.get("output"), dict) else {}
            if "artifact not found" in str(output.get("error") or step.get("summary") or "").lower():
                return (
                    "The last artifact read used a reference that does not exist. Do not invent or retry artifact ids. "
                    "Use a valid recent artifact ref, call search_artifacts with a concrete query, or continue from "
                    "already observed evidence with a durable record."
                )
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


def _latest_evidence_grounding_block(recent_steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    resolution_after_block = False
    for step in reversed(recent_steps[-8:]):
        if (
            step.get("status") == "completed"
            and step.get("tool_name") in EVIDENCE_GROUNDING_RESOLUTION_TOOLS
        ):
            resolution_after_block = True
            continue
        if step.get("status") != "blocked":
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        if output.get("error") != "evidence grounding required":
            continue
        if resolution_after_block:
            return None
        grounding = output.get("evidence_grounding") if isinstance(output.get("evidence_grounding"), dict) else {}
        return grounding or {"unsupported_tokens": []}
    return None


def _milestone_validation_needed(job: dict[str, Any]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        status = str(milestone.get("status") or "planned")
        validation_status = str(milestone.get("validation_status") or "not_started")
        if status == "validating" or validation_status == "pending":
            return milestone
        features = milestone.get("features") if isinstance(milestone.get("features"), list) else []
        if status == "active" and features and all(
            isinstance(feature, dict) and str(feature.get("status") or "planned") in {"done", "skipped"}
            for feature in features
        ):
            return milestone
    return None


def _tool_call_matches_pending_milestone_need(tool_name: str, args: dict[str, Any], milestone: dict[str, Any]) -> bool:
    if str(milestone.get("validation_status") or "").strip().lower() != "pending":
        return False
    if tool_name not in BRANCH_WORK_TOOLS:
        return False
    return _text_matches_pending_milestone_need(_json_value_text(args), milestone)


def _text_matches_pending_milestone_need(text: str, milestone: dict[str, Any]) -> bool:
    parts = [
        str(milestone.get("title") or ""),
        str(milestone.get("next_action") or ""),
        str(milestone.get("acceptance_criteria") or ""),
        str(milestone.get("evidence_needed") or ""),
        str(milestone.get("validation_evidence") or ""),
        str(milestone.get("validation_result") or ""),
        " ".join(str(item) for item in milestone.get("validation_issues") or [] if item),
    ]
    features = milestone.get("features") if isinstance(milestone.get("features"), list) else []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        parts.extend([
            str(feature.get("title") or ""),
            str(feature.get("goal") or ""),
            str(feature.get("acceptance_criteria") or ""),
            str(feature.get("evidence_needed") or ""),
        ])
    need_tokens = _substantive_next_action_tokens(" ".join(parts)) - MILESTONE_MATCH_STOPWORDS
    if not need_tokens:
        return False
    call_tokens = _substantive_next_action_tokens(text) - MILESTONE_MATCH_STOPWORDS
    if not call_tokens:
        return False
    return bool(need_tokens & call_tokens)


def _milestone_validation_call_matches_current(args: dict[str, Any], milestone: dict[str, Any]) -> bool:
    requested = _norm_task_key("", str(args.get("milestone") or args.get("title") or ""))
    if not requested:
        return False
    candidates = [
        _norm_task_key("", str(milestone.get("title") or "")),
        _norm_task_key("", str(milestone.get("key") or "")),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if requested == candidate or requested in candidate or candidate in requested:
            return True
    return False


def _normalize_milestone_validation_args_for_active_gate(
    tool_name: str,
    args: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    if tool_name != "record_milestone_validation":
        return args
    milestone = _milestone_validation_needed(job)
    if not milestone or _milestone_validation_call_matches_current(args, milestone):
        return args
    if not _text_matches_pending_milestone_need(_json_value_text(args), milestone):
        return args
    normalized = dict(args)
    normalized["milestone"] = str(milestone.get("title") or args.get("milestone") or "")
    metadata = normalized.get("metadata") if isinstance(normalized.get("metadata"), dict) else {}
    normalized["metadata"] = {
        **metadata,
        "normalized_from_milestone": str(args.get("milestone") or ""),
        "normalized_to_active_gate": True,
    }
    return normalized


def _latest_experiment_next_action_context(job: dict[str, Any]) -> dict[str, Any] | None:
    experiments = _metadata_list(job, "experiment_ledger")
    for experiment in reversed(experiments):
        if not isinstance(experiment, dict):
            continue
        status = str(experiment.get("status") or "").strip().lower()
        next_action = str(experiment.get("next_action") or "").strip()
        if not next_action:
            continue
        if status in {"measured", "failed", "blocked"} or experiment.get("metric_value") is not None:
            return {
                "title": experiment.get("title"),
                "status": status,
                "metric_name": experiment.get("metric_name"),
                "metric_value": experiment.get("metric_value"),
                "next_action": next_action,
            }
    return None


def _experiment_next_action_requires_delivery(context: dict[str, Any] | None) -> bool:
    if not context:
        return False
    next_action = str(context.get("next_action") or "").lower()
    if not next_action:
        return False
    tokens = set(re.findall(r"[a-z][a-z0-9_-]+", next_action))
    if not tokens & EXPERIMENT_DELIVERY_ACTION_TERMS:
        return False
    return not bool(tokens & EXPERIMENT_INFORMATION_ACTION_TERMS)


def _experiment_next_action_failure_context(job: dict[str, Any], recent_steps: list[dict[str, Any]], *, window: int = 8) -> dict[str, Any] | None:
    context = _latest_experiment_next_action_context(job)
    if not _experiment_next_action_requires_delivery(context):
        return None
    latest_experiment_step_no = max(
        (
            _as_int(step.get("step_no"))
            for step in recent_steps
            if step.get("tool_name") == "record_experiment" and step.get("status") == "completed"
        ),
        default=0,
    )
    next_action = str(context.get("next_action") or "") if context else ""
    for step in reversed(_completed_or_failed_recent_steps(recent_steps)[-window:]):
        if step.get("tool_name") != "shell_exec":
            continue
        if latest_experiment_step_no and _as_int(step.get("step_no")) <= latest_experiment_step_no:
            continue
        text = _shell_step_failure_text(step)
        if not text.strip() or not _shell_output_has_missing_command(text):
            continue
        command = _step_command(step)
        if not _shell_command_matches_next_action(command, next_action):
            continue
        return {
            "step_no": step.get("step_no"),
            "command": command,
            "excerpt": text.strip(),
            "missing_commands": _missing_commands_from_shell_output(text),
            "missing_paths": _missing_paths_from_shell_output(text),
            "experiment_next_action": context,
        }
    return None


def _shell_command_looks_like_write(command: str) -> bool:
    text = command.strip()
    if not text:
        return False
    if re.match(r"(?is)^curl\b", text):
        download_flags = (
            r"(?:^|\s)(?:-o\s*\S+|-O\b|--output(?:=|\s+)\S+|--remote-name\b|--output-dir(?:=|\s+)\S+)"
        )
        if re.search(download_flags, text):
            return True
    if re.match(r"(?is)^(?:wget|aria2c)\b", text):
        return True
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


def _shell_command_looks_read_only(command: str) -> bool:
    text = command.strip()
    if not text:
        return False
    if _shell_command_looks_like_write(text):
        return False
    if READ_ONLY_SHELL_COMMAND_PATTERN.search(text):
        return True
    if re.match(r"(?is)^curl\b", text):
        mutating_flags = r"\b-X\s*(?:POST|PUT|PATCH|DELETE)\b|--request\s+(?:POST|PUT|PATCH|DELETE)\b|(?:^|\s)(?:-d|--data|--form|-F|-T|--upload-file)\b"
        return not bool(re.search(mutating_flags, text))
    return False


def _shell_command_supports_experiment_next_action(command: str, context: dict[str, Any] | None) -> bool:
    if not context:
        return False
    text = command.strip()
    if not text or not EXPERIMENT_NEXT_ACTION_VERIFY_SHELL_PATTERN.search(text):
        return False
    next_action = str(context.get("next_action") or "")
    if not next_action.strip():
        return False
    action_tokens = _substantive_next_action_tokens(next_action)
    if not action_tokens:
        return False
    command_tokens = _substantive_next_action_tokens(text)
    return bool(action_tokens & command_tokens)


def _shell_command_matches_next_action(command: str, next_action: str) -> bool:
    if not command.strip() or not next_action.strip():
        return False
    action_tokens = _substantive_next_action_tokens(next_action)
    command_tokens = _substantive_next_action_tokens(command)
    return bool(action_tokens & command_tokens)


def _substantive_next_action_tokens(text: str) -> set[str]:
    tokens = set()
    for token in re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", text.lower()):
        token = token.strip("._-")
        if len(token) < 3:
            continue
        if token in TEXT_TOKEN_STOPWORDS or token in EXPERIMENT_NEXT_ACTION_VERIFY_STOPWORDS:
            continue
        tokens.add(token)
        for part in re.split(r"[._/-]+", token):
            if len(part) >= 3 and part not in TEXT_TOKEN_STOPWORDS and part not in EXPERIMENT_NEXT_ACTION_VERIFY_STOPWORDS:
                tokens.add(part)
    return tokens


def _roadmap_staleness_context(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    if not milestones:
        return None
    if any(step.get("tool_name") in {"record_roadmap", "record_milestone_validation"} for step in recent_steps):
        return None
    if any(
        isinstance(milestone, dict)
        and (
            str(milestone.get("status") or "planned") != "planned"
            or str(milestone.get("validation_status") or "not_started") != "not_started"
        )
        for milestone in milestones
    ):
        return None
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    completed_artifacts = [
        step for step in recent_steps
        if step.get("status") == "completed" and step.get("tool_name") == "write_artifact"
    ]
    task_updates = [
        step for step in recent_steps
        if step.get("status") == "completed" and step.get("tool_name") == "record_tasks"
    ]
    if len(completed_artifacts) < 2 and len(task_updates) < 2 and len(tasks) < 8:
        return None
    return {
        "title": roadmap.get("title") or "Roadmap",
        "status": roadmap.get("status") or "planned",
        "milestone_count": len(milestones),
        "task_count": len(tasks),
        "artifact_count": len(completed_artifacts),
        "task_update_count": len(task_updates),
    }


def _roadmap_missing_for_broad_job(job: dict[str, Any]) -> bool:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    if isinstance(metadata.get("roadmap"), dict):
        return False
    objective = str(job.get("objective") or "")
    tasks = metadata.get("task_queue") if isinstance(metadata.get("task_queue"), list) else []
    if len(tasks) >= 6:
        return True
    words = re.findall(r"[A-Za-z0-9_]+", objective)
    broad_terms = {"build", "create", "develop", "implement", "research", "improve", "optimize", "migrate", "write", "analyze"}
    return len(words) >= 14 and any(term in objective.lower() for term in broad_terms)


def _task_queue_exhausted(job: dict[str, Any]) -> bool:
    tasks = _metadata_list(job, "task_queue")
    if not tasks:
        return False
    runnable = {"open", "active"}
    return not any(str(task.get("status") or "open").strip().lower() in runnable for task in tasks)


def _task_queue_saturation_context(job: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    tasks = _metadata_list(job, "task_queue")
    objective_tasks = [task for task in tasks if not _is_guard_recovery_task(task)]
    open_tasks = [task for task in objective_tasks if str(task.get("status") or "open").strip().lower() in {"open", "active"}]
    incoming = args.get("tasks") if isinstance(args.get("tasks"), list) else []
    if not incoming:
        return None
    existing_keys = {
        _norm_task_key(str(task.get("parent") or ""), str(task.get("title") or ""))
        for task in tasks
    }
    semantic_matches = []
    new_open_titles = []
    new_titles = []
    for task in incoming:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "open").strip().lower().replace(" ", "_")
        title = str(task.get("title") or task.get("name") or "").strip()
        parent = str(task.get("parent") or "")
        key = _norm_task_key(parent, title)
        matched_existing = key in existing_keys
        semantic_match = None
        if not matched_existing and (len(objective_tasks) > TASK_QUEUE_TOTAL_SOFT_LIMIT or len(open_tasks) >= TASK_QUEUE_SATURATION_OPEN_TASKS):
            semantic_match = find_semantic_task_match(
                title=title,
                parent=parent,
                tasks=[existing for existing in tasks if not _is_guard_recovery_task(existing)],
            )
            matched_existing = bool(semantic_match)
        if semantic_match:
            semantic_matches.append({
                "title": title,
                "matched_title": semantic_match.get("title"),
                "score": semantic_match.get("score"),
            })
        if not matched_existing:
            new_titles.append(str(task.get("title") or "").strip())
        if status in {"open", "active"} and not matched_existing:
            new_open_titles.append(str(task.get("title") or "").strip())
    projected_total = len(objective_tasks) + len(new_titles)
    projected_open = len(open_tasks) + len(new_open_titles)
    if projected_total > TASK_QUEUE_TOTAL_SOFT_LIMIT and new_titles:
        return {
            "reason": "total task queue is too large",
            "total_count": len(objective_tasks),
            "projected_total_count": projected_total,
            "total_threshold": TASK_QUEUE_TOTAL_SOFT_LIMIT,
            "open_count": len(open_tasks),
            "open_titles": [
                str(task.get("title") or "").strip()
                for task in open_tasks[:8]
                if str(task.get("title") or "").strip()
            ],
            "new_count": len(new_titles),
            "new_titles": new_titles[:8],
            "semantic_matches": semantic_matches[:8],
            "recovery_task_count": len(tasks) - len(objective_tasks),
        }
    if projected_open < TASK_QUEUE_SATURATION_OPEN_TASKS:
        return None
    if not new_open_titles:
        return None
    return {
        "reason": "too many open tasks",
        "open_count": len(open_tasks),
        "projected_open_count": projected_open,
        "open_threshold": TASK_QUEUE_SATURATION_OPEN_TASKS,
        "total_count": len(objective_tasks),
        "open_titles": [
            str(task.get("title") or "").strip()
            for task in open_tasks[:8]
            if str(task.get("title") or "").strip()
        ],
        "new_open_count": len(new_open_titles),
        "new_open_titles": new_open_titles[:8],
        "semantic_matches": semantic_matches[:8],
        "recovery_task_count": len(tasks) - len(objective_tasks),
    }


def _recent_task_queue_saturation_context(recent_steps: list[dict[str, Any]], *, window: int = 6) -> dict[str, Any] | None:
    for step in reversed(recent_steps[-window:]):
        if step.get("tool_name") != "record_tasks" or step.get("status") != "blocked":
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        if output.get("error") != "task queue saturated":
            continue
        task_queue = output.get("task_queue") if isinstance(output.get("task_queue"), dict) else {}
        return {
            "step_no": step.get("step_no"),
            "reason": task_queue.get("reason") or "task queue saturated",
            "open_count": task_queue.get("open_count"),
            "total_count": task_queue.get("total_count"),
            "open_titles": task_queue.get("open_titles") if isinstance(task_queue.get("open_titles"), list) else [],
        }
    return None


def _record_task_backlog_pressure(
    *,
    db: AgentDB,
    job_id: str,
    step_no: int | str | None,
    task_queue: dict[str, Any],
    source: str,
) -> None:
    if not isinstance(task_queue, dict) or not task_queue:
        return
    pressure = {
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "latest_step_no": step_no,
        "reason": task_queue.get("reason") or "task queue saturated",
        "open_count": task_queue.get("open_count"),
        "total_count": task_queue.get("total_count"),
        "projected_open_count": task_queue.get("projected_open_count"),
        "projected_total_count": task_queue.get("projected_total_count"),
        "open_titles": task_queue.get("open_titles") if isinstance(task_queue.get("open_titles"), list) else [],
    }
    db.update_job_metadata(job_id, {"task_backlog_pressure": pressure})
    db.append_agent_update(
        job_id,
        (
            "Task backlog pressure is active; next worker turns should execute, complete, block, skip, "
            "or consolidate existing tasks instead of adding new branches."
        ),
        category="blocked",
        metadata={"task_backlog_pressure": pressure},
    )


def _clear_stale_task_backlog_pressure(db: AgentDB, job_id: str, job: dict[str, Any]) -> bool:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    pressure = metadata.get("task_backlog_pressure")
    if not isinstance(pressure, dict) or not pressure:
        return False
    if _current_task_backlog_pressure_context(job):
        return False
    cleared = dict(pressure)
    cleared["resolved_at"] = datetime.now(timezone.utc).isoformat()
    db.update_job_metadata(job_id, {"task_backlog_pressure": {}})
    db.append_agent_update(
        job_id,
        "Task backlog pressure cleared; the active task queue is back under saturation limits.",
        category="progress",
        metadata={"cleared_task_backlog_pressure": cleared},
    )
    return True


def _repeated_task_queue_saturation_context(recent_steps: list[dict[str, Any]], *, window: int = 8, threshold: int = 2) -> dict[str, Any] | None:
    matches = []
    for step in recent_steps[-window:]:
        if step.get("tool_name") != "record_tasks" or step.get("status") != "blocked":
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        if output.get("error") == "task queue saturated":
            matches.append(step)
    if len(matches) < threshold:
        return None
    latest = matches[-1]
    output = latest.get("output") if isinstance(latest.get("output"), dict) else {}
    task_queue = output.get("task_queue") if isinstance(output.get("task_queue"), dict) else {}
    return {
        "count": len(matches),
        "latest_step_no": latest.get("step_no"),
        "reason": task_queue.get("reason") or "task queue saturated",
    }


def _task_planning_stagnation_context(job: dict[str, Any]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    streak = _as_int(metadata.get("task_planning_checkpoint_streak"))
    if streak < TASK_PLANNING_STAGNATION_CHECKPOINTS:
        return None
    tasks = _metadata_list(job, "task_queue")
    open_tasks = [
        task
        for task in tasks
        if str(task.get("status") or "open").strip().lower().replace(" ", "_") in {"open", "active"}
    ]
    return {
        "task_only_checkpoints": streak,
        "threshold": TASK_PLANNING_STAGNATION_CHECKPOINTS,
        "total_tasks": len(tasks),
        "open_tasks": len(open_tasks),
    }


def _is_guard_recovery_task(task: dict[str, Any]) -> bool:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    return bool(metadata.get("guard_recovery")) or str(task.get("title") or "").strip().lower().startswith("resolve guard:")


def _record_tasks_adds_new_open_work(args: dict[str, Any], job: dict[str, Any]) -> bool:
    incoming = args.get("tasks") if isinstance(args.get("tasks"), list) else []
    if not incoming:
        incoming = [args]
    tasks = _metadata_list(job, "task_queue")
    existing_keys = {
        _norm_task_key(str(task.get("parent") or ""), str(task.get("title") or ""))
        for task in tasks
    }
    for task in incoming:
        if not isinstance(task, dict):
            continue
        title = str(task.get("title") or task.get("name") or "").strip()
        if not title:
            continue
        status = str(task.get("status") or "open").strip().lower().replace(" ", "_")
        key = _norm_task_key(str(task.get("parent") or ""), title)
        if status in {"open", "active"} and key not in existing_keys:
            return True
    return False


def _norm_task_key(parent: str, title: str) -> str:
    return task_key(parent, title)


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
    left_host, left_path = _source_path_key(left)
    right_host, right_path = _source_path_key(right)
    if not left_host or left_host != right_host:
        return False
    if right_path in {"", "/"} or left_path in {"", "/"}:
        return False
    return left_path == right_path or left_path.startswith(right_path + "/") or right_path.startswith(left_path + "/")


def _source_path_key(value: str) -> tuple[str, str]:
    parsed = urlparse(_normalized_source_url(value))
    host = parsed.netloc.lower().removeprefix("www.")
    path = (parsed.path or "/").rstrip("/") or "/"
    return host, path


def _shell_source_matches(left: str, right: str) -> bool:
    if _same_source_url(left, right):
        return True
    left_host, left_path = _source_path_key(left)
    right_host, right_path = _source_path_key(right)
    if not left_host or left_host != right_host:
        return False
    if right_path in {"", "/"} or left_path in {"", "/"}:
        return False
    return left_path == right_path or left_path.startswith(right_path + "/") or right_path.startswith(left_path + "/")


def _urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"https?://[^\s'\"<>)}\]]+", str(text or "")):
        url = match.group(0).rstrip(".,;:")
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls


def _source_url_has_path(value: str) -> bool:
    _, path = _source_path_key(value)
    return path not in {"", "/"}


def _shell_guard_urls(text: str) -> list[str]:
    urls = _urls_from_text(text)
    if len(urls) <= 1:
        return urls
    path_urls = [url for url in urls if _source_url_has_path(url)]
    return path_urls or urls


SHELL_PLACEHOLDER_URL_HOSTS = {
    "domain",
    "endpoint",
    "example",
    "file",
    "host",
    "input",
    "output",
    "path",
    "placeholder",
    "source",
    "target",
    "url",
    "uri",
}

SHELL_PLACEHOLDER_FIELD_NAMES = (
    "command",
    "domain",
    "endpoint",
    "file",
    "host",
    "input",
    "output",
    "path",
    "source",
    "target",
    "url",
    "uri",
)


def _shell_placeholder_context(command: str) -> dict[str, Any] | None:
    command = str(command or "").strip()
    if not command:
        return None
    if "```" in command:
        return {
            "kind": "markdown_code_fence",
            "value": "```",
            "reason": "command contains markdown code fences instead of executable shell only",
        }
    if re.search(r"(?m)^\s*-{3,}\s+\S", command) or re.search(r"(?m)^\s*\d+\.\s+```", command):
        return {
            "kind": "markdown_prose",
            "value": "markdown prose",
            "reason": "command contains copied markdown prose instead of executable shell only",
        }
    for url in _urls_from_text(command):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host in SHELL_PLACEHOLDER_URL_HOSTS:
            return {
                "kind": "placeholder_url",
                "value": url,
                "reason": "URL host looks like an unresolved placeholder field",
            }
    fields = "|".join(re.escape(name) for name in SHELL_PLACEHOLDER_FIELD_NAMES)
    placeholder_patterns = [
        rf"<\s*(?:{fields})(?:[-_ ][A-Za-z0-9]+)?\s*>",
        rf"\{{\{{\s*(?:{fields})(?:[-_ ][A-Za-z0-9]+)?\s*\}}\}}",
        rf"\{{\s*(?:{fields})(?:[-_ ][A-Za-z0-9]+)?\s*\}}",
        r"</?\s*(?:parameter|arguments?|tool_call|function_call)\b[^>]*>",
        r"\b(?:YOUR|REPLACE|TODO|INSERT)_[A-Z0-9_]{3,}\b",
    ]
    for pattern in placeholder_patterns:
        match = re.search(pattern, command, flags=re.IGNORECASE)
        if match:
            return {
                "kind": "placeholder_token",
                "value": match.group(0),
                "reason": "command contains an unresolved placeholder token",
            }
    return None


def _source_failure_family_url(value: str) -> str:
    parsed = urlparse(_normalized_source_url(value))
    if not parsed.scheme or not parsed.netloc:
        return ""
    segments = [segment for segment in (parsed.path or "").split("/") if segment]
    if len(segments) < 2:
        return ""
    last = segments[-1]
    looks_file_like = "." in last
    family_segments = segments[:-1] if looks_file_like else segments
    if len(family_segments) < 2:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/{'/'.join(family_segments)}"


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
    if name not in {"browser_navigate", "web_extract", "shell_exec"}:
        return None
    bad_sources = _known_bad_sources(job)
    if not bad_sources:
        return None
    urls: list[str] = []
    if name == "browser_navigate":
        urls = [str(args.get("url") or "")]
    elif isinstance(args.get("urls"), list):
        urls = [str(url) for url in args["urls"]]
    elif name == "shell_exec":
        urls = _shell_guard_urls(str(args.get("command") or ""))
    for url in [url for url in urls if url.strip()]:
        for source in bad_sources:
            source_value = str(source.get("source") or "")
            if not source_value:
                continue
            matches = _shell_source_matches(url, source_value) if name == "shell_exec" else _source_matches(url, source_value)
            if matches:
                return source
            if name == "shell_exec":
                source_family = _source_failure_family_url(source_value)
                if source_family and _shell_source_matches(url, source_family):
                    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
                    return {
                        **source,
                        "source": source_family,
                        "source_type": "shell_exec_family",
                        "metadata": {**metadata, "source_family": True, "source_family_from": source_value},
                    }
    return None


def _tool_signature(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"


def _duplicate_recent_tool_call(
    name: str,
    args: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    window: int = 24,
) -> dict[str, Any] | None:
    if name in {"browser_snapshot", "defer_job"}:
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


def _completed_or_failed_recent_steps(recent_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in recent_steps if step.get("status") in {"completed", "failed"}]


BROWSER_RUNTIME_UNAVAILABLE_TERMS = (
    "browser runtime unavailable",
    "browser not found",
    "browser executable",
    "chrome not found",
    "could not find chrome",
    "chromium executable",
    "executable doesn't exist",
    "playwright browser cache",
    "puppeteer browser cache",
)


SELF_DEFER_TERMS = (
    "next worker turn",
    "next worker step",
    "picked up by next worker",
    "picked up by the next worker",
    "picked up by next turn",
    "picked up by the next turn",
)


def _is_browser_tool(name: str | None) -> bool:
    return bool(str(name or "").startswith("browser_"))


def _browser_runtime_unavailable_context(
    recent_steps: list[dict[str, Any]],
    *,
    window: int = 512,
) -> dict[str, Any] | None:
    latest_browser_success_no = max(
        (
            int(step.get("step_no") or 0)
            for step in recent_steps[-window:]
            if _is_browser_tool(step.get("tool_name")) and step.get("status") == "completed"
        ),
        default=0,
    )
    for step in reversed(recent_steps[-window:]):
        if not _is_browser_tool(step.get("tool_name")):
            continue
        step_no = int(step.get("step_no") or 0)
        if step_no <= latest_browser_success_no:
            continue
        if step.get("status") not in {"failed", "blocked"}:
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        text = " ".join(
            str(part or "")
            for part in (
                step.get("summary"),
                step.get("error"),
                output.get("error"),
                output.get("summary"),
                output.get("stderr"),
                output.get("stdout"),
            )
        ).lower()
        if any(term in text for term in BROWSER_RUNTIME_UNAVAILABLE_TERMS):
            error = str(output.get("error") or step.get("error") or step.get("summary") or "")
            return {
                "step_no": step.get("step_no"),
                "tool": step.get("tool_name"),
                "status": step.get("status"),
                "error": _clip_text(error, 500),
            }
    return None


def _self_defer_context(args: dict[str, Any]) -> dict[str, Any] | None:
    reason = str(args.get("reason") or "")
    next_action = str(args.get("next_action") or "")
    text = f"{reason} {next_action}".lower()
    matched = next((term for term in SELF_DEFER_TERMS if term in text), "")
    if not matched and next_action.strip() and not reason.strip():
        matched = "missing wait reason"
    if not matched:
        return None
    return {
        "matched": matched,
        "reason": reason,
        "next_action": next_action,
    }


EVIDENCE_GROUNDED_TOOLS = {
    "record_experiment",
    "record_findings",
    "record_lesson",
    "record_memory_graph",
    "record_roadmap",
    "report_update",
    "write_artifact",
}
NARRATIVE_EVIDENCE_GROUNDED_TOOLS = {
    "report_update",
    "write_artifact",
}
EVIDENCE_GROUNDING_RESOLUTION_TOOLS = {
    "record_experiment",
    "record_findings",
    "record_lesson",
    "record_memory_graph",
    "record_milestone_validation",
    "record_roadmap",
    "record_source",
    "record_tasks",
    "report_update",
    "write_artifact",
}
EVIDENCE_CHECKPOINT_RESOLUTION_TOOLS = {
    "record_experiment",
    "record_findings",
    "record_lesson",
    "record_milestone_validation",
    "record_roadmap",
    "record_source",
    "record_tasks",
}
EVIDENCE_CHECKPOINT_ACCOUNTING_TOOLS = EVIDENCE_CHECKPOINT_RESOLUTION_TOOLS | {"guard_recovery"}
EVIDENCE_CHECKPOINT_PROMPT_TOOLS = {
    "record_experiment",
    "record_findings",
    "record_lesson",
    "record_source",
}
EVIDENCE_TOKEN_IGNORE = {
    "acceptance",
    "action",
    "actions",
    "active",
    "agent",
    "artifact",
    "api",
    "baseline",
    "branch",
    "branches",
    "candidate",
    "candidates",
    "cdn",
    "checkpoint",
    "compare",
    "complete",
    "constraint",
    "criteria",
    "current",
    "data",
    "deliverable",
    "direct",
    "done",
    "download",
    "downloadable",
    "downloaded",
    "downloading",
    "downloads",
    "discovered",
    "discovery",
    "environment",
    "existing",
    "evidence",
    "experiment",
    "experiments",
    "feature",
    "features",
    "file",
    "files",
    "format",
    "finding",
    "findings",
    "file-level",
    "html",
    "http",
    "https",
    "inspect",
    "inspection",
    "investigate",
    "investigation",
    "json",
    "goal",
    "gguf",
    "hardware",
    "improve",
    "located",
    "memory",
    "metric",
    "milestone",
    "milestones",
    "model",
    "next",
    "observation",
    "observations",
    "open",
    "oid",
    "output",
    "outputs",
    "plan",
    "planned",
    "pending",
    "priority",
    "progress",
    "parse",
    "parsed",
    "parsing",
    "record",
    "report",
    "rest",
    "research",
    "result",
    "roadmap",
    "runtime",
    "search",
    "server",
    "source",
    "sources",
    "status",
    "sha",
    "sha256",
    "step",
    "steps",
    "task",
    "tasks",
    "test",
    "throughput",
    "tool",
    "tools",
    "false",
    "none",
    "null",
    "true",
    "url",
    "usable",
    "unvalidated",
    "valid",
    "validity",
    "validate",
    "validated",
    "validating",
    "validation",
    "worker",
    "xml",
    "yaml",
    "yml",
    "confirmed",
    "consider",
    "checking",
    "ongoing",
    "proceed",
    "proceeding",
}
EVIDENCE_TOKEN_IGNORE.update({f"p{index}" for index in range(10)})
STALE_CLAIM_TOKEN_IGNORE = {
    "api",
    "ascii",
    "blocked",
    "broken",
    "cdn",
    "cli",
    "critical",
    "cpu",
    "cuda",
    "discovered",
    "ggml",
    "gguf",
    "gpu",
    "hf_token",
    "html",
    "http",
    "https",
    "incomplete",
    "json",
    "lfs",
    "not_found",
    "oid",
    "onnx",
    "planned",
    "python",
    "python3",
    "ram",
    "rest",
    "severe",
    "sha",
    "sha256",
    "vram",
    "xet",
    "xml",
    "yaml",
    "yml",
}
NEGATIVE_EXISTENCE_MARKERS = (
    "0 files",
    "0 results",
    "cannot access",
    "does not exist",
    "failed to find",
    "has not been",
    "is not installed",
    "missing",
    "no ",
    "no such",
    "none",
    "not available",
    "not detected",
    "not downloaded",
    "not found",
    "not installed",
    "unavailable",
    "was not",
    "without",
)
NEGATIVE_ROLE_CLASSIFICATION_MARKERS = (
    "not a primary",
    "not a required",
    "not a target",
    "not an expected",
    "not suitable as",
    "not suitable for",
    "not the expected",
    "not the needed",
    "not the primary",
    "not the required",
    "not the target",
    "not usable as",
    "not usable for",
    "only support",
    "support file",
    "support files",
)
EVIDENCE_NEGATIVE_LINE_MARKERS = (
    "0 files",
    "0 results",
    "cannot access",
    "denied",
    "does not exist",
    "error",
    "failed",
    "failure",
    "has not been",
    "missing",
    "no such",
    "not available",
    "not detected",
    "not downloaded",
    "not found",
    "not installed",
    "permission",
    "timeout",
    "unavailable",
    "was not",
)


def _stale_claim_tokens_from_unsupported(tokens: list[str], *, reference_text: str = "") -> list[str]:
    stale_tokens: list[str] = []
    seen: set[str] = set()
    reference_norm = _normalize_claim_text(reference_text)
    for token in tokens:
        cleaned = str(token or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen or key in STALE_CLAIM_TOKEN_IGNORE or key in EVIDENCE_TOKEN_IGNORE:
            continue
        if reference_norm and _normalize_claim_text(cleaned) in reference_norm:
            continue
        if _looks_like_generated_or_file_token(cleaned):
            continue
        if len(cleaned) < 4:
            continue
        distinctive = any(ch.isalpha() for ch in cleaned) and any(ch.isdigit() for ch in cleaned)
        distinctive = distinctive or (cleaned.isupper() and len(cleaned) >= 4)
        if not distinctive:
            continue
        seen.add(key)
        stale_tokens.append(cleaned)
    return stale_tokens


def _looks_like_generated_or_file_token(token: str) -> bool:
    lowered = token.lower()
    if lowered.startswith((
        "art_",
        "step_",
        "step-",
        "shell_",
        "shell-",
        "web_",
        "web-",
        "episode-",
        "fact-",
        "source-",
        "quality-",
        "constraint-",
        "baseline-",
        "question-",
        "verified_",
        "verified-",
        "timeout_",
        "timeout-",
    )):
        return True
    if lowered.endswith((".md", ".py", ".json", ".yaml", ".yml", ".gguf", ".txt", ".log")):
        return True
    if lowered.startswith(("python-", "pip", "pip3")):
        return True
    if "_" in lowered and any(ch.isdigit() for ch in lowered) and any(ch.isalpha() for ch in lowered):
        return True
    return False


def _normalize_claim_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _evidence_grounding_context(
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    tool_name: str,
    args: dict[str, Any],
    window: int = 8,
) -> dict[str, Any] | None:
    if tool_name not in EVIDENCE_GROUNDED_TOOLS:
        return None
    full_proposed_text = _json_text(args)
    proposed_text = _evidence_grounding_proposed_text(tool_name, args)
    if len(full_proposed_text.strip()) < 80:
        return None
    cited_steps = _cited_step_numbers(full_proposed_text)
    evidence_text = _recent_evidence_text(job, recent_steps, window=window, step_numbers=cited_steps or None)
    fresh_evidence_text = _recent_evidence_text(
        job,
        recent_steps,
        window=window,
        step_numbers=cited_steps or None,
        include_durable=False,
        include_job_context=False,
    )
    recent_grounding_paths = _candidate_file_paths_from_recent_grounding_blocks(recent_steps, window=window)
    if len(evidence_text.strip()) < 80 and not recent_grounding_paths:
        return None
    job_reference_text = " ".join(str(job.get(key) or "") for key in ("title", "objective", "kind"))
    proposed_tokens = [
        token
        for token in _concrete_evidence_tokens_for_grounding(tool_name, proposed_text)
        if not _grounding_token_in_reference_text(token, job_reference_text)
    ]
    positive_path_conflicts = _positive_path_claim_conflicts_for_grounding(
        tool_name=tool_name,
        proposed_text=proposed_text,
        full_proposed_text=full_proposed_text,
        fresh_evidence_text=fresh_evidence_text,
    )
    if positive_path_conflicts:
        conflict_paths = [item["path"] for item in positive_path_conflicts]
        return {
            "unsupported_tokens": conflict_paths[:12],
            "negative_path_conflicts": positive_path_conflicts[:6],
            "evidence_steps": [
                step.get("step_no")
                for step in _evidence_steps_for_grounding(recent_steps, window=window, step_numbers=cited_steps or None)
            ],
            "cited_steps": sorted(cited_steps),
            "guidance": (
                "The proposed durable record claims a path or executable is present/available, but recent shell "
                "evidence says that same path is missing or inaccessible. Inspect again, record it as missing, "
                "or cite a newer positive check before saving the claim."
            ),
        }
    negative_conflicts = _negative_claim_conflicts_for_grounding(
        tool_name=tool_name,
        proposed_text=proposed_text,
        fresh_evidence_text=fresh_evidence_text,
        tokens=proposed_tokens,
    )
    if negative_conflicts:
        conflict_tokens = [item["token"] for item in negative_conflicts]
        return {
            "unsupported_tokens": conflict_tokens[:12],
            "negative_claim_conflicts": negative_conflicts[:6],
            "evidence_steps": [
                step.get("step_no")
                for step in _evidence_steps_for_grounding(recent_steps, window=window, step_numbers=cited_steps or None)
            ],
            "cited_steps": sorted(cited_steps),
            "guidance": (
                "The proposed durable record negates a concrete item or file pattern that appears in recent positive evidence. "
                "Inspect the evidence again or record uncertainty instead of saving a conflicting claim."
            ),
        }
    missing_paths = _missing_candidate_paths_for_grounding(
        job=job,
        recent_steps=recent_steps,
        recent_grounding_paths=recent_grounding_paths,
        tool_name=tool_name,
        proposed_text=proposed_text,
        full_proposed_text=full_proposed_text,
        fresh_evidence_text=fresh_evidence_text,
    )
    if missing_paths:
        return {
            "unsupported_tokens": missing_paths[:8],
            "missing_candidate_paths": missing_paths[:8],
            "evidence_steps": [
                step.get("step_no")
                for step in _evidence_steps_for_grounding(recent_steps, window=window, step_numbers=cited_steps or None)
            ],
            "cited_steps": sorted(cited_steps),
            "guidance": (
                "Recent evidence contains concrete file/path candidates, but the durable record only summarized them. "
                "Record the exact observed candidate paths, or explicitly state why those paths are not relevant."
            ),
        }
    stale_tokens = _active_stale_claim_token_set(job)
    proposed_stale_tokens = [
        token
        for token in _concrete_evidence_tokens_for_grounding(tool_name, full_proposed_text)
        if not _grounding_token_in_reference_text(token, job_reference_text)
        if token.lower() in stale_tokens
    ]
    if tool_name == "record_lesson" and not proposed_stale_tokens:
        return None
    unsupported_threshold = 1 if cited_steps or proposed_stale_tokens else 3
    candidate_tokens = proposed_stale_tokens if tool_name == "record_lesson" else proposed_tokens + proposed_stale_tokens
    candidate_high_risk = [token for token in candidate_tokens if _high_risk_evidence_token(token)]
    if len(candidate_tokens) < unsupported_threshold and not candidate_high_risk:
        return None
    evidence_lower = evidence_text.lower()
    fresh_evidence_lower = fresh_evidence_text.lower()
    unsupported = []
    for token in candidate_tokens:
        lowered = token.lower()
        if lowered in fresh_evidence_lower:
            continue
        if lowered in evidence_lower and lowered not in stale_tokens:
            continue
        unsupported.append(token)
    unique = []
    seen = set()
    for token in unsupported:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(token)
    high_risk_unique = [token for token in unique if _high_risk_evidence_token(token)]
    if len(unique) < unsupported_threshold and not high_risk_unique:
        return None
    return {
        "unsupported_tokens": (high_risk_unique or unique)[:12],
        "evidence_steps": [
            step.get("step_no")
            for step in _evidence_steps_for_grounding(recent_steps, window=window, step_numbers=cited_steps or None)
        ],
        "cited_steps": sorted(cited_steps),
        "guidance": (
            "The proposed durable record contains concrete tokens that are not present in recent evidence. "
            "Use exact observed evidence, inspect the source again, or record uncertainty instead of writing unsupported claims."
        ),
    }


def _concrete_evidence_tokens_for_grounding(tool_name: str, text: str) -> list[str]:
    tokens = _concrete_evidence_tokens(text)
    if tool_name not in NARRATIVE_EVIDENCE_GROUNDED_TOOLS:
        return tokens
    return [token for token in tokens if _high_risk_evidence_token(token)]


def _grounding_token_in_reference_text(token: str, reference_text: str) -> bool:
    normalized_token = _normalize_claim_text(token)
    if not normalized_token:
        return False
    return normalized_token in _normalize_claim_text(reference_text)


def _missing_candidate_paths_for_grounding(
    *,
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    recent_grounding_paths: list[str] | None = None,
    tool_name: str,
    proposed_text: str,
    full_proposed_text: str,
    fresh_evidence_text: str,
) -> list[str]:
    if tool_name not in {"record_findings", "record_experiment", "write_artifact", "report_update"}:
        return []
    proposed_lower = f"{proposed_text}\n{full_proposed_text}".lower()
    if not any(term in proposed_lower for term in ("file", "files", "path", "paths", "candidate", "found", "discovered")):
        return []
    positive_evidence_text = "\n".join(
        line
        for line in str(fresh_evidence_text or "").splitlines()
        if not _evidence_line_is_negative(line.lower())
    )
    evidence_paths = [
        *_extract_candidate_file_paths(positive_evidence_text),
        *(recent_grounding_paths or _candidate_file_paths_from_recent_grounding_blocks(recent_steps)),
    ]
    if not evidence_paths:
        return []
    if any(_path_mentioned_in_text(path, proposed_lower) for path in evidence_paths):
        return []
    distinctive_paths: list[str] = []
    seen: set[str] = set()
    for path in _rank_candidate_file_paths(job, full_proposed_text, evidence_paths):
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        distinctive_paths.append(path)
        if len(distinctive_paths) >= 8:
            break
    return distinctive_paths


POSITIVE_PATH_CLAIM_MARKERS = (
    "available",
    "exists",
    "found",
    "is at",
    "located",
    "present",
    "ready",
    "succeed",
    "usable",
    "valid",
    "verified",
)


def _positive_path_claim_conflicts_for_grounding(
    *,
    tool_name: str,
    proposed_text: str,
    full_proposed_text: str,
    fresh_evidence_text: str,
) -> list[dict[str, str]]:
    if tool_name not in {"record_findings", "record_experiment", "record_source", "record_lesson", "write_artifact", "report_update"}:
        return []
    proposed_combined = f"{proposed_text}\n{full_proposed_text}"
    proposed_lower = proposed_combined.lower()
    conflicts: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in str(fresh_evidence_text or "").splitlines():
        line_lower = line.lower()
        if not _evidence_line_is_negative(line_lower):
            continue
        paths = [
            *_extract_candidate_file_paths(line),
            *_extract_candidate_executable_paths(line),
        ]
        for path in paths:
            path = str(path or "").strip()
            if not path:
                continue
            key = path.lower()
            if key in seen:
                continue
            if key not in proposed_lower:
                continue
            if not _path_near_positive_claim(proposed_combined, path):
                continue
            seen.add(key)
            conflicts.append({
                "path": path,
                "evidence": _clip_text(line.strip(), 220),
                "claim": _clip_text(_excerpt_around(proposed_combined, path, window=96), 220),
            })
            if len(conflicts) >= 8:
                return conflicts
    return conflicts


def _path_near_positive_claim(text: str, path: str, *, window: int = 96) -> bool:
    for excerpt in _excerpts_around_all(text, path, window=window):
        excerpt_lower = excerpt.lower()
        if _evidence_line_is_negative(excerpt_lower):
            continue
        if any(marker in excerpt_lower for marker in POSITIVE_PATH_CLAIM_MARKERS):
            return True
    return False


def _excerpt_around(text: str, needle: str, *, window: int = 80) -> str:
    excerpts = _excerpts_around_all(text, needle, window=window, max_matches=1)
    return excerpts[0] if excerpts else ""


def _excerpts_around_all(text: str, needle: str, *, window: int = 80, max_matches: int = 8) -> list[str]:
    source = str(text or "")
    needle_text = str(needle or "")
    if not source or not needle_text:
        return []
    source_lower = source.lower()
    needle_lower = needle_text.lower()
    excerpts: list[str] = []
    index = 0
    while len(excerpts) < max_matches:
        found = source_lower.find(needle_lower, index)
        if found < 0:
            break
        start = max(0, found - window)
        end = min(len(source), found + len(needle_text) + window)
        excerpts.append(source[start:end])
        index = found + max(1, len(needle_text))
    return excerpts


def _path_mentioned_in_text(path: str, text_lower: str) -> bool:
    path_lower = path.lower()
    if path_lower in text_lower:
        return True
    name = Path(path).name.lower()
    return bool(name and name in text_lower)


def _refresh_contradicted_negative_claims(
    db: AgentDB,
    job_id: str,
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
) -> int:
    fresh_evidence_text = _recent_evidence_text(
        job,
        recent_steps,
        window=8,
        include_durable=False,
        include_job_context=False,
    )
    if len(fresh_evidence_text.strip()) < 80:
        return 0
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    existing = metadata.get("stale_negative_records") if isinstance(metadata.get("stale_negative_records"), list) else []
    seen = {
        (
            str(item.get("kind") or ""),
            str(item.get("record_id") or ""),
            str(item.get("token") or "").lower(),
        )
        for item in existing
        if isinstance(item, dict)
    }
    now = datetime.now(timezone.utc).isoformat()
    new_records: list[dict[str, Any]] = []
    for kind, records in (
        ("finding", _metadata_list(job, "finding_ledger")[-80:]),
        ("lesson", _metadata_list(job, "lessons")[-80:]),
        (
            "memory_node",
            (
                metadata.get("memory_graph", {}).get("nodes", [])
                if isinstance(metadata.get("memory_graph"), dict)
                and isinstance(metadata.get("memory_graph", {}).get("nodes"), list)
                else []
            ),
        ),
    ):
        for record in records:
            if not isinstance(record, dict):
                continue
            record_text = _negative_record_text(kind, record)
            if not record_text:
                continue
            conflicts = _negative_claim_conflicts_for_grounding(
                tool_name="record_findings",
                proposed_text=record_text,
                fresh_evidence_text=fresh_evidence_text,
                tokens=_concrete_evidence_tokens(record_text),
            )
            if not conflicts:
                continue
            record_id = _negative_record_id(kind, record)
            for conflict in conflicts[:4]:
                token = str(conflict.get("token") or "")
                key = (kind, record_id, token.lower())
                if key in seen:
                    continue
                seen.add(key)
                new_records.append({
                    "kind": kind,
                    "record_id": record_id,
                    "title": _negative_record_title(kind, record),
                    "token": token,
                    "evidence": conflict.get("evidence") or "",
                    "observed_at": now,
                })
    if not new_records:
        return 0
    db.update_job_metadata(job_id, {"stale_negative_records": (existing + new_records)[-120:]})
    db.append_agent_update(
        job_id,
        f"Suppressed {len(new_records)} contradicted negative durable claim(s) after fresh evidence.",
        category="memory",
        metadata={"stale_negative_records": new_records[:12]},
    )
    return len(new_records)


def _negative_record_text(kind: str, record: dict[str, Any]) -> str:
    if kind == "lesson":
        return str(record.get("lesson") or "")
    if kind == "memory_node":
        return " ".join(
            str(record.get(key) or "")
            for key in ("key", "title", "kind", "status", "summary")
        )
    return " ".join(
        str(record.get(key) or "")
        for key in ("name", "category", "reason", "status", "source_url", "url")
    )


def _negative_record_id(kind: str, record: dict[str, Any]) -> str:
    for key in ("key", "event_id", "id"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return _normalize_claim_text(f"{kind}:{_negative_record_title(kind, record)}")[:120]


def _negative_record_title(kind: str, record: dict[str, Any]) -> str:
    if kind == "lesson":
        return _clip_text(str(record.get("lesson") or "lesson"), 120)
    return str(record.get("name") or record.get("title") or "finding")


def _negative_claim_conflicts_for_grounding(
    *,
    tool_name: str,
    proposed_text: str,
    fresh_evidence_text: str,
    tokens: list[str],
) -> list[dict[str, str]]:
    if tool_name not in EVIDENCE_GROUNDED_TOOLS:
        return []
    proposed_lower = proposed_text.lower()
    if not any(marker in proposed_lower for marker in NEGATIVE_EXISTENCE_MARKERS):
        return []
    evidence_lines = [line.strip() for line in fresh_evidence_text.splitlines() if line.strip()]
    if not evidence_lines:
        return []
    candidates = tokens + _file_pattern_tokens_for_grounding(proposed_text)
    conflicts: list[dict[str, str]] = []
    seen: set[str] = set()
    for token in candidates:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        if not token.startswith(".") and "/" not in token and not _high_risk_evidence_token(token):
            continue
        if not _token_near_negative_claim(proposed_text, token):
            continue
        positive_line = _positive_evidence_line_for_token(evidence_lines, token)
        if not positive_line:
            continue
        conflicts.append({"token": token, "evidence": _clip_text(positive_line, 220)})
    return conflicts


def _file_pattern_tokens_for_grounding(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?<![A-Za-z0-9])(?:\*\.)?\.?([A-Za-z0-9][A-Za-z0-9_-]{1,12})(?![A-Za-z0-9_-])", text or ""):
        raw = match.group(0).strip("'\"`")
        if not raw:
            continue
        if not raw.startswith((".", "*.")):
            continue
        if "." not in raw and not raw.startswith("*."):
            continue
        if raw.startswith(".") and not raw.startswith("*."):
            previous_char = text[match.start() - 1] if match.start() > 0 else ""
            next_char = text[match.end()] if match.end() < len(text) else ""
            if previous_char == "/" or next_char == "/":
                continue
        ext = "." + match.group(1).lower().lstrip(".")
        if ext in {".app", ".co", ".com", ".dev", ".edu", ".gov", ".io", ".net", ".org", ".www", ".http", ".https"}:
            continue
        if ext in seen:
            continue
        seen.add(ext)
        tokens.append(ext)
    return tokens


def _token_near_negative_claim(text: str, token: str, *, window: int = 64) -> bool:
    text_lower = text.lower()
    token_lower = token.lower()
    start = 0
    while True:
        index = text_lower.find(token_lower, start)
        if index < 0:
            return False
        nearby = text_lower[max(0, index - window): index + len(token_lower) + window]
        if any(marker in nearby for marker in NEGATIVE_EXISTENCE_MARKERS):
            if _nearby_negative_is_role_classification(nearby):
                start = index + len(token_lower)
                continue
            if _nearby_negative_is_positive_validation(nearby):
                start = index + len(token_lower)
                continue
            return True
        start = index + len(token_lower)


def _nearby_negative_is_role_classification(text: str) -> bool:
    return any(marker in text for marker in NEGATIVE_ROLE_CLASSIFICATION_MARKERS)


def _nearby_negative_is_positive_validation(text: str) -> bool:
    return bool(re.search(r"\bnot\s+(?:a|an|the)?\s*(?:[\w.-]+\s+){0,5}(?:stub|placeholder|empty file)\b", text))


def _positive_evidence_line_for_token(lines: list[str], token: str) -> str:
    token_lower = token.lower()
    for line in lines:
        line_lower = line.lower()
        if token_lower not in line_lower:
            continue
        if _evidence_line_is_negative(line_lower):
            continue
        return line
    return ""


def _evidence_line_is_negative(line_lower: str) -> bool:
    if any(marker in line_lower for marker in EVIDENCE_NEGATIVE_LINE_MARKERS):
        return True
    return line_lower.startswith("no ") or " no " in line_lower or line_lower.startswith("zero ") or " zero " in line_lower


def _evidence_grounding_proposed_text(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name != "record_memory_graph":
        return _json_value_text(args)
    parts: list[str] = []
    nodes = args.get("nodes") if isinstance(args.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in ("title", "summary", "tags", "metadata"):
            value = node.get(key)
            if value:
                parts.append(_json_text(value))
    edges = args.get("edges") if isinstance(args.get("edges"), list) else []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        for key in ("evidence_refs", "metadata"):
            value = edge.get(key)
            if value:
                parts.append(_json_text(value))
    return "\n".join(parts)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _json_value_text(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(_json_value_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(_json_value_text(item) for item in value)
    return str(value or "")


def _cited_step_numbers(text: str) -> set[int]:
    numbers = set()
    patterns = [
        r"(?i)\bsteps?\s*(?:#|-)?\s*(\d+)\b",
        r"(?i)\bstep[_-](\d+)\b",
        r"(?i)\bshell_exec[_\s-]*step[_\s#-]*(\d+)\b",
        r"(?i)\btool[_\s-]*step[_\s#-]*(\d+)\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            raw = match.group(1)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                numbers.add(value)
    return numbers


def _evidence_steps_for_grounding(
    recent_steps: list[dict[str, Any]],
    *,
    window: int,
    step_numbers: set[int] | None = None,
) -> list[dict[str, Any]]:
    completed = _completed_recent_steps(recent_steps)
    if step_numbers:
        steps = [step for step in completed if int(step.get("step_no") or 0) in step_numbers]
    else:
        steps = completed[-window:]
    return [
        step
        for step in steps
        if step.get("tool_name") in {"browser_snapshot", "shell_exec", "web_extract", "web_search", "read_artifact"}
    ]


def _recent_evidence_text(
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    window: int,
    step_numbers: set[int] | None = None,
    include_durable: bool = True,
    include_job_context: bool = True,
) -> str:
    parts: list[str] = []
    if include_job_context:
        parts.extend([str(job.get("title") or ""), str(job.get("objective") or ""), str(job.get("kind") or "")])
    durable_text = _durable_records_for_grounding(job) if include_durable else ""
    if include_durable and durable_text:
        parts.append(durable_text)
    for step in _evidence_steps_for_grounding(recent_steps, window=window, step_numbers=step_numbers):
        parts.append(str(step.get("summary") or ""))
        input_data = step.get("input") if isinstance(step.get("input"), dict) else {}
        if input_data:
            parts.append(_json_text(input_data))
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        if not output:
            continue
        for key in ("stdout", "stderr", "text", "content", "excerpt", "query", "command"):
            if output.get(key):
                parts.append(str(output.get(key)))
        pages = output.get("pages") if isinstance(output.get("pages"), list) else []
        for page in pages[:6]:
            if isinstance(page, dict):
                parts.append(_json_text({key: page.get(key) for key in ("url", "title", "text", "error", "source_warning")}))
        results = output.get("results") if isinstance(output.get("results"), list) else []
        for item in results[:8]:
            if isinstance(item, dict):
                parts.append(_json_text({key: item.get(key) for key in ("url", "title", "snippet")}))
    return "\n".join(parts)


def _active_stale_claim_token_set(job: dict[str, Any]) -> set[str]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    raw_tokens = metadata.get("unsupported_claim_tokens") if isinstance(metadata.get("unsupported_claim_tokens"), list) else []
    filtered = _stale_claim_tokens_from_unsupported(
        [str(token) for token in raw_tokens],
        reference_text=" ".join(str(job.get(key) or "") for key in ("title", "objective", "kind")),
    )
    return {str(token).strip().lower() for token in filtered if str(token).strip()}


def _durable_records_for_grounding(job: dict[str, Any]) -> str:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    parts: list[str] = []
    for finding in _metadata_list(job, "finding_ledger")[-20:]:
        parts.append(_json_text({
            "finding": finding.get("name") or finding.get("title"),
            "category": finding.get("category"),
            "reason": finding.get("reason") or finding.get("summary"),
            "location": finding.get("location"),
            "status": finding.get("status"),
            "evidence_artifact": finding.get("evidence_artifact"),
            "url": finding.get("url"),
            "metadata": finding.get("metadata") if isinstance(finding.get("metadata"), dict) else {},
        }))
    for experiment in _metadata_list(job, "experiment_ledger")[-12:]:
        parts.append(_json_text({
            "experiment": experiment.get("title") or experiment.get("name"),
            "hypothesis": experiment.get("hypothesis"),
            "status": experiment.get("status"),
            "metric_name": experiment.get("metric_name"),
            "metric_value": experiment.get("metric_value"),
            "metric_unit": experiment.get("metric_unit"),
            "result": experiment.get("result"),
            "next_action": experiment.get("next_action"),
            "config": experiment.get("config") if isinstance(experiment.get("config"), dict) else {},
        }))
    for source in _metadata_list(job, "source_ledger")[-12:]:
        parts.append(_json_text({
            "source": source.get("source") or source.get("url"),
            "source_type": source.get("source_type"),
            "outcome": source.get("outcome"),
            "score": source.get("score"),
        }))
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    if roadmap:
        parts.append(_json_text({
            "roadmap": roadmap.get("title"),
            "objective": roadmap.get("objective"),
            "current_milestone": roadmap.get("current_milestone"),
            "validation_contract": roadmap.get("validation_contract"),
        }))
    graph = metadata.get("memory_graph") if isinstance(metadata.get("memory_graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in [item for item in nodes if isinstance(item, dict)][-20:]:
        parts.append(_json_text({
            "memory_node": node.get("key"),
            "kind": node.get("kind"),
            "title": node.get("title"),
            "summary": node.get("summary"),
        }))
    return "\n".join(parts)


def _concrete_evidence_tokens(text: str) -> list[str]:
    text = text.replace("\\n", "\n").replace("\\t", "\t")
    tokens: list[str] = []
    seen_numeric: set[str] = set()
    for raw in re.findall(
        r"(?i)\b\d+(?:\.\d+)?\s*(?:[KMGTPE]i?B|[KMGTPE]|bytes?|tok/s|t/s|tokens/sec|tokens/s|ms|sec|secs|seconds?|minutes?|hours?|%)\b",
        text,
    ):
        token = re.sub(r"\s+", "", raw.strip())
        key = token.lower()
        if key in seen_numeric:
            continue
        seen_numeric.add(key)
        tokens.append(token)
    for raw in re.findall(r"\b[A-Za-z][A-Za-z0-9_.+-]{1,}\b", text):
        token = raw.strip("._+-")
        if not token:
            continue
        lowered = token.lower()
        if lowered in EVIDENCE_TOKEN_IGNORE:
            continue
        if re.match(r"^[a-z]\d+$", token):
            continue
        if _looks_like_generated_evidence_token(token):
            continue
        if lowered.startswith("art_"):
            continue
        if lowered.startswith("step_"):
            continue
        if lowered.endswith("_output") or lowered.endswith("_stdout") or lowered.endswith("_stderr"):
            continue
        if token.isupper() and len(token) >= 3:
            tokens.append(token)
            continue
        if any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token):
            tokens.append(token)
            continue
        if token[:1].isupper() and token[1:].islower() and len(token) >= 4:
            tokens.append(token)
            continue
    return tokens


def _high_risk_evidence_token(token: str) -> bool:
    lowered = token.lower()
    if not token or lowered in EVIDENCE_TOKEN_IGNORE:
        return False
    if _looks_like_generated_evidence_token(token):
        return False
    if lowered.startswith(("art_", "step_")):
        return False
    if lowered.endswith((".md", ".py", ".json", ".yaml", ".yml", ".gguf", ".txt", ".log")):
        return True
    if any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token):
        return True
    if token.isupper() and len(token) >= 3:
        return True
    return False


def _looks_like_generated_evidence_token(token: str) -> bool:
    lowered = token.lower().strip()
    if re.match(
        r"^(?:art|step|shell|web|episode|fact|source|quality|constraint|baseline|question|verified|timeout)[_-]\d+[a-z]*$",
        lowered,
    ):
        return True
    return bool(re.match(r"^(?:shell|web|browser|tool)[a-z0-9_-]*[_-]step[_-]?\d+[a-z]*$", lowered))


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


def _evidence_checkpoint_accounting_for_prompt(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> str:
    context = _auto_checkpoint_accounting_context(job, recent_steps)
    if not context:
        return "None."
    read_text = "already read" if context.get("checkpoint_read") else "not read yet"
    next_action = (
        "Next use record_findings, record_source, record_experiment, record_tasks, record_roadmap, "
        "record_milestone_validation, or record_lesson to account for it. Do not read the checkpoint again. "
        if context.get("checkpoint_read")
        else "Next either read that checkpoint artifact, or use record_findings, record_source, record_experiment, "
        "record_tasks, record_roadmap, record_milestone_validation, or record_lesson to account for it. "
    )
    return (
        "An auto-saved evidence checkpoint is waiting for durable accounting. "
        f"artifact={context.get('artifact_id') or '?'} title={context.get('title') or ''} "
        f"evidence_step={context.get('evidence_step_no') or context.get('evidence_step') or '?'} "
        f"blocked_tool={context.get('blocked_tool') or ''} status={read_text}. "
        f"{next_action}"
        "Do not continue shell, search, file, artifact, report, or other branch work until this is resolved."
    )


def _pending_evidence_checkpoint(job: dict[str, Any]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    checkpoint = metadata.get("pending_evidence_checkpoint")
    if isinstance(checkpoint, dict) and checkpoint and not checkpoint.get("resolved_at"):
        return checkpoint
    return None


def _step_created_auto_checkpoint(step: dict[str, Any]) -> dict[str, Any] | None:
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    checkpoint = output.get("auto_checkpoint")
    if not isinstance(checkpoint, dict):
        return None
    if not checkpoint.get("artifact_id"):
        return None
    # Only auto-persisted checkpoints have a stored path. Guard-context payloads use a
    # different key so they cannot reset the read/accounting state.
    if not checkpoint.get("path"):
        return None
    return checkpoint


def _auto_checkpoint_accounting_context(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    pending = _pending_evidence_checkpoint(job)
    if pending:
        return {
            "artifact_id": str(pending.get("artifact_id") or ""),
            "title": str(pending.get("title") or ""),
            "checkpoint_step_no": pending.get("checkpoint_step_no"),
            "evidence_step": pending.get("evidence_step"),
            "evidence_step_no": pending.get("evidence_step_no"),
            "blocked_tool": pending.get("blocked_tool"),
            "checkpoint_read": bool(pending.get("read_at")),
            "read_at": pending.get("read_at"),
            "created_at": pending.get("created_at"),
            "source": "job_metadata",
        }
    checkpoint_step = None
    checkpoint = None
    for step in reversed(recent_steps):
        created = _step_created_auto_checkpoint(step)
        if created:
            checkpoint_step = step
            checkpoint = created
            break
    if not checkpoint_step or not checkpoint:
        return None
    checkpoint_step_no = int(checkpoint_step.get("step_no") or 0)
    tail = [step for step in recent_steps if int(step.get("step_no") or 0) > checkpoint_step_no]
    if any(step.get("tool_name") in EVIDENCE_CHECKPOINT_ACCOUNTING_TOOLS for step in tail if step.get("status") == "completed"):
        return None
    artifact_id = str(checkpoint.get("artifact_id") or "")
    artifact_title = str(checkpoint.get("title") or "")
    checkpoint_read = any(
        step.get("tool_name") == "read_artifact"
        and step.get("status") == "completed"
        and _read_artifact_args_match_checkpoint(step, artifact_id=artifact_id, artifact_title=artifact_title)
        for step in tail
    )
    return {
        "artifact_id": artifact_id,
        "title": artifact_title,
        "checkpoint_step_no": checkpoint_step.get("step_no"),
        "evidence_step": checkpoint.get("evidence_step"),
        "blocked_tool": checkpoint.get("blocked_tool"),
        "checkpoint_read": checkpoint_read,
    }


def _evidence_checkpoint_blocks_tool(name: str, args: dict[str, Any], context: dict[str, Any] | None) -> bool:
    if not context:
        return False
    if name in EVIDENCE_CHECKPOINT_RESOLUTION_TOOLS or name == "acknowledge_operator_context":
        return False
    if (
        name == "read_artifact"
        and not context.get("checkpoint_read")
        and _read_artifact_call_matches_checkpoint(
            args,
            artifact_id=str(context.get("artifact_id") or ""),
            artifact_title=str(context.get("title") or ""),
        )
    ):
        return False
    return True


def _evidence_checkpoint_block_guidance(context: dict[str, Any]) -> str:
    tools = (
        "record_findings, record_source, record_experiment, record_tasks, "
        "record_roadmap, record_milestone_validation, or record_lesson"
    )
    if context.get("checkpoint_read"):
        return (
            "The auto-saved evidence checkpoint has already been read. Do not read it again. "
            f"Use {tools} to account for what the checkpoint proved, rejected, changed, or blocked "
            "before more shell, search, file, report, artifact, or branch work."
        )
    return (
        "An auto-saved evidence checkpoint is waiting to be converted into durable progress. "
        f"Read that checkpoint artifact once, or use {tools} to account for it before more shell, "
        "search, file, report, artifact, or other branch work."
    )


def _read_artifact_args_match_checkpoint(step: dict[str, Any], *, artifact_id: str, artifact_title: str) -> bool:
    input_data = step.get("input") if isinstance(step.get("input"), dict) else {}
    args = input_data.get("arguments") if isinstance(input_data.get("arguments"), dict) else {}
    return _read_artifact_call_matches_checkpoint(args, artifact_id=artifact_id, artifact_title=artifact_title)


def _read_artifact_call_matches_checkpoint(args: dict[str, Any], *, artifact_id: str, artifact_title: str) -> bool:
    values = [str(args.get(key) or "").strip() for key in ("artifact_id", "id", "title", "query")]
    values = [value for value in values if value]
    if artifact_id and artifact_id in values:
        return True
    return bool(artifact_title and any(value == artifact_title for value in values))


def _recent_search_streak(recent_steps: list[dict[str, Any]]) -> int:
    return _recent_tool_streak(recent_steps, "web_search")


def _pending_measurement_obligation(job: dict[str, Any]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    obligation = metadata.get("pending_measurement_obligation")
    if isinstance(obligation, dict) and obligation and not obligation.get("resolved_at"):
        return obligation
    return None


CODELIKE_FILE_SUFFIXES = {
    ".bash",
    ".cfg",
    ".cjs",
    ".conf",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".lua",
    ".mjs",
    ".php",
    ".pl",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
    ".zsh",
}


def _pending_file_validation_obligation(job: dict[str, Any]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    obligation = metadata.get("pending_file_validation_obligation")
    if isinstance(obligation, dict) and obligation and not obligation.get("resolved_at"):
        return obligation
    return None


def _file_output_needs_validation(path: str, content: str) -> bool:
    suffix = Path(path).suffix.lower()
    if suffix in CODELIKE_FILE_SUFFIXES:
        return True
    first_line = content.lstrip().splitlines()[0] if content.strip() else ""
    if first_line.startswith("#!"):
        return True
    lowered = Path(path).name.lower()
    return lowered in {"dockerfile", "makefile", "justfile", "procfile"}


def _suggested_file_validation(path: str) -> str:
    suffix = Path(path).suffix.lower()
    quoted = shlex_quote(path)
    if suffix == ".py":
        return f"python3 -m py_compile {quoted}"
    if suffix in {".sh", ".bash", ".zsh"}:
        return f"bash -n {quoted}"
    if suffix == ".json":
        return f"python3 -m json.tool {quoted}"
    if suffix in {".yaml", ".yml"}:
        return f"python3 - <<'PY'\nimport pathlib, yaml\nyaml.safe_load(pathlib.Path({path!r}).read_text())\nPY"
    return f"run the narrowest available syntax check, test, or dry-run for {quoted}"


def shlex_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _clear_invalid_measurement_obligation(db: AgentDB, job_id: str) -> bool:
    job = db.get_job(job_id)
    obligation = _pending_measurement_obligation(job)
    if not obligation:
        return False
    candidates = obligation.get("metric_candidates") if isinstance(obligation.get("metric_candidates"), list) else []
    if not candidates:
        return False
    command = str(obligation.get("command") or "")
    if not measurement_candidates_are_diagnostic_only(candidates, command=command):
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


def _activity_stagnation_context(job: dict[str, Any]) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    streak = _as_int(metadata.get("activity_checkpoint_streak"))
    if streak < ACTIVITY_STAGNATION_CHECKPOINTS:
        return None
    counts = metadata.get("last_checkpoint_counts") if isinstance(metadata.get("last_checkpoint_counts"), dict) else {}
    return {
        "streak": streak,
        "threshold": ACTIVITY_STAGNATION_CHECKPOINTS,
        "counts": {key: _as_int(counts.get(key)) for key in ("findings", "sources", "tasks", "experiments", "lessons", "milestones")},
    }


def _research_balance_context(
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    window: int = 28,
    min_execution_actions: int = 5,
) -> dict[str, Any] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    sources = len(_metadata_list(job, "source_ledger"))
    findings = len(_metadata_list(job, "finding_ledger"))
    experiments = len(_metadata_list(job, "experiment_ledger"))
    if sources > 0 or findings > 0:
        return None
    if metadata.get("pending_measurement_obligation"):
        return None
    completed = [step for step in recent_steps if step.get("status") == "completed"]
    if not completed:
        return None
    tail = completed[-window:]
    execution_tools = {"shell_exec", "write_file", "record_experiment", "write_artifact"}
    research_tools = {
        "web_search",
        "web_extract",
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "record_source",
        "record_findings",
    }
    execution_actions = [step for step in tail if step.get("tool_name") in execution_tools]
    research_actions = [step for step in tail if step.get("tool_name") in research_tools]
    file_actions = [step for step in tail if step.get("tool_name") in {"write_file", "shell_exec"}]
    tasks = _metadata_list(job, "task_queue")
    active_research_tasks = [
        task
        for task in tasks
        if str(task.get("status") or "open") in {"open", "active", "blocked"}
        and str(task.get("output_contract") or "") == "research"
    ]
    has_research_intent = bool(active_research_tasks) or any(
        "research" in str(job.get(key) or "").lower()
        for key in ("title", "objective", "kind")
    )
    if len(execution_actions) < min_execution_actions and not (has_research_intent and len(file_actions) >= 3):
        return None
    if research_actions and not (has_research_intent and len(execution_actions) >= min_execution_actions * 2):
        return None
    if experiments <= 0 and len(execution_actions) < min_execution_actions + 2:
        return None
    return {
        "completed_window": len(tail),
        "execution_actions": len(execution_actions),
        "research_actions": len(research_actions),
        "sources": sources,
        "findings": findings,
        "experiments": experiments,
        "files": len(file_actions),
    }


def _source_yield_context(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    sources = _metadata_list(job, "source_ledger")
    source_count = len(sources)
    if source_count < SOURCE_YIELD_MIN_SOURCES:
        return None
    findings = _metadata_list(job, "finding_ledger")
    yielded_sources = [
        source
        for source in sources
        if _as_int(source.get("yield_count")) > 0
        or _as_float(source.get("usefulness_score")) >= 0.8
    ]
    required_yield = max(2, source_count // 8)
    if len(findings) + len(yielded_sources) >= required_yield:
        return None
    completed = [step for step in recent_steps if step.get("status") == "completed"]
    last_synthesis_no = 0
    for step in completed:
        if step.get("tool_name") in {
            "record_findings",
            "record_source",
            "record_tasks",
            "record_roadmap",
            "record_milestone_validation",
            "record_lesson",
        }:
            last_synthesis_no = max(last_synthesis_no, _as_int(step.get("step_no")))
    gathering_after_synthesis = [
        step
        for step in completed
        if _as_int(step.get("step_no")) > last_synthesis_no
        and step.get("tool_name") in {
            "web_search",
            "web_extract",
            "browser_navigate",
            "browser_snapshot",
            "browser_click",
            "browser_scroll",
        }
    ]
    recent_gathering = gathering_after_synthesis[-24:]
    if len(recent_gathering) < SOURCE_YIELD_MIN_RECENT_GATHERING:
        return None
    recent_source_titles = [
        str(source.get("source") or source.get("title") or "").strip()
        for source in sources[-8:]
        if str(source.get("source") or source.get("title") or "").strip()
    ]
    return {
        "sources": source_count,
        "findings": len(findings),
        "yielded_sources": len(yielded_sources),
        "required_yield": required_yield,
        "recent_gathering": len(recent_gathering),
        "since_step": recent_gathering[0].get("step_no") if recent_gathering else None,
        "recent_source_titles": recent_source_titles,
    }


def _artifact_accounting_context(
    recent_steps: list[dict[str, Any]],
    *,
    threshold: int = 3,
    window: int = 12,
) -> dict[str, Any] | None:
    completed = [step for step in recent_steps if step.get("status") == "completed"]
    tail: list[dict[str, Any]] = []
    for step in reversed(completed[-window:]):
        if step.get("tool_name") in LEDGER_PROGRESS_TOOLS:
            break
        tail.append(step)
    tail.reverse()
    artifact_steps = [step for step in tail if step.get("tool_name") == "write_artifact"]
    if len(artifact_steps) < threshold:
        return None
    titles = []
    for step in artifact_steps[-5:]:
        input_data = step.get("input") if isinstance(step.get("input"), dict) else {}
        args = input_data.get("arguments") if isinstance(input_data.get("arguments"), dict) else {}
        title = str(args.get("title") or step.get("summary") or f"step #{step.get('step_no')}")
        titles.append(_clip_text(title, 120))
    return {
        "artifact_count": len(artifact_steps),
        "since_step": tail[0].get("step_no") if tail else None,
        "artifact_steps": [step.get("step_no") for step in artifact_steps],
        "artifact_titles": titles,
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


def _job_requires_deliverable_progress(job: dict[str, Any]) -> bool:
    tasks = _metadata_list(job, "task_queue")
    report_tasks: list[dict[str, Any]] = []
    competing_execution_tasks: list[dict[str, Any]] = []
    for task in tasks:
        status = str(task.get("status") or "open").strip().lower()
        if status in {"done", "skipped"}:
            continue
        contract = str(task.get("output_contract") or "").strip().lower()
        if contract == "report":
            report_tasks.append(task)
        elif contract in {"action", "experiment", "monitor"}:
            competing_execution_tasks.append(task)
    if report_tasks:
        active_report = any(str(task.get("status") or "open").strip().lower() == "active" for task in report_tasks)
        active_competing = any(
            str(task.get("status") or "open").strip().lower() == "active"
            for task in competing_execution_tasks
        )
        max_report_priority = max(_as_int(task.get("priority")) for task in report_tasks)
        higher_priority_competing = any(
            _as_int(task.get("priority")) >= max_report_priority
            for task in competing_execution_tasks
        )
        if active_report or (not active_competing and not higher_priority_competing):
            return True
    text = " ".join(str(job.get(key) or "") for key in ("title", "objective", "kind")).lower()
    tokens = set(re.findall(r"[a-z][a-z0-9_-]+", text))
    objective_terms = DELIVERABLE_ARTIFACT_TERMS - {"compiled", "final", "revision", "section", "updated"}
    return bool(tokens & objective_terms)


def _step_is_deliverable_checkpoint(step: dict[str, Any]) -> bool:
    tool = step.get("tool_name")
    if tool == "write_file":
        return True
    if tool != "write_artifact":
        return False
    input_data = step.get("input") if isinstance(step.get("input"), dict) else {}
    args = input_data.get("arguments") if isinstance(input_data.get("arguments"), dict) else {}
    text = " ".join(
        str(value or "")
        for value in (
            args.get("title"),
            args.get("summary"),
            args.get("artifact_type"),
            step.get("summary"),
        )
    ).lower()
    tokens = set(re.findall(r"[a-z][a-z0-9_-]+", text))
    if tokens & EVIDENCE_ARTIFACT_TERMS:
        return False
    return bool(tokens & DELIVERABLE_ARTIFACT_TERMS)


def _deliverable_progress_guard_context(
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    budget: int = DELIVERABLE_RESEARCH_BUDGET_STEPS,
) -> dict[str, Any] | None:
    if not _job_requires_deliverable_progress(job):
        return None
    completed = [step for step in recent_steps if step.get("status") == "completed"]
    if not completed:
        return None
    last_checkpoint_index = -1
    for index, step in enumerate(completed):
        if _step_is_deliverable_checkpoint(step):
            last_checkpoint_index = index
    tail = completed[last_checkpoint_index + 1 :]
    branch_activity = [
        step
        for step in tail
        if step.get("tool_name") in BRANCH_WORK_TOOLS
        or (
            step.get("tool_name") == "shell_exec"
            and _shell_command_looks_read_only(_step_command(step))
        )
    ]
    if len(branch_activity) < budget:
        return None
    deliverable_accounting_tools = {"record_tasks", "record_roadmap", "record_milestone_validation", "record_lesson"}
    if any(step.get("tool_name") in deliverable_accounting_tools for step in tail[-6:]):
        return None
    return {
        "reason": "no deliverable checkpoint yet" if last_checkpoint_index < 0 else "no recent deliverable checkpoint",
        "research_budget": budget,
        "completed_since_last_deliverable": len(tail),
        "branch_activity": len(branch_activity),
        "since_step": branch_activity[0].get("step_no") if branch_activity else None,
        "tools": [step.get("tool_name") or step.get("kind") for step in branch_activity[-10:]],
    }


def _step_command(step: dict[str, Any]) -> str:
    input_data = step.get("input") if isinstance(step.get("input"), dict) else {}
    args = input_data.get("arguments") if isinstance(input_data.get("arguments"), dict) else {}
    return str(args.get("command") or "")


def _read_only_shell_churn_context(recent_steps: list[dict[str, Any]], *, window: int = 10, threshold: int = 3) -> dict[str, Any] | None:
    completed = [step for step in recent_steps if step.get("status") == "completed"]
    if not completed:
        return None
    tail = completed[-window:]
    read_only_shell = [
        step
        for step in tail
        if step.get("tool_name") == "shell_exec" and _shell_command_looks_read_only(_step_command(step))
    ]
    if len(read_only_shell) < threshold:
        return None
    action_steps = [
        step
        for step in tail
        if step.get("tool_name") in {"write_file", "write_artifact", "defer_job"}
        or step.get("tool_name") in {
            "record_experiment",
            "record_findings",
            "record_lesson",
            "record_milestone_validation",
            "record_roadmap",
            "record_source",
            "record_tasks",
            "report_update",
        }
        or (step.get("tool_name") == "shell_exec" and not _shell_command_looks_read_only(_step_command(step)))
    ]
    if action_steps:
        return None
    return {
        "read_only_shell_count": len(read_only_shell),
        "threshold": threshold,
        "window": len(tail),
        "since_step": read_only_shell[0].get("step_no"),
        "commands": [_clip_text(_step_command(step), 140) for step in read_only_shell[-5:]],
    }


def _experiment_metric_group_key(experiment: dict[str, Any]) -> tuple[str, str, bool] | None:
    metric_name = str(experiment.get("metric_name") or "").strip().lower()
    if not metric_name:
        return None
    if experiment.get("metric_value") is None:
        return None
    return (
        metric_name,
        str(experiment.get("metric_unit") or "").strip().lower(),
        bool(experiment.get("higher_is_better", True)),
    )


def _experiment_metric_number(experiment: dict[str, Any]) -> float | None:
    try:
        return float(experiment.get("metric_value"))
    except (TypeError, ValueError):
        return None


def _experiment_value_improves(*, value: float, best_value: float, higher_is_better: bool) -> bool:
    return value > best_value if higher_is_better else value < best_value


def _experiment_stagnation_context(job: dict[str, Any], recent_steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not _job_requires_measured_progress(job):
        return None
    experiments = [
        experiment
        for experiment in _metadata_list(job, "experiment_ledger")
        if str(experiment.get("status") or "").lower() == "measured"
        and _experiment_metric_group_key(experiment) is not None
    ]
    if len(experiments) < EXPERIMENT_STAGNATION_MIN_TRIALS:
        return None
    latest = experiments[-1]
    key = _experiment_metric_group_key(latest)
    if key is None:
        return None
    group = [experiment for experiment in experiments if _experiment_metric_group_key(experiment) == key]
    if len(group) < EXPERIMENT_STAGNATION_MIN_TRIALS:
        return None
    higher_is_better = bool(latest.get("higher_is_better", True))
    best_index = 0
    best_value = _experiment_metric_number(group[0])
    for index, experiment in enumerate(group[1:], start=1):
        value = _experiment_metric_number(experiment)
        if value is None:
            continue
        if best_value is None or _experiment_value_improves(
            value=value,
            best_value=best_value,
            higher_is_better=higher_is_better,
        ):
            best_index = index
            best_value = value
    if best_value is None:
        return None
    non_improving = group[best_index + 1:]
    if len(non_improving) < EXPERIMENT_STAGNATION_NON_IMPROVING:
        return None
    last_experiment_step_no = 0
    for step in recent_steps:
        if step.get("tool_name") == "record_experiment" and str(step.get("status") or "").lower() == "completed":
            last_experiment_step_no = max(last_experiment_step_no, _as_int(step.get("step_no")))
    if last_experiment_step_no > 0:
        decision_tools = {"record_lesson", "record_tasks", "record_roadmap", "record_milestone_validation"}
        if any(
            _as_int(step.get("step_no")) > last_experiment_step_no
            and str(step.get("status") or "").lower() == "completed"
            and step.get("tool_name") in decision_tools
            for step in recent_steps
        ):
            return None
    best = group[best_index]
    return {
        "metric_name": latest.get("metric_name"),
        "metric_unit": latest.get("metric_unit"),
        "higher_is_better": higher_is_better,
        "best_title": best.get("title"),
        "best_value": best.get("metric_value"),
        "latest_title": latest.get("title"),
        "latest_value": latest.get("metric_value"),
        "non_improving_count": len(non_improving),
        "recent_trials": len(group),
        "recent_titles": [str(experiment.get("title") or "") for experiment in non_improving[-5:]],
    }


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
    if any(_step_accounts_for_measured_progress_guard(step) for step in tail[-6:]):
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


def _step_accounts_for_measured_progress_guard(step: dict[str, Any]) -> bool:
    tool_name = step.get("tool_name")
    if tool_name == "record_lesson":
        return True
    if tool_name != "record_tasks":
        return False
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    tasks = output.get("tasks") if isinstance(output.get("tasks"), list) else []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "open").strip().lower().replace(" ", "_")
        if status in {"done", "skipped"}:
            continue
        contract = str(task.get("output_contract") or "").strip().lower().replace(" ", "_")
        if contract in {"experiment", "monitor"}:
            return True
        if contract == "action" and _task_text_requires_measurement(task):
            return True
    return False


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
    candidates = measurement_candidates(result, command=command)
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


def _maybe_create_file_validation_obligation(
    *,
    db: AgentDB,
    job_id: str,
    step: dict[str, Any] | None,
    args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    path = str(result.get("path") or args.get("path") or "").strip()
    content = str(args.get("content") or "")
    if not path or not _file_output_needs_validation(path, content):
        return
    metadata = db.get_job(job_id).get("metadata")
    if isinstance(metadata, dict):
        existing = metadata.get("pending_file_validation_obligation")
        if isinstance(existing, dict) and existing and not existing.get("resolved_at"):
            return
    obligation = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_step_id": step.get("id") if step else "",
        "source_step_no": step.get("step_no") if step else None,
        "tool": "write_file",
        "path": path,
        "reason": "code/config/script-like file was written and needs validation before more branch work",
        "suggested_validation": _suggested_file_validation(path),
    }
    db.update_job_metadata(job_id, {"pending_file_validation_obligation": obligation})
    db.append_agent_update(
        job_id,
        f"File output needs validation: {path}",
        category="blocked",
        metadata={"pending_file_validation_obligation": obligation},
    )


def _command_references_path(command: str, path: str) -> bool:
    if not command or not path:
        return False
    path_obj = Path(path)
    needles = {str(path_obj), path_obj.name}
    try:
        needles.add(str(path_obj.expanduser().resolve()))
    except OSError:
        pass
    return any(needle and needle in command for needle in needles)


def _resolve_file_validation_obligation(
    db: AgentDB,
    job_id: str,
    *,
    status: str,
    reason: str,
    via_tool: str,
    result: dict[str, Any] | None = None,
) -> None:
    job = db.get_job(job_id)
    obligation = _pending_file_validation_obligation(job)
    if not obligation:
        return
    resolved = dict(obligation)
    resolved.update({
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "resolution_status": status,
        "resolution_reason": reason[:1000],
        "resolution_tool": via_tool,
    })
    if result:
        resolved["validation_result"] = {
            key: result.get(key)
            for key in ("success", "returncode", "error", "summary")
            if key in result
        }
    db.update_job_metadata(
        job_id,
        {
            "pending_file_validation_obligation": {},
            "last_file_validation_obligation": resolved,
        },
    )
    db.append_agent_update(
        job_id,
        f"File validation {status}: {reason[:220]}",
        category="progress" if status == "validated" else "blocked",
        metadata={"file_validation_obligation": resolved},
    )


def _maybe_resolve_file_validation_obligation(
    *,
    db: AgentDB,
    job_id: str,
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
    ok: bool,
) -> None:
    obligation = _pending_file_validation_obligation(db.get_job(job_id))
    if not obligation:
        return
    if tool_name == "shell_exec":
        command = str(args.get("command") or result.get("command") or "")
        path = str(obligation.get("path") or "")
        if not _command_references_path(command, path):
            return
        status = "validated" if ok else "failed"
        reason = "Validation command completed." if ok else f"Validation command failed: {result.get('error') or 'non-zero result'}"
        _resolve_file_validation_obligation(db, job_id, status=status, reason=reason, via_tool=tool_name, result=result)
        return
    if ok and tool_name in {"record_lesson", "record_tasks", "record_experiment", "record_milestone_validation"}:
        _resolve_file_validation_obligation(
            db,
            job_id,
            status="deferred",
            reason=f"Validation was handled or deferred via {tool_name}.",
            via_tool=tool_name,
            result=result,
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


def _text_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) > 2 and token not in TEXT_TOKEN_STOPWORDS
    }


def _similar_recent_search(
    args: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    *,
    window: int = 12,
) -> dict[str, Any] | None:
    return _similar_recent_query_tool("web_search", args, recent_steps, window=window)


def _similar_recent_query_tool(
    tool_name: str,
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
        if step.get("tool_name") != tool_name:
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


def _recent_tool_streak(recent_steps: list[dict[str, Any]], tool_name: str) -> int:
    streak = 0
    for step in reversed(_completed_recent_steps(recent_steps)):
        current_tool = step.get("tool_name")
        if current_tool == tool_name:
            streak += 1
            continue
        if current_tool:
            break
    return streak


def _repeated_guard_block_context(
    recent_steps: list[dict[str, Any]],
    *,
    threshold: int = 3,
    window: int = 12,
) -> dict[str, Any] | None:
    recoveries = [
        step
        for step in recent_steps
        if step.get("tool_name") == "guard_recovery" and step.get("status") == "completed"
    ]
    last_recovery = max(
        recoveries,
        key=lambda step: int(step.get("step_no") or 0),
        default=None,
    )
    last_recovery_no = int(last_recovery.get("step_no") or 0) if last_recovery else 0
    last_recovery_error = ""
    if last_recovery:
        recovery_output = last_recovery.get("output") if isinstance(last_recovery.get("output"), dict) else {}
        recovery_context = recovery_output.get("guard_recovery") if isinstance(recovery_output.get("guard_recovery"), dict) else {}
        last_recovery_error = str(recovery_context.get("error") or "")
    operational_steps = [
        step
        for step in recent_steps
        if int(step.get("step_no") or 0) > last_recovery_no
        if step.get("kind") in {"tool", "recovery", "assistant"} and step.get("tool_name") != "guard_recovery"
    ]
    tail = operational_steps[-window:]
    latest_blocked = next((step for step in reversed(tail) if step.get("status") == "blocked"), None)
    if not latest_blocked:
        return None
    output = latest_blocked.get("output") if isinstance(latest_blocked.get("output"), dict) else {}
    error = str(output.get("error") or latest_blocked.get("error") or "")
    if error not in RECOVERABLE_GUARD_ERRORS:
        return None
    count = 0
    blocked_tools = []
    first_step_no = None
    for step in tail:
        step_output = step.get("output") if isinstance(step.get("output"), dict) else {}
        step_error = str(step_output.get("error") or step.get("error") or "")
        if step.get("status") == "blocked" and step_error == error:
            count += 1
            first_step_no = first_step_no or step.get("step_no")
            blocked_tools.append(str(step.get("tool_name") or step.get("kind") or "tool"))
    effective_threshold = 1 if _already_read_checkpoint_accounting_block(latest_blocked) else threshold
    if count < effective_threshold:
        return None
    progress_after_recovery = any(
        step.get("status") == "completed"
        and step.get("tool_name") != "guard_recovery"
        for step in operational_steps
    )
    if last_recovery_error == error and not progress_after_recovery:
        return None
    context = {
        "error": error,
        "count": count,
        "first_step_no": first_step_no,
        "latest_step_no": latest_blocked.get("step_no"),
        "blocked_tools": blocked_tools[-8:],
    }
    if error == "task queue saturated":
        task_queue = output.get("task_queue") if isinstance(output.get("task_queue"), dict) else {}
        context["task_queue"] = {
            "reason": task_queue.get("reason") or "task queue saturated",
            "open_count": task_queue.get("open_count"),
            "total_count": task_queue.get("total_count"),
            "open_titles": task_queue.get("open_titles") if isinstance(task_queue.get("open_titles"), list) else [],
        }
    return context


def _already_read_checkpoint_accounting_block(step: dict[str, Any]) -> bool:
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    checkpoint = output.get("pending_evidence_checkpoint") if isinstance(output.get("pending_evidence_checkpoint"), dict) else {}
    return (
        output.get("error") == "evidence checkpoint accounting required"
        and (bool(output.get("checkpoint_already_read")) or bool(checkpoint.get("checkpoint_read")))
    )


def _step_error_text(step: dict[str, Any]) -> str:
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    parts = [
        output.get("error"),
        output.get("error_type"),
        output.get("detail"),
        output.get("message"),
        step.get("error"),
        step.get("summary"),
    ]
    return " ".join(str(part) for part in parts if part)


def _blocked_tool_call_result(
    name: str,
    args: dict[str, Any],
    recent_steps: list[dict[str, Any]],
    job: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    if name == "defer_job":
        self_defer = _self_defer_context(args)
        if self_defer:
            result = {
                "success": False,
                "error": "self-defer blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "self_defer": self_defer,
                "guidance": (
                    "Do not defer merely for a future worker turn to pick up ordinary work. Use defer_job only when "
                    "waiting for a real external process, scheduled monitor interval, long-running command, "
                    "or other time-based condition. Otherwise execute, measure, record a task/experiment/lesson, or "
                    "mark the branch blocked now."
                ),
            }
            return result, "blocked defer_job; self-defer is not progress"

    if name == "record_tasks":
        saturated = _task_queue_saturation_context(job, args)
        if saturated:
            result = {
                "success": False,
                "error": "task queue saturated",
                "blocked_tool": name,
                "blocked_arguments": args,
                "task_queue": saturated,
                "guidance": (
                    "The durable task queue already has many branches. Do not create more branch sprawl. "
                    "Choose an existing high-priority task and execute it, update existing tasks to active, "
                    "done, blocked, or skipped, or consolidate the queue into roadmap/milestone state."
                ),
            }
            return result, f"blocked record_tasks; {saturated['reason']}"
        task_planning_stagnation = _task_planning_stagnation_context(job)
        if task_planning_stagnation and _record_tasks_adds_new_open_work(args, job):
            result = {
                "success": False,
                "error": "task execution required",
                "blocked_tool": name,
                "blocked_arguments": args,
                "task_planning": task_planning_stagnation,
                "guidance": (
                    "Recent checkpoints only expanded the task queue. Do not add more new open tasks yet. "
                    "Execute or validate an existing branch, save a durable checkpoint, record findings/source/"
                    "experiment evidence, mark existing tasks done/blocked/skipped, or record a lesson."
                ),
            }
            return result, "blocked record_tasks; task-only planning needs execution"

    current_milestone_validation = _milestone_validation_needed(job)
    if (
        name == "record_milestone_validation"
        and current_milestone_validation
        and not _milestone_validation_call_matches_current(args, current_milestone_validation)
    ):
        result = {
            "success": False,
            "error": "current milestone validation required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "milestone": {
                "title": current_milestone_validation.get("title"),
                "status": current_milestone_validation.get("status"),
                "validation_status": current_milestone_validation.get("validation_status"),
                "acceptance_criteria": current_milestone_validation.get("acceptance_criteria"),
                "evidence_needed": current_milestone_validation.get("evidence_needed"),
            },
            "guidance": (
                "A milestone validation gate is already active. Validate that current milestone by name, "
                "or update the roadmap to make a different milestone current before validating another one."
            ),
        }
        return result, "blocked record_milestone_validation; current milestone validation required"

    auto_checkpoint_accounting = _auto_checkpoint_accounting_context(job, recent_steps)
    checkpoint_read_call = bool(
        auto_checkpoint_accounting
        and name == "read_artifact"
        and not auto_checkpoint_accounting.get("checkpoint_read")
        and _read_artifact_call_matches_checkpoint(
            args,
            artifact_id=str(auto_checkpoint_accounting.get("artifact_id") or ""),
            artifact_title=str(auto_checkpoint_accounting.get("title") or ""),
        )
    )
    if _evidence_checkpoint_blocks_tool(name, args, auto_checkpoint_accounting):
        checkpoint_already_read = bool(auto_checkpoint_accounting and auto_checkpoint_accounting.get("checkpoint_read"))
        result = {
            "success": False,
            "error": "evidence checkpoint accounting required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "pending_evidence_checkpoint": auto_checkpoint_accounting,
            "checkpoint_already_read": checkpoint_already_read,
            "required_next_action": "durable_checkpoint_accounting" if checkpoint_already_read else "read_or_account_checkpoint",
            "allowed_resolution_tools": sorted(EVIDENCE_CHECKPOINT_RESOLUTION_TOOLS),
            "guidance": _evidence_checkpoint_block_guidance(auto_checkpoint_accounting or {}),
        }
        return result, f"blocked {name}; evidence checkpoint accounting required"
    checkpoint_resolution_call = bool(auto_checkpoint_accounting and name in EVIDENCE_CHECKPOINT_RESOLUTION_TOOLS)

    if name == "shell_exec":
        placeholder = _shell_placeholder_context(str(args.get("command") or ""))
        if placeholder:
            result = {
                "success": False,
                "error": "unresolved placeholder in shell command",
                "blocked_tool": name,
                "blocked_arguments": args,
                "placeholder": placeholder,
                "guidance": (
                    "Do not execute shell commands that still contain placeholder URLs, paths, hosts, or template "
                    "tokens. Resolve the concrete value from evidence, ask the operator if it is genuinely unknown, "
                    "or record a blocked task/source before continuing."
                ),
            }
            return result, "blocked shell_exec; unresolved placeholder in command"
        candidate_recovery = _observed_candidate_recovery_required_context(recent_steps, args)
        if candidate_recovery:
            result = {
                "success": False,
                "error": "observed executable recovery required",
                "blocked_tool": name,
                "blocked_arguments": args,
                "candidate_recovery": candidate_recovery,
                "guidance": (
                    "A recent shell step reported this command as missing, and later evidence showed candidate "
                    "executable paths. Retry with an exact observed executable path, add its directory to PATH, "
                    "or record why that observed candidate is invalid before running the bare command again."
                ),
            }
            return result, "blocked shell_exec; observed executable recovery required"
        privileged_failure = _recent_privileged_shell_failure_context(recent_steps)
        if privileged_failure and _shell_command_looks_privileged_or_package_manager(str(args.get("command") or "")):
            result = {
                "success": False,
                "error": "privileged command recovery required",
                "blocked_tool": name,
                "blocked_arguments": args,
                "privileged_failure": privileged_failure,
                "guidance": (
                    "A recent privileged/package-manager shell command failed due permission or authorization. "
                    "Do not retry that class of command until the failure is accounted for. Use observed executable "
                    "paths, user-writable installs, existing project files, or record_tasks/record_lesson/"
                    "record_experiment to mark the branch blocked or choose a non-privileged recovery."
                ),
            }
            return result, "blocked shell_exec; privileged command recovery required"

    unpersisted_evidence = _unpersisted_evidence_step(recent_steps)
    if unpersisted_evidence and name in BRANCH_WORK_TOOLS:
        result = {
            "success": False,
            "error": "artifact required before more research",
            "blocked_tool": name,
            "blocked_arguments": args,
            "previous_step": unpersisted_evidence["id"],
            "guidance": (
                "Fresh browser, extracted, or shell evidence is waiting. Save or account for that evidence with "
                "write_artifact, record_findings, record_source, record_experiment, record_tasks, "
                "record_roadmap, record_milestone_validation, or record_lesson before doing more search, "
                "browsing, shell work, or artifact review."
            ),
        }
        return result, f"blocked {name}; write_artifact required after evidence step #{unpersisted_evidence['step_no']}"

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

    if checkpoint_read_call:
        return None

    browser_runtime_unavailable = _browser_runtime_unavailable_context(recent_steps)
    if browser_runtime_unavailable and _is_browser_tool(name):
        result = {
            "success": False,
            "error": "browser runtime unavailable",
            "blocked_tool": name,
            "blocked_arguments": args,
            "browser_runtime": browser_runtime_unavailable,
            "guidance": (
                "Browser automation is unavailable on this host. Do not retry browser tools until the runtime is "
                "installed or configured. Use web_search, web_extract, shell_exec, source/ledger tools, or record "
                "a blocked task/source and continue through a non-browser branch."
            ),
        }
        return result, f"blocked {name}; browser runtime unavailable"

    measurement_obligation = _pending_measurement_obligation(job)
    if (
        measurement_obligation
        and not checkpoint_resolution_call
        and name in MEASUREMENT_BLOCKED_TOOLS
        and name not in MEASUREMENT_RESOLUTION_TOOLS
    ):
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

    file_validation_obligation = _pending_file_validation_obligation(job)
    if (
        file_validation_obligation
        and not checkpoint_resolution_call
        and name in FILE_VALIDATION_BLOCKED_TOOLS
        and name not in FILE_VALIDATION_RESOLUTION_TOOLS
    ):
        result = {
            "success": False,
            "error": "file validation pending",
            "blocked_tool": name,
            "blocked_arguments": args,
            "pending_file_validation_obligation": file_validation_obligation,
            "guidance": (
                "A recent file output needs validation before more research/output churn. "
                "Use shell_exec to run a syntax check, dry-run, test, or other narrow validation for the file, "
                "or use record_tasks/record_lesson/record_experiment if validation is blocked or deferred."
            ),
        }
        return result, f"blocked {name}; file validation required after write_file"

    early_anti_bot_context = _recent_anti_bot_context(recent_steps)
    if early_anti_bot_context and name == "write_artifact" and not _artifact_args_acknowledge_block(args):
        result = {
            "success": False,
            "error": "misleading blocked-source artifact blocked",
            "blocked_tool": name,
            "blocked_arguments": args,
            "anti_bot_source": early_anti_bot_context,
            "guidance": "The latest browser evidence is an anti-bot/CAPTCHA block. Write only a blocked-source note or pivot.",
        }
        return result, f"blocked misleading write_artifact; anti-bot source at step #{early_anti_bot_context.get('step_no')}"

    evidence_grounding = _evidence_grounding_context(job, recent_steps, tool_name=name, args=args)
    if evidence_grounding:
        result = {
            "success": False,
            "error": "evidence grounding required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "evidence_grounding": evidence_grounding,
            "guidance": evidence_grounding["guidance"],
        }
        return result, f"blocked {name}; evidence grounding required"

    measured_progress_guard = _measured_progress_guard_context(job, recent_steps)
    experiment_stagnation = _experiment_stagnation_context(job, recent_steps)
    deliverable_progress_guard = _deliverable_progress_guard_context(job, recent_steps)
    source_yield = _source_yield_context(job, recent_steps)
    progress_churn = _progress_churn_context(recent_steps)
    artifact_accounting = _artifact_accounting_context(recent_steps)
    activity_stagnation = _activity_stagnation_context(job)
    memory_consolidation = _memory_graph_consolidation_context(job, recent_steps)
    shell_read_only = name == "shell_exec" and _shell_command_looks_read_only(str(args.get("command") or ""))
    if (
        artifact_accounting
        and name in ARTIFACT_ACCOUNTING_BLOCKED_TOOLS
        and name not in ARTIFACT_ACCOUNTING_RESOLUTION_TOOLS
    ):
        result = {
            "success": False,
            "error": "progress accounting required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "artifact_accounting": artifact_accounting,
            "guidance": (
                "Recent saved outputs have not been reflected in durable progress state. "
                "Use record_tasks or record_roadmap to mark completed/open branches, "
                "record_milestone_validation for milestone checks, record_findings or record_source "
                "for reusable evidence, record_experiment for measurements, or record_lesson "
                "if the outputs were low-value before continuing."
            ),
        }
        return result, f"blocked {name}; progress accounting required after saved outputs"

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

    read_only_shell_churn = _read_only_shell_churn_context(recent_steps)
    if read_only_shell_churn and shell_read_only:
        result = {
            "success": False,
            "error": "action decision required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "read_only_shell_churn": read_only_shell_churn,
            "guidance": (
                "Recent shell work only inspected or listed state. Stop re-probing the same branch. "
                "Run the next concrete action, write/persist the candidate decision, record an experiment/monitor task, "
                "or record why the branch is blocked before another read-only shell command."
            ),
        }
        return result, f"blocked {name}; action decision required"

    if activity_stagnation and name in ACTIVITY_STAGNATION_BLOCKED_TOOLS:
        result = {
            "success": False,
            "error": "durable progress required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "activity_stagnation": activity_stagnation,
            "guidance": (
                "Several checkpoints have produced no durable ledger delta. "
                "Use record_findings, record_source, record_experiment, record_tasks, record_roadmap, "
                "record_milestone_validation, or record_lesson to classify the branch, mark it blocked/skipped, "
                "or open a better branch before more research, shell, file, report, or artifact work."
            ),
        }
        return result, f"blocked {name}; durable progress required after activity-only checkpoints"

    if source_yield and name in SOURCE_YIELD_BLOCKED_TOOLS:
        result = {
            "success": False,
            "error": "source yield accounting required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "source_yield": source_yield,
            "guidance": (
                "The job has gathered enough sources without enough durable findings or yielded source outcomes. "
                "Before more search, extraction, browsing, shell execution, file/output work, or report chatter, "
                "use record_findings to save source-backed facts/candidates, record_source to mark source yield "
                "or low-yield outcomes, or update tasks/roadmap/lessons to pivot from the source branch."
            ),
        }
        return result, f"blocked {name}; source yield accounting required"

    if memory_consolidation and name in MEMORY_CONSOLIDATION_BLOCKED_TOOLS:
        result = {
            "success": False,
            "error": "memory graph consolidation required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "memory_consolidation": memory_consolidation,
            "guidance": (
                "The job has enough reusable durable records that raw ledgers should be consolidated into connected "
                "memory. Use record_memory_graph to add/update nodes and links before more branch work, or record_lesson "
                "if there is no reusable memory to preserve."
            ),
        }
        return result, f"blocked {name}; memory graph consolidation required"

    record_experiment_closes_branch = (
        name == "record_experiment"
        and str(args.get("status") or "").strip().lower().replace(" ", "_") in {"failed", "blocked", "skipped"}
    )
    if (
        experiment_stagnation
        and not record_experiment_closes_branch
        and (
            name in BRANCH_WORK_TOOLS
            or name in {"record_experiment", "write_artifact", "write_file", "report_update"}
        )
    ):
        result = {
            "success": False,
            "error": "experiment stagnation decision required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "experiment_stagnation": experiment_stagnation,
            "guidance": (
                "Recent measured trials have not improved the best observed result. Before more experiments, "
                "execution, research, file/output work, or report chatter, make a durable decision: use "
                "record_tasks, record_roadmap, record_milestone_validation, record_lesson, or a blocked/skipped/"
                "failed record_experiment to reject, block, or pivot the stagnant branch."
            ),
        }
        return result, f"blocked {name}; experiment stagnation decision required"

    lesson_sprawl = _lesson_sprawl_context(job, recent_steps)
    if lesson_sprawl and name == "record_lesson":
        result = {
            "success": False,
            "error": "lesson consolidation required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "lesson_consolidation": lesson_sprawl,
            "guidance": (
                "This job already has many raw lessons and the connected memory graph is behind. "
                "Do not add another raw lesson. Use record_memory_graph to consolidate reusable strategy, mistake, "
                "constraint, decision, question, skill, or episode nodes with evidence links, or update existing "
                "tasks/roadmap/milestone state if this is only branch status."
            ),
        }
        return result, "blocked record_lesson; lesson consolidation required"

    if deliverable_progress_guard and (name in DELIVERABLE_PROGRESS_BLOCKED_TOOLS or shell_read_only):
        result = {
            "success": False,
            "error": "deliverable checkpoint required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "deliverable_progress_guard": deliverable_progress_guard,
            "guidance": (
                "This job is deliverable-framed and has done enough background work without a draft/report/file "
                "checkpoint. Save a partial deliverable with write_file or write_artifact, or record_tasks, "
                "record_roadmap, record_milestone_validation, or record_lesson if the deliverable is blocked."
            ),
        }
        return result, f"blocked {name}; deliverable checkpoint required"

    research_balance = _research_balance_context(job, recent_steps)
    if research_balance and name in RESEARCH_BALANCE_BLOCKED_TOOLS:
        result = {
            "success": False,
            "error": "research balance required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "research_balance": research_balance,
            "guidance": (
                "Recent work is execution-heavy but has no durable sources or findings. "
                "Use web/browser/documentation/local-inspection tools and record_source or record_findings "
                "before continuing execution, artifact review, raw lesson accumulation, report updates, or file churn."
            ),
        }
        return result, f"blocked {name}; research balance required"

    roadmap_staleness = _roadmap_staleness_context(job, recent_steps)
    if roadmap_staleness and not checkpoint_resolution_call and name in ROADMAP_STALENESS_BLOCKED_TOOLS:
        result = {
            "success": False,
            "error": "roadmap update required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "roadmap_staleness": roadmap_staleness,
            "guidance": (
                "The roadmap has not advanced despite durable task/artifact activity. "
                "Use record_roadmap to mark milestone progress, record_milestone_validation "
                "to judge an evidence-backed checkpoint, or record_lesson if the roadmap is wrong."
            ),
        }
        return result, f"blocked {name}; roadmap update required"

    milestone_validation = _milestone_validation_needed(job)
    milestone_validation_action = milestone_validation and _tool_call_matches_pending_milestone_need(
        name,
        args,
        milestone_validation,
    )
    if (
        milestone_validation
        and not milestone_validation_action
        and not checkpoint_resolution_call
        and name in MILESTONE_VALIDATION_BLOCKED_TOOLS
    ):
        result = {
            "success": False,
            "error": "milestone validation required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "milestone": {
                "title": milestone_validation.get("title"),
                "status": milestone_validation.get("status"),
                "validation_status": milestone_validation.get("validation_status"),
                "acceptance_criteria": milestone_validation.get("acceptance_criteria"),
                "evidence_needed": milestone_validation.get("evidence_needed"),
            },
            "guidance": (
                "The current milestone is ready for validation. Use record_milestone_validation "
                "with evidence and pass/fail/blocker status, read an existing artifact if needed, "
                "or create follow-up tasks for validation gaps before starting more branch work."
            ),
        }
        return result, f"blocked {name}; milestone validation required"

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

    experiment_next_action = _latest_experiment_next_action_context(job)
    action_failure = _experiment_next_action_failure_context(job, recent_steps)
    if (
        action_failure
        and name not in {"record_experiment", "record_tasks", "record_lesson", "record_milestone_validation"}
    ):
        result = {
            "success": False,
            "error": "action result accounting required",
            "blocked_tool": name,
            "blocked_arguments": args,
            "action_failure": action_failure,
            "guidance": (
                "The latest experiment next action was attempted and the observed output reports a missing command, "
                "path, or prerequisite. Before more work, use record_experiment, record_tasks, or record_lesson to "
                "account for the failed/blocked action and choose a concrete recovery branch."
            ),
        }
        return result, f"blocked {name}; action result accounting required"
    if (
        _experiment_next_action_requires_delivery(experiment_next_action)
        and (
            name in EXPERIMENT_NEXT_ACTION_BLOCKED_TOOLS
            or (
                name == "shell_exec"
                and _shell_command_looks_read_only(str(args.get("command") or ""))
                and not _shell_command_supports_experiment_next_action(str(args.get("command") or ""), experiment_next_action)
            )
        )
    ):
        result = {
            "success": False,
            "error": "experiment next action pending",
            "blocked_tool": name,
            "blocked_arguments": args,
            "experiment_next_action": experiment_next_action,
            "guidance": (
                "The latest measured experiment selected a delivery/action next step. "
                "Act on that next action with an execution or ledger tool, or use record_experiment/record_tasks/record_lesson "
                "to explain why it is invalid or blocked before doing more research or artifact review."
            ),
        }
        return result, f"blocked {name}; experiment next action pending"

    shell_budget_exhausted = (
        name == "shell_exec"
        and _as_int(measured_progress_guard.get("shell_actions_since_last_experiment")) >= MEASURABLE_ACTION_BUDGET_STEPS
    ) if measured_progress_guard else False
    candidate_validation_shell = (
        name == "shell_exec" and _shell_exec_targets_candidate_file(job, recent_steps, args)
    )
    if (
        measured_progress_guard
        and not checkpoint_resolution_call
        and (name in MEASURABLE_RESEARCH_BLOCKED_TOOLS or (shell_budget_exhausted and not candidate_validation_shell))
    ):
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

    if name == "search_artifacts":
        similar_step = _similar_recent_query_tool("search_artifacts", args, recent_steps)
        if similar_step:
            result = {
                "success": False,
                "error": "similar artifact search blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "previous_step": similar_step["id"],
                "guidance": (
                    "Use a returned artifact, record what the prior artifact searches proved, "
                    "or create the next concrete task instead of searching saved outputs again."
                ),
            }
            return result, f"blocked similar search_artifacts; previous step #{similar_step['step_no']}"
        streak = _recent_tool_streak(recent_steps, "search_artifacts")
        if streak >= 3:
            result = {
                "success": False,
                "error": "artifact search loop blocked",
                "blocked_tool": name,
                "blocked_arguments": args,
                "recent_artifact_search_streak": streak,
                "guidance": (
                    "Stop searching saved outputs. Read a specific returned artifact, update tasks/findings/lessons, "
                    "or write the next report artifact from already-read evidence."
                ),
            }
            return result, f"blocked search_artifacts after {streak} consecutive artifact searches"

    return None


def _error_result(exc: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }
    if isinstance(exc, LLMResponseError) and exc.payload:
        result["provider_payload"] = exc.payload
    return result


def _hard_llm_provider_failure_note(exc: Exception) -> str:
    return provider_action_required_note(exc)


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


def _lesson_already_recorded(job: dict[str, Any], lesson: str, *, category: str) -> bool:
    text = " ".join(str(lesson or "").split())
    wanted_category = str(category or "memory").strip().lower() or "memory"
    return any(
        str(entry.get("category") or "memory").strip().lower() == wanted_category
        and " ".join(str(entry.get("lesson") or "").split()) == text
        for entry in _metadata_list(job, "lessons")
    )


def _reflection_strategy(
    *,
    failures: list[dict[str, Any]],
    findings: list[Any],
    sources: list[Any],
    tasks: list[Any],
    measured_experiments: list[dict[str, Any]],
    pending_measurement: bool,
    validating_milestones: list[dict[str, Any]],
    active_operator_messages: list[dict[str, Any]],
) -> str:
    if pending_measurement:
        return "Resolve the pending measurement obligation before expanding research, outputs, or branch work."
    if active_operator_messages:
        return "Incorporate or supersede active operator context before choosing new autonomous branches."
    if validating_milestones:
        return "Validate the current roadmap milestone from evidence before adding more milestone scope."
    if measured_experiments:
        return "Continue from the best measured result; reject or pivot branches that do not improve the active metric."
    yielded_sources = [
        source
        for source in sources
        if isinstance(source, dict)
        and (_as_int(source.get("yield_count")) > 0 or _as_float(source.get("usefulness_score")) >= 0.8)
    ]
    if len(sources) >= SOURCE_YIELD_MIN_SOURCES and len(findings) + len(yielded_sources) < max(2, len(sources) // 8):
        return "Distill gathered sources into durable findings or source yield decisions before collecting more sources."
    if failures:
        return "Classify blocked or failed steps into durable task, source, experiment, or lesson outcomes before retrying."
    open_tasks = [
        task
        for task in tasks
        if isinstance(task, dict)
        and str(task.get("status") or "open").lower() in {"open", "active", "blocked"}
    ]
    if open_tasks:
        return "Execute or resolve the highest-priority open task before creating more task branches."
    return "Choose the next branch from durable evidence, then record the result as findings, tasks, experiments, sources, or memory."


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
    *,
    messages: list[dict[str, Any]],
    context_length: int,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    if response.tool_calls:
        body = ", ".join(call.name for call in response.tool_calls)
        metadata = {"run_id": run_id, "tool_calls": [call.name for call in response.tool_calls]}
    else:
        body = response.content[:1000]
        metadata = {"run_id": run_id, "tool_calls": []}
    metadata["usage"] = turn_usage_metadata(response, messages=messages, context_length=context_length)
    if duration_seconds is not None:
        metadata["duration_seconds"] = round(max(0.0, float(duration_seconds)), 3)
    if response.model:
        metadata["model"] = response.model
    if response.response_id:
        metadata["response_id"] = response.response_id
    db.append_event(
        job_id,
        event_type="loop",
        title="message_end",
        body=body,
        ref_table="job_runs",
        ref_id=run_id,
        metadata=metadata,
    )
    return metadata["usage"]


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
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    validating_milestones = [
        milestone for milestone in milestones
        if isinstance(milestone, dict)
        and (
            str(milestone.get("status") or "planned") == "validating"
            or str(milestone.get("validation_status") or "not_started") == "pending"
        )
    ]
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
            f"{experiment.get('title')} " + format_metric_value(
                experiment.get("metric_name") or "metric",
                experiment.get("metric_value"),
                experiment.get("metric_unit") or "",
            )
            for experiment in best_experiments[-3:]
        )
    summary = (
        f"Reflection through step #{step_no}: {len(findings)} findings, {len(sources)} sources, "
        f"{len(tasks)} tasks, {len(experiments)} experiments, {len(milestones)} roadmap milestones, "
        f"{len(lessons)} lessons, "
        f"{len(active_operator_messages)} active operator messages, "
        f"{len(finding_batches)} recent finding artifacts, {len(failures)} recent blocked/failed steps. "
        f"Best source direction: {source_text}. Best measured result: {best_experiment_text}."
        + (f" Roadmap '{roadmap.get('title')}' has {len(validating_milestones)} milestone(s) needing validation." if roadmap else "")
        + (" Pending measurement obligation needs resolution." if pending_measurement else "")
    )
    strategy = _reflection_strategy(
        failures=failures,
        findings=findings,
        sources=sources,
        tasks=tasks,
        measured_experiments=measured_experiments,
        pending_measurement=bool(pending_measurement),
        validating_milestones=validating_milestones,
        active_operator_messages=active_operator_messages,
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
            "roadmap_milestone_count": len(milestones),
            "roadmap_validation_needed_count": len(validating_milestones),
            "measured_experiment_count": len(measured_experiments),
            "active_operator_message_count": len(active_operator_messages),
            "pending_measurement_obligation": bool(pending_measurement),
        },
    )
    lesson = None
    if not _lesson_already_recorded(job, strategy, category="strategy"):
        lesson = db.append_lesson(
            job_id,
            strategy,
            category="strategy",
            confidence=0.75,
            metadata={"source": "reflection", "through_step": step_no},
        )
    db.append_agent_update(job_id, summary, category="plan", metadata={"reflection": reflection})
    result = {"success": True, "reflection": reflection, "lesson_recorded": bool(lesson)}
    db.finish_step(step_id, status="completed", summary=summary, output_data=result)
    db.finish_run(run_id, "completed")
    _emit_loop_end(db, job_id, run_id, status="completed", step_id=step_id, tool_name="reflect", detail=summary)
    refresh_memory_index(db, job_id)
    return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name="reflect", status="completed", result=result)


def _run_guard_recovery_step(
    context: dict[str, Any],
    *,
    db: AgentDB,
    job_id: str,
    run_id: str,
) -> StepExecution:
    error = str(context.get("error") or "recoverable guard")
    checkpoint_accounting = error == "evidence checkpoint accounting required"
    task_queue_saturated = error == "task queue saturated"
    task_goal = "Convert the repeated guard block into durable progress before retrying the blocked action."
    acceptance = (
        "Use record_tasks, record_findings, record_source, record_experiment, or record_lesson to state what "
        "changed, what branch is rejected, or what concrete branch should run next."
    )
    stall_behavior = "If the same guard appears again, pivot to a different branch or record the branch as blocked."
    if checkpoint_accounting:
        task_goal = (
            "Account for the already-read evidence checkpoint as durable progress, a rejected branch, "
            "or a blocked branch before continuing."
        )
        acceptance = (
            "Use record_findings, record_source, record_experiment, record_tasks, record_roadmap, "
            "record_milestone_validation, or record_lesson to state exactly what the checkpoint proved, "
            "invalidated, changed, or failed to provide. Do not read the same checkpoint again."
        )
        stall_behavior = (
            "If the checkpoint cannot produce durable progress, record a lesson or task that names the blocker "
            "and choose a different branch."
        )
    step_id = db.add_step(job_id=job_id, run_id=run_id, kind="recovery", tool_name="guard_recovery")
    if task_queue_saturated:
        task_queue = context.get("task_queue") if isinstance(context.get("task_queue"), dict) else {}
        lesson = db.append_lesson(
            job_id,
            (
                f"Repeated task queue saturation occurred {context.get('count')} times. "
                "Do not open guard-recovery tasks for saturation; consolidate, complete, block, or skip existing branches "
                "before adding new work."
            ),
            category="strategy",
            confidence=0.85,
            metadata={"guard_recovery": context},
        )
        db.update_job_metadata(
            job_id,
            {
                "task_backlog_pressure": {
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                    "guard_recovery": context,
                    "reason": task_queue.get("reason") or "task queue saturated",
                    "open_count": task_queue.get("open_count"),
                    "total_count": task_queue.get("total_count"),
                }
            },
        )
        message = (
            f"Guard recovery recorded task queue saturation from step #{context.get('first_step_no')} "
            f"to #{context.get('latest_step_no')}; no new task was opened."
        )
        update = db.append_agent_update(
            job_id,
            message,
            category="blocked",
            metadata={"guard_recovery": context, "lesson_key": lesson.get("key"), "task_queue_saturation": True},
        )
        result = {
            "success": True,
            "guard_recovery": context,
            "lesson": lesson,
            "update": update,
            "task_opened": False,
        }
        db.finish_step(step_id, status="completed", summary=message, output_data=result)
        finished_step = _step_by_id(db, job_id, step_id)
        _resolve_evidence_checkpoint(
            db=db,
            job_id=job_id,
            tool_name="guard_recovery",
            step=finished_step,
        )
        db.finish_run(run_id, "completed")
        _emit_loop_end(db, job_id, run_id, status="completed", step_id=step_id, tool_name="guard_recovery", detail=message)
        refresh_memory_index(db, job_id)
        return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name="guard_recovery", status="completed", result=result)

    lesson = db.append_lesson(
        job_id,
        (
            f"Repeated guard block '{error}' occurred {context.get('count')} times. "
            + (
                "The checkpoint has already been read; do not reread it. Account for the evidence with a durable "
                "record or reject/block that branch before continuing."
                if checkpoint_accounting
                else "Do not retry the same blocked tool pattern; update durable progress state, create a new branch, "
                "or explicitly reject the branch before continuing."
            )
        ),
        category="strategy",
        confidence=0.75,
        metadata={"guard_recovery": context},
    )
    task = db.append_task_record(
        job_id,
        title=f"Resolve guard: {error}",
        status="open",
        priority=9,
        goal=task_goal,
        output_contract="decision",
        acceptance_criteria=acceptance,
        evidence_needed=f"Recent blocked tools: {', '.join(context.get('blocked_tools') or [])}",
        stall_behavior=stall_behavior,
        metadata={"guard_recovery": context, "resolves_evidence_checkpoint": checkpoint_accounting},
    )
    message = (
        f"Guard recovery opened a task after repeated '{error}' blocks "
        f"from step #{context.get('first_step_no')} to #{context.get('latest_step_no')}."
    )
    update = db.append_agent_update(
        job_id,
        message,
        category="blocked",
        metadata={"guard_recovery": context, "task_key": task.get("key"), "lesson_key": lesson.get("key")},
    )
    result = {
        "success": True,
        "guard_recovery": context,
        "lesson": lesson,
        "task": task,
        "update": update,
    }
    db.finish_step(step_id, status="completed", summary=message, output_data=result)
    finished_step = _step_by_id(db, job_id, step_id)
    _resolve_evidence_checkpoint(
        db=db,
        job_id=job_id,
        tool_name="guard_recovery",
        step=finished_step,
    )
    db.finish_run(run_id, "completed")
    _emit_loop_end(db, job_id, run_id, status="completed", step_id=step_id, tool_name="guard_recovery", detail=message)
    refresh_memory_index(db, job_id)
    return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name="guard_recovery", status="completed", result=result)


def _usage_budget_limit_context(config: AppConfig, usage: dict[str, Any]) -> dict[str, Any] | None:
    limit = config.runtime.max_job_cost_usd
    if limit is None or limit <= 0 or not bool(usage.get("has_cost")):
        return None
    cost = _as_float(usage.get("cost"))
    if cost < float(limit):
        return None
    return {
        "limit": float(limit),
        "cost": cost,
        "calls": _as_int(usage.get("calls")),
        "total_tokens": _as_int(usage.get("total_tokens")),
        "prompt_tokens": _as_int(usage.get("prompt_tokens")),
        "completion_tokens": _as_int(usage.get("completion_tokens")),
    }


def _run_usage_budget_limit_step(
    context: dict[str, Any],
    *,
    db: AgentDB,
    job_id: str,
    run_id: str,
) -> StepExecution:
    limit = float(context.get("limit") or 0.0)
    cost = float(context.get("cost") or 0.0)
    message = (
        f"Paused job: configured model cost limit ${limit:g} reached "
        f"(current cost ${cost:.4f}, {context.get('calls')} model calls, "
        f"{_compact_usage_tokens(context.get('total_tokens'))} tokens). "
        "Raise the limit, switch model/provider, or resume after deciding the budget is acceptable."
    )
    metadata = {
        "reason": "usage_budget_limit",
        "usage_budget_limit": context,
        "last_note": message,
        "usage_budget_blocked_at": datetime.now(timezone.utc).isoformat(),
    }
    db.update_job_status(job_id, "paused", metadata_patch=metadata)
    step_id = db.add_step(job_id=job_id, run_id=run_id, kind="recovery", tool_name="budget_limit")
    result = {"success": True, "job_id": job_id, "paused": True, **context}
    db.append_agent_update(
        job_id,
        message,
        category="blocked",
        metadata={"reason": "usage_budget_limit", "usage_budget_limit": context},
    )
    db.finish_step(step_id, status="completed", summary=message, output_data=result)
    db.finish_run(run_id, "completed")
    _emit_loop_end(db, job_id, run_id, status="completed", step_id=step_id, tool_name="budget_limit", detail=message)
    refresh_memory_index(db, job_id)
    return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name="budget_limit", status="completed", result=result)


def _compact_usage_tokens(value: object) -> str:
    number = _as_int(value)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(number)


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
    db.update_job_metadata(
        job_id,
        {
            "pending_evidence_checkpoint": {
                "artifact_id": stored.id,
                "title": stored.title or f"Auto Evidence Checkpoint after step {evidence_step.get('step_no')}",
                "path": str(stored.path),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "checkpoint_step_id": step_id,
                "evidence_step": evidence_step.get("id"),
                "evidence_step_no": evidence_step.get("step_no"),
                "evidence_tool": evidence_step.get("tool_name") or evidence_step.get("kind"),
                "blocked_tool": blocked_tool,
            }
        },
    )
    return {"artifact_id": stored.id, "path": str(stored.path), "lesson": lesson}


def _auto_record_grounding_block_lesson(*, db: AgentDB, job_id: str, result: dict[str, Any]) -> None:
    if result.get("error") != "evidence grounding required":
        return
    grounding = result.get("evidence_grounding") if isinstance(result.get("evidence_grounding"), dict) else {}
    unsupported = grounding.get("unsupported_tokens") if isinstance(grounding.get("unsupported_tokens"), list) else []
    unsupported = [str(token) for token in unsupported if str(token).strip()]
    if not unsupported:
        return
    cited_steps = grounding.get("cited_steps") if isinstance(grounding.get("cited_steps"), list) else []
    blocked_tool = str(result.get("blocked_tool") or "")
    fingerprint = "|".join([blocked_tool, ",".join(unsupported[:8]), ",".join(str(step) for step in cited_steps[:8])])
    job = db.get_job(job_id)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    seen = metadata.get("grounding_block_fingerprints") if isinstance(metadata.get("grounding_block_fingerprints"), list) else []
    if fingerprint in seen:
        return
    db.append_lesson(
        job_id,
        (
            f"Evidence grounding rejected unsupported concrete tokens for {blocked_tool or 'a durable record'}: "
            f"{', '.join(unsupported[:8])}. Treat matching prior ledger, artifact, or memory claims as stale until "
            "they are re-verified from the cited evidence."
        ),
        category="mistake",
        confidence=0.9,
        metadata={"evidence_grounding": grounding, "blocked_tool": blocked_tool},
    )
    metadata_patch: dict[str, Any] = {"grounding_block_fingerprints": (seen + [fingerprint])[-100:]}
    stale_tokens = _stale_claim_tokens_from_unsupported(
        unsupported,
        reference_text=" ".join(str(job.get(key) or "") for key in ("title", "objective", "kind")),
    )
    if stale_tokens:
        existing_tokens = [
            str(token)
            for token in metadata.get("unsupported_claim_tokens", [])
            if str(token).strip()
        ] if isinstance(metadata.get("unsupported_claim_tokens"), list) else []
        combined: list[str] = []
        combined_seen: set[str] = set()
        for token in existing_tokens + stale_tokens:
            key = token.lower()
            if key in combined_seen:
                continue
            combined_seen.add(key)
            combined.append(token)
        metadata_patch["unsupported_claim_tokens"] = combined[-80:]
    db.update_job_metadata(job_id, metadata_patch)


def _mark_evidence_checkpoint_read(
    *,
    db: AgentDB,
    job_id: str,
    tool_name: str,
    args: dict[str, Any],
    step: dict[str, Any] | None,
) -> None:
    if tool_name != "read_artifact":
        return
    job = db.get_job(job_id)
    pending = _pending_evidence_checkpoint(job)
    if not pending or pending.get("read_at"):
        return
    if not _read_artifact_call_matches_checkpoint(
        args,
        artifact_id=str(pending.get("artifact_id") or ""),
        artifact_title=str(pending.get("title") or ""),
    ):
        return
    updated = dict(pending)
    updated["read_at"] = datetime.now(timezone.utc).isoformat()
    if step:
        updated["read_step_id"] = step.get("id")
        updated["read_step_no"] = step.get("step_no")
    db.update_job_metadata(job_id, {"pending_evidence_checkpoint": updated})
    db.append_agent_update(
        job_id,
        f"Read evidence checkpoint {pending.get('artifact_id')}; durable accounting is required next.",
        category="blocked",
        metadata={"pending_evidence_checkpoint": updated},
    )


def _resolve_evidence_checkpoint(
    *,
    db: AgentDB,
    job_id: str,
    tool_name: str,
    step: dict[str, Any] | None,
) -> None:
    if tool_name not in EVIDENCE_CHECKPOINT_RESOLUTION_TOOLS and tool_name != "guard_recovery":
        return
    job = db.get_job(job_id)
    pending = _pending_evidence_checkpoint(job)
    if not pending:
        return
    updated = dict(pending)
    updated["resolved_at"] = datetime.now(timezone.utc).isoformat()
    updated["resolved_by_tool"] = tool_name
    if step:
        updated["resolved_by_step_id"] = step.get("id")
        updated["resolved_by_step_no"] = step.get("step_no")
    db.update_job_metadata(job_id, {"pending_evidence_checkpoint": updated})
    db.append_agent_update(
        job_id,
        f"Evidence checkpoint {pending.get('artifact_id')} accounted for with {tool_name}.",
        category="progress",
        metadata={"pending_evidence_checkpoint": updated},
    )


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
    if tool_name == "web_search":
        query = str(result.get("query") or "").strip()
        results = result.get("results") if isinstance(result.get("results"), list) else []
        for item in results[:8]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            title = str(item.get("title") or "").strip()
            db.append_source_record(
                job_id,
                url,
                source_type="web_search",
                usefulness_score=0.35,
                yield_count=0,
                outcome=f"search result for {query or 'query'}: {title[:160]}",
                metadata={"auto_from_tool": "web_search", "query": query, "title": title},
            )
        return
    if tool_name == "web_extract":
        pages = result.get("pages") if isinstance(result.get("pages"), list) else []
        for page in pages[:12]:
            if not isinstance(page, dict):
                continue
            url = str(page.get("url") or "").strip()
            if not url:
                continue
            text = str(page.get("text") or "")
            error = str(page.get("error") or "")
            if error:
                db.append_source_record(
                    job_id,
                    url,
                    source_type="web_extract",
                    usefulness_score=0.1,
                    fail_count_delta=1,
                    warnings=[error[:180]],
                    outcome=f"extract failed: {error[:180]}",
                    metadata={"auto_from_tool": "web_extract"},
                )
                continue
            score = 0.35
            if len(text.strip()) >= 500:
                score = 0.55
            if len(text.strip()) >= 3000:
                score = 0.7
            db.append_source_record(
                job_id,
                url,
                source_type="web_extract",
                usefulness_score=score,
                yield_count=0,
                outcome=f"extracted {len(text.strip())} chars for possible use",
                metadata={"auto_from_tool": "web_extract"},
            )
        return
    if tool_name in {"browser_navigate", "browser_snapshot"}:
        context = _browser_warning_context(result)
        if not context:
            return
        result["source_warning"] = context["reason"]
        result["source_url"] = context.get("url") or ""
        _auto_record_blocked_source(db=db, job_id=job_id, context=context, blocked_tool=tool_name or "browser")


def _auto_record_failed_shell_sources(
    *,
    db: AgentDB,
    job_id: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    error_text = " ".join(str(result.get(key) or "") for key in ("error", "stderr", "stdout"))
    lowered = error_text.lower()
    if not any(
        marker in lowered
        for marker in (
            "authentication",
            "authorization",
            "unauthorized",
            "forbidden",
            "http failure",
            "http 401",
            "http 403",
            "401 unauthorized",
            "403 forbidden",
        )
    ):
        return
    recorded: set[str] = set()
    for url in _shell_guard_urls(str(args.get("command") or ""))[:3]:
        candidates = [url]
        family_url = _source_failure_family_url(url)
        if family_url and not _same_source_url(family_url, url):
            candidates.append(family_url)
        for candidate in candidates:
            if candidate.lower() in recorded:
                continue
            recorded.add(candidate.lower())
            is_family = candidate != url
            warning = (
                "shell command reported authentication/authorization or HTTP failure for this source family"
                if is_family
                else "shell command reported authentication/authorization or HTTP failure"
            )
            outcome = (
                f"Source family blocked after failed child URL {url}: {_clip_text(str(result.get('error') or error_text), 420)}"
                if is_family
                else _clip_text(str(result.get("error") or error_text), 500)
            )
            metadata = {"auto_from_tool": "shell_exec", "failure_kind": "auth_or_http"}
            if is_family:
                metadata.update({"source_family": True, "failed_child_url": url})
            db.append_source_record(
                job_id,
                candidate,
                source_type="shell_exec_family" if is_family else "shell_exec",
                usefulness_score=0.01,
                fail_count_delta=1,
                warnings=[warning],
                outcome=outcome,
                metadata=metadata,
            )


def _auto_reconcile_artifact_tasks(
    *,
    db: AgentDB,
    job_id: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    artifact_id = str(result.get("artifact_id") or "")
    if not artifact_id:
        return []
    artifact_title = str(args.get("title") or "")
    artifact_summary = str(args.get("summary") or "")
    artifact_content = str(args.get("content") or "")
    artifact_text = " ".join([artifact_title, artifact_summary, artifact_content[:4000]])
    artifact_tokens = _text_tokens(artifact_text)
    if len(artifact_tokens) < 2:
        return []
    job = db.get_job(job_id)
    reconciled = []
    for task in _metadata_list(job, "task_queue"):
        status = str(task.get("status") or "open").strip().lower()
        if status not in {"open", "active"}:
            continue
        contract = str(task.get("output_contract") or "").strip().lower()
        if contract in {"experiment", "action", "monitor"}:
            continue
        task_text = " ".join(
            str(task.get(key) or "")
            for key in ("title", "goal", "acceptance_criteria", "evidence_needed", "source_hint")
        )
        if not _artifact_can_reconcile_task(
            contract=contract,
            task_text=task_text,
            artifact_title=artifact_title,
            artifact_summary=artifact_summary,
        ):
            continue
        task_tokens = _text_tokens(task_text)
        if len(task_tokens) < 2:
            continue
        overlap = task_tokens & artifact_tokens
        needed = max(2, min(4, (len(task_tokens) + 1) // 2))
        if len(overlap) < needed:
            continue
        updated = db.append_task_record(
            job_id,
            title=str(task.get("title") or ""),
            status="done",
            priority=_as_int(task.get("priority")),
            goal=str(task.get("goal") or ""),
            source_hint=str(task.get("source_hint") or ""),
            result=f"Saved output {artifact_id}: {_clip_text(artifact_title or artifact_summary, 180)}",
            parent=str(task.get("parent") or ""),
            output_contract=contract,
            acceptance_criteria=str(task.get("acceptance_criteria") or ""),
            evidence_needed=str(task.get("evidence_needed") or ""),
            stall_behavior=str(task.get("stall_behavior") or ""),
            metadata={
                **(task.get("metadata") if isinstance(task.get("metadata"), dict) else {}),
                "auto_reconciled_from_artifact": artifact_id,
                "matched_tokens": sorted(overlap)[:12],
            },
        )
        reconciled.append(updated)
    if reconciled:
        titles = ", ".join(str(task.get("title") or "") for task in reconciled[:4])
        db.append_agent_update(
            job_id,
            f"Task progress reconciled from saved output {artifact_id}: {titles}.",
            category="plan",
            metadata={"artifact_id": artifact_id, "task_count": len(reconciled)},
        )
    return reconciled


def _auto_open_revision_task_for_deliverable(
    *,
    db: AgentDB,
    job_id: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    artifact_id = str(result.get("artifact_id") or "")
    if not artifact_id:
        return None
    artifact_title = str(args.get("title") or "")
    artifact_summary = str(args.get("summary") or "")
    if not _artifact_can_reconcile_task(
        contract="report",
        task_text="review revise draft report deliverable",
        artifact_title=artifact_title,
        artifact_summary=artifact_summary,
    ):
        return None
    job = db.get_job(job_id)
    for task in _metadata_list(job, "task_queue"):
        if str(task.get("status") or "open").strip().lower() not in {"open", "active"}:
            continue
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        if metadata.get("revision_source_artifact_id") == artifact_id:
            return None
        if metadata.get("source") == "auto_revision_loop":
            db.append_task_record(
                job_id,
                title=str(task.get("title") or ""),
                status="skipped",
                priority=_as_int(task.get("priority")),
                goal=str(task.get("goal") or ""),
                source_hint=str(task.get("source_hint") or ""),
                result=f"Superseded by newer saved output {artifact_id}.",
                parent=str(task.get("parent") or ""),
                output_contract=str(task.get("output_contract") or ""),
                acceptance_criteria=str(task.get("acceptance_criteria") or ""),
                evidence_needed=str(task.get("evidence_needed") or ""),
                stall_behavior=str(task.get("stall_behavior") or ""),
                metadata={**metadata, "superseded_by_artifact_id": artifact_id},
            )
    task = db.append_task_record(
        job_id,
        title=f"Review and revise saved output {artifact_id}",
        status="open",
        priority=4,
        goal="Use the latest saved deliverable as a baseline, check it against evidence and acceptance criteria, then improve it.",
        source_hint=artifact_id,
        output_contract="report",
        acceptance_criteria="The saved output is reviewed and either revised, validated, or given concrete follow-up gaps.",
        evidence_needed="Saved output, relevant evidence artifacts or files, and explicit gap/revision notes.",
        stall_behavior="If no useful revision is possible, record why and open the next evidence, validation, or monitoring branch.",
        metadata={
            "source": "auto_revision_loop",
            "revision_source_artifact_id": artifact_id,
            "source_title": artifact_title,
        },
    )
    db.append_agent_update(
        job_id,
        f"Opened revision branch for saved output {artifact_id}: {_clip_text(artifact_title or artifact_summary, 160)}.",
        category="plan",
        metadata={"artifact_id": artifact_id, "task_key": task.get("key"), "source": "auto_revision_loop"},
    )
    return task


def _artifact_can_reconcile_task(
    *,
    contract: str,
    task_text: str,
    artifact_title: str,
    artifact_summary: str,
) -> bool:
    contract = contract.strip().lower()
    if contract in {"experiment", "action", "monitor"}:
        return False
    if contract == "research":
        return True
    artifact_text = f"{artifact_title} {artifact_summary}".lower()
    task_lower = task_text.lower()
    evidence_like = any(term in artifact_text for term in EVIDENCE_ARTIFACT_TERMS)
    deliverable_like = any(term in artifact_text for term in DELIVERABLE_ARTIFACT_TERMS)
    task_needs_deliverable_action = any(term in task_lower for term in TASK_DELIVERABLE_ACTION_TERMS)
    if evidence_like:
        return False
    if task_needs_deliverable_action and not deliverable_like:
        return False
    return True


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
    previous = metadata.get("last_checkpoint_counts") if isinstance(metadata.get("last_checkpoint_counts"), dict) else {}
    checkpoint = build_progress_checkpoint(
        metadata,
        previous_counts=previous,
        step_no=step_no,
        tool_name=tool_name,
        artifact_id=str(result.get("artifact_id") or ""),
        is_finding_output=is_finding_batch,
    )
    db.append_agent_update(
        job_id,
        checkpoint.message,
        category=checkpoint.category,
        metadata={
            "step_no": step_no,
            "tool": tool_name,
            "deltas": checkpoint.deltas,
            "updates": checkpoint.updates,
            "resolutions": checkpoint.resolutions,
        },
    )
    streak = _as_int(metadata.get("activity_checkpoint_streak"))
    streak = streak + 1 if checkpoint.category == "activity" else 0
    task_durable_change = checkpoint.deltas.get("tasks", 0) + checkpoint.updates.get("tasks", 0)
    non_task_durable_change = any(
        checkpoint.deltas.get(key, 0) > 0
        or checkpoint.updates.get(key, 0) > 0
        or checkpoint.resolutions.get(key, 0) > 0
        for key in ("findings", "sources", "experiments", "lessons", "milestones")
    )
    task_resolution = checkpoint.resolutions.get("tasks", 0) > 0
    task_only_progress = task_durable_change > 0 and not non_task_durable_change and not task_resolution
    task_planning_streak = _as_int(metadata.get("task_planning_checkpoint_streak"))
    task_planning_streak = task_planning_streak + 1 if task_only_progress else 0
    db.update_job_metadata(
        job_id,
        {
            "last_checkpoint_counts": checkpoint.counts,
            "last_checkpoint_at": datetime.now(timezone.utc).isoformat(),
            "activity_checkpoint_streak": streak,
            "task_planning_checkpoint_streak": task_planning_streak,
        },
    )


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
    args = _normalize_milestone_validation_args_for_active_gate(call.name, call.arguments, job)
    input_data = {"tool_call_id": call.id, "arguments": args}
    if args != call.arguments:
        input_data["original_arguments"] = call.arguments
    step_id = db.add_step(
        job_id=job_id,
        run_id=run_id,
        kind="tool",
        tool_name=call.name,
        input_data=input_data,
    )
    validate_arguments = getattr(registry, "validate_arguments", None)
    argument_block = validate_arguments(call.name, args, config) if callable(validate_arguments) else None
    if argument_block:
        concrete_fields = [*(argument_block.get("missing_arguments") or []), *(argument_block.get("placeholder_arguments") or [])]
        reason = "missing required arguments" if argument_block.get("missing_arguments") else str(argument_block.get("error") or "invalid tool arguments")
        summary = f"blocked {call.name}; {reason}: {', '.join(concrete_fields)}"
        db.finish_step(
            step_id,
            status="blocked",
            summary=summary,
            output_data=argument_block,
            error=None,
        )
        db.append_agent_update(
            job_id,
            summary,
            category="blocked",
            metadata={
                "reason": "tool_arguments_missing",
                "tool": call.name,
                "missing_arguments": argument_block.get("missing_arguments") or [],
                "placeholder_arguments": argument_block.get("placeholder_arguments") or [],
            },
        )
        return (
            StepExecution(
                job_id=job_id,
                run_id=run_id,
                step_id=step_id,
                tool_name=call.name,
                status="blocked",
                result=argument_block,
            ),
            True,
            summary,
            None,
        )
    blocked = _blocked_tool_call_result(call.name, args, recent_steps, job)
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
        if result.get("error") == "task queue saturated":
            step = _step_by_id(db, job_id, step_id)
            task_queue = result.get("task_queue") if isinstance(result.get("task_queue"), dict) else {}
            _record_task_backlog_pressure(
                db=db,
                job_id=job_id,
                step_no=(step or {}).get("step_no"),
                task_queue=task_queue,
                source="blocked_record_tasks",
            )
        _auto_record_grounding_block_lesson(db=db, job_id=job_id, result=result)
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
        raw_result = registry.handle(call.name, args, ctx)
        result = _parse_tool_result(raw_result)
        ok = bool(result.get("success", True)) and not result.get("error")
        status = "completed" if ok else "blocked" if result.get("recoverable") is True else "failed"
        if ok:
            _auto_record_tool_source_quality(db=db, job_id=job_id, tool_name=call.name, result=result)
        elif call.name == "shell_exec":
            _auto_record_failed_shell_sources(db=db, job_id=job_id, args=args, result=result)
        summary = _summarize_tool_result(call.name, args, result, ok=ok)
        db.finish_step(step_id, status=status, summary=summary, output_data=result, error=result.get("error"))
        if call.name == "shell_exec":
            _maybe_resolve_file_validation_obligation(
                db=db,
                job_id=job_id,
                tool_name=call.name,
                args=args,
                result=result,
                ok=ok,
            )
        if ok:
            finished_step = _step_by_id(db, job_id, step_id)
            _mark_evidence_checkpoint_read(
                db=db,
                job_id=job_id,
                tool_name=call.name,
                args=args,
                step=finished_step,
            )
            _resolve_evidence_checkpoint(
                db=db,
                job_id=job_id,
                tool_name=call.name,
                step=finished_step,
            )
            _maybe_create_measurement_obligation(
                db=db,
                job_id=job_id,
                step=finished_step,
                tool_name=call.name,
                args=args,
                result=result,
            )
            if call.name == "write_file":
                _maybe_create_file_validation_obligation(
                    db=db,
                    job_id=job_id,
                    step=finished_step,
                    args=args,
                    result=result,
                )
            elif call.name in {"record_lesson", "record_tasks", "record_experiment", "record_milestone_validation"}:
                _maybe_resolve_file_validation_obligation(
                    db=db,
                    job_id=job_id,
                    tool_name=call.name,
                    args=args,
                    result=result,
                    ok=ok,
                )
            _auto_checkpoint_update(
                db=db,
                job_id=job_id,
                step_no=(finished_step or db.list_steps(job_id=job_id)[-1])["step_no"],
                tool_name=call.name,
                args=args,
                result=result,
            )
            if call.name == "write_artifact":
                reconciled_tasks = _auto_reconcile_artifact_tasks(
                    db=db,
                    job_id=job_id,
                    args=args,
                    result=result,
                )
                if reconciled_tasks:
                    result["auto_reconciled_tasks"] = [
                        {"title": task.get("title"), "status": task.get("status")}
                        for task in reconciled_tasks[:8]
                    ]
                revision_task = _auto_open_revision_task_for_deliverable(
                    db=db,
                    job_id=job_id,
                    args=args,
                    result=result,
                )
                if revision_task:
                    result["auto_revision_task"] = {
                        "title": revision_task.get("title"),
                        "status": revision_task.get("status"),
                        "key": revision_task.get("key"),
                    }
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


def _is_continuable_recoverable_input_block(execution: StepExecution) -> bool:
    result = execution.result if isinstance(execution.result, dict) else {}
    error = str(result.get("error") or "")
    if execution.status != "blocked" or result.get("recoverable") is not True:
        return False
    if error in {"missing required tool arguments", "placeholder tool arguments"}:
        return bool(result.get("missing_arguments") or result.get("placeholder_arguments"))
    return error.startswith("artifact not found:") or error == "no active operator context to acknowledge"


def _ordered_tool_calls_for_execution(
    tool_calls: list[ToolCall],
    *,
    job: dict[str, Any],
    recent_steps: list[dict[str, Any]],
) -> list[ToolCall]:
    """Run guard-unblocking calls before branch work when a model batches both."""

    if len(tool_calls) < 2:
        return tool_calls
    if _browser_runtime_unavailable_context(recent_steps) and any(not _is_browser_tool(call.name) for call in tool_calls):
        tool_calls = [call for call in tool_calls if not _is_browser_tool(call.name)]
        if len(tool_calls) < 2:
            return tool_calls
    checkpoint = _auto_checkpoint_accounting_context(job, recent_steps)
    saturated_record_tasks = any(
        call.name == "record_tasks" and _task_queue_saturation_context(job, call.arguments)
        for call in tool_calls
    )
    if not checkpoint and not saturated_record_tasks:
        return tool_calls

    artifact_id = str(checkpoint.get("artifact_id") or "") if checkpoint else ""
    artifact_title = str(checkpoint.get("title") or "") if checkpoint else ""
    checkpoint_read = bool(checkpoint and checkpoint.get("checkpoint_read"))
    accounting_tools = {
        "record_experiment",
        "record_findings",
        "record_lesson",
        "record_memory_graph",
        "record_milestone_validation",
        "record_roadmap",
        "record_source",
        "report_update",
        "write_artifact",
    }

    def priority(call: ToolCall) -> int:
        if checkpoint:
            if call.name in EVIDENCE_CHECKPOINT_RESOLUTION_TOOLS:
                return 0
            if (
                not checkpoint_read
                and call.name == "read_artifact"
                and _read_artifact_call_matches_checkpoint(
                    call.arguments,
                    artifact_id=artifact_id,
                    artifact_title=artifact_title,
                )
            ):
                return 0
        if saturated_record_tasks:
            if call.name == "record_tasks" and _task_queue_saturation_context(job, call.arguments):
                return 2
            if call.name in accounting_tools:
                return 0
        return 1

    ordered = sorted(enumerate(tool_calls), key=lambda item: (priority(item[1]), item[0]))
    return [call for _, call in ordered]


def _registry_tools(registry: ToolRegistry, config: AppConfig) -> list[dict[str, Any]]:
    try:
        return registry.openai_tools(config=config)
    except TypeError:
        return registry.openai_tools()


def _registry_tools_for_step(
    registry: ToolRegistry,
    config: AppConfig,
    recent_steps: list[dict[str, Any]],
    *,
    job: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    tools = _registry_tools(registry, config)
    resolution_tools = _active_obligation_tool_names(job, recent_steps) if job else None
    if resolution_tools:
        tools = [tool for tool in tools if _openai_tool_name(tool) in resolution_tools]
    suppressed_tools = _suppressed_tool_names(job, recent_steps)
    if resolution_tools:
        suppressed_tools -= resolution_tools
    if suppressed_tools:
        tools = [tool for tool in tools if _openai_tool_name(tool) not in suppressed_tools]
    if not _browser_runtime_unavailable_context(recent_steps):
        return tools
    return [tool for tool in tools if not _is_browser_tool(_openai_tool_name(tool))]


def _active_obligation_tool_names(job: dict[str, Any] | None, recent_steps: list[dict[str, Any]]) -> set[str] | None:
    if not job:
        return None
    allowed: set[str] = set()
    checkpoint = _auto_checkpoint_accounting_context(job, recent_steps)
    if checkpoint:
        if not checkpoint.get("checkpoint_read"):
            allowed.add("read_artifact")
        allowed.update(EVIDENCE_CHECKPOINT_PROMPT_TOOLS)
    if _pending_measurement_obligation(job):
        allowed.update(MEASUREMENT_RESOLUTION_TOOLS)
    if _experiment_next_action_failure_context(job, recent_steps):
        allowed.update(MEASUREMENT_RESOLUTION_TOOLS)
    measured_progress = _measured_progress_guard_context(job, recent_steps)
    if measured_progress:
        allowed.update(MEASUREMENT_RESOLUTION_TOOLS)
        if _as_int(measured_progress.get("shell_actions_since_last_experiment")) < MEASURABLE_ACTION_BUDGET_STEPS:
            allowed.add("shell_exec")
    if _pending_file_validation_obligation(job):
        allowed.update(FILE_VALIDATION_RESOLUTION_TOOLS)
    return allowed or None


def _suppressed_tool_names(job: dict[str, Any] | None, recent_steps: list[dict[str, Any]]) -> set[str]:
    if not job:
        return set()
    suppressed: set[str] = set()
    if _repeated_task_queue_saturation_context(recent_steps):
        suppressed.add("record_tasks")
    if not _has_acknowledgeable_operator_context(job):
        suppressed.add("acknowledge_operator_context")
    return suppressed


def _has_acknowledgeable_operator_context(job: dict[str, Any]) -> bool:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    messages = metadata.get("operator_messages") if isinstance(metadata.get("operator_messages"), list) else []
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        mode = str(entry.get("mode") or "steer").strip().lower().replace("-", "_")
        if mode not in {"steer", "follow_up"}:
            continue
        if not entry.get("claimed_at"):
            continue
        if entry.get("acknowledged_at") or entry.get("superseded_at"):
            continue
        return True
    return False


def _openai_tool_name(tool: dict[str, Any]) -> str:
    function = tool.get("function") if isinstance(tool, dict) else None
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(tool.get("name") or "") if isinstance(tool, dict) else ""


def _call_next_action_with_timeout(
    llm: StepLLM,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    timeout_seconds: float,
) -> LLMResponse:
    timeout = max(0.0, float(timeout_seconds or 0.0))
    if timeout <= 0 or threading.current_thread() is not threading.main_thread():
        return llm.next_action(messages=messages, tools=tools)

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def _raise_timeout(_signum: int, _frame: Any) -> None:
        raise TimeoutError(f"model call timed out after {timeout:g}s")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return llm.next_action(messages=messages, tools=tools)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            elapsed = max(0.0, time.monotonic() - started)
            remaining = max(0.001, previous_timer[0] - elapsed)
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


def _tool_repair_messages(messages: list[dict[str, Any]], response: LLMResponse) -> list[dict[str, Any]]:
    content = str(response.content or "").strip()
    if len(content) > 2000:
        content = content[:2000] + " ..."
    repair_prompt = (
        "Your previous worker response did not call a tool. This worker must advance by calling exactly "
        "one available tool now. Do not answer in prose. Choose one bounded action that fits the current "
        "state, such as executing existing work, recording a measurement, updating an existing task, "
        "saving an evidence-backed output, recording a lesson/finding/source, or deferring only for a real wait."
    )
    repaired = list(messages)
    if content:
        repaired.append({"role": "assistant", "content": content})
    repaired.append({"role": "user", "content": repair_prompt})
    return repaired


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
        if _clear_stale_task_backlog_pressure(db, job_id, job):
            job = db.get_job(job_id)
        run_id = db.start_run(job_id, model=config.model.model)
        _emit_loop_start(db, job_id, run_id)
        recent_steps = db.list_steps(job_id=job_id)
        if _refresh_contradicted_negative_claims(db, job_id, job, recent_steps):
            job = db.get_job(job_id)
        model_config = config.model
        if _should_reflect(job, recent_steps):
            return _run_reflection_step(job, recent_steps, db=db, job_id=job_id, run_id=run_id)
        guard_recovery = _repeated_guard_block_context(recent_steps)
        if guard_recovery:
            return _run_guard_recovery_step(guard_recovery, db=db, job_id=job_id, run_id=run_id)
        active_operator_messages = _claim_operator_queue(db, job_id)
        if active_operator_messages:
            job = db.get_job(job_id)
        usage = db.job_token_usage(job_id)
        usage_budget_limit = _usage_budget_limit_context(config, usage)
        if usage_budget_limit:
            return _run_usage_budget_limit_step(
                usage_budget_limit,
                db=db,
                job_id=job_id,
                run_id=run_id,
            )
        messages = build_messages(
            job,
            recent_steps,
            memory_entries=db.list_memory(job_id),
            program_text=_load_program_text(config, job_id),
            timeline_events=db.list_timeline_events(job_id, limit=30),
            active_operator_messages=active_operator_messages,
            include_unclaimed_operator_messages=True,
            token_usage=usage,
        )
        llm = llm or OpenAIChatLLM(model_config)
        llm_started = time.monotonic()
        try:
            response: LLMResponse = _call_next_action_with_timeout(
                llm,
                messages=messages,
                tools=_registry_tools_for_step(registry, config, recent_steps, job=job),
                timeout_seconds=model_config.request_timeout_seconds,
            )
        except Exception as exc:
            llm_duration_seconds = round(max(0.0, time.monotonic() - llm_started), 3)
            step_id = db.add_step(
                job_id=job_id,
                run_id=run_id,
                kind="llm",
                status="failed",
                summary=f"model call failed: {type(exc).__name__}",
                input_data={
                    "model": config.model.model,
                    "duration_seconds": llm_duration_seconds,
                    "request_timeout_seconds": model_config.request_timeout_seconds,
                },
            )
            result = _error_result(exc)
            result["duration_seconds"] = llm_duration_seconds
            hard_failure_note = _hard_llm_provider_failure_note(exc)
            if hard_failure_note:
                result["provider_action_required"] = True
                result["pause_reason"] = "llm_provider_blocked"
                db.update_job_status(
                    job_id,
                    "paused",
                    metadata_patch={
                        "last_note": hard_failure_note,
                        "provider_blocked_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                db.append_agent_update(
                    job_id,
                    hard_failure_note,
                    category="error",
                    metadata={"reason": "llm_provider_blocked", "error_type": type(exc).__name__},
                )
            db.finish_step(step_id, status="failed", output_data=result, error=str(exc))
            db.finish_run(run_id, "failed", error=str(exc))
            _emit_loop_end(db, job_id, run_id, status="failed", step_id=step_id, detail=str(exc))
            refresh_memory_index(db, job_id)
            return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=None, status="failed", result=result)

        llm_duration_seconds = round(max(0.0, time.monotonic() - llm_started), 3)
        job = db.get_job(job_id)
        usage = _emit_assistant_message_event(
            db,
            job_id,
            run_id,
            response,
            messages=messages,
            context_length=config.model.context_length,
            duration_seconds=llm_duration_seconds,
        )
        emit_context_pressure_update(db, job_id, usage)
        emit_usage_pressure_update(db, job_id, db.job_token_usage(job_id))

        tool_repair_attempted = False
        tool_repair_error: dict[str, Any] | None = None
        original_content = response.content
        if not response.tool_calls and getattr(llm, "tool_repair", False):
            tool_repair_attempted = True
            repair_messages = _tool_repair_messages(messages, response)
            repair_started = time.monotonic()
            try:
                repair_response = _call_next_action_with_timeout(
                    llm,
                    messages=repair_messages,
                    tools=_registry_tools_for_step(registry, config, recent_steps, job=job),
                    timeout_seconds=model_config.request_timeout_seconds,
                )
            except Exception as exc:
                tool_repair_error = _error_result(exc)
                tool_repair_error["duration_seconds"] = round(max(0.0, time.monotonic() - repair_started), 3)
            else:
                repair_duration_seconds = round(max(0.0, time.monotonic() - repair_started), 3)
                repair_usage = _emit_assistant_message_event(
                    db,
                    job_id,
                    run_id,
                    repair_response,
                    messages=repair_messages,
                    context_length=config.model.context_length,
                    duration_seconds=repair_duration_seconds,
                )
                emit_context_pressure_update(db, job_id, repair_usage)
                emit_usage_pressure_update(db, job_id, db.job_token_usage(job_id))
                if repair_response.tool_calls:
                    response = repair_response

        if response.tool_calls:
            executions: list[StepExecution] = []
            details: list[str] = []
            run_error: str | None = None
            ordered_tool_calls = _ordered_tool_calls_for_execution(
                response.tool_calls,
                job=db.get_job(job_id),
                recent_steps=db.list_steps(job_id=job_id),
            )
            for index, call in enumerate(ordered_tool_calls):
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
                    if index < len(ordered_tool_calls) - 1 and _is_continuable_recoverable_input_block(execution):
                        details.append(f"continued after recoverable {call.name} input block")
                        continue
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
            status="blocked",
            summary="worker returned content without a tool call",
            input_data={},
        )
        result = {
            "success": False,
            "recoverable": True,
            "error": "worker tool call required",
            "content": response.content,
            "original_content": original_content,
            "tool_repair_attempted": tool_repair_attempted,
            "tool_repair_error": tool_repair_error,
            "next": (
                "Worker turns must use a tool call. Continue by choosing one bounded action such as "
                "record_tasks, report_update, write_artifact, write_file, shell_exec, record_findings, "
                "record_source, record_experiment, record_lesson, or defer_job."
            ),
        }
        db.append_agent_update(
            job_id,
            "Worker returned a message without a tool call; continuing with a tool-action recovery constraint.",
            category="blocked",
            metadata={"reason": "worker_tool_call_required", "step_id": step_id},
        )
        db.finish_step(
            step_id,
            status="blocked",
            summary="blocked assistant-only worker turn; tool call required",
            output_data=result,
            error="worker tool call required",
        )
        db.finish_run(run_id, "blocked", error="worker tool call required")
        _emit_loop_end(
            db,
            job_id,
            run_id,
            status="blocked",
            step_id=step_id,
            detail="worker tool call required",
        )
        refresh_memory_index(db, job_id)
        return StepExecution(job_id=job_id, run_id=run_id, step_id=step_id, tool_name=None, status="blocked", result=result)
    finally:
        if owns_db:
            db.close()
