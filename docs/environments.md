# 开发评测环境

评测环境位于 `envs/<name>/`，把任务、输入材料、可选工具和评分逻辑封装成可独立
加载的单元。环境目录名必须匹配 `^[a-z][a-z0-9_-]*$`，并与 `meta.yaml` 的
`name` 一致。

## 最小目录

```text
envs/<name>/
├── meta.yaml           # 必需：元数据、前置条件、能力入口和评分维度
├── scorer.py           # 必需：score(...) -> list[dict]
├── tasks/*.json        # 必需：至少一个预置任务
├── core.py             # 可选：用 @env_tool 注册工具
├── mcp_server.py       # 可选：声明 MCP 工具时使用
├── schema.sql          # 可选：每次尝试独立环境数据库的结构
├── inputs/             # 可选：评分器或任务输入
└── materials/          # 可选：复制给 Agent 的材料
```

运行时会分别加载 `meta.yaml`、`core.py`、`scorer.py` 和任务文件。单个环境的
`core.py` 导入失败时，服务仍可启动，但该环境会在前端显示为不可用；结构错误、评分器
错误和任务错误应通过校验工具提前发现。

## `meta.yaml`

```yaml
schema_version: "1.0"
name: my-env
type: skill                    # 常用值：skill、coding
category: baseline
test_focus: "这个环境实际测量什么"
description: "前端展示的一句话说明"
pass_threshold: 60

prerequisites:
  level: none
  summary: "本机运行所需条件"
  requires:
    - "python3"
  on_missing: "缺失时对运行或评分的影响"

materials:
  agent:
    - path: materials/public
      target: .

entrypoints:
  mcp:
    enabled: true
    transport: stdio
    command: ["uv", "run", "--project", ".", "python", "envs/my-env/mcp_server.py"]

dimensions:
  - name: task_completion
    weight: 60
    description: "任务完成度"
  - name: constraint_compliance
    weight: 40
    description: "约束遵守情况"
```

`dimensions[*].weight` 用于聚合 `score_total`。权重大于零的维度按加权平均计算；
如果所有维度权重都为零或缺失，则退化为简单平均。零权重维度适合展示诊断指标，
例如上下文保真度，而不改变历史任务总分口径。

`entrypoints` 是 Agent 获得环境能力的唯一事实来源。即使目录中存在
`mcp_server.py`，只要 `entrypoints.mcp.enabled` 不是 `true`，调度器就不会生成
MCP 配置或在 Prompt 中提及工具。`command` 必须是完整参数数组；适配器不会根据
环境名猜测启动命令。

前置条件的完整语义见[环境前置条件](env-prerequisites.md)。

## 任务文件

```json
{
  "id": "my_task_001",
  "prompt": "Agent 需要完成的工作。",
  "context": {},
  "constraints": {"required_output": "result.json"},
  "files": [
    {"path": "inputs/source.pdf", "name": "source.pdf"}
  ],
  "timeout_seconds": 600
}
```

规范字段为：

- `id`、`prompt`：非空字符串；
- `context`、`constraints`：对象；其中 `constraints` 对框架透明，由评分器解释；
- `files`：字符串或含 `path` 的对象数组。相对路径优先按环境目录解析，再在调度时
  复制到每个 Attempt 的独立工作区；
- `timeout_seconds`：正整数。文件任务默认 600 秒；API 临时任务省略时默认 1000 秒，
  显式传 `null` 表示不设总时限。

加载器仍兼容 `task_id → id`、`query → prompt`、`timeout → timeout_seconds` 和
`files → context.uploaded_files`，但新环境应直接使用规范字段。

### 多轮任务

多轮计划放在 `context._conversation` 中；下划线前缀使它不会被普通上下文渲染器
重复塞进 Prompt。

```json
{
  "id": "multi_turn_001",
  "prompt": "首轮兼容说明",
  "context": {
    "_conversation": [
      {
        "id": "setup",
        "action": "send_message",
        "purpose": "setup",
        "prompt": "读取材料并记住关键事实。"
      },
      {
        "id": "probe",
        "action": "send_message",
        "purpose": "probe",
        "prompt": "回答先前事实，并写出结果文件。",
        "score_after": true
      }
    ]
  },
  "timeout_seconds": 1200
}
```

计划还支持 `answer_interaction` 与 `wait_for`，但只有显式声明相应能力的适配器才能
运行。`turn_index` 由数组位置确定，ID 不能重复，`score_after` 最多出现一次且必须
位于最后一轮。没有 `_conversation` 的任务按历史单轮语义执行。

## Agent 材料

`materials.agent` 声明环境级公共材料。调度器按条目把 `path` 复制到
`skill_workspace/` 的 `target`；任务级 `files` 再复制具体输入文件。隐藏答案、
Rubric 或参考产物不得放进 Agent 可见材料。

材料属于评测协议的一部分。修改题面、示例工具或 Starter 代码可能改变基准语义，
应像修改评分器一样评审，并在需要时提升数据或生成器版本。

## 工具实现：`core.py`

只有工具型环境需要注册工具。`@env_tool` 自动记录调用参数、结果、错误和耗时；业务
函数只需返回可 JSON 序列化的数据。

```python
from lane.env_api import EnvContext, env_tool

@env_tool(
    name="my_tool",
    description="工具用途",
    parameters={
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    },
)
def my_tool(ctx: EnvContext, value: str) -> dict:
    # ctx.db 是本 Attempt 独享的 SQLite 连接
    return {"result": value.upper()}
```

`schema.sql` 会在首次工具调用时幂等应用到 `env.db`。环境模块不应修改全局
`sys.path`；公共接口只从顶层 `lane` 包导入。

## MCP 入口：`mcp_server.py`

MCP Server 是薄包装层：通过 stdio 接收 Agent 调用，再把请求转发给后端 Attempt
Server。可参考 `envs/order-desk/mcp_server.py`。框架会注入
`LANE_ATTEMPT_ID`、`LANE_SESSION_TOKEN` 和 `LANE_BASE_URL`，不要把这些凭证
写进命令行或任务材料。

## 评分器：`scorer.py`

```python
def score(*, attempt_id, task, env_db, trace, final_state) -> list[dict]:
    return [
        {"dimension": "task_completion", "value": 90, "detail": "..."}
    ]
```

- `task`：规范化后的任务字典；
- `env_db`：本 Attempt 的环境 SQLite 路径；
- `trace`：解析后的工具调用轨迹；
- `final_state`：环境写出的 `final_state.json`，不存在时为空对象。

纯代码环境通常从 Attempt 工作区读取提交物，编译或批量执行隐藏用例。评分器异常会
把 Attempt 标为 `scoring_failed`；安全扫描独立运行，不影响任务得分。

## 校验与自检

```bash
# 单个环境
uv run python scripts/lint_env.py my-env

# 全部环境
uv run python scripts/lint_env.py --all

# 服务启动后的基础设施检查
curl http://127.0.0.1:8100/api/selfcheck
```

校验器与运行时复用相同的名称和任务规范化逻辑，并额外要求
`schema_version` 与至少一个任务文件。

## 参考环境

- `order-desk`：MCP 工具、环境数据库和约束评分；
- `cpp-optimizer`：无工具的 C++ 批量评分；
- `edgebench-juliet`：向 Agent 复制公共材料并用隐藏数据评分；
- `ppt-visual-repair`：Office 产物与多模态 Judge；
- `context-compaction-benchmark`：多轮任务和零权重诊断维度。
