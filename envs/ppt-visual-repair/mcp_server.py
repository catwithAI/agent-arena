"""Minimal MCP server for the PPT human-taste evaluation env."""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lane-ppt-visual-repair")

ATTEMPT_ID = os.environ.get("LANE_ATTEMPT_ID", "")
ENV_TOKEN = os.environ.get("LANE_SESSION_TOKEN", "")
BASE_URL = os.environ.get("LANE_BASE_URL", "http://127.0.0.1:8100")


def _call(tool_name: str, args: dict | None = None) -> dict:
    response = httpx.post(
        f"{BASE_URL}/attempts/{ATTEMPT_ID}/tools/{tool_name}",
        headers={"Authorization": f"Bearer {ENV_TOKEN}"},
        json=args or {},
        timeout=30,
    )
    if response.status_code >= 400:
        try:
            upstream = response.json()
        except ValueError:
            upstream = {"text": response.text}
        return {
            "error": "env_server_error",
            "tool": tool_name,
            "status_code": response.status_code,
            "upstream": upstream,
        }
    return response.json()


@mcp.tool()
def task_brief() -> dict:
    """Return the task contract and expected output file."""
    return _call("task_brief")


@mcp.tool()
def workspace_status() -> dict:
    """Report whether expected PPT input/output files exist in the workspace."""
    return _call("workspace_status")


@mcp.tool()
def annotate_pptx(
    input_path: str = "draft.pptx",
    output_path: str = "draft_annotated.pptx",
    manifest_path: str = "object_manifest.json",
) -> dict:
    """Create an annotated PPTX with object IDs, red boxes, and a scale bar."""
    return _call(
        "annotate_pptx",
        {
            "input_path": input_path,
            "output_path": output_path,
            "manifest_path": manifest_path,
        },
    )


if __name__ == "__main__":
    mcp.run()
