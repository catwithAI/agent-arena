"""C4-1X：Codex turn correlation 验证（真实多轮 fixture）。

Codex 当前只有 attempt/turn aggregate usage，无逐调用 evidence（`subagent_identity=
false`、`call_boundary=aggregate-only`）。因此 correlation 验证范围**限于 turn/
session 边界**，不含逐调用压缩判定（R3.3.5）。点亮「Codex turn correlation（边界
范围）」声明（C3-1 + C3-2 + C4-1 + **C4-1X**）。

用真实 `codex_multiturn/events.jsonl`（`exec resume` 多轮，同一 thread）验证：
- 多轮同一 thread → session continuity **continuous**（不误报 broken）；
- aggregate 带正确的 producer_session_id（单一会话）；
- 多 thread（resume 落到不同会话）→ session_continuity_broken，evaluation summary
  据此判 unsupported（不把跨会话消耗静默归给一个）。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.wire.evaluation import evaluate_compaction, inputs_from_wire
from backend.wire.normalizers.codex import CodexNormalizer

FIXTURE = Path(__file__).parent / "fixtures" / "codex_multiturn"


def _evidence(res):
    return [e.model_dump() for e in res.evidence]


# ---------- 多轮同 thread → 连续会话 ---------------------------------------


def test_multiturn_single_thread_continuous_session():
    res = CodexNormalizer().normalize(attempt_id="att_c41x", attempt_dir=FIXTURE)
    ev = _evidence(res)
    agg = [e for e in ev if e["evidence_type"] == "aggregate_usage"]
    assert len(agg) == 1
    # 同一 thread across setup+probe → 单一 session ID（不置 None）。
    assert agg[0]["correlation_hints"]["producer_session_id"]
    # 不产出 session_continuity_broken 事件。
    broken = [
        e for e in ev
        if e["evidence_type"] == "capture_event"
        and (e["payload"] or {}).get("reason_code") == "session_continuity_broken"
    ]
    assert broken == []


# ---------- turn/session 边界：aggregate 是 turn 累计（不伪造逐调用）--------


def test_aggregate_only_no_per_call_evidence():
    res = CodexNormalizer().normalize(attempt_id="att_c41x2", attempt_dir=FIXTURE)
    ev = _evidence(res)
    # aggregate-only：没有 native_llm_call（不伪造逐调用边界，R3.3.5）。
    assert not any(e["evidence_type"] == "native_llm_call" for e in ev)
    assert any(e["evidence_type"] == "aggregate_usage" for e in ev)


# ---------- 多 thread → 会话断裂 → evaluation unsupported -------------------


def test_multi_thread_session_broken_yields_unsupported(tmp_path):
    # 造一份 resume 落到不同 thread 的 events（会话断裂）。
    attempt_dir = tmp_path / "attempts" / "att_c41x_broken"
    attempt_dir.mkdir(parents=True)
    rows = [
        {"type": "thread.started", "thread_id": "thread-A",
         "x-lane.turn-id": "setup", "x-lane.turn-index": 0,
         "timestamp": "2026-07-20T10:00:00Z"},
        {"type": "turn.completed", "thread_id": "thread-A",
         "usage": {"input_tokens": 100, "output_tokens": 10},
         "x-lane.turn-id": "setup", "x-lane.turn-index": 0,
         "timestamp": "2026-07-20T10:00:05Z"},
        {"type": "thread.started", "thread_id": "thread-B",  # resume 落到别的 thread
         "x-lane.turn-id": "probe", "x-lane.turn-index": 1,
         "timestamp": "2026-07-20T10:01:00Z"},
        {"type": "turn.completed", "thread_id": "thread-B",
         "usage": {"input_tokens": 200, "output_tokens": 20},
         "x-lane.turn-id": "probe", "x-lane.turn-index": 1,
         "timestamp": "2026-07-20T10:01:05Z"},
    ]
    (attempt_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    res = CodexNormalizer().normalize(
        attempt_id="att_c41x_broken", attempt_dir=attempt_dir,
    )
    ev = _evidence(res)
    # 会话断裂事件产出。
    broken = [
        e for e in ev
        if e["evidence_type"] == "capture_event"
        and (e["payload"] or {}).get("reason_code") == "session_continuity_broken"
    ]
    assert broken, "多 thread 应产出 session_continuity_broken"
    # evaluation summary：session broken + 无 compaction record → unsupported。
    summary = evaluate_compaction(inputs_from_wire(
        manifest={"status": "complete", "sources": [], "gaps": []},
        records=[],
        session_continuity="broken",
    ))
    assert summary["compaction_status"] == "unsupported"


# ---------- codex aggregate-only 单独喂 evaluation → unsupported -----------


def test_codex_aggregate_only_capability_gap_unsupported():
    # codex 的 source capability call_boundary=aggregate-only → evaluation unsupported
    # （不能用 turn 累计伪造相邻调用 token 曲线，R6.6）。
    manifest = {
        "status": "complete",
        "sources": [{"kind": "codex-native",
                     "capabilities": {"call_boundary": "aggregate-only",
                                      "subagent_identity": False}}],
        "gaps": [],
    }
    summary = evaluate_compaction(inputs_from_wire(manifest=manifest, records=[]))
    assert summary["compaction_status"] == "unsupported"
    assert "aggregate-only-usage" in summary["limitations"]
