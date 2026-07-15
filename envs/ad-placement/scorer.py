"""Batch scorer for the EdgeBench ad-placement optimization task."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ENV_DIR = Path(__file__).parent
CASES_DIR = ENV_DIR / "inputs" / "cases"
TESTER = ENV_DIR / "tools" / "bin" / "tester"
SOLUTION_BIN = "solution"
CASE_TIMEOUT_S = 5
TESTER_TIMEOUT_S = 10
SCORE_RE = re.compile(r"Score\s*=\s*(\d+)")


def score(
    *,
    attempt_id: str,
    task: dict,
    env_db: Path | None = None,
    trace: list | None = None,
    final_state: dict | None = None,
    **_kwargs: Any,
) -> list[dict[str, Any]]:
    attempt_dir = _attempt_dir(attempt_id, env_db)
    skill_workspace = attempt_dir / "skill_workspace"
    skill_workspace.mkdir(parents=True, exist_ok=True)

    # Agent submissions land under skill_workspace (the shared cwd convention
    # for coding envs; see docs/environments.md).
    result = _evaluate_workspace(skill_workspace)
    sidecar = skill_workspace / "eval_result.json"
    sidecar.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    value = _normalized_score(result)
    detail = (
        f"TOTAL_SCORE={result['total_score']}; "
        f"normalized_score={value}; "
        f"compile_ok={result['compile_ok']}; "
        f"ok={result['ok_cases']}/{result['cases']}; "
        f"tle={result['timeout_cases']}; "
        f"re={result['runtime_error_cases']}; "
        f"wa={result['invalid_cases']}; "
        f"sidecar={sidecar}"
    )
    if result.get("compile_error"):
        detail += f"; compile_error={str(result['compile_error'])[:300]}"
    return [{"dimension": "batch_score", "value": value, "detail": detail}]


def _normalized_score(result: dict[str, Any]) -> int:
    if not result["compile_ok"] or result["cases"] <= 0:
        return 0
    max_score = int(result["cases"]) * 1_000_000_000
    if max_score <= 0:
        return 0
    return round(100 * int(result["total_score"]) / max_score)


def _attempt_dir(attempt_id: str, env_db: Path | None) -> Path:
    if env_db and env_db.parent.name == attempt_id:
        return env_db.parent
    return Path("data/attempts") / attempt_id


def _evaluate_workspace(workspace: Path) -> dict[str, Any]:
    case_files = sorted(CASES_DIR.glob("*.txt"))
    result: dict[str, Any] = {
        "compile_ok": False,
        "cases": len(case_files),
        "ok_cases": 0,
        "timeout_cases": 0,
        "runtime_error_cases": 0,
        "invalid_cases": 0,
        "total_score": 0,
        "case_scores": [],
        "compile_error": None,
    }
    if not case_files:
        result["compile_error"] = f"no cases found in {CASES_DIR}"
        return result
    if not TESTER.is_file():
        result["compile_error"] = f"tester missing: {TESTER}"
        return result

    sources = sorted(
        p for p in workspace.rglob("*.cpp")
        if "tools" not in p.relative_to(workspace).parts
    )
    if not sources:
        result["compile_error"] = "no C++ source found"
        return result

    with tempfile.TemporaryDirectory(prefix="lane_ad_eval_") as tmp:
        eval_dir = Path(tmp)
        shutil.copytree(workspace, eval_dir / "work", dirs_exist_ok=True)
        work = eval_dir / "work"
        source_rel = _select_submission_source(workspace, sources)
        compile_ok, compile_error = _compile(work, source_rel)
        result["compile_ok"] = compile_ok
        result["compile_error"] = compile_error
        if not compile_ok:
            return result

        solution = work / SOLUTION_BIN
        for case_file in case_files:
            case_result = _run_case(solution, case_file, eval_dir)
            result["case_scores"].append(case_result)
            status = case_result["status"]
            if status == "OK":
                result["ok_cases"] += 1
                result["total_score"] += int(case_result["score"])
            elif status == "TLE":
                result["timeout_cases"] += 1
            elif status == "RE":
                result["runtime_error_cases"] += 1
            else:
                result["invalid_cases"] += 1

    return result


def _select_submission_source(workspace: Path, sources: list[Path]) -> Path:
    """Choose the final submission source, not auxiliary scratch files."""
    preferred = workspace / "solution.cpp"
    if preferred.is_file():
        return preferred.relative_to(workspace)
    root_sources = [p for p in sources if p.parent == workspace]
    if len(root_sources) == 1:
        return root_sources[0].relative_to(workspace)
    return sources[0].relative_to(workspace)


def _compile(work: Path, source_rel: Path) -> tuple[bool, str | None]:
    solution = work / SOLUTION_BIN
    makefile = work / "Makefile"
    env = {**os.environ, "LC_ALL": "C"}
    if makefile.is_file():
        proc = subprocess.run(
            ["make", "-C", str(work)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            env=env,
        )
        if proc.returncode == 0 and os.access(solution, os.X_OK):
            return True, None

    proc = subprocess.run(
        ["g++", "-std=c++17", "-O2", "-o", str(solution), str(work / source_rel)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        env=env,
    )
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "compile failed")
    return True, None


def _run_case(solution: Path, case_file: Path, eval_dir: Path) -> dict[str, Any]:
    case_id = case_file.stem
    out_file = eval_dir / f"out_{case_id}.txt"
    try:
        with case_file.open("rb") as stdin, out_file.open("wb") as stdout:
            proc = subprocess.run(
                [str(solution)],
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.DEVNULL,
                timeout=CASE_TIMEOUT_S,
            )
    except subprocess.TimeoutExpired:
        return {"case_id": case_id, "status": "TLE", "score": 0}
    if proc.returncode != 0:
        return {"case_id": case_id, "status": "RE", "score": 0}

    try:
        tester = subprocess.run(
            [str(TESTER), str(case_file), str(out_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=TESTER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"case_id": case_id, "status": "WA", "score": 0, "message": "tester timeout"}
    match = SCORE_RE.search(tester.stderr or "")
    parsed_score = int(match.group(1)) if match else 0
    if tester.returncode == 0 and match:
        return {"case_id": case_id, "status": "OK", "score": parsed_score}
    return {
        "case_id": case_id,
        "status": "WA",
        "score": parsed_score,
        "message": (tester.stderr or "")[:300],
    }
