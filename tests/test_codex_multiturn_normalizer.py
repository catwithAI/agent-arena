"""C3-2: Codex 多轮 normalizer 与 aggregate-only 能力边界。

fixture `tests/fixtures/codex_multiturn/events.jsonl` 来自**真实**两轮
`codex exec` + `codex exec resume`（codex-cli 0.144.5，2026-07-20，
kimi-codex provider）：setup 轮注入口令、probe 轮回答，两轮同一 thread。

对应 spec R3.3.5 / R6.6：只有 attempt aggregate usage 时，压缩检测必须是
unsupported——不能用轮次累计值伪造相邻调用的 token 曲线。
"""

import json
from collections import Counter
from pathlib import Path

import pytest

from backend.wire.normalizers.codex import CALL_BOUNDARY, CodexNormalizer

FIXTURE = Path(__file__).parent / "fixtures" / "codex_multiturn"
THREAD_ID = "019f7f0c-c2aa-77c0-8924-ac7c7cd73d6b"


@pytest.fixture(scope="module")
def result():
    return CodexNormalizer().normalize(
        attempt_id="att_codex_mt", attempt_dir=FIXTURE,
    )


def _events() -> list[dict]:
    return [
        json.loads(line)
        for line in (FIXTURE / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _by_type(result, etype: str) -> list[dict]:
    return [
        e.model_dump() for e in result.evidence
        if e.model_dump().get("evidence_type") == etype
    ]


# ---------- 真实样本的形状（记录事实） -------------------------------------


def test_fixture_has_two_turns_same_thread():
    """两轮各有自己的 thread.started，但 thread_id 相同——resume 成立。"""
    rows = _events()
    starts = [r for r in rows if r.get("type") == "thread.started"]
    assert len(starts) == 2
    assert {r["thread_id"] for r in starts} == {THREAD_ID}
    assert {r.get("x-lane.turn-id") for r in starts} == {"setup", "probe"}


def test_per_turn_usage_is_process_local():
    """Codex 的 usage 每个进程内部累计、进程间独立（实测确认）。

    setup 7336 / probe 14696——若是跨进程累计，probe 应是 22032。
    adapter 侧据此用"轮基线 + 本轮值"累加；normalizer 侧据此对多个
    turn.completed 求和。
    """
    usages = [
        r["usage"]["input_tokens"]
        for r in _events() if r.get("type") == "turn.completed"
    ]
    assert usages == [7336, 14696]


def test_parses_without_errors(result):
    assert result.parse_errors == 0


# ---------- R6.6：aggregate-only 不伪造逐调用 ------------------------------


def test_no_native_llm_call_evidence(result):
    """Codex 不产逐调用 evidence——detector 因此收不到任何输入。

    这是**正确行为**：伪造一条 per-call 曲线会让压缩判定建立在虚构数据上。
    """
    assert _by_type(result, "native_llm_call") == []


def test_single_aggregate_evidence_sums_all_turns(result):
    """多轮只产一条 attempt 级 aggregate，数值是各轮之和。"""
    aggs = _by_type(result, "aggregate_usage")
    assert len(aggs) == 1
    payload = aggs[0]["payload"]
    assert payload["scope"] == "attempt"
    # 7336 + 14696 = 22032；32 + 69 = 101
    assert payload["usage"]["input_tokens"] == 22032
    assert payload["usage"]["output_tokens"] == 101
    # 保留 producer event type（R2.1.5），不抹掉来源
    assert payload["producer_event_type"] == "turn.completed"


def test_capability_declares_aggregate_only(result):
    """capabilities 必须明说逐调用边界不可得，供 manifest/评测判 unsupported。"""
    aggs = _by_type(result, "aggregate_usage")
    assert aggs[0]["capabilities"]["call_boundary"] == CALL_BOUNDARY
    assert CALL_BOUNDARY == "aggregate-only"


def test_session_id_carried_on_evidence(result):
    """thread_id → producer_session_id，供分段/关联使用。"""
    aggs = _by_type(result, "aggregate_usage")
    hints = aggs[0]["correlation_hints"]
    assert hints["producer_session_id"] == THREAD_ID


# ---------- detector 端到端：拿不到输入 = unsupported ----------------------


def test_detector_receives_nothing_from_codex(result):
    """把 Codex 的 evidence 投影成 canonical records → 没有 llm_call。

    压缩检测因此得不出任何结论，这正是 R3.3.5 要求的"manifest 报 capability
    gap"而非静默产出。C4-3 的 evaluation summary 会据此判 unsupported。
    """
    from _wire_projection import llm_call_records
    from backend.wire.compaction import detect_compactions

    records = llm_call_records(result)
    assert records == [], "Codex 不应产出 llm_call record"
    assert detect_compactions(records) == []


# ---------- trajectory 仍可用 ----------------------------------------------


def test_trajectory_still_produced(result):
    """aggregate-only 不影响 trajectory：工具调用/消息步骤照常可见。"""
    steps = result.trajectory.get("steps") or []
    assert steps, "trajectory 不应为空"
    kinds = Counter(s.get("kind") for s in steps)
    assert kinds["assistant"] >= 2, f"两轮各有回复，实际 {dict(kinds)}"


def test_trajectory_steps_have_no_logical_call_id(result):
    """aggregate-only：无逐调用 lc，step 不挂 logical_call_id（不伪造关联）。"""
    for step in result.trajectory.get("steps") or []:
        assert not step.get("logical_call_id")


# ---------- C3-3：无子 agent（capability 与实测一致） ----------------------


def test_no_agent_attribution_fields_in_real_events():
    """Codex 事件 schema 里没有任何 agent 归属字段（C3-3 spike 结论）。

    实测（codex-cli 0.144.5）：顶层字段只有
    {item, thread_id, timestamp, type, usage}，item 只有
    {id, message, text, type}。agent 自述"我没有派生子 agent 的工具"。
    详见 `docs/specs/context_compaction_evaluation/spike-codex-subagent.md`。

    这条测试锁定事实：若将来 Codex 引入子 agent 且事件带上归属字段，
    它会失败并提醒更新 capability matrix 与 spike 文档。
    """
    rows = _events()
    top_keys = {k for r in rows for k in r if not k.startswith("x-lane.")}
    assert top_keys == {"item", "thread_id", "timestamp", "type", "usage"}, (
        f"事件 schema 已变化：{sorted(top_keys)}"
    )

    item_keys = {
        k for r in rows if r.get("type") == "item.completed"
        for k in (r.get("item") or {})
    }
    assert item_keys <= {"id", "message", "text", "type"}, (
        f"item schema 已变化：{sorted(item_keys)}"
    )

    # 逐条扫描可能的归属线索
    blob = json.dumps(rows, ensure_ascii=False).lower()
    for hint in ("parent_", "child_", "agent_id", "sub_agent", "subagent"):
        assert hint not in blob, f"事件里出现了 {hint!r}，需重新评估子 agent 能力"


def test_no_subagent_evidence_emitted(result):
    """不产任何带 agent 扩展的 evidence——没有子 agent 就不该有归属。"""
    for e in result.evidence:
        ext = e.model_dump().get("extensions") or {}
        assert "x-lane.agent-id" not in ext


def test_capability_declares_subagent_unavailable(result):
    """R3.3.5：manifest 必须**可执行地**报告 identity gap，不只写在文档里。

    评测/manifest 据此判子 agent 压缩 unsupported；靠人读 spike 文档不算
    capability 声明。措辞限定到被测版本——"未观察到"不等于"永久不存在"。
    """
    from backend.wire.normalizers.codex import OBSERVED_CLI_VERSION

    caps = [
        e.model_dump().get("capabilities") or {} for e in result.evidence
    ]
    assert caps, "至少一条 evidence 必须带 capabilities"
    for cap in caps:
        assert cap["subagent_identity"] is False
        # 结论限定到实测版本，不宣称产品永久无此能力
        assert OBSERVED_CLI_VERSION in cap["subagent_identity_basis"]
        assert "not observed" in cap["subagent_identity_basis"]


# ---------- R2.3：跨 thread 的 aggregate 不归给单一 session ----------------


def _norm(tmp_path, rows: list[dict]):
    from backend.wire.normalizers.codex import CodexNormalizer

    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return CodexNormalizer().normalize(attempt_id="att_x", attempt_dir=tmp_path)


def _turn(thread: str, tokens: int) -> list[dict]:
    return [
        {"type": "thread.started", "thread_id": thread},
        {"type": "turn.completed",
         "usage": {"input_tokens": tokens, "output_tokens": tokens // 10}},
    ]


def test_multiple_threads_drop_single_session_id(tmp_path):
    """两个 thread 的 usage 合并后**不得**标成其中一个的 session ID。

    回归点：原实现用最后一次 thread.started 的 ID，于是
    `thread A(100) + thread B(200)` 会产出 `aggregate=300, session=B`——
    把 A 的消耗错误归给了 B，且没有保留连续性断裂的事实。
    """
    r = _norm(tmp_path, [*_turn("thread_A", 100), *_turn("thread_B", 200)])
    aggs = [
        e.model_dump() for e in r.evidence
        if e.model_dump().get("evidence_type") == "aggregate_usage"
    ]
    assert len(aggs) == 1
    assert aggs[0]["payload"]["usage"]["input_tokens"] == 300
    assert aggs[0]["correlation_hints"]["producer_session_id"] is None, (
        "跨 session 的 aggregate 不得标单一 session ID"
    )


def test_multiple_threads_emit_continuity_gap(tmp_path):
    """出现多个 thread ID → 明确的 capability gap，供评测判 incomplete。"""
    r = _norm(tmp_path, [*_turn("thread_A", 100), *_turn("thread_B", 200)])
    gaps = [
        e.model_dump()["payload"] for e in r.evidence
        if e.model_dump().get("evidence_type") == "capture_event"
    ]
    reasons = [g["reason_code"] for g in gaps]
    assert "session_continuity_broken" in reasons

    gap = next(g for g in gaps if g["reason_code"] == "session_continuity_broken")
    assert gap["counters"]["session_count"] == 2
    assert "thread_A" in gap["message"] and "thread_B" in gap["message"]


def test_single_thread_keeps_session_id_and_no_gap(tmp_path):
    """正常情况（同一 thread 多轮）：保留 session ID，不产断裂 gap。"""
    r = _norm(tmp_path, [*_turn("thread_A", 100), *_turn("thread_A", 200)])
    aggs = [
        e.model_dump() for e in r.evidence
        if e.model_dump().get("evidence_type") == "aggregate_usage"
    ]
    assert aggs[0]["correlation_hints"]["producer_session_id"] == "thread_A"
    assert aggs[0]["payload"]["usage"]["input_tokens"] == 300

    reasons = [
        e.model_dump()["payload"]["reason_code"] for e in r.evidence
        if e.model_dump().get("evidence_type") == "capture_event"
    ]
    assert "session_continuity_broken" not in reasons


def test_session_id_is_first_thread_not_last(tmp_path):
    """单 thread 场景下 session_id 取首个——语义上它才是 attempt 的身份。"""
    r = _norm(tmp_path, _turn("thread_first", 50))
    aggs = [
        e.model_dump() for e in r.evidence
        if e.model_dump().get("evidence_type") == "aggregate_usage"
    ]
    assert aggs[0]["correlation_hints"]["producer_session_id"] == "thread_first"
