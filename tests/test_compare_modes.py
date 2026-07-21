"""compare_mode validation and job-plan fan-out on POST /api/runs."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch


def _post(test_client, **overrides):
    body = {"env_name": "order-desk", "prompt": "hi", **overrides}
    with patch("backend.api.dispatch_attempt", new=AsyncMock(return_value=None)):
        return test_client.post("/api/runs", json=body)


# ---------- multi-model -------------------------------------------------------


def test_multi_model_creates_one_attempt_per_model(test_client):
    resp = _post(
        test_client,
        agents=["claude-code"],
        compare_mode="multi-model",
        models=["openai/gpt-5.2", "z-ai/glm-5.2"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["attempts"]) == 2
    assert all(a["agent"] == "claude-code" for a in body["attempts"])
    assert sorted(a["model"] for a in body["attempts"]) == [
        "openai/gpt-5.2", "z-ai/glm-5.2",
    ]


def test_multi_model_rejects_multiple_agents(test_client):
    resp = _post(
        test_client,
        agents=["claude-code", "codex"],
        compare_mode="multi-model",
        models=["m1", "m2"],
    )
    assert resp.status_code == 400


def test_multi_model_rejects_single_model(test_client):
    resp = _post(
        test_client, agents=["claude-code"], compare_mode="multi-model", models=["m1"]
    )
    assert resp.status_code == 400


def test_multi_model_rejects_models_dict(test_client):
    resp = _post(
        test_client,
        agents=["claude-code"],
        compare_mode="multi-model",
        models={"claude-code": "m1"},
    )
    assert resp.status_code == 400


# ---------- same-model --------------------------------------------------------


def test_same_model_expands_single_model_to_all_agents(test_client):
    resp = _post(
        test_client,
        agents=["claude-code", "codex"],
        compare_mode="same-model",
        model="z-ai/glm-5.2",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["attempts"]) == 2
    assert {a["model"] for a in body["attempts"]} == {"z-ai/glm-5.2"}


def test_same_model_accepts_full_agent_model_map(test_client):
    resp = _post(
        test_client,
        agents=["claude-code", "codex"],
        compare_mode="same-model",
        models={"claude-code": "or-cc/m", "codex": "or-codex/m"},
    )
    assert resp.status_code == 200, resp.text
    by_agent = {a["agent"]: a["model"] for a in resp.json()["attempts"]}
    assert by_agent == {"claude-code": "or-cc/m", "codex": "or-codex/m"}


def test_same_model_rejects_partial_map(test_client):
    resp = _post(
        test_client,
        agents=["claude-code", "codex"],
        compare_mode="same-model",
        models={"claude-code": "m"},
    )
    assert resp.status_code == 400


def test_same_model_rejects_single_agent(test_client):
    resp = _post(
        test_client, agents=["claude-code"], compare_mode="same-model", model="m"
    )
    assert resp.status_code == 400


def test_same_model_rejects_duplicate_agents(test_client):
    resp = _post(
        test_client,
        agents=["claude-code", "claude-code"],
        compare_mode="same-model",
        model="m",
    )
    assert resp.status_code == 400


def test_same_model_rejects_missing_model(test_client):
    resp = _post(
        test_client, agents=["claude-code", "codex"], compare_mode="same-model"
    )
    assert resp.status_code == 400


# ---------- shared behavior ---------------------------------------------------


def test_unknown_compare_mode_rejected(test_client):
    resp = _post(test_client, agents=["claude-code"], compare_mode="nope")
    assert resp.status_code == 400


def test_unknown_execution_rejected(test_client):
    resp = _post(test_client, agents=["claude-code"], execution="warp-speed")
    assert resp.status_code == 400


def test_run_records_compare_mode_and_execution(test_app):
    app, test_client = test_app
    resp = _post(
        test_client,
        agents=["claude-code"],
        compare_mode="multi-model",
        models=["m1", "m2"],
    )
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]

    detail = test_client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["compare_mode"] == "multi-model"
    assert body["execution"] == "parallel"  # default for non-same-model modes

    listing = test_client.get("/api/runs").json()
    row = next(r for r in listing if r["run_id"] == run_id)
    assert row["compare_mode"] == "multi-model"
    assert row["execution"] == "parallel"


def test_same_model_defaults_to_serial_execution(test_client):
    resp = _post(
        test_client,
        agents=["claude-code", "codex"],
        compare_mode="same-model",
        model="m",
    )
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    assert test_client.get(f"/api/runs/{run_id}").json()["execution"] == "serial"


def test_default_mode_unchanged(test_client):
    resp = _post(test_client, agents=["claude-code", "codex"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["attempts"]) == 2
    run_id = body["run_id"]
    assert test_client.get(f"/api/runs/{run_id}").json()["compare_mode"] == "multi-agent"


def test_capture_policy_forwarded_to_every_dispatch_job(test_client):
    mock = AsyncMock(return_value=None)
    with patch("backend.api.dispatch_attempt", new=mock):
        resp = test_client.post("/api/runs", json={
            "env_name": "order-desk",
            "prompt": "hi",
            "agents": ["claude-code"],
            "compare_mode": "multi-model",
            "models": ["m1", "m2"],
            "capture_policy": "full",
        })
    assert resp.status_code == 200, resp.text
    assert mock.await_count == 2
    for call in mock.await_args_list:
        assert call.kwargs["capture_policy"] == "full"


def test_openrouter_model_entry_shape_includes_modalities():
    from backend.api import _arch_modalities

    m = {"architecture": {"input_modalities": ["text", "image", 3], "output_modalities": ["text"]}}
    assert _arch_modalities(m, "input_modalities") == ["text", "image"]
    assert _arch_modalities(m, "output_modalities") == ["text"]
    assert _arch_modalities({}, "input_modalities") == []
    assert _arch_modalities({"architecture": "x"}, "input_modalities") == []


def test_migration_adds_columns_to_legacy_db(tmp_path):
    import sqlite3

    from backend.db import _init_db_sync, _open_sync

    db = tmp_path / "lane.db"
    # Simulate a pre-compare_mode database.
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,"
        " env_name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,"
        " started_at TEXT, ended_at TEXT)"
    )
    conn.execute(
        "INSERT INTO runs(id, task_id, env_name, status, created_at)"
        " VALUES('r1', 't1', 'order-desk', 'queued', 'x')"
    )
    conn.commit()
    conn.close()

    _init_db_sync(db)
    with _open_sync(db) as conn:
        row = conn.execute("SELECT compare_mode, execution FROM runs WHERE id='r1'").fetchone()
    assert row["compare_mode"] == "multi-agent"
    assert row["execution"] is None
