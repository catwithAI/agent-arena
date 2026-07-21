"""Self-check: independent health checks exposed on one endpoint.

Design: every check is an isolated function; one failing never hides the
others. Returned structure (test contract):

    {
        "checks": [
            {"name": "config", "status": "ok|fail|skipped", "detail": "..."},
            ...
        ],
        "summary": {"ok": N, "fail": N, "skipped": N}
    }

Check names:
- config / env_scan / env_api_import / env_tool_registry
- env_token_auth / trace_write
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request

from . import runtime_state
from .config import Settings
from .db import _init_db_sync, _open_sync, hash_session_token, new_session_token

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "fail" | "skipped"
    detail: str = ""
    data: dict[str, Any] | None = None


# ---------- individual checks ---------------------------------------------


def _check_config(settings: Settings) -> CheckResult:
    try:
        _ = str(settings.lane.data_path)
        _ = str(settings.lane.envs_path)
    except Exception as exc:
        return CheckResult("config", "fail", f"failed to read settings: {exc}")
    return CheckResult(
        "config",
        "ok",
        f"data_path={settings.lane.data_path}",
        data={
            "data_path": str(settings.lane.data_path),
            "envs_path": str(settings.lane.envs_path),
            "public_base_url": settings.lane.public_base_url,
        },
    )


def _check_env_scan(envs: dict[str, Any]) -> CheckResult:
    if not envs:
        return CheckResult("env_scan", "fail", "no envs discovered")
    summary = {
        name: {
            "tools": list(env.tools.keys()),
            "tasks": [t.id for t in env.tasks],
        }
        for name, env in envs.items()
    }
    return CheckResult(
        "env_scan",
        "ok",
        f"{len(envs)} envs: {sorted(envs)}",
        data=summary,
    )


def _check_env_api_import() -> CheckResult:
    try:
        from lane.env_api import (  # noqa: F401
            EnvContext,
            RegisteredTool,
            TraceWriter,
            clear_current_registry,
            env_tool,
            get_current_registry,
        )
    except Exception as exc:
        return CheckResult("env_api_import", "fail", str(exc))
    return CheckResult("env_api_import", "ok", "all lane.env_api symbols importable")


def _check_env_tool_registry(envs: dict[str, Any]) -> CheckResult:
    total = sum(len(env.tools) for env in envs.values())
    if total == 0:
        return CheckResult("env_tool_registry", "fail", "no @env_tool registrations at all")
    return CheckResult(
        "env_tool_registry",
        "ok",
        f"{total} tools registered",
        data={name: list(env.tools) for name, env in envs.items()},
    )


def _check_env_token_auth() -> CheckResult:
    """Build a throwaway attempt + token and verify the hash-comparison path
    in memory. No HTTP involved — this only proves our own auth
    implementation is sound.
    """
    try:
        with tempfile.TemporaryDirectory() as td:
            local_db = Path(td) / "lane.db"
            _init_db_sync(local_db)
            token = new_session_token()
            with _open_sync(local_db) as conn:
                conn.execute(
                    "INSERT INTO tasks(id, env_name, prompt, context_json, constraints_json,"
                    " timeout_seconds, source, created_at)"
                    " VALUES('t', 'order-desk', 'p', '{}', '{}', 600, 'file', 'x')"
                )
                conn.execute(
                    "INSERT INTO runs(id, task_id, env_name, status, created_at)"
                    " VALUES('r', 't', 'order-desk', 'queued', 'x')"
                )
                conn.execute(
                    "INSERT INTO attempts(id, run_id, task_id, env_name, agent_name, status,"
                    " session_id, session_token_hash, external_refs_json, event_count, created_at)"
                    " VALUES('a', 'r', 't', 'order-desk', 'claude-code', 'running',"
                    " 'es', ?, '{}', 0, 'x')",
                    (hash_session_token(token),),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT session_token_hash FROM attempts WHERE id='a'"
                ).fetchone()
            assert row is not None
            assert row[0] == hash_session_token(token), "hash mismatch"
            assert row[0] != hash_session_token("wrong"), "wrong token must not match"
    except Exception as exc:
        return CheckResult("env_token_auth", "fail", str(exc))
    return CheckResult("env_token_auth", "ok", "token hash comparison path works")


def _check_trace_write() -> CheckResult:
    """Write one JSONL row through TraceWriter, then read it back."""
    from lane.env_api import TraceWriter

    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tw = TraceWriter(data_path=td, attempt_id="probe", session_id="es")
            tw.record(
                tool_name="probe_tool",
                arguments={"x": 1},
                result={"y": 2},
                is_error=False,
                duration_ms=1,
            )
            text = (td / "attempts" / "probe" / "trace.jsonl").read_text(encoding="utf-8")
            row = json.loads(text.strip().splitlines()[-1])
            assert row["tool_name"] == "probe_tool"
    except Exception as exc:
        return CheckResult("trace_write", "fail", str(exc))
    return CheckResult("trace_write", "ok", "trace.jsonl write + read-back OK")


# ---------- entry point -----------------------------------------------------


async def run_all_checks(*, settings: Settings, envs: dict[str, Any]) -> list[CheckResult]:
    return [
        _check_config(settings),
        _check_env_scan(envs),
        _check_env_api_import(),
        _check_env_tool_registry(envs),
        _check_env_token_auth(),
        _check_trace_write(),
    ]


# ---------- HTTP route -------------------------------------------------------


def build_router() -> APIRouter:
    router = APIRouter(tags=["selfcheck"])

    @router.get("/selfcheck")
    async def selfcheck(request: Request) -> dict[str, Any]:
        state = runtime_state.get()
        results = await run_all_checks(
            settings=request.app.state.settings,
            envs=state.envs,
        )
        return {
            "checks": [
                {k: v for k, v in asdict(r).items() if v is not None}
                for r in results
            ],
            "summary": {
                "ok": sum(1 for r in results if r.status == "ok"),
                "fail": sum(1 for r in results if r.status == "fail"),
                "skipped": sum(1 for r in results if r.status == "skipped"),
            },
        }

    return router


def register_routes(app: FastAPI, prefix: str = "/api") -> None:
    app.include_router(build_router(), prefix=prefix)
