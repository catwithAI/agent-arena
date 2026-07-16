"""W3-1/W3-2 验收：MCP stdio tap 透明 pump + JSON-RPC 帧解析配对。

用真实子进程跑 mcp_tap 包一个 echo-JSON-RPC child，验证：
- 字节级透明转发（含大 payload、跨 chunk）；
- 正常退出 / SIGTERM / child crash 无孤儿；
- spool 逐行可读；
- 帧解析：request/response 按 id 配对、notification 无 id、超大帧 dropped 计数。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.wire import spool

_TAP = [sys.executable, "-m", "backend.wire.mcp_tap"]

# echo child：读一行 JSON-RPC，tools/call → 回一条同 id 的 result；notify → 不回。
_ECHO_CHILD = r'''
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    mid = msg.get("id")
    method = msg.get("method")
    if method == "tools/call" and mid is not None:
        size = msg.get("params", {}).get("arguments", {}).get("size", 10)
        resp = {"jsonrpc": "2.0", "id": mid, "result": {"content": "X" * size}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
'''


def _write_child(tmp_path: Path, body: str = _ECHO_CHILD) -> Path:
    p = tmp_path / "child.py"
    p.write_text(body)
    return p


def _tap_cmd(tmp_path: Path, child: Path, *, instance="travel", policy="metadata",
             extra=()) -> list[str]:
    spool_dir = tmp_path / "wire-sources"
    return [
        *_TAP, "--attempt-id", "att_mcp", "--phase", "agent_run",
        "--spool-dir", str(spool_dir), "--policy", policy, "--instance", instance,
        *extra, "--", sys.executable, str(child),
    ]


def _read_frames(tmp_path: Path, instance="travel") -> list[dict]:
    d = tmp_path / "wire-sources"
    f = d / f"mcp-stdio@{instance}.jsonl"
    if not f.exists():
        f = d / f"mcp-stdio@{instance}.jsonl.partial"
    if not f.exists():
        return []
    return spool.read_spool(f).records


def _req(mid, tool="flight_search", size=10):
    return json.dumps({
        "jsonrpc": "2.0", "id": mid, "method": "tools/call",
        "params": {"name": tool, "arguments": {"size": size}},
    }) + "\n"


# ---------- W3-1：透明转发 -------------------------------------------------

def test_byte_equivalent_forward_small(tmp_path):
    child = _write_child(tmp_path)
    p = subprocess.Popen(_tap_cmd(tmp_path, child),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = p.communicate(_req(1, size=10).encode(), timeout=15)
    resp = json.loads(out.decode().strip())
    assert resp["id"] == 1
    assert resp["result"]["content"] == "X" * 10
    assert p.returncode == 0


def test_byte_equivalent_large_payload(tmp_path):
    # 大 payload（跨多个 read chunk）字节级等价。
    child = _write_child(tmp_path)
    p = subprocess.Popen(_tap_cmd(tmp_path, child),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    big = 500_000
    out, _ = p.communicate(_req(1, size=big).encode(), timeout=30)
    resp = json.loads(out.decode().strip())
    assert len(resp["result"]["content"]) == big  # 无损


def test_multiple_calls_and_spool_readable(tmp_path):
    child = _write_child(tmp_path)
    p = subprocess.Popen(_tap_cmd(tmp_path, child),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    payload = (_req(1) + _req(2)).encode()
    out, _ = p.communicate(payload, timeout=15)
    lines = [ln for ln in out.decode().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert p.returncode == 0

    frames = _read_frames(tmp_path)
    reqs = [f for f in frames if f["payload"]["message_kind"] == "request"]
    resps = [f for f in frames if f["payload"]["message_kind"] == "response"]
    assert len(reqs) == 2 and len(resps) == 2


# ---------- W3-1：信号 / crash / 无孤儿 --------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# child 把自己的 pid 写到 --pidfile，然后 sleep（模拟不退出的 MCP server）。
_SLEEPER_CHILD = (
    "import sys, os, time\n"
    "open(sys.argv[sys.argv.index('--pidfile')+1],'w').write(str(os.getpid()))\n"
    "while True: time.sleep(0.05)\n"
)


def _spawn_tree_with_pidfile(tmp_path, *, new_session: bool):
    """spawn 一个 tap→sleeper-child 树，返回 (tap_proc, child_pid)。

    new_session=True 模拟 adapter 对 CLI 的 start_new_session spawn（tap 处在 CLI 组）。"""
    child = _write_child(tmp_path, _SLEEPER_CHILD)
    pidfile = tmp_path / "child.pid"
    cmd = [
        *_TAP, "--attempt-id", "att_mcp", "--phase", "agent_run",
        "--spool-dir", str(tmp_path / "wire-sources"), "--policy", "metadata",
        "--", sys.executable, str(child), "--pidfile", str(pidfile),
    ]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         start_new_session=new_session)
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.02)
    return p, int(pidfile.read_text().strip())


def test_sigterm_propagates_no_orphan(tmp_path):
    # SIGTERM tap → os.kill(child) → child 退出，无孤儿。
    p, child_pid = _spawn_tree_with_pidfile(tmp_path, new_session=False)
    assert _pid_alive(child_pid)
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=10)
    time.sleep(0.5)
    assert not _pid_alive(child_pid), "SIGTERM 后 MCP child 应已退出（无孤儿）"


def test_adapter_sigkill_killpg_no_mcp_orphan(tmp_path):
    """评审 #1 P0：adapter 超时对 CLI 进程组 SIGKILL，MCP child 不留孤儿。

    真实路径：adapter spawn CLI(start_new_session=True)，超时 killpg(CLI 组)。tap 与
    child 同在 CLI 组 → killpg 一并杀掉，child 不孤儿。（旧实现 child 独立 session 时
    killpg 够不着 → 孤儿。）"""
    p, child_pid = _spawn_tree_with_pidfile(tmp_path, new_session=True)
    assert _pid_alive(child_pid)
    # adapter kill_process_tree 的核心动作：killpg(CLI 进程组, SIGKILL)。
    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    p.wait(timeout=10)
    time.sleep(0.5)
    alive = _pid_alive(child_pid)
    if alive:
        os.kill(child_pid, signal.SIGKILL)  # cleanup
    assert not alive, "adapter SIGKILL(killpg CLI组) 后 MCP child 仍存活 = 孤儿"


def test_child_crash_surfaces_exit_code(tmp_path):
    # child 立即非零退出；tap 透传其 exit code，无挂起。
    child = _write_child(tmp_path, "import sys\nsys.exit(3)\n")
    p = subprocess.Popen(_tap_cmd(tmp_path, child),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = p.communicate(b"", timeout=10)
    assert p.returncode == 3
    # stop capture_event 记录了非零 exit code。
    frames = _read_frames(tmp_path)
    stops = [f for f in frames if f["evidence_type"] == "capture_event"
             and f["payload"]["event"] == "stop"]
    assert stops
    assert stops[0]["extensions"].get("x-lane.mcp-child-exit-code") == 3


# ---------- W3-2：帧解析与配对 ----------------------------------------------

def test_request_response_paired_by_id(tmp_path):
    child = _write_child(tmp_path)
    p = subprocess.Popen(_tap_cmd(tmp_path, child),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    p.communicate(_req(7, tool="hotel_search").encode(), timeout=15)

    frames = _read_frames(tmp_path)
    req = next(f for f in frames if f["payload"]["message_kind"] == "request")
    resp = next(f for f in frames if f["payload"]["message_kind"] == "response")
    # 同一 jsonrpc_id + 同一 paired-anchor。
    assert req["payload"]["jsonrpc_id"] == "7"
    assert resp["payload"]["jsonrpc_id"] == "7"
    assert req["extensions"]["x-lane.mcp-paired-anchor"] == \
        resp["extensions"]["x-lane.mcp-paired-anchor"]
    # request 提取 tool_name；direction 正确。
    assert req["payload"]["tool_name"] == "hotel_search"
    assert req["payload"]["direction"] == "client-to-server"
    assert resp["payload"]["direction"] == "server-to-client"
    assert resp["payload"]["is_error"] is False


def test_notification_has_no_id_not_mispaired(tmp_path):
    child = _write_child(tmp_path)
    p = subprocess.Popen(_tap_cmd(tmp_path, child),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    # notification（无 id）+ 一条正常 request。
    notify = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    p.communicate((notify + _req(1)).encode(), timeout=15)

    frames = _read_frames(tmp_path)
    notifs = [f for f in frames if f["payload"]["message_kind"] == "notification"]
    assert len(notifs) == 1
    assert notifs[0]["payload"]["jsonrpc_id"] is None
    # notification 不产生配对 anchor。
    assert "x-lane.mcp-paired-anchor" not in notifs[0]["extensions"]


def test_cross_chunk_single_frame_reassembled(tmp_path):
    # 一帧分多次 write（跨 chunk）仍重组成单帧。
    child = _write_child(tmp_path)
    p = subprocess.Popen(_tap_cmd(tmp_path, child),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    req = _req(1).encode()
    # 手动分片写 stdin。
    mid = len(req) // 2
    p.stdin.write(req[:mid])
    p.stdin.flush()
    time.sleep(0.1)
    p.stdin.write(req[mid:])
    p.stdin.flush()
    p.stdin.close()
    out = p.stdout.read()
    p.wait(timeout=15)
    assert json.loads(out.decode().strip())["id"] == 1

    frames = _read_frames(tmp_path)
    reqs = [f for f in frames if f["payload"]["message_kind"] == "request"]
    assert len(reqs) == 1  # 半帧没被误当两帧


def test_oversized_frame_dropped_counted(tmp_path):
    # max-frame-bytes 极小 → request 超限被 drop（但仍透明转发给 child）。
    child = _write_child(tmp_path)
    p = subprocess.Popen(
        _tap_cmd(tmp_path, child, extra=("--max-frame-bytes", "50")),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = p.communicate(_req(1, size=10).encode(), timeout=15)
    # 转发不受影响：child 仍回了 response。
    assert json.loads(out.decode().strip())["id"] == 1
    frames = _read_frames(tmp_path)
    stops = [f for f in frames if f["evidence_type"] == "capture_event"
             and f["payload"]["event"] == "stop"]
    # drop 计数进 stop 事件 counters。
    assert stops
    assert (stops[0]["payload"]["counters"] or {}).get("frames_dropped", 0) >= 1


def test_capture_failure_does_not_break_forward(tmp_path, monkeypatch):
    # spool 建不了（spool-dir 指向文件）→ tap 退化为纯透明 pump，转发照常。
    child = _write_child(tmp_path)
    bad = tmp_path / "not-a-dir"
    bad.write_text("x")  # spool-dir 是文件，FrameCapture 构造失败
    cmd = [
        *_TAP, "--attempt-id", "att_mcp", "--phase", "agent_run",
        "--spool-dir", str(bad / "sub"), "--policy", "metadata",
        "--", sys.executable, str(child),
    ]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = p.communicate(_req(1).encode(), timeout=15)
    # capture 挂了，转发仍成功。
    assert json.loads(out.decode().strip())["id"] == 1
    assert p.returncode == 0


# ---------- #5：capture 初始化失败写 unavailable marker -------------------

def test_capture_unavailable_marker_written(tmp_path):
    """评审 #5：capture 初始化失败 → 写 unavailable capture_event，让下游区分
    「没有 MCP 调用」与「采集器没工作」。"""
    from backend.wire.mcp_tap import _write_unavailable_marker

    spool_dir = tmp_path / "wire-sources"
    _write_unavailable_marker("att_mcp", "agent_run", spool_dir, "travel")
    frames = _read_frames(tmp_path)
    events = [f for f in frames if f["evidence_type"] == "capture_event"]
    assert len(events) == 1
    p = events[0]["payload"]
    assert p["event"] == "error"
    assert p["status"] == "unavailable"
    assert p["reason_code"] == "mcp_tap_capture_unavailable"


# ---------- #6：pending request TTL / 未配对淘汰申报 ----------------------

def test_unpaired_requests_evicted_reported_at_close(tmp_path):
    """评审 #6：close 时仍未配对的 request 计入 pending_requests_evicted，进 stop。"""
    from backend.wire import spool as _spool
    from backend.wire.mcp_frames import FrameCapture

    d = tmp_path / "ws"
    fc = FrameCapture(attempt_id="a", phase="agent_run", spool_dir=d,
                      instance="env", policy="metadata", max_frame_bytes=99999)
    c2s = fc.client_to_server()
    # 两个 request 都不回 response。
    c2s.feed(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x"}}\n')
    c2s.feed(b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"y"}}\n')
    fc.close_all()

    f = d / "mcp-stdio@env.jsonl"
    if not f.exists():
        f = d / "mcp-stdio@env.jsonl.partial"
    recs = _spool.read_spool(f).records
    stop = next(r for r in recs if r["evidence_type"] == "capture_event"
                and r["payload"]["event"] == "stop")
    assert stop["payload"]["counters"]["pending_requests_evicted"] == 2
    assert stop["payload"]["status"] == "partial"


def test_ttl_evicts_stale_pending(tmp_path, monkeypatch):
    """评审 #6：超 TTL 未配对的 request 在下一个 request 到来时被淘汰。"""
    from backend.wire import mcp_frames
    from backend.wire.mcp_frames import FrameCapture

    monkeypatch.setattr(mcp_frames, "_PENDING_TTL_S", 0.0)  # 立即过期
    fc = FrameCapture(attempt_id="a", phase="agent_run", spool_dir=tmp_path / "ws",
                      instance="env", policy="metadata", max_frame_bytes=99999)
    c2s = fc.client_to_server()
    c2s.feed(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x"}}\n')
    # 下一个 request 触发淘汰上一个（TTL=0 立即过期）。
    c2s.feed(b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"y"}}\n')
    # id=1 已被淘汰，其 response 现在配不上（pop 不到）。
    assert fc._pending_evicted >= 1


# ---------- W3-3：McpStdioSource command rewrite --------------------------

@pytest.mark.asyncio
async def test_mcp_source_produces_tap_rewrite(tmp_path):
    from backend.wire.lifecycle import CaptureContext
    from backend.wire.policy import resolve_effective_policy
    from backend.wire.sources.mcp_stdio import McpStdioSource

    src = McpStdioSource(attempt_id="att_1", env_name="travel-planner", data_path=tmp_path)
    ctx = CaptureContext(
        attempt_id="att_1", attempt_dir=tmp_path / "attempts" / "att_1",
        agent_name="claude-code", phase="agent_run",
        policy=resolve_effective_policy(task_requested="metadata"))
    inj = await src.start(ctx)

    # key 是 adapter 约定的 lane-<env_name>。
    assert list(inj.mcp_rewrites.keys()) == ["lane-travel-planner"]
    rw = inj.mcp_rewrites["lane-travel-planner"]
    # command 用 python 解释器，args_prefix 走 mcp_tap 且以 -- 结尾。
    assert rw.args_prefix[:2] == ("-m", "backend.wire.mcp_tap")
    assert rw.args_prefix[-1] == "--"
    assert "--attempt-id" in rw.args_prefix and "att_1" in rw.args_prefix

    # 模拟 adapter 应用 rewrite（[*args_prefix, orig_command, *orig_args]）后能真跑：
    child = _write_child(tmp_path)
    final = [rw.command, *rw.args_prefix, sys.executable, str(child)]
    p = subprocess.Popen(final, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = p.communicate(_req(1).encode(), timeout=15)
    assert json.loads(out.decode().strip())["id"] == 1  # 包装后工具行为等价


def test_mcp_source_kind_and_rewrites_transport():
    from backend.wire.sources.mcp_stdio import McpStdioSource
    src = McpStdioSource(attempt_id="a", env_name="e", data_path=Path("/d"))
    assert src.kind == "mcp-stdio"
    assert src.rewrites_transport is True
    assert src.server_key == "lane-e"


# ---------- W3-4：mcp_frame ↔ trajectory step 关联 -------------------------

def _write_mcp_frame_spool(tmp_path, instance="env", *, tool="mcp__env__plan"):
    """写一对 mcp_frame（request+response，tools/call tool）到 spool。"""
    from backend.wire import ids, paths, spool
    from backend.wire.evidence import (
        CorrelationHints, EvidenceProducer, EvidenceRawRef, EvidenceRedaction,
        EvidenceSource, EvidenceTime, McpFrameEvidence, McpFramePayload,
    )

    def _frame(seq, direction, kind, jid, tname, paired):
        return McpFrameEvidence(
            evidence_id=ids.evidence_id(
                attempt_id="att_mcp4", source_kind="mcp-stdio", source_instance=instance,
                raw_ref=f"mcp:{seq}", producer_id=direction),
            attempt_id="att_mcp4", phase="agent_run",
            source=EvidenceSource(kind="mcp-stdio", instance=instance),
            producer=EvidenceProducer(name="lane-mcp-tap", version="mcp-tap-v1"),
            time=EvidenceTime(observed_at="2026-07-14T00:00:0%d.000Z" % seq,
                              started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(kind="mcp-stdio", file="mcp-stdio.jsonl", line=None),
            correlation_hints=CorrelationHints(jsonrpc_id=jid),
            capabilities={}, redaction=EvidenceRedaction(policy="metadata", status="applied"),
            errors=[], extensions=({"x-lane.mcp-paired-anchor": paired} if paired else {}),
            payload=McpFramePayload(
                direction=direction, jsonrpc_id=jid, message_kind=kind,
                method="tools/call" if kind == "request" else None,
                tool_name=tname, bytes=100, is_error=(False if kind == "response" else None),
                truncated=False),
        )
    f = paths.source_spool_file(tmp_path, "att_mcp4", "mcp-stdio", instance)
    w = spool.SpoolWriter(f, expected_attempt_id="att_mcp4")
    w.append(_frame(1, "client-to-server", "request", "5", tool, f"{instance}:5"))
    w.append(_frame(2, "server-to-client", "response", "5", None, f"{instance}:5"))
    w.close()


def test_mcp_frame_associates_to_trajectory_step_by_tool_name(tmp_path):
    """W3-4：mcp_frame 的 tools/call request 按 tool name 关联到 trajectory step。

    用真实 ClaudeCodeNormalizer 产 trajectory（tool_use=mcp__env__plan），mcp_frame
    带同名 tools/call → finalize 后 frame.data.trajectory_step_id 指向该 step。"""
    import shutil
    from backend.wire import finalize, paths
    from backend.wire.normalizers.runner import run_native_normalizer
    from backend.wire.policy import resolve_effective_policy

    att = "att_mcp4"
    ad = paths.attempt_dir(tmp_path, att)
    ad.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).parent / "fixtures" / "wire" / "claude" / "events.jsonl"
    shutil.copy(fixture, ad / "events.jsonl")
    assert run_native_normalizer(agent_name="claude-code", attempt_id=att, data_path=tmp_path)
    # 真实命名（评审 #3）：trajectory 里工具是 mcp__env__plan，但 MCP tools/call
    # 的 params.name 是**裸** plan——归一后才能对齐。用裸名验证真实场景关联。
    _write_mcp_frame_spool(tmp_path, instance="env", tool="plan")

    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=att,
        policy=resolve_effective_policy(task_requested="metadata"),
        started_at="2026-07-14T00:00:00Z", finished_at="2026-07-14T00:00:01Z")

    wire = paths.wire_file(tmp_path, att)
    recs = [json.loads(ln) for ln in wire.read_text().splitlines()]
    frames = [r for r in recs if r["record_type"] == "mcp_frame"]
    req = next(f for f in frames if f["data"]["message_kind"] == "request")
    resp = next(f for f in frames if f["data"]["message_kind"] == "response")
    # request/response 已配对（W3-2）。
    assert req["data"]["paired_record_id"] == resp["record_id"]
    # request 按 tool name 关联到 trajectory step（W3-4），带 confidence。
    assert req["data"]["trajectory_step_id"] is not None
    assert req["data"]["association_confidence"] == "tool-name-order"
    # 关联到的 step 确实是 mcp__env__plan 的 tool_call step。
    traj = json.loads((ad / "trajectory.json").read_text())
    step = next(s for s in traj["steps"] if s["step_id"] == req["data"]["trajectory_step_id"])
    assert step["tool_name"] == "mcp__env__plan"


def test_mcp_frame_no_matching_step_stays_unassociated(tmp_path):
    """无同名 trajectory step → 不关联（confidence=None），不猜。"""
    import shutil
    from backend.wire import finalize, paths
    from backend.wire.normalizers.runner import run_native_normalizer
    from backend.wire.policy import resolve_effective_policy

    att = "att_mcp4"
    ad = paths.attempt_dir(tmp_path, att)
    ad.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).parent / "fixtures" / "wire" / "claude" / "events.jsonl"
    shutil.copy(fixture, ad / "events.jsonl")
    run_native_normalizer(agent_name="claude-code", attempt_id=att, data_path=tmp_path)
    # mcp frame 的工具名不在 trajectory 里。
    _write_mcp_frame_spool(tmp_path, instance="env", tool="nonexistent_tool")

    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=att,
        policy=resolve_effective_policy(task_requested="metadata"),
        started_at="2026-07-14T00:00:00Z", finished_at="2026-07-14T00:00:01Z")

    recs = [json.loads(ln) for ln in paths.wire_file(tmp_path, att).read_text().splitlines()]
    req = next(r for r in recs if r["record_type"] == "mcp_frame"
               and r["data"]["message_kind"] == "request")
    assert req["data"]["trajectory_step_id"] is None
    assert req["data"]["association_confidence"] is None


# ---------- 工具帧 ↔ provider 调用关联（B 层：codex 时间就近兜底）-----------

def test_orphan_mcp_frame_associates_to_nearest_provider_call():
    """无 lc 的工具帧按时间就近挂到最近的 provider 调用（codex 场景）。

    复刻真实交错：flight_search 帧发生在某次 /responses 前 ~200ms → 关联到那次调用。"""
    from backend.wire.finalize import _associate_orphan_mcp_calls

    recs = [
        {"record_type": "http_exchange", "data": {"path": "/v1/responses"},
         "time": {"timestamp": "2026-07-14T10:50:07.653Z"},
         "correlation": {"logical_call_id": "lc_A"}},
        {"record_type": "http_exchange", "data": {"path": "/v1/responses"},
         "time": {"timestamp": "2026-07-14T10:50:11.607Z"},
         "correlation": {"logical_call_id": "lc_B"}},
        {"record_type": "mcp_frame",
         "data": {"message_kind": "request", "tool_name": "flight_search"},
         "time": {"timestamp": "2026-07-14T10:50:07.426Z"}, "correlation": {}},
        {"record_type": "mcp_frame",
         "data": {"message_kind": "request", "tool_name": "flight_search"},
         "time": {"timestamp": "2026-07-14T10:50:11.405Z"}, "correlation": {}},
    ]
    _associate_orphan_mcp_calls(recs)
    assert recs[2]["correlation"]["logical_call_id"] == "lc_A"
    assert recs[3]["correlation"]["logical_call_id"] == "lc_B"
    # 就近推断标 time-proximity，与 explicit 区分。
    assert recs[2]["data"]["association_confidence"] == "time-proximity"


def test_orphan_env_inbound_tool_hop_also_associates():
    """env-inbound 的工具回调 http_exchange（/tools/...）也按时间就近挂到 provider
    调用——它是同一次工具调用的另一面，不应单独成一条 unmatched 泳道。"""
    from backend.wire.finalize import _associate_orphan_mcp_calls

    recs = [
        {"record_type": "http_exchange", "data": {"path": "/v1/responses"},
         "time": {"timestamp": "2026-07-14T10:50:11.607Z"},
         "correlation": {"logical_call_id": "lc_call"}},
        {"record_type": "http_exchange",
         "data": {"path": "/attempts/att_x/tools/flight_search"},
         "time": {"timestamp": "2026-07-14T10:50:11.400Z"},
         "correlation": {"confidence": "unmatched"}},
    ]
    _associate_orphan_mcp_calls(recs)
    assert recs[1]["correlation"]["logical_call_id"] == "lc_call"
    assert recs[1]["data"]["association_confidence"] == "time-proximity"


def test_orphan_assoc_never_overwrites_existing_lc():
    """已有 lc 的工具帧（精确关联）绝不被时间就近覆盖。"""
    from backend.wire.finalize import _associate_orphan_mcp_calls

    recs = [
        {"record_type": "http_exchange", "data": {"path": "/v1/responses"},
         "time": {"timestamp": "2026-07-14T10:50:07.653Z"},
         "correlation": {"logical_call_id": "lc_near"}},
        {"record_type": "mcp_frame",
         "data": {"message_kind": "request", "tool_name": "plan",
                  "association_confidence": "tool-name-order"},
         "time": {"timestamp": "2026-07-14T10:50:07.400Z"},
         "correlation": {"logical_call_id": "lc_precise"}},
    ]
    _associate_orphan_mcp_calls(recs)
    assert recs[1]["correlation"]["logical_call_id"] == "lc_precise"  # 不被覆盖
    assert recs[1]["data"]["association_confidence"] == "tool-name-order"  # 保留精确 confidence


def test_orphan_assoc_noop_without_provider_anchor():
    """无任何 provider 调用锚点 → 什么都不做，工具帧 lc 保持 None。"""
    from backend.wire.finalize import _associate_orphan_mcp_calls

    recs = [
        {"record_type": "mcp_frame",
         "data": {"message_kind": "request", "tool_name": "flight_search"},
         "time": {"timestamp": "2026-07-14T10:50:07.426Z"}, "correlation": {}},
    ]
    _associate_orphan_mcp_calls(recs)
    assert recs[0]["correlation"].get("logical_call_id") is None


# ---------- #4：双向 RPC 同 id 配对不互相覆盖 -----------------------------

def test_bidirectional_same_id_not_mispaired(tmp_path):
    """评审 #4：MCP 双向 RPC——client 与 server 都发 id=1 的 request，各自的
    response（相反方向）分别配对，不互相覆盖。"""
    from backend.wire import spool as _spool
    from backend.wire.mcp_frames import FrameCapture

    d = tmp_path / "ws"
    fc = FrameCapture(attempt_id="a", phase="agent_run", spool_dir=d,
                      instance="env", policy="metadata", max_frame_bytes=99999)
    c2s = fc.client_to_server()
    s2c = fc.server_to_client()
    c2s.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "a"}}).encode() + b"\n")
    s2c.feed(json.dumps({"jsonrpc": "2.0", "id": 1,
                         "method": "sampling/createMessage"}).encode() + b"\n")
    s2c.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"x": 1}}).encode() + b"\n")
    c2s.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"y": 2}}).encode() + b"\n")
    fc.close_all()

    f = d / "mcp-stdio@env.jsonl"
    if not f.exists():
        f = d / "mcp-stdio@env.jsonl.partial"
    frames = [r for r in _spool.read_spool(f).records if r["evidence_type"] == "mcp_frame"]
    reqs = [r for r in frames if r["payload"]["message_kind"] == "request"]
    resps = [r for r in frames if r["payload"]["message_kind"] == "response"]
    assert len(reqs) == 2 and len(resps) == 2
    # 两 request anchor 不同（方向区分，不互相覆盖）。
    req_anchors = {r["extensions"]["x-lane.mcp-paired-anchor"] for r in reqs}
    assert len(req_anchors) == 2
    for resp in resps:
        assert resp["extensions"]["x-lane.mcp-paired-anchor"] in req_anchors


def test_numeric_and_string_id_distinct(tmp_path):
    """评审 #4：数字 id 1 与字符串 id '1' 是不同 JSON-RPC id，不撞键。"""
    from backend.wire import spool as _spool
    from backend.wire.mcp_frames import FrameCapture

    d = tmp_path / "ws2"
    fc = FrameCapture(attempt_id="a", phase="agent_run", spool_dir=d,
                      instance="env", policy="metadata", max_frame_bytes=99999)
    c2s = fc.client_to_server()
    c2s.feed(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"n"}}\n')
    c2s.feed(b'{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"s"}}\n')
    fc.close_all()

    f = d / "mcp-stdio@env.jsonl"
    if not f.exists():
        f = d / "mcp-stdio@env.jsonl.partial"
    frames = [r for r in _spool.read_spool(f).records if r["evidence_type"] == "mcp_frame"]
    reqs = [r for r in frames if r["payload"]["message_kind"] == "request"]
    # 数字 1 ≠ 字符串 "1" → anchor 不同。
    assert len({r["extensions"]["x-lane.mcp-paired-anchor"] for r in reqs}) == 2


# ---------- 回归：相对 spool-dir × tap 子进程非默认 CWD -----------------------

def test_tap_rewrite_spool_dir_is_absolute():
    """回归：_tap_rewrite 传给 tap 的 --spool-dir 必须绝对。

    tap 是 adapter（codex/CC）以 cwd=attempt_dir 拉起的独立子进程；若传相对
    spool-dir，会被 tap 的 CWD 二次解析成嵌套路径，finalize 扫不到 → mcp_frame 恒 0。
    这里用**相对** data_path 构造 source，断言产出的 --spool-dir 已是绝对路径。
    """
    from backend.wire.lifecycle import CaptureContext
    from backend.wire.policy import resolve_effective_policy
    from backend.wire.sources.mcp_stdio import McpStdioSource

    src = McpStdioSource(
        attempt_id="att_x", env_name="travel-planner", data_path=Path("./data"))
    ctx = CaptureContext(
        attempt_id="att_x", attempt_dir=Path("./data/attempts/att_x"),
        agent_name="codex", phase="agent_run",
        policy=resolve_effective_policy(task_requested="metadata"))
    rewrite = src._tap_rewrite(ctx)
    args = rewrite.args_prefix
    spool_dir = args[args.index("--spool-dir") + 1]
    assert Path(spool_dir).is_absolute(), f"--spool-dir 必须绝对，实为 {spool_dir!r}"
    # 且指向正确的（未嵌套的）wire-sources。
    assert spool_dir.endswith("attempts/att_x/wire-sources")


def test_tap_from_nondefault_cwd_writes_to_absolute_spool(tmp_path):
    """端到端：tap 以非默认 CWD 启动 + 绝对 spool-dir → spool 落在期望绝对路径，
    不产生嵌套目录。复现 adapter（cwd=attempt_dir）拉起 tap 的真实场景。"""
    child = _write_child(tmp_path)
    spool_dir = (tmp_path / "wire-sources").resolve()  # 绝对
    workdir = tmp_path / "attempt_cwd"
    workdir.mkdir()
    cmd = [
        *_TAP, "--attempt-id", "att_cwd", "--phase", "agent_run",
        "--spool-dir", str(spool_dir), "--policy", "metadata", "--instance", "travel",
        "--", sys.executable, str(child),
    ]
    # 关键：子进程 CWD 设成一个非默认目录（模拟 codex 的 -C attempt_dir）。
    p = subprocess.Popen(cmd, cwd=str(workdir),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    p.communicate(_req(1, size=10).encode(), timeout=15)
    # spool 落在绝对 spool_dir，且 workdir 下没有嵌套的 wire-sources。
    assert (spool_dir / "mcp-stdio@travel.jsonl").exists() or \
        (spool_dir / "mcp-stdio@travel.jsonl.partial").exists()
    assert not (workdir / "wire-sources").exists(), "不应在 tap CWD 下产生嵌套 spool"
