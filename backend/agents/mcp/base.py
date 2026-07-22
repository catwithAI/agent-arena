"""MCP IR built exclusively from AdapterRunInput declarations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from ...adapters.base import AdapterRunInput


class McpDialectError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedMcpServer:
    name: str
    command: str
    args: tuple[str, ...]
    cwd: str | None
    env: Mapping[str, str]

    def redacted_shape(self) -> dict[str, object]:
        return {
            "name": self.name,
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
            "env_names": sorted(self.env),
        }


McpRewrite = Callable[[ResolvedMcpServer], ResolvedMcpServer]


def resolve_mcp_servers(
    task: AdapterRunInput,
    *,
    rewrite: McpRewrite | None = None,
) -> tuple[ResolvedMcpServer, ...]:
    resolved: list[ResolvedMcpServer] = []
    seen: set[str] = set()
    for server in task.mcp_servers:
        if server.name in seen:
            raise McpDialectError(f"duplicate MCP server name: {server.name!r}")
        seen.add(server.name)
        env = {
            **server.env,
            "LANE_ATTEMPT_ID": task.attempt_id,
            "LANE_SESSION_TOKEN": task.session_token,
            "LANE_BASE_URL": task.env_base_url,
        }
        item = ResolvedMcpServer(
            name=server.name,
            command=server.command,
            args=tuple(server.args),
            cwd=server.cwd,
            env=env,
        )
        resolved.append(rewrite(item) if rewrite else item)
    return tuple(resolved)
