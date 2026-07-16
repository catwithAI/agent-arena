"""CodexAdapter — drives Codex via the `codex` CLI subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..model_providers import ModelProviderSection, ModelRef, parse_model_ref, resolve_api_key
from ..wire.injection import WireInjection
from .base import (
    AdapterResult,
    AdapterRunInput,
    build_security_meta,
    prompt_context,
    time_budget_notice,
)

logger = logging.getLogger(__name__)


class CodexAdapter:
    def __init__(
        self,
        *,
        model: str = "gpt-5",
        project_path: str | Path = ".",
        providers: dict[str, ModelProviderSection] | None = None,
    ) -> None:
        self.model = model
        self.project_path = str(Path(project_path).resolve())
        self.providers = providers or {}

    @property
    def wire_capture_capabilities(self) -> dict[str, Any]:
        """Wire injection consumption capability declaration (lifecycle uses
        this before the agent starts to filter/drop what this adapter can't
        consume). Codex has no static provider header channel, so
        llm_headers stays False."""
        return {
            "process_env": True,
            "llm_base_url": True,
            "llm_headers": False,
            "mcp_rewrites": True,
        }

    def _provider_cli_args(self, model_ref: ModelRef, injection: WireInjection) -> list[str]:
        """Provider-prefixed models are injected as a one-shot `-c` override
        naming a provider — never touches the user's global config.toml.

        Wire injection consumption point: injection.llm_base_url overrides
        this run's model_providers.<id>.base_url."""
        if model_ref.provider is None:
            return ["-m", model_ref.model]
        p = self.providers[model_ref.provider]
        name = model_ref.provider
        base_url = (
            injection.llm_base_url if injection.enabled and injection.llm_base_url else p.base_url
        )
        args = [
            "-c", f'model_providers.{name}.name="{name}"',
            "-c", f'model_providers.{name}.base_url="{base_url}"',
            "-c", f'model_providers.{name}.wire_api="{p.wire_api}"',
        ]
        if p.api_key_env:
            args += ["-c", f'model_providers.{name}.env_key="{p.api_key_env}"']
        # Capture token goes through Codex's env_http_headers mapping: the
        # X-Lane-Capture-Token header's value is read from the
        # LANE_WIRE_CAPTURE_TOKEN env var at request time, so the token never
        # appears on the command line (-c args).
        if injection.enabled and injection.capture_token:
            args += [
                "-c",
                'model_providers.'
                f'{name}.env_http_headers='
                '{ "X-Lane-Capture-Token" = "LANE_WIRE_CAPTURE_TOKEN" }',
            ]
        args += ["-c", f'model_provider="{name}"', "-m", model_ref.model]
        return args

    async def run(self, task: AdapterRunInput, env: Any, data_path: Path) -> AdapterResult:
        data_path = Path(data_path)
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        # Agent workspace (design: skill_workspace is the agent's sole world
        # boundary). cwd (-C) is set here, so agent submissions land here —
        # the attempt root is reserved for the framework's own runtime
        # metadata (events/thinking/wire/isolated home) and never mixed with
        # agent output. Defensive mkdir in case nothing has staged env
        # materials into it yet.
        workspace = attempt_dir / "skill_workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"
        final_message_path = attempt_dir / "codex_final.txt"

        cli_path = shutil.which("codex")
        if not cli_path:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="codex_not_in_path",
                error_message="codex CLI not found in PATH",
            )

        prompt = self._render_prompt(task)
        model_ref = parse_model_ref(self.model, self.providers)
        cmd = [
            cli_path,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-rules",
            "--dangerously-bypass-approvals-and-sandbox",
            *self._provider_cli_args(model_ref, task.wire_injection),
            "-C", str(workspace.resolve()),
            "-o", str(final_message_path.resolve()),
        ]
        for spec in task.mcp_servers:
            mcp_command, mcp_args = self._mcp_command_and_args(task, spec)
            cmd += [
                "-c", f"mcp_servers.{spec.name}.command="
                f"{json.dumps(mcp_command, ensure_ascii=True)}",
                "-c", f"mcp_servers.{spec.name}.args="
                f"{json.dumps(mcp_args, ensure_ascii=True)}",
            ]
            if spec.cwd:
                cmd += [
                    "-c", f"mcp_servers.{spec.name}.cwd="
                    f"{json.dumps(spec.cwd, ensure_ascii=True)}",
                ]
        cmd.append(prompt)
        if task.mcp_servers:
            self._write_mcp_config_snapshot(task, attempt_dir)

        events_count = 0
        thinking_count = 0
        total_input_tokens = 0
        total_output_tokens = 0
        last_event_at: str | None = None
        error_message: str | None = None
        started_at = datetime.now(timezone.utc)
        proc = None

        # CODEX_HOME isolation: point Codex's config/state at a clean,
        # per-attempt directory instead of the host's global ~/.codex
        # (config.toml, skills, plugins, memories, history), so a benchmark
        # run never picks up whoever's operating this box's private setup.
        # Codex's own built-in tools are not disabled by this.
        iso_codex_home = attempt_dir / ".codex-iso-home"
        iso_codex_home.mkdir(parents=True, exist_ok=True)
        subprocess_env = {
            **os.environ,
            "CODEX_HOME": str(iso_codex_home.resolve()),
        }
        # Codex's MCP config is passed via argv (-c), which is visible to
        # anything reading the process list — attempt credentials must never
        # go there. Only when the scenario actually provides an MCP server
        # do we hand the credentials to the subprocess env, so its stdio MCP
        # child (which Codex spawns inheriting its own env) can read them.
        if task.mcp_servers:
            subprocess_env.update({
                "LANE_ATTEMPT_ID": task.attempt_id,
                "LANE_SESSION_TOKEN": task.session_token,
                "LANE_BASE_URL": task.env_base_url,
            })
        if model_ref.provider is not None:
            provider = self.providers[model_ref.provider]
            api_key = resolve_api_key(provider)
            if provider.api_key_env and api_key:
                subprocess_env[provider.api_key_env] = api_key

        # wire injection consumption point (process_env + capture token);
        # base_url/mcp rewrites are consumed above via _provider_cli_args /
        # _mcp_command_and_args.
        if task.wire_injection.enabled:
            subprocess_env.update(task.wire_injection.process_env)
            if task.wire_injection.capture_token:
                subprocess_env["LANE_WIRE_CAPTURE_TOKEN"] = task.wire_injection.capture_token

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
                limit=10 * 1024 * 1024,
                env=subprocess_env,
            )

            async def _consume() -> None:
                nonlocal events_count, thinking_count, last_event_at
                nonlocal total_input_tokens, total_output_tokens
                assert proc and proc.stdout is not None
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

                    text = _message_text(data)
                    if text and _looks_like_reasoning(data):
                        thinking_count += 1
                        _append_jsonl(
                            thinking_path,
                            {
                                "timestamp": ts,
                                "sequence": thinking_count,
                                "content": text,
                                "type": "thinking",
                            },
                        )
                    usage = _usage(data)
                    total_input_tokens = max(total_input_tokens, usage[0])
                    total_output_tokens = max(total_output_tokens, usage[1])

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
                error_code="codex_exec_failed",
                error_message="failed to execute codex CLI",
            )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="unexpected_error",
                error_message=str(exc),
            )

        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        status = _classify_outcome(proc.returncode if proc else None, error_message)
        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={"cli_path": cli_path, "model_used": self.model},
            error_code=None if status == "completed" else (error_message or "cli_error"),
            error_message=error_message,
            events_count=events_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            token_usage={"input_tokens": total_input_tokens, "output_tokens": total_output_tokens},
            duration_ms=duration_ms,
            security_meta=build_security_meta(
                execution_locus="host",
                permission_mode="--dangerously-bypass-approvals-and-sandbox",
                workspace_root=str(workspace.resolve()),
            ),
        )

    def _mcp_command_and_args(
        self, task: AdapterRunInput, spec: Any
    ) -> tuple[str, list[str]]:
        """Final command/args for a declared MCP server; the wire mcp
        rewrite hook is applied here so it wraps only the server the
        scenario declared."""
        command = spec.command
        args = list(spec.args)
        rewrite = task.wire_injection.mcp_rewrites.get(spec.name)
        if task.wire_injection.enabled and rewrite is not None:
            args = [*rewrite.args_prefix, command, *args]
            command = rewrite.command
        return command, args

    def _write_mcp_config_snapshot(self, task: AdapterRunInput, attempt_dir: Path) -> None:
        servers: dict[str, Any] = {}
        for spec in task.mcp_servers:
            command, args = self._mcp_command_and_args(task, spec)
            server: dict[str, Any] = {
                "command": command,
                "args": args,
                "env": {
                    "LANE_ATTEMPT_ID": task.attempt_id,
                    "LANE_SESSION_TOKEN": task.session_token,
                    "LANE_BASE_URL": task.env_base_url,
                },
            }
            if spec.cwd:
                server["cwd"] = spec.cwd
            servers[spec.name] = server
        config = {"mcp_servers": servers}
        (attempt_dir / "codex_mcp_config.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _render_prompt(self, task: AdapterRunInput) -> str:
        # The adapter does not prescribe a solving method — MCP is only one
        # option among whatever the agent brings natively.
        parts: list[str] = []
        # `codex exec`'s PROMPT argument is the only instructions channel —
        # there's no separate system-prompt slot like Claude Code's
        # --append-system-prompt — so the time budget (a framework-level
        # constraint, not part of the task) is placed at the very top of the
        # message instead. None (unlimited) yields no notice at all.
        notice = time_budget_notice(task.timeout_seconds)
        if notice:
            parts += [notice, ""]
        parts.append(task.task_prompt)
        context = prompt_context(task.task_context) if task.task_context else {}
        if context:
            parts.append("")
            parts.append("Context:")
            parts.append(json.dumps(context, ensure_ascii=False, indent=2))
        return "\n".join(parts)


def _classify_outcome(returncode: int | None, error_message: str | None) -> str:
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    if returncode == 0:
        return "completed"
    return "cli_error"


def _usage(data: dict[str, Any]) -> tuple[int, int]:
    usage = data.get("usage") or data.get("token_usage") or {}
    if isinstance(usage, dict):
        return int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
    return 0, 0


def _message_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "\n".join(filter(None, (_message_text(x) for x in data)))
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("text"), str):
        return data["text"]
    if isinstance(data.get("content"), str):
        return data["content"]
    return "\n".join(filter(None, (_message_text(v) for v in data.values())))


def _looks_like_reasoning(data: dict[str, Any]) -> bool:
    text = json.dumps(data, ensure_ascii=False).lower()
    return "reasoning" in text or "thinking" in text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
