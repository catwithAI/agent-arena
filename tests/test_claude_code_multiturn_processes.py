"""C2-4: CC 多轮的进程组清理与 timeout 边界。

对应 spec R3.2.5（每轮 subprocess 独立进程组、异常/取消时终止整个进程组）、
R8.1/R8.2（总预算跨轮共享、轮间耗尽不再启动新进程）、R8.3（已完成轮的
trace/usage 保留，conversation 标失败）。

既有 `test_process_tree_kill.py` 覆盖单轮 cancel；这里补多轮特有的场景：
cancel 落在第二轮、超时后不再 spawn 后续轮、进程组隔离。
"""

import asyncio
import contextlib
import json
import sys

from backend.adapters.base import AdapterRunInput, ConversationTurn
from backend.adapters.claude_code import ClaudeCodeAdapter
from backend.conversation.writer import CONVERSATION_FILENAME

THREE_TURNS = (
    ConversationTurn(turn_id="setup", turn_index=0, purpose="setup", prompt="一"),
    ConversationTurn(turn_id="pressure", turn_index=1, purpose="pressure", prompt="二"),
    ConversationTurn(
        turn_id="probe", turn_index=2, purpose="probe", prompt="三", score_after=True,
    ),
)


def _task(**overrides) -> AdapterRunInput:
    defaults = dict(
        attempt_id="att_cc_proc",
        task_id="task",
        task_prompt="一",
        task_context={},
        timeout_seconds=30,
        env_name="env",
        env_skill_id="lane/env",
        session_token="tok",
        env_base_url="http://lane.test",
        conversation_turns=THREE_TURNS,
    )
    defaults.update(overrides)
    return AdapterRunInput(**defaults)


def _ok_stream() -> str:
    """一轮 stream-json：init + result，立即结束。"""
    return "\n".join([
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": "s", "model": "m"}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "session_id": "s",
                    "usage": {"input_tokens": 10, "output_tokens": 1}}),
    ])


def _conversation(tmp_path, attempt_id) -> list[dict]:
    path = tmp_path / "attempts" / attempt_id / CONVERSATION_FILENAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------- R3.2.5：cancel 落在第二轮 --------------------------------------


async def test_cancel_during_second_turn_kills_that_turns_process(
    tmp_path, monkeypatch
):
    """多轮场景下 cancel 必须杀掉**当前正在跑的那一轮**的进程组。

    单轮测试（test_process_tree_kill.py）覆盖不到这个：那里 proc 只有一个，
    这里第一轮已正常退出、第二轮在跑，被 cancel 的是第二轮。
    """
    monkeypatch.setattr(
        "shutil.which", lambda name: sys.executable if name == "claude" else None
    )
    adapter = ClaudeCodeAdapter(
        project_path=tmp_path, model="m", providers={},
    )

    real_exec = asyncio.create_subprocess_exec
    spawned: list = []

    async def fake_exec(*cmd, **kwargs):
        # 第一轮：打印一轮完整 stream 后立即退出；
        # 第二轮起：长睡眠，等着被 cancel 杀掉
        if not spawned:
            script = f"import sys; sys.stdout.write({_ok_stream()!r}); sys.stdout.write(chr(10))"
        else:
            script = "import time; time.sleep(60)"
        proc = await real_exec(
            sys.executable, "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    run_task = asyncio.create_task(adapter.run(_task(), None, tmp_path))
    # 等第一轮跑完、第二轮起来
    for _ in range(50):
        await asyncio.sleep(0.1)
        if len(spawned) >= 2:
            break
    assert len(spawned) >= 2, "第一轮应正常完成并进入第二轮"
    assert spawned[1].returncode is None, "第二轮进程应在运行中"

    run_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run_task

    await asyncio.wait_for(spawned[1].wait(), timeout=5)
    assert spawned[1].returncode is not None, "cancel 必须杀掉第二轮的进程组"


async def test_each_turn_gets_its_own_process_group(tmp_path, monkeypatch):
    """每轮 subprocess 必须 start_new_session=True（自成进程组）。

    否则 kill 只能杀 CLI 本体，它拉起的 MCP stdio server 会过继给 init
    继续烧 token。
    """
    monkeypatch.setattr(
        "shutil.which", lambda name: sys.executable if name == "claude" else None
    )
    adapter = ClaudeCodeAdapter(
        project_path=tmp_path, model="m", providers={},
    )
    real_exec = asyncio.create_subprocess_exec
    kwargs_seen: list[dict] = []

    async def fake_exec(*cmd, **kwargs):
        kwargs_seen.append(dict(kwargs))
        return await real_exec(
            sys.executable, "-c",
            f"import sys; sys.stdout.write({_ok_stream()!r}); sys.stdout.write(chr(10))",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await adapter.run(_task(), None, tmp_path)

    assert len(kwargs_seen) == 3, "三轮各起一个进程"
    for kw in kwargs_seen:
        assert kw.get("start_new_session") is True


# ---------- R8.2：轮间预算耗尽不再 spawn -----------------------------------


async def test_budget_exhausted_between_turns_spawns_no_more(
    tmp_path, monkeypatch
):
    """总预算在轮间耗尽 → 后续轮一个进程都不该起（R8.2）。

    这里测的是**轮与轮之间**的门禁：预算已尽时连 spawn 都不该发生，
    而不是起了再杀。首轮就吃光预算（睡 1.2s > 1s 总预算），第二轮的
    `check_before_turn()` 必须拦住。
    """
    monkeypatch.setattr(
        "shutil.which", lambda name: sys.executable if name == "claude" else None
    )
    adapter = ClaudeCodeAdapter(
        project_path=tmp_path, model="m", providers={},
    )
    real_exec = asyncio.create_subprocess_exec
    spawned: list = []

    async def fake_exec(*cmd, **kwargs):
        proc = await real_exec(
            sys.executable, "-c", "import time; time.sleep(1.2)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await adapter.run(_task(timeout_seconds=1), None, tmp_path)

    assert result.status != "completed"
    assert len(spawned) == 1, (
        f"首轮耗尽预算后不应再 spawn，实际起了 {len(spawned)}"
    )
    # 起过的进程都必须已退出，不留孤儿
    for proc in spawned:
        assert proc.returncode is not None


async def test_turn_timeout_leaves_no_orphan_process(tmp_path, monkeypatch):
    """轮内超时（wait_for 触发）同样必须杀干净进程组。

    与轮间门禁是两条路径：这里进程**已经起来**并卡住，靠 `_run_turn` 的
    finally 兜底 kill。
    """
    monkeypatch.setattr(
        "shutil.which", lambda name: sys.executable if name == "claude" else None
    )
    adapter = ClaudeCodeAdapter(
        project_path=tmp_path, model="m", providers={},
    )
    real_exec = asyncio.create_subprocess_exec
    spawned: list = []

    async def fake_exec(*cmd, **kwargs):
        # 长睡眠且不产出 stdout：wait_for 必然超时
        proc = await real_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await adapter.run(_task(timeout_seconds=1), None, tmp_path)

    assert result.status == "timeout", f"实际 {result.status}"
    assert len(spawned) == 1
    await asyncio.wait_for(spawned[0].wait(), timeout=5)
    assert spawned[0].returncode is not None, "轮内超时必须杀掉进程组"


# ---------- R8.3：失败时保留已完成轮的 trace -------------------------------


async def test_completed_turns_trace_survives_later_failure(
    tmp_path, monkeypatch
):
    """后续轮失败不得抹掉已完成轮的记录——conversation 标失败，
    但前面轮的 turn.completed 与 usage 要留着（排查/部分结果都需要）。"""
    monkeypatch.setattr(
        "shutil.which", lambda name: sys.executable if name == "claude" else None
    )
    adapter = ClaudeCodeAdapter(
        project_path=tmp_path, model="m", providers={},
    )
    real_exec = asyncio.create_subprocess_exec
    calls = {"n": 0}

    async def fake_exec(*cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            script = f"import sys; sys.stdout.write({_ok_stream()!r}); sys.stdout.write(chr(10))"
            return await real_exec(
                sys.executable, "-c", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        # 第二轮：非零退出、无 result
        return await real_exec(
            sys.executable, "-c", "import sys; sys.exit(3)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await adapter.run(_task(), None, tmp_path)

    assert result.status != "completed"
    assert calls["n"] == 2, "第二轮失败后不应继续第三轮"

    records = _conversation(tmp_path, result.attempt_id)
    kinds = [r["event"] for r in records]
    # 首轮的完成记录保留
    completed = [r for r in records if r["event"] == "turn.completed"]
    assert [r["turn_id"] for r in completed] == ["setup"]
    assert "turn.failed" in kinds
    assert kinds[-1] == "conversation.failed"
    # 已完成轮的 usage 也保留
    assert result.token_usage.get("input_tokens", 0) > 0
