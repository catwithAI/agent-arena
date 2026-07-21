"""确定性生成 benchmark 的 task JSON + facts manifest（tasks.md C5-3）。

跑 `python envs/context-compaction-benchmark/build_tasks.py` 会用固定 seed 生成：

- `tasks/compaction_main_001.json`：主 agent 三阶段（setup→pressure→probe）多轮；
- `tasks/compaction_subagent_001.json`：要求在同一子 agent invocation 内读 facts +
  处理 pressure + 回答 probe（让同一子 agent 段内产生多个可比较 call）；
- `inputs/facts_main_001.json` / `inputs/facts_subagent_001.json`：facts manifest
  （scorer 读取，答案只存 hash）。

**幂等**：同 seed/版本重复跑产出逐字节相同（不引入时间/随机全局态）。生成产物入库
（git tracked），运行时不再动态生成——运行只读静态 task + manifest。
"""

from __future__ import annotations

import json
from pathlib import Path

import materials

ENV_DIR = Path(__file__).resolve().parent
TASKS_DIR = ENV_DIR / "tasks"
INPUTS_DIR = ENV_DIR / "inputs"

# 固定 seed（入库固定，改题需改 seed 且升 generator 版本）。
MAIN_SEED = 20260720
SUBAGENT_SEED = 20260721
TIMEOUT_SECONDS = 1800


def _probe_prompt(facts: list[materials.GeneratedFact]) -> str:
    keys = []
    for f in facts:
        key = f.question.split("'")[1] if "'" in f.question else f.id
        keys.append(f'  "{f.id}": "<设施 {key!r} 的登记编码>"')
    template = "{\n" + ",\n".join(keys) + "\n}"
    return (
        "现在回到最初的设施登记记录。请仅根据你先前读到的内容，"
        "把每个设施的登记编码填进下面的 JSON，并写入当前工作目录的 "
        "`probe_answers.json`（只写这个文件，不要复述推理过程）。"
        "如果某条你已经不记得，就把该字段留空字符串，不要编造。\n\n"
        f"```json\n{template}\n```"
    )


def _main_task(gen: materials.GeneratedMaterials) -> dict:
    return {
        "id": "compaction_main_001",
        "env_name": "context-compaction-benchmark",
        "prompt": gen.setup_text,  # 单轮向后兼容：首轮即 setup
        "context": {
            "_conversation": [
                {"id": "setup", "purpose": "setup", "prompt": gen.setup_text},
                {"id": "pressure", "purpose": "pressure", "prompt": gen.pressure_text},
                {
                    "id": "probe", "purpose": "probe", "score_after": True,
                    "prompt": _probe_prompt(gen.facts),
                },
            ],
            "facts_manifest_file": "facts_main_001.json",
        },
        "timeout_seconds": TIMEOUT_SECONDS,
    }


def _subagent_task(gen: materials.GeneratedMaterials) -> dict:
    # 子 agent 场景：父 agent 只创建一次子任务，子任务内部完成读 facts + 处理
    # pressure + 回答 probe（同一子 agent invocation 内多个可比较 call）。
    sub_brief = (
        "请创建**一个**子 agent（Task），把下面整段工作**一次性**交给它完成，"
        "不要分成多个子 agent：\n\n"
        "1. 读取并记住设施登记编码；\n2. 逐条核对运行日志；\n"
        "3. 最后把每个设施的登记编码写入工作目录的 `probe_answers.json`。\n\n"
        "=== 设施登记记录 ===\n" + gen.setup_text + "\n\n"
        "=== 运行日志 ===\n" + gen.pressure_text + "\n\n"
        "=== 需要回答 ===\n" + _probe_prompt(gen.facts)
    )
    return {
        "id": "compaction_subagent_001",
        "env_name": "context-compaction-benchmark",
        "prompt": sub_brief,
        "context": {
            "facts_manifest_file": "facts_subagent_001.json",
            "agent_scope": "subagent",
        },
        "timeout_seconds": TIMEOUT_SECONDS,
    }


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build() -> None:
    main = materials.generate(seed=MAIN_SEED)
    sub = materials.generate(seed=SUBAGENT_SEED)

    _write_json(TASKS_DIR / "compaction_main_001.json", _main_task(main))
    _write_json(TASKS_DIR / "compaction_subagent_001.json", _subagent_task(sub))
    _write_json(INPUTS_DIR / "facts_main_001.json", main.manifest)
    _write_json(INPUTS_DIR / "facts_subagent_001.json", sub.manifest)
    print("built tasks + facts manifests (deterministic)")


if __name__ == "__main__":
    build()
