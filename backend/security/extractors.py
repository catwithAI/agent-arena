"""从各 agent 的原始日志里提取 shell 命令原文。

真实数据（data/attempts 样本）确认命令来源都带 command 字段：
- Claude Code events.jsonl：type=assistant → content[].tool_use，name ∈ {Bash, *run_shell*}
- 其他 trace.jsonl 风格来源：tool_name ∈ {Bash, run_shell}，arguments.command
- Codex：事件字段结构与 CC 类似，走同一 events.jsonl 提取路径；字段缺失则空返回 + 标记覆盖缺口

统一产出 ExtractedCommand（命令原文 + 溯源信息），供 classifier 逐条判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 命令型工具名：命中即认为 input/arguments.command 是 shell 命令原文。
# 覆盖裸 Bash、裸 run_shell、以及 MCP 前缀 mcp__lane-*__run_shell。
_SHELL_TOOL_SUFFIXES = ("bash", "run_shell", "shell", "exec", "execute")


def _is_shell_tool(name: str) -> bool:
    n = (name or "").lower()
    return any(n == s or n.endswith(s) for s in _SHELL_TOOL_SUFFIXES)


@dataclass
class ExtractedCommand:
    command: str
    source_ref: dict[str, Any] = field(default_factory=dict)  # {log, line} / {trace_seq}
    tool_name: str = ""


def _from_tool_input(name: str, tool_input: dict[str, Any]) -> str | None:
    if not _is_shell_tool(name):
        return None
    cmd = tool_input.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return cmd
    return None


def extract_from_events(events: list[dict[str, Any]]) -> list[ExtractedCommand]:
    """CC/Codex 风格 events.jsonl。"""
    out: list[ExtractedCommand] = []
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        # CC: type=assistant → message.content[].tool_use
        if ev.get("type") == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                cmd = _from_tool_input(block.get("name", ""), block.get("input") or {})
                if cmd:
                    out.append(
                        ExtractedCommand(
                            command=cmd,
                            source_ref={"log": "events.jsonl", "line": i + 1},
                            tool_name=block.get("name", ""),
                        )
                    )
        # Codex 兜底：顶层 exec/command 事件（字段名 spike 后可补）
        elif ev.get("type") in ("exec", "command", "shell_call"):
            cmd = ev.get("command") or (ev.get("input") or {}).get("command")
            if isinstance(cmd, str) and cmd.strip():
                out.append(
                    ExtractedCommand(
                        command=cmd,
                        source_ref={"log": "events.jsonl", "line": i + 1},
                        tool_name=ev.get("type", ""),
                    )
                )
    return out


def extract_from_trace(trace: list[dict[str, Any]]) -> list[ExtractedCommand]:
    """trace.jsonl 风格来源：tool_name + arguments.command。"""
    out: list[ExtractedCommand] = []
    for i, row in enumerate(trace):
        if not isinstance(row, dict):
            continue
        name = row.get("tool_name", "")
        args = row.get("arguments") or {}
        cmd = _from_tool_input(name, args if isinstance(args, dict) else {})
        if cmd:
            out.append(
                ExtractedCommand(
                    command=cmd,
                    source_ref={"log": "trace.jsonl", "trace_seq": i},
                    tool_name=name,
                )
            )
    return out


def extract_commands(
    *,
    events: list[dict[str, Any]],
    trace: list[dict[str, Any]],
) -> list[ExtractedCommand]:
    """合并 events + trace 的命令源。两者都扫，去重靠 source_ref 天然不同。"""
    return extract_from_events(events) + extract_from_trace(trace)
