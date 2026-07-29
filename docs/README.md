# agent-arena

agent-arena 是一个开放的 coding agent 评测框架，用同一批任务、工具和评分标准，
帮助用户公平、可重复地比较自己的 Agent 与真实基线，而不是维护公共排行榜。

## 文档语言约定

项目文档以中文为主，根目录 `README.md` 保留为英文入口。命令、API 路径、配置字段、
代码标识、错误码和上游专有名词保留原文，避免翻译破坏可执行性或检索能力。
`envs/**/materials/`、`inputs/` 和 fixture 属于评测输入或复现证据，保持题目固定时的
原始语言，不按普通项目文档翻译。

每次对比运行会采集三类信息：

1. **执行过程**：工具调用、错误、重试和耗时。
2. **推理过程**：任务理解、规划、决策和修正（仅限 Agent 实际暴露的内容，例如
   Claude Code 的 `thinking` block）。
3. **最终产物**：最终状态、代码文件、测试结果和得分。

## 内置基线

项目内置 **Claude Code**、**Codex**、**Kimi Code**、**OpenCode**、
**MiMo Code** 和 **DeerFlow**。它们通过统一 adapter 契约调用原生 CLI 或固定版本的
runner，不享有特殊模型访问能力。

自研 Agent、研究原型和内部工具也可以通过同一 registry 与结果契约接入，支持 YAML
本地 CLI profile、ACP server、远程服务和受信任的 Python plugin。详见
[Agent 接入指南](agents.md)。

## 一次运行如何执行

1. 选择一个评测**环境**（`envs/<name>/`），其中包含任务定义、所需工具（纯编程任务
   可以没有工具）和 scorer。
2. 选择一个或多个 **Agent** 执行同一任务。
3. 每个 Attempt 在独立工作区、session token 和 trace 中运行，彼此不可见、不可干扰。
4. 环境自己的 **scorer** 读取 trace 和最终状态，生成加权的 0–100 分。
5. 前端并排展示对话、工具调用、耗时、token usage 和得分。

## 技术栈

后端使用 Python 3.11+、FastAPI、SQLite 和 uv；前端使用 React、Vite 和
TypeScript。

## 文档

- [架构说明](architecture.md)
- [评测环境编写指南](environments.md)
- [Agent 接入指南](agents.md)
- [批量实验、恢复、聚合与复现清单](experiments.md)

## 快速开始

```bash
./start.sh
```

打开前端，选择 `order-desk` 等环境和一个或多个已安装 Agent 后运行。只有对应 CLI
或 runner 已位于 `PATH` 且版本满足约束时，内置 Agent 才会显示为可用。
