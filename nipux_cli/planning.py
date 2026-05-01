"""Generic initial planning primitives for long-running jobs."""

from __future__ import annotations

import re
from typing import Any


_PROFILE_TERMS: dict[str, set[str]] = {
    "measured": {
        "accelerate",
        "benchmark",
        "compare",
        "decrease",
        "faster",
        "improve",
        "increase",
        "latency",
        "measure",
        "metric",
        "optimize",
        "performance",
        "reduce",
        "score",
        "speed",
        "test",
        "throughput",
    },
    "deliverable": {
        "article",
        "artifact",
        "checklist",
        "create",
        "deck",
        "doc",
        "document",
        "draft",
        "file",
        "generate",
        "guide",
        "manual",
        "memo",
        "outline",
        "paper",
        "produce",
        "presentation",
        "report",
        "spec",
        "template",
        "write",
    },
    "monitor": {
        "alert",
        "check",
        "observe",
        "periodic",
        "reporting",
        "track",
        "watch",
        "monitor",
    },
    "implementation": {
        "automate",
        "build",
        "change",
        "code",
        "debug",
        "deploy",
        "fix",
        "implement",
        "install",
        "repair",
        "run",
        "setup",
    },
    "research": {
        "analyze",
        "compare",
        "explore",
        "find",
        "investigate",
        "learn",
        "map",
        "research",
        "review",
        "summarize",
        "survey",
    },
}
_PROFILE_PRIORITY = {
    "measured": 0,
    "monitor": 1,
    "implementation": 2,
    "deliverable": 3,
    "research": 4,
}


def objective_profiles(objective: str) -> list[str]:
    """Infer generic work profiles from an objective without binding to a domain."""

    tokens = set(re.findall(r"[a-z][a-z0-9_-]+", objective.lower()))
    scores: list[tuple[int, str]] = []
    for profile, terms in _PROFILE_TERMS.items():
        score = len(tokens & terms)
        if score:
            scores.append((score, profile))
    if not scores:
        return ["general"]
    scores.sort(key=lambda item: (-item[0], _PROFILE_PRIORITY.get(item[1], 99), item[1]))
    profiles = [profile for _score, profile in scores[:2]]
    return profiles or ["general"]


def initial_plan_for_objective(objective: str) -> dict[str, Any]:
    objective_text = " ".join(objective.split())
    profiles = objective_profiles(objective_text)
    tasks = _initial_tasks_for_profiles(profiles)
    questions = _initial_questions_for_profiles(profiles)
    return {
        "status": "needs_operator_review",
        "summary": _initial_summary_for_profiles(profiles),
        "profile": profiles[0],
        "profiles": profiles,
        "tasks": tasks,
        "questions": questions,
        "objective": objective_text,
    }


def initial_task_contract(task_title: str) -> dict[str, str]:
    lowered = task_title.lower()
    if any(word in lowered for word in ("baseline", "benchmark", "compare", "experiment", "measure", "metric", "test")):
        return {
            "output_contract": "experiment",
            "acceptance_criteria": "A baseline, result, comparison, or explicit blocked measurement is recorded.",
            "evidence_needed": "Experiment record with metric, environment or inputs, result direction, and next action.",
            "stall_behavior": "Record why measurement is blocked and create the smallest follow-up task that can obtain it.",
        }
    if any(
        word in lowered
        for word in (
            "article",
            "checklist",
            "draft",
            "deliverable",
            "document",
            "file",
            "generate",
            "guide",
            "manual",
            "paper",
            "produce",
            "report",
            "template",
            "write",
        )
    ):
        return {
            "output_contract": "report",
            "acceptance_criteria": "A durable draft, report, or deliverable section is saved with its evidence status.",
            "evidence_needed": "Saved output plus cited evidence, assumptions, gaps, or review notes.",
            "stall_behavior": "Save the partial output, record the gap, and create the next evidence or revision task.",
        }
    if any(word in lowered for word in ("validate", "review", "decide", "criteria", "constraint", "success")):
        return {
            "output_contract": "decision",
            "acceptance_criteria": "The decision, validation result, or success criteria are explicit.",
            "evidence_needed": "Operator context, durable notes, milestone validation, or task/roadmap updates.",
            "stall_behavior": "Ask for the missing constraint or record a reversible assumption and continue.",
        }
    if any(word in lowered for word in ("monitor", "watch", "track", "check", "observe")):
        return {
            "output_contract": "monitor",
            "acceptance_criteria": "A check cadence, signal, state change, or next observation time is recorded.",
            "evidence_needed": "Monitor result, status update, deferred follow-up, or recorded blocker.",
            "stall_behavior": "Defer the job until the next useful check or pivot to a diagnostic task.",
        }
    if any(word in lowered for word in ("act", "apply", "build", "change", "deploy", "fix", "implement", "install", "run")):
        return {
            "output_contract": "action",
            "acceptance_criteria": "The action produces an observable durable change or a clear blocker.",
            "evidence_needed": "Tool result plus file, artifact, ledger, task, roadmap, or experiment update.",
            "stall_behavior": "Record the blocker and open a smaller follow-up action.",
        }
    if "clarify" in lowered or "criteria" in lowered or "constraint" in lowered:
        return {
            "output_contract": "decision",
            "acceptance_criteria": "Success criteria, constraints, and first branches are explicit.",
            "evidence_needed": "Operator context, durable notes, or an updated roadmap/task queue.",
            "stall_behavior": "Ask for the missing constraint or record a decision with the best current assumption.",
        }
    if "map" in lowered or "research" in lowered or "branch" in lowered:
        return {
            "output_contract": "research",
            "acceptance_criteria": "At least one viable branch is selected and low-value branches are avoided.",
            "evidence_needed": "Source notes, branch rationale, source ledger entries, or saved research output.",
            "stall_behavior": "Record a low-yield lesson and pivot to a different branch.",
        }
    if "collect" in lowered or "evidence" in lowered or "save" in lowered or "output" in lowered:
        return {
            "output_contract": "artifact",
            "acceptance_criteria": "A durable output is saved and linked to the task or ledger.",
            "evidence_needed": "Artifact, file output, finding record, source record, or experiment record.",
            "stall_behavior": "Record what evidence is missing and create the next evidence-producing task.",
        }
    if "reflect" in lowered or "memory" in lowered or "continue" in lowered:
        return {
            "output_contract": "monitor",
            "acceptance_criteria": "Progress is evaluated from durable deltas and the next branch is chosen.",
            "evidence_needed": "Reflection, lesson, task update, roadmap validation, or experiment comparison.",
            "stall_behavior": "Record a blocker or pivot when no durable delta was produced.",
        }
    return {
        "output_contract": "action",
        "acceptance_criteria": "The task produces an observable durable change.",
        "evidence_needed": "Tool result plus artifact, ledger, task, roadmap, or experiment update.",
        "stall_behavior": "Record a blocker and open a smaller follow-up task.",
    }


def initial_roadmap_for_objective(*, title: str, objective: str) -> dict[str, Any]:
    profiles = objective_profiles(objective)
    execute_contract = _primary_execution_contract(profiles)
    return {
        "title": title,
        "status": "planned",
        "objective": objective,
        "scope": (
            "Initial roadmap generated from the objective and inferred generic work profile "
            f"({', '.join(profiles)}). Refine this as evidence and operator context arrive."
        ),
        "current_milestone": "Clarify and frame the work",
        "validation_contract": (
            "Each milestone needs observable evidence that its acceptance criteria were met, "
            "or a recorded blocker plus follow-up tasks."
        ),
        "milestones": [
            {
                "title": "Clarify and frame the work",
                "status": "planned",
                "priority": 10,
                "goal": "Turn the objective into concrete success criteria and constraints.",
                "acceptance_criteria": "Success criteria and first branches are explicit.",
                "evidence_needed": "Operator context, planning notes, or a recorded task queue.",
                "features": [{"title": "Capture success criteria", "status": "planned", "output_contract": "decision"}],
            },
            {
                "title": "Execute first durable branches",
                "status": "planned",
                "priority": 8,
                "goal": "Produce artifacts, findings, actions, or measurements that advance the objective.",
                "acceptance_criteria": "At least one branch produces durable evidence.",
                "evidence_needed": "Saved outputs, ledger updates, action results, or experiment records.",
                "features": [
                    {
                        "title": "Run the first evidence-producing branch",
                        "status": "planned",
                        "output_contract": execute_contract,
                    }
                ],
            },
            {
                "title": "Validate and continue",
                "status": "planned",
                "priority": 6,
                "goal": "Check results against acceptance criteria and create follow-up work.",
                "acceptance_criteria": "Validation is passed, failed, or blocked with a next action.",
                "evidence_needed": "record_milestone_validation entry and follow-up tasks if needed.",
                "features": [{"title": "Validate the checkpoint", "status": "planned", "output_contract": "decision"}],
            },
        ],
        "metadata": {"phase": "initial_plan"},
    }


def _initial_summary_for_profiles(profiles: list[str]) -> str:
    primary = profiles[0] if profiles else "general"
    if primary == "measured":
        return "I will start by defining the measurable baseline, then iterate on branches that can prove improvement."
    if primary == "deliverable":
        return "I will start by framing the deliverable, collecting evidence, and saving drafts that can be improved."
    if primary == "monitor":
        return "I will start by defining the watched signals, first check, cadence, and durable update format."
    if primary == "implementation":
        return "I will start by inspecting the current state, planning a small action, and validating the result."
    if primary == "research":
        return "I will start by mapping source branches, collecting evidence, and saving concise findings."
    return "I will turn this objective into a durable long-running job before starting tool work."


def _initial_tasks_for_profiles(profiles: list[str]) -> list[str]:
    tasks: list[str] = ["Clarify success criteria, constraints, and stop conditions."]
    primary = profiles[0] if profiles else "general"
    if primary == "measured":
        tasks.extend(
            [
                "Record the baseline metric and measurement method.",
                "Run the first measurable branch and record an experiment.",
                "Compare the result with the best known baseline and choose the next branch.",
            ]
        )
    elif primary == "deliverable":
        tasks.extend(
            [
                "Map the outline, audience, evidence gaps, and acceptance criteria.",
                "Collect evidence for the first section or deliverable unit.",
                "Save a durable draft or report checkpoint.",
            ]
        )
    elif primary == "monitor":
        tasks.extend(
            [
                "Define watched signals, check cadence, and alert conditions.",
                "Run the first status check and save the observation.",
                "Defer or continue based on the next useful check time.",
            ]
        )
    elif primary == "implementation":
        tasks.extend(
            [
                "Inspect current state and identify the smallest safe action.",
                "Apply one change or execute one action with observable output.",
                "Validate the result and record any follow-up branch.",
            ]
        )
    else:
        tasks.extend(
            [
                "Map the first research or execution branches.",
                "Collect evidence and save outputs as files.",
                "Reflect on what worked, update memory, and continue with the next branch.",
            ]
        )
    tasks.append("Publish a concise progress update and keep working on the next useful branch.")
    return tasks


def _initial_questions_for_profiles(profiles: list[str]) -> list[str]:
    questions = [
        "What result would make this job successful?",
        "Are there constraints, risks, or approaches I should avoid?",
    ]
    primary = profiles[0] if profiles else "general"
    if primary == "measured":
        questions.insert(1, "What metric should be treated as the primary measure of progress?")
    elif primary == "deliverable":
        questions.insert(1, "Who is the audience, and what quality bar should the deliverable meet?")
    elif primary == "monitor":
        questions.insert(1, "How often should I check, and what change should trigger a report?")
    elif primary == "implementation":
        questions.insert(1, "Which environment or files are in scope, and what requires approval?")
    else:
        questions.insert(1, "Which sources, artifacts, or signals should I trust most?")
    questions.append("Should this run aggressively in the background or wait for review between branches?")
    return questions


def _primary_execution_contract(profiles: list[str]) -> str:
    if "measured" in profiles:
        return "experiment"
    if "deliverable" in profiles:
        return "report"
    if "monitor" in profiles:
        return "monitor"
    if "implementation" in profiles:
        return "action"
    if "research" in profiles:
        return "research"
    return "artifact"


def format_initial_plan(plan: dict[str, Any]) -> str:
    tasks = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    questions = plan.get("questions") if isinstance(plan.get("questions"), list) else []
    lines = [str(plan.get("summary") or "Initial plan created.")]
    if tasks:
        lines.append("Plan:")
        lines.extend(f"- {task}" for task in tasks)
    if questions:
        lines.append("Questions:")
        lines.extend(f"- {question}" for question in questions)
    lines.append("Reply with answers, or use the right-side Run control when this plan is good enough to start.")
    return "\n".join(lines)
