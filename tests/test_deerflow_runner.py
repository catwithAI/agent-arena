from __future__ import annotations

import io
import json
import stat
from pathlib import Path
from types import SimpleNamespace

from backend.agents.deerflow.runner import MAX_SUMMARY_BYTES, run_headless


class FakeClient:
    events = []
    error: Exception | None = None
    init_kwargs = None
    stream_kwargs = None

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs

    def stream(self, prompt, **kwargs):
        type(self).stream_kwargs = {"prompt": prompt, **kwargs}
        if self.error:
            raise self.error
        yield from self.events


def _run(tmp_path: Path, *, events, error=None, prompt="do work"):
    FakeClient.events = events
    FakeClient.error = error
    output = io.StringIO()
    summary = tmp_path / "summary.json"
    code = run_headless(
        config_path=tmp_path / "config.yaml",
        summary_path=summary,
        prompt=prompt,
        thread_id="thread-fixture",
        subagent=True,
        thinking=False,
        plan_mode=True,
        recursion_limit=77,
        client_factory=FakeClient,
        output=output,
    )
    return code, output.getvalue(), summary


def test_runner_streams_ndjson_and_writes_bounded_atomic_summary(tmp_path):
    code, stdout, summary_path = _run(
        tmp_path,
        events=[
            SimpleNamespace(type="messages-tuple", data={"type": "ai", "content": "first"}),
            SimpleNamespace(
                type="messages-tuple",
                data={
                    "type": "ai",
                    "content": "final answer",
                    "usage_metadata": {"input_tokens": 4, "output_tokens": 5},
                },
            ),
            SimpleNamespace(type="end", data={}),
        ],
    )

    events = [json.loads(line) for line in stdout.splitlines()]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 0
    assert [event["type"] for event in events] == ["messages-tuple", "messages-tuple", "end"]
    assert summary["status"] == "completed"
    assert summary["final_text"] == "final answer"
    assert summary["usage"] == {"input_tokens": 4, "output_tokens": 5}
    assert summary_path.stat().st_size <= MAX_SUMMARY_BYTES
    assert stat.S_IMODE(summary_path.stat().st_mode) == 0o600
    assert FakeClient.init_kwargs == {
        "config_path": str((tmp_path / "config.yaml").resolve()),
        "model_name": "arena-model",
        "thinking_enabled": False,
        "subagent_enabled": True,
        "plan_mode": True,
    }
    assert FakeClient.stream_kwargs == {
        "prompt": "do work",
        "thread_id": "thread-fixture",
        "recursion_limit": 77,
    }


def test_runner_reassembles_official_message_deltas_by_stable_id(tmp_path):
    code, _stdout, summary_path = _run(
        tmp_path,
        events=[
            {
                "type": "messages-tuple",
                "data": {"id": "answer-1", "type": "ai", "content": "final "},
            },
            {
                "type": "messages-tuple",
                "data": {
                    "id": "answer-1",
                    "type": "ai",
                    "content": "answer",
                    "usage_metadata": {"input_tokens": 4, "output_tokens": 2},
                },
            },
        ],
    )

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 0
    assert summary["final_text"] == "final answer"
    assert summary["usage"] == {"input_tokens": 4, "output_tokens": 2}


def test_runner_restores_provider_fallback_as_nonzero(tmp_path):
    code, _stdout, summary_path = _run(
        tmp_path,
        events=[
            {
                "type": "messages-tuple",
                "data": {"type": "ai", "content": "Provider error: API key invalid"},
            }
        ],
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 25
    assert summary["status"] == "provider_error"
    assert summary["error"]["code"] == "agent_auth_failed"


def test_runner_detects_provider_fallback_split_across_deltas(tmp_path):
    code, _stdout, summary_path = _run(
        tmp_path,
        events=[
            {
                "type": "messages-tuple",
                "data": {"id": "fallback", "type": "ai", "content": "API key "},
            },
            {
                "type": "messages-tuple",
                "data": {"id": "fallback", "type": "ai", "content": "invalid"},
            },
        ],
    )

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 25
    assert summary["status"] == "provider_error"
    assert summary["final_text"] == "API key invalid"


def test_runner_recursion_limit_is_explainable_and_keeps_summary(tmp_path):
    class GraphRecursionError(RuntimeError):
        pass

    code, _stdout, summary_path = _run(
        tmp_path,
        events=[],
        error=GraphRecursionError("recursion limit reached after artifacts were written"),
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 24
    assert summary["status"] == "recursion_limit"
    assert "artifacts were written" in summary["error"]["message"]


def test_runner_exception_is_visible_to_shared_error_classifier(tmp_path):
    code, stdout, summary_path = _run(
        tmp_path,
        events=[],
        error=RuntimeError("rate limit from provider"),
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    diagnostic = json.loads(stdout)
    assert code == 25
    assert summary["error"]["code"] == "agent_rate_limited"
    assert diagnostic["type"] == "runner_diagnostic"
    assert diagnostic["data"]["message"] == "rate limit from provider"


def test_runner_bad_event_degrades_without_breaking_ndjson(tmp_path):
    code, stdout, summary_path = _run(
        tmp_path,
        events=[
            {"type": "unknown", "data": {}},
            {"type": "messages-tuple", "data": {"type": "ai", "content": "ok"}},
        ],
    )
    events = [json.loads(line) for line in stdout.splitlines()]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 0
    assert events[0]["data"]["code"] == "invalid_stream_event"
    assert summary["diagnostics"] == ["invalid_stream_event"]


def test_runner_truncates_oversized_summary_content(tmp_path):
    code, _stdout, summary_path = _run(
        tmp_path,
        events=[
            {
                "type": "messages-tuple",
                "data": {"type": "ai", "content": "x" * (MAX_SUMMARY_BYTES + 100)},
            }
        ],
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 0
    assert summary_path.stat().st_size <= MAX_SUMMARY_BYTES
    assert summary["final_text"] is None
    assert summary["diagnostics"] == ["summary_truncated"]
