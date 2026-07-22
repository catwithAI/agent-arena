from __future__ import annotations

import json
import sys

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app


def _missing_auth_profile() -> dict:
    return {
        "schema_version": "1",
        "display_name": "Missing Auth Profile",
        "transport": "local-cli",
        "implementation": {"kind": "profile-runtime"},
        "availability": {
            "executable": sys.executable,
            "version_command": [sys.executable, "--version"],
        },
        "launch": {"executable": sys.executable, "args": ["-c", "print('unused')"]},
        "prompt": {"mode": "stdin"},
        "model": {"binding": "unsupported"},
        "auth": [
            {"name": "missing", "env_var": "FIXTURE_AGENT_SECRET"},
            {"name": "present", "env_var": "FIXTURE_PRESENT_SECRET"},
        ],
        "mcp": {"dialect": "unsupported"},
        "output": {"parser": "text"},
        "capabilities": {"single_turn": "declared"},
        "isolation": {"execution_locus": "host", "network_required": "none"},
        "metadata": {
            "description": "Catalog fixture",
            "installation_url": "https://example.invalid/install",
        },
    }


def _broken_plugin() -> dict:
    return {
        "schema_version": "1",
        "display_name": "Broken Plugin",
        "transport": "python-sdk",
        "implementation": {
            "kind": "plugin",
            "import_path": "package_that_does_not_exist.agent:build",
        },
        "availability": {"executable": sys.executable},
        "prompt": {"mode": "driver-owned"},
        "model": {"binding": "agent-default"},
        "mcp": {"dialect": "unsupported"},
        "output": {"parser": "text"},
        "isolation": {"execution_locus": "host", "network_required": "none"},
    }


def test_agents_v2_schema_keeps_unavailable_and_isolates_broken_plugin(
    settings: Settings, monkeypatch
):
    secret = "must-never-appear-in-agent-catalog"
    monkeypatch.delenv("FIXTURE_AGENT_SECRET", raising=False)
    monkeypatch.setenv("FIXTURE_PRESENT_SECRET", secret)
    configured = Settings(
        lane=settings.lane,
        agents={
            "profiles": {"missing-auth": _missing_auth_profile()},
            "plugins": {"broken-plugin": _broken_plugin()},
        },
    )
    app = create_app(configured)

    with TestClient(app) as client:
        response = client.get("/api/agents")

    assert response.status_code == 200
    encoded = json.dumps(response.json())
    assert secret not in encoded
    catalog = {item["id"]: item for item in response.json()}
    assert {"claude-code", "codex", "deerflow", "missing-auth", "broken-plugin"} <= catalog.keys()

    missing = catalog["missing-auth"]
    assert set(missing) == {
        "id",
        "name",
        "display_name",
        "source",
        "transport",
        "availability",
        "version",
        "status",
        "detail",
        "cli_path",
        "capabilities",
        "model_support",
        "metadata",
        "spec_hash",
        "warnings",
    }
    assert missing["availability"] == {
        "status": "missing_auth",
        "version": None,
        "reason": "missing required environment variables: FIXTURE_AGENT_SECRET",
    }
    assert missing["version"] is None
    assert missing["status"] == "not_found"
    assert missing["source"] == "config"
    assert missing["transport"] == "local-cli"
    assert missing["capabilities"]["single_turn"]["state"] == "declared"
    assert missing["model_support"]["binding"] == "unsupported"
    assert missing["metadata"]["description"] == "Catalog fixture"

    broken = catalog["broken-plugin"]
    assert broken["availability"]["status"] == "available"
    assert broken["source"] == "plugin"
