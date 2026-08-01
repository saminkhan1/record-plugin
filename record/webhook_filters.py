"""Plugin-owned validation for reviewed webhook filters."""

from __future__ import annotations

import re
from typing import Any


_OPERATORS = (
    "exists",
    "missing",
    "equals",
    "not_equals",
    "contains",
    "in",
    "regex",
)


def normalize_webhook_filter(
    value: Any,
    *,
    subject: str = "webhook filter",
    sequence_is_all: bool = False,
) -> dict[str, Any]:
    """Validate a reviewed filter and return its canonical object form."""

    if sequence_is_all and isinstance(value, list):
        if not value:
            raise ValueError(f"{subject} must be a non-empty list or filter object")
        return {
            "all": [
                normalize_webhook_filter(item, subject=subject)
                for item in value
            ]
        }
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{subject} must be a declarative filter object")

    groups = [key for key in ("all", "any", "not") if key in value]
    if groups:
        if len(groups) != 1 or len(value) != 1:
            raise ValueError(f"{subject} has an invalid nested filter")
        group = groups[0]
        nested = value[group]
        if group == "not":
            return {
                group: normalize_webhook_filter(nested, subject=subject)
            }
        if not isinstance(nested, list) or not nested:
            raise ValueError(f"{subject}.{group} must be a non-empty list")
        return {
            group: [
                normalize_webhook_filter(item, subject=subject)
                for item in nested
            ]
        }

    field = value.get("field")
    if not isinstance(field, str) or not field.strip():
        raise ValueError(f"{subject} requires a non-empty field")
    operators = [key for key in _OPERATORS if key in value]
    if len(operators) != 1 or set(value) != {"field", *operators}:
        raise ValueError(f"{subject} requires exactly one supported filter operator")

    operator = operators[0]
    operand = value[operator]
    if operator == "exists" and not isinstance(operand, bool):
        raise ValueError(f"{subject}.exists must be a boolean")
    if operator == "missing" and operand is not True:
        raise ValueError(f"{subject}.missing must be true")
    if operator == "in" and not isinstance(operand, list):
        raise ValueError(f"{subject}.in must be a list")
    if operator == "regex":
        if not isinstance(operand, str):
            raise ValueError(f"{subject}.regex must be a string")
        try:
            re.compile(operand)
        except re.error as exc:
            raise ValueError(f"{subject}.regex is invalid") from exc
    return {"field": field.strip(), operator: operand}
