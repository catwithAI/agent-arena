"""Scans `envs/` and loads each env's meta, core tools, scorer, and tasks.

Constraints:

- env directory names may contain hyphens (`travel-planner`), so importing
  `core.py` / `scorer.py` must use `importlib.util.spec_from_file_location` —
  `import envs.travel-planner.core` is not valid Python syntax.
- We never mutate the global `sys.path`. An env's core/scorer must be fully
  self-contained, only importing from the installed top-level `lane`
  package. If an env ever needs a local helper module, use a sibling import
  inside the env directory, not by adding `envs/` to sys.path.
- Any single env failing to load raises `EnvLoadError(env_name=..., stage=...)`
  immediately rather than silently skipping it — partial loads make
  debugging confusing and comparisons unfair.
- Registry ownership: `lane.env_api`'s module-level registry is a transient
  "conveyor belt" for the clear -> import -> snapshot sequence, not something
  held long-term. Each env binds its snapshot to a `LoadedEnv.tools` dict
  immediately after import.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from lane.env_api import RegisteredTool, clear_current_registry, get_current_registry

logger = logging.getLogger(__name__)


class EnvLoadError(RuntimeError):
    def __init__(self, env_name: str, stage: str, detail: str) -> None:
        super().__init__(f"[env={env_name}] {stage} failed: {detail}")
        self.env_name = env_name
        self.stage = stage
        self.detail = detail


@dataclass
class Task:
    """Normalized task definition."""

    id: str
    env_name: str
    prompt: str
    context: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 600
    raw: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "env_name": self.env_name,
            "prompt": self.prompt,
            "context": self.context,
            "constraints": self.constraints,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass
class LoadedEnv:
    name: str
    skill_id: str  # "lane/<name>"
    env_dir: Path
    meta: dict[str, Any]
    tools: dict[str, RegisteredTool]
    scorer: Callable[..., list[dict[str, Any]]] | None
    tasks: list[Task] = field(default_factory=list)


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_meta(env_dir: Path, env_name: str) -> dict[str, Any]:
    meta_path = env_dir / "meta.yaml"
    if not meta_path.is_file():
        raise EnvLoadError(env_name, "meta", f"missing {meta_path}")
    try:
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise EnvLoadError(env_name, "meta", str(exc)) from exc
    if not isinstance(data, dict):
        raise EnvLoadError(env_name, "meta", "top level must be a mapping")
    return data


def _load_core_tools(env_dir: Path, env_name: str) -> dict[str, RegisteredTool]:
    core_path = env_dir / "core.py"
    if not core_path.is_file():
        return {}
    clear_current_registry()
    try:
        _load_module(core_path, f"lane_env_{env_name}_core")
    except Exception as exc:
        raise EnvLoadError(env_name, "core", str(exc)) from exc
    return get_current_registry()


def _load_scorer(env_dir: Path, env_name: str) -> Callable[..., list[dict[str, Any]]] | None:
    scorer_path = env_dir / "scorer.py"
    if not scorer_path.is_file():
        return None
    try:
        module = _load_module(scorer_path, f"lane_env_{env_name}_scorer")
    except Exception as exc:
        raise EnvLoadError(env_name, "scorer", str(exc)) from exc
    score_fn = getattr(module, "score", None)
    if score_fn is None:
        raise EnvLoadError(env_name, "scorer", "module has no `score` function")
    return score_fn


def _load_tasks(env_dir: Path, env_name: str) -> list[Task]:
    tasks_dir = env_dir / "tasks"
    if not tasks_dir.is_dir():
        return []
    tasks: list[Task] = []
    for task_path in sorted(tasks_dir.glob("*.json")):
        try:
            raw = json.loads(task_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EnvLoadError(env_name, "tasks", f"{task_path.name}: {exc}") from exc
        tasks.append(
            Task(
                id=raw.get("id") or raw.get("task_id") or task_path.stem,
                env_name=env_name,
                prompt=raw.get("prompt") or raw.get("query") or "",
                context=raw.get("context", {}),
                constraints=raw.get("constraints", {}),
                timeout_seconds=raw.get("timeout_seconds", raw.get("timeout", 600)),
                raw=raw,
            )
        )
    return tasks


def load_env(env_dir: Path) -> LoadedEnv:
    env_name = env_dir.name
    meta = _load_meta(env_dir, env_name)
    tools = _load_core_tools(env_dir, env_name)
    scorer = _load_scorer(env_dir, env_name)
    tasks = _load_tasks(env_dir, env_name)
    return LoadedEnv(
        name=env_name,
        skill_id=f"lane/{env_name}",
        env_dir=env_dir,
        meta=meta,
        tools=tools,
        scorer=scorer,
        tasks=tasks,
    )


def load_all_envs(envs_path: Path) -> dict[str, LoadedEnv]:
    envs_path = Path(envs_path)
    if not envs_path.is_dir():
        return {}
    loaded: dict[str, LoadedEnv] = {}
    for env_dir in sorted(p for p in envs_path.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        env = load_env(env_dir)
        loaded[env.name] = env
        logger.info("loaded env=%s tools=%d tasks=%d", env.name, len(env.tools), len(env.tasks))
    return loaded
