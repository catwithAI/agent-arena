# PPT 人类审美对齐

本环境评估演示文稿可用性，以及 Agent 的设计审美是否符合成熟的人类偏好。两个轴独立
评分，避免美观分掩盖遮挡、裁切、内容缺失或页面损坏。

Agent 获得有效且可编辑、但故意包含视觉质量问题的 `draft.pptx`，必须输出：

- `polished.pptx`

`design_notes.md` 可选，高分不依赖它。MCP 工具 `annotate_pptx` 可生成
`draft_annotated.pptx` 和 `object_manifest.json` 作为诊断辅助。

任务明确要求编辑前把 `draft.pptx` 渲染为 PNG 并进行视觉检查，编辑后再次渲染
`polished.pptx` 做前后对比。只检查 OOXML、文本或对象属性不满足任务要求。

## 评估模型

隐藏的人类设计稿是质量锚点，不是 pixel-perfect 标准答案；允许多种视觉方案。Judge
比较：

1. `REFERENCE`：用于校准专业质量的隐藏人类设计。
2. `DRAFT`：故意弱化的视觉草稿。
3. `CANDIDATE`：Agent 的优化结果。

环境包含单页材料集 `ppt_0003`、`ppt_0005`、`ppt_0007`。当前故意排除多页源文件，
确保 judge 每次评估一张完整 slide。

评分维度：

- `artifact_contract`（10%）：`polished.pptx` 存在且为有效 OOXML。
- `office_render`（10%）：三份 deck 都能通过 LibreOffice 渲染为 PNG preview。
- `llm_visual_judge`（80%）：多模态 judge 评估 4 个可用性维度和 6 个审美维度，包括
  图片位置/旋转、视觉中心、左右密度和功能性留白。只缩小文字、不修正失衡图片位置，
  无法获得高构图分或直接通过。

仅打开并重新保存草稿不算视觉改进。无实质变化、人类不会优先选择、或丢失内容的提交
都会被限制最高分。

Judge 直接调用 Anthropic Messages API，三张 PNG preview 作为 image block 附加，
不启动外部 Agent session。配置读取自 `arena.yaml`：

```yaml
ppt_visual_repair:
  judge:
    api_key: ""      # 默认 $ANTHROPIC_API_KEY
    base_url: ""     # 默认 https://api.anthropic.com/v1/messages
    model: ""        # 默认当前 Claude model
    timeout: 300
```

环境变量覆盖 YAML：`PPT_JUDGE_API_KEY`、`PPT_JUDGE_BASE_URL`、
`PPT_JUDGE_MODEL`、`PPT_JUDGE_TIMEOUT`，或共享的 `ANTHROPIC_API_KEY` /
`LLM_JUDGE_MODEL`。
