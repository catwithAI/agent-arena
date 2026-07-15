"""Scorer for the PPT visual repair env.

All Office rendering and LLM-as-judge logic is intentionally local to this env
so the agent-lane platform core remains unchanged.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

ENV_DIR = Path(__file__).resolve().parent
INPUTS_DIR = ENV_DIR / "inputs"
PUBLIC_RUBRIC = INPUTS_DIR / "ppt_0003" / "public_rubric.md"
REQUIRED_OUTPUTS = ["polished.pptx"]


def _load_local(name: str):
    path = ENV_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_ppt_visual_repair_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def score(
    *,
    attempt_id: str,
    task: dict,
    env_db: Path | None = None,
    trace: list | None = None,
    final_state: dict | None = None,
    **_kwargs: Any,
) -> list[dict[str, Any]]:
    case = _resolve_case(task)
    attempt_dir = _attempt_dir(attempt_id, env_db)
    workspace = attempt_dir / "skill_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    eval_dir = _private_eval_dir(attempt_dir)
    eval_dir.mkdir(parents=True, exist_ok=True)

    artifact_value, artifact_detail = _score_artifact_contract(workspace)
    if not (workspace / "polished.pptx").is_file():
        render_value, render_detail, previews = 0, "skipped because polished.pptx is missing", None
        judge_value, judge_detail = 0, "skipped because polished.pptx is missing"
    else:
        render_value, render_detail, previews = _score_office_render(workspace, eval_dir, case)
        judge_value, judge_detail = _score_llm_judge(workspace, eval_dir, previews, case) if previews else (0, "skipped because Office previews are unavailable")

    sidecar = {
        "source_case_id": case["id"],
        "artifact_contract": {"value": artifact_value, "detail": artifact_detail},
        "office_render": {"value": render_value, "detail": render_detail, "previews": {k: str(v) for k, v in (previews or {}).items()}},
        "llm_visual_judge": {"value": judge_value, "detail": judge_detail},
    }
    (eval_dir / "score_sidecar.json").write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")

    return [
        {"dimension": "artifact_contract", "value": artifact_value, "detail": artifact_detail},
        {"dimension": "office_render", "value": render_value, "detail": render_detail},
        {"dimension": "llm_visual_judge", "value": judge_value, "detail": judge_detail},
    ]


def _attempt_dir(attempt_id: str, env_db: Path | None) -> Path:
    if env_db and env_db.parent.name == attempt_id:
        return env_db.parent
    return Path("data/attempts") / attempt_id


def _private_eval_dir(attempt_dir: Path) -> Path:
    return attempt_dir.parent / "_private_eval" / attempt_dir.name / "ppt_visual_repair_eval"


def _resolve_case(task: dict[str, Any]) -> dict[str, Any]:
    context = task.get("context") if isinstance(task, dict) else None
    context = context if isinstance(context, dict) else {}
    case_id = str(context.get("source_case_id") or "ppt_0003")
    if not case_id.startswith("ppt_") or not case_id[4:].isdigit():
        raise ValueError(f"invalid PPT source_case_id: {case_id!r}")
    case_dir = INPUTS_DIR / case_id
    case = {
        "id": case_id,
        "clean": case_dir / "private" / "clean.pptx",
        "draft": case_dir / "agent_materials" / "corrupted.pptx",
        "summary": case_dir / "private" / "corruption_summary.md",
    }
    missing = [name for name, path in case.items() if name != "id" and not path.is_file()]
    if missing:
        raise FileNotFoundError(f"PPT case {case_id} missing assets: {', '.join(missing)}")
    return case


def _score_artifact_contract(workspace: Path) -> tuple[int, str]:
    office_preview = _load_local("office_preview")
    missing = [name for name in REQUIRED_OUTPUTS if not (workspace / name).is_file()]
    if missing:
        return 0, f"missing required outputs: {', '.join(missing)}"
    empty = [name for name in REQUIRED_OUTPUTS if (workspace / name).stat().st_size <= 0]
    if empty:
        return 20, f"empty required outputs: {', '.join(empty)}"
    try:
        office_preview.validate_pptx(workspace / "polished.pptx")
    except Exception as exc:
        return 40, f"polished.pptx is not valid: {exc}"
    return 100, "required output exists; polished.pptx is valid OOXML"


def _score_office_render(workspace: Path, eval_dir: Path, case: dict[str, Any]) -> tuple[int, str, dict[str, Path] | None]:
    office_preview = _load_local("office_preview")
    polished = workspace / "polished.pptx"
    cache_dir = eval_dir / "previews"
    try:
        clean_pages = office_preview.ensure_png_previews(case["clean"], cache_dir / "clean")
        corrupted_pages = office_preview.ensure_png_previews(case["draft"], cache_dir / "corrupted")
        polished_pages = office_preview.ensure_png_previews(polished, cache_dir / "polished")
    except Exception as exc:
        return 0, f"Office render failed: {exc}", None
    if not clean_pages or not corrupted_pages or not polished_pages:
        return 0, "Office render produced no pages", None
    if len(clean_pages) != len(polished_pages):
        return 45, f"page count changed: reference={len(clean_pages)}, polished={len(polished_pages)}", {
            "reference": clean_pages[0],
            "draft": corrupted_pages[0],
            "candidate": polished_pages[0],
        }
    return 100, f"rendered reference/draft/candidate previews; pages={len(polished_pages)}", {
        "reference": clean_pages[0],
        "draft": corrupted_pages[0],
        "candidate": polished_pages[0],
    }


def _score_llm_judge(workspace: Path, eval_dir: Path, previews: dict[str, Path], case: dict[str, Any]) -> tuple[int, str]:
    judge = _load_local("judge_local")
    report_path = workspace / "design_notes.md"
    result = judge.run_visual_judge(
        reference_png=previews["reference"],
        draft_png=previews["draft"],
        candidate_png=previews["candidate"],
        public_rubric=PUBLIC_RUBRIC.read_text(encoding="utf-8"),
        corruption_summary=case["summary"].read_text(encoding="utf-8"),
        design_notes=report_path.read_text(encoding="utf-8", errors="replace") if report_path.is_file() else "",
        artifact_dir=eval_dir / "judge",
    )
    if not result.get("ok"):
        return 0, result.get("error", "LLM judge failed or skipped")
    review = result["judge_review"]
    overall = review.get("overall", {})
    return int(result["score_100"]), (
        f"judge_score={overall.get('total_score')}/{overall.get('max_score')}; "
        f"recommendation={overall.get('accept_recommendation')}; "
        f"note={review.get('review_note_draft', '')[:300]}"
    )
