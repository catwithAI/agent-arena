from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.adapters.base import AdapterRunInput, ConversationTurn
from backend.agents.drivers.command_resume import CommandResumeDriver
from backend.agents.parsers import EvidenceSet, JsonlMappingParser
from backend.agents.registry import AgentRegistry
from backend.agents.transports import ProfileRuntimeAdapter
from backend.config import Settings


def _task() -> AdapterRunInput:
    return AdapterRunInput(
        attempt_id="attempt-cli-builtins",
        task_id="task-cli-builtins",
        task_prompt="first prompt",
        task_context={},
        timeout_seconds=30,
        env_name="fixture",
        env_skill_id="lane/fixture",
        session_token="fixture-session-token",
        env_base_url="http://127.0.0.1:8100",
        conversation_turns=(
            ConversationTurn(turn_id="turn-0", turn_index=0, prompt="first prompt"),
            ConversationTurn(turn_id="turn-1", turn_index=1, prompt="second prompt"),
        ),
    )


def _plans(agent_id: str, tmp_path: Path, *, model: str | None = None):
    spec = AgentRegistry.from_settings(Settings()).resolve(agent_id).spec
    plan = CommandResumeDriver().prepare(
        spec=spec,
        task=_task(),
        attempt_workspace=tmp_path / "workspace",
        project_path=tmp_path / "project",
        attempt_private=tmp_path / "private",
        effective_model=model,
        mcp_config_file=tmp_path / "private" / "mcp.json",
    )
    return spec, plan.render_turn(0), plan.render_turn(1, session_id="session-123")


def _evidence(tmp_path: Path, records: list[dict]) -> EvidenceSet:
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    (raw / "stdout.log").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    (raw / "stderr.log").write_text("", encoding="utf-8")
    return EvidenceSet.from_runtime_dir(tmp_path)


def test_kimi_code_builtin_uses_print_json_resume_and_mcp(tmp_path: Path):
    spec, first, resumed = _plans("kimi-code", tmp_path, model="kimi-code/kimi-for-coding")

    assert isinstance(
        AgentRegistry.from_settings(Settings()).resolve("kimi-code").build_adapter(),
        ProfileRuntimeAdapter,
    )
    assert spec.isolation.inherit_user_config is False
    assert {item.env_var for item in spec.auth if item.required} == {
        "KIMI_MODEL_NAME",
        "KIMI_MODEL_API_KEY",
    }
    assert first.argv[:2] == ("kimi", "-p")
    assert first.argv[first.argv.index("-m") + 1] == "kimi-code/kimi-for-coding"
    assert first.argv[first.argv.index("--mcp-config-file") + 1].endswith("mcp.json")
    assert resumed.argv[:3] == ("kimi", "--session", "session-123")
    assert "--output-format" in resumed.argv


def test_mimo_code_builtin_uses_headless_json_and_explicit_resume(tmp_path: Path):
    spec, first, resumed = _plans("mimo-code", tmp_path, model="mimo/mimo-v2.5")

    assert isinstance(
        AgentRegistry.from_settings(Settings()).resolve("mimo-code").build_adapter(),
        ProfileRuntimeAdapter,
    )
    assert spec.isolation.inherit_user_config is False
    assert first.argv[:5] == (
        "mimo",
        "run",
        "--format",
        "json",
        "--dangerously-skip-permissions",
    )
    assert first.argv[first.argv.index("--model") + 1] == "mimo/mimo-v2.5"
    assert resumed.argv[resumed.argv.index("--session") + 1] == "session-123"


@pytest.mark.asyncio
async def test_kimi_stream_json_mapping_extracts_final_session_and_tools(tmp_path: Path):
    records = [
        {"role": "assistant", "content": "checking", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        {"role": "assistant", "content": "done"},
        {
            "role": "meta",
            "type": "session.resume_hint",
            "session_id": "kimi-session",
            "content": "resume hint",
        },
    ]
    spec = AgentRegistry.from_settings(Settings()).resolve("kimi-code").spec
    result = await JsonlMappingParser(**spec.output.config).parse(_evidence(tmp_path, records))

    assert result.final_text == "done"
    assert result.session_id == "kimi-session"
    assert len(result.tool_refs) == 1


@pytest.mark.asyncio
async def test_mimo_run_json_mapping_extracts_final_session_thinking_and_usage(tmp_path: Path):
    records = [
        {"type": "reasoning", "sessionID": "mimo-session", "part": {"text": "think"}},
        {"type": "tool_use", "sessionID": "mimo-session", "part": {"tool": "read"}},
        {
            "type": "step_finish",
            "sessionID": "mimo-session",
            "part": {"tokens": {"input_tokens": 7, "output_tokens": 3}},
        },
        {"type": "text", "sessionID": "mimo-session", "part": {"text": "done"}},
    ]
    spec = AgentRegistry.from_settings(Settings()).resolve("mimo-code").spec
    result = await JsonlMappingParser(**spec.output.config).parse(_evidence(tmp_path, records))

    assert result.final_text == "done"
    assert result.session_id == "mimo-session"
    assert result.usage == {"input_tokens": 7, "output_tokens": 3}
    assert len(result.thinking) == 1
    assert len(result.tool_refs) == 1
