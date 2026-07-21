"""C4-1：turn correlation 通用逻辑（design §6.1）。

adapter-agnostic 合成 fixture：多轮 canonical records + turn header/时间戳，验证
correlation 算法本身（不依赖任何 adapter 的多轮实现）。覆盖：

- explicit turn header 投影（confidence=explicit）；
- 时间窗口 inferred 关联（唯一命中）；
- 并发/边界歧义不强行关联（重叠窗口 / 窗口外 → 不标 turn）；
- 同一 attempt 三轮 calls 稳定分组；
- turn 沿 logical call 合并（http hop 无 header 借 native call 的 turn 定位）；
- explicit 冲突不静默择一（记 conflict）；
- conversation.jsonl 时间窗口读取（含截断 fail-open）。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.wire import turn_correlation as tc


# ---------- fixture helpers -------------------------------------------------


def _llm_call(lc: str, *, ts: str, ext: dict | None = None) -> dict:
    """一条 canonical llm_call。ext 模拟 finalize._base_record 已投影的 explicit turn。"""
    corr: dict = {"logical_call_id": lc, "agent_id": "main", "producer_session_id": "s1"}
    if ext:
        exp = tc.explicit_turn(ext)
        if exp is not None:
            corr["turn_id"] = exp[0]
            corr["turn_index"] = exp[1]
            corr["turn_confidence"] = "explicit"
    return {
        "record_type": "llm_call",
        "correlation": corr,
        "time": {"started_at": ts, "timestamp": ts},
        "data": {"call_role": "main"},
    }


def _http_hop(lc: str, *, ts: str) -> dict:
    """一条 http_exchange hop，无显式 turn（借 logical call 合并定位）。"""
    return {
        "record_type": "http_exchange",
        "correlation": {"logical_call_id": lc},
        "time": {"started_at": ts, "timestamp": ts},
        "data": {},
    }


def _window(turn_id, idx, start, end) -> tc.TurnWindow:
    return tc.TurnWindow(
        turn_id=turn_id,
        turn_index=idx,
        start_ms=tc._epoch_ms(start),
        end_ms=tc._epoch_ms(end) if end else None,
    )


# ---------- explicit turn header 投影 ---------------------------------------


def test_explicit_turn_from_extension():
    exp = tc.explicit_turn({tc.EXT_TURN_ID: "probe", tc.EXT_TURN_INDEX: 2})
    assert exp == ("probe", 2)


def test_explicit_turn_id_without_index():
    exp = tc.explicit_turn({tc.EXT_TURN_ID: "setup"})
    assert exp == ("setup", None)


def test_explicit_turn_missing_id_returns_none():
    assert tc.explicit_turn({tc.EXT_TURN_INDEX: 3}) is None
    assert tc.explicit_turn({}) is None
    assert tc.explicit_turn(None) is None


def test_explicit_projected_and_not_overwritten_by_inferred():
    # explicit call 落在一个会诱导 inferred 的窗口里——explicit 必须胜出。
    rec = _llm_call("lc0", ts="2026-07-20T09:00:10Z", ext={tc.EXT_TURN_ID: "probe"})
    windows = [_window("setup", 0, "2026-07-20T09:00:00Z", "2026-07-20T09:00:20Z")]
    tc.project_turn_correlation([rec], windows)
    corr = rec["correlation"]
    assert corr["turn_id"] == "probe"
    assert corr["turn_confidence"] == "explicit"


# ---------- 时间窗口 inferred 关联 ------------------------------------------


def test_inferred_unique_hit():
    rec = _llm_call("lc0", ts="2026-07-20T09:00:05Z")
    windows = [
        _window("setup", 0, "2026-07-20T09:00:00Z", "2026-07-20T09:00:10Z"),
        _window("probe", 1, "2026-07-20T09:00:20Z", "2026-07-20T09:00:30Z"),
    ]
    tc.project_turn_correlation([rec], windows)
    corr = rec["correlation"]
    assert corr["turn_id"] == "setup"
    assert corr["turn_index"] == 0
    assert corr["turn_confidence"] == "inferred"


def test_inferred_boundary_inclusive():
    # 恰在 start / end 边界上（闭区间）算命中。
    windows = [_window("t0", 0, "2026-07-20T09:00:00Z", "2026-07-20T09:00:10Z")]
    at_start = _llm_call("a", ts="2026-07-20T09:00:00Z")
    at_end = _llm_call("b", ts="2026-07-20T09:00:10Z")
    tc.project_turn_correlation([at_start, at_end], windows)
    assert at_start["correlation"]["turn_id"] == "t0"
    assert at_end["correlation"]["turn_id"] == "t0"


def test_no_turn_outside_all_windows():
    rec = _llm_call("lc0", ts="2026-07-20T09:00:15Z")  # 落在两窗口之间的间隙
    windows = [
        _window("setup", 0, "2026-07-20T09:00:00Z", "2026-07-20T09:00:10Z"),
        _window("probe", 1, "2026-07-20T09:00:20Z", "2026-07-20T09:00:30Z"),
    ]
    tc.project_turn_correlation([rec], windows)
    assert "turn_id" not in rec["correlation"]
    assert "turn_confidence" not in rec["correlation"]


# ---------- 并发/边界歧义不强行关联 ----------------------------------------


def test_overlapping_windows_ambiguous_no_turn():
    # 两窗口重叠区域内的时间戳落进 2 个窗口 → 歧义，不猜。
    rec = _llm_call("lc0", ts="2026-07-20T09:00:05Z")
    windows = [
        _window("a", 0, "2026-07-20T09:00:00Z", "2026-07-20T09:00:10Z"),
        _window("b", 1, "2026-07-20T09:00:03Z", "2026-07-20T09:00:08Z"),
    ]
    tc.project_turn_correlation([rec], windows)
    assert "turn_id" not in rec["correlation"]


def test_unclosed_window_does_not_swallow():
    # 未见终态的窗口（end=None）无右边界，不吞后续时间戳。
    rec = _llm_call("lc0", ts="2026-07-20T09:05:00Z")
    windows = [_window("open", 0, "2026-07-20T09:00:00Z", None)]
    tc.project_turn_correlation([rec], windows)
    assert "turn_id" not in rec["correlation"]


def test_infer_turn_none_timestamp():
    windows = [_window("t0", 0, "2026-07-20T09:00:00Z", "2026-07-20T09:00:10Z")]
    assert tc.infer_turn(None, windows) is None


# ---------- 三轮稳定分组 -----------------------------------------------------


def test_three_turns_stable_grouping_inferred():
    # 同一 attempt 三轮 calls，每轮一次调用，按时间窗口稳定归到各自 turn。
    windows = [
        _window("setup", 0, "2026-07-20T09:00:00Z", "2026-07-20T09:00:10Z"),
        _window("pressure", 1, "2026-07-20T09:00:20Z", "2026-07-20T09:00:30Z"),
        _window("probe", 2, "2026-07-20T09:00:40Z", "2026-07-20T09:00:50Z"),
    ]
    recs = [
        _llm_call("lc0", ts="2026-07-20T09:00:05Z"),
        _llm_call("lc1", ts="2026-07-20T09:00:25Z"),
        _llm_call("lc2", ts="2026-07-20T09:00:45Z"),
    ]
    tc.project_turn_correlation(recs, windows)
    assert [r["correlation"]["turn_id"] for r in recs] == ["setup", "pressure", "probe"]
    assert [r["correlation"]["turn_index"] for r in recs] == [0, 1, 2]

    # 幂等：再跑一次结果不变（确定性）。
    tc.project_turn_correlation(recs, windows)
    assert [r["correlation"]["turn_id"] for r in recs] == ["setup", "pressure", "probe"]


def test_three_turns_stable_grouping_explicit_headers():
    recs = [
        _llm_call("lc0", ts="2026-07-20T09:00:05Z", ext={tc.EXT_TURN_ID: "setup", tc.EXT_TURN_INDEX: 0}),
        _llm_call("lc1", ts="2026-07-20T09:00:25Z", ext={tc.EXT_TURN_ID: "pressure", tc.EXT_TURN_INDEX: 1}),
        _llm_call("lc2", ts="2026-07-20T09:00:45Z", ext={tc.EXT_TURN_ID: "probe", tc.EXT_TURN_INDEX: 2}),
    ]
    tc.project_turn_correlation(recs, [])  # 无窗口也能按 explicit 分组
    assert [r["correlation"]["turn_id"] for r in recs] == ["setup", "pressure", "probe"]
    assert all(r["correlation"]["turn_confidence"] == "explicit" for r in recs)


# ---------- turn 沿 logical call 合并 --------------------------------------


def test_http_hop_borrows_turn_from_native_call_explicit():
    # native call 带 explicit turn header；同 lc 的 http hop 没带 → 借 native 的 turn。
    native = _llm_call("lc0", ts="2026-07-20T09:00:05Z", ext={tc.EXT_TURN_ID: "probe", tc.EXT_TURN_INDEX: 2})
    hop = _http_hop("lc0", ts="2026-07-20T09:00:06Z")
    tc.project_turn_correlation([native, hop], [])
    assert hop["correlation"]["turn_id"] == "probe"
    assert hop["correlation"]["turn_index"] == 2
    assert hop["correlation"]["turn_confidence"] == "explicit"


def test_inferred_upgraded_to_explicit_within_lc():
    # hop 先被时间窗口标成 inferred，native call 有 explicit → 合并后升级为 explicit。
    windows = [_window("setup", 0, "2026-07-20T09:00:00Z", "2026-07-20T09:00:10Z")]
    native = _llm_call("lc0", ts="2026-07-20T09:05:00Z", ext={tc.EXT_TURN_ID: "probe"})
    hop = _http_hop("lc0", ts="2026-07-20T09:00:05Z")  # 落进 setup 窗口
    tc.project_turn_correlation([native, hop], windows)
    # explicit 优先：整组用 native 的 probe，而非 hop 时间落进的 setup。
    assert hop["correlation"]["turn_id"] == "probe"
    assert hop["correlation"]["turn_confidence"] == "explicit"
    assert native["correlation"]["turn_id"] == "probe"


def test_different_lc_not_merged():
    # 不同 logical call 不互相借 turn。
    a = _llm_call("lcA", ts="2026-07-20T09:00:05Z", ext={tc.EXT_TURN_ID: "t0"})
    b = _http_hop("lcB", ts="2026-07-20T09:00:06Z")  # 不同 lc，无窗口
    tc.project_turn_correlation([a, b], [])
    assert a["correlation"]["turn_id"] == "t0"
    assert "turn_id" not in b["correlation"]


def test_conflicting_explicit_turns_not_silently_merged():
    # 同一 lc 被打上两个互相冲突的 explicit turn → 记 conflict，不静默择一。
    a = _llm_call("lc0", ts="2026-07-20T09:00:05Z", ext={tc.EXT_TURN_ID: "probe"})
    b = _llm_call("lc0", ts="2026-07-20T09:00:06Z", ext={tc.EXT_TURN_ID: "pressure"})
    tc.project_turn_correlation([a, b], [])
    for rec in (a, b):
        conflicts = rec.get("conflicts") or []
        assert any(c["field"] == "turn_id" for c in conflicts)
        assert sorted(conflicts[0]["candidates"]) == ["pressure", "probe"]
    # 各自保留原 explicit（不伪造统一）。
    assert a["correlation"]["turn_id"] == "probe"
    assert b["correlation"]["turn_id"] == "pressure"


# ---------- conversation.jsonl 时间窗口读取 --------------------------------


def _write_conversation(tmp_path: Path, lines: list[dict]) -> Path:
    path = tmp_path / tc._CONVERSATION_FILENAME
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return tmp_path


def test_load_turn_windows_from_conversation(tmp_path):
    attempt_dir = _write_conversation(tmp_path, [
        {"event": "conversation.started", "attempt_id": "att"},
        {"event": "turn.started", "turn_id": "setup", "turn_index": 0, "timestamp": "2026-07-20T09:00:00Z"},
        {"event": "turn.completed", "turn_id": "setup", "turn_index": 0, "timestamp": "2026-07-20T09:00:10Z"},
        {"event": "turn.started", "turn_id": "probe", "turn_index": 1, "timestamp": "2026-07-20T09:00:20Z"},
        {"event": "turn.failed", "turn_id": "probe", "turn_index": 1, "timestamp": "2026-07-20T09:00:30Z"},
    ])
    windows = tc.load_turn_windows(attempt_dir)
    assert [w.turn_id for w in windows] == ["setup", "probe"]
    assert windows[0].end_ms is not None
    assert windows[1].end_ms is not None  # turn.failed 也给右边界


def test_load_turn_windows_missing_file(tmp_path):
    assert tc.load_turn_windows(tmp_path) == []


def test_load_turn_windows_truncated_tail_fail_open(tmp_path):
    # 尾行截断（非法 JSON）：fail-open 跳过，已完整的 turn 仍可读。
    path = tmp_path / tc._CONVERSATION_FILENAME
    path.write_text(
        json.dumps({"event": "turn.started", "turn_id": "setup", "turn_index": 0, "timestamp": "2026-07-20T09:00:00Z"}) + "\n"
        + json.dumps({"event": "turn.completed", "turn_id": "setup", "turn_index": 0, "timestamp": "2026-07-20T09:00:10Z"}) + "\n"
        + '{"event": "turn.started", "turn_id": "pr',  # 截断
        encoding="utf-8",
    )
    windows = tc.load_turn_windows(tmp_path)
    assert [w.turn_id for w in windows] == ["setup"]


def test_started_only_window_has_no_end(tmp_path):
    attempt_dir = _write_conversation(tmp_path, [
        {"event": "turn.started", "turn_id": "active", "turn_index": 0, "timestamp": "2026-07-20T09:00:00Z"},
    ])
    windows = tc.load_turn_windows(attempt_dir)
    assert len(windows) == 1
    assert windows[0].end_ms is None
