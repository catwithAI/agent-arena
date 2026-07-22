# 规模化 Agent 接入——实施任务

> 需求：[requirements.md](requirements.md)（R1-R13）
>
> 设计：[design.md](design.md)（§3-§18）
>
> 状态：Completed（A0–A8 与发布门槛均已完成）

拆分原则：先统一运行时和准入契约，再增加 Agent 数量；每项能力必须有 fixture/contract
支撑；真实付费账号 smoke test 不能替代本地可重复测试。

难度：★ 低 / ★★ 中 / ★★★ 高。满足依赖的任务可并行。

## 任务总览

| 组 | 内容 | 价值节点 |
|---|---|---|
| A0 | DeerFlow spike 与契约冻结 | 不把 Harbor 实现细节当作永久事实 |
| A1 | AgentSpec + Registry | Agent 目录、构建、API 单一真相源 |
| A2 | Local CLI Runtime | 所有 CLI 共享安全可靠生命周期 |
| A3 | Driver/MCP/Parser | profile 能表达常见差异 |
| A4 | Dispatch/API/UI 迁移 | 新目录真正可选择运行 |
| A5 | DeerFlow 纵向闭环 | 首批唯一产品化 Agent |
| A6 | DeerFlow 深度观测 | StreamEvent/Wire/子 Agent 覆盖 |
| A7 | ACP | 一个 transport 扩大生态覆盖 |
| A8 | SDK/远程 | 复杂运行时扩展 |

关键依赖：`A0 → A1 → A2/A3 → A4 → A5`；A2 与 A3 部分可并行；A6 依赖 A4，A7
依赖 A1/A2/A3，A8 依赖 A1/A4。

---

## A0 · Spike 与规范冻结

- [x] **A0-1 Harbor 差异映射**　★
  - 记录 Harbor `BaseAgent`、`BaseInstalledAgent`、factory、ACP registry 中可复用的设计；
  - 明确容器内安装/路径/环境抽象不可直接复制的部分；
  - 列出移植代码的 Apache-2.0 attribution/NOTICE 要求。
  - _验收：本 spec 增补一份带具体版本/commit 的调研附录；不得把外部仓库作为运行依赖。_

- [x] **A0-2 DeerFlow 接入探测矩阵**　★★★
  - 对 pinned DeerFlow revision 收集官方安装来源、package version、stock CLI 与
    `DeerFlowClient` API、Prompt、模型、extension/MCP、session、StreamEvents、local sandbox、
    subagent、summarization、权限和退出语义；
  - 每项结论注明 CLI version、日期和证据；secret/账号输出先脱敏；
  - 不能验证的能力标 unknown/unsupported，不从 Harbor 实现推定。
  - _验收：`deerflow-spike.md` + 脱敏源码/API/运行 fixtures；形成 runner/config/MCP/session
    四项 go/no-go，结论回写 design §15。_

- [x] **A0-3 版本语义 spike**　★
  - 验证 npm/Python/自定义版本输出如何统一检查；
  - 决定 version constraint 内部模型和不可解析时的行为。
  - _验收：至少覆盖 semver、PEP 440 和非标准版本各一例；结论回写 design §19.2。_

## A1 · AgentSpec 与 Registry

- [x] **A1-1 AgentSpec v1 models/schema**　★★
  - 实现 identity、implementation、launch、prompt、model、auth、MCP、output、capabilities、
    isolation 和 metadata 模型；
  - 严格未知字段、slug、模板变量和 secret-in-argv 校验；
  - 导出/提交 JSON Schema。
  - _验收：`tests/test_agent_spec.py` 覆盖 round-trip、未知字段、非法变量、secret argv、
    capability 简写规范化和 v1 兼容读取。_

- [x] **A1-2 Registry loader 与冲突规则**　★★
  - 内置/profile/plugin/legacy 四类来源；
  - lazy import；重复 ID 失败；显式 override；spec hash；
  - plugin import 失败只影响该 descriptor，不拖垮目录。
  - _Depends on：A1-1。验收：`tests/test_agent_registry.py` 覆盖确定性顺序、冲突、override、
    lazy import 和坏 plugin。_

- [x] **A1-3 Claude/Codex/SSH builder 注册**　★
  - 用 `existing-adapter` bridge 注册三个现有实现；
  - registry facade 返回与当前 `known_agents/build_adapter` 相同结果；
  - 不迁移内部 adapter 逻辑。
  - _Depends on：A1-2。验收：现有 adapter、API、compare mode 测试零回退。_

- [x] **A1-4 legacy custom_agents translator**　★★
  - 把现有配置映射成 spec v1；
  - 保留 command/prompt/output/env/MCP 可见行为；
  - 添加 source=legacy 和 migration warning。
  - _Depends on：A1-1。验收：现有 custom CLI tests + translation golden；同一配置生成稳定
    spec hash。_

- [x] **A1-5 Availability service**　★★
  - which/version/dependency/auth-ref 探测；超时、缓存、redaction；
  - 实现全部 availability 状态；
  - 探测不发模型请求、不安装软件。
  - _Depends on：A1-1。验收：`tests/test_agent_availability.py` 用 fake executables 覆盖慢
    version、坏版本、缺 auth、缓存失效和 secret 不回显。_

## A2 · Local CLI Runtime

- [x] **A2-1 LaunchPlan renderer**　★★
  - tokenized argv、cwd、env names、Prompt/MCP/session slots；
  - secret 延迟解析；redacted plan 与 deterministic hash；
  - 参数长度检查和 Prompt fallback。
  - _Depends on：A1-1。验收：`tests/test_agent_launch_plan.py` 覆盖条件 omission、路径差异不
    改逻辑 hash、secret 排除和 shell injection 字符。_

- [x] **A2-2 subprocess 双流与原始 evidence**　★★★
  - process group；stdout/stderr 并发 drain；行/总量限制；
  - 原始 evidence frame；正常/nonzero/坏 UTF-8 收尾；
  - parser 不阻塞 pipe。
  - _Depends on：A2-1。验收：fake CLI 同时填满 stdout/stderr 不死锁；超长行/坏 UTF-8 有
    明确截断/替换诊断。_

- [x] **A2-3 timeout/cancel/process tree cleanup**　★★★
  - TERM→grace→KILL；await drain；cleanup event；
  - `timeout=None` 无限预算路径；
  - fork child/MCP child 不残留。
  - _Depends on：A2-2。验收：`tests/test_agent_local_cli_runtime.py` 用 PID 探针确认 timeout、
    API cancel、异常三条路径进程树消失。_

- [x] **A2-4 错误 taxonomy 与 patterns**　★★
  - 稳定错误码；共享分类优先级；profile failure regex；
  - redacted diagnostics；parse degraded 与任务失败分离。
  - _Depends on：A2-2。验收：auth/model/rate limit/network/nonzero fixtures，无 pattern 时稳定
    fallback。_

- [x] **A2-5 agent manifest**　★★
  - prepared/final 原子写；spec/plan/component versions；
  - requested/effective model、coverage、cleanup；
  - secret/path sanitization 和历史读取。
  - _Depends on：A1-1、A2-1。验收：`tests/test_agent_manifest.py` 扫描已知 secret 不出现；
    prepared 后崩溃仍可诊断；finalize 幂等。_

## A3 · Driver、MCP 与 Parser

- [x] **A3-1 shared Prompt renderer + OneShotDriver**　★★
  - 复用现有 prompt context/time notice；
  - 单轮 capability gate；turn evidence；
  - stdin/file/arg 三种 transport。
  - _验收：同一 task 对不同 profile 的语义正文/hash一致；多轮输入对 oneshot 启动前拒绝。_

- [x] **A3-2 CommandResumeDriver**　★★★
  - 首轮/续轮计划；显式 session ID；turn refs；
  - session 获取失败/多个候选稳定失败；禁止 latest session。
  - _Depends on：A2-1、A3-1。验收：fake CLI 三轮 fixture 不跨两个并行 Attempt 串 session。_

- [x] **A3-3 MCP IR + JsonFileDialect**　★★
  - 只从 `AdapterRunInput.mcp_servers` 构建；多 server；cwd/args/env；
  - Wire rewrite 顺序；owner-only 私有配置；manifest redacted shape。
  - _验收：`tests/test_agent_mcp_dialects.py` 覆盖零/一/多 server、特殊字符、token 不落日志、
    未声明 MCP 不生成配置。_

- [x] **A3-4 CommandRegister/NativeConfig dialect SPI**　★★
  - 私有 config root 中注册；可回滚/清理；
  - dialect capability 与 Wire rewrite consumption；
  - 注册失败不启动 Agent。
  - _Depends on：A3-3。验收：fake dialect 证明不读取/修改用户 HOME。_

- [x] **A3-5 Text/JSONL parser contract**　★★
  - parser version、final text、events/thinking/usage/session、diagnostics；
  - truncated JSONL/schema drift；原始证据可离线重跑；
  - 未观测字段保持 null/unsupported。
  - _验收：`tests/test_agent_parser_contract.py` golden fixtures；parser crash 不改变成功 exit 的
    任务终态但 coverage=degraded。_

- [x] **A3-6 Wire normalizer registry**　★★
  - normalizer factory、HTTP/MCP injection capability 从 spec 驱动；
  - 移除新增 Agent 必须编辑硬编码集合的要求；
  - Claude/Codex 输出保持一致。
  - _Depends on：A1-2。验收：fake Agent 动态注册 normalizer；无 normalizer manifest 明确
    not-applicable；完整 Wire 回归。_

- [x] **A3-7 Compatibility preflight**　★★
  - 平台/version/auth/model/provider/MCP/conversation/Wire 检查；
  - run 整单原子拒绝；dispatch 二次检查；结构化 diagnostics。
  - _Depends on：A1-5、A3-1、A3-3。验收：`tests/test_agent_compatibility.py` 覆盖所有 mismatch，
    compare group 不产生半组 attempts。_

## A4 · Dispatch、API 和 UI 迁移

- [x] **A4-1 app/runtime registry 接线**　★★★
  - lifespan 构造 registry；dispatch 通过 resolved builder；
  - 保留兼容 facade；active task/cancel/recovery 不变；
  - legacy 和 existing adapters 可混合比较。
  - _Depends on：A1-2~4、A2、A3-7。验收：完整后端测试；一个 run 同时运行 existing
    adapter + fake profile runtime。_

- [x] **A4-2 `/api/agents` descriptor v2**　★★
  - 新增 source/transport/availability/version/capabilities/model support/metadata；
  - 保留 `name/status/cli_path` 兼容字段；
  - 不可用 Agent 也返回。
  - _Depends on：A1-5、A4-1。验收：API schema snapshot、缺 auth 不泄露 env value、坏 plugin
    不拖垮其他 Agent。_

- [x] **A4-3 前端 Agent 目录与能力提示**　★★
  - 展示未安装/缺 auth/版本错误；不可用禁选；
  - 展示 single/multi-turn、MCP、结构化事件、token、Wire coverage；
  - declared 与 verified 视觉区分；安装链接使用 registry metadata。
  - _Depends on：A4-2。验收：前端测试覆盖全部 availability；键盘可访问；构建通过。_

- [x] **A4-4 提交页 compatibility feedback**　★★
  - model/provider/MCP/conversation mismatch 在提交前/提交响应中可读；
  - same-model 不兼容时指出具体 Agent；
  - 服务端仍为权威。
  - _Depends on：A3-7、A4-3。验收：固定 API fixture 覆盖整单拒绝，无残留 attempt。_

- [x] **A4-5 Attempt agent manifest/coverage 展示**　★★
  - 详情展示 Agent version、requested/effective model、profile source、能力降级；
  - 不展示 argv 中敏感值；历史 manifest 缺失有降级态。
  - _Depends on：A2-5、A4-2。验收：前端 fixture + API path traversal/secret 测试。_

## A5 · DeerFlow 纵向闭环

- [x] **A5-1 DeerFlow profile + availability**　★★
  - 注册稳定 `deerflow` ID、官方来源、pinned revision/package version、Python/runtime 依赖；
  - 探测预安装 harness 和 runner compatibility，不在普通 run 安装；
  - 缺 package、坏版本、缺模型 key 分别给出诊断。
  - _Depends on：A0-2、A1。验收：profile snapshot + availability fixtures；目录/API/UI 可见。_

- [x] **A5-2 DeerFlow plugin 与私有配置**　★★★
  - 生成 Attempt 私有 project/home/config；
  - requested model 映射为 `arena-model`，provider integration/base URL/secret ref 严格校验；
  - `subagent/thinking/plan_mode/summarize/recursion_limit` 类型化并进入 manifest；
  - 不读取或修改用户全局 DeerFlow 配置。
  - _Depends on：A2-1、A2-5、A5-1。验收：config golden 覆盖各已支持 provider、未知
    provider 拒绝、secret value 不落盘。_

- [x] **A5-3 workspace/local sandbox bridge**　★★★
  - 把真实 `skill_workspace` 安全映射为 DeerFlow 工作根；
  - 显式记录 `allow_host_bash`/execution locus/permission mode；
  - Prompt steering 只是辅助，路径边界由配置和框架验证；
  - 不允许 `/mnt/user-data` 等 DeerFlow 私有目录承载最终交付物。
  - _Depends on：A5-2。验收：fake scorer E2E 只在 skill workspace 找到 DeerFlow 产物；
    `..`、symlink 和错误 workdir 被拒绝。_

- [x] **A5-4 versioned headless runner**　★★★
  - stdin 读取任务，显式构造 `DeerFlowClient`；
  - 输出逐行 NDJSON StreamEvents；特性开关和 recursion limit；
  - provider fallback error 恢复为非零错误；recursion limit 可解释收尾；
  - 最大 64 KiB summary 原子写入 status/usage/error。
  - _Depends on：A0-2、A2。验收：无需真实 API 的 fake DeerFlowClient 覆盖 completed、provider
    error、recursion、坏 event、summary 原子性和大小上限。_

- [x] **A5-5 DeerFlow parser + AdapterResult**　★★★
  - 校验/保存 NDJSON，提取 final text、thinking、tools、usage、status 和 diagnostics；
  - summary 视为不可信输入并做 schema/type/non-negative/size 校验；
  - summary 缺失/损坏只降级 coverage，不伪造 zero；
  - provider/auth/quota/network 错误进入共享 taxonomy。
  - _Depends on：A3-5、A5-4。验收：脱敏 golden fixtures、截断行、重复 usage、fallback error、
    parser offline rebuild。_

- [x] **A5-6 DeerFlow MCP spike 与决议实现**　★★★
  - 对 pinned revision 验证 extension/MCP 配置、生命周期和 StreamEvents；
  - 可行时实现只消费 `task.mcp_servers` 的 DeerFlow dialect + Wire rewrite；
  - 不可行时明确 `mcp=unsupported`，依赖 MCP 的环境 preflight 拒绝；
  - 不把内建 web/bash 工具当作 Lane MCP。
  - _Depends on：A0-2、A3-3/4、A5-2。验收：无论 go/no-go 都有 fixture 和 capability basis；
    不允许“未知但继续运行依赖 MCP 的任务”。_

- [x] **A5-7 DeerFlow 单轮/subagent/cleanup E2E**　★★★
  - 完成一个真实 workspace 文件任务并通过 scorer；
  - `subagent=false/true` 都有 StreamEvent fixture，identity 只按真实证据声明；
  - timeout/cancel 清理 runner、DeerFlow、bash/MCP 子进程；
  - manifest 对账 model/options/workspace/version/coverage。
  - _Depends on：A5-1~6。验收：本地参数化 E2E；真实模型 smoke 单独记录，不进默认 CI。_

## A6 · DeerFlow 深度观测

- [x] **A6-1 DeerFlow Wire normalizer**　★★★
  - 从固定 StreamEvent/session fixtures 生成调用级或 aggregate-only evidence；
  - parser/revision version、offline rebuild、usage conflict；
  - 无逐调用 usage 时不得伪造 logical call。
  - _Depends on：A3-6、A5-5。验收：Wire manifest/coverage/token conflict tests。_

- [x] **A6-2 DeerFlow tool/subagent trajectory**　★★★
  - 归一化 planning、thinking、tool 和子任务事件；
  - 只有稳定 producer identity 才建立 parent/child topology；
  - 打开 subagent 与可观测 subagent identity 分开报告。
  - _Depends on：A5-7、A6-1。验收：开关两组 fixture，跨 agent/turn 不误关联。_

- [x] **A6-3 比较页 coverage matrix**　★★
  - DeerFlow 与 Claude/Codex 同 run 时展示 Prompt/model/MCP/trajectory/token/Wire 可比范围；
  - coverage 缺口不影响任务分，但阻止误读。
  - _Depends on：A6-1/2。验收：partial/aggregate-only/verified 清晰区分 unknown 与 zero。_

## A7 · ACP

- [x] **A7-1 ACP protocol/client spike**　★★
  - 固定协议版本、session/update/permission/cancel 语义；
  - 确定可复用库或最小 client 边界；
  - 评审 registry/distribution 供应链。
  - _验收：决议回写 design；两个 fake ACP server transcripts。_

- [x] **A7-2 ACP registry resolver/cache**　★★
  - schema 校验、版本/hash pin、HTTPS/checksum、原始 metadata cache；
  - offline cached run；普通 run 不下载/安装。
  - _Depends on：A7-1。验收：坏 checksum、版本不存在、registry 离线、cache 污染测试。_

- [x] **A7-3 AcpTransport + driver**　★★★
  - session、Prompt、updates、tools、usage、permission、cancel；
  - 标准 ParseResult/AdapterResult/manifest；
  - 未配置 permission answer 时不自动批准。
  - _Depends on：A2、A3、A7-1。验收：两个 fake servers 完成单/多轮、permission deny、
    crash、timeout。_

- [x] **A7-4 ACP descriptor/API/UI**　★★
  - `acp:<id>@<version>` 或等价稳定 ID 解析；
  - 展示 registry metadata、distribution、版本、可用性和数据边界；
  - run 创建时 pin 到 resolved hash。
  - _Depends on：A7-2/3。验收：两个 registry Agent 使用同一 transport，无复制 adapter。_

- [x] **A7-5 真实 ACP smoke**　★★
  - 选择至少两个许可/安装来源清晰的 registry Agent；
  - 固定版本，完成最小任务；脱敏保存 transcript/manifest。
  - _Depends on：A7-4。验收：附加测试记录；不作为默认 CI 前置。_

## A8 · SDK/远程 Agent

- [x] **A8-1 RemoteTransport contract**　★★
  - upload/data residency、server-side session、poll/stream、cancel unknown、artifact sync；
  - UI 运行前 disclosure；稳定终态映射。
  - _验收：fake remote service 覆盖取消已确认/未知、超时、部分 artifact。_

- [x] **A8-2 Python Agent plugin template**　★★
  - 用户自定义 Python Agent 的最小 plugin；
  - 延迟 import/可选依赖；统一 manifest/result；
  - 不允许 plugin 绕过 task MCP 声明和 artifact 路径。
  - _验收：仓库外样例包可通过 import path 注册并完成 fake E2E。_

- [x] **A8-3 后续 SDK/远程 Agent 可行性**　★★★
  - 具体 Agent 另行排期；满足 RemoteTransport contract 才进入内置目录。
  - _验收：本任务不属于首批发布门槛。_

- [x] **A8-4 隔离安装/runtime image ADR**　★★
  - 基于 DeerFlow/ACP 实际摩擦评估是否需要 per-Agent tool cache 或 runtime image；
  - 比较宿主预安装、uvx/npx cache、容器镜像的公平性/性能/供应链成本。
  - _验收：单独 ADR；在决议前继续禁止普通 run 隐式安装。_

## 发布门槛

Phase 0/1 标记完成前必须同时满足：

- [x] Claude/Codex/SSH Claude 和 legacy custom Agent 全部回归；
- [x] registry 是目录、构建、API 的唯一真相源；
- [x] fake CLI timeout/cancel 后没有遗留进程；
- [x] agent manifest 和 API secret 扫描为零命中；
- [x] MCP 只来自 `task.mcp_servers`，零/多 server 均有测试；
- [x] unsupported conversation/model/MCP 在启动前拒绝；
- [x] DeerFlow 有 pinned revision/package version、官方来源、runner/parser fixtures 和
      capability basis；
- [x] 前端区分 available/not installed/missing auth/unsupported version；
- [x] coverage 区分 unsupported、unknown、aggregate-only 和 verified；
- [x] 文档 `docs/agents.md` 与 `arena.yaml.example` 更新。

## 验证记录

实施开始后，每个完成任务在此追加：日期、commit、精确测试命令、结果和任何 capability
降级。真实账号/远程测试必须注明 Agent/CLI version、模型、执行位置和脱敏 fixture 路径。

### 2026-07-22 · 当前工作树

- commit：本功能提交（分支 `feat/scalable-agent-integration`）。
- 后端：`UV_CACHE_DIR=/tmp/agent-arena-uv-cache uv run pytest -q` → 900 passed、1 skipped、
  9 warnings；skip 为默认关闭的真实 ACP smoke。
- 前端：`npm test -- --run` → 60 passed；`npm run build` → passed。
- 静态检查：本次新增/修改的 Agent/ACP 模块 Ruff 通过；全仓仍有 4 个既有 unused 报告。
- ACP：fake server contract 与脱敏 transcript 测试通过；真实 smoke 使用统一 OpenRouter
  key/model 完成 OpenCode `1.18.4` 与 Kilo `7.4.11` 最小任务，均返回 `ACP_SMOKE_OK`。
  archive 已按 registry SHA-256 校验；脱敏 transcript/manifest 位于
  `fixtures/acp-real-2026-07-22/`，默认 CI 仍保持 skip。
- 能力降级：DeerFlow MCP、跨 Attempt resume、可观测 subagent identity 保持 unsupported；
  DeerFlow/ACP token coverage 按实际证据标记 aggregate-only/partial，不推断为逐调用 usage。
