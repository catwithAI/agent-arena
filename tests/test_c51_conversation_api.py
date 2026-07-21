"""C5-1 验收：attempt conversation API（design §9，R10.1/R11.2）。

detail API 返回 conversation summary/turns/evaluation；缺 conversation.jsonl 的历史
attempt 返回 legacy summary + 空 turns；prompt 默认不返回（只 bytes/hash）；partial
trace 与权限边界。
"""

from __future__ import annotations

import json
from pathlib import Path


def _attempt_dir(test_app, attempt_id: str) -> Path:
    app, _client = test_app
    data_path = Path(app.state.settings.lane.data_path)
    d = data_path / "attempts" / attempt_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_conversation(attempt_dir: Path, lines: list[dict]) -> None:
    (attempt_dir / "conversation.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
    )


def _write_wire(attempt_dir: Path, manifest: dict, records: list[dict]) -> None:
    (attempt_dir / "wire-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (attempt_dir / "wire.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n" if records else "",
        encoding="utf-8",
    )


# ---------- legacy attempt（无 conversation.jsonl）--------------------------


def test_legacy_attempt_returns_legacy_summary(test_client, completed_run):
    resp = test_client.get(
        f"/api/runs/{completed_run.id}/attempts/{completed_run.attempt_id}"
    )
    assert resp.status_code == 200
    conv = resp.json()["conversation"]
    assert conv["summary"]["is_legacy"] is True
    assert conv["summary"]["turn_count"] == 1
    assert conv["turns"] == []
    # 无 wire → 采集不完整，状态 incomplete（不冒充 observed/not_observed）。
    assert conv["evaluation"]["compaction_status"] == "incomplete"


# ---------- 多轮 attempt：summary + turns + evaluation ----------------------


def _seed_multiturn(test_app, completed_run):
    attempt_dir = _attempt_dir(test_app, completed_run.attempt_id)
    _write_conversation(attempt_dir, [
        {"schema_version": "lane-conversation-v1", "event": "conversation.started",
         "attempt_id": completed_run.attempt_id, "turn_count": 3,
         "score_turn_id": "probe", "is_legacy": False},
        {"event": "turn.started", "turn_id": "setup", "turn_index": 0,
         "purpose": "setup", "producer_session_id": "sess-1",
         "prompt_bytes": 1200, "prompt_hash": "sha256:aaa",
         "timestamp": "2026-07-20T09:00:00Z"},
        {"event": "turn.completed", "turn_id": "setup", "turn_index": 0,
         "purpose": "setup", "producer_session_id": "sess-1",
         "timestamp": "2026-07-20T09:00:05Z"},
        {"event": "turn.started", "turn_id": "pressure", "turn_index": 1,
         "purpose": "pressure", "producer_session_id": "sess-1",
         "prompt_bytes": 90000, "prompt_hash": "sha256:bbb",
         "timestamp": "2026-07-20T09:00:10Z"},
        {"event": "turn.completed", "turn_id": "pressure", "turn_index": 1,
         "purpose": "pressure", "producer_session_id": "sess-1",
         "timestamp": "2026-07-20T09:00:20Z"},
        {"event": "turn.started", "turn_id": "probe", "turn_index": 2,
         "purpose": "probe", "producer_session_id": "sess-1",
         "prompt_bytes": 300, "prompt_hash": "sha256:ccc",
         "timestamp": "2026-07-20T09:00:25Z"},
        {"event": "turn.completed", "turn_id": "probe", "turn_index": 2,
         "purpose": "probe", "producer_session_id": "sess-1",
         "timestamp": "2026-07-20T09:00:30Z"},
    ])
    _write_wire(
        attempt_dir,
        {"status": "complete", "sources": [], "gaps": []},
        [{"record_type": "context_compaction", "attempt_id": completed_run.attempt_id,
          "data": {"before_call_id": "lc1", "after_call_id": "lc2",
                   "before_turn_id": "pressure", "after_turn_id": "probe"}}],
    )
    return attempt_dir


def test_multiturn_summary_turns_evaluation(test_client, test_app, completed_run):
    _seed_multiturn(test_app, completed_run)
    resp = test_client.get(
        f"/api/runs/{completed_run.id}/attempts/{completed_run.attempt_id}"
    )
    assert resp.status_code == 200
    conv = resp.json()["conversation"]

    # summary
    assert conv["summary"]["is_legacy"] is False
    assert conv["summary"]["turn_count"] == 3
    assert conv["summary"]["completed_turn_count"] == 3
    assert conv["summary"]["session_continuity"] == "continuous"
    assert conv["summary"]["score_turn_id"] == "probe"

    # turns（按 turn_index 排序）
    turns = conv["turns"]
    assert [t["turn_id"] for t in turns] == ["setup", "pressure", "probe"]
    assert [t["turn_index"] for t in turns] == [0, 1, 2]
    assert [t["status"] for t in turns] == ["completed"] * 3
    assert [t["purpose"] for t in turns] == ["setup", "pressure", "probe"]

    # evaluation（有 compaction record → observed）
    assert conv["evaluation"]["compaction_status"] == "observed"
    assert conv["evaluation"]["compaction_count"] == 1


# ---------- prompt 默认不返回（只 bytes/hash，R9.1）------------------------


def test_turns_do_not_leak_prompt(test_client, test_app, completed_run):
    _seed_multiturn(test_app, completed_run)
    resp = test_client.get(
        f"/api/runs/{completed_run.id}/attempts/{completed_run.attempt_id}"
    )
    turns = resp.json()["conversation"]["turns"]
    for t in turns:
        # 非敏感投影在。
        assert "prompt_bytes" in t and "prompt_hash" in t
        # prompt 原文绝不出现。
        assert "prompt" not in t
    setup = next(t for t in turns if t["turn_id"] == "setup")
    assert setup["prompt_bytes"] == 1200
    assert setup["prompt_hash"] == "sha256:aaa"


# ---------- partial trace（截断尾行）---------------------------------------


def test_partial_conversation_flagged(test_client, test_app, completed_run):
    attempt_dir = _attempt_dir(test_app, completed_run.attempt_id)
    (attempt_dir / "conversation.jsonl").write_text(
        json.dumps({"event": "conversation.started", "attempt_id": completed_run.attempt_id,
                    "turn_count": 2, "is_legacy": False}) + "\n"
        + json.dumps({"event": "turn.started", "turn_id": "setup", "turn_index": 0,
                      "purpose": "setup", "producer_session_id": "s1",
                      "timestamp": "2026-07-20T09:00:00Z"}) + "\n"
        + '{"event": "turn.started", "turn_id": "pr',  # 截断尾行
        encoding="utf-8",
    )
    resp = test_client.get(
        f"/api/runs/{completed_run.id}/attempts/{completed_run.attempt_id}"
    )
    conv = resp.json()["conversation"]
    assert conv["summary"]["partial"] is True
    # 完整的那一轮仍可读。
    assert [t["turn_id"] for t in conv["turns"]] == ["setup"]


# ---------- 权限边界：conversation 不绕过 run/attempt 归属 -----------------


def test_conversation_respects_run_attempt_binding(test_client, completed_run):
    # 用错误的 run_id 请求 → 仍 404，conversation 块不会泄漏。
    resp = test_client.get(
        f"/api/runs/wrong_run/attempts/{completed_run.attempt_id}"
    )
    assert resp.status_code == 404


# ---------- session broken → evaluation unsupported ------------------------


def test_session_broken_yields_unsupported(test_client, test_app, completed_run):
    attempt_dir = _attempt_dir(test_app, completed_run.attempt_id)
    _write_conversation(attempt_dir, [
        {"event": "conversation.started", "attempt_id": completed_run.attempt_id,
         "turn_count": 2, "is_legacy": False},
        {"event": "turn.started", "turn_id": "setup", "turn_index": 0,
         "purpose": "setup", "producer_session_id": "sess-A",
         "timestamp": "2026-07-20T09:00:00Z"},
        {"event": "turn.completed", "turn_id": "setup", "turn_index": 0,
         "producer_session_id": "sess-A", "timestamp": "2026-07-20T09:00:05Z"},
        {"event": "turn.started", "turn_id": "probe", "turn_index": 1,
         "purpose": "probe", "producer_session_id": "sess-B",  # session 变 → broken
         "timestamp": "2026-07-20T09:00:10Z"},
        {"event": "turn.completed", "turn_id": "probe", "turn_index": 1,
         "producer_session_id": "sess-B", "timestamp": "2026-07-20T09:00:15Z"},
    ])
    _write_wire(attempt_dir, {"status": "complete", "sources": [], "gaps": []}, [])
    resp = test_client.get(
        f"/api/runs/{completed_run.id}/attempts/{completed_run.attempt_id}"
    )
    conv = resp.json()["conversation"]
    assert conv["summary"]["session_continuity"] == "broken"
    # 无 record + session broken → unsupported。
    assert conv["evaluation"]["compaction_status"] == "unsupported"
