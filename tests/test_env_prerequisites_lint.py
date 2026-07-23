"""Prerequisite checking + env contract lint.

Covers: the binary-candidate regex against real-world samples (natural-language
requirement lines must not be misread as binary names), check_prerequisites
negative paths, and lint rejecting each class of invalid task with exactly the
same function the runtime loader uses (normalize_task — one implementation,
so agreement is structural, not tested-by-approximation).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.env_loader import (
    ENV_CATEGORIES,
    EnvLoadError,
    _extract_binary_candidates,
    check_name_consistency,
    check_prerequisites,
    normalize_task,
)

REPO = Path(__file__).resolve().parent.parent


# ---------- _extract_binary_candidates --------------------------------------


@pytest.mark.parametrize("requires,expected", [
    (["python3", "bash"], [["python3"], ["bash"]]),
    (["LibreOffice/soffice（office_render）"], [["LibreOffice", "soffice"]]),
    (["PyMuPDF/fitz（pdf→png）"], [["PyMuPDF", "fitz"]]),
    (["g++ / clang (C++17)"], [["g++", "clang"]]),
    (["ffmpeg / ffprobe"], [["ffmpeg", "ffprobe"]]),
    # Natural-language / env-var lines: skipped, never misreported.
    (["支持图片检查的 llm_judge.model 或默认模型"], []),
    (["export SELECTED_SKILLS_DIR=/path/to/selected-skills"], []),
    (["完成该场景所需的浏览、计算或文件理解能力"], []),
    (["python3 标准库"], []),
    (["Linux 运行环境（tester 为 x86-64 ELF）"], []),
    ([123, None], []),  # non-string entries never crash
])
def test_extract_binary_candidates(requires, expected):
    assert _extract_binary_candidates(requires) == expected


def test_check_prerequisites_warns_on_missing_binary():
    meta = {"name": "x", "prerequisites": {
        "requires": ["definitely-not-a-real-binary-xyz"],
        "on_missing": "scores will suffer",
    }}
    ws = check_prerequisites(meta)
    assert len(ws) == 1 and "definitely-not-a-real-binary-xyz" in ws[0]
    assert "scores will suffer" in ws[0]


def test_check_prerequisites_passes_when_any_candidate_hits():
    # python3 is guaranteed present; any hit within a group satisfies it.
    meta = {"name": "x", "prerequisites": {
        "requires": ["nonexistent-alt/python3"],
    }}
    assert check_prerequisites(meta) == []


def test_check_prerequisites_tolerates_malformed_meta():
    assert check_prerequisites({}) == []
    assert check_prerequisites({"prerequisites": "not-a-dict"}) == []
    assert check_prerequisites({"prerequisites": {"requires": "not-a-list"}}) == []


# ---------- check_name_consistency -------------------------------------------


def test_name_consistency():
    assert check_name_consistency("travel-planner", "travel-planner") is None
    assert check_name_consistency(None, "x") is None  # missing name ≠ mismatch
    assert "does not match" in check_name_consistency("a", "b")


# ---------- lint: task validation is the loader's own (normalize_task) -------


def _make_env(tmp_path: Path, task: dict) -> Path:
    env_dir = tmp_path / "lint-target"
    (env_dir / "tasks").mkdir(parents=True)
    (env_dir / "meta.yaml").write_text(
        'name: lint-target\n'
        'schema_version: "1.0"\n'
        'category: baseline\n'
        'test_focus: "Basic capability test."\n'
        'description: "Temporary environment used by lint tests."\n',
        encoding="utf-8",
    )
    (env_dir / "tasks" / "t1.json").write_text(
        json.dumps(task, ensure_ascii=False), encoding="utf-8"
    )
    return env_dir


def _run_lint(env_dir: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lint_env.py"), str(env_dir)],
        capture_output=True, text=True, cwd=REPO,
    )
    return proc.returncode, proc.stdout + proc.stderr


BAD_TASKS = [
    ({"prompt": "p"}, "id/task_id"),                                    # missing id
    ({"id": 123, "prompt": "p"}, "id/task_id"),                         # id not str
    ({"id": "t", "prompt": ""}, "prompt/query"),                        # empty prompt
    ({"id": "t", "prompt": "p", "timeout_seconds": 0}, "timeout"),      # timeout 0
    ({"id": "t", "prompt": "p", "timeout_seconds": -5}, "timeout"),     # negative
    ({"id": "t", "prompt": "p", "timeout_seconds": "600"}, "timeout"),  # string
    ({"id": "t", "prompt": "p", "env_name": "other"}, "env_name"),      # mismatch
    ({"id": "t", "prompt": "p", "context": [1]}, "context"),            # not object
    ({"id": "t", "prompt": "p", "constraints": [1]}, "constraints"),    # not object
    ({"id": "t", "prompt": "p", "files": {"a": 1}}, "files"),           # not array
]


@pytest.mark.parametrize("task,keyword", BAD_TASKS)
def test_lint_rejects_matches_loader(tmp_path, task, keyword):
    env_dir = _make_env(tmp_path, task)
    # The runtime path (normalize_task) must reject it...
    with pytest.raises(EnvLoadError):
        normalize_task(env_dir.name, "t1.json", dict(task), env_dir)
    # ...and lint must report the same class of error (same function).
    rc, out = _run_lint(env_dir)
    assert rc == 1, out
    assert keyword in out, out


def test_lint_passes_valid_env(tmp_path):
    env_dir = _make_env(tmp_path, {"id": "t", "prompt": "p"})
    rc, out = _run_lint(env_dir)
    assert rc == 0, out


def test_lint_missing_schema_version(tmp_path):
    env_dir = _make_env(tmp_path, {"id": "t", "prompt": "p"})
    (env_dir / "meta.yaml").write_text(
        "name: lint-target\n"
        "category: baseline\n"
        "test_focus: Basic capability test.\n"
        "description: Temporary environment used by lint tests.\n",
        encoding="utf-8",
    )
    rc, out = _run_lint(env_dir)
    assert rc == 1 and "schema_version" in out


def test_environment_category_taxonomy_is_stable():
    assert ENV_CATEGORIES == {
        "general-assistant",
        "office-productivity",
        "real-skill",
        "complex-workflow",
        "coding",
        "agent-system",
        "safety-hitl",
        "baseline",
    }


def test_lint_rejects_missing_scenario_metadata(tmp_path):
    env_dir = _make_env(tmp_path, {"id": "t", "prompt": "p"})
    meta_path = env_dir / "meta.yaml"
    meta_path.write_text(
        meta_path.read_text(encoding="utf-8").replace(
            'test_focus: "Basic capability test."\n', ""
        ),
        encoding="utf-8",
    )
    rc, out = _run_lint(env_dir)
    assert rc == 1 and "test_focus" in out


def test_lint_rejects_unknown_category(tmp_path):
    env_dir = _make_env(tmp_path, {"id": "t", "prompt": "p"})
    meta_path = env_dir / "meta.yaml"
    meta_path.write_text(
        meta_path.read_text(encoding="utf-8").replace(
            "category: baseline", "category: one-off-category"
        ),
        encoding="utf-8",
    )
    rc, out = _run_lint(env_dir)
    assert rc == 1 and "category" in out


def test_lint_missing_meta(tmp_path):
    env_dir = tmp_path / "no-meta"
    env_dir.mkdir()
    rc, out = _run_lint(env_dir)
    assert rc == 1 and "meta.yaml" in out


def test_lint_all_bundled_envs_pass():
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lint_env.py"), "--all"],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
