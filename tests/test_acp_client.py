from __future__ import annotations

import sys

import pytest

from backend.agents.acp.client import AcpClient, AcpClientError
from backend.agents.acp.parser import AcpParser


_FAKE_SERVER = r'''
import json, sys, time
mode = sys.argv[1]
session = "fixture-session"
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"protocolVersion":1,"agentCapabilities":{},"agentInfo":{"name":"fixture","version":"1.0.0"},"authMethods":[]}}), flush=True)
    elif method == "session/new":
        print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"sessionId":session}}), flush=True)
    elif method == "session/prompt":
        text = msg["params"]["prompt"][0]["text"]
        if mode == "crash":
            sys.exit(17)
        if mode == "hang":
            time.sleep(10)
        print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session,"update":{"sessionUpdate":"agent_thought_chunk","content":{"type":"text","text":"think"}}}}), flush=True)
        print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session,"update":{"sessionUpdate":"tool_call","toolCallId":"tool-1","title":"edit","kind":"edit","status":"pending"}}}), flush=True)
        if mode == "permission":
            print(json.dumps({"jsonrpc":"2.0","id":80,"method":"session/request_permission","params":{"sessionId":session,"toolCall":{"toolCallId":"tool-1"},"options":[{"optionId":"allow","name":"Allow","kind":"allow_once"},{"optionId":"deny","name":"Deny","kind":"reject_once"}]}}), flush=True)
            permission = json.loads(sys.stdin.readline())
            selected = permission["result"]["outcome"].get("optionId", permission["result"]["outcome"]["outcome"])
            text = text + ":" + selected
        print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session,"update":{"sessionUpdate":"agent_message_chunk","messageId":"m1","content":{"type":"text","text":text}}}}), flush=True)
        print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session,"update":{"sessionUpdate":"usage_update","used":12,"size":100}}}), flush=True)
        print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"stopReason":"end_turn"}}), flush=True)
    elif method == "session/cancel":
        pass
'''


def _client(tmp_path, mode="normal", **kwargs):
    return AcpClient(
        [sys.executable, "-u", "-c", _FAKE_SERVER, mode],
        cwd=tmp_path,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_environment_is_allowlisted_and_explicit_values_win(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOULD_NOT_REACH_ACP", "global-secret")
    script = r'''
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg["method"] == "initialize":
        result = {"protocolVersion": 1}
    elif msg["method"] == "session/new":
        result = {"sessionId": "s"}
    else:
        text = f'{os.environ.get("SHOULD_NOT_REACH_ACP", "absent")}:{os.environ["HOME"]}'
        print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"text":text}}}}), flush=True)
        result = {"stopReason": "end_turn"}
    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":result}), flush=True)
'''
    private_home = tmp_path / "private-home"
    result = await AcpClient(
        [sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        env={"HOME": str(private_home)},
    ).run(["x"], timeout_seconds=3)

    assert AcpParser().parse(result).final_text == f"absent:{private_home}"


@pytest.mark.asyncio
async def test_acp_single_and_multi_turn_transcript(tmp_path):
    result = await _client(tmp_path).run(["one", "two"], timeout_seconds=3)
    parsed = AcpParser().parse(result)

    assert result.session_id == "fixture-session"
    assert result.stop_reasons == ("end_turn", "end_turn")
    assert parsed.final_text == "onetwo"
    assert len(parsed.tool_refs) == 2
    assert parsed.usage == {"context_tokens": 12, "context_window_tokens": 100}
    assert result.transcript[0]["direction"] == "client_to_agent"
    assert result.transcript[0]["message"]["method"] == "initialize"
    assert any(item["direction"] == "agent_to_client" for item in result.transcript)


@pytest.mark.asyncio
async def test_permission_is_never_implicitly_approved(tmp_path):
    result = await _client(tmp_path, "permission").run(["edit"], timeout_seconds=3)
    parsed = AcpParser().parse(result)
    assert result.permission_unanswered is True
    assert parsed.final_text == "edit:cancelled"
    assert parsed.degraded is True
    assert any(item.code == "acp.permission_unanswered" for item in parsed.diagnostics)


@pytest.mark.asyncio
async def test_explicit_permission_answer_selects_only_declared_option(tmp_path):
    result = await _client(
        tmp_path,
        "permission",
        permission_answers={"tool-1": "deny"},
    ).run(["edit"], timeout_seconds=3)
    assert AcpParser().parse(result).final_text == "edit:deny"
    assert result.permission_unanswered is False


@pytest.mark.asyncio
async def test_crash_and_timeout_are_stable_failures(tmp_path):
    with pytest.raises(AcpClientError) as crash:
        await _client(tmp_path, "crash").run(["x"], timeout_seconds=3)
    assert crash.value.code == "agent_nonzero_exit"

    with pytest.raises(AcpClientError) as timeout:
        await _client(tmp_path, "hang").run(["x"], timeout_seconds=0.05)
    assert timeout.value.code == "agent_timeout"


@pytest.mark.asyncio
async def test_protocol_version_mismatch_is_rejected(tmp_path):
    script = _FAKE_SERVER.replace('"protocolVersion":1', '"protocolVersion":2')
    client = AcpClient([sys.executable, "-u", "-c", script, "normal"], cwd=tmp_path)
    with pytest.raises(AcpClientError) as error:
        await client.run(["x"], timeout_seconds=3)
    assert error.value.code == "agent_version_unsupported"
