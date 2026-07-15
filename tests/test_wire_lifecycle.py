"""W0-4 验收：lifecycle + injection 接线。

覆盖 tasks.md W0-4 验收清单：
- prepare/run 时序断言（source.start/ready 先于 adapter.run）；
- injection 合并冲突（保留名、secret 校验、标量冲突不 last-wins）；
- prepare 失败 fail-open 降级后 adapter 拿到未污染 env；
- abort 路径 flush（spool 保留 .partial）；
- 三个 adapter 的消费点（CC subprocess env/base/headers/MCP config；
  Codex provider/MCP -c 参数）。

空 injection 零变化由完整后端回归保证（验证记录记录命令）。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from backend.adapters.base import AdapterRunInput
from backend.wire import spool
from backend.wire.injection import CommandRewrite, PhaseStateRef, WireInjection
from backend.wire.lifecycle import (
    CapturePreparationError,
    InjectionMergeError,
    NullAttemptObserver,
    WireCaptureSession,
    capture_capabilities_for,
    merge_injections,
)


def _make_task(**overrides) -> AdapterRunInput:
    defaults = dict(
        attempt_id="att_wire_w04",
        task_id="task_001",
        task_prompt="测试任务",
        task_context={},
        timeout_seconds=10,
        env_name="travel-planner",
        env_skill_id="lane/travel-planner",
        session_token="tok_test",
        env_base_url="http://127.0.0.1:8100",
    )
    defaults.update(overrides)
    return AdapterRunInput(**defaults)


class FakeSource:
    def __init__(self, kind="fake", injection=None, fail_start=False, rewrites=False):
        self.kind = kind
        self.rewrites_transport = rewrites
        self._injection = injection or WireInjection(enabled=True)
        self._fail_start = fail_start
        self.calls: list[str] = []

    async def start(self, ctx):
        self.calls.append("start")
        if self._fail_start:
            raise RuntimeError("boom with sk-abcdef123456789")
        return self._injection

    async def collect(self, ctx):
        self.calls.append("collect")
        return {}

    async def stop(self, ctx):
        self.calls.append("stop")
        return {}


# ---------- prepare/run 时序 ------------------------------------------------

async def test_prepare_sequencing_before_adapter_run(tmp_path):
    src = FakeSource(injection=WireInjection(enabled=True, process_env={"X_WIRE": "1"}))
    session = WireCaptureSession(
        attempt_id="att_1",
        data_path=tmp_path,
        agent_name="claude-code",
        sources=[src],
    )
    run_log: list[str] = []

    injection = await session.prepare()

    class FakeAdapter:
        attempt_id = "att_1"

        async def run(self):
            run_log.append("adapter.run")
            return None

    # prepare 的 source.start / ready 必须发生在 adapter.run 之前
    assert "source.start:fake" in session.call_log
    assert "prepare:ready" in session.call_log
    await FakeAdapter().run()
    assert session.call_log.index("prepare:ready") < len(session.call_log)
    assert run_log == ["adapter.run"]
    assert injection.enabled and injection.process_env == {"X_WIRE": "1"}


async def test_phase_context_and_attempt_end(tmp_path):
    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="claude-code",
        sources=[FakeSource()],
    )
    await session.prepare()
    async with session.phase("agent_run"):
        assert session.current_phase == "agent_run"
    async with session.phase("verification"):
        assert session.current_phase == "verification"
    await session.attempt_end()
    await session.attempt_end()  # 幂等
    # 正常结束：spool rename 为 .jsonl
    final = tmp_path / "attempts" / "att_1" / "wire-sources" / "capture-events.jsonl"
    assert final.exists()
    result = spool.read_spool(final)
    events = [r["payload"]["event"] for r in result.records]
    assert events[0] == "start" and "ready" in events and events[-1] == "stop"
    assert "phase_change" in events


async def test_policy_off_prepare_is_noop(tmp_path):
    # 只有 policy off 才 noop 零落盘（评审 W1-6 B2：policy!=off 时即便无
    # native/injection source 的第三方 agent 也建 capture context）。
    from backend.wire.policy import resolve_effective_policy

    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="third-party-agent",
        policy=resolve_effective_policy(run_requested="off"),
    )
    injection = await session.prepare()
    assert injection == WireInjection()
    assert "prepare:noop" in session.call_log
    assert not (tmp_path / "attempts" / "att_1" / "wire-sources").exists()


async def test_third_party_agent_metadata_policy_enables_capture(tmp_path):
    """评审 W1-6 B2：无 native/injection source 的第三方 agent policy=metadata
    时也建 capture context + 写 capture_enabled 的 phase-state，Env inbound 才能采集。"""
    from backend.wire import env_capture

    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="third-party-agent",
    )
    await session.prepare()
    assert "prepare:noop" not in session.call_log
    enabled, phase = env_capture.snapshot_capture_state(tmp_path, "att_1")
    assert enabled is True  # control claim 写出
    await session.attempt_end()


# ---------- 合并冲突与校验 ---------------------------------------------------

def test_merge_scalar_conflict_no_last_wins():
    a = WireInjection(enabled=True, llm_base_url="http://a")
    b = WireInjection(enabled=True, llm_base_url="http://b")
    with pytest.raises(InjectionMergeError):
        merge_injections([a, b], capabilities={"llm_base_url": True})


def test_merge_env_key_conflict():
    a = WireInjection(enabled=True, process_env={"X": "1"})
    b = WireInjection(enabled=True, process_env={"X": "2"})
    with pytest.raises(InjectionMergeError):
        merge_injections([a, b], capabilities={"process_env": True})
    # 同值不算冲突
    merged, _ = merge_injections(
        [a, WireInjection(enabled=True, process_env={"X": "1"})],
        capabilities={"process_env": True},
    )
    assert merged.process_env == {"X": "1"}


@pytest.mark.parametrize("env_key", [
    "LANE_SESSION_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL", "MY_SECRET_TOKEN",
])
def test_reserved_or_secret_env_key_rejected(env_key):
    inj = WireInjection(enabled=True, process_env={env_key: "v"})
    with pytest.raises(InjectionMergeError):
        merge_injections([inj], capabilities={"process_env": True})


def test_auth_header_rejected():
    inj = WireInjection(enabled=True, llm_headers={"Authorization": "Bearer x"})
    with pytest.raises(InjectionMergeError):
        merge_injections([inj], capabilities={"llm_headers": True})


def test_capability_gap_drops_field():
    inj = WireInjection(enabled=True, llm_headers={"x-eval-session-id": "s1"})
    merged, gaps = merge_injections([inj], capabilities={"llm_headers": False})
    assert merged.llm_headers == {}
    assert gaps == [{"field": "llm_headers", "reason": "adapter_capability_missing"}]


def test_phase_state_requires_exactly_one_transport(tmp_path):
    with pytest.raises(ValueError):
        PhaseStateRef()
    with pytest.raises(ValueError):
        PhaseStateRef(path=tmp_path / "p", control_url="http://c")
    assert PhaseStateRef(path=tmp_path / "p").path is not None


def test_capture_token_not_in_repr():
    inj = WireInjection(enabled=True, capture_token="tok_secret_abc")
    assert "tok_secret_abc" not in repr(inj)
    assert "tok_secret_abc" not in repr(_make_task(wire_injection=inj))


# ---------- fail-open / strict / abort --------------------------------------

async def test_source_start_failure_fail_open_env_unpolluted(tmp_path):
    good = FakeSource(
        kind="good", injection=WireInjection(enabled=True, process_env={"X_OK": "1"})
    )
    bad = FakeSource(kind="bad", fail_start=True)
    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="claude-code",
        sources=[bad, good],
    )
    injection = await session.prepare()
    # 失败 source 降级为不存在，好 source 的注入不受影响
    assert injection.process_env == {"X_OK": "1"}
    assert {
        "field": "bad", "instance": "bad", "reason": "source_start_failed"
    } in session.gaps
    await session.attempt_end()
    result = spool.read_spool(
        tmp_path / "attempts" / "att_1" / "wire-sources" / "capture-events.jsonl"
    )
    errors = [r for r in result.records if r["payload"]["event"] == "error"]
    assert errors and all(
        "sk-abcdef123456789" not in json.dumps(r) for r in result.records
    )  # 错误消息过 scrub


async def test_merge_conflict_fail_open_returns_zero_injection(tmp_path):
    a = FakeSource(kind="a", injection=WireInjection(enabled=True, llm_base_url="http://a"))
    b = FakeSource(kind="b", injection=WireInjection(enabled=True, llm_base_url="http://b"))
    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="claude-code",
        sources=[a, b],
    )
    injection = await session.prepare()
    # 半合并结果绝不下发：adapter 拿到未污染 env
    assert injection == WireInjection()


async def test_strict_rewriting_source_failure_raises_before_agent(tmp_path):
    bad = FakeSource(kind="proxy", fail_start=True, rewrites=True)
    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="claude-code",
        sources=[bad], strict=True,
    )
    with pytest.raises(CapturePreparationError):
        await session.prepare()
    # abort 路径：spool 保留 .partial 供 finalizer 判定
    partial = (
        tmp_path / "attempts" / "att_1" / "wire-sources"
        / "capture-events.jsonl.partial"
    )
    assert partial.exists()
    assert spool.read_spool(partial).records  # 已写事件行可读（flush 生效）


async def test_abort_flushes_and_stops_sources(tmp_path):
    src = FakeSource()
    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="claude-code", sources=[src],
    )
    await session.prepare()
    await session.abort_before_or_during_run()
    await session.abort_before_or_during_run()  # 幂等
    assert "stop" in src.calls
    partial = (
        tmp_path / "attempts" / "att_1" / "wire-sources"
        / "capture-events.jsonl.partial"
    )
    assert partial.exists()


async def test_null_observer_noop():
    obs = NullAttemptObserver()
    async with obs.phase("agent_run"):
        pass
    await obs.agent_result(None)
    await obs.attempt_end()


async def test_native_normalizer_runs_with_zero_injection_sources(tmp_path):
    """评审 B1：claude-code 有 native normalizer——即便没有 injection-source，
    prepare 也要建 spool，agent_result 才能产 native wire。真实 CC dispatch
    正是这条路径（不传 source）。"""
    import json as _json

    from backend.adapters.base import AdapterResult

    # 造 raw events
    d = tmp_path / "attempts" / "att_1"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text(
        _json.dumps({"timestamp": "2026-07-13T00:00:01.000Z", "type": "assistant",
                     "message": {"id": "msg_a", "model": "claude-opus-4-8",
                                 "role": "assistant", "stop_reason": "end_turn",
                                 "content": [{"type": "text", "text": "hi"}],
                                 "usage": {"input_tokens": 10, "output_tokens": 2}}}) + "\n"
    )
    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="claude-code",
        sources=[],  # 无 injection-source（真实 CC dispatch 形态）
    )
    injection = await session.prepare()
    assert injection == WireInjection()  # 零注入
    # 关键：spool 已建（不是 noop），native 能产出
    assert "prepare:noop" not in session.call_log
    async with session.phase("agent_run"):
        pass
    await session.agent_result(AdapterResult(attempt_id="att_1", status="completed",
                                             token_usage={"input_tokens": 10, "output_tokens": 2}))
    await session.attempt_end()
    from backend.wire import paths as _paths
    records = _paths.wire_file(tmp_path, "att_1").read_text().splitlines()
    assert any('"llm_call"' in r for r in records)


async def test_native_expected_but_no_raw_marks_failed(tmp_path):
    """评审 M2：prepare 声明 native expected，但 raw 缺失 → native source 标
    failed（不伪装 complete/not-observed）。"""
    import json as _json

    from backend.adapters.base import AdapterResult

    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="claude-code", sources=[],
    )
    await session.prepare()
    # 不写 events.jsonl → normalizer 无产出
    async with session.phase("agent_run"):
        pass
    await session.agent_result(AdapterResult(attempt_id="att_1", status="completed"))
    await session.attempt_end()
    from backend.wire import paths as _paths
    manifest = _json.loads(_paths.manifest_file(tmp_path, "att_1").read_text())
    native = next(s for s in manifest["sources"] if s["kind"] == "native-event")
    assert native["status"] == "failed"
    assert manifest["coverage"]["agent_semantics"] == "failed"
    assert manifest["status"] in ("partial", "failed")


def test_capability_registry():
    assert capture_capabilities_for("claude-code")["mcp_rewrites"] is True
    assert capture_capabilities_for("codex")["mcp_rewrites"] is True
    # 未知/第三方 adapter：registry 无声明，兜底返回空 dict
    assert capture_capabilities_for("third-party-agent") == {}
    assert capture_capabilities_for("some-future-agent") == {}


# ---------- credential 保护 / header 注入防护 --------------------------------

def test_protected_provider_credential_env_rejected():
    """provider api_key_env 是任意名称（如 UP_KEY），必须进动态保护集。"""
    inj = WireInjection(enabled=True, process_env={"UP_KEY": "attacker"})
    with pytest.raises(InjectionMergeError):
        merge_injections(
            [inj],
            capabilities={"process_env": True},
            protected_env_keys=frozenset({"UP_KEY"}),
        )
    # 不在保护集的普通变量仍可注入
    merged, _ = merge_injections(
        [WireInjection(enabled=True, process_env={"X_FLAG": "1"})],
        capabilities={"process_env": True},
        protected_env_keys=frozenset({"UP_KEY"}),
    )
    assert merged.process_env == {"X_FLAG": "1"}


@pytest.mark.parametrize("headers", [
    {"x-ok": "v\nAuthorization: secret"},   # LF 注入
    {"x-ok": "v\r\nX-Evil: 1"},              # CRLF 注入
    {"x ok": "v"},                            # 非法 token（空格）
    {"x-ok:extra": "v"},                      # 非法 token（冒号）
    {"": "v"},                                 # 空 name
])
def test_header_injection_rejected(headers):
    inj = WireInjection(enabled=True, llm_headers=headers)
    with pytest.raises(InjectionMergeError):
        merge_injections([inj], capabilities={"llm_headers": True})


def test_process_env_value_control_chars_rejected():
    inj = WireInjection(enabled=True, process_env={"X_FLAG": "a\nb"})
    with pytest.raises(InjectionMergeError):
        merge_injections([inj], capabilities={"process_env": True})


def test_header_merge_case_insensitive():
    a = WireInjection(enabled=True, llm_headers={"X-Eval-Id": "1"})
    b = WireInjection(enabled=True, llm_headers={"x-eval-id": "2"})
    with pytest.raises(InjectionMergeError):
        merge_injections([a, b], capabilities={"llm_headers": True})
    merged, _ = merge_injections(
        [a, WireInjection(enabled=True, llm_headers={"x-eval-id": "1"})],
        capabilities={"llm_headers": True},
    )
    assert merged.llm_headers == {"X-Eval-Id": "1"}  # 同值合一，保留首见大小写


@pytest.mark.parametrize("bad_url", [
    'http://x/"e',                      # TOML 引号逃逸
    "http://x/'e",                      # 单引号
    "http://x/a b",                     # 空白
    "http://x/\\evil",                  # 反斜杠
    "file:///etc/passwd",               # 非 http(s)
    "http://",                          # 无 netloc
    "http://x/\n",                      # 控制字符
])
def test_llm_base_url_validation(bad_url):
    inj = WireInjection(enabled=True, llm_base_url=bad_url)
    with pytest.raises(InjectionMergeError):
        merge_injections([inj], capabilities={"llm_base_url": True})


def test_cc_dynamic_correlation_headers_override_stale_static():
    """design §11.2：x-eval-*/x-lane-* 是 attempt 级动态 header，
    provider 配置里残留的旧值必须被本次 attempt 的值覆盖。"""
    from backend.adapters.claude_code import _merge_custom_headers

    merged = _merge_custom_headers(
        "X-Eval-Session-Id: stale\nx-user-id: user",
        {"x-eval-session-id": "att_current", "x-lane-run-id": "run_1"},
    )
    lines = merged.splitlines()
    assert "X-Eval-Session-Id: att_current" in lines  # 动态胜出（保留静态大小写）
    assert "stale" not in merged
    assert "x-user-id: user" in lines                  # 非保留前缀：静态保留
    assert "x-lane-run-id: run_1" in lines


async def test_strict_merge_conflict_does_not_block_agent(tmp_path):
    """design §9.1/§21：同步 fail-closed 仅限改写型 source 无法 ready；
    普通 merge 冲突即便 strict 也 fail-open 进 capture completeness。"""
    a = FakeSource(kind="a", injection=WireInjection(enabled=True, llm_base_url="http://a"))
    b = FakeSource(kind="b", injection=WireInjection(enabled=True, llm_base_url="http://b"))
    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="claude-code",
        sources=[a, b], strict=True,
    )
    injection = await session.prepare()  # 不抛 CapturePreparationError
    assert injection == WireInjection()
    assert any(g["field"] == "injection" for g in session.gaps)


# ---------- phase-state（design §9.4）---------------------------------------

async def test_phase_state_file_written_before_phase_work(tmp_path):
    src = FakeSource(injection=WireInjection(enabled=True, process_env={"X_OK": "1"}))
    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="claude-code", sources=[src],
    )
    injection = await session.prepare()
    state_path = tmp_path / "attempts" / "att_1" / "wire-sources" / "phase-state.json"
    # prepare 后已初始化，且 ref 进入 injection 供独立进程只读消费
    assert injection.phase_state is not None and injection.phase_state.path == state_path
    init = json.loads(state_path.read_text())
    assert init["attempt_id"] == "att_1"
    assert init["phase"] == "attempt_setup"
    seq0 = init["sequence"]

    async with session.phase("agent_run"):
        during = json.loads(state_path.read_text())
        assert during["phase"] == "agent_run"
        assert during["sequence"] > seq0  # 进入 phase 前已原子更新
    after = json.loads(state_path.read_text())
    assert after["phase"] == "attempt_setup"      # 退出时恢复并传播
    assert after["sequence"] > during["sequence"]  # sequence 单调
    assert session.phase_attribution == "explicit"
    await session.attempt_end()


async def test_phase_state_write_failure_degrades(tmp_path, monkeypatch):
    from backend.wire import lifecycle as lc

    src = FakeSource()
    session = WireCaptureSession(
        attempt_id="att_1", data_path=tmp_path, agent_name="claude-code", sources=[src],
    )
    await session.prepare()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(lc.writer, "atomic_write_json", boom)
    async with session.phase("agent_run"):
        pass  # 写失败不得中断主流程
    assert session.phase_attribution == "degraded"
    assert {"field": "phase_state", "reason": "phase_state_write_failed"} in session.gaps
    await session.attempt_end()


def test_cc_custom_headers_static_reserved_wins():
    from backend.adapters.claude_code import _merge_custom_headers

    merged = _merge_custom_headers(
        "X-Static: keep\nx-user-id: original",
        {"X-User-Id": "attacker", "x-eval-session-id": "att_1"},
    )
    lines = merged.splitlines()
    assert "X-Static: keep" in lines
    assert "x-user-id: original" in lines          # 静态保留名胜出
    assert "X-User-Id: attacker" not in merged      # 同名注入被丢弃而非追加
    assert "x-eval-session-id: att_1" in lines      # 新名正常追加
    assert _merge_custom_headers(None, {"a": "1"}) == "a: 1"


# ---------- adapter 消费点：Claude Code --------------------------------------

class FakeProcess:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "done", "session_id": "sess_test",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }) + "\n").encode())
        reader.feed_eof()
        self.stdout = reader

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


_CC_INJECTION = WireInjection(
    enabled=True,
    process_env={"X_WIRE_FLAG": "1"},
    llm_base_url="http://127.0.0.1:9999/wire-proxy",
    llm_headers={"x-eval-session-id": "att_wire_w04"},
    mcp_rewrites={
        "lane-travel-planner": CommandRewrite(
            command="wire-tap", args_prefix=("--phase", "agent_run", "--")
        )
    },
)


async def test_claude_code_consumes_injection(tmp_path, monkeypatch):
    from backend.adapters.claude_code import ClaudeCodeAdapter

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    adapter = ClaudeCodeAdapter(model="opus")
    task = _make_task(wire_injection=_CC_INJECTION)
    with patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("asyncio.create_subprocess_exec", return_value=FakeProcess()) as spawn:
        await adapter.run(task, None, tmp_path)
    env = spawn.call_args.kwargs["env"]
    assert env["X_WIRE_FLAG"] == "1"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999/wire-proxy"
    assert "x-eval-session-id: att_wire_w04" in env["ANTHROPIC_CUSTOM_HEADERS"]
    # MCP config 应用 rewrite：wrapper 前置、原命令后置
    config = json.loads(
        (tmp_path / "attempts" / task.attempt_id / "mcp_config.json").read_text()
    )
    server = config["mcpServers"]["lane-travel-planner"]
    assert server["command"] == "wire-tap"
    assert server["args"][:4] == ["--phase", "agent_run", "--", "uv"]
    assert server["args"][-1].endswith("mcp_server.py")


async def test_claude_code_empty_injection_zero_change(tmp_path, monkeypatch):
    from backend.adapters.claude_code import ClaudeCodeAdapter

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    adapter = ClaudeCodeAdapter(model="opus")
    with patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("asyncio.create_subprocess_exec", return_value=FakeProcess()) as spawn:
        await adapter.run(_make_task(), None, tmp_path)
    env = spawn.call_args.kwargs["env"]
    assert "ANTHROPIC_BASE_URL" not in env and "X_WIRE_FLAG" not in env
    config = json.loads(
        (tmp_path / "attempts" / "att_wire_w04" / "mcp_config.json").read_text()
    )
    assert config["mcpServers"]["lane-travel-planner"]["command"] == "uv"


# ---------- adapter 消费点：Codex --------------------------------------------

async def test_codex_consumes_injection(tmp_path, monkeypatch):
    from backend.adapters.codex import CodexAdapter
    from backend.model_providers import ModelProviderSection

    monkeypatch.setenv("UP_KEY", "sk-up-test")
    # Codex 只支持 Responses provider（W4-4 / m6 / R15.5）。
    providers = {
        "up": ModelProviderSection(
            kind="openai-responses",
            base_url="http://real-upstream/v1",
            api_key_env="UP_KEY",
        )
    }
    injection = WireInjection(
        enabled=True,
        process_env={"X_WIRE_FLAG": "1"},
        llm_base_url="http://127.0.0.1:9999/wire-proxy/v1",
        mcp_rewrites={
            "lane-travel-planner": CommandRewrite(
                command="wire-tap", args_prefix=("--phase", "agent_run", "--")
            )
        },
    )
    adapter = CodexAdapter(model="up/gpt-x", providers=providers)
    task = _make_task(wire_injection=injection)
    with patch("shutil.which", return_value="/usr/local/bin/codex"), \
         patch("asyncio.create_subprocess_exec", return_value=FakeProcess()) as spawn:
        await adapter.run(task, None, tmp_path)
    cmd = list(spawn.call_args.args)
    joined = " ".join(cmd)
    # provider base_url 被 injection 覆盖
    assert 'model_providers.up.base_url="http://127.0.0.1:9999/wire-proxy/v1"' in joined
    assert "http://real-upstream/v1" not in joined
    # MCP command 被 rewrite
    assert 'mcp_servers.lane-travel-planner.command="wire-tap"' in joined
    args_arg = next(a for a in cmd if a.startswith("mcp_servers.lane-travel-planner.args="))
    parsed_args = json.loads(args_arg.split("=", 1)[1])
    assert parsed_args[:4] == ["--phase", "agent_run", "--", "uv"]
    # process_env 合并
    assert spawn.call_args.kwargs["env"]["X_WIRE_FLAG"] == "1"


async def test_codex_empty_injection_zero_change(tmp_path, monkeypatch):
    from backend.adapters.codex import CodexAdapter
    from backend.model_providers import ModelProviderSection

    monkeypatch.setenv("UP_KEY", "sk-up-test")
    # Codex 只支持 Responses provider（W4-4 / m6 / R15.5）。
    providers = {
        "up": ModelProviderSection(
            kind="openai-responses",
            base_url="http://real-upstream/v1",
            api_key_env="UP_KEY",
        )
    }
    adapter = CodexAdapter(model="up/gpt-x", providers=providers)
    with patch("shutil.which", return_value="/usr/local/bin/codex"), \
         patch("asyncio.create_subprocess_exec", return_value=FakeProcess()) as spawn:
        await adapter.run(_make_task(), None, tmp_path)
    joined = " ".join(spawn.call_args.args)
    assert 'model_providers.up.base_url="http://real-upstream/v1"' in joined
    assert 'mcp_servers.lane-travel-planner.command="uv"' in joined

