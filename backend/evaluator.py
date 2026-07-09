"""Evaluator — reads trace + final_state, calls the env's scorer, writes scores.

Scorer signature (`envs/<env>/scorer.py:score`):

    score(*, attempt_id, task, env_db, trace, final_state) -> list[dict]
        each item has dimension / value / detail.

Total score: weighted average using `meta.yaml`'s `dimensions[*].weight`; if
weights are missing, dimensions are equally weighted. `pass_threshold` comes
from the top-level `meta.yaml` field, defaulting to 60.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class EvaluationOutcome:
    score_total: int
    pass_threshold: int
    scores: list[dict[str, Any]]
    passed: bool


def load_trace(data_path: Path, attempt_id: str) -> list[dict[str, Any]]:
    return _load_jsonl(data_path / "attempts" / attempt_id / "trace.jsonl")


def load_final_state(data_path: Path, attempt_id: str) -> dict[str, Any]:
    p = data_path / "attempts" / attempt_id / "final_state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _load_jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    items: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def load_events(data_path: Path, attempt_id: str) -> list[dict[str, Any]]:
    return _load_jsonl(data_path / "attempts" / attempt_id / "events.jsonl")


def load_thinking(data_path: Path, attempt_id: str) -> list[dict[str, Any]]:
    return _load_jsonl(data_path / "attempts" / attempt_id / "thinking.jsonl")


def env_db_path(data_path: Path, attempt_id: str) -> Path:
    return data_path / "attempts" / attempt_id / "env.db"


def _extract_meta(env: Any) -> tuple[int, dict[str, int]]:
    meta: dict[str, Any] = getattr(env, "meta", {}) or {}
    pass_threshold = int(meta.get("pass_threshold", 60))
    weights: dict[str, int] = {}
    for dim in meta.get("dimensions", []) or []:
        if not isinstance(dim, dict):
            continue
        name = dim.get("name")
        if not name:
            continue
        try:
            weights[name] = int(dim.get("weight", 0))
        except (TypeError, ValueError):
            weights[name] = 0
    return pass_threshold, weights


def _aggregate_total(scores: list[dict[str, Any]], weights: dict[str, int]) -> int:
    """Weighted average (dimensions with weight 0 are excluded; falls back to
    a simple average if all weights are 0). Each dimension scores out of
    100, and so does score_total."""
    weighted_sum = 0
    weight_total = 0
    fallback_values: list[int] = []
    for s in scores:
        try:
            value = int(s.get("value", 0))
        except (TypeError, ValueError):
            value = 0
        fallback_values.append(value)
        w = weights.get(s.get("dimension", ""), 0)
        if w > 0:
            weighted_sum += value * w
            weight_total += w
    if weight_total > 0:
        return round(weighted_sum / weight_total)
    if fallback_values:
        return round(sum(fallback_values) / len(fallback_values))
    return 0


def evaluate(
    *,
    attempt_id: str,
    task: dict[str, Any],
    env: Any,
    data_path: Path,
    scorer: Callable[..., list[dict[str, Any]]],
) -> EvaluationOutcome:
    """Run one evaluation. If the scorer raises, the exception propagates —
    the runner catches it and sets a `scoring_failed` status."""
    trace = load_trace(data_path, attempt_id)
    final_state = load_final_state(data_path, attempt_id)
    db_path = env_db_path(data_path, attempt_id)
    raw_scores = scorer(
        attempt_id=attempt_id,
        task=task,
        env_db=db_path,
        trace=trace,
        final_state=final_state,
    )
    if not isinstance(raw_scores, list):
        raise TypeError(f"scorer must return list, got {type(raw_scores).__name__}")
    pass_threshold, weights = _extract_meta(env)
    score_total = _aggregate_total(raw_scores, weights)

    return EvaluationOutcome(
        score_total=score_total,
        pass_threshold=pass_threshold,
        scores=raw_scores,
        passed=score_total >= pass_threshold,
    )


def write_scores_sync(db_path: Path, attempt_id: str, scores: list[dict[str, Any]]) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM scores WHERE attempt_id=?", (attempt_id,))
        for s in scores:
            conn.execute(
                "INSERT INTO scores(attempt_id, dimension, value, detail) VALUES(?, ?, ?, ?)",
                (attempt_id, str(s.get("dimension", "")), int(s.get("value", 0)), str(s.get("detail", ""))),
            )
        conn.commit()


def write_attempt_score_sync(db_path: Path, attempt_id: str, score_total: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE attempts SET score_total=? WHERE id=?", (score_total, attempt_id))
        conn.commit()
