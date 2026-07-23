"""Env contract linter.

Usage:
    uv run python scripts/lint_env.py order-desk        # single env by name
    uv run python scripts/lint_env.py envs/order-desk   # or by path
    uv run python scripts/lint_env.py --all             # every env

Lint strength stays aligned with the runtime loader by **reusing the same
functions** instead of maintaining a second approximate checklist:

- directory name vs meta.yaml `name`: reuses `env_loader.check_name_consistency()`;
- task files (string id/prompt, positive-integer timeout_seconds, matching
  env_name, object context/constraints, array files): calls
  `env_loader.normalize_task()` and reports any `EnvLoadError`;
- meta.yaml has `schema_version`, tasks/ has at least one JSON: lint-only
  checks (runtime only warns about a missing schema_version; lint requires it).

A manual tool for now — not wired into CI as a hard gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.env_loader import (  # noqa: E402
    ENV_CATEGORIES,
    EnvLoadError,
    check_name_consistency,
    normalize_task,
)


def lint_env(env_dir: Path) -> list[str]:
    errors: list[str] = []
    meta_path = env_dir / "meta.yaml"
    if not meta_path.is_file():
        return ["missing meta.yaml"]
    try:
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [f"meta.yaml YAML parse error: {exc}"]
    if not isinstance(meta, dict):
        return [f"meta.yaml top level must be a mapping, got {type(meta).__name__}"]

    # Same predicate as the runtime loader — never reimplemented here.
    msg = check_name_consistency(meta.get("name"), env_dir.name)
    if msg:
        errors.append(msg)

    if "schema_version" not in meta:
        errors.append("meta.yaml missing schema_version")

    for field_name in ("description", "test_focus"):
        value = meta.get(field_name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"meta.yaml missing non-empty {field_name}")

    category = meta.get("category")
    if category not in ENV_CATEGORIES:
        choices = ", ".join(sorted(ENV_CATEGORIES))
        errors.append(f"meta.yaml category must be one of: {choices}")

    tasks_dir = env_dir / "tasks"
    task_files = sorted(tasks_dir.glob("*.json")) if tasks_dir.is_dir() else []
    if not task_files:
        errors.append("no *.json under tasks/")

    for tf in task_files:
        try:
            task = json.loads(tf.read_text(encoding="utf-8"))
        except ValueError as exc:
            errors.append(f"{tf.name}: JSON parse error: {exc}")
            continue
        if not isinstance(task, dict):
            errors.append(f"{tf.name}: top level must be an object")
            continue
        try:
            # Exactly the same validation the runtime performs (string
            # id/prompt, positive timeout, matching env_name, object
            # context/constraints, array files) — one implementation, no drift.
            normalize_task(env_dir.name, tf.name, dict(task), env_dir)
        except EnvLoadError as exc:
            errors.append(str(exc))

    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("env", nargs="?", help="env name or path (envs/<name>)")
    ap.add_argument("--all", action="store_true", help="lint every directory under envs/")
    args = ap.parse_args()

    if args.all:
        targets = sorted(
            d for d in (REPO / "envs").iterdir()
            if d.is_dir() and not d.name.startswith((".", "_"))
        )
    elif args.env:
        p = Path(args.env)
        target = p if p.is_dir() else REPO / "envs" / args.env
        if not target.is_dir():
            print(f"env directory not found: {args.env}", file=sys.stderr)
            return 2
        targets = [target]
    else:
        ap.print_help()
        return 2

    total_errors = 0
    for d in targets:
        errors = lint_env(d)
        if errors:
            total_errors += len(errors)
            print(f"✗ {d.name}")
            for e in errors:
                print(f"    {e}")
        else:
            print(f"✓ {d.name}")
    print(f"\n{len(targets)} env(s), {total_errors} problem(s)")
    return 1 if total_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
