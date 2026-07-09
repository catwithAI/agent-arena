"""ClaudeCodeAdapter — drives Claude Code via the `claude` CLI subprocess.

Flow:
1. Write a temporary MCP config file (env vars carry attempt_id / session
   token / base_url so the env's MCP server can call back into the attempt
   server).
2. Spawn `claude -p "{prompt}" --output-format stream-json --verbose
   --mcp-config {config}`.
3. Parse stdout JSONL line by line, collecting thinking blocks / usage /
   events as they arrive.

stdout stream-json shapes:
- type=system subtype=init: session bootstrap info.
- type=assistant: one LLM turn; message.content[] holds text/thinking/
  tool_use/tool_result blocks.
- type=result: final summary, including usage / total_cost_usd / num_turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..model_providers import ModelProviderSection, parse_model_ref, resolve_api_key
from .base import AdapterResult, AdapterRunInput, build_security_meta, prompt_context
from .token_usage import (
    estimate_tokens_from_event,
    result_usage_tokens,
    usage_input_tokens,
    usage_output_tokens,
)

logger = logging.getLogger(__name__)


class ClaudeCodeAdapter:
    def __init__(
        self,
        *,
        model: str = "sonnet",
        max_budget_usd: float = 5.0,
        project_path: str | Path = ".",
        providers: dict[str, ModelProviderSection] | None = None,
    ) -> None:
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.project_path = str(Path(project_path).resolve())
        self.providers = providers or {}

    async def run(self, task: AdapterRunInput, env: Any, data_path: Path) -> AdapterResult:
        data_path = Path(data_path)
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"

        cli_path = shutil.which("claude")
        if not cli_path:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="claude_not_in_path",
                error_message="claude CLI not found in PATH",
            )

        mcp_config_path = self._write_mcp_config(task, attempt_dir)
        prompt = self._render_prompt(task)
        # Provider-prefixed models point the subprocess at a third-party
        # endpoint via its own env, leaving global settings.json untouched so
        # concurrent sessions don't interfere with each other. `--model` gets
        # the model name with the provider prefix stripped (the CLI doesn't
        # understand "<provider>/" prefixes).
        #
        # Auth uses ANTHROPIC_AUTH_TOKEN (sent as `Authorization: Bearer`),
        # not ANTHROPIC_API_KEY (sent as `x-api-key`) — many gateways only
        # accept Bearer, and the two env vars map to different HTTP auth
        # schemes that must not be mixed.
        model_ref = parse_model_ref(self.model, self.providers)
        subprocess_env = {**os.environ}
        if model_ref.provider is not None:
            provider = self.providers[model_ref.provider]
            subprocess_env["ANTHROPIC_BASE_URL"] = provider.base_url
            api_key = resolve_api_key(provider)
            if api_key:
                subprocess_env["ANTHROPIC_AUTH_TOKEN"] = api_key
                subprocess_env.pop("ANTHROPIC_API_KEY", None)
            if provider.custom_headers:
                subprocess_env["ANTHROPIC_CUSTOM_HEADERS"] = provider.custom_headers
        cmd = [
            cli_path,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--mcp-config", str(mcp_config_path.resolve()),
            "--model", model_ref.model,
            "--max-budget-usd", str(self.max_budget_usd),
            "--dangerously-skip-permissions",
        ]

        events_count = 0
        thinking_count = 0
        total_input_tokens = 0
        total_output_tokens = 0
        estimated_input_tokens = 0
        estimated_output_tokens = 0
        last_event_at: str | None = None
        final_result: dict | None = None
        error_message: str | None = None
        model_used: str | None = None
        started_at = datetime.now(timezone.utc)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(attempt_dir),
                limit=10 * 1024 * 1024,
                env=subprocess_env,
            )

            async def _consume() -> None:
                nonlocal events_count, thinking_count, last_event_at
                nonlocal total_input_tokens, total_output_tokens, final_result
                nonlocal estimated_input_tokens, estimated_output_tokens
                nonlocal model_used
                assert proc.stdout is not None
                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    ts = _now_iso()
                    last_event_at = ts

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        _append_jsonl(events_path, {"timestamp": ts, "raw_line": line})
                        events_count += 1
                        continue

                    _append_jsonl(events_path, {"timestamp": ts, **data})
                    events_count += 1
                    est_input, est_output = estimate_tokens_from_event(data)
                    estimated_input_tokens += est_input
                    estimated_output_tokens += est_output

                    msg_type = data.get("type")

                    if msg_type == "system" and data.get("subtype") == "init":
                        model_used = data.get("model") or model_used

                    if msg_type == "assistant":
                        message = data.get("message", {})
                        model_used = message.get("model") or model_used
                        for block in message.get("content", []):
                            if block.get("type") == "thinking":
                                thinking_count += 1
                                _append_jsonl(
                                    thinking_path,
                                    {
                                        "timestamp": ts,
                                        "sequence": thinking_count,
                                        "content": block.get("thinking", ""),
                                        "type": "thinking",
                                    },
                                )
                        usage = message.get("usage", {})
                        total_input_tokens += usage_input_tokens(usage)
                        total_output_tokens += usage_output_tokens(usage)

                    elif msg_type == "result":
                        final_result = data
                        result_input, result_output = result_usage_tokens(data)
                        if result_input:
                            total_input_tokens = result_input
                        if result_output:
                            total_output_tokens = result_output

            try:
                await asyncio.wait_for(_consume(), timeout=task.timeout_seconds)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                error_message = f"timeout after {task.timeout_seconds}s"

            if proc.stderr:
                stderr_bytes = await proc.stderr.read()
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                if stderr_text:
                    (attempt_dir / "stderr.txt").write_text(stderr_text, encoding="utf-8")
                    if not error_message and proc.returncode and proc.returncode != 0:
                        error_message = stderr_text[:500]

        except FileNotFoundError:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="claude_exec_failed",
                error_message="failed to execute claude CLI",
            )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="unexpected_error",
                error_message=str(exc),
            )

        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        status = _classify_outcome(proc.returncode, final_result, error_message)
        token_usage_estimated = False
        if total_input_tokens == 0 and total_output_tokens == 0 and (
            estimated_input_tokens or estimated_output_tokens
        ):
            total_input_tokens = estimated_input_tokens
            total_output_tokens = estimated_output_tokens
            token_usage_estimated = True

        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                "session_id": final_result.get("session_id") if final_result else None,
                "cli_path": cli_path,
                "token_usage_estimated": token_usage_estimated,
                "model_used": model_used or self.model,
            },
            error_code=None if status == "completed" else (error_message or "cli_error"),
            error_message=error_message,
            events_count=events_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            token_usage={"input_tokens": total_input_tokens, "output_tokens": total_output_tokens},
            duration_ms=duration_ms,
            security_meta=build_security_meta(
                execution_locus="host",
                permission_mode="--dangerously-skip-permissions",
                workspace_root=str(attempt_dir.resolve()),
            ),
        )

    def _write_mcp_config(self, task: AdapterRunInput, attempt_dir: Path) -> Path:
        config = {
            "mcpServers": {
                f"lane-{task.env_name}": {
                    "command": "uv",
                    "args": [
                        "run", "--project", self.project_path,
                        "python",
                        f"{self.project_path}/envs/{task.env_name}/mcp_server.py",
                    ],
                    "env": {
                        "LANE_ATTEMPT_ID": task.attempt_id,
                        "LANE_SESSION_TOKEN": task.session_token,
                        "LANE_BASE_URL": task.env_base_url,
                    },
                }
            }
        }
        path = attempt_dir / "mcp_config.json"
        path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def _render_prompt(self, task: AdapterRunInput) -> str:
        parts = [
            f"You are completing an agent-lane benchmark task. Use the tools "
            f"exposed by the `lane-{task.env_name}` MCP server to complete it.",
            "",
            "Task:",
            task.task_prompt,
        ]
        context = prompt_context(task.task_context) if task.task_context else {}
        if context:
            parts.append("")
            parts.append("Context:")
            parts.append(json.dumps(context, ensure_ascii=False, indent=2))
        return "\n".join(parts)


def _classify_outcome(
    returncode: int | None, final_result: dict | None, error_message: str | None
) -> str:
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    if returncode is None:
        return "cli_error"
    if final_result:
        subtype = final_result.get("subtype", "")
        if "budget" in subtype:
            return "timeout"
        if subtype == "success" and not final_result.get("is_error"):
            return "completed"
        if final_result.get("is_error"):
            return "cli_error"
    if returncode != 0:
        return "cli_error"
    return "completed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
