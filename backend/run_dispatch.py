"""Multi-agent comparison dispatch.

`POST /runs` creates one attempt per requested agent; they execute
concurrently (or serially, for UIs that want to show one at a time) via
`backend.runner`.

Agent resolution is intentionally simple and open-ended:
- "claude-code" / "codex" map to the reference adapters.
- Anything else is looked up in `settings.custom_agents` and built into a
  `CustomCliAdapter` — this is the extension point for third-party agents,
  see backend/adapters/custom_cli.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import runtime_state
from .adapters.base import AdapterRunInput
from .adapters.custom_cli import CustomCliAdapter, CustomCliConfig
from .config import Settings
from .db import _now_iso, _open_sync
from .runner import run_attempt

logger = logging.getLogger(__name__)

_DEFAULT_MODELS = {
    "claude-code": "sonnet",
    "codex": "gpt-5",
}


def known_agents(settings: Settings) -> tuple[str, ...]:
    return ("claude-code", "codex", *settings.custom_agents.keys())


def build_adapter(agent_name: str, settings: Settings, model: str | None = None) -> Any:
    if agent_name == "claude-code":
        from .adapters.claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter(
            project_path=Path(".").resolve(),
            model=model or _DEFAULT_MODELS["claude-code"],
            providers=settings.model_providers,
        )

    if agent_name == "codex":
        from .adapters.codex import CodexAdapter

        return CodexAdapter(
            project_path=Path(".").resolve(),
            model=model or _DEFAULT_MODELS["codex"],
            providers=settings.model_providers,
        )

    custom = settings.custom_agents.get(agent_name)
    if custom is not None:
        config = CustomCliConfig(name=agent_name, **custom.model_dump())
        return CustomCliAdapter(config, project_path=Path(".").resolve())

    return None


class _BoundAdapter:
    def __init__(self, *, adapter: Any, task: AdapterRunInput, env: Any, data_path: Path) -> None:
        self.attempt_id = task.attempt_id
        self._adapter = adapter
        self._task = task
        self._env = env
        self._data_path = data_path

    async def run(self):
        return await self._adapter.run(self._task, self._env, self._data_path)


async def dispatch(
    *,
    settings: Settings,
    attempt_id: str,
    agent_name: str,
    task_id: str,
    task_prompt: str,
    task_context: dict[str, Any],
    timeout_seconds: int,
    env_name: str,
    session_token: str,
    model: str | None = None,
) -> None:
    state = runtime_state.get()
    _mark_running(state.db_path, attempt_id)
    env = state.envs.get(env_name)
    if env is None:
        from .runner import _finalize_no_score

        logger.error("dispatch: env %s not found, skipping", env_name)
        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="agent_unavailable",
            error_code="env_not_loaded",
            error_message=f"env not loaded: {env_name}",
            pass_threshold=60,
        )
        _refresh_run_status(state.db_path, attempt_id)
        return

    adapter = build_adapter(agent_name, settings, model=model)
    if adapter is None:
        from .runner import _finalize_no_score

        logger.warning("dispatch: no adapter for agent %s, marking cli_not_found", agent_name)
        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="cli_not_found",
            error_code="adapter_not_configured",
            error_message=f"no adapter registered for agent '{agent_name}'",
            pass_threshold=60,
        )
        _refresh_run_status(state.db_path, attempt_id)
        return

    task = AdapterRunInput(
        attempt_id=attempt_id,
        task_id=task_id,
        task_prompt=task_prompt,
        task_context=task_context,
        timeout_seconds=timeout_seconds,
        env_name=env_name,
        env_skill_id=f"lane/{env_name}",
        session_token=session_token,
        env_base_url=settings.lane.public_base_url,
    )
    bound = _BoundAdapter(adapter=adapter, task=task, env=env, data_path=state.data_path)
    await run_attempt(bound)
    _refresh_run_status(state.db_path, attempt_id)


def _mark_running(db_path: Path, attempt_id: str) -> None:
    with _open_sync(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET status='running', started_at=? WHERE id=?",
            (_now_iso(), attempt_id),
        )
        conn.commit()


def _refresh_run_status(db_path: Path, attempt_id: str) -> None:
    with _open_sync(db_path) as conn:
        row = conn.execute("SELECT run_id FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if not row:
            return
        run_id = row["run_id"]
        statuses = [
            r["status"]
            for r in conn.execute(
                "SELECT status FROM attempts WHERE run_id=?", (run_id,)
            ).fetchall()
        ]
        if all(s in _TERMINAL for s in statuses):
            conn.execute(
                "UPDATE runs SET status='completed', ended_at=? WHERE id=?",
                (_now_iso(), run_id),
            )
        conn.commit()


_TERMINAL = frozenset(
    {
        "completed",
        "gave_up",
        "timeout",
        "agent_unavailable",
        "auth_failed",
        "session_create_failed",
        "chat_failed",
        "scoring_failed",
        "cli_not_found",
        "cli_error",
    }
)
