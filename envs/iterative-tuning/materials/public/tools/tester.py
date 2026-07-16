#!/usr/bin/env python3
"""iterative-tuning 本地评分器（黑盒）。

用法：
    python tools/tester.py <params_file>
    python tools/tester.py            # 无参数则从 solution.py 取输出

<params_file> 是一行 12 个空格分隔的数字（每个 ∈ [0,10]），例如：
    5 5 5 5 5 5 5 5 5 5 5 5

输出（stdout）：
    SCORE = <目标值>          成功
    ERROR: <原因>             参数非法

目标函数 objective.py 源码公开（可读），但最优参数不在源码里、隐含在数据里——只能试
参数、看分数、改进。分数越大越好。迭代建议：先跑一版看基线分，再用坐标爬山/随机搜索
逐步调参，反复跑本 tester 直到分数收敛。（提示：朴素的全 5.0 只有约 24% 上限，提升空间很大。）
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

DIMS = 12


def _load_objective():
    """加载 objective（与本 tester 同目录的 objective.py）。

    objective.py 源码是公开的——你可以读它，但读了也没有捷径：最优参数不在源码里，
    只隐含在它的固定数据里，要拟合/搜索才知道。这里直接 import 它来打分。
    """
    cand = Path(__file__).resolve().parent / "objective.py"
    if not cand.is_file():
        raise RuntimeError("评分器不可用（objective.py 缺失）")
    spec = importlib.util.spec_from_file_location("_tuning_objective", str(cand))
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 objective")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_params_from_solution() -> list[float]:
    """跑 solution.py，把它 stdout 的一行 12 个数字当参数。"""
    sol = Path("solution.py")
    if not sol.is_file():
        raise ValueError("未提供 params_file 且当前目录无 solution.py")
    proc = subprocess.run(
        [sys.executable, str(sol)], capture_output=True, text=True, timeout=30
    )
    out = (proc.stdout or "").strip()
    if not out:
        raise ValueError("solution.py 无输出（应打印一行 12 个数字）")
    return _parse(out.splitlines()[-1])


def _parse(line: str) -> list[float]:
    parts = line.replace(",", " ").split()
    if len(parts) != DIMS:
        raise ValueError(f"需要 {DIMS} 个数字，收到 {len(parts)}：{line[:60]!r}")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"含非数字：{line[:60]!r}") from None


def main() -> int:
    try:
        if len(sys.argv) >= 2:
            params = _parse(Path(sys.argv[1]).read_text(encoding="utf-8").strip())
        else:
            params = _read_params_from_solution()
        obj = _load_objective()
        score = obj.evaluate(params)
        print(f"SCORE = {score}")
        return 0
    except Exception as exc:  # noqa: BLE001 — tester 面向 agent，报清错即可
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
