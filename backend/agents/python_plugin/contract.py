"""Minimal SDK-neutral contract exposed to custom Python Agent packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from ..mcp import ResolvedMcpServer


@dataclass(frozen=True)
class PythonAgentContext:
    attempt_id: str
    prompt: str
    workspace: Path
    model: str | None
    mcp_servers: tuple[ResolvedMcpServer, ...] = ()

    def artifact_path(self, relative_path: str) -> Path:
        """Return a path inside the Attempt workspace or fail closed."""
        if not relative_path or Path(relative_path).is_absolute():
            raise ValueError("artifact path must be a non-empty relative path")
        root = self.workspace.resolve()
        destination = (root / relative_path).resolve()
        if not destination.is_relative_to(root):
            raise ValueError("artifact path escapes the Attempt workspace")
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination


@dataclass(frozen=True)
class PythonAgentOutput:
    status: str = "completed"
    final_text: str | None = None
    events: tuple[Mapping[str, Any], ...] = ()
    thinking: tuple[Mapping[str, Any], ...] = ()
    usage: Mapping[str, int] = field(default_factory=dict)
    session_id: str | None = None
    artifacts: tuple[str, ...] = ()
    effective_model: str | None = None


class PythonAgentPlugin(Protocol):
    async def run(self, context: PythonAgentContext) -> PythonAgentOutput: ...
