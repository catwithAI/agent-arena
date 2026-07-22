from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from backend.adapters.base import AdapterRunInput, McpServerSpec
from backend.agents.mcp import (
    CommandRegisterDialect,
    JsonFileDialect,
    McpCommandResult,
    McpDialectError,
    NativeConfigDialect,
    ResolvedMcpServer,
    resolve_mcp_servers,
)


def _task(*servers: McpServerSpec, token: str = "lane-super-secret") -> AdapterRunInput:
    return AdapterRunInput(
        attempt_id="att-1",
        task_id="task-1",
        task_prompt="task",
        task_context={},
        timeout_seconds=60,
        env_name="must-not-be-guessed",
        env_skill_id="lane/must-not-be-guessed",
        session_token=token,
        env_base_url="http://127.0.0.1:8100",
        mcp_servers=tuple(servers),
    )


def test_zero_declared_servers_creates_no_config(tmp_path):
    resolved = resolve_mcp_servers(_task())
    assert resolved == ()
    assert JsonFileDialect().render(resolved, attempt_private=tmp_path) is None
    assert not (tmp_path / "mcp.json").exists()


def test_json_dialect_supports_multiple_servers_and_redacts_manifest_shape(tmp_path):
    task = _task(
        McpServerSpec(
            name="first",
            command="python",
            args=("server one.py", "--value=$(not-shell)"),
            cwd="/project/one",
            env={"STATIC": "yes"},
        ),
        McpServerSpec(name="second", command="node", args=("server.js",)),
    )
    resolved = resolve_mcp_servers(task)
    result = JsonFileDialect().render(resolved, attempt_private=tmp_path)
    assert result is not None
    config = json.loads(result.path.read_text(encoding="utf-8"))
    assert list(config["mcpServers"]) == ["first", "second"]
    assert config["mcpServers"]["first"]["args"][1] == "--value=$(not-shell)"
    assert config["mcpServers"]["first"]["env"]["LANE_SESSION_TOKEN"] == task.session_token
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    assert task.session_token not in repr(result.redacted_shape)
    assert task.session_token not in result.config_hash
    assert "LANE_SESSION_TOKEN" in result.redacted_shape[0]["env_names"]


def test_wire_rewrite_happens_before_dialect_and_hash_ignores_token_value(tmp_path):
    server = McpServerSpec(name="tool", command="original", args=("arg",))

    def rewrite(item: ResolvedMcpServer) -> ResolvedMcpServer:
        return ResolvedMcpServer(
            name=item.name,
            command="wire-tap",
            args=(item.command, *item.args),
            cwd=item.cwd,
            env=item.env,
        )

    first = JsonFileDialect().render(
        resolve_mcp_servers(_task(server, token="first"), rewrite=rewrite),
        attempt_private=tmp_path / "first",
    )
    second = JsonFileDialect().render(
        resolve_mcp_servers(_task(server, token="second"), rewrite=rewrite),
        attempt_private=tmp_path / "second",
    )
    assert first is not None and second is not None
    config = json.loads(first.path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["tool"]["command"] == "wire-tap"
    assert config["mcpServers"]["tool"]["args"] == ["original", "arg"]
    assert first.config_hash == second.config_hash


@pytest.mark.asyncio
async def test_command_register_uses_private_home_and_cleans_in_reverse(tmp_path, monkeypatch):
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    sentinel = user_home / "must-not-change"
    sentinel.write_text("safe")
    monkeypatch.setenv("HOME", str(user_home))
    calls = []

    async def execute(argv, *, cwd: Path, env):
        calls.append((argv, cwd, dict(env)))
        assert Path(env["HOME"]).is_relative_to(tmp_path / "private")
        assert cwd.is_relative_to(tmp_path / "private")
        return McpCommandResult(returncode=0)

    dialect = CommandRegisterDialect(
        register_command=lambda server: (
            "agent", "mcp", "add", server.name, "--command", server.command
        ),
        unregister_command=lambda server: ("agent", "mcp", "remove", server.name),
        executor=execute,
    )
    servers = resolve_mcp_servers(
        _task(McpServerSpec(name="first", command="one"), McpServerSpec(name="second", command="two")),
        rewrite=lambda item: ResolvedMcpServer(
            name=item.name,
            command=f"wire-{item.command}",
            args=item.args,
            cwd=item.cwd,
            env=item.env,
        ),
    )
    prepared = await dialect.prepare(servers, attempt_private=tmp_path / "private")

    assert prepared is not None
    assert [call[0][3] for call in calls] == ["first", "second"]
    assert calls[0][0][-1] == "wire-one"
    assert await prepared.cleanup() == ()
    assert [call[0][3] if call[0][1:3] == ("mcp", "add") else call[0][-1] for call in calls] == [
        "first", "second", "second", "first"
    ]
    assert await prepared.cleanup() == ()
    assert sentinel.read_text() == "safe"


@pytest.mark.asyncio
async def test_command_register_failure_rolls_back_and_prevents_launch(tmp_path):
    calls = []

    async def execute(argv, *, cwd, env):
        calls.append(argv)
        return McpCommandResult(returncode=9 if argv[-1] == "bad" else 0)

    dialect = CommandRegisterDialect(
        register_command=lambda server: ("register", server.name),
        unregister_command=lambda server: ("unregister", server.name),
        executor=execute,
    )
    servers = resolve_mcp_servers(
        _task(McpServerSpec(name="good", command="one"), McpServerSpec(name="bad", command="two"))
    )

    with pytest.raises(McpDialectError, match="exited with 9"):
        await dialect.prepare(servers, attempt_private=tmp_path)
    assert calls == [("register", "good"), ("register", "bad"), ("unregister", "good")]
    assert all(call[0] != "launch-agent" for call in calls)


@pytest.mark.asyncio
async def test_native_config_is_owner_only_custom_shape_and_removable(tmp_path):
    task = _task(McpServerSpec(name="tool", command="python", args=("server.py",)))
    servers = resolve_mcp_servers(task)
    dialect = NativeConfigDialect(
        filename="agent-native.json",
        renderer=lambda items: {
            "tools": [
                {"id": item.name, "exec": [item.command, *item.args], "environment": dict(item.env)}
                for item in items
            ]
        },
    )

    prepared = await dialect.prepare(servers, attempt_private=tmp_path)

    assert prepared is not None and prepared.config_path is not None
    payload = json.loads(prepared.config_path.read_text())
    assert payload["tools"][0]["exec"] == ["python", "server.py"]
    assert payload["tools"][0]["environment"]["LANE_SESSION_TOKEN"] == task.session_token
    assert task.session_token not in repr(prepared.redacted_shape)
    assert stat.S_IMODE(prepared.config_path.stat().st_mode) == 0o600
    assert await prepared.cleanup() == ()
    assert not prepared.config_path.exists()
