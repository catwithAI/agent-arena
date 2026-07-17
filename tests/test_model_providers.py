"""backend.model_providers: resolve_api_key 优先级 + CC/Codex adapter 缺 key fail fast。

覆盖：
- resolve_api_key：环境变量优先，未设时回落到 ModelProviderSection.api_key
  （arena.yaml 直填，已 gitignore）；两者皆无返回 None。
- provider 命中但 env/config 都没 key 时，ClaudeCodeAdapter / CodexAdapter 直接
  返回 auth_failed + provider_api_key_missing，不启子进程——避免 CLI 自己报
  「Not logged in」/「Missing environment variable」这类让人摸不着头脑的错误。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from backend.adapters.base import AdapterRunInput
from backend.adapters.claude_code import ClaudeCodeAdapter
from backend.adapters.codex import CodexAdapter
from backend.model_providers import ModelProviderSection, resolve_api_key

_aio = pytest.mark.asyncio

_OPENROUTER = {"openrouter": ModelProviderSection(
    kind="openai-chat", base_url="https://openrouter.ai/api/v1",
    api_key_env="OPENROUTER_API_KEY",
)}


def _make_task(**over) -> AdapterRunInput:
    d = dict(attempt_id="att_x", task_id="t", task_prompt="p", task_context={},
             timeout_seconds=10, env_name="travel-planner",
             env_skill_id="lane/travel-planner", session_token="tok",
             env_base_url="http://127.0.0.1:8100")
    d.update(over)
    return AdapterRunInput(**d)


_STREAM_OK = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "done", "session_id": "s", "usage": {},
})


class FakeProcess:
    def __init__(self):
        self.returncode = 0
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        r = asyncio.StreamReader()
        r.feed_data((_STREAM_OK + "\n").encode())
        r.feed_eof()
        self.stdout = r

    async def wait(self):
        return 0

    def kill(self):
        pass


# ---------- resolve_api_key ---------------------------------------------------


def test_resolve_api_key_env_only():
    p = ModelProviderSection(kind="openai-chat", base_url="x", api_key_env="X_KEY")
    with patch.dict("os.environ", {"X_KEY": "sk-env"}, clear=False):
        assert resolve_api_key(p) == "sk-env"


def test_resolve_api_key_missing_env_returns_none():
    p = ModelProviderSection(kind="openai-chat", base_url="x", api_key_env="X_KEY_NOPE")
    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("X_KEY_NOPE", None)
        assert resolve_api_key(p) is None


def test_resolve_api_key_no_env_configured():
    p = ModelProviderSection(kind="openai-chat", base_url="x")
    assert resolve_api_key(p) is None


def test_resolve_api_key_falls_back_to_config(monkeypatch):
    """env 未设时回落到 arena.yaml 直填的 api_key。"""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    p = ModelProviderSection(
        kind="openai-chat", base_url="x",
        api_key_env="OPENROUTER_API_KEY", api_key="sk-from-yaml",
    )
    assert resolve_api_key(p) == "sk-from-yaml"


def test_resolve_api_key_env_wins_over_config(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    p = ModelProviderSection(
        kind="openai-chat", base_url="x",
        api_key_env="OPENROUTER_API_KEY", api_key="sk-from-yaml",
    )
    assert resolve_api_key(p) == "sk-from-env"


# ---------- adapter fail-fast --------------------------------------------------


@_aio
async def test_claude_code_provider_key_missing_fails_fast(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    adapter = ClaudeCodeAdapter(model="openrouter/glm-5.2", providers=_OPENROUTER)
    with patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("asyncio.create_subprocess_exec", return_value=FakeProcess()) as spawn:
        result = await adapter.run(_make_task(), None, tmp_path)
    assert spawn.call_count == 0
    assert result.status == "auth_failed"
    assert result.error_code == "provider_api_key_missing"
    assert "OPENROUTER_API_KEY" in (result.error_message or "")


@_aio
async def test_codex_provider_key_missing_fails_fast(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    adapter = CodexAdapter(model="openrouter/glm-5.2", providers=_OPENROUTER)
    with patch("shutil.which", return_value="/usr/local/bin/codex"), \
         patch("asyncio.create_subprocess_exec", return_value=FakeProcess()) as spawn:
        result = await adapter.run(_make_task(), None, tmp_path)
    assert spawn.call_count == 0
    assert result.status == "auth_failed"
    assert result.error_code == "provider_api_key_missing"
    assert "OPENROUTER_API_KEY" in (result.error_message or "")


@_aio
async def test_claude_code_provider_key_present_does_not_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    adapter = ClaudeCodeAdapter(model="openrouter/glm-5.2", providers=_OPENROUTER)
    with patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("asyncio.create_subprocess_exec", return_value=FakeProcess()) as spawn:
        result = await adapter.run(_make_task(), None, tmp_path)
    assert spawn.call_count == 1
    assert result.status != "auth_failed"


@_aio
async def test_codex_provider_key_present_does_not_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    adapter = CodexAdapter(model="openrouter/glm-5.2", providers=_OPENROUTER)
    with patch("shutil.which", return_value="/usr/local/bin/codex"), \
         patch("asyncio.create_subprocess_exec", return_value=FakeProcess()) as spawn:
        result = await adapter.run(_make_task(), None, tmp_path)
    assert spawn.call_count == 1
    assert result.status != "auth_failed"
