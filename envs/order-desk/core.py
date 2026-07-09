"""order-desk business logic.

Design:
- Mock data (a small book catalog) lives at module scope; no external data
  source is needed for a benchmark env.
- Every tool is decorated with `@env_tool`; the wrapper writes the trace
  line automatically. Business functions never write their own trace.
- Side effects (order rows / call logs) go through `ctx.db`, a sqlite
  Connection whose schema is `schema.sql`, applied on first use.
- Return values must be JSON-serializable dicts — they're written straight
  to trace.jsonl and returned to the agent as-is.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from lane.env_api import EnvContext, env_tool

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


_CATALOG: list[dict[str, Any]] = [
    {"book_id": "BK-001", "title": "Introduction to Algorithms", "author": "Cormen et al.", "price": 89.99, "stock": 4},
    {"book_id": "BK-002", "title": "The Pragmatic Programmer", "author": "Hunt & Thomas", "price": 42.50, "stock": 10},
    {"book_id": "BK-003", "title": "Designing Data-Intensive Applications", "author": "Kleppmann", "price": 55.00, "stock": 2},
    {"book_id": "BK-004", "title": "Clean Code", "author": "Martin", "price": 38.75, "stock": 0},
    {"book_id": "BK-005", "title": "Structure and Interpretation of Computer Programs", "author": "Abelson & Sussman", "price": 61.20, "stock": 6},
]


def _log_api(ctx: EnvContext, api_name: str, request: dict[str, Any], response: dict[str, Any], is_error: bool) -> None:
    ctx.db.execute(
        "INSERT INTO env_api_logs(session_id, api_name, request_data, response_data, is_error)"
        " VALUES(?, ?, ?, ?, ?)",
        (ctx.session_id, api_name, json.dumps(request, ensure_ascii=False), json.dumps(response, ensure_ascii=False), 1 if is_error else 0),
    )
    ctx.db.commit()


_CATALOG_SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Free-text search over title/author."},
        "max_price": {"type": "number", "description": "Optional upper price bound."},
    },
    "required": ["query"],
}


@env_tool(
    name="catalog_search",
    description="Search the book catalog by title/author, optionally capped by max_price.",
    parameters=_CATALOG_SEARCH_PARAMETERS,
)
def catalog_search(ctx: EnvContext, query: str, max_price: float | None = None) -> dict[str, Any]:
    _ensure_schema(ctx.db)
    request = {"query": query, "max_price": max_price}
    q = query.strip().lower()
    results = [
        b
        for b in _CATALOG
        if (q in b["title"].lower() or q in b["author"].lower())
        and (max_price is None or b["price"] <= max_price)
    ]
    response = {"query": query, "results": results}
    _log_api(ctx, "catalog_search", request, response, is_error=False)
    return response


_PLACE_ORDER_PARAMETERS = {
    "type": "object",
    "properties": {
        "book_id": {"type": "string"},
        "quantity": {"type": "integer", "default": 1, "minimum": 1},
    },
    "required": ["book_id"],
}


@env_tool(
    name="place_order",
    description="Place an order for a book by id. Fails if out of stock.",
    parameters=_PLACE_ORDER_PARAMETERS,
)
def place_order(ctx: EnvContext, book_id: str, quantity: int = 1) -> dict[str, Any]:
    _ensure_schema(ctx.db)
    request = {"book_id": book_id, "quantity": quantity}

    book = next((b for b in _CATALOG if b["book_id"] == book_id), None)
    if book is None:
        response = {"error": f"unknown book_id: {book_id}", "is_error": True}
        _log_api(ctx, "place_order", request, response, is_error=True)
        return response
    if book["stock"] < quantity:
        response = {"error": f"insufficient stock for {book_id}: have {book['stock']}, want {quantity}", "is_error": True}
        _log_api(ctx, "place_order", request, response, is_error=True)
        return response

    order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
    total_price = round(book["price"] * quantity, 2)
    ctx.db.execute(
        "INSERT INTO env_orders(session_id, order_id, status, book_id, title, quantity,"
        " unit_price, total_price, currency, raw_data)"
        " VALUES(?, ?, 'confirmed', ?, ?, ?, ?, ?, 'USD', ?)",
        (ctx.session_id, order_id, book_id, book["title"], quantity, book["price"], total_price, json.dumps(book, ensure_ascii=False)),
    )
    ctx.db.commit()

    response = {
        "order_id": order_id,
        "status": "confirmed",
        "book_id": book_id,
        "title": book["title"],
        "quantity": quantity,
        "unit_price": book["price"],
        "total_price": total_price,
    }
    _log_api(ctx, "place_order", request, response, is_error=False)
    return response
