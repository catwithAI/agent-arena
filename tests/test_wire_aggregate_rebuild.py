"""W1-4/W1-5 验收：token 聚合回填 + 离线重建。

- W1-4：有/无 canonical calls 两分支；聚合值与 adapter 值冲突时双保留。
- W1-5：仓库内 Claude 历史 fixture 重建出调用级 llm_call，重复幂等且
  generation 递增；不触碰原始 events。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from backend.db import _init_db_sync, _open_sync
from backend.wire import aggregate, finalize, paths, rebuild
from backend.wire.normalizers.runner import run_native_normalizer
from backend.wire.policy import resolve_effective_policy

FIXTURE = Path(__file__).parent / "fixtures" / "wire" / "claude" / "events.jsonl"
ATT = "att_agg1"
POLICY = resolve_effective_policy(task_requested="metadata")


def _seed(tmp_path, *, adapter_usage=None) -> Path:
    db = tmp_path / "lane.db"
    _init_db_sync(db)
    with _open_sync(db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(id, env_name, prompt, created_at) "
            "VALUES('t', 'env', 'p', 'now')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO runs(id, task_id, env_name, status, created_at) "
            "VALUES('r', 't', 'env', 'running', 'now')"
        )
        conn.execute(
            "INSERT INTO attempts(id, run_id, task_id, env_name, agent_name, status,"
            " session_id, session_token_hash, token_usage_json, created_at) "
            "VALUES(?, 'r', 't', 'env', 'claude-code', 'completed', ?, 'h', ?, 'now')",
            (ATT, f"e-{ATT}", json.dumps(adapter_usage or {})),
        )
        conn.commit()
    d = paths.attempt_dir(tmp_path, ATT)
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, d / "events.jsonl")
    return db


def _finalize(tmp_path):
    run_native_normalizer(agent_name="claude-code", attempt_id=ATT, data_path=tmp_path)
    return finalize.finalize_attempt(
        data_path=tmp_path, attempt_id=ATT, policy=POLICY,
        declared_sources=[{"kind": "native-event", "instance": "native-event"}],
    )


# ---------- W1-4 聚合回填 -----------------------------------------------------

def test_aggregate_from_canonical_calls(tmp_path):
    db = _seed(tmp_path)
    _finalize(tmp_path)
    source = aggregate.backfill_token_usage(db, tmp_path, ATT)
    assert source == "wire"
    with sqlite3.connect(db) as conn:
        tu, ext = conn.execute(
            "SELECT token_usage_json, external_refs_json FROM attempts WHERE id=?",
            (ATT,),
        ).fetchone()
    ext = json.loads(ext)
    assert ext["token_usage_source"] == "wire"
    # 3 个 call: msg_1(in200,out40) + msg_2(in260,out15) + orphan(out3)
    assert ext["wire_token_usage"]["input_tokens"] == 460
    assert ext["wire_token_usage"]["output_tokens"] == 58
    assert json.loads(tu)["input_tokens"] == 460


def test_no_canonical_calls_falls_back_to_adapter(tmp_path):
    db = _seed(tmp_path, adapter_usage={"input_tokens": 99, "output_tokens": 9})
    # 不 finalize：无 wire.jsonl
    source = aggregate.backfill_token_usage(db, tmp_path, ATT)
    assert source == "adapter"
    with sqlite3.connect(db) as conn:
        tu, ext = conn.execute(
            "SELECT token_usage_json, external_refs_json FROM attempts WHERE id=?",
            (ATT,),
        ).fetchone()
    assert json.loads(ext)["token_usage_source"] == "adapter"
    assert json.loads(tu) == {"input_tokens": 99, "output_tokens": 9}  # 未改


def test_adapter_zero_usage_is_data_not_missing(tmp_path):
    """评审 M6：adapter 上报 0/0 是有效数据（区分 null），与 wire 460 冲突
    → 双保留，不当成「无 adapter 数据」而静默覆盖。"""
    db = _seed(tmp_path, adapter_usage={"input_tokens": 0, "output_tokens": 0})
    _finalize(tmp_path)
    aggregate.backfill_token_usage(db, tmp_path, ATT)
    with sqlite3.connect(db) as conn:
        tu, ext = conn.execute(
            "SELECT token_usage_json, external_refs_json FROM attempts WHERE id=?",
            (ATT,),
        ).fetchone()
    ext = json.loads(ext)
    assert ext["token_usage_conflict"]["adapter"]["input_tokens"] == 0
    assert json.loads(tu) == {"input_tokens": 0, "output_tokens": 0}  # 不被覆盖


def test_conflict_resolved_after_reparse_clears_stale(tmp_path):
    """parser 升级后 wire 与 adapter 一致：旧 token_usage_conflict 必须清除。"""
    db = _seed(tmp_path, adapter_usage={"input_tokens": 460, "output_tokens": 58})
    _finalize(tmp_path)
    # 先人为写入一个陈旧 conflict（模拟旧 parser 结果）
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE attempts SET external_refs_json=? WHERE id=?",
            (json.dumps({"token_usage_source": "wire",
                         "wire_token_usage": {"input_tokens": 999},
                         "token_usage_conflict": {"adapter": {"input_tokens": 1000},
                                                  "wire": {"input_tokens": 999}}}), ATT),
        )
        conn.commit()
    aggregate.backfill_token_usage(db, tmp_path, ATT)
    with sqlite3.connect(db) as conn:
        ext = json.loads(conn.execute(
            "SELECT external_refs_json FROM attempts WHERE id=?", (ATT,)
        ).fetchone()[0])
    assert "token_usage_conflict" not in ext  # 收敛为 resolved
    assert ext["wire_token_usage"]["input_tokens"] == 460
    assert ext["token_usage_source"] == "wire"


def test_wire_to_adapter_fallback_clears_wire_fields(tmp_path):
    """已有 wire 派生态的 attempt，重建时 wire 消失（无 canonical）→ 回退
    adapter 并清掉 wire_token_usage / token_usage_conflict。"""
    db = _seed(tmp_path, adapter_usage={"input_tokens": 77, "output_tokens": 7})
    # 先写入陈旧 wire 派生态
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE attempts SET external_refs_json=? WHERE id=?",
            (json.dumps({"token_usage_source": "wire",
                         "wire_token_usage": {"input_tokens": 460},
                         "token_usage_conflict": {"adapter": {"input_tokens": 77},
                                                  "wire": {"input_tokens": 460}}}), ATT),
        )
        conn.commit()
    # 无 wire.jsonl → wire_usage None → 回退 adapter
    source = aggregate.backfill_token_usage(db, tmp_path, ATT)
    assert source == "adapter"
    with sqlite3.connect(db) as conn:
        ext = json.loads(conn.execute(
            "SELECT external_refs_json FROM attempts WHERE id=?", (ATT,)
        ).fetchone()[0])
    assert "wire_token_usage" not in ext
    assert "token_usage_conflict" not in ext
    assert ext["token_usage_source"] == "adapter"


def test_conflict_dual_retained(tmp_path):
    db = _seed(tmp_path, adapter_usage={"input_tokens": 1000, "output_tokens": 200})
    _finalize(tmp_path)
    aggregate.backfill_token_usage(db, tmp_path, ATT)
    with sqlite3.connect(db) as conn:
        tu, ext = conn.execute(
            "SELECT token_usage_json, external_refs_json FROM attempts WHERE id=?",
            (ATT,),
        ).fetchone()
    ext = json.loads(ext)
    # 冲突：双保留，adapter 值不被覆盖
    assert ext["token_usage_conflict"]["adapter"]["input_tokens"] == 1000
    assert ext["token_usage_conflict"]["wire"]["input_tokens"] == 460
    assert json.loads(tu) == {"input_tokens": 1000, "output_tokens": 200}


# ---------- W1-5 离线重建 -----------------------------------------------------

def test_rebuild_produces_call_level_and_increments_generation(tmp_path):
    db = _seed(tmp_path)
    m1 = rebuild.rebuild_attempt(data_path=tmp_path, attempt_id=ATT, db_path=db)
    assert m1["totals"]["logical_calls"] == 3
    records = [
        json.loads(line)
        for line in paths.wire_file(tmp_path, ATT).read_text().splitlines()
    ]
    assert len([r for r in records if r["record_type"] == "llm_call"]) == 3

    # 重复重建幂等（内容一致）+ generation 递增
    wire_before = paths.wire_file(tmp_path, ATT).read_bytes()
    m2 = rebuild.rebuild_attempt(data_path=tmp_path, attempt_id=ATT, db_path=db)
    assert paths.wire_file(tmp_path, ATT).read_bytes() == wire_before
    assert m2["generation"] == m1["generation"] + 1


def test_rebuild_does_not_touch_raw_events(tmp_path):
    db = _seed(tmp_path)
    events_path = paths.attempt_dir(tmp_path, ATT) / "events.jsonl"
    before = events_path.read_bytes()
    rebuild.rebuild_attempt(data_path=tmp_path, attempt_id=ATT, db_path=db)
    assert events_path.read_bytes() == before


def test_rebuild_backfills_db_summary_and_usage(tmp_path):
    db = _seed(tmp_path)
    rebuild.rebuild_attempt(data_path=tmp_path, attempt_id=ATT, db_path=db)
    with sqlite3.connect(db) as conn:
        wire_status, wire_calls, ext = conn.execute(
            "SELECT wire_status, wire_call_count, external_refs_json"
            " FROM attempts WHERE id=?",
            (ATT,),
        ).fetchone()
    # fixture 含 1 坏行 → native source partial（parse-error completeness）
    assert wire_status == "partial" and wire_calls == 3
    assert json.loads(ext)["token_usage_source"] == "wire"


def test_rebuild_carries_adapter_usage_for_reconciliation(tmp_path):
    """评审 M4：offline rebuild 把 DB 里的历史 adapter usage 带入对账，
    与在线 finalize 得到相同的 reconciliation conflict。"""
    db = _seed(tmp_path, adapter_usage={"input_tokens": 9999, "output_tokens": 1})
    m = rebuild.rebuild_attempt(data_path=tmp_path, attempt_id=ATT, db_path=db)
    recon = [a for a in m["aggregates"] if a.get("scope") == "reconciliation"]
    assert recon and recon[0]["conflict"]["adapter"]["input_tokens"] == 9999
    assert recon[0]["conflict"]["native"]["input_tokens"] == 460
    assert any(g["field"] == "token_usage" for g in m["gaps"])


def test_rebuild_rejects_non_native_agent(tmp_path):
    db = _seed(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE attempts SET agent_name='third-party-agent' WHERE id=?", (ATT,))
        conn.commit()
    import pytest

    with pytest.raises(ValueError):
        rebuild.rebuild_attempt(data_path=tmp_path, attempt_id=ATT, db_path=db)
