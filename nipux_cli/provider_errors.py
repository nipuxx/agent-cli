"""Generic model-provider error classification."""

from __future__ import annotations

import json
from typing import Any


PROVIDER_ACTION_MARKERS = (
    "authenticationerror",
    "permissiondeniederror",
    "authentication failed",
    "permission denied",
    "invalid api key",
    "incorrect api key",
    "user not found",
    "key limit exceeded",
    "insufficient_quota",
    "insufficient quota",
    "insufficient credits",
    "billing",
    "payment required",
    "credit limit",
    "quota exceeded",
    "401",
    "403",
)

RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "ratelimit",
    "too many requests",
    "temporarily over capacity",
)

PROVIDER_ACTION_REQUIRED_NOTE = (
    "Model provider requires operator action: authentication, permission, billing, or quota is blocking calls. "
    "Paused this job so the daemon does not repeat failing model requests. Update credentials/model access, then resume."
)


def provider_error_text(error: Any) -> str:
    if isinstance(error, str):
        return error.lower()
    parts = [type(error).__name__, str(error)]
    payload = getattr(error, "payload", None)
    if isinstance(payload, dict) and payload:
        parts.append(json.dumps(payload, ensure_ascii=False, default=str))
    return " ".join(parts).lower()


def provider_action_required(text_or_error: Any) -> bool:
    text = provider_error_text(text_or_error)
    return any(marker in text for marker in PROVIDER_ACTION_MARKERS)


def provider_action_required_note(text_or_error: Any) -> str:
    return PROVIDER_ACTION_REQUIRED_NOTE if provider_action_required(text_or_error) else ""


def provider_rate_limited(text_or_error: Any) -> bool:
    text = provider_error_text(text_or_error)
    return any(marker in text for marker in RATE_LIMIT_MARKERS)
