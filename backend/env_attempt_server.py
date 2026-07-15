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

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


# ---------- wire inbound capture helpers (all fail-open) --------------------
#
# Every tool call through this server is also an inbound wire evidence
# record: request/response size, timing, and phase. Capture failures must
# never affect the tool call itself, the trace write, or the HTTP response.

# Per-attempt evidence sequence counter (only used for dedup ordering).
# Thread-safe. Restart recovers the starting seq from what's already on disk
# so a restarted process doesn't collide raw_ref with old spool lines.
_inbound_seq: dict[str, int] = {}
_inbound_seq_lock = threading.Lock()


def _recover_inbound_seq(data_path: Path, attempt_id: str) -> int:
    """Best-effort recovery of the inbound sequence counter from an
    already-flushed env-inbound spool. Evidence ID *uniqueness* is guaranteed
    by the process generation anchor (see wire.env_capture), so recovery only
    needs to keep the sequence roughly monotonic -- getting it wrong never
    causes an ID collision."""
    from backend.wire import paths as _wpaths
    from backend.wire.spool import find_spool_file, read_spool

    try:
        final = _wpaths.source_spool_file(data_path, attempt_id, "env-inbound")
        existing = find_spool_file(final)
        if existing is None:
            return 0
        max_seq = -1
        http_count = 0
        for rec in read_spool(existing).records:
            if rec.get("evidence_type") != "http_exchange":
                continue
            http_count += 1
            seq = (rec.get("extensions") or {}).get("x-lane.env-inbound-seq")
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq
        return max(max_seq + 1, http_count)
    except Exception:
        return 0


def _next_inbound_seq(attempt_id: str, data_path: Path | None = None) -> int:
    with _inbound_seq_lock:
        if attempt_id not in _inbound_seq and data_path is not None:
            _inbound_seq[attempt_id] = _recover_inbound_seq(data_path, attempt_id)
        n = _inbound_seq.get(attempt_id, 0)
        _inbound_seq[attempt_id] = n + 1
        return n


def _json_bytes(obj: Any) -> int | None:
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return None


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _wire_inbound_start(data_path: Path, attempt_id: str, body: dict[str, Any] | None):
    """On request arrival: snapshot (capture_enabled, phase) and register the
    in-flight request (so a spool close/drain waits for it to finish).
    Returns the state `_wire_inbound_finish` needs."""
    from backend.wire.env_capture import begin_request, snapshot_capture_state

    try:
        enabled, phase = snapshot_capture_state(data_path, attempt_id)
    except Exception:
        enabled, phase = False, "unknown"
    entry = begin_request(data_path, attempt_id, enabled)
    return _now_iso_utc(), time.monotonic(), _json_bytes(body), enabled, phase, entry


def _wire_inbound_finish(
    *,
    entry,
    data_path: Path,
    attempt_id: str,
    tool_name: str,
    started_at: str,
    t0: float,
    request_bytes: int | None,
    response: Any,
    status_code: int,
    capture_enabled: bool,
    phase: str,
) -> None:
    """Capture wrap-up: turns timing/size into an http_exchange evidence
    record (phase comes from the request-arrival snapshot, not now), then
    always ends the in-flight registration."""
    from backend.wire.env_capture import end_request, record_inbound_tool_call

    try:
        if capture_enabled:
            finished_at = _now_iso_utc()
            duration_ms = (time.monotonic() - t0) * 1000.0
            record_inbound_tool_call(
                entry=entry,
                data_path=data_path,
                attempt_id=attempt_id,
                tool_name=tool_name,
                request_bytes=request_bytes,
                response_bytes=_json_bytes(response) if response is not None else None,
                status_code=status_code,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                seq=_next_inbound_seq(attempt_id, data_path),
                phase=phase,
                capture_enabled=capture_enabled,
            )
    except Exception:
        logger.exception("wire inbound finish failed attempt=%s", attempt_id)
    finally:
        end_request(entry)


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

    # Wire inbound capture (size/timing/attempt/phase). Always fail-open: a
    # dedicated try/finally records on both the success and exception paths
    # without ever swallowing a genuine tool error.
    started_at, t0, req_bytes, cap_enabled, cap_phase, cap_entry = _wire_inbound_start(
        data_path, attempt_id, body
    )
    status_code = 200
    result: Any = None
    trace = TraceWriter(data_path=data_path, attempt_id=attempt_id, session_id=authorized.session_id)
    try:
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
                status_code = 500
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            conn.commit()
        return result
    finally:
        _wire_inbound_finish(
            entry=cap_entry,
            data_path=data_path,
            attempt_id=attempt_id,
            tool_name=tool_name,
            started_at=started_at,
            t0=t0,
            request_bytes=req_bytes,
            response=result,
            status_code=status_code,
            capture_enabled=cap_enabled,
            phase=cap_phase,
        )
