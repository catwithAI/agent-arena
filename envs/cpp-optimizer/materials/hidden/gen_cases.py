"""Deterministic-seed generator for weighted job scheduling test cases.

Not randomized at import/collection time — call `generate(seed)` explicitly.
Cases are written once (e.g. via `if __name__ == "__main__"`) and checked
into `cases/`, so re-running the scorer doesn't regenerate/shift them.
"""

from __future__ import annotations

import random
from pathlib import Path


def generate(seed: int, n: int, max_time: int = 1_000_000_000, max_weight: int = 1_000_000) -> str:
    rng = random.Random(seed)
    jobs = []
    for _ in range(n):
        s = rng.randint(0, max_time - 1)
        e = rng.randint(s + 1, max_time)
        w = rng.randint(1, max_weight)
        jobs.append((s, e, w))
    lines = [str(n)] + [f"{s} {e} {w}" for s, e, w in jobs]
    return "\n".join(lines) + "\n"


def reference_answer(case_text: str) -> int:
    lines = case_text.strip().splitlines()
    n = int(lines[0])
    jobs = []
    for line in lines[1 : n + 1]:
        s, e, w = map(int, line.split())
        jobs.append((s, e, w))
    jobs.sort(key=lambda j: j[1])
    ends = [j[1] for j in jobs]
    dp = [0] * (n + 1)
    for i in range(1, n + 1):
        s, e, w = jobs[i - 1]
        lo, hi = 0, i - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ends[mid - 1] <= s:
                lo = mid
            else:
                hi = mid - 1
        dp[i] = max(dp[i - 1], dp[lo] + w)
    return dp[n]


CASE_SIZES = [5, 50, 500, 5000, 20000, 20000]


if __name__ == "__main__":
    cases_dir = Path(__file__).parent / "cases"
    cases_dir.mkdir(exist_ok=True)
    answers = {}
    for i, n in enumerate(CASE_SIZES):
        text = generate(seed=1000 + i, n=n)
        (cases_dir / f"{i:02d}.txt").write_text(text, encoding="utf-8")
        answers[f"{i:02d}"] = reference_answer(text)
    import json

    (cases_dir / "answers.json").write_text(json.dumps(answers, indent=2), encoding="utf-8")
    print(f"generated {len(CASE_SIZES)} cases into {cases_dir}")
