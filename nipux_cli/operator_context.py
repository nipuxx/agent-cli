"""Generic filtering for operator messages that enter worker context."""

from __future__ import annotations

import re
from typing import Any


CONVERSATION_ONLY_PATTERNS = [
    re.compile(r"(?i)^\s*(hi|hello|hey|yo|thanks|thank you|ok|okay|cool|nice|great|hello\?)\s*[.!?]*\s*$"),
    re.compile(r"(?i)^\s*(how('?s| is) it going|what('?s| is) going on|any updates?|status|jobs|ls|help|clear|exit|quit)\s*[.!?]*\s*$"),
    re.compile(r"(?i)^\s*(how('?s| is) it going)\??\s*(have you got|any)\s+(any\s+)?(improvements?|updates?|results?)\s*(yet)?\s*[.!?]*\s*$"),
    re.compile(r"(?i)^\s*(run|start|stop|pause|resume|cancel|work|status|jobs|clear|exit|quit|help)\s+\d+\s*$"),
]

ACTIONABLE_PATTERNS = [
    re.compile(
        r"(?i)\b("
        r"avoid|because|benchmark|change|constraint|correct|do not|don't|dont|fix|focus|instead|instruction|"
        r"measure|must|need|never|only|prefer|priority|remember|should|target|use|wrong"
        r"|prioriti[sz]e)\b"
    ),
    re.compile(r"[\"'`][^\"'`]{2,}[\"'`]"),
]


def operator_entry_is_active(entry: dict[str, Any]) -> bool:
    mode = str(entry.get("mode") or "steer").strip().lower().replace("-", "_")
    return (
        mode in {"steer", "follow_up"}
        and not entry.get("acknowledged_at")
        and not entry.get("superseded_at")
    )


def operator_entry_is_prompt_relevant(entry: dict[str, Any]) -> bool:
    mode = str(entry.get("mode") or "steer").strip().lower().replace("-", "_")
    message = str(entry.get("message") or "").strip()
    if not message:
        return False
    if mode == "note":
        return not _conversation_only(message)
    if mode not in {"steer", "follow_up"}:
        return False
    if entry.get("acknowledged_at") or entry.get("superseded_at"):
        return False
    return _actionable(message)


def active_prompt_operator_entries(messages: list[Any]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in messages
        if isinstance(entry, dict)
        and operator_entry_is_prompt_relevant(entry)
    ]


def inactive_prompt_operator_ids(messages: list[Any]) -> list[str]:
    ids: list[str] = []
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        if not operator_entry_is_active(entry):
            continue
        if operator_entry_is_prompt_relevant(entry):
            continue
        event_id = str(entry.get("event_id") or "")
        if event_id:
            ids.append(event_id)
    return ids


def _conversation_only(message: str) -> bool:
    text = " ".join(message.split())
    return any(pattern.search(text) for pattern in CONVERSATION_ONLY_PATTERNS)


def _actionable(message: str) -> bool:
    text = " ".join(message.split())
    if _conversation_only(text):
        return False
    return any(pattern.search(text) for pattern in ACTIONABLE_PATTERNS)
