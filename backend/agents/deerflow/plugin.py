"""Attempt-scoped DeerFlow adapter built on the shared local CLI runtime."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any

import rfc8785

from ...adapters.base import (
    AdapterCapabilities,
    AdapterResult,
    AdapterRunInput,
    build_security_meta,
)
from ..errors import classify_runtime_result
from ..launch import RenderedLaunchPlan
from ..manifest import AgentManifestStore
from ..parsers import EvidenceSet, ParseDiagnostic, ParseResult
from ..prompt import render_task_prompt
from ..runtime import LocalCliRuntime
from ..transports.adapter import (
    _agent_options,
    _isolated_base_env,
    _stage_uploaded_files,
    _write_parser_outputs,
)
from . import DEERFLOW_REVISION, DEERFLOW_VERSION, RUNNER_VERSION
from .config import DeerFlowPrivateConfig, build_private_config
from .parser import DeerFlowParser


class DeerFlowAdapter:
    def __init__(self, *, spec: Any, settings: Any, model: str | None = None) -> None:
        self.spec = spec
        self.settings = settings
        self.model = model
        self.capabilities = AdapterCapabilities(
            execution_locus=spec.isolation.execution_locus,
            network_required=spec.isolation.network_required,
            system_requires=spec.isolation.system_requires,
            interaction_answer=False,
        )

    async def run(self, task: AdapterRunInput, env: Any, data_path: Path) -> AdapterResult:
        attempt = Path(data_path) / "attempts" / task.attempt_id
        workspace = attempt / "skill_workspace"
        private = attempt / ".agent-runtime"
        control = attempt / ".agent-control"
        workspace.mkdir(parents=True, exist_ok=True)
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        control.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest = AgentManifestStore(control / "agent-manifest.json")
        secret_values: tuple[str, ...] = ()

        try:
            self._validate_request(task)
            _stage_uploaded_files(task, workspace, env=env, data_path=Path(data_path))
            prompt = render_task_prompt(task)
            prompt_text = (
                prompt.text + "\n\nDeerFlow execution note: place every deliverable in "
                "/mnt/arena-workspace. Files elsewhere are private runtime state and will "
                "not be submitted."
            )
            config = build_private_config(
                private_root=private / "deerflow",
                requested_model=self.model,
                providers=self.settings.model_providers,
                raw_options=dict(_agent_options(task, self.spec.id)),
                workspace=workspace,
                attempt_root=attempt,
            )
            summary_path = control / "runtime" / "deerflow-summary.json"
            plan = _launch_plan(
                spec_hash=self.spec.spec_hash,
                prompt=prompt_text,
                attempt_id=task.attempt_id,
                config=config,
                summary_path=summary_path,
                requested_model=self.model,
            )
            secret_values = tuple(
                dict.fromkeys(
                    [
                        *config.child_env.values(),
                        task.session_token,
                        *task.wire_injection.process_env.values(),
                        task.wire_injection.capture_token or "",
                    ]
                )
            )
            secret_values = tuple(value for value in secret_values if value)
            manifest.prepare(
                attempt_id=task.attempt_id,
                spec=self.spec,
                plan=plan,
                agent_version=DEERFLOW_VERSION,
                requested_model=self.model,
                provider=config.summary["provider"],
                components={
                    "runtime": "local-cli@1",
                    "runner": f"deerflow-arena-runner@{RUNNER_VERSION}",
                    "parser": f"{DeerFlowParser.parser_id}@{DeerFlowParser.parser_version}",
                    "deerflow": f"deerflow-harness@{DEERFLOW_VERSION}",
                },
                config_summary={
                    **config.summary,
                    "revision": DEERFLOW_REVISION,
                    "prompt_hash": (
                        "sha256:" + hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
                    ),
                },
                path_aliases=_path_aliases(workspace, private, control),
                secrets=secret_values,
            )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="agent_launch_plan_invalid",
                error_message=str(exc),
                security_meta=self._security_meta(workspace, False),
            )

        try:
            runtime_result = await LocalCliRuntime().run(
                plan,
                evidence_dir=control / "runtime",
                timeout_seconds=task.timeout_seconds,
                base_env=_isolated_base_env(self.spec, private / "deerflow"),
                redact_secrets=secret_values,
            )
        except asyncio.CancelledError:
            manifest.finalize(
                effective_model=self.model,
                effective_model_known=True,
                coverage={},
                cleanup={"status": "cancelled_runtime_cleanup"},
                outcome={"status": "cancelled", "error_code": "agent_cancelled"},
                degradations=("attempt cancelled before parser completion",),
                path_aliases=_path_aliases(workspace, private, control),
                secrets=secret_values,
            )
            raise
        except Exception as exc:
            manifest.finalize(
                effective_model=None,
                effective_model_known=False,
                coverage={},
                cleanup={"status": "unknown"},
                outcome={"status": "cli_error", "error_code": "agent_internal_error"},
                degradations=("runtime raised before producing a terminal result",),
                secrets=secret_values,
            )
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="agent_internal_error",
                error_message=str(exc),
                security_meta=self._security_meta(workspace, config.options.allow_host_bash),
            )

        parse_result = await _parse(control)
        classification = classify_runtime_result(
            runtime_result,
            failure_patterns=self.spec.failure_patterns,
            secrets=secret_values,
            parse_degraded=parse_result.degraded,
        )
        adapter_status = {
            "completed": "completed",
            "failed": "cli_error",
            "timeout": "timeout",
        }[runtime_result.status]
        _write_parser_outputs(attempt, parse_result)
        manifest.finalize(
            effective_model=self.model,
            effective_model_known=True,
            coverage=parse_result.coverage,
            cleanup={"status": runtime_result.cleanup},
            outcome={
                "status": adapter_status,
                "returncode": runtime_result.returncode,
                "error_code": classification.error_code,
                "parse_degraded": classification.parse_degraded,
            },
            sessions=(
                ({"turn_id": f"{task.task_id}::t0", "session_id": parse_result.session_id},)
                if parse_result.session_id
                else ()
            ),
            degradations=tuple(item.message for item in parse_result.diagnostics),
            path_aliases=_path_aliases(workspace, private, control),
            secrets=secret_values,
        )
        usage = {
            key: value
            for key, value in (parse_result.usage or {}).items()
            if isinstance(value, int)
        }
        return AdapterResult(
            attempt_id=task.attempt_id,
            status=adapter_status,
            external_refs={
                "agent_manifest": str(manifest.path),
                "coverage": dict(parse_result.coverage),
                "spec_hash": self.spec.spec_hash,
                "plan_hash": plan.plan_hash,
                "deerflow_revision": DEERFLOW_REVISION,
            },
            error_code=(classification.error_code if adapter_status != "completed" else None),
            error_message=(classification.diagnostic if adapter_status != "completed" else None),
            events_count=len(parse_result.events),
            thinking_count=len(parse_result.thinking),
            token_usage=usage,
            duration_ms=runtime_result.duration_ms,
            security_meta=self._security_meta(workspace, config.options.allow_host_bash),
        )

    def _validate_request(self, task: AdapterRunInput) -> None:
        if task.mcp_servers:
            raise ValueError("DeerFlow Lane MCP integration is not supported")
        if task.wire_injection.enabled:
            raise ValueError("DeerFlow wire injection is not supported")
        if task.conversation_turns:
            raise ValueError("DeerFlow multi-turn execution is not supported")

    def _security_meta(self, workspace: Path, allow_host_bash: bool) -> dict[str, Any]:
        permission = self.spec.isolation.permission_mode
        if allow_host_bash:
            permission = f"{permission or 'workspace-write'}+host-bash"
        return build_security_meta(
            execution_locus=self.spec.isolation.execution_locus,
            permission_mode=permission,
            workspace_root=str(workspace.resolve()),
        )


def build_deerflow_adapter(
    *, spec: Any, settings: Any, model: str | None = None, **_: Any
) -> DeerFlowAdapter:
    return DeerFlowAdapter(spec=spec, settings=settings, model=model)


def _launch_plan(
    *,
    spec_hash: str,
    prompt: str,
    attempt_id: str,
    config: DeerFlowPrivateConfig,
    summary_path: Path,
    requested_model: str | None,
) -> RenderedLaunchPlan:
    argv = [
        sys.executable,
        "-m",
        "backend.agents.deerflow.runner",
        "--config",
        str(config.config_path.resolve()),
        "--summary",
        str(summary_path.resolve()),
        "--thread-id",
        f"arena-{attempt_id}",
        "--recursion-limit",
        str(config.options.recursion_limit),
    ]
    if config.options.subagent:
        argv.append("--subagent")
    if not config.options.thinking:
        argv.append("--no-thinking")
    if config.options.plan_mode:
        argv.append("--plan-mode")
    logical = {
        "schema_version": "1",
        "agent_id": "deerflow",
        "spec_hash": spec_hash,
        "runner_version": RUNNER_VERSION,
        "requested_model": requested_model,
        "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
        "options": config.options.model_dump(),
    }
    plan_hash = f"sha256:{hashlib.sha256(rfc8785.dumps(logical)).hexdigest()}"
    child_env = {
        **config.child_env,
        "DEER_FLOW_PROJECT_ROOT": str(config.project_dir.resolve()),
        "DEER_FLOW_HOME": str(config.home_dir.resolve()),
        "DEER_FLOW_CONFIG_PATH": str(config.config_path.resolve()),
        "DEERFLOW_ARENA_WORKSPACE": str(config.summary["workspace"] or ""),
    }
    redacted = {name: value for name, value in child_env.items()}
    for name in config.child_env:
        redacted[name] = "***"
    return RenderedLaunchPlan(
        argv=tuple(argv),
        cwd=config.project_dir.resolve(),
        env=child_env,
        env_redacted=redacted,
        stdin_data=prompt.encode("utf-8"),
        prompt_mode="stdin",
        plan_hash=plan_hash,
    )


async def _parse(control: Path) -> ParseResult:
    try:
        return await DeerFlowParser().parse(EvidenceSet.from_runtime_dir(control / "runtime"))
    except Exception as exc:
        return ParseResult(
            final_text=None,
            coverage={"final_text": "unknown", "structured_events": "degraded"},
            diagnostics=(ParseDiagnostic("parser_crashed", f"{type(exc).__name__}: {exc}"),),
            degraded=True,
        )


def _path_aliases(workspace: Path, private: Path, control: Path) -> dict[Path, str]:
    return {
        workspace: "skill_workspace",
        private: "attempt_private",
        control: "attempt_control",
    }
