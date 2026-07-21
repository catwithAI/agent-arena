# agent-arena 文档

agent-arena 用统一任务、输入、预算和评分规则比较不同的编程 Agent 与模型。它的
目标不是维护公共排行榜，而是让开发者在自己的机器和数据上进行可重复、可审计的
对比实验。

一次评测会形成 `任务 → 运行 → 尝试 → 评分` 四层数据：

1. 选择一个评测环境和预置任务，或提交临时 Prompt；
2. 选择多 Agent、同模型或多模型对比模式；
3. 每个组合在独立工作目录中执行，可并行或串行调度；
4. 环境评分器读取工具轨迹、最终状态和工作区产物并输出分维度得分；
5. 前端集中展示事件、推理、Token、产物、Wire 证据、安全事件和多轮诊断。

## 主要能力

- 内置 Claude Code、Codex 和可选的远程 Claude Code 适配器；
- 通过 YAML 接入任意 CLI Agent，或实现 Python 适配器获得完整控制；
- 支持多 Agent、同模型、多模型三种对比模式以及串行/并行执行；
- 既支持 MCP 工具型场景，也支持纯代码、Office、会计与多轮对话场景；
- 按尝试隔离工作区、会话令牌和 Agent 配置目录；
- 记录规范化的模型/MCP 通信证据、上下文曲线和子 Agent 拓扑；
- 独立呈现安全事件，不把安全结论混入任务完成分；
- 对 PPTX、DOCX、XLSX 产物提供受限的服务端静态预览。

## 技术栈

- 后端：Python 3.11+、FastAPI、SQLite、uv；
- 前端：React、Vite、TypeScript；
- 环境 SDK：轻量级 `lane.env_api`，负责工具注册、调用计时和轨迹落盘。

## 文档导航

- [系统架构](architecture.md)：模块边界、请求链路、存储与隔离；
- [接入 Agent](agents.md)：内置适配器、第三方模型服务和自定义 Agent；
- [开发评测环境](environments.md)：目录协议、任务、工具、材料和评分器；
- [环境前置条件](env-prerequisites.md)：声明、自动检查与降级语义；
- [安全维度需求](specs/security_dimension/requirements.md)与
  [设计](specs/security_dimension/design.md)；
- [通信观测需求](specs/wire_observability/requirements.md)、
  [设计](specs/wire_observability/design.md)与
  [实施进度](specs/wire_observability/tasks.md)；
- [Office 预览决策记录](specs/wire_observability/office-preview-adr.md)。

## 本地启动

```bash
uv sync
cp arena.yaml.example arena.yaml
uv run uvicorn backend.main:create_app --factory --port 8100

cd web
npm install
npm run dev
```

前端默认地址为 `http://127.0.0.1:5173`，后端默认地址为
`http://127.0.0.1:8100`。启动后可访问 `GET /api/selfcheck` 检查本机配置和
环境基础设施。

## 开发检查

```bash
uv run python scripts/lint_env.py --all
uv run pytest
uv run ruff check backend lane tests scripts
cd web && npm test && npm run build
```

`envs/*/materials/` 中的说明属于评测输入，不属于本目录的语言约束；为保持基准
语义稳定，它们可以使用任务原本的交付语言。
