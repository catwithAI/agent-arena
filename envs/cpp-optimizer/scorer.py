"""Batch scorer for the cpp-optimizer weighted-job-scheduling task.

Compiles the agent's `solution.cpp`, runs it once per hidden case, and
compares stdout to the precomputed reference answer for that case.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ENV_DIR = Path(__file__).parent
CASES_DIR = ENV_DIR / "materials" / "hidden" / "cases"
SOLUTION_BIN = "solution"
CASE_TIMEOUT_S = 5


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
    result = _evaluate_workspace(attempt_dir)

    value = _normalized_score(result)
    detail = (
        f"compile_ok={result['compile_ok']}; "
        f"ok={result['ok_cases']}/{result['cases']}; "
        f"tle={result['timeout_cases']}; "
        f"re={result['runtime_error_cases']}; "
        f"wrong={result['wrong_cases']}"
    )
    if result.get("compile_error"):
        detail += f"; compile_error={str(result['compile_error'])[:300]}"
    return [{"dimension": "batch_score", "value": value, "detail": detail}]


def _normalized_score(result: dict[str, Any]) -> int:
    if not result["compile_ok"] or result["cases"] <= 0:
        return 0
    return round(100 * result["ok_cases"] / result["cases"])


def _attempt_dir(attempt_id: str, env_db: Path | None) -> Path:
    if env_db and env_db.parent.name == attempt_id:
        return env_db.parent
    return Path("data/attempts") / attempt_id


def _evaluate_workspace(workspace: Path) -> dict[str, Any]:
    case_files = sorted(CASES_DIR.glob("*.txt"))
    answers = json.loads((CASES_DIR / "answers.json").read_text(encoding="utf-8"))
    result: dict[str, Any] = {
        "compile_ok": False,
        "cases": len(case_files),
        "ok_cases": 0,
        "timeout_cases": 0,
        "runtime_error_cases": 0,
        "wrong_cases": 0,
        "compile_error": None,
    }
    if not case_files:
        result["compile_error"] = f"no cases found in {CASES_DIR}"
        return result

    source = workspace / "solution.cpp"
    if not source.is_file():
        result["compile_error"] = "solution.cpp not found in workspace"
        return result

    with tempfile.TemporaryDirectory(prefix="lane_cpp_eval_") as tmp:
        work = Path(tmp)
        solution = work / SOLUTION_BIN
        compile_ok, compile_error = _compile(source, solution)
        result["compile_ok"] = compile_ok
        result["compile_error"] = compile_error
        if not compile_ok:
            return result

        for case_file in case_files:
            case_id = case_file.stem
            expected = answers.get(case_id)
            status = _run_case(solution, case_file, expected)
            if status == "OK":
                result["ok_cases"] += 1
            elif status == "TLE":
                result["timeout_cases"] += 1
            elif status == "RE":
                result["runtime_error_cases"] += 1
            else:
                result["wrong_cases"] += 1

    return result


def _compile(source: Path, solution: Path) -> tuple[bool, str | None]:
    env = {**os.environ, "LC_ALL": "C"}
    proc = subprocess.run(
        ["g++", "-std=c++17", "-O2", "-o", str(solution), str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        env=env,
    )
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "compile failed")
    return True, None


def _run_case(solution: Path, case_file: Path, expected: int | None) -> str:
    try:
        with case_file.open("rb") as stdin:
            proc = subprocess.run(
                [str(solution)],
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=CASE_TIMEOUT_S,
            )
    except subprocess.TimeoutExpired:
        return "TLE"
    if proc.returncode != 0:
        return "RE"
    try:
        actual = int(proc.stdout.decode().strip())
    except ValueError:
        return "WA"
    return "OK" if actual == expected else "WA"
