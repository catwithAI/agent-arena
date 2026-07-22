"""Owner-only JSON ``mcpServers`` dialect."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import rfc8785

from .base import ResolvedMcpServer


@dataclass(frozen=True)
class McpRenderResult:
    path: Path
    config_hash: str
    redacted_shape: tuple[dict[str, object], ...]


class JsonFileDialect:
    dialect_id = "json-file"
    dialect_version = "1"

    def render(
        self,
        servers: tuple[ResolvedMcpServer, ...],
        *,
        attempt_private: Path,
        filename: str = "mcp.json",
    ) -> McpRenderResult | None:
        if not servers:
            return None
        path = Path(attempt_private) / filename
        payload = {
            "mcpServers": {
                server.name: {
                    "command": server.command,
                    "args": list(server.args),
                    **({"cwd": server.cwd} if server.cwd else {}),
                    "env": dict(server.env),
                }
                for server in servers
            }
        }
        redacted_shape = tuple(server.redacted_shape() for server in servers)
        config_hash = f"sha256:{hashlib.sha256(rfc8785.dumps(redacted_shape)).hexdigest()}"
        _atomic_private_json(path, payload)
        return McpRenderResult(
            path=path,
            config_hash=config_hash,
            redacted_shape=redacted_shape,
        )


def _atomic_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.chmod(temporary_name, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
