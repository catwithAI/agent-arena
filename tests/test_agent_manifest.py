from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from backend.agents.launch import RenderedLaunchPlan
from backend.agents.manifest import AgentManifestError, AgentManifestStore
from backend.agents.models import AgentSpec


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "schema_version": "1",
            "id": "manifest-agent",
            "display_name": "Manifest Agent",
            "source": "builtin",
            "transport": "local-cli",
            "implementation": {"kind": "profile-runtime"},
            "availability": {"executable": "manifest-agent"},
            "launch": {"executable": "manifest-agent", "args": []},
            "prompt": {"mode": "file"},
            "model": {
                "binding": "agent-default",
                "default_model": "vendor/default-model",
            },
            "mcp": {"dialect": "unsupported"},
            "output": {"parser": "text"},
            "capabilities": {"single_turn": "verified"},
            "isolation": {"execution_locus": "host", "network_required": "public_internet"},
        }
    )


def _plan(root: Path, secret: str) -> RenderedLaunchPlan:
    private = root / "attempt-private-random"
    return RenderedLaunchPlan(
        argv=("manifest-agent", "--prompt-file", str(private / "prompt.txt")),
        cwd=root / "workspace-random",
        env={"MODEL_API_KEY": secret},
        env_redacted={"MODEL_API_KEY": "***"},
        stdin_data=None,
        prompt_mode="file",
        plan_hash="sha256:logical-plan",
    )


def test_prepared_manifest_is_atomic_private_redacted_and_diagnostic(tmp_path):
    secret = "manifest-super-secret"
    private = tmp_path / "attempt-private-random"
    workspace = tmp_path / "workspace-random"
    store = AgentManifestStore(tmp_path / "control" / "agent-manifest.json")
    prepared = store.prepare(
        attempt_id="att_123",
        spec=_spec(),
        plan=_plan(tmp_path, secret),
        agent_version="1.2.3",
        requested_model=None,
        provider=None,
        components={"runtime": "local-cli@1", "parser": "text@1"},
        config_summary={"path": str(private / "config.json"), "token": secret},
        path_aliases={private: "attempt_private", workspace: "skill_workspace"},
        secrets=(secret,),
    )
    raw = store.path.read_text(encoding="utf-8")
    assert prepared["status"] == "prepared"
    assert prepared["model"]["request_mode"] == "agent-default"
    assert secret not in raw
    assert str(private) not in raw
    assert "<attempt_private>/prompt.txt" in raw
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert not list(store.path.parent.glob("*.tmp"))
    assert store.read() == json.loads(raw)


def test_finalize_is_idempotent_and_preserves_unknown_effective_model(tmp_path):
    secret = "final-secret"
    store = AgentManifestStore(tmp_path / "agent-manifest.json")
    store.prepare(
        attempt_id="att_123",
        spec=_spec(),
        plan=_plan(tmp_path, secret),
        agent_version=None,
        requested_model="provider/requested",
        provider="provider",
        components={"runtime": "local-cli@1"},
        secrets=(secret,),
    )
    kwargs = {
        "effective_model": "do-not-copy-requested",
        "effective_model_known": False,
        "coverage": {"events": "degraded"},
        "cleanup": {"status": "terminated"},
        "outcome": {"status": "completed", "diagnostic": secret},
        "sessions": [{"turn_id": "turn-1", "session_id": "session-1"}],
        "degradations": ["parser summary missing"],
        "secrets": (secret,),
    }
    first = store.finalize(**kwargs)
    second = store.finalize(**kwargs)
    assert first == second
    assert first["status"] == "final"
    assert first["model"]["effective"] is None
    assert first["model"]["effective_status"] == "unknown"
    assert secret not in store.path.read_text(encoding="utf-8")


def test_finalize_rejects_conflicting_second_write_and_missing_prepare(tmp_path):
    store = AgentManifestStore(tmp_path / "agent-manifest.json")
    with pytest.raises(AgentManifestError, match="missing"):
        store.finalize(
            effective_model=None,
            effective_model_known=False,
            coverage={},
            cleanup={},
            outcome={},
        )

    store.prepare(
        attempt_id="att_123",
        spec=_spec(),
        plan=_plan(tmp_path, "secret"),
        agent_version=None,
        requested_model=None,
        provider=None,
        components={},
        secrets=("secret",),
    )
    store.finalize(
        effective_model=None,
        effective_model_known=False,
        coverage={},
        cleanup={},
        outcome={"status": "completed"},
    )
    with pytest.raises(AgentManifestError, match="different data"):
        store.finalize(
            effective_model=None,
            effective_model_known=False,
            coverage={"changed": True},
            cleanup={},
            outcome={"status": "completed"},
        )


def test_manifest_redacts_both_symlinked_and_resolved_path_spellings(tmp_path):
    target = tmp_path / "real-tools"
    target.mkdir()
    alias = tmp_path / "linked-tools"
    alias.symlink_to(target, target_is_directory=True)
    executable = alias / "agent"
    store = AgentManifestStore(tmp_path / "alias-manifest.json")
    plan = RenderedLaunchPlan(
        argv=(str(executable),),
        cwd=tmp_path,
        env={},
        env_redacted={},
        stdin_data=None,
        prompt_mode="driver-owned",
        plan_hash="sha256:alias-plan",
    )

    prepared = store.prepare(
        attempt_id="att_alias",
        spec=_spec(),
        plan=plan,
        agent_version="1.0.0",
        requested_model=None,
        provider=None,
        components={},
        config_summary={"resolved": str(executable.resolve())},
        path_aliases={executable: "agent_executable"},
    )

    assert prepared["launch"]["argv_redacted"] == ["<agent_executable>"]
    assert prepared["config_summary"]["resolved"] == "<agent_executable>"
