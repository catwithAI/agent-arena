# GDPval 官方 Rubric 评审器

本环境保留 GDPval 官方任务、Excel 交付协议、源文件、完整 rubric 和专家工作簿参考。
评分器直接调用 Anthropic Messages API：把 Agent 交付物用 openpyxl 提取为
sheet/cell/formula 文本、把源 PDF 作为原生 document block 一并发送，要求
Judge 使用严格的二元判定对每条官方 rubric 评分：完全满足的条目获得该条目的
全部权重，其他情况均得零分。56 条 rubric 的原始满分为 95 分，之后归一化为
0～100 分。

专家工作簿用于校准，而不是要求像素级一致的标准答案。发票事实和会计分类必须
根据官方源 PDF 和 COA 进行核验。Judge prompt、HTTP 状态、原始响应和归一化结果
均保存在对应 attempt 的 `private_eval/gdpval_rubric_judge/` 目录中。

配置默认读取 `ANTHROPIC_API_KEY` 环境变量和内置默认模型。可以通过
`arena.yaml` 中的 `gdpval_prepaid_amortization_official.judge` 配置段
（`api_key` / `base_url` / `model` / `timeout`），或 `GDPVAL_JUDGE_*` 环境变量
进行覆盖。
