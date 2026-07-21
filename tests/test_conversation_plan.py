"""C0-1: ConversationTurn 模型、plan 校验与 legacy 映射。"""

import pytest

from backend.adapters.base import (
    AdapterRunInput,
    ConversationTurn,
    InteractionWaitFor,
)
from backend.conversation.plan import (
    CONVERSATION_CONTEXT_KEY,
    ConversationPlanError,
    conversation_turns_from_context,
    effective_conversation,
    parse_conversation,
    validate_turns,
)


def _task(**overrides) -> AdapterRunInput:
    defaults = dict(
        attempt_id="att_1",
        task_id="task_1",
        task_prompt="做这个任务",
        task_context={},
        timeout_seconds=600,
        env_name="travel-planner",
        env_skill_id="lane/travel-planner",
        session_token="tok",
        env_base_url="http://127.0.0.1:8100",
    )
    defaults.update(overrides)
    return AdapterRunInput(**defaults)


# ---------- legacy 等价（R1.2） -------------------------------------------


def test_legacy_mapping_single_task_turn():
    plan = effective_conversation(_task())
    assert plan.is_legacy
    assert len(plan.turns) == 1
    turn = plan.turns[0]
    assert turn.action == "send_message"
    assert turn.purpose == "task"
    assert turn.prompt == "做这个任务"
    assert turn.score_after is True
    assert turn.turn_index == 0
    assert plan.score_turn is turn
    assert not plan.requires_interaction_answer


def test_legacy_turn_id_deterministic():
    a = effective_conversation(_task()).turns[0].turn_id
    b = effective_conversation(_task()).turns[0].turn_id
    assert a == b
    assert "task_1" in a


# ---------- 多轮解析（R1.1，含 answer_interaction） ------------------------


SPEC_EXAMPLE = [
    {"id": "setup", "purpose": "setup", "prompt": "读取材料并确认"},
    {
        "id": "choose",
        "action": "answer_interaction",
        "wait_for": {"tool_name": "builtin:AskUserQuestion", "question_key": "strategy"},
        "answer": {"option_id": "balanced"},
    },
    {"id": "pressure", "purpose": "pressure", "prompt": "处理这批 filler"},
    {"id": "probe", "purpose": "probe", "prompt": "早前的事实是什么？", "score_after": True},
]


def test_parse_spec_example_conversation():
    turns = parse_conversation(SPEC_EXAMPLE, task_id="task_1")
    assert [t.turn_id for t in turns] == ["setup", "choose", "pressure", "probe"]
    assert [t.turn_index for t in turns] == [0, 1, 2, 3]

    choose = turns[1]
    assert choose.action == "answer_interaction"
    assert choose.purpose == "interaction"  # 缺省 purpose 按 action 推导
    assert choose.wait_for == InteractionWaitFor(
        tool_name="builtin:AskUserQuestion", question_key="strategy"
    )
    assert choose.answer == {"option_id": "balanced"}

    plan = effective_conversation(_task(conversation_turns=turns))
    assert not plan.is_legacy
    assert [t.turn_id for t in plan.send_message_turns] == ["setup", "pressure", "probe"]
    assert [t.turn_id for t in plan.interaction_turns] == ["choose"]
    assert plan.requires_interaction_answer
    assert plan.score_turn.turn_id == "probe"


def test_score_turn_defaults_to_last_when_unset():
    turns = parse_conversation(
        [{"id": "a", "prompt": "one"}, {"id": "b", "prompt": "two"}],
        task_id="task_1",
    )
    plan = effective_conversation(_task(conversation_turns=turns))
    assert plan.score_turn.turn_id == "b"


def test_default_turn_id_generated_from_position():
    turns = parse_conversation([{"prompt": "hi"}], task_id="task_9")
    assert turns[0].turn_id == "task_9::t0"


# ---------- R1.5 拒绝规则 --------------------------------------------------


def test_empty_conversation_rejected():
    with pytest.raises(ConversationPlanError, match="不能为空"):
        parse_conversation([], task_id="task_1")


def test_duplicate_turn_id_rejected():
    with pytest.raises(ConversationPlanError, match="重复"):
        parse_conversation(
            [{"id": "x", "prompt": "a"}, {"id": "x", "prompt": "b"}],
            task_id="task_1",
        )


def test_explicit_turn_index_mismatch_rejected():
    with pytest.raises(ConversationPlanError, match="不一致"):
        parse_conversation(
            [{"id": "a", "prompt": "a", "turn_index": 0},
             {"id": "b", "prompt": "b", "turn_index": 5}],
            task_id="task_1",
        )


def test_validate_turns_detects_index_gap():
    turns = (
        ConversationTurn(turn_id="a", turn_index=0, prompt="a"),
        ConversationTurn(turn_id="b", turn_index=2, prompt="b"),
    )
    with pytest.raises(ConversationPlanError, match="断裂"):
        validate_turns(turns)


def test_score_after_must_be_on_last_turn():
    """score_after 必须落在最后一轮（R1.4）。

    回归点：`score_after` 原本没有任何执行语义——adapter 跑完全部 turns、
    runner 在终态后统一评分一次。于是 `t0(score_after) → t1` 这种计划实际
    在 t1 后评分，声明与行为不符，scorer 还会看到本不该看到的后续轮产物。
    当前不支持中间评分点，校验期拒绝比运行期静默偏移安全。
    """
    with pytest.raises(ConversationPlanError, match="必须位于最后一轮"):
        parse_conversation(
            [{"id": "probe", "prompt": "问", "score_after": True},
             {"id": "pressure", "prompt": "压"}],
            task_id="task_1",
        )


def test_score_after_on_last_turn_accepted():
    turns = parse_conversation(
        [{"id": "setup", "prompt": "记住"},
         {"id": "probe", "prompt": "问", "score_after": True}],
        task_id="task_1",
    )
    plan = effective_conversation(_task(conversation_turns=turns))
    assert plan.score_turn.turn_id == "probe"


def test_score_after_last_turn_may_be_interaction():
    """最后一轮是 answer_interaction 时同样成立（它也是一轮）。"""
    turns = parse_conversation(
        [{"id": "setup", "prompt": "开始"},
         {"id": "choose", "action": "answer_interaction",
          "wait_for": {"tool_name": "builtin:AskUserQuestion"},
          "answer": {"option_id": "a"}, "score_after": True}],
        task_id="task_1",
    )
    assert turns[-1].score_after is True


def test_multiple_score_turns_rejected():
    with pytest.raises(ConversationPlanError, match="评分"):
        parse_conversation(
            [{"id": "a", "prompt": "a", "score_after": True},
             {"id": "b", "prompt": "b", "score_after": True}],
            task_id="task_1",
        )


def test_send_message_requires_prompt():
    with pytest.raises(ConversationPlanError, match="prompt"):
        parse_conversation([{"id": "a"}], task_id="task_1")


def test_send_message_rejects_interaction_fields():
    with pytest.raises(ConversationPlanError, match="wait_for/answer"):
        parse_conversation(
            [{"id": "a", "prompt": "a", "answer": {"option_id": "x"}}],
            task_id="task_1",
        )


def test_answer_interaction_requires_wait_for():
    with pytest.raises(ConversationPlanError, match="wait_for"):
        parse_conversation(
            [{"id": "a", "prompt": "a"},
             {"id": "b", "action": "answer_interaction", "answer": {"option_id": "x"}}],
            task_id="task_1",
        )


def test_answer_interaction_requires_answer():
    with pytest.raises(ConversationPlanError, match="answer"):
        parse_conversation(
            [{"id": "a", "prompt": "a"},
             {"id": "b", "action": "answer_interaction",
              "wait_for": {"tool_name": "builtin:AskUserQuestion"}}],
            task_id="task_1",
        )


def test_answer_interaction_cannot_be_first_turn():
    with pytest.raises(ConversationPlanError, match="首轮"):
        parse_conversation(
            [{"id": "a", "action": "answer_interaction",
              "wait_for": {"tool_name": "builtin:AskUserQuestion"},
              "answer": {"option_id": "x"}}],
            task_id="task_1",
        )


def test_unknown_action_and_purpose_rejected():
    with pytest.raises(ConversationPlanError, match="action"):
        parse_conversation([{"id": "a", "prompt": "a", "action": "teleport"}], task_id="t")
    with pytest.raises(ConversationPlanError, match="purpose"):
        parse_conversation([{"id": "a", "prompt": "a", "purpose": "vibes"}], task_id="t")


def test_wait_for_unknown_fields_rejected():
    with pytest.raises(ConversationPlanError, match="未知字段"):
        parse_conversation(
            [{"id": "a", "prompt": "a"},
             {"id": "b", "action": "answer_interaction",
              "wait_for": {"tool_name": "x", "oops": 1},
              "answer": {"option_id": "y"}}],
            task_id="task_1",
        )


# ---------- context 接入点（dispatch 消费） --------------------------------


def test_context_without_conversation_returns_empty():
    assert conversation_turns_from_context(task_id="t", task_context={}) == ()
    assert conversation_turns_from_context(task_id="t", task_context={"k": 1}) == ()


def test_context_with_conversation_parses():
    ctx = {CONVERSATION_CONTEXT_KEY: [{"id": "a", "prompt": "hi"}]}
    turns = conversation_turns_from_context(task_id="t", task_context=ctx)
    assert len(turns) == 1
    assert turns[0].turn_id == "a"


def test_context_with_invalid_conversation_raises():
    ctx = {CONVERSATION_CONTEXT_KEY: []}
    with pytest.raises(ConversationPlanError):
        conversation_turns_from_context(task_id="t", task_context=ctx)


def test_conversation_key_hidden_from_agent_prompt_context():
    """`_conversation` 走下划线前缀通道：不得渲染进给 agent 看的上下文。"""
    from backend.adapters.base import prompt_context

    ctx = {"business": "x", CONVERSATION_CONTEXT_KEY: [{"id": "a", "prompt": "hi"}]}
    rendered = prompt_context(ctx)
    assert CONVERSATION_CONTEXT_KEY not in rendered
    assert rendered["business"] == "x"
