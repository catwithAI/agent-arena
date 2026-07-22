"""Deterministic, secret-safe launch plan rendering for local CLI profiles."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import rfc8785

from .models import AgentSpec, EnvironmentValue, LaunchArgument


class LaunchPlanError(ValueError):
    """A profile cannot be rendered safely for the supplied attempt."""


@dataclass(frozen=True)
class LaunchContext:
    prompt: str
    attempt_workspace: Path
    project_path: Path
    attempt_private: Path
    prompt_file: Path | None = None
    effective_model: str | None = None
    session_id: str | None = None
    mcp_config_file: Path | None = None
    options: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)
    mcp_shape: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class RenderedLaunchPlan:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    env_redacted: Mapping[str, str]
    stdin_data: bytes | None
    prompt_mode: str
    plan_hash: str

    @property
    def argv_redacted(self) -> tuple[str, ...]:
        # AgentSpec forbids secret references in argv, so this is identical
        # by construction rather than relying on best-effort replacement.
        return self.argv

    @property
    def env_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.env))


def render_launch_plan(
    spec: AgentSpec,
    context: LaunchContext,
    *,
    secrets: Mapping[str, str] | None = None,
    arg_max: int | None = None,
) -> RenderedLaunchPlan:
    launch = spec.launch
    if launch is None:
        raise LaunchPlanError(f"agent {spec.id!r} has no declarative launch specification")
    unknown_options = sorted(set(context.options) - set(spec.options))
    if unknown_options:
        raise LaunchPlanError("undeclared agent options: " + ", ".join(unknown_options))

    cwd = {
        "attempt_workspace": context.attempt_workspace,
        "project": context.project_path,
        "attempt_private": context.attempt_private,
    }[launch.cwd].resolve()

    mode = spec.prompt.mode
    stdin_data = context.prompt.encode("utf-8") if mode == "stdin" else None
    argv = _render_argv(launch.executable, launch.args, context)

    if mode == "arg":
        maximum = arg_max if arg_max is not None else _platform_arg_max()
        if _argv_size(argv) > maximum:
            fallback = spec.prompt.arg_fallback
            if fallback == "error":
                raise LaunchPlanError(
                    f"rendered argv is {_argv_size(argv)} bytes, exceeding limit {maximum}"
                )
            if fallback == "file":
                if context.prompt_file is None:
                    raise LaunchPlanError("file prompt fallback requires prompt_file")
                mode = "file"
                argv = _render_argv(
                    launch.executable,
                    launch.args,
                    context,
                    prompt_override=str(context.prompt_file.resolve()),
                )
            elif fallback == "stdin":
                mode = "stdin"
                stdin_data = context.prompt.encode("utf-8")
                argv = _render_argv(
                    launch.executable,
                    launch.args,
                    context,
                    omit_prompt=True,
                )
            if _argv_size(argv) > maximum:
                raise LaunchPlanError(
                    f"fallback argv is {_argv_size(argv)} bytes, exceeding limit {maximum}"
                )

    env, env_redacted = _render_env(launch.env, secrets=secrets)
    plan_hash = _logical_plan_hash(spec, context, effective_prompt_mode=mode)
    return RenderedLaunchPlan(
        argv=argv,
        cwd=cwd,
        env=env,
        env_redacted=env_redacted,
        stdin_data=stdin_data,
        prompt_mode=mode,
        plan_hash=plan_hash,
    )


def _render_argv(
    executable: str,
    args: tuple[str | LaunchArgument, ...],
    context: LaunchContext,
    *,
    prompt_override: str | None = None,
    omit_prompt: bool = False,
) -> tuple[str, ...]:
    rendered = [executable]
    for argument in args:
        if isinstance(argument, str):
            rendered.append(argument)
            continue
        if argument.value == "prompt" and omit_prompt:
            continue
        value = _launch_value(argument.value, context)
        if argument.value == "prompt" and prompt_override is not None:
            value = prompt_override
        if value is None:
            if argument.omit_if_none:
                continue
            raise LaunchPlanError(f"launch value {argument.value!r} is required but unavailable")
        if argument.flag:
            rendered.append(argument.flag)
        rendered.append(str(value))
    return tuple(rendered)


def _launch_value(name: str, context: LaunchContext) -> Any:
    if name == "prompt":
        return context.prompt
    if name == "prompt_file":
        return str(context.prompt_file.resolve()) if context.prompt_file else None
    if name == "effective_model":
        return context.effective_model
    if name == "session_id":
        return context.session_id
    if name == "mcp_config_file":
        return str(context.mcp_config_file.resolve()) if context.mcp_config_file else None
    if name.startswith("option."):
        return context.options.get(name[7:])
    raise LaunchPlanError(f"unsupported launch value {name!r}")


def _render_env(
    configured: Mapping[str, str | EnvironmentValue],
    *,
    secrets: Mapping[str, str] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    secret_source = os.environ if secrets is None else secrets
    env: dict[str, str] = {}
    redacted: dict[str, str] = {}
    for name, configured_value in configured.items():
        if isinstance(configured_value, str):
            env[name] = configured_value
            redacted[name] = configured_value
            continue
        if configured_value.secret_ref is not None:
            secret = secret_source.get(configured_value.secret_ref)
            if secret is None:
                raise LaunchPlanError(
                    f"required secret reference {configured_value.secret_ref!r} is unavailable"
                )
            env[name] = secret
            redacted[name] = "***"
        else:
            assert configured_value.value is not None
            env[name] = configured_value.value
            redacted[name] = configured_value.value
    return env, redacted


def _logical_plan_hash(
    spec: AgentSpec, context: LaunchContext, *, effective_prompt_mode: str
) -> str:
    logical = {
        "schema_version": "1",
        "agent_id": spec.id,
        "spec_hash": spec.spec_hash,
        "launch": spec.launch.model_dump(mode="json", exclude_none=True) if spec.launch else None,
        "logical_cwd": spec.launch.cwd if spec.launch else None,
        "prompt_mode": effective_prompt_mode,
        "prompt_hash": hashlib.sha256(context.prompt.encode("utf-8")).hexdigest(),
        "effective_model": context.effective_model,
        "options": dict(sorted(context.options.items())),
        "mcp_shape": list(context.mcp_shape),
        # Absolute attempt paths, resolved secret values, and random session
        # IDs are intentionally absent.
    }
    return f"sha256:{hashlib.sha256(rfc8785.dumps(logical)).hexdigest()}"


def _platform_arg_max() -> int:
    try:
        # Leave room for the inherited environment and platform bookkeeping.
        return max(4096, int(os.sysconf("SC_ARG_MAX")) - 32768)
    except (AttributeError, OSError, ValueError):
        return 128 * 1024


def _argv_size(argv: tuple[str, ...]) -> int:
    return sum(len(value.encode("utf-8")) + 1 for value in argv)
