from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.adapters.base import AdapterRunInput, ConversationTurn, McpServerSpec
from backend.agents.registry import AgentRegistry
from backend.agents.transports import ProfileRuntimeAdapter
from backend.config import Settings


_FAKE_AGENT = """
import json
import os
import pathlib
import sys

prompt = sys.stdin.read()
mcp = json.loads(pathlib.Path(sys.argv[1]).read_text())
material = pathlib.Path("input.txt").read_text()
pathlib.Path("artifact.txt").write_text(prompt + "\\n" + material)
os.write(2, ("diagnostic=" + os.environ["PROFILE_SECRET"] + "\\n").encode())
print(json.dumps({"type": "thinking", "text": "checking material"}))
print(json.dumps({
    "type": "final",
    "text": ",".join(sorted(mcp["mcpServers"])),
    "session": "fake-session",
    "usage": {"input_tokens": 2, "output_tokens": 3},
}))
"""


def _profile() -> dict:
    return {
        "schema_version": "1",
        "display_name": "Fake Profile Agent",
        "transport": "local-cli",
        "implementation": {"kind": "profile-runtime"},
        "availability": {
            "executable": sys.executable,
            "version_command": [sys.executable, "--version"],
        },
        "launch": {
            "executable": sys.executable,
            "args": ["-c", _FAKE_AGENT, {"value": "mcp_config_file"}],
            "env": {"PROFILE_SECRET": {"secret_ref": "PROFILE_SECRET_ENV"}},
        },
        "prompt": {"mode": "stdin"},
        "model": {"binding": "agent-default"},
        "auth": [{"name": "profile", "env_var": "PROFILE_SECRET_ENV"}],
        "mcp": {"dialect": "json-file"},
        "output": {
            "parser": "jsonl",
            "config": {"final_type_value": "final", "session_field": "session"},
        },
        "capabilities": {
            "single_turn": "verified",
            "mcp": "verified",
            "structured_events": "verified",
            "token_usage": "declared",
        },
        "isolation": {"execution_locus": "host", "network_required": "none"},
    }


@pytest.mark.asyncio
async def test_profile_runtime_runs_fake_cli_with_mcp_materials_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "profile-super-secret"
    session_token = "attempt-session-token"
    monkeypatch.setenv("PROFILE_SECRET_ENV", secret)
    env_dir = tmp_path / "environment"
    env_dir.mkdir()
    material = env_dir / "input.txt"
    material.write_text("fixture material", encoding="utf-8")
    settings = Settings(agents={"profiles": {"fake-profile": _profile()}})
    registry = AgentRegistry.from_settings(settings)
    adapter = registry.resolve("fake-profile").build_adapter()
    task = AdapterRunInput(
        attempt_id="attempt-profile",
        task_id="task-profile",
        task_prompt="Use the staged material.",
        task_context={"uploaded_files": [{"name": "input.txt", "path": str(material)}]},
        timeout_seconds=5,
        env_name="fake-env",
        env_skill_id="lane/fake-env",
        session_token=session_token,
        env_base_url="http://127.0.0.1:9999",
        mcp_servers=(
            McpServerSpec(
                name="declared-tool",
                command=sys.executable,
                args=("-c", "raise SystemExit(0)"),
                env={"SESSION_TOKEN": session_token},
            ),
        ),
    )

    result = await adapter.run(task, SimpleNamespace(env_dir=env_dir), tmp_path / "data")

    attempt = tmp_path / "data" / "attempts" / task.attempt_id
    manifest_path = attempt / ".agent-control" / "agent-manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    stderr = attempt / ".agent-control" / "runtime" / "raw" / "stderr.log"
    mcp_path = attempt / ".agent-runtime" / "mcp.json"
    assert isinstance(adapter, ProfileRuntimeAdapter)
    assert result.status == "completed", (result.error_code, result.error_message)
    assert result.token_usage == {"input_tokens": 2, "output_tokens": 3}
    assert result.events_count == 2
    assert result.thinking_count == 1
    assert (attempt / "agent_final.txt").read_text(encoding="utf-8") == "declared-tool"
    artifact = (attempt / "skill_workspace" / "artifact.txt").read_text(encoding="utf-8")
    assert "Use the staged material." in artifact
    assert "fixture material" in artifact
    assert manifest["status"] == "final"
    assert manifest["agent"]["id"] == "fake-profile"
    assert manifest["sessions"] == [{"turn_id": "task-profile::t0", "session_id": "fake-session"}]
    assert secret not in manifest_text
    assert session_token not in manifest_text
    assert secret not in stderr.read_text(encoding="utf-8")
    assert "diagnostic=***" in stderr.read_text(encoding="utf-8")
    assert stat.S_IMODE(mcp_path.stat().st_mode) == 0o600
    rendered_server = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"][
        "declared-tool"
    ]
    assert rendered_server["args"] == ["-c", "raise SystemExit(0)"]
    assert rendered_server["command"] == sys.executable
    assert rendered_server["env"] == {
        "LANE_ATTEMPT_ID": task.attempt_id,
        "LANE_BASE_URL": task.env_base_url,
        "LANE_SESSION_TOKEN": session_token,
        "SESSION_TOKEN": session_token,
    }


def test_profile_and_existing_adapters_are_built_through_same_registry():
    registry = AgentRegistry.from_settings(
        Settings(agents={"profiles": {"fake-profile": _profile()}})
    )

    assert registry.known_agents() == (
        "claude-code",
        "codex",
        "deerflow",
        "fake-profile",
    )
    assert isinstance(registry.resolve("fake-profile").build_adapter(), ProfileRuntimeAdapter)
    assert type(registry.resolve("codex").build_adapter()).__name__ == "CodexAdapter"


_FAKE_RESUME_AGENT = r'''
import json, pathlib, sys
log = pathlib.Path("calls.jsonl")
if sys.argv[1] == "start":
    prompt = sys.argv[2]
    session = "session-" + pathlib.Path.cwd().parent.name
else:
    assert sys.argv[1:3] == ["resume", "--session"]
    session = sys.argv[3]
    assert sys.argv[4] == "--prompt"
    prompt = sys.argv[5]
with log.open("a") as file:
    file.write(json.dumps({"argv": sys.argv[1:], "session": session}) + "\n")
print(json.dumps({"type":"final", "text":prompt, "session":session,
                  "usage":{"input_tokens":2,"output_tokens":1}}))
'''


def _resume_profile() -> dict:
    return {
        "schema_version": "1",
        "display_name": "Fake Resume Profile",
        "transport": "local-cli",
        "implementation": {"kind": "profile-runtime"},
        "availability": {"executable": sys.executable},
        "launch": {
            "executable": sys.executable,
            "args": ["-c", _FAKE_RESUME_AGENT, "start", {"value": "prompt"}],
        },
        "prompt": {"mode": "arg"},
        "driver": {
            "kind": "command-resume",
            "resume_args": [
                "-c",
                _FAKE_RESUME_AGENT,
                "resume",
                {"flag": "--session", "value": "session_id"},
                {"flag": "--prompt", "value": "prompt"},
            ],
        },
        "model": {"binding": "agent-default"},
        "mcp": {"dialect": "unsupported"},
        "output": {
            "parser": "jsonl",
            "config": {"final_type_value": "final", "session_field": "session"},
        },
        "capabilities": {
            "single_turn": "verified",
            "resume_send_message": "verified",
            "structured_events": "verified",
            "token_usage": "verified",
        },
        "isolation": {"execution_locus": "host", "network_required": "none"},
    }


@pytest.mark.asyncio
async def test_profile_runtime_command_resume_three_turn_e2e(tmp_path: Path):
    settings = Settings(agents={"profiles": {"fake-resume": _resume_profile()}})
    adapter = AgentRegistry.from_settings(settings).resolve("fake-resume").build_adapter()
    turns = tuple(
        ConversationTurn(turn_id=f"turn-{index}", turn_index=index, prompt=text)
        for index, text in enumerate(("first", "second", "third"))
    )
    task = AdapterRunInput(
        attempt_id="attempt-resume",
        task_id="task-resume",
        task_prompt="first",
        task_context={},
        timeout_seconds=5,
        env_name="fake-env",
        env_skill_id="lane/fake-env",
        session_token="session-secret-7f4b9c2a",
        env_base_url="http://127.0.0.1:9999",
        conversation_turns=turns,
    )

    result = await adapter.run(task, SimpleNamespace(), tmp_path / "data")

    attempt = tmp_path / "data" / "attempts" / task.attempt_id
    assert result.status == "completed", (result.error_code, result.error_message)
    assert result.token_usage == {"input_tokens": 6, "output_tokens": 3}
    assert (attempt / "agent_final.txt").read_text() == "third"
    calls = [
        json.loads(line)
        for line in (attempt / "skill_workspace" / "calls.jsonl").read_text().splitlines()
    ]
    assert len(calls) == 3
    assert calls[0]["argv"][0] == "start"
    assert calls[1]["argv"][0:3] == ["resume", "--session", calls[0]["session"]]
    assert calls[2]["session"] == calls[0]["session"]
    manifest = json.loads(
        (attempt / ".agent-control" / "agent-manifest.json").read_text()
    )
    assert manifest["components"]["driver"] == "command-resume@1"
    assert [item["turn_id"] for item in manifest["sessions"]] == [
        "turn-0",
        "turn-1",
        "turn-2",
    ]
    assert len(result.external_refs["turn_plan_hashes"]) == 3
