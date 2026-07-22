from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents.errors import classify_runtime_result
from backend.agents.models import FailurePatternSpec
from backend.agents.runtime.local_cli import RuntimeResult, StreamStats
from backend.agents.secrets import redact_text, redact_value


def _result(tmp_path: Path, *, stderr: str = "", returncode: int = 1, **overrides):
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    evidence_path = tmp_path / "evidence.jsonl"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    evidence_path.write_text("", encoding="utf-8")
    values = {
        "status": "failed",
        "returncode": returncode,
        "error_code": "agent_nonzero_exit",
        "timed_out": False,
        "cleanup": "not_needed",
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "evidence_path": evidence_path,
        "stdout": StreamStats(),
        "stderr": StreamStats(),
        "started_at": "start",
        "ended_at": "end",
        "duration_ms": 1,
    }
    values.update(overrides)
    return RuntimeResult(**values)


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("401 unauthorized: invalid api key", "agent_auth_failed"),
        ("unknown model fancy-9000", "agent_model_unsupported"),
        ("HTTP 429 too many requests", "agent_rate_limited"),
        ("insufficient credits; billing limit", "agent_usage_limit"),
        ("connection refused", "agent_network_error"),
    ],
)
def test_builtin_error_patterns(tmp_path, message, code):
    classification = classify_runtime_result(_result(tmp_path, stderr=message))
    assert classification.error_code == code
    assert classification.task_failed is True


def test_classification_priority_is_stable(tmp_path):
    classification = classify_runtime_result(
        _result(tmp_path, stderr="429 rate limit followed by 401 unauthorized")
    )
    assert classification.error_code == "agent_auth_failed"


def test_profile_pattern_and_stable_fallback(tmp_path):
    pattern = FailurePatternSpec(
        error_code="agent_usage_limit",
        pattern=r"vendor code E123",
        streams=("stderr",),
        producer_code="vendor.E123",
    )
    matched = classify_runtime_result(
        _result(tmp_path, stderr="vendor code E123"), failure_patterns=(pattern,)
    )
    assert matched.error_code == "agent_usage_limit"
    assert matched.producer_code == "vendor.E123"

    fallback = classify_runtime_result(_result(tmp_path, stderr="unclassified failure"))
    assert fallback.error_code == "agent_nonzero_exit"


def test_parse_degradation_does_not_change_success_to_task_failure(tmp_path):
    result = _result(
        tmp_path,
        returncode=0,
        status="completed",
        error_code=None,
    )
    classification = classify_runtime_result(result, parse_degraded=True)
    assert classification.error_code == "agent_output_parse_degraded"
    assert classification.parse_degraded is True
    assert classification.task_failed is False


def test_timeout_and_cleanup_failure_take_precedence(tmp_path):
    timeout = classify_runtime_result(
        _result(tmp_path, timed_out=True, error_code="agent_timeout", status="timeout")
    )
    assert timeout.error_code == "agent_timeout"

    cleanup = classify_runtime_result(_result(tmp_path, cleanup="failed"))
    assert cleanup.error_code == "agent_cleanup_failed"


def test_redaction_covers_exact_secrets_and_common_credentials():
    secret = "value-with-special-.*-characters"
    text = redact_text(
        f"raw={secret} Authorization: Bearer bearer-value api_key=another-value",
        (secret,),
    )
    assert secret not in text
    assert "bearer-value" not in text
    assert "another-value" not in text
    nested = redact_value({"items": [secret, {"password": f"password={secret}"}]}, (secret,))
    assert secret not in repr(nested)
