# PPT 可用性与人类审美对齐

本环境分别评估演示文稿的客观可用性和设计审美，避免较好的视觉观感掩盖遮挡、裁切、
内容缺失或页面损坏。

Agent 会收到有效且可编辑的 `draft.pptx`。其中故意加入了视觉质量问题，而不是文件
损坏。必须交付：

- `polished.pptx`

`design_notes.md` 是可选文件，不影响获得高分。MCP 工具 `annotate_pptx` 可生成
`draft_annotated.pptx` 和 `object_manifest.json`，帮助诊断对象位置。

任务要求 Agent 在编辑前把 `draft.pptx` 渲染为 PNG 并进行视觉检查，编辑后再次
渲染 `polished.pptx` 做前后对比。只检查 OOXML、文本或对象属性不满足任务要求。

## 评估模型

隐藏的人类设计稿是质量锚点，不是要求像素级复刻的唯一答案。Judge 比较：

1. `REFERENCE`：用于校准专业质量的隐藏人类设计；
2. `DRAFT`：故意降低视觉质量的草稿；
3. `CANDIDATE`：Agent 改进后的结果。

当前包含 `ppt_0003`、`ppt_0005` 和 `ppt_0007` 三组单页材料。暂不纳入多页源文件，
使现有 Judge 每次评估一张完整幻灯片。

评分维度：

- `artifact_contract`（10%）：`polished.pptx` 存在且是有效 OOXML；
- `office_render`（10%）：三份文稿都能通过 LibreOffice 渲染为 PNG；
- `llm_visual_judge`（80%）：多模态 Judge 评估 4 个可用性维度和 6 个审美维度，
  包括图像位置/旋转、视觉中心、左右密度与功能性留白。

仅打开后重新保存草稿不算视觉改进。没有实质变化、人类不会优先选择，或丢失内容的
提交都会被限制得分。

Judge 直接调用 Anthropic Messages API，把三张 PNG 作为图像块发送，不启动额外
Agent Session。配置从 `arena.yaml` 读取：

```yaml
ppt_visual_repair:
  judge:
    api_key: ""      # 默认读取 ANTHROPIC_API_KEY
    base_url: ""     # 默认 https://api.anthropic.com/v1/messages
    model: ""        # 默认使用当前 Claude 模型
    timeout: 300
```

环境变量优先于 YAML：`PPT_JUDGE_API_KEY`、`PPT_JUDGE_BASE_URL`、
`PPT_JUDGE_MODEL`、`PPT_JUDGE_TIMEOUT`，以及共享的 `ANTHROPIC_API_KEY` /
`LLM_JUDGE_MODEL`。
