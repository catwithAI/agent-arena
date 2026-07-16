"""W6-2 验收：从 canonical wire 派生曲线（不写主 DB）。

- context / tool-result size 曲线；
- 回传形态四态有证据支撑不臆断 summarized；
- 并发度对 native-only 降级为 sequence。
"""

from __future__ import annotations

from backend.wire.curves import concurrency, context_series, tool_result_series


def _call(lc, inp, cache, msgs, bytes_, ts, role="main"):
    return {
        "record_type": "llm_call",
        "correlation": {"logical_call_id": lc},
        "time": {"timestamp": ts},
        "data": {"call_role": role,
                 "usage": {"input_tokens": inp, "cache_read_tokens": cache},
                 "request": {"message_count": msgs, "message_bytes": bytes_}},
    }


def _resp(jid, bytes_, ts, *, truncated=False, is_error=False):
    return {
        "record_type": "mcp_frame",
        "time": {"timestamp": ts},
        "data": {"message_kind": "response", "jsonrpc_id": jid, "bytes": bytes_,
                 "truncated": truncated, "is_error": is_error},
    }


# ---------- context 曲线 -----------------------------------------------------

def test_context_series_main_calls_sorted_nulls_preserved():
    recs = [
        _call("lc2", None, 40, None, 3000, "2026-07-14T00:00:05Z"),
        _call("lc1", 100, 50, 5, 2000, "2026-07-14T00:00:00Z"),
        _call("lcX", 999, 0, 9, 9, "2026-07-14T00:00:02Z", role="tool"),  # 非 main
    ]
    out = context_series(recs)
    assert [p["logical_call_id"] for p in out] == ["lc1", "lc2"]  # 排序 + 只 main
    # null 保留（不补 0）。
    assert out[1]["input_tokens"] is None and out[1]["message_count"] is None


# ---------- tool-result 回传形态 --------------------------------------------

def test_tool_result_forms_evidence_backed():
    recs = [
        _resp("1", 4000, "2026-07-14T00:00:01Z", truncated=True),
        _resp("2", 100, "2026-07-14T00:00:02Z", truncated=False),
        _resp("3", None, "2026-07-14T00:00:03Z", is_error=True),
    ]
    forms = [(p["bytes"], p["return_form"]) for p in tool_result_series(recs)]
    assert forms == [(4000, "truncated"), (100, "full"), (None, "error")]


def test_tool_result_unknown_when_truncated_missing_not_summarized():
    # truncated 不可得 → unknown，绝不臆断 summarized/reduced。
    rec = {
        "record_type": "mcp_frame", "time": {"timestamp": "t"},
        "data": {"message_kind": "response", "jsonrpc_id": "1", "bytes": 500,
                 "truncated": None, "is_error": None},
    }
    out = tool_result_series([rec])
    assert out[0]["return_form"] == "unknown"


def test_tool_result_only_responses_not_requests():
    recs = [
        {"record_type": "mcp_frame", "time": {"timestamp": "t"},
         "data": {"message_kind": "request", "jsonrpc_id": "1", "bytes": 50}},
        _resp("1", 200, "2026-07-14T00:00:02Z"),
    ]
    assert len(tool_result_series(recs)) == 1  # request 不算 tool-result


# ---------- 并发度 -----------------------------------------------------------

def test_concurrency_native_only_degrades_to_sequence():
    # native call 只有 timestamp（无 started_at）→ 降级 sequence。
    recs = [
        _call("a", 100, 0, 5, 100, "2026-07-14T00:00:00Z"),
        _call("b", 100, 0, 5, 100, "2026-07-14T00:00:01Z"),
    ]
    c = concurrency(recs)
    assert c["mode"] == "sequence"
    assert c["max_concurrent"] is None
    assert c["n"] == 2


def test_concurrency_interval_overlap():
    recs = [
        {"record_type": "llm_call", "correlation": {"logical_call_id": "a"},
         "time": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:05Z"},
         "data": {"call_role": "main"}},
        {"record_type": "llm_call", "correlation": {"logical_call_id": "b"},
         "time": {"started_at": "2026-01-01T00:00:03Z", "finished_at": "2026-01-01T00:00:08Z"},
         "data": {"call_role": "main"}},
        {"record_type": "llm_call", "correlation": {"logical_call_id": "c"},
         "time": {"started_at": "2026-01-01T00:00:10Z", "finished_at": "2026-01-01T00:00:12Z"},
         "data": {"call_role": "main"}},
    ]
    c = concurrency(recs)
    assert c["mode"] == "interval"
    assert c["max_concurrent"] == 2  # a,b 重叠；c 不重叠
    assert c["n"] == 3


def test_concurrency_mixed_missing_start_degrades():
    # 一个有 started_at、一个没有 → 整体降级 sequence（不混算）。
    recs = [
        {"record_type": "llm_call", "correlation": {"logical_call_id": "a"},
         "time": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:05Z"},
         "data": {"call_role": "main"}},
        _call("b", 100, 0, 5, 100, "2026-07-14T00:00:01Z"),  # 无 started_at
    ]
    assert concurrency(recs)["mode"] == "sequence"
