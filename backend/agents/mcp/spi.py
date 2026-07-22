"""Lifecycle contract for MCP dialects with setup/rollback side effects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Protocol


@dataclass(frozen=True)
class McpCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class McpCommandExecutor(Protocol):
    async def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> McpCommandResult: ...


CleanupCallback = Callable[[], Awaitable[tuple[str, ...]]]


@dataclass(frozen=True)
class McpPrepared:
    dialect_id: str
    dialect_version: str
    private_root: Path
    config_path: Path | None
    config_hash: str
    redacted_shape: tuple[dict[str, object], ...]
    cleanup: CleanupCallback


class McpLifecycleDialect(Protocol):
    dialect_id: str
    dialect_version: str

    async def prepare(self, *args, **kwargs) -> McpPrepared | None: ...
