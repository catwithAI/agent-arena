from __future__ import annotations

from pathlib import Path

import pytest

from backend.db import hash_session_token, init_db, insert_attempt, insert_task, new_session_token
from backend.models import AttemptModel


async def test_init_db_creates_tables(tmp_path: Path):
    db_path = tmp_path / "lane.db"
    await init_db(db_path)
    assert db_path.exists()


async def test_insert_task_and_attempt_roundtrip(tmp_path: Path):
    db_path = tmp_path / "lane.db"
    await init_db(db_path)
    await insert_task(
        db_path,
        {
            "id": "task_1",
            "env_name": "order-desk",
            "prompt": "do the thing",
            "context_json": "{}",
            "constraints_json": "{}",
            "timeout_seconds": 600,
            "source": "adhoc",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    token = new_session_token()
    model = AttemptModel(
        id="att_1",
        run_id="run_1",
        task_id="task_1",
        env_name="order-desk",
        agent_name="claude-code",
        status="queued",
        session_id="sess_1",
        session_token_hash=hash_session_token(token),
        created_at="2026-01-01T00:00:00Z",
    )
    await insert_attempt(db_path, model.to_db_row())


def test_attempt_model_rejects_token_in_external_refs():
    with pytest.raises(ValueError):
        AttemptModel(
            id="att_1",
            run_id="run_1",
            task_id="task_1",
            env_name="order-desk",
            session_id="sess_1",
            session_token_hash="hash",
            external_refs={"session_token": "leaked"},
        )


def test_hash_session_token_deterministic():
    token = "abc123"
    assert hash_session_token(token) == hash_session_token(token)
    assert hash_session_token(token) != hash_session_token("different")
