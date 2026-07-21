from __future__ import annotations


def test_selfcheck_returns_all_named_checks(test_client):
    response = test_client.get("/api/selfcheck")

    assert response.status_code == 200
    body = response.json()
    names = {check["name"] for check in body["checks"]}
    assert {
        "config",
        "env_scan",
        "env_api_import",
        "env_tool_registry",
        "env_token_auth",
        "trace_write",
    } <= names
    assert body["summary"]["fail"] == 0


def test_selfcheck_all_local_checks_pass(test_client):
    response = test_client.get("/api/selfcheck")

    checks = {check["name"]: check for check in response.json()["checks"]}
    for name in ("config", "env_scan", "env_api_import", "env_tool_registry",
                 "env_token_auth", "trace_write"):
        assert checks[name]["status"] == "ok", checks[name]


def test_list_envs_reports_availability_and_prereq_fields(test_client):
    response = test_client.get("/api/envs")

    assert response.status_code == 200
    for env in response.json():
        assert env["available"] is True
        assert env["load_error"] is None
        assert isinstance(env["prerequisite_warnings"], list)
        assert isinstance(env["agent_modalities"], list)
        assert isinstance(env["multi_turn"], bool)
