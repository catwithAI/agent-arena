"""Env Attempt Server — the HTTP endpoint an env's `mcp_server.py` calls into.

Fixed mount point: `POST /attempts/{attempt_id}/tools/{tool_name}`. Nothing
else (the frontend included) is allowed to reach past this layer to touch an
env's own DB or trace file directly.

Auth contract:

- `attempt_id` in the URL, `Authorization: Bearer <session_token>` header.
- The plaintext `session_token` is compared (sha256) against
  `attempts.session_token_hash`.
- Once an attempt has reached a terminal status (completed / gave_up /
  timeout / any failure state) → 401. No "late" trace writes after scoring.
- Unknown `attempt_id` → 404.

Dispatch flow:

1. Authenticate, then look up env_name + session_id for the attempt.
2. Find `env.db` at `<data_path>/attempts/{attempt_id}/env.db`; if missing,
   initialize it from `envs/<env_name>/schema.sql` (empty file -> executescript).
3. Look up the tool on `app.state.envs[env_name]`; missing -> 404.
4. Build an `EnvContext` and call the `RegisteredTool` wrapper (which times
   the call and writes the trace line automatically).
5. A tool raising internally -> wrapper already wrote an `is_error=true`
   trace line and re-raised -> this layer returns 500.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request

from .db import _open_sync, hash_session_token
from .env_loader import LoadedEnv
from lane.env_api import EnvContext, TraceWriter

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "gave_up",
        "timeout",
        "agent_unavailable",
        "auth_failed",
        "session_create_failed",
        "chat_failed",
        "scoring_failed",
    }
)


class AuthorizedAttempt:
    __slots__ = ("attempt_id", "env_name", "session_id", "status")

    def __init__(self, attempt_id: str, env_name: str, session_id: str, status: str) -> None:
        self.attempt_id = attempt_id
        self.env_name = env_name
        self.session_id = session_id
        self.status = status


def _parse_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing or malformed Bearer token")
    return authorization[len("Bearer ") :].strip()


def _query_attempt_sync(db_path: Path, attempt_id: str) -> tuple[str, str, str, str] | None:
    if not db_path.exists():
        return None
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT env_name, session_id, session_token_hash, status FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
    return tuple(row) if row else None


def _verify_attempt_token(
    request: Request,
    attempt_id: str,
    authorization: str | None = Header(default=None),
) -> AuthorizedAttempt:
    token = _parse_bearer(authorization)
    db_path: Path = request.app.state.db_path
    row = _query_attempt_sync(db_path, attempt_id)
    if row is None:
        raise HTTPException(status_code=404, detail="attempt not found")
    env_name, session_id, token_hash, status = row
    if hash_session_token(token) != token_hash:
        raise HTTPException(status_code=401, detail="invalid session token")
    if status in _TERMINAL_STATUSES:
        raise HTTPException(status_code=401, detail="attempt already finalized")
    return AuthorizedAttempt(attempt_id, env_name, session_id, status)


def _ensure_env_db(data_path: Path, attempt_id: str, env: LoadedEnv) -> Path:
    attempt_dir = data_path / "attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    db_path = attempt_dir / "env.db"
    schema_path = env.env_dir / "schema.sql"
    if not db_path.exists() and schema_path.is_file():
        with sqlite3.connect(db_path) as conn:
            conn.executescript(schema_path.read_text(encoding="utf-8"))
            conn.commit()
    elif not db_path.exists():
        db_path.touch()
    return db_path


router = APIRouter()


@router.post("/attempts/{attempt_id}/tools/{tool_name}")
async def call_tool(
    attempt_id: str,
    tool_name: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    authorized = _verify_attempt_token(request, attempt_id, authorization)
    envs: dict[str, LoadedEnv] = request.app.state.envs
    env = envs.get(authorized.env_name)
    if env is None:
        raise HTTPException(status_code=404, detail=f"env not loaded: {authorized.env_name}")
    tool = env.tools.get(tool_name)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"unknown tool: {tool_name}")

    data_path: Path = request.app.state.data_path
    db_path = _ensure_env_db(data_path, attempt_id, env)
    body = await request.json()
    arguments = body if isinstance(body, dict) else {}

    trace = TraceWriter(data_path=data_path, attempt_id=attempt_id, session_id=authorized.session_id)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ctx = EnvContext(
            attempt_id=attempt_id,
            session_id=authorized.session_id,
            db=conn,
            trace=trace,
        )
        try:
            result = await tool.call(ctx, **arguments)
        except Exception as exc:
            logger.exception("tool call failed attempt=%s tool=%s", attempt_id, tool_name)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        conn.commit()
    return result
