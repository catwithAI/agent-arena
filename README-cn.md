# agent-arena

[English](README.md)

agent-arena 是一个开源的 Agent 评测框架，用相同任务、输入、预算和评分规则比较
不同的编程 Agent 与模型。项目内置 **Claude Code** 和 **Codex** 适配器，也支持
仅通过配置接入任意 CLI Agent，并提供多 Agent、同模型和多模型三种对比模式。

每次尝试都会记录执行事件、Agent 暴露的推理、工具调用轨迹、Token 用量、产物、
评分和安全观测；启用通信观测后，还会保存模型调用与 MCP 调用的 Wire 证据。多轮
任务会保留可恢复的对话记录，并给出上下文压缩诊断。

## 快速开始

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js/npm，以及至少一个
位于 `PATH` 中的 Agent CLI（`claude` 或 `codex`）。

```bash
uv sync
cp arena.yaml.example arena.yaml
uv run uvicorn backend.main:create_app --factory --port 8100

cd web
npm install
npm run dev
```

浏览器打开 `http://127.0.0.1:5173`。后端 API 位于
`http://127.0.0.1:8100`；`GET /api/selfcheck` 会检查配置、环境加载、
尝试令牌认证和轨迹写入是否正常。

如需使用第三方模型服务，在 `arena.yaml` 的 `model_providers` 中配置。API Key
可放入 `api_key_env` 指定的环境变量（参见 `.env.example`），也可写入已被 Git
忽略的本地 `arena.yaml`。模型引用格式为 `<provider>/<model>`，例如
`openrouter/openai/gpt-5`。

## 内置评测环境

| 环境 | 评测重点 | 额外前置 |
|---|---|---|
| `order-desk` | MCP 工具调用与预算约束 | 无 |
| `cpp-optimizer` | C++17 正确性与优化能力 | C++ 编译器 |
| `ad-placement` | 批量计分的启发式优化 | Linux + C++17 工具链 |
| `apple-incremental-game` | 长期策略与复利优化 | Python 3 |
| `edgebench-juliet` | 基于事实的静态漏洞分析 | Python 3 + Bash |
| `gdpval-prepaid-amortization-db` | 确定性会计信息提取 | Python 3 |
| `gdpval-prepaid-amortization-official` | 按官方 Rubric 评审 Excel 交付物 | Anthropic 兼容 Judge |
| `ppt-visual-repair` | 演示文稿可用性与设计审美 | LibreOffice + 多模态 Judge |
| `context-compaction-benchmark` | 多轮信息保真与压缩可观测性 | 建议使用支持多轮的适配器 |

环境前置检查只告警、不阻断启动，并会在提交前展示。新增或修改环境后运行：

```bash
uv run python scripts/lint_env.py --all
```

## 文档

- [文档总览](docs/README.md)
- [系统架构](docs/architecture.md)
- [接入 Agent](docs/agents.md)
- [开发评测环境](docs/environments.md)
- [环境前置条件](docs/env-prerequisites.md)

`docs/` 下的文档统一使用中文。`envs/*/materials/` 是评测输入的一部分，可能按
任务交付语言保留英文。

## 验证

```bash
uv run pytest
uv run ruff check backend lane tests scripts
cd web && npm test && npm run build
```

## 许可证

Apache-2.0，详见 [LICENSE](LICENSE)。
