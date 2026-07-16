"""W4-6：第三方模型反代接线 —— HttpProxySource + adapter 注入 + dispatch 判定。

覆盖 tasks.md W4-6 验收：
- HttpProxySource.start 签发 token + 构造 proxy base URL injection（token 走专用
  字段不进 process_env）；
- CC/Codex adapter 消费 injection：base URL 覆盖成 proxy、LANE_WIRE_CAPTURE_TOKEN
  注入子进程；
- dispatch 判定：只有 CC/Codex 且命名第三方 provider 才挂 source；官方 provider /
  第三方 agent 不挂（能力边界，评审 #9 选项 A）；
- 并发两个 attempt 的 proxy URL/token 隔离；
- capture_policy 与 server maximum 求最严格交集。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from backend.adapters.base import AdapterRunInput
from backend.config import Settings
from backend.model_providers import ModelProviderSection
from backend.wire import capture_token, paths
from backend.wire.injection import WireInjection
from backend.wire.lifecycle import CaptureContext
from backend.wire.policy import resolve_effective_policy
from backend.wire.sources.http_proxy_source import HttpProxySource

# 只给 async 测试标 asyncio，避免同步 dispatch-gate / policy 测试触发无关警告。
_aio = pytest.mark.asyncio


def _ctx(attempt_id="att_x", phase="agent_run"):
    return CaptureContext(
        attempt_id=attempt_id, attempt_dir=Path("/tmp"), agent_name="claude-code",
        phase=phase, policy=resolve_effective_policy(task_requested="metadata"),
    )


@pytest.fixture(autouse=True)
def _reset_token():
    capture_token.reset()
    yield
    capture_token.reset()


# ---------- HttpProxySource ---------------------------------------------------

@_aio
async def test_source_start_issues_token_and_builds_proxy_url():
    src = HttpProxySource(
        attempt_id="att_x", provider="up", public_base_url="http://127.0.0.1:8100")
    inj = await src.start(_ctx("att_x"))
    assert inj.enabled is True
    assert inj.llm_base_url == "http://127.0.0.1:8100/internal/wire-proxy/att_x/up"
    # token 走专用字段，绑定该 attempt（可被 proxy 路由 resolve）。
    assert inj.capture_token is not None
    assert capture_token.resolve(inj.capture_token) == "att_x"
    # token 绝不进 process_env（否则被 secret-key 校验拒绝）。
    assert "LANE_WIRE_CAPTURE_TOKEN" not in inj.process_env


@_aio
async def test_source_kind_and_instance():
    src = HttpProxySource(attempt_id="a", provider="openrouter", public_base_url="http://x")
    assert src.kind == "lane-http"
    assert src.rewrites_transport is True
    assert src.instance == "openrouter"  # finalizer 按 instance 归属


@_aio
async def test_source_injection_passes_validate():
    # source 的 injection 必须过 lifecycle 的 merge/validate（不含 secret env 名）。
    from backend.wire.lifecycle import merge_injections
    src = HttpProxySource(attempt_id="att_x", provider="up", public_base_url="http://127.0.0.1:8100")
    inj = await src.start(_ctx("att_x"))
    merged, gaps = merge_injections([inj], capabilities={"llm_base_url": True})
    assert merged.llm_base_url == inj.llm_base_url
    assert merged.capture_token == inj.capture_token


# ---------- adapter 消费 injection -------------------------------------------

_STREAM_OK = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "done", "session_id": "s", "usage": {},
})


class _FakeProc:
    def __init__(self):
        self.returncode = 0
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        r = asyncio.StreamReader()
        r.feed_data((_STREAM_OK + "\n").encode())
        r.feed_eof()
        self.stdout = r

    async def wait(self): return 0
    def kill(self): pass


def _task(**over) -> AdapterRunInput:
    d = dict(attempt_id="att_x", task_id="t", task_prompt="p", task_context={},
             timeout_seconds=10, env_name="travel-planner",
             env_skill_id="lane/travel-planner", session_token="tok",
             env_base_url="http://127.0.0.1:8100")
    d.update(over)
    return AdapterRunInput(**d)


_PROXY_INJECTION = WireInjection(
    enabled=True, phase="agent_run",
    llm_base_url="http://127.0.0.1:8100/internal/wire-proxy/att_x/up",
    capture_token="TOK-SECRET",
)


@_aio
async def test_claude_adapter_injects_proxy_base_and_token(tmp_path, monkeypatch):
    from backend.adapters.claude_code import ClaudeCodeAdapter
    providers = {"up": ModelProviderSection(
        kind="anthropic", base_url="https://real.upstream/v1")}
    adapter = ClaudeCodeAdapter(model="up/glm", providers=providers)
    with patch("shutil.which", return_value="/x/claude"), \
         patch("asyncio.create_subprocess_exec", return_value=_FakeProc()) as spawn:
        await adapter.run(_task(wire_injection=_PROXY_INJECTION), None, tmp_path)
    env = spawn.call_args.kwargs["env"]
    # base URL 被反代覆盖（不是真实 upstream）。
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8100/internal/wire-proxy/att_x/up"
    # capture token 注入子进程。
    assert env["LANE_WIRE_CAPTURE_TOKEN"] == "TOK-SECRET"


@_aio
async def test_codex_adapter_injects_proxy_base_and_token(tmp_path, monkeypatch):
    from backend.adapters.codex import CodexAdapter
    providers = {"up": ModelProviderSection(
        kind="openai-responses", base_url="https://real.upstream/v1")}
    adapter = CodexAdapter(model="up/glm", providers=providers)
    with patch("shutil.which", return_value="/x/codex"), \
         patch("asyncio.create_subprocess_exec", return_value=_FakeProc()) as spawn:
        await adapter.run(_task(wire_injection=_PROXY_INJECTION), None, tmp_path)
    env = spawn.call_args.kwargs["env"]
    assert env["LANE_WIRE_CAPTURE_TOKEN"] == "TOK-SECRET"
    # Codex 的 -c base_url 覆盖成反代。
    joined = " ".join(spawn.call_args.args)
    assert "internal/wire-proxy/att_x/up" in joined


# ---------- dispatch 判定（能力边界）-----------------------------------------

def _settings_with_provider() -> Settings:
    s = Settings()
    s.model_providers = {"up": ModelProviderSection(
        kind="anthropic", base_url="https://up/v1")}
    return s


def _build(agent, model, *, with_mcp=True):
    from pathlib import Path
    from backend.run_dispatch import _build_wire_sources
    return _build_wire_sources(
        agent_name=agent, model=model, settings=_settings_with_provider(),
        attempt_id="att_x", env_name="travel-planner", data_path=Path("/tmp/d"),
        mcp_server_names=("lane-travel-planner",) if with_mcp else ())


def _kinds(srcs):
    return {s.kind for s in srcs}


def test_dispatch_gate_cc_named_provider_gets_proxy_and_mcp():
    srcs = _build("claude-code", "up/glm")
    # 命名 provider → 反代 + MCP tap 两个 source。
    assert _kinds(srcs) == {"lane-http", "mcp-stdio"}
    proxy = next(s for s in srcs if s.kind == "lane-http")
    assert proxy.provider == "up"


def test_dispatch_gate_codex_named_provider_gets_proxy_and_mcp():
    assert _kinds(_build("codex", "up/glm")) == {"lane-http", "mcp-stdio"}


def test_dispatch_gate_official_provider_only_mcp_no_proxy():
    # 官方默认 provider（无 / 前缀命中）→ 不挂反代，但仍挂 MCP tap（本机 MCP server）。
    srcs = _build("claude-code", "opus")
    assert _kinds(srcs) == {"mcp-stdio"}


def test_dispatch_gate_third_party_agent_no_source():
    # 第三方 agent 走 SDK / Env Server HTTP，反代与本机 MCP tap 都不适用。
    assert _build("third-party-agent", "up/glm") == []


def test_dispatch_gate_without_scene_mcp_does_not_create_tap():
    # 场景未声明 MCP server 时，即使是命名 provider 也不挂 MCP tap。
    assert _kinds(_build("codex", "up/glm", with_mcp=False)) == {"lane-http"}


# ---------- _mcp_server_specs：场景声明式 MCP，framework 不推断 ----------------


def test_mcp_entrypoint_is_scene_declared_not_inferred(tmp_path: Path):
    from types import SimpleNamespace

    from backend.run_dispatch import _mcp_server_specs

    env = SimpleNamespace(
        name="demo-env",
        env_dir=tmp_path / "envs" / "demo-env",
        meta={"entrypoints": {"mcp": {
            "enabled": True,
            "transport": "stdio",
            "name": "scene-search",
            "command": ["custom-search-server", "--stdio"],
        }}},
    )
    specs = _mcp_server_specs(env)
    assert len(specs) == 1
    assert specs[0].name == "scene-search"
    assert specs[0].command == "custom-search-server"
    assert specs[0].args == ("--stdio",)


def test_mcp_entrypoint_defaults_name_to_lane_prefixed_env_name(tmp_path: Path):
    from types import SimpleNamespace

    from backend.run_dispatch import _mcp_server_specs

    env = SimpleNamespace(
        name="demo-env",
        env_dir=tmp_path / "envs" / "demo-env",
        meta={"entrypoints": {"mcp": {
            "enabled": True,
            "transport": "stdio",
            "command": ["uv", "run", "--project", ".", "python", "envs/demo-env/mcp_server.py"],
        }}},
    )
    specs = _mcp_server_specs(env)
    assert specs[0].name == "lane-demo-env"
    assert specs[0].command == "uv"
    assert specs[0].args == ("run", "--project", ".", "python", "envs/demo-env/mcp_server.py")


def test_no_mcp_declaration_means_no_mcp_even_if_file_exists(tmp_path: Path):
    from types import SimpleNamespace

    from backend.run_dispatch import _mcp_server_specs

    env_dir = tmp_path / "envs" / "demo-env"
    env_dir.mkdir(parents=True)
    (env_dir / "mcp_server.py").write_text("# must not be inferred")
    env = SimpleNamespace(name="demo-env", env_dir=env_dir, meta={})
    assert _mcp_server_specs(env) == ()

    env_disabled = SimpleNamespace(
        name="demo-env", env_dir=env_dir,
        meta={"entrypoints": {"mcp": {"enabled": False}}},
    )
    assert _mcp_server_specs(env_disabled) == ()


def test_mcp_entrypoint_rejects_non_stdio_transport(tmp_path: Path):
    from types import SimpleNamespace

    from backend.run_dispatch import _mcp_server_specs

    env = SimpleNamespace(
        name="demo-env",
        env_dir=tmp_path / "envs" / "demo-env",
        meta={"entrypoints": {"mcp": {
            "enabled": True, "transport": "http", "command": ["x"],
        }}},
    )
    with pytest.raises(ValueError):
        _mcp_server_specs(env)


def test_mcp_entrypoint_rejects_empty_command(tmp_path: Path):
    from types import SimpleNamespace

    from backend.run_dispatch import _mcp_server_specs

    env = SimpleNamespace(
        name="demo-env",
        env_dir=tmp_path / "envs" / "demo-env",
        meta={"entrypoints": {"mcp": {"enabled": True, "command": []}}},
    )
    with pytest.raises(ValueError):
        _mcp_server_specs(env)


def test_mcp_entrypoint_rejects_illegal_server_name(tmp_path: Path):
    from types import SimpleNamespace

    from backend.run_dispatch import _mcp_server_specs

    env = SimpleNamespace(
        name="demo-env",
        env_dir=tmp_path / "envs" / "demo-env",
        meta={"entrypoints": {"mcp": {
            "enabled": True, "name": "bad name!", "command": ["x"],
        }}},
    )
    with pytest.raises(ValueError):
        _mcp_server_specs(env)


# ---------- 并发隔离 ----------------------------------------------------------

@_aio
async def test_concurrent_attempts_isolated_tokens_and_urls():
    a = HttpProxySource(attempt_id="att_A", provider="up", public_base_url="http://h:8100")
    b = HttpProxySource(attempt_id="att_B", provider="up", public_base_url="http://h:8100")
    ia = await a.start(_ctx("att_A"))
    ib = await b.start(_ctx("att_B"))
    # token 各绑各的 attempt，不互串。
    assert ia.capture_token != ib.capture_token
    assert capture_token.resolve(ia.capture_token) == "att_A"
    assert capture_token.resolve(ib.capture_token) == "att_B"
    # base URL 各含各自 attempt。
    assert "att_A" in ia.llm_base_url and "att_B" in ib.llm_base_url


@_aio
async def test_revoke_invalidates_token():
    src = HttpProxySource(attempt_id="att_x", provider="up", public_base_url="http://h")
    inj = await src.start(_ctx("att_x"))
    assert capture_token.resolve(inj.capture_token) == "att_x"
    capture_token.revoke("att_x")  # lifecycle finalize/abort 会调
    assert capture_token.resolve(inj.capture_token) is None


# ---------- capture_policy 解析 ----------------------------------------------

def test_capture_policy_strictest_intersection():
    # run 请求 full，但 server_max=metadata → effective 降到 metadata。
    p = resolve_effective_policy(server_max="metadata", run_requested="full")
    assert p.effective == "metadata"
    assert p.requested == "full"
    assert p.downgrade_reason == "server_max"


def test_capture_policy_default_metadata():
    assert resolve_effective_policy().effective == "metadata"


# ---------- 端到端：adapter 配置 → 模拟 CLI 请求 → proxy 授权成功（评审 #1）----
#
# 这是关键闭环：证明 adapter 配置的 capture token 头能真的通过 proxy 授权，
# 而不是只落 env（CLI 不会自动把 env 转 header，之前的洞）。

def _parse_anthropic_custom_headers(raw: str) -> dict[str, str]:
    """解析 ANTHROPIC_CUSTOM_HEADERS（"Name: value"，\\n 分隔）——CLI 就是这样
    把它拆成真实请求头的。"""
    out = {}
    for line in raw.split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


@_aio
async def test_e2e_claude_configured_token_authorizes_at_proxy(tmp_path):
    """CC adapter 配好的 X-Lane-Capture-Token 头，打到真实 proxy 路由 → 200。"""
    from fastapi.testclient import TestClient

    from backend.adapters.claude_code import ClaudeCodeAdapter
    from backend.config import Settings
    from backend.db import _init_db_sync, resolve_db_path
    from backend.main import create_app

    # 1) 用真实 HttpProxySource 生成 injection（含绑定 att 的 token）。
    att = "att_e2e"
    src = HttpProxySource(attempt_id=att, provider="up", public_base_url="http://127.0.0.1:8100")
    inj = await src.start(_ctx(att))

    # 2) 让 CC adapter 消费 injection，取它配的 ANTHROPIC_CUSTOM_HEADERS。
    providers = {"up": ModelProviderSection(kind="anthropic", base_url="https://up/v1")}
    adapter = ClaudeCodeAdapter(model="up/glm", providers=providers)
    with patch("shutil.which", return_value="/x/claude"), \
         patch("asyncio.create_subprocess_exec", return_value=_FakeProc()) as spawn:
        await adapter.run(_task(attempt_id=att, wire_injection=inj), None, tmp_path)
    env = spawn.call_args.kwargs["env"]
    cli_headers = _parse_anthropic_custom_headers(env.get("ANTHROPIC_CUSTOM_HEADERS", ""))
    assert "X-Lane-Capture-Token" in cli_headers  # adapter 确实配了头

    # 3) 起真实 app + proxy 路由，用 mock upstream，模拟 CLI 带该头请求 proxy。
    data_path = tmp_path / "data"
    data_path.mkdir()
    db_path = resolve_db_path(data_path)
    _init_db_sync(db_path)
    settings = Settings()
    settings.lane.data_path = data_path
    settings.lane.envs_path = Path("envs").resolve()
    settings.model_providers = providers
    # phase-state：capture 启用（proxy 路由要读它拿 policy/phase）。
    ps = paths.phase_state_file(data_path, att)
    ps.parent.mkdir(parents=True, exist_ok=True)
    ps.write_text(json.dumps({
        "attempt_id": att, "phase": "agent_run", "capture_enabled": True, "policy": "metadata",
    }), encoding="utf-8")

    app = create_app(settings)
    with TestClient(app) as client:
        def handler(request: httpx.Request) -> httpx.Response:
            async def _one():
                yield b'{"ok":true}'
            return httpx.Response(200, stream=_OneShot(_one()),
                                  headers={"content-type": "application/json"})
        app.state.wire_proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        # 模拟 CLI：base URL = injection.llm_base_url，带 adapter 配的 capture 头。
        # CLI 还会带 provider auth（Authorization），proxy 应认独立的 capture 头。
        r = client.post(
            f"/internal/wire-proxy/{att}/up/v1/messages",
            content=b'{"model":"glm"}',
            headers={
                "X-Lane-Capture-Token": cli_headers["X-Lane-Capture-Token"],
                "authorization": "Bearer PROVIDER-API-KEY",  # provider auth，非 capture token
                "content-type": "application/json",
            },
        )
    # 授权成功（不是 401）：token 头被正确识别。
    assert r.status_code == 200, r.content
    assert r.json()["ok"] is True


class _OneShot(httpx.AsyncByteStream):
    def __init__(self, agen):
        self._agen = agen

    async def __aiter__(self):
        async for c in self._agen:
            yield c

    async def aclose(self):
        pass
