"""Single-turn conversation driver for declarative CLI profiles."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ...adapters.base import AdapterRunInput
from ...conversation.plan import effective_conversation
from ..launch import LaunchContext
from ..models import AgentSpec
from ..prompt import RenderedPrompt, render_task_prompt


class OneShotDriverError(ValueError):
    pass


@dataclass(frozen=True)
class OneShotPlan:
    turn_id: str
    turn_index: int
    prompt: RenderedPrompt
    launch_context: LaunchContext


class OneShotDriver:
    driver_id = "oneshot"
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
    ) -> OneShotPlan:
        if spec.capabilities.single_turn.state == "unsupported":
            raise OneShotDriverError(f"agent {spec.id!r} does not support single-turn execution")
        conversation = effective_conversation(task)
        if len(conversation.turns) != 1 or len(conversation.send_message_turns) != 1:
            raise OneShotDriverError(
                f"oneshot driver cannot execute {len(conversation.turns)} conversation turns"
            )
        turn = conversation.send_message_turns[0]
        assert turn.prompt is not None
        rendered = render_task_prompt(task, message=turn.prompt)
        prompt_file: Path | None = None
        if spec.prompt.mode == "driver-owned":
            raise OneShotDriverError("oneshot driver cannot use prompt mode 'driver-owned'")
        if spec.prompt.mode == "file" or spec.prompt.arg_fallback == "file":
            prompt_file = Path(attempt_private) / "prompt.txt"
            _atomic_private_write(prompt_file, rendered.text)
        context = LaunchContext(
            prompt=rendered.text,
            prompt_file=prompt_file,
            effective_model=effective_model,
            session_id=None,
            mcp_config_file=mcp_config_file,
            attempt_workspace=Path(attempt_workspace),
            project_path=Path(project_path),
            attempt_private=Path(attempt_private),
            options=options or {},
            mcp_shape=mcp_shape,
        )
        return OneShotPlan(
            turn_id=turn.turn_id,
            turn_index=turn.turn_index,
            prompt=rendered,
            launch_context=context,
        )


def _atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.chmod(temporary_name, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
