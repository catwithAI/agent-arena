from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.evaluator import evaluate


def _scorer_ok(**kwargs):
    return [
        {"dimension": "task_completion", "value": 90, "detail": "ok"},
        {"dimension": "constraint_compliance", "value": 50, "detail": "partial"},
    ]


def test_evaluate_weighted_average(tmp_path: Path):
    env = SimpleNamespace(
        meta={
            "pass_threshold": 60,
            "dimensions": [
                {"name": "task_completion", "weight": 60},
                {"name": "constraint_compliance", "weight": 40},
            ],
        }
    )
    outcome = evaluate(
        attempt_id="att_1",
        task={},
        env=env,
        data_path=tmp_path,
        scorer=_scorer_ok,
    )
    assert outcome.score_total == round(90 * 0.6 + 50 * 0.4)
    assert outcome.pass_threshold == 60
    assert outcome.passed is True


def test_evaluate_below_threshold_fails(tmp_path: Path):
    env = SimpleNamespace(meta={"pass_threshold": 95, "dimensions": []})
    outcome = evaluate(
        attempt_id="att_1",
        task={},
        env=env,
        data_path=tmp_path,
        scorer=_scorer_ok,
    )
    assert outcome.passed is False


def test_evaluate_no_weights_uses_simple_average(tmp_path: Path):
    env = SimpleNamespace(meta={"pass_threshold": 60, "dimensions": []})
    outcome = evaluate(
        attempt_id="att_1",
        task={},
        env=env,
        data_path=tmp_path,
        scorer=_scorer_ok,
    )
    assert outcome.score_total == round((90 + 50) / 2)
