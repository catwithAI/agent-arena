from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.agents.models import AgentSpec, agent_spec_json_schema


def _profile(**overrides):
    profile = {
        "schema_version": "1",
        "id": "fixture-agent",
        "display_name": "Fixture Agent",
        "source": "config",
        "transport": "local-cli",
        "implementation": {"kind": "profile-runtime"},
        "availability": {"executable": "fixture-agent"},
        "launch": {
            "executable": "fixture-agent",
            "args": ["run", {"flag": "--model", "value": "effective_model", "omit_if_none": True}],
        },
        "prompt": {"mode": "stdin"},
        "model": {"binding": "flag", "flag": "--model"},
        "mcp": {"dialect": "unsupported"},
        "output": {"parser": "text"},
        "capabilities": {"single_turn": "verified", "mcp": False},
        "isolation": {"execution_locus": "host", "network_required": "public_internet"},
    }
    profile.update(overrides)
    return profile


def test_agent_spec_round_trip_and_capability_shorthand():
    spec = AgentSpec.model_validate(_profile())
    assert spec.capabilities.single_turn.state == "verified"
    assert spec.capabilities.mcp.state == "unsupported"
    assert AgentSpec.model_validate(spec.model_dump(mode="json")) == spec
    assert spec.spec_hash == AgentSpec.model_validate(_profile()).spec_hash


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": True},
        {"id": "Not A Slug"},
        {"launch": {"executable": "x", "args": [{"value": "secret.API_KEY"}]}},
        {"launch": {"executable": "x", "args": [{"value": "arbitrary_python"}]}},
        {"launch": {"executable": "x", "args": [{"value": "option.api_key"}]}},
    ],
)
def test_agent_spec_rejects_unknown_fields_bad_slug_and_unsafe_values(change):
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(_profile(**change))


def test_checked_in_agent_spec_schema_is_current():
    path = Path("backend/agents/agent-spec-v1.schema.json")
    assert json.loads(path.read_text(encoding="utf-8")) == agent_spec_json_schema()


def test_sensitive_declared_option_cannot_be_rendered_into_argv():
    profile = _profile(
        options={"api_key": {"type": "string", "sensitive": True}},
        launch={"executable": "x", "args": [{"value": "option.api_key"}]},
    )
    with pytest.raises(ValidationError, match="sensitive option"):
        AgentSpec.model_validate(profile)
