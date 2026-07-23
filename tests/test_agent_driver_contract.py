from __future__ import annotations

import stat
from pathlib import Path

import pytest

from backend.adapters.base import AdapterRunInput, ConversationTurn
from backend.agents.drivers.oneshot import OneShotDriver, OneShotDriverError
from backend.agents.drivers.command_resume import CommandResumeDriver, CommandResumeDriverError
from backend.agents.models import AgentSpec


def _task(**overrides) -> AdapterRunInput:
    values = {
        "attempt_id": "att-1",
        "task_id": "task-1",
        "task_prompt": "Create the requested artifact.",
        "task_context": {
            "public": "value",
            "_private": "hidden",
            "uploaded_files": [{"name": "input.txt", "host_path": "/secret/path"}],
        },
        "timeout_seconds": 90,
        "env_name": "fixture-env",
        "env_skill_id": "lane/fixture-env",
        "session_token": "token",
        "env_base_url": "http://127.0.0.1:8100",
    }
    values.update(overrides)
    return AdapterRunInput(**values)


def _spec(prompt_mode: str, *, single_turn: str = "verified") -> AgentSpec:
    prompt = {"mode": prompt_mode}
    if prompt_mode == "arg":
        prompt["arg_fallback"] = "file"
    return AgentSpec.model_validate(
        {
            "schema_version": "1",
            "id": f"driver-{prompt_mode}",
            "display_name": "Driver Fixture",
            "source": "config",
            "transport": "local-cli",
            "implementation": {"kind": "profile-runtime"},
            "availability": {"executable": "fixture"},
            "launch": {"executable": "fixture", "args": []},
            "prompt": prompt,
            "model": {"binding": "agent-default"},
            "mcp": {"dialect": "unsupported"},
            "output": {"parser": "text"},
            "capabilities": {"single_turn": single_turn},
            "isolation": {"execution_locus": "host", "network_required": "none"},
        }
    )


def _prepare(tmp_path: Path, spec: AgentSpec, task: AdapterRunInput | None = None):
    return OneShotDriver().prepare(
        spec=spec,
        task=task or _task(),
        attempt_workspace=tmp_path / "workspace",
        project_path=tmp_path / "project",
        attempt_private=tmp_path / spec.id,
    )


def test_prompt_semantics_and_hash_are_identical_across_transports(tmp_path):
    plans = [_prepare(tmp_path, _spec(mode)) for mode in ("stdin", "file", "arg")]
    assert len({plan.prompt.content_hash for plan in plans}) == 1
    assert len({plan.prompt.text for plan in plans}) == 1
    prompt = plans[0].prompt.text
    assert "1 minute(s) 30 second(s)" in prompt
    assert prompt.count("time budget") == 1
    assert '"public": "value"' in prompt
    assert "input.txt" in prompt
    assert "/secret/path" not in prompt
    assert "_private" not in prompt


def test_file_prompt_is_owner_only_and_matches_rendered_hash(tmp_path):
    plan = _prepare(tmp_path, _spec("file"))
    prompt_file = plan.launch_context.prompt_file
    assert prompt_file is not None
    assert prompt_file.read_text(encoding="utf-8") == plan.prompt.text
    assert stat.S_IMODE(prompt_file.stat().st_mode) == 0o600


def test_oneshot_rejects_multiturn_interaction_and_unsupported_capability(tmp_path):
    turns = (
        ConversationTurn(turn_id="t0", turn_index=0, prompt="first"),
        ConversationTurn(turn_id="t1", turn_index=1, prompt="second"),
    )
    with pytest.raises(OneShotDriverError, match="cannot execute 2"):
        _prepare(tmp_path, _spec("stdin"), _task(conversation_turns=turns))
    with pytest.raises(OneShotDriverError, match="does not support"):
        _prepare(tmp_path, _spec("stdin", single_turn="unsupported"))


def test_driver_owned_prompt_is_not_claimed_by_oneshot(tmp_path):
    with pytest.raises(OneShotDriverError, match="driver-owned"):
        _prepare(tmp_path, _spec("driver-owned"))


def _resume_spec(*, resume_args=None) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "schema_version": "1",
            "id": "resume-fixture",
            "display_name": "Resume Fixture",
            "source": "config",
            "transport": "local-cli",
            "implementation": {"kind": "profile-runtime"},
            "availability": {"executable": "fixture"},
            "launch": {
                "executable": "fixture",
                "args": [{"flag": "--prompt", "value": "prompt"}],
            },
            "prompt": {"mode": "arg"},
            "driver": {
                "kind": "command-resume",
                "resume_args": resume_args
                or [
                    "resume",
                    {"flag": "--session", "value": "session_id"},
                    {"flag": "--prompt", "value": "prompt"},
                ],
            },
            "model": {"binding": "agent-default"},
            "mcp": {"dialect": "unsupported"},
            "output": {"parser": "jsonl", "config": {"session_field": "session_id"}},
            "capabilities": {
                "single_turn": "verified",
                "resume_send_message": "verified",
            },
            "isolation": {"execution_locus": "host", "network_required": "none"},
        }
    )


def _three_turn_task(attempt_id="att-1") -> AdapterRunInput:
    turns = tuple(
        ConversationTurn(turn_id=f"t{index}", turn_index=index, prompt=prompt)
        for index, prompt in enumerate(("first", "second", "third"))
    )
    return _task(attempt_id=attempt_id, conversation_turns=turns)


def _prepare_resume(tmp_path, *, attempt_id="att-1"):
    return CommandResumeDriver().prepare(
        spec=_resume_spec(),
        task=_three_turn_task(attempt_id),
        attempt_workspace=tmp_path / attempt_id / "workspace",
        project_path=tmp_path / "project",
        attempt_private=tmp_path / attempt_id / "private",
    )


def test_command_resume_builds_explicit_three_turn_plans(tmp_path):
    plan = _prepare_resume(tmp_path)
    first = plan.render_turn(0)
    session_id = plan.resolve_session(["session-A"])
    second = plan.render_turn(1, session_id=session_id)
    third = plan.render_turn(2, session_id=session_id)

    assert first.argv == ("fixture", "--prompt", plan.turns[0].prompt.text)
    assert "--session" not in first.argv
    assert second.argv == ("fixture", "resume", "--session", "session-A", "--prompt", "second")
    assert third.argv[-1] == "third"
    assert second.argv.count("session-A") == 1
    assert plan.turns[0].prompt.text.count("time budget") == 1
    assert "time budget" not in plan.turns[1].prompt.text
    assert "public" not in plan.turns[1].prompt.text


def test_command_resume_also_builds_a_single_turn_plan(tmp_path):
    plan = CommandResumeDriver().prepare(
        spec=_resume_spec(),
        task=_task(),
        attempt_workspace=tmp_path / "workspace",
        project_path=tmp_path / "project",
        attempt_private=tmp_path / "private",
    )

    assert len(plan.turns) == 1
    assert plan.render_turn(0).argv[:2] == ("fixture", "--prompt")


def test_command_resume_refuses_missing_multiple_or_implicit_latest_sessions(tmp_path):
    plan = _prepare_resume(tmp_path)
    with pytest.raises(CommandResumeDriverError, match="did not yield"):
        plan.resolve_session([])
    with pytest.raises(CommandResumeDriverError, match="multiple session"):
        plan.resolve_session(["one", "two"])
    with pytest.raises(CommandResumeDriverError, match="requires"):
        plan.render_turn(1)
    with pytest.raises(ValueError, match="latest/implicit"):
        _resume_spec(
            resume_args=["--continue", {"flag": "--session", "value": "session_id"}]
        )


def test_command_resume_parallel_attempts_never_share_discovered_session(tmp_path):
    left = _prepare_resume(tmp_path, attempt_id="left")
    right = _prepare_resume(tmp_path, attempt_id="right")
    left_id = left.resolve_session(["session-left"])
    right_id = right.resolve_session(["session-right"])

    assert left.render_turn(1, session_id=left_id).argv[3] == "session-left"
    assert right.render_turn(1, session_id=right_id).argv[3] == "session-right"
    assert left.render_turn(1, session_id=left_id).cwd != right.render_turn(
        1, session_id=right_id
    ).cwd
