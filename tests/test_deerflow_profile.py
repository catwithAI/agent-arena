from __future__ import annotations

import sys
import types

from backend.adapters.base import McpServerSpec
from backend.agents.availability import AvailabilityResult
from backend.agents.compatibility import check_compatibility
from backend.agents.deerflow import DEERFLOW_REVISION
from backend.agents.deerflow.runner import probe_harness
from backend.agents.registry import AgentRegistry
from backend.config import Settings


def test_deerflow_builtin_profile_is_pinned_and_conservative():
    spec = AgentRegistry.from_settings(Settings()).resolve("deerflow").spec

    assert str(spec.metadata.repository) == "https://github.com/bytedance/deer-flow"
    assert spec.metadata.revision == DEERFLOW_REVISION
    assert spec.metadata.package_name == "deerflow-harness"
    assert spec.metadata.package_version == "2.0.0"
    assert spec.availability.executable == "deerflow-arena-runner"
    assert spec.availability.version_constraint == "==2.0.0"
    assert spec.capabilities.single_turn.state == "verified"
    assert spec.capabilities.resume_send_message.state == "unsupported"
    assert spec.capabilities.mcp.state == "unsupported"
    assert spec.capabilities.subagent_identity.state == "unsupported"
    assert spec.model.protocols == ("openai-chat", "openai-responses", "anthropic")


def test_deerflow_probe_distinguishes_missing_and_bad_package(monkeypatch):
    import backend.agents.deerflow.runner as runner

    def missing(_name: str) -> str:
        raise runner.importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(runner.importlib.metadata, "version", missing)
    assert probe_harness() == (20, "deerflow-harness is not installed")

    monkeypatch.setattr(runner.importlib.metadata, "version", lambda _name: "2.1.0")
    code, message = probe_harness()
    assert code == 21
    assert "2.1.0 is unsupported" in message


def test_deerflow_probe_validates_embedded_client_signature(monkeypatch):
    import backend.agents.deerflow.runner as runner

    class FakeClient:
        def __init__(
            self,
            config_path=None,
            *,
            model_name=None,
            thinking_enabled=True,
            subagent_enabled=False,
            plan_mode=False,
        ):
            pass

        def stream(self, message, *, thread_id, **kwargs):
            return ()

    package = types.ModuleType("deerflow")
    package.__path__ = []
    client = types.ModuleType("deerflow.client")
    client.DeerFlowClient = FakeClient
    monkeypatch.setitem(sys.modules, "deerflow", package)
    monkeypatch.setitem(sys.modules, "deerflow.client", client)
    monkeypatch.setattr(runner.importlib.metadata, "version", lambda _name: "2.0.0")

    assert probe_harness() == (0, "deerflow-harness 2.0.0")


def test_deerflow_probe_rejects_incompatible_stream_signature(monkeypatch):
    import backend.agents.deerflow.runner as runner

    class FakeClient:
        def __init__(
            self,
            config_path=None,
            *,
            model_name=None,
            thinking_enabled=True,
            subagent_enabled=False,
            plan_mode=False,
        ):
            pass

        def stream(self, message):
            return ()

    package = types.ModuleType("deerflow")
    package.__path__ = []
    client = types.ModuleType("deerflow.client")
    client.DeerFlowClient = FakeClient
    monkeypatch.setitem(sys.modules, "deerflow", package)
    monkeypatch.setitem(sys.modules, "deerflow.client", client)
    monkeypatch.setattr(runner.importlib.metadata, "version", lambda _name: "2.0.0")

    code, message = probe_harness()
    assert code == 22
    assert "recursion_limit" in message


def test_deerflow_missing_provider_key_has_preflight_diagnostic():
    spec = AgentRegistry.from_settings(Settings()).resolve("deerflow").spec
    report = check_compatibility(
        spec,
        availability=AvailabilityResult(status="available", version="2.0.0"),
        requested_model="openrouter/model",
        provider_protocol="openai-chat",
        provider_auth_available=False,
        mcp_servers=(),
        conversation_turns=(),
    )

    assert not report.compatible
    assert any(issue.code == "agent_auth_missing" for issue in report.issues)


def test_deerflow_mcp_no_go_is_rejected_in_preflight():
    spec = AgentRegistry.from_settings(Settings()).resolve("deerflow").spec
    report = check_compatibility(
        spec,
        availability=AvailabilityResult(status="available", version="2.0.0"),
        requested_model="provider/model",
        provider_protocol="openai-chat",
        provider_auth_available=True,
        mcp_servers=(McpServerSpec(name="lane", command="lane-server"),),
        conversation_turns=(),
    )

    assert not report.compatible
    issue = next(item for item in report.issues if item.code == "agent_mcp_unsupported")
    assert issue.detail == {"server_names": ["lane"]}
    assert "embedded extension lifecycle" in (spec.capabilities.mcp.basis or "")
