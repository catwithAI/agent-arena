"""Strict Attempt-private DeerFlow v2 configuration generation."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ...model_providers import ModelProviderSection, parse_model_ref, resolve_api_key


class DeerFlowConfigError(ValueError):
    pass


class DeerFlowOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    subagent: bool = False
    thinking: bool = True
    plan_mode: bool = False
    summarize: bool = False
    recursion_limit: int = Field(default=1000, ge=1, le=10_000)
    allow_host_bash: bool = False


@dataclass(frozen=True)
class DeerFlowPrivateConfig:
    project_dir: Path
    home_dir: Path
    config_path: Path
    child_env: dict[str, str]
    summary: dict[str, Any]
    options: DeerFlowOptions


_INTEGRATIONS = {
    "anthropic": "langchain_anthropic:ChatAnthropic",
    "openai-chat": "langchain_openai:ChatOpenAI",
    "openai-responses": "langchain_openai:ChatOpenAI",
}


def build_private_config(
    *,
    private_root: Path,
    requested_model: str | None,
    providers: dict[str, ModelProviderSection],
    raw_options: dict[str, Any] | None = None,
    workspace: Path | None = None,
    attempt_root: Path | None = None,
) -> DeerFlowPrivateConfig:
    if not requested_model:
        raise DeerFlowConfigError("DeerFlow requires an explicit provider/model")
    model_ref = parse_model_ref(requested_model, providers)
    if model_ref.provider is None:
        raise DeerFlowConfigError("DeerFlow model must use a configured provider/model prefix")
    provider = providers[model_ref.provider]
    if not model_ref.model.strip():
        raise DeerFlowConfigError("DeerFlow model id cannot be empty")
    _validate_base_url(provider.base_url)
    secret = resolve_api_key(provider)
    if not secret:
        raise DeerFlowConfigError(f"model provider {model_ref.provider!r} has no available API key")
    options = DeerFlowOptions.model_validate(raw_options or {})
    if options.summarize:
        raise DeerFlowConfigError(
            "summarize=true is unsupported by the pinned DeerFlow embedded client"
        )
    workspace_root: Path | None = None
    if workspace is not None:
        if attempt_root is None:
            raise DeerFlowConfigError("attempt_root is required with workspace")
        workspace_root = validate_workspace(workspace, attempt_root=attempt_root)
    project = Path(private_root) / "project"
    home = Path(private_root) / "home"
    project.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = project / "config.yaml"
    secret_env = "DEERFLOW_ARENA_MODEL_API_KEY"
    integration = _INTEGRATIONS[provider.kind]
    model: dict[str, Any] = {
        "name": "arena-model",
        "display_name": requested_model,
        "use": integration,
        "model": model_ref.model,
        "api_key": f"${secret_env}",
        "max_retries": 2,
    }
    if provider.kind == "anthropic":
        model["base_url"] = provider.base_url
        model["default_request_timeout"] = 600.0
    else:
        model["base_url"] = provider.base_url
        model["request_timeout"] = 600.0
    if provider.kind == "openai-responses":
        model.update(use_responses_api=True, output_version="responses/v1")

    tools = [
        {"name": "ls", "group": "file:read", "use": "deerflow.sandbox.tools:ls_tool"},
        {
            "name": "read_file",
            "group": "file:read",
            "use": "deerflow.sandbox.tools:read_file_tool",
        },
        {
            "name": "glob",
            "group": "file:read",
            "use": "deerflow.sandbox.tools:glob_tool",
            "max_results": 200,
        },
        {
            "name": "grep",
            "group": "file:read",
            "use": "deerflow.sandbox.tools:grep_tool",
            "max_results": 100,
        },
        {
            "name": "write_file",
            "group": "file:write",
            "use": "deerflow.sandbox.tools:write_file_tool",
        },
        {
            "name": "str_replace",
            "group": "file:write",
            "use": "deerflow.sandbox.tools:str_replace_tool",
        },
    ]
    if options.allow_host_bash:
        tools.append({"name": "bash", "group": "bash", "use": "deerflow.sandbox.tools:bash_tool"})
    payload = {
        "config_version": 14,
        "models": [model],
        "token_usage": {"enabled": True},
        "tool_groups": [
            {"name": "file:read"},
            {"name": "file:write"},
            {"name": "bash"},
        ],
        "tools": tools,
        "sandbox": {
            "use": "deerflow.sandbox.local:LocalSandboxProvider",
            "allow_host_bash": options.allow_host_bash,
            "mounts": [
                {
                    "host_path": str(workspace_root),
                    "container_path": "/mnt/arena-workspace",
                    "read_only": False,
                }
            ]
            if workspace_root is not None
            else [],
        },
        "suggestions": {"enabled": False},
    }
    _atomic_private_yaml(config_path, payload)
    return DeerFlowPrivateConfig(
        project_dir=project,
        home_dir=home,
        config_path=config_path,
        child_env={secret_env: secret},
        summary={
            "requested_model": requested_model,
            "effective_model": "arena-model",
            "provider": model_ref.provider,
            "provider_kind": provider.kind,
            "integration": integration,
            "base_url": provider.base_url,
            "secret_ref": secret_env,
            "options": options.model_dump(),
            "workspace": "/mnt/arena-workspace" if workspace_root is not None else None,
        },
        options=options,
    )


def validate_workspace(workspace: Path, *, attempt_root: Path) -> Path:
    lexical = Path(workspace).absolute()
    attempt = Path(attempt_root).resolve(strict=True)
    if lexical.name != "skill_workspace":
        raise DeerFlowConfigError("DeerFlow workspace must be the Attempt skill_workspace")
    if lexical.is_symlink():
        raise DeerFlowConfigError("DeerFlow workspace cannot be a symlink")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise DeerFlowConfigError("DeerFlow workspace does not exist") from exc
    if resolved.parent != attempt or not resolved.is_dir():
        raise DeerFlowConfigError("DeerFlow workspace is outside its Attempt root")
    for path in resolved.rglob("*"):
        if path.is_symlink():
            raise DeerFlowConfigError(
                f"DeerFlow workspace contains a symlink: {path.relative_to(resolved)}"
            )
    return resolved


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DeerFlowConfigError("provider base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise DeerFlowConfigError("provider base_url must not contain credentials")


def _atomic_private_yaml(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.chmod(temporary_name, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            yaml.safe_dump(payload, file, sort_keys=False, allow_unicode=True)
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
