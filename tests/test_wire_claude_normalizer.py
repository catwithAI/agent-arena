"""W1-1 验收：Claude normalizer + 最小 trajectory（design §10.1、§10.6）。

覆盖 tasks.md W1-1 验收清单：
- golden fixture 覆盖多轮/工具/重复事件/无 id/解析失败保留 parser version；
- 对同一 attempt 重跑幂等（R2.1.7）；
- step 在 correlation 前后保持同一 ID，所有非空 trajectory_step_id 可解析；
- native normalizer → finalize 端到端产出调用级 llm_call。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path


from backend.wire import finalize, paths
from backend.wire.normalizers.claude_code import ClaudeCodeNormalizer
from backend.wire.normalizers.runner import run_native_normalizer
from backend.wire.policy import resolve_effective_policy

FIXTURE = Path(__file__).parent / "fixtures" / "wire" / "claude" / "events.jsonl"
ATT = "att_cc1"
POLICY = resolve_effective_policy(task_requested="metadata")


def _attempt(tmp_path) -> Path:
    d = paths.attempt_dir(tmp_path, ATT)
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, d / "events.jsonl")
    return d


# ---------- normalizer 直接产出 --------------------------------------------

def test_multi_turn_tools_and_streaming_merge(tmp_path):
    _attempt(tmp_path)
    r = ClaudeCodeNormalizer().normalize(attempt_id=ATT, attempt_dir=paths.attempt_dir(tmp_path, ATT))
    calls = [e for e in r.evidence if e.evidence_type == "native_llm_call"]
    aggs = [e for e in r.evidence if e.evidence_type == "aggregate_usage"]
    # msg_1(重复两次合并) + msg_2 + 无 id 的 orphan = 3 个 call
    assert len(calls) == 3
    assert len(aggs) == 1
    assert r.parse_errors == 1  # garbage 行

    by_id = {c.payload.producer_call_id: c for c in calls if c.payload.producer_call_id}
    # 流式合并：msg_1 取信息更全的第二版 usage
    m1 = by_id["msg_1"]
    assert m1.payload.usage.output_tokens == 40
    assert m1.payload.usage.cache_read_tokens == 10
    assert m1.payload.usage.cache_write_tokens == 5
    assert m1.payload.finish_reason == "tool_use"
    assert m1.correlation_hints.producer_call_id == "msg_1"
    # 无 id 的 call：inferred（sequence anchor，无 producer_call_id）
    orphan = [c for c in calls if c.payload.producer_call_id is None]
    assert len(orphan) == 1
    assert orphan[0].correlation_hints.sequence is not None

    # aggregate 不伪造 call：result usage 单独
    assert aggs[0].payload.scope == "attempt"
    assert aggs[0].payload.usage.input_tokens == 660


def test_canonical_response_hash_uses_messages_ir(tmp_path):
    """评审 M3：canonical response_summary.content_hash 用 §10.5 messages IR
    （[{role, content}]），不是裸 parts。"""
    import json as _json
    from backend.wire.normalizers.claude_code import _content_to_ir_parts, _part_semantic_hash

    d = paths.attempt_dir(tmp_path, ATT)
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text(_json.dumps({
        "timestamp": "2026-07-14T00:00:01.000Z", "type": "assistant",
        "message": {"id": "m", "role": "assistant", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "canonical 内容"}],
                    "usage": {"input_tokens": 5, "output_tokens": 2}}}) + "\n")
    r = ClaudeCodeNormalizer().normalize(attempt_id=ATT, attempt_dir=d)
    call = next(e for e in r.evidence if e.evidence_type == "native_llm_call")
    got = call.payload.response_summary.content_hash
    # 与 _part_semantic_hash（messages IR）一致，不是裸 parts
    parts = _content_to_ir_parts([{"type": "text", "text": "canonical 内容"}])
    expect, _ = _part_semantic_hash(parts)
    assert got == expect
    assert call.payload.response_summary.hash_domain == "lane-semantic-jcs-nfc-v1"


def test_parser_version_recorded(tmp_path):
    _attempt(tmp_path)
    r = ClaudeCodeNormalizer().normalize(attempt_id=ATT, attempt_dir=paths.attempt_dir(tmp_path, ATT))
    call = next(e for e in r.evidence if e.evidence_type == "native_llm_call")
    assert call.producer.version == "claude-code-normalizer-v1"
    assert call.source.version == "1.2.3"  # CLI 版本


def test_trajectory_steps(tmp_path):
    _attempt(tmp_path)
    r = ClaudeCodeNormalizer().normalize(attempt_id=ATT, attempt_dir=paths.attempt_dir(tmp_path, ATT))
    kinds = [s["kind"] for s in r.trajectory["steps"]]
    # msg_1 出现在两个 assistant 事件（流式），各产一个 step；+ msg_2 + orphan
    assert kinds.count("assistant") == 4
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    # tool_call step 带 tool_call_id 且挂 logical_call_id
    tc = next(s for s in r.trajectory["steps"] if s["kind"] == "tool_call")
    assert tc["tool_call_id"] == "tu_1"
    assert tc["logical_call_id"].startswith("lc_")


def test_normalizer_idempotent(tmp_path):
    _attempt(tmp_path)
    n = ClaudeCodeNormalizer()
    r1 = n.normalize(attempt_id=ATT, attempt_dir=paths.attempt_dir(tmp_path, ATT))
    r2 = n.normalize(attempt_id=ATT, attempt_dir=paths.attempt_dir(tmp_path, ATT))
    assert [e.evidence_id for e in r1.evidence] == [e.evidence_id for e in r2.evidence]
    assert r1.trajectory == r2.trajectory


def test_missing_events_empty_trajectory(tmp_path):
    d = paths.attempt_dir(tmp_path, ATT)
    d.mkdir(parents=True, exist_ok=True)
    r = ClaudeCodeNormalizer().normalize(attempt_id=ATT, attempt_dir=d)
    assert r.evidence == []
    assert r.trajectory["steps"] == []


# ---------- runner：spool + trajectory 落盘 ---------------------------------

def test_runner_writes_spool_and_trajectory(tmp_path):
    _attempt(tmp_path)
    produced = run_native_normalizer(
        agent_name="claude-code", attempt_id=ATT, data_path=tmp_path
    )
    assert produced is True
    spool_path = paths.source_spool_file(tmp_path, ATT, "native-event")
    assert spool_path.exists()
    lines = spool_path.read_text().splitlines()
    # 3 call + 1 aggregate + 1 parse-error capture_event（fixture 含 1 坏行）
    assert len(lines) == 5
    traj = json.loads((paths.attempt_dir(tmp_path, ATT) / "trajectory.json").read_text())
    assert traj["schema_version"] == "lane-trajectory-v1"
    assert len(traj["steps"]) == 6


def test_runner_unknown_agent_noop(tmp_path):
    assert run_native_normalizer(
        agent_name="codex", attempt_id=ATT, data_path=tmp_path
    ) is False


def test_runner_missing_raw_preserves_existing(tmp_path):
    """评审 B2：raw events 缺失时不重建成零调用，保留旧产物。"""
    _attempt(tmp_path)
    run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
    spool_path = paths.source_spool_file(tmp_path, ATT, "native-event")
    saved = spool_path.read_text()
    # 删掉 raw events 后再跑：不得动已有 spool
    (paths.attempt_dir(tmp_path, ATT) / "events.jsonl").unlink()
    produced = run_native_normalizer(
        agent_name="claude-code", attempt_id=ATT, data_path=tmp_path
    )
    assert produced is False
    assert spool_path.read_text() == saved


def test_runner_staging_leaves_no_rebuild_file(tmp_path):
    _attempt(tmp_path)
    run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
    src_dir = paths.sources_dir(tmp_path, ATT)
    assert not list(src_dir.glob("*.rebuild*"))


def test_runner_staging_residue_does_not_corrupt(tmp_path):
    """评审 B1：上次失败留下的 .rebuild.partial 必须先清理，不能被追加。"""
    _attempt(tmp_path)
    run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
    spool_path = paths.source_spool_file(tmp_path, ATT, "native-event")
    clean = spool_path.read_text()
    # 手工造残留 staging partial（模拟上次崩溃）
    staging_partial = spool_path.with_name(spool_path.name + ".rebuild.partial")
    staging_partial.write_text('{"garbage": "residue"}\n')
    # 重跑：残留不得混入，产物幂等
    run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
    assert spool_path.read_text() == clean
    assert not staging_partial.exists()


def test_schema_drift_bad_message_counts_parse_error_not_crash(tmp_path):
    """评审 M3：assistant.message 变 list/string 不让整次 normalizer fail-open，
    坏事件计 parse error 继续处理后续正常事件。"""
    import json as _json
    d = paths.attempt_dir(tmp_path, ATT)
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text("\n".join([
        _json.dumps({"timestamp": "2026-07-13T00:00:01.000Z", "type": "assistant",
                     "message": "this should be an object not a string"}),
        _json.dumps({"timestamp": "2026-07-13T00:00:02.000Z", "type": "assistant",
                     "message": {"id": "msg_ok", "role": "assistant",
                                 "stop_reason": "end_turn",
                                 "content": [{"type": "text", "text": "ok"}],
                                 "usage": {"input_tokens": 10, "output_tokens": 2}}}),
        "not even json",
    ]) + "\n")
    r = ClaudeCodeNormalizer().normalize(attempt_id=ATT, attempt_dir=d)
    calls = [e for e in r.evidence if e.evidence_type == "native_llm_call"]
    assert len(calls) == 1  # 坏事件跳过，正常事件仍产 call
    assert r.parse_errors == 2  # 坏 message + 非 JSON
    assert 1 in r.error_lines and 3 in r.error_lines


def test_parse_error_evidence_has_precise_line_and_utc(tmp_path):
    """评审 M3：parse-error evidence 带精确行号 + 有效 UTC 时间（非空串）。"""
    _attempt(tmp_path)
    run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
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
    assert err["raw_ref"]["line"] is not None  # 精确定位
    assert err["time"]["observed_at"] and err["time"]["observed_at"].endswith("Z")
    assert "lines:" in err["payload"]["message"]


def test_parse_error_emits_capture_event(tmp_path):
    """评审 M3：parse error 写进 spool（capture_event），不只留内存。"""
    _attempt(tmp_path)
    run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
    lines = [
        json.loads(line)
        for line in paths.source_spool_file(tmp_path, ATT, "native-event")
        .read_text()
        .splitlines()
    ]
    errs = [
        line for line in lines
        if line["evidence_type"] == "capture_event"
        and line["payload"]["event"] == "error"
    ]
    assert errs and errs[0]["payload"]["counters"]["parse_errors"] == 1
    assert errs[0]["payload"]["reason_code"] == "parse_failed"


def test_adapter_usage_reconciliation_conflict(tmp_path):
    """评审 M4：adapter 累计 usage 与 native 聚合不一致 → manifest conflict。"""
    _attempt(tmp_path)
    run_native_normalizer(
        agent_name="claude-code", attempt_id=ATT, data_path=tmp_path,
        adapter_usage={"input_tokens": 9999, "output_tokens": 1},
    )
    manifest = finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT, policy=POLICY,
        declared_sources=[{"kind": "native-event", "instance": "native-event"}],
    )
    recon = [a for a in manifest["aggregates"] if a.get("scope") == "reconciliation"]
    assert recon and recon[0]["conflict"]["adapter"]["input_tokens"] == 9999
    assert recon[0]["conflict"]["native"]["input_tokens"] == 460
    assert manifest["totals"]["conflicts"] >= 1
    assert any(g["field"] == "token_usage" for g in manifest["gaps"])


def test_adapter_usage_matching_no_conflict(tmp_path):
    _attempt(tmp_path)
    run_native_normalizer(
        agent_name="claude-code", attempt_id=ATT, data_path=tmp_path,
        adapter_usage={"input_tokens": 460, "output_tokens": 58},
    )
    manifest = finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT, policy=POLICY,
        declared_sources=[{"kind": "native-event", "instance": "native-event"}],
    )
    assert not any(a.get("scope") == "reconciliation" for a in manifest["aggregates"])
    assert not any(g["field"] == "token_usage" for g in manifest["gaps"])


def test_runner_rerun_idempotent(tmp_path):
    _attempt(tmp_path)
    run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
    first = paths.source_spool_file(tmp_path, ATT, "native-event").read_text()
    run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
    second = paths.source_spool_file(tmp_path, ATT, "native-event").read_text()
    assert first == second


# ---------- 端到端：normalize → finalize → canonical llm_call ---------------

def test_end_to_end_finalize_produces_call_level_curve(tmp_path):
    _attempt(tmp_path)
    run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
    manifest = finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT, policy=POLICY,
        declared_sources=[{"kind": "native-event", "instance": "native-event"}],
    )
    records = [
        json.loads(line)
        for line in paths.wire_file(tmp_path, ATT).read_text().splitlines()
    ]
    calls = [r for r in records if r["record_type"] == "llm_call"]
    assert len(calls) == 3
    # 调用级 token 曲线：每个 call 有 usage
    assert all(c["data"]["usage"] for c in calls)
    # fixture 含 1 坏行 → parse-error capture_event → native source partial
    assert manifest["coverage"]["agent_semantics"] == "partial"
    # aggregate 不进 canonical，进 manifest
    assert manifest["aggregates"][0]["usage"]["input_tokens"] == 660

    # 显式 msg id 的 call confidence=explicit，无 id 的 unmatched/inferred
    explicit = [c for c in calls if c["correlation"]["confidence"] == "explicit"]
    assert len(explicit) == 2


def test_trajectory_step_lc_resolves_after_finalize(tmp_path):
    """step 在 correlation 前后保持同一 ID，logical_call_id 可解析（§10.6）。"""
    _attempt(tmp_path)
    run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
    traj_before = json.loads(
        (paths.attempt_dir(tmp_path, ATT) / "trajectory.json").read_text()
    )
    step_ids_before = [s["step_id"] for s in traj_before["steps"]]

    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT, policy=POLICY,
        declared_sources=[{"kind": "native-event", "instance": "native-event"}],
    )
    traj_after = json.loads(
        (paths.attempt_dir(tmp_path, ATT) / "trajectory.json").read_text()
    )
    # step ID 不因 correlation 改变
    assert [s["step_id"] for s in traj_after["steps"]] == step_ids_before

    # 所有非空 logical_call_id 都出现在 canonical 的 llm_call 里
    records = [
        json.loads(line)
        for line in paths.wire_file(tmp_path, ATT).read_text().splitlines()
    ]
    canonical_lcs = {
        r["correlation"].get("logical_call_id")
        for r in records
        if r["record_type"] == "llm_call"
    }
    step_lcs = {
        s["logical_call_id"] for s in traj_after["steps"] if s["logical_call_id"]
    }
    assert step_lcs and step_lcs <= canonical_lcs
