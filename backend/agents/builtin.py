"""Lazy builders for the existing adapters during registry migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_claude_code_adapter(*, settings: Any, model: str | None = None, **_: Any) -> Any:
    from ..adapters.claude_code import ClaudeCodeAdapter

    return ClaudeCodeAdapter(
        project_path=Path(".").resolve(),
        model=model or "sonnet",
        providers=settings.model_providers,
    )


def build_codex_adapter(*, settings: Any, model: str | None = None, **_: Any) -> Any:
    from ..adapters.codex import CodexAdapter

    return CodexAdapter(
        project_path=Path(".").resolve(),
        model=model or "gpt-5",
        providers=settings.model_providers,
    )


def build_ssh_claude_code_adapter(*, settings: Any, **_: Any) -> Any:
    from ..adapters.ssh_claude_code import SshClaudeCodeAdapter

    ssh = settings.ssh_claude_code
    if ssh.ssh_host is None:
        return None
    return SshClaudeCodeAdapter(
        ssh_host=ssh.ssh_host,
        ssh_user=ssh.ssh_user,
        ssh_password=ssh.ssh_password.get_secret_value() if ssh.ssh_password else "",
        project_path=Path(".").resolve(),
        max_budget_usd=ssh.max_budget_usd,
    )


def build_legacy_custom_adapter(*, settings: Any, spec: Any, **_: Any) -> Any:
    from ..adapters.custom_cli import CustomCliAdapter, CustomCliConfig

    custom = settings.custom_agents[spec.id]
    config = CustomCliConfig(name=spec.id, **custom.model_dump())
    return CustomCliAdapter(config, project_path=Path(".").resolve())
