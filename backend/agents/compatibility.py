"""Pure compatibility preflight for an AgentSpec and requested attempt."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Literal

from ..adapters.base import ConversationTurn, McpServerSpec
from .availability import AvailabilityResult
from .models import AgentSpec


CompatibilityCode = Literal[
    "agent_not_installed",
    "agent_version_unsupported",
    "agent_auth_missing",
    "agent_dependency_missing",
    "agent_misconfigured",
    "agent_availability_unknown",
    "agent_platform_unsupported",
    "agent_model_unsupported",
    "agent_provider_unsupported",
    "agent_mcp_unsupported",
    "agent_single_turn_unsupported",
    "agent_resume_unsupported",
    "agent_interaction_unsupported",
    "agent_wire_unsupported",
]


@dataclass(frozen=True)
class CompatibilityIssue:
    code: CompatibilityCode
    message: str
    field: str
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CompatibilityReport:
    agent_id: str
    issues: tuple[CompatibilityIssue, ...]

    @property
    def compatible(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "compatible": self.compatible,
            "issues": [issue.as_dict() for issue in self.issues],
        }


_AVAILABILITY_ISSUES: dict[str, tuple[CompatibilityCode, str]] = {
    "not_installed": ("agent_not_installed", "Agent executable is not installed"),
    "version_unsupported": (
        "agent_version_unsupported",
        "Agent version is outside the supported range",
    ),
    "missing_auth": ("agent_auth_missing", "Required Agent authentication is missing"),
    "missing_dependency": (
        "agent_dependency_missing",
        "Required system dependency is missing",
    ),
    "misconfigured": ("agent_misconfigured", "Agent availability probe is misconfigured"),
    "unknown": ("agent_availability_unknown", "Agent availability could not be confirmed"),
}


def check_compatibility(
    spec: AgentSpec,
    *,
    availability: AvailabilityResult,
    requested_model: str | None,
    provider_protocol: str | None,
    mcp_servers: tuple[McpServerSpec, ...],
    conversation_turns: tuple[ConversationTurn, ...],
    provider_auth_available: bool | None = None,
    strict_wire: bool = False,
    platform: str | None = None,
) -> CompatibilityReport:
    issues: list[CompatibilityIssue] = []
    if availability.status != "available":
        code, message = _AVAILABILITY_ISSUES[availability.status]
        issues.append(
            CompatibilityIssue(
                code=code,
                message=message,
                field="availability",
                detail={
                    "status": availability.status,
                    "version": availability.version,
                    "reason": availability.reason,
                },
            )
        )

    current_platform = platform or sys.platform
    supported_platforms = spec.metadata.supported_platforms
    if supported_platforms and current_platform not in supported_platforms:
        issues.append(
            CompatibilityIssue(
                code="agent_platform_unsupported",
                message=f"Agent does not support platform {current_platform!r}",
                field="platform",
                detail={"platform": current_platform},
            )
        )

    if requested_model is not None and spec.model.binding in {"unsupported", "agent-default"}:
        issues.append(
            CompatibilityIssue(
                code="agent_model_unsupported",
                message="Agent does not support overriding its model",
                field="model",
                detail={"requested_model": requested_model, "binding": spec.model.binding},
            )
        )
    if (
        provider_protocol is not None
        and spec.model.protocols
        and provider_protocol not in spec.model.protocols
    ):
        issues.append(
            CompatibilityIssue(
                code="agent_provider_unsupported",
                message=f"Agent does not support provider protocol {provider_protocol!r}",
                field="provider",
                detail={
                    "requested_protocol": provider_protocol,
                    "supported_protocols": list(spec.model.protocols),
                },
            )
        )
    if provider_auth_available is False:
        issues.append(
            CompatibilityIssue(
                code="agent_auth_missing",
                message="Requested model provider authentication is missing",
                field="provider_auth",
                detail={},
            )
        )

    if mcp_servers and (
        spec.capabilities.mcp.state == "unsupported" or spec.mcp.dialect == "unsupported"
    ):
        issues.append(
            CompatibilityIssue(
                code="agent_mcp_unsupported",
                message="Task declares MCP servers but Agent does not support MCP",
                field="mcp",
                detail={"server_names": [server.name for server in mcp_servers]},
            )
        )

    send_count = sum(turn.action == "send_message" for turn in conversation_turns) or 1
    if spec.capabilities.single_turn.state == "unsupported":
        issues.append(
            CompatibilityIssue(
                code="agent_single_turn_unsupported",
                message="Agent has no verified or declared single-turn capability",
                field="conversation",
                detail={"send_message_turns": send_count},
            )
        )
    if send_count > 1 and spec.capabilities.resume_send_message.state == "unsupported":
        issues.append(
            CompatibilityIssue(
                code="agent_resume_unsupported",
                message="Conversation requires explicit resume/send-message support",
                field="conversation",
                detail={"send_message_turns": send_count},
            )
        )
    if any(turn.action == "answer_interaction" for turn in conversation_turns) and (
        spec.capabilities.answer_interaction.state == "unsupported"
    ):
        issues.append(
            CompatibilityIssue(
                code="agent_interaction_unsupported",
                message="Conversation requires an interaction-answer channel",
                field="conversation",
                detail={},
            )
        )
    if strict_wire and spec.capabilities.wire.state == "unsupported":
        issues.append(
            CompatibilityIssue(
                code="agent_wire_unsupported",
                message="Strict Wire coverage was requested but is unsupported",
                field="wire",
                detail={},
            )
        )
    return CompatibilityReport(agent_id=spec.id, issues=tuple(issues))
