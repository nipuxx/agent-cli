"""Read-only CLI commands for job records, ledgers, memory, and usage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from nipux_cli.cli_render import job_ref_text as default_job_ref_text
from nipux_cli.cli_render import json_default, rule
from nipux_cli.daemon import daemon_lock_status
from nipux_cli.tui_status import active_operator_messages, worker_label
from nipux_cli.tui_style import _one_line
from nipux_cli.usage import format_usage_report


@dataclass(frozen=True)
class RecordCommandDeps:
    db_factory: Callable[[], tuple[Any, Any]]
    resolve_job_id: Callable[[Any, Any], str | None]
    job_ref_text: Callable[[Any], str] = default_job_ref_text


def cmd_findings_impl(args: Any, deps: RecordCommandDeps) -> None:
    db, _ = deps.db_factory()
    try:
        job_id = _resolve_or_print(db, args, deps)
        if not job_id:
            return
        job = db.get_job(job_id)
        findings = _metadata_records(job, "finding_ledger")
        if args.json:
            print(json.dumps(findings, ensure_ascii=False, indent=2, default=json_default))
            return
        print(f"findings {job['title']} | {len(findings)} unique")
        print(rule("="))
        if not findings:
            print("none yet")
            return
        ranked = sorted(findings, key=lambda finding: float(finding.get("score") or 0), reverse=True)
        for index, finding in enumerate(ranked[: args.limit], start=1):
            score = finding.get("score")
            score_text = f" score={score:g}" if isinstance(score, (int, float)) else ""
            print(f"{index:>2}. {_one_line(finding.get('name') or 'unknown', 54)}{score_text}")
            details = " | ".join(
                value
                for value in [
                    str(finding.get("location") or "").strip(),
                    str(finding.get("category") or "").strip(),
                    str(finding.get("status") or "").strip(),
                ]
                if value
            )
            if details:
                print(f"    {details}")
            if finding.get("url") or finding.get("source_url"):
                print(f"    {finding.get('url') or finding.get('source_url')}")
            if finding.get("reason"):
                print(f"    {_one_line(finding['reason'], args.chars)}")
    finally:
        db.close()


def cmd_tasks_impl(args: Any, deps: RecordCommandDeps) -> None:
    db, _ = deps.db_factory()
    try:
        job_id = _resolve_or_print(db, args, deps)
        if not job_id:
            return
        job = db.get_job(job_id)
        tasks = _metadata_records(job, "task_queue")
        if args.status:
            wanted = {status.strip().lower() for status in args.status}
            tasks = [task for task in tasks if str(task.get("status") or "open").lower() in wanted]
        if args.json:
            print(json.dumps(tasks, ensure_ascii=False, indent=2, default=json_default))
            return
        status_order = {"active": 0, "open": 1, "blocked": 2, "done": 3, "skipped": 4}
        ranked = sorted(
            tasks,
            key=lambda task: (
                status_order.get(str(task.get("status") or "open"), 9),
                -int(task.get("priority") or 0),
                str(task.get("title") or ""),
            ),
        )
        print(f"tasks {job['title']} | {len(ranked)} tracked")
        print(rule("="))
        if not ranked:
            print("none yet")
            return
        for index, task in enumerate(ranked[: args.limit], start=1):
            status = str(task.get("status") or "open")
            priority = int(task.get("priority") or 0)
            print(f"{index:>2}. {status:<7} p={priority:<3} {_one_line(task.get('title') or 'untitled', 54)}")
            details = " | ".join(
                value
                for value in [
                    f"contract={task.get('output_contract')}" if task.get("output_contract") else "",
                    f"accept={task.get('acceptance_criteria')}" if task.get("acceptance_criteria") else "",
                    f"evidence={task.get('evidence_needed')}" if task.get("evidence_needed") else "",
                    f"stall={task.get('stall_behavior')}" if task.get("stall_behavior") else "",
                    str(task.get("goal") or "").strip(),
                    str(task.get("source_hint") or "").strip(),
                    str(task.get("result") or "").strip(),
                ]
                if value
            )
            if details:
                print(f"    {_one_line(details, args.chars)}")
    finally:
        db.close()


def cmd_roadmap_impl(args: Any, deps: RecordCommandDeps) -> None:
    db, _ = deps.db_factory()
    try:
        job_id = _resolve_or_print(db, args, deps)
        if not job_id:
            return
        job = db.get_job(job_id)
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
        if args.json:
            print(json.dumps(roadmap, ensure_ascii=False, indent=2, default=json_default))
            return
        print(f"roadmap {job['title']}")
        print(rule("="))
        if not roadmap:
            print("none yet")
            print("the worker can create one with record_roadmap when broad work needs milestones")
            return
        milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
        print(f"title: {roadmap.get('title') or 'Roadmap'}")
        print(f"status: {roadmap.get('status') or 'planned'} | milestones: {len(milestones)}")
        if roadmap.get("current_milestone"):
            print(f"current: {_one_line(roadmap.get('current_milestone') or '', args.chars)}")
        if roadmap.get("scope"):
            print(f"scope: {_one_line(roadmap.get('scope') or '', args.chars)}")
        if roadmap.get("validation_contract"):
            print(f"validation: {_one_line(roadmap.get('validation_contract') or '', args.chars)}")
        _print_milestones(milestones, limit=args.limit, features=args.features, chars=args.chars)
    finally:
        db.close()


def cmd_experiments_impl(args: Any, deps: RecordCommandDeps) -> None:
    db, _ = deps.db_factory()
    try:
        job_id = _resolve_or_print(db, args, deps)
        if not job_id:
            return
        job = db.get_job(job_id)
        experiments = _metadata_records(job, "experiment_ledger")
        if args.status:
            wanted = {status.strip().lower() for status in args.status}
            experiments = [
                experiment for experiment in experiments if str(experiment.get("status") or "planned").lower() in wanted
            ]
        if args.json:
            print(json.dumps(experiments, ensure_ascii=False, indent=2, default=json_default))
            return
        status_order = {"running": 0, "planned": 1, "measured": 2, "blocked": 3, "failed": 4, "skipped": 5}
        ranked = sorted(
            experiments,
            key=lambda experiment: (
                not bool(experiment.get("best_observed")),
                status_order.get(str(experiment.get("status") or "planned"), 9),
                str(experiment.get("updated_at") or experiment.get("created_at") or ""),
            ),
        )
        print(f"experiments {job['title']} | {len(ranked)} tracked")
        print(rule("="))
        if not ranked:
            print("none yet")
            return
        for index, experiment in enumerate(ranked[: args.limit], start=1):
            status = str(experiment.get("status") or "planned")
            best = " *best*" if experiment.get("best_observed") else ""
            metric = ""
            if experiment.get("metric_value") is not None:
                metric = (
                    f" {experiment.get('metric_name') or 'metric'}="
                    f"{experiment.get('metric_value')}{experiment.get('metric_unit') or ''}"
                )
            print(f"{index:>2}. {status:<8} {_one_line(experiment.get('title') or 'experiment', 54)}{metric}{best}")
            details = " | ".join(
                value
                for value in [
                    str(experiment.get("result") or "").strip(),
                    f"next: {experiment.get('next_action')}" if experiment.get("next_action") else "",
                    f"delta: {experiment.get('delta_from_previous_best')}"
                    if experiment.get("delta_from_previous_best") is not None
                    else "",
                ]
                if value
            )
            if details:
                print(f"    {_one_line(details, args.chars)}")
    finally:
        db.close()


def cmd_sources_impl(args: Any, deps: RecordCommandDeps) -> None:
    db, _ = deps.db_factory()
    try:
        job_id = _resolve_or_print(db, args, deps)
        if not job_id:
            return
        job = db.get_job(job_id)
        sources = _metadata_records(job, "source_ledger")
        if args.json:
            print(json.dumps(sources, ensure_ascii=False, indent=2, default=json_default))
            return
        ranked = sorted(
            sources,
            key=lambda source: (float(source.get("usefulness_score") or 0), int(source.get("yield_count") or 0)),
            reverse=True,
        )
        print(f"sources {job['title']} | {len(sources)} scored")
        print(rule("="))
        if not ranked:
            print("none yet")
            return
        for index, source in enumerate(ranked[: args.limit], start=1):
            score = float(source.get("usefulness_score") or 0)
            print(
                f"{index:>2}. {_one_line(source.get('source') or 'unknown', 58)} "
                f"score={score:g} findings={source.get('yield_count') or 0} fails={source.get('fail_count') or 0}"
            )
            detail = " | ".join(
                value
                for value in [
                    str(source.get("source_type") or "").strip(),
                    str(source.get("last_outcome") or "").strip(),
                ]
                if value
            )
            if detail:
                print(f"    {_one_line(detail, args.chars)}")
            warnings = source.get("warnings") if isinstance(source.get("warnings"), list) else []
            if warnings:
                print(f"    warnings: {_one_line(', '.join(str(item) for item in warnings[-3:]), args.chars)}")
    finally:
        db.close()


def cmd_memory_impl(args: Any, deps: RecordCommandDeps) -> None:
    db, _ = deps.db_factory()
    try:
        job_id = _resolve_or_print(db, args, deps)
        if not job_id:
            return
        job = db.get_job(job_id)
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        lessons = _metadata_records(job, "lessons")
        reflections = _metadata_records(job, "reflections")
        compact = db.list_memory(job_id)
        active_operator = active_operator_messages(metadata)
        pending_measurement = (
            metadata.get("pending_measurement_obligation")
            if isinstance(metadata.get("pending_measurement_obligation"), dict)
            else {}
        )
        print(f"memory {job['title']}")
        print(rule("="))
        print(f"lessons={len(lessons)} reflections={len(reflections)} compact_entries={len(compact)}")
        _print_memory_sections(
            active_operator=active_operator,
            pending_measurement=pending_measurement,
            reflections=reflections,
            lessons=lessons,
            compact=compact,
            limit=args.limit,
            chars=args.chars,
        )
    finally:
        db.close()


def cmd_metrics_impl(args: Any, deps: RecordCommandDeps) -> None:
    db, config = deps.db_factory()
    try:
        job_id = _resolve_or_print(db, args, deps)
        if not job_id:
            return
        job = db.get_job(job_id)
        steps = db.list_steps(job_id=job_id)
        artifacts = db.list_artifacts(job_id, limit=1000)
        findings = _metadata_records(job, "finding_ledger")
        sources = _metadata_records(job, "source_ledger")
        tasks = _metadata_records(job, "task_queue")
        experiments = _metadata_records(job, "experiment_ledger")
        lessons = _metadata_records(job, "lessons")
        reflections = _metadata_records(job, "reflections")
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
        milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
        daemon = daemon_lock_status(config.runtime.home / "agentd.lock")
        finding_batches = [
            artifact
            for artifact in artifacts
            if "finding" in str(artifact.get("title") or artifact.get("summary") or "").lower()
        ]
        blocked = [step for step in steps if step.get("status") == "blocked"]
        failed = [step for step in steps if step.get("status") == "failed"]
        print(f"metrics {job['title']}")
        print(rule("="))
        print(f"daemon: {'running' if daemon['running'] else 'stopped'} | worker: {worker_label(job, bool(daemon['running']))}")
        print(f"steps: {_step_count(steps)} | failed: {len(failed)} | blocked/recovered: {len(blocked)}")
        print(f"artifacts: {len(artifacts)} | finding_batches: {len(finding_batches)}")
        print(
            f"findings: {len(findings)} | sources: {len(sources)} | tasks: {len(tasks)} | "
            f"milestones: {len(milestones)} | experiments: {len(experiments)} | "
            f"lessons: {len(lessons)} | reflections: {len(reflections)}"
        )
        _print_best_records(sources=sources, findings=findings, experiments=experiments, chars=args.chars)
    finally:
        db.close()


def cmd_usage_impl(args: Any, deps: RecordCommandDeps) -> None:
    db, config = deps.db_factory()
    try:
        job_id = _resolve_or_print(db, args, deps)
        if not job_id:
            return
        job = db.get_job(job_id)
        usage = db.job_token_usage(job_id)
        usage["input_cost_per_million"] = config.model.input_cost_per_million
        usage["output_cost_per_million"] = config.model.output_cost_per_million
        if args.json:
            print(json.dumps(usage, ensure_ascii=False, indent=2, sort_keys=True))
            return
        lines = format_usage_report(
            title=str(job.get("title") or job_id),
            usage=usage,
            context_length=int(config.model.context_length or 0),
            model=str(config.model.model),
            base_url=str(config.model.base_url),
        )
        print("\n".join(lines))
    finally:
        db.close()


def _resolve_or_print(db: Any, args: Any, deps: RecordCommandDeps) -> str | None:
    job_id = deps.resolve_job_id(db, args.job_id)
    if job_id:
        return job_id
    ref = deps.job_ref_text(args.job_id)
    print(f"No job matched: {ref}" if ref else "No jobs found.")
    return None


def _metadata_records(job: dict[str, Any], key: str) -> list[dict[str, Any]]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    values = metadata.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _print_milestones(milestones: list[Any], *, limit: int, features: int, chars: int) -> None:
    if not milestones:
        return
    print()
    status_order = {"active": 0, "validating": 1, "planned": 2, "blocked": 3, "done": 4, "skipped": 5}
    ranked = sorted(
        [milestone for milestone in milestones if isinstance(milestone, dict)],
        key=lambda milestone: (
            status_order.get(str(milestone.get("status") or "planned"), 9),
            -int(milestone.get("priority") or 0),
            str(milestone.get("title") or ""),
        ),
    )
    for index, milestone in enumerate(ranked[:limit], start=1):
        status = str(milestone.get("status") or "planned")
        validation = str(milestone.get("validation_status") or "not_started")
        milestone_features = milestone.get("features") if isinstance(milestone.get("features"), list) else []
        open_features = sum(
            1 for feature in milestone_features
            if isinstance(feature, dict) and str(feature.get("status") or "planned") in {"planned", "active"}
        )
        print(
            f"{index:>2}. {status:<10} validation={validation:<11} "
            f"p={int(milestone.get('priority') or 0):<3} {_one_line(milestone.get('title') or 'milestone', 54)}"
        )
        details = " | ".join(
            value
            for value in [
                f"features={len(milestone_features)}/{open_features} open" if milestone_features else "",
                f"accept={milestone.get('acceptance_criteria')}" if milestone.get("acceptance_criteria") else "",
                f"evidence={milestone.get('evidence_needed')}" if milestone.get("evidence_needed") else "",
                f"result={milestone.get('validation_result')}" if milestone.get("validation_result") else "",
                f"next={milestone.get('next_action')}" if milestone.get("next_action") else "",
            ]
            if value
        )
        if details:
            print(f"    {_one_line(details, chars)}")
        for feature in milestone_features[: min(3, features)]:
            if isinstance(feature, dict):
                print(f"    - {str(feature.get('status') or 'planned'):<7} {_one_line(feature.get('title') or 'feature', max(30, chars - 16))}")


def _print_memory_sections(
    *,
    active_operator: list[dict[str, Any]],
    pending_measurement: dict[str, Any],
    reflections: list[dict[str, Any]],
    lessons: list[dict[str, Any]],
    compact: list[dict[str, Any]],
    limit: int,
    chars: int,
) -> None:
    if active_operator:
        print()
        print("active operator context:")
        for entry in active_operator[-min(limit, 8) :]:
            marker = entry.get("event_id") or "operator"
            print(f"  {marker}: {_one_line(entry.get('message') or '', chars)}")
    if pending_measurement:
        print()
        print(f"pending measurement: step #{pending_measurement.get('source_step_no') or '?'}")
        candidates = pending_measurement.get("metric_candidates") if isinstance(pending_measurement.get("metric_candidates"), list) else []
        if candidates:
            print(f"  candidates: {_one_line(', '.join(str(item) for item in candidates[:5]), chars)}")
    if reflections:
        print()
        print("latest reflection:")
        reflection = reflections[-1]
        print(f"  {_one_line(reflection.get('summary') or '', chars)}")
        if reflection.get("strategy"):
            print(f"  strategy: {_one_line(reflection['strategy'], chars)}")
    if lessons:
        print()
        print("latest lessons:")
        for lesson in lessons[-min(limit, 8) :]:
            print(f"  {lesson.get('category') or 'memory'}: {_one_line(lesson.get('lesson') or '', chars)}")
    if compact:
        print()
        print("compact memory:")
        for entry in compact[: min(limit, 3)]:
            print(f"  {entry.get('key')}: {_one_line(entry.get('summary') or '', chars)}")


def _print_best_records(
    *,
    sources: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    chars: int,
) -> None:
    if sources:
        best = max(sources, key=lambda source: float(source.get("usefulness_score") or 0))
        print(f"best source: {_one_line(best.get('source') or '', chars)} score={best.get('usefulness_score')}")
    if findings:
        best_finding = max(findings, key=lambda finding: float(finding.get("score") or 0))
        print(f"best finding: {_one_line(best_finding.get('name') or '', chars)} score={best_finding.get('score')}")
    measured = [experiment for experiment in experiments if experiment.get("metric_value") is not None]
    best_experiments = [experiment for experiment in measured if experiment.get("best_observed")]
    if best_experiments:
        best_experiment = best_experiments[-1]
        metric = f"{best_experiment.get('metric_name') or 'metric'}={best_experiment.get('metric_value')}{best_experiment.get('metric_unit') or ''}"
        print(f"best experiment: {_one_line(best_experiment.get('title') or '', chars)} {metric}")


def _step_count(steps: list[dict[str, Any]]) -> int:
    numbers = [int(step.get("step_no") or 0) for step in steps]
    return max(numbers, default=0)
