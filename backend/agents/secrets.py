"""Central redaction helpers for agent plans, diagnostics and manifests."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s,;]+)"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*)([^\s,;]+)"),
)


def redact_text(value: str, secrets: Sequence[str] = ()) -> str:
    redacted = value
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        redacted = redacted.replace(secret, "***")
    for pattern in _CREDENTIAL_PATTERNS:
        redacted = pattern.sub(r"\1***", redacted)
    return redacted


def redact_value(value: Any, secrets: Sequence[str] = ()) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {str(key): redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_value(item, secrets) for item in value)
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    return value
