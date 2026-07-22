"""Process-wide runtime state, set up once during FastAPI's lifespan."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from .agents.registry import AgentRegistry
from .config import Settings
from .env_loader import LoadedEnv


@dataclass
class RuntimeState:
    settings: Settings
    db: aiosqlite.Connection
    db_path: Path
    data_path: Path
    agent_registry: AgentRegistry
    envs: dict[str, LoadedEnv] = field(default_factory=dict)
    active_tasks: dict[str, list[Any]] = field(default_factory=dict)


_state: RuntimeState | None = None


def set(state: RuntimeState) -> None:
    global _state
    _state = state


def get() -> RuntimeState:
    if _state is None:
        raise RuntimeError("runtime_state not initialized — is the app lifespan running?")
    return _state


def clear() -> None:
    global _state
    _state = None
