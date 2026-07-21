"""Scans `envs/` and loads each env's meta, core tools, scorer, and tasks.

Constraints:

- env directory names may contain hyphens (`travel-planner`), so importing
  `core.py` / `scorer.py` must use `importlib.util.spec_from_file_location` —
  `import envs.travel-planner.core` is not valid Python syntax.
- We never mutate the global `sys.path`. An env's core/scorer must be fully
  self-contained, only importing from the installed top-level `lane`
  package. If an env ever needs a local helper module, use a sibling import
  inside the env directory, not by adding `envs/` to sys.path.
  `load_all_envs` still snapshots and restores sys.path so a misbehaving env
  cannot poison the process even if it violates this rule.
- Any single env failing to load raises `EnvLoadError(env_name=..., stage=...)`
  immediately rather than silently skipping it — partial loads make
  debugging confusing and comparisons unfair. Service startup may opt into
  `allow_unavailable_core=True` so an env whose core import fails still shows
  up in the list as unavailable (`LoadedEnv.load_error`), deferring the hard
  error until someone actually uses that env.
- Registry ownership: `lane.env_api`'s module-level registry is a transient
  "conveyor belt" for the clear -> import -> snapshot sequence, not something
  held long-term. Each env binds its snapshot to a `LoadedEnv.tools` dict
  immediately after import.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import shutil
import sys
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
    """Normalized task definition.

    Field names follow the canonical schema: `task_id -> id`,
    `query -> prompt`, `timeout -> timeout_seconds`. Every task JSON an env
    ships is validated against this schema at load time; legacy field names
    are translated by the loader.
    """

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
    tasks_by_id: dict[str, Task] = field(default_factory=dict)
    # Set when core import failed but the loader was told to keep going
    # (`allow_unavailable_core=True`); the env is listed as unavailable and
    # this error is raised lazily when someone tries to run it.
    load_error: EnvLoadError | None = None
    # Prerequisite check results: warn-only, surfaced in the env list so the
    # UI can flag "this env will underperform on this machine".
    prerequisite_warnings: list[str] = field(default_factory=list)


_VALID_ENV_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


def check_name_consistency(meta_name: Any, env_dir_name: str) -> str | None:
    """Check that meta.yaml `name` matches the directory name.

    Returns an error description or None. Shared between the runtime loader
    and `scripts/lint_env.py` so the two never drift apart.
    """
    if meta_name and meta_name != env_dir_name:
        return f"meta.yaml name={meta_name!r} does not match directory name {env_dir_name!r}"
    return None


# ---------- prerequisite checking ----------------------------------------

# Only matches whole lines of the form "short-candidate" or
# "candidate/candidate (note)": candidates are ASCII binary-ish names without
# spaces (e.g. "python3", "LibreOffice/soffice（office_render）"). Full
# natural-language sentences (spaces, CJK connectives) do not match — we
# would rather skip an undecidable requirement than misread prose as a
# binary name and emit a false warning.
_BINARY_HINT = re.compile(
    r"^([A-Za-z][A-Za-z0-9_.+-]{0,39})"
    r"(?:\s*/\s*([A-Za-z][A-Za-z0-9_.+-]{0,39}))?"
    r"(?:\s*[（(][^）)]*[）)])?\s*$"
)


def _extract_binary_candidates(requires: list[Any]) -> list[list[str]]:
    """Heuristically extract checkable binary names from `prerequisites.requires`.

    "python3" -> [["python3"]]; "LibreOffice/soffice（office_render）" ->
    [["LibreOffice", "soffice"]] (slash-separated alternatives — any one hit
    satisfies the group); natural-language items and env-var instructions
    like "export SELECTED_SKILLS_DIR=..." produce no candidates.
    """
    candidates: list[list[str]] = []
    for item in requires:
        if not isinstance(item, str):
            continue
        m = _BINARY_HINT.match(item.strip())
        if not m:
            continue
        names = [g for g in (m.group(1), m.group(2)) if g]
        if names:
            candidates.append(names)
    return candidates


def check_prerequisites(meta: dict[str, Any]) -> list[str]:
    """Existence-check the decidable binary requirements from meta.yaml.

    Returns a list of warnings. Warn-only by design: a dev machine missing
    some tool is a normal state and must not block startup — structural
    errors are what `EnvLoadError` is for.
    """
    prereqs = meta.get("prerequisites")
    if not isinstance(prereqs, dict):
        return []
    requires = prereqs.get("requires")
    if not isinstance(requires, list):
        return []
    warnings: list[str] = []
    for names in _extract_binary_candidates(requires):
        if not any(_dependency_available(n) for n in names):
            on_missing = prereqs.get("on_missing") or ""
            suffix = f" ({on_missing})" if on_missing else ""
            warnings.append(
                f"{meta.get('name', '?')}: missing {' or '.join(names)}{suffix}"
            )
    return warnings


def _dependency_available(name: str) -> bool:
    """Whether a candidate dependency is available locally: a PATH binary or
    an importable Python package.

    Requirements like "PyMuPDF/fitz（pdf→png）" are Python packages that look
    identical to binary names, so a which()-only check would keep warning on
    machines that have the package installed — find_spec is the fallback and
    is still a purely local check (no network).
    """
    if shutil.which(name):
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ---------- module loading -------------------------------------------------


def _module_name_for(env_name: str, kind: str) -> str:
    # Hyphens are invalid in module names; the prefix avoids clashing with
    # any real installed package.
    normalized = env_name.replace("-", "_")
    return f"lane_env_{normalized}_{kind}"


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Registering in sys.modules gives relative references inside
    # exec_module a fallback; the `lane_env_<name>_<kind>` naming cannot
    # collide with a real package.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


# ---------- per-stage loaders ----------------------------------------------


def _load_meta(env_dir: Path, env_name: str) -> dict[str, Any]:
    meta_path = env_dir / "meta.yaml"
    if not meta_path.is_file():
        raise EnvLoadError(env_name, "meta", f"missing {meta_path}")
    try:
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EnvLoadError(env_name, "meta", str(exc)) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise EnvLoadError(
            env_name, "meta", f"top level must be a mapping, got {type(data).__name__}"
        )
    # Missing schema_version is warn-only at runtime (lint requires it):
    # hard-failing would break every legacy env at once.
    if "schema_version" not in data:
        logger.warning("env %s: meta.yaml missing schema_version", env_name)
    return data


def _load_core_tools(env_dir: Path, env_name: str) -> dict[str, RegisteredTool]:
    core_path = env_dir / "core.py"
    if not core_path.is_file():
        return {}
    clear_current_registry()
    try:
        _load_module(core_path, _module_name_for(env_name, "core"))
    except Exception as exc:
        raise EnvLoadError(env_name, "core", str(exc)) from exc
    finally:
        tools = get_current_registry()
        clear_current_registry()  # don't leak this env's tools into the next
    return tools


def _load_scorer(env_dir: Path, env_name: str) -> Callable[..., list[dict[str, Any]]] | None:
    scorer_path = env_dir / "scorer.py"
    if not scorer_path.is_file():
        return None
    try:
        module = _load_module(scorer_path, _module_name_for(env_name, "scorer"))
    except Exception as exc:
        raise EnvLoadError(env_name, "scorer", str(exc)) from exc
    score_fn = getattr(module, "score", None)
    if score_fn is None:
        raise EnvLoadError(env_name, "scorer", "module has no `score` function")
    return score_fn


def normalize_task(env_name: str, filename: str, data: dict[str, Any], env_dir: Path) -> Task:
    """Normalize a raw task dict to the canonical schema, validating strictly.

    - `task_id` -> `id`
    - `query` -> `prompt`
    - `timeout` -> `timeout_seconds`
    - `files` -> `context.uploaded_files` (task input materials; dispatch
      copies them into each agent's workspace)

    Shared with `scripts/lint_env.py` so lint and runtime enforce the exact
    same contract.
    """
    task_id = data.get("id") or data.get("task_id")
    if not task_id or not isinstance(task_id, str):
        raise EnvLoadError(
            env_name, "tasks", f"{filename} missing a string id/task_id field"
        )
    prompt = data.get("prompt") or data.get("query")
    if not prompt or not isinstance(prompt, str):
        raise EnvLoadError(
            env_name, "tasks", f"{filename} missing a string prompt/query field"
        )
    timeout_seconds = data.get("timeout_seconds")
    if timeout_seconds is None:
        timeout_seconds = data.get("timeout", 600)
    if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise EnvLoadError(
            env_name,
            "tasks",
            f"{filename} timeout_seconds must be a positive integer, got {timeout_seconds!r}",
        )
    declared_env = data.get("env_name", env_name)
    if declared_env != env_name:
        raise EnvLoadError(
            env_name,
            "tasks",
            f"{filename} env_name={declared_env!r} does not match directory name {env_name!r}",
        )
    context = data.get("context") or {}
    constraints = data.get("constraints") or {}
    if not isinstance(context, dict):
        raise EnvLoadError(env_name, "tasks", f"{filename} context must be an object")
    if not isinstance(constraints, dict):
        raise EnvLoadError(env_name, "tasks", f"{filename} constraints must be an object")

    files = data.get("files")
    if files is not None:
        if not isinstance(files, list):
            raise EnvLoadError(env_name, "tasks", f"{filename} files must be an array")
        uploaded = list(context.get("uploaded_files") or [])
        seen_names = {uf.get("name") for uf in uploaded if isinstance(uf, dict)}
        for entry in files:
            if isinstance(entry, str):
                name, raw_path = Path(entry).name, entry
            elif isinstance(entry, dict) and entry.get("path"):
                raw_path = str(entry["path"])
                name = str(entry.get("name") or Path(raw_path).name)
            else:
                raise EnvLoadError(
                    env_name, "tasks",
                    f"{filename} files entries must be strings or objects with path: {entry!r}",
                )
            p = Path(raw_path)
            # Prefer env-dir-relative paths (self-contained env materials);
            # otherwise keep as-is and let dispatch resolve against the
            # project root. Existence is hard-checked at dispatch time.
            if not p.is_absolute() and (env_dir / p).is_file():
                raw_path = str((env_dir / p).resolve())
            if name not in seen_names:
                uploaded.append({"name": name, "path": raw_path})
                seen_names.add(name)
        context["uploaded_files"] = uploaded
    return Task(
        id=task_id,
        env_name=env_name,
        prompt=prompt,
        context=context,
        constraints=constraints,
        timeout_seconds=timeout_seconds,
        raw=data,
    )


def _load_tasks(env_dir: Path, env_name: str) -> list[Task]:
    tasks_dir = env_dir / "tasks"
    if not tasks_dir.is_dir():
        return []
    seen_ids: set[str] = set()
    tasks: list[Task] = []
    for task_path in sorted(tasks_dir.glob("*.json")):
        try:
            raw = json.loads(task_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EnvLoadError(env_name, "tasks", f"{task_path.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise EnvLoadError(env_name, "tasks", f"{task_path.name} top level must be an object")
        task = normalize_task(env_name, task_path.name, raw, env_dir)
        if task.id in seen_ids:
            raise EnvLoadError(
                env_name, "tasks", f"duplicate task id: {task.id} ({task_path.name})"
            )
        seen_ids.add(task.id)
        tasks.append(task)
    return tasks


# ---------- entry points -----------------------------------------------------


def load_env(env_dir: Path, *, allow_unavailable_core: bool = False) -> LoadedEnv:
    env_name = env_dir.name
    if not _VALID_ENV_NAME.match(env_name):
        raise EnvLoadError(
            env_name,
            "name",
            f"directory name must be lowercase letters/digits/underscore/hyphen: {env_name!r}",
        )

    meta = _load_meta(env_dir, env_name)
    name_error = check_name_consistency(meta.get("name"), env_name)
    if name_error:
        raise EnvLoadError(env_name, "meta", name_error)

    prereq_warnings = check_prerequisites(meta)
    for w in prereq_warnings:
        logger.warning("env prerequisite: %s", w)

    load_error: EnvLoadError | None = None
    try:
        tools = _load_core_tools(env_dir, env_name)
    except EnvLoadError as exc:
        if not allow_unavailable_core:
            raise
        logger.warning("env core unavailable, deferring error until use: %s", exc)
        tools = {}
        load_error = exc
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
        tasks_by_id={t.id: t for t in tasks},
        load_error=load_error,
        prerequisite_warnings=prereq_warnings,
    )


def load_all_envs(envs_path: Path, *, allow_unavailable_core: bool = False) -> dict[str, LoadedEnv]:
    envs_path = Path(envs_path)
    if not envs_path.is_dir():
        return {}
    original_sys_path = list(sys.path)
    loaded: dict[str, LoadedEnv] = {}
    try:
        for env_dir in sorted(
            p for p in envs_path.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))
        ):
            # Skip directories that are clearly not envs.
            if not (env_dir / "meta.yaml").is_file() and not (env_dir / "core.py").is_file():
                continue
            env = load_env(env_dir, allow_unavailable_core=allow_unavailable_core)
            loaded[env.name] = env
            logger.info("loaded env=%s tools=%d tasks=%d", env.name, len(env.tools), len(env.tasks))
    finally:
        # Restore sys.path even when a load blows up mid-scan.
        if sys.path != original_sys_path:
            sys.path[:] = original_sys_path
    return loaded
