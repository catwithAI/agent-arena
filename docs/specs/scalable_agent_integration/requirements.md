# 规模化 Agent 接入（Scalable Agent Integration）

## 背景

agent-arena 当前内置 Claude Code、Codex 和可选的 SSH Claude Code，并允许通过
`CustomCliAdapter` 配置任意一次性 CLI Agent。这个扩展点已经证明“可以运行更多
Agent”，但还不足以支撑几十种 Agent 的长期维护：

- Agent 的命令、模型参数、认证、MCP 配置和输出事件各不相同；
- 配置型 Agent 目前缺少会话恢复、精确能力声明和原生轨迹归一化；
- Agent 列表只返回可执行文件是否存在，无法说明版本、模型兼容性和观测覆盖；
- 新增完整适配器仍需修改集中式 dispatch 分支；
- 宿主机执行模型与 Harbor 的“在任务容器内安装并运行 Agent”不同，不能直接复制其
  安装脚本和路径约定；
- 如果每个 Agent 各自实现 subprocess、超时、日志、MCP 和 secret 处理，会快速产生
  行为漂移，破坏公平比较。

Harbor 已注册约三十余种 Agent。其可借鉴之处是统一生命周期、注册表、声明式 CLI
参数、能力标记和 Agent 专属轨迹解析，而不是具体的容器内安装命令。本 spec 将这些思想
映射到 agent-arena 的 Attempt 工作区、宿主机 CLI、Lane MCP、Wire 证据和多轮模型。

## 核心问题

**agent-arena 能否在不牺牲隔离、公平性、可观测性和失败可解释性的前提下，把新增一个
常规 CLI Agent 的成本降到“一份声明式 profile + fixtures”，并用少量协议/原生适配器
覆盖需要多轮或特殊运行时的 Agent？**

## 目标

1. 支持几十种 Agent，而不在 `run_dispatch.py` 累积同等数量的条件分支；
2. 常规 CLI Agent 不写 Python 执行循环，只声明差异；
3. 复杂 Agent 可以复用同一个运行时，并插入专属 session driver、parser 或 transport；
4. API/UI 在启动前就能展示真实可用性、版本、能力和缺口；
5. 所有 Agent 遵守相同的 Attempt 隔离、时间预算、MCP 声明和 secret 边界；
6. 新接入不自动获得“完整轨迹”“多轮”“同模型可比”等未经验证的能力标签；
7. **首个产品化接入必须是 DeerFlow**；通用基础设施以 DeerFlow 的完整纵向闭环作为
   验收对象，其他 Agent 不进入首批发布门槛。

## 非目标

- 不承诺所有 Agent 支持任意模型或同一个模型服务；
- 不在首期复刻 Harbor 的容器编排和容器内动态安装系统；
- 不在 agent-arena 启动时修改宿主机全局 Node/Python/Rust 环境；
- 不把所有 Agent 输出强行伪装成 Claude/Codex 同等精度的 thinking、token 或子 Agent；
- 不维护第三方 Agent 的账号、订阅、服务端配额或许可证；
- 不要求一次性接入 Harbor 的全部 Agent；按可验证批次逐步开放。

## 术语

- **Agent profile**：描述 Agent 身份、启动方式、模型/MCP 方言、认证引用和能力的声明。
- **Agent registry**：合并内置 profile、用户 profile 和插件实现后的权威目录。
- **runtime**：统一负责子进程生命周期、隔离、日志、超时和结果收尾的执行器。
- **driver**：把统一 conversation 转成某 Agent 的首轮/续轮调用序列。
- **parser**：把原始 stdout、stderr 或 session 文件转成 adapter event/evidence 的解析器。
- **transport**：`local-cli`、`ssh-cli`、`acp`、`sdk` 等实际通信/执行方式。
- **verified capability**：有 fixture、contract test 或明确实测依据的能力。
- **availability**：在当前部署上是否具备运行前置条件，不等于账号一定有调用额度。

## 需求

### R1 AgentSpec 与注册表

系统必须建立 versioned `AgentSpec` 和统一 `AgentRegistry`。

1. 每个 Agent 至少声明：
   - 稳定 `id`、展示名、spec version；
   - transport 与实现类型；
   - executable/入口和版本探测；
   - 默认模型与模型参数方言；
   - Prompt、MCP、认证、输出解析和能力；
   - execution locus、网络需求、系统依赖；
2. registry 支持三类来源：
   - 仓库内置 profile；
   - `arena.yaml` 本地 profile；
   - Python import path/plugin，用于复杂实现；
3. 同名冲突必须确定性失败；覆盖内置 profile 必须使用显式 `override: true`，并在 API 中
   标出来源，不能依赖加载顺序静默覆盖；
4. profile schema 未知字段默认拒绝，schema 升级必须保留向后兼容读取策略；
5. registry 使用懒加载，列出 Agent 不得 import 所有可选 SDK；
6. `known_agents()`、`build_adapter()` 和 `/api/agents` 必须从同一 registry 读取，禁止维护
   三份 Agent 名单；
7. Agent ID 必须满足稳定 slug 规则并作为 DB/API 标识；展示名可变化。

### R2 分层实现模型

接入必须分成三层，避免“一个 Agent 一个完整 subprocess 实现”。

| 层级 | 适用范围 | 实现 |
|---|---|---|
| Profile | 一次性或规则化 CLI | 声明 command/prompt/model/MCP/parser |
| Driver/Parser plugin | CLI 特殊、多轮、结构化事件 | 复用 runtime，只替换差异点 |
| Native transport | ACP、SDK、远程服务、特殊沙盒 | 实现统一 adapter contract |

要求：

1. 新 Agent 默认先评估 Profile 层；只有 profile 无法表达的行为才新增 Python；
2. driver、parser、transport 分离；解析格式变化不要求重写启动逻辑；
3. 原生实现通过 import path 注册，不修改核心 dispatch；
4. 内置 Claude/Codex 可以渐进迁移到 registry，但迁移不得降低其多轮、Wire、thinking、
   token 或子 Agent 观测能力；
5. 现有 `custom_agents` 配置继续可读，迁移期映射为 legacy profile，并给出弃用提示而非
   立即失效。

### R3 统一 CLI runtime

所有 local CLI profile 必须由同一个 runtime 执行。

1. runtime 统一负责：
   - Attempt 工作目录和私有配置目录；
   - argv/env 构造与 secret 边界；
   - stdout/stderr 增量落盘；
   - timeout、取消、异常和进程树清理；
   - exit code/错误分类；
   - `AdapterResult` 与 security meta；
2. 子进程必须创建独立 process group/session；timeout 和取消必须清理 Agent 及其 MCP
   子进程，不能留下孤儿；
3. runtime 必须持续 drain stdout/stderr，避免 pipe 背压死锁；单行和总日志大小必须有
   明确上限与截断标记；
4. `timeout_seconds=None` 表示无限任务预算，不能错误传给 `asyncio.wait_for`；仍可使用明确
   声明的无输出 watchdog；
5. 非零退出、认证失败、模型不存在、限流、超时和解析降级必须使用稳定错误码；
6. profile 不得直接拼接 shell 字符串；默认使用 argv。确需 shell 的实现必须声明
   `shell: true`、说明原因并通过注入测试；
7. runtime 输出原始证据后再解析；parser 失败不能丢失已经产生的 stdout/stderr/events。

### R4 Prompt 与 conversation driver

1. profile 必须声明 Prompt 传输方式：`stdin`、`file`、`arg` 或 driver-owned；
2. Prompt 统一由共享 renderer 生成，包含任务、公开上下文、上传文件名和一次性的时间预算
   提示；profile 不得私自改变实验语义；
3. `arg` 模式必须有平台参数长度检查，过长时失败或按 profile 声明安全降级到文件/stdin；
4. 多轮 Agent 必须保存并使用当前 Attempt 的显式 session/thread ID；禁止使用
   “最近会话”“continue last”等可能串 Attempt 的恢复方式；
5. capabilities 必须区分：
   - `single_turn`；
   - `resume_send_message`；
   - `answer_interaction`；
6. conversation 中存在不支持的 action 时必须在启动前失败，不得只执行第一轮后假装完成；
7. driver 必须为每轮保留 turn ID、开始/结束时间、命令引用和 session 引用。

### R5 模型与 Provider 兼容性

1. profile 声明模型绑定方式：flag、环境变量、配置文件、Agent 默认值或不支持覆盖；
2. profile 声明接受的 provider/protocol 类型，至少可表达：Anthropic、OpenAI Chat、
   OpenAI Responses、Agent 自有云和任意模型代理；
3. 同模型模式创建 attempt 前必须验证每个 Agent 是否能消费请求的模型/provider 组合；
4. `model=None`、Agent 默认模型和显式模型必须在结果中区分；
5. attempt 必须记录 requested model、effective model、provider、Agent version 和相关非敏感
   参数；无法确认 effective model 时记 unknown，不能照抄 requested value；
6. 模型参数如 reasoning effort、thinking tokens 不强求跨 Agent 同名，但必须规范化记录原值，
   供公平性报告展示；
7. provider secret 只能通过 secret reference/环境变量注入，禁止进入 argv、profile API、事件
   和持久化配置快照。

### R6 MCP 与工具能力

1. Agent 只能收到环境 `meta.yaml` 显式声明的 MCP servers；profile 不得从 env 名称猜测或
   自动生成不存在的 server；
2. MCP 方言必须插件化，至少覆盖：
   - JSON `mcpServers` 配置文件；
   - CLI add/register 命令；
   - Agent 专属配置文件；
   - unsupported；
3. MCP 配置中的 command、args、cwd 和 env 必须从 `AdapterRunInput.mcp_servers` 转换，
   不能硬编码仓库路径；
4. session token 只能写入 Attempt 私有、权限受限的临时配置或子进程 env；结果和日志必须
   脱敏；
5. Agent 不支持 MCP 时，只有不依赖 MCP 的环境可运行；依赖 MCP 的任务必须启动前失败并
   给出 capability mismatch；
6. MCP config/注册动作必须属于 Attempt 生命周期，不能污染用户全局 Agent 配置。

### R7 输出、轨迹和 Wire 归一化

1. parser 支持能力分级：
   - `text-only`：只保证最终文本和原始日志；
   - `jsonl-events`：结构化消息、推理和工具事件；
   - `native-session`：从 Agent session 文件离线重建；
   - `atif` 或其他标准轨迹；
2. 每个 parser 必须有 version、输入 schema/CLI 版本依据和离线 fixture；
3. parser 输出统一 adapter event/evidence，前端不得直接理解第三方 Agent 私有 schema；
4. token、cost、thinking、tool call、sub-agent identity 只有在真实可观测时才声明；未知为
   null/unsupported，禁止用 0 或空数组冒充完整观测；
5. parser 失败默认不改变 Agent 的任务完成状态，但 manifest/结果必须标记观测降级；
6. 新 Agent 若要参与 Wire 调用级比较，必须实现对应 normalizer 或声明 wire
   `not-applicable/partial`；
7. 原始输出和 session 文件必须允许离线重建，升级 parser 不要求重跑昂贵 Attempt。

### R8 能力声明与可用性探测

`GET /api/agents` 必须返回结构化目录，而不只是 `which()` 结果。

至少包含：

```json
{
  "id": "deerflow",
  "display_name": "DeerFlow",
  "source": "builtin",
  "transport": "local-cli",
  "availability": {"status": "available", "version": "...", "reason": null},
  "capabilities": {
    "single_turn": true,
    "resume_send_message": true,
    "answer_interaction": false,
    "mcp": true,
    "structured_events": true,
    "token_usage": "verified",
    "subagent_identity": false,
    "wire": "partial"
  }
}
```

要求：

1. availability 状态至少为 `available`、`not_installed`、`version_unsupported`、
   `missing_auth`、`missing_dependency`、`misconfigured`、`unknown`；
2. 探测必须只读、快速、有超时并可缓存；不得通过探测发起付费模型调用；
3. 认证只检查引用的环境变量是否存在，不回显 secret；
4. 能力项带 `verified`、`declared` 或 `unsupported` 依据；UI 必须区分；
5. 创建 run 时服务端重新执行关键兼容性校验，不能只信任浏览器之前缓存的状态；
6. Agent 未安装仍可出现在目录中，UI 显示安装说明，但不可选中运行。

### R9 安装、版本和供应链边界

1. 首期不自动安装或升级宿主机 Agent；registry 只提供官方安装文档、版本约束和探测命令；
2. 不得在服务启动、列目录或创建普通 run 时执行 `npm install -g`、`pip install`、curl pipe
   shell 等宿主机变更；
3. 后续若支持自动安装，只能作为显式管理动作，使用隔离的 tool cache/容器、固定版本和
   checksum/lock，且必须有审计记录；
4. attempt 记录实际 CLI 版本；未满足 profile 支持范围时默认拒绝，允许显式实验性覆盖并
   标记结果；
5. 第三方 Agent 许可证、安装来源和主页属于 registry metadata；不能因 Agent 名称相同就
   假定二进制来源可信；
6. 从 Harbor 移植代码时遵守 Apache-2.0 NOTICE/归属要求，并重写依赖其环境抽象的部分。

### R10 隔离与安全

1. 每个 Attempt 使用独立工作区、配置目录、session 目录和临时 MCP 配置；
2. profile 必须声明会读取哪些默认用户目录；内置 profile 默认阻止读取用户的 Agent 全局
   配置，除非用户显式选择 `inherit_user_config`；
3. profile 中的 env 只允许静态非敏感值或 secret reference；API 和日志返回脱敏快照；
4. argv、env、生成配置和错误信息在落盘前经过统一 redaction；
5. execution locus、permission mode 和 workspace root 进入现有 security meta；
6. Agent 申请的权限模式必须可见。不能为了接入方便在 profile 中静默开启更宽权限；
7. local CLI 的网络需求只是声明和展示，首期不伪装成已实施网络隔离；
8. Agent 产生的路径引用必须经过 Attempt 路径边界检查后才能作为 artifact/API 暴露。

### R11 公平性与可复现性

1. 所有 Agent 获得相同的任务正文、公开上下文、材料、总时间预算和环境声明；
2. 保留 Agent 原生搜索、技能、子 Agent 和任务拆解能力，但必须记录是否启用；
3. 用户私有全局技能、历史会话和配置默认隔离，避免某 Agent 获得未声明先验；
4. attempt 生成不可变 `agent-manifest.json`，至少记录：
   - spec/profile ID 与 hash；
   - Agent/CLI version；
   - requested/effective model；
   - 非敏感 argv/env/config 摘要；
   - capabilities 与实际降级；
   - session/transport/parser/driver version；
5. 同一 profile、输入和版本应生成确定性的启动计划；动态路径和 secret 不进入 plan hash；
6. “支持某模型”“支持 token”“支持 resume”等产品声明必须由 contract test 或实测 fixture
   支撑；仅配置作者声明的能力在 UI 中标 `declared`；
7. Agent 能运行但观测较少时允许评分，比较页必须显示 coverage 差异，不能把不可观测当成
   行为不存在。

### R12 ACP、SDK 和远程 Agent

1. ACP 使用一个协议级 transport/driver 接入，不为 registry 中每个 ACP Agent复制 runtime；
2. ACP registry 条目必须 pin 版本或内容 hash并缓存原始 metadata；远程 registry 不可用时，
   已缓存版本仍可运行；
3. ACP permission request 必须映射到统一 interaction/capability 模型，不得默认自动批准；
4. 后续 SDK/远程 Agent 使用 native transport，仍输出同一个 `AdapterResult`、agent
   manifest 和 capability block；
5. 远程 Agent 必须声明代码/材料会离开本机、数据驻留和取消语义，UI 在运行前展示；
6. 远程取消若无法保证停止服务端执行，结果必须标记 `cancel_requested_remote_unknown`；
7. transport 扩展不得绕过 Attempt token、artifact 路径和结果终态规则。

### R13 测试与准入门槛

每个内置 Agent 必须通过同一套 contract tests：

1. profile schema、registry 冲突与 deterministic launch plan；
2. 缺 CLI、缺 auth、版本不符、非零退出、timeout、取消和坏输出；
3. Prompt 三种传输方式中的适用项；
4. MCP 零/一/多个 server，且不泄露 token；
5. capability gate：不支持多轮/交互/MCP 时启动前拒绝；
6. process tree cleanup；
7. parser golden fixtures、截断行和 schema 漂移；
8. agent manifest 不含 secret，effective config 可复现；
9. 至少一个无付费 API 的 fake CLI E2E；真实账号 smoke test 只作为附加验收；
10. 新 Agent 在 UI 中出现、不可用原因可读、可用时能创建 Attempt。

进入默认内置目录前，Agent 必须满足：

- 官方或可审计的安装来源；
- 稳定非交互入口；
- Attempt 工作目录可控；
- 至少完成单轮任务和终态收尾；
- capability/coverage 不夸大；
- 有维护者、fixture 和支持版本范围。

## 首批范围

首批只承诺 **DeerFlow**，且不是“名单里出现”即算完成，而是以下纵向闭环全部成立：

1. registry/API/UI 中有稳定 `deerflow` descriptor、版本和可用性诊断；
2. 使用 Attempt 私有 DeerFlow project/home/config，不读取或污染用户全局状态；
3. 根据请求模型生成 DeerFlow model 配置，准确处理 provider、base URL 和 secret ref；
4. DeerFlow 能在真实 `skill_workspace` 工作，产物由现有 scorer 正常发现；
5. headless runner 输出原始 NDJSON，并有有界 summary/错误恢复通道；
6. `thinking`、`plan_mode`、`subagent`、`summarize`、`recursion_limit` 等 DeerFlow 特性
   显式配置、写入 manifest，不靠不可见默认值；
7. 子 Agent 开关至少有 fixture 验证；能否观测子 Agent identity 由真实事件决定；
8. 明确 Lane MCP 的接入结论：支持则只转换声明的 `task.mcp_servers`，不支持则对依赖 MCP
   的任务启动前拒绝，不能伪装支持；
9. timeout/cancel 能清理 runner、DeerFlow 和其子进程；
10. 使用固定 DeerFlow revision/版本和离线 fixtures 完成可重复测试。

DeerFlow 之外的具体 Agent 名单不属于首批范围，也不阻塞本 spec 的首个里程碑。后续新增
Agent 继续使用 R1-R13 的通用准入契约，但另行排期。本 spec 不把 Harbor 当前 DeerFlow
命令、内部 API 或配置格式视为永久事实；实施时必须以固定 revision 的源码、官方文档和
真实运行 fixture 为准。

## 成功指标

1. 新增一个规则化 CLI Agent 不修改 dispatch/runtime 核心代码；
2. DeerFlow 通过专属 plugin/driver/runner 接入，通用 runtime 不出现 DeerFlow 名称分支；
3. 目录可展示已注册但未安装的 Agent 及精确原因；
4. 所有内置 Agent 通过共享 contract suite；
5. timeout/cancel 测试没有遗留 Agent/MCP 子进程；
6. capability mismatch 全部在启动前失败；
7. 同一 Attempt 的 secret 不出现在 argv、API、manifest 和测试日志；
8. DeerFlow 的模型、工作区、特性开关、NDJSON、usage/error summary 和清理均有 fixture；
9. Claude/Codex 现有功能和测试无回退。

## 依赖与风险

| 风险 | 影响 | 处理 |
|---|---|---|
| 第三方 CLI 频繁变更事件 schema | parser 失效 | 原始证据、parser version、fixture、版本范围 |
| Agent 不支持任意 provider/model | 同模型比较不可用 | compatibility preflight，不强行改写 |
| 宿主机全局配置污染 | 公平性和凭证风险 | Attempt 私有 HOME/config，默认不继承 |
| CLI 启动 MCP 子进程后被 timeout | 孤儿进程 | process group 清理 contract test |
| profile 过度复杂成为新编程语言 | 维护成本 | 只表达稳定差异，复杂逻辑升级为 plugin |
| 大量“能跑但不可观测”的接入 | 比较结论误导 | coverage/capability 显示，分批准入 |
| DeerFlow API/config schema 随 revision 变化 | runner 或配置失效 | pin revision、runner/parser version、离线 fixtures |
| DeerFlow 自带 sandbox 与 Attempt workspace 不一致 | 产物写错位置 | 显式 local sandbox/workspace bridge + scorer E2E |
| 远程 Agent 上传私有材料 | 数据泄露 | transport disclosure + 显式配置 |
| 自动安装污染供应链 | 主机风险、不可复现 | 首期禁止隐式安装，后续隔离/pin/checksum |
