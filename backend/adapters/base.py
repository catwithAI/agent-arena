"""Adapter interface and shared data structures.

This is the extension point of the whole project: `AgentAdapter` is a
`Protocol`, not a base class, so plugging in a new agent means writing one
file that satisfies this shape and registering it — nothing else in
`backend.runner` / `backend.run_dispatch` needs to change. `ClaudeCodeAdapter`
and `CodexAdapter` are the reference implementations; `CustomCliAdapter`
(see `custom_cli.py`) is a generic, config-driven adapter for wiring up any
other CLI-based agent without writing Python at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ..wire.injection import WireInjection


def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a CLI subprocess and its whole process group (MCP stdio servers
    and other grandchildren included).

    Requires the process to have been spawned with start_new_session=True
    (so it owns its process group). A bare proc.kill() only kills the CLI
    itself — MCP servers it spawned get reparented to init as orphans.
    Returns silently if the process has already exited.
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

# An adapter's own network needs, as a tri-state rather than a bool: some
# agents only need to reach an already-running local/LAN service
# (local_service), while CLI agents like Claude Code / Codex call public
# model APIs (public_internet). These are different kinds of dependencies.
NetworkRequirement = Literal["none", "local_service", "public_internet"]


@dataclass(frozen=True)
class AdapterCapabilities:
    """Static capability declaration for an adapter.

    Declaration/display only: `execution_locus` is the authoritative value
    fed into `build_security_meta()`; `network_required` / `system_requires`
    are declared but not consumed by any runtime scheduling or gating.
    `system_requires` lists adapter-level host binary dependencies, distinct
    from env-level `meta.yaml prerequisites`.
    """

    execution_locus: Literal["host", "docker-sandbox", "remote-host"]
    network_required: NetworkRequirement
    system_requires: tuple[str, ...] = ()
    # Whether the adapter can submit an answer while the agent is waiting on
    # an interactive question (AskUserQuestion-style). All current adapters
    # are False (`claude -p` / `codex exec` expose no such channel);
    # dispatch fails fast on conversations containing answer_interaction
    # turns when this is False.
    interaction_answer: bool = False


def build_security_meta(
    *,
    execution_locus: str,
    permission_mode: str | None,
    workspace_root: str | None,
) -> dict[str, Any]:
    """Snapshot of how/where the agent actually ran.

    - execution_locus: e.g. "host", "docker-sandbox", "remote-host"
    - permission_mode: the literal approval/permission flag passed at launch
    - workspace_root: the directory boundary the agent was authorized to touch
    """
    return {
        "execution_locus": execution_locus,
        "permission_mode": permission_mode,
        "workspace_root": workspace_root,
    }


@dataclass
class McpServerSpec:
    """An MCP stdio entrypoint explicitly declared by a scenario's meta.yaml.

    Adapters translate this into whatever config shape the target CLI wants;
    they must never guess or synthesize a server from `env_name` alone.
    `cwd` is the directory the scenario's command should be resolved/run
    relative to.
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)


# ---------- Conversation turns (multi-turn attempts) ----------

# "interaction" is reserved for answer_interaction turns; the other four
# describe the experimental phase a turn belongs to.
TurnPurpose = Literal["setup", "pressure", "probe", "task", "interaction"]
TurnAction = Literal["send_message", "answer_interaction"]


@dataclass(frozen=True)
class InteractionWaitFor:
    """Describes the interaction request an answer_interaction turn answers.

    The driver matches the producer's interaction-request event by tool_name
    (plus question_key to disambiguate when needed); an interaction request
    that matches no declared turn is an *unexpected interaction* and fails
    the attempt rather than guessing an answer on the agent's behalf.
    """

    tool_name: str  # e.g. "builtin:AskUserQuestion"
    question_key: str | None = None


@dataclass(frozen=True)
class ConversationTurn:
    """One conversation turn of input.

    Two shapes, keyed by `action`: send_message requires `prompt`;
    answer_interaction requires `wait_for` + `answer` (a static answer the
    scenario author writes at task-definition time). Mutual-exclusion
    validation lives in backend.conversation.plan — this is just the data
    carrier.
    """

    turn_id: str
    turn_index: int
    action: TurnAction = "send_message"
    purpose: TurnPurpose = "task"
    score_after: bool = False
    prompt: str | None = None
    wait_for: InteractionWaitFor | None = None
    answer: dict[str, Any] | None = None


@dataclass
class AdapterRunInput:
    """Minimal input for a single attempt.

    Deliberately not the full backend.models dataclass/Pydantic model —
    adapters should not depend on backend's internal schema.

    `session_token` is plaintext, generated by the runner and handed to the
    adapter. Adapters that expose it to the agent's tool-calling environment
    (e.g. via env vars for an MCP server) must never persist it to any
    durable channel.
    """

    attempt_id: str
    task_id: str
    task_prompt: str
    task_context: dict[str, Any]
    # None means "unlimited": no time-budget notice is injected into the
    # prompt, and adapters must not enforce a wall-clock deadline via
    # asyncio.wait_for (an inactivity/no-output watchdog, if any, still
    # applies). This is the "no timeout" baseline for testing agents without
    # a time constraint.
    timeout_seconds: int | None
    env_name: str
    env_skill_id: str  # "lane/<env_name>"
    session_token: str  # plaintext, only used to authorize the MCP tool server
    env_base_url: str  # public address of the attempt server (agent-reachable)
    # Explicitly declared by the scenario's meta.yaml (`entrypoints.mcp`); an
    # empty tuple means the scenario provides no MCP server. Adapters must
    # not infer or fabricate a server from env_name.
    mcp_servers: tuple[McpServerSpec, ...] = ()
    # Wire observability injection (backend/wire/), merged by dispatch before
    # the adapter runs. Defaults to a no-op injection so adapters that don't
    # care about wire capture behave exactly as before it existed.
    wire_injection: WireInjection = field(default_factory=WireInjection)
    # Multi-turn conversation. An empty tuple means the historical
    # single-turn behavior: the adapter renders one message from
    # task_prompt + task_context, exactly as before this field existed.
    # Non-empty tuples are validated and consumed via
    # backend.conversation.plan.effective_conversation; see ConversationTurn
    # for the shapes of non-send_message turns.
    conversation_turns: tuple[ConversationTurn, ...] = ()


def _format_time_budget(seconds: int) -> str:
    """Format a second count into a natural-language duration for the agent
    (whole minutes preferred, falling back to minutes+seconds or bare
    seconds)."""
    if seconds % 60 == 0:
        return f"{seconds // 60} minute(s)"
    if seconds < 60:
        return f"{seconds} second(s)"
    return f"{seconds // 60} minute(s) {seconds % 60} second(s)"


def time_budget_notice(timeout_seconds: int | None) -> str | None:
    """Time-budget notice shared by every adapter, so the comparison stays
    fair regardless of which agent is under test.

    This exists to probe an agent's "capability per unit of time": tell it
    the total budget up front and nudge it to produce a submittable result
    quickly, then spend whatever's left iterating. `None` (unlimited) — or
    any non-positive value — returns `None`: no time constraint is injected,
    so the agent's behavior is identical to a build without this feature.
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        return None
    budget = _format_time_budget(int(timeout_seconds))
    return (
        f"You have a time budget of {budget} for this task. Plan accordingly: "
        "produce a submittable result as soon as possible, then use any "
        "remaining time to iterate and improve it. When the time is up the "
        "evaluation ends, so make sure your best result is already in place."
    )


def prompt_context(task_context: dict[str, Any]) -> dict[str, Any]:
    """Render task_context into what the agent actually sees.

    `uploaded_files` becomes a list of filenames under the agent's working
    directory (materials are already placed there by dispatch), never a host
    path. Shared across adapters so every agent gets the same prompt shape,
    keeping the comparison fair.
    """
    context = {
        k: v for k, v in task_context.items() if k != "uploaded_files" and not k.startswith("_")
    }
    uploaded = task_context.get("uploaded_files")
    if uploaded and isinstance(uploaded, list):
        names = [uf.get("name", "") for uf in uploaded if uf.get("name")]
        if names:
            context["input_files_in_workdir"] = names
    return context


@dataclass
class AdapterResult:
    attempt_id: str
    status: str
    external_refs: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    events_count: int = 0
    last_event_at: str | None = None
    thinking_count: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0
    security_meta: dict[str, Any] = field(default_factory=dict)
    # Multi-turn conversation summary. Single-turn/legacy attempts keep an
    # empty dict; multi-turn adapters fill it via
    # backend.conversation.summary.summarize_conversation.
    conversation_summary: dict[str, Any] = field(default_factory=dict)


class AgentAdapter(Protocol):
    @property
    def capabilities(self) -> AdapterCapabilities:
        """Static capability declaration: a class attribute or a simple
        property both work; no runtime negotiation is introduced."""
        ...

    async def run(
        self,
        task: AdapterRunInput,
        env: Any,
        data_path: Path,
    ) -> AdapterResult:
        """Run one attempt.

        - Must handle its own timeout/failure classification; never raise
          (wrap crashes as `error_code`+`error_message` with a terminal
          `status`).
        - Writes `events.jsonl` / `thinking.jsonl` to
          `<data_path>/attempts/{attempt_id}/`.
        - Must NOT write the env DB or trace.jsonl — that's the attempt
          server's job, triggered by the agent's own tool calls.
        """
        ...
