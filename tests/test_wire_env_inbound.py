"""W1-6 验收：Env Attempt Server inbound 采集（design §9.4，评审 m9）。

覆盖 tasks.md W1-6 验收清单：
- 一次工具调用产生一条 http_exchange evidence 且 attempt 归属正确；
- 并发 attempts 不串 spool；
- phase 从 phase-state 快照；缺失/不匹配 → unknown，禁止默认 agent_run；
- unknown phase 不进入 agent_run 聚合（R3.6）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

import pytest

from backend.wire import env_capture, finalize, paths, spool, writer
from backend.wire.policy import resolve_effective_policy

ATT = "att_env1"
POLICY = resolve_effective_policy(task_requested="metadata")


@dataclass
class _SeededAttempt:
    id: str
    token: str


@pytest.fixture
def seeded_attempt(test_client) -> _SeededAttempt:
    """A real queued attempt against the `order-desk` env, so the two
    end-to-end tests below can hit the actual `/attempts/{id}/tools/...`
    route (not just the wire-capture helpers directly)."""
    import asyncio

    from backend import runtime_state
    from backend.db import insert_task
    from backend.runner import create_attempt

    state = runtime_state.get()
    task_id = "task_env_inbound"
    asyncio.run(
        insert_task(
            state.db_path,
            {
                "id": task_id,
                "env_name": "order-desk",
                "prompt": "buy a book",
                "context_json": "{}",
                "constraints_json": "{}",
                "timeout_seconds": 600,
                "source": "file",
                "created_at": "now",
            },
        )
    )
    model, token = asyncio.run(create_attempt(task_id, "claude-code"))
    return _SeededAttempt(id=model.id, token=token)


def _record(tmp_path, attempt_id=ATT, tool="build_formation", seq=0, **over):
    # 生产流程：begin（登记 in-flight）→ 快照 phase → record → end。
    enabled, phase = env_capture.snapshot_capture_state(tmp_path, attempt_id)
    entry = env_capture.begin_request(tmp_path, attempt_id, enabled)
    try:
        kw = dict(
            entry=entry, data_path=tmp_path, attempt_id=attempt_id, tool_name=tool,
            request_bytes=120, response_bytes=340, status_code=200,
            started_at="2026-07-13T00:00:00.000Z",
            finished_at="2026-07-13T00:00:00.050Z", duration_ms=50.0, seq=seq,
            phase=phase, capture_enabled=enabled,
        )
        kw.update(over)
        env_capture.record_inbound_tool_call(**kw)
    finally:
        env_capture.end_request(entry)


def _write_phase(tmp_path, attempt_id, phase, capture_enabled=True):
    writer.atomic_write_json(
        paths.phase_state_file(tmp_path, attempt_id),
        {"attempt_id": attempt_id, "phase": phase, "sequence": 1,
         "updated_at": "2026-07-13T00:00:00.000Z",
         "capture_enabled": capture_enabled, "policy": "metadata"},
    )


def _read_inbound(tmp_path, attempt_id=ATT):
    env_capture.close_attempt_spool(tmp_path, attempt_id)
    p = paths.source_spool_file(tmp_path, attempt_id, "env-inbound")
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()]


# ---------- 基本采集：一条 evidence + attempt 归属 ---------------------------

def test_single_call_one_evidence_correct_attribution(tmp_path):
    _write_phase(tmp_path, ATT, "agent_run")
    _record(tmp_path)
    recs = _read_inbound(tmp_path)
    assert len(recs) == 1
    e = recs[0]
    assert e["evidence_type"] == "http_exchange"
    assert e["attempt_id"] == ATT
    assert e["phase"] == "agent_run"
    assert e["payload"]["request_bytes"] == 120
    assert e["payload"]["response_bytes"] == 340
    assert e["payload"]["path"] == f"/attempts/{ATT}/tools/build_formation"
    assert e["payload"]["timing"]["duration_ms"] == 50.0
    assert e["source"]["kind"] == "env-inbound"


def test_evidence_id_dedup_by_seq(tmp_path):
    _write_phase(tmp_path, ATT, "agent_run")
    _record(tmp_path, seq=0)
    _record(tmp_path, seq=1)
    recs = _read_inbound(tmp_path)
    assert len({e["evidence_id"] for e in recs}) == 2  # 不同 seq → 不同 ID


# ---------- B2：policy off / 无 control claim → 零采集 ----------------------

def test_no_phase_state_means_no_capture(tmp_path):
    """评审 B2：无 phase-state（policy off / 无 wire prepare）→ 不采集，零落盘。"""
    _record(tmp_path)  # 未写 phase-state
    p = paths.source_spool_file(tmp_path, ATT, "env-inbound")
    assert not p.exists() and not p.with_name(p.name + ".partial").exists()


def test_capture_disabled_flag_means_no_capture(tmp_path):
    """phase-state 存在但 capture_enabled=False（policy off 的显式态）→ 不采集。"""
    _write_phase(tmp_path, ATT, "agent_run", capture_enabled=False)
    _record(tmp_path)
    assert not paths.source_spool_file(tmp_path, ATT, "env-inbound").exists()


# ---------- phase 快照：不匹配 → unknown（capture 已启用时）------------------

def test_attempt_mismatch_phase_state_yields_unknown(tmp_path):
    # phase-state capture_enabled 但属于别的 attempt → 采集但 phase=unknown
    writer.atomic_write_json(
        paths.phase_state_file(tmp_path, ATT),
        {"attempt_id": "att_other", "phase": "agent_run", "sequence": 1,
         "updated_at": "x", "capture_enabled": True, "policy": "metadata"},
    )
    _record(tmp_path)
    assert _read_inbound(tmp_path)[0]["phase"] == "unknown"


def test_verification_phase_snapshot(tmp_path):
    _write_phase(tmp_path, ATT, "verification")
    _record(tmp_path)
    assert _read_inbound(tmp_path)[0]["phase"] == "verification"


def test_phase_snapshotted_at_request_arrival(tmp_path):
    """评审 M3：phase 在请求到达时快照，工具执行中 phase 变化不改归属。"""
    _write_phase(tmp_path, ATT, "agent_run")
    enabled, phase = env_capture.snapshot_capture_state(tmp_path, ATT)
    entry = env_capture.begin_request(tmp_path, ATT, enabled)
    # 工具执行期间 lifecycle 切到 verification（模拟长耗时工具跨 phase）
    _write_phase(tmp_path, ATT, "verification")
    # 用 start 时快照的 phase 记录，而非结束时重读
    env_capture.record_inbound_tool_call(
        entry=entry, data_path=tmp_path, attempt_id=ATT, tool_name="slow",
        request_bytes=1, response_bytes=1, status_code=200, started_at="x",
        finished_at="y", duration_ms=999.0, seq=0, phase=phase, capture_enabled=enabled,
    )
    env_capture.end_request(entry)
    assert _read_inbound(tmp_path)[0]["phase"] == "agent_run"  # 到达时的 phase


def test_direction_is_inbound(tmp_path):
    """评审 M4：Env Server hop 的 direction 是 inbound，不被 canonical 反写。"""
    _write_phase(tmp_path, ATT, "agent_run")
    _record(tmp_path)
    e = _read_inbound(tmp_path)[0]
    assert e["payload"]["direction"] == "inbound"


def test_sealed_after_close_no_reopen(tmp_path):
    """评审 B1：close（seal）后 in-flight 请求结束再 record 不重开 spool，
    正式 .jsonl 不被移回 .partial。"""
    _write_phase(tmp_path, ATT, "agent_run")
    _record(tmp_path, seq=0)
    env_capture.close_attempt_spool(tmp_path, ATT)  # seal
    final = paths.source_spool_file(tmp_path, ATT, "env-inbound")
    assert final.exists()
    # finalize 后 in-flight 请求结束才到：被 sealed 挡住
    _record(tmp_path, seq=1)
    assert final.exists()  # 仍是 .jsonl
    assert not final.with_name(final.name + ".partial").exists()  # 没被移回
    # 内容仍只有 seal 前的 1 条
    recs = [json.loads(line) for line in final.read_text().splitlines()]
    assert len(recs) == 1


def test_drain_waits_for_active_and_records_it(tmp_path):
    """评审 R1：close 等待已 begin 的请求完成（不静默丢），其 evidence 进 spool。"""
    import threading

    _write_phase(tmp_path, ATT, "agent_run")
    enabled, phase = env_capture.snapshot_capture_state(tmp_path, ATT)
    entry = env_capture.begin_request(tmp_path, ATT, enabled)  # 请求已 begin
    started = threading.Event()
    done = threading.Event()

    def closer():
        started.set()
        env_capture.close_attempt_spool(tmp_path, ATT)  # 应 drain 等 end
        done.set()

    t = threading.Thread(target=closer)
    t.start()
    started.wait()
    import time
    time.sleep(0.1)  # 确认 close 在 drain 等待（还没完成）
    assert not done.is_set()
    # 请求现在完成：record + end
    env_capture.record_inbound_tool_call(
        entry=entry, data_path=tmp_path, attempt_id=ATT, tool_name="slow",
        request_bytes=1, response_bytes=1, status_code=200, started_at="x",
        finished_at="y", duration_ms=1.0, seq=0, phase=phase, capture_enabled=enabled,
    )
    env_capture.end_request(entry)
    t.join(timeout=3)
    assert done.is_set()
    recs = [json.loads(line) for line in
            paths.source_spool_file(tmp_path, ATT, "env-inbound").read_text().splitlines()]
    assert any(r["evidence_type"] == "http_exchange" for r in recs)  # active 请求被记录


async def test_async_close_does_not_block_request_coro(tmp_path):
    """评审 B1（本轮）：lifecycle 用 to_thread(close) 后 await——close 的 drain
    在线程池等待，请求协程仍能在事件循环上运行到 end_request，evidence 被记录
    而非超时丢弃。"""
    import asyncio

    from backend.wire.lifecycle import _to_thread

    _write_phase(tmp_path, ATT, "agent_run")
    enabled, phase = env_capture.snapshot_capture_state(tmp_path, ATT)
    entry = env_capture.begin_request(tmp_path, ATT, enabled)

    async def request_coro():
        await asyncio.sleep(0.03)  # 模拟工具执行 30ms
        env_capture.record_inbound_tool_call(
            entry=entry, data_path=tmp_path, attempt_id=ATT, tool_name="slow",
            request_bytes=1, response_bytes=1, status_code=200, started_at="x",
            finished_at="y", duration_ms=30.0, seq=0, phase=phase, capture_enabled=enabled,
        )
        env_capture.end_request(entry)

    # 并发：请求协程 + close（后者放线程池 drain）——两者都能推进
    await asyncio.gather(
        request_coro(),
        _to_thread(env_capture.close_attempt_spool, tmp_path, ATT),
    )
    recs = [json.loads(line) for line in
            paths.source_spool_file(tmp_path, ATT, "env-inbound").read_text().splitlines()]
    # 请求被记录（http_exchange），未因 drain 阻塞而超时丢弃
    assert any(r["evidence_type"] == "http_exchange" for r in recs)
    assert not any(r["evidence_type"] == "capture_event" and r["payload"]["event"] == "drop"
                   for r in recs)


def test_drain_timeout_counts_dropped(tmp_path):
    """评审 R1：drain 超时后仍有 in-flight → 计 dropped + 写 drop capture_event。"""
    _write_phase(tmp_path, ATT, "agent_run")
    enabled, _ = env_capture.snapshot_capture_state(tmp_path, ATT)
    entry = env_capture.begin_request(tmp_path, ATT, enabled)  # 永不 end
    env_capture.close_attempt_spool(tmp_path, ATT, drain_timeout=0.05)  # 超时
    recs = [json.loads(line) for line in
            paths.source_spool_file(tmp_path, ATT, "env-inbound").read_text().splitlines()]
    drop = [r for r in recs if r["evidence_type"] == "capture_event"
            and r["payload"]["event"] == "drop"]
    assert drop and drop[0]["payload"]["counters"]["records_dropped"] >= 1
    env_capture.end_request(entry)  # 清理


async def test_env_inbound_declared_zero_comm_vs_not_working(tmp_path):
    """评审 R2：env-inbound 进 declared sources + prepare 建空 spool——
    「零通信」（空 .jsonl，无请求）与「采集器没工作」（无 spool）可区分。"""
    from backend.wire.lifecycle import WireCaptureSession

    session = WireCaptureSession(
        attempt_id=ATT, data_path=tmp_path, agent_name="third-party-agent", sources=[],
    )
    await session.prepare()
    # prepare 后 env-inbound 空 spool 已存在（capture 启用）
    env_spool = paths.source_spool_file(tmp_path, ATT, "env-inbound")
    assert env_spool.exists()
    # 无任何 inbound 请求 → 零通信
    await session.attempt_end()
    manifest = json.loads(paths.manifest_file(tmp_path, ATT).read_text())
    env_src = next(s for s in manifest["sources"] if s["kind"] == "env-inbound")
    assert env_src["status"] == "complete"  # 零通信=complete（0 records）
    assert env_src["records"] == 0


def test_seq_recovers_after_restart_no_duplicate_ids(tmp_path):
    """评审 R3：重启后续接同一 attempt，序号从已落盘 max+1 恢复，evidence ID 不重复。"""
    from backend.env_attempt_server import _inbound_seq, _next_inbound_seq

    _write_phase(tmp_path, ATT, "agent_run")
    _record(tmp_path, seq=0)
    _record(tmp_path, seq=1)
    env_capture.close_attempt_spool(tmp_path, ATT)
    _inbound_seq.clear()
    env_capture._REGISTRY._sealed.clear()
    assert _next_inbound_seq(ATT, tmp_path) == 2


def test_sparse_seq_recovers_by_max_not_count(tmp_path):
    """评审 M2：稀疏序号（如 [0, 2]）恢复用 max(seq)+1，不数行——否则会复用 2。"""
    from backend.env_attempt_server import _inbound_seq, _next_inbound_seq

    _write_phase(tmp_path, ATT, "agent_run")
    _record(tmp_path, seq=0)
    _record(tmp_path, seq=2)  # 稀疏：跳过 1
    env_capture.close_attempt_spool(tmp_path, ATT)
    _inbound_seq.clear()
    env_capture._REGISTRY._sealed.clear()
    # 数行会得 2（撞已用的 2）；max+1 得 3
    assert _next_inbound_seq(ATT, tmp_path) == 3


def test_seq_stored_in_extensions_not_raw_ref(tmp_path):
    """评审 M1：seq 存 namespaced extensions，不造假 raw_ref.line provenance。"""
    _write_phase(tmp_path, ATT, "agent_run")
    _record(tmp_path, seq=5)
    e = _read_inbound(tmp_path)[0]
    assert e["extensions"]["x-lane.env-inbound-seq"] == 5
    assert e["raw_ref"]["line"] is None  # 不用行号冒充 seq


def test_legacy_spool_upgrade_no_duplicate_ids(tmp_path):
    """评审 M1：旧格式 spool（seq 不在 extensions）升级后重启续写，序号回退到
    http_exchange 计数——不从 0 重来撞旧 seq 0。"""
    from backend.env_attempt_server import _inbound_seq, _next_inbound_seq

    # 造旧格式 spool：8 条 http_exchange，无 x-lane.env-inbound-seq
    _write_phase(tmp_path, ATT, "agent_run")
    w = spool.SpoolWriter(
        paths.source_spool_file(tmp_path, ATT, "env-inbound"), expected_attempt_id=ATT,
    )
    for i in range(8):
        ev = _legacy_http_evidence(ATT, i)
        w.append(ev)
    w.close()
    _inbound_seq.clear()
    env_capture._REGISTRY._sealed.clear()
    # 旧格式无 extensions seq → 回退 http_count=8，下一个序号=8（不撞 0..7）
    assert _next_inbound_seq(ATT, tmp_path) == 8


def test_legacy_sparse_seq_no_id_collision_via_generation(tmp_path):
    """评审 M1：旧数据稀疏（[0,2]，seq 恢复会得 2）时，evidence ID 唯一性由
    进程 generation anchor 保证，新记录不与旧 ID 撞——不靠 seq 恢复的准确性。"""
    from backend.env_attempt_server import _inbound_seq, _next_inbound_seq

    _write_phase(tmp_path, ATT, "agent_run")
    # 旧格式稀疏 spool：evidence_id 用旧风格（不含本进程 generation）
    w = spool.SpoolWriter(
        paths.source_spool_file(tmp_path, ATT, "env-inbound"), expected_attempt_id=ATT,
    )
    for i in (0, 2):  # 稀疏：跳过 1
        w.append(_legacy_http_evidence(ATT, i))
    w.close()
    old_ids = {json.loads(line)["evidence_id"] for line in
               paths.source_spool_file(tmp_path, ATT, "env-inbound").read_text().splitlines()}
    # 重启续写
    _inbound_seq.clear()
    env_capture._REGISTRY._sealed.clear()
    seq = _next_inbound_seq(ATT, tmp_path)  # 恢复得 2（旧逻辑会撞）
    _record(tmp_path, seq=seq)
    env_capture.close_attempt_spool(tmp_path, ATT)  # rename .partial → .jsonl
    all_ids = [json.loads(line)["evidence_id"] for line in
               paths.source_spool_file(tmp_path, ATT, "env-inbound").read_text().splitlines()]
    # generation anchor 使新 ID 唯一，即便 seq=2 与旧 seq 2 相同
    assert len(all_ids) == len(set(all_ids))  # 全部不重复
    new_ids = [i for i in all_ids if i not in old_ids]
    assert len(new_ids) == 1 and set(new_ids).isdisjoint(old_ids)


def _legacy_http_evidence(attempt_id, i):
    from backend.wire import evidence as ev
    return {
        "evidence_id": f"we_legacy_{i}",
        "attempt_id": attempt_id,
        "phase": "agent_run",
        "evidence_type": "http_exchange",
        "source": {"kind": "env-inbound", "instance": "env-inbound"},
        "producer": {"name": "lane-env-server"},
        "time": {"observed_at": "2026-07-14T00:00:00.000Z"},
        "raw_ref": {"kind": "trace-jsonl", "file": "trace.jsonl", "line": None},
        "correlation_hints": {},
        "capabilities": {},
        "redaction": {"policy": "metadata", "status": "applied"},
        "errors": [],
        "extensions": {},  # 旧格式：无 seq
        "payload": {**ev.null_payload("http_exchange"), "direction": "inbound",
                    "method": "POST", "status_code": 200},
    }


def test_corrupt_phase_state_captures_as_degraded(tmp_path):
    """评审 M5：phase-state 损坏（非缺失）→ 采集但 phase=unknown（降级），
    不被静默当 policy off。"""
    paths.phase_state_file(tmp_path, ATT).parent.mkdir(parents=True, exist_ok=True)
    paths.phase_state_file(tmp_path, ATT).write_text("{ broken json")
    enabled, phase = env_capture.snapshot_capture_state(tmp_path, ATT)
    assert enabled is True and phase == "unknown"  # 故障≠off
    _record(tmp_path)
    assert _read_inbound(tmp_path)[0]["phase"] == "unknown"


# ---------- 并发 attempts 不串 spool ----------------------------------------

def test_concurrent_attempts_do_not_cross(tmp_path):
    _write_phase(tmp_path, "att_a", "agent_run")
    _write_phase(tmp_path, "att_b", "agent_run")

    def pump(aid, base):
        for i in range(30):
            _record(tmp_path, attempt_id=aid, seq=base + i)

    t1 = threading.Thread(target=pump, args=("att_a", 0))
    t2 = threading.Thread(target=pump, args=("att_b", 1000))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    a = _read_inbound(tmp_path, "att_a")
    b = _read_inbound(tmp_path, "att_b")
    assert len(a) == 30 and all(e["attempt_id"] == "att_a" for e in a)
    assert len(b) == 30 and all(e["attempt_id"] == "att_b" for e in b)


def test_concurrent_same_attempt_serialized(tmp_path):
    _write_phase(tmp_path, ATT, "agent_run")

    def pump(base):
        for i in range(25):
            _record(tmp_path, seq=base + i)

    threads = [threading.Thread(target=pump, args=(b,)) for b in (0, 100, 200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    recs = _read_inbound(tmp_path)
    assert len(recs) == 75  # 无丢行、无损坏（串行 append）
    # 全部合法 evidence
    from backend.wire.evidence import validate_evidence
    for r in recs:
        validate_evidence(r)


# ---------- canonical：direction=inbound + unknown → degraded ----------------

def _finalize_inbound(tmp_path):
    env_capture.close_attempt_spool(tmp_path, ATT)
    return finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT, policy=POLICY,
        declared_sources=[{"kind": "env-inbound", "instance": "env-inbound"}],
    )


def _wire_records(tmp_path):
    return [
        json.loads(line)
        for line in paths.wire_file(tmp_path, ATT).read_text().splitlines()
    ]


def test_canonical_hop_direction_inbound(tmp_path):
    """评审 M4：finalize 后 canonical http_exchange direction 仍是 inbound，
    不被硬编码成 outbound。"""
    _write_phase(tmp_path, ATT, "agent_run")
    _record(tmp_path)
    _finalize_inbound(tmp_path)
    hop = next(r for r in _wire_records(tmp_path) if r["record_type"] == "http_exchange")
    assert hop["data"]["direction"] == "inbound"
    # W1-9：source 产出的实测 duration 必须进入 canonical time envelope。
    assert isinstance(hop["time"]["duration_ms"], (int, float))
    assert hop["time"]["duration_ms"] >= 0


def test_unknown_phase_degrades_attribution(tmp_path):
    """评审 M5：unknown-phase evidence → manifest phase_attribution=degraded，
    finalizer 自动推导（不只信 lifecycle 参数）。"""
    # capture 启用但 attempt 不匹配 → phase=unknown
    writer.atomic_write_json(
        paths.phase_state_file(tmp_path, ATT),
        {"attempt_id": "att_other", "phase": "agent_run", "sequence": 1,
         "updated_at": "x", "capture_enabled": True, "policy": "metadata"},
    )
    _record(tmp_path)
    manifest = _finalize_inbound(tmp_path)
    assert manifest["phase_attribution"] == "degraded"
    assert any(g["field"] == "phase" for g in manifest["gaps"])
    assert manifest["status"] == "partial"


def test_unknown_hop_not_in_agent_run_aggregation(tmp_path):
    """unknown phase 的 hop 不进 agent_run 聚合（R3.6）。"""
    writer.atomic_write_json(
        paths.phase_state_file(tmp_path, ATT),
        {"attempt_id": "att_other", "phase": "agent_run", "sequence": 1,
         "updated_at": "x", "capture_enabled": True, "policy": "metadata"},
    )
    _record(tmp_path)
    _finalize_inbound(tmp_path)
    records = _wire_records(tmp_path)
    assert finalize.select_agent_run_calls(records) == []


# ---------- 端到端：真实路由产 evidence，不影响 trace -----------------------

def test_route_emits_evidence_and_keeps_trace(test_client, seeded_attempt):
    from backend import runtime_state

    data_path = runtime_state.get().data_path
    aid = seeded_attempt.id
    # capture 启用（写 control claim），route 调用后才会采集
    _write_phase(data_path, aid, "agent_run")

    resp = test_client.post(
        f"/attempts/{aid}/tools/catalog_search",
        headers={"Authorization": f"Bearer {seeded_attempt.token}"},
        json={"query": "algorithms"},
    )
    assert resp.status_code == 200  # 工具调用不受采集影响

    env_capture.close_attempt_spool(data_path, aid)
    inbound = paths.source_spool_file(data_path, aid, "env-inbound")
    assert inbound.exists()
    recs = [json.loads(line) for line in inbound.read_text().splitlines()]
    assert len(recs) == 1
    assert recs[0]["attempt_id"] == aid
    assert recs[0]["payload"]["status_code"] == 200
    assert recs[0]["payload"]["request_bytes"] > 0
    assert recs[0]["payload"]["direction"] == "inbound"
    assert recs[0]["phase"] == "agent_run"  # control claim 的 phase
    # trace 照常写入
    trace = data_path / "attempts" / aid / "trace.jsonl"
    assert trace.exists() and trace.read_text().strip()


def test_route_off_policy_no_capture(test_client, seeded_attempt):
    """评审 B2：无 control claim（policy off）→ route 调用不落任何 wire 盘。"""
    from backend import runtime_state

    data_path = runtime_state.get().data_path
    aid = seeded_attempt.id
    # 不写 phase-state
    resp = test_client.post(
        f"/attempts/{aid}/tools/catalog_search",
        headers={"Authorization": f"Bearer {seeded_attempt.token}"},
        json={"query": "algorithms"},
    )
    assert resp.status_code == 200
    env_capture.close_attempt_spool(data_path, aid)
    p = paths.source_spool_file(data_path, aid, "env-inbound")
    assert not p.exists() and not p.with_name(p.name + ".partial").exists()


async def test_lifecycle_closes_env_inbound_spool_before_finalize(tmp_path):
    """评审 B1：lifecycle attempt_end 在 finalize 前关闭 env-inbound spool，
    source 标 complete（非 partial），无需测试手工 close。"""
    from backend.wire.lifecycle import WireCaptureSession

    # 第三方 agent（无 native/injection source）policy=metadata 也建 capture context（B2）
    session2 = WireCaptureSession(
        attempt_id=ATT, data_path=tmp_path, agent_name="third-party-agent", sources=[],
    )
    await session2.prepare()
    # 模拟 Env Server 在 agent_run 期间采集
    enabled, phase = env_capture.snapshot_capture_state(tmp_path, ATT)
    assert enabled  # prepare 写了 capture_enabled 的 phase-state
    _record(tmp_path, seq=0, tool="t")
    async with session2.phase("agent_run"):
        pass
    await session2.attempt_end()  # 应在 finalize 前 close env-inbound spool
    # finalized 正常档存在，无残留 .partial
    p = paths.source_spool_file(tmp_path, ATT, "env-inbound")
    assert p.exists() and not p.with_name(p.name + ".partial").exists()
    manifest = json.loads(paths.manifest_file(tmp_path, ATT).read_text())
    env_src = next((s for s in manifest["sources"] if s["kind"] == "env-inbound"), None)
    assert env_src and env_src["status"] == "complete"  # 非 partial
