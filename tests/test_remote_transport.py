from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from backend.agents.remote import RemoteTransportClient, RemoteTransportError


@pytest.mark.asyncio
async def test_remote_poll_completed_and_partial_artifact_sync(tmp_path):
    good = b"remote artifact"
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"sessionId": "s1", "status": "running"})
        if request.url.path == "/v1/sessions/s1":
            return httpx.Response(
                200,
                json={
                    "sessionId": "s1",
                    "status": "completed",
                    "finalText": "done",
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                    "events": [{"type": "tool", "name": "build"}],
                    "artifacts": [
                        {
                            "path": "result.txt",
                            "url": "/files/good",
                            "sha256": hashlib.sha256(good).hexdigest(),
                            "size": len(good),
                        },
                        {
                            "path": "bad.txt",
                            "url": "/files/bad",
                            "sha256": "0" * 64,
                            "size": 3,
                        },
                    ],
                },
            )
        if request.url.path == "/files/good":
            return httpx.Response(200, content=good)
        if request.url.path == "/files/bad":
            return httpx.Response(200, content=b"bad")
        raise AssertionError(request.url)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RemoteTransportClient(
        "https://remote.test/", client=http, poll_interval_seconds=0
    )
    result = await client.run({"prompt": "task"}, workspace=tmp_path, timeout_seconds=2)

    assert result.status == "completed"
    assert result.final_text == "done"
    assert result.usage == {"input_tokens": 4, "output_tokens": 2}
    assert result.artifacts == (tmp_path / "result.txt",)
    assert (tmp_path / "result.txt").read_bytes() == good
    assert result.artifact_failures and "checksum mismatch" in result.artifact_failures[0]
    assert ("POST", "/v1/sessions") in calls
    await http.aclose()


@pytest.mark.asyncio
async def test_remote_ndjson_stream_carries_updates_and_terminal_snapshot(tmp_path):
    stream = "\n".join(
        [
            json.dumps({"type": "message", "text": "working"}),
            json.dumps(
                {
                    "type": "snapshot",
                    "data": {
                        "sessionId": "stream-1",
                        "status": "completed",
                        "finalText": "streamed",
                    },
                }
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "sessionId": "stream-1",
                    "status": "running",
                    "streamUrl": "/v1/sessions/stream-1/updates",
                },
            )
        return httpx.Response(200, content=stream.encode())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RemoteTransportClient("https://remote.test", client=http)
    result = await client.run({}, workspace=tmp_path, timeout_seconds=2)

    assert result.final_text == "streamed"
    assert result.events == ({"type": "message", "text": "working"},)
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confirmed", "expected"),
    [(True, "cancel_confirmed"), (False, "cancel_requested_remote_unknown")],
)
async def test_remote_timeout_reports_cancel_confirmation(
    tmp_path, confirmed: bool, expected: str
):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"sessionId": "slow", "status": "running"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"confirmed": confirmed})
        return httpx.Response(200, json={"sessionId": "slow", "status": "running"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RemoteTransportClient(
        "https://remote.test", client=http, poll_interval_seconds=0.01
    )
    with pytest.raises(RemoteTransportError) as error:
        await client.run({}, workspace=tmp_path, timeout_seconds=0.03)

    assert error.value.code == "agent_timeout"
    assert expected in str(error.value)
    await http.aclose()


@pytest.mark.asyncio
async def test_remote_cancel_network_failure_and_cross_origin_artifact_fail_closed(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"sessionId": "s2", "status": "running"})
        if request.method == "DELETE":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(
            200,
            json={
                "sessionId": "s2",
                "status": "completed",
                "artifacts": [
                    {
                        "path": "stolen.txt",
                        "url": "https://other.test/file",
                        "sha256": "0" * 64,
                        "size": 0,
                    }
                ],
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RemoteTransportClient("https://remote.test", client=http, poll_interval_seconds=0)
    result = await client.run({}, workspace=tmp_path, timeout_seconds=2)
    assert result.artifacts == ()
    assert "crossed" in result.artifact_failures[0]
    assert await client.cancel() == "cancel_requested_remote_unknown"
    await http.aclose()


def test_remote_endpoint_requires_https():
    with pytest.raises(ValueError, match="HTTPS"):
        RemoteTransportClient("http://remote.test")
