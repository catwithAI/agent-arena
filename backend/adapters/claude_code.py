"""ClaudeCodeAdapter — drives Claude Code via the `claude` CLI subprocess.

Flow:
1. If the scenario declares an MCP server (`entrypoints.mcp` in its
   meta.yaml), write a temporary MCP config file for it (env vars carry
   attempt_id / session token / base_url so the env's MCP server can call
   back into the attempt server). Scenarios without a declared MCP server
   get no `--mcp-config` at all — Claude Code's native tools (WebSearch,
   Task, skills, slash commands, ...) are left untouched either way.
2. Spawn `claude -p "{prompt}" --output-format stream-json --verbose
   [--mcp-config {config}]` — once for a legacy single-turn task, or once
   per send_message turn for a multi-turn conversation (first turn
   `--session-id`, later turns `--resume` the same deterministic ID).
3. Parse stdout JSONL line by line, collecting thinking blocks / usage /
   events as they arrive.

stdout stream-json shapes:
- type=system subtype=init: session bootstrap info.
- type=assistant: one LLM turn; message.content[] holds text/thinking/
  tool_use/tool_result blocks.
- type=result: final summary, including usage / total_cost_usd / num_turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
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
from ..conversation.turns import render_turn_prompt, with_turn_ext
from ..conversation.writer import CONVERSATION_FILENAME, ConversationTraceWriter
from ..model_providers import ModelProviderSection, parse_model_ref, resolve_api_key
from .base import (
    AdapterCapabilities,
    AdapterResult,
    AdapterRunInput,
    build_security_meta,
    kill_process_tree,
    prompt_context,
    time_budget_notice,
)
from .token_usage import (
    estimate_tokens_from_event,
    result_usage_tokens,
    usage_input_tokens,
    usage_output_tokens,
)

logger = logging.getLogger(__name__)

# Deterministic namespace for attempt_id → CC session UUID: recovery and
# rebuild can recompute it, no random values. Fixed constant — changing it
# would change the session IDs of historical attempts.
_CC_SESSION_NAMESPACE = uuid.UUID("6f9d3a4e-6d0f-4a1e-9a5a-2f6b1c8d7e30")


def _clear_stale_cc_session(iso_home: Path, session_id: str) -> None:
    """Remove leftover records for this session ID inside the isolated home
    (same-attempt reruns).

    Only deletes exact-filename matches inside the attempt's own iso_home —
    never recurses into other sessions, and the host's global ~/.claude is
    out of scope entirely. Silently returns when nothing matches.
    """
    claude_dir = iso_home / ".claude"
    if not claude_dir.is_dir():
        return
    for path in claude_dir.rglob(f"{session_id}.jsonl"):
        try:
            path.unlink()
            logger.info("cleared stale CC session file from a previous run: %s", path)
        except OSError as exc:  # pragma: no cover - permission/race fallback
            logger.warning("failed to clear CC session file %s: %s", path, exc)


class ClaudeCodeAdapter:
    # Static capability declaration: execution_locus is the authoritative
    # input to build_security_meta; network_required/system_requires are
    # declared but not consumed.
    capabilities = AdapterCapabilities(
        execution_locus="host",
        network_required="public_internet",
        # `claude -p` has no channel for answering interactive questions
        # mid-run: conversations containing answer_interaction turns are
        # rejected by dispatch before launch, never silently skipped.
        interaction_answer=False,
    )

    def __init__(
        self,
        *,
        model: str = "sonnet",
        max_budget_usd: float = 5.0,
        project_path: str | Path = ".",
        providers: dict[str, ModelProviderSection] | None = None,
    ) -> None:
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.project_path = str(Path(project_path).resolve())
        self.providers = providers or {}

    async def run(self, task: AdapterRunInput, env: Any, data_path: Path) -> AdapterResult:
        data_path = Path(data_path)
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        # Agent workspace (design: skill_workspace is the agent's sole world
        # boundary). cwd is set here, so agent submissions land here — the
        # attempt root is reserved for the framework's own runtime metadata
        # (events/thinking/wire/isolated home) and never mixed with agent
        # output. Defensive mkdir in case nothing has staged env materials
        # into it yet.
        workspace = attempt_dir / "skill_workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"

        cli_path = shutil.which("claude")
        if not cli_path:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="claude_not_in_path",
                error_message="claude CLI not found in PATH",
            )

        mcp_config_path = self._write_mcp_config(task, attempt_dir)
        prompt = self._render_prompt(task)
        # Time budget (probes capability-per-unit-time) goes through Claude
        # Code's native --append-system-prompt: it's a framework-level
        # constraint, not part of the task itself, so it belongs in the
        # system channel rather than the user prompt. None (unlimited)
        # yields None from time_budget_notice, so nothing is injected.
        budget_notice = time_budget_notice(task.timeout_seconds)
        # Provider-prefixed models point the subprocess at a third-party
        # endpoint via its own env, leaving global settings.json untouched so
        # concurrent sessions don't interfere with each other. `--model` gets
        # the model name with the provider prefix stripped (the CLI doesn't
        # understand "<provider>/" prefixes).
        #
        # Which auth env var to use is decided by provider.auth_mode:
        # "bearer" (default) uses ANTHROPIC_AUTH_TOKEN (sent as
        # `Authorization: Bearer` — many gateways only accept Bearer),
        # "api-key" uses ANTHROPIC_API_KEY (sent as `x-api-key`). The two map
        # to different HTTP auth schemes and are injected mutually
        # exclusively, never both.
        model_ref = parse_model_ref(self.model, self.providers)
        subprocess_env = {**os.environ}
        # Local-state isolation: point CLAUDE_CONFIG_DIR / HOME at a clean,
        # empty directory inside the attempt so Claude Code never reads the
        # host's global ~/.claude (skills/plugins/MCP/memory/CLAUDE.md/
        # settings) — that would leak whoever runs this benchmark's personal
        # config into the result. One directory per attempt, so concurrent
        # attempts never interfere with each other. This does not disable any
        # of Claude Code's own built-in capabilities (WebSearch, Task,
        # skills, slash commands, ...) — only the host's private state is
        # kept out.
        iso_home = attempt_dir / ".cc-iso-home"
        (iso_home / ".claude").mkdir(parents=True, exist_ok=True)
        subprocess_env["CLAUDE_CONFIG_DIR"] = str((iso_home / ".claude").resolve())
        subprocess_env["HOME"] = str(iso_home.resolve())
        if model_ref.provider is not None:
            provider = self.providers[model_ref.provider]
            subprocess_env["ANTHROPIC_BASE_URL"] = provider.base_url
            api_key = resolve_api_key(provider)
            # Without this, a missing key just makes the CLI print an
            # unhelpful "Not logged in · Please run /login" — fail fast with
            # something actionable instead.
            if api_key is None and provider.api_key_env:
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
            if api_key:
                if provider.effective_auth_mode() == "api-key":
                    subprocess_env["ANTHROPIC_API_KEY"] = api_key
                    subprocess_env.pop("ANTHROPIC_AUTH_TOKEN", None)
                else:
                    subprocess_env["ANTHROPIC_AUTH_TOKEN"] = api_key
                    subprocess_env.pop("ANTHROPIC_API_KEY", None)
            if provider.custom_headers:
                subprocess_env["ANTHROPIC_CUSTOM_HEADERS"] = provider.custom_headers
        # wire injection consumption point: after subprocess_env is built,
        # before the subprocess is spawned. Merge/validation already happened
        # in lifecycle (reserved names, secrets) -- this only consumes.
        wi = task.wire_injection
        if wi.enabled:
            subprocess_env.update(wi.process_env)
            if wi.llm_base_url:
                subprocess_env["ANTHROPIC_BASE_URL"] = wi.llm_base_url
            extra_headers = dict(wi.llm_headers) if wi.llm_headers else {}
            # Capture token must land in a real HTTP header -- the CLI won't
            # turn an env var into a header on its own, only what's in
            # ANTHROPIC_CUSTOM_HEADERS gets sent with the request to the
            # (proxied) base URL. Uses its own header name, not Authorization
            # (that's occupied by provider auth and the proxy strips it).
            if wi.capture_token:
                extra_headers["X-Lane-Capture-Token"] = wi.capture_token
                subprocess_env["LANE_WIRE_CAPTURE_TOKEN"] = wi.capture_token
            if extra_headers:
                subprocess_env["ANTHROPIC_CUSTOM_HEADERS"] = _merge_custom_headers(
                    subprocess_env.get("ANTHROPIC_CUSTOM_HEADERS"), extra_headers
                )
        # Conversation plan: a single-turn task maps to one legacy turn whose
        # argv is byte-identical to the pre-conversation behavior; multi-turn
        # opens a session with --session-id on the first turn and --resume
        # on the same ID afterwards.
        plan = effective_conversation(task)
        # Deterministic UUID derived from the attempt: recomputable during
        # recovery/rebuild, and the CLI requires standard UUID shape (uuid5
        # guarantees that).
        cc_session_id = str(uuid.uuid5(_CC_SESSION_NAMESPACE, task.attempt_id))
        if not plan.is_legacy:
            # The session ID is derived from attempt_id, so a rerun of the
            # same attempt (failure retry, recovery, manual replay) collides
            # with the record the previous run left in the isolated home and
            # the CLI aborts with "Session ID ... is already in use" without
            # executing anything. The isolated home is attempt-private, so a
            # leftover with this ID can only come from this attempt's
            # previous run — clearing it never touches other attempts or the
            # host's sessions.
            _clear_stale_cc_session(iso_home, cc_session_id)

        def _build_cmd(turn: Any, *, is_first: bool) -> list[str]:
            """argv for one turn.

            The first turn pins the session ID explicitly and later turns
            resume that same ID — never `--continue` (it picks "the most
            recent session", which crosses over to another attempt's session
            when several run concurrently).
            """
            turn_prompt = render_turn_prompt(task, turn, base_prompt=prompt)
            cmd = [
                cli_path,
                "-p", turn_prompt,
                "--output-format", "stream-json",
                "--verbose",
                "--model", model_ref.model,
                "--max-budget-usd", str(self.max_budget_usd),
                "--dangerously-skip-permissions",
                # Required for sub-agent observability: without this flag the
                # text/thinking/usage events of sub-agents spawned via the
                # Task tool never reach stream-json, so sub-agent compaction
                # can't be assessed. Needs CLI >= 2.1.215.
                "--forward-subagent-text",
            ]
            if plan.is_legacy:
                # Single turn: keep the pre-conversation argv, no session
                # arguments (backward compatibility).
                pass
            elif is_first:
                cmd += ["--session-id", cc_session_id]
            else:
                cmd += ["--resume", cc_session_id]
            # Isolating HOME only strips the host operator's private config;
            # it does not disable Claude Code's native tools. Only append an
            # MCP config when the scenario actually declared one.
            if mcp_config_path is not None:
                cmd += ["--mcp-config", str(mcp_config_path.resolve())]
            # The time budget is an attempt-level framework constraint,
            # injected on the first turn only — repeating "you have X
            # minutes" on later turns would mislead the agent.
            if budget_notice and is_first:
                cmd += ["--append-system-prompt", budget_notice]
            return cmd

        events_count = 0
        thinking_count = 0
        total_input_tokens = 0
        total_output_tokens = 0
        estimated_input_tokens = 0
        estimated_output_tokens = 0
        last_event_at: str | None = None
        final_result: dict | None = None
        error_message: str | None = None
        model_used: str | None = None
        started_at = datetime.now(timezone.utc)

        # One attempt-level deadline shared across all turns — a turn does
        # not get a fresh full budget. For a single turn this is equivalent
        # to the previous wait_for(timeout=task.timeout_seconds).
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
        proc: asyncio.subprocess.Process | None = None

        try:
            async def _run_turn(turn: Any, *, is_first: bool) -> None:
                """Run one turn: spawn a claude -p subprocess and consume its
                stdout to completion."""
                nonlocal proc, events_count, thinking_count, last_event_at
                nonlocal total_input_tokens, total_output_tokens, final_result
                nonlocal estimated_input_tokens, estimated_output_tokens
                nonlocal model_used, error_message

                # Running totals at the start of this turn: the result event
                # recomputes from this baseline, so the per-assistant-event
                # accumulation and the authoritative result usage never get
                # double-counted.
                turn_base_input = total_input_tokens
                turn_base_output = total_output_tokens
                # final_result is per-turn state and must be reset each turn:
                # otherwise a turn that exits non-zero without emitting a
                # result event (and with empty stderr) would let
                # _classify_outcome read the *previous* turn's success result
                # and return completed before checking the returncode — a
                # failed turn misjudged as success, and later turns keep
                # running.
                final_result = None

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
                    nonlocal total_input_tokens, total_output_tokens, final_result
                    nonlocal estimated_input_tokens, estimated_output_tokens
                    nonlocal model_used
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
                        est_input, est_output = estimate_tokens_from_event(data)
                        estimated_input_tokens += est_input
                        estimated_output_tokens += est_output

                        msg_type = data.get("type")

                        if msg_type == "system" and data.get("subtype") == "init":
                            model_used = data.get("model") or model_used

                        if msg_type == "assistant":
                            message = data.get("message", {})
                            model_used = message.get("model") or model_used
                            for block in message.get("content", []):
                                if block.get("type") == "thinking":
                                    thinking_count += 1
                                    _append_jsonl(
                                        thinking_path,
                                        {
                                            "timestamp": ts,
                                            "sequence": thinking_count,
                                            "content": block.get("thinking", ""),
                                            "type": "thinking",
                                        },
                                    )
                            usage = message.get("usage", {})
                            total_input_tokens += usage_input_tokens(usage)
                            total_output_tokens += usage_output_tokens(usage)

                        elif msg_type == "result":
                            final_result = data
                            result_input, result_output = result_usage_tokens(data)
                            # The result event's usage is the authoritative
                            # total *for this turn*; it replaces the estimate
                            # accumulated from this turn's assistant events
                            # (for a single turn this is exactly the previous
                            # assignment semantics). With multiple turns it
                            # must be recomputed as turn baseline + this
                            # turn's authoritative value, otherwise the two
                            # accounting styles stack and double-count.
                            if result_input:
                                total_input_tokens = turn_base_input + result_input
                            if result_output:
                                total_output_tokens = turn_base_output + result_output

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

                if turn_proc.stderr:
                    stderr_bytes = await turn_proc.stderr.read()
                    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                    if stderr_text:
                        (attempt_dir / "stderr.txt").write_text(stderr_text, encoding="utf-8")
                        if (
                            not error_message
                            and turn_proc.returncode
                            and turn_proc.returncode != 0
                        ):
                            error_message = stderr_text[:500]

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
                        turn, producer_session_id=cc_session_id,
                        error_code=ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS,
                        error_summary=error_message,
                    )
                    break
                conversation_trace.turn_started(
                    turn, producer_session_id=cc_session_id,
                )
                await _run_turn(turn, is_first=(index == 0))
                # Any failed turn terminates the conversation; later turns
                # are never sent. The verdict must reuse _classify_outcome —
                # non-zero exit codes, result.is_error and budget overruns
                # are all failures that checking error_message alone would
                # miss (a failing process with empty stderr is exactly that
                # case).
                turn_status = _classify_outcome(
                    proc.returncode if proc is not None else None,
                    final_result,
                    error_message,
                )
                if turn_status != "completed":
                    if not error_message:
                        error_message = (
                            f"turn {turn.turn_id!r} did not complete "
                            f"(status={turn_status})"
                        )
                    conversation_trace.turn_failed(
                        turn, producer_session_id=cc_session_id,
                        error_code=None, error_summary=error_message,
                    )
                    break
                conversation_trace.turn_completed(
                    turn, producer_session_id=cc_session_id,
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
                error_code="claude_exec_failed",
                error_message="failed to execute claude CLI",
            )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="unexpected_error",
                error_message=str(exc),
            )

        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        # proc is the last turn's process; when the budget ran out between
        # turns it may be None (no turn ever ran) — classify as "no exit
        # code", error_message already explains why.
        status = _classify_outcome(
            proc.returncode if proc is not None else None,
            final_result,
            error_message,
        )
        token_usage_estimated = False
        if total_input_tokens == 0 and total_output_tokens == 0 and (
            estimated_input_tokens or estimated_output_tokens
        ):
            total_input_tokens = estimated_input_tokens
            total_output_tokens = estimated_output_tokens
            token_usage_estimated = True

        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                # The CLI's self-reported session_id is authoritative; for a
                # multi-turn attempt it should equal our chosen cc_session_id
                # (--session-id/--resume both use it).
                "session_id": (
                    final_result.get("session_id") if final_result
                    else (None if plan.is_legacy else cc_session_id)
                ),
                "cli_path": cli_path,
                "token_usage_estimated": token_usage_estimated,
                "model_used": model_used or self.model,
            },
            conversation_summary=summarize_conversation(attempt_dir),
            error_code=None if status == "completed" else (error_message or "cli_error"),
            error_message=error_message,
            events_count=events_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            token_usage={"input_tokens": total_input_tokens, "output_tokens": total_output_tokens},
            duration_ms=duration_ms,
            security_meta=build_security_meta(
                execution_locus=self.capabilities.execution_locus,
                permission_mode="--dangerously-skip-permissions",
                workspace_root=str(workspace.resolve()),
            ),
        )

    def _write_mcp_config(self, task: AdapterRunInput, attempt_dir: Path) -> Path | None:
        if not task.mcp_servers:
            return None
        servers: dict[str, dict[str, Any]] = {}
        for spec in task.mcp_servers:
            command = spec.command
            args = list(spec.args)
            # wire mcp rewrite consumption point: only wraps the server the
            # scenario itself declared; the original command is pushed to
            # the end of args.
            rewrite = task.wire_injection.mcp_rewrites.get(spec.name)
            if task.wire_injection.enabled and rewrite is not None:
                args = [*rewrite.args_prefix, command, *args]
                command = rewrite.command
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
        config = {"mcpServers": servers}
        path = attempt_dir / "mcp_config.json"
        path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def _render_prompt(self, task: AdapterRunInput) -> str:
        # The adapter does not prescribe a solving method — MCP, WebSearch,
        # Bash, Python, etc. are decided by whatever the scenario declares
        # plus whatever the agent brings natively.
        parts = [task.task_prompt]
        context = prompt_context(task.task_context) if task.task_context else {}
        if context:
            parts.append("")
            parts.append("Context:")
            parts.append(json.dumps(context, ensure_ascii=False, indent=2))
        return "\n".join(parts)


def _classify_outcome(
    returncode: int | None, final_result: dict | None, error_message: str | None
) -> str:
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    if returncode is None:
        return "cli_error"
    if final_result:
        subtype = final_result.get("subtype", "")
        if "budget" in subtype:
            return "timeout"
        if subtype == "success" and not final_result.get("is_error"):
            return "completed"
        if final_result.get("is_error"):
            return "cli_error"
    if returncode != 0:
        return "cli_error"
    return "completed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")


_DYNAMIC_HEADER_PREFIXES = ("x-eval-", "x-lane-")


def _merge_custom_headers(existing: str | None, extra: dict[str, str]) -> str:
    """Merge the wire injection's attempt-level headers into
    ANTHROPIC_CUSTOM_HEADERS.

    Parses existing "Name: value" lines case-insensitively (see
    docs/specs/wire_observability/design.md):
    - ``x-eval-*`` / ``x-lane-*``: the attempt (injection) value wins,
      overriding any static value already there;
    - any other same-named header: the provider's static value is kept, the
      injected one is dropped rather than appended as a duplicate line.
    Header name/value legality (token, no CR/LF) is already enforced by
    lifecycle's merge/validation step.
    """
    ordered: list[str] = []  # lowercase name, first-seen order preserved
    values: dict[str, tuple[str, str]] = {}  # lowercase name -> (original name, value)
    if existing:
        for line in existing.splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            name = name.strip()
            if not name or name.lower() in values:
                continue
            values[name.lower()] = (name, value.strip())
            ordered.append(name.lower())
    for name, value in extra.items():
        key = name.lower()
        if key not in values:
            values[key] = (name, value)
            ordered.append(key)
        elif key.startswith(_DYNAMIC_HEADER_PREFIXES):
            # attempt-level correlation header wins, keep the static entry's
            # original casing/position.
            values[key] = (values[key][0], value)
        # any other same-named header: keep the static value
    return "\n".join(f"{n}: {v}" for n, v in (values[k] for k in ordered))
