"""C0-3: attempt 总 deadline——fake clock 证明多轮共享一个预算。"""

import pytest

from backend.conversation.deadline import (
    ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS,
    AttemptBudgetExhausted,
    AttemptDeadline,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_three_turns_share_one_budget():
    """R8.1：每轮拿到的是 remaining，不是重新获得完整预算。"""
    clock = FakeClock()
    deadline = AttemptDeadline(300, clock=clock)

    assert deadline.check_before_turn() == pytest.approx(300)
    clock.advance(120)  # 第一轮耗时 120s
    assert deadline.check_before_turn() == pytest.approx(180)
    clock.advance(150)  # 第二轮耗时 150s
    assert deadline.check_before_turn() == pytest.approx(30)


def test_exhausted_between_turns_blocks_next_turn():
    """R8.2：轮间耗尽必须抛错，不再启动下一轮请求/进程。"""
    clock = FakeClock()
    deadline = AttemptDeadline(100, clock=clock)
    clock.advance(100)

    assert deadline.expired()
    assert deadline.remaining() == 0.0  # 不返回负数
    with pytest.raises(AttemptBudgetExhausted) as exc_info:
        deadline.check_before_turn()
    assert exc_info.value.error_code == ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS


def test_unlimited_budget():
    """timeout_seconds=None → 不限时：remaining None、永不 expired。"""
    clock = FakeClock()
    deadline = AttemptDeadline(None, clock=clock)

    assert deadline.unlimited
    assert deadline.remaining() is None
    clock.advance(10_000_000)
    assert not deadline.expired()
    assert deadline.check_before_turn() is None


def test_remaining_clamps_to_zero_after_overrun():
    clock = FakeClock()
    deadline = AttemptDeadline(50, clock=clock)
    clock.advance(200)
    assert deadline.remaining() == 0.0
