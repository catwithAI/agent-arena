"""C4-4 验收：retention scorer（design §7/§8）。

覆盖精确匹配、等价格式、缺失、幻觉补全、部分保留五类用例。断言 scorer 只读
probe 答案与 facts manifest（answer_hash / 结构化 expected），不读 reasoning。
"""

from __future__ import annotations

import pytest

from backend.wire.retention import (
    RETENTION_NORMALIZER_VERSION,
    Fact,
    FactsManifest,
    answer_hash,
    normalize_answer,
    score_retention,
)


def _manifest(*facts: Fact, **meta) -> FactsManifest:
    return FactsManifest(facts=list(facts), **meta)


def _verdicts(res):
    return {fs.fact_id: (fs.verdict, fs.score) for fs in res.per_fact}


# ---------- answer normalization -------------------------------------------


def test_normalize_thousands_separator_equivalent():
    assert normalize_answer("1,000") == normalize_answer("1000")


def test_normalize_case_and_whitespace():
    assert normalize_answer("  True ") == normalize_answer("true")
    assert normalize_answer("a\tb\nc") == "a b c"


def test_normalize_none_is_empty():
    assert normalize_answer(None) == ""


def test_normalize_list_comma_not_stripped():
    # "a,b" 不是数字千分位，逗号保留（不误当分隔）。
    assert normalize_answer("a,b") == "a,b"


# ---------- 精确匹配 --------------------------------------------------------


def test_exact_match():
    m = _manifest(Fact(id="f1", expected="arena"))
    res = score_retention(m, {"f1": "arena"})
    assert res.retention_score == 1.0
    assert _verdicts(res)["f1"] == ("exact", 1.0)


# ---------- 等价格式 --------------------------------------------------------


def test_equivalent_format_number():
    m = _manifest(Fact(id="f1", expected=1000))
    res = score_retention(m, {"f1": "1,000"})
    assert res.retention_score == 1.0
    assert _verdicts(res)["f1"][0] == "equivalent"


def test_equivalent_format_case():
    m = _manifest(Fact(id="f1", expected="TRUE"))
    res = score_retention(m, {"f1": "true"})
    assert _verdicts(res)["f1"] == ("equivalent", 1.0)


# ---------- 缺失 ------------------------------------------------------------


def test_missing_fact_not_in_answers():
    m = _manifest(Fact(id="f1", expected="x"))
    res = score_retention(m, {})  # f1 未作答
    assert res.retention_score == 0.0
    assert _verdicts(res)["f1"] == ("missing", 0.0)


def test_missing_empty_answer():
    m = _manifest(Fact(id="f1", expected="x"))
    res = score_retention(m, {"f1": "   "})  # 空白视作缺失
    assert _verdicts(res)["f1"] == ("missing", 0.0)


# ---------- 幻觉补全 --------------------------------------------------------


def test_hallucinated_wrong_answer():
    m = _manifest(Fact(id="f1", expected="arena"))
    res = score_retention(m, {"f1": "hexagon"})
    assert res.retention_score == 0.0
    assert _verdicts(res)["f1"][0] == "hallucinated"


# ---------- 部分保留 --------------------------------------------------------


def test_partial_retention_collection():
    # expected 4 个元素，命中 2 个 → 0.5。
    m = _manifest(Fact(id="f1", expected=["a", "b", "c", "d"]))
    res = score_retention(m, {"f1": ["a", "b"]})
    assert res.retention_score == 0.5
    fs = res.per_fact[0]
    assert fs.verdict == "partial"
    assert fs.detail["hit_count"] == 2 and fs.detail["expected_count"] == 4


def test_partial_full_hit_with_extra_not_penalized():
    # 命中全部 expected + 多余项：保留分 1.0，多余项记 detail 不扣分。
    m = _manifest(Fact(id="f1", expected=["a", "b"]))
    res = score_retention(m, {"f1": ["a", "b", "z"]})
    assert res.retention_score == 1.0
    assert res.per_fact[0].detail["extra"] == ["z"]


def test_partial_zero_hit_is_hallucinated():
    m = _manifest(Fact(id="f1", expected=["a", "b"]))
    res = score_retention(m, {"f1": ["x", "y"]})
    assert res.retention_score == 0.0
    assert res.per_fact[0].verdict == "hallucinated"


def test_collection_equivalent_format_members():
    # 集合成员也走规范化：1,000 命中 1000。
    m = _manifest(Fact(id="f1", expected=[1000, 2000]))
    res = score_retention(m, {"f1": ["1,000", "2000"]})
    assert res.retention_score == 1.0


# ---------- answer_hash 路径（明文不落 manifest）---------------------------


def test_answer_hash_exact_match():
    m = _manifest(Fact(id="f1", answer_hash=answer_hash("secret-value")))
    res = score_retention(m, {"f1": "secret-value"})
    assert res.retention_score == 1.0
    assert _verdicts(res)["f1"][0] == "equivalent"


def test_answer_hash_equivalent_format():
    # hash 建立在规范化答案上 → 等价格式也匹配。
    m = _manifest(Fact(id="f1", answer_hash=answer_hash(1000)))
    res = score_retention(m, {"f1": "1,000"})
    assert res.retention_score == 1.0


def test_answer_hash_mismatch_hallucinated():
    m = _manifest(Fact(id="f1", answer_hash=answer_hash("right")))
    res = score_retention(m, {"f1": "wrong"})
    assert _verdicts(res)["f1"] == ("hallucinated", 0.0)


def test_answer_hash_missing():
    m = _manifest(Fact(id="f1", answer_hash=answer_hash("x")))
    res = score_retention(m, {})
    assert _verdicts(res)["f1"] == ("missing", 0.0)


# ---------- 均值 / 混合 / 边界 ---------------------------------------------


def test_mixed_facts_mean():
    m = _manifest(
        Fact(id="f1", expected="a"),           # 命中 → 1.0
        Fact(id="f2", expected=["p", "q"]),     # 部分 0.5
        Fact(id="f3", expected="z"),           # 缺失 0.0
    )
    res = score_retention(m, {"f1": "a", "f2": ["p"]})
    assert res.retention_score == pytest.approx((1.0 + 0.5 + 0.0) / 3)
    assert res.facts_total == 3 and res.facts_scored == 3


def test_empty_facts_score_none():
    # 无可评分 fact → None（不硬凑 0/1，交由 evaluation summary 判定）。
    res = score_retention(_manifest(), {})
    assert res.retention_score is None
    assert res.facts_total == 0


def test_only_reads_answers_not_reasoning():
    # scorer 的输入是结构化答案 map，不接触任何 reasoning 文本——多余键被忽略，
    # 只有 fact.id 对应的答案参与评分。
    m = _manifest(Fact(id="f1", expected="a"))
    res = score_retention(m, {"f1": "a", "reasoning": "我先想了很久然后猜 b", "chain": "..."})
    assert res.retention_score == 1.0  # 只看 f1，不受 reasoning 键影响


def test_fact_requires_hash_or_expected():
    with pytest.raises(ValueError):
        Fact(id="bad")


def test_normalizer_version_reported():
    res = score_retention(_manifest(Fact(id="f1", expected="a")), {"f1": "a"})
    assert res.normalizer_version == RETENTION_NORMALIZER_VERSION
