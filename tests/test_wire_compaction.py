"""W6-1 验收：被动 compaction 检测（design §10.4）。

四档 confidence（explicit 走 hint 另路）+ new-session 不误判；strategy 靠 size 启发
（逐消息 hash 缺失时不臆断）；从 canonical main calls 分型，产 context_compaction。
"""

from __future__ import annotations

import json

from backend.wire.compaction import (
    ANALYZER_VERSION,
    AnalyzerConfig,
    detect_compactions,
)


def _call(lc, inp, msgs, ts, *, mhash="h", session="s1", role="main", agent="main"):
    return {
        "record_type": "llm_call", "attempt_id": "att", "phase": "agent_run",
        "correlation": {"logical_call_id": lc, "agent_id": agent,
                        "producer_session_id": session},
        "time": {"timestamp": ts},
        "data": {"call_role": role,
                 "usage": {"input_tokens": inp},
                 "request": {"message_count": msgs, "messages_hash": mhash}},
    }


def _cids(recs):
    return [(r["data"]["confidence"], r["data"]["strategy"]) for r in recs]


# ---------- confidence 四档 --------------------------------------------------

def test_high_confidence_token_and_message_drop_tight():
    # 90k→28k + 消息大降 + 时间紧接 → high。
    recs = [
        _call("lc1", 90000, 31, "2026-07-14T00:00:00Z", mhash="a"),
        _call("lc2", 28000, 2, "2026-07-14T00:00:03Z", mhash="b"),
    ]
    out = detect_compactions(recs)
    assert len(out) == 1
    d = out[0]["data"]
    assert d["confidence"] == "high"
    assert d["before_tokens"] == 90000 and d["after_tokens"] == 28000
    assert d["dropped_messages"] == 29
    assert d["before_call_id"] == "lc1" and d["after_call_id"] == "lc2"
    assert d["analyzer_version"] == ANALYZER_VERSION


def test_medium_confidence_token_drop_hash_diff_no_msg_count():
    # token 大降 + message hash 变（但无 message count 下降信号）→ medium。
    recs = [
        _call("lc1", 90000, None, "2026-07-14T00:00:00Z", mhash="a"),
        _call("lc2", 20000, None, "2026-07-14T00:00:03Z", mhash="b"),
    ]
    assert _cids(detect_compactions(recs)) == [("medium", "unknown")]


def test_low_confidence_only_token_drop():
    # 只有 token 突降（无 message 信号、hash 相同）→ low。
    recs = [
        _call("lc1", 90000, None, "2026-07-14T00:00:00Z", mhash="same"),
        _call("lc2", 20000, None, "2026-07-14T00:00:03Z", mhash="same"),
    ]
    assert _cids(detect_compactions(recs)) == [("low", "unknown")]


# ---------- 不误判 -----------------------------------------------------------

def test_new_session_not_compaction():
    # session ID 改变 → 不记 compaction（design §10.4 规则 5）。
    recs = [
        _call("lc1", 90000, 31, "2026-07-14T00:00:00Z", session="s1"),
        _call("lc2", 28000, 2, "2026-07-14T00:00:03Z", session="s2"),
    ]
    assert detect_compactions(recs) == []


def test_token_increase_not_compaction():
    recs = [
        _call("lc1", 30000, 10, "2026-07-14T00:00:00Z"),
        _call("lc2", 31000, 11, "2026-07-14T00:00:03Z"),
    ]
    assert detect_compactions(recs) == []


def test_small_token_drop_below_threshold_not_compaction():
    # 下降但未过 ratio(0.6)/abs(5000) 门槛 → 不判。
    recs = [
        _call("lc1", 10000, 10, "2026-07-14T00:00:00Z"),
        _call("lc2", 8000, 9, "2026-07-14T00:00:03Z"),  # ratio 0.8，abs 2000
    ]
    assert detect_compactions(recs) == []


def test_missing_input_tokens_no_false_detect():
    # input token 缺失 → 无法判定，不猜。
    recs = [
        _call("lc1", None, 31, "2026-07-14T00:00:00Z"),
        _call("lc2", 28000, 2, "2026-07-14T00:00:03Z"),
    ]
    assert detect_compactions(recs) == []


def test_non_main_calls_ignored():
    # 只看 call_role=main；sub/tool call 不参与。
    recs = [
        _call("lc1", 90000, 31, "2026-07-14T00:00:00Z", role="tool"),
        _call("lc2", 28000, 2, "2026-07-14T00:00:03Z", role="tool"),
    ]
    assert detect_compactions(recs) == []


def test_full_summary_strategy():
    # 大量删除后只剩极少消息 → full-summary。
    recs = [
        _call("lc1", 90000, 40, "2026-07-14T00:00:00Z", mhash="a"),
        _call("lc2", 20000, 1, "2026-07-14T00:00:03Z", mhash="b"),
    ]
    assert detect_compactions(recs)[0]["data"]["strategy"] == "full-summary"


# ---------- versioned config -------------------------------------------------

def test_analyzer_config_thresholds_tunable():
    # 自定义更宽松阈值：原本不判的小下降现在被判。
    recs = [
        _call("lc1", 10000, 10, "2026-07-14T00:00:00Z"),
        _call("lc2", 8000, 5, "2026-07-14T00:00:03Z"),
    ]
    loose = AnalyzerConfig(ratio_drop=0.9, abs_drop=1000)
    assert len(detect_compactions(recs, config=loose)) == 1
    assert detect_compactions(recs) == []  # 默认阈值不判


# ---------- 端到端：finalize 产 canonical context_compaction record ----------

def test_finalize_emits_context_compaction_record(tmp_path):
    from backend.wire import evidence, paths, spool
    from backend.wire.policy import resolve_effective_policy
    from backend.wire import finalize

    ATT = "att_comp"

    def _native(i, inp, msgs, ts, mhash, call_id):
        payload = {
            **evidence.null_payload("native_llm_call"),
            "producer_call_id": call_id, "model": "glm", "call_role": "main",
            "usage": {"input_tokens": inp, "output_tokens": 10,
                      "cache_read_tokens": None, "cache_write_tokens": None,
                      "reasoning_tokens": None, "estimated": False},
            "request_summary": {
                "model": "glm", "message_count": msgs, "message_bytes": inp,
                "system_hash": None, "messages_hash": mhash, "tools_hash": None,
                "hash_domain": "lane-semantic-jcs-nfc-v1"},
        }
        return {
            "evidence_id": f"we_{i}", "attempt_id": ATT, "phase": "agent_run",
            "evidence_type": "native_llm_call",
            "source": {"kind": "native-event", "instance": "native-event"},
            "producer": {"name": "test"},
            "time": {"observed_at": ts},
            "raw_ref": {"kind": "raw-line", "file": "events.jsonl", "line": i},
            "correlation_hints": {"producer_call_id": call_id,
                                  "producer_session_id": "sess1"},
            "capabilities": {}, "redaction": {"policy": "metadata", "status": "applied"},
            "errors": [], "extensions": {}, "payload": payload,
        }

    w = spool.SpoolWriter(
        paths.source_spool_file(tmp_path, ATT, "native-event"), expected_attempt_id=ATT)
    w.append(_native(1, 90000, 31, "2026-07-14T00:00:00Z", "hashA", "msg_1"))
    w.append(_native(2, 28000, 2, "2026-07-14T00:00:03Z", "hashB", "msg_2"))
    w.close()

    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT,
        policy=resolve_effective_policy(task_requested="metadata"),
        started_at="2026-07-14T00:00:00Z", finished_at="2026-07-14T00:00:04Z")

    recs = [json.loads(ln) for ln in paths.wire_file(tmp_path, ATT).read_text().splitlines()]
    comps = [r for r in recs if r["record_type"] == "context_compaction"]
    assert len(comps) == 1
    d = comps[0]["data"]
    assert d["confidence"] == "high"
    assert d["before_tokens"] == 90000 and d["after_tokens"] == 28000
    # before/after 引用 canonical lc（logical_call_id）。
    lcs = {r["correlation"]["logical_call_id"] for r in recs
           if r["record_type"] == "llm_call"}
    assert d["before_call_id"] in lcs and d["after_call_id"] in lcs
