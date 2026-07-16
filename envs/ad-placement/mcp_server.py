"""Minimal MCP helper for the ad-placement coding env."""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lane-ad-placement")


@mcp.tool()
def problem_summary() -> dict:
    """Return the task contract and expected final file."""
    return {
        "final_file": "solution.cpp",
        "language": "C++17",
        "compile": "g++ -std=c++17 -O2 -o solution solution.cpp",
        "input": "n followed by n rows: x_i y_i r_i",
        "output": "n rows: a_i b_i c_i d_i",
        "constraints": [
            "0 <= a_i < c_i <= 10000",
            "0 <= b_i < d_i <= 10000",
            "rectangles must not overlap with positive area",
            "rectangle i should contain (x_i + 0.5, y_i + 0.5)",
        ],
    }


@mcp.tool()
def workspace_status() -> dict:
    """Report whether expected local submission files exist."""
    cwd = Path.cwd()
    files = {}
    for name in ("solution.cpp", "Makefile", "README.md"):
        p = cwd / name
        files[name] = {"exists": p.exists(), "size": p.stat().st_size if p.exists() else 0}
    return {"cwd": str(cwd), "files": files}


if __name__ == "__main__":
    mcp.run()
