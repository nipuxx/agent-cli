"""LLM provider adapters for one bounded worker step."""

from __future__ import annotations

import json
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
            tool_choice="auto",
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
        return LLMResponse(content=message.content or "", tool_calls=calls)

    def complete(self, *, messages: list[dict[str, Any]]) -> str:
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
        return choices[0].message.content or ""


class ScriptedLLM:
    """Tiny deterministic LLM used by smoke tests and CLI dry runs."""

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
