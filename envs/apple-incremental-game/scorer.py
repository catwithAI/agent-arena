"""Batch scorer for the EdgeBench apple-incremental-game optimization task."""

from __future__ import annotations

import json
import os
import py_compile
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ENV_DIR = Path(__file__).parent
CASES_DIR = ENV_DIR / "inputs" / "cases"
TESTER = ENV_DIR / "materials" / "public" / "tools" / "bin" / "tester"
SOLUTION = "solution.py"
CASE_TIMEOUT_S = 30
TESTER_TIMEOUT_S = 60
MAX_WORKERS = 8
SCORE_RE = re.compile(r"Score\s*[=:]\s*(-?\d+(?:\.\d+)?)")

# EdgeBench reports an unbounded optimization score. Scale the raw total into a
# more readable number, but do not cap it so optimization quality stays ordered.
RAW_POINTS_PER_OCTAGON_POINT = 1_000_000


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
    workspace = attempt_dir / "skill_workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    result = _evaluate_workspace(workspace)
    sidecar = workspace / "eval_result.json"
    sidecar.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    value = _scaled_score(result)
    detail = (
        f"TOTAL_SCORE={result['total_score']}; "
        f"scaled_score={value}; "
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


def _scaled_score(result: dict[str, Any]) -> int:
    if not result["compile_ok"] or result["cases"] <= 0:
        return 0
    return round(int(result["total_score"]) / RAW_POINTS_PER_OCTAGON_POINT)


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

    solution = workspace / SOLUTION
    if not solution.is_file():
        result["compile_error"] = f"missing {SOLUTION}"
        return result

    try:
        py_compile.compile(str(solution), doraise=True)
    except py_compile.PyCompileError as exc:
        result["compile_error"] = exc.msg
        return result

    result["compile_ok"] = True

    with tempfile.TemporaryDirectory(prefix="lane_apple_eval_") as tmp:
        eval_dir = Path(tmp)
        shutil.copytree(workspace, eval_dir / "work", dirs_exist_ok=True)
        work = eval_dir / "work"

        workers = min(MAX_WORKERS, len(case_files)) or 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_run_case, work, case_file, eval_dir) for case_file in case_files]
            for fut in as_completed(futures):
                case_result = fut.result()
                result["case_scores"].append(case_result)

    result["case_scores"].sort(key=lambda item: item["case_id"])
    for case_result in result["case_scores"]:
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


def _run_case(work: Path, case_file: Path, eval_dir: Path) -> dict[str, Any]:
    case_id = case_file.stem
    out_file = eval_dir / f"out_{case_id}.txt"
    env = {**os.environ, "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        with case_file.open("rb") as stdin, out_file.open("wb") as stdout:
            proc = subprocess.run(
                ["python3", SOLUTION],
                cwd=str(work),
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                timeout=CASE_TIMEOUT_S,
                env=env,
            )
    except subprocess.TimeoutExpired:
        return {"case_id": case_id, "status": "TLE", "score": 0}
    if proc.returncode != 0:
        return {
            "case_id": case_id,
            "status": "RE",
            "score": 0,
            "message": (proc.stderr or b"").decode("utf-8", errors="replace")[:300],
        }

    try:
        tester = subprocess.run(
            [str(TESTER), str(case_file), str(out_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=TESTER_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"case_id": case_id, "status": "WA", "score": 0, "message": "tester timeout"}

    output = (tester.stdout or "") + "\n" + (tester.stderr or "")
    match = SCORE_RE.search(output)
    if match:
        return {"case_id": case_id, "status": "OK", "score": int(float(match.group(1)))}
    return {
        "case_id": case_id,
        "status": "WA",
        "score": 0,
        "message": output[:300],
    }
