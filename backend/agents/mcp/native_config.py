"""Agent-owned config shape rendered under an Attempt-private root."""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

import rfc8785

from .base import McpDialectError, ResolvedMcpServer
from .json_file import _atomic_private_json
from .spi import McpPrepared


NativeRenderer = Callable[[tuple[ResolvedMcpServer, ...]], Mapping[str, Any]]


class NativeConfigDialect:
    dialect_id = "native-config"
    dialect_version = "1"

    def __init__(self, *, filename: str, renderer: NativeRenderer) -> None:
        if not filename or Path(filename).name != filename:
            raise ValueError("native MCP config filename must be a basename")
        self.filename = filename
        self.renderer = renderer

    async def prepare(
        self,
        servers: tuple[ResolvedMcpServer, ...],
        *,
        attempt_private: Path,
    ) -> McpPrepared | None:
        if not servers:
            return None
        root = (Path(attempt_private) / "mcp-native-config").resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / self.filename
        try:
            payload = dict(self.renderer(servers))
        except Exception as exc:
            raise McpDialectError(
                f"native MCP renderer failed: {type(exc).__name__}: {exc}"
            ) from exc
        _atomic_private_json(path, payload)
        redacted = tuple(server.redacted_shape() for server in servers)
        digest = hashlib.sha256(rfc8785.dumps(redacted)).hexdigest()
        cleaned = False

        async def cleanup() -> tuple[str, ...]:
            nonlocal cleaned
            if cleaned:
                return ()
            cleaned = True
            try:
                path.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    root.rmdir()
            except OSError:
                return (self.filename,)
            return ()

        return McpPrepared(
            dialect_id=self.dialect_id,
            dialect_version=self.dialect_version,
            private_root=root,
            config_path=path,
            config_hash=f"sha256:{digest}",
            redacted_shape=redacted,
            cleanup=cleanup,
        )
