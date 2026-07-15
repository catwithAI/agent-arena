"""canonical token 聚合回填（design §18，W1-4）。

有 canonical `llm_call` 时，从 `phase=agent_run` 的 call usage 聚合出 attempt
级 token 总量，写回 `token_usage_json`，并标 `external_refs.token_usage_source`：

- `wire`：来自 canonical calls（调用级证据聚合）；
- `adapter`：无 canonical calls，沿用 adapter result（保底）。

聚合值与 adapter 已有值冲突时**双保留**（不覆盖 adapter 值、不改历史 score），
把 wire 聚合另存 `external_refs.wire_token_usage`，差异记 `token_usage_conflict`。
不因 canonical 重建自动改历史 score（design §18.4）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend.wire import paths

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def aggregate_agent_run_usage(records: Any) -> dict[str, int] | None:
    """phase=agent_run 的 llm_call usage 求和；无 call 返回 None。

    ``records`` 可为 list 或行迭代器——逐条累计，不整份进内存（评审 M6）。
    """
    totals = {f: 0 for f in _USAGE_FIELDS}
    seen = False
    for rec in records:
        if rec.get("record_type") != "llm_call" or rec.get("phase") != "agent_run":
            continue
        usage = (rec.get("data") or {}).get("usage") or {}
        for f in _USAGE_FIELDS:
            v = usage.get(f)
            if isinstance(v, (int, float)):
                totals[f] += int(v)
                seen = True
    return totals if seen else None


def _iter_wire_records(wire_path: Path) -> Any:
    """逐行 yield canonical record，不整份读入（评审 M6）。"""
    if not wire_path.exists():
        return
    with wire_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def backfill_token_usage(db_path: Path, data_path: Path, attempt_id: str) -> str:
    """把 wire 聚合回填 attempts 表，返回 token_usage_source（wire|adapter）。

    canonical 缺失时不动 token_usage_json，只标 source=adapter。冲突双保留。
    """
    wire_path = paths.wire_file(data_path, attempt_id)
    wire_usage = aggregate_agent_run_usage(_iter_wire_records(wire_path))

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT token_usage_json, external_refs_json FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return "adapter"
        adapter_usage = json.loads(row[0] or "{}")
        external = json.loads(row[1] or "{}")

        # backfill 是幂等重算：先无条件清掉上一次的 wire 派生字段，再按本次
        # 结果重建——否则 parser 升级后 conflict → resolved / wire → adapter
        # 的状态转换不会收敛，旧 conflict 会残留（评审）。
        external.pop("wire_token_usage", None)
        external.pop("token_usage_conflict", None)

        if wire_usage is None:
            # 无 canonical calls：回退 adapter，不留任何 wire 派生态。
            source = "adapter"
            external["token_usage_source"] = source
        else:
            source = "wire"
            external["token_usage_source"] = source
            external["wire_token_usage"] = wire_usage
            # 冲突：adapter 已上报数值（含显式 0，区分 null）且与 wire 不一致
            # → 双保留，不覆盖 score 相关。用 isinstance 判定「有数据」，
            # 不能用 truthiness（0 是有效上报，不是缺失，评审 M6）。
            adapter_in = adapter_usage.get("input_tokens")
            adapter_out = adapter_usage.get("output_tokens")
            has_adapter = isinstance(adapter_in, (int, float)) or isinstance(
                adapter_out, (int, float)
            )
            if has_adapter and (
                adapter_in != wire_usage["input_tokens"]
                or adapter_out != wire_usage["output_tokens"]
            ):
                external["token_usage_conflict"] = {
                    "adapter": {
                        "input_tokens": adapter_in,
                        "output_tokens": adapter_out,
                    },
                    "wire": {
                        "input_tokens": wire_usage["input_tokens"],
                        "output_tokens": wire_usage["output_tokens"],
                    },
                }
            else:
                # 一致（含 resolved）：token_usage_json 采用 wire 聚合（更细）
                adapter_usage = {
                    "input_tokens": wire_usage["input_tokens"],
                    "output_tokens": wire_usage["output_tokens"],
                }

        conn.execute(
            "UPDATE attempts SET token_usage_json=?, external_refs_json=? WHERE id=?",
            (
                json.dumps(adapter_usage, ensure_ascii=False),
                json.dumps(external, ensure_ascii=False),
                attempt_id,
            ),
        )
        conn.commit()
    return source
