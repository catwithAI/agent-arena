"""C0-2: conversation.jsonl writer/reader/summary。"""

import json

from backend.adapters.base import AdapterResult, ConversationTurn
from backend.conversation.summary import (
    read_conversation_events,
    summarize_conversation,
)
from backend.conversation.writer import (
    CONVERSATION_FILENAME,
    ConversationTraceWriter,
)


def _turn(index: int, *, turn_id: str | None = None, prompt: str = "做任务") -> ConversationTurn:
    return ConversationTurn(
        turn_id=turn_id or f"t{index}",
        turn_index=index,
        prompt=prompt,
        purpose="task",
    )


def _write_three_turn_trace(attempt_dir, *, session_ids=("sess-1", "sess-1", "sess-1")):
    path = attempt_dir / CONVERSATION_FILENAME
    with ConversationTraceWriter(path, attempt_id="att_1") as w:
        w.conversation_started(turn_count=3, is_legacy=False)
        for i, sid in enumerate(session_ids):
            turn = _turn(i)
            w.turn_started(turn, producer_session_id=sid)
            w.turn_completed(turn, producer_session_id=sid)
        w.conversation_completed()
    return path


# ---------- 事件 round-trip ------------------------------------------------


def test_events_round_trip(tmp_path):
    path = _write_three_turn_trace(tmp_path)
    events, partial = read_conversation_events(path)

    assert not partial
    kinds = [e["event"] for e in events]
    assert kinds[0] == "conversation.started"
    assert kinds[-1] == "conversation.completed"
    assert kinds.count("turn.started") == 3
    assert kinds.count("turn.completed") == 3

    for e in events:
        assert e["schema_version"] == "lane-conversation-v1"
        assert e["attempt_id"] == "att_1"
        assert e["timestamp"]


def test_turn_record_minimum_fields(tmp_path):
    """R4.3：turn record 至少含 attempt_id/turn_id/turn_index/purpose/时间/session/状态。"""
    path = _write_three_turn_trace(tmp_path)
    events, _ = read_conversation_events(path)
    completed = [e for e in events if e["event"] == "turn.completed"]
    for e in completed:
        for key in ("attempt_id", "turn_id", "turn_index", "purpose",
                    "timestamp", "producer_session_id", "status"):
            assert key in e, f"turn.completed 缺少 {key}"


# ---------- 敏感 prompt 不落盘（R9.1） -------------------------------------


def test_prompt_never_written_verbatim(tmp_path):
    secret_prompt = "机密任务：口令是 SWORDFISH-42"
    path = tmp_path / CONVERSATION_FILENAME
    with ConversationTraceWriter(path, attempt_id="att_1") as w:
        turn = _turn(0, prompt=secret_prompt)
        w.turn_started(turn, producer_session_id="sess-1")

    raw = path.read_text(encoding="utf-8")
    assert "SWORDFISH-42" not in raw
    record = json.loads(raw.strip())
    assert record["prompt_bytes"] == len(secret_prompt.encode("utf-8"))
    assert record["prompt_hash"].startswith("sha256:")


def test_answer_interaction_turn_has_no_prompt_digest(tmp_path):
    from backend.adapters.base import InteractionWaitFor

    turn = ConversationTurn(
        turn_id="choose", turn_index=1, action="answer_interaction",
        purpose="interaction",
        wait_for=InteractionWaitFor(tool_name="builtin:AskUserQuestion"),
        answer={"option_id": "balanced"},
    )
    path = tmp_path / CONVERSATION_FILENAME
    with ConversationTraceWriter(path, attempt_id="att_1") as w:
        w.turn_started(turn, producer_session_id="sess-1")
        w.interaction_answered(
            turn, producer_session_id="sess-1",
            tool_name="builtin:AskUserQuestion",
        )

    events, _ = read_conversation_events(path)
    assert "prompt_bytes" not in events[0]
    answered = events[1]
    assert answered["event"] == "turn.interaction_answered"
    assert answered["tool_name"] == "builtin:AskUserQuestion"
    # answer 内容不进 trace
    assert "balanced" not in path.read_text(encoding="utf-8")


# ---------- 崩溃截断 fail-open（R4.5） -------------------------------------


def test_truncated_tail_line_is_partial(tmp_path):
    path = _write_three_turn_trace(tmp_path)
    with path.open("a", encoding="utf-8") as fp:
        fp.write('{"schema_version":"lane-conversation-v1","event":"turn.sta')

    events, partial = read_conversation_events(path)
    assert partial
    assert [e["event"] for e in events][-1] == "conversation.completed"

    summary = summarize_conversation(tmp_path)
    assert summary["partial"] is True
    assert summary["completed_turn_count"] == 3


# ---------- summary 投影 ---------------------------------------------------


def test_summary_continuous_session(tmp_path):
    _write_three_turn_trace(tmp_path)
    summary = summarize_conversation(tmp_path)
    assert summary["is_legacy"] is False
    assert summary["turn_count"] == 3
    assert summary["completed_turn_count"] == 3
    assert summary["last_completed_turn_index"] == 2
    assert summary["producer_session_id"] == "sess-1"
    assert summary["session_continuity"] == "continuous"


def test_summary_broken_session(tmp_path):
    """R2.3：多个 producer_session_id → continuity broken。"""
    _write_three_turn_trace(tmp_path, session_ids=("sess-1", "sess-1", "sess-2"))
    summary = summarize_conversation(tmp_path)
    assert summary["session_continuity"] == "broken"


def test_summary_counts_failed_turn(tmp_path):
    path = tmp_path / CONVERSATION_FILENAME
    with ConversationTraceWriter(path, attempt_id="att_1") as w:
        w.conversation_started(turn_count=2, is_legacy=False)
        t0, t1 = _turn(0), _turn(1)
        w.turn_started(t0, producer_session_id="sess-1")
        w.turn_completed(t0, producer_session_id="sess-1")
        w.turn_started(t1, producer_session_id="sess-1")
        w.turn_failed(
            t1, producer_session_id="sess-1",
            error_code="turn_timeout", error_summary="turn 超时",
        )
        w.conversation_failed(error_code="turn_timeout", error_summary="turn 超时")

    summary = summarize_conversation(tmp_path)
    assert summary["completed_turn_count"] == 1
    assert summary["failed_turn_count"] == 1
    assert summary["last_completed_turn_index"] == 0


# ---------- legacy reader（R11.2） -----------------------------------------


def test_answered_interaction_counts_as_completed_turn(tmp_path):
    """被成功应答的 interaction turn 计入 completed（R1.1 的 turn 模型）。

    回归点：`turn_count` 来自 plan（含 interaction turns），若只数
    turn.completed，setup+interaction+probe 的成功场景会显示 2/3——完整跑完
    的 conversation 看起来没跑完。interaction 若是最后一个逻辑 turn，
    last_completed_turn_index 也会偏小。
    """
    from backend.adapters.base import InteractionWaitFor

    setup = _turn(0, turn_id="setup")
    choose = ConversationTurn(
        turn_id="choose", turn_index=1, action="answer_interaction",
        purpose="interaction",
        wait_for=InteractionWaitFor(tool_name="builtin:AskUserQuestion"),
        answer={"option_label": "A"},
    )
    probe = _turn(2, turn_id="probe")

    path = tmp_path / CONVERSATION_FILENAME
    with ConversationTraceWriter(path, attempt_id="att_1") as w:
        w.conversation_started(turn_count=3, is_legacy=False)
        w.turn_started(setup, producer_session_id="s1")
        w.turn_completed(setup, producer_session_id="s1")
        # interaction turn 不占 send_message 序号：只有 interaction_answered，
        # 没有配对的 turn.started/turn.completed
        w.interaction_answered(
            choose, producer_session_id="s1",
            tool_name="builtin:AskUserQuestion",
        )
        w.turn_started(probe, producer_session_id="s1")
        w.turn_completed(probe, producer_session_id="s1")
        w.conversation_completed()

    summary = summarize_conversation(tmp_path)
    assert summary["turn_count"] == 3
    assert summary["completed_turn_count"] == 3, (
        "被应答的 interaction turn 必须计入完成轮数"
    )
    assert summary["last_completed_turn_index"] == 2


def test_interaction_as_last_turn_sets_last_completed_index(tmp_path):
    """interaction 是最后一个逻辑 turn 时，last_completed_turn_index 要准确。"""
    from backend.adapters.base import InteractionWaitFor

    setup = _turn(0, turn_id="setup")
    choose = ConversationTurn(
        turn_id="choose", turn_index=1, action="answer_interaction",
        purpose="interaction",
        wait_for=InteractionWaitFor(tool_name="builtin:AskUserQuestion"),
        answer={"option_label": "A"},
    )
    path = tmp_path / CONVERSATION_FILENAME
    with ConversationTraceWriter(path, attempt_id="att_1") as w:
        w.conversation_started(turn_count=2, is_legacy=False)
        w.turn_started(setup, producer_session_id="s1")
        w.turn_completed(setup, producer_session_id="s1")
        w.interaction_answered(
            choose, producer_session_id="s1",
            tool_name="builtin:AskUserQuestion",
        )
        w.conversation_completed()

    summary = summarize_conversation(tmp_path)
    assert summary["completed_turn_count"] == 2
    assert summary["last_completed_turn_index"] == 1


def test_score_turn_id_projected_from_plan(tmp_path):
    """score_turn_id 由 plan 经 conversation.started 传入，不再恒为 None。"""
    path = tmp_path / CONVERSATION_FILENAME
    with ConversationTraceWriter(path, attempt_id="att_1") as w:
        w.conversation_started(
            turn_count=2, is_legacy=False, score_turn_id="probe",
        )
        for t in (_turn(0, turn_id="setup"), _turn(1, turn_id="probe")):
            w.turn_started(t, producer_session_id="s1")
            w.turn_completed(t, producer_session_id="s1")
        w.conversation_completed()

    summary = summarize_conversation(tmp_path)
    assert summary["score_turn_id"] == "probe"


def test_missing_file_yields_legacy_summary(tmp_path):
    summary = summarize_conversation(tmp_path)
    assert summary["is_legacy"] is True
    assert summary["turn_count"] == 1
    assert summary["session_continuity"] == "unknown"
    # 完成情况以 attempts 表为权威，这里不猜
    assert summary["completed_turn_count"] is None


def test_adapter_result_conversation_summary_defaults_empty():
    result = AdapterResult(attempt_id="att_1", status="completed")
    assert result.conversation_summary == {}
