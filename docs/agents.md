# 接入 Agent

agent-arena 内置 **Claude Code** 与 **Codex** 适配器，并提供可选的远程 Claude
Code 和两种自定义接入方式。适配器负责把统一的任务输入转换为 CLI 调用、持续采集
事件，并把所有成功或失败结果归一化为 `AdapterResult`。

## 统一约束

所有适配器都应遵守以下边界：

- Agent 的当前目录是本 Attempt 的 `skill_workspace/`；
- 使用统一的 Prompt 上下文与时间预算提示，不硬编码偏好的解题工具；
- 环境只在 `meta.yaml` 显式声明 MCP 入口时获得工具；
- 明文会话令牌只通过子进程环境传递，不进入参数、事件或持久化引用；
- 失败转换为终态和错误码，不把异常抛出到调度层；
- 显式声明执行位置、网络需求和交互应答能力；
- 多轮任务必须续接同一个明确的 Session/Thread，不能用“最近会话”之类会跨
  Attempt 串线的隐式选择。

公平比较保留各 Agent 的原生搜索、技能、子 Agent 和任务拆解能力。框架隔离的是
操作者私有配置与不同 Attempt 的状态，不会为了“工具看起来相同”而削弱 Agent。

## 内置 Claude Code

`backend/adapters/claude_code.py` 以 `claude -p` 和 `stream-json` 模式运行 CLI，
启用详细事件与子 Agent 文本转发，并按行提取文本、推理、工具调用、Token 和成本。

- `CLAUDE_CONFIG_DIR` 与 `HOME` 指向 Attempt 私有目录，避免读取宿主机的
  `~/.claude`；
- 单轮保持一次性历史行为；多轮首轮使用明确 Session ID，后续用该 ID 恢复；
- 环境声明 MCP 时才生成并传入 MCP 配置；
- 第三方 Anthropic 协议服务通过 Attempt 子进程环境注入 Base URL 和凭证；
- `claude -p` 当前没有运行中回答交互问题的通道，因此
  `answer_interaction` 能力为关闭状态。

CLI 需支持 `--forward-subagent-text`；当前实现依赖该选项观测子 Agent 事件。

## 内置 Codex

`backend/adapters/codex.py` 使用 `codex exec --json`，通过一次性 `-c` 参数注入
模型服务和 MCP 配置，不修改用户全局 `~/.codex/config.toml`。

- `CODEX_HOME` 指向 Attempt 私有目录；
- 单轮使用 `--ephemeral`；多轮首轮持久化明确的 Thread ID，后续执行
  `codex exec resume <thread_id>`；
- Attempt 凭证只进入环境变量，不放入进程列表可见的 `-c` 参数；
- 自定义模型服务必须兼容 OpenAI Responses API；
- 当前 `codex exec` 同样不支持框架在运行中回答交互问题。

## 第三方模型服务

在本地 `arena.yaml` 中配置：

```yaml
model_providers:
  openrouter:
    kind: openai-responses       # anthropic | openai-chat | openai-responses
    base_url: "https://openrouter.ai/api/v1"
    api_key_env: "OPENROUTER_API_KEY"
    wire_api: responses

model_suggestions:
  - "sonnet"
  - "gpt-5"
  - "openrouter/openai/gpt-5"
```

运行时模型写成 `<provider>/<model>`。环境变量优先于配置文件中的 `api_key`
回退值；两者都不存在时，Attempt 会立即以 `provider_api_key_missing` 失败。

Provider 的协议必须与 Agent 匹配：Claude Code 使用 Anthropic 兼容协议；Codex 的
自定义 Provider 使用 Responses 协议。Wire 反向代理会在可支持的组合上注入，但不会
改变协议兼容性。

## 可选的远程 Claude Code

配置 `ssh_claude_code.ssh_host` 后会注册 `ssh-claude-code`：

```yaml
ssh_claude_code:
  ssh_host: "10.0.0.5"
  ssh_user: "ai"
  ssh_password: "..."     # 更推荐 LANE_SSH_CLAUDE_PASSWORD
  max_budget_usd: 5.0
```

该适配器通过 SSH/SCP 上传 Prompt 与 MCP 配置，避免把任务文本拼进远端 Shell。
远端需要预先提供 `/tmp/lane-mcp-venv/bin/python`；当前每个环境最多支持一个已声明
的 MCP Server。远端进程没有本地 Wire Spool/注入通道，因此通信观测能力会明确标记
为不支持。

也可以使用 `LANE_SSH_CLAUDE_HOST`、`LANE_SSH_CLAUDE_USER` 和
`LANE_SSH_CLAUDE_PASSWORD` 覆盖配置。

## 方式一：仅配置 CLI Agent

当 Agent 能接收 Prompt 并向 stdout 输出文本或 JSONL 时，优先使用
`CustomCliAdapter`：

```yaml
custom_agents:
  my-agent:
    command: ["my-agent-cli", "--prompt-file", "{prompt_file}"]
    prompt_mode: file            # stdin | file | arg
    output_format: jsonl         # text | jsonl
    env:
      MY_AGENT_MODE: benchmark
    jsonl_fields:
      type_field: "type"
      thinking_type_value: "reasoning"
      text_field: "text"
      usage_field: "usage"
    mcp_config_flag: "--mcp-config"
```

`prompt_mode` 的语义：

- `stdin`：Prompt 写入标准输入；
- `file`：生成文件，并用 `{prompt_file}` 替换命令参数；
- `arg`：用 `{prompt}` 替换命令参数。

`output_format: text` 会保留逐行事件和最终文本，但没有结构化推理/用量；JSONL 模式
可通过点分字段路径提取推理与 Token。配置型 CLI 没有会话恢复和交互应答通道，适合
单轮任务。

配置完成后，Agent 会出现在 `GET /api/agents` 和前端选择器中，并可直接用于
`POST /api/runs`。

## 方式二：实现 Python 适配器

需要自定义传输、重试、会话恢复或用量统计时，实现
`backend/adapters/base.py` 定义的协议：

```python
class MyAdapter:
    capabilities = AdapterCapabilities(
        execution_locus="host",
        network_required="public_internet",
        interaction_answer=False,
    )

    async def run(self, task: AdapterRunInput, env, data_path: Path) -> AdapterResult:
        # 使用 task.task_prompt / task.task_context / task.conversation_turns
        # 只接入 task.mcp_servers 中显式声明的服务
        # 消费支持的 task.wire_injection 字段
        # 捕获所有异常并返回 AdapterResult
        ...
```

随后在 `backend/run_dispatch.py::build_adapter` 注册。还应提供准确的
`wire_capture_capabilities`，让生命周期管理器在启动前丢弃适配器无法消费的注入
字段，而不是制造虚假的观测覆盖。

## 对比模式

同一适配器可以参与三种展开方式：

- `multi-agent`：多个 Agent 各运行一次，可分别指定模型；
- `same-model`：至少两个不同 Agent 使用同一个模型，`models` 映射必须覆盖所有
  Agent；默认串行，避免争用本地独占模型；
- `multi-model`：一个 Agent 针对模型列表各运行一次；
- `execution` 可显式选择 `parallel` 或 `serial`。

适配器不需要自行实现对比展开；API 和调度层会为每个组合创建独立 Attempt。

## 接入检查清单

1. CLI 不存在、认证失败、超时和非零退出都能返回稳定错误；
2. 工作目录、配置目录和 Session/Thread 不会跨 Attempt 复用；
3. MCP 仅按声明接入，明文 Token 不进入日志与参数；
4. 时间预算只在首轮提示，并由适配器执行；
5. 多轮能力、交互能力和 Wire 能力声明与真实实现一致；
6. 文本/JSONL 解析面对坏行和部分输出时仍能保留证据并正常收尾。
