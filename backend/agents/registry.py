"""Authoritative, deterministic agent registry.

Construction only validates descriptors.  Adapter/plugin code is imported
when ``build_adapter`` is called, so an unavailable optional SDK cannot make
the rest of the catalog disappear.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Iterable

from pydantic import ValidationError

from .availability import AvailabilityResult, AvailabilityService
from .models import AgentSpec


class AgentRegistryError(ValueError):
    """A deterministic profile registration or resolution failure."""


_availability_service = AvailabilityService()


def _load_attribute(import_path: str) -> Any:
    module_name, attribute = import_path.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


@dataclass(frozen=True)
class ResolvedAgent:
    spec: AgentSpec
    settings: Any

    @property
    def spec_hash(self) -> str:
        return self.spec.spec_hash

    def build_adapter(self, model: str | None = None) -> Any:
        path = self.spec.implementation.import_path
        if path is None:
            if self.spec.implementation.kind != "profile-runtime":
                raise AgentRegistryError(f"agent {self.spec.id!r} has no implementation path")
            from .transports.adapter import ProfileRuntimeAdapter

            return ProfileRuntimeAdapter(spec=self.spec, settings=self.settings, model=model)
        try:
            factory = _load_attribute(path)
            return factory(settings=self.settings, spec=self.spec, model=model)
        except AgentRegistryError:
            raise
        except Exception as exc:
            raise AgentRegistryError(
                f"failed to load adapter for agent {self.spec.id!r} from {path!r}: {exc}"
            ) from exc


class AgentRegistry:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._specs: dict[str, AgentSpec] = {}

    @classmethod
    def from_settings(cls, settings: Any) -> "AgentRegistry":
        registry = cls(settings)
        for spec in _builtin_specs(settings):
            registry.register(spec)

        agent_settings = getattr(settings, "agents", None)
        profiles = getattr(agent_settings, "profiles", {}) if agent_settings else {}
        for agent_id in sorted(profiles):
            raw = dict(profiles[agent_id])
            override = bool(raw.pop("override", False))
            supplied_id = raw.pop("id", agent_id)
            if supplied_id != agent_id:
                raise AgentRegistryError(
                    f"agent profile key {agent_id!r} does not match id {supplied_id!r}"
                )
            if "source" in raw:
                raise AgentRegistryError(f"agent profile {agent_id!r} cannot declare source")
            raw.update(
                id=agent_id,
                source="config-override" if override else "config",
            )
            try:
                spec = AgentSpec.model_validate(raw)
            except ValidationError as exc:
                raise AgentRegistryError(f"invalid agent profile {agent_id!r}: {exc}") from exc
            registry.register(spec, override=override)

        plugins = getattr(agent_settings, "plugins", {}) if agent_settings else {}
        for agent_id in sorted(plugins):
            raw = dict(plugins[agent_id])
            supplied_id = raw.pop("id", agent_id)
            if supplied_id != agent_id:
                raise AgentRegistryError(
                    f"agent plugin key {agent_id!r} does not match id {supplied_id!r}"
                )
            if "source" in raw or "override" in raw:
                raise AgentRegistryError(
                    f"agent plugin {agent_id!r} cannot declare source or override"
                )
            raw.update(id=agent_id, source="plugin")
            try:
                spec = AgentSpec.model_validate(raw)
            except ValidationError as exc:
                raise AgentRegistryError(f"invalid agent plugin {agent_id!r}: {exc}") from exc
            if spec.implementation.kind != "plugin":
                raise AgentRegistryError(
                    f"agent plugin {agent_id!r} must use implementation.kind='plugin'"
                )
            registry.register(spec)

        acp_agents = getattr(agent_settings, "acp", {}) if agent_settings else {}
        for stable_id in sorted(acp_agents):
            registry.register(_acp_spec(stable_id, acp_agents[stable_id]))

        remote_agents = getattr(agent_settings, "remote", {}) if agent_settings else {}
        for agent_id in sorted(remote_agents):
            registry.register(_remote_spec(agent_id, remote_agents[agent_id]))

        python_plugins = getattr(agent_settings, "python_plugins", {}) if agent_settings else {}
        for agent_id in sorted(python_plugins):
            registry.register(_python_plugin_spec(agent_id, python_plugins[agent_id]))

        for spec in _legacy_specs(settings):
            registry.register(spec)
        return registry

    def register(self, spec: AgentSpec, *, override: bool = False) -> None:
        previous = self._specs.get(spec.id)
        if previous is None:
            if override:
                raise AgentRegistryError(
                    f"agent {spec.id!r} sets override=true but no built-in profile exists"
                )
            self._specs[spec.id] = spec
            return
        if not override:
            raise AgentRegistryError(
                f"duplicate agent id {spec.id!r}: {previous.source} conflicts with {spec.source}"
            )
        if previous.source != "builtin":
            raise AgentRegistryError(
                f"agent {spec.id!r} may only explicitly override a built-in profile"
            )
        if spec.source != "config-override":
            raise AgentRegistryError("overridden profile source must be config-override")
        self._specs[spec.id] = spec

    def resolve(self, agent_id: str) -> ResolvedAgent:
        try:
            spec = self._specs[agent_id]
        except KeyError as exc:
            raise AgentRegistryError(f"unknown agent: {agent_id!r}") from exc
        return ResolvedAgent(spec=spec, settings=self.settings)

    def known_agents(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def specs(self) -> tuple[AgentSpec, ...]:
        return tuple(self._specs.values())

    def describe_all(self) -> list[dict[str, Any]]:
        """Return the catalog without importing adapter/plugin code.

        Full version/auth probing is owned by AvailabilityService (A1-5).
        This initial descriptor intentionally limits itself to a read-only
        executable lookup while exposing both v2 and compatibility fields.
        """
        # Compatibility helper for synchronous callers.  The API uses the
        # async variant below so version/auth/dependency probes are included.
        results = {
            spec.id: AvailabilityResult(status="unknown", reason="availability probe not run")
            for spec in self._specs.values()
        }
        return [self._descriptor(spec, results[spec.id]) for spec in self._specs.values()]

    async def describe_all_async(
        self, *, service: AvailabilityService | None = None
    ) -> list[dict[str, Any]]:
        service = service or _availability_service
        specs = self.specs()
        results = await service.probe_all(specs)
        return [self._descriptor(spec, results[spec.id]) for spec in specs]

    async def probe_availability(
        self,
        agent_id: str,
        *,
        service: AvailabilityService | None = None,
        refresh: bool = False,
    ) -> AvailabilityResult:
        service = service or _availability_service
        return await service.probe(self.resolve(agent_id).spec, refresh=refresh)

    @staticmethod
    def _descriptor(spec: AgentSpec, result: AvailabilityResult) -> dict[str, Any]:
        return {
            "id": spec.id,
            "name": spec.id,
            "display_name": spec.display_name,
            "source": spec.source,
            "transport": spec.transport,
            "availability": result.as_dict(),
            "version": result.version,
            # Compatibility fields retained for the current frontend.
            "status": "available" if result.available else "not_found",
            "detail": result.reason,
            "cli_path": result.cli_path,
            "capabilities": spec.capabilities.model_dump(mode="json"),
            "model_support": spec.model.model_dump(mode="json"),
            "metadata": spec.metadata.model_dump(mode="json", exclude_none=True),
            "spec_hash": spec.spec_hash,
            "warnings": list(spec.warnings),
        }

    def __iter__(self) -> Iterable[ResolvedAgent]:
        for agent_id in self._specs:
            yield self.resolve(agent_id)


def _acp_spec(stable_id: str, configured: Any) -> AgentSpec:
    from .acp.registry import RegistryAgent

    try:
        prefix, version = stable_id.removeprefix("acp:").rsplit("@", 1)
    except ValueError as exc:
        raise AgentRegistryError(f"invalid ACP stable id: {stable_id!r}") from exc
    if not configured.command:
        raise AgentRegistryError(f"ACP agent {stable_id!r} requires a preinstalled command")
    entry = None
    if configured.registry_entry is not None:
        try:
            entry = RegistryAgent.model_validate(configured.registry_entry)
        except ValidationError as exc:
            raise AgentRegistryError(f"invalid ACP metadata for {stable_id!r}: {exc}") from exc
        if entry.id != prefix or entry.version != version:
            raise AgentRegistryError(f"ACP metadata does not match stable id {stable_id!r}")
    metadata = {
        "description": entry.description if entry else "Preinstalled ACP registry agent",
        "repository": entry.repository if entry else None,
        "homepage": entry.website if entry else None,
        "license": entry.license if entry else None,
        "package_version": version,
        "maintainer": ", ".join(entry.authors) if entry and entry.authors else None,
        "experimental": True,
        "registry_url": configured.registry_url,
        "registry_sha256": configured.registry_sha256,
        "distribution": (
            entry.distribution.model_dump(mode="json", exclude_none=True) if entry else None
        ),
        "data_boundary": "local ACP subprocess; agent-specific network behavior applies",
    }
    auth = [
        {"name": f"environment:{name.lower()}", "env_var": name}
        for name in configured.env_from
    ]
    return AgentSpec.model_validate(
        {
            "schema_version": "1",
            "id": stable_id,
            "display_name": entry.name if entry else prefix,
            "source": "config",
            "transport": "acp",
            "implementation": {
                "kind": "plugin",
                "import_path": "backend.agents.acp.adapter:build_acp_adapter",
            },
            "availability": {"executable": configured.command[0]},
            "prompt": {"mode": "driver-owned"},
            "model": {"binding": "agent-default"},
            "auth": auth,
            "mcp": {"dialect": "native-config"},
            "output": {
                "parser": "plugin",
                "parser_import_path": "backend.agents.acp.parser:AcpParser",
            },
            "capabilities": {
                "single_turn": "verified",
                "resume_send_message": "verified",
                "answer_interaction": "declared",
                "mcp": "verified",
                "structured_events": "verified",
                "token_usage": "declared",
                "thinking": "verified",
                "tools": "verified",
                "wire": "unsupported",
            },
            "isolation": {
                "execution_locus": "host",
                "network_required": "public_internet",
                "permission_mode": "explicit-acp-options",
            },
            "metadata": {key: value for key, value in metadata.items() if value is not None},
            "warnings": (
                "ACP registry metadata is untrusted; execution uses only the configured preinstalled command",
            ),
        }
    )


def _remote_spec(agent_id: str, configured: Any) -> AgentSpec:
    auth = (
        [{"name": "remote-api", "env_var": configured.api_key_env}]
        if configured.api_key_env
        else []
    )
    return AgentSpec.model_validate(
        {
            "schema_version": "1",
            "id": agent_id,
            "display_name": agent_id,
            "source": "config",
            "transport": "remote",
            "implementation": {
                "kind": "plugin",
                "import_path": "backend.agents.remote.adapter:build_remote_adapter",
            },
            "availability": {"configured_available": True},
            "prompt": {"mode": "driver-owned"},
            "model": {
                "binding": "agent-default" if not configured.supports_model else "config-file"
            },
            "auth": auth,
            "mcp": {"dialect": "unsupported"},
            "output": {
                "parser": "plugin",
                "parser_import_path": "backend.agents.remote.adapter:RemoteTransportAdapter",
            },
            "capabilities": {
                "single_turn": "verified",
                "resume_send_message": (
                    "declared" if configured.supports_multi_turn else "unsupported"
                ),
                "structured_events": "declared",
                "token_usage": "declared",
                "tools": "declared",
                "wire": "unsupported",
            },
            "isolation": {
                "execution_locus": "remote-host",
                "network_required": "public_internet",
                "permission_mode": "remote-service-defined",
            },
            "metadata": {
                "experimental": True,
                "description": "Configured remote Agent service",
                "remote_endpoint": str(configured.endpoint),
                "data_boundary": "task data leaves the Agent Arena host",
                "data_residency": configured.data_residency,
                "uploads_source_files": configured.upload_files,
                "cancellation_semantics": configured.cancellation_semantics,
            },
            "warnings": (
                "Remote execution may continue after local cancellation unless confirmed by the service",
            ),
        }
    )


def _python_plugin_spec(agent_id: str, configured: Any) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "schema_version": "1",
            "id": agent_id,
            "display_name": configured.display_name or agent_id,
            "source": "plugin",
            "transport": "python-sdk",
            "implementation": {
                "kind": "plugin",
                "import_path": "backend.agents.python_plugin.adapter:build_python_plugin_adapter",
            },
            "availability": {"configured_available": True},
            "prompt": {"mode": "driver-owned"},
            "model": {
                "binding": "agent-default" if not configured.supports_model else "config-file"
            },
            "mcp": {
                "dialect": "native-config" if configured.supports_mcp else "unsupported"
            },
            "output": {
                "parser": "plugin",
                "parser_import_path": configured.entrypoint,
            },
            "capabilities": {
                "single_turn": "verified",
                "mcp": "declared" if configured.supports_mcp else "unsupported",
                "structured_events": "declared",
                "token_usage": "declared",
                "tools": "declared",
                "wire": "unsupported",
            },
            "isolation": {
                "execution_locus": "host",
                "network_required": "public_internet",
                "permission_mode": "trusted-in-process-plugin",
            },
            "metadata": {
                "experimental": True,
                "description": "Framework-wrapped Python Agent plugin",
                "package_name": configured.package_name,
                "package_version": configured.package_version,
                "data_boundary": "trusted Python code executes in the Agent Arena process",
            },
            "warnings": (
                "Python plugins are trusted in-process code, not a security sandbox",
            ),
        }
    )
def _existing_spec(
    *,
    agent_id: str,
    display_name: str,
    executable: str | None,
    import_path: str,
    transport: str = "local-cli",
    execution_locus: str = "host",
    version_args: tuple[str, ...] = ("--version",),
) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "schema_version": "1",
            "id": agent_id,
            "display_name": display_name,
            "source": "builtin",
            "transport": transport,
            "implementation": {"kind": "existing-adapter", "import_path": import_path},
            "availability": {
                "executable": executable,
                "version_command": ["{executable}", *version_args] if executable else [],
            },
            "prompt": {"mode": "driver-owned"},
            "model": {"binding": "flag", "flag": "--model"},
            "mcp": {"dialect": "native-config"},
            "output": {"parser": "plugin", "parser_import_path": import_path},
            "capabilities": {
                "single_turn": "verified",
                "resume_send_message": "verified",
                "answer_interaction": "unsupported",
                "mcp": "verified",
                "structured_events": "verified",
                "token_usage": "verified",
                "thinking": "verified",
                "tools": "verified",
                "wire": "verified" if agent_id in {"claude-code", "codex"} else "unsupported",
            },
            "isolation": {
                "execution_locus": execution_locus,
                "network_required": "public_internet",
            },
        }
    )


def _builtin_specs(settings: Any) -> tuple[AgentSpec, ...]:
    specs = [
        _existing_spec(
            agent_id="claude-code",
            display_name="Claude Code",
            executable="claude",
            import_path="backend.agents.builtin:build_claude_code_adapter",
        ),
        _existing_spec(
            agent_id="codex",
            display_name="Codex",
            executable="codex",
            import_path="backend.agents.builtin:build_codex_adapter",
        ),
        AgentSpec.model_validate(
            {
                "schema_version": "1",
                "id": "deerflow",
                "display_name": "DeerFlow",
                "source": "builtin",
                "transport": "local-cli",
                "implementation": {
                    "kind": "plugin",
                    "import_path": "backend.agents.deerflow.plugin:build_deerflow_adapter",
                },
                "availability": {
                    "executable": "deerflow-arena-runner",
                    "version_command": ["{executable}", "--probe"],
                    "version_constraint": "==2.0.0",
                },
                "prompt": {"mode": "driver-owned"},
                "model": {
                    "binding": "config-file",
                    "protocols": ["openai-chat", "openai-responses", "anthropic"],
                },
                "mcp": {"dialect": "unsupported"},
                "output": {
                    "parser": "plugin",
                    "parser_import_path": "backend.agents.deerflow.parser:DeerFlowParser",
                },
                "capabilities": {
                    "single_turn": {
                        "state": "verified",
                        "basis": "agent-arena DeerFlow v2.0.0 offline runner fixtures",
                    },
                    "resume_send_message": {
                        "state": "unsupported",
                        "basis": "pinned embedded client has no validated cross-attempt resume contract",
                    },
                    "mcp": {
                        "state": "unsupported",
                        "basis": "embedded extension lifecycle not yet validated for Lane MCP",
                    },
                    "structured_events": "verified",
                    "token_usage": "declared",
                    "thinking": "declared",
                    "tools": "verified",
                    "subagent_identity": "unsupported",
                    "wire": "unsupported",
                },
                "isolation": {
                    "execution_locus": "host",
                    "network_required": "public_internet",
                    "permission_mode": "workspace-write",
                },
                "metadata": {
                    "homepage": "https://deerflow.tech/",
                    "installation_url": "https://github.com/bytedance/deer-flow/tree/v2.0.0",
                    "repository": "https://github.com/bytedance/deer-flow",
                    "revision": "7e7f0410797693cf882594555ba414e0361d4c6f",
                    "package_name": "deerflow-harness",
                    "package_version": "2.0.0",
                    "maintainer": "ByteDance DeerFlow",
                    "license": "MIT",
                    "description": "Pinned DeerFlow 2 embedded client integration",
                    "experimental": True,
                    "supported_platforms": ["linux", "darwin"],
                },
                "options": {
                    "subagent": {"type": "boolean", "default": False},
                    "thinking": {"type": "boolean", "default": True},
                    "plan_mode": {"type": "boolean", "default": False},
                    "summarize": {"type": "boolean", "default": False},
                    "recursion_limit": {"type": "integer", "default": 1000},
                    "allow_host_bash": {"type": "boolean", "default": False},
                },
            }
        ),
    ]
    if settings.ssh_claude_code.ssh_host is not None:
        specs.append(
            _existing_spec(
                agent_id="ssh-claude-code",
                display_name="Claude Code over SSH",
                executable="ssh",
                import_path="backend.agents.builtin:build_ssh_claude_code_adapter",
                transport="ssh-cli",
                execution_locus="remote-host",
                version_args=("-V",),
            )
        )
    return tuple(specs)


def _legacy_specs(settings: Any) -> tuple[AgentSpec, ...]:
    translated: list[AgentSpec] = []
    for agent_id in sorted(settings.custom_agents):
        custom = settings.custom_agents[agent_id]
        command = list(custom.command)
        executable = command[0] if command else None
        output_config = custom.jsonl_fields.model_dump() if custom.output_format == "jsonl" else {}
        translated.append(
            AgentSpec.model_validate(
                {
                    "schema_version": "1",
                    "id": agent_id,
                    "display_name": agent_id,
                    "source": "legacy",
                    "transport": "local-cli",
                    "implementation": {
                        "kind": "existing-adapter",
                        "import_path": "backend.agents.builtin:build_legacy_custom_adapter",
                    },
                    "availability": {"executable": executable},
                    "launch": {"executable": executable or "invalid", "args": command[1:]},
                    "prompt": {"mode": custom.prompt_mode},
                    "model": {"binding": "unsupported"},
                    "mcp": {
                        "dialect": "json-file" if custom.mcp_config_flag else "unsupported",
                        "config_flag": custom.mcp_config_flag,
                    },
                    "output": {
                        "parser": custom.output_format,
                        "config": output_config,
                    },
                    "capabilities": {
                        "single_turn": "declared",
                        "mcp": "declared" if custom.mcp_config_flag else "unsupported",
                        "structured_events": (
                            "declared" if custom.output_format == "jsonl" else "unsupported"
                        ),
                    },
                    "isolation": {
                        "execution_locus": "host",
                        "network_required": "public_internet",
                    },
                    "warnings": (
                        "custom_agents is deprecated; migrate this entry to agents.profiles",
                    ),
                }
            )
        )
    return tuple(translated)
