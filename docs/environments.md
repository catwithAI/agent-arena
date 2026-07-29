# 编写评测环境

环境是 `envs/<name>/` 下的目录，包含任务、Agent 完成任务可能需要的工具，以及结果的
评分方式。

## 最小目录结构

```text
envs/<name>/
├── meta.yaml           # 必需：type、pass_threshold、dimensions、entrypoints
├── core.py             # @env_tool 工具实现；纯编程任务可为空
├── scorer.py           # 必需：score(...) -> list[dict]
├── mcp_server.py       # core.py 注册工具时需要
├── schema.sql          # 可选：工具状态使用的 Attempt 级 SQLite schema
└── tasks/*.json        # 预定义任务
```

## `meta.yaml`

每个环境必须提供三个展示字段：

- `category`：下表中的稳定顶层能力分类。
- `test_focus`：一句话说明考察能力、约束和评分重点。
- `description`：任务背景、预期产物和评分方法。

顶层分类应保持精简；行业、学科和 benchmark 系列应放进 `type`、描述或未来的 tag。

| `category` | 展示名称 | 范围 |
|---|---|---|
| `general-assistant` | 通用助理 | 搜索、文件阅读、多模态理解、开放式问题求解 |
| `office-productivity` | 办公与内容 | 表格、会计材料、演示文稿、多源业务工作 |
| `real-skill` | 真实技能 | 由外部业务 skill 支撑的确定性执行链 |
| `complex-workflow` | 复杂工作流 | 多步工具编排、规划、产物和恢复 |
| `coding` | 编程与算法 | 实现、优化、静态分析和隐藏测试 |
| `agent-system` | Agent 系统 | 多轮记忆、压缩、子 Agent 和可观测性 |
| `safety-hitl` | 安全与 HITL | 高后果操作前的确认与安全替代方案 |
| `baseline` | 基础约束 | 基本工具使用和显式用户约束遵循 |

```yaml
name: my-env
type: skill              # skill（有工具）| coding（仅提交产物）
category: baseline
description: 显示在 UI 中的一句话说明。
test_focus: 该环境真正考察的内容。
pass_threshold: 60       # score_total >= 此值时 Attempt 状态为 completed

entrypoints:
  mcp:
    enabled: true        # 无工具的纯编程环境设为 false
    transport: stdio
    command: ["uv", "run", "--project", ".", "python", "envs/my-env/mcp_server.py"]

dimensions:
  - name: task_completion
    weight: 60
    description: ...
  - name: constraint_compliance
    weight: 40
    description: ...
```

权重决定如何把 `scorer.py` 的输出聚合为 `score_total`（0–100 的加权平均值）。
所有权重均为 0 或缺失时使用简单平均值。

`entrypoints` 是 Agent 工具能力的唯一事实源。目录中仅仅存在 `mcp_server.py` 并不会
启用工具。`entrypoints.mcp.enabled: false` 或缺少该配置时，dispatcher 不生成 MCP
配置、不启动 capture tap，也不向 Prompt 添加 MCP 文本。

`command` 必须是场景完整、真实的启动 argv；adapter 不会根据 `env_name` 猜测或重构。
命令从项目根目录解析，MCP 子进程通过 `LANE_WORKSPACE` 获得真实的
`data/attempts/<attempt_id>/skill_workspace`。

## 工具：`core.py`

只有 `type: skill` 的环境需要工具。使用 `@env_tool` 装饰普通函数；wrapper 自动处理
trace 写入和计时，函数只需返回可 JSON 序列化的值。

```python
from lane.env_api import EnvContext, env_tool

@env_tool(
    name="my_tool",
    description="展示给 Agent 的工具说明。",
    parameters={
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    },
)
def my_tool(ctx: EnvContext, x: str) -> dict:
    # ctx.db 是当前 Attempt 独享的 sqlite3.Connection
    # schema.sql 会在首次使用时幂等应用；trace 由 wrapper 自动处理
    return {"result": x.upper()}
```

持久状态通过 `ctx.db` 保存。

## `mcp_server.py`

这是一个轻量 MCP stdio wrapper，把每次工具调用通过 HTTP 转发到 Attempt server。
可以复制 `envs/order-desk/mcp_server.py`，再按 `core.py` 调整工具签名。只要
`core.py` 注册了工具，就必须提供此文件。声明支持 MCP 的 Agent 会通过各自支持的
MCP 方言连接这个 wrapper。

## Scorer：`scorer.py`

```python
def score(*, attempt_id, task, env_db, trace, final_state) -> list[dict]:
    # env_db：当前 Attempt 的 SQLite 文件
    # trace：按顺序解析的 trace.jsonl 工具调用
    # final_state：环境写入 final_state.json 时的解析结果
    return [{"dimension": "task_completion", "value": 90, "detail": "..."}]
```

无工具的纯编程环境通常编译或运行 Agent 写入 Attempt 工作区的文件。批量评分示例见
`envs/cpp-optimizer/scorer.py`：一次编译，在 N 个隐藏 fixture 上运行，再归一化为
0–100 分。

## 任务

`tasks/*.json`：

```json
{
  "id": "my_task_001",
  "prompt": "Agent 需要完成的任务。",
  "context": {},
  "constraints": {"any_key": "由 scorer 使用"},
  "timeout_seconds": 600
}
```

`context` 会被渲染进 Agent Prompt（内部 bookkeeping 和上传文件信息除外，由 adapter
单独处理）；`constraints` 对框架透明，由 `scorer.py` 直接读取。

正数 `timeout_seconds` 是 Attempt 时间预算：adapter 通过 `asyncio.wait_for` 强制执行，
超时后终止 CLI 子进程，同时用
`backend/adapters/base.time_budget_notice` 向所有 Agent 注入相同提示。通过
`POST /api/runs` 创建运行时，该字段也可为 `null`，表示无限时：不注入预算提示，也不
执行总超时。省略字段仍使用现有默认值 1000 秒。

## 参考环境

- `envs/order-desk/`：工具型环境，在约束下搜索模拟图书目录并下单。
- `envs/cpp-optimizer/`：纯编程环境，提交 `solution.cpp`，编译后在固定隐藏用例上
  批量评分。
