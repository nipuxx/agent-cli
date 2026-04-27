"""Program templates for generic long-running jobs."""

from __future__ import annotations


def program_for_job(*, kind: str, title: str, objective: str) -> str:
    kind = (kind or "generic").strip().lower()
    body = _TEMPLATES.get(kind, _generic_template)
    return body(title=title, objective=objective).strip() + "\n"


def _generic_template(*, title: str, objective: str) -> str:
    return f"""# {title}

## Objective

{objective}

## Operating Rules

- Work forever in bounded, resumable steps until the operator explicitly cancels or pauses the job.
- Treat useful results as checkpoints, not endings: save the result, create the next branch, and continue.
- Save important observations as artifacts.
- Use report_update for short progress notes or blocked-state notes.
- Use record_lesson when a source, mistake, operator preference, or strategy should affect future steps.
- Use record_source and record_findings when those tools are available so the job improves its ledgers over time.
- Use record_tasks to split broad objectives into durable branches with output contracts, acceptance criteria, required evidence, and stall behavior.
- Use record_experiment whenever a branch produces measured results, comparisons, benchmarks, scores, or optimization data.
- Use acknowledge_operator_context after incorporating or superseding active operator steering.
- Use browser and web tools first. Do not assume memory is exact unless it points to an artifact.
- Prefer quantity of attempts over one giant plan.
"""


def _research_paper_template(*, title: str, objective: str) -> str:
    return f"""# {title}

## Objective

{objective}

## Research Rules

- Save exact source URLs and extracted text snippets as artifacts.
- Keep a rolling citation map with claims, evidence, and open questions.
- Separate facts from hypotheses.
- Produce drafts only after evidence artifacts exist.
- Use report_update for brief progress, gap, or blocked-source notes.
- Use record_tasks to track source clusters, sections, and unresolved evidence gaps with output contracts and acceptance criteria.
- Use acknowledge_operator_context after incorporating or superseding active operator steering.

## Step Loop

1. Search for one source cluster.
2. Extract and save relevant evidence.
3. Update the citation/evidence map.
4. Write or improve one section when enough evidence exists.
"""


_TEMPLATES = {
    "research_paper": _research_paper_template,
}
