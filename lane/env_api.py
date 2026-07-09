"""Narrow interface exposed to environment authors.

Two things an env's `core.py` uses:

- `@env_tool(name=, description=, parameters=)` decorates a plain function and
  registers it into a module-level registry. The loader clears the registry
  before importing `core.py` and snapshots it right after, binding the result
  to that env's name.
- The decorated function receives an `EnvContext` as first argument when
  called through the MCP/HTTP dispatch path; the wrapper times the call,
  writes a trace line, and re-raises on error. Business functions do not need
  their own try/except for tracing.

Trace file convention: `<data_path>/attempts/{attempt_id}/trace.jsonl`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# ---------- TraceWriter ---------------------------------------------------


class TraceWriter:
    """Appends tool-call records as JSONL to a per-attempt file.

    Thread-safe: concurrent tool calls within one attempt serialize through a
    lock before writing a line.
    """

    def __init__(self, *, data_path: Path | str, attempt_id: str, session_id: str) -> None:
        self._data_path = Path(data_path)
        self._attempt_id = attempt_id
        self._session_id = session_id
        self._lock = threading.Lock()
        self._path = self._data_path / "attempts" / attempt_id / "trace.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        is_error: bool,
        duration_ms: int,
    ) -> None:
        row = {
            "timestamp": _now_iso(),
            "attempt_id": self._attempt_id,
            "session_id": self._session_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result,
            "is_error": is_error,
            "duration_ms": duration_ms,
        }
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as fp:
            fp.write(line)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------- EnvContext -----------------------------------------------------


@dataclass
class EnvContext:
    """Context object passed to every env tool call.

    Constructed by the attempt server before dispatch; business functions
    only read from it.
    """

    attempt_id: str
    session_id: str
    db: sqlite3.Connection
    trace: TraceWriter


# ---------- RegisteredTool --------------------------------------------------


ToolFunc = Callable[..., Any] | Callable[..., Awaitable[Any]]


@dataclass
class RegisteredTool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunc
    is_async: bool

    async def call(self, ctx: EnvContext, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            if self.is_async:
                result = await self.func(ctx, **kwargs)
            else:
                result = self.func(ctx, **kwargs)
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            ctx.trace.record(
                tool_name=self.name,
                arguments=kwargs,
                result={"error": str(exc)},
                is_error=True,
                duration_ms=duration_ms,
            )
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        ctx.trace.record(
            tool_name=self.name,
            arguments=kwargs,
            result=result,
            is_error=False,
            duration_ms=duration_ms,
        )
        return result


# ---------- module-level registry (transient) ------------------------------

_current_registry: dict[str, RegisteredTool] = {}


def clear_current_registry() -> None:
    _current_registry.clear()


def get_current_registry() -> dict[str, RegisteredTool]:
    return dict(_current_registry)


def env_tool(
    *, name: str, description: str, parameters: dict[str, Any] | None = None
) -> Callable[[ToolFunc], ToolFunc]:
    """Decorator env authors use to expose a Python function as an agent tool."""

    def decorator(func: ToolFunc) -> ToolFunc:
        import asyncio

        is_async = asyncio.iscoroutinefunction(func)
        if name in _current_registry:
            raise ValueError(f"duplicate tool name in this env: {name}")
        _current_registry[name] = RegisteredTool(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            func=func,
            is_async=is_async,
        )
        return func

    return decorator
