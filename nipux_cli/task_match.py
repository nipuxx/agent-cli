"""Task title matching helpers for long-running job queues."""

from __future__ import annotations

import re
from typing import Any

TASK_MATCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "then",
    "to",
    "via",
    "with",
}


def task_key(parent: str, title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{parent}|{title}".lower()).strip("-")[:120]


def find_semantic_task_match(
    *,
    title: str,
    parent: str,
    tasks: list[dict[str, Any]],
    statuses: set[str] | None = None,
    min_score: float = 0.55,
) -> dict[str, Any] | None:
    incoming_title = str(title or "").strip()
    if not incoming_title:
        return None
    incoming_parent = str(parent or "").strip()
    incoming_key = task_key(incoming_parent, incoming_title)
    incoming_tokens = _task_tokens(incoming_title)
    if len(incoming_tokens) < 2:
        return None
    allowed_statuses = statuses or {"active", "open", "blocked"}
    best: dict[str, Any] | None = None
    best_score = 0.0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        candidate_title = str(task.get("title") or "").strip()
        if not candidate_title:
            continue
        candidate_parent = str(task.get("parent") or "").strip()
        if incoming_parent and candidate_parent and incoming_parent != candidate_parent:
            continue
        candidate_key = str(task.get("key") or task_key(candidate_parent, candidate_title))
        if candidate_key == incoming_key:
            return None
        status = str(task.get("status") or "open").strip().lower().replace(" ", "_")
        if status not in allowed_statuses:
            continue
        candidate_tokens = _task_tokens(candidate_title)
        if len(candidate_tokens) < 2:
            continue
        score, overlap = _task_similarity(incoming_tokens, candidate_tokens)
        if overlap < 2 or score < min_score or score <= best_score:
            continue
        best_score = score
        best = {
            "task": task,
            "key": candidate_key,
            "title": candidate_title,
            "parent": candidate_parent,
            "status": status,
            "score": round(score, 3),
            "overlap": overlap,
        }
    return best


def _task_tokens(text: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", str(text or "").lower())
        if len(token) > 1 and token not in TASK_MATCH_STOPWORDS
    }
    return tokens


def _task_similarity(left: set[str], right: set[str]) -> tuple[float, int]:
    overlap = len(left & right)
    if overlap <= 0:
        return 0.0, 0
    jaccard = overlap / max(1, len(left | right))
    containment = overlap / max(1, min(len(left), len(right)))
    return max(jaccard, containment), overlap
