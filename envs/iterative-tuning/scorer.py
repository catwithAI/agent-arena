"""Scorer for iterative-tuning：跑 agent 的 solution.py 取 12 参数，用私有
objective 打分，归一化成"占已知最优的百分比"。

设计意图（测"实验驱动迭代"意愿）：朴素解（全 5.0）≈ 44% 上限；愿意用本地 tester
迭代（哪怕最朴素的坐标爬山）就能到 90%+。低分 = 没充分迭代。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ENV_DIR = Path(__file__).parent
# objective 是白盒（无最优参数明文，最优隐含在数据里）——scorer 与 agent 用同一份
# materials/public/tools/objective.py，评分口径一致。
OBJECTIVE_PATH = ENV_DIR / "materials" / "public" / "tools" / "objective.py"
SOLUTION = "solution.py"
RUN_TIMEOUT_S = 30
DIMS = 12


def score(
    *,
    attempt_id: str,
    task: dict,
    env_db: Path | None = None,
    trace: list | None = None,
    final_state: dict | None = None,
    **_kwargs: Any,
) -> list[dict[str, Any]]:
    workspace = _attempt_dir(attempt_id, env_db) / "skill_workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    result = _evaluate(workspace)
    sidecar = workspace / "eval_result.json"
    sidecar.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    value = int(result["percent"])  # 占已知最优百分比，0-100（可略超 100 则夹到 100）
    detail = (
        f"objective={result['objective']}; known_best={result['known_best']}; "
        f"percent={result['percent']}; params={result['params']}; "
        f"status={result['status']}"
    )
    if result.get("error"):
        detail += f"; error={str(result['error'])[:300]}"
    return [{"dimension": "tuning_score", "value": value, "detail": detail}]


def _evaluate(workspace: Path) -> dict[str, Any]:
    base = {
        "objective": None, "known_best": None, "percent": 0,
        "params": None, "status": "unknown", "error": None,
    }
    sol = workspace / SOLUTION
    if not sol.is_file():
        return {**base, "status": "no_submission", "error": f"缺少 {SOLUTION}"}

    # 跑 solution.py 取一行 12 个数字。用相对文件名 + cwd（避免绝对路径在某些
    # 环境下被 cwd 二次拼接）。
    try:
        proc = subprocess.run(
            [sys.executable, SOLUTION], cwd=str(workspace),
            capture_output=True, text=True, timeout=RUN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {**base, "status": "timeout", "error": "solution.py 运行超时"}
    if proc.returncode != 0:
        return {**base, "status": "runtime_error",
                "error": (proc.stderr or "")[:300] or "solution.py 非零退出"}

    out = (proc.stdout or "").strip()
    if not out:
        return {**base, "status": "empty_output", "error": "solution.py 无输出"}
    try:
        params = _parse(out.splitlines()[-1])
    except ValueError as exc:
        return {**base, "status": "invalid_output", "error": str(exc)}

    # 用私有 objective 打分。
    try:
        obj = _load_objective()
        val = obj.evaluate(params)
        known = float(obj.KNOWN_BEST)
    except Exception as exc:  # noqa: BLE001
        return {**base, "params": params, "status": "invalid_params", "error": str(exc)}

    percent = max(0, min(100, round(val / known * 100)))
    return {
        "objective": val, "known_best": known, "percent": percent,
        "params": params, "status": "ok", "error": None,
    }


def _parse(line: str) -> list[float]:
    parts = line.replace(",", " ").split()
    if len(parts) != DIMS:
        raise ValueError(f"需要 {DIMS} 个数字，收到 {len(parts)}")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise ValueError("输出含非数字") from None


def _load_objective():
    spec = importlib.util.spec_from_file_location("_tuning_obj", OBJECTIVE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 objective: {OBJECTIVE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _attempt_dir(attempt_id: str, env_db: Path | None) -> Path:
    if env_db and env_db.parent.name == attempt_id:
        return env_db.parent
    # 回落：从 env_db 往上找 attempts/<id>，或用相对 data 路径。
    if env_db:
        for p in env_db.parents:
            if (p / attempt_id).is_dir():
                return p / attempt_id
    return Path("data") / "attempts" / attempt_id
