"""Small formatting helpers for measured worker results."""

from __future__ import annotations

from typing import Any


def format_metric_value(name: Any, value: Any, unit: Any = "") -> str:
    """Return a readable metric string such as ``score=0.82`` or ``tokens=4200 tokens``."""

    metric_name = str(name or "metric").strip() or "metric"
    metric_value = str(value).strip()
    metric_unit = str(unit or "").strip()
    if not metric_unit:
        return f"{metric_name}={metric_value}"
    separator = "" if metric_unit.startswith(("%", "/", "°")) else " "
    return f"{metric_name}={metric_value}{separator}{metric_unit}"
