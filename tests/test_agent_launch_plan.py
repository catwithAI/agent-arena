from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents.launch import LaunchContext, LaunchPlanError, render_launch_plan
from backend.agents.models import AgentSpec


def _spec(*, prompt=None, launch=None) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "schema_version": "1",
            "id": "launch-agent",
            "display_name": "Launch Agent",
            "source": "config",
            "transport": "local-cli",
            "implementation": {"kind": "profile-runtime"},
            "availability": {"executable": "launch-agent"},
            "launch": launch
            or {
                "executable": "launch-agent",
                "args": [
                    "run",
                    {"flag": "--prompt", "value": "prompt"},
                    {"flag": "--model", "value": "effective_model", "omit_if_none": True},
                    {"flag": "--session", "value": "session_id", "omit_if_none": True},
                ],
                "env": {
                    "STATIC_SETTING": "on",
                    "MODEL_API_KEY": {"secret_ref": "MODEL_SECRET"},
                },
            },
            "prompt": prompt or {"mode": "arg"},
            "model": {"binding": "flag", "flag": "--model"},
            "mcp": {"dialect": "unsupported"},
            "output": {"parser": "text"},
            "capabilities": {"single_turn": "declared"},
            "isolation": {"execution_locus": "host", "network_required": "public_internet"},
        }
    )


def _context(root: Path, **overrides) -> LaunchContext:
    values = {
        "prompt": "fix $(touch /tmp/not-executed); `whoami`",
        "attempt_workspace": root / "workspace",
        "project_path": root / "project",
        "attempt_private": root / "private",
        "prompt_file": root / "private" / "prompt.txt",
        "effective_model": None,
        "session_id": None,
    }
    values.update(overrides)
    return LaunchContext(**values)


def test_tokenized_argv_omits_none_and_preserves_shell_characters(tmp_path):
    plan = render_launch_plan(_spec(), _context(tmp_path), secrets={"MODEL_SECRET": "s3cr3t"})
    assert plan.argv == (
        "launch-agent",
        "run",
        "--prompt",
        "fix $(touch /tmp/not-executed); `whoami`",
    )
    assert plan.env["MODEL_API_KEY"] == "s3cr3t"
    assert plan.env_redacted["MODEL_API_KEY"] == "***"
    assert "s3cr3t" not in repr(plan.argv_redacted)
    assert plan.env_names == ("MODEL_API_KEY", "STATIC_SETTING")


def test_missing_secret_fails_before_launch(tmp_path):
    with pytest.raises(LaunchPlanError, match="secret reference"):
        render_launch_plan(_spec(), _context(tmp_path), secrets={})


def test_plan_hash_excludes_dynamic_paths_session_and_secret(tmp_path):
    first = render_launch_plan(
        _spec(),
        _context(tmp_path / "one", session_id="random-one"),
        secrets={"MODEL_SECRET": "first-secret"},
    )
    second = render_launch_plan(
        _spec(),
        _context(tmp_path / "two", session_id="random-two"),
        secrets={"MODEL_SECRET": "second-secret"},
    )
    assert first.argv != second.argv  # session is omitted in both here; cwd differs below
    assert first.cwd != second.cwd
    assert first.plan_hash == second.plan_hash
    assert "first-secret" not in first.plan_hash


def test_long_arg_can_fail_or_fallback_to_file(tmp_path):
    context = _context(tmp_path, prompt="x" * 1000)
    with pytest.raises(LaunchPlanError, match="exceeding limit"):
        render_launch_plan(_spec(), context, secrets={"MODEL_SECRET": "x"}, arg_max=300)

    spec = _spec(prompt={"mode": "arg", "arg_fallback": "file"})
    plan = render_launch_plan(spec, context, secrets={"MODEL_SECRET": "x"}, arg_max=300)
    assert plan.prompt_mode == "file"
    assert str(context.prompt_file.resolve()) in plan.argv
    assert "x" * 1000 not in plan.argv


def test_long_arg_can_fallback_to_stdin_and_drop_prompt_flag(tmp_path):
    spec = _spec(prompt={"mode": "arg", "arg_fallback": "stdin"})
    context = _context(tmp_path, prompt="x" * 1000)
    plan = render_launch_plan(spec, context, secrets={"MODEL_SECRET": "x"}, arg_max=300)
    assert plan.prompt_mode == "stdin"
    assert plan.stdin_data == b"x" * 1000
    assert "--prompt" not in plan.argv


def test_undeclared_runtime_options_are_rejected(tmp_path):
    with pytest.raises(LaunchPlanError, match="undeclared agent options"):
        render_launch_plan(
            _spec(),
            _context(tmp_path, options={"api_key": "must-not-enter-argv"}),
            secrets={"MODEL_SECRET": "x"},
        )
