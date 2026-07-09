"""agent-lane backend configuration.

Contract:
- `load_settings()` returns a `Settings`
- `settings.lane.data_path` is a `pathlib.Path`, overridable via `LANE_DATA_PATH`
- `repr(settings)` never leaks provider API keys
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .adapters.custom_cli import JsonlFieldMap
from .model_providers import ModelProviderSection

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("agentlane.yaml")


class CustomAgentSection(BaseModel):
    """Config-only way to plug an arbitrary CLI-based agent into agent-lane.
    See backend/adapters/custom_cli.py for the full field reference."""

    command: list[str]
    prompt_mode: str = "arg"  # stdin | file | arg
    output_format: str = "text"  # text | jsonl
    env: dict[str, str] = Field(default_factory=dict)
    jsonl_fields: JsonlFieldMap = Field(default_factory=JsonlFieldMap)
    mcp_config_flag: str | None = None


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
    # /runs`. This is how third parties bring their own agent to agent-lane
    # without writing a Python adapter.
    custom_agents: dict[str, CustomAgentSection] = Field(default_factory=dict)


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
    data["lane"] = lane
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
        },
        # only provider names and kind, never base_url / api_key_env target
        "model_providers": {name: p.kind for name, p in settings.model_providers.items()},
    }
    logger.info("agent-lane settings: %s", safe)
