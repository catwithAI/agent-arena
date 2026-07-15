"""W6-3 验收：sub-agent topology（R2.2.5、R4.8）。

CC 的 Task 子 agent 事件带 parent_tool_use_id → 子 agent 独立 agent_id +
parent_agent_id，独立 trajectory（父子分离），不压成普通 tool result。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from backend.wire import finalize, paths
from backend.wire.normalizers.runner import run_native_normalizer
from backend.wire.policy import resolve_effective_policy

_FIXTURE = Path(__file__).parent / "fixtures" / "wire" / "claude" / "events_subagent.jsonl"
ATT = "att_sub"


def _finalize(tmp_path):
    ad = paths.attempt_dir(tmp_path, ATT)
    ad.mkdir(parents=True, exist_ok=True)
    shutil.copy(_FIXTURE, ad / "events.jsonl")
    assert run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT,
        policy=resolve_effective_policy(task_requested="metadata"),
        started_at="2026-07-14T00:00:00Z", finished_at="2026-07-14T00:00:05Z")
    recs = [json.loads(ln) for ln in paths.wire_file(tmp_path, ATT).read_text().splitlines()]
    traj = json.loads((ad / "trajectory.json").read_text())
    return recs, traj


def test_subagent_llm_call_has_distinct_agent_id(tmp_path):
    recs, _ = _finalize(tmp_path)
    calls = [r for r in recs if r["record_type"] == "llm_call"]
    agent_ids = {r["correlation"]["agent_id"] for r in calls}
    # 主 agent + 一个子 agent。
    assert "main" in agent_ids
    sub = [a for a in agent_ids if a.startswith("sub-")]
    assert len(sub) == 1
    # 子 agent call 的 parent_agent_id=main。
    sub_call = next(r for r in calls if r["correlation"]["agent_id"].startswith("sub-"))
    assert sub_call["correlation"]["parent_agent_id"] == "main"


def test_subagent_trajectory_parent_child_separated(tmp_path):
    _, traj = _finalize(tmp_path)
    steps = traj["steps"]
    # Task tool_call 在 main；子 agent 的 flight_search + tool_result 在 sub-。
    task_step = next(s for s in steps if s.get("tool_name") == "Task")
    assert task_step["agent_id"] == "main"

    flight_step = next(s for s in steps if s.get("tool_name") == "flight_search")
    assert flight_step["agent_id"].startswith("sub-")
    assert flight_step["parent_agent_id"] == "main"

    # 子 agent 的 tool_result 也归属子 agent（不压回 main）。
    sub_results = [s for s in steps
                   if s["kind"] == "tool_result" and s["agent_id"].startswith("sub-")]
    assert len(sub_results) == 1


def test_main_calls_stay_main(tmp_path):
    recs, _ = _finalize(tmp_path)
    # msg_main1 / msg_main2 保持 main（parent_agent_id=None）。
    mains = [r for r in recs if r["record_type"] == "llm_call"
             and r["correlation"]["agent_id"] == "main"]
    assert len(mains) == 2
    for m in mains:
        assert m["correlation"]["parent_agent_id"] is None


def test_no_subagent_all_main(tmp_path):
    # 无 parent_tool_use_id 的普通 fixture → 全 main（不误标子 agent）。
    ad = paths.attempt_dir(tmp_path, "att_plain")
    ad.mkdir(parents=True, exist_ok=True)
    plain = Path(__file__).parent / "fixtures" / "wire" / "claude" / "events.jsonl"
    shutil.copy(plain, ad / "events.jsonl")
    run_native_normalizer(agent_name="claude-code", attempt_id="att_plain", data_path=tmp_path)
    finalize.finalize_attempt(
        data_path=tmp_path, attempt_id="att_plain",
        policy=resolve_effective_policy(task_requested="metadata"),
        started_at="2026-07-14T00:00:00Z", finished_at="2026-07-14T00:00:05Z")
    recs = [json.loads(ln) for ln in paths.wire_file(tmp_path, "att_plain").read_text().splitlines()]
    calls = [r for r in recs if r["record_type"] == "llm_call"]
    assert all(r["correlation"]["agent_id"] == "main" for r in calls)
