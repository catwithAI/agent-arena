"""CustomCliAdapter — plug in any CLI-based agent without writing Python.

`ClaudeCodeAdapter` and `CodexAdapter` are reference implementations, but the
whole point of agent-arena is that you shouldn't need one of those per agent.
`CustomCliAdapter` runs any command line, feeds it a prompt (via stdin, an
argv template, or a temp file — configurable), and parses its stdout as
either plain text or JSONL, extracting whatever fields you point it at.

Minimal example (`arena.yaml`):

    custom_agents:
      my-agent:
        command: ["my-agent-cli", "--prompt-file", "{prompt_file}"]
        prompt_mode: file          # stdin | file | arg
        output_format: text        # text | jsonl
        mcp_config:
          # optional: if your agent supports MCP, name the file/flag it reads
          flag: "--mcp-config"
          file: true

For agents that emit structured events, set `output_format: jsonl` and
(optionally) `jsonl_fields` to map your schema's usage/thinking keys onto the
ones agent-arena understands — see `JsonlFieldMap` below. Agents that only
print a final answer to stdout can be left as `output_format: text`; you'll
still get pass/fail scoring, just no per-turn trace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .base import AdapterResult, AdapterRunInput, build_security_meta, prompt_context
from .token_usage import usage_input_tokens, usage_output_tokens

logger = logging.getLogger(__name__)

PromptMode = Literal["stdin", "file", "arg"]
OutputFormat = Literal["text", "jsonl"]


class JsonlFieldMap(BaseModel):
    """Where to find usage/thinking info inside each JSONL line, using
    dotted paths (e.g. "message.usage.input_tokens")."""

    thinking_type_value: str = "thinking"
    type_field: str = "type"
    text_field: str = "text"
    usage_field: str = "usage"


class CustomCliConfig(BaseModel):
    name: str
    command: list[str]
    prompt_mode: PromptMode = "arg"
    output_format: OutputFormat = "text"
    env: dict[str, str] = Field(default_factory=dict)
    jsonl_fields: JsonlFieldMap = Field(default_factory=JsonlFieldMap)
    mcp_config_flag: str | None = None
    mcp_config_is_file: bool = True


class CustomCliAdapter:
    """Generic adapter driven entirely by `CustomCliConfig` — no subclassing
    needed to onboard a new agent."""

    def __init__(self, config: CustomCliConfig, *, project_path: str | Path = ".") -> None:
        self.config = config
        self.project_path = str(Path(project_path).resolve())

    async def run(self, task: AdapterRunInput, env: Any, data_path: Path) -> AdapterResult:
        cfg = self.config
        data_path = Path(data_path)
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"

        exe = cfg.command[0]
        cli_path = shutil.which(exe) or (exe if Path(exe).exists() else None)
        if not cli_path:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code=f"{cfg.name}_not_in_path",
                error_message=f"'{exe}' not found in PATH",
            )

        prompt = self._render_prompt(task)
        prompt_file: Path | None = None
        stdin_data: bytes | None = None
        argv = list(cfg.command)

        if cfg.prompt_mode == "file":
            prompt_file = attempt_dir / "prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            argv = [a.format(prompt_file=str(prompt_file.resolve())) for a in argv]
        elif cfg.prompt_mode == "arg":
            argv = [a.format(prompt=prompt) for a in argv]
        elif cfg.prompt_mode == "stdin":
            stdin_data = prompt.encode("utf-8")

        mcp_config_path = self._write_mcp_config(task, attempt_dir)
        if cfg.mcp_config_flag and mcp_config_path is not None:
            argv += [cfg.mcp_config_flag, str(mcp_config_path.resolve())]

        subprocess_env = {**os.environ, **cfg.env}

        events_count = 0
        thinking_count = 0
        total_input_tokens = 0
        total_output_tokens = 0
        last_event_at: str | None = None
        error_message: str | None = None
        started_at = datetime.now(timezone.utc)
        proc = None
        final_text_lines: list[str] = []

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(attempt_dir),
                limit=10 * 1024 * 1024,
                env=subprocess_env,
            )

            async def _consume() -> None:
                nonlocal events_count, thinking_count, last_event_at
                nonlocal total_input_tokens, total_output_tokens
                assert proc.stdout is not None
                if stdin_data is not None:
                    assert proc.stdin is not None
                    proc.stdin.write(stdin_data)
                    proc.stdin.close()
                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                    if not line.strip():
                        continue
                    ts = _now_iso()
                    last_event_at = ts

                    if cfg.output_format == "text":
                        final_text_lines.append(line)
                        _append_jsonl(events_path, {"timestamp": ts, "raw_line": line})
                        events_count += 1
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        _append_jsonl(events_path, {"timestamp": ts, "raw_line": line})
                        events_count += 1
                        continue

                    _append_jsonl(events_path, {"timestamp": ts, **data})
                    events_count += 1

                    fm = cfg.jsonl_fields
                    if _get_path(data, fm.type_field) == fm.thinking_type_value:
                        thinking_count += 1
                        _append_jsonl(
                            thinking_path,
                            {
                                "timestamp": ts,
                                "sequence": thinking_count,
                                "content": _get_path(data, fm.text_field) or "",
                                "type": "thinking",
                            },
                        )
                    usage = _get_path(data, fm.usage_field)
                    if isinstance(usage, dict):
                        total_input_tokens += usage_input_tokens(usage)
                        total_output_tokens += usage_output_tokens(usage)

            try:
                await asyncio.wait_for(_consume(), timeout=task.timeout_seconds)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                error_message = f"timeout after {task.timeout_seconds}s"

            if proc.stderr:
                stderr = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
                if stderr:
                    (attempt_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
                    if not error_message and proc.returncode and proc.returncode != 0:
                        error_message = stderr[:500]

        except FileNotFoundError:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code=f"{cfg.name}_exec_failed",
                error_message=f"failed to execute '{exe}'",
            )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="unexpected_error",
                error_message=str(exc),
            )

        if cfg.output_format == "text" and final_text_lines:
            (attempt_dir / f"{cfg.name}_final.txt").write_text(
                "\n".join(final_text_lines), encoding="utf-8"
            )

        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        status = _classify_outcome(proc.returncode if proc else None, error_message)
        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={"cli_path": cli_path},
            error_code=None if status == "completed" else (error_message or "cli_error"),
            error_message=error_message,
            events_count=events_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            token_usage={"input_tokens": total_input_tokens, "output_tokens": total_output_tokens},
            duration_ms=duration_ms,
            security_meta=build_security_meta(
                execution_locus="host",
                permission_mode=None,
                workspace_root=str(attempt_dir.resolve()),
            ),
        )

    def _write_mcp_config(self, task: AdapterRunInput, attempt_dir: Path) -> Path | None:
        if not self.config.mcp_config_flag:
            return None
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
            f"You are completing an agent-arena benchmark task. If your tools "
            f"support MCP, use the `lane-{task.env_name}` server to complete it.",
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


def _classify_outcome(returncode: int | None, error_message: str | None) -> str:
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    if returncode is None:
        return "cli_error"
    return "completed" if returncode == 0 else "cli_error"


def _get_path(data: dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
