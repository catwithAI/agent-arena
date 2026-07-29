# 接入 Agent

agent-arena 内置 **Claude Code**、**Codex**、**Kimi Code**、**OpenCode**、
**MiMo Code** 和固定版本的 **DeerFlow 2**，并通过 registry 支持 CLI profile、
ACP、受信任的 Python plugin 和远程服务。

内置集成会保留已经验证的原生能力，例如 WebSearch、子 Agent/任务委派、skill 和
slash command。Adapter 只隔离宿主机本地状态，并显式公布不支持的能力。参见
[公平性说明](#公平性说明)。

## 内置：Claude Code

`backend/adapters/claude_code.py` 启动：

```text
claude -p "<prompt>" --output-format stream-json --verbose \
  --model <model> --max-budget-usd <budget> \
  --dangerously-skip-permissions \
  [--mcp-config <generated mcp_config.json>]
```

- 逐行解析 `stream-json`。`type=assistant` turn 包含
  `thinking`、`text`、`tool_use` 和 `tool_result` block；
  `type=result` 提供成本、token usage 和成功/失败摘要。
- `CLAUDE_CONFIG_DIR`/`HOME` 指向干净的 Attempt 私有目录，不读取宿主机全局
  `~/.claude` 中的 skill、plugin、MCP、memory、`CLAUDE.md` 或 setting。这是状态
  隔离，不会禁用 CLI 自带工具。
- 只有场景 `meta.yaml` 声明 `entrypoints.mcp` 时才传入 `--mcp-config`；adapter
  不会根据 `env_name` 猜测 server。
- 第三方 provider 使用 `arena.yaml` 的 `model_providers` 前缀，例如
  `"openrouter/glm-5"`。Adapter 只向子进程注入 `ANTHROPIC_BASE_URL` 和
  `ANTHROPIC_AUTH_TOKEN`，不修改全局 Claude 配置。

## 内置：Codex

`backend/adapters/codex.py` 启动：

```text
codex exec --json --skip-git-repo-check --ephemeral --ignore-rules \
  --dangerously-bypass-approvals-and-sandbox \
  -C <skill_workspace> -o <final_message_path> \
  [-c mcp_servers.<name>.command=... ...] [-c model_providers.*...] \
  "<prompt>"
```

- 使用一次性 `-c key=value` 覆盖，不修改全局 `~/.codex/config.toml`。
- `CODEX_HOME` 指向 Attempt 私有目录，不泄漏全局 config、skill、plugin、memory
  或 history。
- 仅在场景声明 MCP 时生成 `mcp_servers.*` 覆盖。Attempt 凭证
  `LANE_ATTEMPT_ID`、`LANE_SESSION_TOKEN`、`LANE_BASE_URL` 不进入进程列表可见的
  `-c` 参数，只在确实要启动 MCP 子进程时进入环境变量。
- 第三方 provider 必须支持 OpenAI Responses API（`wire_api: responses`）；
  Codex 不再支持自定义 provider 的旧 `chat` wire protocol。

## 内置（可选）：SSH Claude Code

`backend/adapters/ssh_claude_code.py` 通过 `ssh`/`scp` 在远程主机执行与本地
Claude Code 相同的 `claude -p ... --output-format stream-json` 流程，适合专用
worker 等不同网络或文件系统环境。

默认不启用。只有 `arena.yaml` 配置 `ssh_claude_code.ssh_host`，或设置
`LANE_SSH_CLAUDE_HOST`、`LANE_SSH_CLAUDE_USER`、`LANE_SSH_CLAUDE_PASSWORD`
后才注册为 `"ssh-claude-code"`：

```yaml
ssh_claude_code:
  ssh_host: "10.0.0.5"
  ssh_user: "ai"
  ssh_password: "..."   # 建议改用 LANE_SSH_CLAUDE_PASSWORD
  max_budget_usd: 5.0
```

- Prompt 和 MCP 配置先写入本地文件再通过 SCP 上传，避免引号、换行和反引号被远程
  shell 解释。
- 远程 MCP server 使用预先部署的 `/tmp/lane-mcp-venv/bin/python`；
  `entrypoints.mcp.command` 指定的 Python entrypoint 会按 Attempt 复制。目前每个
  场景只支持一个声明的 MCP server。
- 远程 CLI 没有本地 spool/injection channel，因此
  `wire_capture_capabilities` 的所有字段均为 unsupported。

## 内置：DeerFlow

稳定的 `deerflow` descriptor 固定到 `deerflow-harness==2.0.0` 和 revision
`7e7f0410797693cf882594555ba414e0361d4c6f`。管理员必须预装 package 与
`deerflow-arena-runner`，普通 Run 不会安装或更新它们。每个 Attempt 都有私有
DeerFlow project、home、config，以及经过校验的 `skill_workspace` bridge。

当前集成支持已验证的单轮 runner 和本地 sandbox event stream。Lane MCP、跨 Attempt
resume 和可观测的子 Agent identity 仍不支持。详见
[固定版本 spike](specs/scalable_agent_integration/deerflow-spike.md)。

## 内置（实验性）：Kimi Code

`kimi-code` descriptor 通过共享本地 profile runtime 使用 Kimi Code CLI 0.29+：

```text
kimi -p "<prompt>" --output-format stream-json \
  [-m <model>]
```

- 结构化 JSONL 会映射为统一 event/final-text 契约。
- 多轮场景只使用首轮显式产生的 `session.resume_hint`，通过
  `-r <session_id>` 恢复；不会选择“最新”session。
- 声明的 MCP server 写入 Attempt 私有 `$KIMI_CODE_HOME/mcp.json`。Kimi Code
  0.29.1 没有 `--mcp-config-file` 参数。
- CLI 使用 Attempt 私有 home，不继承全局登录、session、skill、plugin 或配置。
  凭证通过环境变量提供，至少设置 `KIMI_MODEL_NAME` 和 `KIMI_MODEL_API_KEY`；
  可选 `KIMI_MODEL_*` 选择 endpoint 和 provider protocol。显式覆盖 Arena 模型时，
  使用隔离 runtime 中可用的 Kimi config alias；环境定义的 alias 为
  `__kimi_env_model__`。

按 [Kimi Code 官方仓库](https://github.com/MoonshotAI/kimi-code)安装，并确保
`kimi` 位于 `PATH`。

## 内置（实验性）：OpenCode 与 MiMo Code

`opencode` 和 `mimo-code` descriptor 通过同一 profile runtime 使用
OpenCode 1.18.5+ 与 MiMo Code 0.1.9+：

```text
opencode run --format json --auto --dir <skill_workspace> \
  [--model <provider/model>] "<prompt>"
mimo run --format json --dangerously-skip-permissions --dir <skill_workspace> \
  [--model <provider/model>] "<prompt>"
```

- JSON event 提供 final text、reasoning、tool activity、aggregate usage，以及用于安全
  多轮 resume 的显式 `sessionID`。
- 始终显式传入 `--dir`。仅设置子进程 cwd 不够，因为该 CLI family 可能发现父项目并
  读取其他 Attempt 的文件。
- 自动批准参数因 family 而异：OpenCode 使用 `--auto`，MiMo fork 使用
  `--dangerously-skip-permissions`。缺少正确参数时，Agent loop 可能正常退出，但工具
  调用会被拒绝。
- 使用 Attempt 私有 HOME/XDG 和 `*_CONFIG_DIR`，不继承全局认证、memory、skill
  或 session。
- 在 provider/MCP 组合配置生命周期形成经过验证的 dialect 前，这两个 headless
  profile 不支持 Lane MCP 注入。

按 [OpenCode 仓库](https://github.com/anomalyco/opencode)或
[MiMo Code 仓库](https://github.com/XiaomiMiMo/MiMo-Code)安装，并确保对应可执行
文件位于 `PATH`。

## Registry 驱动的 Agent 配置

`AgentRegistry` 是目录、兼容性预检和 dispatch 的事实源。`agents.profiles` 使用严格
AgentSpec v1 字段描述本地 CLI；`agents.acp`、`agents.remote` 和
`agents.python_plugins` 的配置示例见
[`arena.yaml.example`](../arena.yaml.example)。旧 `custom_agents` 仍可用于迁移，
在目录中显示为 `source=legacy` 并带 warning。

### ACP v1

ACP 条目使用 `acp:my-agent@1.2.3` 等精确 ID。命令必须已经安装，registry metadata
必须固定 SHA-256。普通 Run 不会执行 registry 中 `binary`、`npx` 或 `uvx` 的安装
metadata。所有条目复用同一个 transport；未匹配的 permission request 会被取消并使
Attempt 失败，不会隐式选择 allow。

### 远程服务

Picker 会展示 endpoint、data residency、源码上传策略和取消语义。Endpoint 必须使用
HTTPS。只有 `upload_files` 开启时才发送文件；返回产物必须同源、通过 size/checksum
验证并解析到 Attempt 工作区内。无法确认的服务端取消记录为
`cancel_requested_remote_unknown`。详见
[远程契约](specs/scalable_agent_integration/remote-transport.md)。

### 受信任的 Python plugin

`agents.python_plugins` 指向外部 `module:attribute`，仅在选中时导入。共享 wrapper
负责 Prompt/MCP 输入、manifest、脱敏、输出限制和产物验证。Plugin 在后端进程内执行，
属于受信任代码而非 sandbox。起步示例见
[示例 package](../examples/python_agent_plugin/README.md)。

## 接入自己的 Agent

新集成应优先使用上述 `agents.profiles`、`agents.acp`、`agents.remote` 或
`agents.python_plugins`。下面保留旧配置型 CLI 和需要 Python 代码的两种实现路径。

### 旧配置型 CLI：`CustomCliAdapter`

只接收 Prompt 并输出结果的 CLI 无需编写 Python，可在 `arena.yaml` 中声明：

```yaml
custom_agents:
  my-agent:
    command: ["my-agent-cli", "--prompt-file", "{prompt_file}"]
    prompt_mode: file        # stdin | file | arg
    output_format: text      # text | jsonl
    # output_format: jsonl
    # jsonl_fields:
    #   type_field: "type"
    #   thinking_type_value: "reasoning"
    #   text_field: "text"
    #   usage_field: "usage"
    # mcp_config_flag: "--mcp-config"
```

随后 `"my-agent"` 会像 `claude-code`/`codex` 一样出现在
`POST /api/runs` 的 `agents` 列表及前端 picker 中。完整字段见
`backend/adapters/custom_cli.py`。

### 框架封装的 Python plugin

实现小型 plugin 契约：

```python
from backend.agents.python_plugin import PythonAgentOutput

class MyAgent:
    async def run(self, context):
        output = context.artifact_path("answer.txt")
        output.write_text("done")
        return PythonAgentOutput(
            final_text="Created answer.txt",
            artifacts=("answer.txt",),
        )
```

在 `agents.python_plugins` 下注册外部 entrypoint，无需修改 dispatch。确实需要新
transport 时，只增加一个 registry builder，并把 runtime/parser 行为保持在标准
`AgentAdapter` 结果契约之后。

## 公平性说明

“公平比较”表示任务、输入材料、时间/预算和外部资源边界一致，不表示把所有 Agent
裁剪成相同工具集合。原生能力差异本身就是评测结果的一部分。

Adapter 会统一：

- **Prompt 形态**：通过 `backend/adapters/base.py` 的 `prompt_context()` 渲染，
  不硬编码首选解法。
- **宿主机隔离**：Claude Code、Codex、Kimi Code、OpenCode 和 MiMo Code 均使用
  Attempt 私有 HOME/config/session 位置，不继承操作者全局 Agent 状态。
- **Attempt 隔离**：每个 Attempt 独享 `skill_workspace`、私有 runtime/control
  目录、session token 和 env server session。

Adapter 不会为了“可比”而关闭原生工具、skill 或任务拆解能力，也不会虚构 MCP
server。只有场景 `meta.yaml` 显式声明 `entrypoints.mcp` 时，Agent 才能获得对应
环境工具。
