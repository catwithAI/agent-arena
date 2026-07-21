from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app


@pytest.fixture
def data_path(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def settings(data_path: Path) -> Settings:
    return Settings(
        lane={
            "data_path": data_path,
            "envs_path": Path("envs"),
            "public_base_url": "http://127.0.0.1:8100",
        }
    )


@pytest.fixture
def test_app(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield app, client


@pytest.fixture
def test_client(test_app):
    return test_app[1]


@dataclass
class CompletedRun:
    id: str  # run_id
    attempt_id: str


@pytest.fixture
def completed_run(test_app, data_path: Path) -> CompletedRun:
    """Seed a run + completed attempt + trace + final_state + scores directly."""
    from backend.db import (
        _init_db_sync,
        _open_sync,
        hash_session_token,
        new_session_token,
        resolve_db_path,
    )

    data_path.mkdir(parents=True, exist_ok=True)
    db_path = resolve_db_path(data_path)
    _init_db_sync(db_path)

    run_id = "run_completed_1"
    attempt_id = "att_completed_1"
    token_hash = hash_session_token(new_session_token())
    with _open_sync(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(id, env_name, prompt, context_json, constraints_json,"
            " timeout_seconds, source, created_at)"
            " VALUES('order_001','order-desk','p','{}','{}',600,'file','2026-04-30T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO runs(id, task_id, env_name, status, created_at, ended_at)"
            " VALUES(?, 'order_001', 'order-desk', 'completed', '2026-04-30T00:00:00Z',"
            " '2026-04-30T00:00:30Z')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO attempts("
            " id, run_id, task_id, env_name, agent_name, status,"
            " session_id, session_token_hash, external_refs_json,"
            " event_count, last_event_at, score_total, started_at, ended_at, created_at"
            ") VALUES(?, ?, 'order_001', 'order-desk', 'claude-code', 'completed',"
            " 'env_x', ?, '{}', 2, '2026-04-30T00:00:25Z', 80, '2026-04-30T00:00:00Z',"
            " '2026-04-30T00:00:30Z', '2026-04-30T00:00:00Z')",
            (attempt_id, run_id, token_hash),
        )
        conn.execute(
            "INSERT INTO scores(attempt_id, dimension, value, detail) VALUES(?, ?, ?, ?)",
            (attempt_id, "task_completion", 90, "ok"),
        )
        conn.commit()

    attempt_dir = data_path / "attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "trace.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-04-30T00:00:10Z",
                "attempt_id": attempt_id,
                "env_session_id": "env_x",
                "tool_name": "list_orders",
                "arguments": {},
                "result": {"orders": []},
                "is_error": False,
                "duration_ms": 5,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (attempt_dir / "final_state.json").write_text(
        json.dumps({"orders": 1}), encoding="utf-8"
    )
    return CompletedRun(id=run_id, attempt_id=attempt_id)
