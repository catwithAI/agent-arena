"""确定性压力材料 + facts manifest 生成器（design §7.1，tasks.md C5-3）。

给定固定 seed，生成三类内容，**完全确定性、可幂等重放**（同 seed 同版本 →
逐字节相同），并记录 seed / bytes / estimated_tokens / content hash / facts，纳入
versioned manifest：

1. **setup facts**：若干不可从常识补全的 key/value（随机 token 值），只要求 agent
   确认读取；probe 阶段回问这些 fact 检验是否被保留（retention）；
2. **pressure filler**：结构化但不可从常识补全的多条记录（避免纯重复字符——
   tokenizer/cache/框架可能特殊处理，design §464），把上下文推向压缩；
3. **facts manifest**：`answer_hash` 用 `backend.wire.retention.answer_hash`
   （明文答案不落 manifest），scorer 用同一函数比对。

**不硬编码特定模型 context window 作为真值**（design §7.1 末 / R7.7）：manifest 只
记材料的 bytes / estimated_tokens（估算，粗口径），是否「超过声明窗口」由运行配置
的预算给出，不在这里假设某个模型的窗口大小。

generator 版本纳入 manifest；升级生成逻辑必须升版本，历史 attempt 的可复算性靠
(seed, generator_version) 锚定。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 允许作为独立脚本 / 被 backend 导入两种方式。
try:
    from backend.wire.retention import answer_hash
except ImportError:  # pragma: no cover - 仅在裸跑生成器时走
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.wire.retention import answer_hash

GENERATOR_VERSION = "lane-compaction-materials-v1"

# 粗略 token 估算：约 4 字节/token（不针对任何具体 tokenizer，只给数量级）。
_BYTES_PER_TOKEN = 4

# 语料表：拼装不可从常识补全的 key/value 与压力记录。用固定表 + seed 抽样，
# 保证同 seed 逐字节可复现（不引入时间/随机全局态）。
_NOUNS = [
    "quasar", "lattice", "meridian", "cinder", "halcyon", "vortex", "nimbus",
    "basalt", "zephyr", "cobalt", "marrow", "thistle", "granite", "beacon",
]
_ADJS = [
    "amber", "hollow", "crimson", "silent", "brittle", "molten", "distant",
    "frozen", "gilded", "sunken", "velvet", "ragged", "coiled", "pale",
]


def _rng(seed: int) -> random.Random:
    """seed 派生的独立 Random 实例——不碰全局 random state，确定性可复算。"""
    return random.Random(seed)


def _token(rng: random.Random) -> str:
    """一个不可从常识猜出的 6 位小写十六进制 token。"""
    return f"{rng.randrange(16**6):06x}"


@dataclass(frozen=True)
class GeneratedFact:
    """一个可评分 fact 及其明文答案（明文只用于拼 setup 文案，不进 manifest）。"""

    id: str
    question: str
    answer: str


@dataclass
class GeneratedMaterials:
    facts: list[GeneratedFact]
    setup_text: str
    pressure_text: str
    manifest: dict[str, Any]


def _make_facts(rng: random.Random, count: int) -> list[GeneratedFact]:
    facts: list[GeneratedFact] = []
    for i in range(count):
        adj = _ADJS[rng.randrange(len(_ADJS))]
        noun = _NOUNS[rng.randrange(len(_NOUNS))]
        key = f"{adj}-{noun}"
        value = _token(rng)
        facts.append(GeneratedFact(
            id=f"fact-{i + 1:02d}",
            question=f"设施 {key!r} 的登记编码是什么？",
            answer=value,
        ))
    return facts


def _setup_text(facts: list[GeneratedFact]) -> str:
    lines = [
        "以下是一批设施登记记录。请逐条读取并记住每条的登记编码，稍后会回问。",
        "",
    ]
    for f in facts:
        # key 从 question 里回推（question 形如 “设施 'x' 的...”）。
        key = f.question.split("'")[1] if "'" in f.question else f.id
        lines.append(f"- 设施 {key!r}：登记编码 = {f.answer}")
    lines.append("")
    lines.append("读完请只回复 `ACK`，不要复述编码。")
    return "\n".join(lines)


def _pressure_text(rng: random.Random, records: int) -> str:
    """结构化压力记录（非重复字符）：每条是一行带随机字段的日志。"""
    lines = ["以下是需要你逐条核对的运行日志，请确认没有异常后回复 `CHECKED`。", ""]
    for i in range(records):
        adj = _ADJS[rng.randrange(len(_ADJS))]
        noun = _NOUNS[rng.randrange(len(_NOUNS))]
        ts = 1_600_000_000 + rng.randrange(10_000_000)
        val = _token(rng)
        lines.append(
            f"[{ts}] node={adj}-{noun} seq={i:04d} checksum={val} status=ok"
        )
    return "\n".join(lines)


def generate(
    *, seed: int, fact_count: int = 6, pressure_records: int = 400
) -> GeneratedMaterials:
    """确定性生成材料 + facts manifest（同 seed/版本 → 逐字节相同）。

    fact/pressure 从**同一个** seeded RNG 顺序抽样：facts 先抽（固定顺序），再抽
    pressure，保证给定参数下每次调用产出完全一致。
    """
    rng = _rng(seed)
    facts = _make_facts(rng, fact_count)
    setup_text = _setup_text(facts)
    pressure_text = _pressure_text(rng, pressure_records)

    # manifest 覆盖 setup+pressure 的字节与估算 token（材料规模，不是模型窗口）。
    material_bytes = len(setup_text.encode("utf-8")) + len(pressure_text.encode("utf-8"))
    from backend.wire.hashing import raw_bytes_hash

    content_sha256 = raw_bytes_hash(
        (setup_text + "\x00" + pressure_text).encode("utf-8")
    )
    manifest = {
        "schema_version": "lane-compaction-facts-v1",
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "fact_count": fact_count,
        "pressure_records": pressure_records,
        "bytes": material_bytes,
        "estimated_tokens": material_bytes // _BYTES_PER_TOKEN,
        "content_sha256": content_sha256,
        # facts 只落 id + answer_hash（明文答案不进 manifest，隐私 + 防泄题）。
        "facts": [
            {"id": f.id, "answer_hash": answer_hash(f.answer)} for f in facts
        ],
    }
    return GeneratedMaterials(
        facts=facts,
        setup_text=setup_text,
        pressure_text=pressure_text,
        manifest=manifest,
    )
