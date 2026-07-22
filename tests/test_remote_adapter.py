from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from backend.adapters.base import AdapterRunInput
from backend.agents.registry import AgentRegistry
from backend.agents.remote.adapter import RemoteTransportAdapter
from backend.config import Settings


def _settings(**overrides) -> Settings:
    config = {
        "endpoint": "https://remote.test/api/",
        "data_residency": "eu-west",
        "upload_files": False,
        "poll_interval_seconds": 0,
    }
    config.update(overrides)
    return Settings(agents={"remote": {"remote-fixture": config}})


def _task(**overrides) -> AdapterRunInput:
    values = {
        "attempt_id": "attempt-remote",
        "task_id": "task-remote",
        "task_prompt": "build it",
        "task_context": {},
        "timeout_seconds": 2,
        "env_name": "fixture",
        "env_skill_id": "lane/fixture",
        "session_token": "session-secret",
        "env_base_url": "http://127.0.0.1:8100",
    }
    values.update(overrides)
    return AdapterRunInput(**values)


def test_remote_registry_descriptor_discloses_boundary_without_network_probe():
    registry = AgentRegistry.from_settings(_settings())
    resolved = registry.resolve("remote-fixture")
    descriptor = next(item for item in registry.describe_all() if item["id"] == "remote-fixture")

    assert resolved.spec.transport == "remote"
    assert resolved.spec.implementation.import_path.endswith("remote.adapter:build_remote_adapter")
    assert descriptor["metadata"]["data_residency"] == "eu-west"
    assert descriptor["metadata"]["uploads_source_files"] is False
    assert descriptor["metadata"]["cancellation_semantics"] == "best-effort-unknown"


@pytest.mark.asyncio
async def test_remote_adapter_standard_manifest_result_and_artifact_partial(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=b"x")
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["protocolVersion"] == "arena-remote-v1"
        assert body["turns"][0]["text"].endswith("build it")
        return httpx.Response(
            200,
            json={
                "sessionId": "remote-session",
                "status": "completed",
                "finalText": "done",
                "events": [{"type": "message", "text": "done"}],
                "usage": {"input_tokens": 8, "output_tokens": 2},
                "artifacts": [
                    {
                        "path": "bad.txt",
                        "url": "/api/files/bad",
                        "sha256": "0" * 64,
                        "size": 1,
                    }
                ],
                "metadata": {"model": "remote/model"},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = _settings(supports_model=True)
    spec = AgentRegistry.from_settings(settings).resolve("remote-fixture").spec
    adapter = RemoteTransportAdapter(
        settings=settings, spec=spec, model="requested/model", http_client=http
    )

    result = await adapter.run(_task(), SimpleNamespace(), tmp_path)

    assert result.status == "completed"
    assert result.token_usage == {"input_tokens": 8, "output_tokens": 2}
    assert result.external_refs["artifact_sync"]["status"] == "partial"
    attempt = tmp_path / "attempts" / "attempt-remote"
    assert (attempt / "agent_final.txt").read_text() == "done"
    manifest = json.loads((attempt / ".agent-control" / "agent-manifest.json").read_text())
    assert manifest["agent"]["transport"] == "remote"
    assert manifest["model"]["effective"] == "remote/model"
    assert manifest["coverage"]["artifacts"] == "partial"
    await http.aclose()


@pytest.mark.asyncio
async def test_remote_adapter_refuses_undeclared_file_upload_before_http(tmp_path):
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    material = env_dir / "source.txt"
    material.write_text("private source")
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = _settings(upload_files=False)
    spec = AgentRegistry.from_settings(settings).resolve("remote-fixture").spec
    adapter = RemoteTransportAdapter(settings=settings, spec=spec, http_client=http)
    task = _task(
        task_context={
            "uploaded_files": [{"name": "source.txt", "path": str(material)}]
        }
    )

    result = await adapter.run(task, SimpleNamespace(env_dir=env_dir), tmp_path / "data")

    assert result.status == "cli_error"
    assert result.error_code == "agent_remote_upload_not_allowed"
    assert called is False
    await http.aclose()
