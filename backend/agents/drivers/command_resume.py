"""State-safe multi-turn command/resume driver for declarative profiles."""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from ...adapters.base import AdapterRunInput
from ...conversation.plan import effective_conversation
from ...conversation.turns import render_turn_prompt
from ..launch import LaunchContext, render_launch_plan
from ..models import AgentSpec
from ..prompt import RenderedPrompt, render_task_prompt
from .oneshot import _atomic_private_write


class CommandResumeDriverError(ValueError):
    pass


@dataclass(frozen=True)
class CommandTurnPlan:
    turn_id: str
    turn_index: int
    prompt: RenderedPrompt
    launch_context: LaunchContext


@dataclass(frozen=True)
class CommandResumePlan:
    spec: AgentSpec
    turns: tuple[CommandTurnPlan, ...]

    @property
    def first(self) -> CommandTurnPlan:
        return self.turns[0]

    def resolve_session(self, candidates: Sequence[str | None]) -> str:
        unique = tuple(
            dict.fromkeys(candidate.strip() for candidate in candidates if candidate and candidate.strip())
        )
        if not unique:
            raise CommandResumeDriverError("first turn did not yield an explicit session ID")
        if len(unique) != 1:
            raise CommandResumeDriverError(
                f"first turn yielded multiple session IDs ({len(unique)}); refusing implicit selection"
            )
        session_id = unique[0]
        if len(session_id.encode("utf-8")) > 512 or re.search(r"[\x00-\x1f\x7f]", session_id):
            raise CommandResumeDriverError("producer session ID is invalid or exceeds 512 bytes")
        return session_id

    def render_turn(self, turn_index: int, *, session_id: str | None = None):
        try:
            turn = self.turns[turn_index]
        except IndexError as exc:
            raise CommandResumeDriverError(f"unknown conversation turn index {turn_index}") from exc
        if turn_index == 0:
            if session_id is not None:
                raise CommandResumeDriverError("first turn cannot resume a preselected session")
            return render_launch_plan(self.spec, turn.launch_context)
        if not session_id:
            raise CommandResumeDriverError("resume turn requires the first turn's session ID")
        assert self.spec.launch is not None
        resume_spec = self.spec.model_copy(
            update={
                "launch": self.spec.launch.model_copy(update={"args": self.spec.driver.resume_args})
            }
        )
        return render_launch_plan(
            resume_spec,
            replace(turn.launch_context, session_id=session_id),
        )


class CommandResumeDriver:
    driver_id = "command-resume"
    driver_version = "1"

    def prepare(
        self,
        *,
        spec: AgentSpec,
        task: AdapterRunInput,
        attempt_workspace: Path,
        project_path: Path,
        attempt_private: Path,
        effective_model: str | None = None,
        options: Mapping[str, str | int | float | bool | None] | None = None,
        mcp_config_file: Path | None = None,
        mcp_shape: tuple[Mapping[str, object], ...] = (),
    ) -> CommandResumePlan:
        if spec.driver.kind != "command-resume":
            raise CommandResumeDriverError("profile does not select command-resume driver")
        if spec.capabilities.resume_send_message.state == "unsupported":
            raise CommandResumeDriverError(f"agent {spec.id!r} does not support resume")
        if spec.prompt.mode == "driver-owned":
            raise CommandResumeDriverError("command-resume cannot use driver-owned prompt mode")
        conversation = effective_conversation(task)
        if conversation.interaction_turns:
            raise CommandResumeDriverError(
                "command-resume does not implement interactive answer turns"
            )
        send_turns = conversation.send_message_turns
        if not send_turns:
            raise CommandResumeDriverError("command-resume requires at least one message turn")

        base = render_task_prompt(task)
        prepared: list[CommandTurnPlan] = []
        for turn in send_turns:
            text = render_turn_prompt(task, turn, base_prompt=base.text)
            encoded = text.encode("utf-8")
            rendered = RenderedPrompt(
                text=text,
                content_hash=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
                byte_count=len(encoded),
            )
            prompt_file = None
            if spec.prompt.mode == "file" or spec.prompt.arg_fallback == "file":
                prompt_file = Path(attempt_private) / "prompts" / f"turn-{turn.turn_index}.txt"
                _atomic_private_write(prompt_file, text)
            prepared.append(
                CommandTurnPlan(
                    turn_id=turn.turn_id,
                    turn_index=turn.turn_index,
                    prompt=rendered,
                    launch_context=LaunchContext(
                        prompt=text,
                        prompt_file=prompt_file,
                        effective_model=effective_model,
                        session_id=None,
                        mcp_config_file=mcp_config_file,
                        attempt_workspace=Path(attempt_workspace),
                        project_path=Path(project_path),
                        attempt_private=Path(attempt_private),
                        options=options or {},
                        mcp_shape=mcp_shape,
                    ),
                )
            )
        return CommandResumePlan(spec=spec, turns=tuple(prepared))
