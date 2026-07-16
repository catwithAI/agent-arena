"""W0-6 验收：DB 摘要 + wire API + SSE 签名。

覆盖 tasks.md W0-6 验收清单：
- 分页/过滤/409 wire_changed/404/traversal/policy 阻断 blob；
- 老库迁移幂等；
- 相同 record count 的 rebuild 仍递增 generation，旧 cursor 409 且 SSE 签名变化；
- wire 产物排除普通 artifact 列表与文件接口（R12.6，评审 m1）。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from backend import runtime_state
from backend.wire import evidence, finalize, paths, spool, writer
from backend.wire.policy import resolve_effective_policy


def _paths():
    state = runtime_state.get()
    return state.data_path, state.db_path


def _seed_attempt(test_client, agents=("claude-code",)) -> tuple[str, str]:
    """直接在 DB 建 run+attempt（不走 POST /runs 的后台 dispatch），避免
    dispatch 的 wire capture 产出干扰「无 wire」类断言。"""
    from backend import runtime_state
    from backend.db import _open_sync

    state = runtime_state.get()
    run_id = f"run_wireapi_{_next_id()}"
    aid = f"att_{_next_id()}"
    with _open_sync(state.db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(id, env_name, prompt, created_at) "
            "VALUES('strike_001', 'carrier-strike', 'p', 'now')"
        )
        conn.execute(
            "INSERT INTO runs(id, task_id, env_name, status, created_at) "
            "VALUES(?, 'strike_001', 'carrier-strike', 'completed', 'now')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO attempts(id, run_id, task_id, env_name, agent_name, status,"
            " session_id, session_token_hash, created_at) "
            "VALUES(?, ?, 'strike_001', 'carrier-strike', ?, 'completed', ?, 'h', 'now')",
            (aid, run_id, agents[0], f"sess-{aid}"),
        )
        conn.commit()
    return run_id, aid


_ID_COUNTER = [0]


def _next_id() -> str:
    _ID_COUNTER[0] += 1
    return f"{_ID_COUNTER[0]:012x}"


def _ev(attempt_id: str, i: int, *, phase="agent_run", record_type="native_llm_call",
        hints=None, payload=None) -> dict:
    return {
        "evidence_id": f"we_{i}",
        "attempt_id": attempt_id,
        "phase": phase,
        "evidence_type": record_type,
        "source": {"kind": "native-event", "instance": "native-event"},
        "producer": {"name": "test"},
        "time": {"observed_at": f"2026-07-13T00:00:{i:02d}.000Z"},
        "raw_ref": None,
        "correlation_hints": hints or {},
        "capabilities": {},
        "redaction": {"policy": "metadata", "status": "applied"},
        "errors": [],
        "extensions": {},
        "payload": {**evidence.null_payload(record_type), **(payload or {})},
    }


def _write_wire(data_path, attempt_id, n_calls=3, policy="metadata"):
    w = spool.SpoolWriter(
        paths.source_spool_file(data_path, attempt_id, "native-event"),
        expected_attempt_id=attempt_id,
    )
    for i in range(n_calls):
        w.append(_ev(attempt_id, i, hints={"producer_call_id": f"msg_{i}"}))
    w.close()
    return finalize.finalize_attempt(
        data_path=data_path,
        attempt_id=attempt_id,
        policy=resolve_effective_policy(task_requested=policy),
    )


# ---------- wire 列表：分页 / 过滤 / cursor ----------------------------------

def test_wire_not_available_before_capture(test_client):
    run_id, attempt_id = _seed_attempt(test_client)
    resp = test_client.get(f"/api/runs/{run_id}/attempts/{attempt_id}/wire")
    assert resp.status_code == 200
    assert resp.json() == {
        "items": [], "next_cursor": None, "manifest_status": "not_available",
    }
    assert test_client.get(
        f"/api/runs/{run_id}/attempts/{attempt_id}/wire/manifest"
    ).json() == {"status": "not_available"}
    assert test_client.get(
        f"/api/runs/{run_id}/attempts/{attempt_id}/wire/trajectory"
    ).json() == {"status": "not_available", "steps": []}


def test_wire_trajectory_index(test_client):
    """W1-9：trajectory 走受 attempt/run guard 保护的专用只读接口。"""
    run_id, attempt_id = _seed_attempt(test_client)
    data_path, _ = _paths()
    trajectory = {
        "schema_version": "lane-trajectory-v1",
        "attempt_id": attempt_id,
        "steps": [{
            "step_id": "ts_1", "sequence": 1, "kind": "tool_call",
            "logical_call_id": "lc_1", "tool_call_id": "tool_1",
        }],
    }
    writer.atomic_write_json(
        paths.attempt_dir(data_path, attempt_id) / "trajectory.json", trajectory
    )
    resp = test_client.get(
        f"/api/runs/{run_id}/attempts/{attempt_id}/wire/trajectory"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "complete"
    assert body["schema_version"] == "lane-trajectory-v1"
    assert body["steps"] == trajectory["steps"]
    assert test_client.get(
        f"/api/runs/run_wrong/attempts/{attempt_id}/wire/trajectory"
    ).status_code == 404


def test_wire_pagination_and_filters(test_client):
    run_id, attempt_id = _seed_attempt(test_client)
    data_path, _ = _paths()
    _write_wire(data_path, attempt_id, n_calls=5)

    base = f"/api/runs/{run_id}/attempts/{attempt_id}/wire"
    page1 = test_client.get(base, params={"limit": 2}).json()
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    page2 = test_client.get(base, params={"limit": 2, "cursor": page1["next_cursor"]}).json()
    assert len(page2["items"]) == 2
    ids1 = {r["record_id"] for r in page1["items"]}
    ids2 = {r["record_id"] for r in page2["items"]}
    assert not ids1 & ids2
    # 过滤
    only_calls = test_client.get(base, params={"record_type": "llm_call"}).json()
    assert len(only_calls["items"]) == 5
    none = test_client.get(base, params={"record_type": "mcp_frame"}).json()
    assert none["items"] == []
    by_phase = test_client.get(base, params={"phase": "verification"}).json()
    assert by_phase["items"] == []
    lc = only_calls["items"][0]["correlation"]["logical_call_id"]
    by_lc = test_client.get(base, params={"logical_call_id": lc}).json()
    assert len(by_lc["items"]) == 1
    assert only_calls["manifest_status"] == "complete"


def test_wire_404_for_wrong_run(test_client):
    run_id, attempt_id = _seed_attempt(test_client)
    _write_wire(_paths()[0], attempt_id)
    resp = test_client.get(f"/api/runs/run_nonexistent/attempts/{attempt_id}/wire")
    assert resp.status_code == 404


def test_wire_invalid_cursor_400(test_client):
    run_id, attempt_id = _seed_attempt(test_client)
    _write_wire(_paths()[0], attempt_id)
    resp = test_client.get(
        f"/api/runs/{run_id}/attempts/{attempt_id}/wire", params={"cursor": "!!!"}
    )
    assert resp.status_code == 400


def test_wire_cursor_offset_bounds(test_client):
    """负数/越界/行中间 offset 一律 400，不静默丢记录（评审 M4）。"""
    import base64

    run_id, attempt_id = _seed_attempt(test_client)
    data_path, _ = _paths()
    m = _write_wire(data_path, attempt_id, n_calls=2)
    gen = m["generation"]
    base = f"/api/runs/{run_id}/attempts/{attempt_id}/wire"

    def cur(offset):
        raw = json.dumps({"offset": offset, "generation": gen}).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    for bad_offset in (-1, 10**9, 3):  # 负数 / 越界 / 行中间
        resp = test_client.get(base, params={"cursor": cur(bad_offset)})
        assert resp.status_code == 400, bad_offset


# ---------- rebuild → generation 递增、旧 cursor 409、SSE 签名变化 ------------

def test_rebuild_same_count_increments_generation_and_409(test_client):
    from backend.api import _attempt_change_signature

    run_id, attempt_id = _seed_attempt(test_client)
    data_path, _ = _paths()
    m1 = _write_wire(data_path, attempt_id, n_calls=3)
    base = f"/api/runs/{run_id}/attempts/{attempt_id}/wire"
    page = test_client.get(base, params={"limit": 2}).json()
    old_cursor = page["next_cursor"]
    sig1 = _attempt_change_signature(data_path / "attempts" / attempt_id)

    # rebuild：record count 与文件大小完全相同
    m2 = finalize.finalize_attempt(
        data_path=data_path, attempt_id=attempt_id,
        policy=resolve_effective_policy(task_requested="metadata"),
    )
    assert m2["generation"] == m1["generation"] + 1
    assert m2["totals"]["records"] == m1["totals"]["records"]

    resp = test_client.get(base, params={"limit": 2, "cursor": old_cursor})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "wire_changed"
    # SSE 签名必须变化（generation 进签名，不依赖 mtime/size）
    sig2 = _attempt_change_signature(data_path / "attempts" / attempt_id)
    assert sig1 != sig2


def test_wire_same_size_replacement_409(test_client):
    """同尺寸替换 + manifest 未换：只比尺寸挡不住，必须比内容 SHA-256。"""
    run_id, attempt_id = _seed_attempt(test_client)
    data_path, _ = _paths()
    _write_wire(data_path, attempt_id, n_calls=1)
    wire_path = paths.wire_file(data_path, attempt_id)
    data = wire_path.read_bytes()
    # 翻转 record_id 里的一个字符，长度不变
    idx = data.index(b"wr_") + 3
    flip = b"0" if data[idx : idx + 1] != b"0" else b"1"
    wire_path.write_bytes(data[:idx] + flip + data[idx + 1 :])
    assert wire_path.stat().st_size == len(data)
    assert wire_path.read_bytes() != data
    resp = test_client.get(f"/api/runs/{run_id}/attempts/{attempt_id}/wire")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "wire_changed"


def test_wire_pagination_does_not_slurp_whole_file(test_client, monkeypatch):
    """§19.2：分页不得整份读入——即使 Path.read_bytes 被禁用也能正常分页。"""
    from pathlib import Path

    run_id, attempt_id = _seed_attempt(test_client)
    data_path, _ = _paths()
    _write_wire(data_path, attempt_id, n_calls=5)

    orig = Path.read_bytes

    def _guard_read_bytes(self):
        if self.name == "wire.jsonl":
            raise AssertionError("分页路径不应整份 read_bytes(wire.jsonl)")
        return orig(self)

    monkeypatch.setattr(Path, "read_bytes", _guard_read_bytes)
    base = f"/api/runs/{run_id}/attempts/{attempt_id}/wire"
    page1 = test_client.get(base, params={"limit": 2})
    assert page1.status_code == 200
    body = page1.json()
    assert len(body["items"]) == 2 and body["next_cursor"]
    page2 = test_client.get(base, params={"limit": 2, "cursor": body["next_cursor"]})
    assert page2.status_code == 200 and len(page2.json()["items"]) == 2


def test_wire_fingerprint_mismatch_409(test_client):
    """finalizer「新 wire + 旧 manifest」中间窗口：尺寸与指纹不符即 409，
    不返回新旧混合快照（评审 M1）。"""
    run_id, attempt_id = _seed_attempt(test_client)
    data_path, _ = _paths()
    _write_wire(data_path, attempt_id, n_calls=2)
    wire_path = paths.wire_file(data_path, attempt_id)
    # 模拟 finalize 已换 wire、还没换 manifest
    with wire_path.open("ab") as fh:
        fh.write(b'{"record_id":"wr_new"}\n')
    resp = test_client.get(f"/api/runs/{run_id}/attempts/{attempt_id}/wire")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "wire_changed"


# ---------- blob：policy / traversal ------------------------------------------

def _enable_blob_api(test_app):
    test_app[0].state.settings.lane.wire_blob_api_enabled = True


def test_blob_api_disabled_by_default(test_client, test_app):
    """design §19.3：无用户级 auth 时 blob API 默认禁用，full 档也 404。"""
    assert test_app[0].state.settings.lane.wire_blob_api_enabled is False
    run_id, attempt_id = _seed_attempt(test_client)
    data_path, _ = _paths()
    blob = writer.BlobWriter(data_path, attempt_id).write_json({"x": 1})
    _write_wire(data_path, attempt_id, policy="full")
    resp = test_client.get(
        f"/api/runs/{run_id}/attempts/{attempt_id}/wire/blobs/{blob.ref}"
    )
    assert resp.status_code == 404


def test_blob_blocked_under_metadata_policy(test_client, test_app):
    _enable_blob_api(test_app)
    run_id, attempt_id = _seed_attempt(test_client)
    data_path, _ = _paths()
    blob = writer.BlobWriter(data_path, attempt_id).write_json({"x": 1})
    _write_wire(data_path, attempt_id, policy="metadata")
    resp = test_client.get(
        f"/api/runs/{run_id}/attempts/{attempt_id}/wire/blobs/{blob.ref}"
    )
    assert resp.status_code == 404  # metadata 档 blob endpoint 一律 404


def test_blob_served_under_full_policy(test_client, test_app):
    _enable_blob_api(test_app)
    run_id, attempt_id = _seed_attempt(test_client)
    data_path, _ = _paths()
    blob = writer.BlobWriter(data_path, attempt_id).write_json({"x": 1})
    _write_wire(data_path, attempt_id, policy="full")
    resp = test_client.get(
        f"/api/runs/{run_id}/attempts/{attempt_id}/wire/blobs/{blob.ref}"
    )
    assert resp.status_code == 200
    assert resp.json() == {"x": 1}


@pytest.mark.parametrize("bad_ref", [
    "../wire.jsonl",
    "..%2Fwire.jsonl",
    "sha256-" + "a" * 63 + ".json.gz",
    "sha256-" + "a" * 64 + ".json.tar",
])
def test_blob_traversal_and_bad_ref_404(test_client, test_app, bad_ref):
    _enable_blob_api(test_app)
    run_id, attempt_id = _seed_attempt(test_client)
    _write_wire(_paths()[0], attempt_id, policy="full")
    resp = test_client.get(
        f"/api/runs/{run_id}/attempts/{attempt_id}/wire/blobs/{bad_ref}"
    )
    assert resp.status_code == 404


# ---------- artifact 隔离（R12.6，评审 m1）-----------------------------------

def test_wire_files_excluded_from_artifacts(test_client):
    run_id, attempt_id = _seed_attempt(test_client)
    data_path, _ = _paths()
    writer.BlobWriter(data_path, attempt_id).write_json({"secret-ish": 1})
    _write_wire(data_path, attempt_id)
    # 在产物根（skill_workspace）放一个真实产物，验证它出现但 wire 框架产物被隔离
    workspace = data_path / "attempts" / attempt_id / "skill_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "result.txt").write_text("ok")

    listing = test_client.get(
        f"/api/runs/{run_id}/attempts/{attempt_id}/artifacts"
    ).json()
    listed = json.dumps(listing)
    assert "wire.jsonl" not in listed
    assert "wire-manifest.json" not in listed
    assert "wire-sources" not in listed and "wire-blobs" not in listed
    assert "result.txt" in listed
    # artifact 文件接口也不放行
    for p in ("wire.jsonl", "wire-sources/native-event.jsonl"):
        resp = test_client.get(
            f"/api/runs/{run_id}/attempts/{attempt_id}/artifacts/{p}"
        )
        assert resp.status_code == 404, p


# ---------- DB 迁移幂等 + 摘要列 ----------------------------------------------

def test_wire_columns_migration_idempotent(tmp_path):
    from backend.db import _init_db_sync

    db = tmp_path / "lane.db"
    _init_db_sync(db)
    _init_db_sync(db)  # 幂等
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    assert {
        "wire_status", "wire_record_count", "wire_call_count",
        "wire_error_count", "wire_manifest_version",
    } <= cols


def test_update_db_summary(test_client):
    run_id, attempt_id = _seed_attempt(test_client)
    data_path, db_path = _paths()
    manifest = _write_wire(data_path, attempt_id, n_calls=2)
    finalize.update_db_summary(db_path, attempt_id, manifest)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT wire_status, wire_record_count, wire_call_count,"
            " wire_manifest_version FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
    assert row[0] == "complete"
    assert row[1] == 2 and row[2] == 2
    assert row[3] == "lane-wire-manifest-v1"
