"""Shared AgentAdapter implementation for every configured ACP registry agent."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

import rfc8785

from ...adapters.base import AdapterCapabilities, AdapterResult, AdapterRunInput, build_security_meta
from ...conversation.plan import effective_conversation
from ...conversation.turns import render_turn_prompt
from ..launch import RenderedLaunchPlan
from ..manifest import AgentManifestStore
from ..prompt import render_task_prompt
from ..secrets import redact_value
from ..transports.adapter import _stage_uploaded_files, _write_parser_outputs
from .client import AcpClient, AcpClientError
from .parser import AcpParser


class AcpTransportAdapter:
    def __init__(self, *, settings: Any, spec: Any, model: str | None = None) -> None:
        self.settings = settings
        self.spec = spec
        self.model = model
        self.config = settings.agents.acp[spec.id]
        self.capabilities = AdapterCapabilities(
            execution_locus="host",
            network_required="public_internet",
            system_requires=(self.config.command[0],),
            interaction_answer=True,
        )

    async def run(self, task: AdapterRunInput, env: Any, data_path: Path) -> AdapterResult:
        started = time.monotonic()
        missing_env = [name for name in self.config.env_from if not os.environ.get(name)]
        if missing_env:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="agent_auth_missing",
                error_message=(
                    "ACP agent requires environment variable(s): " + ", ".join(missing_env)
                ),
            )
        forwarded_env = {name: os.environ[name] for name in self.config.env_from}
        attempt_dir = Path(data_path) / "attempts" / task.attempt_id
        workspace = attempt_dir / "skill_workspace"
        control = attempt_dir / ".agent-control"
        private_root = attempt_dir / ".agent-runtime"
        private_home = private_root / "home"
        workspace.mkdir(parents=True, exist_ok=True)
        control.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_home.mkdir(exist_ok=True, mode=0o700)
        private_env = {
            **self.config.env,
            **forwarded_env,
            "HOME": str(private_home),
            "XDG_CONFIG_HOME": str(private_home / ".config"),
            "XDG_CACHE_HOME": str(private_home / ".cache"),
            "XDG_DATA_HOME": str(private_home / ".local" / "share"),
            "XDG_STATE_HOME": str(private_home / ".local" / "state"),
        }
        manifest = AgentManifestStore(control / "agent-manifest.json")
        secret_values = tuple(
            value
            for value in (
                task.session_token,
                *self.config.env.values(),
                *forwarded_env.values(),
            )
            if isinstance(value, str) and value
        )
        _stage_uploaded_files(task, workspace, env=env, data_path=Path(data_path))

        conversation = effective_conversation(task)
        base_prompt = render_task_prompt(task)
        prompts = [
            render_turn_prompt(task, turn, base_prompt=base_prompt.text)
            for turn in conversation.send_message_turns
        ]
        plan = self._launch_plan(workspace, base_prompt.content_hash)
        entry = self.config.registry_entry or {}
        path_aliases = {
            workspace: "skill_workspace",
            control: "attempt_control",
            private_home: "attempt_home",
        }
        executable = Path(self.config.command[0])
        if executable.is_absolute():
            path_aliases[executable] = "agent_executable"
        manifest.prepare(
            attempt_id=task.attempt_id,
            spec=self.spec,
            plan=plan,
            agent_version=entry.get("version"),
            requested_model=self.model,
            provider=None,
            components={"transport": "acp-v1@1", "driver": "acp-session@1", "parser": AcpParser.version},
            config_summary={
                "prompt_hash": base_prompt.content_hash,
                "registry_url": self.config.registry_url,
                "registry_sha256": self.config.registry_sha256,
                "registry_entry": entry,
                "distribution": entry.get("distribution"),
                "data_boundary": "local subprocess; agent network access is implementation-defined",
            },
            path_aliases=path_aliases,
            secrets=secret_values,
        )

        client = AcpClient(
            self.config.command,
            cwd=workspace,
            env=private_env,
            permission_answers=self.config.permission_answers,
        )
        try:
            run_result = await client.run(
                prompts,
                mcp_servers=tuple(_mcp_server(server) for server in task.mcp_servers),
                timeout_seconds=task.timeout_seconds,
            )
            parsed = AcpParser().parse(run_result)
            transcript_path = control / "acp-transcript.jsonl"
            _write_transcript(
                transcript_path,
                run_result.transcript,
                secrets=secret_values,
                path_aliases=path_aliases,
            )
            permission_failed = run_result.permission_unanswered
            status = "cli_error" if permission_failed else "completed"
            error_code = "agent_permission_required" if permission_failed else None
            error_message = (
                "ACP agent requested permission without a configured answer" if permission_failed else None
            )
            _write_parser_outputs(attempt_dir, parsed)
            manifest.finalize(
                effective_model=None,
                effective_model_known=False,
                coverage=parsed.coverage,
                cleanup={"status": "confirmed"},
                outcome={"status": status, "error_code": error_code},
                sessions=({"session_id": parsed.session_id},) if parsed.session_id else (),
                degradations=tuple(item.message for item in parsed.diagnostics),
                path_aliases=path_aliases,
                secrets=secret_values,
            )
            return AdapterResult(
                attempt_id=task.attempt_id,
                status=status,
                external_refs={
                    "agent_manifest": str(manifest.path),
                    "coverage": dict(parsed.coverage),
                    "spec_hash": self.spec.spec_hash,
                    "plan_hash": plan.plan_hash,
                    "registry_sha256": self.config.registry_sha256,
                    "acp_transcript": str(transcript_path),
                },
                error_code=error_code,
                error_message=error_message,
                events_count=len(parsed.events),
                thinking_count=len(parsed.thinking),
                token_usage={k: v for k, v in (parsed.usage or {}).items() if isinstance(v, int)},
                duration_ms=int((time.monotonic() - started) * 1000),
                security_meta=self._security_meta(workspace),
            )
        except AcpClientError as exc:
            manifest.finalize(
                effective_model=None,
                effective_model_known=False,
                coverage={},
                cleanup={"status": "confirmed"},
                outcome={"status": "timeout" if exc.code == "agent_timeout" else "cli_error", "error_code": exc.code},
                degradations=(str(exc),),
                path_aliases=path_aliases,
                secrets=secret_values,
            )
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="timeout" if exc.code == "agent_timeout" else "cli_error",
                external_refs={"agent_manifest": str(manifest.path), "registry_sha256": self.config.registry_sha256},
                error_code=exc.code,
                error_message=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
                security_meta=self._security_meta(workspace),
            )
        finally:
            shutil.rmtree(private_root, ignore_errors=True)

    def _launch_plan(self, workspace: Path, prompt_hash: str) -> RenderedLaunchPlan:
        logical = {
            "transport": "acp-v1",
            "agent": self.spec.id,
            "spec_hash": self.spec.spec_hash,
            "registry_sha256": self.config.registry_sha256,
            "command": self.config.command,
            "prompt_hash": prompt_hash,
        }
        plan_hash = f"sha256:{hashlib.sha256(rfc8785.dumps(logical)).hexdigest()}"
        return RenderedLaunchPlan(
            argv=tuple(self.config.command),
            cwd=workspace,
            env=self.config.env,
            env_redacted={key: "***" for key in self.config.env},
            stdin_data=None,
            prompt_mode="driver-owned",
            plan_hash=plan_hash,
        )

    @staticmethod
    def _security_meta(workspace: Path) -> dict[str, Any]:
        return build_security_meta(
            execution_locus="host",
            permission_mode="explicit-acp-options",
            workspace_root=str(workspace),
        )


def _mcp_server(server: Any) -> Mapping[str, Any]:
    command = Path(server.command)
    if not command.is_absolute():
        command = Path(server.cwd or ".").resolve() / command
    return {
        "name": server.name,
        "command": str(command.resolve()),
        "args": list(server.args),
        "env": [{"name": key, "value": value} for key, value in sorted(server.env.items())],
    }


def _write_transcript(
    path: Path,
    transcript: tuple[Mapping[str, Any], ...],
    *,
    secrets: tuple[str, ...],
    path_aliases: Mapping[Path, str] | None = None,
) -> None:
    """Persist owner-only, structurally valid and redacted JSONL evidence."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        for envelope in transcript:
            sanitized = redact_value(
                _replace_paths(dict(envelope), path_aliases or {}),
                secrets,
            )
            stream.write(json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")


def _replace_paths(value: Any, aliases: Mapping[Path, str]) -> Any:
    if isinstance(value, str):
        rendered = value
        ordered = sorted(
            ((str(path), f"<{label}>") for path, label in aliases.items()),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for raw, replacement in ordered:
            rendered = rendered.replace(raw, replacement)
        return rendered
    if isinstance(value, Mapping):
        return {str(key): _replace_paths(item, aliases) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_replace_paths(item, aliases) for item in value)
    if isinstance(value, list):
        return [_replace_paths(item, aliases) for item in value]
    return value


def build_acp_adapter(*, settings: Any, spec: Any, model: str | None = None) -> AcpTransportAdapter:
    return AcpTransportAdapter(settings=settings, spec=spec, model=model)
