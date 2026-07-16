"""Scorer for the EdgeBench Juliet Vulnerability Analyzer task.

The agent receives only materials/public. The hidden evaluator is stored under
materials/hidden and is read only by this scorer.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


_ENV_DIR = Path(__file__).resolve().parent
_HIDDEN_SCORE = _ENV_DIR / "materials" / "hidden" / "evaluator-hidden" / "score_hidden.sh"
_SCORE_RE = re.compile(r"^SCORE=([0-9]+(?:\.[0-9]+)?)$", re.MULTILINE)
_STATUS_RE = re.compile(r"^SCORE_STATUS=(.+)$", re.MULTILINE)


def _attempt_workspace(env_db: Path) -> Path:
    return env_db.parent / "skill_workspace"


def _copy_submission(workspace: Path, dest: Path) -> Path:
    src = workspace / "agent-start"
    if not (src / "analyzer.py").is_file():
        raise FileNotFoundError(f"missing submission analyzer: {src / 'analyzer.py'}")
    shutil.copytree(src, dest / "agent-start", ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "out"))
    return dest / "agent-start"


def _parse_score(output: str) -> tuple[int, str]:
    score_match = _SCORE_RE.search(output)
    status_match = _STATUS_RE.search(output)
    if not score_match:
        return 0, "hidden evaluator did not emit SCORE"
    status = status_match.group(1).strip() if status_match else "UNKNOWN"
    value = round(float(score_match.group(1)))
    return max(0, min(100, value)), status


def _summarize_output(output: str) -> str:
    keep_prefixes = (
        "SCORE=", "SCORE_STATUS=", "RAW_PASSED=", "RAW_TOTAL=",
        "TP=", "FP=", "FN=", "FINDING_F1=", "NEGATIVE_PRECISION=",
        "ADVANCED_F1=", "TRACE_QUALITY=", "PER_CWE=",
    )
    lines = [line for line in output.splitlines() if line.startswith(keep_prefixes)]
    detail = "; ".join(lines)
    if len(detail) > 3500:
        detail = detail[:3500] + "..."
    return detail or output[-1000:]


def score(
    *,
    attempt_id: str,
    task: dict[str, Any],
    env_db: Path,
    trace: list[dict[str, Any]],
    final_state: dict[str, Any],
) -> list[dict[str, Any]]:
    workspace = _attempt_workspace(env_db)
    if not _HIDDEN_SCORE.is_file():
        return [{
            "dimension": "hidden_score",
            "value": 0,
            "detail": f"hidden evaluator missing: {_HIDDEN_SCORE}",
        }]

    with tempfile.TemporaryDirectory(prefix=f"lane-juliet-{attempt_id}-") as td:
        tmp = Path(td)
        submission = _copy_submission(workspace, tmp)
        proc = subprocess.run(
            ["bash", str(_HIDDEN_SCORE), str(submission)],
            cwd=str(_ENV_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
        output = proc.stdout or ""

    value, status = _parse_score(output)
    if proc.returncode != 0 and status == "OK":
        status = f"RETURN_CODE_{proc.returncode}"
    detail = _summarize_output(output)
    try:
        per_cwe_line = next((line for line in output.splitlines() if line.startswith("PER_CWE=")), "")
        if per_cwe_line:
            json.loads(per_cwe_line.split("=", 1)[1])
    except Exception:
        detail += "; PER_CWE parse failed"

    return [{
        "dimension": "hidden_score",
        "value": value,
        "detail": f"status={status}; {detail}",
    }]
