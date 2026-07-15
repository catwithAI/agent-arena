"""Claude Code native normalizer（design §10.1、§10.6）。

输入：attempt 的 ``events.jsonl``（claude CLI stream-json 逐行）。
输出：``native_llm_call`` / ``aggregate_usage`` WireEvidence + ``trajectory.json``。

状态机（§10.1）：

1. ``system/init``：记录 session/model/version，不生成 call；
2. ``assistant.message.id`` 首次出现建 candidate call；相同 ID 的重复/增量
   事件合并（流式先发 partial、后发 final，usage 取信息更全的一版）；
3. assistant usage 作为该 call 的 producer-reported usage；
4. tool_use blocks 记 adjacency（trajectory step），不当额外 LLM call；
5. ``result`` usage 作为 attempt aggregate evidence，不生成额外 call；
6. 无 message ID 时按 assistant event sequence 建 call，confidence=inferred；
7. timestamp 是 stdout 到达时间，只作完成时间近似，request start/duration=null。

trajectory（§10.6）两阶段：normalizer 先按 producer event ref 生成稳定 step
ID 与邻接，logical_call_id 用 message ID 作为 producer_call_id anchor（与
finalizer 的 lc 生成同源，因此重跑幂等、correlation 前后 step ID 不变）。

解析失败保留 raw + parser version，不中断整体（R2.1.7 幂等）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from backend.wire import correlate, hashing, ids
from backend.wire.evidence import (
    CorrelationHints,
    EvidenceProducer,
    EvidenceRawRef,
    EvidenceRedaction,
    EvidenceSource,
    EvidenceTime,
    NativeLlmCallEvidence,
    NativeLlmCallPayload,
    RequestSummary,
    ResponseSummary,
    UsagePayload,
    AggregateUsageEvidence,
    AggregateUsagePayload,
)

PRODUCER_NAME = "claude-code"
PARSER_VERSION = "claude-code-normalizer-v1"
SOURCE_KIND = "native-event"
# spool 文件名 native-event.jsonl 无 @instance 后缀，finalize 读到的 instance
# 等于 kind——因此 sequence anchor 的 instance 段也必须是 "native-event"，
# 否则 normalizer 与 finalize 对 orphan call 算出不同 lc。producer 身份
# （claude-code）另由 producer.name/source.version 记录。
SOURCE_INSTANCE = "native-event"
RAW_FILE = "events.jsonl"

TRAJECTORY_SCHEMA_VERSION = "lane-trajectory-v1"

# CLI role → semantic IR role（§10.5）。
_ROLE_MAP = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
    "tool": "tool",
}


@dataclass
class _Call:
    """一次 assistant turn 聚成的 candidate call。"""

    message_id: str | None
    anchor: str            # producer-call:<id> 或 source-seq:...
    confidence: str        # explicit | inferred
    first_line: int
    last_line: int
    seq_no: int | None = None  # inferred call 的稳定序号（进 hints.sequence）
    model: str | None = None
    usage: dict[str, Any] | None = None
    stop_reason: str | None = None
    content_parts: list[dict[str, Any]] = field(default_factory=list)
    output_blocks: int = 0
    observed_at: str | None = None
    # sub-agent 拓扑（W6-3）：CC 的 Task 子 agent 事件带 parent_tool_use_id；
    # 该 call 归属的 agent_id（main 或 sub-<parent_tool_use_id>）与父 agent。
    agent_id: str = "main"
    parent_agent_id: str | None = None


@dataclass
class _Step:
    step_id: str
    sequence: int
    timestamp: str | None
    kind: str
    producer_event_refs: list[dict[str, Any]]
    tool_call_id: str | None = None
    # 工具名（tool_call 步骤）：W3-4 用它把 MCP frame 按 tool name 关联到 step。
    tool_name: str | None = None
    logical_call_id: str | None = None
    # sub-agent 拓扑（W6-3）：该 step 归属的 agent。
    agent_id: str = "main"
    parent_agent_id: str | None = None
    # 可见 payload 的 semantic hash + 原始 size（§10.5，evidence 不可得时 null）
    content_hash: str | None = None
    content_bytes: int | None = None


@dataclass
class NormalizeResult:
    evidence: list[Any] = field(default_factory=list)
    trajectory: dict[str, Any] = field(default_factory=dict)
    parse_errors: int = 0
    # 出错 raw event 的行号（精确定位；进 parse-error evidence 供 debug）
    error_lines: list[int] = field(default_factory=list)
    # raw 里见到的最后一个 timestamp（确定性 UTC，供派生 evidence 的 observed_at；
    # 用数据自身的时间既满足 UTC 约定又保持 rebuild 幂等）
    last_ts: str | None = None

    def record_error(self, lineno: int) -> None:
        self.parse_errors += 1
        # 上限避免病态文件把行号列表撑爆
        if len(self.error_lines) < 100:
            self.error_lines.append(lineno)


def _iter_events(path: Path) -> Iterator[tuple[int, dict[str, Any] | None]]:
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield lineno, json.loads(raw)
            except json.JSONDecodeError:
                yield lineno, None


def _content_to_ir_parts(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """assistant/user content[] → semantic IR parts（§10.5）。"""
    parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "thinking":
            parts.append({"type": "text", "text": block.get("thinking", "")})
        elif btype == "tool_use":
            parts.append({
                "type": "tool_call",
                "name": block.get("name", ""),
                "arguments": block.get("input", {}),
            })
        elif btype == "tool_result":
            parts.append({
                "type": "tool_result",
                "content": block.get("content", ""),
            })
    return parts


def _usage_payload(usage: dict[str, Any] | None) -> UsagePayload:
    u = usage or {}
    # cache_creation_input_tokens 是 Anthropic 的 cache write。
    return UsagePayload(
        input_tokens=u.get("input_tokens"),
        output_tokens=u.get("output_tokens"),
        cache_read_tokens=u.get("cache_read_input_tokens"),
        cache_write_tokens=u.get("cache_creation_input_tokens"),
        reasoning_tokens=None,
        estimated=False,
    )


def _usage_completeness(usage: dict[str, Any] | None) -> int:
    """流式合并时取信息更全的一版：非空数值字段越多越优先。"""
    if not usage:
        return -1
    return sum(1 for v in usage.values() if isinstance(v, (int, float)))


def _part_semantic_hash(
    parts: list[dict[str, Any]], role: str = "assistant"
) -> tuple[str | None, int | None]:
    """content parts → (semantic_hash, utf8 bytes)，用 design §10.5 规定的
    messages IR `[{role, content:[part...]}]` 形状（评审 R4：不是裸 parts）。

    跨 source 共用：等价内容得同 hash。空 parts 或 hash 失败返回 (None, None)。
    """
    if not parts:
        return None, None
    ir = [{"role": role, "content": parts}]
    try:
        h = hashing.semantic_hash("messages", ir)
    except hashing.SemanticHashError:
        return None, None
    try:
        size = len(json.dumps(parts, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        size = None
    return h, size


@dataclass
class _ParseState:
    calls: dict[str, _Call] = field(default_factory=dict)
    call_order: list[str] = field(default_factory=list)
    steps: list[_Step] = field(default_factory=list)
    model_hint: str | None = None
    session_id: str | None = None
    cli_version: str | None = None
    result_usage: dict[str, Any] | None = None
    result_line: int | None = None
    result_ts: str | None = None
    step_seq: int = 0


class ClaudeCodeNormalizer:
    producer = PRODUCER_NAME
    parser_version = PARSER_VERSION

    def normalize(self, *, attempt_id: str, attempt_dir: Path) -> NormalizeResult:
        attempt_dir = Path(attempt_dir)
        events_path = attempt_dir / RAW_FILE
        result = NormalizeResult()
        if not events_path.exists():
            result.trajectory = _empty_trajectory(attempt_id)
            return result

        st = _ParseState()
        for lineno, event in _iter_events(events_path):
            if event is None or not isinstance(event, dict):
                # 非法 JSON 或顶层非 object（schema drift）：计 parse error
                result.record_error(lineno)
                continue
            etype = event.get("type")
            _ev_ts = event.get("timestamp")
            if isinstance(_ev_ts, str) and _ev_ts:
                result.last_ts = _ev_ts

            # adapter 把无法解析的 CLI 输出包成 {"raw_line": ...}（见
            # claude_code adapter）：它是合法 JSON 但不是已知事件，计 parse error
            # 而非静默当未知事件吞掉。
            if "raw_line" in event and etype is None:
                result.record_error(lineno)
                continue

            # 单个事件的解析异常（message 变 list/string 等 schema drift）不能
            # 让整次 normalizer fail-open——per-event 兜住并计 parse error 继续
            # （评审 M3）。已知 type 但 payload 畸形也走这条路。
            try:
                self._apply_event(event, etype, lineno, attempt_id, st)
            except Exception:
                result.record_error(lineno)
                continue

        # 产出 call evidence
        for anchor in st.call_order:
            call = st.calls[anchor]
            result.evidence.append(
                self._call_evidence(
                    attempt_id, call, st.model_hint, st.session_id, st.cli_version
                )
            )
        # aggregate usage evidence（§10.1.6）
        if st.result_usage is not None:
            result.evidence.append(
                self._aggregate_evidence(
                    attempt_id, st.result_usage, st.result_line, st.result_ts,
                    st.session_id,
                )
            )
        result.trajectory = {
            "schema_version": TRAJECTORY_SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "steps": [_step_dict(s) for s in st.steps],
        }
        return result

    def _apply_event(
        self, event: dict[str, Any], etype: Any, lineno: int,
        attempt_id: str, st: "_ParseState",
    ) -> None:
        """处理单个 raw event，更新 st。异常由调用方计 parse error。"""
        ts = event.get("timestamp")

        if etype == "system" and event.get("subtype") == "init":
            st.session_id = event.get("session_id") or st.session_id
            st.model_hint = event.get("model") or st.model_hint
            st.cli_version = event.get("version") or st.cli_version
            return

        # sub-agent 归属（W6-3）：CC 的 Task 子 agent 事件带 parent_tool_use_id
        # （顶层字段）。有则该事件属于以此 tool_use 为父的子 agent；agent_id 由
        # parent_tool_use_id 稳定派生，parent_agent_id=main。无则 main。
        parent_tuid = event.get("parent_tool_use_id")
        cur_agent_id, cur_parent = _agent_of(parent_tuid)

        if etype == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                raise ValueError("assistant.message 非 object")
            msg_id = message.get("id")
            if msg_id:
                anchor = f"{correlate.ANCHOR_PRODUCER_CALL}:{msg_id}"
                confidence, seq_no = "explicit", None
            else:
                seq_no = len([a for a in st.call_order if not st.calls[a].message_id])
                anchor = correlate.sequence_anchor(SOURCE_KIND, SOURCE_INSTANCE, seq_no)
                confidence = "inferred"

            call = st.calls.get(anchor)
            if call is None:
                call = _Call(
                    message_id=msg_id, anchor=anchor, confidence=confidence,
                    first_line=lineno, last_line=lineno,
                    agent_id=cur_agent_id, parent_agent_id=cur_parent,
                )
                call.seq_no = seq_no
                st.calls[anchor] = call
                st.call_order.append(anchor)
            call.last_line = lineno
            call.observed_at = ts or call.observed_at
            call.model = message.get("model") or call.model
            call.stop_reason = message.get("stop_reason") or call.stop_reason
            content = message.get("content")
            content = content if isinstance(content, list) else []
            new_parts = _content_to_ir_parts(content)
            if len(new_parts) >= len(call.content_parts):
                call.content_parts = new_parts
                call.output_blocks = len([c for c in content if isinstance(c, dict)])
            usage = message.get("usage")
            usage = usage if isinstance(usage, dict) else None
            if _usage_completeness(usage) > _usage_completeness(call.usage):
                call.usage = usage

            # assistant step 的 content hash：该 message 的 text parts（与 Codex
            # agent_message 用同一 per-part messages IR，跨 source 可比，评审 M3）。
            text_parts = [p for p in _content_to_ir_parts(content) if p.get("type") == "text"]
            a_hash, a_bytes = _part_semantic_hash(text_parts)
            st.step_seq += 1
            st.steps.append(_Step(
                step_id=ids.trajectory_step_id(
                    attempt_id=attempt_id, step_anchor=f"{RAW_FILE}:{lineno}:assistant",
                ),
                sequence=st.step_seq, timestamp=ts, kind="assistant",
                producer_event_refs=[{"file": RAW_FILE, "line": lineno}],
                logical_call_id=_lc(attempt_id, anchor),
                content_hash=a_hash, content_bytes=a_bytes,
            ))
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tc_hash, tc_bytes = _part_semantic_hash([{
                        "type": "tool_call", "name": block.get("name", ""),
                        "arguments": block.get("input"),
                    }])
                    st.step_seq += 1
                    st.steps.append(_Step(
                        step_id=ids.trajectory_step_id(
                            attempt_id=attempt_id,
                            step_anchor=f"{RAW_FILE}:{lineno}:tool_use:{block.get('id')}",
                        ),
                        sequence=st.step_seq, timestamp=ts, kind="tool_call",
                        producer_event_refs=[{"file": RAW_FILE, "line": lineno}],
                        tool_call_id=block.get("id"),
                        tool_name=block.get("name"),
                        logical_call_id=_lc(attempt_id, anchor),
                        content_hash=tc_hash, content_bytes=tc_bytes,
                        agent_id=cur_agent_id, parent_agent_id=cur_parent,
                    ))
            return

        if etype == "user":
            message = event.get("message")
            if not isinstance(message, dict):
                raise ValueError("user.message 非 object")
            content = message.get("content")
            content = content if isinstance(content, list) else []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    st.step_seq += 1
                    st.steps.append(_Step(
                        step_id=ids.trajectory_step_id(
                            attempt_id=attempt_id,
                            step_anchor=f"{RAW_FILE}:{lineno}:tool_result:{block.get('tool_use_id')}",
                        ),
                        sequence=st.step_seq, timestamp=ts, kind="tool_result",
                        producer_event_refs=[{"file": RAW_FILE, "line": lineno}],
                        tool_call_id=block.get("tool_use_id"),
                        agent_id=cur_agent_id, parent_agent_id=cur_parent,
                    ))
            return

        if etype == "result":
            # §10.1.6：result usage 作为 attempt aggregate，不生成额外 call
            usage = event.get("usage")
            st.result_usage = (usage if isinstance(usage, dict) else None) or st.result_usage
            st.result_line = lineno
            st.result_ts = ts
            return
        # 其他已知/未知 type（rate_limit_event 等）：无 call 语义，忽略但不计错

    # ---- evidence 构造 -----------------------------------------------

    def _call_evidence(
        self, attempt_id: str, call: _Call,
        model_hint: str | None, session_id: str | None, cli_version: str | None,
    ) -> NativeLlmCallEvidence:
        model = call.model or model_hint
        # response_summary：对 assistant content parts 算 semantic hash——用
        # 与 trajectory 同一 _part_semantic_hash（§10.5 [{role,content}] IR，
        # 评审 M3：canonical response hash 不能再用裸 parts）。
        content_hash, _ = _part_semantic_hash(call.content_parts)
        hash_domain = hashing.DOMAIN_SEMANTIC if content_hash else None
        response_summary = ResponseSummary(
            content_hash=content_hash,
            hash_domain=hash_domain,
            message_bytes=None,
            output_blocks=call.output_blocks,
        )
        request_summary = RequestSummary(
            model=model, message_count=None, message_bytes=None,
            system_hash=None, messages_hash=None, tools_hash=None, hash_domain=None,
        )
        payload = NativeLlmCallPayload(
            producer_call_id=call.message_id,
            model=model,
            call_role="main",
            request_summary=request_summary,
            response_summary=response_summary,
            usage=_usage_payload(call.usage),
            finish_reason=call.stop_reason,
        )
        return NativeLlmCallEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref=f"{RAW_FILE}:{call.first_line}", producer_id=call.anchor,
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE, version=cli_version),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION, event_id=call.message_id),
            time=EvidenceTime(observed_at=call.observed_at or "", started_at=None, finished_at=call.observed_at),
            raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=call.first_line),
            correlation_hints=CorrelationHints(
                producer_session_id=session_id,
                producer_call_id=call.message_id,
                model=model,
                sequence=call.seq_no,
            ),
            capabilities={"call_boundary": True},
            redaction=EvidenceRedaction(policy="metadata", status="applied", hash_algorithm="sha256", hash_domain=hash_domain),
            errors=[],
            # sub-agent 拓扑（W6-3）：非 main agent 的 call 带 agent_id/parent（走
            # namespaced 扩展，CorrelationHints 无 agent_id 字段）。finalizer 读它填
            # canonical llm_call.correlation.agent_id。main call 不写（默认 main）。
            extensions=(
                {"x-lane.agent-id": call.agent_id,
                 "x-lane.parent-agent-id": call.parent_agent_id}
                if call.agent_id != "main" else {}
            ),
            payload=payload,
        )

    def _aggregate_evidence(
        self, attempt_id: str, usage: dict[str, Any],
        line: int | None, ts: str | None, session_id: str | None,
    ) -> AggregateUsageEvidence:
        return AggregateUsageEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref=f"{RAW_FILE}:{line}", producer_id="result-aggregate",
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=ts or "", started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=line),
            correlation_hints=CorrelationHints(producer_session_id=session_id),
            capabilities={},
            redaction=EvidenceRedaction(policy="metadata", status="applied", hash_algorithm=None, hash_domain=None),
            errors=[],
            extensions={},
            payload=AggregateUsagePayload(
                scope="attempt",
                usage=_usage_payload(usage),
                producer_event_type="result",
            ),
        )


def _lc(attempt_id: str, anchor: str) -> str:
    """trajectory step 的 logical_call_id：与 finalizer 的 lc 生成同源
    （anchor 未桥接前的确定值）。"""
    return ids.logical_call_id(attempt_id=attempt_id, call_anchor=anchor)


def _agent_of(parent_tool_use_id: Any) -> tuple[str, str | None]:
    """事件的 agent 归属（W6-3）：CC Task 子 agent 事件带 parent_tool_use_id。

    有 → 子 agent（agent_id 由 parent_tool_use_id 稳定派生，父为 main）；无 → main。
    子 agent 不压成普通 tool result——它有独立 agent_id 与独立 trajectory 归属
    （R2.2.5、R4.8）。"""
    if isinstance(parent_tool_use_id, str) and parent_tool_use_id:
        return f"sub-{parent_tool_use_id}", "main"
    return "main", None


def _step_dict(s: _Step) -> dict[str, Any]:
    return {
        "step_id": s.step_id,
        "sequence": s.sequence,
        "timestamp": s.timestamp,
        "agent_id": s.agent_id,
        "parent_agent_id": s.parent_agent_id,
        "kind": s.kind,
        "producer_event_refs": s.producer_event_refs,
        "tool_call_id": s.tool_call_id,
        "tool_name": s.tool_name,
        "logical_call_id": s.logical_call_id,
        "content_hash": s.content_hash,
        "content_bytes": s.content_bytes,
    }


def _empty_trajectory(attempt_id: str) -> dict[str, Any]:
    return {
        "schema_version": TRAJECTORY_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "steps": [],
    }
