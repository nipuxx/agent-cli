"""Measurement parsing helpers for generic progress accounting."""

from __future__ import annotations

import re
from typing import Any


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
    r"(?i)\b\d+(?:\.\d+)?\s*(?:%|ms|msec|sec|secs|seconds|it/s|ops/s|req/s|qps|rps|samples/s|items/s|units/s|"
    r"tokens/s|tok/s|t/s|kb/s|mb/s|gb/s|tb/s)\b"
)


def measurement_candidates(output: dict[str, Any], *, command: str = "", limit: int = 8) -> list[str]:
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
        if not EXPLICIT_RESULT_UNIT_PATTERN.search(candidate):
            expanded = " ".join(text[match.start() : min(len(text), match.end() + 32)].split())
            if EXPLICIT_RESULT_UNIT_PATTERN.search(expanded):
                candidate = expanded
        if _candidate_is_diagnostic_only(candidate, command_has_measurement_intent):
            continue
        if candidate not in candidates:
            candidates.append(candidate[:140])
        if len(candidates) >= limit:
            break
    return candidates


def measurement_candidates_are_diagnostic_only(candidates: list[Any], *, command: str = "") -> bool:
    command_has_measurement_intent = bool(MEASUREMENT_INTENT_PATTERN.search(command))
    return all(_candidate_is_diagnostic_only(str(candidate), command_has_measurement_intent) for candidate in candidates)


def _candidate_is_diagnostic_only(candidate: str, command_has_measurement_intent: bool) -> bool:
    has_structured_metric = bool(EXPLICIT_RESULT_UNIT_PATTERN.search(candidate) or LABELED_MEASUREMENT_PATTERN.search(candidate))
    if command_has_measurement_intent:
        return not has_structured_metric
    if DIAGNOSTIC_MEASUREMENT_PATTERN.search(candidate):
        return True
    if EXPLICIT_RESULT_UNIT_PATTERN.search(candidate) and not re.search(r"(?i)\b(?:cpu|gpu|ram|mem|memory)\b", candidate):
        return False
    if ACTION_MEASUREMENT_PATTERN.search(candidate):
        return not bool(LABELED_MEASUREMENT_PATTERN.search(candidate))
    return True
