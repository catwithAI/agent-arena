"""C4-1C：Claude Code turn correlation 验证（真实多轮 fixture）。

点亮 CC 的 turn correlation 产品声明（tasks.md 发布门槛：C2-1 + C2-2 + C4-1 +
C4-1C）。用真实 CC events（`--forward-subagent-text` 样本 + 真实多轮 driver 产出的
turn-tagged events）验证 C4-1 的**显式 turn header**关联路径：

- CC normalizer 把 events.jsonl 行级 `x-lane.turn-id/-index`（adapter with_turn_ext
  写入）投影进 native_llm_call evidence 的 extensions；
- finalizer 的 `_base_record` 再投影成 canonical `correlation.turn_*`（explicit）；
- 同一 turn 的 calls 稳定归到该 turn；子 agent call 也带各自 turn。

不依赖 C1-2/C3-2/C4-1B/C4-1X。
"""

from __future__ import annotations

import json
from pathlib import Path


from backend.wire import finalize, paths, spool
from backend.wire.normalizers.claude_code import ClaudeCodeNormalizer
from backend.wire.policy import resolve_effective_policy

REAL_FIXTURE = Path(__file__).parent / "fixtures" / "cc_subagent"


def _llm_evidence(result):
    out = []
    for e in result.evidence:
        d = e.model_dump()
        if "call_role" in (d.get("payload") or {}):
            out.append(d)
    return out


# ---------- 真实 fixture：normalizer 投影 turn 进 evidence extensions --------


def test_real_fixture_projects_turn_into_evidence():
    result = ClaudeCodeNormalizer().normalize(
        attempt_id="att_c41c_real", attempt_dir=REAL_FIXTURE,
    )
    calls = _llm_evidence(result)
    assert calls, "fixture 应产出 llm_call evidence"
    # cc_subagent fixture 的所有行都带 turn-id="fork-task"（真实样本）。
    for c in calls:
        ext = c.get("extensions") or {}
        assert ext.get("x-lane.turn-id") == "fork-task"
        assert ext.get("x-lane.turn-index") == 0


# ---------- 真实 fixture → 全链路 finalize：canonical correlation.turn_* -----


def _write_events_spool_from_normalizer(tmp_path, attempt_id, result):
    """把 normalizer evidence 写进 spool，供真实 finalizer 消费（不手搓 canonical）。"""
    writer = spool.SpoolWriter(
        paths.source_spool_file(tmp_path, attempt_id, "claude-code"),
        expected_attempt_id=attempt_id,
    )
    for e in result.evidence:
        writer.append(e.model_dump())
    writer.close()


def _read_wire(tmp_path, attempt_id):
    p = paths.wire_file(tmp_path, attempt_id)
    return [json.loads(line) for line in p.read_text().splitlines()]


def test_real_fixture_finalize_sets_canonical_turn(tmp_path):
    attempt_id = "att_c41c_final"
    result = ClaudeCodeNormalizer().normalize(
        attempt_id=attempt_id, attempt_dir=REAL_FIXTURE,
    )
    _write_events_spool_from_normalizer(tmp_path, attempt_id, result)
    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=attempt_id,
        policy=resolve_effective_policy(task_requested="metadata"),
    )
    records = _read_wire(tmp_path, attempt_id)
    llm_calls = [r for r in records if r["record_type"] == "llm_call"]
    assert llm_calls
    # 每条 llm_call 的 canonical correlation 带 explicit turn。
    for r in llm_calls:
        corr = r["correlation"]
        assert corr.get("turn_id") == "fork-task"
        assert corr.get("turn_confidence") == "explicit"
    # 子 agent call 也带 turn（turn 与 agent 拓扑正交）。
    sub = [r for r in llm_calls if r["correlation"].get("agent_id") != "main"]
    assert sub, "fixture 应含子 agent call"
    assert all(r["correlation"].get("turn_id") == "fork-task" for r in sub)


# ---------- 合成多轮 CC events（真实行形状）：三轮稳定分组 -------------------
#
# cc_subagent 是单轮真实样本；这里按**真实 CC 行形状**构造三轮 events（每轮一次
# assistant call），验证多轮 turn 分组——形状取自真实样本（type/message/id/
# x-lane.turn-*），只是把 turn 维度扩到三轮。


def _assistant_row(turn_id, turn_index, msg_id, ts):
    return {
        "type": "assistant",
        "x-lane.turn-id": turn_id,
        "x-lane.turn-index": turn_index,
        "timestamp": ts,
        "message": {
            "id": msg_id,
            "model": "claude-sonnet-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 50},
            "content": [{"type": "text", "text": "ok"}],
        },
    }


def _write_multiturn_events(attempt_dir):
    rows = [
        {"type": "system", "subtype": "init", "session_id": "sess-1",
         "model": "claude-sonnet-5", "version": "2.1.215"},
        _assistant_row("setup", 0, "msg_setup_1", "2026-07-20T09:00:01Z"),
        _assistant_row("pressure", 1, "msg_pressure_1", "2026-07-20T09:00:10Z"),
        _assistant_row("probe", 2, "msg_probe_1", "2026-07-20T09:00:20Z"),
    ]
    (attempt_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_synthetic_multiturn_groups_by_turn(tmp_path):
    attempt_id = "att_c41c_multi"
    attempt_dir = paths.attempt_dir(tmp_path, attempt_id)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    _write_multiturn_events(attempt_dir)

    result = ClaudeCodeNormalizer().normalize(
        attempt_id=attempt_id, attempt_dir=attempt_dir,
    )
    _write_events_spool_from_normalizer(tmp_path, attempt_id, result)
    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=attempt_id,
        policy=resolve_effective_policy(task_requested="metadata"),
    )
    records = _read_wire(tmp_path, attempt_id)
    llm_calls = [r for r in records if r["record_type"] == "llm_call"]
    by_turn = {
        r["correlation"].get("turn_id"): r["correlation"].get("turn_index")
        for r in llm_calls
    }
    assert by_turn == {"setup": 0, "pressure": 1, "probe": 2}
    assert all(
        r["correlation"].get("turn_confidence") == "explicit" for r in llm_calls
    )


# ---------- 单轮 legacy：无 turn ext（向后兼容）-----------------------------


def test_single_turn_events_no_turn_correlation(tmp_path):
    attempt_id = "att_c41c_legacy"
    attempt_dir = paths.attempt_dir(tmp_path, attempt_id)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    # 无 x-lane.turn-* 的单轮 events。
    rows = [
        {"type": "system", "subtype": "init", "session_id": "s", "version": "2.1.215"},
        {"type": "assistant", "timestamp": "2026-07-20T09:00:01Z",
         "message": {"id": "msg_x", "model": "m", "stop_reason": "end_turn",
                     "usage": {"input_tokens": 100, "output_tokens": 10},
                     "content": [{"type": "text", "text": "ok"}]}},
    ]
    (attempt_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    result = ClaudeCodeNormalizer().normalize(
        attempt_id=attempt_id, attempt_dir=attempt_dir,
    )
    # 单轮 call 的 evidence extensions 不含 turn（产物逐字节兼容）。
    for c in _llm_evidence(result):
        assert "x-lane.turn-id" not in (c.get("extensions") or {})
