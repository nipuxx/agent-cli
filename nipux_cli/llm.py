"""LLM provider adapters for one bounded worker step."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import OpenAI

from nipux_cli.config import ModelConfig


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass(frozen=True)
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    response_id: str = ""


class LLMResponseError(RuntimeError):
    """Raised when a provider returns an OpenAI-shaped response without choices."""

    def __init__(self, message: str, *, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


class StepLLM(Protocol):
    def next_action(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        ...


class OpenAIChatLLM:
    """OpenAI-compatible chat-completions adapter."""

    def __init__(self, config: ModelConfig):
        self.config = config
        headers = {}
        if "openrouter.ai" in config.base_url:
            headers = {
                "HTTP-Referer": "https://github.com/nipuxx/agent-cli",
                "X-Title": "Nipux CLI",
            }
        self._openai = OpenAI(
            api_key=config.api_key or "local-no-key",
            base_url=config.base_url,
            timeout=config.request_timeout_seconds,
            default_headers=headers or None,
        )

    def next_action(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        response = self._openai.chat.completions.create(
            model=self.config.model,
            messages=messages,
            tools=tools,
        )
        choices = response.choices or []
        if not choices:
            payload = _response_payload(response)
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            detail = error.get("message") or payload.get("message") or "provider returned no choices"
            raise LLMResponseError(str(detail), payload=payload)
        message = choices[0].message
        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            raw_args = call.function.arguments or "{}"
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed = {}
            calls.append(ToolCall(name=call.function.name, arguments=parsed, id=call.id or ""))
        content = message.content or ""
        response_id = _response_id(response)
        usage = _response_usage(response, messages=messages, content=content, tool_calls=calls)
        usage = _enrich_openrouter_generation_usage(
            usage,
            response_id=response_id,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
        )
        return LLMResponse(
            content=content,
            tool_calls=calls,
            usage=usage,
            model=_response_model(response),
            response_id=response_id,
        )

    def complete(self, *, messages: list[dict[str, Any]]) -> str:
        return self.complete_response(messages=messages).content

    def complete_response(self, *, messages: list[dict[str, Any]]) -> LLMResponse:
        response = self._openai.chat.completions.create(
            model=self.config.model,
            messages=messages,
        )
        choices = response.choices or []
        if not choices:
            payload = _response_payload(response)
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            detail = error.get("message") or payload.get("message") or "provider returned no choices"
            raise LLMResponseError(str(detail), payload=payload)
        content = choices[0].message.content or ""
        response_id = _response_id(response)
        usage = _response_usage(response, messages=messages, content=content, tool_calls=[])
        usage = _enrich_openrouter_generation_usage(
            usage,
            response_id=response_id,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
        )
        return LLMResponse(
            content=content,
            usage=usage,
            model=_response_model(response),
            response_id=response_id,
        )


class ScriptedLLM:
    """Tiny deterministic LLM used by tests and CLI dry runs."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)

    def next_action(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        del messages, tools
        if not self.responses:
            return LLMResponse(content="No scripted response left.")
        return self.responses.pop(0)


def _response_payload(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        dumped = response.model_dump()
        return dumped if isinstance(dumped, dict) else {"response": dumped}
    if hasattr(response, "to_dict"):
        dumped = response.to_dict()
        return dumped if isinstance(dumped, dict) else {"response": dumped}
    return {"response": repr(response)}


def _response_usage(
    response: Any,
    *,
    messages: list[dict[str, Any]],
    content: str,
    tool_calls: list[ToolCall],
) -> dict[str, Any]:
    payload = _response_payload(response)
    usage = payload.get("usage")
    if isinstance(usage, dict):
        normalized = dict(usage)
        normalized["estimated"] = False
        return normalized
    usage_obj = getattr(response, "usage", None)
    if usage_obj is not None:
        dumped = usage_obj.model_dump() if hasattr(usage_obj, "model_dump") else getattr(usage_obj, "__dict__", {})
        if isinstance(dumped, dict) and dumped:
            normalized = dict(dumped)
            normalized["estimated"] = False
            return normalized
    prompt_tokens = _estimate_token_count(json.dumps(messages, ensure_ascii=False, default=str))
    tool_text = json.dumps([{"name": call.name, "arguments": call.arguments} for call in tool_calls], ensure_ascii=False, default=str)
    completion_tokens = _estimate_token_count(content + tool_text)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "estimated": True,
    }


def _enrich_openrouter_generation_usage(
    usage: dict[str, Any],
    *,
    response_id: str,
    base_url: str,
    api_key: str,
) -> dict[str, Any]:
    if usage.get("cost") is not None or not response_id or not api_key:
        return usage
    if "openrouter.ai" not in base_url:
        return usage
    parsed = urllib.parse.urlparse(base_url)
    root = f"{parsed.scheme or 'https'}://{parsed.netloc or 'openrouter.ai'}"
    url = f"{root}/api/v1/generation?id={urllib.parse.quote(response_id)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return usage
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return usage
    enriched = dict(usage)
    cost = _safe_float(data.get("total_cost") or data.get("cost"))
    if cost is not None:
        enriched["cost"] = cost
    prompt = _safe_int(data.get("native_tokens_prompt") or data.get("tokens_prompt"))
    completion = _safe_int(data.get("native_tokens_completion") or data.get("tokens_completion"))
    total = _safe_int(data.get("native_tokens_total") or data.get("tokens_total"))
    if prompt is not None:
        enriched["prompt_tokens"] = prompt
    if completion is not None:
        enriched["completion_tokens"] = completion
    if total is not None:
        enriched["total_tokens"] = total
    elif prompt is not None or completion is not None:
        enriched["total_tokens"] = int(enriched.get("prompt_tokens") or 0) + int(enriched.get("completion_tokens") or 0)
    enriched["estimated"] = bool(enriched.get("estimated")) and cost is None
    return enriched


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _estimate_token_count(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _response_model(response: Any) -> str:
    payload = _response_payload(response)
    return str(payload.get("model") or getattr(response, "model", "") or "")


def _response_id(response: Any) -> str:
    payload = _response_payload(response)
    return str(payload.get("id") or getattr(response, "id", "") or "")
