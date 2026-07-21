"""把 normalizer evidence 投影成 canonical llm_call records（测试用）。

**必须与 `backend/wire/finalize.py` 的真实投影一致**——尤其
`correlation.agent_id` 来自 `extensions["x-lane.agent-id"]`。

早期测试手工把所有 record 的 agent_id 写成 "main"，导致"跨 agent 虚假压缩"
的验证建立在错误投影上：真实链路里 finalize 已经按 extension 设了独立
agent_id，detector 又按 (agent_id, session) 分段，跨 agent 比较根本不会发生。
用这个 helper 保证测试看到的是真实形状。
"""

from __future__ import annotations

from typing import Any

from backend.wire import turn_correlation


def llm_call_records(
    result: Any, *, session_id: str = "s1", ts_base: str = "2026-07-20T09:00:",
) -> list[dict[str, Any]]:
    """NormalizeResult → canonical llm_call records（同 finalize.py 的字段来源）。"""
    out: list[dict[str, Any]] = []
    for i, evidence in enumerate(result.evidence):
        d = evidence.model_dump()
        payload = d.get("payload") or {}
        if "call_role" not in payload:
            continue
        ext = d.get("extensions") or {}
        correlation = {
            # finalize.py:130 的真实取值方式
            "agent_id": ext.get("x-lane.agent-id") or "main",
            "parent_agent_id": ext.get("x-lane.parent-agent-id"),
            "producer_session_id": session_id,
            "logical_call_id": f"lc_{i}",
        }
        # C4-1：显式 turn extension 投影（同 finalize._base_record）——测试看到的
        # canonical 形状必须与真实链路一致，否则 turn correlation 验证脱离链路。
        exp = turn_correlation.explicit_turn(ext)
        if exp is not None:
            correlation["turn_id"] = exp[0]
            correlation["turn_index"] = exp[1]
            correlation["turn_confidence"] = "explicit"
        out.append({
            "record_type": "llm_call",
            "correlation": correlation,
            "time": {"started_at": f"{ts_base}{10 + i:02d}.000Z"},
            "data": {
                "call_role": payload["call_role"],
                "usage": payload.get("usage") or {},
            },
        })
    return out
