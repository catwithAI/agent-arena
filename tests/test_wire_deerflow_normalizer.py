from __future__ import annotations

import json
from pathlib import Path

from backend.wire import finalize, paths
from backend.wire.normalizers.deerflow import CAPABILITIES, DeerFlowNormalizer
from backend.wire.normalizers.runner import normalizer_for, run_native_normalizer
from backend.wire.policy import resolve_effective_policy

ATTEMPT = "attempt-deerflow-wire"
POLICY = resolve_effective_policy(task_requested="metadata")


def _events() -> list[dict]:
    return [
        {
            "kind": "messages-tuple",
            "sequence": 1,
            "data": {
                "id": "answer-1",
                "type": "ai",
                "content": "do",
                "additional_kwargs": {"reasoning_content": "think "},
                "usage_metadata": {"input_tokens": 4, "output_tokens": 5},
            },
        },
        {
            "kind": "messages-tuple",
            "sequence": 2,
            "data": {
                "id": "answer-1",
                "type": "ai",
                "content": "ne",
                "additional_kwargs": {"reasoning_content": "carefully"},
                "usage_metadata": {"input_tokens": 4, "output_tokens": 5},
                "tool_calls": [
                    {"id": "plan-1", "name": "write_todos", "args": {"items": []}},
                    {"id": "task-1", "name": "task", "args": {"description": "delegate"}},
                ],
            },
        },
        {
            "kind": "messages-tuple",
            "sequence": 3,
            "data": {
                "id": "tool-result-1",
                "type": "tool",
                "name": "task",
                "tool_call_id": "task-1",
                "content": "delegated result",
            },
        },
        {"kind": "end", "sequence": 4, "data": {}},
    ]


def _attempt(tmp_path: Path, events: list[dict] | None = None, attempt_id=ATTEMPT) -> Path:
    attempt = paths.attempt_dir(tmp_path, attempt_id)
    attempt.mkdir(parents=True, exist_ok=True)
    (attempt / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in (events or _events())),
        encoding="utf-8",
    )
    summary = attempt / ".agent-control" / "runtime" / "deerflow-summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "completed",
                "thread_id": "thread-deerflow",
            }
        ),
        encoding="utf-8",
    )
    return attempt


def test_deerflow_normalizer_is_registered_and_aggregate_only(tmp_path):
    attempt = _attempt(tmp_path)
    normalizer = normalizer_for("deerflow")
    assert isinstance(normalizer, DeerFlowNormalizer)

    first = normalizer.normalize(attempt_id=ATTEMPT, attempt_dir=attempt)
    second = normalizer.normalize(attempt_id=ATTEMPT, attempt_dir=attempt)

    assert first.trajectory == second.trajectory
    assert [item.model_dump() for item in first.evidence] == [
        item.model_dump() for item in second.evidence
    ]
    assert not [item for item in first.evidence if item.evidence_type == "native_llm_call"]
    aggregate = next(item for item in first.evidence if item.evidence_type == "aggregate_usage")
    assert aggregate.payload.usage.input_tokens == 4
    assert aggregate.payload.usage.output_tokens == 5
    assert aggregate.correlation_hints.producer_session_id == "thread-deerflow"
    assert aggregate.capabilities == CAPABILITIES


def test_deerflow_trajectory_reassembles_deltas_without_inventing_child_identity(tmp_path):
    attempt = _attempt(tmp_path)
    result = DeerFlowNormalizer().normalize(attempt_id=ATTEMPT, attempt_dir=attempt)
    steps = result.trajectory["steps"]

    assert [step["kind"] for step in steps] == [
        "thinking",
        "assistant",
        "planning",
        "tool_call",
        "tool_result",
    ]
    assert all(step["agent_id"] == "main" for step in steps)
    assert all(step["parent_agent_id"] is None for step in steps)
    assistant = next(step for step in steps if step["kind"] == "assistant")
    thinking = next(step for step in steps if step["kind"] == "thinking")
    delegation = next(step for step in steps if step.get("tool_name") == "task")
    assert len(assistant["producer_event_refs"]) == 2
    assert assistant["content_hash"] and thinking["content_hash"]
    assert delegation["attributes"] == {
        "delegation_requested": True,
        "subagent_identity": "unobserved",
    }
    assert delegation["agent_id"] == "main"


def test_deerflow_usage_conflict_is_gap_not_fabricated_aggregate(tmp_path):
    events = _events()
    events[1]["data"]["usage_metadata"] = {"input_tokens": 40, "output_tokens": 50}
    attempt = _attempt(tmp_path, events)

    result = DeerFlowNormalizer().normalize(attempt_id=ATTEMPT, attempt_dir=attempt)

    assert not [item for item in result.evidence if item.evidence_type == "aggregate_usage"]
    gap = next(item for item in result.evidence if item.evidence_type == "capture_event")
    assert gap.payload.reason_code == "usage_conflict"
    assert gap.capabilities["call_boundary"] == "aggregate-only"


def test_deerflow_runner_finalizer_manifest_reports_aggregate_only_coverage(tmp_path):
    _attempt(tmp_path)
    assert run_native_normalizer(
        agent_name="deerflow",
        attempt_id=ATTEMPT,
        data_path=tmp_path,
        adapter_usage={"input_tokens": 4, "output_tokens": 5},
    )
    manifest = finalize.finalize_attempt(
        data_path=tmp_path,
        attempt_id=ATTEMPT,
        policy=POLICY,
        declared_sources=[{"kind": "native-event", "instance": "native-event"}],
    )

    source = next(item for item in manifest["sources"] if item["kind"] == "native-event")
    wire_records = [
        json.loads(line)
        for line in paths.wire_file(tmp_path, ATTEMPT).read_text(encoding="utf-8").splitlines()
    ]
    assert source["status"] == "complete"
    assert source["capabilities"]["call_boundary"] == "aggregate-only"
    assert source["capabilities"]["subagent_identity"] is False
    assert len(manifest["aggregates"]) == 2  # native + AdapterResult reconciliation
    assert not [record for record in wire_records if record["record_type"] == "llm_call"]


def test_deerflow_step_ids_do_not_cross_attempts(tmp_path):
    first_attempt = _attempt(tmp_path, attempt_id="attempt-one")
    second_attempt = _attempt(tmp_path, attempt_id="attempt-two")
    normalizer = DeerFlowNormalizer()

    first = normalizer.normalize(attempt_id="attempt-one", attempt_dir=first_attempt)
    second = normalizer.normalize(attempt_id="attempt-two", attempt_dir=second_attempt)

    assert {step["step_id"] for step in first.trajectory["steps"]}.isdisjoint(
        step["step_id"] for step in second.trajectory["steps"]
    )
