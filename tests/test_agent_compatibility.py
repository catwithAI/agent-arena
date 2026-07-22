from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

from backend.adapters.base import ConversationTurn, McpServerSpec
from backend.agents.availability import AvailabilityResult
from backend.agents.compatibility import check_compatibility
from backend.agents.models import AgentSpec
from backend.config import CustomAgentSection


def _spec(**overrides) -> AgentSpec:
    raw = {
        "schema_version": "1",
        "id": "compat-agent",
        "display_name": "Compatibility Agent",
        "source": "builtin",
        "transport": "local-cli",
        "implementation": {
            "kind": "existing-adapter",
            "import_path": "backend.agents.builtin:build_codex_adapter",
        },
        "availability": {"executable": "fixture"},
        "prompt": {"mode": "driver-owned"},
        "model": {
            "binding": "flag",
            "flag": "--model",
            "protocols": ["openai-responses"],
        },
        "mcp": {"dialect": "json-file"},
        "output": {"parser": "text"},
        "capabilities": {
            "single_turn": "verified",
            "resume_send_message": "unsupported",
            "answer_interaction": "unsupported",
            "mcp": "verified",
            "wire": "unsupported",
        },
        "isolation": {"execution_locus": "host", "network_required": "public_internet"},
    }
    raw.update(overrides)
    return AgentSpec.model_validate(raw)


def _available() -> AvailabilityResult:
    return AvailabilityResult(status="available", version="1.0.0", cli_path="/bin/fixture")


def test_compatible_single_turn_report_is_empty():
    report = check_compatibility(
        _spec(),
        availability=_available(),
        requested_model="provider/model",
        provider_protocol="openai-responses",
        mcp_servers=(),
        conversation_turns=(),
    )
    assert report.compatible is True
    assert report.issues == ()


def test_availability_statuses_have_stable_codes():
    expected = {
        "not_installed": "agent_not_installed",
        "version_unsupported": "agent_version_unsupported",
        "missing_auth": "agent_auth_missing",
        "missing_dependency": "agent_dependency_missing",
        "misconfigured": "agent_misconfigured",
        "unknown": "agent_availability_unknown",
    }
    for status, code in expected.items():
        report = check_compatibility(
            _spec(),
            availability=AvailabilityResult(status=status),
            requested_model=None,
            provider_protocol=None,
            mcp_servers=(),
            conversation_turns=(),
        )
        assert report.issues[0].code == code


def test_model_provider_mcp_conversation_and_wire_mismatches_accumulate():
    turns = (
        ConversationTurn(turn_id="t0", turn_index=0, prompt="first"),
        ConversationTurn(turn_id="t1", turn_index=1, prompt="second"),
        ConversationTurn(
            turn_id="t2",
            turn_index=2,
            action="answer_interaction",
            purpose="interaction",
            wait_for=None,
            answer={"answer": "x"},
        ),
    )
    report = check_compatibility(
        _spec(mcp={"dialect": "unsupported"}),
        availability=_available(),
        requested_model="provider/model",
        provider_protocol="anthropic",
        mcp_servers=(McpServerSpec(name="tool", command="tool"),),
        conversation_turns=turns,
        strict_wire=True,
    )
    assert {issue.code for issue in report.issues} == {
        "agent_provider_unsupported",
        "agent_mcp_unsupported",
        "agent_resume_unsupported",
        "agent_interaction_unsupported",
        "agent_wire_unsupported",
    }


def test_agent_default_and_unsupported_bindings_reject_explicit_model():
    for binding in ("agent-default", "unsupported"):
        report = check_compatibility(
            _spec(model={"binding": binding}),
            availability=_available(),
            requested_model="explicit/model",
            provider_protocol=None,
            mcp_servers=(),
            conversation_turns=(),
        )
        assert any(issue.code == "agent_model_unsupported" for issue in report.issues)


def test_missing_provider_auth_is_reported_without_secret_detail():
    report = check_compatibility(
        _spec(),
        availability=_available(),
        requested_model="provider/model",
        provider_protocol="openai-responses",
        provider_auth_available=False,
        mcp_servers=(),
        conversation_turns=(),
    )
    issue = next(issue for issue in report.issues if issue.field == "provider_auth")
    assert issue.code == "agent_auth_missing"
    assert issue.detail == {}


def test_platform_gate_is_explicit():
    raw = _spec().model_dump(mode="json")
    raw["metadata"] = {"supported_platforms": ["definitely-not-this-platform"]}
    report = check_compatibility(
        AgentSpec.model_validate(raw),
        availability=_available(),
        requested_model=None,
        provider_protocol=None,
        mcp_servers=(),
        conversation_turns=(),
        platform="linux",
    )
    assert any(issue.code == "agent_platform_unsupported" for issue in report.issues)


def test_api_rejects_incompatible_comparison_atomically_before_attempts(test_app):
    app, client = test_app
    app.state.settings.custom_agents["legacy-no-model"] = CustomAgentSection(
        command=[sys.executable, "-c", "print('unused')"],
        prompt_mode="arg",
    )
    create_attempt = AsyncMock()
    with patch("backend.api.create_attempt", new=create_attempt):
        response = client.post(
            "/api/runs",
            json={
                "env_name": "apple-incremental-game",
                "prompt": "hello",
                "agents": ["claude-code", "legacy-no-model"],
                "model": "explicit/model",
            },
        )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "agent_compatibility_mismatch"
    legacy = next(report for report in detail["reports"] if report["agent_id"] == "legacy-no-model")
    assert any(issue["code"] == "agent_model_unsupported" for issue in legacy["issues"])
    create_attempt.assert_not_awaited()
