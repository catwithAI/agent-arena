"""W0-5 验收：finalizer + manifest + correlate（显式 anchor 部分）。

覆盖 tasks.md W0-5 验收清单：
- fake source 写 evidence 后生成可读 canonical + manifest；
- 「零通信」与「source 启动失败」manifest 可区分（R12.1）；
- verification/unknown phase 的 evidence 不进 agent_run 聚合（R3.6）；
- 七类 evidence type 全部执行规定映射：aggregate_usage 不伪造 call，
  证据不足的 compaction_hint 不伪造 context_compaction；
- correlation-map.json 离线重建复用 ID；
- MCP jsonrpc_id 只配对 MCP frame，不与 LLM logical call 交叉合并。
"""

from __future__ import annotations

import json

from backend.wire import correlate, evidence, finalize, paths, spool
from backend.wire.policy import resolve_effective_policy

POLICY = resolve_effective_policy(task_requested="metadata")
ATT = "att_w05"


def _ev(
    evidence_type: str,
    i: int,
    *,
    phase: str = "agent_run",
    kind: str = "native-event",
    instance: str = "native-event",
    hints: dict | None = None,
    payload: dict | None = None,
) -> dict:
    return {
        "evidence_id": f"we_{evidence_type}_{i}",
        "attempt_id": ATT,
        "phase": phase,
        "evidence_type": evidence_type,
        "source": {"kind": kind, "instance": instance},
        "producer": {"name": "test"},
        "time": {"observed_at": f"2026-07-13T00:00:{i:02d}.000Z"},
        "raw_ref": {"kind": "raw-line", "file": "events.jsonl", "line": i},
        "correlation_hints": hints or {},
        "capabilities": {},
        "redaction": {"policy": "metadata", "status": "applied"},
        "errors": [],
        "extensions": {},
        "payload": {**evidence.null_payload(evidence_type), **(payload or {})},
    }


def _write_spool(tmp_path, kind, records, *, instance=None, close=True):
    w = spool.SpoolWriter(
        paths.source_spool_file(tmp_path, ATT, kind, instance),
        expected_attempt_id=ATT,
    )
    for r in records:
        w.append(r)
    if close:
        w.close()
    else:
        w.abandon()
    return w


def _finalize(tmp_path, **kwargs):
    defaults = dict(
        data_path=tmp_path, attempt_id=ATT, policy=POLICY,
        started_at="2026-07-13T00:00:00.000Z", finished_at="2026-07-13T00:01:00.000Z",
    )
    defaults.update(kwargs)
    return finalize.finalize_attempt(**defaults)


def _read_wire(tmp_path):
    p = paths.wire_file(tmp_path, ATT)
    return [json.loads(line) for line in p.read_text().splitlines()]


# ---------- 基本 finalize：canonical + manifest -----------------------------

def test_fake_source_evidence_to_canonical_and_manifest(tmp_path):
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1,
            hints={"producer_call_id": "msg_a"},
            payload={"model": "glm-5.2", "call_role": "main",
                     "usage": {"input_tokens": 100, "output_tokens": 20,
                               "cache_read_tokens": None, "cache_write_tokens": None,
                               "reasoning_tokens": None, "estimated": None}}),
        _ev("capture_event", 2, payload={"event": "start", "source_instance": "native-event"}),
    ])
    manifest = _finalize(tmp_path)
    records = _read_wire(tmp_path)
    assert manifest["status"] == "complete"
    assert manifest["schema_version"] == "lane-wire-manifest-v1"
    assert manifest["policy"] == {
        "requested": "metadata", "effective": "metadata", "downgrade_reason": None,
    }
    types = {r["record_type"] for r in records}
    assert types == {"llm_call", "capture_event"}
    call = next(r for r in records if r["record_type"] == "llm_call")
    assert call["correlation"]["logical_call_id"].startswith("lc_")
    assert call["correlation"]["confidence"] == "explicit"
    assert call["data"]["usage"]["input_tokens"] == 100
    assert manifest["totals"]["logical_calls"] == 1
    assert manifest["coverage"]["correlated_calls"] == 1


def test_generation_increments_per_finalize(tmp_path):
    _write_spool(tmp_path, "native-event", [_ev("native_llm_call", 1)])
    m1 = _finalize(tmp_path)
    m2 = _finalize(tmp_path)
    assert m2["generation"] == m1["generation"] + 1


# ---------- R12.1：零通信 vs source 没工作 ----------------------------------

def test_zero_communication_vs_source_failure_distinguishable(tmp_path):
    # source A：干净关闭、0 行 = 零通信
    _write_spool(tmp_path, "native-event", [])
    # source B：声明启用但没有任何 spool = 启动失败
    manifest = _finalize(
        tmp_path,
        declared_sources=[
            {"kind": "native-event", "instance": "native-event"},
            {"kind": "http-proxy", "instance": "p1"},
        ],
        gaps=[{"field": "http-proxy", "reason": "source_start_failed"}],
    )
    by_kind = {s["kind"]: s for s in manifest["sources"]}
    assert by_kind["native-event"]["status"] == "complete"
    assert by_kind["native-event"]["records"] == 0          # 零通信
    assert by_kind["http-proxy"]["status"] == "failed"       # 没工作
    assert by_kind["http-proxy"]["failure_reason"] == "source_start_failed"
    assert manifest["status"] == "partial"


def test_crashed_spool_marks_source_partial(tmp_path):
    _write_spool(tmp_path, "native-event", [_ev("native_llm_call", 1)], close=False)
    manifest = _finalize(tmp_path)
    src = manifest["sources"][0]
    assert src["status"] == "partial"
    assert manifest["status"] == "partial"
    assert len(_read_wire(tmp_path)) == 1  # 已写完整行仍可用


def test_all_declared_failed_manifest_failed(tmp_path):
    manifest = _finalize(
        tmp_path,
        declared_sources=[{"kind": "http-proxy", "instance": "p1"}],
        gaps=[{"field": "http-proxy", "reason": "source_start_failed"}],
    )
    assert manifest["status"] == "failed"


def test_policy_off_or_nothing_is_not_applicable(tmp_path):
    manifest = _finalize(tmp_path)  # 无声明无 spool
    assert manifest["status"] == "not-applicable"


# ---------- R3.6：phase 聚合排除 --------------------------------------------

def test_verification_and_unknown_phase_excluded_from_agent_run(tmp_path):
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, phase="agent_run",
            hints={"producer_call_id": "a"}),
        _ev("native_llm_call", 2, phase="verification",
            hints={"producer_call_id": "b"}),
        _ev("native_llm_call", 3, phase="unknown",
            hints={"producer_call_id": "c"}),
    ])
    _finalize(tmp_path)
    records = _read_wire(tmp_path)
    assert len(records) == 3  # verification/unknown 单独保留
    agent_calls = finalize.select_agent_run_calls(records)
    assert len(agent_calls) == 1
    assert agent_calls[0]["phase"] == "agent_run"


# ---------- 七类映射 ---------------------------------------------------------

def test_all_seven_types_mapping(tmp_path):
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "msg_a"},
            payload={"finish_reason": "end_turn"}),
        _ev("aggregate_usage", 2, payload={
            "scope": "attempt",
            "usage": {"input_tokens": 500, "output_tokens": 50,
                      "cache_read_tokens": None, "cache_write_tokens": None,
                      "reasoning_tokens": None, "estimated": None},
            "producer_event_type": "token_count"}),
        _ev("compaction_hint", 3, payload={"strategy": "full-summary",
                                            "confidence": "low"}),
        _ev("capture_event", 4, payload={"event": "ready",
                                          "source_instance": "native-event"}),
    ])
    _write_spool(tmp_path, "http-proxy", [
        _ev("http_exchange", 1, kind="http-proxy", instance="p1",
            hints={"request_id": "req_1", "producer_call_id": "msg_a"},
            payload={"method": "POST", "status_code": 200}),
        _ev("stream_chunk", 2, kind="http-proxy", instance="p1",
            payload={"hop_anchor": "proxy-request:req_1", "sequence": 0,
                     "bytes": 64}),
    ], instance="p1")
    _write_spool(tmp_path, "mcp-stdio", [
        _ev("mcp_frame", 1, kind="mcp-stdio", instance="env1",
            hints={"jsonrpc_id": "7"},
            payload={"direction": "client-to-server", "jsonrpc_id": "7",
                     "message_kind": "request", "method": "tools/call",
                     "tool_name": "create_plan"}),
        _ev("mcp_frame", 2, kind="mcp-stdio", instance="env1",
            hints={"jsonrpc_id": "7"},
            payload={"direction": "server-to-client", "jsonrpc_id": "7",
                     "message_kind": "response"}),
    ], instance="env1")

    manifest = _finalize(tmp_path)
    records = _read_wire(tmp_path)
    by_type: dict[str, list] = {}
    for r in records:
        by_type.setdefault(r["record_type"], []).append(r)

    # aggregate_usage 不伪造 call：canonical 里没有多余 llm_call，进 manifest
    assert len(by_type["llm_call"]) == 1
    assert by_type["llm_call"][0]["data"]["finish_reason"] == "end_turn"
    assert manifest["aggregates"][0]["scope"] == "attempt"
    assert manifest["aggregates"][0]["usage"]["input_tokens"] == 500
    # compaction_hint 证据不足：不出现 context_compaction record
    assert "context_compaction" not in by_type
    assert manifest["compaction_hints"][0]["strategy"] == "full-summary"
    # http_exchange 显式 anchor 关联到同一 logical call
    call_lc = by_type["llm_call"][0]["correlation"]["logical_call_id"]
    hop = by_type["http_exchange"][0]
    assert hop["correlation"]["logical_call_id"] == call_lc
    assert hop["data"]["hop_id"].startswith("hop_")
    # stream_chunk 挂到同一 hop
    assert by_type["stream_chunk"][0]["data"]["hop_id"] == hop["data"]["hop_id"]
    # mcp_frame 按 jsonrpc_id 配对，且不产生 logical call 关联
    req, resp = by_type["mcp_frame"]
    assert req["data"]["paired_record_id"] == resp["record_id"]
    assert resp["data"]["paired_record_id"] == req["record_id"]
    assert req["correlation"].get("logical_call_id") is None
    # capture_event 正常映射
    assert by_type["capture_event"][0]["data"]["event"] == "ready"
    assert manifest["totals"]["hops"] == 1


def test_mcp_jsonrpc_never_merges_with_llm_call(tmp_path):
    """jsonrpc_id 与 producer_call_id 撞值也不得互相合并。"""
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "7"}),
    ])
    _write_spool(tmp_path, "mcp-stdio", [
        _ev("mcp_frame", 1, kind="mcp-stdio", instance="env1",
            hints={"jsonrpc_id": "7"},
            payload={"direction": "client-to-server", "jsonrpc_id": "7",
                     "message_kind": "request", "method": "tools/call"}),
    ], instance="env1")
    _finalize(tmp_path)
    records = _read_wire(tmp_path)
    frame = next(r for r in records if r["record_type"] == "mcp_frame")
    assert frame["correlation"].get("logical_call_id") is None


def test_http_without_anchor_stays_unmatched(tmp_path):
    """并行请求无显式 ID：不按时间/顺序强配（§7.3）。"""
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "msg_a"}),
    ])
    _write_spool(tmp_path, "http-proxy", [
        _ev("http_exchange", 1, kind="http-proxy", instance="p1",
            payload={"method": "POST", "status_code": 200}),
    ], instance="p1")
    manifest = _finalize(tmp_path)
    records = _read_wire(tmp_path)
    hop = next(r for r in records if r["record_type"] == "http_exchange")
    assert hop["correlation"]["confidence"] == "unmatched"
    assert hop["correlation"].get("logical_call_id") is None
    # 评审 M6：unmatched hop 计入 unmatched_calls（与 UI 分组一致）
    assert manifest["coverage"]["unmatched_calls"] >= 1


def test_unmatched_count_consistent_with_confidence(tmp_path):
    """评审 M6：unmatched_calls 只数 confidence==unmatched；inferred call 是
    已匹配（有 lc）不计 unmatched，与 UI 曲线口径一致。"""
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "a"}),  # explicit
        _ev("native_llm_call", 2),                                    # inferred（无 id）
    ])
    _write_spool(tmp_path, "http-proxy", [
        _ev("http_exchange", 1, kind="http-proxy", instance="p1",
            payload={"method": "POST", "status_code": 200}),          # unmatched
    ], instance="p1")
    manifest = _finalize(tmp_path)
    records = _read_wire(tmp_path)
    confs = [r["correlation"]["confidence"] for r in records
             if r["record_type"] in ("llm_call", "http_exchange")]
    # inferred call 不算 unmatched；只有那条 http hop 算
    n_unmatched = sum(1 for c in confs if c == "unmatched")
    assert manifest["coverage"]["unmatched_calls"] == n_unmatched == 1
    # inferred call 有 lc（进曲线）
    inferred = [r for r in records if r["correlation"]["confidence"] == "inferred"]
    assert inferred and inferred[0]["correlation"]["logical_call_id"]


def test_old_evidence_without_direction_validates(tmp_path):
    """评审 B4：direction 是 v1 内追加可选字段，旧 http_exchange evidence（不带
    direction 键）仍能 validate，finalize fallback outbound。"""
    d = _ev("http_exchange", 1, payload={"method": "POST", "status_code": 200})
    d["payload"].pop("direction", None)  # 旧 v1 无此键
    obj = evidence.validate_evidence(d)  # 不抛
    assert obj.payload.direction is None

def test_correlation_map_reused_across_rebuilds(tmp_path):
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "msg_a"}),
    ])
    _finalize(tmp_path)
    lc1 = _read_wire(tmp_path)[0]["correlation"]["logical_call_id"]
    map_path = paths.sources_dir(tmp_path, ATT) / "correlation-map.json"
    assert json.loads(map_path.read_text())["anchors"] == {
        "producer-call:msg_a": lc1
    }
    # 重建（如 parser 升级后 anchor 选择变化）：旧映射优先复用
    _finalize(tmp_path)
    lc2 = _read_wire(tmp_path)[0]["correlation"]["logical_call_id"]
    assert lc2 == lc1


def test_correlation_map_new_anchor_joins_existing_call(tmp_path):
    """后到的 gateway evidence 带 response id + 同 producer call id：
    不产生第二个 call，response id 登记到同一 lc。"""
    cmap = correlate.CorrelationMap(attempt_id=ATT)
    lc_a, _, conf = cmap.resolve_call(["producer-call:msg_a"])
    lc_b, _, _ = cmap.resolve_call(
        ["producer-call:msg_a", "provider-response:resp_1"]
    )
    assert lc_a == lc_b and conf == "explicit"
    assert cmap.anchors["provider-response:resp_1"] == lc_a
    # 顺序锚点 confidence 是 inferred
    _, _, conf2 = cmap.resolve_call(
        [correlate.sequence_anchor("native-event", "native-event", 5)]
    )
    assert conf2 == "inferred"


# ---------- W0-7 startup recovery --------------------------------------------

def _seed_db_attempt(db_path, attempt_id: str, status: str) -> None:
    from backend.db import _init_db_sync, _open_sync

    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(id, env_name, prompt, created_at) "
            "VALUES('task-1', 'env', 'prompt', 'now')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO runs(id, task_id, env_name, status, created_at) "
            "VALUES('run-1', 'task-1', 'env', 'running', 'now')"
        )
        conn.execute(
            "INSERT INTO attempts(id, run_id, task_id, env_name, agent_name, "
            "status, session_id, session_token_hash, created_at) "
            "VALUES(?, 'run-1', 'task-1', 'env', 'claude-code', ?, ?, 'hash', 'now')",
            (attempt_id, status, f"sess-{attempt_id}"),
        )
        conn.commit()


def _crash_leftovers(tmp_path):
    """模拟崩溃：.partial spool + in-progress manifest。"""
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "msg_a"}),
    ], close=False)
    finalize.write_in_progress_manifest(
        data_path=tmp_path, attempt_id=ATT, policy=POLICY, strict=False,
        started_at="2026-07-13T00:00:00.000Z",
    )


def test_recovery_finalizes_crashed_attempt_as_recovered(tmp_path):
    from backend.wire.recovery import recover_wire_manifests

    db = tmp_path / "lane.db"
    _seed_db_attempt(db, ATT, "timeout")  # attempt 已终态
    _crash_leftovers(tmp_path)
    handled = recover_wire_manifests(tmp_path, db)
    assert handled == 1
    manifest = json.loads(paths.manifest_file(tmp_path, ATT).read_text())
    assert manifest["status"] == "recovered"
    assert len(_read_wire(tmp_path)) == 1  # .partial 里的完整行被恢复
    import sqlite3

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT wire_status, wire_record_count FROM attempts WHERE id=?", (ATT,)
        ).fetchone()
    assert row == ("recovered", 1)


def test_recovery_skips_running_attempt(tmp_path):
    from backend.wire.recovery import recover_wire_manifests

    db = tmp_path / "lane.db"
    _seed_db_attempt(db, ATT, "running")
    _crash_leftovers(tmp_path)
    assert recover_wire_manifests(tmp_path, db) == 0
    manifest = json.loads(paths.manifest_file(tmp_path, ATT).read_text())
    assert manifest["status"] == "in-progress"  # 活着的 attempt 不动


def test_recovery_does_not_refinalize_completed_manifest(tmp_path):
    from backend.wire.recovery import recover_wire_manifests

    db = tmp_path / "lane.db"
    _seed_db_attempt(db, ATT, "completed")
    _write_spool(tmp_path, "native-event", [_ev("native_llm_call", 1)])
    m = _finalize(tmp_path)  # 已正常 finalize
    assert recover_wire_manifests(tmp_path, db) == 0
    manifest = json.loads(paths.manifest_file(tmp_path, ATT).read_text())
    assert manifest["generation"] == m["generation"]  # 没被重复 finalize


def test_recovery_marks_failed_when_finalize_impossible(tmp_path, monkeypatch):
    from backend.wire import recovery as wr

    db = tmp_path / "lane.db"
    _seed_db_attempt(db, ATT, "timeout")
    _crash_leftovers(tmp_path)

    def boom(**_kwargs):
        raise OSError("disk broke")

    monkeypatch.setattr(wr.finalize, "finalize_attempt", boom)
    assert wr.recover_wire_manifests(tmp_path, db) == 1
    manifest = json.loads(paths.manifest_file(tmp_path, ATT).read_text())
    assert manifest["status"] == "failed"  # 绝不长期伪装 in-progress


# ---------- split-brain 合并（评审 B1）---------------------------------------

def test_bridge_evidence_merges_split_logical_calls():
    """native 先见 producer-call、gateway 先见 provider-response，桥接 evidence
    到达后所有 anchor 必须收敛到同一 lc，后续单 anchor 不再分裂。"""
    cmap = correlate.CorrelationMap(attempt_id=ATT)
    lc_a, _, _ = cmap.resolve_call(["producer-call:p"])
    lc_b, _, _ = cmap.resolve_call(["provider-response:r"])
    assert lc_a != lc_b  # 桥接前确实是两个集合
    lc_bridge, _, _ = cmap.resolve_call(["producer-call:p", "provider-response:r"])
    lc_p, _, _ = cmap.resolve_call(["producer-call:p"])
    lc_r, _, _ = cmap.resolve_call(["provider-response:r"])
    assert lc_bridge == lc_p == lc_r  # 不再 split-brain
    assert cmap.anchors["producer-call:p"] == cmap.anchors["provider-response:r"]


def test_finalize_bridge_in_same_pass_yields_single_call(tmp_path):
    """同一次 finalize 内：先 native、后 gateway 桥接——canonical 里所有相关
    记录用同一个 lc（两阶段解析，不受到达顺序影响）。"""
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "p"}),
    ])
    _write_spool(tmp_path, "http-proxy", [
        # gateway/proxy 只带 response id
        _ev("http_exchange", 1, kind="http-proxy", instance="p1",
            hints={"provider_response_id": "r"},
            payload={"method": "POST", "status_code": 200}),
        # 桥接：同时带两个 anchor
        _ev("http_exchange", 2, kind="http-proxy", instance="p1",
            hints={"producer_call_id": "p", "provider_response_id": "r"},
            payload={"method": "POST", "status_code": 200}),
    ], instance="p1")
    manifest = _finalize(tmp_path)
    records = _read_wire(tmp_path)
    lcs = {
        r["correlation"]["logical_call_id"]
        for r in records
        if r["correlation"].get("logical_call_id")
    }
    assert len(lcs) == 1
    assert manifest["totals"]["logical_calls"] == 1


# ---------- capture_event 驱动 source status（评审 M1/M2）--------------------

def test_capture_error_event_downgrades_source_status(tmp_path):
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "a"}),
        _ev("capture_event", 2, payload={
            "event": "error", "source_instance": "native-event",
            "reason_code": "spool_write_failed"}),
    ])
    manifest = _finalize(tmp_path)
    src = next(s for s in manifest["sources"] if s["kind"] == "native-event")
    assert src["status"] == "partial"
    assert src["errors"] == 1
    assert manifest["status"] == "partial"


def test_capture_drop_event_counts_dropped(tmp_path):
    _write_spool(tmp_path, "native-event", [
        _ev("capture_event", 1, payload={
            "event": "drop", "source_instance": "native-event",
            "reason_code": "queue_full"}),
    ])
    manifest = _finalize(tmp_path)
    src = next(s for s in manifest["sources"] if s["kind"] == "native-event")
    assert src["dropped"] == 1 and src["status"] == "partial"


def test_capabilities_and_coverage_axes(tmp_path):
    ev1 = _ev("native_llm_call", 1, hints={"producer_call_id": "a"})
    ev1["capabilities"] = {"call_boundary": True}
    _write_spool(tmp_path, "native-event", [ev1])
    _write_spool(tmp_path, "mcp-stdio", [
        _ev("mcp_frame", 1, kind="mcp-stdio", instance="env1",
            payload={"direction": "client-to-server", "jsonrpc_id": "1",
                     "message_kind": "request", "method": "tools/call"}),
    ], instance="env1")
    manifest = _finalize(tmp_path)
    src = next(s for s in manifest["sources"] if s["kind"] == "native-event")
    assert src["capabilities"] == {"call_boundary": True}
    cov = manifest["coverage"]
    assert cov["agent_semantics"] == "complete"
    assert cov["mcp"] == "complete"
    assert cov["llm_transport"] == "not-observed"
    # capture_event 的 effective_capabilities 进 canonical
    ev2 = _ev("capture_event", 9, payload={
        "event": "ready", "source_instance": "native-event",
        "effective_capabilities": {"llm_base_url": True}})
    _write_spool(tmp_path, "capture-events", [ev2])
    _finalize(tmp_path)
    rec = next(
        r for r in _read_wire(tmp_path) if r["record_type"] == "capture_event"
    )
    assert rec["data"]["effective_capabilities"] == {"llm_base_url": True}


# ---------- recovery 快照（评审 B2/M3）---------------------------------------

def test_recovery_no_spool_with_declared_sources_is_failed(tmp_path):
    """capture 启动了但 source 在建 spool 前失败：恢复后必须 failed，
    不是 not-applicable——declared_sources 从 in-progress manifest 复原。"""
    from backend.wire.recovery import recover_wire_manifests

    db = tmp_path / "lane.db"
    _seed_db_attempt(db, ATT, "timeout")
    finalize.write_in_progress_manifest(
        data_path=tmp_path, attempt_id=ATT, policy=POLICY, strict=False,
        started_at="2026-07-13T00:00:00.000Z",
        declared_sources=[{"kind": "http-proxy", "instance": "p1"}],
        gaps=[{"field": "http-proxy", "reason": "source_start_failed"}],
    )
    assert recover_wire_manifests(tmp_path, db) == 1
    manifest = json.loads(paths.manifest_file(tmp_path, ATT).read_text())
    assert manifest["status"] == "failed"
    src = manifest["sources"][0]
    assert src["status"] == "failed"
    assert src["failure_reason"] == "source_start_failed"
    import sqlite3

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT wire_status FROM attempts WHERE id=?", (ATT,)
        ).fetchone()
    assert row[0] == "failed"


def test_recovery_failed_write_also_updates_db(tmp_path, monkeypatch):
    """finalize 不可能时写 failed manifest，DB 摘要必须同步（评审 M3）。"""
    from backend.wire import recovery as wr

    db = tmp_path / "lane.db"
    _seed_db_attempt(db, ATT, "timeout")
    _crash_leftovers(tmp_path)

    def boom(**_kwargs):
        raise OSError("disk broke")

    monkeypatch.setattr(wr.finalize, "finalize_attempt", boom)
    wr.recover_wire_manifests(tmp_path, db)
    import sqlite3

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT wire_status FROM attempts WHERE id=?", (ATT,)
        ).fetchone()
    assert row[0] == "failed"


def test_recovery_single_attempt_filter(tmp_path):
    """attempt 级恢复收尾时按单 attempt 触发（异步 recovery 竞态补救）。"""
    from backend.wire.recovery import recover_wire_manifests

    db = tmp_path / "lane.db"
    _seed_db_attempt(db, ATT, "timeout")
    _crash_leftovers(tmp_path)
    assert recover_wire_manifests(tmp_path, db, attempt_id="att_other") == 0
    assert recover_wire_manifests(tmp_path, db, attempt_id=ATT) == 1


# ---------- 第五轮评审回归 ----------------------------------------------------

def test_recovery_skips_non_terminal_running_status(tmp_path):
    """running 是可恢复中间态，不是终态——不得提前 finalize
    （否则 attempt 恢复成功后 manifest 已非 in-progress，无法补收敛）。"""
    from backend.wire.recovery import recover_wire_manifests

    db = tmp_path / "lane.db"
    _seed_db_attempt(db, ATT, "running")
    _crash_leftovers(tmp_path)
    assert recover_wire_manifests(tmp_path, db) == 0
    manifest = json.loads(paths.manifest_file(tmp_path, ATT).read_text())
    assert manifest["status"] == "in-progress"


def test_recovery_skips_unknown_attempt(tmp_path):
    """DB 查不到 attempt（None）不当终态处理。"""
    from backend.wire.recovery import recover_wire_manifests

    from backend.db import _init_db_sync

    db = tmp_path / "lane.db"
    _init_db_sync(db)  # 空库，attempt 不存在
    _crash_leftovers(tmp_path)
    assert recover_wire_manifests(tmp_path, db) == 0


def test_payload_producer_call_id_used_as_anchor(tmp_path):
    """producer 只按 payload 契约填 producer_call_id 时不得退化为 seq anchor：
    两个 source 的同 ID evidence 必须合并成一个 logical call。"""
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, payload={"producer_call_id": "same"}),
    ])
    _write_spool(tmp_path, "llm-gateway", [
        _ev("native_llm_call", 1, kind="llm-gateway", instance="llm-gateway",
            payload={"producer_call_id": "same"}),
    ])
    manifest = _finalize(tmp_path)
    assert manifest["totals"]["logical_calls"] == 1
    records = _read_wire(tmp_path)
    lcs = {r["correlation"]["logical_call_id"] for r in records
           if r["record_type"] == "llm_call"}
    assert len(lcs) == 1


def test_payload_hints_producer_id_conflict_recorded(tmp_path):
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "from_hints"},
            payload={"producer_call_id": "from_payload"}),
    ])
    manifest = _finalize(tmp_path)
    call = next(r for r in _read_wire(tmp_path) if r["record_type"] == "llm_call")
    assert call["conflicts"] and call["conflicts"][0]["field"] == "producer_call_id"
    assert call["conflicts"][0]["selected"] == "from_hints"
    assert manifest["totals"]["conflicts"] == 1


def test_capture_error_does_not_fan_out_to_sibling_instances(tmp_path):
    """p1 的 error 不得污染同 kind 的 p2（按 resolved instance 归属）。"""
    _write_spool(tmp_path, "http-proxy", [
        _ev("http_exchange", 1, kind="http-proxy", instance="p1",
            payload={"method": "POST", "status_code": 200}),
    ], instance="p1")
    _write_spool(tmp_path, "http-proxy", [
        _ev("http_exchange", 1, kind="http-proxy", instance="p2",
            payload={"method": "POST", "status_code": 200}),
    ], instance="p2")
    _write_spool(tmp_path, "capture-events", [
        _ev("capture_event", 1, kind="capture-events", instance="capture-events",
            payload={"event": "error", "source_instance": "p1",
                     "reason_code": "spool_write_failed"}),
    ])
    manifest = _finalize(tmp_path)
    by_instance = {s["instance"]: s for s in manifest["sources"]}
    assert by_instance["p1"]["status"] == "partial" and by_instance["p1"]["errors"] == 1
    assert by_instance["p2"]["status"] == "complete" and by_instance["p2"]["errors"] == 0


def test_null_call_role_maps_to_unknown_not_main(tmp_path):
    """call_role 不可得（null）→ unknown，不伪造 main（R1.4）。"""
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "a"}),  # call_role=null
        _ev("native_llm_call", 2, hints={"producer_call_id": "b"},
            payload={"call_role": "compaction"}),
    ])
    _finalize(tmp_path)
    calls = {r["provenance"][0]["evidence_id"]: r["data"]["call_role"]
             for r in _read_wire(tmp_path) if r["record_type"] == "llm_call"}
    assert calls["we_native_llm_call_1"] == "unknown"
    assert calls["we_native_llm_call_2"] == "compaction"


def test_nulls_preserved_in_canonical(tmp_path):
    """R1.4：不可观测字段保留 null，不伪造 False/0/client-to-server。"""
    _write_spool(tmp_path, "mcp-stdio", [
        _ev("mcp_frame", 1, kind="mcp-stdio", instance="env1",
            payload={"method": "tools/call"}),  # direction/is_error 等全 null
    ], instance="env1")
    _write_spool(tmp_path, "http-proxy", [
        _ev("http_exchange", 1, kind="http-proxy", instance="p1",
            payload={"method": "POST"}),
        _ev("stream_chunk", 2, kind="http-proxy", instance="p1",
            payload={"hop_anchor": "x", "sequence": 0}),
    ], instance="p1")
    _finalize(tmp_path)
    records = {r["record_type"]: r for r in _read_wire(tmp_path)}
    frame = records["mcp_frame"]["data"]
    assert frame["direction"] is None and frame["is_error"] is None
    assert records["http_exchange"]["data"]["partial"] is None
    chunk = records["stream_chunk"]["data"]
    assert chunk["is_terminal"] is None and chunk["dropped_before"] is None


def test_cumulative_counters_enter_completeness(tmp_path):
    """stop 事件报 records_dropped=100 时 manifest 不得仍是 complete；
    多次 cumulative 汇报取 max 不重复相加。"""
    _write_spool(tmp_path, "http-proxy", [
        _ev("http_exchange", 1, kind="http-proxy", instance="p1",
            payload={"method": "POST", "status_code": 200}),
    ], instance="p1")
    _write_spool(tmp_path, "capture-events", [
        _ev("capture_event", 1, kind="capture-events", instance="capture-events",
            payload={"event": "phase_change", "source_instance": "p1",
                     "counters": {"records_dropped": 40}}),
        _ev("capture_event", 2, kind="capture-events", instance="capture-events",
            payload={"event": "stop", "source_instance": "p1",
                     "counters": {"records_dropped": 100}}),
    ])
    manifest = _finalize(tmp_path)
    p1 = next(s for s in manifest["sources"] if s["instance"] == "p1")
    assert p1["dropped"] == 100  # max，不是 140
    assert p1["status"] == "partial"
    assert manifest["status"] == "partial"


def test_dangling_trajectory_step_forces_partial(tmp_path):
    """评审 M5：trajectory 里有解析不了的 logical_call_id 时 manifest 必须
    partial——引用完整性检查在状态计算之前完成。"""
    from backend.wire import writer as _writer

    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "a"}),
    ])
    # 手工写一个含悬空 lc 的 trajectory（正常 normalizer 不会，模拟不一致）
    _writer.atomic_write_json(
        paths.attempt_dir(tmp_path, ATT) / "trajectory.json",
        {
            "schema_version": "lane-trajectory-v1",
            "attempt_id": ATT,
            "steps": [{
                "step_id": "ts_x", "sequence": 1, "timestamp": None,
                "agent_id": "main", "parent_agent_id": None, "kind": "assistant",
                "producer_event_refs": [], "tool_call_id": None,
                "logical_call_id": "lc_does_not_exist",
            }],
        },
    )
    manifest = _finalize(tmp_path)
    assert manifest["status"] == "partial"
    assert any(g["field"] == "trajectory" for g in manifest["gaps"])


def test_manifest_contains_wire_file_fingerprint(tmp_path):
    _write_spool(tmp_path, "native-event", [
        _ev("native_llm_call", 1, hints={"producer_call_id": "a"}),
    ])
    manifest = _finalize(tmp_path)
    fp = manifest["wire_file"]
    wire_bytes = paths.wire_file(tmp_path, ATT).read_bytes()
    assert fp["bytes"] == len(wire_bytes)
    import hashlib

    assert fp["sha256"] == hashlib.sha256(wire_bytes).hexdigest()
    assert fp["records"] == manifest["totals"]["records"]


# ---------- in-progress manifest 与 lifecycle 集成 ---------------------------

async def test_lifecycle_end_to_end_finalize(tmp_path):
    """fake source 经完整 lifecycle：prepare(in-progress) → 写 evidence →
    attempt_end 产出 canonical + finalized manifest。"""
    from backend.wire.injection import WireInjection
    from backend.wire.lifecycle import WireCaptureSession

    class SpoolingSource:
        kind = "native-event"
        instance = "native-event"
        rewrites_transport = False

        async def start(self, ctx):
            self._writer = spool.SpoolWriter(
                paths.source_spool_file(ctx.attempt_dir.parent.parent, ATT, self.kind),
                expected_attempt_id=ATT,
            )
            return WireInjection(enabled=True, process_env={"X_OK": "1"})

        async def collect(self, ctx):
            return {}

        async def stop(self, ctx):
            self._writer.append(_ev("native_llm_call", 1,
                                    hints={"producer_call_id": "msg_a"}))
            self._writer.close()
            return {}

    session = WireCaptureSession(
        attempt_id=ATT, data_path=tmp_path, agent_name="claude-code",
        sources=[SpoolingSource()],
    )
    await session.prepare()
    manifest_path = paths.manifest_file(tmp_path, ATT)
    assert json.loads(manifest_path.read_text())["status"] == "in-progress"
    async with session.phase("agent_run"):
        pass
    await session.attempt_end()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] in ("complete", "partial")
    assert manifest["generation"] >= 1
    records = _read_wire(tmp_path)
    assert any(r["record_type"] == "llm_call" for r in records)
    assert any(r["record_type"] == "capture_event" for r in records)


def test_lane_http_source_counts_as_llm_transport(tmp_path):
    """评审 #8：lane-http source 采集成功后 coverage.llm_transport 不能
    显示 not-observed。"""
    _write_spool(tmp_path, "lane-http", [
        _ev("http_exchange", 1, kind="lane-http", instance="up",
            payload={"direction": "outbound", "method": "POST", "path": "/v1/messages",
                     "status_code": 200, "request_bytes": 10, "response_bytes": 20,
                     "streamed": False, "partial": False}),
        _ev("capture_event", 2, kind="lane-http", instance="up",
            payload={"event": "start", "source_instance": "up"}),
    ], instance="up")
    manifest = _finalize(
        tmp_path, declared_sources=[{"kind": "lane-http", "instance": "up"}]
    )
    assert manifest["coverage"]["llm_transport"] != "not-observed"
