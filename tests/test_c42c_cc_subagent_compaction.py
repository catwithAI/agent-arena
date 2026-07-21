"""C4-2C：Claude Code 子 agent 压缩验证（真实 --forward-subagent-text fixture）。

用真实 `cc_subagent/events.jsonl`（parent_tool_use_id 拓扑）跑 C4-2 detector，确认
CC 的 evidence 形状被正确处理：main/subagent 正确分段、跨 agent 边界不误判、真实
段内 token drop 能被检出。点亮「Claude Code 支持子 agent 压缩」声明（C2-1 + C2-2 +
C4-1C + C4-2 + **C4-2C**）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.wire.compaction import detect_compactions
from backend.wire.normalizers.claude_code import ClaudeCodeNormalizer

from _wire_projection import llm_call_records

FIXTURE = Path(__file__).parent / "fixtures" / "cc_subagent"
SUB_AGENT = "sub-tool_AI5JqduBFLmbMnjaKQgGs5XT"


@pytest.fixture(scope="module")
def records():
    res = ClaudeCodeNormalizer().normalize(
        attempt_id="att_c42c", attempt_dir=FIXTURE,
    )
    return llm_call_records(res)


def test_real_fixture_has_main_and_subagent(records):
    agents = {r["correlation"]["agent_id"] for r in records}
    assert "main" in agents
    assert SUB_AGENT in agents


def test_no_false_positive_across_agent_boundary(records):
    # 真实 fixture 里 main 的 token 未大降（20752→21122），子 agent 的 12163 更低，
    # detector 绝不能把 main→subagent 的 token 差当成压缩（跨 agent 分段隔离）。
    out = detect_compactions(records)
    assert out == []


def test_real_topology_detects_intra_main_drop(records):
    # 在真实 main 段内注入一次 token 大降 → detector 应只在 main 段检出一次，
    # 不波及子 agent 段（证明真实拓扑下正例识别 + 跨 agent 隔离同时成立）。
    main = [r for r in records if r["correlation"]["agent_id"] == "main"]
    assert len(main) >= 2
    # 复制真实 main call，把第二个 main 的 input_tokens 压低造一次 drop。
    import copy

    injected = copy.deepcopy(records)
    main_recs = [r for r in injected if r["correlation"]["agent_id"] == "main"]
    # 给 main 段一个明确的相邻大降：first=90000, next=10000。
    main_recs[0]["data"]["usage"]["input_tokens"] = 90000
    main_recs[0]["time"] = {"timestamp": "2026-07-20T09:00:00Z"}
    main_recs[1]["data"]["usage"]["input_tokens"] = 10000
    main_recs[1]["time"] = {"timestamp": "2026-07-20T09:00:03Z"}
    # 给每条 record 一个 attempt_id（detector 分段键需要）。
    for r in injected:
        r.setdefault("attempt_id", "att_c42c")
    out = detect_compactions(injected)
    assert len(out) == 1
    comp = out[0]
    # 压缩归到 main 段，不是子 agent。
    assert comp["correlation"]["agent_id"] == "main"


def test_subagent_isolated_from_main_segment(records):
    # 子 agent 段只有一个 call → 无相邻对，永远不会与 main 的 call 比较。
    import copy

    injected = copy.deepcopy(records)
    for r in injected:
        r.setdefault("attempt_id", "att_c42c")
    sub = [r for r in injected if r["correlation"]["agent_id"] == SUB_AGENT]
    assert len(sub) == 1  # 单 call 段，无从产出压缩
    out = detect_compactions(injected)
    assert all(c["correlation"]["agent_id"] != SUB_AGENT for c in out)
