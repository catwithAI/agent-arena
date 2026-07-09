from __future__ import annotations


def test_healthz(test_client):
    resp = test_client.get("/api/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_list_envs_includes_bundled_envs(test_client):
    resp = test_client.get("/api/envs")
    assert resp.status_code == 200
    names = {e["name"] for e in resp.json()}
    assert "order-desk" in names
    assert "cpp-optimizer" in names


def test_list_agents_reports_claude_code_and_codex(test_client):
    resp = test_client.get("/api/agents")
    assert resp.status_code == 200
    names = {a["name"] for a in resp.json()}
    assert "claude-code" in names
    assert "codex" in names


def test_create_run_rejects_unknown_env(test_client):
    resp = test_client.post(
        "/api/runs",
        json={"env_name": "does-not-exist", "prompt": "hi", "agents": ["claude-code"]},
    )
    assert resp.status_code == 404


def test_create_run_rejects_blank_prompt(test_client):
    resp = test_client.post(
        "/api/runs",
        json={"env_name": "order-desk", "prompt": "   ", "agents": ["claude-code"]},
    )
    assert resp.status_code == 400


def test_create_run_rejects_unknown_agent(test_client):
    resp = test_client.post(
        "/api/runs",
        json={"env_name": "order-desk", "prompt": "hi", "agents": ["not-a-real-agent"]},
    )
    assert resp.status_code == 400


def test_get_missing_run_404s(test_client):
    resp = test_client.get("/api/runs/run_does_not_exist")
    assert resp.status_code == 404
