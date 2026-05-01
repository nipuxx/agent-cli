"""Prompt-facing summaries for worker history and tool observations."""

from __future__ import annotations

import json
import re
from typing import Any

from nipux_cli.metric_format import format_metric_value
from nipux_cli.source_quality import anti_bot_reason
from nipux_cli.worker_policy import BROWSER_REF_IGNORE_NAMES


def compact(value: Any, limit: int = 500) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def clip_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def format_step_for_prompt(step: dict[str, Any]) -> str:
    tool = f" tool={step['tool_name']}" if step.get("tool_name") else ""
    summary = step.get("summary") or step.get("error") or ""
    pieces = [f"- #{step['step_no']} {step['kind']} {step['status']}{tool}: {summary}"]
    input_data = step.get("input") or {}
    args = input_data.get("arguments") if isinstance(input_data, dict) else None
    if args:
        pieces.append(f"  args: {compact(args, 320)}")
    output = step.get("output") or {}
    observation = observation_for_prompt(step.get("tool_name"), output)
    if observation:
        pieces.append(f"  observed: {observation}")
    return "\n".join(pieces)


def observation_for_prompt(tool_name: str | None, output: dict[str, Any]) -> str:
    if not output:
        return ""
    if tool_name == "web_search":
        results = output.get("results") if isinstance(output.get("results"), list) else []
        titles = []
        for result in results[:5]:
            title = result.get("title") or "untitled"
            url = result.get("url") or ""
            titles.append(f"{title} <{url}>")
        return clip_text(f"query={output.get('query')!r}; results={'; '.join(titles)}", 650)
    if tool_name == "web_extract":
        pages = output.get("pages") if isinstance(output.get("pages"), list) else []
        parts = []
        for page in pages[:3]:
            if page.get("error"):
                parts.append(f"{page.get('url')}: ERROR {page.get('error')}")
            else:
                text = str(page.get("text") or "")
                parts.append(f"{page.get('url')}: {clip_text(text, 160)}")
        return clip_text("; ".join(parts), 650)
    if tool_name == "shell_exec":
        stdout = str(output.get("stdout") or "")
        stderr = str(output.get("stderr") or "")
        excerpt = stdout.strip() or stderr.strip()
        return (
            f"command={output.get('command')!r}; rc={output.get('returncode')}; "
            f"duration={output.get('duration_seconds')}s; output={clip_text(excerpt, 360)}"
        )[:650]
    if tool_name == "write_artifact":
        return f"saved artifact={output.get('artifact_id')} path={output.get('path')}"
    if tool_name == "report_update":
        update = output.get("update") if isinstance(output.get("update"), dict) else {}
        return clip_text(f"agent_update={update.get('message') or ''}", 420)
    if tool_name == "record_lesson":
        lesson = output.get("lesson") if isinstance(output.get("lesson"), dict) else {}
        return clip_text(f"lesson={lesson.get('category') or 'memory'}: {lesson.get('lesson') or ''}", 420)
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
            metric = format_metric_value(
                experiment.get("metric_name") or "metric",
                experiment.get("metric_value"),
                experiment.get("metric_unit") or "",
            )
        delta = f" delta={experiment.get('delta_from_previous_best')}" if experiment.get("delta_from_previous_best") is not None else ""
        best = " best_observed" if experiment.get("best_observed") else ""
        return clip_text(f"experiment={experiment.get('title')} status={experiment.get('status')} {metric}{delta}{best}", 520)
    if tool_name == "acknowledge_operator_context":
        return f"operator_context {output.get('status')} count={output.get('count')}"[:700]
    if tool_name in {"browser_click", "browser_type"} and output.get("error"):
        recovery = output.get("recovery_snapshot") if isinstance(output.get("recovery_snapshot"), dict) else {}
        candidates = browser_candidates_for_prompt(recovery)
        suffix = f"; recovery_candidates={candidates}" if candidates else ""
        return clip_text(f"error={output.get('error')}; guidance={output.get('recovery_guidance', '')}{suffix}", 700)
    if tool_name == "browser_navigate":
        data = output.get("data") if isinstance(output.get("data"), dict) else {}
        title = data.get("title") or ""
        url = data.get("url") or ""
        snapshot = str(output.get("snapshot") or "")
        warning = anti_bot_reason(title, url, snapshot)
        suffix = f"; source_warning={warning}" if warning else ""
        candidates = browser_candidates_for_prompt(output)
        candidate_suffix = f"; candidates={candidates}" if candidates else ""
        return clip_text(f"opened {title} <{url}>; snapshot_chars={len(snapshot)}{suffix}{candidate_suffix}", 700)
    if tool_name == "browser_snapshot":
        data = output.get("data") if isinstance(output.get("data"), dict) else {}
        snapshot = str(output.get("snapshot") or data.get("snapshot") or output.get("data") or "")
        warning = anti_bot_reason(snapshot)
        suffix = f"; source_warning={warning}" if warning else ""
        candidates = browser_candidates_for_prompt(output)
        candidate_suffix = f"; candidates={candidates}" if candidates else ""
        return clip_text(f"snapshot_chars={len(snapshot)}{suffix}{candidate_suffix}", 700)
    if output.get("error"):
        return f"error={output.get('error')}"
    return compact(output, 700)


def browser_candidates_for_prompt(output: dict[str, Any], *, limit: int = 18) -> str:
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
