"""order-desk MCP server — the tool entry point for Claude Code / Codex.

Goes through the same HTTP path (the attempt server) any other agent would,
so trace files line up regardless of which agent ran. Does not import
core.py directly.

Launched by the adapter (see `claude --mcp-config` / codex `-c mcp_servers.*`):
    uv run --project /path/to/agent-arena python envs/order-desk/mcp_server.py

Environment variables:
    LANE_ATTEMPT_ID    — current attempt id
    LANE_SESSION_TOKEN — bearer token for the attempt server
    LANE_BASE_URL      — agent-arena backend address (e.g. http://127.0.0.1:8100)
"""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lane-order-desk")

ATTEMPT_ID = os.environ.get("LANE_ATTEMPT_ID", "")
SESSION_TOKEN = os.environ.get("LANE_SESSION_TOKEN", "")
BASE_URL = os.environ.get("LANE_BASE_URL", "http://127.0.0.1:8100")

_TIMEOUT = 30


def _call(tool_name: str, args: dict) -> dict:
    resp = httpx.post(
        f"{BASE_URL}/attempts/{ATTEMPT_ID}/tools/{tool_name}",
        headers={"Authorization": f"Bearer {SESSION_TOKEN}"},
        json=args,
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 400:
        try:
            upstream = resp.json()
        except ValueError:
            upstream = {"text": resp.text}
        return {"error": "env_server_error", "tool": tool_name, "status_code": resp.status_code, "upstream": upstream}
    return resp.json()


@mcp.tool()
def catalog_search(query: str, max_price: float | None = None) -> dict:
    """Search the book catalog by title/author.

    Args:
        query: Free-text search term matched against title/author.
        max_price: Optional upper price bound.
    """
    return _call("catalog_search", {"query": query, "max_price": max_price})


@mcp.tool()
def place_order(book_id: str, quantity: int = 1) -> dict:
    """Place an order for a book by id. Fails if out of stock.

    Args:
        book_id: The id returned by catalog_search, e.g. "BK-002".
        quantity: Number of copies to order, default 1.
    """
    return _call("place_order", {"book_id": book_id, "quantity": quantity})


if __name__ == "__main__":
    mcp.run()
