from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.agents.deerflow.parser import DeerFlowParser
from backend.agents.deerflow.runner import MAX_EVENT_BYTES, MAX_SUMMARY_BYTES
from backend.agents.parsers import EvidenceSet


def _evidence(tmp_path: Path, events: list[object] | None = None) -> EvidenceSet:
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    stdout = "".join(json.dumps(item) + "\n" for item in (events or []))
    (raw / "stdout.log").write_text(stdout, encoding="utf-8")
    (raw / "stderr.log").write_text("", encoding="utf-8")
    return EvidenceSet.from_runtime_dir(tmp_path)


def _summary(tmp_path: Path, **overrides: object) -> None:
    payload = {
        "schema_version": "1",
        "status": "completed",
        "thread_id": "thread-7",
        "usage": {"input_tokens": 3, "output_tokens": 5},
        "final_text": "done",
        **overrides,
    }
    (tmp_path / "deerflow-summary.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_parser_extracts_final_thinking_tools_and_deduplicated_usage(tmp_path):
    chunks = [
        {
            "type": "messages-tuple",
            "data": {
                "id": "message-1",
                "type": "ai",
                "content": content,
                "additional_kwargs": {"reasoning_content": "consider"},
                "tool_calls": [{"name": "write_file"}],
                "usage_metadata": {"input_tokens": 3, "output_tokens": 5},
            },
        }
        for content in ("do", "ne")
    ]
    evidence = _evidence(tmp_path, [*chunks, {"type": "end", "data": {}}])
    _summary(tmp_path)

    first = await DeerFlowParser().parse(evidence)
    second = await DeerFlowParser().parse(evidence)

    assert first == second
    assert first.final_text == "done"
    assert first.usage == {"input_tokens": 3, "output_tokens": 5}
    assert first.session_id == "thread-7"
    assert len(first.thinking) == 2
    assert len(first.tool_refs) == 2
    assert first.degraded is False


@pytest.mark.asyncio
async def test_parser_summary_conflicts_are_explicit(tmp_path):
    evidence = _evidence(
        tmp_path,
        [
            {
                "type": "messages-tuple",
                "data": {
                    "id": "one",
                    "type": "ai",
                    "content": "raw final",
                    "usage_metadata": {"input_tokens": 1, "output_tokens": 2},
                },
            }
        ],
    )
    _summary(
        tmp_path,
        final_text="summary final",
        usage={"input_tokens": 9, "output_tokens": 9},
    )

    result = await DeerFlowParser().parse(evidence)

    assert result.final_text == "raw final"
    assert result.usage == {"input_tokens": 1, "output_tokens": 2}
    assert result.degraded is True
    assert {item.code for item in result.diagnostics} == {
        "deerflow_summary_final_conflict",
        "deerflow_usage_conflict",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_state", ["missing", "invalid", "oversized", "bad_usage"])
async def test_parser_degrades_on_bad_summary_without_fabricating_usage(tmp_path, summary_state):
    evidence = _evidence(tmp_path)
    path = tmp_path / "deerflow-summary.json"
    if summary_state == "invalid":
        path.write_text("{broken", encoding="utf-8")
    elif summary_state == "oversized":
        path.write_bytes(b"x" * (MAX_SUMMARY_BYTES + 1))
    elif summary_state == "bad_usage":
        _summary(tmp_path, usage={"input_tokens": -1})

    result = await DeerFlowParser().parse(evidence)

    assert result.usage is None
    assert result.degraded is True


@pytest.mark.asyncio
async def test_parser_skips_oversized_event_and_continues(tmp_path):
    evidence = _evidence(tmp_path)
    evidence.stdout_path.write_bytes(
        b"x" * (MAX_EVENT_BYTES + 1)
        + b"\n"
        + json.dumps({"type": "messages-tuple", "data": {"type": "ai", "content": "kept"}}).encode()
        + b"\n"
    )
    _summary(tmp_path, final_text="kept", usage=None)

    result = await DeerFlowParser().parse(evidence)

    assert result.final_text == "kept"
    assert "deerflow_event_truncated" in {item.code for item in result.diagnostics}
