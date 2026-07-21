"""FastAPI entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRouter

from . import runtime_state
from .api import register_routes as register_frontend_routes
from .config import Settings, load_settings
from .db import open_db
from .env_attempt_server import router as env_attempt_router
from .env_loader import load_all_envs
from .runtime_state import RuntimeState

logger = logging.getLogger(__name__)


def _env_is_multi_turn(env: object) -> bool:
    """Whether an env is a multi-turn conversation scenario: any task whose
    context carries a non-empty `_conversation` list. Decided by data, not by
    naming conventions; the frontend uses this to adjust submit options.
    """
    from .conversation.plan import CONVERSATION_CONTEXT_KEY

    for task in getattr(env, "tasks", []) or []:
        ctx = getattr(task, "context", None) or {}
        conv = ctx.get(CONVERSATION_CONTEXT_KEY) if isinstance(ctx, dict) else None
        if isinstance(conv, list) and conv:
            return True
    return False


def create_app(settings: Settings | None = None) -> FastAPI:
    """uvicorn factory entry point.

    `uv run uvicorn backend.main:create_app --factory --port 8100`
    """
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        data_path = cfg.lane.data_path
        db = await open_db(data_path)
        db_path = data_path / "lane.db"
        # allow_unavailable_core: an env whose core.py fails to import still
        # shows up in the list as unavailable instead of blocking startup.
        envs = load_all_envs(cfg.lane.envs_path, allow_unavailable_core=True)
        logger.info("loaded envs: %s", list(envs))

        app.state.settings = cfg
        app.state.db = db
        app.state.db_path = db_path
        app.state.data_path = data_path
        app.state.envs = envs

        runtime_state.set(
            RuntimeState(settings=cfg, db=db, db_path=db_path, data_path=data_path, envs=envs)
        )

        # Wire manifest recovery (backend/wire/recovery.py): only settles
        # manifests left in-progress by an unclean shutdown; orthogonal to
        # attempt-level recovery, failures never block startup.
        try:
            from .wire.recovery import recover_wire_manifests

            wire_recovered = recover_wire_manifests(data_path, db_path)
            if wire_recovered:
                logger.warning("wire recovery handled %d manifest(s)", wire_recovered)
        except Exception:
            logger.exception("wire manifest recovery scan failed (ignored)")

        # Shared httpx client for the reverse HTTP capture proxy.
        from .wire.proxy_api import close_proxy_client, open_proxy_client

        await open_proxy_client(app)
        try:
            yield
        finally:
            await close_proxy_client(app)
            await db.close()
            runtime_state.clear()

    app = FastAPI(title="agent-arena", lifespan=lifespan)
    app.state.settings = cfg
    app.state.envs = {}

    api = APIRouter(prefix="/api")

    @api.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @api.get("/envs")
    async def list_envs() -> list[dict[str, object]]:
        return [
            {
                "name": env.name,
                "skill_id": env.skill_id,
                "description": env.meta.get("description", ""),
                "category": env.meta.get("category", ""),
                "test_focus": env.meta.get("test_focus", ""),
                "pass_threshold": env.meta.get("pass_threshold"),
                "dimensions": env.meta.get("dimensions", []),
                "tool_count": len(env.tools),
                "task_count": len(env.tasks),
                "multi_turn": _env_is_multi_turn(env),
                "available": env.load_error is None,
                "load_error": str(env.load_error) if env.load_error else None,
                # Warn-only local dependency check results; the frontend
                # surfaces them on the env card so the "this run will lose
                # points" pain lands before submit, not after.
                "prerequisite_warnings": env.prerequisite_warnings,
                # Input modalities the agent-side model must support
                # (meta.yaml prerequisites.agent_modalities, machine-readable);
                # the frontend cross-checks them against the selected model's
                # input_modalities.
                "agent_modalities": (
                    (env.meta.get("prerequisites") or {}).get("agent_modalities", [])
                    if isinstance(env.meta.get("prerequisites"), dict) else []
                ),
            }
            for env in app.state.envs.values()
        ]

    @api.get("/envs/{name}/tasks")
    async def list_env_tasks(name: str) -> list[dict[str, object]]:
        env = app.state.envs.get(name)
        if env is None:
            raise HTTPException(status_code=404, detail=f"env not found: {name}")
        return [t.model_dump() for t in env.tasks]

    app.include_router(api)

    # Agent-facing tool callback endpoint stays at the root path.
    app.include_router(env_attempt_router)
    # Frontend-facing API mounts under /api.
    register_frontend_routes(app, prefix="/api")

    # Self-check endpoint (backend/selfcheck.py): GET /api/selfcheck.
    from .selfcheck import register_routes as register_selfcheck_routes

    register_selfcheck_routes(app, prefix="/api")

    # Wire observability read API (backend/wire/api.py): trace/manifest/
    # trajectory/blob endpoints for the run detail view, mounted under /api
    # alongside the rest of the frontend-facing surface.
    from .wire.api import build_wire_router

    app.include_router(build_wire_router(), prefix="/api")

    # Reverse HTTP capture proxy (backend/wire/proxy_api.py): adapters inject
    # this as the model provider base URL, so it must live at the fixed
    # `/internal/wire-proxy/...` path the adapters construct — no /api prefix.
    from .wire.proxy_api import build_proxy_router

    app.include_router(build_proxy_router())
    return app
