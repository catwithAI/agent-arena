"""Frontend-facing REST API (mounted with no prefix by `main.create_app()`).

- `GET /agents` — lists agents this instance can dispatch to right now:
  the built-in `claude-code` / `codex` adapters plus anything declared under
  `custom_agents` in `agentlane.yaml`.
- `GET /models/providers` — third-party model provider names configured for
  claude-code/codex (never exposes base_url/api_key_env values).
- `POST /runs` — create a comparison run: one attempt per requested agent,
  dispatched concurrently in the background.
- `GET /runs` / `GET /runs/{id}` / `GET /runs/{id}/attempts/{aid}` — history
  and detail views, including trace / thinking / raw events / artifacts.
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from . import runtime_state
from .db import _now_iso, _open_sync
from .run_dispatch import dispatch as dispatch_attempt
from .run_dispatch import known_agents
from .runner import create_attempt

logger = logging.getLogger(__name__)


async def _dispatch_all(run_id: str, jobs: list[dict[str, Any]]) -> None:
    import asyncio

    state = runtime_state.get()
    tasks = [asyncio.create_task(dispatch_attempt(**job)) for job in jobs]
    state.active_tasks[run_id] = tasks
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.exception("dispatch task crashed", exc_info=result)
    finally:
        state.active_tasks.pop(run_id, None)


# ---------- /agents -----------------------------------------------------------


def _list_agents(settings) -> list[dict[str, Any]]:
    agents = []
    claude_path = shutil.which("claude")
    agents.append(
        {
            "name": "claude-code",
            "status": "available" if claude_path else "not_found",
            "detail": None if claude_path else "claude CLI not found in PATH",
            "cli_path": claude_path,
        }
    )
    codex_path = shutil.which("codex")
    agents.append(
        {
            "name": "codex",
            "status": "available" if codex_path else "not_found",
            "detail": None if codex_path else "codex CLI not found in PATH",
            "cli_path": codex_path,
        }
    )
    for name, custom in settings.custom_agents.items():
        exe = custom.command[0] if custom.command else ""
        exe_path = shutil.which(exe) or (exe if Path(exe).exists() else None)
        agents.append(
            {
                "name": name,
                "status": "available" if exe_path else "not_found",
                "detail": None if exe_path else f"'{exe}' not found in PATH",
                "cli_path": exe_path,
            }
        )
    return agents


# ---------- /runs body ---------------------------------------------------------


class CreateRunRequest(BaseModel):
    env_name: str
    task_id: str | None = None
    prompt: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 1000
    agents: list[str] = Field(default=["claude-code", "codex"])
    # single model applied to every agent, or a per-agent {agent: model} map
    model: str | None = None
    models: dict[str, str] | None = None
    # legacy convenience: a single agent name
    agent: str | None = None

    @model_validator(mode="after")
    def _normalize(self) -> "CreateRunRequest":
        if self.agent and self.agent not in self.agents:
            self.agents = [self.agent]
        return self

    def model_for(self, agent_name: str) -> str | None:
        if self.models:
            return self.models.get(agent_name, self.model)
        return self.model


# ---------- DB helpers ---------------------------------------------------------


def _get_or_create_adhoc_task_sync(db_path: Path, body: CreateRunRequest) -> tuple[str, str]:
    """Returns (task_id, env_name). A `task_id` is looked up as-is (and
    refreshed from the env's file definition if it's a file-backed task —
    the JSON file is the source of truth); a free-form `prompt` creates a
    throwaway task."""
    if body.task_id:
        state = runtime_state.get()
        env = state.envs.get(body.env_name)
        matching = next((t for t in env.tasks if t.id == body.task_id), None) if env else None
        if matching is not None:
            return _hydrate_file_task(db_path, matching)
        with _open_sync(db_path) as conn:
            row = conn.execute("SELECT env_name FROM tasks WHERE id=?", (body.task_id,)).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"task not found in env={body.env_name}: {body.task_id}"
            )
        return body.task_id, row["env_name"]

    task_id = f"adhoc_{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    with _open_sync(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks(id, env_name, prompt, context_json, constraints_json,"
            " timeout_seconds, source, created_at) VALUES(?, ?, ?, ?, ?, ?, 'adhoc', ?)",
            (
                task_id,
                body.env_name,
                body.prompt or "",
                json.dumps(body.context, ensure_ascii=False),
                json.dumps(body.constraints, ensure_ascii=False),
                body.timeout_seconds,
                now,
            ),
        )
        conn.commit()
    return task_id, body.env_name


def _hydrate_file_task(db_path: Path, task) -> tuple[str, str]:
    now = _now_iso()
    context_json = json.dumps(task.context, ensure_ascii=False)
    constraints_json = json.dumps(task.constraints, ensure_ascii=False)
    with _open_sync(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(id, env_name, prompt, context_json, constraints_json,"
            " timeout_seconds, source, created_at) VALUES(?, ?, ?, ?, ?, ?, 'file', ?)",
            (task.id, task.env_name, task.prompt, context_json, constraints_json, task.timeout_seconds, now),
        )
        conn.execute(
            "UPDATE tasks SET prompt=?, context_json=?, constraints_json=?,"
            " timeout_seconds=? WHERE id=? AND source='file'",
            (task.prompt, context_json, constraints_json, task.timeout_seconds, task.id),
        )
        conn.commit()
    return task.id, task.env_name


def _list_runs_sync(db_path: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    with _open_sync(db_path) as conn:
        rows = conn.execute(
            "SELECT r.id AS run_id, r.task_id, r.env_name, r.status AS run_status,"
            " r.created_at, r.started_at, r.ended_at,"
            " (SELECT COUNT(*) FROM attempts a WHERE a.run_id=r.id) AS attempt_count"
            " FROM runs r ORDER BY r.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _get_run_sync(db_path: Path, run_id: str) -> dict[str, Any] | None:
    with _open_sync(db_path) as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            return None
        attempts = conn.execute(
            "SELECT id, agent_name, model, status, score_total, event_count,"
            " thinking_count, tool_call_count, token_usage_json, cost_estimate,"
            " duration_ms, started_at, ended_at, external_refs_json,"
            " error_code, error_message, execution_locus"
            " FROM attempts WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()
    att_rows = []
    for a in attempts:
        row = dict(a)
        try:
            refs = json.loads(row.pop("external_refs_json") or "{}")
        except json.JSONDecodeError:
            refs = {}
        row["model_used"] = refs.get("model_used")
        att_rows.append(row)
    return {**dict(run), "attempts": att_rows}


def _get_attempt_detail_sync(db_path: Path, attempt_id: str) -> dict[str, Any] | None:
    with _open_sync(db_path) as conn:
        att = conn.execute(
            "SELECT id, run_id, task_id, env_name, agent_name, model, status, session_id,"
            " external_refs_json, event_count, last_event_at, thinking_count,"
            " tool_call_count, token_usage_json, cost_estimate, duration_ms,"
            " score_total, error_code, error_message, started_at, ended_at,"
            " created_at, execution_locus, permission_mode, workspace_root"
            " FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if att is None:
            return None
        scores = conn.execute(
            "SELECT dimension, value, detail FROM scores WHERE attempt_id=? ORDER BY id",
            (attempt_id,),
        ).fetchall()
    detail = dict(att)
    detail["external_refs"] = json.loads(detail.pop("external_refs_json") or "{}")
    detail["token_usage"] = json.loads(detail.pop("token_usage_json", None) or "{}")
    detail["scores"] = [dict(s) for s in scores]
    detail["execution"] = {
        "execution_locus": detail.pop("execution_locus", None),
        "permission_mode": detail.pop("permission_mode", None),
        "workspace_root": detail.pop("workspace_root", None),
    }
    return detail


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _read_final_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


# Framework-written runtime metadata/intermediate files under the attempt
# root — never the agent's actual submission — excluded from the artifacts
# view so every attempt doesn't show a pile of noise.
_ATTEMPT_ROOT_FRAMEWORK_FILES = {
    "events.jsonl",
    "thinking.jsonl",
    "mcp_config.json",
    "codex_mcp_config.json",
    "codex_final.txt",
    "stderr.txt",
    "prompt.txt",
}


def _artifacts_root(attempt_dir: Path) -> Path:
    """claude-code/codex/custom-cli adapters all run as a host process with
    cwd=attempt_dir, so submissions land directly in the attempt root."""
    return attempt_dir


# ---------- routes -------------------------------------------------------------


def build_router() -> APIRouter:
    router = APIRouter(tags=["frontend"])

    @router.get("/agents")
    async def list_agents(request: Request) -> list[dict[str, Any]]:
        return _list_agents(request.app.state.settings)

    @router.get("/models/providers")
    async def list_model_providers(request: Request) -> dict[str, Any]:
        settings = request.app.state.settings
        return {
            "providers": sorted(settings.model_providers.keys()),
            "suggested": settings.model_suggestions,
        }

    @router.get("/envs")
    async def list_envs() -> list[dict[str, Any]]:
        state = runtime_state.get()
        return [
            {
                "name": env.name,
                "meta": env.meta,
                "tasks": [{"id": t.id, "prompt": t.prompt} for t in env.tasks],
            }
            for env in state.envs.values()
        ]

    @router.post("/runs")
    async def post_runs(
        body: CreateRunRequest,
        background_tasks: BackgroundTasks,
        request: Request,
    ) -> dict[str, Any]:
        state = runtime_state.get()
        if body.env_name not in state.envs:
            raise HTTPException(status_code=404, detail=f"env not found: {body.env_name}")

        if not body.task_id and not (body.prompt or "").strip():
            raise HTTPException(
                status_code=400, detail="either task_id or a non-blank prompt is required"
            )

        settings = request.app.state.settings
        agents = body.agents
        if not agents:
            raise HTTPException(status_code=400, detail="agents list is empty")

        allowed = known_agents(settings)
        for ag in agents:
            if ag not in allowed:
                raise HTTPException(status_code=400, detail=f"unknown agent: {ag!r}, known: {allowed}")

        task_id, env_name = _get_or_create_adhoc_task_sync(state.db_path, body)

        with _open_sync(state.db_path) as conn:
            row = conn.execute(
                "SELECT prompt, context_json, timeout_seconds FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        prompt = row["prompt"] if row else (body.prompt or "")
        context = json.loads(row["context_json"]) if row and row["context_json"] else (body.context or {})
        timeout_seconds = row["timeout_seconds"] if row and row["timeout_seconds"] is not None else body.timeout_seconds

        run_id = f"run_{uuid.uuid4().hex[:12]}"

        attempts_info = []
        dispatch_jobs = []
        for agent_name in agents:
            agent_model = body.model_for(agent_name)
            attempt, session_token = await create_attempt(
                task_id=task_id, agent_name=agent_name, run_id=run_id, model=agent_model
            )
            dispatch_jobs.append(
                {
                    "settings": settings,
                    "attempt_id": attempt.id,
                    "agent_name": agent_name,
                    "task_id": task_id,
                    "task_prompt": prompt,
                    "task_context": copy.deepcopy(context),
                    "timeout_seconds": timeout_seconds,
                    "env_name": env_name,
                    "session_token": session_token,
                    "model": agent_model,
                }
            )
            attempts_info.append(
                {"attempt_id": attempt.id, "agent": agent_name, "model": agent_model, "status": attempt.status}
            )
        background_tasks.add_task(_dispatch_all, run_id, dispatch_jobs)

        return {
            "run_id": run_id,
            "task_id": task_id,
            "env_name": env_name,
            "agents": agents,
            "attempts": attempts_info,
        }

    @router.get("/runs")
    async def list_runs(limit: int = 50) -> list[dict[str, Any]]:
        return _list_runs_sync(runtime_state.get().db_path, limit=limit)

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run = _get_run_sync(runtime_state.get().db_path, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        return run

    @router.get("/runs/{run_id}/attempts/{attempt_id}")
    async def get_attempt(run_id: str, attempt_id: str) -> dict[str, Any]:
        state = runtime_state.get()
        detail = _get_attempt_detail_sync(state.db_path, attempt_id)
        if detail is None or detail.get("run_id") != run_id:
            raise HTTPException(status_code=404, detail=f"attempt not found under run={run_id}: {attempt_id}")
        attempt_dir = state.data_path / "attempts" / attempt_id
        tool_calls = _read_jsonl(attempt_dir / "trace.jsonl")
        events = _read_jsonl(attempt_dir / "events.jsonl")
        final_state = _read_final_state(attempt_dir / "final_state.json")
        return {**detail, "tool_calls": tool_calls, "events": events, "final_state": final_state}

    @router.get("/runs/{run_id}/attempts/{attempt_id}/thinking")
    async def get_attempt_thinking(run_id: str, attempt_id: str) -> list[dict[str, Any]]:
        state = runtime_state.get()
        return _read_jsonl(state.data_path / "attempts" / attempt_id / "thinking.jsonl")

    @router.get("/runs/{run_id}/attempts/{attempt_id}/trace")
    async def get_attempt_trace(run_id: str, attempt_id: str) -> list[dict[str, Any]]:
        state = runtime_state.get()
        return _read_jsonl(state.data_path / "attempts" / attempt_id / "trace.jsonl")

    @router.get("/runs/{run_id}/attempts/{attempt_id}/events")
    async def get_attempt_events(run_id: str, attempt_id: str) -> list[dict[str, Any]]:
        state = runtime_state.get()
        return _read_jsonl(state.data_path / "attempts" / attempt_id / "events.jsonl")

    @router.post("/runs/{run_id}/stop")
    async def stop_run(run_id: str) -> dict[str, Any]:
        state = runtime_state.get()
        tasks = state.active_tasks.pop(run_id, [])
        cancelled = 0
        for t in tasks:
            if not t.done():
                t.cancel()
                cancelled += 1
        with _open_sync(state.db_path) as conn:
            conn.execute(
                "UPDATE attempts SET status='timeout', error_code='user_stopped',"
                " error_message='stopped by user', ended_at=?"
                " WHERE run_id=? AND status IN ('running','queued')",
                (_now_iso(), run_id),
            )
            conn.execute(
                "UPDATE runs SET status='failed', ended_at=? WHERE id=? AND status IN ('running','queued')",
                (_now_iso(), run_id),
            )
            conn.commit()
        return {"stopped": cancelled, "run_id": run_id}

    @router.get("/runs/{run_id}/attempts/{attempt_id}/artifacts")
    async def list_artifacts(run_id: str, attempt_id: str) -> list[dict[str, Any]]:
        state = runtime_state.get()
        attempt_dir = state.data_path / "attempts" / attempt_id
        root = _artifacts_root(attempt_dir)
        if not root.is_dir():
            return []

        def _file_type(suffix: str) -> str:
            if suffix in (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"):
                return "image"
            if suffix in (".mp4", ".webm", ".mov"):
                return "video"
            if suffix in (".mp3", ".wav", ".ogg"):
                return "audio"
            return "text"

        def _scan_dir(d: Path, *, exclude: set[str] = frozenset()) -> list[dict[str, Any]]:
            return [
                {"name": f.name, "size": f.stat().st_size, "type": _file_type(f.suffix)}
                for f in sorted(d.iterdir())
                if f.is_file() and not f.name.startswith(".") and f.name not in exclude
            ]

        items: list[dict[str, Any]] = []
        root_files = _scan_dir(root, exclude=_ATTEMPT_ROOT_FRAMEWORK_FILES)
        if root_files:
            items.append({"step": "attempt-root", "files": root_files})
        for sub in sorted(root.iterdir()):
            if sub.is_dir():
                files = _scan_dir(sub)
                if files:
                    items.append({"step": sub.name, "files": files})
        return items

    @router.get("/runs/{run_id}/attempts/{attempt_id}/artifacts/{path:path}")
    async def get_artifact(run_id: str, attempt_id: str, path: str):
        from fastapi.responses import FileResponse, PlainTextResponse

        state = runtime_state.get()
        attempt_dir = state.data_path / "attempts" / attempt_id
        root = _artifacts_root(attempt_dir)
        file_path = root / path
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail=f"artifact not found: {path}")
        try:
            file_path.resolve().relative_to(root.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="path traversal blocked")
        binary_types = {
            ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp",
            ".mp4", ".webm", ".mov", ".mp3", ".wav", ".ogg",
        }
        if file_path.suffix in binary_types:
            return FileResponse(file_path)
        return PlainTextResponse(file_path.read_text(encoding="utf-8", errors="replace"))

    @router.post("/upload")
    async def upload_file(request: Request):
        form = await request.form()
        state = runtime_state.get()
        upload_dir = state.data_path / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        saved: list[dict[str, Any]] = []
        for key in form:
            item = form[key]
            if hasattr(item, "read"):
                content = await item.read()
                fname = getattr(item, "filename", key)
                dest = upload_dir / fname
                dest.write_bytes(content)
                saved.append({"name": fname, "path": str(dest.resolve()), "size": len(content)})
        return {"files": saved}

    return router


def register_routes(app: FastAPI, prefix: str = "") -> None:
    app.include_router(build_router(), prefix=prefix)
