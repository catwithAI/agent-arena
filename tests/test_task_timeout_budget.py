"""Task timeout budget ("capability per unit of time").

Covers three layers:
1. `time_budget_notice` pure logic: formatting, `None`/non-positive ->
   no notice.
2. Injection channel per adapter (fairness): Claude Code (local and SSH)
   have a native system-prompt channel (`--append-system-prompt`), so the
   notice must not leak into the user-facing prompt; Codex has no such
   channel, so the notice is prepended to the rendered prompt instead.
3. `None` (unlimited) means no adapter injects any time constraint at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.adapters.base import AdapterRunInput, time_budget_notice
from backend.adapters.claude_code import ClaudeCodeAdapter
from backend.adapters.codex import CodexAdapter
from backend.adapters.ssh_claude_code import SshClaudeCodeAdapter


def _task(timeout_seconds: int | None, **overrides) -> AdapterRunInput:
    defaults = dict(
        attempt_id="att_timeout_budget",
        task_id="task_timeout_budget",
        task_prompt="Solve the optimization problem",
        task_context={},
        timeout_seconds=timeout_seconds,
        env_name="demo-env",
        env_skill_id="lane/demo-env",
        session_token="tok_test",
        env_base_url="http://127.0.0.1:8100",
    )
    defaults.update(overrides)
    return AdapterRunInput(**defaults)


# ---------- 1. Pure logic ---------------------------------------------------


def test_notice_formats_whole_minutes():
    notice = time_budget_notice(600)
    assert notice is not None
    assert "10 minute" in notice
    assert "iterat" in notice  # "iterate"/"iterating"


def test_notice_formats_minutes_and_seconds():
    notice = time_budget_notice(90)
    assert notice is not None
    assert "1 minute" in notice and "30 second" in notice


def test_notice_formats_sub_minute():
    notice = time_budget_notice(45)
    assert notice is not None
    assert "45 second" in notice


@pytest.mark.parametrize("value", [None, 0, -5])
def test_notice_none_or_nonpositive_returns_none(value):
    assert time_budget_notice(value) is None


# ---------- 2. Claude Code: system-prompt channel ---------------------------


class FakeProcess:
    def __init__(self, stdout_lines: list[str], returncode: int = 0):
        import asyncio

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


def test_claude_code_prompt_stays_clean_and_notice_appended_via_system_prompt(tmp_path):
    """CC: time budget doesn't pollute the user prompt itself."""
    adapter = ClaudeCodeAdapter.__new__(ClaudeCodeAdapter)
    prompt = adapter._render_prompt(_task(600))
    assert "time budget" not in prompt


async def test_claude_code_appends_system_prompt_flag_when_timed(tmp_path):
    adapter = ClaudeCodeAdapter(project_path=str(tmp_path))
    task = _task(600)
    proc = FakeProcess([_CC_RESULT_OK])

    with patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await adapter.run(task, None, tmp_path)

    cmd = list(spawn.call_args.args)
    assert "--append-system-prompt" in cmd
    notice = cmd[cmd.index("--append-system-prompt") + 1]
    assert "10 minute" in notice


async def test_claude_code_unlimited_adds_no_system_prompt_flag(tmp_path):
    adapter = ClaudeCodeAdapter(project_path=str(tmp_path))
    task = _task(None)
    proc = FakeProcess([_CC_RESULT_OK])

    with patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await adapter.run(task, None, tmp_path)

    cmd = list(spawn.call_args.args)
    assert "--append-system-prompt" not in cmd


# ---------- 2b. Codex: no system channel -> top-of-message fallback --------


def test_codex_notice_at_top_of_prompt(tmp_path):
    adapter = CodexAdapter.__new__(CodexAdapter)
    prompt = adapter._render_prompt(_task(600))
    assert "10 minute" in prompt
    assert prompt.index("time budget") < prompt.index("Solve the optimization")


def test_codex_unlimited_no_notice(tmp_path):
    adapter = CodexAdapter.__new__(CodexAdapter)
    prompt = adapter._render_prompt(_task(None))
    assert "time budget" not in prompt
    assert prompt.startswith("Solve the optimization")


# ---------- 2c. SSH Claude Code: same system-prompt channel, remote --------


async def test_ssh_claude_code_uploads_notice_and_appends_flag(tmp_path):
    adapter = SshClaudeCodeAdapter(
        ssh_host="host", ssh_user="user", ssh_password="pw", project_path=str(tmp_path)
    )
    task = _task(600)

    async def _fake_ssh_exec(cmd):
        return 0

    uploaded: list[tuple[str, str]] = []

    async def _fake_upload_files(task, attempt_dir, mcp_server_path, remote_dir, *, include_mcp_config, include_system_notice=False):
        uploaded.append(("include_system_notice", include_system_notice))
        return True

    async def _fake_upload_attachments(task, remote_dir, data_path):
        return None

    proc = FakeProcess([_CC_RESULT_OK])

    with patch.object(adapter, "_ssh_exec", side_effect=_fake_ssh_exec), \
         patch.object(adapter, "_upload_files", side_effect=_fake_upload_files), \
         patch.object(adapter, "_upload_attachments", side_effect=_fake_upload_attachments), \
         patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await adapter.run(task, None, tmp_path)

    assert ("include_system_notice", True) in uploaded
    attempt_dir = tmp_path / "attempts" / task.attempt_id
    notice_file = attempt_dir / "system_notice.txt"
    assert notice_file.exists()
    assert "10 minute" in notice_file.read_text()

    remote_cmd = spawn.call_args.args[-1]
    assert "--append-system-prompt" in remote_cmd
    assert "system_notice.txt" in remote_cmd


async def test_ssh_claude_code_unlimited_no_notice_file_or_flag(tmp_path):
    adapter = SshClaudeCodeAdapter(
        ssh_host="host", ssh_user="user", ssh_password="pw", project_path=str(tmp_path)
    )
    task = _task(None)

    async def _fake_ssh_exec(cmd):
        return 0

    async def _fake_upload_files(task, attempt_dir, mcp_server_path, remote_dir, *, include_mcp_config, include_system_notice=False):
        assert include_system_notice is False
        return True

    async def _fake_upload_attachments(task, remote_dir, data_path):
        return None

    proc = FakeProcess([_CC_RESULT_OK])

    with patch.object(adapter, "_ssh_exec", side_effect=_fake_ssh_exec), \
         patch.object(adapter, "_upload_files", side_effect=_fake_upload_files), \
         patch.object(adapter, "_upload_attachments", side_effect=_fake_upload_attachments), \
         patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await adapter.run(task, None, tmp_path)

    attempt_dir = tmp_path / "attempts" / task.attempt_id
    assert not (attempt_dir / "system_notice.txt").exists()
    remote_cmd = spawn.call_args.args[-1]
    assert "--append-system-prompt" not in remote_cmd
