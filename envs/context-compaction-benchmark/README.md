# context-compaction-benchmark

上下文压缩标准评测环境（`context_compaction_evaluation` spec C5-3）。

## 目的

用同一批**确定性生成**的材料，在多轮 / 子 agent 场景下衡量：

1. **retention**：上下文被压缩后，setup 阶段注入的关键事实是否仍被保留并在 probe
   阶段正确复现；
2. **compaction observability**：本次运行是否观察到可判定的上下文压缩，以及证据
   完整性（五态：observed / not_observed_under_budget / unsupported / incomplete /
   insufficient_calls）。

**不把「未观察到压缩」当失败**——那可能只是预算内没压到，或证据不足（见 status）。

## 结构

- `materials.py`：确定性材料 + facts manifest 生成器（同 seed/版本 → 逐字节相同）。
  粗口径记 bytes / estimated_tokens，**不硬编码任何模型 context window 作为真值**。
- `build_tasks.py`：用固定 seed 生成并入库 task JSON + facts manifest（幂等）。
  改题需改 seed 并升 `GENERATOR_VERSION`。
- `tasks/compaction_main_001.json`：主 agent 三阶段多轮（setup→pressure→probe）。
- `tasks/compaction_subagent_001.json`：要求在**同一个**子 agent invocation 内读
  facts + 处理 pressure + 回答 probe（让同一子 agent 段内产生多个可比较 call，
  R7.5）。
- `inputs/facts_*.json`：facts manifest，答案只存 `answer_hash`（明文不落，防泄题）。
- `scorer.py`：复用 `backend.wire.retention` 与 `backend.wire.evaluation`。

## 评分维度

| 维度 | 权重 | 说明 |
|---|---|---|
| `task_completion` | 100 | 计分。probe 产出可解析 `probe_answers.json` 且至少答对一个 fact。 |
| `retention` | 0（诊断） | probe 对 setup facts 的保真度（精确/等价/缺失/幻觉/部分保留）。 |
| `compaction_observability` | 0（诊断） | 五态压缩状态；未触发不判失败。 |

诊断维度 weight=0 → 不进总分（design §522，默认不改历史 benchmark 口径）。要把
retention 纳入总分，把它的 weight 调成非零即可。

## agent 需要产出

probe 阶段把每个 fact 的答案写进工作目录的 `probe_answers.json`：

```json
{ "fact-01": "<登记编码>", "fact-02": "...", ... }
```

scorer **只读这个文件与 facts manifest，不读 reasoning**（design §521）。

## 重新生成材料

```bash
python envs/context-compaction-benchmark/build_tasks.py
```

幂等：同 seed/版本重复跑产出逐字节相同。
