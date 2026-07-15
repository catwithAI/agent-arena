"""GDPval rubric scorer backed by a direct Anthropic LLM judge call."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


ENV_DIR = Path(__file__).resolve().parent
INPUTS_DIR = ENV_DIR / "inputs"
RUBRIC_PATH = ENV_DIR / "private" / "official_rubric.json"
EXPERT_WORKBOOK = INPUTS_DIR / "official_expert_deliverable.xlsx"
SOURCE_FILES = [
    INPUTS_DIR / "COA.xlsx",
    INPUTS_DIR / "Aurisic_Prepaid_Expenses_Jan25.pdf",
    INPUTS_DIR / "Aurisic_Prepaid_Expenses_Feb25.pdf",
    INPUTS_DIR / "Aurisic_Prepaid_Expenses_Mar25.pdf",
    INPUTS_DIR / "Aurisic_Prepaid_Expenses_Apr25.pdf",
    INPUTS_DIR / "Aurisic_Prepaid_Insurance.pdf",
]


def _load_judge():
    path = ENV_DIR / "judge_local.py"
    spec = importlib.util.spec_from_file_location("_gdpval_prepaid_official_judge", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _attempt_dir(attempt_id: str, env_db: Path | None) -> Path:
    return env_db.parent if env_db else Path("data/attempts") / attempt_id


def _candidate_workbooks(workspace: Path) -> list[Path]:
    # COA.xlsx is an official task input, not an Agent deliverable.
    return sorted(
        path
        for path in workspace.glob("*.xlsx")
        if path.name.casefold() != "coa.xlsx"
    )


def score(
    *,
    attempt_id: str,
    task: dict[str, Any],
    env_db: Path | None = None,
    **_kwargs: Any,
) -> list[dict[str, Any]]:
    attempt_dir = _attempt_dir(attempt_id, env_db)
    workspace = attempt_dir / "skill_workspace"
    candidates = _candidate_workbooks(workspace)
    eval_dir = attempt_dir / "private_eval" / "gdpval_rubric_judge"
    eval_dir.mkdir(parents=True, exist_ok=True)

    if not candidates:
        return [_result(0, "没有找到 Agent 提交的 Excel workbook，未运行 LLM judge")]

    judge = _load_judge()
    judged = judge.run_rubric_judge(
        candidate_workbooks=candidates,
        source_files=SOURCE_FILES,
        expert_workbook=EXPERT_WORKBOOK,
        rubric_path=RUBRIC_PATH,
        task_prompt=str(task.get("prompt") or ""),
        artifact_dir=eval_dir,
    )
    if not judged.get("ok"):
        return [_result(0, str(judged.get("error") or "LLM judge failed"))]

    review = judged["judge_review"]
    overall = review["overall"]
    detail = (
        f"rubric_points={overall['awarded_points']}/{overall['max_points']}; "
        f"confidence={overall['confidence']}; "
        f"summary={overall.get('summary', '')[:500]}"
    )
    return [_result(int(judged["score_100"]), detail)]


def _result(value: int, detail: str) -> dict[str, Any]:
    return {
        "dimension": "official_rubric_judge",
        "value": value,
        "detail": detail,
    }
