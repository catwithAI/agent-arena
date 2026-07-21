"""CodexAdapter — drives Codex via the `codex` CLI subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..conversation.deadline import (
    ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS,
    AttemptBudgetExhausted,
    AttemptDeadline,
)
from ..conversation.plan import effective_conversation
from ..conversation.summary import summarize_conversation
from ..conversation.turns import (
    render_turn_prompt,
    with_turn_ext,
    write_checkpoint,
)
from ..conversation.writer import CONVERSATION_FILENAME, ConversationTraceWriter
from ..model_providers import ModelProviderSection, ModelRef, parse_model_ref, resolve_api_key
from ..wire.injection import WireInjection
from .base import (
    AdapterCapabilities,
    AdapterResult,
    AdapterRunInput,
    build_security_meta,
    kill_process_tree,
    prompt_context,
    time_budget_notice,
)

logger = logging.getLogger(__name__)


class CodexAdapter:
    # Static capability declaration: see the AdapterCapabilities docstring.
    capabilities = AdapterCapabilities(
        execution_locus="host",
        network_required="public_internet",
        # `codex exec` has no channel for answering interactive questions
        # mid-run: conversations containing answer_interaction turns are
        # rejected by dispatch before launch, never silently skipped.
        interaction_answer=False,
    )

    def __init__(
        self,
        *,
        model: str = "gpt-5",
        project_path: str | Path = ".",
        providers: dict[str, ModelProviderSection] | None = None,
    ) -> None:
        self.model = model
        self.project_path = str(Path(project_path).resolve())
        self.providers = providers or {}

    @property
    def wire_capture_capabilities(self) -> dict[str, Any]:
        """Wire injection consumption capability declaration (lifecycle uses
        this before the agent starts to filter/drop what this adapter can't
        consume). Codex has no static provider header channel, so
        llm_headers stays False."""
        return {
            "process_env": True,
            "llm_base_url": True,
            "llm_headers": False,
            "mcp_rewrites": True,
        }

    def _provider_cli_args(self, model_ref: ModelRef, injection: WireInjection) -> list[str]:
        """Provider-prefixed models are injected as a one-shot `-c` override
        naming a provider — never touches the user's global config.toml.

        Wire injection consumption point: injection.llm_base_url overrides
        this run's model_providers.<id>.base_url."""
        if model_ref.provider is None:
            return ["-m", model_ref.model]
        p = self.providers[model_ref.provider]
        name = model_ref.provider
        # Fail fast before the agent starts when the provider doesn't speak
        # the Responses protocol — Codex only supports responses wire_api;
        # chat would silently mismatch.
        if p.wire_api != "responses":
            raise ValueError(
                f"Codex does not support provider kind={p.kind!r} "
                f"(wire_api={p.wire_api!r}); Codex needs an openai-responses "
                "provider."
            )
        base_url = (
            injection.llm_base_url if injection.enabled and injection.llm_base_url else p.base_url
        )
        args = [
            "-c", f'model_providers.{name}.name="{name}"',
            "-c", f'model_providers.{name}.base_url="{base_url}"',
            "-c", f'model_providers.{name}.wire_api="{p.wire_api}"',
        ]
        if p.api_key_env:
            args += ["-c", f'model_providers.{name}.env_key="{p.api_key_env}"']
        # Capture token goes through Codex's env_http_headers mapping: the
        # X-Lane-Capture-Token header's value is read from the
        # LANE_WIRE_CAPTURE_TOKEN env var at request time, so the token never
        # appears on the command line (-c args).
        if injection.enabled and injection.capture_token:
            args += [
                "-c",
                'model_providers.'
                f'{name}.env_http_headers='
                '{ "X-Lane-Capture-Token" = "LANE_WIRE_CAPTURE_TOKEN" }',
            ]
        args += ["-c", f'model_provider="{name}"', "-m", model_ref.model]
        return args

    async def run(self, task: AdapterRunInput, env: Any, data_path: Path) -> AdapterResult:
        data_path = Path(data_path)
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        # Agent workspace (design: skill_workspace is the agent's sole world
        # boundary). cwd (-C) is set here, so agent submissions land here —
        # the attempt root is reserved for the framework's own runtime
        # metadata (events/thinking/wire/isolated home) and never mixed with
        # agent output. Defensive mkdir in case nothing has staged env
        # materials into it yet.
        workspace = attempt_dir / "skill_workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"
        final_message_path = attempt_dir / "codex_final.txt"

        cli_path = shutil.which("codex")
        if not cli_path:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="codex_not_in_path",
                error_message="codex CLI not found in PATH",
            )

        prompt = self._render_prompt(task)
        model_ref = parse_model_ref(self.model, self.providers)
        # Non-Responses providers fail fast before the agent starts, with an
        # explicit terminal instead of a ValueError surfacing as
        # adapter_crashed.
        try:
            provider_args = self._provider_cli_args(model_ref, task.wire_injection)
        except ValueError as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="provider_protocol_unsupported",
                error_message=str(exc),
            )
        # Conversation plan: a single-turn task maps to one legacy turn whose
        # argv matches the pre-conversation behavior (--ephemeral included);
        # multi-turn opens a thread on the first turn and resumes it after.
        plan = effective_conversation(task)
        # Authoritative thread ID, read from the first turn's thread.started
        # event. Never self-derived — Codex generates thread IDs; they can
        # only be read, not chosen.
        codex_thread_id: str | None = None
        # Multi-turn checkpoint: persist the thread ID the moment we have it,
        # so a crash right after doesn't lose the only credential that can
        # recover this attempt's server-side session.
        #
        # Persistence only for now, no recovery execution: the startup
        # recovery path (resuming unfinished turns from a checkpoint) is not
        # implemented yet. Writing the checkpoint now means no data gap when
        # recovery lands.
        checkpoint_state: dict[str, Any] = {
            "codex_cli_path": cli_path,
            "conversation_plan_hash": plan.plan_hash if not plan.is_legacy else None,
            "conversation_turn_count": len(plan.turns) if not plan.is_legacy else None,
            "codex_thread_id": None,
            "last_completed_turn_index": None,
            "active_turn_index": None,
            "recoverability": "checkpoint-only",
        }

        def _checkpoint(**updates: Any) -> None:
            """Update and persist the checkpoint (thread ID / turn progress).

            Only declared keys may be updated — with bare `**kwargs` a typo'd
            key silently adds a junk field instead of updating the target
            (seen in practice: `thread_id=` instead of `codex_thread_id=`
            left the checkpointed ID permanently None).
            """
            unknown = set(updates) - set(checkpoint_state)
            if unknown:
                raise KeyError(f"unknown checkpoint fields: {sorted(unknown)}")
            checkpoint_state.update(updates)
            write_checkpoint(attempt_dir, checkpoint_state)

        def _common_args(*, with_workspace: bool = True) -> list[str]:
            """Args identical for every turn (provider / workspace / MCP).

            `with_workspace=False` is for `exec resume` — that subcommand
            does not accept `-C` (codex-cli 0.144.5 errors with "unexpected
            argument '-C'"). The working directory is still guaranteed by
            the subprocess's cwd=workspace, so dropping the flag doesn't
            change the agent's actual workspace.
            """
            args = [
                "--json",
                "--skip-git-repo-check",
                "--ignore-rules",
                "--dangerously-bypass-approvals-and-sandbox",
                *provider_args,
            ]
            if with_workspace:
                args += ["-C", str(workspace.resolve())]
            args += ["-o", str(final_message_path.resolve())]
            for spec in task.mcp_servers:
                mcp_command, mcp_args = self._mcp_command_and_args(task, spec)
                args += [
                    "-c", f"mcp_servers.{spec.name}.command="
                    f"{json.dumps(mcp_command, ensure_ascii=True)}",
                    "-c", f"mcp_servers.{spec.name}.args="
                    f"{json.dumps(mcp_args, ensure_ascii=True)}",
                ]
                if spec.cwd:
                    args += [
                        "-c", f"mcp_servers.{spec.name}.cwd="
                        f"{json.dumps(spec.cwd, ensure_ascii=True)}",
                    ]
            return args

        def _build_cmd(turn: Any, *, is_first: bool) -> list[str]:
            """argv for one turn.

            A single turn keeps `--ephemeral` (no session record, matching
            the pre-conversation behavior); multi-turn MUST drop it — an
            ephemeral session isn't persisted and therefore can't be
            resumed. Later turns use `codex exec resume <thread_id>
            <prompt>`, never `--last`: that picks "the most recently
            recorded session", which resumes someone else's thread when
            several attempts run concurrently.
            """
            turn_prompt = render_turn_prompt(task, turn, base_prompt=prompt)
            if plan.is_legacy:
                # Single turn: re-insert --ephemeral right after
                # --skip-git-repo-check so the argv matches the
                # pre-conversation order position for position.
                args = _common_args()
                args.insert(args.index("--skip-git-repo-check") + 1, "--ephemeral")
                return [cli_path, "exec", *args, turn_prompt]
            if is_first:
                return [cli_path, "exec", *_common_args(), turn_prompt]
            if not codex_thread_id:
                raise RuntimeError(
                    "multi-turn Codex resume is missing a thread ID: the "
                    "first turn never emitted a thread.started event"
                )
            return [
                cli_path, "exec", "resume", codex_thread_id,
                *_common_args(with_workspace=False), turn_prompt,
            ]

        if task.mcp_servers:
            self._write_mcp_config_snapshot(task, attempt_dir)

        events_count = 0
        thinking_count = 0
        total_input_tokens = 0
        total_output_tokens = 0
        last_event_at: str | None = None
        error_message: str | None = None
        turn_failed_message: str | None = None
        started_at = datetime.now(timezone.utc)
        proc = None

        # CODEX_HOME isolation: point Codex's config/state at a clean,
        # per-attempt directory instead of the host's global ~/.codex
        # (config.toml, skills, plugins, memories, history), so a benchmark
        # run never picks up whoever's operating this box's private setup.
        # Codex's own built-in tools are not disabled by this.
        iso_codex_home = attempt_dir / ".codex-iso-home"
        iso_codex_home.mkdir(parents=True, exist_ok=True)
        subprocess_env = {
            **os.environ,
            "CODEX_HOME": str(iso_codex_home.resolve()),
        }
        # Codex's MCP config is passed via argv (-c), which is visible to
        # anything reading the process list — attempt credentials must never
        # go there. Only when the scenario actually provides an MCP server
        # do we hand the credentials to the subprocess env, so its stdio MCP
        # child (which Codex spawns inheriting its own env) can read them.
        if task.mcp_servers:
            subprocess_env.update({
                "LANE_ATTEMPT_ID": task.attempt_id,
                "LANE_SESSION_TOKEN": task.session_token,
                "LANE_BASE_URL": task.env_base_url,
            })
        if model_ref.provider is not None:
            provider = self.providers[model_ref.provider]
            api_key = resolve_api_key(provider)
            # Without this, codex only reports "Missing environment variable"
            # inside turn.failed — fail fast with something actionable and
            # skip spawning the subprocess entirely.
            if provider.api_key_env and api_key is None:
                return AdapterResult(
                    attempt_id=task.attempt_id,
                    status="auth_failed",
                    error_code="provider_api_key_missing",
                    error_message=(
                        f"provider {model_ref.provider!r} is missing an API key: "
                        f"env var {provider.api_key_env} is not set, and "
                        f"arena.yaml model_providers.{model_ref.provider}.api_key "
                        "is also empty"
                    ),
                )
            if provider.api_key_env and api_key:
                subprocess_env[provider.api_key_env] = api_key

        # wire injection consumption point (process_env + capture token);
        # base_url/mcp rewrites are consumed above via _provider_cli_args /
        # _mcp_command_and_args.
        if task.wire_injection.enabled:
            subprocess_env.update(task.wire_injection.process_env)
            if task.wire_injection.capture_token:
                subprocess_env["LANE_WIRE_CAPTURE_TOKEN"] = task.wire_injection.capture_token

        # One attempt-level deadline shared across all turns. For a single
        # turn this is equivalent to the previous
        # wait_for(timeout=task.timeout_seconds).
        deadline = AttemptDeadline(task.timeout_seconds)
        conversation_trace = ConversationTraceWriter(
            attempt_dir / CONVERSATION_FILENAME, attempt_id=task.attempt_id,
        )
        conversation_trace.conversation_started(
            turn_count=len(plan.turns), is_legacy=plan.is_legacy,
            score_turn_id=plan.score_turn.turn_id,
        )
        current_turn_id: str | None = None
        current_turn_index: int | None = None

        try:
            async def _run_turn(turn: Any, *, is_first: bool) -> None:
                """Run one turn: spawn a codex exec subprocess and consume
                its stdout to completion."""
                nonlocal proc, events_count, thinking_count, last_event_at
                nonlocal total_input_tokens, total_output_tokens
                nonlocal turn_failed_message, error_message, codex_thread_id

                # turn.failed is per-turn state: a previous turn's failure
                # message must not leak into this turn's verdict.
                turn_failed_message = None
                # Attempt-level running totals at the start of this turn.
                # Codex usage semantics: each `codex exec` process
                # accumulates internally, and processes are independent of
                # each other (measured: two turns report 7336 / 14696, not
                # 7336 / 22032). So across turns it must be "turn baseline +
                # this turn's value" — a plain max would let a later turn
                # overwrite the earlier turns' usage.
                turn_base_input = total_input_tokens
                turn_base_output = total_output_tokens
                proc = await asyncio.create_subprocess_exec(
                    *_build_cmd(turn, is_first=is_first),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workspace),
                    limit=10 * 1024 * 1024,
                    env=subprocess_env,
                    # Give the CLI its own process group so a timeout/cancel
                    # can kill it together with any MCP servers it spawned.
                    start_new_session=True,
                )
                turn_proc = proc

                async def _consume() -> None:
                    nonlocal events_count, thinking_count, last_event_at
                    nonlocal total_input_tokens, total_output_tokens
                    nonlocal turn_failed_message, codex_thread_id, error_message
                    assert turn_proc.stdout is not None
                    async for raw_line in turn_proc.stdout:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        ts = _now_iso()
                        last_event_at = ts
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            _append_jsonl(events_path, with_turn_ext(
                                {"timestamp": ts, "raw_line": line},
                                current_turn_id, current_turn_index,
                            ))
                            events_count += 1
                            continue

                        _append_jsonl(events_path, with_turn_ext(
                            {"timestamp": ts, **data},
                            current_turn_id, current_turn_index,
                        ))
                        events_count += 1

                        # Authoritative thread ID: only ever read from
                        # producer events, never self-derived — every later
                        # resume depends on it.
                        if data.get("type") == "thread.started":
                            tid = data.get("thread_id")
                            if isinstance(tid, str) and tid:
                                if codex_thread_id and tid != codex_thread_id:
                                    # A resume that lands on a *different*
                                    # thread means the context chain broke
                                    # (session_continuity_broken). Logging a
                                    # warning and overwriting would silently
                                    # switch the attempt to the new thread,
                                    # keep resuming it, and score against a
                                    # broken context — set the error now and
                                    # let the end-of-turn status terminate
                                    # the conversation.
                                    session_broken = (
                                        f"session_continuity_broken: expected thread "
                                        f"{codex_thread_id}, resume returned {tid}"
                                    )
                                    logger.error(
                                        "codex attempt=%s %s",
                                        task.attempt_id, session_broken,
                                    )
                                    if not error_message:
                                        error_message = session_broken
                                    # Keep the *original* thread ID: it is
                                    # this attempt's identity and must not be
                                    # overwritten by an unexpected new one.
                                else:
                                    codex_thread_id = tid
                                    _checkpoint(codex_thread_id=tid)

                        # turn.failed is codex's authoritative failure event
                        # (e.g. an upstream 404). The last stderr line is
                        # often just a routine notice ("Reading additional
                        # input from stdin...") — using it as error_message
                        # would completely mislead debugging.
                        if data.get("type") == "turn.failed":
                            err = data.get("error")
                            msg = err.get("message") if isinstance(err, dict) else None
                            if isinstance(msg, str) and msg:
                                turn_failed_message = msg

                        text = _message_text(data)
                        if text and _looks_like_reasoning(data):
                            thinking_count += 1
                            _append_jsonl(thinking_path, {
                                "timestamp": ts,
                                "sequence": thinking_count,
                                "content": text,
                                "type": "thinking",
                            })
                        usage = _usage(data)
                        # max within a turn (a process's usage events are
                        # increasing snapshots), baseline-added across turns
                        # (processes are independent, see turn_base above).
                        total_input_tokens = max(
                            total_input_tokens, turn_base_input + usage[0],
                        )
                        total_output_tokens = max(
                            total_output_tokens, turn_base_output + usage[1],
                        )

                try:
                    await asyncio.wait_for(_consume(), timeout=deadline.remaining())
                    await asyncio.wait_for(turn_proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    error_message = f"timeout after {task.timeout_seconds}s"
                finally:
                    # Timeouts, cancellation (/runs/{id}/stop, backend
                    # shutdown) and unexpected exceptions must all terminate
                    # the CLI process group: CancelledError is not caught by
                    # the except Exception below, and without this finally
                    # the subprocess would keep burning tokens as an orphan
                    # until the budget cap.
                    if turn_proc.returncode is None:
                        kill_process_tree(turn_proc)
                        await turn_proc.wait()

                # Error message precedence: timeout (already set) >
                # authoritative turn.failed event > stderr tail.
                if not error_message and turn_failed_message:
                    error_message = turn_failed_message[:500]

                if turn_proc.stderr:
                    stderr = (
                        await turn_proc.stderr.read()
                    ).decode("utf-8", errors="replace").strip()
                    if stderr:
                        (attempt_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
                        if (
                            not error_message
                            and turn_proc.returncode
                            and turn_proc.returncode != 0
                        ):
                            error_message = stderr[:500]

            for index, turn in enumerate(plan.send_message_turns):
                current_turn_id = None if plan.is_legacy else turn.turn_id
                current_turn_index = None if plan.is_legacy else turn.turn_index
                try:
                    deadline.check_before_turn()
                except AttemptBudgetExhausted:
                    # Budget exhausted between turns: don't spawn the next
                    # process.
                    error_message = (
                        error_message or f"timeout after {task.timeout_seconds}s"
                    )
                    conversation_trace.turn_failed(
                        turn, producer_session_id=codex_thread_id,
                        error_code=ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS,
                        error_summary=error_message,
                    )
                    break
                conversation_trace.turn_started(
                    turn, producer_session_id=codex_thread_id,
                )
                if not plan.is_legacy:
                    _checkpoint(active_turn_index=turn.turn_index)
                await _run_turn(turn, is_first=(index == 0))
                # Any failed turn terminates the conversation; later turns
                # are never sent.
                turn_status = _classify_outcome(
                    proc.returncode if proc is not None else None, error_message,
                )
                if turn_status != "completed":
                    if not error_message:
                        error_message = (
                            f"turn {turn.turn_id!r} did not complete "
                            f"(status={turn_status})"
                        )
                    conversation_trace.turn_failed(
                        turn, producer_session_id=codex_thread_id,
                        error_code=(
                            "session_continuity_broken"
                            if "session_continuity_broken" in error_message
                            else None
                        ),
                        error_summary=error_message,
                    )
                    break
                conversation_trace.turn_completed(
                    turn, producer_session_id=codex_thread_id,
                )
                if not plan.is_legacy:
                    # last_completed is only written after turn.completed
                    # (it is the sole checkpoint that proves the turn was
                    # both sent and finished).
                    _checkpoint(
                        active_turn_index=None,
                        last_completed_turn_index=turn.turn_index,
                    )
            else:
                conversation_trace.conversation_completed()
            if error_message:
                conversation_trace.conversation_failed(
                    error_code=None, error_summary=error_message,
                )
            conversation_trace.close()

        except FileNotFoundError:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="codex_exec_failed",
                error_message="failed to execute codex CLI",
            )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="unexpected_error",
                error_message=str(exc),
            )

        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        status = _classify_outcome(proc.returncode if proc else None, error_message)
        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                "cli_path": cli_path,
                "model_used": self.model,
                # Authoritative thread ID (from thread.started). Multi-turn
                # resume uses it; a single --ephemeral turn records no
                # session, so it's usually None.
                "codex_thread_id": codex_thread_id,
            },
            error_code=(
                None if status == "completed"
                # A broken session gets its own code: it's not an ordinary
                # cli_error — evaluation/UI must distinguish "the agent
                # crashed" from "the context broke, the conclusion is
                # unusable".
                else "session_continuity_broken"
                if error_message and "session_continuity_broken" in error_message
                else (error_message or "cli_error")
            ),
            error_message=error_message,
            events_count=events_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            token_usage={"input_tokens": total_input_tokens, "output_tokens": total_output_tokens},
            duration_ms=duration_ms,
            conversation_summary=summarize_conversation(attempt_dir),
            security_meta=build_security_meta(
                execution_locus=self.capabilities.execution_locus,
                permission_mode="--dangerously-bypass-approvals-and-sandbox",
                workspace_root=str(workspace.resolve()),
            ),
        )

    def _mcp_command_and_args(
        self, task: AdapterRunInput, spec: Any
    ) -> tuple[str, list[str]]:
        """Final command/args for a declared MCP server; the wire mcp
        rewrite hook is applied here so it wraps only the server the
        scenario declared."""
        command = spec.command
        args = list(spec.args)
        rewrite = task.wire_injection.mcp_rewrites.get(spec.name)
        if task.wire_injection.enabled and rewrite is not None:
            args = [*rewrite.args_prefix, command, *args]
            command = rewrite.command
        return command, args

    def _write_mcp_config_snapshot(self, task: AdapterRunInput, attempt_dir: Path) -> None:
        servers: dict[str, Any] = {}
        for spec in task.mcp_servers:
            command, args = self._mcp_command_and_args(task, spec)
            server: dict[str, Any] = {
                "command": command,
                "args": args,
                "env": {
                    "LANE_ATTEMPT_ID": task.attempt_id,
                    "LANE_SESSION_TOKEN": task.session_token,
                    "LANE_BASE_URL": task.env_base_url,
                },
            }
            if spec.cwd:
                server["cwd"] = spec.cwd
            servers[spec.name] = server
        config = {"mcp_servers": servers}
        (attempt_dir / "codex_mcp_config.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _render_prompt(self, task: AdapterRunInput) -> str:
        # The adapter does not prescribe a solving method — MCP is only one
        # option among whatever the agent brings natively.
        parts: list[str] = []
        # `codex exec`'s PROMPT argument is the only instructions channel —
        # there's no separate system-prompt slot like Claude Code's
        # --append-system-prompt — so the time budget (a framework-level
        # constraint, not part of the task) is placed at the very top of the
        # message instead. None (unlimited) yields no notice at all.
        notice = time_budget_notice(task.timeout_seconds)
        if notice:
            parts += [notice, ""]
        parts.append(task.task_prompt)
        context = prompt_context(task.task_context) if task.task_context else {}
        if context:
            parts.append("")
            parts.append("Context:")
            parts.append(json.dumps(context, ensure_ascii=False, indent=2))
        return "\n".join(parts)


def _classify_outcome(returncode: int | None, error_message: str | None) -> str:
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    # error_message takes precedence over the returncode: codex's
    # authoritative failure event is `turn.failed` on stdout (e.g. an
    # upstream 404), and the process may still exit 0 in that case — the
    # error was fully expressed in the JSON events. Checking only the
    # returncode would classify such an attempt as completed and silently
    # swallow the failure (and, multi-turn, keep sending later turns).
    if error_message:
        return "cli_error"
    if returncode == 0:
        return "completed"
    return "cli_error"


def _usage(data: dict[str, Any]) -> tuple[int, int]:
    usage = data.get("usage") or data.get("token_usage") or {}
    if isinstance(usage, dict):
        return int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
    return 0, 0


def _message_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "\n".join(filter(None, (_message_text(x) for x in data)))
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("text"), str):
        return data["text"]
    if isinstance(data.get("content"), str):
        return data["content"]
    return "\n".join(filter(None, (_message_text(v) for v in data.values())))


def _looks_like_reasoning(data: dict[str, Any]) -> bool:
    text = json.dumps(data, ensure_ascii=False).lower()
    return "reasoning" in text or "thinking" in text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
