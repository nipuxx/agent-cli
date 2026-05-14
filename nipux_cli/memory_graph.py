"""Job-local memory graph helpers for long-running workers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from nipux_cli.worker_prompt_format import clip_text


NODE_KINDS = {
    "artifact",
    "constraint",
    "decision",
    "episode",
    "experiment",
    "fact",
    "milestone",
    "question",
    "skill",
    "source",
    "strategy",
    "task",
}
NODE_STATUSES = {"active", "blocked", "deprecated", "open", "resolved", "stable"}
DEFAULT_NODE_KIND = "fact"
DEFAULT_NODE_STATUS = "active"


def memory_graph_from_job(job: dict[str, Any]) -> dict[str, Any]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    graph = metadata.get("memory_graph") if isinstance(metadata.get("memory_graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    return {
        "nodes": [node for node in nodes if isinstance(node, dict)],
        "edges": [edge for edge in edges if isinstance(edge, dict)],
        "updated_at": graph.get("updated_at") or "",
    }


def memory_graph_for_prompt(job: dict[str, Any], *, limit: int = 10, stale_tokens: list[str] | None = None) -> str:
    graph = memory_graph_from_job(job)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    stale_tokens = [str(token) for token in stale_tokens or [] if str(token).strip()]
    stale_node_ids = {
        str(record.get("record_id") or "")
        for record in metadata.get("stale_negative_records", [])
        if isinstance(record, dict) and str(record.get("kind") or "") == "memory_node"
    } if isinstance(metadata.get("stale_negative_records"), list) else set()
    stale_nodes = [
        node
        for node in graph["nodes"]
        if _node_contains_stale_token(node, stale_tokens) or _node_has_stale_id(node, stale_node_ids)
    ]
    active_nodes = [node for node in graph["nodes"] if node not in stale_nodes]
    nodes = rank_memory_nodes(active_nodes, limit=limit)
    durable_count = _durable_signal_count(job)
    if not nodes:
        hint = (
            "No memory graph yet. When a branch produces reusable knowledge, use record_memory_graph "
            "to save connected episode, fact, strategy, skill, question, decision, or constraint nodes."
        )
        if durable_count:
            hint += (
                f" Durable ledgers already contain {durable_count} reusable item(s); consolidate the most important "
                "ones into graph nodes before raw history grows further."
            )
        return hint
    edge_index = _edge_index(graph["edges"])
    lines = [
        f"Memory graph: nodes={len(graph['nodes'])} edges={len(graph['edges'])}",
    ]
    if stale_nodes:
        lines.append(
            f"Suppressed {len(stale_nodes)} stale memory node(s) matching unsupported tokens; "
            "do not use them as facts until observed again."
        )
    if durable_count >= 8 and len(graph["nodes"]) < max(4, durable_count // 4):
        lines.append(
            "Consolidation pressure: durable ledgers are growing faster than the memory graph. "
            "After the next meaningful checkpoint, use record_memory_graph to add or update connected nodes."
        )
    for node in nodes:
        key = str(node.get("key") or "")
        title = str(node.get("title") or key or "memory")
        kind = str(node.get("kind") or DEFAULT_NODE_KIND)
        status = str(node.get("status") or DEFAULT_NODE_STATUS)
        summary = str(node.get("summary") or "")
        tags = _clean_string_list(node.get("tags"))[:5]
        refs = _clean_string_list(node.get("evidence_refs"))[:4]
        parent = str(node.get("parent_key") or "")
        line = f"- {status} {kind}: {title}"
        if parent:
            line += f" | parent={parent}"
        if tags:
            line += f" | tags={', '.join(tags)}"
        if refs:
            line += f" | evidence={', '.join(refs)}"
        if summary:
            line += f" | {summary}"
        lines.append(clip_text(line, 620))
        related = edge_index.get(key, [])[:3]
        if related:
            lines.append("  links: " + clip_text("; ".join(related), 420))
    return "\n".join(lines)


def _node_contains_stale_token(node: dict[str, Any], stale_tokens: list[str]) -> bool:
    if not stale_tokens:
        return False
    text_parts = [
        node.get("key"),
        node.get("title"),
        node.get("kind"),
        node.get("status"),
        node.get("summary"),
        " ".join(_clean_string_list(node.get("tags"))),
    ]
    text = " ".join(str(part or "") for part in text_parts)
    for token in stale_tokens:
        pattern = r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])"
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True
    return False


def _node_has_stale_id(node: dict[str, Any], stale_node_ids: set[str]) -> bool:
    if not stale_node_ids:
        return False
    for key in ("key", "event_id", "id"):
        value = str(node.get(key) or "").strip()
        if value and value in stale_node_ids:
            return True
    return False


def search_memory_graph(graph: dict[str, Any], query: str, *, limit: int = 10) -> dict[str, Any]:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    ranked = rank_memory_nodes([node for node in nodes if isinstance(node, dict)], query=query, limit=limit)
    keys = {str(node.get("key") or "") for node in ranked}
    related_edges = [
        edge
        for edge in edges
        if isinstance(edge, dict)
        and (str(edge.get("from_key") or "") in keys or str(edge.get("to_key") or "") in keys)
    ][: max(limit * 2, 10)]
    return {"nodes": ranked, "edges": related_edges}


def rank_memory_nodes(nodes: list[dict[str, Any]], *, query: str = "", limit: int = 10) -> list[dict[str, Any]]:
    tokens = _tokens(query)
    ranked = sorted(nodes, key=lambda node: _node_score(node, tokens), reverse=True)
    if tokens:
        ranked = [node for node in ranked if _node_score(node, tokens) > 0]
    return ranked[: max(0, limit)]


def _node_score(node: dict[str, Any], query_tokens: set[str]) -> float:
    haystack = " ".join(
        str(value or "")
        for value in [
            node.get("key"),
            node.get("title"),
            node.get("kind"),
            node.get("status"),
            node.get("summary"),
            " ".join(_clean_string_list(node.get("tags"))),
        ]
    ).lower()
    score = 0.0
    for token in query_tokens:
        if token in haystack:
            score += 4.0 if token in str(node.get("title") or "").lower() else 2.0
    score += _float_between(node.get("salience"), 0.0, 1.0) * 3.0
    score += _float_between(node.get("confidence"), 0.0, 1.0)
    status = str(node.get("status") or DEFAULT_NODE_STATUS)
    if status in {"active", "open"}:
        score += 1.2
    elif status == "stable":
        score += 0.7
    elif status == "deprecated":
        score -= 1.5
    kind = str(node.get("kind") or DEFAULT_NODE_KIND)
    if kind in {"strategy", "skill", "decision", "constraint", "question"}:
        score += 0.5
    score += min(int(node.get("use_count") or 0), 8) * 0.08
    score += _recency_score(str(node.get("updated_at") or node.get("created_at") or ""))
    return score


def _edge_index(edges: list[dict[str, Any]]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for edge in edges:
        from_key = str(edge.get("from_key") or "")
        to_key = str(edge.get("to_key") or "")
        if not from_key or not to_key:
            continue
        relation = str(edge.get("relation") or "related_to")
        index.setdefault(from_key, []).append(f"{relation} -> {to_key}")
        index.setdefault(to_key, []).append(f"{relation} <- {from_key}")
    return index


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9][a-z0-9_-]{1,}", value.lower()) if token not in _STOPWORDS}


def _float_between(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    return min(high, max(low, number))


def _recency_score(value: str) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    age_seconds = max(0.0, (datetime.now(parsed.tzinfo) - parsed).total_seconds())
    if age_seconds < 3600:
        return 0.8
    if age_seconds < 86_400:
        return 0.4
    if age_seconds < 604_800:
        return 0.15
    return 0.0


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [" ".join(str(item).split()) for item in value if str(item).strip()]


def _durable_signal_count(job: dict[str, Any]) -> int:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    count = 0
    for key in (
        "experiment_ledger",
        "finding_ledger",
        "lessons",
        "source_ledger",
        "task_queue",
    ):
        values = metadata.get(key)
        if isinstance(values, list):
            count += sum(1 for value in values if isinstance(value, dict))
    roadmap = metadata.get("roadmap") if isinstance(metadata.get("roadmap"), dict) else {}
    milestones = roadmap.get("milestones") if isinstance(roadmap.get("milestones"), list) else []
    count += sum(1 for milestone in milestones if isinstance(milestone, dict))
    return count


_STOPWORDS = {
    "and",
    "for",
    "from",
    "into",
    "that",
    "the",
    "this",
    "with",
}
