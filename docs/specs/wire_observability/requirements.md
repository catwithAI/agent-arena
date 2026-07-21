# 通信观测基础层（Wire Observability Foundation）

## 背景

agent-arena 当前采集三类 agent 的事件、thinking 和 env tool trace，能够回答 agent
“做了什么”，但无法稳定回答 framework 在本地工具与远端服务之间“怎样通信”：

- 每轮何时调用 LLM，发送了多少上下文；
- 上下文如何增长、压缩和重建；
- 工具结果以全量、截断还是摘要形式回传；
- 请求是否流式、是否并行、是否 retry/failover；
- 同一个模型下，CC、Codex 的 token 差异来自哪里。

`~/work_codes/llm-gateway` 已经具备第三方模型路由和一套较完整的 LLM call
observability，可以作为第一种接入源和实现参考。但 agent-arena 不能把核心观测能力建立在
某个外部 gateway 的持续存在上：

- 用户可能直连 OpenAI、Anthropic、OpenRouter 或自建 provider；
- Codex Responses、MCP stdio、Env Attempt Server 等流量不一定经过该 gateway；
- 外部 gateway 的部署位置、可用性、schema 和版本不由 agent-arena 控制；
- agent-arena 必须能保存可重放、可比较、与 attempt 同生命周期的本地证据。

因此，本 spec 将通信观测定义为 **agent-arena 自有的基础能力**。外部 llm-gateway、
原生 CLI 日志、内置 HTTP 代理和 MCP wrapper 都只是可插拔 capture source，不能成为
agent-arena canonical 数据模型、存储、API 或前端的硬依赖。

Harbor 的关键启发不是“必须有一个网络 gateway”，而是先把各 agent 的原始事件通过
adapter-specific normalizer 转成统一调用/trajectory，再由统一模型驱动统计和展示。
Harbor 的 Codex adapter 会把 `token_count` 事件解释为一次模型调用结束并提取本次 usage；
Claude Code adapter 也从原生 session/event 日志恢复逐 step metrics。这类 native event
不是 wire 的替代品，但能在不引入代理、不处理 CA 的情况下先得到调用次数、模型和 token
曲线，并为后续 HTTP/MCP evidence 提供 correlation 锚点。

本仓库内的关联资料包括[设计文档](design.md)、[实施记录](tasks.md)和
[`WireEvidence v1` 数据结构](wire-evidence-v1.schema.json)。早期迁移阶段使用的
Harbor 调研笔记和安装草稿未随本仓库发布，不再作为规范依赖。

## 核心实验问题

**在不依赖特定第三方 gateway 的前提下，agent-arena 能否为每个 attempt 建立一份完整、
安全、可关联的通信证据，使不同 agent/framework 的 LLM 调用、上下文管理、工具结果
回传、retry、streaming 和多跳行为可以被直接比较？**

## 定位与原则

### agent-arena 必须拥有的能力

以下能力属于 agent-arena，不得委托给某个外部 gateway 作为唯一实现：

1. canonical wire schema 和 schema version；
2. attempt 级 correlation ID；
3. capture lifecycle；
4. 本地持久化和 manifest；
5. 脱敏和 capture policy；
6. source 能力声明与缺失覆盖说明；
7. wire API、前端通信时序和分析逻辑；
8. 与 events/thinking/trace/trajectory 的关联。

### 外部组件的角色

外部组件只提供证据：

- llm-gateway：LLM 请求、路由、usage、TTFT、retry/translation；
- CLI native event：调用级 usage、模型和 agent step；
- MCP wrapper：stdio JSON-RPC 帧；
- agent-arena 内置 HTTP capture：可控 HTTP/HTTPS endpoint。

任何 source 缺失或不可用时，agent-arena 仍必须能完成 attempt，并明确展示观测覆盖不足。

### 决策：一起设计，分源实现

原生事件和网关观测属于同一个“通信观测基础层”，应在本 spec 内一起设计，但实现上必须
保持为并列 source：

```text
CC/Codex native events ────┐
llm-gateway connector ─────┤
agent-arena HTTP capture ───┼─→ canonical logical calls / hops ─→ wire.jsonl
MCP stdio tap ──────────────┘                             └─→ trajectory correlation
```

作出这一决定的原因：

1. native event 和网络证据描述的是同一次 logical call，若分成两个独立功能会重复计算
   token、难以对齐 step，并产生两套 UI；
2. native event 覆盖 agent/framework 语义，网络 source 覆盖真实 payload、hop、retry 和
   streaming，二者互补；
3. native event 不依赖网络代理，是最先可交付、风险最低的 source；
4. 后续接入 gateway 时只增加 provenance 和字段完整度，不需要迁移已有数据模型；
5. 任一 source 单独不可用时，另一个 source 仍能提供部分证据。

因此实施顺序是 **native-event first，gateway evidence second**，但 schema、ID、manifest、
API 和前端从一开始按多 source 融合设计。native event 不能作为 adapter 私有统计留在
`AdapterResult.token_usage` 后就结束，必须进入 canonical `llm_call`。

### Harbor 设计借鉴清单

本 spec 明确采用 Harbor 的以下设计思想：

1. **raw evidence 与 normalized trajectory 分层**：保留原始 events/wire，同时生成稳定
   的统一调用和 trajectory；
2. **adapter-specific normalizer**：CC/Codex 各自理解原生 schema，核心分析层不理解
   厂商事件格式；
3. **调用级 metrics**：token/cost/cache 挂到单次 call/step，而不只保存 attempt 总量；
4. **phase-scoped lifecycle**：agent、verification、artifact collection 分阶段观测；
5. **lifecycle hooks**：start/end/cancel 都走统一 capture hook，避免逻辑散落在 adapter；
6. **artifact manifest**：采集产物必须说明来源、完整性和失败，而不是只看文件是否存在；
7. **sub-agent trajectory**：子 agent 保留独立 identity/trajectory，由父 step 引用；
8. **fail-open observability**：观测 sink 默认不阻断 agent 主流程；
9. **沙盒透明重定向 egress 骨架**：Harbor 的 docker 环境用 sidecar（GOST
   `type: red` 透明重定向 + SNI 嗅探）+ 容器内 nftables 把全部出站 TCP 无感
   redirect 到 sidecar，白名单可热更新
   （`src/harbor/environments/docker/harbor-docker-egress-control-sidecar/`、
   `docker.py` 的 `_apply_network_policy`）。Harbor 只用它做域名白名单（不解
   TLS），但这套「容器网络命名空间内 redirect + sidecar」正是 agent-arena 沙盒内
   流量采集的插入点：把 sidecar 换成会记录/MITM 的代理即可，**不依赖被测 CLI
   遵守 proxy 环境变量**（见 R8.1）。

不直接照搬 Harbor 的具体 ATIF 文件格式作为 wire schema；agent-arena 可以在 design 阶段
决定 trajectory 是否兼容/映射 ATIF，但网络 hop、MCP frame 和 payload blob 仍使用
agent-arena 自有 wire schema。

## 术语

- **wire record**：一次通信事实的 canonical 记录，可能来自网络、stdio 或原生事件。
- **capture source**：产生 wire evidence 的组件或 connector。
- **logical call**：一次业务意义上的 LLM/tool 调用；可能跨多个网络 hop。
- **hop**：logical call 中的一次具体传输，如 runtime → LLM gateway。
- **trajectory step**：归一化后的 agent 行为步骤，不等同于 wire record。
- **capture manifest**：说明本次 attempt 启用了哪些 source、采集是否完整及失败原因。
- **strict capture**：观测不完整即使 attempt 不满足验收的模式。
- **fail-open capture**：观测故障不阻断 agent 主流程的默认模式。

## 需求

### R1 agent-arena 自有 canonical 数据模型

agent-arena 必须定义独立于任何 provider/gateway 的 versioned wire schema。

1. canonical 文件为：
   - `<attempt_dir>/wire.jsonl`：结构化索引；
   - `<attempt_dir>/wire-blobs/`：可选的大 payload；
   - `<attempt_dir>/wire-manifest.json`：覆盖范围和完整性；
2. `wire.jsonl` 至少支持以下 record type：
   - `llm_call`：一次逻辑 LLM 调用；
   - `http_exchange`：一次具体 HTTP request/response hop；
   - `stream_chunk`：可选的流式分片或分片摘要；
   - `mcp_frame`：MCP JSON-RPC message；
   - `capture_event`：source start/stop/error/drop；
3. 所有 record 必须包含：
   - `schema_version`；
   - `record_id`；
   - `attempt_id`；
   - `phase`；
   - `source`；
   - `record_type`；
   - `timestamp` 或 `started_at`/`finished_at`；
4. 可选字段缺失时必须为 null/absent，并记录 capability 或 parse error；禁止用零值
   冒充真实观测值；
5. 外部 source 的原始字段必须经过 adapter 转成 canonical schema，前端和分析代码不得
   直接依赖 llm-gateway SQLite schema、Claude stream-json schema 或 Codex session schema；
6. schema 必须允许向后兼容读取；新增字段不要求重写历史文件。
7. source spool 使用独立、versioned 的 evidence schema；Python/Go/Node/sidecar 只能写该
   schema，不能把私有事件任意 JSON 直接交给 finalizer。

### R2 capture source 插件化

通信观测必须支持多个 source 同时工作，并允许按 agent/provider/部署环境选择。

初始 source 类型：

| Source | 覆盖 | 定位 |
|---|---|---|
| `native-event` | CC/Codex 原生调用级 usage 和 step | 必备低风险基线 |
| `llm-gateway` | 经现有 gateway 的 LLM call、路由、retry、TTFT | 可选 connector |
| `lane-http` | base URL 可注入或 agent-arena 控制的 HTTP 流量 | agent-arena 自有代理能力 |
| `mcp-stdio` | CC/Codex ↔ MCP server JSON-RPC | agent-arena 自有 wrapper |
| `https-mitm` | 不可改 base URL 的 HTTPS 流量 | 后期可选能力；沙盒内可用透明重定向（R8.1），不依赖 CLI 遵守 proxy env |

每个 source 必须声明 capability，例如：

```json
{
  "request_metadata": true,
  "request_payload": false,
  "response_payload": false,
  "usage": true,
  "stream_timing": "ttft-only",
  "retry_detail": "count-only",
  "correlation": "attempt+time"
}
```

要求：

1. source 使用统一 start/flush/stop 生命周期；
2. source 不得自行定义前端消费格式；
3. 同一事实被多个 source 捕获时必须合并或关联，不能重复计入 token/调用总量；
4. source 优先级只决定字段可信度，不得静默覆盖冲突；冲突必须保留 provenance；
5. 新增 source 不要求修改所有 agent adapter。

#### R2.1 native-event 是一等 source

native-event 不能只是 gateway 不可用时的临时 fallback，必须作为长期保留的一等 source：

1. CC/Codex normalizer 由各自 adapter 或独立 parser 模块维护；
2. parser 输入必须是已保存的原始 events/session 日志，支持离线重建，不能只依赖运行时
   内存累计；
3. 每个可识别模型调用输出一个 canonical `llm_call`，而不是只更新 attempt 总 token；
4. 至少提取：原始 event reference、call/turn 标识、timestamp、model、input/output/cache/
   reasoning usage、agent step/tool call 邻接关系；
5. Codex `token_count` 等“调用结束标记”应保留 producer event type，避免把启发式边界
   展示成协议事实；
6. Claude/Codex CLI schema 或版本变化导致解析失败时，保留 raw event、parser version 和
   parse error；
7. normalizer 必须可对历史 attempt 重跑，parser 升级不要求重新执行昂贵的 agent；
8. native-event record 的 `source`、`provenance` 和 `confidence` 必须明确，不能伪装成
   实际 HTTP exchange。
9. Phase 1 必须从 native events 生成最小、versioned 的 trajectory step 索引，至少包含
   `step_id`、顺序/时间、agent identity、producer event refs、tool 邻接和可选
   `logical_call_id`；在该产物存在前，UI 不得承诺从 trajectory step 跳转到 wire。

#### R2.2 多 source 融合规则

当 native event 与 gateway/HTTP source 同时看到一次调用时：

1. 生成一个 logical `llm_call`，网络请求作为其 hop/evidence，不生成两个业务调用；
2. 优先使用显式 call ID；其次使用 turn、时间窗口、模型、usage 和顺序关联；
3. token 字段同时保留各 source 原值和 provenance；canonical 选值优先级由字段级策略决定；
4. provider response usage 通常优先于 CLI 累计/估算值，但冲突不得被删除；
5. native event 提供 trajectory step/tool 邻接，HTTP source 提供 endpoint/routing/retry/chunk；
6. 无法可靠合并时保留两条 evidence 并标 `unmatched`，禁止强行去重；
7. attempt 总 token 只能由 canonical logical calls 聚合，不能把 source totals 相加。

### R3 attempt 生命周期和 phase

wire capture 必须跟随 attempt 生命周期，而不是跟随全局进程生命周期。

标准 phase：

```text
attempt_setup
agent_run
verification
artifact_collection
attempt_cleanup
```

要求：

1. attempt 创建时生成 capture context；
2. agent 启动前完成 source 注入；
3. agent 正常完成、timeout、取消和异常时都必须 flush；
4. cleanup 必须释放端口、proxy、临时证书和 wrapper 子进程；
5. 默认只将 `agent_run` 计入 framework 通信画像；其他 phase 可采集但必须分开；
6. 安装依赖、健康检查、scorer 的 LLM 调用不得误算为被测 agent 调用；
7. capture start/stop 失败写入 manifest，不能只写普通后端日志。
8. 独立进程 source 必须通过启动参数、只读 phase 文件或控制通道获得 phase；无法可靠归属
   时写 `unknown` 并从 `agent_run` 指标中排除，禁止仅凭时间窗口猜 phase。

### R4 correlation 和身份传播

每个 attempt 必须使用稳定的 correlation identity：

```text
attempt_id
logical_call_id
hop_id
parent_hop_id
trajectory_step_id
tool_call_id
agent_id
parent_agent_id
```

要求：

1. `attempt_id` 是 wire、events、thinking、trace、trajectory 的共同根 ID；
2. 一次 LLM logical call 必须尽可能分配稳定 `logical_call_id`；
3. 多跳通信通过 `parent_hop_id` 或等价关联形成链路；
4. 主动可控的 HTTP 请求注入 `x-lane-attempt-id`；只有 agent-arena 自身是逐调用 caller，
   或 runtime hook 确实位于每次调用路径上时，才注入 `x-lane-call-id`。CC/Codex 的
   进程级静态 header 只能传播 attempt，不能冒充逐调用 ID；对兼容现有 llm-gateway 的
   路径同时支持将 attempt 映射为 `x-eval-session-id`；
5. correlation header 在离开受控 gateway 前必须被移除，不能泄露内部 attempt ID 给
   第三方 provider；
6. 不能主动传播 ID 时，允许用时间、模型、usage、顺序进行启发式关联，但必须输出
   `correlation_confidence`；
7. 全局 active-session 只能用于串行诊断，禁止作为并发评测的正式关联方案；
8. sub-agent 必须使用独立 `agent_id`，并保留 `parent_agent_id`。
9. 反向代理和 provider connector 应提取其接收请求 ID、provider response request/message
   ID，并把它们作为 call/hop anchor；无法跨 source 传播时只能输出 source-local ID。

### R5 LLM 调用观测

每次可识别的 LLM logical call 应尽可能记录：

- protocol：`anthropic-messages`、`openai-chat-completions`、
  `openai-responses` 或其他显式值；
- model requested / model resolved；
- provider/source/routing path；
- request、routing、upstream send、first token、complete 时间；
- input/output/cache-read/cache-write/reasoning tokens；
- request/response bytes；
- messages count；
- 每条 message 的 role、类型、大小和 hash；
- system prompt、tools schema 的大小和 hash；
- tool result message 的大小和 hash；
- stream、chunk count 和可选 chunk timing；
- finish reason；
- HTTP status、error class；
- retry/failover 次数及可选逐次明细；
- translation direction 和耗时；
- payload/body reference（capture policy 允许时）。

其中：

1. token 应优先采用 provider 返回的精确 usage；
2. native event 和 HTTP usage 冲突时必须保留两者及 provenance，分析层选择可信优先级；
3. 估算 token 必须标记 `estimated=true` 和 tokenizer/算法；
4. retry/failover 不能重复计入一次 logical call 的业务调用数，但应计入实际 upstream
   attempt 数；
5. 流式中断时仍应保留已观测 chunk、耗时和 partial 标记。
6. 只有 native event 时，允许生成没有 HTTP hop 的 `llm_call`；manifest 必须显示 transport
   evidence 缺失。
7. 只有 HTTP/gateway evidence 时，允许生成没有 trajectory step 的 `llm_call`；manifest
   必须显示 agent-semantic correlation 缺失。
8. 所有用于跨 source 比对的 message/system/tools/tool-result hash 必须使用同一 canonical
   semantic IR、Unicode NFC、RFC 8785 JCS 和 SHA-256，并携带 `hash_domain`；raw body hash
   与第三方私有 hash 不得冒充可跨 source 比较的 semantic hash。
9. 同模型对比或多模型对比中，CC/Codex 选择了 agent-arena 命名的第三方 provider 时，流量
   必须可按 attempt 接入 agent-arena 内建反代；`full` policy 保存协议原生 request/response
   payload（写盘前脱敏），canonical 只保存 summary/hash/size/blob ref，不用 agent 事件
   反推或改写原始交互。
10. 每个比较 attempt 的 body/blob、capture token、provider/model 和 logical call 归属必须
    隔离；并发运行不得串 prompt/response。比较模式只决定编排，不得导致某个 agent 静默
    绕过 capture。
11. 流式响应应保存完整、脱敏后的协议事件流并同时生成 `stream_chunk` timing；中断时保存
    已收到部分并标 `partial`。这里的“原始”指协议原生结构，不包含 Authorization、Cookie、
    API key 等被 R11 强制剔除的凭证。
12. 未经过内建反代的默认官方 provider、历史 run 或走非 HTTP 协议（如 SDK/WebSocket）的
    内部流量，不得宣称已保存原始 prompt/response；manifest/UI 必须显示
    `transport not observed` 或等价能力边界。

### R6 上下文增长与压缩分析

agent-arena 必须能从 wire evidence 生成调用级上下文画像：

- input token 曲线；
- cache token 曲线；
- message count/bytes 曲线；
- tool result 回传大小曲线；
- system/tools schema 开销；
- compaction 前后 token、message 变化；
- summary/compaction call 与主调用的关系。

要求：

1. 压缩检测同时使用 token 突降、message hash diff 和显式 native event；
2. 检测结果带 `source` 和 `confidence`，不得把启发式结论展示成确定事实；
3. call role 至少区分 `main`、`compaction`、`planning`、`meta`、`subagent`、
   `unknown`；
4. 分析必须能区分“上下文被压缩”和“启动了全新 session”；
5. 不要求第一版保存完整 prompt，也必须能通过 token、size、count、hash 做趋势分析。
6. hash domain 不同或无法重算 canonical semantic hash 时，只允许在同一 source/domain 内
   做 message diff，不能据此判定跨 source full/truncated/summarized。

### R7 MCP stdio 采集

agent-arena 必须提供自有 MCP stdio wrapper，不能依赖网络 gateway。

注入形态：

```text
lane-mcp-tap [capture args] -- <original command> [original args...]
```

要求：

1. stdin/stdout 字节透明转发；
2. stderr 不得混入 JSON-RPC stdout；
3. 支持一条 JSON-RPC message 跨多个 read chunk；
4. 记录方向、JSON-RPC id、method、大小、时间、成功/错误；
5. request/response 必须按 JSON-RPC id 配对；notification 允许无 id；
6. tool result 记录原始大小、脱敏后大小、是否截断；
7. timeout/cancel/signal 传播给整个子进程组；
8. 默认 fail-open：旁路日志失败不得破坏 MCP 主通信；
9. strict capture 下 frame 丢失或解析失败会使 capture completeness 不通过；
10. wrapper 性能开销必须有基线测试。

### R8 agent-arena 自有 HTTP capture

对于 base URL 可注入或由 agent-arena 控制的流量，agent-arena 必须具备不依赖外部
llm-gateway 的 HTTP capture 路径。

第一阶段要求：

1. 支持反向代理形态，而非一开始要求通用 HTTPS MITM；
2. 支持 HTTP/1.1、SSE 和长连接取消；
3. 保持请求方法、path、query、body 和非敏感 header 语义；
4. 正确处理 backpressure，不能先完整缓存无限流式响应再转发；
5. 支持 metadata-only 和 parsed payload 模式；
6. 对 LLM 协议使用 parser plugin，不在核心 proxy 中写死单一协议；
7. capture 故障可旁路时必须回退，不能无提示改变 agent 结果；
8. 是否支持 WebSocket、Socket.IO 和通用 CONNECT 在 design 阶段单独决定，不作为
   第一阶段前置条件。

#### R8.1 沙盒透明重定向（sandbox transparent redirect）

对运行在容器/沙盒内、无法或不可靠地通过 base URL / proxy env 注入的流量
（沙盒内的工具进程，以及未来容器化运行的 CC/Codex），采集插入点采用
**容器网络命名空间内的透明重定向**，参考 Harbor egress sidecar 的成熟骨架：

1. 沙盒容器内用 nftables 把出站 TCP 无感 redirect 到同 network namespace 的
   capture sidecar 端口；被测进程对重定向零感知，**不依赖其遵守
   `HTTP_PROXY`/`HTTPS_PROXY`**；
2. sidecar 分档工作，与 R11 capture policy 对应：
   - 仅连接元数据 + SNI 嗅探（等价 Harbor 现状，不解 TLS）→ `metadata`；
   - TLS 终止（MITM）后按 R8 的 parser plugin 解析 → `parsed`/`full`；
3. MITM 档需要向沙盒注入本地 CA 信任（系统 CA 目录 / `NODE_EXTRA_CA_CERTS` /
   `SSL_CERT_FILE`，按被测进程的 TLS 栈逐一验证），CA 私钥不进沙盒、证书按
   attempt 生成并在 cleanup 销毁（R3.4）；
4. 重定向规则与白名单必须支持按 phase 热更新（R3），并在 manifest 中记录
   生效档位与规则版本；
5. 该机制回答沙盒内工具进程的流量从哪截的问题：答案是容器内 redirect + sidecar，
   而非宿主机网桥抓包——后者拿不到按 attempt 隔离的归属，且需要宿主机特权。

### R10 外部 llm-gateway connector

agent-arena 应提供可选 llm-gateway capture source，以便尽快复用现有观测能力，但它不是
必需组件。

connector 要求：

1. 配置只包含 endpoint、认证引用和 capability，不暴露 gateway SQLite 路径；
2. agent-arena 不直接 attach/read 外部 SQLite；
3. 通过 versioned HTTP API 按 `attempt_id/session_id` 增量读取 calls 和 compactions；
4. 外部记录必须转换为 agent-arena canonical wire schema；
5. gateway 不可用、API 版本不兼容或数据缺失时，attempt 默认继续，manifest 标记失败；
6. connector 必须能与 native-event source 去重/关联；
7. 外部 gateway 的 prompt preview 默认不直接导入；只有 agent-arena capture policy 允许时
   才可导入 payload/preview；
8. llm-gateway connector 首期仅承诺 Anthropic Messages 和 Chat Completions；
9. Codex 只有在 gateway 提供 Responses API 后才能使用该 source，不能用 endpoint
   redirect 冒充协议兼容；
10. 移除 connector 后，agent-arena 的 wire API、历史数据和前端必须继续工作。

### R11 capture policy 与脱敏

至少支持四档 capture policy：

| Policy | 行为 |
|---|---|
| `off` | 不采集 |
| `metadata` | endpoint、size、timing、status、usage、hash |
| `parsed` | 加入经过字段级脱敏的结构化 payload |
| `full` | 保存脱敏后的完整 payload/blob |

要求：

1. 默认不得持久化 Authorization、Proxy-Authorization、Cookie、API key、OAuth token；
2. secret 必须在写盘前脱敏，不能先落原文再异步清理；
3. 脱敏失败时丢弃 payload 并保留 metadata，不能保存未脱敏原文；
4. blob 使用 content-addressed 文件名并记录 hash、原始/保存大小和压缩方式；
5. capture policy 必须落 manifest；
6. 真实用户例子默认 `metadata`；专用 benchmark 可显式提升到 `parsed/full`；
7. prompt/response preview 也属于敏感 payload，受相同策略约束；
8. UI/API 不得返回被 policy 禁止的内容；
9. 日志、错误消息和 parse error 同样需要 secret scrub；
10. 后续数据保留和删除策略必须能按 attempt 清理全部 wire/blobs。

### R12 持久化、manifest 与完整性

`wire-manifest.json` 至少包含：

- schema version；
- capture policy；
- source 列表及版本；
- 每个 source 的 capability；
- started/finished 时间；
- record/blob 数量和大小；
- dropped/parse-error/conflict 数；
- complete/partial/failed/not-applicable 状态；
- failure reason；
- strict/fail-open 模式；
- correlation coverage；
- protocol coverage。

要求：

1. “没有发生通信”与“采集器没有工作”必须可区分；
2. JSONL 尾部不完整时读取器应跳过损坏尾行并报告 manifest mismatch；
3. 并发 source 写入不能互相覆盖；
4. attempt 完成后 manifest 必须 finalize；
5. timeout/进程崩溃后下一次后端启动应能识别并标记未 finalize manifest；
6. canonical `wire.jsonl`/`wire-manifest.json` 可通过专用 Wire UI/API 访问；
   `wire-sources/` 与 `wire-blobs/` 排除普通 artifact 列表，blob 只能通过受 capture policy
   和授权控制的 Wire API 下载；
7. DB 只保存摘要和索引，不把大 payload 塞入主数据库。

### R13 API 与前端

agent-arena 后端需要提供：

```text
GET /runs/{run_id}/attempts/{attempt_id}/wire
GET /runs/{run_id}/attempts/{attempt_id}/wire/manifest
GET /runs/{run_id}/attempts/{attempt_id}/wire/blobs/{ref}
```

wire API 要求：

1. 支持 record type、phase、protocol、logical call、时间范围过滤；
2. 大数据量使用分页/cursor，不一次返回整个 JSONL；
3. blob ref 必须做目录穿越保护和 policy/权限检查；
4. 返回 provenance、confidence 和 parse error；
5. 历史 attempt 没有 wire 文件时返回明确 `not_available`，不视为后端异常。

RunDetail 新增“通信时序”视图，至少展示：

- LLM call 时间轴；
- input/cache/output token 曲线；
- TTFT/总耗时；
- model/provider/routing；
- retry/failover；
- compaction 标记；
- MCP tool call/result size；
- capture completeness；
- trajectory step ↔ logical call ↔ hop 的跳转。

不能只展示总 token；调用级曲线是本功能的核心产出。

### R14 可靠性与行为扰动

1. 默认 `fail-open`：capture 故障不改变 agent 成功/失败状态；
2. capture 状态必须独立于 attempt status，不能把二者混为一个错误码；
3. strict capture 仅由 benchmark/spec 显式开启；
4. capture 组件必须设置独立的 buffer、超时和大小限制；
5. 不允许同步写盘阻塞 SSE 主转发；
6. wrapper/proxy 开启前后必须做旁路对照测试；
7. 需要测量并报告：
   - 非流式额外延迟；
   - TTFT 增量；
   - streaming throughput 下降；
   - CPU/内存/磁盘开销；
8. 第一阶段目标：metadata capture 不使成功率发生可检测下降，非流式额外延迟
   p95 < 20 ms，TTFT 增量 p95 < 20 ms；若部署环境无法满足，需在 design 中重新定标；
9. 无限或超大 payload 必须流式处理并设上限，不能无界聚合进内存；
10. kill/cancel 后不得残留 proxy 或 MCP wrapper 子进程。

### R15 provider 与协议边界

1. Claude Code 走第三方 provider 时必须保持 bearer/api-key 认证语义，不互相转写；
2. attempt-scoped correlation header 必须与 provider 的静态 custom headers 合并，不能
   覆盖 `x-user-id` 等现有 header；本项不表示 CLI 能逐调用动态刷新 header；
3. gateway/source 负责真实上游 credential，agent-arena wire log 永不保存 credential；
4. Codex provider 必须声明 `openai-responses` capability；
5. 选择不兼容协议时在 adapter 启动前 fail fast；
6. Responses → Chat Completions 转换若实现，必须是独立 source/sidecar，不能内嵌到
   wire schema 或分析层；
7. 协议转换前后的两个 hop 应能分别记录，并归属同一 logical call；
8. 模型 alias 必须同时保留 requested 和 resolved model，避免分析时把不同真实模型
   当成同模型；
9. **与现有 provider 配置对齐，不另起一套 schema**：本节的 protocol/auth 声明
   必须落在既有 `ModelProviderSection`（`backend/model_providers.py`，字段
   `kind` / `api_key_env` / `custom_headers`）的扩展上——
   - 配置枚举使用 `anthropic | openai-chat | openai-responses`，Wire 层分别映射为
     `anthropic-messages | openai-chat-completions | openai-responses`；
   - `auth_mode: bearer | api-key` 显式决定 Claude Code 注入
     `ANTHROPIC_AUTH_TOKEN` 还是 `ANTHROPIC_API_KEY`，两种模式不得互相转写；
   - `arena.yaml` 中未填写新字段时保持默认值：`kind: anthropic`、
     `auth_mode: bearer`、`wire_api: responses`。

### R16 测试与验收

#### 单元测试

- canonical schema 序列化/兼容读取；
- source capability 和冲突合并；
- secret redaction；
- HTTP/SSE parser；
- MCP 跨 chunk frame 重组；
- correlation 和 heuristic confidence；
- compaction 检测；
- manifest finalize/recovery；
- JSONL 损坏尾行容错；
- path traversal 和 blob policy；
- CC/Codex 原生事件 → 调用级 `llm_call` normalizer；
- 同一次调用的 native event + gateway record 融合和 token 冲突保留；
- parser 升级后对历史 raw events 离线重建。

#### 集成测试

至少覆盖：

1. fake Anthropic Messages，stream/non-stream/tool_use；
2. fake Chat Completions，stream/non-stream/tool_calls；
3. fake Responses source（即使第一版 Codex 不接外部 gateway，也要验证 schema 可表达）；
4. upstream 429 → failover → success；
5. SSE 中途断开；
6. MCP request/result、notification、server crash；
7. attempt timeout/cancel；
8. 两个并发 attempts 不串 correlation；
9. llm-gateway connector unavailable 时 fail-open；
10. metadata policy 下没有敏感 payload 落盘。

#### 端到端验收

以 `drone-strike-hitl` 或等价多工具场景执行至少一次三 agent 对比：

- 每个 attempt 都有 `wire-manifest.json`；
- CC/Codex 至少有 native-event 调用级记录；
- CC 走 llm-gateway 时能同时获得 gateway call 并与 native event 对齐；
- MCP tool call/result 与 env trace 对齐；
- 能直接回答每轮 input/cache/output token 曲线；
- 能看到至少一次 tool result 回传大小；
- 能区分 retry、并发和 compaction；
- 前端可从 trajectory step 跳到对应 wire evidence；
- 关闭/移除 llm-gateway connector 后，native-event、MCP、agent-arena HTTP capture 和历史
  wire 页面仍正常工作。

## 非功能要求

1. Python/Go/Node source 产生的记录必须能统一消费；
2. 时间统一使用 UTC，持久化为 ISO 8601 或明确单位的 epoch；
3. 所有 duration/size/token 字段单位固定并写进 schema；
4. capture 代码必须有结构化日志，但结构化日志不代替 manifest；
5. source 版本、CLI 版本、provider/gateway build 应进入 attempt 快照；
6. 本地开发可只启用 native-event + MCP，部署环境再选择 gateway/HTTP source；
7. 不要求联网才能读取和分析已完成 attempt 的 wire 数据。

## 不做的事

- 第一阶段不实现通用 TCP packet capture/tcpdump；
- 第一阶段不承诺任意 HTTPS CLI 的 MITM；
- 第一阶段不解析所有供应商私有协议；
- 不用 wire log 替代 events、thinking、trace 或 trajectory；
- 不要求保存完整 prompt/response 才能交付 token/context 分析；
- 不让外部 llm-gateway 成为 agent-arena 启动依赖；
- 不直接读取或迁移 llm-gateway SQLite；
- 不在本 spec 内实现 Responses → Chat 协议转换；
- 不把 capture 失败默认算作 agent 任务失败；
- 不在第一阶段做跨 run 的大规模通信分析仓库。

## 依赖与风险

### 外部接口

- llm-gateway 需要补充 versioned、按 session 查询 calls/compactions 的只读 API；
- CC 支持通过 `ANTHROPIC_CUSTOM_HEADERS` 注入 attempt-scoped header；
- Codex 自定义 header 和 Responses endpoint 能力随 CLI 版本变化，需要 capability test；
- R8.1 的 MITM 档依赖各被测进程 TLS 栈的 CA 信任注入可行性（Node 的
  `NODE_EXTRA_CA_CERTS`、Rust/OpenSSL 的 `SSL_CERT_FILE`、系统 CA 目录），
  需要按 CLI×版本建立兼容矩阵；certificate pinning 的进程只能降级 `metadata` 档。

这些是对应 source 的依赖，不是 agent-arena 基础能力整体的启动依赖。

### 数据安全

prompt、工具结果和响应可能包含代码、凭证或真实用户信息。`parsed/full` 上线前必须完成
字段级脱敏、权限检查和删除测试。无法证明安全时只开放 `metadata`。

### correlation 不完整

第三方服务可能移除 header，部分 runtime 老版本可能无法传播 attempt ID。所有不确定关联
必须显式标 confidence，不能为了让 UI 看起来完整而伪造确定关系。

### 行为扰动

proxy/wrapper 会引入延迟、backpressure 和新的失败点。每个 source 上线前必须有 bypass
对照；不能只测“能跑通”，还要测“没有明显改变被测行为”。

## 实施顺序

1. **Foundation**：canonical schema、writer、manifest、API、capture lifecycle；
2. **Native events（首个生产 source）**：借鉴 Harbor 的 adapter normalizer，将 CC/Codex
   原始事件逐调用转换为 `llm_call`，替代仅保存 attempt token 总量的现状；
3. **llm-gateway connector**：attempt-scoped header、calls API、拉取与去重；
4. **MCP stdio**：自有 wrapper、tool result size、trace 对齐；
5. **agent-arena HTTP capture**：先反代/SSE，后扩协议；沙盒内流量的透明重定向
   形态（R8.1）按需落地；
6. **分析与 UI**：context 曲线、时序、retry、compaction；
7. **Responses/HTTPS 扩展**：按实际需求增加，不阻塞基础层交付。

这个顺序允许 agent-arena 尽快利用现有 llm-gateway 产出价值，同时从第一天起保持数据模型、
生命周期和用户体验独立于它。
