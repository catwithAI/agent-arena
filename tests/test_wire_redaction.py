"""W0-2 验收：policy + redaction + paths。

覆盖 tasks.md W0-2 验收清单：
- 五类敏感 header 永不落盘；
- JSON path 与自由文本 pattern 命中；
- redactor 抛异常时输出 metadata-only + ``redaction_failed``（R11.3）；
- policy 交集与降档（design §16.1）；
- blob ref 白名单与目录穿越防护（预演 W0-6 的 API 用例）。
"""

import json
import re
from pathlib import Path

import pytest

from backend.wire import paths, policy, redaction


# ---------- policy：四档与最严格交集 --------------------------------------

def test_policy_order_and_rank():
    assert [policy.policy_rank(p) for p in ("off", "metadata", "parsed", "full")] == [0, 1, 2, 3]
    with pytest.raises(ValueError):
        policy.policy_rank("everything")


def test_effective_policy_strictest_intersection():
    eff = policy.resolve_effective_policy(
        server_max="parsed",
        task_requested="full",
        run_requested="full",
        source_capability="full",
    )
    assert eff.requested == "full"
    assert eff.effective == "parsed"
    assert eff.downgrade_reason == "server_max"


def test_effective_policy_source_capability_downgrade():
    eff = policy.resolve_effective_policy(
        server_max="full",
        task_requested="parsed",
        source_capability="metadata",
    )
    assert eff.effective == "metadata"
    assert eff.downgrade_reason == "source_capability"


def test_effective_policy_no_downgrade_when_within_limits():
    eff = policy.resolve_effective_policy(server_max="full", task_requested="parsed")
    assert eff.requested == "parsed"
    assert eff.effective == "parsed"
    assert eff.downgrade_reason is None


def test_run_can_tighten_but_not_loosen_task():
    # run 请求比 task 松：requested 取更严格的 task 档
    eff = policy.resolve_effective_policy(task_requested="metadata", run_requested="full")
    assert eff.requested == "metadata"
    # run 收紧：生效
    eff = policy.resolve_effective_policy(task_requested="parsed", run_requested="off")
    assert eff.requested == "off"


def test_default_policy_is_metadata():
    """R11.6：什么都没配时默认 metadata。"""
    eff = policy.resolve_effective_policy()
    assert eff.requested == "metadata"
    assert eff.effective == "metadata"


def test_invalid_policy_value_raises():
    with pytest.raises(ValueError):
        policy.resolve_effective_policy(task_requested="verbose")


# ---------- redaction：header 黑名单 ---------------------------------------

SENSITIVE_HEADERS = {
    "Authorization": "Bearer sk-live-abcdef1234567890",
    "Proxy-Authorization": "Basic dXNlcjpwYXNz",
    "X-Api-Key": "sk-vendor-v3--1",
    "Cookie": "session=deadbeef",
    "Set-Cookie": "auth=cafebabe; HttpOnly",
}


def test_five_sensitive_headers_never_persisted():
    cleaned = redaction.redact_headers({**SENSITIVE_HEADERS, "Content-Type": "application/json"})
    serialized = json.dumps(cleaned)
    for secret in SENSITIVE_HEADERS.values():
        assert secret not in serialized
    # key 保留供 metadata 观测，值必须是占位符
    for key in SENSITIVE_HEADERS:
        assert cleaned[key] == redaction.REDACTED
    assert cleaned["Content-Type"] == "application/json"


def test_header_blacklist_is_case_insensitive():
    cleaned = redaction.redact_headers({"AUTHORIZATION": "Bearer topsecret123"})
    assert cleaned["AUTHORIZATION"] == redaction.REDACTED


def test_custom_header_value_with_embedded_token_is_scrubbed():
    cleaned = redaction.redact_headers({"X-Debug": "creds sk-abcdef123456789 trailing"})
    assert "sk-abcdef123456789" not in cleaned["X-Debug"]


# ---------- redaction：JSON key pattern 与文本 pattern ----------------------

def test_json_key_pattern_hits():
    payload = {
        "api_key": "sk-xyz",
        "nested": {"Password": "hunter2", "list": [{"authToken": "abc123secret"}]},
        "model": "glm-4.7",
    }
    cleaned = redaction.redact_json(payload)
    assert cleaned["api_key"] == redaction.REDACTED
    assert cleaned["nested"]["Password"] == redaction.REDACTED
    assert cleaned["nested"]["list"][0]["authToken"] == redaction.REDACTED
    assert cleaned["model"] == "glm-4.7"


def test_text_secret_patterns_hit():
    text = (
        "calling with sk-proj-abc123def456 and Bearer eyJhbGciOiJIUzI1NiJ9"
        " aws AKIAIOSFODNN7EXAMPLE github ghp_abcdefghij0123456789"
        " jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4"
    )
    scrubbed = redaction.scrub_text(text)
    assert "sk-proj-abc123def456" not in scrubbed
    assert "AKIAIOSFODNN7EXAMPLE" not in scrubbed
    assert "ghp_abcdefghij0123456789" not in scrubbed
    assert "SflKxwRJSMeKKF2QT4" not in scrubbed
    assert redaction.REDACTED in scrubbed


def test_key_value_assignment_in_free_text_scrubbed():
    scrubbed = redaction.scrub_text("retrying with api_key=abcd1234efgh after 429")
    assert "abcd1234efgh" not in scrubbed


def test_string_values_inside_json_are_scrubbed():
    cleaned = redaction.redact_json({"message": "use sk-abcdef123456789 to auth"})
    assert "sk-abcdef123456789" not in cleaned["message"]


def test_extra_key_patterns_tighten_only():
    payload = {"internal_ref": "血条-123", "normal": "keep"}
    cleaned = redaction.redact_json(
        payload, extra_key_patterns=(re.compile(r"internal_ref"),)
    )
    assert cleaned["internal_ref"] == redaction.REDACTED
    assert cleaned["normal"] == "keep"
    # 默认规则仍然生效
    cleaned2 = redaction.redact_json(
        {"api_key": "x" * 10}, extra_key_patterns=(re.compile(r"internal_ref"),)
    )
    assert cleaned2["api_key"] == redaction.REDACTED


# ---------- redaction：失败收敛为 metadata-only（R11.3）--------------------

def test_redactor_failure_drops_payload_keeps_metadata(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("parser exploded on sk-abcdef123456789")

    monkeypatch.setattr(redaction, "redact_json", boom)
    result = redaction.safe_redact_payload({"prompt": "hi"}, policy="parsed")
    assert result.status == "failed"
    assert result.payload is None
    assert "redaction_failed" in result.flags
    # 错误消息本身也被 scrub（R11.9）
    assert "sk-abcdef123456789" not in (result.error or "")


def test_policy_off_and_metadata_skip_payload():
    for p in ("off", "metadata"):
        result = redaction.safe_redact_payload({"prompt": "secret stuff"}, policy=p)
        assert result.status == "skipped"
        assert result.payload is None


def test_policy_parsed_applies_redaction():
    result = redaction.safe_redact_payload(
        {"api_key": "sk-abc123def456", "text": "ok"}, policy="parsed"
    )
    assert result.status == "applied"
    assert result.payload["api_key"] == redaction.REDACTED
    assert result.payload["text"] == "ok"


# ---------- paths：布局、blob ref 白名单、目录穿越 --------------------------

def test_wire_layout_paths(tmp_path):
    d = paths.attempt_dir(tmp_path, "att_1")
    assert paths.wire_file(tmp_path, "att_1") == d / "wire.jsonl"
    assert paths.manifest_file(tmp_path, "att_1") == d / "wire-manifest.json"
    assert paths.blobs_dir(tmp_path, "att_1") == d / "wire-blobs"
    assert (
        paths.source_spool_file(tmp_path, "att_1", "mcp-stdio", "env1")
        == d / "wire-sources" / "mcp-stdio@env1.jsonl"
    )
    assert (
        paths.source_spool_file(tmp_path, "att_1", "capture-events")
        == d / "wire-sources" / "capture-events.jsonl"
    )


@pytest.mark.parametrize("bad", ["../att_2", "a/b", "..", ".", "att%2F1", "att 1", ""])
def test_bad_attempt_id_rejected(bad):
    with pytest.raises(paths.WirePathError):
        paths.attempt_dir(Path("/data"), bad)


def test_blob_ref_whitelist():
    good = "sha256-" + "a" * 64 + ".json.gz"
    assert paths.BLOB_REF_RE.match(good)
    for bad in [
        "sha256-" + "a" * 63 + ".json.gz",          # hex 长度不对
        "sha256-" + "A" * 64 + ".json.gz",          # 大写 hex
        "sha256-" + "a" * 64 + ".json",              # 缺 codec
        "sha256-" + "a" * 64 + ".json.tar",          # 未知 codec
        "../sha256-" + "a" * 64 + ".json.gz",       # 穿越
        "sha256-" + "a" * 64 + ".json.gz\n",        # 尾部注入
    ]:
        assert not paths.BLOB_REF_RE.match(bad), bad


def test_resolve_blob_path_traversal_guard(tmp_path):
    ref = "sha256-" + "b" * 64 + ".json.gz"
    resolved = paths.resolve_blob_path(tmp_path, "att_1", ref)
    assert resolved.parent == (tmp_path / "attempts" / "att_1" / "wire-blobs").resolve()
    with pytest.raises(paths.WirePathError):
        paths.resolve_blob_path(tmp_path, "att_1", "../wire.jsonl")
    with pytest.raises(paths.WirePathError):
        paths.resolve_blob_path(tmp_path, "../att_2", ref)


def test_blob_ref_for_round_trip():
    ref = paths.blob_ref_for("c" * 64, "gz")
    assert paths.BLOB_REF_RE.match(ref)
    with pytest.raises(paths.WirePathError):
        paths.blob_ref_for("c" * 64, "tar")
    with pytest.raises(paths.WirePathError):
        paths.blob_ref_for("not-hex" * 8, "gz")
