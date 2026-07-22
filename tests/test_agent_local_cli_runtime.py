from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from backend.agents.launch import RenderedLaunchPlan
from backend.agents.runtime.local_cli import LocalCliRuntime, RuntimeLimits


def _plan(tmp_path: Path, code: str, *, stdin_data: bytes | None = None) -> RenderedLaunchPlan:
    return RenderedLaunchPlan(
        argv=(sys.executable, "-c", code),
        cwd=tmp_path,
        env={},
        env_redacted={},
        stdin_data=stdin_data,
        prompt_mode="stdin" if stdin_data is not None else "driver-owned",
        plan_hash="sha256:fixture",
    )


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_concurrently_drains_full_stdout_and_stderr_pipes(tmp_path):
    size = 512 * 1024
    code = f"import os; os.write(1, b'o' * {size}); os.write(2, b'e' * {size})"
    result = await LocalCliRuntime().run(
        _plan(tmp_path, code),
        evidence_dir=tmp_path / "evidence",
        timeout_seconds=5,
    )
    assert result.status == "completed"
    assert result.stdout.bytes_seen == size
    assert result.stderr.bytes_seen == size
    assert result.stdout_path.read_bytes() == b"o" * size
    assert result.stderr_path.read_bytes() == b"e" * size


@pytest.mark.asyncio
async def test_log_limits_bad_utf8_and_long_line_have_diagnostics(tmp_path):
    code = "import os; os.write(1, b'bad\\xff-' + b'x' * 200); os.write(2, b'y' * 200)"
    runtime = LocalCliRuntime(
        limits=RuntimeLimits(
            max_line_bytes=32,
            max_total_log_bytes=256,
            max_evidence_frames=20,
            read_chunk_bytes=16,
            termination_grace_seconds=0.05,
        )
    )
    result = await runtime.run(
        _plan(tmp_path, code), evidence_dir=tmp_path / "evidence", timeout_seconds=5
    )
    assert result.status == "completed"
    assert result.stdout.bytes_written + result.stderr.bytes_written == 256
    assert result.stdout.bytes_dropped + result.stderr.bytes_dropped > 0
    assert result.stdout.lines_truncated + result.stderr.lines_truncated > 0
    assert result.stdout.decoding_replacements > 0
    end = _events(result.evidence_path)[-1]
    assert end["type"] == "runtime_end"
    assert end["total_log_bytes_written"] == 256


@pytest.mark.asyncio
async def test_nonzero_exit_has_stable_error_code(tmp_path):
    result = await LocalCliRuntime().run(
        _plan(tmp_path, "raise SystemExit(7)"),
        evidence_dir=tmp_path / "evidence",
        timeout_seconds=5,
    )
    assert result.status == "failed"
    assert result.returncode == 7
    assert result.error_code == "agent_nonzero_exit"


@pytest.mark.asyncio
async def test_redacts_secrets_across_read_chunk_boundaries(tmp_path):
    secret = "profile-super-secret"
    runtime = LocalCliRuntime(limits=RuntimeLimits(read_chunk_bytes=4))
    result = await runtime.run(
        _plan(
            tmp_path,
            f"import os; os.write(1, b'before-{secret}-after\\n')",
        ),
        evidence_dir=tmp_path / "evidence",
        timeout_seconds=5,
        redact_secrets=(secret,),
    )

    raw = result.stdout_path.read_text(encoding="utf-8")
    evidence = result.evidence_path.read_text(encoding="utf-8")
    assert raw == "before-***-after\n"
    assert secret not in evidence
    assert "***" in evidence


@pytest.mark.asyncio
async def test_timeout_none_does_not_call_wait_for_with_none(tmp_path):
    result = await LocalCliRuntime().run(
        _plan(tmp_path, "import time; time.sleep(0.05); print('done')"),
        evidence_dir=tmp_path / "evidence",
        timeout_seconds=None,
    )
    assert result.status == "completed"
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_timeout_cleans_parent_and_child_process_group(tmp_path):
    pid_file = tmp_path / "pids.json"
    code = _forking_code(pid_file)
    runtime = LocalCliRuntime(limits=RuntimeLimits(termination_grace_seconds=0.05))
    result = await runtime.run(
        _plan(tmp_path, code),
        evidence_dir=tmp_path / "evidence",
        timeout_seconds=0.15,
    )
    assert result.status == "timeout"
    assert result.error_code == "agent_timeout"
    pids = json.loads(pid_file.read_text(encoding="utf-8"))
    await _wait_process_group_gone(pids["parent"])


@pytest.mark.asyncio
async def test_cancellation_cleans_parent_and_child_process_group(tmp_path):
    pid_file = tmp_path / "pids.json"
    runtime = LocalCliRuntime(limits=RuntimeLimits(termination_grace_seconds=0.05))
    task = asyncio.create_task(
        runtime.run(
            _plan(tmp_path, _forking_code(pid_file)),
            evidence_dir=tmp_path / "evidence",
            timeout_seconds=None,
        )
    )
    await _wait_for_file(pid_file)
    pids = json.loads(pid_file.read_text(encoding="utf-8"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_process_group_gone(pids["parent"])
    events = _events(tmp_path / "evidence" / "evidence.jsonl")
    assert any(event["type"] == "cancelled" for event in events)


@pytest.mark.asyncio
async def test_normal_parent_exit_still_cleans_orphaned_group_child(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    code = (
        "import pathlib, subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))"
    )
    runtime = LocalCliRuntime(limits=RuntimeLimits(termination_grace_seconds=0.05))
    result = await runtime.run(
        _plan(tmp_path, code), evidence_dir=tmp_path / "evidence", timeout_seconds=5
    )
    assert result.status == "completed"
    assert result.cleanup in {"terminated", "killed"}
    assert child_pid_file.is_file()
    await _wait_process_group_gone(_started_pid(result.evidence_path))


def _forking_code(pid_file: Path) -> str:
    return (
        "import json, os, pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text("
        "json.dumps({'parent': os.getpid(), 'child': child.pid})); "
        "time.sleep(60)"
    )


def _started_pid(evidence_path: Path) -> int:
    event = next(event for event in _events(evidence_path) if event["type"] == "process_started")
    return int(event["pid"])


async def _wait_for_file(path: Path) -> None:
    for _ in range(100):
        if path.is_file():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


async def _wait_process_group_gone(pgid: int) -> None:
    for _ in range(100):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"process group {pgid} is still alive")
