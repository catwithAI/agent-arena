"""DeerFlow v2 StreamEvent normalizer.

The embedded client exposes message/tool trajectory and aggregate token usage, but
not request boundaries. Consequently this normalizer never fabricates
``native_llm_call`` evidence: usage is attempt-scoped and explicitly marked
``aggregate-only``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from backend.wire import hashing, ids
from backend.wire.evidence import (
    AggregateUsageEvidence,
    AggregateUsagePayload,
    CaptureEventEvidence,
    CaptureEventPayload,
    CorrelationHints,
    EvidenceProducer,
    EvidenceRawRef,
    EvidenceRedaction,
    EvidenceSource,
    EvidenceTime,
    UsagePayload,
)
from backend.wire.normalizers.base import NormalizeResult, trajectory_validation_evidence
from backend.wire.trajectory_schema import (
    TRAJECTORY_SCHEMA_VERSION,
    Trajectory,
    TrajectoryStep,
    empty_trajectory,
    trajectory_to_dict,
)

PRODUCER_NAME = "deerflow"
PARSER_VERSION = "deerflow-normalizer-v1"
RAW_FILE = "events.jsonl"
SUMMARY_FILE = ".agent-control/runtime/deerflow-summary.json"
SOURCE_KIND = "native-event"
SOURCE_INSTANCE = "native-event"
EPOCH = "1970-01-01T00:00:00.000Z"

CAPABILITIES: dict[str, Any] = {
    "call_boundary": "aggregate-only",
    "usage": "aggregate-only",
    "trajectory": "stream-events",
    "subagent_execution": "task-tool-observable",
    "subagent_identity": False,
    "subagent_identity_basis": (
        "DeerFlow v2.0.0 StreamEvents expose delegation tool activity but no stable child identity"
    ),
}


@dataclass
class _DraftStep:
    order: tuple[int, int]
    anchor: str
    kind: str
    refs: list[dict[str, Any]]
    content_parts: list[str] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    semantic_parts: list[dict[str, Any]] = field(default_factory=list)
    attributes: dict[str, Any] | None = None


@dataclass
class _State:
    drafts: list[_DraftStep] = field(default_factory=list)
    messages: dict[tuple[str, str], _DraftStep] = field(default_factory=dict)
    usage_by_id: dict[str, dict[str, int]] = field(default_factory=dict)
    usage_line: int | None = None
    usage_conflict: bool = False
    session_id: str | None = None


def _iter_events(path: Path) -> Iterator[tuple[int, dict[str, Any] | None]]:
    with path.open("r", encoding="utf-8") as file:
        for line_number, raw in enumerate(file, start=1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                yield line_number, None
                continue
            yield line_number, value if isinstance(value, dict) else None


class DeerFlowNormalizer:
    producer = PRODUCER_NAME
    parser_version = PARSER_VERSION
    raw_file = RAW_FILE

    def has_input(self, attempt_dir: Path) -> bool:
        return (Path(attempt_dir) / RAW_FILE).exists()

    def normalize(self, *, attempt_id: str, attempt_dir: Path) -> NormalizeResult:
        attempt_dir = Path(attempt_dir)
        events_path = attempt_dir / RAW_FILE
        result = NormalizeResult(raw_file=RAW_FILE)
        if not events_path.exists():
            result.trajectory = empty_trajectory(attempt_id)
            return result

        state = _State(session_id=_summary_session(attempt_dir / SUMMARY_FILE))
        for line_number, event in _iter_events(events_path):
            if event is None:
                result.record_error(line_number)
                continue
            try:
                self._apply_event(event, line_number, state)
            except (TypeError, ValueError):
                result.record_error(line_number)

        steps = _materialize_steps(attempt_id, state.drafts)
        usage = _combined_usage(state)
        if state.usage_conflict:
            result.evidence.append(
                _gap_evidence(
                    attempt_id,
                    state.session_id,
                    "usage_conflict",
                    "conflicting usage_metadata for one DeerFlow message id",
                )
            )
        elif usage is not None:
            result.evidence.append(
                _aggregate_evidence(
                    attempt_id,
                    usage,
                    state.usage_line,
                    state.session_id,
                )
            )
        elif steps:
            result.evidence.append(
                _gap_evidence(
                    attempt_id,
                    state.session_id,
                    "usage_not_observed",
                    "StreamEvents contained trajectory but no usage_metadata",
                )
            )

        trajectory = Trajectory(
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            attempt_id=attempt_id,
            steps=tuple(steps),
            producer=PRODUCER_NAME,
        )
        validation_errors = trajectory.validate()
        if validation_errors:
            result.evidence.append(
                trajectory_validation_evidence(
                    attempt_id=attempt_id,
                    producer=PRODUCER_NAME,
                    parser_version=PARSER_VERSION,
                    errors=validation_errors,
                    last_ts=None,
                    raw_file=RAW_FILE,
                )
            )
        result.trajectory = trajectory_to_dict(trajectory)
        return result

    def _apply_event(self, event: dict[str, Any], line_number: int, state: _State) -> None:
        kind = event.get("kind")
        data = event.get("data")
        if not isinstance(kind, str) or not isinstance(data, dict):
            raise ValueError("invalid parsed DeerFlow event")
        if kind == "runner_diagnostic":
            raise ValueError("runner diagnostic indicates degraded StreamEvents")
        if kind != "messages-tuple":
            if kind not in {"values", "custom", "end"}:
                raise ValueError("unknown DeerFlow event kind")
            return

        message_type = data.get("type")
        if message_type == "ai":
            self._ai_event(data, line_number, state)
        elif message_type == "tool":
            self._tool_result(data, line_number, state)
        else:
            raise ValueError("unknown DeerFlow message type")
        self._usage(data, line_number, state)

    def _ai_event(self, data: dict[str, Any], line_number: int, state: _State) -> None:
        message_id = data.get("id")
        stable_id = (
            message_id if isinstance(message_id, str) and message_id else f"line-{line_number}"
        )
        reference = {"file": RAW_FILE, "line": line_number}
        content = data.get("content")
        if isinstance(content, str) and content:
            draft = _message_draft(state, stable_id, "assistant", line_number, reference)
            draft.content_parts.append(content)

        additional = data.get("additional_kwargs")
        reasoning = additional.get("reasoning_content") if isinstance(additional, dict) else None
        if isinstance(reasoning, str) and reasoning:
            draft = _message_draft(state, stable_id, "thinking", line_number, reference)
            draft.content_parts.append(reasoning)

        tool_calls = data.get("tool_calls")
        if tool_calls is None:
            return
        if not isinstance(tool_calls, list):
            raise ValueError("tool_calls is not a list")
        for index, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                raise ValueError("tool call is not an object")
            name = call.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("tool call name is missing")
            call_id = call.get("id")
            tool_call_id = call_id if isinstance(call_id, str) and call_id else None
            planning = name in {"write_todos", "update_plan"}
            attributes: dict[str, Any] | None = None
            if name == "task":
                attributes = {
                    "delegation_requested": True,
                    "subagent_identity": "unobserved",
                }
            state.drafts.append(
                _DraftStep(
                    order=(line_number, 10 + index),
                    anchor=f"tool:{stable_id}:{tool_call_id or index}",
                    kind="planning" if planning else "tool_call",
                    refs=[reference],
                    tool_call_id=tool_call_id,
                    tool_name=name,
                    semantic_parts=[
                        {
                            "type": "tool_call",
                            "name": name,
                            "arguments": call.get("args"),
                        }
                    ],
                    attributes=attributes,
                )
            )

    def _tool_result(self, data: dict[str, Any], line_number: int, state: _State) -> None:
        call_id = data.get("tool_call_id")
        tool_call_id = call_id if isinstance(call_id, str) and call_id else None
        name = data.get("name")
        tool_name = name if isinstance(name, str) and name else None
        state.drafts.append(
            _DraftStep(
                order=(line_number, 0),
                anchor=f"tool-result:{tool_call_id or line_number}",
                kind="tool_result",
                refs=[{"file": RAW_FILE, "line": line_number}],
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                semantic_parts=[
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": data.get("content"),
                    }
                ],
            )
        )

    def _usage(self, data: dict[str, Any], line_number: int, state: _State) -> None:
        raw = data.get("usage_metadata")
        if raw is None:
            return
        usage = _valid_usage(raw)
        if usage is None:
            raise ValueError("invalid usage_metadata")
        message_id = data.get("id")
        identity = (
            message_id if isinstance(message_id, str) and message_id else f"line-{line_number}"
        )
        previous = state.usage_by_id.get(identity)
        if previous is not None and previous != usage:
            state.usage_conflict = True
            return
        state.usage_by_id[identity] = usage
        state.usage_line = line_number


def _message_draft(
    state: _State,
    message_id: str,
    kind: str,
    line_number: int,
    reference: dict[str, Any],
) -> _DraftStep:
    key = (message_id, kind)
    draft = state.messages.get(key)
    if draft is None:
        draft = _DraftStep(
            order=(line_number, 0 if kind == "thinking" else 1),
            anchor=f"{kind}:{message_id}",
            kind=kind,
            refs=[reference],
        )
        state.messages[key] = draft
        state.drafts.append(draft)
    elif reference not in draft.refs:
        draft.refs.append(reference)
    return draft


def _materialize_steps(attempt_id: str, drafts: list[_DraftStep]) -> list[TrajectoryStep]:
    steps: list[TrajectoryStep] = []
    for sequence, draft in enumerate(sorted(drafts, key=lambda item: item.order), start=1):
        semantic_parts = draft.semantic_parts
        if draft.content_parts:
            semantic_parts = [{"type": "text", "text": "".join(draft.content_parts)}]
        content_hash, content_bytes = hashing.part_semantic_hash(semantic_parts)
        steps.append(
            TrajectoryStep(
                step_id=ids.trajectory_step_id(
                    attempt_id=attempt_id,
                    step_anchor=f"{RAW_FILE}:{draft.anchor}",
                ),
                sequence=sequence,
                timestamp=None,
                agent_id="main",
                parent_agent_id=None,
                kind=draft.kind,
                producer_event_refs=tuple(draft.refs),
                tool_call_id=draft.tool_call_id,
                tool_name=draft.tool_name,
                logical_call_id=None,
                content_hash=content_hash,
                content_bytes=content_bytes,
                attributes=draft.attributes,
            )
        )
    return steps


def _valid_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    usage: dict[str, int] = {}
    for target, names in {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
    }.items():
        value = next((raw[name] for name in names if name in raw), None)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        usage[target] = value
    return usage or None


def _combined_usage(state: _State) -> dict[str, int] | None:
    if not state.usage_by_id or state.usage_conflict:
        return None
    combined: dict[str, int] = {}
    for usage in state.usage_by_id.values():
        for name, value in usage.items():
            combined[name] = combined.get(name, 0) + value
    return combined or None


def _summary_session(path: Path) -> str | None:
    try:
        if path.stat().st_size > 64 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != "1":
        return None
    thread_id = value.get("thread_id")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _aggregate_evidence(
    attempt_id: str,
    usage: dict[str, int],
    line_number: int | None,
    session_id: str | None,
) -> AggregateUsageEvidence:
    return AggregateUsageEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=attempt_id,
            source_kind=SOURCE_KIND,
            source_instance=SOURCE_INSTANCE,
            raw_ref=f"{RAW_FILE}:{line_number}",
            producer_id="stream-aggregate",
        ),
        attempt_id=attempt_id,
        phase="agent_run",
        source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
        producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
        time=EvidenceTime(observed_at=EPOCH),
        raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=line_number),
        correlation_hints=CorrelationHints(producer_session_id=session_id),
        capabilities=dict(CAPABILITIES),
        redaction=EvidenceRedaction(policy="metadata", status="applied"),
        errors=[],
        extensions={},
        payload=AggregateUsagePayload(
            scope="attempt",
            usage=UsagePayload(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_read_tokens=None,
                cache_write_tokens=None,
                reasoning_tokens=None,
                estimated=False,
            ),
            producer_event_type="messages-tuple.usage_metadata",
        ),
    )


def _gap_evidence(
    attempt_id: str,
    session_id: str | None,
    reason_code: str,
    message: str,
) -> CaptureEventEvidence:
    return CaptureEventEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=attempt_id,
            source_kind=SOURCE_KIND,
            source_instance=SOURCE_INSTANCE,
            raw_ref=f"deerflow:{reason_code}",
            producer_id="normalizer",
        ),
        attempt_id=attempt_id,
        phase="agent_run",
        source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
        producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
        time=EvidenceTime(observed_at=EPOCH),
        raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=None),
        correlation_hints=CorrelationHints(producer_session_id=session_id),
        capabilities=dict(CAPABILITIES),
        redaction=EvidenceRedaction(policy="metadata", status="applied"),
        errors=[],
        extensions={},
        payload=CaptureEventPayload(
            event="error",
            source_instance=SOURCE_INSTANCE,
            status=None,
            reason_code=reason_code,
            message=message,
            counters=None,
            effective_capabilities=None,
        ),
    )
