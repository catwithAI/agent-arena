"""End-to-end AgentAdapter bridge for declarative local CLI profiles."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from ...adapters.base import (
    AdapterCapabilities,
    AdapterResult,
    AdapterRunInput,
    build_security_meta,
)
from ..availability import AvailabilityService
from ..drivers.oneshot import OneShotDriver
from ..drivers.command_resume import (
    CommandResumeDriver,
    CommandResumeDriverError,
    CommandResumePlan,
)
from ..errors import classify_runtime_result
from ..launch import RenderedLaunchPlan, render_launch_plan
from ..manifest import AgentManifestStore
from ..mcp import JsonFileDialect, McpDialectError, ResolvedMcpServer, resolve_mcp_servers
from ..models import AgentSpec
from ..parsers import (
    EvidenceSet,
    JsonlMappingParser,
    ParseDiagnostic,
    ParseResult,
    TextParser,
)
from ..runtime import LocalCliRuntime


class ProfileRuntimeAdapter:
    """Compose shared profile components without Agent-specific branches."""

    def __init__(self, *, spec: AgentSpec, settings: Any, model: str | None = None) -> None:
        self.spec = spec
        self.settings = settings
        self.model = model
        self.capabilities = AdapterCapabilities(
            execution_locus=spec.isolation.execution_locus,
            network_required=spec.isolation.network_required,
            system_requires=spec.isolation.system_requires,
            interaction_answer=(spec.capabilities.answer_interaction.state != "unsupported"),
        )

    async def run(self, task: AdapterRunInput, env: Any, data_path: Path) -> AdapterResult:
        attempt_dir = Path(data_path) / "attempts" / task.attempt_id
        workspace = attempt_dir / "skill_workspace"
        private = attempt_dir / ".agent-runtime"
        control = attempt_dir / ".agent-control"
        workspace.mkdir(parents=True, exist_ok=True)
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        control.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest = AgentManifestStore(control / "agent-manifest.json")

        try:
            _stage_uploaded_files(task, workspace, env=env, data_path=Path(data_path))
            mcp_result = self._render_mcp(task, private)
            options = _agent_options(task, self.spec.id)
            driver_kwargs = {
                "spec": self.spec,
                "task": task,
                "attempt_workspace": workspace,
                "project_path": Path(".").resolve(),
                "attempt_private": private,
                "effective_model": self.model,
                "options": options,
                "mcp_config_file": mcp_result.path if mcp_result else None,
                "mcp_shape": mcp_result.redacted_shape if mcp_result else (),
            }
            command_resume: CommandResumePlan | None = None
            if self.spec.driver.kind == "command-resume":
                command_resume = CommandResumeDriver().prepare(**driver_kwargs)
                launch_plan = command_resume.render_turn(0)
                driver_name = "command-resume@1"
                prompt_hashes = [item.prompt.content_hash for item in command_resume.turns]
                initial_turn_id = command_resume.first.turn_id
            else:
                one_shot = OneShotDriver().prepare(**driver_kwargs)
                launch_plan = render_launch_plan(self.spec, one_shot.launch_context)
                driver_name = "oneshot@1"
                prompt_hashes = [one_shot.prompt.content_hash]
                initial_turn_id = one_shot.turn_id
            launch_plan = _apply_wire_process_env(launch_plan, task)
            secret_values = _runtime_secret_values(self.spec, task, launch_plan)
            availability = await AvailabilityService().probe(self.spec)
            manifest.prepare(
                attempt_id=task.attempt_id,
                spec=self.spec,
                plan=launch_plan,
                agent_version=availability.version,
                requested_model=self.model,
                provider=_provider_name(self.model, self.settings),
                components={
                    "runtime": "local-cli@1",
                    "driver": driver_name,
                    "parser": f"{self.spec.output.parser}@{self.spec.output.parser_version}",
                    "mcp_dialect": f"{self.spec.mcp.dialect}@1",
                },
                config_summary={
                    "prompt_hash": prompt_hashes[0],
                    "prompt_hashes": prompt_hashes,
                    "options": options,
                    "mcp": mcp_result.redacted_shape if mcp_result else [],
                    "mcp_config_hash": mcp_result.config_hash if mcp_result else None,
                },
                path_aliases={
                    workspace: "skill_workspace",
                    private: "attempt_private",
                    control: "attempt_control",
                },
                secrets=secret_values,
            )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="agent_launch_plan_invalid",
                error_message=str(exc),
                security_meta=self._security_meta(workspace),
            )

        if command_resume is not None:
            return await self._run_command_resume(
                task=task,
                attempt_dir=attempt_dir,
                workspace=workspace,
                private=private,
                control=control,
                manifest=manifest,
                plan=command_resume,
                initial_launch_plan=launch_plan,
                secret_values=secret_values,
            )

        runtime = LocalCliRuntime()
        try:
            runtime_result = await runtime.run(
                launch_plan,
                evidence_dir=control / "runtime",
                timeout_seconds=task.timeout_seconds,
                base_env=_isolated_base_env(self.spec, private),
                redact_secrets=secret_values,
            )
        except asyncio.CancelledError:
            manifest.finalize(
                effective_model=None,
                effective_model_known=False,
                coverage={},
                cleanup={"status": "cancelled_runtime_cleanup"},
                outcome={"status": "cancelled", "error_code": "agent_cancelled"},
                degradations=("attempt cancelled before parser completion",),
                path_aliases={workspace: "skill_workspace", private: "attempt_private"},
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
                security_meta=self._security_meta(workspace),
            )

        parse_result = await self._parse(runtime_result, control)
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
        _write_parser_outputs(attempt_dir, parse_result)
        manifest.finalize(
            effective_model=None,
            effective_model_known=False,
            coverage=parse_result.coverage,
            cleanup={"status": runtime_result.cleanup},
            outcome={
                "status": adapter_status,
                "returncode": runtime_result.returncode,
                "error_code": classification.error_code,
                "parse_degraded": classification.parse_degraded,
            },
            sessions=(
                ({"turn_id": initial_turn_id, "session_id": parse_result.session_id},)
                if parse_result.session_id
                else ()
            ),
            degradations=tuple(item.message for item in parse_result.diagnostics),
            path_aliases={workspace: "skill_workspace", private: "attempt_private"},
            secrets=_secret_values(self.spec),
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
                "plan_hash": launch_plan.plan_hash,
            },
            error_code=(classification.error_code if adapter_status != "completed" else None),
            error_message=(classification.diagnostic if adapter_status != "completed" else None),
            events_count=len(parse_result.events),
            thinking_count=len(parse_result.thinking),
            token_usage=usage,
            duration_ms=runtime_result.duration_ms,
            security_meta=self._security_meta(workspace),
        )

    def _render_mcp(self, task: AdapterRunInput, private: Path):
        servers = resolve_mcp_servers(task, rewrite=lambda item: _wire_rewrite(item, task))
        if not servers:
            return None
        if self.spec.mcp.dialect != "json-file":
            raise McpDialectError(
                f"MCP dialect {self.spec.mcp.dialect!r} is not implemented for profile runtime"
            )
        filename = "mcp.json"
        return JsonFileDialect().render(servers, attempt_private=private, filename=filename)

    async def _parse(
        self, runtime_result, control: Path, *, runtime_subdir: str = "runtime"
    ) -> ParseResult:
        evidence = EvidenceSet.from_runtime_dir(control / runtime_subdir)
        try:
            if self.spec.output.parser == "text":
                parser = TextParser()
            elif self.spec.output.parser == "jsonl":
                parser = JsonlMappingParser(**self.spec.output.config)
            else:
                raise ValueError(f"unsupported profile parser: {self.spec.output.parser}")
            return await parser.parse(evidence)
        except Exception as exc:
            return ParseResult(
                final_text=None,
                coverage={"final_text": "unknown", "structured_events": "degraded"},
                diagnostics=(ParseDiagnostic("parser_crashed", f"{type(exc).__name__}: {exc}"),),
                degraded=True,
            )

    async def _run_command_resume(
        self,
        *,
        task: AdapterRunInput,
        attempt_dir: Path,
        workspace: Path,
        private: Path,
        control: Path,
        manifest: AgentManifestStore,
        plan: CommandResumePlan,
        initial_launch_plan: RenderedLaunchPlan,
        secret_values: tuple[str, ...],
    ) -> AdapterResult:
        runtime = LocalCliRuntime()
        started = time.monotonic()
        parsed_turns: list[ParseResult] = []
        sessions: list[dict[str, Any]] = []
        plan_hashes: list[str] = []
        session_id: str | None = None
        total_duration = 0
        cleanup_states: list[str] = []
        terminal_error: tuple[str, str, str] | None = None

        try:
            for index, turn in enumerate(plan.turns):
                launch = (
                    initial_launch_plan
                    if index == 0
                    else _apply_wire_process_env(
                        plan.render_turn(index, session_id=session_id), task
                    )
                )
                plan_hashes.append(launch.plan_hash)
                elapsed = time.monotonic() - started
                remaining = (
                    None
                    if task.timeout_seconds is None
                    else max(0.001, task.timeout_seconds - elapsed)
                )
                runtime_result = await runtime.run(
                    launch,
                    evidence_dir=control / "runtime" / f"turn-{index}",
                    timeout_seconds=remaining,
                    base_env=_isolated_base_env(self.spec, private),
                    redact_secrets=secret_values,
                )
                total_duration += runtime_result.duration_ms
                cleanup_states.append(runtime_result.cleanup)
                parsed = await self._parse(
                    runtime_result, control, runtime_subdir=f"runtime/turn-{index}"
                )
                parsed_turns.append(parsed)
                classification = classify_runtime_result(
                    runtime_result,
                    failure_patterns=self.spec.failure_patterns,
                    secrets=secret_values,
                    parse_degraded=parsed.degraded,
                )
                if runtime_result.status != "completed":
                    status = "timeout" if runtime_result.status == "timeout" else "cli_error"
                    terminal_error = (
                        status,
                        classification.error_code or "agent_nonzero_exit",
                        classification.diagnostic or "command-resume turn failed",
                    )
                    break
                if index == 0:
                    try:
                        session_id = plan.resolve_session([parsed.session_id])
                    except CommandResumeDriverError as exc:
                        terminal_error = ("cli_error", "agent_session_invalid", str(exc))
                        break
                elif parsed.session_id is not None and parsed.session_id != session_id:
                    terminal_error = (
                        "cli_error",
                        "agent_session_invalid",
                        f"resume returned session {parsed.session_id!r}, expected {session_id!r}",
                    )
                    break
                sessions.append({"turn_id": turn.turn_id, "session_id": session_id})
        except asyncio.CancelledError:
            manifest.finalize(
                effective_model=None,
                effective_model_known=False,
                coverage={},
                cleanup={"status": "cancelled_runtime_cleanup"},
                outcome={"status": "cancelled", "error_code": "agent_cancelled"},
                sessions=sessions,
                degradations=("attempt cancelled during command-resume execution",),
                path_aliases={workspace: "skill_workspace", private: "attempt_private"},
                secrets=secret_values,
            )
            raise
        except Exception as exc:
            terminal_error = ("cli_error", "agent_internal_error", str(exc))

        combined = _combine_parse_results(parsed_turns, session_id=session_id)
        _write_parser_outputs(attempt_dir, combined)
        status, error_code, error_message = terminal_error or ("completed", None, None)
        cleanup = "failed" if "failed" in cleanup_states else "confirmed"
        degradations = [item.message for item in combined.diagnostics]
        if error_message and error_message not in degradations:
            degradations.append(error_message)
        manifest.finalize(
            effective_model=None,
            effective_model_known=False,
            coverage=combined.coverage,
            cleanup={"status": cleanup, "turns": cleanup_states},
            outcome={"status": status, "error_code": error_code},
            sessions=sessions,
            degradations=tuple(degradations),
            path_aliases={workspace: "skill_workspace", private: "attempt_private"},
            secrets=secret_values,
        )
        usage = {
            key: value for key, value in (combined.usage or {}).items() if isinstance(value, int)
        }
        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                "agent_manifest": str(manifest.path),
                "coverage": dict(combined.coverage),
                "spec_hash": self.spec.spec_hash,
                "plan_hash": initial_launch_plan.plan_hash,
                "turn_plan_hashes": plan_hashes,
            },
            error_code=error_code,
            error_message=error_message,
            events_count=len(combined.events),
            thinking_count=len(combined.thinking),
            token_usage=usage,
            duration_ms=total_duration,
            security_meta=self._security_meta(workspace),
        )

    def _security_meta(self, workspace: Path) -> dict[str, Any]:
        return build_security_meta(
            execution_locus=self.spec.isolation.execution_locus,
            permission_mode=self.spec.isolation.permission_mode,
            workspace_root=str(workspace.resolve()),
        )


def _isolated_base_env(spec: AgentSpec, private: Path) -> dict[str, str]:
    env = dict(os.environ)
    if not spec.isolation.inherit_user_config:
        home = private / "home"
        config = private / "config"
        cache = private / "cache"
        temporary = private / "tmp"
        for path in (home, config, cache, temporary):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        env.update(
            HOME=str(home.resolve()),
            XDG_CONFIG_HOME=str(config.resolve()),
            XDG_CACHE_HOME=str(cache.resolve()),
            TMPDIR=str(temporary.resolve()),
        )
    return env


def _wire_rewrite(server: ResolvedMcpServer, task: AdapterRunInput) -> ResolvedMcpServer:
    rewrite = task.wire_injection.mcp_rewrites.get(server.name)
    if rewrite is None:
        return server
    return ResolvedMcpServer(
        name=server.name,
        command=rewrite.command,
        args=(*rewrite.args_prefix, server.command, *server.args),
        cwd=server.cwd,
        env=server.env,
    )


def _apply_wire_process_env(plan: RenderedLaunchPlan, task: AdapterRunInput) -> RenderedLaunchPlan:
    if not task.wire_injection.process_env:
        return plan
    env = {**plan.env, **task.wire_injection.process_env}
    redacted = {**plan.env_redacted}
    redacted.update({key: "***" for key in task.wire_injection.process_env})
    return replace(plan, env=env, env_redacted=redacted)


def _agent_options(task: AdapterRunInput, agent_id: str) -> Mapping[str, Any]:
    all_options = task.task_context.get("_agent_options", {})
    if not isinstance(all_options, Mapping):
        return {}
    options = all_options.get(agent_id, {})
    return dict(options) if isinstance(options, Mapping) else {}


def _secret_values(spec: AgentSpec) -> tuple[str, ...]:
    return tuple(value for ref in spec.auth if (value := os.environ.get(ref.env_var)) is not None)


def _runtime_secret_values(
    spec: AgentSpec,
    task: AdapterRunInput,
    launch_plan: RenderedLaunchPlan,
) -> tuple[str, ...]:
    values = list(_secret_values(spec))
    if task.session_token:
        values.append(task.session_token)
    for server in task.mcp_servers:
        values.extend(server.env.values())
    if task.wire_injection is not None:
        if task.wire_injection.capture_token:
            values.append(task.wire_injection.capture_token)
        values.extend(task.wire_injection.process_env.values())
    values.extend(
        value
        for name, value in launch_plan.env.items()
        if launch_plan.env_redacted.get(name) == "***"
    )
    return tuple(dict.fromkeys(value for value in values if value))


def _provider_name(model: str | None, settings: Any) -> str | None:
    if model is None or "/" not in model:
        return None
    prefix = model.split("/", 1)[0]
    return prefix if prefix in (settings.model_providers or {}) else None


def _stage_uploaded_files(
    task: AdapterRunInput, workspace: Path, *, env: Any, data_path: Path
) -> None:
    uploaded = task.task_context.get("uploaded_files")
    if not uploaded:
        return
    if not isinstance(uploaded, list):
        raise ValueError("uploaded_files must be a list")
    root = workspace.resolve()
    allowed_source_roots = (
        Path(env.env_dir).resolve(),
        (Path(data_path) / "uploads").resolve(),
    )
    for item in uploaded:
        if not isinstance(item, Mapping):
            raise ValueError("uploaded file descriptor must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise ValueError(f"unsafe uploaded file name: {name!r}")
        destination = (root / name).resolve()
        if not destination.is_relative_to(root):
            raise ValueError(f"uploaded file escapes workspace: {name!r}")
        if destination.is_file():
            continue
        source_value = item.get("path") or item.get("host_path")
        if not isinstance(source_value, str):
            raise ValueError(f"uploaded file {name!r} has no source path")
        source = Path(source_value).resolve()
        if not source.is_file():
            raise ValueError(f"uploaded file source does not exist: {name!r}")
        if not any(source.is_relative_to(allowed) for allowed in allowed_source_roots):
            raise ValueError(f"uploaded file source is outside declared material roots: {name!r}")
        shutil.copy2(source, destination)


def _write_parser_outputs(attempt_dir: Path, result: ParseResult) -> None:
    events_path = attempt_dir / "events.jsonl"
    with events_path.open("w", encoding="utf-8") as file:
        for event in result.events:
            file.write(json.dumps(dict(event), ensure_ascii=False, default=str) + "\n")
    thinking_path = attempt_dir / "thinking.jsonl"
    with thinking_path.open("w", encoding="utf-8") as file:
        for event in result.thinking:
            file.write(json.dumps(dict(event), ensure_ascii=False, default=str) + "\n")
    if result.final_text is not None:
        (attempt_dir / "agent_final.txt").write_text(result.final_text, encoding="utf-8")


def _combine_parse_results(
    results: list[ParseResult], *, session_id: str | None
) -> ParseResult:
    if not results:
        return ParseResult(
            final_text=None,
            session_id=session_id,
            coverage={"final_text": "unknown", "session": "unknown"},
            diagnostics=(ParseDiagnostic("no_turn_result", "no command-resume turn completed"),),
            degraded=True,
        )
    events: list[Mapping[str, Any]] = []
    thinking: list[Mapping[str, Any]] = []
    tools: list[Mapping[str, Any]] = []
    diagnostics: list[ParseDiagnostic] = []
    usage: dict[str, int] = {}
    coverage: dict[str, str] = {}
    final_text = None
    for index, result in enumerate(results):
        if result.final_text is not None:
            final_text = result.final_text
        events.extend({**dict(item), "turn_index": index} for item in result.events)
        thinking.extend({**dict(item), "turn_index": index} for item in result.thinking)
        tools.extend({**dict(item), "turn_index": index} for item in result.tool_refs)
        diagnostics.extend(result.diagnostics)
        for key, value in (result.usage or {}).items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value
        for key, value in result.coverage.items():
            previous = coverage.get(key)
            coverage[key] = value if previous in (None, value) else "partial"
    coverage["session"] = "verified" if session_id else "unknown"
    return ParseResult(
        final_text=final_text,
        events=tuple(events),
        thinking=tuple(thinking),
        tool_refs=tuple(tools),
        usage=usage or None,
        session_id=session_id,
        coverage=coverage,
        diagnostics=tuple(diagnostics),
        degraded=any(result.degraded for result in results),
    )
