"""Scene-declared MCP servers + preserved native capability for CC/Codex.

Covers:
- adapters consume `task.mcp_servers` (McpServerSpec) instead of guessing a
  server path/name from env_name;
- no scenario-declared MCP server -> no --mcp-config / mcp_servers.* config,
  no "you must use MCP server X" text in the prompt;
- Claude Code's CLAUDE_CONFIG_DIR/HOME and Codex's CODEX_HOME point at a
  clean per-attempt directory (host-state isolation, not capability
  restriction);
- Codex never puts attempt credentials on argv, and only exposes them to
  the subprocess environment when a scene MCP server is actually declared.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from backend.adapters.base import AdapterRunInput, McpServerSpec
from backend.adapters.claude_code import ClaudeCodeAdapter
from backend.adapters.codex import CodexAdapter


def _make_task(**overrides) -> AdapterRunInput:
    defaults = dict(
        attempt_id="att_mcp_spec_test",
        task_id="task_001",
        task_prompt="Do the task",
        task_context={},
        timeout_seconds=10,
        env_name="demo-env",
        env_skill_id="lane/demo-env",
        session_token="tok_test",
        env_base_url="http://127.0.0.1:8100",
    )
    defaults.update(overrides)
    return AdapterRunInput(**defaults)


def _cc_mcp() -> McpServerSpec:
    return McpServerSpec(
        name="lane-demo-env", command="uv",
        args=("run", "--project", ".", "python", "envs/demo-env/mcp_server.py"),
        cwd="/tmp/lane",
    )


def _codex_mcp() -> McpServerSpec:
    return McpServerSpec(
        name="scene-tools", command="uv",
        args=("run", "python", "tools/scene_mcp.py"), cwd="/tmp/lane",
    )


class FakeProcess:
    def __init__(self, stdout_lines: list[str], returncode: int = 0):
        self.returncode = returncode
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        reader = asyncio.StreamReader()
        for line in stdout_lines:
            reader.feed_data((line + "\n").encode())
        reader.feed_eof()
        self.stdout = reader

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


_CC_RESULT_OK = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "session_id": "sess_x", "usage": {"input_tokens": 10, "output_tokens": 5},
})

_CODEX_RESULT_OK = json.dumps({"type": "turn.completed"})


# ---------- Claude Code ------------------------------------------------------


async def test_claude_code_no_scene_mcp_keeps_native_tools_and_writes_no_config(tmp_path):
    adapter = ClaudeCodeAdapter(project_path=str(tmp_path))
    task = _make_task()
    proc = FakeProcess([_CC_RESULT_OK])

    with patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await adapter.run(task, None, tmp_path)

    cmd = list(spawn.call_args.args)
    assert "--mcp-config" not in cmd
    assert "--disallowed-tools" not in cmd
    assert "--disable-slash-commands" not in cmd
    assert "--bare" not in cmd
    assert "--strict-mcp-config" not in cmd
    assert "MCP server" not in " ".join(str(x) for x in cmd)
    assert not (tmp_path / "attempts" / task.attempt_id / "mcp_config.json").exists()


async def test_claude_code_writes_config_only_for_declared_server(tmp_path):
    adapter = ClaudeCodeAdapter(project_path=str(tmp_path))
    task = _make_task(mcp_servers=(_cc_mcp(),))
    proc = FakeProcess([_CC_RESULT_OK])

    with patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await adapter.run(task, None, tmp_path)

    cmd = list(spawn.call_args.args)
    assert "--mcp-config" in cmd
    config_path = tmp_path / "attempts" / task.attempt_id / "mcp_config.json"
    config = json.loads(config_path.read_text())
    server = config["mcpServers"]["lane-demo-env"]
    assert server["command"] == "uv"
    assert server["cwd"] == "/tmp/lane"
    assert server["env"]["LANE_SESSION_TOKEN"] == task.session_token


async def test_claude_code_isolates_home_per_attempt(tmp_path):
    adapter = ClaudeCodeAdapter(project_path=str(tmp_path))
    task = _make_task()
    proc = FakeProcess([_CC_RESULT_OK])

    with patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await adapter.run(task, None, tmp_path)

    env = spawn.call_args.kwargs["env"]
    attempt_dir = tmp_path / "attempts" / task.attempt_id
    assert env["HOME"] == str((attempt_dir / ".cc-iso-home").resolve())
    assert env["CLAUDE_CONFIG_DIR"] == str((attempt_dir / ".cc-iso-home" / ".claude").resolve())


# ---------- Codex -------------------------------------------------------------


async def test_codex_no_scene_mcp_adds_no_mcp_config_or_credentials(tmp_path):
    adapter = CodexAdapter(project_path=str(tmp_path))
    task = _make_task()
    proc = FakeProcess([_CODEX_RESULT_OK])

    with patch("shutil.which", return_value="/usr/local/bin/codex"), \
         patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await adapter.run(task, None, tmp_path)

    cmd_joined = " ".join(spawn.call_args.args)
    assert "mcp_servers." not in cmd_joined
    assert "MCP server" not in cmd_joined
    assert "LANE_SESSION_TOKEN" not in spawn.call_args.kwargs["env"]
    assert "LANE_ATTEMPT_ID" not in spawn.call_args.kwargs["env"]
    assert not (tmp_path / "attempts" / task.attempt_id / "codex_mcp_config.json").exists()


async def test_codex_declared_mcp_server_configured_and_token_off_argv(tmp_path):
    adapter = CodexAdapter(project_path=str(tmp_path))
    secret = "SECRET_TOKEN_zzz_do_not_leak"
    task = _make_task(session_token=secret, mcp_servers=(_codex_mcp(),))
    proc = FakeProcess([_CODEX_RESULT_OK])

    with patch("shutil.which", return_value="/usr/local/bin/codex"), \
         patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await adapter.run(task, None, tmp_path)

    cmd = list(spawn.call_args.args)
    joined = " ".join(cmd)
    assert "mcp_servers.scene-tools.command" in joined
    assert secret not in joined
    assert spawn.call_args.kwargs["env"]["LANE_SESSION_TOKEN"] == secret

    config_path = tmp_path / "attempts" / task.attempt_id / "codex_mcp_config.json"
    config = json.loads(config_path.read_text())
    assert config["mcp_servers"]["scene-tools"]["env"]["LANE_SESSION_TOKEN"] == secret


async def test_codex_isolates_codex_home_per_attempt(tmp_path):
    adapter = CodexAdapter(project_path=str(tmp_path))
    task = _make_task()
    proc = FakeProcess([_CODEX_RESULT_OK])

    with patch("shutil.which", return_value="/usr/local/bin/codex"), \
         patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await adapter.run(task, None, tmp_path)

    env = spawn.call_args.kwargs["env"]
    attempt_dir = tmp_path / "attempts" / task.attempt_id
    assert env["CODEX_HOME"] == str((attempt_dir / ".codex-iso-home").resolve())


async def test_prompts_do_not_prescribe_mcp_solving_method(tmp_path):
    """Neither adapter should tell the agent it 'must' use a particular MCP
    server -- native tool choice (WebSearch, shell, MCP, ...) is left to the
    agent, whether or not the scenario declares an MCP server."""
    cc = ClaudeCodeAdapter()
    cc_prompt = cc._render_prompt(_make_task(mcp_servers=(_cc_mcp(),)))
    assert "must use" not in cc_prompt.lower()
    assert "MCP server" not in cc_prompt

    codex = CodexAdapter()
    codex_prompt = codex._render_prompt(_make_task(mcp_servers=(_codex_mcp(),)))
    assert "must use" not in codex_prompt.lower()
    assert "MCP server" not in codex_prompt
