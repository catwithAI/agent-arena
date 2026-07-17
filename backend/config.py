"""agent-arena backend configuration.

Contract:
- `load_settings()` returns a `Settings`
- `settings.lane.data_path` is a `pathlib.Path`, overridable via `LANE_DATA_PATH`
- `repr(settings)` never leaks provider API keys
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from pydantic import SecretStr

from .adapters.custom_cli import JsonlFieldMap
from .model_providers import ModelProviderSection

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("arena.yaml")


class CustomAgentSection(BaseModel):
    """Config-only way to plug an arbitrary CLI-based agent into agent-arena.
    See backend/adapters/custom_cli.py for the full field reference."""

    command: list[str]
    prompt_mode: str = "arg"  # stdin | file | arg
    output_format: str = "text"  # text | jsonl
    env: dict[str, str] = Field(default_factory=dict)
    jsonl_fields: JsonlFieldMap = Field(default_factory=JsonlFieldMap)
    mcp_config_flag: str | None = None


class SshClaudeCodeSection(BaseModel):
    """Optional: run Claude Code on a remote machine over SSH instead of a
    local subprocess (see backend/adapters/ssh_claude_code.py). Disabled
    unless `ssh_host` is set — most setups just use the local `claude-code`
    adapter. env: LANE_SSH_CLAUDE_HOST / LANE_SSH_CLAUDE_USER /
    LANE_SSH_CLAUDE_PASSWORD."""

    ssh_host: str | None = None
    ssh_user: str = "root"
    ssh_password: SecretStr | None = None
    max_budget_usd: float = 5.0


class LaneSection(BaseModel):
    # Where attempt traces / final_state / per-attempt sqlite DBs live.
    # env: LANE_DATA_PATH
    data_path: Path = Path("./data")

    # Where environment definitions (envs/<name>/) are discovered.
    # env: LANE_ENVS_PATH
    envs_path: Path = Path("./envs")

    # Address the agent-facing MCP tool server calls back to. Only needs to
    # change from loopback if the agent runs in a different network
    # namespace than this backend (e.g. inside a container).
    # env: LANE_PUBLIC_BASE_URL
    public_base_url: str = "http://127.0.0.1:8100"

    # Wire observability (backend/wire/): captures the raw HTTP/MCP traffic
    # between agent and model/tools for the trace viewer. Disabled by default
    # since agent-arena has no per-user auth yet — knowing a run/attempt ID is
    # enough to call the API, so parsed/full request bodies stay off until a
    # permission model exists. env: LANE_WIRE_BLOB_API_ENABLED
    wire_blob_api_enabled: bool = False
    # Upper bound on the wire capture policy a run/task may request; the
    # effective policy is the strictest intersection of this and the request.
    # None = no extra ceiling (still defaults to "off" unless requested).
    # env: LANE_WIRE_CAPTURE_MAX_POLICY
    wire_capture_max_policy: Literal["off", "metadata", "parsed", "full"] | None = None


class Settings(BaseModel):
    lane: LaneSection = Field(default_factory=LaneSection)
    # Third-party model providers usable by claude-code / codex adapters,
    # keyed by the prefix used in "<provider>/<model>" refs. API keys are
    # referenced by env var name only — never stored in this file.
    model_providers: dict[str, ModelProviderSection] = Field(default_factory=dict)
    # Suggested model strings shown in the UI's model dropdown (free text
    # still works).
    model_suggestions: list[str] = Field(default_factory=list)
    # Any other CLI-based agent, keyed by the agent name used in `POST
    # /runs`. This is how third parties bring their own agent to agent-arena
    # without writing a Python adapter.
    custom_agents: dict[str, CustomAgentSection] = Field(default_factory=dict)
    # Optional: Claude Code over SSH on a remote machine, registered as the
    # "ssh-claude-code" agent when ssh_host is set (see build_adapter in
    # backend/run_dispatch.py).
    ssh_claude_code: SshClaudeCodeSection = Field(default_factory=SshClaudeCodeSection)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fp:
        loaded = yaml.safe_load(fp) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} top level must be a mapping, got {type(loaded).__name__}")
    return loaded


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    lane = dict(data.get("lane") or {})
    if v := os.environ.get("LANE_DATA_PATH"):
        lane["data_path"] = v
    if v := os.environ.get("LANE_ENVS_PATH"):
        lane["envs_path"] = v
    if v := os.environ.get("LANE_PUBLIC_BASE_URL"):
        lane["public_base_url"] = v
    if v := os.environ.get("LANE_WIRE_BLOB_API_ENABLED"):
        lane["wire_blob_api_enabled"] = v.lower() in ("1", "true", "yes")
    if v := os.environ.get("LANE_WIRE_CAPTURE_MAX_POLICY"):
        lane["wire_capture_max_policy"] = v
    data["lane"] = lane

    ssh_claude = dict(data.get("ssh_claude_code") or {})
    if v := os.environ.get("LANE_SSH_CLAUDE_HOST"):
        ssh_claude["ssh_host"] = v
    if v := os.environ.get("LANE_SSH_CLAUDE_USER"):
        ssh_claude["ssh_user"] = v
    if v := os.environ.get("LANE_SSH_CLAUDE_PASSWORD"):
        ssh_claude["ssh_password"] = v
    if ssh_claude:
        data["ssh_claude_code"] = ssh_claude
    return data


def load_settings(config_path: Path | None = None) -> Settings:
    raw = _load_yaml(config_path or DEFAULT_CONFIG_PATH)
    raw = _apply_env_overrides(raw)
    settings = Settings(**raw)
    _log_settings(settings)
    return settings


def _log_settings(settings: Settings) -> None:
    safe = {
        "lane": {
            "data_path": str(settings.lane.data_path),
            "envs_path": str(settings.lane.envs_path),
            "public_base_url": settings.lane.public_base_url,
            "wire_blob_api_enabled": settings.lane.wire_blob_api_enabled,
            "wire_capture_max_policy": settings.lane.wire_capture_max_policy,
        },
        # only provider names and kind, never base_url / api_key_env target
        "model_providers": {name: p.kind for name, p in settings.model_providers.items()},
        "ssh_claude_code_enabled": settings.ssh_claude_code.ssh_host is not None,
    }
    logger.info("agent-arena settings: %s", safe)
