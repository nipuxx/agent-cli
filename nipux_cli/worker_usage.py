"""Usage accounting for worker model turns."""

from __future__ import annotations

import json
from typing import Any

from nipux_cli.llm import LLMResponse


def turn_usage_metadata(
    response: LLMResponse,
    *,
    messages: list[dict[str, Any]],
    context_length: int,
) -> dict[str, Any]:
    prompt_text = json.dumps(messages, ensure_ascii=False, default=str)
    completion_text = response.content + json.dumps(
        [{"name": call.name, "arguments": call.arguments} for call in response.tool_calls],
        ensure_ascii=False,
        default=str,
    )
    usage = dict(response.usage) if isinstance(response.usage, dict) else {}
    prompt_tokens = _as_int(usage.get("prompt_tokens")) or estimate_token_count(prompt_text)
    completion_tokens = _as_int(usage.get("completion_tokens")) or estimate_token_count(completion_text)
    usage.setdefault("prompt_tokens", prompt_tokens)
    usage.setdefault("completion_tokens", completion_tokens)
    usage.setdefault("total_tokens", prompt_tokens + completion_tokens)
    usage.setdefault("estimated", not bool(response.usage))
    usage["prompt_chars"] = len(prompt_text)
    usage["completion_chars"] = len(completion_text)
    if context_length > 0:
        usage["context_length"] = context_length
        usage["context_fraction"] = round(prompt_tokens / max(1, context_length), 6)
    return usage


def estimate_token_count(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
