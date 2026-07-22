from __future__ import annotations

import stat

import pytest
import yaml
from pydantic import ValidationError

from backend.agents.deerflow.config import (
    DeerFlowConfigError,
    build_private_config,
    validate_workspace,
)
from backend.model_providers import ModelProviderSection


@pytest.mark.parametrize(
    ("kind", "integration", "extra"),
    [
        ("anthropic", "langchain_anthropic:ChatAnthropic", {}),
        ("openai-chat", "langchain_openai:ChatOpenAI", {}),
        (
            "openai-responses",
            "langchain_openai:ChatOpenAI",
            {"use_responses_api": True, "output_version": "responses/v1"},
        ),
    ],
)
def test_private_config_provider_goldens_do_not_persist_secret(tmp_path, kind, integration, extra):
    secret = "deerflow-provider-secret"
    result = build_private_config(
        private_root=tmp_path / "private",
        requested_model="provider/model-name",
        providers={
            "provider": ModelProviderSection(
                kind=kind,
                base_url="https://models.example.invalid/v1",
                api_key=secret,
            )
        },
        raw_options={
            "subagent": True,
            "thinking": False,
            "plan_mode": True,
            "summarize": False,
            "recursion_limit": 321,
            "allow_host_bash": False,
        },
    )

    text = result.config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(text)
    model = config["models"][0]
    assert model == {
        "name": "arena-model",
        "display_name": "provider/model-name",
        "use": integration,
        "model": "model-name",
        "api_key": "$DEERFLOW_ARENA_MODEL_API_KEY",
        "max_retries": 2,
        "base_url": "https://models.example.invalid/v1",
        ("default_request_timeout" if kind == "anthropic" else "request_timeout"): 600.0,
        **extra,
    }
    assert config["sandbox"]["allow_host_bash"] is False
    assert "summarization" not in config
    assert "database" not in config
    assert "run_events" not in config
    assert "memory" not in config
    assert [tool["name"] for tool in config["tools"]] == [
        "ls",
        "read_file",
        "glob",
        "grep",
        "write_file",
        "str_replace",
    ]
    assert result.summary["effective_model"] == "arena-model"
    assert result.summary["options"]["recursion_limit"] == 321
    assert result.child_env == {"DEERFLOW_ARENA_MODEL_API_KEY": secret}
    assert secret not in text
    assert stat.S_IMODE(result.config_path.stat().st_mode) == 0o600
    assert result.project_dir.is_relative_to(tmp_path / "private")
    assert result.home_dir.is_relative_to(tmp_path / "private")


def test_private_config_rejects_unknown_provider_missing_key_and_unsafe_url(tmp_path):
    provider = ModelProviderSection(
        kind="openai-chat",
        base_url="https://models.example.invalid/v1",
    )
    with pytest.raises(DeerFlowConfigError, match="configured provider/model"):
        build_private_config(
            private_root=tmp_path,
            requested_model="unknown/model",
            providers={"known": provider},
        )
    with pytest.raises(DeerFlowConfigError, match="no available API key"):
        build_private_config(
            private_root=tmp_path,
            requested_model="known/model",
            providers={"known": provider},
        )
    with pytest.raises(DeerFlowConfigError, match="must not contain credentials"):
        build_private_config(
            private_root=tmp_path,
            requested_model="known/model",
            providers={
                "known": ModelProviderSection(
                    kind="openai-chat",
                    base_url="https://user:password@example.invalid/v1",
                    api_key="secret",
                )
            },
        )


def test_private_config_options_are_strict_and_bounded(tmp_path):
    provider = ModelProviderSection(
        kind="openai-chat",
        base_url="https://models.example.invalid/v1",
        api_key="secret",
    )
    with pytest.raises(ValidationError):
        build_private_config(
            private_root=tmp_path,
            requested_model="known/model",
            providers={"known": provider},
            raw_options={"subagent": "yes"},
        )
    with pytest.raises(ValidationError):
        build_private_config(
            private_root=tmp_path,
            requested_model="known/model",
            providers={"known": provider},
            raw_options={"recursion_limit": 0},
        )
    with pytest.raises(DeerFlowConfigError, match="summarize=true is unsupported"):
        build_private_config(
            private_root=tmp_path,
            requested_model="known/model",
            providers={"known": provider},
            raw_options={"summarize": True},
        )


def test_host_bash_option_adds_only_the_official_bash_tool(tmp_path):
    result = build_private_config(
        private_root=tmp_path,
        requested_model="known/model",
        providers={
            "known": ModelProviderSection(
                kind="openai-chat",
                base_url="https://models.example.invalid/v1",
                api_key="secret",
            )
        },
        raw_options={"allow_host_bash": True},
    )

    config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))
    assert config["sandbox"]["allow_host_bash"] is True
    assert config["tools"][-1] == {
        "name": "bash",
        "group": "bash",
        "use": "deerflow.sandbox.tools:bash_tool",
    }


def test_workspace_bridge_mounts_only_real_attempt_workspace(tmp_path):
    attempt = tmp_path / "attempt"
    workspace = attempt / "skill_workspace"
    workspace.mkdir(parents=True)
    result = build_private_config(
        private_root=attempt / ".agent-runtime" / "deerflow",
        workspace=workspace,
        attempt_root=attempt,
        requested_model="known/model",
        providers={
            "known": ModelProviderSection(
                kind="openai-chat",
                base_url="https://models.example.invalid/v1",
                api_key="secret",
            )
        },
    )

    config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))
    assert config["sandbox"]["mounts"] == [
        {
            "host_path": str(workspace.resolve()),
            "container_path": "/mnt/arena-workspace",
            "read_only": False,
        }
    ]
    assert result.summary["workspace"] == "/mnt/arena-workspace"


def test_workspace_bridge_rejects_wrong_root_traversal_and_symlinks(tmp_path):
    attempt = tmp_path / "attempt"
    workspace = attempt / "skill_workspace"
    workspace.mkdir(parents=True)
    wrong = tmp_path / "wrong" / "skill_workspace"
    wrong.mkdir(parents=True)
    with pytest.raises(DeerFlowConfigError, match="outside its Attempt root"):
        validate_workspace(wrong, attempt_root=attempt)
    with pytest.raises(DeerFlowConfigError, match="must be the Attempt"):
        validate_workspace(attempt / "skill_workspace" / "..", attempt_root=attempt)

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(DeerFlowConfigError, match="contains a symlink"):
        validate_workspace(workspace, attempt_root=attempt)
