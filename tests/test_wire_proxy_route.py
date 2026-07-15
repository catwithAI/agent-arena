"""W4-1：reverse proxy 路由层 —— capture token 授权 + SSRF 拒绝（design §13.1）。

用 TestClient 打真实路由，upstream 用注入的 MockTransport client 拦截。重点是
路由层的授权/SSRF 语义（转发正确性在 test_wire_http_proxy.py 覆盖）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend.config import Settings
from backend.model_providers import ModelProviderSection
from backend.wire import capture_token, paths

ATT = "att_route"


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def proxy_app(tmp_path, monkeypatch):
    """建 app + bind runtime_state + 注册一个 provider + 注入 mock upstream client。"""
    from fastapi.testclient import TestClient

    from backend import runtime_state
    from backend.db import _init_db_sync, resolve_db_path
    from backend.main import create_app

    data_path = tmp_path / "data"
    data_path.mkdir(parents=True)
    db_path = resolve_db_path(data_path)
    _init_db_sync(db_path)

    settings = Settings()
    settings.lane.data_path = data_path
    settings.lane.envs_path = Path("envs").resolve()
    settings.model_providers = {
        "up": ModelProviderSection(kind="anthropic",
                                   base_url="https://upstream.test/v1")
    }
    app = create_app(settings)

    # phase-state：capture 启用
    ps = paths.phase_state_file(data_path, ATT)
    ps.parent.mkdir(parents=True, exist_ok=True)
    ps.write_text(json.dumps({
        "attempt_id": ATT, "phase": "agent_run", "capture_enabled": True, "policy": "metadata",
    }), encoding="utf-8")

    capture_token.reset()
    with TestClient(app) as client:
        # 覆盖 lifespan 建的 client 为 mock upstream。
        def handler(request: httpx.Request) -> httpx.Response:
            async def _one():
                yield json.dumps({"upstream_saw": request.url.path,
                                  "auth": request.headers.get("authorization")}).encode()
            return httpx.Response(200, stream=_ByteStream(_one()),
                                  headers={"content-type": "application/json"})

        app.state.wire_proxy_client = _mock_client(handler)
        yield client, app
    capture_token.reset()


def test_duplicate_set_cookie_preserved(proxy_app):
    # 评审 #1：两个 Set-Cookie 不能被合并成一条（multi_items + raw_headers）。
    client, app = proxy_app

    def handler(request: httpx.Request) -> httpx.Response:
        async def _one():
            yield b"{}"
        return httpx.Response(
            200, stream=_ByteStream(_one()),
            headers=[("set-cookie", "a=1"), ("set-cookie", "b=2"),
                     ("content-type", "application/json")],
        )

    app.state.wire_proxy_client = _mock_client(handler)
    tok = capture_token.issue(ATT)
    r = client.post(f"/internal/wire-proxy/{ATT}/up/messages", content=b"{}",
                    headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    # httpx/TestClient 的 raw headers 保留两条 set-cookie
    set_cookies = [v for k, v in r.headers.raw if k.lower() == b"set-cookie"]
    assert set_cookies == [b"a=1", b"b=2"]


def test_missing_token_401(proxy_app):
    client, _ = proxy_app
    r = client.post(f"/internal/wire-proxy/{ATT}/up/messages", content=b"{}")
    assert r.status_code == 401


def test_wrong_token_401(proxy_app):
    client, _ = proxy_app
    r = client.post(f"/internal/wire-proxy/{ATT}/up/messages", content=b"{}",
                    headers={"authorization": "Bearer bogus"})
    assert r.status_code == 401


def test_token_for_other_attempt_403(proxy_app):
    client, _ = proxy_app
    # token 绑定 att_other，拿去代理 ATT → 403
    tok = capture_token.issue("att_other")
    r = client.post(f"/internal/wire-proxy/{ATT}/up/messages", content=b"{}",
                    headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_unknown_provider_404(proxy_app):
    client, _ = proxy_app
    tok = capture_token.issue(ATT)
    r = client.post(f"/internal/wire-proxy/{ATT}/evil/messages", content=b"{}",
                    headers={"authorization": f"Bearer {tok}"})
    # provider 只从 server config 查，未知 provider（含任意 SSRF 目标）一律 404
    assert r.status_code == 404


def test_authorized_forwards_to_configured_upstream(proxy_app):
    client, _ = proxy_app
    tok = capture_token.issue(ATT)
    r = client.post(
        f"/internal/wire-proxy/{ATT}/up/messages", content=b'{"model":"m"}',
        headers={"authorization": f"Bearer {tok}",
                 "x-lane-capture-token": tok},
    )
    assert r.status_code == 200
    body = r.json()
    # upstream 只可能是 server 配置的 base_url path，不是客户端指定的任意 URL
    assert body["upstream_saw"] == "/v1/messages"
    # inbound Authorization 未转发（provider 无 key → upstream 无 auth）
    assert body["auth"] is None


def test_x_lane_header_token_also_accepted(proxy_app):
    client, _ = proxy_app
    tok = capture_token.issue(ATT)
    r = client.post(f"/internal/wire-proxy/{ATT}/up/messages", content=b"{}",
                    headers={"x-lane-capture-token": tok})
    assert r.status_code == 200


def test_path_traversal_rejected_at_route(proxy_app):
    client, _ = proxy_app
    tok = capture_token.issue(ATT)
    # 用编码 %2e%2e 让 `..` 段进入 handler（未编码的 ../ 会被 Starlette 在路由前
    # 折叠成 404）。到达 _build_upstream_url 后被拒（评审 M1），不打 upstream。
    r = client.post(f"/internal/wire-proxy/{ATT}/up/%2e%2e/%2e%2e/admin", content=b"{}",
                    headers={"authorization": f"Bearer {tok}"})
    # 400（path 逃逸被拒）或 404（被 URL normalize 拦下）都表示未转发到 upstream。
    assert r.status_code in (400, 404)


def test_oversized_request_413(proxy_app, monkeypatch):
    client, _ = proxy_app
    from backend.wire import proxy_api
    monkeypatch.setattr(proxy_api, "_MAX_PROXY_REQUEST_BYTES", 50)
    tok = capture_token.issue(ATT)
    r = client.post(f"/internal/wire-proxy/{ATT}/up/messages", content=b"A" * 500,
                    headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 413


class _ByteStream(httpx.AsyncByteStream):
    def __init__(self, agen):
        self._agen = agen

    async def __aiter__(self):
        async for chunk in self._agen:
            yield chunk

    async def aclose(self):
        aclose = getattr(self._agen, "aclose", None)
        if aclose is not None:
            await aclose()
