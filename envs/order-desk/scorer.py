"""order-desk scorer.

Signature contract:

    score(*, attempt_id, task, env_db, trace, final_state) -> list[dict]

Returns a value per dimension (0-100).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def _query_orders(env_db: Path) -> list[dict[str, Any]]:
    if not env_db.exists():
        return []
    conn = sqlite3.connect(env_db)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT order_id, status, book_id, title, quantity, unit_price, total_price"
            " FROM env_orders WHERE status = 'confirmed'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def score(
    *,
    attempt_id: str,
    task: dict[str, Any],
    env_db: Path,
    trace: list[dict[str, Any]],
    final_state: dict[str, Any],
) -> list[dict[str, Any]]:
    orders = _query_orders(env_db)

    if orders:
        completion = 90
        completion_detail = "placed order(s): " + ", ".join(o["order_id"] for o in orders)
    else:
        completion = 0
        completion_detail = "no confirmed order was placed"

    budget = (task.get("constraints") or {}).get("max_total_price")
    if not orders:
        compliance = 0
        compliance_detail = "no order to check against budget"
    elif budget is None:
        compliance = 100
        compliance_detail = "no budget constraint set on this task"
    else:
        total = sum(o["total_price"] for o in orders)
        if total <= budget:
            compliance = 100
            compliance_detail = f"total {total} within budget {budget}"
        else:
            compliance = 0
            compliance_detail = f"total {total} exceeds budget {budget}"

    return [
        {"dimension": "task_completion", "value": completion, "detail": completion_detail},
        {"dimension": "constraint_compliance", "value": compliance, "detail": compliance_detail},
    ]
