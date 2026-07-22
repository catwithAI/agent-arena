from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from backend.adapters.base import AdapterRunInput, McpServerSpec
from backend.agents.registry import AgentRegistry, AgentRegistryError
from backend.config import Settings


_PLUGIN = r'''
from backend.agents.python_plugin import PythonAgentOutput

class ExternalAgent:
    async def run(self, context):
        path = context.artifact_path("external-result.txt")
        names = [server.name for server in context.mcp_servers]
        path.write_text("mcp=" + ",".join(names), encoding="utf-8")
        return PythonAgentOutput(
            final_text="external complete",
            events=({"type":"done", "mcp":names},),
            usage={"input_tokens":3, "output_tokens":2},
            artifacts=("external-result.txt",),
            effective_model=context.model,
        )
'''


def _settings(entrypoint="external_agent:ExternalAgent", **overrides):
    config = {
        "entrypoint": entrypoint,
        "display_name": "External Agent",
        "supports_model": True,
        "supports_mcp": True,
        "package_name": "external-agent",
        "package_version": "0.1.0",
    }
    config.update(overrides)
    return Settings(agents={"python_plugins": {"external-python": config}})


def _task(**overrides):
    values = {
        "attempt_id": "attempt-python-plugin",
        "task_id": "task-python-plugin",
        "task_prompt": "create the result",
        "task_context": {},
        "timeout_seconds": 2,
        "env_name": "fixture",
        "env_skill_id": "lane/fixture",
        "session_token": "plugin-session-secret",
        "env_base_url": "http://127.0.0.1:8100",
        "mcp_servers": (McpServerSpec(name="declared-only", command="fake-mcp"),),
    }
    values.update(overrides)
    return AdapterRunInput(**values)


@pytest.mark.asyncio
async def test_external_python_package_is_lazy_and_completes_wrapped_e2e(
    tmp_path, monkeypatch
):
    package_root = tmp_path / "outside-package"
    package_root.mkdir()
    (package_root / "external_agent.py").write_text(_PLUGIN)
    monkeypatch.syspath_prepend(str(package_root))
    sys.modules.pop("external_agent", None)

    registry = AgentRegistry.from_settings(_settings())
    descriptor = next(
        item for item in registry.describe_all() if item["id"] == "external-python"
    )
    assert descriptor["source"] == "plugin"
    assert descriptor["availability"]["status"] == "unknown"
    assert "external_agent" not in sys.modules

    adapter = registry.resolve("external-python").build_adapter(model="fixture/model")
    assert "external_agent" in sys.modules
    result = await adapter.run(_task(), SimpleNamespace(), tmp_path / "data")

    assert result.status == "completed"
    assert result.token_usage == {"input_tokens": 3, "output_tokens": 2}
    attempt = tmp_path / "data" / "attempts" / "attempt-python-plugin"
    assert (attempt / "skill_workspace" / "external-result.txt").read_text() == (
        "mcp=declared-only"
    )
    manifest = json.loads((attempt / ".agent-control" / "agent-manifest.json").read_text())
    assert manifest["components"]["runtime"] == "trusted-python-plugin@1"
    assert manifest["model"]["effective"] == "fixture/model"
    assert manifest["config_summary"]["mcp"][0]["name"] == "declared-only"
    assert "plugin-session-secret" not in json.dumps(manifest)


def test_missing_optional_dependency_only_breaks_selected_plugin(tmp_path, monkeypatch):
    package_root = tmp_path / "missing-package"
    package_root.mkdir()
    (package_root / "broken_external.py").write_text(
        "import dependency_that_is_not_installed\nclass Agent: pass\n"
    )
    monkeypatch.syspath_prepend(str(package_root))
    registry = AgentRegistry.from_settings(_settings("broken_external:Agent"))

    assert "broken_external" not in sys.modules
    assert any(item["id"] == "codex" for item in registry.describe_all())
    with pytest.raises(AgentRegistryError, match="failed to load adapter"):
        registry.resolve("external-python").build_adapter()


@pytest.mark.asyncio
async def test_plugin_cannot_report_artifact_outside_workspace(tmp_path, monkeypatch):
    package_root = tmp_path / "escape-package"
    package_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (package_root / "escape_agent.py").write_text(
        "from backend.agents.python_plugin import PythonAgentOutput\n"
        "class Agent:\n"
        "  async def run(self, context):\n"
        "    return PythonAgentOutput(artifacts=('../outside.txt',))\n"
    )
    monkeypatch.syspath_prepend(str(package_root))
    settings = _settings("escape_agent:Agent", supports_mcp=False)
    adapter = AgentRegistry.from_settings(settings).resolve("external-python").build_adapter()

    result = await adapter.run(_task(mcp_servers=()), SimpleNamespace(), tmp_path / "data")

    assert result.status == "cli_error"
    assert result.error_code == "agent_internal_error"
    assert "outside workspace" in result.error_message
