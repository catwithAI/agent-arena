"""Opt-in smoke against two administrator-installed, pinned ACP agents.

This module is collected by the normal suite but skipped unless
``ARENA_ACP_SMOKE_CONFIG`` points at a JSON configuration. It never downloads
or installs an agent. Successful evidence remains under the configured output
directory instead of pytest's temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.adapters.base import AdapterRunInput
from backend.agents.acp.registry import RegistryDocument
from backend.agents.registry import AgentRegistry
from backend.config import Settings


CONFIG_ENV = "ARENA_ACP_SMOKE_CONFIG"


def _load_config() -> tuple[dict, dict[tuple[str, str], dict]]:
    config_path = Path(os.environ[CONFIG_ENV]).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    registry_path = (config_path.parent / config["registry_path"]).resolve()
    raw = registry_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == config["registry_sha256"].removeprefix("sha256:")
    registry = RegistryDocument.model_validate_json(raw)
    entries = {(item.id, item.version): item.model_dump(mode="json") for item in registry.agents}
    assert len(config["agents"]) >= 2, "A7-5 requires at least two real ACP agents"
    return config, entries


@pytest.mark.skipif(CONFIG_ENV not in os.environ, reason="real ACP smoke is explicitly opt-in")
@pytest.mark.asyncio
async def test_two_pinned_real_acp_agents_complete_minimal_task():
    config, entries = _load_config()
    output_dir = Path(config["output_dir"]).resolve()
    installed: dict[str, dict] = {}
    for candidate in config["agents"]:
        identity = (candidate["id"], candidate["version"])
        entry = entries[identity]
        assert entry.get("license", "").lower() not in {"", "proprietary"}
        stable_id = f"acp:{identity[0]}@{identity[1]}"
        env_from = candidate.get("env_from", [])
        assert all(os.environ.get(name) for name in env_from), (
            stable_id,
            "a requested credential environment variable is absent",
        )
        inline_env: dict[str, str] = {}
        if config_env := candidate.get("config_content_env"):
            inline_env[config_env] = json.dumps(
                {
                    "model": config["model"],
                    "small_model": config["model"],
                    "provider": {
                        "openrouter": {
                            "options": {"apiKey": "{env:OPENROUTER_API_KEY}"}
                        }
                    },
                },
                separators=(",", ":"),
            )
        installed[stable_id] = {
            "command": candidate["command"],
            "registry_sha256": config["registry_sha256"],
            "registry_entry": entry,
            "env": {**candidate.get("env", {}), **inline_env},
            "env_from": env_from,
            "permission_answers": candidate.get("permission_answers", {}),
        }

    settings = Settings(agents={"acp": installed})
    registry = AgentRegistry.from_settings(settings)
    for index, stable_id in enumerate(installed):
        attempt_id = f"acp-real-smoke-{index + 1}-{stable_id.split(':', 1)[1].replace('@', '-')}"
        task = AdapterRunInput(
            attempt_id=attempt_id,
            task_id="a7-5-minimal-reply",
            task_prompt=(
                "Reply with exactly ACP_SMOKE_OK and do not use tools or modify files."
            ),
            task_context={},
            timeout_seconds=float(config.get("timeout_seconds", 120)),
            env_name="acp-real-smoke",
            env_skill_id="scalable-agent-integration/a7-5",
            session_token="acp-smoke-secret-placeholder",
            env_base_url="http://127.0.0.1.invalid",
        )
        result = await registry.resolve(stable_id).build_adapter().run(
            task, SimpleNamespace(), output_dir
        )
        attempt = output_dir / "attempts" / attempt_id
        assert result.status == "completed", (stable_id, result.error_code, result.error_message)
        assert "ACP_SMOKE_OK" in (attempt / "agent_final.txt").read_text(encoding="utf-8")
        assert (attempt / ".agent-control" / "acp-transcript.jsonl").is_file()
        assert (attempt / ".agent-control" / "agent-manifest.json").is_file()
