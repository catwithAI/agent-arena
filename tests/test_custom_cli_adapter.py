from __future__ import annotations

import sys
from pathlib import Path

from backend.adapters.base import AdapterRunInput
from backend.adapters.custom_cli import CustomCliAdapter, CustomCliConfig


def _task(attempt_id: str, env_name: str = "order-desk") -> AdapterRunInput:
    return AdapterRunInput(
        attempt_id=attempt_id,
        task_id="task_1",
        task_prompt="say hello",
        task_context={},
        timeout_seconds=10,
        env_name=env_name,
        env_skill_id=f"lane/{env_name}",
        session_token="tok",
        env_base_url="http://127.0.0.1:8100",
    )


async def test_custom_cli_adapter_runs_text_agent(tmp_path: Path):
    config = CustomCliConfig(
        name="echo-agent",
        command=[sys.executable, "-c", "import sys; print('done: ' + sys.argv[1])", "{prompt}"],
        prompt_mode="arg",
        output_format="text",
    )
    adapter = CustomCliAdapter(config)
    result = await adapter.run(_task("att_1"), env=None, data_path=tmp_path)
    assert result.status == "completed"
    final = (tmp_path / "attempts" / "att_1" / "echo-agent_final.txt").read_text()
    assert "done:" in final


async def test_custom_cli_adapter_reports_missing_binary(tmp_path: Path):
    config = CustomCliConfig(name="ghost", command=["definitely-not-a-real-binary-xyz"])
    adapter = CustomCliAdapter(config)
    result = await adapter.run(_task("att_2"), env=None, data_path=tmp_path)
    assert result.status == "cli_not_found"


async def test_custom_cli_adapter_parses_jsonl_thinking(tmp_path: Path):
    script = (
        "import json,sys;"
        "print(json.dumps({'type': 'thinking', 'text': 'considering options'}));"
        "print(json.dumps({'type': 'final', 'text': 'ok', 'usage': {'input_tokens': 5, 'output_tokens': 3}}))"
    )
    config = CustomCliConfig(
        name="jsonl-agent",
        command=[sys.executable, "-c", script],
        prompt_mode="stdin",
        output_format="jsonl",
    )
    adapter = CustomCliAdapter(config)
    result = await adapter.run(_task("att_3"), env=None, data_path=tmp_path)
    assert result.status == "completed"
    assert result.thinking_count == 1
    assert result.token_usage == {"input_tokens": 5, "output_tokens": 3}


async def test_custom_cli_adapter_times_out(tmp_path: Path):
    config = CustomCliConfig(
        name="slow-agent",
        command=[sys.executable, "-c", "import time; time.sleep(5)"],
        prompt_mode="stdin",
        output_format="text",
    )
    adapter = CustomCliAdapter(config)
    task = _task("att_4")
    task.timeout_seconds = 1
    result = await adapter.run(task, env=None, data_path=tmp_path)
    assert result.status == "timeout"
