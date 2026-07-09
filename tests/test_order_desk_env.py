from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.env_loader import load_env
from lane.env_api import EnvContext, TraceWriter


def _make_ctx(tmp_path: Path, attempt_id: str = "att_1") -> EnvContext:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    trace = TraceWriter(data_path=tmp_path, attempt_id=attempt_id, session_id="sess_1")
    return EnvContext(attempt_id=attempt_id, session_id="sess_1", db=conn, trace=trace)


async def test_catalog_search_finds_matching_book(tmp_path: Path):
    env = load_env(Path("envs/order-desk"))
    ctx = _make_ctx(tmp_path)
    result = await env.tools["catalog_search"].call(ctx, query="algorithms")
    assert any("Algorithms" in b["title"] for b in result["results"])


async def test_catalog_search_respects_max_price(tmp_path: Path):
    env = load_env(Path("envs/order-desk"))
    ctx = _make_ctx(tmp_path)
    result = await env.tools["catalog_search"].call(ctx, query="a", max_price=40)
    assert all(b["price"] <= 40 for b in result["results"])


async def test_place_order_confirms_and_writes_row(tmp_path: Path):
    env = load_env(Path("envs/order-desk"))
    ctx = _make_ctx(tmp_path)
    result = await env.tools["place_order"].call(ctx, book_id="BK-002", quantity=1)
    assert result["status"] == "confirmed"
    row = ctx.db.execute("SELECT * FROM env_orders WHERE order_id=?", (result["order_id"],)).fetchone()
    assert row is not None


async def test_place_order_rejects_out_of_stock(tmp_path: Path):
    env = load_env(Path("envs/order-desk"))
    ctx = _make_ctx(tmp_path)
    result = await env.tools["place_order"].call(ctx, book_id="BK-004", quantity=1)
    assert result.get("is_error") is True


async def test_scorer_scores_confirmed_order_within_budget(tmp_path: Path):
    env = load_env(Path("envs/order-desk"))
    db_path = tmp_path / "env.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ctx = EnvContext(
        attempt_id="att_1",
        session_id="sess_1",
        db=conn,
        trace=TraceWriter(data_path=tmp_path, attempt_id="att_1", session_id="sess_1"),
    )
    await env.tools["place_order"].call(ctx, book_id="BK-002", quantity=1)
    conn.close()

    scores = env.scorer(
        attempt_id="att_1",
        task={"constraints": {"max_total_price": 60}},
        env_db=db_path,
        trace=[],
        final_state={},
    )
    by_dim = {s["dimension"]: s["value"] for s in scores}
    assert by_dim["task_completion"] == 90
    assert by_dim["constraint_compliance"] == 100
