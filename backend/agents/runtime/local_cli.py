"""Safe shared subprocess lifecycle for local CLI agent profiles."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Literal, TextIO

from ..launch import RenderedLaunchPlan


@dataclass(frozen=True)
class RuntimeLimits:
    max_line_bytes: int = 10 * 1024 * 1024
    max_total_log_bytes: int = 100 * 1024 * 1024
    max_evidence_frames: int = 100_000
    read_chunk_bytes: int = 64 * 1024
    termination_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        for name in (
            "max_line_bytes",
            "max_total_log_bytes",
            "max_evidence_frames",
            "read_chunk_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.termination_grace_seconds < 0:
            raise ValueError("termination_grace_seconds cannot be negative")


@dataclass
class StreamStats:
    bytes_seen: int = 0
    bytes_written: int = 0
    bytes_dropped: int = 0
    lines_emitted: int = 0
    lines_truncated: int = 0
    line_bytes_dropped: int = 0
    decoding_replacements: int = 0
    frames_dropped: int = 0


@dataclass(frozen=True)
class RuntimeResult:
    status: Literal["completed", "failed", "timeout"]
    returncode: int | None
    error_code: str | None
    timed_out: bool
    cleanup: Literal["not_needed", "terminated", "killed", "failed"]
    stdout_path: Path
    stderr_path: Path
    evidence_path: Path
    stdout: StreamStats
    stderr: StreamStats
    started_at: str
    ended_at: str
    duration_ms: int


@dataclass
class _TotalBudget:
    limit: int
    used: int = 0

    def take(self, data: bytes) -> tuple[bytes, int]:
        remaining = max(0, self.limit - self.used)
        accepted = data[:remaining]
        self.used += len(accepted)
        return accepted, len(data) - len(accepted)


@dataclass
class _ByteRedactor:
    secrets: tuple[bytes, ...]
    buffer: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes, *, final: bool = False) -> bytes:
        if not self.secrets:
            return data
        self.buffer.extend(data)
        if final:
            emitted = bytes(self.buffer)
            self.buffer.clear()
            for secret in self.secrets:
                emitted = emitted.replace(secret, b"***")
            return emitted

        maximum = max(len(secret) for secret in self.secrets)
        emitted = bytearray()
        while len(self.buffer) > maximum:
            matched = next(
                (secret for secret in self.secrets if self.buffer.startswith(secret)),
                None,
            )
            if matched is not None:
                emitted.extend(b"***")
                del self.buffer[: len(matched)]
            else:
                emitted.append(self.buffer[0])
                del self.buffer[0]
        return bytes(emitted)


@dataclass
class _EvidenceWriter:
    file: TextIO
    max_frames: int
    sequence: int = 0
    content_frames: int = 0

    def emit(self, event: dict[str, Any], *, content: bool = False) -> bool:
        if content and self.content_frames >= self.max_frames:
            return False
        if content:
            self.content_frames += 1
        self.sequence += 1
        payload = {"sequence": self.sequence, "timestamp": _now_iso(), **event}
        self.file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        self.file.flush()
        return True


@dataclass
class _LineAccumulator:
    stream: str
    max_line_bytes: int
    writer: _EvidenceWriter
    stats: StreamStats
    buffer: bytearray = field(default_factory=bytearray)
    dropped: int = 0

    def feed(self, data: bytes) -> None:
        for part in data.splitlines(keepends=True):
            complete = part.endswith(b"\n")
            content = part[:-1] if complete else part
            if content.endswith(b"\r") and complete:
                content = content[:-1]
            remaining = max(0, self.max_line_bytes - len(self.buffer))
            self.buffer.extend(content[:remaining])
            self.dropped += max(0, len(content) - remaining)
            if complete:
                self.flush()

    def flush(self) -> None:
        if not self.buffer and not self.dropped:
            return
        text = bytes(self.buffer).decode("utf-8", errors="replace")
        replacements = text.count("\ufffd")
        self.stats.decoding_replacements += replacements
        truncated = self.dropped > 0
        if truncated:
            self.stats.lines_truncated += 1
            self.stats.line_bytes_dropped += self.dropped
        emitted = self.writer.emit(
            {
                "type": "stream_line",
                "stream": self.stream,
                "text": text,
                "byte_count": len(self.buffer) + self.dropped,
                "truncated": truncated,
                "dropped_bytes": self.dropped,
                "decode_replacements": replacements,
            },
            content=True,
        )
        if emitted:
            self.stats.lines_emitted += 1
        else:
            self.stats.frames_dropped += 1
        self.buffer.clear()
        self.dropped = 0


class LocalCliRuntime:
    runtime_id = "local-cli"
    runtime_version = "1"

    def __init__(self, *, limits: RuntimeLimits | None = None) -> None:
        self.limits = limits or RuntimeLimits()

    async def run(
        self,
        plan: RenderedLaunchPlan,
        *,
        evidence_dir: Path,
        timeout_seconds: float | None,
        base_env: dict[str, str] | None = None,
        redact_secrets: tuple[str, ...] = (),
    ) -> RuntimeResult:
        evidence_dir = Path(evidence_dir)
        raw_dir = evidence_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stdout_path = raw_dir / "stdout.log"
        stderr_path = raw_dir / "stderr.log"
        evidence_path = evidence_dir / "evidence.jsonl"
        started_at = _now_iso()
        started_clock = asyncio.get_running_loop().time()
        stdout_stats = StreamStats()
        stderr_stats = StreamStats()
        budget = _TotalBudget(self.limits.max_total_log_bytes)
        timed_out = False
        cleanup: Literal["not_needed", "terminated", "killed", "failed"] = "not_needed"
        process: asyncio.subprocess.Process | None = None
        execution_task: (
            asyncio.Task[Literal["not_needed", "terminated", "killed", "failed"]] | None
        ) = None

        with (
            stdout_path.open("wb") as stdout_file,
            stderr_path.open("wb") as stderr_file,
            evidence_path.open("w", encoding="utf-8") as evidence_file,
        ):
            evidence = _EvidenceWriter(evidence_file, self.limits.max_evidence_frames)
            evidence.emit(
                {
                    "type": "runtime_start",
                    "runtime": f"{self.runtime_id}@{self.runtime_version}",
                    "argv": [_redact_text(value, redact_secrets) for value in plan.argv_redacted],
                    "cwd": str(plan.cwd),
                    "env_names": list(plan.env_names),
                    "plan_hash": plan.plan_hash,
                }
            )
            child_env = dict(os.environ if base_env is None else base_env)
            child_env.update(plan.env)
            try:
                process = await asyncio.create_subprocess_exec(
                    *plan.argv,
                    stdin=asyncio.subprocess.PIPE if plan.stdin_data is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(plan.cwd),
                    env=child_env,
                    start_new_session=True,
                )
                evidence.emit({"type": "process_started", "pid": process.pid})
                execution_task = asyncio.create_task(
                    self._execute(
                        process,
                        plan.stdin_data,
                        stdout_file,
                        stderr_file,
                        evidence,
                        budget,
                        stdout_stats,
                        stderr_stats,
                        redact_secrets,
                    )
                )
                try:
                    if timeout_seconds is None:
                        cleanup = await asyncio.shield(execution_task)
                    else:
                        cleanup = await asyncio.wait_for(
                            asyncio.shield(execution_task), timeout=max(0.0, timeout_seconds)
                        )
                except asyncio.TimeoutError:
                    timed_out = True
                    evidence.emit({"type": "timeout", "timeout_seconds": timeout_seconds})
                    termination_cleanup = await self._terminate_process_group(process)
                    execution_cleanup = await execution_task
                    cleanup = _merge_cleanup(termination_cleanup, execution_cleanup)
                except asyncio.CancelledError:
                    evidence.emit({"type": "cancel_requested"})
                    termination_cleanup = await asyncio.shield(
                        self._terminate_process_group(process)
                    )
                    if execution_task is not None:
                        with contextlib.suppress(Exception):
                            execution_cleanup = await asyncio.shield(execution_task)
                            cleanup = _merge_cleanup(termination_cleanup, execution_cleanup)
                    if cleanup == "not_needed":
                        cleanup = termination_cleanup
                    evidence.emit({"type": "cancelled", "cleanup": cleanup})
                    raise
                except BaseException:
                    termination_cleanup = await asyncio.shield(
                        self._terminate_process_group(process)
                    )
                    if execution_task is not None:
                        with contextlib.suppress(Exception):
                            execution_cleanup = await asyncio.shield(execution_task)
                            cleanup = _merge_cleanup(termination_cleanup, execution_cleanup)
                    if cleanup == "not_needed":
                        cleanup = termination_cleanup
                    evidence.emit({"type": "runtime_error", "cleanup": cleanup})
                    raise
            except BaseException:
                if process is not None and process.returncode is None:
                    await asyncio.shield(self._terminate_process_group(process))
                raise

            assert process is not None
            returncode = process.returncode
            status: Literal["completed", "failed", "timeout"]
            error_code: str | None
            if timed_out:
                status, error_code = "timeout", "agent_timeout"
            elif returncode == 0:
                status, error_code = "completed", None
            else:
                status, error_code = "failed", "agent_nonzero_exit"
            ended_at = _now_iso()
            duration_ms = int((asyncio.get_running_loop().time() - started_clock) * 1000)
            evidence.emit(
                {
                    "type": "runtime_end",
                    "status": status,
                    "returncode": returncode,
                    "error_code": error_code,
                    "cleanup": cleanup,
                    "stdout": asdict(stdout_stats),
                    "stderr": asdict(stderr_stats),
                    "total_log_bytes_written": budget.used,
                }
            )

        return RuntimeResult(
            status=status,
            returncode=returncode,
            error_code=error_code,
            timed_out=timed_out,
            cleanup=cleanup,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            evidence_path=evidence_path,
            stdout=stdout_stats,
            stderr=stderr_stats,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
        )

    async def _execute(
        self,
        process: asyncio.subprocess.Process,
        stdin_data: bytes | None,
        stdout_file: BinaryIO,
        stderr_file: BinaryIO,
        evidence: _EvidenceWriter,
        budget: _TotalBudget,
        stdout_stats: StreamStats,
        stderr_stats: StreamStats,
        redact_secrets: tuple[str, ...],
    ) -> Literal["not_needed", "terminated", "killed", "failed"]:
        assert process.stdout is not None and process.stderr is not None
        drain_tasks = [
            asyncio.create_task(
                self._drain_stream(
                    "stdout",
                    process.stdout,
                    stdout_file,
                    evidence,
                    budget,
                    stdout_stats,
                    redact_secrets,
                )
            ),
            asyncio.create_task(
                self._drain_stream(
                    "stderr",
                    process.stderr,
                    stderr_file,
                    evidence,
                    budget,
                    stderr_stats,
                    redact_secrets,
                )
            ),
        ]
        # asyncio.Process.wait() may itself wait for pipe EOF. A descendant
        # that inherited stdout/stderr can therefore keep it pending after
        # the direct child has exited. Poll the child-watcher returncode so
        # we can clean the process group before awaiting final pipe closure.
        process_task = asyncio.create_task(_wait_for_returncode(process))
        tasks: list[asyncio.Task[Any]] = [*drain_tasks, process_task]
        if stdin_data is not None:
            tasks.append(asyncio.create_task(self._write_stdin(process, stdin_data)))
        try:
            active_drains = set(drain_tasks)
            while not process_task.done():
                done, _ = await asyncio.wait(
                    {process_task, *active_drains}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    if task is process_task:
                        continue
                    active_drains.discard(task)
                    exception = task.exception()
                    if exception is not None:
                        raise exception
            await process_task
            cleanup = await self._cleanup_remaining_group(process.pid)
            await asyncio.gather(*drain_tasks)
            await process.wait()
            for task in tasks[3:]:
                await task
            return cleanup
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _drain_stream(
        self,
        stream_name: str,
        reader: asyncio.StreamReader,
        raw_file: BinaryIO,
        evidence: _EvidenceWriter,
        budget: _TotalBudget,
        stats: StreamStats,
        redact_secrets: tuple[str, ...],
    ) -> None:
        lines = _LineAccumulator(stream_name, self.limits.max_line_bytes, evidence, stats)
        redactor = _ByteRedactor(
            tuple(
                sorted(
                    {secret.encode("utf-8") for secret in redact_secrets if secret},
                    key=len,
                    reverse=True,
                )
            )
        )
        while True:
            chunk = await reader.read(self.limits.read_chunk_bytes)
            if not chunk:
                break
            stats.bytes_seen += len(chunk)
            sanitized = redactor.feed(chunk)
            accepted, dropped = budget.take(sanitized)
            if accepted:
                raw_file.write(accepted)
                raw_file.flush()
                stats.bytes_written += len(accepted)
                lines.feed(accepted)
            stats.bytes_dropped += dropped
        tail = redactor.feed(b"", final=True)
        accepted, dropped = budget.take(tail)
        if accepted:
            raw_file.write(accepted)
            raw_file.flush()
            stats.bytes_written += len(accepted)
            lines.feed(accepted)
        stats.bytes_dropped += dropped
        lines.flush()
        evidence.emit(
            {
                "type": "stream_end",
                "stream": stream_name,
                "bytes_seen": stats.bytes_seen,
                "bytes_written": stats.bytes_written,
                "bytes_dropped": stats.bytes_dropped,
                "frames_dropped": stats.frames_dropped,
            }
        )

    @staticmethod
    async def _write_stdin(process: asyncio.subprocess.Process, data: bytes) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()

    async def _terminate_process_group(
        self, process: asyncio.subprocess.Process
    ) -> Literal["not_needed", "terminated", "killed", "failed"]:
        pgid = process.pid
        if process.returncode is not None and not _group_exists(pgid):
            return "not_needed"
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            if process.returncode is None:
                await process.wait()
            return "not_needed"
        except (PermissionError, OSError):
            return "failed"

        try:
            await asyncio.wait_for(
                asyncio.shield(process.wait()), timeout=self.limits.termination_grace_seconds
            )
        except asyncio.TimeoutError:
            pass
        if not _group_exists(pgid):
            return "terminated"
        try:
            os.killpg(pgid, signal.SIGKILL)
            if process.returncode is None:
                await process.wait()
            return "killed"
        except ProcessLookupError:
            return "terminated"
        except (PermissionError, OSError):
            return "failed"

    async def _cleanup_remaining_group(
        self, pgid: int
    ) -> Literal["not_needed", "terminated", "killed", "failed"]:
        if not _group_exists(pgid):
            return "not_needed"
        try:
            os.killpg(pgid, signal.SIGTERM)
            await asyncio.sleep(self.limits.termination_grace_seconds)
            if _group_exists(pgid):
                os.killpg(pgid, signal.SIGKILL)
                return "killed"
            return "terminated"
        except ProcessLookupError:
            return "terminated"
        except (PermissionError, OSError):
            return "failed"


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def _wait_for_returncode(process: asyncio.subprocess.Process) -> int:
    while process.returncode is None:
        await asyncio.sleep(0.01)
    return process.returncode


def _merge_cleanup(
    first: Literal["not_needed", "terminated", "killed", "failed"],
    second: Literal["not_needed", "terminated", "killed", "failed"],
) -> Literal["not_needed", "terminated", "killed", "failed"]:
    priority = {"not_needed": 0, "terminated": 1, "killed": 2, "failed": 3}
    return first if priority[first] >= priority[second] else second


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "***")
    return value
