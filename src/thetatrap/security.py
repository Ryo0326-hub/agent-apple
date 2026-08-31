"""Small redaction helpers used before values enter logs or SQLite."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


SENSITIVE_FRAGMENTS = (
    "api_key",
    "secret",
    "token",
    "authorization",
    "password",
)


def redact(value: Any, key: str | None = None) -> Any:
    if key and any(fragment in key.lower() for fragment in SENSITIVE_FRAGMENTS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): redact(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value
