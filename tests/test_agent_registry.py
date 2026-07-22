from __future__ import annotations

import sys

import pytest

from backend.agents.models import AgentSpec
from backend.agents.registry import AgentRegistry, AgentRegistryError
from backend.config import Settings


def _plugin_spec(agent_id="plugin-agent", source="plugin") -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "schema_version": "1",
            "id": agent_id,
            "display_name": "Plugin",
            "source": source,
            "transport": "python-sdk",
            "implementation": {"kind": "plugin", "import_path": "missing_sdk.agent:build"},
            "prompt": {"mode": "driver-owned"},
            "model": {"binding": "agent-default"},
            "mcp": {"dialect": "unsupported"},
            "output": {"parser": "text"},
            "isolation": {"execution_locus": "host", "network_required": "public_internet"},
        }
    )


def test_registry_is_deterministic_and_plugin_import_is_lazy():
    registry = AgentRegistry(Settings())
    registry.register(_plugin_spec())
    assert registry.known_agents() == ("plugin-agent",)
    assert "missing_sdk" not in sys.modules
    assert registry.describe_all()[0]["id"] == "plugin-agent"
    assert "missing_sdk" not in sys.modules
    with pytest.raises(AgentRegistryError, match="failed to load adapter"):
        registry.resolve("plugin-agent").build_adapter()


def test_plugin_descriptors_load_from_settings_without_importing_sdk():
    raw = _plugin_spec().model_dump(mode="json")
    raw.pop("id")
    raw.pop("source")
    settings = Settings(agents={"plugins": {"plugin-agent": raw}})
    registry = AgentRegistry.from_settings(settings)
    assert registry.resolve("plugin-agent").spec.source == "plugin"
    descriptor = next(item for item in registry.describe_all() if item["id"] == "plugin-agent")
    assert descriptor["availability"]["status"] == "unknown"
    assert "missing_sdk" not in sys.modules


def test_duplicate_ids_fail_and_only_builtin_can_be_explicitly_overridden():
    registry = AgentRegistry(Settings())
    registry.register(_plugin_spec())
    with pytest.raises(AgentRegistryError, match="duplicate agent id"):
        registry.register(_plugin_spec())
    with pytest.raises(AgentRegistryError, match="only explicitly override a built-in"):
        registry.register(_plugin_spec(source="config-override"), override=True)


def test_settings_profile_requires_explicit_override_for_builtin():
    raw = _plugin_spec(agent_id="codex", source="config").model_dump(mode="json")
    raw.pop("id")
    raw.pop("source")
    settings = Settings(agents={"profiles": {"codex": raw}})
    with pytest.raises(AgentRegistryError, match="duplicate agent id"):
        AgentRegistry.from_settings(settings)

    raw["override"] = True
    registry = AgentRegistry.from_settings(Settings(agents={"profiles": {"codex": raw}}))
    assert registry.resolve("codex").spec.source == "config-override"


def test_existing_and_legacy_agents_share_the_registry_and_hash_is_stable():
    settings = Settings(
        custom_agents={
            "my-agent": {
                "command": ["my-cli", "--prompt", "{prompt}"],
                "prompt_mode": "arg",
            }
        }
    )
    first = AgentRegistry.from_settings(settings)
    second = AgentRegistry.from_settings(settings)
    assert first.known_agents() == ("claude-code", "codex", "deerflow", "my-agent")
    legacy = first.resolve("my-agent").spec
    assert legacy.source == "legacy"
    assert legacy.warnings
    assert legacy.spec_hash == second.resolve("my-agent").spec_hash


def test_unknown_agent_resolution_is_explicit():
    with pytest.raises(AgentRegistryError, match="unknown agent"):
        AgentRegistry.from_settings(Settings()).resolve("nope")
