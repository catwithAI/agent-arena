"""Main sqlite DB (aiosqlite + sqlite3 dual-mode).

Two access paths:

- `open_db(data_path)` — async, used by the FastAPI lifespan. Returns a
  long-lived connection for read-mostly queries.
- `init_db(db_path)` / `insert_attempt(db_path, ...)` — async helpers that
  take a single sqlite file path and open short-lived connections. Used by
  the runner/evaluator and tests, which insert/update by attempt_id
  frequently and shouldn't serialize on the lifespan connection.

Per-attempt event streams (raw CLI events, thinking blocks) do not go into
this DB — they live at `<data_path>/attempts/{attempt_id}/events.jsonl` etc.
The `attempts` table only stores summary columns (`event_count`,
`last_event_at`).
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    env_name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    constraints_json TEXT NOT NULL DEFAULT '{}',
    timeout_seconds INTEGER NOT NULL DEFAULT 600,
    source TEXT NOT NULL DEFAULT 'file',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    env_name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    env_name TEXT NOT NULL,
    agent_name TEXT NOT NULL DEFAULT 'claude-code',
    model TEXT,
    status TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_token_hash TEXT NOT NULL,
    external_refs_json TEXT NOT NULL DEFAULT '{}',
    event_count INTEGER NOT NULL DEFAULT 0,
    last_event_at TEXT,
    thinking_count INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    token_usage_json TEXT NOT NULL DEFAULT '{}',
    cost_estimate REAL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    score_total INTEGER,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT,
    ended_at TEXT,
    created_at TEXT NOT NULL,
    execution_locus TEXT,
    permission_mode TEXT,
    workspace_root TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_run_id ON attempts(run_id);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    dimension TEXT NOT NULL,
    value INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_scores_attempt_id ON scores(attempt_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_FORBIDDEN_EXTERNAL_REF_KEYS = frozenset({"session_token", "session_token_hash"})


def _validate_external_refs(external_refs: dict[str, Any]) -> None:
    leaks = _FORBIDDEN_EXTERNAL_REF_KEYS & set(external_refs)
    if leaks:
        raise ValueError(f"external_refs must not contain sensitive fields: {sorted(leaks)}")


@contextmanager
def _open_sync(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


async def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()


async def open_db(data_path: Path) -> aiosqlite.Connection:
    db_path = Path(data_path) / "lane.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.commit()
    return conn


async def insert_task(db_path: Path, row: dict[str, Any]) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO tasks
               (id, env_name, prompt, context_json, constraints_json,
                timeout_seconds, source, created_at)
               VALUES (:id, :env_name, :prompt, :context_json, :constraints_json,
                       :timeout_seconds, :source, :created_at)""",
            row,
        )
        await conn.commit()


async def insert_run(db_path: Path, row: dict[str, Any]) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO runs (id, task_id, env_name, status, created_at, started_at, ended_at)
               VALUES (:id, :task_id, :env_name, :status, :created_at, :started_at, :ended_at)""",
            row,
        )
        await conn.commit()


async def insert_attempt(db_path: Path, row: dict[str, Any]) -> None:
    _validate_external_refs(__import__("json").loads(row.get("external_refs_json") or "{}"))
    async with aiosqlite.connect(db_path) as conn:
        columns = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        await conn.execute(f"INSERT INTO attempts ({columns}) VALUES ({placeholders})", row)
        await conn.commit()
