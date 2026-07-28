"""Versioned JSONL field-mapping parser."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .base import EvidenceSet, ParseDiagnostic, ParseResult, dotted_get


_USAGE_FIELDS: dict[str, tuple[str, ...]] = {
    "input_tokens": (
        "input_tokens",
        "prompt_tokens",
        "inputTokens",
        "promptTokens",
        "input",
    ),
    "output_tokens": (
        "output_tokens",
        "completion_tokens",
        "outputTokens",
        "completionTokens",
        "output",
    ),
    "cache_read_tokens": ("cache_read_tokens", "cacheReadTokens", "cache.read"),
    "cache_write_tokens": ("cache_write_tokens", "cacheWriteTokens", "cache.write"),
    "reasoning_tokens": ("reasoning_tokens", "reasoningTokens", "reasoning"),
}


class JsonlMappingParser:
    parser_id = "jsonl-mapping"
    parser_version = "1"

    def __init__(
        self,
        *,
        type_field: str = "type",
        text_field: str = "text",
        usage_field: str = "usage",
        thinking_type_value: str = "thinking",
        final_type_value: str | None = None,
        session_field: str | None = None,
        tool_type_value: str | None = None,
        max_line_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self.type_field = type_field
        self.text_field = text_field
        self.usage_field = usage_field
        self.thinking_type_value = thinking_type_value
        self.final_type_value = final_type_value
        self.session_field = session_field
        self.tool_type_value = tool_type_value
        self.max_line_bytes = max_line_bytes

    async def parse(self, evidence: EvidenceSet) -> ParseResult:
        diagnostics: list[ParseDiagnostic] = []
        events: list[dict[str, Any]] = []
        thinking: list[dict[str, Any]] = []
        tools: list[dict[str, Any]] = []
        text_candidates: list[str] = []
        final_candidates: list[str] = []
        session_id: str | None = None
        usage_totals = {name: 0 for name in _USAGE_FIELDS}
        usage_observed = {name: False for name in _USAGE_FIELDS}

        try:
            file = evidence.stdout_path.open("rb")
        except OSError as exc:
            return ParseResult(
                final_text=None,
                coverage={"final_text": "unknown", "structured_events": "unknown"},
                diagnostics=(ParseDiagnostic("jsonl_read_failed", type(exc).__name__),),
                degraded=True,
            )
        with file:
            for line_number, raw_line in enumerate(file, start=1):
                if len(raw_line) > self.max_line_bytes:
                    diagnostics.append(
                        ParseDiagnostic(
                            "jsonl_line_truncated",
                            f"line exceeded {self.max_line_bytes} bytes",
                            line_number,
                        )
                    )
                    raw_line = raw_line[: self.max_line_bytes]
                decoded = raw_line.decode("utf-8", errors="replace").strip()
                if "\ufffd" in decoded:
                    diagnostics.append(
                        ParseDiagnostic(
                            "jsonl_decode_replaced", "invalid UTF-8 replaced", line_number
                        )
                    )
                if not decoded:
                    continue
                try:
                    data = json.loads(decoded)
                except json.JSONDecodeError:
                    diagnostics.append(
                        ParseDiagnostic("jsonl_invalid_json", "invalid JSON object", line_number)
                    )
                    continue
                if not isinstance(data, Mapping):
                    diagnostics.append(
                        ParseDiagnostic("jsonl_schema_drift", "event is not an object", line_number)
                    )
                    continue
                event_type = dotted_get(data, self.type_field)
                text = dotted_get(data, self.text_field)
                kind = "event"
                if event_type == self.thinking_type_value:
                    kind = "thinking"
                elif self.tool_type_value is not None and event_type == self.tool_type_value:
                    kind = "tool"
                elif isinstance(text, str):
                    kind = "message"
                event = {
                    "kind": kind,
                    "sequence": len(events) + 1,
                    "text": text if isinstance(text, str) else None,
                }
                events.append(event)
                if isinstance(text, str) and text and kind == "message":
                    text_candidates.append(text)
                    if self.final_type_value is None or event_type == self.final_type_value:
                        final_candidates.append(text)
                if kind == "thinking":
                    thinking.append(event)
                elif kind == "tool":
                    tools.append(event)

                session = dotted_get(data, self.session_field)
                if isinstance(session, str) and session:
                    session_id = session
                usage = dotted_get(data, self.usage_field)
                if usage is not None:
                    if not isinstance(usage, Mapping):
                        diagnostics.append(
                            ParseDiagnostic(
                                "jsonl_usage_invalid", "usage is not an object", line_number
                            )
                        )
                    else:
                        recognized = False
                        for canonical, keys in _USAGE_FIELDS.items():
                            value, valid = _token_value(usage, keys)
                            if valid:
                                recognized = True
                                usage_observed[canonical] = True
                                usage_totals[canonical] += value
                        if not recognized:
                            diagnostics.append(
                                ParseDiagnostic(
                                    "jsonl_usage_schema_unknown",
                                    "usage contains no recognized non-negative token fields",
                                    line_number,
                                )
                            )

        usage_result = None
        if any(usage_observed.values()):
            usage_result = {
                name: usage_totals[name]
                for name, observed in usage_observed.items()
                if observed
            }
        final_text = (final_candidates or text_candidates or [None])[-1]
        degraded = bool(diagnostics)
        return ParseResult(
            final_text=final_text,
            events=tuple(events),
            thinking=tuple(thinking),
            tool_refs=tuple(tools),
            usage=usage_result,
            session_id=session_id,
            coverage={
                "final_text": "verified" if final_text is not None else "unknown",
                "structured_events": "degraded" if degraded else "verified",
                "token_usage": "verified" if usage_result is not None else "unknown",
                "thinking": "verified" if thinking else "unknown",
                "tools": "verified" if tools else "unknown",
                "session": "verified" if session_id else "unknown",
            },
            diagnostics=tuple(diagnostics),
            degraded=degraded,
        )


def _token_value(usage: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[int, bool]:
    for key in keys:
        value = dotted_get(usage, key)
        if value is None:
            continue
        if isinstance(value, bool):
            return 0, False
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0, False
        return (parsed, True) if parsed >= 0 else (0, False)
    return 0, False
