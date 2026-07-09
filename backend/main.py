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
        envs = load_all_envs(cfg.lane.envs_path)
        logger.info("loaded envs: %s", list(envs))

        app.state.settings = cfg
        app.state.db = db
        app.state.db_path = db_path
        app.state.data_path = data_path
        app.state.envs = envs

        runtime_state.set(
            RuntimeState(settings=cfg, db=db, db_path=db_path, data_path=data_path, envs=envs)
        )
        try:
            yield
        finally:
            await db.close()
            runtime_state.clear()

    app = FastAPI(title="agent-lane", lifespan=lifespan)
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
    return app
