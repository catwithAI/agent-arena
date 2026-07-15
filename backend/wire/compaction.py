"""上下文压缩检测（design §10.4，W6-1）。

只消费 canonical `llm_call` records（不重新解析私有格式）。按
``(agent_id, producer_session_id)`` 分段——避免把**新 session**当成压缩。对每段内
``call_role=="main"`` 的相邻调用算 token/message/hash delta，按证据优先级分四档
confidence，并推断 strategy，产出 canonical ``context_compaction`` record。

证据优先级（design §10.4）：
1. producer 显式 compaction event → explicit（W0-5 的 compaction_hint，另路，本模块只做被动检测）；
2. summary/compaction call 后 token/message 大幅下降 → high；
3. token 下降且 message hash diff 显示中段删除/摘要插入 → medium；
4. 只有 token 突降 → low；
5. session ID 改变 → new-session，不记 compaction。

阈值做成 **versioned analyzer config**（沿用 llm-gateway 保守值）。message 级
prefix/suffix 精确 diff 需要逐消息 hash；canonical RequestSummary 只有聚合
messages_hash，故 strategy 只能靠 size 启发（design §493：domain/粒度不足降 size
启发并降 confidence），不足时 strategy=unknown。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ANALYZER_VERSION = "compaction-analyzer-v1"


@dataclass(frozen=True)
class AnalyzerConfig:
    """versioned 阈值（design §10.4，沿用 llm-gateway 保守值）。"""

    version: str = ANALYZER_VERSION
    # current_input / previous_input < ratio_drop → 视为大幅下降。
    ratio_drop: float = 0.6
    # previous_input - current_input > abs_drop → 绝对下降门槛。
    abs_drop: int = 5000
    # summary/compaction 回看窗口（秒）：相邻 main call 时间差在此内才算「紧接」。
    summary_lookback_s: float = 5.0


_DEFAULT_CONFIG = AnalyzerConfig()


def _input_tokens(rec: dict[str, Any]) -> int | None:
    usage = (rec.get("data") or {}).get("usage") or {}
    v = usage.get("input_tokens")
    return v if isinstance(v, int) else None


def _message_count(rec: dict[str, Any]) -> int | None:
    req = (rec.get("data") or {}).get("request") or {}
    v = req.get("message_count")
    return v if isinstance(v, int) else None


def _messages_hash(rec: dict[str, Any]) -> str | None:
    req = (rec.get("data") or {}).get("request") or {}
    h = req.get("messages_hash")
    return h if isinstance(h, str) and h else None


def _ts(rec: dict[str, Any]) -> str:
    return (rec.get("time") or {}).get("timestamp") or ""


def _seconds_between(prev_ts: str, cur_ts: str) -> float | None:
    """两个 ISO 时间戳的秒差；解析失败返回 None（不因缺时间戳误判紧接）。"""
    from datetime import datetime

    def _parse(s: str):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    a, b = _parse(prev_ts), _parse(cur_ts)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def _segment_key(rec: dict[str, Any]) -> tuple[str, str]:
    """分段键：(agent_id, producer_session_id)。session 变 = 新段（不跨段检测）。"""
    corr = rec.get("correlation") or {}
    return (corr.get("agent_id") or "main", corr.get("producer_session_id") or "")


def _classify(
    prev: dict[str, Any], cur: dict[str, Any], cfg: AnalyzerConfig,
) -> dict[str, Any] | None:
    """相邻两个 main call 是否构成压缩 + confidence/strategy。无迹象返回 None。"""
    prev_in = _input_tokens(prev)
    cur_in = _input_tokens(cur)
    # token 是被动检测的核心信号；缺失无法判定（不猜）。
    if prev_in is None or cur_in is None or prev_in <= 0:
        return None

    ratio = cur_in / prev_in
    abs_drop = prev_in - cur_in
    big_token_drop = ratio < cfg.ratio_drop and abs_drop > cfg.abs_drop
    if not big_token_drop:
        return None  # 没有大幅 token 下降 → 不是压缩

    prev_msgs = _message_count(prev)
    cur_msgs = _message_count(cur)
    msg_drop = (
        prev_msgs is not None and cur_msgs is not None and cur_msgs < prev_msgs
    )
    # message hash 变化（中段删除/摘要插入的间接信号）。
    hash_changed = (
        _messages_hash(prev) is not None
        and _messages_hash(cur) is not None
        and _messages_hash(prev) != _messages_hash(cur)
    )
    # 时间紧接（summary lookback 窗口内）。
    dt = _seconds_between(_ts(prev), _ts(cur))
    tight = dt is not None and 0 <= dt <= cfg.summary_lookback_s

    # confidence 分档（design §10.4）：
    # high  = token+message 大幅下降且时间紧接（像 summary/compaction）；
    # medium= token 下降 + message hash diff（中段删除/摘要插入的间接证据）；
    # low   = 只有 token 突降。
    if msg_drop and tight:
        confidence = "high"
    elif msg_drop or hash_changed:
        confidence = "medium"
    else:
        confidence = "low"

    strategy = _infer_strategy(prev_msgs, cur_msgs, hash_changed)
    return {
        "confidence": confidence,
        "strategy": strategy,
        "before_tokens": prev_in,
        "after_tokens": cur_in,
        "dropped_messages": (prev_msgs - cur_msgs)
        if (prev_msgs is not None and cur_msgs is not None) else None,
    }


def _infer_strategy(
    prev_msgs: int | None, cur_msgs: int | None, hash_changed: bool,
) -> str:
    """strategy 推断（canonical 只有聚合 messages_hash，靠 size 启发，design §493）。

    逐消息 hash 缺失时无法做 prefix/suffix 精确 diff → 只能按消息数变化粗判，
    证据不足一律 unknown（不臆断 full/selective/sliding）。
    """
    if prev_msgs is None or cur_msgs is None:
        return "unknown"
    if cur_msgs <= 2 and prev_msgs - cur_msgs >= 5:
        # 大量删除后只剩极少消息（+可能一条摘要）：像 full-summary。
        return "full-summary"
    # 其余：有下降但缺逐消息 hash 无法区分 selective/sliding → unknown。
    return "unknown"


def detect_compactions(
    records: list[dict[str, Any]], *, config: AnalyzerConfig | None = None,
) -> list[dict[str, Any]]:
    """从 canonical records 检测压缩，返回 ``context_compaction`` record 列表。

    只看 ``llm_call`` 且 ``call_role=="main"``；按 (agent_id, session) 分段，段内按
    时间排序取相邻对。每个检出的压缩产一条 record（before/after call id + tokens +
    strategy + confidence + analyzer_version）。
    """
    cfg = config or _DEFAULT_CONFIG
    mains = [
        r for r in records
        if r.get("record_type") == "llm_call"
        and (r.get("data") or {}).get("call_role") == "main"
    ]
    # 分段。
    segments: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in mains:
        segments.setdefault(_segment_key(r), []).append(r)

    out: list[dict[str, Any]] = []
    for calls in segments.values():
        calls.sort(key=_ts)
        for prev, cur in zip(calls, calls[1:]):
            verdict = _classify(prev, cur, cfg)
            if verdict is None:
                continue
            out.append(_compaction_record(prev, cur, verdict, cfg))
    return out


def _compaction_record(
    prev: dict[str, Any], cur: dict[str, Any], verdict: dict[str, Any],
    cfg: AnalyzerConfig,
) -> dict[str, Any]:
    from backend.wire import ids

    before_lc = (prev.get("correlation") or {}).get("logical_call_id")
    after_lc = (cur.get("correlation") or {}).get("logical_call_id")
    anchor = f"compaction:{before_lc}:{after_lc}"
    return {
        "schema_version": "lane-wire-v1",
        "record_id": ids.record_id(
            attempt_id=cur.get("attempt_id", ""),
            record_kind="context_compaction", record_anchor=anchor,
        ),
        "record_type": "context_compaction",
        "attempt_id": cur.get("attempt_id"),
        "phase": cur.get("phase", "agent_run"),
        "source": {"kind": "lane-analyzer", "instance": "compaction", "version": None},
        "time": {"timestamp": _ts(cur), "started_at": None, "finished_at": None},
        "correlation": {"agent_id": (cur.get("correlation") or {}).get("agent_id", "main"),
                        "confidence": verdict["confidence"]},
        "provenance": [
            {"evidence_id": before_lc, "raw_ref": None},
            {"evidence_id": after_lc, "raw_ref": None},
        ],
        "field_sources": {},
        "conflicts": [],
        "data": {
            "before_call_id": before_lc,
            "after_call_id": after_lc,
            "summary_call_id": None,
            "before_tokens": verdict["before_tokens"],
            "after_tokens": verdict["after_tokens"],
            "dropped_messages": verdict["dropped_messages"],
            "inserted_messages": None,
            "kept_prefix": None,
            "kept_suffix": None,
            "strategy": verdict["strategy"],
            "source": "passive-detector",
            "confidence": verdict["confidence"],
            "analyzer_version": cfg.version,
        },
    }
