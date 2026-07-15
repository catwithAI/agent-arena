"""W1-3 验收：Codex normalizer（design §10.2、§27.1）。

覆盖 tasks.md W1-3 验收清单：
- fixtures 覆盖多调用/仅 aggregate/schema 漂移；
- aggregate-only 时不伪造曲线（0 个 llm_call，1 条 aggregate_usage）；
- call_boundary=aggregate-only 落 manifest；producer event type 保留（R2.1.5）；
- 可见 payload（tool/command）映射 trajectory step。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from backend.wire import finalize, paths
from backend.wire.normalizers.codex import CodexNormalizer
from backend.wire.normalizers.runner import run_native_normalizer
from backend.wire.policy import resolve_effective_policy

FIXTURE = Path(__file__).parent / "fixtures" / "wire" / "codex" / "events.jsonl"
ATT = "att_cx1"
POLICY = resolve_effective_policy(task_requested="metadata")


def _attempt(tmp_path) -> Path:
    d = paths.attempt_dir(tmp_path, ATT)
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, d / "events.jsonl")
    return d


def _finalize(tmp_path):
    run_native_normalizer(agent_name="codex", attempt_id=ATT, data_path=tmp_path)
    return finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT, policy=POLICY,
        declared_sources=[{"kind": "native-event", "instance": "native-event"}],
    )


def _read_wire(tmp_path):
    return [
        json.loads(line)
        for line in paths.wire_file(tmp_path, ATT).read_text().splitlines()
    ]


# ---------- normalizer 直接产出 --------------------------------------------

def test_aggregate_only_no_fabricated_calls(tmp_path):
    _attempt(tmp_path)
    r = CodexNormalizer().normalize(attempt_id=ATT, attempt_dir=paths.attempt_dir(tmp_path, ATT))
    calls = [e for e in r.evidence if e.evidence_type == "native_llm_call"]
    aggs = [e for e in r.evidence if e.evidence_type == "aggregate_usage"]
    assert calls == []  # aggregate-only：不伪造逐调用
    assert len(aggs) == 1
    assert r.parse_errors == 1  # garbage 行
    agg = aggs[0]
    assert agg.payload.scope == "attempt"
    assert agg.payload.producer_event_type == "turn.completed"  # R2.1.5 保留
    assert agg.payload.usage.input_tokens == 5000
    assert agg.payload.usage.cache_read_tokens == 4000  # cached_input → cache_read
    assert agg.payload.usage.cache_write_tokens is None  # codex 无 cache_write（null）
    assert agg.payload.usage.reasoning_tokens == 30
    # capability 声明 aggregate-only
    assert agg.capabilities.get("call_boundary") == "aggregate-only"
    assert agg.correlation_hints.producer_session_id == "th_abc"


def test_trajectory_steps_from_items(tmp_path):
    _attempt(tmp_path)
    r = CodexNormalizer().normalize(attempt_id=ATT, attempt_dir=paths.attempt_dir(tmp_path, ATT))
    kinds = [s["kind"] for s in r.trajectory["steps"]]
    # 2 agent_message → assistant；mcp_tool_call + command_execution → tool_call；
    # todo_list 不建 step
    assert kinds.count("assistant") == 2
    assert kinds.count("tool_call") == 2
    tc = [s for s in r.trajectory["steps"] if s["kind"] == "tool_call"]
    assert tc[0]["tool_call_id"] == "it_1"
    # aggregate-only：step 不挂 logical_call_id
    assert all(s["logical_call_id"] is None for s in r.trajectory["steps"])
    # 评审 #2：mcp_tool_call step 填 tool_name（裸工具名，供 W3-4 关联）；
    # command_execution 用固定名。否则 W3-4 对 Codex 永不关联。
    tool_names = {s.get("tool_name") for s in tc}
    assert "build_formation" in tool_names  # 裸 MCP 工具名（item.tool）
    assert "command_execution" in tool_names


def test_normalizer_idempotent(tmp_path):
    _attempt(tmp_path)
    n = CodexNormalizer()
    r1 = n.normalize(attempt_id=ATT, attempt_dir=paths.attempt_dir(tmp_path, ATT))
    r2 = n.normalize(attempt_id=ATT, attempt_dir=paths.attempt_dir(tmp_path, ATT))
    assert [e.evidence_id for e in r1.evidence] == [e.evidence_id for e in r2.evidence]
    assert r1.trajectory == r2.trajectory


def test_schema_drift_bad_item_counts_parse_error(tmp_path):
    """item.completed.item 变 string 等畸形只计 parse error，不整次 fail-open。"""
    d = paths.attempt_dir(tmp_path, ATT)
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text("\n".join([
        json.dumps({"timestamp": "2026-07-13T00:00:00.000Z", "type": "thread.started",
                    "thread_id": "th_x"}),
        json.dumps({"timestamp": "2026-07-13T00:00:01.000Z", "type": "item.completed",
                    "item": "should be object"}),
        json.dumps({"timestamp": "2026-07-13T00:00:02.000Z", "type": "item.completed",
                    "item": {"id": "ok", "type": "agent_message", "text": "hi"}}),
        json.dumps({"timestamp": "2026-07-13T00:00:03.000Z", "type": "turn.completed",
                    "usage": {"input_tokens": 100, "output_tokens": 5}}),
    ]) + "\n")
    r = CodexNormalizer().normalize(attempt_id=ATT, attempt_dir=d)
    assert r.parse_errors == 1
    assert 2 in r.error_lines
    # 坏 item 后的正常事件仍处理
    assert len([s for s in r.trajectory["steps"] if s["kind"] == "assistant"]) == 1
    assert [e for e in r.evidence if e.evidence_type == "aggregate_usage"]


def test_interrupted_keeps_trajectory_and_usage_gap(tmp_path):
    """评审 B1：仅有 item、无 turn.completed（中断）——保留 trajectory + 写明确
    usage gap capture_event，不伪造 aggregate。"""
    d = paths.attempt_dir(tmp_path, ATT)
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text("\n".join([
        json.dumps({"timestamp": "2026-07-13T00:00:00.000Z", "type": "item.completed",
                    "item": {"id": "x", "type": "agent_message", "text": "partial"}}),
    ]) + "\n")
    r = CodexNormalizer().normalize(attempt_id=ATT, attempt_dir=d)
    assert not [e for e in r.evidence if e.evidence_type == "aggregate_usage"]  # 不伪造
    gap = [e for e in r.evidence if e.evidence_type == "capture_event"]
    assert gap and gap[0].payload.reason_code == "usage_not_observed"
    assert gap[0].capabilities.get("usage") == "not-observed"
    assert len(r.trajectory["steps"]) == 1


def test_interrupted_still_produces_via_runner(tmp_path):
    """评审 B1 生产链路：runner 对「有 trajectory 无 usage」也写 spool+trajectory。"""
    d = paths.attempt_dir(tmp_path, ATT)
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text(
        json.dumps({"timestamp": "2026-07-13T00:00:00.000Z", "type": "item.completed",
                    "item": {"id": "x", "type": "agent_message", "text": "partial"}}) + "\n"
    )
    produced = run_native_normalizer(agent_name="codex", attempt_id=ATT, data_path=tmp_path)
    assert produced is True
    assert (d / "trajectory.json").exists()
    assert paths.source_spool_file(tmp_path, ATT, "native-event").exists()


def test_raw_line_and_schema_drift_count_parse_error(tmp_path):
    """评审 M2：adapter 包装的 {"raw_line"} + turn.completed.usage 畸形都计 parse error。"""
    d = paths.attempt_dir(tmp_path, ATT)
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text("\n".join([
        json.dumps({"timestamp": "2026-07-13T00:00:00.000Z", "type": "thread.started",
                    "thread_id": "th"}),
        json.dumps({"timestamp": "2026-07-13T00:00:01.000Z", "raw_line": "garbage cli"}),
        json.dumps({"timestamp": "2026-07-13T00:00:02.000Z", "type": "item.completed",
                    "item": {"id": "i", "type": "agent_message", "text": "ok"}}),
        json.dumps({"timestamp": "2026-07-13T00:00:03.000Z", "type": "turn.completed",
                    "usage": "not-an-object"}),  # schema drift
    ]) + "\n")
    r = CodexNormalizer().normalize(attempt_id=ATT, attempt_dir=d)
    assert r.parse_errors == 2  # raw_line + 畸形 usage
    # 畸形 usage 不静默变「无 usage」，也没伪造 aggregate
    assert not [e for e in r.evidence if e.evidence_type == "aggregate_usage"]


def test_semantic_ir_hash_on_trajectory_steps(tmp_path):
    """评审 M3：可见 payload 映射公共 semantic IR，step 带 content_hash + size。"""
    _attempt(tmp_path)
    r = CodexNormalizer().normalize(attempt_id=ATT, attempt_dir=paths.attempt_dir(tmp_path, ATT))
    steps = r.trajectory["steps"]
    asst = next(s for s in steps if s["kind"] == "assistant")
    tool = next(s for s in steps if s["kind"] == "tool_call")
    assert asst["content_hash"] and len(asst["content_hash"]) == 64  # SHA-256 hex
    assert asst["content_bytes"] > 0
    assert tool["content_hash"] and tool["content_bytes"] is not None
    # 等价文本 → 同 hash（per-part messages IR，与 claude 同形状）
    from backend.wire.normalizers.claude_code import _part_semantic_hash
    same, _ = _part_semantic_hash([{"type": "text", "text": "计划：先建立编队"}])
    assert asst["content_hash"] == same


def test_semantic_hash_cross_source_parity(tmp_path):
    """评审 M3 核心：Claude 与 Codex 对相同文本/工具调用得到相同 semantic hash。"""
    import json as _json
    from backend.wire.normalizers.claude_code import ClaudeCodeNormalizer

    # Claude 侧：assistant text + tool_use
    cd = paths.attempt_dir(tmp_path, "att_cc")
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "events.jsonl").write_text(_json.dumps({
        "timestamp": "2026-07-13T00:00:01.000Z", "type": "assistant",
        "message": {"id": "m", "role": "assistant", "stop_reason": "tool_use",
                    "content": [
                        {"type": "text", "text": "统一文本"},
                        {"type": "tool_use", "id": "t", "name": "srv.build", "input": {"n": 1}},
                    ], "usage": {"input_tokens": 1}}}) + "\n")
    cr = ClaudeCodeNormalizer().normalize(attempt_id="att_cc", attempt_dir=cd)
    c_asst = next(s for s in cr.trajectory["steps"] if s["kind"] == "assistant")
    c_tool = next(s for s in cr.trajectory["steps"] if s["kind"] == "tool_call")

    # Codex 侧：同文本 agent_message + 同 name/args 的 mcp_tool_call
    xd = paths.attempt_dir(tmp_path, "att_cx")
    xd.mkdir(parents=True, exist_ok=True)
    (xd / "events.jsonl").write_text("\n".join([
        _json.dumps({"timestamp": "2026-07-13T00:00:01.000Z", "type": "item.completed",
                     "item": {"id": "a", "type": "agent_message", "text": "统一文本"}}),
        _json.dumps({"timestamp": "2026-07-13T00:00:02.000Z", "type": "item.completed",
                     "item": {"id": "b", "type": "mcp_tool_call", "server": "srv",
                              "tool": "build", "arguments": {"n": 1}, "status": "completed"}}),
    ]) + "\n")
    xr = CodexNormalizer().normalize(attempt_id="att_cx", attempt_dir=xd)
    x_asst = next(s for s in xr.trajectory["steps"] if s["kind"] == "assistant")
    x_tool = next(s for s in xr.trajectory["steps"] if s["kind"] == "tool_call")

    assert c_asst["content_hash"] == x_asst["content_hash"]  # 同文本同 hash
    assert c_tool["content_hash"] == x_tool["content_hash"]  # 同工具调用同 hash


def test_semantic_hash_uses_messages_ir_shape():
    """评审 R4：hash 用 design §10.5 的 [{role, content:[part]}] messages IR，
    不是裸 parts。"""
    from backend.wire import hashing
    from backend.wire.normalizers.claude_code import _part_semantic_hash

    parts = [{"type": "text", "text": "hi"}]
    got, _ = _part_semantic_hash(parts)
    # 与显式 messages IR 形状一致
    expect = hashing.semantic_hash("messages", [{"role": "assistant", "content": parts}])
    assert got == expect
    # 与裸 parts 不同（证明确实包了 envelope）
    bare = hashing.semantic_hash("messages", parts)
    assert got != bare


# ---------- 端到端 finalize -------------------------------------------------

def test_end_to_end_aggregate_only_manifest(tmp_path):
    _attempt(tmp_path)
    manifest = _finalize(tmp_path)
    records = _read_wire(tmp_path)
    # 无 llm_call（aggregate-only），无伪造曲线
    assert not any(r["record_type"] == "llm_call" for r in records)
    assert manifest["totals"]["logical_calls"] == 0
    # aggregate 进 manifest
    assert manifest["aggregates"][0]["usage"]["input_tokens"] == 5000
    assert manifest["aggregates"][0]["producer_event_type"] == "turn.completed"
    # call_boundary=aggregate-only 落 source capability
    native = next(s for s in manifest["sources"] if s["kind"] == "native-event")
    assert native["capabilities"]["call_boundary"] == "aggregate-only"


def test_codex_producer_in_derived_evidence(tmp_path):
    """派生 parse-error evidence 的 producer 是 codex，不是硬编码 claude-code。"""
    _attempt(tmp_path)
    run_native_normalizer(agent_name="codex", attempt_id=ATT, data_path=tmp_path)
    lines = [
        json.loads(line)
        for line in paths.source_spool_file(tmp_path, ATT, "native-event")
        .read_text().splitlines()
    ]
    err = next(
        line for line in lines
        if line["evidence_type"] == "capture_event"
        and line["payload"]["event"] == "error"
    )
    assert err["producer"]["name"] == "codex"
    assert err["producer"]["version"] == "codex-normalizer-v1"
