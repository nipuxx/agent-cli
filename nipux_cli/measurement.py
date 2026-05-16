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
TABLE_UNIT_PATTERN = re.compile(
    r"(?i)^(?:%|ms|msec|sec|secs|seconds|it/s|ops/s|req/s|qps|rps|samples/s|items/s|units/s|"
    r"tokens/s|tok/s|t/s|kb/s|mb/s|gb/s|tb/s)$"
)
TABLE_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?(?:\s*(?:±|\+/-)\s*[-+]?\d+(?:\.\d+)?)?")


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
    for candidate in _table_measurement_candidates(text, limit=limit):
        if candidate not in candidates:
            candidates.append(candidate)
        if len(candidates) >= limit:
            return candidates
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


def _table_measurement_candidates(text: str, *, limit: int = 8) -> list[str]:
    candidates: list[str] = []
    table_lines = [line.strip() for line in str(text or "").splitlines() if line.strip().startswith("|") and "|" in line.strip()[1:]]
    for index, line in enumerate(table_lines):
        headers = _split_markdown_table_row(line)
        if not headers or _is_markdown_separator_row(headers):
            continue
        unit_indexes = [idx for idx, header in enumerate(headers) if TABLE_UNIT_PATTERN.search(header.strip())]
        if not unit_indexes:
            continue
        for row_line in table_lines[index + 1 : index + 16]:
            cells = _split_markdown_table_row(row_line)
            if not cells or _is_markdown_separator_row(cells):
                continue
            for unit_index in unit_indexes:
                if unit_index >= len(cells):
                    continue
                value = cells[unit_index].strip()
                number = TABLE_NUMBER_PATTERN.search(value)
                if not number:
                    continue
                unit = headers[unit_index].strip()
                label = _table_measurement_label(headers, cells, unit_index=unit_index)
                candidate = f"{label} {number.group(0).strip()} {unit}".strip()
                if candidate not in candidates:
                    candidates.append(candidate[:140])
                if len(candidates) >= limit:
                    return candidates
    return candidates


def _split_markdown_table_row(line: str) -> list[str]:
    raw = str(line or "").strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [" ".join(cell.strip().split()) for cell in raw.split("|")]


def _is_markdown_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell.strip()) for cell in cells if cell.strip())


def _table_measurement_label(headers: list[str], cells: list[str], *, unit_index: int) -> str:
    preferred_headers = {"test", "metric", "name", "case", "benchmark"}
    for index, header in enumerate(headers):
        if index >= len(cells) or index == unit_index:
            continue
        if header.strip().lower() in preferred_headers and cells[index].strip():
            return cells[index].strip()
    for index in range(min(unit_index, len(cells)) - 1, -1, -1):
        cell = cells[index].strip()
        if cell and not TABLE_NUMBER_PATTERN.fullmatch(cell):
            return cell
    return "measurement"


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
