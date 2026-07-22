from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.agents.parsers import EvidenceSet, JsonlMappingParser, TextParser


def _evidence(tmp_path: Path, stdout: bytes) -> EvidenceSet:
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    (raw / "stdout.log").write_bytes(stdout)
    (raw / "stderr.log").write_bytes(b"")
    return EvidenceSet.from_runtime_dir(tmp_path)


@pytest.mark.asyncio
async def test_text_parser_preserves_final_text_and_reports_decode_degradation(tmp_path):
    evidence = _evidence(tmp_path, b"first line\nfinal \xff line\n")
    result = await TextParser().parse(evidence)
    assert result.final_text == "first line\nfinal \ufffd line"
    assert len(result.events) == 2
    assert result.usage is None
    assert result.coverage["token_usage"] == "unsupported"
    assert result.degraded is True


@pytest.mark.asyncio
async def test_jsonl_parser_extracts_standard_events_usage_session_and_final(tmp_path):
    records = [
        {"type": "thinking", "text": "considering"},
        {"type": "tool", "text": "search", "usage": {"input_tokens": 2}},
        {"type": "message", "text": "draft", "usage": {"output_tokens": 3}},
        {"type": "final", "text": "done", "session": {"id": "session-1"}},
    ]
    evidence = _evidence(
        tmp_path,
        ("\n".join(json.dumps(item) for item in records) + "\n").encode(),
    )
    parser = JsonlMappingParser(
        final_type_value="final",
        session_field="session.id",
        tool_type_value="tool",
    )
    result = await parser.parse(evidence)
    assert result.final_text == "done"
    assert len(result.events) == 4
    assert len(result.thinking) == 1
    assert len(result.tool_refs) == 1
    assert result.usage == {"input_tokens": 2, "output_tokens": 3}
    assert result.session_id == "session-1"
    assert result.degraded is False


@pytest.mark.asyncio
async def test_jsonl_schema_drift_is_degraded_without_fabricating_usage(tmp_path):
    raw = b'{"type":"message","text":"usable"}\n[1,2]\n{"broken":\n'
    evidence = _evidence(tmp_path, raw)
    parser = JsonlMappingParser()
    first = await parser.parse(evidence)
    second = await parser.parse(evidence)
    assert first == second  # offline replay is deterministic
    assert first.final_text == "usable"
    assert first.usage is None
    assert first.coverage["token_usage"] == "unknown"
    assert first.degraded is True
    assert {item.code for item in first.diagnostics} == {
        "jsonl_schema_drift",
        "jsonl_invalid_json",
    }


@pytest.mark.asyncio
async def test_text_output_file_cannot_escape_evidence_root(tmp_path):
    evidence = _evidence(tmp_path, b"stdout")
    with pytest.raises(ValueError, match="escapes"):
        await TextParser(output_file=Path("../outside.txt")).parse(evidence)
