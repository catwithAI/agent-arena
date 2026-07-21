"""C4-3 验收：evaluation summary 五种状态判定（design §7.3/§8）。

纯函数 table tests：输入固定为 C4-2 的 record 列表 + 构造的 manifest/completeness/
capability gap 组合，覆盖五种状态的判定条件。断言判定的**唯一职责方**是本模块，
detector 返回值不携带状态语义（见 test_wire_subagent_compaction.py 的对偶断言）。
"""

from __future__ import annotations

import pytest

from backend.wire.evaluation import EvaluationInputs, evaluate_compaction


def _record(before="lc1", after="lc2"):
    return {
        "record_type": "context_compaction",
        "data": {"before_call_id": before, "after_call_id": after,
                 "source": "passive-detector"},
    }


# ---------- 状态矩阵 table test ---------------------------------------------
#
# 每行：(说明, EvaluationInputs kwargs, 期望 status)。覆盖五种状态 + 优先级边界。
_MATRIX = [
    # observed：有 record，最高正向优先级，completeness 不翻案。
    ("record 非空 → observed",
     dict(compaction_records=[_record()], max_comparable_calls=2), "observed"),
    ("record 非空即便 aggregate_only 也 observed（不翻案）",
     dict(compaction_records=[_record()], aggregate_only=True), "observed"),
    ("record 非空即便采集失败也 observed",
     dict(compaction_records=[_record()], collection_failed=True), "observed"),

    # incomplete：采集/解析失败（空 record）。
    ("空 record + collection_failed → incomplete",
     dict(collection_failed=True, max_comparable_calls=5,
          pressure_exceeds_declared_window=True), "incomplete"),

    # unsupported：证据不足（aggregate-only / unattributed / session broken）。
    ("空 + aggregate_only → unsupported",
     dict(aggregate_only=True, max_comparable_calls=5,
          pressure_exceeds_declared_window=True), "unsupported"),
    ("空 + identity_unattributed → unsupported",
     dict(identity_unattributed=True, max_comparable_calls=5,
          pressure_exceeds_declared_window=True), "unsupported"),
    ("空 + session_broken → unsupported",
     dict(session_broken=True, max_comparable_calls=5,
          pressure_exceeds_declared_window=True), "unsupported"),

    # insufficient_calls：可比较 call 不足两个（证据完整）。
    ("空 + 证据完整 + calls<2 → insufficient_calls",
     dict(max_comparable_calls=1, pressure_exceeds_declared_window=True),
     "insufficient_calls"),
    ("空 + 证据完整 + calls=0 → insufficient_calls",
     dict(max_comparable_calls=0, pressure_exceeds_declared_window=True),
     "insufficient_calls"),

    # not_observed_under_budget：证据完整 + 压力超过声明窗口仍未触发。
    ("空 + 完整 + calls>=2 + 超窗口 → not_observed_under_budget",
     dict(max_comparable_calls=3, pressure_exceeds_declared_window=True),
     "not_observed_under_budget"),

    # incomplete（覆盖不足）：证据完整 + 有可比较对，但压力**没**压到声明窗口。
    ("空 + 完整 + calls>=2 + 未超窗口 → incomplete（覆盖不足，不当作不支持）",
     dict(max_comparable_calls=3, pressure_exceeds_declared_window=False),
     "incomplete"),
]


@pytest.mark.parametrize("desc,kwargs,expected", _MATRIX,
                         ids=[m[0] for m in _MATRIX])
def test_status_matrix(desc, kwargs, expected):
    out = evaluate_compaction(EvaluationInputs(**kwargs))
    assert out["compaction_status"] == expected, desc


# ---------- 优先级：不把未触发解释为不支持（R7.6）--------------------------


def test_not_observed_requires_window_exceeded():
    # 证据完整、有可比较对，但材料没压到声明窗口——绝不报 not_observed_under_budget
    # （那会被读成"跑够了也没压缩"），而是 incomplete + 覆盖不足 limitation。
    out = evaluate_compaction(EvaluationInputs(
        max_comparable_calls=4, pressure_exceeds_declared_window=False,
    ))
    assert out["compaction_status"] == "incomplete"
    assert "pressure-below-declared-window" in out["limitations"]


def test_unsupported_not_reinterpreted_as_no_support_claim():
    # aggregate-only 是"证据不足无法判定"，不是"agent 不支持压缩"——status
    # unsupported，limitation 标 aggregate-only。
    out = evaluate_compaction(EvaluationInputs(
        aggregate_only=True, max_comparable_calls=5,
        pressure_exceeds_declared_window=True,
    ))
    assert out["compaction_status"] == "unsupported"
    assert "aggregate-only-usage" in out["limitations"]


# ---------- summary 汇总形状 -------------------------------------------------


def test_summary_shape_observed():
    out = evaluate_compaction(EvaluationInputs(
        compaction_records=[_record(), _record("lc3", "lc4")],
        retention_score=0.92, task_score=84,
        observability_completeness="complete", agent_scope="subagent",
    ))
    assert out == {
        "compaction_status": "observed",
        "compaction_count": 2,
        "retention_score": 0.92,
        "task_score": 84,
        "observability_completeness": "complete",
        "agent_scope": "subagent",
        "limitations": [],
    }


def test_observed_with_partial_completeness_reports_limitation():
    # observed 但元数据不全：status 仍 observed（不翻案），但 limitation 如实标缺口，
    # completeness=partial 透传——不会被误读为证据完整。
    out = evaluate_compaction(EvaluationInputs(
        compaction_records=[_record()],
        aggregate_only=True,
        observability_completeness="partial",
        agent_scope="subagent",
    ))
    assert out["compaction_status"] == "observed"
    assert out["observability_completeness"] == "partial"
    assert "aggregate-only-usage" in out["limitations"]


def test_retention_and_task_score_independent_passthrough():
    # retention 与 task score 独立透传，不因 status 改动（design §507）。
    out = evaluate_compaction(EvaluationInputs(
        max_comparable_calls=1, retention_score=0.5, task_score=90,
    ))
    assert out["compaction_status"] == "insufficient_calls"
    assert out["retention_score"] == 0.5
    assert out["task_score"] == 90


def test_extra_limitations_merged_deduped():
    out = evaluate_compaction(EvaluationInputs(
        aggregate_only=True, max_comparable_calls=5,
        pressure_exceeds_declared_window=True,
        extra_limitations=["not observed in codex-cli 0.144.5", "aggregate-only-usage"],
    ))
    lims = out["limitations"]
    # 版本限定声明保留，与自动推导的 aggregate-only 去重（不重复）。
    assert "not observed in codex-cli 0.144.5" in lims
    assert lims.count("aggregate-only-usage") == 1


def test_multiple_gaps_all_surfaced():
    out = evaluate_compaction(EvaluationInputs(
        aggregate_only=True, identity_unattributed=True, session_broken=True,
        max_comparable_calls=5, pressure_exceeds_declared_window=True,
    ))
    assert out["compaction_status"] == "unsupported"
    for gap in ("aggregate-only-usage", "subagent-identity-unattributed",
                "session-continuity-broken"):
        assert gap in out["limitations"]


def test_defaults_empty_input_is_incomplete():
    # 全默认（空 record、无信号、calls=0）：calls<2 → insufficient_calls。
    out = evaluate_compaction(EvaluationInputs())
    assert out["compaction_status"] == "insufficient_calls"
    assert out["compaction_count"] == 0
