# 架构说明

## 目录结构

```text
agent-arena/
├── backend/            # FastAPI：调度、执行与评估
│   ├── agents/         # registry、共享 runtime、transport 与 plugin
│   ├── adapters/       # 原生 adapter 与旧接口兼容 adapter
│   └── *.py            # main/api/config/db/models/runner/evaluator/...
├── lane/               # 面向环境作者的轻量 SDK（@env_tool 等）
├── envs/               # 评测环境：任务、工具与 scorer
├── web/                # React + Vite + TypeScript 前端
├── data/               # 运行数据（gitignored）：SQLite 与 Attempt 文件
├── arena.yaml(.example)
└── pyproject.toml
```

## 核心概念

`Task` → `Run` → `Attempt` → `Score`。

- **Task**：Prompt 及上下文/约束，来自 `envs/<name>/tasks/*.json` 或临时输入。
- **Run**：一次对比，将同一任务分发给一个或多个 Agent。
- **Attempt**：某个 Agent 的一次任务执行。隔离容器位于
  `data/attempts/<attempt_id>/`；Agent 可见文件在 `skill_workspace/` 中，
  私有 runtime/control 数据、session token 和 trace 位于工作区之外。
- **Score**：环境 scorer 产生的各维度 0–100 分，按权重聚合为 `score_total`。

## 请求流程

1. `POST /api/runs` 创建 `Task`（如有必要）、`Run` 和每个 Agent 对应的
   `Attempt`，随后由 `backend/run_dispatch.py` 并发调度后台任务。
2. `AgentRegistry` 解析内置或配置的 AgentSpec，检查任务、模型、MCP 和会话能力
   兼容性，并构造对应的 profile runtime、ACP/remote transport、Python plugin
   或原生兼容 adapter。所有路径都实现统一的 `AgentAdapter` 结果契约。
3. 如果场景的 `meta.yaml` 显式声明 `entrypoints.mcp`，且 Agent 选择调用工具，
   MCP 请求会通过 `backend/env_attempt_server.py` 转发到
   `POST /attempts/{attempt_id}/tools/{tool_name}`，并使用 Attempt 级 bearer token
   认证。MCP 进程从项目根目录解析声明命令，同时通过 `LANE_WORKSPACE` 获得真实
   Attempt 工作区。未声明 MCP 的场景不会获得环境工具，框架不会自动推断或虚构。
4. Adapter 结束后，`backend/runner.py` 通过 `backend/evaluator.py` 调用环境的
   `scorer.py`，写入分数并完成 Attempt 状态。
5. 前端轮询 `GET /api/runs/{id}` 和
   `GET /api/runs/{id}/attempts/{id}`，展示实时进度及最终对话、分数和产物。

## 隔离模型

每个 Attempt 都有独立的
`data/attempts/<attempt_id>/skill_workspace/`。本地 runtime 将其作为进程
`cwd`；需要显式工作区参数的 CLI 还会收到对应参数，因此提交产物会落在 scorer
预期的位置。框架元数据、manifest、event 和私有 Agent 配置保存在 Attempt 根目录下
的相邻路径中。同一 Run 内的 Attempt 之间也不共享这些状态。

内置本地集成将 Agent 专属的 HOME、配置和 session 位置指向 Attempt 私有目录，
不会读取操作者全局的 skill、plugin、MCP server、memory 或 session。这是本地状态
隔离，而不是能力限制。

## 能力公平性

agent-arena 比较每个 Agent 已验证的原生能力集合。“公平”指任务、输入材料、时间预算
和外部资源边界一致，并不要求所有 Agent 使用完全相同的工具。

- Adapter 不应为了“可比”而关闭 Agent 的原生工具、skill 或任务拆解能力。
- Adapter 不应在 Prompt 中硬编码 MCP、curl、Python 等首选解法。
- 只有场景 `meta.yaml` 显式声明 `entrypoints.mcp` 时才接入 MCP/skill 能力。
- 宿主机的私有配置、凭证和 plugin 仍需隔离，避免操作者环境影响结果。

## 扩展点

- **新增 Agent**：普通本地 CLI 使用严格的 AgentSpec profile；也可在
  `arena.yaml` 的 `agents` 下配置 ACP server、远程服务或受信任的 Python plugin。
  迁移期间仍支持旧 `custom_agents`。只有现有 transport 无法表达新的 runtime
  契约时，才新增专用 adapter。详见 [Agent 接入指南](agents.md)。
- **新增环境**：在 `envs/` 下新增目录。详见
  [评测环境编写指南](environments.md)。
