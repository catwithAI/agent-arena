"""Small, dependency-free ACP v1 stdio client.

The protocol uses newline-delimited JSON-RPC 2.0.  Keeping this boundary
small avoids coupling the arena runtime to an SDK release while ACP v2 is
still a draft.  The client deliberately advertises no filesystem or terminal
capabilities: the ACP agent operates in the attempt workspace itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


ACP_PROTOCOL_VERSION = 1
_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
_INHERITED_ENV = (
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SYSTEMROOT",
)


class AcpClientError(RuntimeError):
    """Stable protocol/runtime failure with a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AcpRunResult:
    session_id: str | None
    messages: tuple[Mapping[str, Any], ...]
    stop_reasons: tuple[str, ...]
    agent_info: Mapping[str, Any] = field(default_factory=dict)
    agent_capabilities: Mapping[str, Any] = field(default_factory=dict)
    permission_unanswered: bool = False
    stderr: str = ""
    transcript: tuple[Mapping[str, Any], ...] = ()


class AcpClient:
    """Run one ACP subprocess for one attempt and one or more prompt turns."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        permission_answers: Mapping[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("ACP command cannot be empty")
        self.command = tuple(command)
        self.cwd = Path(cwd).resolve()
        self.env = dict(env or {})
        # request id -> selected optionId.  There is intentionally no allow
        # default; an unmatched request receives the ACP cancelled outcome.
        self.permission_answers = dict(permission_answers or {})
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._messages: list[Mapping[str, Any]] = []
        self._session_id: str | None = None
        self._prompt_in_flight = False
        self._permission_unanswered = False
        self._transcript: list[Mapping[str, Any]] = []
        self._transcript_bytes = 0
        self._transcript_truncated = False

    async def run(
        self,
        prompts: Sequence[str],
        *,
        mcp_servers: Sequence[Mapping[str, Any]] = (),
        timeout_seconds: float | None = None,
    ) -> AcpRunResult:
        if not prompts:
            raise ValueError("ACP run requires at least one prompt")
        stderr_task: asyncio.Task[bytes] | None = None
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.cwd,
                env={
                    **{name: os.environ[name] for name in _INHERITED_ENV if name in os.environ},
                    **self.env,
                },
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            assert self._proc.stderr is not None
            stderr_task = asyncio.create_task(self._read_stderr(self._proc.stderr))
            operation = self._exchange(prompts, mcp_servers=mcp_servers)
            if timeout_seconds is None:
                result = await operation
            else:
                result = await asyncio.wait_for(operation, timeout=max(0.001, timeout_seconds))
            await self._close_process()
            stderr = await stderr_task
            return AcpRunResult(
                **result,
                stderr=stderr.decode("utf-8", errors="replace"),
                transcript=tuple(self._transcript),
            )
        except asyncio.TimeoutError as exc:
            await self._cancel_prompt()
            await self._kill_process()
            raise AcpClientError("agent_timeout", "ACP prompt exceeded its time budget") from exc
        except asyncio.CancelledError:
            await self._cancel_prompt()
            await self._kill_process()
            raise
        except AcpClientError:
            await self._kill_process()
            raise
        except (OSError, ValueError) as exc:
            await self._kill_process()
            raise AcpClientError("agent_internal_error", f"ACP transport failed: {exc}") from exc
        finally:
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task

    async def _exchange(
        self,
        prompts: Sequence[str],
        *,
        mcp_servers: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        initialized = await self._request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {},
                "clientInfo": {
                    "name": "agent-arena",
                    "title": "Agent Arena",
                    "version": "1",
                },
            },
        )
        version = initialized.get("protocolVersion")
        if version != ACP_PROTOCOL_VERSION:
            raise AcpClientError(
                "agent_version_unsupported",
                f"ACP agent negotiated protocol {version!r}; only v1 is supported",
            )
        created = await self._request(
            "session/new",
            {"cwd": str(self.cwd), "mcpServers": list(mcp_servers)},
        )
        session_id = created.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpClientError("agent_output_parse_degraded", "session/new omitted sessionId")
        self._session_id = session_id

        stop_reasons: list[str] = []
        for prompt in prompts:
            self._prompt_in_flight = True
            response = await self._request(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
            )
            self._prompt_in_flight = False
            reason = response.get("stopReason")
            if not isinstance(reason, str):
                raise AcpClientError(
                    "agent_output_parse_degraded", "session/prompt omitted stopReason"
                )
            stop_reasons.append(reason)

        return {
            "session_id": session_id,
            "messages": tuple(self._messages),
            "stop_reasons": tuple(stop_reasons),
            "agent_info": initialized.get("agentInfo") or {},
            "agent_capabilities": initialized.get("agentCapabilities") or {},
            "permission_unanswered": self._permission_unanswered,
        }

    async def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = await self._receive()
            if "method" in message:
                await self._handle_incoming(message)
                continue
            if message.get("id") != request_id:
                raise AcpClientError(
                    "agent_output_parse_degraded",
                    f"unexpected ACP response id {message.get('id')!r}, expected {request_id}",
                )
            if "error" in message:
                error = message.get("error") or {}
                raise AcpClientError(
                    "agent_nonzero_exit",
                    f"ACP {method} failed: {error.get('message', 'JSON-RPC error')}",
                )
            result = message.get("result")
            if result is None:
                return {}
            if not isinstance(result, Mapping):
                raise AcpClientError(
                    "agent_output_parse_degraded", f"ACP {method} returned a non-object result"
                )
            return result

    async def _handle_incoming(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        if method == "session/update":
            self._messages.append(message)
            return
        if method == "session/request_permission" and "id" in message:
            params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
            request_key = str(params.get("toolCall", {}).get("toolCallId", message["id"]))
            option_id = self.permission_answers.get(request_key)
            options = params.get("options") if isinstance(params.get("options"), list) else []
            valid_ids = {item.get("optionId") for item in options if isinstance(item, Mapping)}
            if option_id is not None and option_id in valid_ids:
                outcome = {"outcome": "selected", "optionId": option_id}
            else:
                self._permission_unanswered = True
                outcome = {"outcome": "cancelled"}
            await self._send({"jsonrpc": "2.0", "id": message["id"], "result": {"outcome": outcome}})
            self._messages.append(message)
            return
        if "id" in message:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"unsupported client method: {method}"},
                }
            )

    async def _send(self, message: Mapping[str, Any]) -> None:
        proc = self._require_process()
        assert proc.stdin is not None
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        self._record_transcript("client_to_agent", message, len(encoded))
        proc.stdin.write(encoded)
        await proc.stdin.drain()

    async def _receive(self) -> Mapping[str, Any]:
        proc = self._require_process()
        assert proc.stdout is not None
        line = await proc.stdout.readline()
        if not line:
            returncode = await proc.wait()
            raise AcpClientError(
                "agent_nonzero_exit",
                f"ACP server exited before completing JSON-RPC exchange (code {returncode})",
            )
        if len(line) > _MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
            raise AcpClientError("agent_output_parse_degraded", "ACP message exceeds framing limit")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AcpClientError("agent_output_parse_degraded", "invalid ACP JSON-RPC message") from exc
        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            raise AcpClientError("agent_output_parse_degraded", "invalid ACP JSON-RPC envelope")
        self._record_transcript("agent_to_client", message, len(line))
        return message

    def _record_transcript(
        self, direction: str, message: Mapping[str, Any], encoded_size: int
    ) -> None:
        """Retain bounded protocol evidence; the adapter redacts Attempt secrets."""
        if self._transcript_truncated:
            return
        if self._transcript_bytes + encoded_size > _MAX_TRANSCRIPT_BYTES:
            self._transcript.append(
                {"direction": "meta", "message": {"transcriptTruncated": True}}
            )
            self._transcript_truncated = True
            return
        self._transcript.append({"direction": direction, "message": dict(message)})
        self._transcript_bytes += encoded_size

    async def _cancel_prompt(self) -> None:
        if self._proc is None or not self._prompt_in_flight or not self._session_id:
            return
        with contextlib.suppress(Exception):
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": self._session_id},
                }
            )
            await asyncio.sleep(0)

    async def _close_process(self) -> None:
        proc = self._require_process()
        if proc.stdin is not None:
            proc.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await proc.stdin.wait_closed()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            await self._kill_process()
        if proc.returncode not in (0, None):
            raise AcpClientError("agent_nonzero_exit", f"ACP server exited with code {proc.returncode}")

    async def _kill_process(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=1.0)

    @staticmethod
    async def _read_stderr(stream: asyncio.StreamReader) -> bytes:
        data = await stream.read(_MAX_MESSAGE_BYTES)
        return data

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._proc is None:
            raise RuntimeError("ACP process has not started")
        return self._proc
