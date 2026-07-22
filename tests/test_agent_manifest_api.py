from __future__ import annotations

import json

from backend.api import _public_agent_manifest


def test_public_manifest_projection_omits_launch_config_and_secrets(tmp_path):
    secret = "plaintext-secret-must-not-leak"
    control = tmp_path / ".agent-control"
    control.mkdir()
    (control / "agent-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "final",
                "agent": {
                    "id": "fixture-agent",
                    "display_name": "Fixture Agent",
                    "source": "config",
                    "version": "1.2.3",
                    "transport": "local-cli",
                },
                "model": {
                    "requested": "requested-model",
                    "effective": None,
                    "effective_status": "unknown",
                },
                "coverage": {"thinking": "degraded"},
                "cleanup": {"status": "not_needed"},
                "outcome": {"status": "completed"},
                "degradations": ["thinking unavailable"],
                "launch": {"argv_redacted": ["agent", "--token", secret]},
                "config_summary": {"api_key": secret},
            }
        ),
        encoding="utf-8",
    )

    response = _public_agent_manifest(tmp_path)

    assert response["status"] == "available"
    assert response["manifest"]["agent"]["version"] == "1.2.3"
    assert response["manifest"]["model"]["requested"] == "requested-model"
    assert response["manifest"]["coverage"] == {"thinking": "degraded"}
    assert secret not in json.dumps(response)
    assert "launch" not in response["manifest"]
    assert "config_summary" not in response["manifest"]


def test_public_manifest_missing_and_invalid_are_degraded_states(tmp_path):
    assert _public_agent_manifest(tmp_path) == {
        "status": "not_available",
        "manifest": None,
    }
    control = tmp_path / ".agent-control"
    control.mkdir()
    (control / "agent-manifest.json").write_text("not json", encoding="utf-8")
    assert _public_agent_manifest(tmp_path) == {"status": "invalid", "manifest": None}
