"""Frontend-facing REST API (mounted with no prefix by `main.create_app()`).

- `GET /agents` — returns the registry catalog, including built-ins,
  versioned profiles/plugins and translated legacy `custom_agents` entries.
- `GET /models/providers` — third-party model provider names configured for
  claude-code/codex (never exposes base_url/api_key_env values).
- `GET /openrouter/models` — full OpenRouter model catalog (bare model ids),
  cached with TTL; used to power the model search dropdown in the UI.
- `POST /runs` — create a comparison run: one attempt per requested agent,
  dispatched concurrently in the background.
- `GET /runs` / `GET /runs/{id}` / `GET /runs/{id}/attempts/{aid}` — history
  and detail views, including trace / thinking / raw events / artifacts.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from . import runtime_state
from .agents.registry import AgentRegistry
from .agents.compatibility import check_compatibility
from .artifact_preview import scheduled_preview_descriptor
from .db import _now_iso, _open_sync
from .run_dispatch import dispatch as dispatch_attempt
from .run_dispatch import _mcp_server_specs
from .run_dispatch import known_agents
from .model_providers import parse_model_ref, resolve_api_key
from .runner import create_attempt

logger = logging.getLogger(__name__)


async def _dispatch_serial(run_id: str, jobs: list[dict[str, Any]]) -> None:
    """Runs the attempts one after another (execution="serial"): the next
    attempt starts only after the previous one finished. Used when attempts
    would compete for exclusive resources (e.g. a local model)."""
    state = runtime_state.get()
    for job in jobs:
        task = asyncio.create_task(dispatch_attempt(**job))
        state.active_tasks[run_id] = [task]
        try:
            await task
        except asyncio.CancelledError:
            logger.info("serial dispatch cancelled at attempt=%s", job.get("attempt_id"))
            break
        except Exception as exc:
            logger.exception("serial dispatch crashed attempt=%s", job.get("attempt_id"), exc_info=exc)
    state.active_tasks.pop(run_id, None)


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


async def _list_agents(settings) -> list[dict[str, Any]]:
    return await AgentRegistry.from_settings(settings).describe_all_async()


# ---------- /runs body ---------------------------------------------------------


class CreateRunRequest(BaseModel):
    env_name: str
    task_id: str | None = None
    prompt: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    # Omitted -> keeps the existing default (1000s), preserving prior
    # behavior for callers that don't know about this field. Explicit
    # `null` -> unlimited: no time-budget notice is injected into the
    # prompt and the adapter enforces no wall-clock deadline.
    timeout_seconds: int | None = 1000
    agents: list[str] = Field(default=["claude-code", "codex"])
    # multi-agent (default): each agent in `agents` runs once.
    # same-model: >=2 distinct agents share one bare model (models is a
    #   {agent: model} map covering every agent, or a single `model`).
    # multi-model: exactly one agent runs once per entry of `models`
    #   (a list of model ids) — same agent, N attempts.
    compare_mode: str = "multi-agent"
    # single model applied to every agent, or a per-agent {agent: model} map,
    # or (multi-model only) a list of models for the one selected agent
    model: str | None = None
    models: dict[str, str] | list[str] | None = None
    # serial (queue attempts back to back) | parallel. Default per mode:
    # same-model runs serial (a local model is an exclusive resource),
    # everything else parallel.
    execution: str | None = None
    # Requested wire capture level, intersected with the server maximum;
    # applied identically to every attempt of the fan-out.
    capture_policy: Literal["off", "metadata", "parsed", "full"] | None = None
    # legacy convenience: a single agent name
    agent: str | None = None

    @model_validator(mode="after")
    def _normalize(self) -> "CreateRunRequest":
        if self.agent and self.agent not in self.agents:
            self.agents = [self.agent]
        return self

    def model_for(self, agent_name: str) -> str | None:
        if isinstance(self.models, dict):
            return self.models.get(agent_name, self.model)
        return self.model


def _resolve_same_model_models(body: CreateRunRequest, agents: list[str]) -> dict[str, str]:
    """Per-agent model resolution for same-model mode.

    A `models` dict must cover every agent — no silent fallback to a default
    model, because the whole point of a same-model comparison is explicit
    control over what each agent runs. The single-value `model` expands to
    all agents (compatible with existing callers).
    """
    if isinstance(body.models, dict):
        missing = [a for a in agents if not body.models.get(a)]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"same-model mode: models must cover every agent, missing: {missing}",
            )
        return {a: body.models[a] for a in agents}
    if body.models is not None:
        raise HTTPException(
            status_code=400,
            detail="same-model mode: models must be an {agent: model} map",
        )
    if body.model:
        return {a: body.model for a in agents}
    raise HTTPException(
        status_code=400,
        detail="same-model mode requires model or models",
    )


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
            " r.compare_mode, r.execution, r.created_at, r.started_at, r.ended_at,"
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
            " error_code, error_message, execution_locus,"
            " security_event_count, security_max_severity"
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
            " created_at, execution_locus, permission_mode, workspace_root,"
            " security_event_count, security_max_severity, security_hitl_json,"
            " security_reaction"
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
    # Security axis: returned alongside score_total, never merged into it.
    detail["security"] = {
        "event_count": detail.pop("security_event_count", 0) or 0,
        "max_severity": detail.pop("security_max_severity", None),
        "hitl": json.loads(detail.pop("security_hitl_json", None) or "{}"),
        "reaction": detail.pop("security_reaction", None),
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


def _read_wire_manifest(attempt_dir: Path) -> dict[str, Any] | None:
    """Read wire-manifest.json (missing/corrupt returns None)."""
    path = attempt_dir / "wire-manifest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _public_agent_manifest(attempt_dir: Path) -> dict[str, Any]:
    """Return a fixed, display-only projection of the framework-owned manifest.

    Launch argv/env/config are intentionally never exposed by this endpoint,
    even if a corrupt or manually modified manifest contains plaintext.
    """
    path = attempt_dir / ".agent-control" / "agent-manifest.json"
    if not path.is_file():
        return {"status": "not_available", "manifest": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "invalid", "manifest": None}
    if not isinstance(raw, dict) or raw.get("schema_version") != "1":
        return {"status": "invalid", "manifest": None}

    def mapping(name: str) -> dict[str, Any]:
        value = raw.get(name)
        return dict(value) if isinstance(value, dict) else {}

    return {
        "status": "available",
        "manifest": {
            "status": raw.get("status"),
            "agent": mapping("agent"),
            "model": mapping("model"),
            "coverage": mapping("coverage"),
            "cleanup": mapping("cleanup"),
            "outcome": mapping("outcome"),
            "degradations": [
                str(value) for value in raw.get("degradations", [])
            ]
            if isinstance(raw.get("degradations"), list)
            else [],
        },
    }
def _read_wire_records(attempt_dir: Path) -> list[dict[str, Any]]:
    """Read canonical wire.jsonl (missing/truncated fail-open)."""
    return _read_jsonl(attempt_dir / "wire.jsonl")


def _build_conversation_block(attempt_dir: Path) -> dict[str, Any]:
    """The attempt's conversation block: summary + turns + evaluation.

    - **summary**: `summarize_conversation` (historical attempts without a
      conversation.jsonl get a legacy single-turn summary);
    - **turns**: per-turn detail, never the prompt text itself (bytes/hash
      only);
    - **evaluation**: the compaction-evaluation summary, mapped from the
      wire manifest + canonical records (with wire data missing the status
      is `incomplete`, never faked).
    """
    from .conversation.summary import conversation_turns, summarize_conversation
    from .wire.evaluation import evaluate_compaction, inputs_from_wire

    summary = summarize_conversation(attempt_dir)
    turns = conversation_turns(attempt_dir)

    manifest = _read_wire_manifest(attempt_dir)
    records = _read_wire_records(attempt_dir)
    eval_inputs = inputs_from_wire(
        manifest=manifest,
        records=records,
        session_continuity=summary.get("session_continuity"),
    )
    evaluation = evaluate_compaction(eval_inputs)

    return {"summary": summary, "turns": turns, "evaluation": evaluation}


# Note: the agent artifact root is now uniquely attempt_dir/skill_workspace
# (see `_artifacts_root`). The attempt root belongs entirely to the
# framework and never enters the artifact namespace, so there is no longer a
# need to exclude framework output file-by-file/dir-by-dir — events/wire/
# trajectory/isolated home are naturally kept out by staying in the root,
# reachable only through the permission-gated Wire API.


def _artifacts_root(attempt_dir: Path) -> Path | None:
    """The single root for agent artifacts: attempt_dir/skill_workspace
    (design: the agent's world boundary).

    claude-code/codex both set their subprocess cwd to this directory, so
    submissions land here uniformly. The attempt root belongs entirely to
    the framework (wire/trajectory/events/isolated home) and never enters
    the artifact namespace, so wire capture is only reachable through the
    permission-gated Wire API — the isolation holds without a file-name
    blacklist.

    skill_workspace could be maliciously replaced with a symlink pointing
    outside the attempt dir; such a root must never enter the namespace.
    """
    workspace = attempt_dir / "skill_workspace"
    if not workspace.is_dir() or workspace.is_symlink():
        return None
    try:
        workspace.resolve().relative_to(attempt_dir.resolve())
    except (OSError, ValueError):
        return None
    return workspace


def _resolve_artifact_path(attempt_dir: Path, path: str) -> Path:
    """Resolve the public artifact ref emitted by `list_artifacts`.

    `.` is a UI namespace label (the root step name), not a physical child
    directory. Resolution happens before the containment check so the same
    path contract is shared by the download and preview endpoints.
    """
    # The URL path must not contain a hidden segment, backslash, or dot
    # traversal. A final `relative_to` check alone isn't enough: `x/../..`
    # could still end up inside root, and a hidden file would bypass the
    # listing filter and be downloaded directly, so raw parts are checked
    # first.
    raw_parts = Path(path).parts
    if (
        not raw_parts
        or "\\" in path
        or any(part == ".." or part.startswith(".") for part in raw_parts)
    ):
        raise HTTPException(status_code=404, detail=f"artifact not found: {path}")
    root = _artifacts_root(attempt_dir)
    if root is None:
        raise HTTPException(status_code=404, detail=f"artifact not found: {path}")
    parts = raw_parts[1:] if raw_parts[0] == "." else raw_parts
    if not parts:
        raise HTTPException(status_code=404, detail=f"artifact not found: {path}")
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail=f"artifact not found: {path}")
    if resolved.is_file():
        return resolved
    raise HTTPException(status_code=404, detail=f"artifact not found: {path}")


def _scan_artifacts(attempt_dir: Path) -> list[dict[str, Any]]:
    """Synchronous bounded artifact scan; API callers run it in a worker thread."""
    from .artifact_preview import inspect_artifact

    def _scan_dir(d: Path) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        if d.is_symlink():
            return files
        try:
            children = sorted(d.iterdir())
            root_resolved = d.resolve()
        except OSError:
            return files
        for f in children:
            if f.is_symlink() or not f.is_file() or f.name.startswith("."):
                continue
            try:
                resolved = f.resolve()
                resolved.relative_to(root_resolved)
                stat = resolved.stat()
            except (OSError, ValueError):
                continue
            inspection = inspect_artifact(resolved)
            files.append(
                {
                    "name": f.name,
                    "size": stat.st_size,
                    "type": inspection.artifact_type,
                    "media_type": inspection.media_type,
                }
            )
        return files

    items: list[dict[str, Any]] = []
    root = _artifacts_root(attempt_dir)
    if root is None:
        return items
    # Files directly under root get the "." step; each subdirectory becomes
    # its own step (preserving the directory-level grouping in the UI).
    root_files = _scan_dir(root)
    if root_files:
        items.append({"step": ".", "files": root_files})
    try:
        subdirs = sorted(root.iterdir())
        root_resolved = root.resolve()
    except OSError:
        return items
    for sub in subdirs:
        if sub.name.startswith(".") or sub.is_symlink() or not sub.is_dir():
            continue
        try:
            sub.resolve().relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        files = _scan_dir(sub)
        if files:
            items.append({"step": sub.name, "files": files})
    return items


def _artifact_attempt_dir(*, data_path: Path, db_path: Path, run_id: str, attempt_id: str) -> Path:
    """Authorize an artifact namespace by the run→attempt relation.

    Artifact refs are scoped by both IDs in the public URL. Looking up files
    by `attempt_id` alone would let a valid attempt be read through an
    unrelated run URL. Return a uniform 404 so callers can't use this
    endpoint to probe attempt ownership.
    """
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM attempts WHERE id=? AND run_id=?", (attempt_id, run_id)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return data_path / "attempts" / attempt_id


def _attempt_change_signature(attempt_dir: Path) -> tuple[Any, ...]:
    """A cheap, comparable snapshot of "has anything about this attempt
    changed" -- used by callers that poll for updates instead of tailing
    files directly."""
    watched = ("events.jsonl", "thinking.jsonl", "stderr.txt")
    signature: list[Any] = []
    for name in watched:
        path = attempt_dir / name
        try:
            stat = path.stat()
            signature.append((name, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signature.append((name, 0, 0))

    artifact_count = 0
    artifact_size = 0
    artifact_mtime = 0
    root = _artifacts_root(attempt_dir)
    if root is not None and root.is_dir():
        # The artifact root is skill_workspace, which never contains
        # framework directories like wire spool/blob, so no further
        # framework-dir filtering is needed here (design §19.4: wire changes
        # are expressed via the manifest generation counter below instead).
        for path in root.rglob("*"):
            if not path.is_file() or path.name.startswith("."):
                continue
            with contextlib.suppress(OSError):
                stat = path.stat()
                artifact_count += 1
                artifact_size += stat.st_size
                artifact_mtime = max(artifact_mtime, stat.st_mtime_ns)
    signature.append(("artifacts", artifact_count, artifact_size, artifact_mtime))

    # Wire finalize/rebuild changes the manifest without necessarily
    # changing mtime/size in a way pollers would notice reliably, so the
    # monotonic finalize generation counter is part of the signature;
    # fall back to mtime/size if the manifest can't be read.
    manifest_path = attempt_dir / "wire-manifest.json"
    try:
        generation = json.loads(manifest_path.read_text(encoding="utf-8")).get("generation")
        signature.append(("wire-manifest", "generation", generation))
    except (OSError, json.JSONDecodeError, AttributeError):
        try:
            stat = manifest_path.stat()
            signature.append(("wire-manifest", stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signature.append(("wire-manifest", 0, 0))
    return tuple(signature)


# OpenRouter 全量模型列表：给模型下拉列裸模型名。几百个模型 + 接口有延迟，
# 模块级 TTL 缓存 + Lock 防并发穿透；失败时回退旧缓存（stale-while-error）。
def _arch_modalities(m: dict[str, Any], key: str) -> list[str]:
    arch = m.get("architecture")
    if not isinstance(arch, dict):
        return []
    values = arch.get(key)
    if not isinstance(values, list):
        return []
    return [v for v in values if isinstance(v, str)]


_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OR_CACHE_TTL_SECONDS = 600.0
_or_models_cache: dict[str, Any] = {"data": None, "expires_at": 0.0}
_or_models_lock = asyncio.Lock()


async def _openrouter_models() -> dict[str, Any]:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return {"models": [], "error": "OPENROUTER_API_KEY not set"}
    now = time.monotonic()
    cached = _or_models_cache.get("data")
    if cached is not None and _or_models_cache.get("expires_at", 0.0) > now:
        return {"models": cached, "error": None}
    async with _or_models_lock:
        now = time.monotonic()
        cached = _or_models_cache.get("data")
        if cached is not None and _or_models_cache.get("expires_at", 0.0) > now:
            return {"models": cached, "error": None}
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            ) as cli:
                resp = await cli.get(_OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("data") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                raise TypeError("unexpected OpenRouter models response")
            models = [
                {
                    "id": m["id"],
                    "name": m.get("name") or m["id"],
                    "context_length": m.get("context_length"),
                    # architecture.input/output_modalities: the frontend shows
                    # capability badges and cross-checks them against the env's
                    # declared agent_modalities (e.g. an image-input env run
                    # with a text-only model is doomed before it starts).
                    "input_modalities": _arch_modalities(m, "input_modalities"),
                    "output_modalities": _arch_modalities(m, "output_modalities"),
                }
                for m in raw
                if isinstance(m, dict) and isinstance(m.get("id"), str)
            ]
            _or_models_cache["data"] = models
            _or_models_cache["expires_at"] = now + _OR_CACHE_TTL_SECONDS
            return {"models": models, "error": None}
        except Exception as exc:
            if cached is not None:
                logger.warning("openrouter models fetch failed, serving stale: %s", exc)
                return {"models": cached, "error": None, "stale": True}
            return {"models": [], "error": str(exc)}


# ---------- routes -------------------------------------------------------------


def build_router() -> APIRouter:
    router = APIRouter(tags=["frontend"])

    @router.get("/agents")
    async def list_agents(request: Request) -> list[dict[str, Any]]:
        return await request.app.state.agent_registry.describe_all_async()

    @router.get("/models/providers")
    async def list_model_providers(request: Request) -> dict[str, Any]:
        settings = request.app.state.settings
        return {
            "providers": sorted(settings.model_providers.keys()),
            "suggested": settings.model_suggestions,
        }

    @router.get("/openrouter/models")
    async def openrouter_models(request: Request) -> dict[str, Any]:
        """OpenRouter 全量模型（裸模型名）。模型下拉数据源；缓存见
        _openrouter_models。key 走进程环境变量 OPENROUTER_API_KEY，响应不含 key。"""
        return await _openrouter_models()

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

        if body.compare_mode not in ("multi-agent", "same-model", "multi-model"):
            raise HTTPException(
                status_code=400, detail=f"unknown compare_mode: {body.compare_mode!r}"
            )
        if body.execution not in (None, "serial", "parallel"):
            raise HTTPException(
                status_code=400,
                detail=f"unknown execution: {body.execution!r}, expected serial | parallel",
            )
        execution = body.execution or (
            "serial" if body.compare_mode == "same-model" else "parallel"
        )

        same_model_map: dict[str, str] | None = None
        if body.compare_mode == "same-model":
            if len(agents) < 2:
                raise HTTPException(
                    status_code=400, detail="same-model mode requires at least 2 agents"
                )
            if len(set(agents)) != len(agents):
                raise HTTPException(
                    status_code=400, detail="same-model mode: agents must be distinct"
                )
            same_model_map = _resolve_same_model_models(body, agents)

        if body.compare_mode == "multi-model":
            if len(agents) != 1:
                raise HTTPException(
                    status_code=400, detail="multi-model mode requires exactly one agent"
                )
            if not isinstance(body.models, list) or len(body.models) < 2:
                raise HTTPException(
                    status_code=400,
                    detail="multi-model mode: models must be a list of at least 2 entries",
                )

        # Job plan: one (agent_name, model) per attempt. multi-model iterates
        # over models with the single agent fixed; the other modes iterate
        # over agents.
        if body.compare_mode == "multi-model":
            job_plan = [(agents[0], m) for m in (body.models or [])]
        elif body.compare_mode == "same-model":
            assert same_model_map is not None
            job_plan = [(a, same_model_map[a]) for a in agents]
        else:
            job_plan = [(a, body.model_for(a)) for a in agents]

        task_id, env_name = _get_or_create_adhoc_task_sync(state.db_path, body)

        with _open_sync(state.db_path) as conn:
            row = conn.execute(
                "SELECT prompt, context_json, timeout_seconds FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        prompt = row["prompt"] if row else (body.prompt or "")
        context = json.loads(row["context_json"]) if row and row["context_json"] else (body.context or {})
        timeout_seconds = row["timeout_seconds"] if row and row["timeout_seconds"] is not None else body.timeout_seconds

        # Multi-turn conversation task-definition validation: an invalid
        # definition is rejected with a 400 at the entry point and no
        # attempt is created; dispatch runs the same parser as defense in
        # depth.
        from .conversation.plan import (
            CONVERSATION_CONTEXT_KEY,
            ConversationPlanError,
            parse_conversation,
        )
        conversation_turns = ()
        if CONVERSATION_CONTEXT_KEY in (context or {}):
            try:
                conversation_turns = parse_conversation(
                    context[CONVERSATION_CONTEXT_KEY], task_id=task_id
                )
            except ConversationPlanError as exc:
                raise HTTPException(
                    status_code=400, detail=f"invalid conversation: {exc}"
                )

        # Compatibility is checked for the complete fan-out before the first
        # attempt is created. This keeps comparison groups atomic: one bad
        # Agent/model/MCP combination cannot leave a partial run behind.
        try:
            declared_mcp_servers = _mcp_server_specs(state.envs[env_name])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid MCP entrypoint: {exc}")
        registry = AgentRegistry.from_settings(settings)
        incompatible_reports = []
        for agent_name, agent_model in job_plan:
            resolved = registry.resolve(agent_name)
            availability = await registry.probe_availability(agent_name)
            provider_protocol = None
            provider_auth_available = None
            if agent_model is not None:
                model_ref = parse_model_ref(agent_model, settings.model_providers or {})
                if model_ref.provider is not None:
                    provider = settings.model_providers[model_ref.provider]
                    provider_protocol = provider.kind
                    if provider.api_key_env is not None:
                        provider_auth_available = resolve_api_key(provider) is not None
            report = check_compatibility(
                resolved.spec,
                availability=availability,
                requested_model=agent_model,
                provider_protocol=provider_protocol,
                provider_auth_available=provider_auth_available,
                mcp_servers=declared_mcp_servers,
                conversation_turns=conversation_turns,
            )
            if not report.compatible:
                incompatible_reports.append(report.as_dict())
        if incompatible_reports:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "agent_compatibility_mismatch",
                    "reports": incompatible_reports,
                },
            )

        run_id = f"run_{uuid.uuid4().hex[:12]}"

        attempts_info = []
        dispatch_jobs = []
        for agent_name, agent_model in job_plan:
            attempt, session_token = await create_attempt(
                task_id=task_id, agent_name=agent_name, run_id=run_id,
                model=agent_model, compare_mode=body.compare_mode,
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
                    # The same requested policy applies to every attempt of
                    # the fan-out.
                    "capture_policy": body.capture_policy,
                }
            )
            attempts_info.append(
                {"attempt_id": attempt.id, "agent": agent_name, "model": agent_model, "status": attempt.status}
            )
        # The run row was created by the first create_attempt; record the
        # execution mode after the fact.
        with _open_sync(state.db_path) as conn:
            conn.execute("UPDATE runs SET execution=? WHERE id=?", (execution, run_id))
            conn.commit()

        dispatcher = _dispatch_serial if execution == "serial" else _dispatch_all
        background_tasks.add_task(dispatcher, run_id, dispatch_jobs)

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
        # Category breakdown from the per-event detail file (DB only stores the
        # summary columns).
        sec_events = _read_jsonl(attempt_dir / "security_events.jsonl")
        by_category: dict[str, int] = {}
        for e in sec_events:
            if e.get("phase") == "executed":
                cat = e.get("category", "?")
                by_category[cat] = by_category.get(cat, 0) + 1
        if isinstance(detail.get("security"), dict):
            detail["security"]["by_category"] = by_category
        # Conversation block (summary/turns/evaluation). Multi-turn attempts
        # have a conversation.jsonl; historical single-turn attempts get the
        # legacy summary + empty turns.
        conversation = await asyncio.to_thread(_build_conversation_block, attempt_dir)
        return {
            **detail,
            "tool_calls": tool_calls,
            "events": events,
            "final_state": final_state,
            "conversation": conversation,
        }

    @router.get("/runs/{run_id}/attempts/{attempt_id}/agent-manifest")
    async def get_agent_manifest(run_id: str, attempt_id: str) -> dict[str, Any]:
        state = runtime_state.get()
        attempt_dir = await asyncio.to_thread(
            _artifact_attempt_dir,
            data_path=state.data_path,
            db_path=state.db_path,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        return await asyncio.to_thread(_public_agent_manifest, attempt_dir)

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

    @router.get("/runs/{run_id}/attempts/{attempt_id}/security_events")
    async def get_attempt_security_events(
        run_id: str, attempt_id: str
    ) -> list[dict[str, Any]]:
        """Per-event security detail: category/severity/target/locus/
        hitl_status/rule_id/source_ref, traceable back to a specific trace
        line."""
        state = runtime_state.get()
        return _read_jsonl(
            state.data_path / "attempts" / attempt_id / "security_events.jsonl"
        )

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
        attempt_dir = await asyncio.to_thread(
            _artifact_attempt_dir,
            data_path=state.data_path,
            db_path=state.db_path,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        return await asyncio.to_thread(_scan_artifacts, attempt_dir)

    @router.get("/runs/{run_id}/attempts/{attempt_id}/artifact-previews/{path:path}")
    async def get_artifact_preview(run_id: str, attempt_id: str, path: str):
        """Office document preview descriptor (backend/artifact_preview.py).
        Never returns the raw, untrusted Office bytes -- only a scanned,
        structural summary safe to render directly."""
        state = runtime_state.get()
        attempt_dir = await asyncio.to_thread(
            _artifact_attempt_dir,
            data_path=state.data_path,
            db_path=state.db_path,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        file_path = await asyncio.to_thread(_resolve_artifact_path, attempt_dir, path)
        return await asyncio.to_thread(
            scheduled_preview_descriptor, file_path, path, attempt_dir / "artifact-previews"
        )

    @router.get("/runs/{run_id}/attempts/{attempt_id}/artifacts/{path:path}")
    async def get_artifact(run_id: str, attempt_id: str, path: str):
        from fastapi.responses import FileResponse, PlainTextResponse

        from .artifact_preview import inspect_artifact

        state = runtime_state.get()
        attempt_dir = await asyncio.to_thread(
            _artifact_attempt_dir,
            data_path=state.data_path,
            db_path=state.db_path,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        file_path = await asyncio.to_thread(_resolve_artifact_path, attempt_dir, path)
        inspection = await asyncio.to_thread(inspect_artifact, file_path)
        if inspection.artifact_type == "text":
            content = await asyncio.to_thread(
                file_path.read_text, encoding="utf-8", errors="replace"
            )
            return PlainTextResponse(content)
        # Office/unknown binary is always downloaded or rendered by a
        # dedicated endpoint; it must never pass through
        # read_text(errors="replace").
        return FileResponse(file_path, media_type=inspection.media_type)

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
