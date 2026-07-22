"""Agent-neutral task prompt rendering."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..adapters.base import AdapterRunInput, prompt_context, time_budget_notice


@dataclass(frozen=True)
class RenderedPrompt:
    text: str
    content_hash: str
    byte_count: int


def render_task_prompt(task: AdapterRunInput, *, message: str | None = None) -> RenderedPrompt:
    parts: list[str] = []
    notice = time_budget_notice(task.timeout_seconds)
    if notice:
        parts.extend((notice, ""))
    parts.append(task.task_prompt if message is None else message)
    context = prompt_context(task.task_context) if task.task_context else {}
    if context:
        parts.extend(("", "Context:", json.dumps(context, ensure_ascii=False, indent=2)))
    text = "\n".join(parts)
    encoded = text.encode("utf-8")
    return RenderedPrompt(
        text=text,
        content_hash=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        byte_count=len(encoded),
    )
