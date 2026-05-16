"""Shared daemon timing defaults."""

from __future__ import annotations


DAEMON_DEFAULT_POLL_SECONDS = 0.5


def normalize_daemon_poll_seconds(value: float | int | str | None) -> float:
    """Clamp daemon polling to a nonzero interval so active workers cannot spin."""

    try:
        seconds = float(value or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    return max(DAEMON_DEFAULT_POLL_SECONDS, seconds)
