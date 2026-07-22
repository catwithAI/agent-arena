"""Stable runtime failure classification independent of a specific agent."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import AgentErrorCode, FailurePatternSpec
from .runtime.local_cli import RuntimeResult
from .secrets import redact_text


_PATTERN_CODES: tuple[AgentErrorCode, ...] = (
    "agent_auth_failed",
    "agent_model_unsupported",
    "agent_rate_limited",
    "agent_usage_limit",
    "agent_network_error",
)

_BUILTIN_PATTERNS: tuple[FailurePatternSpec, ...] = (
    FailurePatternSpec(
        error_code="agent_auth_failed",
        pattern=r"(?:unauthorized|invalid[ _-]?(?:api[ _-]?)?key|authentication failed|401)",
        producer_code="builtin.auth",
    ),
    FailurePatternSpec(
        error_code="agent_model_unsupported",
        pattern=r"(?:model (?:not found|does not exist|is not supported)|unknown model|404.*model)",
        producer_code="builtin.model",
    ),
    FailurePatternSpec(
        error_code="agent_rate_limited",
        pattern=r"(?:rate[ _-]?limit|too many requests|http\s*429|status\s*429)",
        producer_code="builtin.rate_limit",
    ),
    FailurePatternSpec(
        error_code="agent_usage_limit",
        pattern=r"(?:quota exceeded|usage limit|insufficient credits|billing limit)",
        producer_code="builtin.usage_limit",
    ),
    FailurePatternSpec(
        error_code="agent_network_error",
        pattern=r"(?:connection refused|connection reset|name or service not known|timed out connecting)",
        producer_code="builtin.network",
    ),
)


@dataclass(frozen=True)
class ErrorClassification:
    error_code: AgentErrorCode | None
    diagnostic: str | None
    producer_code: str | None = None
    task_failed: bool = False
    parse_degraded: bool = False


def classify_runtime_result(
    result: RuntimeResult,
    *,
    failure_patterns: Iterable[FailurePatternSpec] = (),
    secrets: tuple[str, ...] = (),
    parse_degraded: bool = False,
    max_scan_bytes: int = 1024 * 1024,
) -> ErrorClassification:
    if result.timed_out or result.error_code == "agent_timeout":
        return ErrorClassification(
            error_code="agent_timeout",
            diagnostic="agent exceeded its wall-clock time budget",
            task_failed=True,
        )
    if result.cleanup == "failed":
        return ErrorClassification(
            error_code="agent_cleanup_failed",
            diagnostic="agent process-group cleanup could not be confirmed",
            task_failed=True,
        )
    if result.returncode not in (None, 0):
        output = {
            "stdout": _read_bounded(result.stdout_path, max_scan_bytes),
            "stderr": _read_bounded(result.stderr_path, max_scan_bytes),
        }
        patterns = tuple(failure_patterns) + _BUILTIN_PATTERNS
        ordered = sorted(
            enumerate(patterns),
            key=lambda item: (
                _PATTERN_CODES.index(item[1].error_code)
                if item[1].error_code in _PATTERN_CODES
                else len(_PATTERN_CODES),
                item[0],
            ),
        )
        for _, failure_pattern in ordered:
            if any(
                re.search(failure_pattern.pattern, output[stream], re.IGNORECASE)
                for stream in failure_pattern.streams
            ):
                diagnostic = redact_text(
                    f"agent exited with code {result.returncode}; matched "
                    f"{failure_pattern.error_code}",
                    secrets,
                )
                return ErrorClassification(
                    error_code=failure_pattern.error_code,
                    diagnostic=diagnostic,
                    producer_code=failure_pattern.producer_code,
                    task_failed=True,
                )
        return ErrorClassification(
            error_code="agent_nonzero_exit",
            diagnostic=f"agent exited with code {result.returncode}",
            task_failed=True,
        )
    if parse_degraded:
        return ErrorClassification(
            error_code="agent_output_parse_degraded",
            diagnostic="agent completed, but output observability was degraded",
            task_failed=False,
            parse_degraded=True,
        )
    return ErrorClassification(error_code=None, diagnostic=None)


def _read_bounded(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as file:
            return file.read(max(0, max_bytes)).decode("utf-8", errors="replace")
    except OSError:
        return ""
