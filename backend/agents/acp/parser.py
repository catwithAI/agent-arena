"""Normalize ACP v1 session notifications into the shared parser contract."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Mapping

from ..parsers import ParseDiagnostic, ParseResult
from .client import AcpRunResult


class AcpParser:
    version = "acp-v1@1"

    def parse(self, result: AcpRunResult) -> ParseResult:
        messages: OrderedDict[str, list[str]] = OrderedDict()
        thinking: list[Mapping[str, Any]] = []
        events: list[Mapping[str, Any]] = []
        tools: list[Mapping[str, Any]] = []
        diagnostics: list[ParseDiagnostic] = []
        usage: dict[str, int] = {}

        for sequence, envelope in enumerate(result.messages):
            method = envelope.get("method")
            params = envelope.get("params")
            if method == "session/request_permission":
                event = {"type": "permission_request", "sequence": sequence, "data": params or {}}
                events.append(event)
                continue
            if method != "session/update" or not isinstance(params, Mapping):
                continue
            update = params.get("update")
            if not isinstance(update, Mapping):
                diagnostics.append(ParseDiagnostic("acp.invalid_update", "session/update has no update"))
                continue
            kind = update.get("sessionUpdate")
            event = {"type": str(kind or "unknown"), "sequence": sequence, "data": dict(update)}
            events.append(event)
            if kind == "agent_message_chunk":
                content = update.get("content")
                text = content.get("text") if isinstance(content, Mapping) else None
                if isinstance(text, str):
                    key = str(update.get("messageId") or "message:default")
                    messages.setdefault(key, []).append(text)
            elif kind == "agent_thought_chunk":
                thinking.append(event)
            elif kind in {"tool_call", "tool_call_update"}:
                tools.append(event)
            elif kind == "usage_update":
                used, size = update.get("used"), update.get("size")
                if isinstance(used, int) and used >= 0:
                    usage["context_tokens"] = used
                if isinstance(size, int) and size >= 0:
                    usage["context_window_tokens"] = size

        if result.permission_unanswered:
            diagnostics.append(
                ParseDiagnostic(
                    "acp.permission_unanswered",
                    "permission request had no configured answer and was cancelled",
                )
            )
        final_text = "\n".join("".join(chunks) for chunks in messages.values()) or None
        return ParseResult(
            final_text=final_text,
            events=tuple(events),
            thinking=tuple(thinking),
            tool_refs=tuple(tools),
            usage=usage or None,
            session_id=result.session_id,
            coverage={
                "trajectory": "verified",
                "tools": "verified",
                "thinking": "verified" if thinking else "unknown",
                "token_usage": "partial" if usage else "unknown",
                "permission": "partial" if result.permission_unanswered else "verified",
            },
            diagnostics=tuple(diagnostics),
            degraded=result.permission_unanswered or bool(diagnostics),
        )
