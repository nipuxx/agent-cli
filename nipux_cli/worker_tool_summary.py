"""Compact result summaries for worker tool executions."""

from __future__ import annotations

from typing import Any

from nipux_cli.metric_format import format_metric_value


def summarize_tool_result(name: str, args: dict[str, Any], result: dict[str, Any], *, ok: bool) -> str:
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
    if name == "write_file":
        return f"write_file {result.get('mode') or 'overwrite'} {result.get('path')} bytes={result.get('bytes')}"
    if name == "defer_job":
        return f"defer_job until {result.get('defer_until')}"
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
    if name == "record_roadmap":
        roadmap = result.get("roadmap") if isinstance(result.get("roadmap"), dict) else {}
        return (
            f"record_roadmap {roadmap.get('status')}: {roadmap.get('title')} "
            f"milestones={len(roadmap.get('milestones') or [])}"
        )
    if name == "record_milestone_validation":
        validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
        return (
            f"record_milestone_validation {validation.get('validation_status')}: "
            f"{validation.get('title')} followups={len(result.get('follow_up_tasks') or [])}"
        )
    if name == "record_experiment":
        experiment = result.get("experiment") if isinstance(result.get("experiment"), dict) else {}
        metric = ""
        if experiment.get("metric_value") is not None:
            metric = " " + format_metric_value(
                experiment.get("metric_name") or "metric",
                experiment.get("metric_value"),
                experiment.get("metric_unit") or "",
            )
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
