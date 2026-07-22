"""Offline parser for DeerFlow runner NDJSON plus its bounded summary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..parsers import EvidenceSet, ParseDiagnostic, ParseResult
from .runner import MAX_EVENT_BYTES, MAX_SUMMARY_BYTES


class DeerFlowParser:
    parser_id = "deerflow-ndjson"
    parser_version = "1"

    async def parse(self, evidence: EvidenceSet) -> ParseResult:
        diagnostics: list[ParseDiagnostic] = []
        events: list[dict[str, Any]] = []
        thinking: list[dict[str, Any]] = []
        tools: list[dict[str, Any]] = []
        final_text: str | None = None
        usage = {"input_tokens": 0, "output_tokens": 0}
        usage_seen = False
        usage_ids: set[tuple[str, str]] = set()
        message_text: dict[str, str] = {}

        try:
            file = evidence.stdout_path.open("rb")
        except OSError as exc:
            return ParseResult(
                final_text=None,
                coverage={"final_text": "unknown", "structured_events": "unknown"},
                diagnostics=(ParseDiagnostic("deerflow_read_failed", type(exc).__name__),),
                degraded=True,
            )
        with file:
            for line_number, raw in enumerate(file, start=1):
                if len(raw) > MAX_EVENT_BYTES:
                    diagnostics.append(
                        ParseDiagnostic(
                            "deerflow_event_truncated",
                            "event exceeded runner line limit",
                            line_number,
                        )
                    )
                    continue
                try:
                    item = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    diagnostics.append(
                        ParseDiagnostic("deerflow_invalid_json", "invalid NDJSON", line_number)
                    )
                    continue
                if not _valid_event(item):
                    diagnostics.append(
                        ParseDiagnostic("deerflow_schema_drift", "invalid StreamEvent", line_number)
                    )
                    continue
                event_type = item["type"]
                data = item["data"]
                event = {"kind": event_type, "sequence": len(events) + 1, "data": data}
                events.append(event)
                if event_type == "runner_diagnostic":
                    diagnostics.append(
                        ParseDiagnostic(
                            str(data.get("code", "deerflow_runner_diagnostic")),
                            "runner reported degraded event coverage",
                            line_number,
                        )
                    )
                    continue
                if event_type == "messages-tuple":
                    message_type = data.get("type")
                    content = data.get("content")
                    if message_type == "ai" and isinstance(content, str) and content:
                        message_id = data.get("id")
                        if isinstance(message_id, str) and message_id:
                            message_text[message_id] = message_text.get(message_id, "") + content
                            final_text = message_text[message_id]
                        else:
                            final_text = content
                    if message_type == "tool" or isinstance(data.get("tool_calls"), list):
                        tools.append(event)
                    reasoning = _reasoning_text(data)
                    if reasoning:
                        thinking.append({**event, "text": reasoning})
                    event_usage = _valid_usage(data.get("usage_metadata"))
                    if event_usage is not None:
                        identity = str(data.get("id") or f"line:{line_number}")
                        fingerprint = json.dumps(event_usage, sort_keys=True)
                        if (identity, fingerprint) not in usage_ids:
                            usage_ids.add((identity, fingerprint))
                            for key, value in event_usage.items():
                                usage[key] += value
                            usage_seen = True

        summary = _read_summary(evidence.root / "deerflow-summary.json", diagnostics)
        if summary is not None:
            summary_final = summary.get("final_text")
            if final_text is None and isinstance(summary_final, str):
                final_text = summary_final
            elif isinstance(summary_final, str) and summary_final != final_text:
                diagnostics.append(
                    ParseDiagnostic(
                        "deerflow_summary_final_conflict",
                        "summary final text differs from NDJSON",
                    )
                )
            summary_usage = _valid_usage(summary.get("usage"))
            if summary.get("usage") is not None and summary_usage is None:
                diagnostics.append(
                    ParseDiagnostic("deerflow_summary_usage_invalid", "invalid summary usage")
                )
            elif summary_usage is not None:
                if usage_seen and summary_usage != usage:
                    diagnostics.append(
                        ParseDiagnostic(
                            "deerflow_usage_conflict",
                            "summary usage differs from deduplicated NDJSON usage",
                        )
                    )
                elif not usage_seen:
                    usage = summary_usage
                    usage_seen = True
            if summary.get("status") not in {
                "completed",
                "provider_error",
                "recursion_limit",
            }:
                diagnostics.append(
                    ParseDiagnostic("deerflow_summary_status_invalid", "invalid summary status")
                )

        degraded = bool(diagnostics)
        return ParseResult(
            final_text=final_text,
            events=tuple(events),
            thinking=tuple(thinking),
            tool_refs=tuple(tools),
            usage=usage if usage_seen else None,
            session_id=(summary.get("thread_id") if summary else None),
            coverage={
                "final_text": "verified" if final_text is not None else "unknown",
                "structured_events": "degraded" if degraded else "verified",
                "token_usage": "verified" if usage_seen else "unknown",
                "thinking": "verified" if thinking else "unknown",
                "tools": "verified" if tools else "unknown",
                "session": "verified" if summary and summary.get("thread_id") else "unknown",
                "error": "verified" if summary is not None else "unknown",
            },
            diagnostics=tuple(diagnostics),
            degraded=degraded,
        )


def _valid_event(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("type") in {"values", "messages-tuple", "custom", "end", "runner_diagnostic"}
        and isinstance(value.get("data"), Mapping)
    )


def _valid_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, int] = {}
    for target, keys in {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
    }.items():
        item = next((value[key] for key in keys if key in value), None)
        if item is None:
            continue
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            return None
        result[target] = item
    return result or None


def _reasoning_text(data: Mapping[str, Any]) -> str | None:
    additional = data.get("additional_kwargs")
    if isinstance(additional, Mapping):
        reasoning = additional.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            return reasoning
    return None


def _read_summary(path: Path, diagnostics: list[ParseDiagnostic]) -> dict[str, Any] | None:
    try:
        size = path.stat().st_size
    except OSError:
        diagnostics.append(ParseDiagnostic("deerflow_summary_missing", "summary is missing"))
        return None
    if size > MAX_SUMMARY_BYTES:
        diagnostics.append(ParseDiagnostic("deerflow_summary_oversized", "summary exceeds 64 KiB"))
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        diagnostics.append(ParseDiagnostic("deerflow_summary_invalid", "summary is invalid"))
        return None
    if not isinstance(value, dict) or value.get("schema_version") != "1":
        diagnostics.append(ParseDiagnostic("deerflow_summary_invalid", "summary schema is invalid"))
        return None
    thread_id = value.get("thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        diagnostics.append(ParseDiagnostic("deerflow_summary_invalid", "thread_id is invalid"))
        return None
    return value
