# 系统架构

## 目录结构

```text
agent-arena/
├── backend/              # FastAPI、调度、执行、评分和观测
│   ├── adapters/         # Agent 适配器
│   ├── conversation/     # 多轮计划、恢复和摘要
│   ├── security/         # 离线安全事件分类
│   └── wire/             # 模型与 MCP 通信观测
├── lane/                 # 环境作者使用的最小工具 SDK
├── envs/                 # 环境定义、任务、材料和评分器
├── web/                  # React + Vite + TypeScript 前端
├── scripts/              # 环境校验等开发工具
├── data/                 # 运行数据，默认不纳入 Git
├── arena.yaml.example    # 本地配置模板
└── pyproject.toml
```

## 核心数据模型

`Task → Run → Attempt → Score`：

- **Task（任务）**：Prompt、上下文、约束和时间预算。来源可以是
  `envs/<name>/tasks/*.json`，也可以是 API 创建的临时任务；
- **Run（运行）**：一次对比实验，记录对比模式与执行方式；
- **Attempt（尝试）**：一个 Agent/模型组合对任务的一次执行，有独立工作区、
  会话令牌、事件和观测数据；
- **Score（评分）**：环境评分器输出的分维度结果，按 `meta.yaml` 权重聚合为
  `score_total`。

安全事件和上下文压缩诊断是独立观测轴，不会被隐式折算进任务分数。

## 请求与执行链路

```text
浏览器 / API
    │ POST /api/runs
    ▼
任务解析与对比展开
    │ 多 Agent / 同模型 / 多模型
    ▼
串行或并行调度 ──→ AgentAdapter ──→ Agent CLI
    │                         │
    │                         ├── MCP stdio ──→ 尝试工具服务
    │                         └── 模型请求 ──→ 可选 Wire 反向代理
    ▼
环境评分器 ──→ 分数、产物、观测摘要 ──→ SQLite / 尝试目录 ──→ 前端
```

1. `POST /api/runs` 校验环境、任务、Agent、模型映射和对比模式，再为每个组合
   创建 Attempt；
2. `backend/run_dispatch.py` 按 `parallel` 或 `serial` 调度，并通过
   `backend/adapters/base.py` 的统一输入协议调用适配器；
3. 适配器在 `data/attempts/<attempt_id>/skill_workspace/` 中启动 Agent；
4. 环境声明 MCP 入口时，适配器只注入该入口。工具调用通过带 Bearer Token 的
   `POST /attempts/{attempt_id}/tools/{tool_name}` 转发，`lane.env_api` 负责计时和
   写入 `trace.jsonl`；
5. Agent 结束后，`backend/evaluator.py` 调用环境 `scorer.py`，随后追加安全扫描、
   Wire 汇总和多轮诊断；
6. 前端通过运行、尝试、产物、Wire 和安全事件 API 展示结果。

## 多轮对话

任务可以在 `context._conversation` 中声明多轮计划。普通历史任务会被映射为一轮
`send_message`。多轮驱动器校验轮次顺序、动作、最终评分点和交互应答能力，并将
控制面事件追加到 `conversation.jsonl`。该日志默认只保存 Prompt 的字节数和哈希，
不保存 Prompt 原文；恢复时还会校验计划哈希，避免把修改后的任务接到旧会话上。

适配器必须显式声明是否支持会话续接和 `answer_interaction`。能力不匹配会在启动
前拒绝，不会静默降级为语义不同的单轮执行。

## 隔离与信任边界

- 每个 Attempt 使用独立的 `skill_workspace/`、会话令牌和环境 SQLite；
- Attempt 根目录属于框架，存放事件、Wire、对话和预览缓存，不进入 Agent 产物
  命名空间；
- Claude Code 使用独立 `CLAUDE_CONFIG_DIR`/`HOME`，Codex 使用独立
  `CODEX_HOME`，避免继承操作者的全局插件、技能、记忆和配置；
- 内置本地适配器仍是宿主机进程。上述隔离用于阻止跨尝试状态泄漏，并不等价于
  操作系统安全沙盒；
- Office 文件按不可信业务产物处理：服务端受限解析，浏览器只接收静态描述，
  不执行宏、公式、嵌入对象或外部关系。

## 观测与存储

SQLite 保存任务、运行、尝试、评分和汇总字段；每个 Attempt 目录保存详细证据：

- `events.jsonl`、`thinking.jsonl`：Agent 原生事件与可用的推理内容；
- `trace.jsonl`、环境数据库：工具调用和场景状态；
- `conversation.jsonl`：多轮控制面边界；
- `wire.jsonl`、`wire-manifest.json`、`trajectory.json`：规范化通信证据、完整性
  信息和轨迹；
- `skill_workspace/`：Agent 可见并可作为产物下载的唯一目录。

Wire 采集策略分为 `off`、`metadata`、`parsed`、`full`。请求级策略还会与服务端
上限取更严格的交集；原始 Blob API 默认关闭。

## 公平性原则

公平指相同任务、材料、预算和外部资源边界，不要求不同 Agent 拥有完全相同的原生
工具。适配器不应禁用原生搜索、技能或任务拆解能力，也不应在 Prompt 中硬编码偏好的
解题方式。环境能力只能来自 `meta.yaml` 的显式声明，框架不会根据文件名猜测或虚构
MCP 服务。

## 扩展点

- 新 Agent：在 `arena.yaml` 配置 `custom_agents`，或实现 `AgentAdapter`；
- 新环境：在 `envs/` 下增加符合协议的目录；
- 新通信来源：实现 `backend/wire/` 的 Source Contract，并转换到规范模型；
- 新产物预览器：遵守版本化描述协议和隔离 Worker 边界。
