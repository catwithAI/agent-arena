"""W6-4a 验收：Responses↔Chat compatibility source（R15.6/R15.7）。

- 转换前后两 hop 分别记录，归属同一 logical call；
- 两跳按各自协议算 semantic summary，跨协议同 hash（证明同一逻辑调用）；
- 独立 source：关闭后原生路径不受影响，不内嵌 schema/分析层。
"""

from __future__ import annotations

import json

from backend.wire import finalize, paths
from backend.wire.policy import resolve_effective_policy
from backend.wire.sources.responses_compat import ResponsesCompatSource

ATT = "att_compat"

# 同一逻辑请求的两种协议表达（Responses vs Chat Completions）。
_RESPONSES = json.dumps({
    "model": "m", "instructions": "be helpful",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
}).encode()
_CHAT = json.dumps({
    "model": "m",
    "messages": [{"role": "system", "content": "be helpful"},
                 {"role": "user", "content": "hi"}],
}).encode()


def _record(tmp_path, **over):
    kw = dict(
        phase="agent_run",
        inbound_protocol="openai-responses", inbound_request_body=_RESPONSES,
        inbound_endpoint="https://gw/v1/responses", inbound_status_code=200,
        outbound_protocol="openai-chat-completions", outbound_request_body=_CHAT,
        outbound_endpoint="https://up/v1/chat/completions", outbound_status_code=200,
    )
    kw.update(over)
    src = ResponsesCompatSource(data_path=tmp_path, attempt_id=ATT, instance="gw")
    return src.record_conversion(**kw)


def _finalize_hops(tmp_path):
    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT,
        policy=resolve_effective_policy(task_requested="metadata"),
        started_at="2026-07-14T00:00:00Z", finished_at="2026-07-14T00:00:01Z")
    recs = [json.loads(ln) for ln in paths.wire_file(tmp_path, ATT).read_text().splitlines()]
    return [r for r in recs if r["record_type"] == "http_exchange"]


def test_two_hops_recorded(tmp_path):
    _record(tmp_path)
    hops = _finalize_hops(tmp_path)
    assert len(hops) == 2
    paths_ = {h["data"]["path"] for h in hops}
    assert paths_ == {"/v1/responses", "/v1/chat/completions"}


def test_two_hops_same_logical_call(tmp_path):
    # R15.7：转换前后两 hop 归属同一 logical call。
    _record(tmp_path)
    hops = _finalize_hops(tmp_path)
    lcs = {h["correlation"].get("logical_call_id") for h in hops}
    assert len(lcs) == 1  # 同一 logical call
    assert None not in lcs
    # 不是 unmatched（有共享 request_id anchor）。
    assert all(h["correlation"]["confidence"] != "unmatched" for h in hops)


def test_cross_protocol_semantic_hash_matches(tmp_path):
    # 两跳按各自协议解析，semantic hash 相等——证明是同一逻辑调用。
    _record(tmp_path)
    hops = _finalize_hops(tmp_path)
    hashes = [(h["data"].get("request_summary") or {}).get("messages_hash") for h in hops]
    assert hashes[0] == hashes[1]
    assert hashes[0] is not None


def test_hops_carry_protocol_metadata(tmp_path):
    # 转换前后协议名可区分（走扩展，不占 schema 字段）。extensions 只在 raw
    # evidence，这里从 spool 读原始 evidence 验证。
    from backend.wire import spool
    _record(tmp_path)
    f = paths.source_spool_file(tmp_path, ATT, "responses-compat", "gw")
    evs = [r for r in spool.read_spool(f).records if r["evidence_type"] == "http_exchange"]
    protos = {e["extensions"]["x-lane.compat-protocol"] for e in evs}
    assert protos == {"openai-responses", "openai-chat-completions"}
    hops_kind = {e["extensions"]["x-lane.compat-hop"] for e in evs}
    assert hops_kind == {"inbound", "outbound"}


def test_source_disabled_native_path_unaffected(tmp_path):
    # R15.6：关闭本 source（不记录）→ 不产 compat evidence，其它 source 照常。
    # 只写一条 native llm_call，不调 record_conversion。
    from backend.wire import evidence, spool
    payload = {**evidence.null_payload("native_llm_call"),
               "producer_call_id": "msg_1", "model": "m", "call_role": "main"}
    native = {
        "evidence_id": "we_n1", "attempt_id": ATT, "phase": "agent_run",
        "evidence_type": "native_llm_call",
        "source": {"kind": "native-event", "instance": "native-event"},
        "producer": {"name": "t"}, "time": {"observed_at": "2026-07-14T00:00:00Z"},
        "raw_ref": {"kind": "raw-line", "file": "events.jsonl", "line": 1},
        "correlation_hints": {"producer_call_id": "msg_1"},
        "capabilities": {}, "redaction": {"policy": "metadata", "status": "applied"},
        "errors": [], "extensions": {}, "payload": payload,
    }
    w = spool.SpoolWriter(
        paths.source_spool_file(tmp_path, ATT, "native-event"), expected_attempt_id=ATT)
    w.append(native)
    w.close()
    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT,
        policy=resolve_effective_policy(task_requested="metadata"),
        started_at="2026-07-14T00:00:00Z", finished_at="2026-07-14T00:00:01Z")
    recs = [json.loads(ln) for ln in paths.wire_file(tmp_path, ATT).read_text().splitlines()]
    # native llm_call 正常产出；无任何 responses-compat http_exchange。
    assert any(r["record_type"] == "llm_call" for r in recs)
    assert all(r["source"]["kind"] != "responses-compat" for r in recs)


def test_shared_conversion_id_returned(tmp_path):
    # 同一 conversion 返回稳定 conversion_id，可供上游做外部关联。
    cid = _record(tmp_path)
    assert cid and cid.startswith("conv:")


def test_explicit_conversion_id_used(tmp_path):
    # 上游可传入自己的 conversion_id（如 gateway trace id）。
    cid = _record(tmp_path, conversion_id="gw-trace-xyz")
    assert cid == "gw-trace-xyz"
    hops = _finalize_hops(tmp_path)
    assert len({h["correlation"].get("logical_call_id") for h in hops}) == 1


def test_auto_conversion_id_globally_unique(tmp_path):
    # 评审：顶层每次新 source 也不能撞 id——多次转换必须各自独立 logical call。
    from backend.wire.sources.responses_compat import record_conversion
    kw = dict(
        phase="agent_run",
        inbound_protocol="openai-responses", inbound_request_body=_RESPONSES,
        inbound_endpoint="https://gw/v1/responses", inbound_status_code=200,
        outbound_protocol="openai-chat-completions", outbound_request_body=_CHAT,
        outbound_endpoint="https://up/v1/chat/completions", outbound_status_code=200,
    )
    ids_ = {record_conversion(data_path=tmp_path, attempt_id=ATT, **kw) for _ in range(5)}
    assert len(ids_) == 5  # 5 次转换 5 个不同 id


def test_inbound_outbound_direction_and_status(tmp_path):
    # 评审：inbound 写 inbound、outbound 写 outbound；两跳各自 status。
    _record(tmp_path, inbound_status_code=200, outbound_status_code=502)
    hops = _finalize_hops(tmp_path)
    by_path = {h["data"]["path"]: h for h in hops}
    assert by_path["/v1/responses"]["data"]["direction"] == "inbound"
    assert by_path["/v1/chat/completions"]["data"]["direction"] == "outbound"
    # 转换层 200、上游 502——各自 status 独立记录。
    assert by_path["/v1/responses"]["data"]["status_code"] == 200
    assert by_path["/v1/chat/completions"]["data"]["status_code"] == 502


def test_fake_converter_roundtrip_two_hops(tmp_path):
    """集成：一个 fake converter 真做 Responses→Chat→Responses 转换，用它的实际
    输入输出调 record_conversion，产两个 hop 且归属同一 logical call。"""
    from backend.wire.sources.responses_compat import ResponsesCompatSource

    # fake converter：把 Responses 请求转成 Chat Completions 请求（真做字段搬运）。
    def responses_to_chat(responses_req: dict) -> dict:
        msgs = []
        if responses_req.get("instructions"):
            msgs.append({"role": "system", "content": responses_req["instructions"]})
        for item in responses_req.get("input", []):
            texts = [b.get("text", "") for b in item.get("content", [])
                     if b.get("type") == "input_text"]
            msgs.append({"role": item.get("role", "user"), "content": "".join(texts)})
        return {"model": responses_req["model"], "messages": msgs}

    responses_req = json.loads(_RESPONSES)
    chat_req = responses_to_chat(responses_req)  # converter 真转换

    src = ResponsesCompatSource(data_path=tmp_path, attempt_id=ATT, instance="gw")
    src.record_conversion(
        phase="agent_run",
        inbound_protocol="openai-responses",
        inbound_request_body=json.dumps(responses_req).encode(),
        inbound_endpoint="https://gw/v1/responses", inbound_status_code=200,
        outbound_protocol="openai-chat-completions",
        outbound_request_body=json.dumps(chat_req).encode(),
        outbound_endpoint="https://up/v1/chat/completions", outbound_status_code=200)

    hops = _finalize_hops(tmp_path)
    assert len(hops) == 2
    # 同一 logical call，且跨协议 semantic hash 相等（converter 转换正确）。
    assert len({h["correlation"].get("logical_call_id") for h in hops}) == 1
    hashes = [(h["data"].get("request_summary") or {}).get("messages_hash") for h in hops]
    assert hashes[0] == hashes[1] and hashes[0] is not None
