"""W4-1/W4-3：agent-arena reverse HTTP capture proxy 转发正确性 + timing/blob。

用 httpx.MockTransport 做 in-process fake upstream，直接驱动 http_proxy.forward()：
- 流式/非流式转发透明（status/body 原样透传）；
- SSE 逐 chunk 转发不缓冲整包（慢 fake 断言首 chunk 早于末 chunk）；
- upstream 不可达 → 502 + partial hop；
- 队列满 drop capture chunk 计数进 evidence，主通信不阻塞；
- inbound 凭证不转发，upstream auth 由 provider 注入；
- metadata 档不落 body，full 档 blob 可回读；
- client 断开级联取消 + partial。

SSRF 拒绝 / capture token 授权在路由层，见 test_wire_proxy_route.py。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.model_providers import ModelProviderSection
from backend.wire import paths, spool
from backend.wire.sources import http_proxy

pytestmark = pytest.mark.asyncio

ATT = "att_proxy"


def _provider(**over) -> ModelProviderSection:
    d = dict(kind="anthropic", base_url="https://upstream.test/v1",
             api_key_env=None, auth_mode="bearer")
    d.update(over)
    return ModelProviderSection(**d)


def _write_phase_state(tmp_path, *, policy="metadata", phase="agent_run", enabled=True):
    p = paths.phase_state_file(tmp_path, ATT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "attempt_id": ATT, "phase": phase, "capture_enabled": enabled, "policy": policy,
    }), encoding="utf-8")


def _read_spool(tmp_path):
    f = paths.source_spool_file(tmp_path, ATT, http_proxy.SOURCE_KIND)
    res = spool.read_spool(f)
    return res.records


def _json_response(status: int, obj) -> httpx.Response:
    """构造一个**流式** JSON response——MockTransport 对 json=/content= 会 eager
    materialize，导致 aiter_raw() StreamConsumed（真实 socket 上游不会）。用
    stream= 让它保持未读，模拟真实上游的流式 body。"""
    raw = json.dumps(obj).encode()

    async def _one():
        yield raw

    return httpx.Response(status, stream=_AsyncByteStream(_one()),
                          headers={"content-type": "application/json"})


async def _drain(body) -> bytes:
    out = b""
    async for chunk in body:
        out += chunk
    return out


@pytest.fixture(autouse=True)
def _reset_registry():
    http_proxy.reset()
    yield
    http_proxy.reset()


# ---------- 非流式透明转发 ---------------------------------------------------

async def test_non_streaming_forward_transparent(tmp_path):
    _write_phase_state(tmp_path, policy="metadata")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        return _json_response(200, {"ok": True, "echo": request.content.decode()})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="",
        headers={"content-type": "application/json"}, body=b'{"model":"m"}',
        phase="agent_run", policy="metadata", client=client,
    )
    assert result.status_code == 200
    body = json.loads(await _drain(result.body))
    assert body["ok"] is True and body["echo"] == '{"model":"m"}'
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    records = _read_spool(tmp_path)
    hops = [r for r in records if r["evidence_type"] == "http_exchange"]
    assert len(hops) == 1
    p = hops[0]["payload"]
    assert p["direction"] == "outbound"
    assert p["status_code"] == 200
    assert p["request_bytes"] == len(b'{"model":"m"}')
    assert p["response_bytes"] > 0
    assert p["partial"] is False


# ---------- SSE 逐 chunk 转发（不缓冲整包）----------------------------------

async def test_sse_streamed_chunk_by_chunk(tmp_path):
    _write_phase_state(tmp_path, policy="metadata")
    arrival: list[float] = []

    async def slow_stream():
        for i in range(3):
            await asyncio.sleep(0.05)
            yield f"data: chunk{i}\n\n".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(slow_stream()),
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="metadata", client=client,
    )
    loop = asyncio.get_event_loop()
    collected = []
    async for chunk in result.body:
        arrival.append(loop.time())
        collected.append(chunk)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    assert len(collected) == 3
    # 逐 chunk：首 chunk 明显早于末 chunk（未缓冲整包）。
    assert arrival[-1] - arrival[0] >= 0.08

    records = _read_spool(tmp_path)
    chunks = [r for r in records if r["evidence_type"] == "stream_chunk"]
    # 3 个 data chunk + 1 个 terminal
    assert len(chunks) == 4
    seqs = [c["payload"]["sequence"] for c in chunks]
    assert seqs == [0, 1, 2, 3]
    assert chunks[-1]["payload"]["terminal"] is True
    # timing 单调递增
    rels = [c["payload"]["relative_ms"] for c in chunks[:3]]
    assert rels[0] < rels[1] < rels[2]
    hop = [r for r in records if r["evidence_type"] == "http_exchange"][0]
    assert hop["payload"]["streamed"] is True
    assert hop["payload"]["timing"]["ttft_ms"] is not None


# ---------- upstream 不可达 → 502 + partial ---------------------------------

async def test_upstream_unreachable_502_partial(tmp_path):
    _write_phase_state(tmp_path, policy="metadata")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(http_proxy.ProxyError) as ei:
        await http_proxy.forward(
            data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
            method="POST", path="messages", query_string="", headers={}, body=b"{}",
            phase="agent_run", policy="metadata", client=client,
        )
    assert ei.value.status_code == 502
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hops = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"]
    assert len(hops) == 1
    assert hops[0]["payload"]["partial"] is True
    assert hops[0]["payload"]["status_code"] is None


# ---------- upstream 429 原样返回且记一条 hop（W4-3）------------------------

async def test_upstream_429_passthrough_single_hop(tmp_path):
    _write_phase_state(tmp_path, policy="metadata")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(429, {"error": "rate_limited"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="metadata", client=client,
    )
    assert result.status_code == 429  # 原样透传
    await _drain(result.body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hops = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"]
    assert len(hops) == 1
    assert hops[0]["payload"]["status_code"] == 429


# ---------- inbound 凭证不转发；provider auth 注入 --------------------------

async def test_inbound_credentials_not_forwarded_provider_auth_injected(tmp_path, monkeypatch):
    _write_phase_state(tmp_path, policy="metadata")
    monkeypatch.setenv("UP_KEY", "sk-upstream")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["xapi"] = request.headers.get("x-api-key")
        return _json_response(200, {})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up",
        provider_cfg=_provider(api_key_env="UP_KEY", auth_mode="bearer"),
        method="POST", path="messages", query_string="",
        headers={"authorization": "Bearer INBOUND-SECRET", "x-api-key": "inbound-key"},
        body=b"{}", phase="agent_run", policy="metadata", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    # inbound 凭证被剥离；upstream 拿到的是 provider 注入的 key。
    assert seen["auth"] == "Bearer sk-upstream"
    assert seen["xapi"] is None


async def test_provider_api_key_auth_mode_injects_x_api_key(tmp_path, monkeypatch):
    _write_phase_state(tmp_path, policy="metadata")
    monkeypatch.setenv("UP_KEY", "sk-upstream")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["xapi"] = request.headers.get("x-api-key")
        return _json_response(200, {})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up",
        provider_cfg=_provider(api_key_env="UP_KEY", auth_mode="api-key"),
        method="POST", path="messages", query_string="",
        headers={"authorization": "Bearer INBOUND"}, body=b"{}",
        phase="agent_run", policy="metadata", client=client,
    )).body)
    await client.aclose()

    assert seen["xapi"] == "sk-upstream"
    assert seen["auth"] is None


# ---------- 队列满 drop capture chunk，主通信不阻塞（W4-1）------------------

async def test_queue_full_drops_capture_not_forwarding(tmp_path, monkeypatch):
    _write_phase_state(tmp_path, policy="metadata")
    # 队列缩到极小；用 gate 卡住 writer task（await 一个直到转发完才放行的 Event），
    # 转发期间 writer drain 不了队列 → 队列填满 → 后续 chunk 走 drop 分支。
    # 单线程事件循环下用 await（非 busy-wait）阻塞 writer，不拖住转发协程。
    monkeypatch.setattr(http_proxy, "_CHUNK_QUEUE_MAXSIZE", 2)
    gate = asyncio.Event()
    orig_writer = http_proxy._chunk_writer

    async def gated_writer(queue, hop, entry):
        # 队列填满前不 drain：等 gate（转发侧填满队列后放行）。用 await 而非
        # busy-wait，避免拖住单线程事件循环上的转发协程。
        await gate.wait()
        await orig_writer(queue, hop, entry)

    monkeypatch.setattr(http_proxy, "_chunk_writer", gated_writer)

    n_chunks = 50

    async def fast_stream():
        for i in range(n_chunks):
            yield f"c{i}".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(fast_stream()))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="metadata", client=client,
    )
    # 主通信：客户端仍拿到全部 chunk（转发不因 capture 队列满而阻塞/丢）。
    # 转发到一半就放行 writer，让它在 body finally 的 drain 窗口里跑完——
    # 此时队列早已填满、drop 已计数。
    collected = []
    async for chunk in result.body:
        collected.append(chunk)
        if len(collected) == 10:
            gate.set()
    gate.set()  # 兜底：不足 10 条也放行
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    assert len(collected) == n_chunks  # 转发无损
    records = _read_spool(tmp_path)
    hop = [r for r in records if r["evidence_type"] == "http_exchange"][0]
    # response_bytes 覆盖全部转发字节（转发计数不受 capture drop 影响）。
    assert hop["payload"]["response_bytes"] == sum(len(f"c{i}".encode()) for i in range(n_chunks))
    # 队列满导致部分 capture chunk 被 drop，计数进 hop extensions（不静默）。
    assert hop["extensions"].get("x-lane.dropped-chunks", 0) > 0


# ---------- body policy：metadata 不落 body，full 落 blob 可回读 -----------

async def test_metadata_policy_no_body_blob(tmp_path):
    _write_phase_state(tmp_path, policy="metadata")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"secret": "value"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="",
        headers={}, body=b'{"model":"m","messages":[]}',
        phase="agent_run", policy="metadata", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    ext = hop["extensions"]
    assert "x-lane.request-body-ref" not in ext
    assert "x-lane.response-body-ref" not in ext
    # metadata 档没有 blob 目录 / 文件
    assert not paths.blobs_dir(tmp_path, ATT).exists()


async def test_full_policy_writes_readable_blob(tmp_path):
    _write_phase_state(tmp_path, policy="full")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"content": [{"type": "text", "text": "hi"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="",
        headers={}, body=b'{"model":"m","messages":[{"role":"user","content":"hi"}]}',
        phase="agent_run", policy="full", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    ext = hop["extensions"]
    assert "x-lane.request-body-ref" in ext
    assert "x-lane.response-body-ref" in ext
    # blob 可回读
    from backend.wire.writer import BlobWriter
    bw = BlobWriter(tmp_path, ATT)
    req_body = json.loads(bw.read_bytes(ext["x-lane.request-body-ref"]))
    assert req_body["model"] == "m"


# ---------- client 断开级联取消 + partial ----------------------------------

async def test_client_disconnect_cancels_and_partial(tmp_path):
    _write_phase_state(tmp_path, policy="metadata")

    async def endless():
        for i in range(1000):
            await asyncio.sleep(0.01)
            yield f"c{i}".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(endless()))

    disconnected = {"v": False}

    async def is_disc():
        return disconnected["v"]

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="metadata", client=client, is_disconnected=is_disc,
    )
    got = 0
    async for _chunk in result.body:
        got += 1
        if got == 3:
            disconnected["v"] = True  # 模拟客户端断开
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    assert hop["payload"]["partial"] is True
    # 已观测 chunk 保留
    chunks = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "stream_chunk"]
    assert len(chunks) >= 1


# ---------- B1：semantic summary 真正落进 http_exchange evidence -------------

async def test_request_summary_persisted_in_evidence(tmp_path):
    _write_phase_state(tmp_path, policy="metadata")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"content": [{"type": "text", "text": "hi"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    body = json.dumps({"model": "claude", "system": "sys",
                       "messages": [{"role": "user", "content": "hello"}]}).encode()
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=body,
        phase="agent_run", policy="metadata", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    rs = hop["payload"]["request_summary"]
    assert rs is not None
    assert rs["model"] == "claude"
    # request 总能解析（request body 恒可得，不依赖 body-buffer policy）→ hash 落盘。
    assert rs["messages_hash"] is not None
    assert rs["hash_domain"] == "lane-semantic-jcs-nfc-v1"


async def test_response_summary_persisted_under_full_policy(tmp_path):
    # response summary 需要缓存 response body（仅 parsed/full 档）；metadata 档
    # 不缓存 body，故无 response semantic hash（符合 policy 门控）。
    _write_phase_state(tmp_path, policy="full")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"content": [{"type": "text", "text": "hi"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="",
        headers={}, body=b'{"model":"m","messages":[{"role":"user","content":"hi"}]}',
        phase="agent_run", policy="full", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    resp = hop["payload"]["response_summary"]
    assert resp is not None and resp["content_hash"] is not None
    assert resp["hash_domain"] == "lane-semantic-jcs-nfc-v1"


# ---------- M1：path `..` 逃逸被拒（SSRF）-----------------------------------

async def test_path_traversal_rejected(tmp_path):
    _write_phase_state(tmp_path, policy="metadata")
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return _json_response(200, {})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(http_proxy.ProxyError) as ei:
        await http_proxy.forward(
            data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
            method="POST", path="../../admin", query_string="", headers={}, body=b"{}",
            phase="agent_run", policy="metadata", client=client,
        )
    assert ei.value.status_code == 400
    assert called["n"] == 0  # upstream 从未被打到
    await client.aclose()


# ---------- M3：full 档非 JSON body（SSE）也脱敏后落盘 ----------------------

async def test_full_policy_non_json_body_redacted(tmp_path):
    _write_phase_state(tmp_path, policy="full")
    # SSE 响应里回显一个像 secret 的串——scrub 规则应把它 [REDACTED]。
    secret_line = "data: {\"key\": \"sk-ant-api03-SECRETSECRETSECRET\"}\n\n"

    async def sse():
        yield secret_line.encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(sse()),
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="full", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    ref = hop["extensions"].get("x-lane.response-body-ref")
    assert ref is not None
    from backend.wire.writer import BlobWriter
    stored = BlobWriter(tmp_path, ATT).read_bytes(ref).decode("utf-8")
    # 原始 secret 不落盘，被 scrub 成 [REDACTED]
    assert "SECRETSECRETSECRET" not in stored
    assert "REDACTED" in stored


# ---------- P0-1：lane 内部头/capture token 不转发 upstream --------------

async def test_internal_headers_not_forwarded_to_upstream(tmp_path):
    _write_phase_state(tmp_path, policy="metadata")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["keys"] = sorted(k.lower() for k in request.headers.keys())
        return _json_response(200, {})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="",
        headers={
            "x-lane-capture-token": "SECRET-TOKEN",
            "x-lane-attempt-id": "att-internal",
            "x-eval-session-id": "sess-internal",
            "x-lane-future-header": "whatever",  # 前缀剥离覆盖将来新增头
            "content-type": "application/json",
        },
        body=b"{}", phase="agent_run", policy="metadata", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    # 任何 x-lane-* / x-eval-* / capture token 都不进 upstream。
    assert not any(k.startswith("x-lane-") for k in seen["keys"])
    assert not any(k.startswith("x-eval-") for k in seen["keys"])
    assert "content-type" in seen["keys"]  # 正常业务头仍透传


# ---------- P0-2：SSE data JSON 字段级 secret 脱敏 --------------------------

async def test_sse_field_level_secret_redacted(tmp_path):
    _write_phase_state(tmp_path, policy="full")
    # 字段级 secret：不是 sk-... 形态，只有解析 JSON 才能脱敏。
    line = 'data: {"api_key":"abcdefghijk","cookie":"sessionvalue","text":"hi"}\n\n'

    async def sse():
        yield line.encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(sse()),
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="full", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    ref = hop["extensions"]["x-lane.response-body-ref"]
    from backend.wire.writer import BlobWriter
    stored = BlobWriter(tmp_path, ATT).read_bytes(ref).decode("utf-8")
    assert "abcdefghijk" not in stored     # api_key 值被脱敏
    assert "sessionvalue" not in stored    # cookie 值被脱敏
    assert "REDACTED" in stored
    assert "hi" in stored                  # 非敏感字段保留


# ---------- #4：stream_chunk 与 http_exchange hop_id 一致（可挂载）---------

async def test_stream_chunk_hop_id_matches_http_exchange(tmp_path):
    from backend.wire import finalize
    from backend.wire.policy import resolve_effective_policy

    _write_phase_state(tmp_path, policy="metadata")

    async def sse():
        for i in range(3):
            yield f"data: c{i}\n\n".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(sse()),
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="metadata", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    # finalize 后 canonical：stream_chunk.hop_id 必须等于对应 http_exchange.hop_id。
    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT,
        policy=resolve_effective_policy(task_requested="metadata"),
        started_at="2026-07-14T00:00:00Z", finished_at="2026-07-14T00:00:01Z",
    )
    wire = paths.wire_file(tmp_path, ATT)
    records = [json.loads(ln) for ln in wire.read_text().splitlines()]
    hop_ids = {r["data"]["hop_id"] for r in records if r["record_type"] == "http_exchange"}
    chunk_hop_ids = {r["data"]["hop_id"] for r in records if r["record_type"] == "stream_chunk"}
    assert len(hop_ids) == 1
    assert chunk_hop_ids == hop_ids  # chunk 全部挂到同一 http_exchange hop


# ---------- #5：禁上游压缩（Accept-Encoding: identity）---------------------

async def test_accept_encoding_identity_sent_upstream(tmp_path):
    _write_phase_state(tmp_path, policy="metadata")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ae"] = request.headers.get("accept-encoding")
        return _json_response(200, {})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="",
        headers={"accept-encoding": "gzip, br"},  # 客户端要压缩也被覆盖
        body=b"{}", phase="agent_run", policy="metadata", client=client,
    )).body)
    await client.aclose()
    assert seen["ae"] == "identity"


# ---------- #6：request 超采集上限标 truncated，转发不受影响 ---------------

async def test_oversized_request_marked_truncated(tmp_path, monkeypatch):
    _write_phase_state(tmp_path, policy="full")
    monkeypatch.setattr(http_proxy, "_MAX_CAPTURE_REQUEST_BYTES", 100)
    got = {}

    def handler(request: httpx.Request) -> httpx.Response:
        got["len"] = len(request.content)
        return _json_response(200, {})

    big = b'{"model":"m","x":"' + b"A" * 500 + b'"}'
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=big,
        phase="agent_run", policy="full", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    assert got["len"] == len(big)  # 转发完整，不受采集上限影响
    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    assert hop["extensions"].get("x-lane.request-body-truncated") is True
    # 超限不解析/不落 blob
    assert "x-lane.request-body-ref" not in hop["extensions"]
    assert hop["payload"]["request_summary"] is None


# ---------- #7：response 脱敏失败时 evidence redaction=failed（不沿用 request）

async def test_response_redaction_failure_reflected(tmp_path, monkeypatch):
    _write_phase_state(tmp_path, policy="full")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"content": [{"type": "text", "text": "hi"}]})

    # 让 response blob 脱敏失败（request 侧成功）。
    orig = http_proxy._maybe_write_body_blob
    calls = {"n": 0}

    def flaky_blob(raw, policy, data_path, attempt_id):
        calls["n"] += 1
        if calls["n"] == 2:  # 第二次是 response
            return None, "failed"
        return orig(raw, policy, data_path, attempt_id)

    monkeypatch.setattr(http_proxy, "_maybe_write_body_blob", flaky_blob)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="",
        headers={}, body=b'{"model":"m","messages":[{"role":"user","content":"hi"}]}',
        phase="agent_run", policy="full", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    # request applied 但 response failed → 合并 failed（不掩盖）。
    assert hop["redaction"]["status"] == "failed"


# ---------- #7：begin 失败不落孤儿 request blob ----------------------------

async def test_no_orphan_blob_when_begin_fails(tmp_path, monkeypatch):
    _write_phase_state(tmp_path, policy="full")

    async def begin_fails(*a, **k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(http_proxy._REGISTRY, "begin", begin_fails)

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="",
        headers={}, body=b'{"model":"m","messages":[{"role":"user","content":"secret"}]}',
        phase="agent_run", policy="full", client=client,
    )).body)
    await client.aclose()

    # begin 失败 → entry=None → 不写任何 blob（无孤儿敏感文件）。
    assert not paths.blobs_dir(tmp_path, ATT).exists()


# ---------- #5：custom_headers 不得覆盖代理控制头 --------------------------

def test_custom_headers_cannot_override_reserved():
    p = _provider(custom_headers=(
        "x-lane-capture-token: LEAK\n"
        "Authorization: Bearer EVIL\n"
        "accept-encoding: gzip\n"
        "x-user-id: custom-value"  # 合法追加头
    ))
    h = http_proxy._forward_headers({"content-type": "application/json"}, p)
    # 保留头不被 custom 覆盖/引入
    assert "x-lane-capture-token" not in {k.lower() for k in h}
    assert h.get("Authorization") is None  # 未被 custom 的 EVIL 覆盖
    assert h["accept-encoding"] == "identity"  # 未被改回 gzip
    # 合法头保留
    assert h["x-user-id"] == "custom-value"


# ---------- #2：SSE 无法结构化验证则 fail-closed（纯函数）------------------

def test_redact_sse_multiline_json_redacted():
    out = http_proxy._redact_sse_text(
        b'data: {"api_key":\ndata: "abcdefghijk"}\n\n')
    assert out is not None and "abcdefghijk" not in out and "REDACTED" in out


def test_redact_sse_broken_json_fail_closed():
    assert http_proxy._redact_sse_text(b'{"cookie":"sessionvalue" BROKEN\n') is None


def test_redact_sse_plain_text_fail_closed():
    assert http_proxy._redact_sse_text(b"just some text\n") is None


def test_redact_sse_partial_data_fail_closed():
    assert http_proxy._redact_sse_text(b'data: {"api_key":\n') is None


def test_redact_sse_done_sentinel_tolerated():
    out = http_proxy._redact_sse_text(b'data: {"x":1}\n\ndata: [DONE]\n\n')
    assert out is not None and "[DONE]" in out


def test_redact_sse_id_field_json_not_leaked():
    # 评审 #1：id: 里塞的 JSON secret 不能原样落盘（只保留 hash）。
    out = http_proxy._redact_sse_text(
        b'id: {"cookie": "session-secret-123"}\ndata: {"ok": true}\n\n')
    assert out is not None
    assert "session-secret-123" not in out
    assert "sha256:" in out  # id 只保留 hash


def test_redact_sse_event_retry_comment_sanitized():
    # 注入到 event/retry/comment 的 secret 被丢弃；合法 event/retry 保留。
    out = http_proxy._redact_sse_text(
        b'event: {"secret":"leak"}\nretry: {"secret":"leak"}\n: {"secret":"leak"}\n'
        b'data: {"x":1}\n\n')
    assert out is not None
    assert "leak" not in out
    # 合法形态保留
    out2 = http_proxy._redact_sse_text(b'event: message\nretry: 3000\ndata: {"x":1}\n\n')
    assert "event: message" in out2 and "retry: 3000" in out2


async def test_full_policy_sse_id_field_no_secret_in_blob(tmp_path):
    """评审 #1：走完整 _maybe_write_body_blob 落盘路径，断言 blob 里无 secret。"""
    _write_phase_state(tmp_path, policy="full")

    async def sse():
        yield b'id: {"cookie":"session-secret-123"}\n'
        yield b'event: message\n'
        yield b'data: {"ok":true}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(sse()),
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="full", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    ref = hop["extensions"].get("x-lane.response-body-ref")
    assert ref is not None  # 合法 SSE，落 blob
    from backend.wire.writer import BlobWriter
    stored = BlobWriter(tmp_path, ATT).read_bytes(ref).decode("utf-8")
    assert "session-secret-123" not in stored  # id secret 未落盘


async def test_full_policy_broken_sse_not_written(tmp_path):
    """损坏 SSE 在 full 档 fail-closed：不落 blob（不泄漏）。"""
    _write_phase_state(tmp_path, policy="full")

    async def broken():
        yield b'data: {"api_key":"secret123","x":\n'  # 残缺 JSON

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(broken()),
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="full", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    assert "x-lane.response-body-ref" not in hop["extensions"]  # fail-closed


# ---------- #3：response 超限缓存连续前缀 + truncated 进 canonical ---------

async def test_response_truncation_contiguous_prefix_and_canonical(tmp_path, monkeypatch):
    from backend.wire import finalize
    from backend.wire.policy import resolve_effective_policy

    _write_phase_state(tmp_path, policy="full")
    monkeypatch.setattr(http_proxy, "_MAX_CAPTURE_RESPONSE_BYTES", 10)

    async def stream():
        yield b"AAAAAAAA"   # 8B，缓存
        yield b"BBBBBBBB"   # 会超 10 → 停止缓存 latch
        yield b"C"          # 小 chunk，绝不能再缓存（否则前缀+跳过+后缀）

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(stream()),
                              headers={"content-type": "application/octet-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collected = await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="full", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    assert collected == b"AAAAAAAABBBBBBBBC"  # 转发完整无损
    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    assert hop["extensions"].get("x-lane.response-body-truncated") is True

    # truncated 映射进 canonical（前端据此提示残缺）。
    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT,
        policy=resolve_effective_policy(task_requested="full"),
        started_at="2026-07-14T00:00:00Z", finished_at="2026-07-14T00:00:01Z",
    )
    wire = paths.wire_file(tmp_path, ATT)
    rec = next(json.loads(ln) for ln in wire.read_text().splitlines()
               if json.loads(ln)["record_type"] == "http_exchange")
    assert rec["data"]["response_body_truncated"] is True


# ---------- #1/#2：反代 hop 与**真实 normalizer** 的 native llm_call 桥接 -------
#
# 关键：用真实 ClaudeCodeNormalizer 产出的 native evidence（写 producer_call_id=
# msg_1），不人工塞字段。反代从 SSE 响应提取 msg_1，同时写 producer_call_id +
# provider_response_id → union-find 桥接两个 namespace，挂到同一 logical call。

import shutil  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_CLAUDE_FIXTURE = _Path(__file__).parent / "fixtures" / "wire" / "claude" / "events.jsonl"


async def test_metadata_policy_correlation_probe_no_body_persisted(tmp_path):
    """评审 #1：默认 metadata 档也要能提 provider id（关联），但绝不落 body。"""
    _write_phase_state(tmp_path, policy="metadata")

    async def sse():
        yield b'event: message_start\n'
        yield b'data: {"type":"message_start","message":{"id":"msg_probe"}}\n\n'
        yield b'event: content_block_delta\n'
        yield b'data: {"delta":{"text":"secret-content-should-not-persist"}}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(sse()),
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="metadata", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    # metadata 下也提取到了 provider id（关联用），两个 anchor 都写了（桥接）。
    assert hop["correlation_hints"]["provider_response_id"] == "msg_probe"
    assert hop["correlation_hints"]["producer_call_id"] == "msg_probe"
    # 但**绝不落 body**：无 blob ref、无 blob 目录。
    assert "x-lane.response-body-ref" not in hop["extensions"]
    assert not paths.blobs_dir(tmp_path, ATT).exists()


async def test_hop_bridges_to_real_normalizer_llm_call_via_sse(tmp_path):
    from backend.wire import finalize, paths as _paths
    from backend.wire.normalizers.runner import run_native_normalizer
    from backend.wire.policy import resolve_effective_policy

    # 用**默认 metadata** 档：证明关联在真实默认模式下也成立（评审 #1），不依赖 full。
    _write_phase_state(tmp_path, policy="metadata")

    # 1) 真实 CC events → normalizer 产 native-event spool（含 producer_call_id=msg_1）。
    attempt_dir = _paths.attempt_dir(tmp_path, ATT)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(_CLAUDE_FIXTURE, attempt_dir / "events.jsonl")
    assert run_native_normalizer(
        agent_name="claude-code", attempt_id=ATT, data_path=tmp_path) is True

    # 2) 反代收到一个 SSE 响应，其 message_start 带同一 msg_1（真实流式形态）。
    async def sse():
        yield b'event: message_start\n'
        yield b'data: {"type":"message_start","message":{"id":"msg_1"}}\n\n'
        yield b'event: content_block_delta\n'
        yield b'data: {"delta":{"text":"hi"}}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(sse()),
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=_provider(),
        method="POST", path="messages", query_string="",
        headers={}, body=b'{"model":"m","messages":[{"role":"user","content":"hi"}]}',
        phase="agent_run", policy="full", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    # 3) finalize → hop 从 SSE 提取到 msg_1，与 normalizer 的 msg_1 call 桥接同一 lc。
    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT,
        policy=resolve_effective_policy(task_requested="metadata"),
        started_at="2026-07-14T00:00:00Z", finished_at="2026-07-14T00:00:01Z",
    )
    wire = paths.wire_file(tmp_path, ATT)
    records = [json.loads(ln) for ln in wire.read_text().splitlines()]
    hop = next(r for r in records if r["record_type"] == "http_exchange")
    # hop 桥接成功：拿到一个非 unmatched 的 logical_call_id。
    hop_lc = hop["correlation"].get("logical_call_id")
    assert hop_lc is not None
    assert hop["correlation"]["confidence"] != "unmatched"
    # 该 lc 必须与某条 native llm_call 共享——证明 hop 真的挂到了 msg_1 的调用，
    # 而不是自成一个孤立 logical call（桥接前的 bug 就是各自成 lc）。
    native_lcs = {
        r["correlation"].get("logical_call_id")
        for r in records if r["record_type"] == "llm_call"
    }
    assert hop_lc in native_lcs, (hop_lc, native_lcs)


async def test_codex_responses_hop_carries_resp_id(tmp_path):
    """W4-6b：Codex 走 Responses API，反代从 SSE 提取 resp_id 写进 hop。

    注意（能力边界）：Codex CLI 事件流只有 turn 级 aggregate、无逐调用 native
    llm_call，故 hop 无 native-call 可桥接 → unmatched（这是 Codex CLI 未暴露逐调用
    事件的上游限制，非本层 bug）。但 hop 自身携带 provider_response_id，已是 Codex
    调用最细粒度的记录。"""
    _write_phase_state(tmp_path, policy="metadata")

    async def responses_sse():
        yield b'event: response.created\n'
        yield b'data: {"type":"response.created","response":{"id":"resp_codex_1"}}\n\n'
        yield b'event: response.output_text.delta\n'
        yield b'data: {"delta":"hi"}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncByteStream(responses_sse()),
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # Codex provider 是 openai-responses kind。
    cfg = _provider(kind="openai-responses")
    await _drain((await http_proxy.forward(
        data_path=tmp_path, attempt_id=ATT, provider="up", provider_cfg=cfg,
        method="POST", path="responses", query_string="", headers={}, body=b"{}",
        phase="agent_run", policy="metadata", client=client,
    )).body)
    await client.aclose()
    await http_proxy.close_attempt(tmp_path, ATT)

    hop = [r for r in _read_spool(tmp_path) if r["evidence_type"] == "http_exchange"][0]
    assert hop["correlation_hints"]["provider_response_id"] == "resp_codex_1"


class _AsyncByteStream(httpx.AsyncByteStream):
    """把 async generator 包成 httpx 可流式消费的 response stream。"""

    def __init__(self, agen):
        self._agen = agen

    async def __aiter__(self):
        async for chunk in self._agen:
            yield chunk

    async def aclose(self):
        aclose = getattr(self._agen, "aclose", None)
        if aclose is not None:
            await aclose()
