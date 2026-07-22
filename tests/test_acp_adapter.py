from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from backend.adapters.base import AdapterRunInput
from backend.agents.acp.adapter import AcpTransportAdapter, _write_transcript
from backend.agents.registry import AgentRegistry
from backend.config import Settings

from test_acp_client import _FAKE_SERVER


def _entry(agent_id: str, name: str) -> dict:
    return {
        "id": agent_id,
        "name": name,
        "version": "1.2.3",
        "description": "ACP fixture",
        "repository": "https://example.test/source",
        "license": "MIT",
        "distribution": {"npx": {"package": f"@example/{agent_id}@1.2.3", "args": ["--acp"]}},
    }


def _settings(*, mode: str = "normal", permission_answers=None) -> Settings:
    return Settings(
        agents={
            "acp": {
                "acp:alpha@1.2.3": {
                    "command": [sys.executable, "-u", "-c", _FAKE_SERVER, mode],
                    "registry_sha256": "a" * 64,
                    "registry_entry": _entry("alpha", "Alpha"),
                    "permission_answers": permission_answers or {},
                },
                "acp:beta@1.2.3": {
                    "command": [sys.executable, "-u", "-c", _FAKE_SERVER, "normal"],
                    "registry_sha256": "b" * 64,
                    "registry_entry": _entry("beta", "Beta"),
                },
            }
        }
    )


def _task() -> AdapterRunInput:
    return AdapterRunInput(
        attempt_id="attempt-acp",
        task_id="task-acp",
        task_prompt="hello",
        task_context={},
        timeout_seconds=3,
        env_name="fixture",
        env_skill_id="lane/fixture",
        session_token="secret",
        env_base_url="http://127.0.0.1:8100",
    )


def test_two_registry_agents_reuse_one_transport_and_expose_pins():
    registry = AgentRegistry.from_settings(_settings())
    alpha = registry.resolve("acp:alpha@1.2.3")
    beta = registry.resolve("acp:beta@1.2.3")

    assert alpha.spec.implementation.import_path == beta.spec.implementation.import_path
    assert alpha.spec.transport == beta.spec.transport == "acp"
    descriptors = {item["id"]: item for item in registry.describe_all()}
    assert descriptors["acp:alpha@1.2.3"]["metadata"]["registry_sha256"] == "sha256:" + "a" * 64
    assert "npx" in descriptors["acp:beta@1.2.3"]["metadata"]["distribution"]


@pytest.mark.asyncio
async def test_acp_adapter_writes_standard_outputs_and_final_manifest(tmp_path):
    settings = _settings()
    resolved = AgentRegistry.from_settings(settings).resolve("acp:alpha@1.2.3")
    adapter = resolved.build_adapter()

    result = await adapter.run(_task(), SimpleNamespace(), tmp_path)

    assert result.status == "completed"
    assert result.events_count == 4
    assert result.token_usage["context_tokens"] == 12
    attempt = tmp_path / "attempts" / "attempt-acp"
    assert (attempt / "events.jsonl").is_file()
    assert (attempt / "agent_final.txt").read_text().endswith("hello")
    transcript = attempt / ".agent-control" / "acp-transcript.jsonl"
    assert transcript.is_file()
    assert '"method":"initialize"' in transcript.read_text()
    assert str(attempt) not in transcript.read_text()
    assert "<skill_workspace>" in transcript.read_text()
    assert transcript.stat().st_mode & 0o777 == 0o600
    assert not (attempt / ".agent-runtime").exists()
    manifest = (attempt / ".agent-control" / "agent-manifest.json").read_text()
    assert '"transport": "acp"' in manifest
    assert "sha256:" + "a" * 64 in manifest


def test_acp_transcript_redacts_exact_secrets(tmp_path):
    transcript = tmp_path / "acp-transcript.jsonl"
    _write_transcript(
        transcript,
        ({"direction": "client_to_agent", "message": {"token": "top-secret"}},),
        secrets=("top-secret",),
    )

    assert "top-secret" not in transcript.read_text()
    assert "***" in transcript.read_text()


@pytest.mark.asyncio
async def test_unanswered_permission_fails_attempt_without_auto_allow(tmp_path):
    settings = _settings(mode="permission")
    spec = AgentRegistry.from_settings(settings).resolve("acp:alpha@1.2.3").spec
    adapter = AcpTransportAdapter(settings=settings, spec=spec)

    result = await adapter.run(_task(), SimpleNamespace(), tmp_path)

    assert result.status == "cli_error"
    assert result.error_code == "agent_permission_required"


def test_acp_config_rejects_unpinned_or_mismatched_metadata():
    with pytest.raises(ValueError, match="SHA-256"):
        Settings(
            agents={
                "acp": {
                    "acp:alpha@1.2.3": {
                        "command": ["alpha"],
                        "registry_sha256": "not-a-pin",
                    }
                }
            }
        )
    settings = _settings()
    settings.agents.acp["acp:alpha@1.2.3"].registry_entry["version"] = "9.9.9"
    with pytest.raises(ValueError, match="does not match"):
        AgentRegistry.from_settings(settings)

    with pytest.raises(ValueError, match="uppercase environment"):
        Settings(
            agents={
                "acp": {
                    "acp:alpha@1.2.3": {
                        "command": ["alpha"],
                        "registry_sha256": "a" * 64,
                        "env_from": ["not-a-secret-name"],
                    }
                }
            }
        )


@pytest.mark.asyncio
async def test_acp_adapter_fails_before_launch_when_forwarded_auth_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("ACP_TEST_API_KEY", raising=False)
    settings = _settings()
    settings.agents.acp["acp:alpha@1.2.3"].env_from = ["ACP_TEST_API_KEY"]
    resolved = AgentRegistry.from_settings(settings).resolve("acp:alpha@1.2.3")

    result = await resolved.build_adapter().run(_task(), SimpleNamespace(), tmp_path)

    assert result.error_code == "agent_auth_missing"
    assert not (tmp_path / "attempts" / "attempt-acp").exists()
