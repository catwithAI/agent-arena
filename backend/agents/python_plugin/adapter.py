"""Safety and manifest wrapper around trusted in-process Python plugins."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import time
from pathlib import Path
from typing import Any, Mapping

import rfc8785

from ...adapters.base import AdapterCapabilities, AdapterResult, AdapterRunInput, build_security_meta
from ...conversation.plan import effective_conversation
from ..launch import RenderedLaunchPlan
from ..manifest import AgentManifestStore
from ..mcp import resolve_mcp_servers
from ..prompt import render_task_prompt
from ..secrets import redact_text, redact_value
from ..transports.adapter import _stage_uploaded_files
from .contract import PythonAgentContext, PythonAgentOutput


class PythonPluginAdapter:
    def __init__(self, *, settings: Any, spec: Any, model: str | None = None) -> None:
        self.settings = settings
        self.spec = spec
        self.model = model
        self.config = settings.agents.python_plugins[spec.id]
        self.plugin = _load_plugin(self.config.entrypoint)
        self.capabilities = AdapterCapabilities(
            execution_locus="host",
            network_required="public_internet",
            interaction_answer=False,
        )

    async def run(self, task: AdapterRunInput, env: Any, data_path: Path) -> AdapterResult:
        started = time.monotonic()
        attempt_dir = Path(data_path) / "attempts" / task.attempt_id
        workspace = attempt_dir / "skill_workspace"
        control = attempt_dir / ".agent-control"
        workspace.mkdir(parents=True, exist_ok=True)
        control.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest = AgentManifestStore(control / "agent-manifest.json")
        secrets = _task_secrets(task)
        try:
            conversation = effective_conversation(task)
            if len(conversation.send_message_turns) != 1 or conversation.interaction_turns:
                raise ValueError("Python plugin template currently supports one message turn")
            _stage_uploaded_files(task, workspace, env=env, data_path=Path(data_path))
            mcp_servers = resolve_mcp_servers(task)
            if mcp_servers and not self.config.supports_mcp:
                raise ValueError("Python plugin did not declare MCP support")
            prompt = render_task_prompt(task)
            plan = self._logical_plan(workspace, prompt.content_hash, mcp_servers)
            manifest.prepare(
                attempt_id=task.attempt_id,
                spec=self.spec,
                plan=plan,
                agent_version=self.config.package_version,
                requested_model=self.model,
                provider=None,
                components={
                    "runtime": "trusted-python-plugin@1",
                    "driver": "python-plugin-oneshot@1",
                    "parser": "python-plugin-output@1",
                },
                config_summary={
                    "entrypoint": self.config.entrypoint,
                    "prompt_hash": prompt.content_hash,
                    "mcp": [server.redacted_shape() for server in mcp_servers],
                },
                path_aliases={workspace: "skill_workspace", control: "attempt_control"},
                secrets=secrets,
            )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="agent_launch_plan_invalid",
                error_message=redact_text(str(exc), secrets),
                security_meta=self._security_meta(workspace),
            )

        context = PythonAgentContext(
            attempt_id=task.attempt_id,
            prompt=prompt.text,
            workspace=workspace.resolve(),
            model=self.model,
            mcp_servers=mcp_servers,
        )
        try:
            raw = self.plugin.run(context)
            if inspect.isawaitable(raw):
                raw = await raw
            output = _validate_output(raw, workspace=workspace)
        except Exception as exc:
            message = redact_text(f"Python plugin failed: {type(exc).__name__}: {exc}", secrets)
            manifest.finalize(
                effective_model=None,
                effective_model_known=False,
                coverage={},
                cleanup={"status": "in_process_returned"},
                outcome={"status": "cli_error", "error_code": "agent_internal_error"},
                degradations=(message,),
                secrets=secrets,
            )
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                external_refs={"agent_manifest": str(manifest.path)},
                error_code="agent_internal_error",
                error_message=message,
                duration_ms=int((time.monotonic() - started) * 1000),
                security_meta=self._security_meta(workspace),
            )

        sanitized = _sanitize_output(output, secrets)
        _write_outputs(attempt_dir, sanitized)
        status = "completed" if sanitized.status == "completed" else "cli_error"
        error_code = None if status == "completed" else "agent_nonzero_exit"
        coverage = {
            "final_text": "verified" if sanitized.final_text is not None else "unknown",
            "structured_events": "verified" if sanitized.events else "unknown",
            "thinking": "verified" if sanitized.thinking else "unknown",
            "token_usage": "verified" if sanitized.usage else "unknown",
            "artifacts": "verified" if sanitized.artifacts else "unknown",
            "wire": "unsupported",
        }
        manifest.finalize(
            effective_model=sanitized.effective_model,
            effective_model_known=sanitized.effective_model is not None,
            coverage=coverage,
            cleanup={"status": "in_process_returned"},
            outcome={"status": status, "error_code": error_code},
            sessions=(
                ({"session_id": sanitized.session_id},) if sanitized.session_id else ()
            ),
            secrets=secrets,
        )
        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                "agent_manifest": str(manifest.path),
                "spec_hash": self.spec.spec_hash,
                "plan_hash": plan.plan_hash,
                "artifacts": list(sanitized.artifacts),
            },
            error_code=error_code,
            error_message="Python plugin reported failure" if error_code else None,
            events_count=len(sanitized.events),
            thinking_count=len(sanitized.thinking),
            token_usage=dict(sanitized.usage),
            duration_ms=int((time.monotonic() - started) * 1000),
            security_meta=self._security_meta(workspace),
        )

    def _logical_plan(self, workspace: Path, prompt_hash: str, mcp_servers) -> RenderedLaunchPlan:
        logical = {
            "agent": self.spec.id,
            "spec_hash": self.spec.spec_hash,
            "entrypoint": self.config.entrypoint,
            "prompt_hash": prompt_hash,
            "model": self.model,
            "mcp": [server.redacted_shape() for server in mcp_servers],
        }
        digest = hashlib.sha256(rfc8785.dumps(logical)).hexdigest()
        return RenderedLaunchPlan(
            argv=("python-plugin", self.config.entrypoint),
            cwd=workspace,
            env={},
            env_redacted={},
            stdin_data=None,
            prompt_mode="driver-owned",
            plan_hash=f"sha256:{digest}",
        )

    @staticmethod
    def _security_meta(workspace: Path) -> dict[str, Any]:
        return build_security_meta(
            execution_locus="host",
            permission_mode="trusted-in-process-plugin",
            workspace_root=str(workspace.resolve()),
        )


def _load_plugin(entrypoint: str) -> Any:
    module_name, attribute_name = entrypoint.split(":", 1)
    module = importlib.import_module(module_name)
    value = getattr(module, attribute_name)
    plugin = value() if inspect.isclass(value) or (callable(value) and not hasattr(value, "run")) else value
    if not callable(getattr(plugin, "run", None)):
        raise TypeError(f"Python plugin {entrypoint!r} must provide run(context)")
    return plugin


def _validate_output(raw: Any, *, workspace: Path) -> PythonAgentOutput:
    if isinstance(raw, Mapping):
        raw = PythonAgentOutput(**raw)
    if not isinstance(raw, PythonAgentOutput):
        raise TypeError("plugin output must be PythonAgentOutput or a compatible mapping")
    if raw.status not in {"completed", "failed"}:
        raise ValueError("plugin status must be completed or failed")
    if len(raw.events) + len(raw.thinking) > 10_000:
        raise ValueError("plugin emitted more than 10000 events")
    encoded = json.dumps([raw.events, raw.thinking], default=str).encode()
    if len(encoded) > 10 * 1024 * 1024:
        raise ValueError("plugin event output exceeded 10 MiB")
    for key, value in raw.usage.items():
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("plugin usage must contain non-negative integer values")
    root = workspace.resolve()
    for relative in raw.artifacts:
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError("plugin artifact references must be relative strings")
        artifact = (root / relative).resolve()
        if not artifact.is_relative_to(root) or not artifact.is_file():
            raise ValueError(f"plugin artifact is outside workspace or missing: {relative!r}")
    return raw


def _sanitize_output(output: PythonAgentOutput, secrets: tuple[str, ...]) -> PythonAgentOutput:
    return PythonAgentOutput(
        status=output.status,
        final_text=redact_text(output.final_text, secrets) if output.final_text else None,
        events=tuple(redact_value(dict(item), secrets) for item in output.events),
        thinking=tuple(redact_value(dict(item), secrets) for item in output.thinking),
        usage=dict(output.usage),
        session_id=redact_text(output.session_id, secrets) if output.session_id else None,
        artifacts=output.artifacts,
        effective_model=output.effective_model,
    )


def _write_outputs(attempt_dir: Path, output: PythonAgentOutput) -> None:
    with (attempt_dir / "events.jsonl").open("w", encoding="utf-8") as file:
        for event in output.events:
            file.write(json.dumps(dict(event), ensure_ascii=False, default=str) + "\n")
    with (attempt_dir / "thinking.jsonl").open("w", encoding="utf-8") as file:
        for event in output.thinking:
            file.write(json.dumps(dict(event), ensure_ascii=False, default=str) + "\n")
    if output.final_text is not None:
        (attempt_dir / "agent_final.txt").write_text(output.final_text, encoding="utf-8")


def _task_secrets(task: AdapterRunInput) -> tuple[str, ...]:
    values = [task.session_token]
    for server in task.mcp_servers:
        values.extend(server.env.values())
    return tuple(dict.fromkeys(value for value in values if value))


def build_python_plugin_adapter(
    *, settings: Any, spec: Any, model: str | None = None
) -> PythonPluginAdapter:
    return PythonPluginAdapter(settings=settings, spec=spec, model=model)
