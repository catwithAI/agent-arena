"""Tokenized command-register MCP dialect with transactional rollback."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

import rfc8785

from .base import McpDialectError, ResolvedMcpServer
from .spi import McpCommandExecutor, McpPrepared


CommandBuilder = Callable[[ResolvedMcpServer], tuple[str, ...]]


class CommandRegisterDialect:
    dialect_id = "command-register"
    dialect_version = "1"

    def __init__(
        self,
        *,
        register_command: CommandBuilder,
        unregister_command: CommandBuilder,
        executor: McpCommandExecutor,
    ) -> None:
        self.register_command = register_command
        self.unregister_command = unregister_command
        self.executor = executor

    async def prepare(
        self,
        servers: tuple[ResolvedMcpServer, ...],
        *,
        attempt_private: Path,
    ) -> McpPrepared | None:
        if not servers:
            return None
        root = (Path(attempt_private) / "mcp-command-register").resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = _private_environment(root)
        registered: list[ResolvedMcpServer] = []
        cleaned = False

        async def rollback() -> tuple[str, ...]:
            nonlocal cleaned
            if cleaned:
                return ()
            cleaned = True
            failures: list[str] = []
            for server in reversed(registered):
                argv = _validated_argv(self.unregister_command(server), action="unregister")
                result = await self.executor(argv, cwd=root, env=env)
                if result.returncode != 0:
                    failures.append(server.name)
            return tuple(failures)

        for server in servers:
            argv = _validated_argv(self.register_command(server), action="register")
            try:
                result = await self.executor(argv, cwd=root, env=env)
            except Exception as exc:
                failures = await rollback()
                suffix = f"; rollback failed for {failures}" if failures else ""
                raise McpDialectError(
                    f"MCP registration for {server.name!r} raised {type(exc).__name__}{suffix}"
                ) from exc
            if result.returncode != 0:
                failures = await rollback()
                suffix = f"; rollback failed for {failures}" if failures else ""
                raise McpDialectError(
                    f"MCP registration for {server.name!r} exited with "
                    f"{result.returncode}{suffix}"
                )
            registered.append(server)

        redacted = tuple(server.redacted_shape() for server in servers)
        digest = hashlib.sha256(rfc8785.dumps(redacted)).hexdigest()
        return McpPrepared(
            dialect_id=self.dialect_id,
            dialect_version=self.dialect_version,
            private_root=root,
            config_path=None,
            config_hash=f"sha256:{digest}",
            redacted_shape=redacted,
            cleanup=rollback,
        )


def _private_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    config = root / "config"
    cache = root / "cache"
    for path in (home, config, cache):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_CACHE_HOME": str(cache),
    }


def _validated_argv(argv: tuple[str, ...], *, action: str) -> tuple[str, ...]:
    if not argv or any(not isinstance(value, str) or not value for value in argv):
        raise McpDialectError(f"MCP {action} command must be non-empty tokenized argv")
    return argv
