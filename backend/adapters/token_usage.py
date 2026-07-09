"""Token usage parsing helpers shared by agent adapters."""

from __future__ import annotations

import json
from typing import Any

INPUT_KEYS = ("input_tokens", "prompt_tokens", "inputTokens", "promptTokens")
OUTPUT_KEYS = ("output_tokens", "completion_tokens", "outputTokens", "completionTokens")


def usage_input_tokens(usage: dict[str, Any] | None) -> int:
    return _first_int(usage, INPUT_KEYS)


def usage_output_tokens(usage: dict[str, Any] | None) -> int:
    return _first_int(usage, OUTPUT_KEYS)


def result_usage_tokens(data: dict[str, Any]) -> tuple[int, int]:
    """Return exact usage from a result-like event.

    Claude-compatible CLIs have emitted several shapes over time:
    `usage.input_tokens`, OpenAI-style `prompt_tokens`, camelCase fields, and
    per-model `modelUsage` objects. Keep all of those compatible here.
    """
    usage = data.get("usage") or data.get("token_usage") or {}
    input_tokens = usage_input_tokens(usage if isinstance(usage, dict) else None)
    output_tokens = usage_output_tokens(usage if isinstance(usage, dict) else None)

    model_usage = data.get("modelUsage") or data.get("model_usage") or {}
    if isinstance(model_usage, dict):
        for item in model_usage.values():
            if not isinstance(item, dict):
                continue
            input_tokens += usage_input_tokens(item)
            output_tokens += usage_output_tokens(item)

    return input_tokens, output_tokens


def estimate_tokens_from_event(data: dict[str, Any]) -> tuple[int, int]:
    """Fallback estimate when a CLI emits zero usage.

    Intentionally conservative and flagged by callers via external refs; it
    prevents a run with rich events from appearing as "no token accounting".
    """
    msg_type = data.get("type")
    if msg_type == "assistant":
        message = data.get("message", {})
        return 0, _estimate_text_tokens(_message_text(message))
    if msg_type == "user":
        message = data.get("message", {})
        return _estimate_text_tokens(_message_text(message)), 0
    if msg_type == "system":
        return _estimate_text_tokens(json.dumps(data, ensure_ascii=False)), 0
    return 0, 0


def _first_int(usage: dict[str, Any] | None, keys: tuple[str, ...]) -> int:
    if not usage:
        return 0
    for key in keys:
        value = usage.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts: list[str] = []
    content = message.get("content", [])
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            for key in ("text", "thinking", "content"):
                value = block.get(key)
                if isinstance(value, str):
                    parts.append(value)
            tool_input = block.get("input")
            if tool_input is not None:
                parts.append(json.dumps(tool_input, ensure_ascii=False))
    return "\n".join(parts)


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + non_ascii_chars)
