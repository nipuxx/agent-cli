"""Static worker prompt and loop policy constants."""

from __future__ import annotations

import re


REFLECTION_INTERVAL_STEPS = 12
WORKER_PROTOCOL_VERSION = "2026-05-01-contract-first-v1"

SYSTEM_PROMPT = """You are a long-running local work agent.

Operate as a bounded worker, not a chat assistant. Choose one useful next step,
call one of the available tools, and persist important evidence as artifacts.
Do not claim the whole job is complete. A strong result is only a checkpoint:
save it, report it, add the next tasks, and continue improving or broadening.

Use a contract-first durable cycle. Read the objective, operator context,
roadmap, active task, and recent evidence; choose the next action that satisfies
the active output contract; produce or measure concrete evidence; update the
right ledger; report the checkpoint; then open or continue the next branch.
Research is only one possible contract. For action, experiment, monitor, report,
or file-deliverable work, prefer execution, measurement, validation, or writing
over more background collection. Keep moving forever until the operator pauses
or cancels the job.
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
Use write_file when the objective requires a concrete file deliverable, source
file, document, config, dataset, or other workspace output. If a measured
experiment says the next action is to write, merge, update, compile, or insert
content, prefer write_file or an execution command that actually changes the
target over more read-only inspection.
Use defer_job when the next useful step is to wait for an external process,
scheduled check, cooldown, long-running command, or monitor interval. Do not
simulate waiting with repeated searches, reports, or shell probes.
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
Use record_roadmap for broad, multi-phase, or ambiguous objectives that need a
higher-level orchestration plan. A roadmap is generic: milestones group related
features or work units; each milestone has acceptance criteria, evidence needed,
and a validation contract. Use record_milestone_validation at milestone checkpoints
to pass, fail, block, or create follow-up tasks from validation gaps. Keep the
roadmap compact and update it from durable evidence, not from activity count.
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
write_file writes workspace/local files directly; write_artifact writes Nipux's
separate saved-output store. Use the right one for the operator-facing result.
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

ARTIFACT_REVIEW_TOOLS = {"read_artifact", "search_artifacts"}
BRANCH_WORK_TOOLS = INFORMATION_GATHERING_TOOLS | ARTIFACT_REVIEW_TOOLS | {"shell_exec"}
LEDGER_PROGRESS_TOOLS = {
    "guard_recovery",
    "record_findings",
    "record_source",
    "record_tasks",
    "record_roadmap",
    "record_milestone_validation",
    "record_experiment",
    "record_lesson",
}
MEASUREMENT_RESOLUTION_TOOLS = {"record_experiment", "record_lesson", "record_tasks", "record_milestone_validation", "acknowledge_operator_context"}
ARTIFACT_ACCOUNTING_RESOLUTION_TOOLS = LEDGER_PROGRESS_TOOLS | {"acknowledge_operator_context"}
ARTIFACT_ACCOUNTING_BLOCKED_TOOLS = INFORMATION_GATHERING_TOOLS | {
    "shell_exec",
    "write_file",
    "write_artifact",
    "read_artifact",
    "search_artifacts",
    "report_update",
}
MEASUREMENT_BLOCKED_TOOLS = INFORMATION_GATHERING_TOOLS | {
    "shell_exec",
    "write_file",
    "write_artifact",
    "record_findings",
    "record_source",
    "report_update",
}
MILESTONE_VALIDATION_BLOCKED_TOOLS = INFORMATION_GATHERING_TOOLS | {
    "shell_exec",
    "write_file",
    "write_artifact",
    "record_findings",
    "record_source",
    "record_experiment",
    "report_update",
}
ROADMAP_STALENESS_BLOCKED_TOOLS = INFORMATION_GATHERING_TOOLS | {
    "shell_exec",
    "write_file",
    "write_artifact",
    "record_findings",
    "record_source",
    "record_tasks",
    "record_experiment",
    "report_update",
}
CHURN_TOOLS = INFORMATION_GATHERING_TOOLS | ARTIFACT_REVIEW_TOOLS | {"shell_exec"}
ACTIVITY_STAGNATION_BLOCKED_TOOLS = CHURN_TOOLS | {"write_artifact", "write_file", "report_update"}
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
RECOVERABLE_GUARD_ERRORS = {
    "artifact search loop blocked",
    "durable progress required",
    "duplicate tool call blocked",
    "experiment next action pending",
    "known bad source blocked",
    "measurement obligation pending",
    "measured progress required",
    "progress accounting required",
    "progress ledger update required",
    "similar artifact search blocked",
    "similar search query blocked",
    "task branch required before more work",
    "task queue saturated",
}
MEASURABLE_RESEARCH_BUDGET_STEPS = 18
MEASURABLE_ACTION_BUDGET_STEPS = 4
ACTIVITY_STAGNATION_CHECKPOINTS = 3
TASK_QUEUE_SATURATION_OPEN_TASKS = 40
PROGRAM_PROMPT_CHARS = 2000
MEMORY_ENTRY_PROMPT_CHARS = 700
MEMORY_PROMPT_CHARS = 1800
RECENT_STATE_STEPS = 5
RECENT_STATE_PROMPT_CHARS = 3000
TIMELINE_PROMPT_EVENTS = 8
SECTION_ITEM_CHARS = 420
MAX_WORKER_PROMPT_CHARS = 18_000
TIMELINE_PROMPT_EVENT_TYPES = {
    "agent_message",
    "artifact",
    "error",
    "experiment",
    "finding",
    "lesson",
    "milestone_validation",
    "reflection",
    "roadmap",
    "source",
    "task",
}
TIMELINE_PROMPT_AGENT_TITLES = {"blocked", "error", "plan", "progress", "report", "update"}
TIMELINE_PROMPT_TOOL_STATUSES = {"blocked", "failed"}
PROMPT_SECTION_BUDGETS = {
    "Workspace": 520,
    "Operator context": 2_200,
    "Pending measurement obligation": 1_100,
    "Measured progress guard": 1_000,
    "Progress accounting guard": 900,
    "Activity stagnation": 900,
    "Program": 1_400,
    "Lessons learned": 1_100,
    "Roadmap": 2_000,
    "Task queue": 2_400,
    "Ledgers": 2_400,
    "Experiment ledger": 2_200,
    "Reflections": 900,
    "Compact memory": 1_100,
    "Recent visible timeline": 1_000,
    "Recent state": 1_800,
    "Next-action constraint": 1_100,
}

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
TEXT_TOKEN_STOPWORDS = {
    "and",
    "are",
    "for",
    "from",
    "into",
    "that",
    "the",
    "this",
    "with",
}

EVIDENCE_ARTIFACT_TERMS = {
    "audit",
    "checkpoint",
    "evidence",
    "extract",
    "extracted",
    "notes",
    "source",
    "sources",
}
DELIVERABLE_ARTIFACT_TERMS = {
    "compiled",
    "deliverable",
    "draft",
    "final",
    "revision",
    "updated",
}
TASK_DELIVERABLE_ACTION_TERMS = {
    "add",
    "append",
    "compile",
    "create",
    "edit",
    "insert",
    "polish",
    "rewrite",
    "update",
    "write",
}
EXPERIMENT_DELIVERY_ACTION_TERMS = {
    "append",
    "apply",
    "build",
    "compile",
    "create",
    "edit",
    "finish",
    "fix",
    "generate",
    "implement",
    "insert",
    "merge",
    "patch",
    "produce",
    "publish",
    "replace",
    "rewrite",
    "save",
    "update",
    "write",
}
EXPERIMENT_INFORMATION_ACTION_TERMS = {
    "audit",
    "collect",
    "extract",
    "find",
    "gather",
    "inspect",
    "read",
    "research",
    "review",
    "search",
    "source",
    "survey",
}
EXPERIMENT_NEXT_ACTION_BLOCKED_TOOLS = INFORMATION_GATHERING_TOOLS | {"report_update"}
READ_ONLY_SHELL_COMMAND_PATTERN = re.compile(
    r"(?is)^\s*(?:"
    r"awk\b|cat\b|df\b|du\b|echo\b|find\b|git\s+(?:diff|grep|log|ls-files|show|status)\b|"
    r"grep\b|head\b|ls\b|pwd\b|rg\b|sed\s+-n\b|stat\b|tail\b|tree\b|wc\b"
    r")"
)

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
