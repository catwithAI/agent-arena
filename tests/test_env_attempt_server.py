from __future__ import annotations

from backend.db import hash_session_token, init_db, insert_attempt, insert_task, new_session_token


async def _seed_attempt(db_path, *, status: str = "running") -> tuple[str, str]:
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
    await insert_attempt(
        db_path,
        {
            "id": "att_1",
            "run_id": "run_1",
            "task_id": "task_1",
            "env_name": "order-desk",
            "agent_name": "claude-code",
            "model": None,
            "status": status,
            "session_id": "sess_1",
            "session_token_hash": hash_session_token(token),
            "external_refs_json": "{}",
            "event_count": 0,
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    return "att_1", token


def test_tool_call_requires_bearer_token(test_app):
    app, client = test_app
    resp = client.post("/attempts/att_1/tools/catalog_search", json={"query": "x"})
    assert resp.status_code == 401


async def test_tool_call_rejects_wrong_token(test_app):
    app, client = test_app
    attempt_id, _token = await _seed_attempt(app.state.db_path)
    resp = client.post(
        f"/attempts/{attempt_id}/tools/catalog_search",
        json={"query": "algorithms"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


async def test_tool_call_succeeds_with_valid_token(test_app):
    app, client = test_app
    attempt_id, token = await _seed_attempt(app.state.db_path)
    resp = client.post(
        f"/attempts/{attempt_id}/tools/catalog_search",
        json={"query": "algorithms"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert "results" in resp.json()


async def test_tool_call_rejects_terminal_attempt(test_app):
    app, client = test_app
    attempt_id, token = await _seed_attempt(app.state.db_path, status="completed")
    resp = client.post(
        f"/attempts/{attempt_id}/tools/catalog_search",
        json={"query": "algorithms"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


async def test_unknown_tool_404s(test_app):
    app, client = test_app
    attempt_id, token = await _seed_attempt(app.state.db_path)
    resp = client.post(
        f"/attempts/{attempt_id}/tools/no_such_tool",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
