"""SshClaudeCodeAdapter unit tests."""

import asyncio
import json
from unittest.mock import patch

from backend.adapters.base import AdapterRunInput
from backend.adapters.ssh_claude_code import SshClaudeCodeAdapter


def _make_task(**overrides) -> AdapterRunInput:
    defaults = dict(
        attempt_id="att_ssh_test",
        task_id="task_001",
        task_prompt="Book a flight from Beijing to Tokyo",
        task_context={"current_date": "2026-02-10"},
        timeout_seconds=10,
        env_name="travel-planner",
        env_skill_id="lane/travel-planner",
        session_token="tok_test",
        env_base_url="http://127.0.0.1:8100",
    )
    defaults.update(overrides)
    return AdapterRunInput(**defaults)


_STREAM_INIT = json.dumps({
    "type": "system", "subtype": "init",
    "session_id": "sess_test", "model": "sonnet",
})

_STREAM_ASSISTANT = json.dumps({
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "Let me think about this"},
            {"type": "text", "text": "OK."},
        ],
        "usage": {"input_tokens": 500, "output_tokens": 30},
    },
})

_STREAM_RESULT_OK = json.dumps({
    "type": "result", "subtype": "success",
    "is_error": False, "session_id": "sess_test",
    "usage": {"input_tokens": 1000, "output_tokens": 80},
})

_STREAM_ASSISTANT_ZERO_USAGE = json.dumps({
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "Task complete."}],
        "usage": {"input_tokens": 0, "output_tokens": 0},
    },
})

_STREAM_RESULT_MODEL_USAGE = json.dumps({
    "type": "result", "subtype": "success",
    "is_error": False, "session_id": "sess_test",
    "modelUsage": {"vllm-local/qwen3.5": {"inputTokens": 222, "outputTokens": 33}},
})


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


async def test_prompt_written_to_file_not_in_command(tmp_path):
    adapter = SshClaudeCodeAdapter(
        ssh_host="x", ssh_user="ai", ssh_password="pw",
        project_path=str(tmp_path),
    )
    task = _make_task(task_prompt='a prompt with "quotes" and\nnewlines')
    # create fake mcp_server.py so SCP doesn't fail finding it
    env_dir = tmp_path / "envs" / task.env_name
    env_dir.mkdir(parents=True)
    (env_dir / "mcp_server.py").write_text("# fake")

    exec_calls = []

    async def fake_exec(*args, **kwargs):
        exec_calls.append(args)
        return FakeProcess([_STREAM_RESULT_OK], returncode=0)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await adapter.run(task, None, tmp_path)

    ssh_cmds = [c for c in exec_calls if "ssh" in str(c) and "claude" in str(c)]
    for cmd in ssh_cmds:
        cmd_str = " ".join(str(x) for x in cmd)
        assert '"quotes"' not in cmd_str

    prompt_file = tmp_path / "attempts" / task.attempt_id / "prompt.txt"
    assert prompt_file.exists()
    assert '"quotes"' in prompt_file.read_text()


async def test_mcp_config_uses_remote_venv_python():
    adapter = SshClaudeCodeAdapter(
        ssh_host="x", ssh_user="ai", ssh_password="pw",
    )
    task = _make_task()
    config = adapter._build_mcp_config(task, "/tmp/lane")
    server = list(config["mcpServers"].values())[0]
    assert server["command"] == "/tmp/lane-mcp-venv/bin/python"
    assert server["env"]["LANE_BASE_URL"] == task.env_base_url
    assert server["env"]["LANE_ATTEMPT_ID"] == task.attempt_id


async def test_happy_path_parses_events(tmp_path):
    adapter = SshClaudeCodeAdapter(
        ssh_host="x", ssh_user="ai", ssh_password="pw",
        project_path=str(tmp_path),
    )
    task = _make_task()
    env_dir = tmp_path / "envs" / task.env_name
    env_dir.mkdir(parents=True)
    (env_dir / "mcp_server.py").write_text("# fake")

    async def fake_exec(*args, **kwargs):
        cmd_str = " ".join(str(a) for a in args)
        if "scp" in cmd_str:
            return FakeProcess([], returncode=0)
        if "claude" in cmd_str:
            return FakeProcess([_STREAM_INIT, _STREAM_ASSISTANT, _STREAM_RESULT_OK])
        return FakeProcess([], returncode=0)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = await adapter.run(task, None, tmp_path)
    assert result.status == "completed"
    assert result.thinking_count == 1
    assert result.events_count == 3


async def test_ssh_parses_model_usage_when_message_usage_is_zero(tmp_path):
    adapter = SshClaudeCodeAdapter(
        ssh_host="x", ssh_user="ai", ssh_password="pw",
        project_path=str(tmp_path),
    )
    task = _make_task()
    env_dir = tmp_path / "envs" / task.env_name
    env_dir.mkdir(parents=True)
    (env_dir / "mcp_server.py").write_text("# fake")

    async def fake_exec(*args, **kwargs):
        cmd_str = " ".join(str(a) for a in args)
        if "scp" in cmd_str:
            return FakeProcess([], returncode=0)
        if "claude" in cmd_str:
            return FakeProcess([
                _STREAM_INIT,
                _STREAM_ASSISTANT_ZERO_USAGE,
                _STREAM_RESULT_MODEL_USAGE,
            ])
        return FakeProcess([], returncode=0)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = await adapter.run(task, None, tmp_path)
    assert result.status == "completed"
    assert result.token_usage == {"input_tokens": 222, "output_tokens": 33}
    assert result.external_refs["token_usage_estimated"] is False


async def test_missing_uploaded_attachment_fails_before_claude(tmp_path):
    adapter = SshClaudeCodeAdapter(
        ssh_host="x", ssh_user="ai", ssh_password="pw",
        project_path=str(tmp_path),
    )
    task = _make_task(
        task_context={"uploaded_files": [{"name": "missing.mp4", "path": "missing.mp4"}]},
    )
    env_dir = tmp_path / "envs" / task.env_name
    env_dir.mkdir(parents=True)
    (env_dir / "mcp_server.py").write_text("# fake")

    claude_started = False

    async def fake_exec(*args, **kwargs):
        nonlocal claude_started
        cmd_str = " ".join(str(a) for a in args)
        if "claude" in cmd_str:
            claude_started = True
        return FakeProcess([], returncode=0)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = await adapter.run(task, None, tmp_path)

    assert result.status == "cli_error"
    assert result.error_code == "missing_uploaded_file"
    assert result.error_message == "missing uploaded file: missing.mp4"
    assert not claude_started


async def test_cleanup_runs_rm_rf(tmp_path):
    adapter = SshClaudeCodeAdapter(
        ssh_host="x", ssh_user="ai", ssh_password="pw",
        project_path=str(tmp_path),
    )
    task = _make_task()
    env_dir = tmp_path / "envs" / task.env_name
    env_dir.mkdir(parents=True)
    (env_dir / "mcp_server.py").write_text("# fake")

    cleanup_cmds = []

    async def fake_exec(*args, **kwargs):
        cmd_str = " ".join(str(a) for a in args)
        if "rm -rf" in cmd_str:
            cleanup_cmds.append(cmd_str)
        return FakeProcess([_STREAM_RESULT_OK], returncode=0)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await adapter.run(task, None, tmp_path)
    assert len(cleanup_cmds) > 0
    assert task.attempt_id in cleanup_cmds[0]


async def test_wire_capture_capabilities_all_unsupported():
    adapter = SshClaudeCodeAdapter(ssh_host="x", ssh_user="ai", ssh_password="pw")
    caps = adapter.wire_capture_capabilities
    assert caps == {
        "process_env": False,
        "llm_base_url": False,
        "llm_headers": False,
        "mcp_rewrites": False,
    }
