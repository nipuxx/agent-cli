"""Generic initial planning primitives for long-running jobs."""

from __future__ import annotations

from typing import Any


def initial_plan_for_objective(objective: str) -> dict[str, Any]:
    objective_text = " ".join(objective.split())
    return {
        "status": "needs_operator_review",
        "summary": "I will turn this objective into a durable long-running job before starting tool work.",
        "tasks": [
            "Clarify the exact success criteria and constraints.",
            "Map the first research or execution branches.",
            "Collect evidence and save outputs as files.",
            "Reflect on what worked, update memory, and continue with the next branch.",
        ],
        "questions": [
            "What result would make this job successful?",
            "Are there sources, actions, or approaches I should avoid?",
            "Should this run aggressively in the background or wait for review between branches?",
        ],
        "objective": objective_text,
    }


def initial_task_contract(task_title: str) -> dict[str, str]:
    lowered = task_title.lower()
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
    return {
        "title": title,
        "status": "planned",
        "objective": objective,
        "scope": "Initial roadmap generated from the objective. Refine this as evidence and operator context arrive.",
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
                "evidence_needed": "Saved outputs plus ledger updates.",
                "features": [
                    {"title": "Run the first evidence-producing branch", "status": "planned", "output_contract": "artifact"}
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
