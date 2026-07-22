"""Strict AgentSpec v1 models.

Profiles are deliberately data-only.  In particular, launch arguments may
reference a small set of framework-owned values, but never arbitrary Python,
shell fragments, environment variables, or secrets.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ALLOWED_LAUNCH_VALUES = frozenset(
    {"prompt", "prompt_file", "effective_model", "session_id", "mcp_config_file"}
)

AgentErrorCode = Literal[
    "agent_not_installed",
    "agent_version_unsupported",
    "agent_auth_missing",
    "agent_auth_failed",
    "agent_model_unsupported",
    "agent_rate_limited",
    "agent_usage_limit",
    "agent_network_error",
    "agent_timeout",
    "agent_cancelled",
    "agent_permission_required",
    "agent_session_invalid",
    "agent_remote_upload_not_allowed",
    "agent_nonzero_exit",
    "agent_output_parse_degraded",
    "agent_cleanup_failed",
    "agent_internal_error",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImplementationSpec(StrictModel):
    kind: Literal["profile-runtime", "plugin", "existing-adapter"]
    import_path: str | None = None

    @model_validator(mode="after")
    def validate_import_path(self) -> "ImplementationSpec":
        if self.kind in {"plugin", "existing-adapter"} and not self.import_path:
            raise ValueError(f"implementation kind {self.kind!r} requires import_path")
        if self.kind == "profile-runtime" and self.import_path is not None:
            raise ValueError("profile-runtime implementation cannot declare import_path")
        if self.import_path and not re.fullmatch(
            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", self.import_path
        ):
            raise ValueError("import_path must use 'module.path:attribute' syntax")
        return self


class LaunchArgument(StrictModel):
    flag: str | None = None
    value: str
    omit_if_none: bool = False

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        if value not in _ALLOWED_LAUNCH_VALUES and not value.startswith("option."):
            raise ValueError(f"unknown launch template value: {value!r}")
        if value.startswith("option.") and not _SLUG_RE.fullmatch(value[7:].replace("_", "-")):
            raise ValueError(f"invalid option launch value: {value!r}")
        return value


class EnvironmentValue(StrictModel):
    value: str | None = None
    secret_ref: str | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> "EnvironmentValue":
        if (self.value is None) == (self.secret_ref is None):
            raise ValueError("environment value requires exactly one of value or secret_ref")
        return self


class LaunchSpec(StrictModel):
    executable: str
    args: tuple[str | LaunchArgument, ...] = ()
    cwd: Literal["attempt_workspace", "project", "attempt_private"] = "attempt_workspace"
    env: dict[str, str | EnvironmentValue] = Field(default_factory=dict)
    shell: bool = False
    shell_reason: str | None = None

    @model_validator(mode="after")
    def validate_shell(self) -> "LaunchSpec":
        if self.shell and not self.shell_reason:
            raise ValueError("shell=true requires shell_reason")
        if not self.shell and self.shell_reason is not None:
            raise ValueError("shell_reason is only valid with shell=true")
        return self


class PromptSpec(StrictModel):
    mode: Literal["stdin", "file", "arg", "driver-owned"]
    arg_fallback: Literal["file", "stdin", "error"] = "error"


class DriverSpec(StrictModel):
    kind: Literal["oneshot", "command-resume"] = "oneshot"
    # ``launch.args`` is the first-turn command. Resume turns replace it with
    # this argv template after the first parser result yields one session ID.
    resume_args: tuple[str | LaunchArgument, ...] = ()

    @model_validator(mode="after")
    def validate_resume_contract(self) -> "DriverSpec":
        if self.kind == "oneshot":
            if self.resume_args:
                raise ValueError("oneshot driver cannot declare resume_args")
            return self
        if not self.resume_args:
            raise ValueError("command-resume driver requires resume_args")
        if not any(
            isinstance(item, LaunchArgument) and item.value == "session_id"
            for item in self.resume_args
        ):
            raise ValueError("command-resume resume_args require an explicit session_id slot")
        unsafe = {"--continue", "continue", "--last", "last", "--latest", "latest"}
        if any(isinstance(item, str) and item.lower() in unsafe for item in self.resume_args):
            raise ValueError("command-resume cannot select a latest/implicit session")
        return self


class ModelBindingSpec(StrictModel):
    binding: Literal["flag", "environment", "config-file", "agent-default", "unsupported"]
    flag: str | None = None
    env_var: str | None = None
    default_model: str | None = None
    protocols: tuple[
        Literal["anthropic", "openai-chat", "openai-responses", "agent-cloud", "model-proxy"], ...
    ] = ()

    @model_validator(mode="after")
    def validate_binding_details(self) -> "ModelBindingSpec":
        if self.binding == "flag" and not self.flag:
            raise ValueError("flag model binding requires flag")
        if self.binding == "environment" and not self.env_var:
            raise ValueError("environment model binding requires env_var")
        return self


class SecretRefSpec(StrictModel):
    name: str
    env_var: str
    required: bool = True

    @field_validator("env_var")
    @classmethod
    def validate_env_var(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
            raise ValueError("env_var must be an uppercase environment variable name")
        return value


class McpDialectSpec(StrictModel):
    dialect: Literal["json-file", "command-register", "native-config", "unsupported"]
    config_flag: str | None = None


class OutputSpec(StrictModel):
    parser: Literal["text", "jsonl", "native-session", "atif", "plugin"]
    parser_version: str = "1"
    parser_import_path: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_parser(self) -> "OutputSpec":
        if self.parser == "plugin" and not self.parser_import_path:
            raise ValueError("plugin parser requires parser_import_path")
        return self


class CapabilityValue(StrictModel):
    state: Literal["verified", "declared", "unsupported"] = "unsupported"
    basis: str | None = None


def _unsupported() -> CapabilityValue:
    return CapabilityValue()


class CapabilitySpec(StrictModel):
    single_turn: CapabilityValue = Field(default_factory=_unsupported)
    resume_send_message: CapabilityValue = Field(default_factory=_unsupported)
    answer_interaction: CapabilityValue = Field(default_factory=_unsupported)
    mcp: CapabilityValue = Field(default_factory=_unsupported)
    structured_events: CapabilityValue = Field(default_factory=_unsupported)
    token_usage: CapabilityValue = Field(default_factory=_unsupported)
    thinking: CapabilityValue = Field(default_factory=_unsupported)
    tools: CapabilityValue = Field(default_factory=_unsupported)
    subagent_identity: CapabilityValue = Field(default_factory=_unsupported)
    wire: CapabilityValue = Field(default_factory=_unsupported)

    @model_validator(mode="before")
    @classmethod
    def normalize_shorthand(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for key, value in normalized.items():
            if isinstance(value, str):
                normalized[key] = {"state": value}
            elif isinstance(value, bool):
                normalized[key] = {"state": "declared" if value else "unsupported"}
        return normalized


class IsolationSpec(StrictModel):
    execution_locus: Literal["host", "docker-sandbox", "remote-host"]
    network_required: Literal["none", "local_service", "public_internet"]
    system_requires: tuple[str, ...] = ()
    inherit_user_config: bool = False
    user_config_paths: tuple[str, ...] = ()
    permission_mode: str | None = None


class AvailabilityProbeSpec(StrictModel):
    executable: str | None = None
    version_command: tuple[str, ...] = ()
    version_constraint: str | None = None
    version_scheme: Literal["pep440", "semver", "regex"] = "pep440"
    version_regex: str | None = None
    timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    system_dependencies: tuple[str, ...] = ()
    configured_available: bool = False

    @field_validator("version_command")
    @classmethod
    def validate_version_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for token in value:
            if "{" in token or "}" in token:
                if token != "{executable}":
                    raise ValueError("version_command only supports the {executable} token")
        return value

    @model_validator(mode="after")
    def validate_version_parser(self) -> "AvailabilityProbeSpec":
        if self.version_constraint and not self.version_command:
            raise ValueError("version_constraint requires version_command")
        if self.version_scheme == "regex" and not self.version_regex:
            raise ValueError("regex version scheme requires version_regex")
        if self.version_regex:
            try:
                re.compile(self.version_regex)
            except re.error as exc:
                raise ValueError(f"invalid version_regex: {exc}") from exc
        return self


class AgentMetadata(StrictModel):
    homepage: HttpUrl | None = None
    installation_url: HttpUrl | None = None
    license: str | None = None
    description: str | None = None
    experimental: bool = False
    supported_platforms: tuple[str, ...] = ()
    repository: HttpUrl | None = None
    revision: str | None = None
    package_name: str | None = None
    package_version: str | None = None
    maintainer: str | None = None
    registry_url: HttpUrl | None = None
    registry_sha256: str | None = None
    distribution: dict[str, Any] | None = None
    data_boundary: str | None = None
    remote_endpoint: HttpUrl | None = None
    data_residency: str | None = None
    uploads_source_files: bool | None = None
    cancellation_semantics: str | None = None


class AgentOptionSpec(StrictModel):
    type: Literal["string", "integer", "number", "boolean"]
    default: Any = None
    sensitive: bool = False

    @model_validator(mode="after")
    def validate_default(self) -> "AgentOptionSpec":
        if self.default is None:
            return self
        expected = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
        }[self.type]
        if isinstance(self.default, bool) and self.type in {"integer", "number"}:
            raise ValueError(f"default must match option type {self.type}")
        if not isinstance(self.default, expected):
            raise ValueError(f"default must match option type {self.type}")
        return self


class FailurePatternSpec(StrictModel):
    error_code: AgentErrorCode
    pattern: str
    streams: tuple[Literal["stdout", "stderr"], ...] = ("stdout", "stderr")
    producer_code: str | None = None

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        try:
            re.compile(value, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid failure regex: {exc}") from exc
        return value

    @field_validator("producer_code")
    @classmethod
    def validate_producer_code(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            raise ValueError("producer_code contains unsupported characters")
        return value


class AgentSpec(StrictModel):
    schema_version: Literal["1"]
    id: str
    display_name: str
    source: Literal["builtin", "config", "config-override", "plugin", "legacy"]
    transport: Literal["local-cli", "ssh-cli", "acp", "python-sdk", "remote"]
    implementation: ImplementationSpec
    availability: AvailabilityProbeSpec = Field(default_factory=AvailabilityProbeSpec)
    launch: LaunchSpec | None = None
    prompt: PromptSpec
    driver: DriverSpec = Field(default_factory=DriverSpec)
    model: ModelBindingSpec
    auth: tuple[SecretRefSpec, ...] = ()
    mcp: McpDialectSpec
    output: OutputSpec
    capabilities: CapabilitySpec = Field(default_factory=CapabilitySpec)
    isolation: IsolationSpec
    metadata: AgentMetadata = Field(default_factory=AgentMetadata)
    options: dict[str, AgentOptionSpec] = Field(default_factory=dict)
    failure_patterns: tuple[FailurePatternSpec, ...] = ()
    warnings: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _SLUG_RE.fullmatch(value) and not re.fullmatch(
            r"acp:[a-z][a-z0-9-]*@[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",
            value,
        ):
            raise ValueError("agent id must be kebab-case or a stable ACP id")
        return value

    @model_validator(mode="after")
    def validate_launch(self) -> "AgentSpec":
        is_acp_id = self.id.startswith("acp:")
        if is_acp_id != (self.transport == "acp"):
            raise ValueError("stable acp: ids require transport='acp' and vice versa")
        if self.implementation.kind == "profile-runtime" and self.launch is None:
            raise ValueError("profile-runtime implementation requires launch")
        if self.driver.kind == "command-resume":
            if self.implementation.kind != "profile-runtime":
                raise ValueError("command-resume is only supported by profile-runtime")
            if self.capabilities.resume_send_message.state == "unsupported":
                raise ValueError("command-resume requires resume_send_message capability")
            assert self.launch is not None
            if any(
                isinstance(item, LaunchArgument) and item.value == "session_id"
                for item in self.launch.args
            ):
                raise ValueError(
                    "command-resume first-turn launch must discover, not guess, the session ID"
                )
        if self.availability.configured_available and self.transport not in {
            "remote",
            "python-sdk",
        }:
            raise ValueError("configured_available is reserved for native configured transports")
        if self.launch:
            arguments = (*self.launch.args, *self.driver.resume_args)
            for argument in arguments:
                if not isinstance(argument, LaunchArgument) or not argument.value.startswith(
                    "option."
                ):
                    continue
                option_name = argument.value[7:]
                option = self.options.get(option_name)
                if option is None:
                    raise ValueError(f"launch references undeclared option {option_name!r}")
                if option.sensitive:
                    raise ValueError(f"sensitive option {option_name!r} cannot be placed in argv")
        return self

    @property
    def spec_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        return f"sha256:{hashlib.sha256(rfc8785.dumps(payload)).hexdigest()}"


def agent_spec_json_schema() -> dict[str, Any]:
    return AgentSpec.model_json_schema()
