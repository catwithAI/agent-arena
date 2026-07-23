# agent-arena

[English](README.md)

一个开源的 agent 评测框架，用同一批任务、同一套工具、同一套评分标准，
公平地比较不同的 coding agent。内置 **Claude Code**、**Codex**、**Kimi Code**、
**MiMo Code** 和 **DeerFlow** 参照实现，同时提供开放的扩展点，可以接入
*任意*其他 agent——CLI 类 agent
只需写配置即可接入，也可以写一个小的 Python adapter 获得完全控制权。

每次对比评测都会采集三件事：执行过程（工具调用、错误、耗时）、思考过程
（agent 暴露出来的 thinking 轨迹，如果有的话）、最终产物（分数、代码、
产物文件）——可以并排比较任意数量的 agent。

这是一个面向大众的开源项目，不局限于 Claude Code 和 Codex —— adapter
接口的设计目标就是让任意 agent（开源、商业、或研究原型）都能接入。项目
也会朝着支持更大规模的方向演进：并发运行 N 个 agent、每个任务重复跑多
轮试验，从而得到统计意义上可靠的评测结果，而不是单次跑分的偶然结果。

## 快速开始

```bash
./start.sh
```

脚本会在首次运行时创建 gitignored 的 `arena.yaml` 并安装缺失依赖。端口覆盖以及仅启动
后端/前端的参数见 `./start.sh --help`。

打开前端（默认 `http://127.0.0.1:5173`），选择一个评测环境，勾选已安装
的 agent（对应的 `claude`、`codex`、`kimi`、`mimo` 或 DeerFlow runner
需要在 `PATH` 中），然后运行。

如果要让 claude-code/codex 走第三方 model provider（见
`arena.yaml.example` 里的 `model_providers`），启动后端前请确保对应的
API key 可用：要么把 `api_key_env` 指定的环境变量导出（`cp .env.example
.env`，填好之后 `source .env`），要么直接把 key 填到 `arena.yaml` 里
该 provider 的 `api_key` 字段（该文件已 gitignore）。如果某个 run 引用的
provider 两边都没配置 key，评测会立刻以清晰的 `provider_api_key_missing`
错误失败，而不是让 CLI 报一个让人摸不着头脑的登录错误。

## 内置评测环境

- **order-desk** —— 工具调用类环境：在预算约束下搜索一个模拟图书目录并
  下单。
- **cpp-optimizer** —— 纯编程类环境：提交一份 C++17 解答，通过编译并跑
  隐藏测试用例批量评分。

参见 [docs/environments.md](docs/environments.md) 了解如何新增自己的评测
环境。

## 文档

- [docs/README.md](docs/README.md) —— 完整设计概览
- [docs/architecture.md](docs/architecture.md) —— 各模块如何协同工作
- [docs/environments.md](docs/environments.md) —— 如何编写新的评测环境
- [docs/agents.md](docs/agents.md) —— 如何接入新的 agent
- [docs/experiments.md](docs/experiments.md) —— 批量实验、断点续跑与统计报告

## 批量实验

Arena 可以在现有 Run/Attempt 之上展开 `task × variant × repeat` 实验矩阵，并将
配置哈希、AgentSpec、有效模型和代码版本一同保存：

```bash
cp experiment.yaml.example experiment.yaml
uv run python scripts/run_experiment.py --config experiment.yaml
```

中断后用输出的 Experiment ID 恢复；失败项只有在显式指定时才重跑：

```bash
uv run python scripts/run_experiment.py \
  --config experiment.yaml \
  --resume exp_20260723_120000 \
  --retry-failed
```

结果位于 `data/experiments/<id>/`，包括原始 JSONL、`summary.json` 和
`report.md`。

## 许可证

Apache-2.0 —— 见 [LICENSE](LICENSE)。
