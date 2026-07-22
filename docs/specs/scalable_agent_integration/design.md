# 规模化 Agent 接入——设计文档

> 状态：Proposed
>
> 日期：2026-07-21
>
> 需求：[requirements.md](requirements.md)

## 1. 设计结论

采用“**注册表描述身份、共享 runtime 执行、driver 表达会话、parser 解释证据、transport
覆盖协议差异**”的结构：

```text
builtin profiles ─┐
arena.yaml ───────┼──> AgentRegistry ──> resolved AgentSpec + compatibility check
Python plugins ───┘                              │
                                                 ▼
                                 deterministic LaunchPlan
                                                 │
                     ┌───────────────────────────┼──────────────────────┐
                     ▼                           ▼                      ▼
                LocalCliRuntime             ACP transport        SDK/remote
                     │
          conversation driver + MCP dialect
                     │
          raw stdout/stderr/session evidence
                     │
                     ▼
            parser / wire normalizer
                     │
                     ▼
       AdapterResult + agent-manifest.json
```

关键决策：

1. 不为每个常规 CLI 写完整 Adapter；
2. registry 是 Agent 列表、构建和 API 的唯一真相源；
3. profile 是数据，不允许嵌入任意 Python 或 shell 模板逻辑；
4. runtime 统一持有高风险生命周期逻辑；
5. capability 默认 unsupported，验证后逐项开放；
6. 宿主机不隐式自动安装；
7. ACP 是一个 transport，不是一批复制的 Agent adapter；
8. Claude/Codex 先包进 registry，底层实现可暂时保持不变，以避免大爆炸迁移。

## 2. 当前实现与缺口

现有锚点：

```text
backend/adapters/base.py         AdapterRunInput/Result、能力和进程树工具
backend/adapters/custom_cli.py   配置型一次性 CLI
backend/adapters/claude_code.py  完整原生 driver/parser
backend/adapters/codex.py        完整原生 driver/parser
backend/adapters/ssh_claude_code.py
backend/run_dispatch.py          known_agents/build_adapter/dispatch
backend/config.py                custom_agents 配置
backend/api.py                   /agents availability
backend/wire/                    capture、normalizer、manifest
backend/security/                execution meta 与离线扫描
```

`CustomCliAdapter` 可作为行为样板，但不直接扩展成巨型类。当前需要修正的结构性缺口：

- profile、Agent 列表和构造逻辑分散；
- 多轮 `conversation_turns` 没有通用 driver；
- MCP 生成应完全消费 `task.mcp_servers`，而不是按 env 名称推导；
- subprocess 没有统一的 process group、双流 drain 和 cleanup contract；
- parser、transport、启动逻辑混在一起；
- availability 只有 `which()`；
- 自定义 Agent 没有细粒度 capability 和 manifest。

## 3. 模块布局

新增包：

```text
backend/agents/
├── __init__.py
├── models.py              # AgentSpec、Capabilities、Availability、LaunchPlan
├── registry.py            # 来源合并、冲突、lazy import、legacy 映射
├── loader.py              # YAML/profile schema 读取
├── compatibility.py       # task/model/MCP/conversation preflight
├── manifest.py            # agent-manifest.json 与 plan hash
├── availability.py        # 只读探测、缓存、版本约束
├── secrets.py             # secret ref 解析和 redacted snapshot
├── runtime/
│   ├── base.py
│   └── local_cli.py       # subprocess、timeout、cancel、双流 drain、cleanup
├── drivers/
│   ├── base.py
│   ├── oneshot.py
│   └── command_resume.py  # 显式 session ID 的首轮/续轮
├── mcp/
│   ├── base.py
│   ├── json_file.py
│   └── command_register.py
├── parsers/
│   ├── base.py
│   ├── text.py
│   └── jsonl.py
├── plugins/
│   └── deerflow/
│       ├── plugin.py       # config/workspace/model/feature bridge
│       ├── runner.py       # DeerFlowClient headless NDJSON runner
│       └── parser.py       # StreamEvent + bounded summary parser
├── transports/
│   ├── adapter.py         # registry resolved impl -> AgentAdapter bridge
│   └── acp.py             # 后续阶段
└── profiles/
    └── deerflow.yaml       # identity/probe/metadata，implementation 指向 plugin
```

插件实现继续位于 `backend/adapters/` 或独立安装包，通过 import path 暴露。registry 不要求
插件必须放在核心包内。

## 4. AgentSpec

建议 Pydantic 结构：

```python
class AgentSpec(BaseModel):
    schema_version: Literal["1"]
    id: str
    display_name: str
    source: Literal["builtin", "config", "plugin", "legacy"]
    transport: Literal["local-cli", "ssh-cli", "acp", "python-sdk", "remote"]
    implementation: ImplementationSpec
    availability: AvailabilityProbeSpec
    launch: LaunchSpec | None
    prompt: PromptSpec
    model: ModelBindingSpec
    auth: tuple[SecretRefSpec, ...]
    mcp: McpDialectSpec
    output: OutputSpec
    capabilities: CapabilitySpec
    isolation: IsolationSpec
    metadata: AgentMetadata
```

这里的 `AgentSpec` 是 registry 解析后的内部结构；`source` 由 loader 根据实际来源写入，
用户 profile schema 不允许自行声称 `source=builtin`。同理，spec hash、override provenance
和 availability 都是解析结果，不是可伪造的输入字段。

### 4.1 implementation

三种实现：

```yaml
implementation:
  kind: profile-runtime
```

```yaml
implementation:
  kind: plugin
  import_path: my_package.agent:MyAgentPlugin
```

```yaml
implementation:
  kind: existing-adapter
  import_path: backend.adapters.codex:CodexAdapter
```

`existing-adapter` 是 Claude/Codex/SSH Claude 的迁移桥。核心 dispatch 只认识 registry
返回的 builder，不再认识具体 Agent 名称。

### 4.2 launch template

launch 不接受 shell format string，使用 token 化模板：

```yaml
launch:
  executable: opencode
  args:
    - run
    - {value: prompt_file, omit_if_none: true}
    - {flag: --model, value: effective_model, omit_if_none: true}
  cwd: attempt_workspace
```

允许的 value 必须来自枚举：`prompt`、`prompt_file`、`effective_model`、`session_id`、
`mcp_config_file`、声明过的非敏感 option。未知变量 schema 校验失败。secret 永远不能成为
argv value。

复杂的条件分支、多个 setup command 或动态 JSON 应由 driver/MCP dialect plugin 实现，
不继续扩张模板语法。

### 4.3 capabilities

能力不是一组乐观 bool，而是带依据的值：

```python
class CapabilityValue(BaseModel):
    state: Literal["verified", "declared", "unsupported"]
    basis: str | None
```

`CapabilitySpec` 包含 conversation、MCP、structured events、token usage、thinking、tools、
sub-agent identity、Wire、execution locus。registry schema 可以使用简写，加载后规范化为完整
结构。

## 5. Registry 解析

### 5.1 来源和优先级

加载顺序只用于形成诊断，不用于静默覆盖：

1. 内置 profile 与内置 existing-adapter；
2. `arena.yaml` 的 `agents.profiles`；
3. `agents.plugins` import paths；
4. legacy `custom_agents` 转换结果。

任意重复 ID 默认报错。配置项写 `override: true` 时可以替换内置项，但 manifest 同时保存
原 spec hash 和覆盖 spec hash，API source 返回 `config-override`。

### 5.2 API 和 builder

目标接口：

```python
registry = AgentRegistry.from_settings(settings)
descriptors = await registry.describe_all()
resolved = registry.resolve(agent_id)
adapter = resolved.build_adapter(model=requested_model)
```

`backend/run_dispatch.py` 保留 dispatch 生命周期，只把：

```python
known_agents(settings)
build_adapter(agent_name, settings, model)
```

替换为 registry 调用。测试期间可保留同名 facade，减少调用面变更。

registry 由 app lifespan 构造一次放入 `app.state`/`runtime_state`。配置 reload 不在首期范围；
变更 `arena.yaml` 后重启服务。

## 6. Compatibility preflight

创建 attempts 前执行纯函数检查：

```python
check_compatibility(
    spec,
    requested_model,
    provider,
    env_mcp_servers,
    conversation_plan,
) -> CompatibilityReport
```

检查顺序：

1. spec/profile schema 有效；
2. 当前平台和 execution locus 支持；
3. CLI/dependency/version；
4. auth refs 存在；
5. requested model/provider 可绑定；
6. 环境需要 MCP 时 Agent 支持；
7. conversation actions 都被支持；
8. strict Wire/coverage 要求（若请求）满足。

API 对用户输入错误返回 4xx 和结构化 mismatch；dispatch 启动时再检查一次运行态依赖，
防止 CLI 在提交与执行之间消失。后者失败写稳定 Attempt 终态。

同一个 run 中有多个 Agent 时分别生成 report；默认整单原子拒绝，避免只创建部分比较组。

## 7. LocalCliRuntime

### 7.1 生命周期

```text
resolve spec
  → preflight
  → create private dirs
  → render prompt/conversation plan
  → render MCP config
  → resolve secrets into child env
  → write initial agent-manifest (status=prepared)
  → spawn process group
  → concurrently drain stdout/stderr
  → parser consumes persisted evidence
  → timeout/cancel/exit cleanup
  → finalize manifest + AdapterResult
```

runtime 使用 `asyncio.create_subprocess_exec(..., start_new_session=True)`。stdout/stderr 各有
独立消费 task，原始记录写 `raw/stdout.log`、`raw/stderr.log` 或 framed JSONL。取消顺序：

1. 向 process group 发送 TERM；
2. 等待 profile/runtime 固定 grace period；
3. 仍存在则 KILL process group；
4. await 两个 drain task；
5. 写 exit/cleanup event。

不得只 `proc.kill()`。MCP server 由 Agent 拉起时同属其 process group；若某 CLI daemonize，
profile 必须声明额外 cleanup strategy 或不通过准入。

### 7.2 日志背压和界限

- stdout/stderr 同时 drain；
- 单行默认 10 MiB 上限，超过后分片或截断并标记；
- attempt 原始日志总量使用配置上限，达到后继续 drain 但只写 drop counter；
- parser 从已落盘 evidence 读取，不能成为 subprocess pipe 的同步瓶颈；
- 最终文本由 parser 明确产出，不能默认把所有 stdout 拼接成用户答案。

### 7.3 错误分类

共享错误 taxonomy：

```text
agent_not_installed
agent_version_unsupported
agent_auth_missing
agent_auth_failed
agent_model_unsupported
agent_rate_limited
agent_usage_limit
agent_network_error
agent_timeout
agent_cancelled
agent_nonzero_exit
agent_output_parse_degraded
agent_cleanup_failed
agent_internal_error
```

profile 可以声明只用于失败输出的 regex patterns，但内置 pattern 必须有 fixture，且匹配到的
原文经过 redaction。多个 pattern 命中时使用明确优先级并保存非敏感 producer code。

## 8. Driver 和多轮

### 8.1 OneShotDriver

仅接受一个有效 `send_message`。历史单轮 task 渲染为一次调用。若 effective conversation
包含多个消息或 interaction，preflight 失败。

### 8.2 CommandResumeDriver

profile/driver 定义 `first_turn` 与 `resume_turn` 两个 launch plan。首轮必须获得显式 session
ID，来源只能是：

- 启动前由框架生成并传给 CLI；
- CLI 结构化事件明确返回；
- Attempt 私有 session 目录中可唯一确定的记录。

后续轮使用这个 ID。任何“最新 session”推断都禁止。每轮 raw evidence 分文件或带
`turn_id` frame，parser 可以离线重建。

### 8.3 InteractionDriver

只有 transport 确实提供运行中双向通道时才实现 `answer_interaction`。普通一次性 CLI 即使
支持 stdin Prompt，也不能因此声明 interaction；stdin 可能已被 CLI 用作协议通道。

## 9. MCP dialect

统一输入是 `tuple[McpServerSpec, ...]`。先转成中间模型：

```python
ResolvedMcpServer(
    name, command, args, cwd,
    env={"LANE_ATTEMPT_ID": ..., "LANE_SESSION_TOKEN": ..., "LANE_BASE_URL": ...},
)
```

具体 dialect 只负责格式转换：

```text
JsonFileDialect       -> Attempt 私有 mcp.json
CommandRegister       -> Agent 私有 config home 中执行注册命令
NativeConfigDialect   -> 生成 Agent 专属 TOML/JSON/YAML
UnsupportedDialect    -> 仅允许空 server 列表
```

`CommandRegister` 不能修改真实用户 HOME。注册命令和主 Agent 进程共享 Attempt 私有 config
root。生成文件 mode 为 owner-only；manifest 只保存 redacted hash/shape，不保存 token。

Wire 的 MCP command rewrite 在 dialect render 前应用，顺序固定为：

```text
task McpServerSpec → Wire command rewrite → Agent MCP dialect → private config
```

这样新增 Agent 不需要进入 `_MCP_TAP_AGENTS` 名单；是否能消费 rewrite 由 dialect capability
声明。

## 10. Parser 与 Wire

parser contract：

```python
class AgentOutputParser(Protocol):
    parser_id: str
    parser_version: str
    async def parse(self, evidence: EvidenceSet, context: ParseContext) -> ParseResult: ...
```

`ParseResult` 包含最终文本、标准 events、thinking、tool refs、usage evidence、session ID、
coverage 和 parse diagnostics。parser 不直接写 DB。

分层实现：

- `TextParser`：保留逐行 raw event，最后非空 stdout 或指定 output file 为 final text；
- `JsonlMappingParser`：延续现有 dotted field mapping，但增加 schema version、finish/session/
  tool 字段和严格 diagnostics；
- Agent native parser：专门理解私有事件/session schema；
- ATIF parser：若 Agent 原生提供 ATIF，映射到 trajectory，但 Wire 仍保留自己的 schema。

Wire 集成通过 registry metadata 找 normalizer factory，不再使用硬编码 Agent 名称集合：

```python
capabilities.wire.native_normalizer = "backend.wire.normalizers.foo:FooNormalizer"
capabilities.wire.http_injection = true
capabilities.wire.mcp_command_rewrite = true
```

不存在 normalizer 时仍可运行任务，manifest 明确 `native-event=not-applicable`。MCP inbound
和可注入 HTTP source 可以独立提供部分证据。

## 11. Availability

`AvailabilityService` 执行 profile 声明的只读探测：

1. `shutil.which(executable)`；
2. 有超时的 `version_command`；
3. 解析并检查版本范围；
4. 检查 system dependencies；
5. 检查 secret ref 是否存在；
6. plugin import 只在对应 descriptor 探测时尝试。

缓存 key 为 spec hash + PATH 摘要 + 相关 secret 是否存在的布尔摘要，默认短 TTL。缓存绝不
保存 secret value。`refresh=true` 可由管理 UI 后续触发，普通列目录使用缓存。

API 兼容现有前端字段：迁移期继续提供：

```json
{"name": "opencode", "status": "available", "cli_path": "..."}
```

同时增加 `id/display_name/source/transport/availability/capabilities/model_support/metadata`。
前端完成迁移后再废弃顶层 `status`。

## 12. agent-manifest.json

每个 Attempt 在 Agent 启动前创建：

```json
{
  "schema_version": "1",
  "attempt_id": "att_...",
  "agent": {
    "id": "deerflow",
    "display_name": "DeerFlow",
    "spec_hash": "sha256:...",
    "source": "builtin",
    "version": "..."
  },
  "model": {
    "requested": "provider/model",
    "effective": null,
    "provider": "provider"
  },
  "launch": {
    "plan_hash": "sha256:...",
    "argv_redacted": ["opencode", "run", "..."],
    "cwd": "skill_workspace",
    "env_names": ["OPENAI_API_KEY"]
  },
  "capabilities": {},
  "components": {
    "runtime": "local-cli@1",
    "driver": "oneshot@1",
    "parser": "opencode-jsonl@1",
    "mcp_dialect": "json-file@1"
  },
  "coverage": {},
  "status": "prepared"
}
```

finalize 时补 effective model、session refs、parse/cleanup status 和实际降级。写入使用原子
replace。manifest 列入 artifact 内部清单但不允许 Agent 修改后的文件覆盖框架副本；建议
存放在 Attempt 控制目录而非 `skill_workspace`。

plan hash 使用规范化 spec、逻辑 cwd、Prompt hash、MCP shape和非敏感 option；排除 secret、
绝对临时路径和随机 session ID。

## 13. 配置迁移

新配置：

```yaml
agents:
  profiles:
    my-agent:
      schema_version: "1"
      display_name: My Agent
      transport: local-cli
      implementation: {kind: profile-runtime}
      launch:
        executable: my-agent
        args:
          - run
          - {value: prompt_file}
      prompt: {mode: file}
      model: {binding: unsupported}
      mcp: {dialect: unsupported}
      output: {parser: text}
      capabilities:
        single_turn: verified
```

旧 `custom_agents` 通过 `LegacyCustomAgentTranslator` 转为 spec v1：

- `command` → tokenized launch；
- `prompt_mode` → prompt；
- `output_format/jsonl_fields` → parser；
- `mcp_config_flag` → legacy JSON-file dialect；
- 所有未声明能力为 unsupported/declared；
- API source 为 legacy 并返回 migration warning。

旧配置的行为先保持，安全修复（process group、明确 MCP 输入、无限 timeout）可以随 runtime
迁移统一生效，并用 compatibility tests 锁定可见差异。

## 14. ACP transport

ACP 阶段增加：

```text
ACP registry/cache
      ↓ resolve + pin
AcpTransport
      ↓ JSON-RPC/session protocol
ACP client session ──> permission/update/tool events
      ↓
standard ParseResult / AdapterResult
```

设计约束：

- registry entry 是分发 metadata，不是可信代码；校验 schema、平台、HTTPS、checksum；
- distribution 首期只允许管理员预安装的 binary/npx/uvx，不由普通 run 动态下载；
- Agent update 映射为标准 event；
- permission request 进入 interaction driver，未配置回答策略时暂停/失败，绝不自动 allow；
- session ID 与 Attempt 绑定；
- ACP server 崩溃走同一个 runtime cleanup/error taxonomy；
- 至少用两个 fake ACP servers 做 contract E2E，真实 registry Agent 为附加 smoke test。

## 15. 首批 Agent：DeerFlow

DeerFlow 是首批唯一必须交付的 Agent。它不是简单的一次性 CLI profile：Harbor 的实现表明，
其 stock headless CLI 不能表达所有 runtime feature，完整接入需要构造 `DeerFlowClient` 的
小型 runner、生成模型/sandbox 配置并解析 NDJSON StreamEvents。因此本设计把它实现为
**registry profile + DeerFlow plugin + 共享 LocalCliRuntime**，不把 DeerFlow 特例塞进通用
runtime。

### 15.1 固定版本与安装边界

- profile 声明经过验证的 DeerFlow revision/`deerflow-harness` version；
- availability 检查指定 Python/venv 中的 package version 和 runner 兼容性；
- 普通 run 不 clone 仓库、不安装 Python、不修改宿主机；
- 首期由部署者预装 pinned runtime，或使用后续独立 ADR 批准的隔离 runtime image/tool cache；
- manifest 记录 repo URL、revision、package version、runner/parser version。

### 15.2 模型配置

plugin 将 agent-arena requested model 转成唯一的 `arena-model`：

- OpenAI、Anthropic、Gemini/Google、DeepSeek 使用各自已验证的 LangChain integration；
- OpenRouter/其他兼容服务只有通过实测后才映射到 OpenAI-compatible client；
- API key 以 `$ENV_NAME` 引用写入 Attempt 私有 config，值只进入 child env；
- config 中 requested/effective model、integration class、base URL 的非敏感摘要进入 manifest；
- 未知 provider preflight 失败，不能无提示回退到 OpenAI。

### 15.3 workspace 与 sandbox bridge

DeerFlow 默认虚拟工作区与 scorer 检查的 `skill_workspace` 可能不同。plugin 必须：

1. 把 `skill_workspace` 解析为规范化真实路径；
2. 只允许该路径作为 DeerFlow local sandbox 的读写根/identity bridge；
3. Prompt 明确交付物写入该目录，但路径安全不能只靠 Prompt；
4. 禁止继承用户 DeerFlow 全局 project/home；
5. E2E 用 scorer 证明文件确实出现在 Attempt workspace，而不是 DeerFlow 私有目录。

若 DeerFlow 的 local sandbox 需要 `allow_host_bash`，该权限必须进入 security meta 和 manifest，
UI 明示其实际 execution locus；不能把“sandbox”标签误展示为独立容器隔离。

### 15.4 headless runner

runner 是小型、versioned bridge：

- 从 stdin 读取一轮任务；
- 显式构造 `DeerFlowClient`，传入 `subagent/thinking/plan_mode/recursion_limit`；
- 原样逐行输出有界 NDJSON StreamEvents；
- 用独立、最大 64 KiB、原子写入的 summary 报告 status、usage 和 provider error；
- 识别 DeerFlow 把 provider failure 转成 fallback message 的情况，并恢复为稳定非零错误；
- recursion limit 形成可解释终态，不吞掉已经生成的工作区产物；
- runner 不负责安装、registry、DB 或 scorer。

summary 和 NDJSON 都是不可信子进程输出，parser 必须做 schema、类型、非负 token、文件大小和
截断检查。summary 缺失或损坏只降级 usage/error coverage，不覆盖真实 exit code。

### 15.5 特性和能力

首期 options：

```text
subagent: false
thinking: true
plan_mode: false
summarize: false
recursion_limit: 1000
```

这些是建议默认值，不是未经验证的永久接口。每项均进入 LaunchPlan/manifest；run 可通过
受类型约束的 Agent options 覆盖。开启 summarization 时使用模型上下文比例触发等稳定配置，
不得把特定模型 token 窗口硬编码成通用默认值。

`subagent=true` 必须至少证明 DeerFlow 确实启用 delegation。只有 StreamEvents 暴露稳定
parent/child identity 时才声明 `subagent_identity=verified`；否则能力为执行支持、身份观测
unsupported/partial，不能从开关值推断观测能力。

### 15.6 MCP 决议门

Harbor 当前 DeerFlow adapter 明确没有传输独立 extension/MCP 文件，因此不能由该实现推定
DeerFlow 已支持 Lane MCP。首批任务必须对 pinned revision 做 spike：

- 若官方 extension/MCP contract 可在 Attempt 私有 project 中配置，则实现 DeerFlow MCP
  dialect，只消费 `AdapterRunInput.mcp_servers` 并通过 Wire rewrite；
- 若不可稳定接入，则 `capabilities.mcp=unsupported`，依赖 MCP 的环境在 preflight 阶段拒绝；
- 不允许退回到按 env 名称硬编码工具，也不允许把 DeerFlow 内建 web/bash 工具当成 Lane MCP。

### 15.7 多轮边界

首个里程碑要求可靠单轮。只有 pinned revision 提供可持久化、显式 thread/session ID，并通过
两个并行 Attempt 三轮隔离测试后，才能打开 `resume_send_message`。LangGraph 内部 state 或
DeerFlow 子任务不能自动等同于 agent-arena conversation resume。

## 16. 实施阶段

### Phase 0：Foundation

- AgentSpec/Registry/availability/manifest；
- LocalCliRuntime 与 fake CLI contract；
- legacy custom_agents 翻译；
- dispatch/API 改由 registry 驱动。

### Phase 1：DeerFlow 纵向闭环

- 固定 revision/API/config/MCP spike；
- DeerFlow profile + plugin + headless runner + parser；
- 模型、workspace/local sandbox、feature options 和错误恢复；
- API/UI capability/availability/manifest 展示；
- 单轮任务、产物、subagent 开关和进程清理 E2E。

### Phase 2：DeerFlow 深度观测

- DeerFlow StreamEvent/Wire normalizer；
- usage、thinking、tool、subagent coverage 对账；
- comparison coverage 与 effective config 展示。

### Phase 3：ACP

- ACP transport、cache、permission/session 映射；
- 两个 fake servers + 至少两个真实 registry smoke fixtures。

### Phase 4：SDK/远程

- remote disclosure/cancel contract；
- 通用 Python plugin 与远程 transport 模板；
- 评估是否引入隔离安装/runtime image。

## 17. 测试架构

新增 `tests/fake_agents/` 可执行 fixtures，模拟：

- text/jsonl 正常输出；
- stdout/stderr 大量并发输出；
- 部分 JSON 行和 schema 漂移；
- auth/model/rate-limit 错误；
- fork 子进程后 hang；
- 显式 session 首轮/续轮；
- MCP config 回显脱敏 shape；
- 收到 TERM 后退出或拒绝退出。

共享测试：

```text
tests/test_agent_spec.py
tests/test_agent_registry.py
tests/test_agent_availability.py
tests/test_agent_compatibility.py
tests/test_agent_local_cli_runtime.py
tests/test_agent_manifest.py
tests/test_agent_mcp_dialects.py
tests/test_agent_driver_contract.py
tests/test_agent_parser_contract.py
tests/test_agent_api.py
```

每个内置 Agent 另有 profile snapshot、launch plan golden、parser fixture 和 capability
expectation。真实 API 测试不得进入默认 CI。

## 18. 兼容性和发布

1. Phase 0 不改变现有 Agent ID、POST body 和 DB 中的 `agent_name`；
2. Claude/Codex first pass 只注册现有 builder，保持现有 adapter 代码路径；
3. `/api/agents` 先增字段，旧字段保留至少一个发布周期；
4. legacy `custom_agents` 至少一个发布周期可用；
5. registry/runtime 默认关闭新内置 profiles 的选择，直到对应 Agent contract 通过；
6. 每批 Agent 用 feature flag 或 `status: experimental` 开放；
7. 回滚 registry 时历史 Attempt 仍可通过 manifest/spec snapshot 展示，不能要求当前 profile
   仍存在。

## 19. 未决问题

1. 内置 profile 使用 YAML 还是 Python 常量生成：首选 YAML，若类型/复用明显恶化再调整；
2. **已决议**：版本比较由 `AvailabilityService` 统一路由。PEP 440 使用
   `packaging.version.Version`/`SpecifierSet`；semver 首期同样规范化为可比较版本，并只转换
   明确支持的 npm `^x.y.z`/`~x.y.z` 范围；非标准输出必须由 profile 提供提取 regex 和完整
   匹配约束。不可提取时为 `unknown`，可提取但不满足约束时为 `version_unsupported`，非法
   profile regex 在 AgentSpec 校验阶段拒绝；不猜测厂商版本语义；
3. 是否把 agent-manifest 摘要落 DB：首期文件为权威，API 可按需读取；规模数据证明需要后
   再加索引列；
4. ACP distribution 是否允许显式自动安装：不阻塞 transport，首期仅预安装；
5. DeerFlow 的显式 session/resume 是否在 pinned revision 稳定：以并行 Attempt fixture
   决定 capability；
6. DeerFlow local sandbox、Lane MCP extension 和 subagent identity 的真实边界需要 spike；
7. local CLI 私有 HOME 与系统 PATH/证书配置的最小继承集合，需要用 DeerFlow 实测收敛。

## 20. Phase 0 调研决议

### 20.1 Harbor 边界

Harbor 对比固定在 `harbor-framework/harbor` revision
`1393655243125f1d63f81f9bd2f217eefaba3633`。可复用的是 descriptor/factory/lifecycle/evidence
分层；其在任务环境中以 root/agent user 动态安装、`BaseEnvironment.exec()`、容器路径和日志
同步假设不进入 Agent Arena host runtime。完整映射和 Apache-2.0 处理规则见
[`harbor-spike.md`](harbor-spike.md)。Harbor 不作为构建或运行依赖。

### 20.2 版本语义

`version_scheme=pep440` 使用 PEP 440；`version_scheme=semver` 支持普通比较符以及明确转换的
caret/tilde 三段范围；`version_scheme=regex` 由 profile 同时声明提取 regex 和完整匹配约束。
版本命令限制时间和输出扫描范围，不联网、不安装。解析失败不得降级为“可用”，而是
`unknown`；约束不满足为 `version_unsupported`。对应 contract 覆盖 Python `1.4.2`、npm
`^2.0.0` 和非标准 `channel-42`。
