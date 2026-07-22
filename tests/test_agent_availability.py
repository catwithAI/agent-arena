from __future__ import annotations

import sys
from pathlib import Path

import pytest

from backend.agents.availability import AvailabilityService
from backend.agents.models import AgentSpec
from backend.agents.registry import AgentRegistry
from backend.config import Settings


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{source}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _spec(executable: str, **availability_overrides) -> AgentSpec:
    availability = {"executable": executable, **availability_overrides}
    return AgentSpec.model_validate(
        {
            "schema_version": "1",
            "id": "probe-agent",
            "display_name": "Probe Agent",
            "source": "builtin",
            "transport": "local-cli",
            "implementation": {
                "kind": "existing-adapter",
                "import_path": "backend.agents.builtin:build_codex_adapter",
            },
            "availability": availability,
            "prompt": {"mode": "driver-owned"},
            "model": {"binding": "agent-default"},
            "mcp": {"dialect": "unsupported"},
            "output": {"parser": "text"},
            "isolation": {"execution_locus": "host", "network_required": "none"},
        }
    )


@pytest.mark.asyncio
async def test_missing_executable_and_dependency_are_distinct(tmp_path):
    service = AvailabilityService(ttl_seconds=0)
    result = await service.probe(_spec("definitely-missing-agent-binary"))
    assert result.status == "not_installed"

    executable = _write_executable(tmp_path / "agent", "print('agent 1.2.3')")
    result = await service.probe(
        _spec(str(executable), system_dependencies=["definitely-missing-dependency"])
    )
    assert result.status == "missing_dependency"


@pytest.mark.asyncio
async def test_pep440_and_semver_constraints(tmp_path):
    executable = _write_executable(tmp_path / "agent", "print('agent v1.4.2')")
    service = AvailabilityService(ttl_seconds=0)
    pep = await service.probe(
        _spec(
            str(executable),
            version_command=["{executable}", "--version"],
            version_constraint=">=1.0,<2",
        )
    )
    assert pep.status == "available"
    assert pep.version == "1.4.2"

    semver = await service.probe(
        _spec(
            str(executable),
            version_command=["{executable}"],
            version_scheme="semver",
            version_constraint="^2.0.0",
        )
    )
    assert semver.status == "version_unsupported"
    assert semver.version == "1.4.2"


@pytest.mark.asyncio
async def test_nonstandard_regex_version(tmp_path):
    executable = _write_executable(tmp_path / "agent", "print('release/channel-42')")
    result = await AvailabilityService(ttl_seconds=0).probe(
        _spec(
            str(executable),
            version_command=["{executable}"],
            version_scheme="regex",
            version_regex=r"channel-(\d+)",
            version_constraint=r"4[0-9]",
        )
    )
    assert result.status == "available"
    assert result.version == "42"


@pytest.mark.asyncio
async def test_missing_auth_is_redacted_and_cache_key_tracks_presence(tmp_path, monkeypatch):
    executable = _write_executable(tmp_path / "agent", "print('agent 1.0.0')")
    raw = _spec(str(executable)).model_dump(mode="json")
    raw["auth"] = [{"name": "model key", "env_var": "PROBE_SECRET"}]
    spec = AgentSpec.model_validate(raw)
    service = AvailabilityService(ttl_seconds=60)

    monkeypatch.delenv("PROBE_SECRET", raising=False)
    missing = await service.probe(spec)
    assert missing.status == "missing_auth"
    assert "super-secret" not in (missing.reason or "")

    monkeypatch.setenv("PROBE_SECRET", "super-secret")
    available = await service.probe(spec)
    assert available.status == "available"
    assert "super-secret" not in repr(available)


@pytest.mark.asyncio
async def test_slow_version_probe_times_out(tmp_path):
    executable = _write_executable(
        tmp_path / "slow-agent", "import time; time.sleep(10); print('agent 1.0.0')"
    )
    result = await AvailabilityService(ttl_seconds=0).probe(
        _spec(
            str(executable),
            version_command=["{executable}"],
            timeout_seconds=0.05,
        )
    )
    assert result.status == "unknown"
    assert "timed out" in (result.reason or "")


@pytest.mark.asyncio
async def test_registry_descriptor_v2_uses_probe_result(tmp_path):
    executable = _write_executable(tmp_path / "agent", "print('agent 3.2.1')")
    registry = AgentRegistry(Settings())
    registry.register(
        _spec(
            str(executable),
            version_command=["{executable}", "--version"],
            version_constraint=">=3",
        )
    )
    descriptors = await registry.describe_all_async(service=AvailabilityService(ttl_seconds=0))
    descriptor = next(item for item in descriptors if item["id"] == "probe-agent")
    assert descriptor["availability"] == {
        "status": "available",
        "version": "3.2.1",
        "reason": None,
    }
    assert descriptor["status"] == "available"
    assert descriptor["source"] == "builtin"
    assert descriptor["capabilities"]["single_turn"]["state"] == "unsupported"
