"""Runner — chains create_attempt -> adapter.run -> evaluator -> terminal state.

Interface:

- `create_attempt(task_id, agent_name)` -> `(AttemptModel, cleartext_session_token)`
  - pulls db_path/data_path from runtime_state by default
  - one run maps to one attempt by default; callers that want to fan a
    single run out to several agents pass an explicit shared `run_id`.
- `run_attempt(adapter)` -> `RunAttemptResult`
  - `adapter` exposes a no-arg async `.run()` returning an AdapterResult-like
    object (attempt_id / status at minimum)
  - combines the adapter's status with the scorer's output to decide the
    terminal status
  - terminal status, event_count / last_event_at / score_total are all
    written back to the DB in one place

Terminal status decision table:

| adapter status | scorer behavior            | final attempt status |
|-----------------|----------------------------|-----------------------|
| completed        | ran ok, score >= threshold | completed             |
| completed        | ran ok, score < threshold  | gave_up               |
| completed        | raised                     | scoring_failed        |
| timeout          | not run                    | timeout               |
| cli_not_found / cli_error / auth_failed / agent_unavailable / ... | not run | unchanged |
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import runtime_state
from .db import _now_iso, _open_sync, hash_session_token, new_session_token
from .evaluator import evaluate, write_scores_sync, write_security_summary_sync
from .models import AttemptModel

logger = logging.getLogger(__name__)


# ---------- create_attempt --------------------------------------------------


async def create_attempt(
    task_id: str,
    agent_name: str = "claude-code",
    *,
    run_id: str | None = None,
    model: str | None = None,
) -> tuple[AttemptModel, str]:
    """Creates an attempt + session token. Returns (model, cleartext token).

    The cleartext token only appears in this return value. **The caller must
    hand it straight to the adapter and never persist it anywhere else** —
    `AttemptModel` has no plaintext token field, so `repr(attempt)` never
    leaks it.
    """
    state = runtime_state.get()
    return await asyncio.to_thread(
        _create_attempt_sync,
        state.db_path,
        task_id=task_id,
        agent_name=agent_name,
        run_id=run_id,
        model_name=model,
    )


def _create_attempt_sync(
    db_path: Path,
    *,
    task_id: str,
    agent_name: str,
    run_id: str | None,
    model_name: str | None = None,
) -> tuple[AttemptModel, str]:
    attempt_id = f"att_{uuid.uuid4().hex[:12]}"
    run_id = run_id or f"run_{attempt_id[4:]}"
    session_id = f"sess_{attempt_id[4:]}"
    token = new_session_token()
    token_hash = hash_session_token(token)
    now = _now_iso()
    with _open_sync(db_path) as conn:
        row = conn.execute("SELECT env_name FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"task not found: {task_id}")
        env_name = row[0]

        existing_run = conn.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
        if existing_run is None:
            conn.execute(
                "INSERT INTO runs(id, task_id, env_name, status, created_at)"
                " VALUES(?, ?, ?, 'queued', ?)",
                (run_id, task_id, env_name, now),
            )

        conn.execute(
            "INSERT INTO attempts("
            " id, run_id, task_id, env_name, agent_name, model, status,"
            " session_id, session_token_hash, external_refs_json,"
            " event_count, created_at"
            ") VALUES(?, ?, ?, ?, ?, ?, 'queued', ?, ?, '{}', 0, ?)",
            (attempt_id, run_id, task_id, env_name, agent_name, model_name, session_id, token_hash, now),
        )
        conn.commit()

    model = AttemptModel(
        id=attempt_id,
        run_id=run_id,
        task_id=task_id,
        env_name=env_name,
        agent_name=agent_name,
        model=model_name,
        status="queued",
        session_id=session_id,
        session_token_hash=token_hash,
        external_refs={},
        event_count=0,
        created_at=now,
    )
    return model, token


# ---------- run_attempt ------------------------------------------------------


@dataclass
class RunAttemptResult:
    attempt_id: str
    status: str
    score_total: int
    pass_threshold: int
    error_code: str | None = None
    error_message: str | None = None


class _AdapterLike(Protocol):
    async def run(self) -> Any: ...


_ADAPTER_TERMINAL_NO_SCORE = frozenset(
    {
        "timeout",
        "chat_failed",
        "auth_failed",
        "session_create_failed",
        "agent_unavailable",
        "cli_not_found",
        "cli_error",
    }
)


async def run_attempt(adapter: _AdapterLike, *, observer: Any | None = None) -> RunAttemptResult:
    """Full lifecycle for a single attempt. Every failure mode is captured
    into a terminal status; this never raises.

    `observer` is the wire capture lifecycle hook (backend/wire/lifecycle.py:
    `WireCaptureSession`) — advances capture phases around the adapter run
    and finalizes the wire manifest. Defaults to a no-op observer so callers
    that don't care about wire capture see identical behavior to before it
    existed.
    """
    from .wire.lifecycle import NullAttemptObserver

    observer = observer or NullAttemptObserver()
    try:
        return await _run_attempt_inner(adapter, observer)
    finally:
        try:
            await observer.attempt_end()
        except Exception:
            logger.exception("wire observer.attempt_end fail-open")


async def _run_attempt_inner(adapter: _AdapterLike, observer: Any) -> RunAttemptResult:
    state = runtime_state.get()

    try:
        async with observer.phase("agent_run"):
            adapter_result = await adapter.run()
    except Exception as exc:
        logger.exception("adapter.run() crashed")
        return _finalize_no_score(
            db_path=state.db_path,
            attempt_id=getattr(adapter, "attempt_id", "<unknown>"),
            status="agent_unavailable",
            error_code="adapter_crashed",
            error_message=str(exc),
            pass_threshold=60,
        )
    try:
        await observer.agent_result(adapter_result)
    except Exception:
        logger.exception("wire observer.agent_result fail-open")

    attempt_id = adapter_result.attempt_id
    adapter_status = adapter_result.status
    tool_call_count = _count_tool_calls(state.data_path, attempt_id)

    def _adapter_stats() -> dict[str, Any]:
        return dict(
            external_refs=getattr(adapter_result, "external_refs", {}),
            event_count=getattr(adapter_result, "events_count", 0),
            last_event_at=getattr(adapter_result, "last_event_at", None),
            thinking_count=getattr(adapter_result, "thinking_count", 0),
            tool_call_count=tool_call_count,
            token_usage=getattr(adapter_result, "token_usage", {}),
            cost_estimate=getattr(adapter_result, "cost_estimate", None),
            duration_ms=getattr(adapter_result, "duration_ms", 0),
        )

    attempt_row, task_row = _fetch_attempt_and_task_sync(state.db_path, attempt_id)
    if attempt_row is None or task_row is None:
        return RunAttemptResult(
            attempt_id=attempt_id,
            status="agent_unavailable",
            score_total=0,
            pass_threshold=60,
            error_code="attempt_or_task_missing",
            error_message=f"attempt_id={attempt_id} has no matching task/run in the DB",
        )
    env_name = attempt_row["env_name"]
    env = state.envs.get(env_name)
    if env is None:
        if adapter_status == "completed":
            return _finalize_no_score(
                db_path=state.db_path,
                attempt_id=attempt_id,
                status="scoring_failed",
                error_code="env_not_loaded",
                error_message=f"env not loaded: {env_name}",
                pass_threshold=60,
                **_adapter_stats(),
            )
        return _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status=adapter_status,
            error_code=adapter_result.error_code,
            error_message=adapter_result.error_message,
            pass_threshold=60,
            **_adapter_stats(),
        )

    pass_threshold = int(getattr(env, "meta", {}).get("pass_threshold", 60))

    if adapter_status in _ADAPTER_TERMINAL_NO_SCORE:
        return _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status=adapter_status,
            error_code=adapter_result.error_code,
            error_message=adapter_result.error_message,
            pass_threshold=pass_threshold,
            **_adapter_stats(),
        )

    task_dict = _row_to_task_dict(task_row)
    security_meta = dict(getattr(adapter_result, "security_meta", {}) or {})
    try:
        outcome = await asyncio.to_thread(
            evaluate,
            attempt_id=attempt_id,
            task=task_dict,
            env=env,
            data_path=state.data_path,
            scorer=env.scorer,
            security_meta=security_meta,
        )
    except Exception as exc:
        logger.exception("scorer raised for attempt=%s", attempt_id)
        return _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="scoring_failed",
            error_code="scorer_exception",
            error_message=str(exc),
            pass_threshold=pass_threshold,
            **_adapter_stats(),
        )

    final_status = "completed" if outcome.passed else "gave_up"
    await asyncio.to_thread(write_scores_sync, state.db_path, attempt_id, outcome.scores)
    await asyncio.to_thread(
        _write_security_columns_sync,
        state.db_path,
        attempt_id,
        security_meta,
        outcome.security,
    )
    await asyncio.to_thread(
        _finalize_with_score_sync,
        state.db_path,
        attempt_id=attempt_id,
        status=final_status,
        score_total=outcome.score_total,
        ended_at=_now_iso(),
        **_adapter_stats(),
    )

    return RunAttemptResult(
        attempt_id=attempt_id,
        status=final_status,
        score_total=outcome.score_total,
        pass_threshold=outcome.pass_threshold,
    )


# ---------- DB helpers -------------------------------------------------------


def _count_tool_calls(data_path: Path, attempt_id: str) -> int:
    """trace.jsonl is written by the env's tool wrapper as the agent calls
    tools, independent of the adapter — count lines directly."""
    trace_path = data_path / "attempts" / attempt_id / "trace.jsonl"
    if not trace_path.exists():
        return 0
    with trace_path.open("r", encoding="utf-8") as fp:
        return sum(1 for line in fp if line.strip())


def _write_security_columns_sync(
    db_path: Path,
    attempt_id: str,
    security_meta: dict,
    security_summary: dict | None,
) -> None:
    """Writes the execution-context snapshot (locus/permission_mode/
    workspace_root) plus the security summary columns."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET execution_locus=?, permission_mode=?, "
            "workspace_root=? WHERE id=?",
            (
                security_meta.get("execution_locus"),
                security_meta.get("permission_mode"),
                security_meta.get("workspace_root"),
                attempt_id,
            ),
        )
        conn.commit()
    write_security_summary_sync(db_path, attempt_id, security_summary)


def _fetch_attempt_and_task_sync(db_path: Path, attempt_id: str) -> tuple[dict | None, dict | None]:
    with _open_sync(db_path) as conn:
        att = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if att is None:
            return None, None
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (att["task_id"],)).fetchone()
        return dict(att), (dict(task) if task else None)


def _row_to_task_dict(row: dict) -> dict[str, Any]:
    return {
        "id": row["id"],
        "env_name": row["env_name"],
        "prompt": row["prompt"],
        "context": json.loads(row.get("context_json") or "{}"),
        "constraints": json.loads(row.get("constraints_json") or "{}"),
        "timeout_seconds": row.get("timeout_seconds", 600),
        "source": row.get("source", "file"),
    }


def _finalize_no_score(
    *,
    db_path: Path,
    attempt_id: str,
    status: str,
    error_code: str | None,
    error_message: str | None,
    pass_threshold: int,
    external_refs: dict[str, Any] | None = None,
    event_count: int = 0,
    last_event_at: str | None = None,
    thinking_count: int = 0,
    tool_call_count: int = 0,
    token_usage: dict[str, int] | None = None,
    cost_estimate: float | None = None,
    duration_ms: int = 0,
) -> RunAttemptResult:
    ended = _now_iso()
    with _open_sync(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET status=?, error_code=?, error_message=?,"
            " external_refs_json=?, event_count=?, last_event_at=?,"
            " thinking_count=?, tool_call_count=?, token_usage_json=?,"
            " cost_estimate=?, duration_ms=?, ended_at=?"
            " WHERE id=?",
            (
                status,
                error_code,
                error_message,
                json.dumps(external_refs or {}, ensure_ascii=False),
                event_count,
                last_event_at,
                thinking_count,
                tool_call_count,
                json.dumps(token_usage or {}, ensure_ascii=False),
                cost_estimate,
                duration_ms,
                ended,
                attempt_id,
            ),
        )
        conn.commit()
    return RunAttemptResult(
        attempt_id=attempt_id,
        status=status,
        score_total=0,
        pass_threshold=pass_threshold,
        error_code=error_code,
        error_message=error_message,
    )


def _finalize_with_score_sync(
    db_path: Path,
    *,
    attempt_id: str,
    status: str,
    score_total: int,
    external_refs: dict[str, Any],
    event_count: int,
    last_event_at: str | None,
    thinking_count: int = 0,
    tool_call_count: int = 0,
    token_usage: dict[str, int] | None = None,
    cost_estimate: float | None = None,
    duration_ms: int = 0,
    ended_at: str,
) -> None:
    with _open_sync(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET status=?, score_total=?, external_refs_json=?,"
            " event_count=?, last_event_at=?, thinking_count=?, tool_call_count=?,"
            " token_usage_json=?, cost_estimate=?, duration_ms=?, ended_at=?"
            " WHERE id=?",
            (
                status,
                score_total,
                json.dumps(external_refs, ensure_ascii=False),
                event_count,
                last_event_at,
                thinking_count,
                tool_call_count,
                json.dumps(token_usage or {}, ensure_ascii=False),
                cost_estimate,
                duration_ms,
                ended_at,
                attempt_id,
            ),
        )
        conn.commit()
