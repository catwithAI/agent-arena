from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.adapters.base import AdapterRunInput, McpServerSpec
from backend.agents.deerflow.plugin import DeerFlowAdapter
from backend.agents.registry import AgentRegistry
from backend.config import Settings


_FAKE_CLIENT = """
from pathlib import Path
import subprocess
import sys
import time
import yaml


class DeerFlowClient:
    def __init__(
        self,
        *,
        config_path,
        model_name,
        thinking_enabled,
        subagent_enabled,
        plan_mode,
    ):
        self.config_path = config_path
        self.model_name = model_name
        self.thinking_enabled = thinking_enabled
        self.subagent_enabled = subagent_enabled
        self.plan_mode = plan_mode

    def stream(self, prompt, *, thread_id, recursion_limit):
        config = yaml.safe_load(Path(self.config_path).read_text())
        workspace = Path(config["sandbox"]["mounts"][0]["host_path"])
        if "provider fails" in prompt:
            raise RuntimeError("rate limit from provider")
        if "spawn a child" in prompt:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
            (workspace / "child.pid").write_text(str(child.pid))
            while True:
                time.sleep(0.1)
        material = (workspace / "input.txt").read_text()
        (workspace / "result.txt").write_text(prompt + "\\nmaterial=" + material)
        yield {
            "type": "messages-tuple",
            "data": {
                "id": "tool-message",
                "type": "tool",
                "content": "wrote result.txt",
            },
        }
        yield {
            "type": "messages-tuple",
            "data": {
                "id": "final-message",
                "type": "ai",
                "content": (
                    f"model={self.model_name};subagent={self.subagent_enabled};"
                    f"thinking={self.thinking_enabled};plan={self.plan_mode};"
                    f"limit={recursion_limit}"
                ),
                "additional_kwargs": {"reasoning_content": "fixture reasoning"},
                "usage_metadata": {"input_tokens": 7, "output_tokens": 11},
            },
        }
        yield {"type": "end", "data": {}}
"""


def _adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DeerFlowAdapter:
    package_root = tmp_path / "fake-package"
    deerflow = package_root / "deerflow"
    deerflow.mkdir(parents=True)
    (deerflow / "__init__.py").write_text("", encoding="utf-8")
    (deerflow / "client.py").write_text(_FAKE_CLIENT, encoding="utf-8")
    existing = os.environ.get("PYTHONPATH")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(package_root) if not existing else f"{package_root}{os.pathsep}{existing}",
    )
    settings = Settings(
        model_providers={
            "fixture": {
                "kind": "openai-chat",
                "base_url": "https://models.example.invalid/v1",
                "api_key": "deerflow-e2e-secret",
            }
        }
    )
    built = (
        AgentRegistry.from_settings(settings)
        .resolve("deerflow")
        .build_adapter("fixture/fake-model")
    )
    assert isinstance(built, DeerFlowAdapter)
    return built


@pytest.mark.asyncio
@pytest.mark.parametrize("subagent", [False, True])
async def test_deerflow_adapter_fake_embedded_client_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subagent: bool
):
    adapter = _adapter(tmp_path, monkeypatch)
    env_dir = tmp_path / "environment"
    env_dir.mkdir()
    material = env_dir / "input.txt"
    material.write_text("fixture material", encoding="utf-8")
    task = AdapterRunInput(
        attempt_id=f"attempt-deerflow-{subagent}",
        task_id="task-deerflow",
        task_prompt="Read input.txt and create result.txt.",
        task_context={
            "uploaded_files": [{"name": "input.txt", "path": str(material)}],
            "_agent_options": {
                "deerflow": {
                    "subagent": subagent,
                    "thinking": False,
                    "plan_mode": True,
                    "summarize": False,
                    "recursion_limit": 88,
                    "allow_host_bash": False,
                }
            },
        },
        timeout_seconds=10,
        env_name="fake-env",
        env_skill_id="lane/fake-env",
        session_token="lane-session-secret",
        env_base_url="http://127.0.0.1:9999",
    )

    result = await adapter.run(task, SimpleNamespace(env_dir=env_dir), tmp_path / "data")

    attempt = tmp_path / "data" / "attempts" / task.attempt_id
    manifest_path = attempt / ".agent-control" / "agent-manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    final = (attempt / "agent_final.txt").read_text(encoding="utf-8")
    artifact = (attempt / "skill_workspace" / "result.txt").read_text(encoding="utf-8")
    assert result.status == "completed", (result.error_code, result.error_message)
    assert result.token_usage == {"input_tokens": 7, "output_tokens": 11}
    assert result.events_count == 3
    assert result.thinking_count == 1
    assert f"subagent={subagent}" in final
    assert "thinking=False;plan=True;limit=88" in final
    assert "Read input.txt and create result.txt." in artifact
    assert "material=fixture material" in artifact
    assert "/mnt/arena-workspace" in artifact
    assert manifest["status"] == "final"
    assert manifest["model"]["effective"] == "fixture/fake-model"
    assert manifest["config_summary"]["options"]["subagent"] is subagent
    assert manifest["sessions"] == [
        {"turn_id": "task-deerflow::t0", "session_id": f"arena-{task.attempt_id}"}
    ]
    assert "deerflow-e2e-secret" not in manifest_text
    assert "lane-session-secret" not in manifest_text
    assert str(attempt) not in manifest_text


@pytest.mark.asyncio
async def test_deerflow_adapter_rejects_unvalidated_lane_mcp(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    task = AdapterRunInput(
        attempt_id="attempt-mcp-rejected",
        task_id="task-deerflow",
        task_prompt="work",
        task_context={},
        timeout_seconds=1,
        env_name="fake-env",
        env_skill_id="lane/fake-env",
        session_token="session",
        env_base_url="http://127.0.0.1:9999",
        mcp_servers=(McpServerSpec(name="lane", command="lane-server"),),
    )

    result = await adapter.run(
        task, SimpleNamespace(env_dir=tmp_path / "environment"), tmp_path / "data"
    )

    assert result.status == "cli_error"
    assert result.error_code == "agent_launch_plan_invalid"
    assert "not supported" in (result.error_message or "")


@pytest.mark.asyncio
async def test_deerflow_adapter_routes_provider_exception_to_shared_taxonomy(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    env_dir = tmp_path / "environment"
    env_dir.mkdir()
    task = AdapterRunInput(
        attempt_id="attempt-provider-error",
        task_id="task-deerflow",
        task_prompt="provider fails",
        task_context={},
        timeout_seconds=5,
        env_name="fake-env",
        env_skill_id="lane/fake-env",
        session_token="session",
        env_base_url="http://127.0.0.1:9999",
    )

    result = await adapter.run(task, SimpleNamespace(env_dir=env_dir), tmp_path / "data")

    assert result.status == "cli_error"
    assert result.error_code == "agent_rate_limited"
    assert result.error_message == "agent exited with code 25; matched agent_rate_limited"


async def _wait_for_path(path: Path, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


async def _assert_process_gone(pid: int) -> None:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.02)
    os.kill(pid, signal.SIGKILL)
    raise AssertionError(f"child process {pid} survived DeerFlow cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_deerflow_adapter_cleans_runner_process_group(tmp_path, monkeypatch, mode):
    adapter = _adapter(tmp_path, monkeypatch)
    env_dir = tmp_path / "environment"
    env_dir.mkdir()
    attempt_id = f"attempt-cleanup-{mode}"
    task = AdapterRunInput(
        attempt_id=attempt_id,
        task_id="task-cleanup",
        task_prompt="spawn a child and wait",
        task_context={},
        timeout_seconds=1 if mode == "timeout" else None,
        env_name="fake-env",
        env_skill_id="lane/fake-env",
        session_token="session",
        env_base_url="http://127.0.0.1:9999",
    )
    pid_path = tmp_path / "data" / "attempts" / attempt_id / "skill_workspace" / "child.pid"

    run = asyncio.create_task(
        adapter.run(task, SimpleNamespace(env_dir=env_dir), tmp_path / "data")
    )
    await _wait_for_path(pid_path)
    pid = int(pid_path.read_text(encoding="utf-8"))
    if mode == "cancel":
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    else:
        result = await run
        assert result.status == "timeout"
        assert result.error_code == "agent_timeout"

    await _assert_process_gone(pid)
    manifest = json.loads(
        (
            tmp_path / "data" / "attempts" / attempt_id / ".agent-control" / "agent-manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "final"
    assert manifest["outcome"]["status"] == ("cancelled" if mode == "cancel" else "timeout")
