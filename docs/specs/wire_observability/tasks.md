# 通信观测基础层——实施任务

> 需求：`requirements.md`（R1-R16）
> 设计：`design.md`（§4 模块结构、§23 实施阶段）
> 拆分原则：每个任务独立可验证、可单测；按「先低风险高价值、后高风险」排序；
> 评审遗留项（m1/m2/m5/m6/m8/m9/m10）已编入对应任务验收；涉及持久化/API 契约的
> 改动必须同步 requirements/design，tasks 不自行批准偏离。

难度标记：★ 低 / ★★ 中 / ★★★ 高。
每个任务完成后更新本文件状态并在文末「验证记录」补一条。满足依赖的任务可以并行；
编号用于分组，不覆盖下方 `Depends on`（例如 W4-4 应先于 W4-1）。

验收约定：后端任务至少记录精确的 `uv run pytest <test-file-or-node>` 命令；前端任务记录
项目现有的 typecheck/test/build 命令；远端 49 实测只能作为附加验收，不能替代本地/CI
可重复的 fixture 和自动化测试。

## 任务总览

| 组 | design Phase | 任务 | 外部依赖 | 系统编程/spike | 价值节点 |
|---|---|---|---|---|---|
| W0 Foundation | 0 | W0-1~7 | 无 | W0-4 侵入 adapter | schema/生命周期地基 |
| W1 Native events + 可读 UI | 1 | W1-1~9 | 无 | W1-2 Codex spike | 调用级数据可逐条检查、定位与对账 |
| W2 gateway connector | 2 | W2-1~4 | llm-gateway API | — | 补 routing/retry/TTFT |
| W3 MCP stdio tap | 3 | W3-1~4 | 无 | W3-1 透明 pump | tool result size |
| W4 reverse HTTP | 4 | W4-1~6 | 无 | W4-1 SSE 转发 | 第三方模型真实 payload/多 hop |
| W5 沙盒 spike | 5 | W5-1~2 | 无 | W5-1/2 沙盒/CA | 沙盒 MITM（gated） |
| W6 分析扩展 | 6 | W6-1~4b | — | W6-4b MITM（gated） | compaction/子 agent/HTTPS |
| W7 Office 产物预览 | 横向 | W7-1~5 | renderer/格式库待 ADR | OOXML 安全转换 | PPT/Word/Excel 可视验收 |

组级依赖：`W0 → W1`；W2-2~4 依赖 W0；W3 依赖
W0-4/W0-5；W4 依赖 W0；W5-2 依赖 W5-1；W6-1~3 依赖 W1 和公共
semantic hash，W6-4b 依赖 W5-2；W7-2~5 依赖 W7-1 的 renderer/安全 contract。

任务级关键依赖：

| 任务 | Depends on |
|---|---|
| W0-3 | W0-1, W0-2 |
| W0-4 | W0-1~3 |
| W0-5 | W0-1, W0-3, W0-4 |
| W0-6/W0-7 | W0-5 |
| W1-1/W1-3/W1-6 | W0-5；W1-3 另依赖 W1-2 决议 |
| W1-4/W1-5 | W1-1, W1-3 |
| W1-7/W1-8/W1-9 | W1-7←W0-6,W1-4；W1-8←W1-7；W1-9←W0-6,W1-1,W1-6,W1-8 |
| W2-2/W2-3/W2-4 | W2-2←W0-4；W2-3←W0-5,W2-1,W2-2；W2-4←W0-6,W2-3 |
| W3-1/W3-2/W3-3/W3-4 | W3-1←W0-3；W3-2←W3-1,W0-1；W3-3←W3-2,W0-4；W3-4←W3-2,W1-1,W1-3 |
| W4-1~6 | W4-4←W0-4；W4-1←W0-3,W0-4,W4-4；W4-2←W0-1,W4-1；W4-3←W0-2,W0-3,W4-1,W4-2；W4-6a←W0-4,W4-1,W4-3,W4-4；W4-6b←W4-6a,W1-2(Codex normalizer)；W4-5←W0-6,W4-3,W4-6a |
| W5-2 | W5-2←W5-1 |
| W6-1~3/W6-4a/W6-4b | W6-1~3←W1；W6-4a←W4-2；W6-4b←W5-2,W4-2 |
| W7-1~5 | W7-2/W7-3/W7-4←W7-1；W7-5←W7-2,W7-3,W7-4 |

评审 minor 项落位：m1→W0-6，m2→W4-3，m4→W4-1，m5/m6→W4-4，m7→W0-2，m8→W3-1，
m9→W1-6，m10→W0-4，nit4→W0-5，nit5→W0-6。

---

## W0 · Foundation（design §23 Phase 0）

- [x] **W0-1 canonical models + WireEvidence v1**　★★
  - `backend/wire/models.py`：envelope + `llm_call`/`http_exchange`/`stream_chunk`/
    `mcp_frame`/`capture_event`/`context_compaction` 六类 payload 的 Pydantic 模型
    （design §6.1-§6.7）；canonical reader 未知字段容忍读取（R1.6）。
  - `backend/wire/evidence.py`：`WireEvidence v1`（design §8.2），导出
    `wire-evidence-v1.schema.json` 并提交到本 spec 目录；evidence envelope/variant 使用
    `additionalProperties=false`，未知字段只允许进入已登记的 namespaced `extensions`。
  - `backend/wire/ids.py`：`wr_`/`we_`/`lc_`/`hop_`/`ts_` 的 uuid5 确定性生成
    （design §7.1-7.2、§10.6）。
  - `backend/wire/hashing.py`：公共 semantic IR、Unicode NFC、RFC 8785 JCS、SHA-256 与
    `hash_domain`；本任务实现协议无关算法，Claude/Codex/HTTP 的协议映射分别留 W1/W4。
  - _验收：`tests/test_wire_models.py` —— 全类型序列化 round-trip；旧版本文件多字段/
    缺可选字段可读；零值与 null 语义区分（R1.4）；evidence JSON Schema 分别验证
    Python/Go/Node 样例并拒绝未知 envelope/variant 字段；同输入 ID 生成幂等；
    `tests/test_wire_hashing.py` 验证 key 序、JSON 空白和 Unicode 转义不影响 semantic hash，
    NFC 后 key 冲突时拒绝生成 canonical semantic hash。_

- [x] **W0-2 policy + redaction + paths**　★★
  - `backend/wire/policy.py`：四档 policy、effective policy 最严格交集（design §16.1）。
  - `backend/wire/redaction.py`：header 黑名单、JSON key pattern、文本 secret pattern、
    脱敏失败→丢弃 payload 保留 metadata（R11.3）；scrub 函数同时供日志/错误消息使用
    （R11.9，评审 m7）。
  - `backend/wire/paths.py`：attempt wire 路径解析 + blob ref 白名单正则 + 目录穿越防护。
  - _验收：`tests/test_wire_redaction.py` —— 五类敏感 header 永不落盘；JSON path 与
    自由文本 pattern 命中；redactor 抛异常时输出 metadata-only + `redaction_failed`；
    policy 交集与降档。path traversal 用例进 `tests/test_wire_api.py` 预留。_

- [x] **W0-3 spool + blob writer**　★★
  - `backend/wire/spool.py`：`.partial` → rename 语义、append-only、单行大小上限、
    **逐行 flush**（SIGKILL 场景下已写行不丢，评审 m8 的 spool 侧保证）。
  - `backend/wire/writer.py`：canonical 原子重写；blob 临时文件 + fsync + content-addressed
    rename + 同 attempt 去重（design §16.3）。
  - _验收：`tests/test_wire_spool.py` —— 崩溃模拟（截断尾行）后 finalizer 读完整行并
    标 partial；并发两个 source 写各自 spool 不互扰；blob hash 命名/去重/codec 记录。_

- [x] **W0-4 lifecycle + injection 接线**　★★★（唯一侵入现有 adapter 的任务）
  - `backend/wire/lifecycle.py`：`WireCaptureSession.prepare()` 严格时序（design §9.1：
    spool → start event → source.start → ready probe → 合并 injection → ready event）、
    `AttemptObserver` phase 上下文、abort 路径。
  - `backend/adapters/base.py`：`AdapterRunInput.wire_injection` 字段（design §8.1，
    `capture_token` 不进 repr/序列化）。
  - CC/Codex 两个 adapter 的**消费点接线**（design §9.1 表）：CC `subprocess_env` 合并 +
    `_write_mcp_config` command 改写钩子；Codex `-c` 覆盖合并。本任务只做透传与合并,
    不启用任何真实 source。
  - `run_dispatch.dispatch()`：prepare 先于 run 的固定调用顺序;fail-open 时恢复原
    env/base_url。strict 通常仍只影响 capture completeness；只有 source 已选择改写
    base URL/command 且无法 ready 时，才在 agent 启动前抛 `CapturePreparationError`，
    记录独立 capture/infrastructure outcome，不伪装成 agent failure（design §9.1、§21）。
  - `ssh_claude_code.py` 显式标 `wire: not-applicable`（评审 m10）。
  - _验收：新增 `tests/test_wire_lifecycle.py` —— prepare/run 时序断言（source.start/ready
    先于 adapter.run 的调用记录）；injection 合并冲突（保留名、secret 校验）；
    prepare 失败 fail-open 降级后 adapter 拿到未污染 env；abort 路径 flush。新增
    adapter 单测分别断言：Claude 最终 subprocess env/base/custom headers/MCP config；
    Codex 最终 provider/MCP `-c` 参数与 subprocess env。最后运行完整后端测试套件，
    证明空 injection 行为零变化。_

- [x] **W0-5 finalizer + manifest + correlate（显式 anchor 部分）**　★★★
  - `backend/wire/finalize.py`：evidence→canonical 确定性映射表（design §8.2 表）、
    原子写 `wire.jsonl`、`correlation-map.json` 复用。
  - `backend/wire/correlate.py`：显式 anchor 链（producer call id / provider response id /
    proxy request id）；anchor 带 namespace/record type，MCP `jsonrpc_id` 只用于 MCP frame
    配对，禁止与 LLM logical call ID 交叉合并；heuristic 评分留接口、本任务不实现。
  - manifest 状态机（design §17：complete/partial/failed/not-applicable/in-progress/
    recovered）+ 双层 status + `policy.downgrade_reason` 字段（评审 nit4）。
  - phase 归属：source 写 evidence 时已通过启动参数、phase-state sequence 或 control channel
    固化 phase；finalizer 只校验显式 phase/sequence，不按 wall-clock 区间推断。状态缺失、
    过期或 attempt 不匹配时保留 `phase=unknown` 并从 `agent_run` 聚合排除（design §9.4）。
  - _验收：`tests/test_wire_manifest.py` + finalize 用例 —— fake source 写 evidence 后
    生成可读 canonical + manifest；「零通信」与「source 启动失败」manifest 可区分
    （R12.1）；verification/unknown phase 的 evidence 不进 agent_run 聚合（R3.6）；
    七类 evidence type 全部执行规定的映射行为：`aggregate_usage` 不伪造 call，证据不足的
    `compaction_hint` 不伪造 `context_compaction`；`correlation-map.json` 离线重建复用 ID。_

- [x] **W0-6 DB 摘要 + wire API + SSE**　★★
  - `backend/db.py`：`wire_status` 等五列幂等迁移（design §18）。
  - `backend/wire/api.py`：三条路由 + cursor 分页（signature 用 manifest finalize 计数
    `generation` 而非裸 mtime，generation 每次成功原子 finalize/rebuild 单调递增，评审
    nit5）+ blob policy/权限 + `not_available`。
  - `api.py` `_attempt_change_signature` 加入 `wire.jsonl`/`wire-manifest.json`；
    canonical wire/manifest 仅通过专用 Wire UI/API 访问，source spool 与 blob 排除普通
    artifact 列表；blob 只能走 capture policy/授权门控的 wire endpoint（R12.6，评审 m1）。
  - _验收：`tests/test_wire_api.py` —— 分页/过滤/409 wire_changed/404/traversal/
    policy 阻断 blob；老库迁移幂等；相同 record count 的 rebuild 仍递增 generation，旧
    cursor 返回 409 且 SSE 签名变化。_

- [x] **W0-7 startup recovery**　★
  - lifespan 扫描 `in-progress` manifest + attempt 已终态 → 重新 finalize 或标 failed
    （design §17 末）；与既有 `recovery.py` 的启动恢复共存不互扰。
  - _验收：`tests/test_wire_manifest.py` 增补 —— 模拟崩溃残留 spool 重启后
    `recovered`；无法恢复标 `failed`；不会重复 finalize 已完成 attempt。_

## W1 · Native events（design §23 Phase 1）

- [x] **W1-1 Claude normalizer + 最小 trajectory**　★★★
  - `backend/wire/normalizers/claude_code.py`：design §10.1 状态机（message id 合并、
    result 不算额外 call、无 id 时 sequence anchor + `inferred`）；把可见的
    message/system/tools/tool-result 映射到 W0-1 semantic IR。
  - trajectory 使用两阶段生成：normalizer 先按 producer event ref 生成稳定 step ID 和
    邻接；finalizer/correlator 建立 logical call 后写 step↔call link，最后执行
    referential-integrity check（design §10.6）。
  - 与 adapter 累计值对账：差异写 manifest conflict 不静默修正。
  - _验收：`tests/test_wire_claude_normalizer.py` —— golden fixtures（标注 CLI 版本）
    覆盖多轮/工具/重复事件/无 id/解析失败保留 raw+parser version；对同一 attempt
    重跑幂等（R2.1.7）；step 在 correlation 前后保持同一 ID，所有非空
    `trajectory_step_id` 均能解析。提交离线脱敏 fixture；49 样本仅用于补充 fixture，
    不作为 CI 前置。_

- [x] **W1-2 Codex 会话日志 spike**　★★（结论决定 W1-3 形态,时间盒 1 天）
  - 验证 `CODEX_HOME` 隔离下 `--ephemeral` 是否保留 internal session JSONL
    （design §10.2）；不行则验证去 ephemeral + 事后删 auth 的对照等价。
  - _验收：spike 结论 + 对照测试记录写入 design §27「暂不决定」的决议；
    确定 W1-3 的输入源优先级。_

- [x] **W1-3 Codex normalizer**　★★
  - `backend/wire/normalizers/codex.py`：`token_count` 关闭调用边界、
    `last_token_usage`、producer event type 保留（R2.1.5）；无法逐调用时 manifest
    标 `call_boundary=aggregate-only`（design §10.2、§27.1：首期即 aggregate-only）；可见 payload 映射到同一 semantic IR。
  - _验收：`tests/test_wire_codex_normalizer.py` —— fixtures 覆盖多调用/仅 aggregate/
    schema 漂移;aggregate-only 时不伪造曲线。_

- [x] **W1-4 canonical token 聚合回填**　★
  - `backend/wire/aggregate.py` 聚合 `phase=agent_run` calls →
    `token_usage_json` + `external_refs.token_usage_source=wire|adapter`（design §18）。
  - _验收：单测 —— 有/无 canonical calls 两分支;聚合值与 adapter 值冲突时双保留。_

- [x] **W1-5 offline rebuild**　★
  - `python -m backend.wire.rebuild <attempt_id>`：`.rebuild` 校验后原子替换
    （design §10.3）,不触碰原始 events。
  - _验收：用仓库内 Claude/Codex 历史 fixture 重建出调用级 `llm_call`，重复执行幂等且
    manifest generation 递增；可附加选取 49 上真实 Claude/Codex attempt 验证。_

- [x] **W1-6 Env Attempt Server inbound 采集（评审 m9,低垂果实）**　★
  - `env_attempt_server.py` 中间件：inbound 工具请求的 size/timing/attempt 归属写
    evidence（token 已在请求里,零新增鉴权）；每个 server/process 使用独立 spool writer，
    并发请求通过该 writer 串行 append。phase 从 lifecycle 管理的 attempt phase registry 在
    请求到达时快照；若 Env Server 独立进程，则使用显式 token claim/control channel。
    无法取得时写 `unknown`，禁止默认 `agent_run`。
  - _验收：单测 —— 一次工具调用产生一条 `http_exchange` evidence 且 attempt 归属
    正确；并发 attempts 不串 spool；token/attempt 不匹配拒绝写入；unknown phase 不进入
    agent_run 聚合；不影响现有 trace 写入（完整后端回归）。_

- [x] **W1-7 前端 M1：wire_status badge + token curve**　★★
  - `WirePanel`/`TokenCurve`（SVG polyline,无新图表依赖,design §20）;
    completeness banner;unmatched 单独分组;按需加载。
  - _验收：tsc + 生产构建;空态/aggregate-only/partial 三种降级态有明确文案;
    固定 API fixture 的曲线点与 canonical calls 精确一致；49 上真实 run 仅作附加手工验收。_

- [x] **W1-8 前端 M2：逐调用检查器 + aggregate/conflict 展开**　★★★
  - 在 `WirePanel` 的曲线下增加调用表，逐条展示 sequence/time、model、call role、
    input/output/cache-read/cache-write/reasoning token、finish reason、phase、correlation
    confidence；`null` 显示「未知」而不是 0。曲线点支持 hover/focus/click，能定位并
    高亮对应表格行；键盘也能获得相同信息，不能只依赖鼠标 tooltip。
  - Codex 等 `aggregate-only` source 使用显式累计卡片展示 input/output/cache/reasoning、
    producer event type 和能力边界；不得因为没有 `llm_call` 而只留一句弱提示，也不得把
    attempt aggregate 伪装成一次调用。
  - conflict 从 banner 数字展开为 adapter/native/result 三方值及 field source；明确区分
    「逐调用求和」「producer result aggregate」「adapter aggregate」，方便定位统计口径问题。
  - metadata 档只展示 token/hash/size 等元数据；真实 prompt/response 内容仍由 W4-5 的
    parsed/full payload viewer 提供，W1-8 不从 events 或日志绕过 policy 泄露内容。
  - _验收：Testing Library 固定 fixture 覆盖 24-call 表格、null≠0、曲线↔行联动、
    aggregate-only 卡片、三方 conflict 展开；以 `run_6ef6ca56e62c` 的 Claude attempt
    `att_0d878d8b0673` 验证 24 条调用可逐条读数，以 Codex attempt
    `att_f8d3749a7672` 验证 0 条伪调用且累计 1,131,205/9,584 可见。_

- [x] **W1-9 前端 M3：工具 hop 时间线 + trajectory 联动**　★★★
  - finalizer 把 evidence `payload.timing.duration_ms` 无损映射到 canonical
    `http_exchange.time.duration_ms`（当前原始 env-inbound evidence 有值但 canonical 丢失）；
    API 类型同步暴露 duration、direction、method/path、status、request/response bytes、
    streamed/partial、source、phase 和 correlation。
  - `WirePanel` 增加按时间排序的 hop 表/时间线；matched hop 挂到 logical call 与
    trajectory step，unmatched 明确单列并解释为何未关联。调用表可跳转到对应 trajectory，
    trajectory 也可回到 wire evidence；无关联时不伪造链接。
  - _验收：后端单测固定 timing 映射且 rebuild 幂等；前端 fixture 覆盖 matched/unmatched、
    duration/bytes/status、call↔hop↔step 跳转和缺 anchor 降级；用
    `run_6ef6ca56e62c` 验证 Claude/Codex 各 4 条 Env 工具请求（含 `task_brief`、
    `workspace_status`、`annotate_pptx`）能在页面逐条查看。_

**W1-7 只完成可观测性摘要 M1；W1-8/W1-9 完成后，用户才具备可读、可定位的调用级
体验。此时再把 W1 视为前端闭环，并做一次端到端验收 + 部署 49。**

## W2 · llm-gateway connector（design §23 Phase 2,外部依赖）

- [ ] **W2-1 外部 API contract 评审**　★（文档任务,**第一周就该并行启动**）
  - 按 design §11.1 与 llm-gateway 仓库对齐 calls/compactions 只读 API 的 schema/
    cursor/auth;明确 hash domain 声明（design §10.5 末段）。
  - _验收：对方仓库 issue/PR 链接 + 双方确认的 schema 样例落入本目录。_

- [ ] **W2-2 correlation header 注入**　★★
  - `merge_custom_headers`（design §11.2）:保留名胜出、Authorization 禁入、
    `x-user-id` 不覆盖；CC provider 在 attempt 启动前合并 attempt-scoped headers，明确
    不生成/复用进程级 `x-lane-call-id`。
  - _验收：单测覆盖大小写/冲突/secret 拒绝;49 实测 gateway 侧能按
    `x-eval-session-id` 查到调用。_

- [ ] **W2-3 connector 拉取 + 融合**　★★★
  - `backend/wire/sources/llm_gateway.py`:分页拉取、重试节奏、fail-open
    （design §11.3）;`normalizers/llm_gateway.py` 转 evidence。
  - heuristic correlation 评分实现（design §7.3 权重表）+ golden fixtures 固定阈值。
  - _验收：`tests/test_wire_correlation.py` —— 同一调用 native+gateway 只出一个
    logical call;token 冲突双保留（R2.2.3）;并行请求无显式 ID 时 unmatched
    不强配;gateway 不可用 attempt 照常完成。_

- [ ] **W2-4 前端 routing/TTFT/retry 展开**　★
  - calls table 增列 + call 展开 provenance/conflicts/hops（design §20）。
  - _验收：tsc;conflict 有 UI 呈现不被静默。_

## W3 · MCP stdio tap（design §23 Phase 3,系统编程风险区）

- [x] **W3-1 透明 pump 骨架**　★★★（先做,它验证整个 tap 的可行性）
  - `backend/wire/mcp_tap.py`:bytes pump、stderr 隔离、SIGTERM/SIGINT 传播进程组;
    **SIGKILL 路径显式不做优雅关闭,完整性由 spool 逐行 flush + `.partial` 恢复保证**
    （评审 m8）。tap 由 adapter 进程树管理；tap 创建 child 的独立 PGID/session，只向
    child PGID 转发信号，禁止把 tap 与 child 放入同一 PGID 后从 handler `killpg` 自己。
  - _验收：`tests/test_wire_mcp_tap.py` —— 回环测试字节级等价（含大 payload、
    跨 chunk）；分别覆盖正常退出、SIGTERM、timeout、tap SIGKILL、child crash，均无
    孤儿；spool 已写完整行可读；tap capture writer 故障（磁盘满模拟）不中断主通信。_

- [x] **W3-2 JSON-RPC 帧解析与配对**　★★
  - 换行 framing + 独立 buffer、`max_frame_bytes` 超限透明转发标 dropped、
    request map TTL（design §12.2）。
  - _验收：跨 chunk 单帧重组;request/response 按 id 配对、notification 无 id;
    server crash 保留 exit code;超大帧 dropped 计数进 manifest。_

- [x] **W3-3 CC/Codex command rewrite 接线**　★★
  - 经 W0-4 的 injection 钩子改写 MCP command（design §12.1,`--phase agent_run`
    显式传入);只包 agent-lane 注入的 server,不动用户全局 MCP 配置。
  - _验收：集成测试 —— 真实 env MCP server 包装前后工具行为等价（复用 t14 链路);
    tool call/result 的 size/truncated 至少一条可见;timeout/cancel 后无残留 wrapper
    或 MCP 子进程；若双方存在同一显式 tool ID 则精确对齐，否则保留基于 tool name/顺序/
    时间的 confidence，不要求虚构相同 `tool_call_id`。_

- [x] **W3-4 MCP 帧 → trace/trajectory 关联**　★
  - `mcp_frame` 的 `tool_name`/`jsonrpc_id` 关联到 env trace 行与 trajectory step
    （design §12.2 末、R7.5);tool result 回传形态初判(size 对比,内容判定留 W6)。
  - _验收：单测 —— request/result 保持两条 canonical `mcp_frame`，通过
    `paired_record_id` 配对并可挂到 trajectory step；显式 ID 缺失时输出关联 confidence；
    notification 无 id 时不误配，不引入 schema 中不存在的 logical-tool-call record。_

**W3-1~4 已实现（2026-07-14）**：`backend/wire/mcp_tap.py`（可执行 tap，透明双向
bytes pump + stderr 直通 + child 独立 PGID/session、SIGTERM/SIGINT 只转发 child、
SIGKILL 不做优雅关闭靠 spool .partial 恢复）；`backend/wire/mcp_frames.py`（换行
framing 解析、按 jsonrpc_id 配对、超帧 dropped 计数、fail-open）；
`backend/wire/sources/mcp_stdio.py` McpStdioSource（产 lane-<env> 的 CommandRewrite，
dispatch `_build_wire_sources` 给 CC/Codex 挂）；finalize `_associate_mcp_trajectory`
按 tool name/顺序把 mcp_frame 关联到 trajectory step（confidence=tool-name-order，
不虚构 ID），normalizer 补 trajectory step.tool_name。tests/test_wire_mcp_tap.py（14）：
字节等价（含 500KB/跨 chunk）、SIGTERM/crash exit code、配对/notification/超帧 drop、
capture fail-open、rewrite、trajectory 关联。后端 mcp/normalizer/manifest 全绿。
**W3-1~3 达成 → 最小完成目标（§28：W0+W1+(W2或W4)+W3-1~3+W1-8/9）齐了。**

## W4 · agent-lane reverse HTTP capture（design §23 Phase 4,系统编程风险区）

- [x] **W4-1 反向代理骨架 + 转发正确性**　★★★（W4-4 后执行，验证转发不改变行为）
  - `backend/wire/sources/http_proxy.py` + `api.py` 内部路由
    `POST /internal/wire-proxy/{attempt_id}/{provider}/{path:path}`（design §13.1):
    upstream 只从 server-side provider config 查(防 SSRF);capture token 授权
    (评审 m4,不复用 URL 中 attempt id);inbound credential 不转发。
  - `httpx.AsyncClient.stream()` 转发 + `StreamingResponse` 逐 chunk;bounded queue +
    单 writer task,**队列满优先保主通信、drop capture chunk 计数**(design §13.2);
    client 断开取消 upstream 并写 partial。
  - _验收：`tests/test_wire_http_proxy.py` —— fake provider 覆盖流式/非流式;SSE 逐
    chunk 转发不缓冲整包(用慢 fake 断言首 chunk 到达早于末 chunk);client 断开
    级联取消;SSRF 拒绝任意 upstream;queue 满时主通信不阻塞、drop 计数进 manifest。_

- [x] **W4-2 三协议 parser → 公共 semantic IR**　★★★
  - `normalizers/{anthropic,openai}.py`:Anthropic Messages / Chat Completions /
    Responses 三协议 request/response 解析，并映射到 W0-1 的公共 semantic IR/hash；
    本任务不另造协议私有 hash。
  - 解析失败仍透明转发,capability 退化 metadata(design §13.2 末)。
  - _验收：`tests/test_wire_hashing.py` 增补三协议 mapping —— 等价输入产出同一 semantic hash
    (cross-protocol golden fixture);key 序/空白/unicode 转义差异不影响 hash;
    工具列表按 name 排序;无完整语义内容时只写 bytes/null 不伪造 semantic hash。_

- [x] **W4-3 SSE timing + body policy/blob**　★★
  - 逐 chunk 异步记 timing → `stream_chunk` evidence(摘要 + full 档逐 chunk);
    body 按 effective policy 走 metadata/parsed/full;blob 复用 W0-3 writer。
  - W4 是固定 upstream 的透明 capture proxy，不实现 retry/failover/router。客户端自行 retry
    时每次入站请求各生成一条 `http_exchange`；只有显式 caller/provider anchor 足以证明时
    才关联到同一 logical call，否则 unmatched。gateway 内部 failover 明细留 W2。
  - _验收：单测 —— upstream 429 原样返回且记录一条 hop；模拟客户端再次请求时记录第二条
    hop，有共同显式 call anchor 时关联、无 anchor 时不强配；SSE 中断保留已观测 chunk + partial 标记;
    metadata 档不落 body,full 档 blob 可回读。_

- [x] **W4-4 provider 配置统一(canonical protocol + auth_mode)**　★★（W4 组先做）
  - `model_providers.py`:`kind` before-validator 接受新旧值统一为 canonical
    (`vllm-responses`→`openai-responses`,design §15.1);新增
    `auth_mode: bearer|api-key`(design §15.2),bearer/api-key 分别注入
    `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`,**不做 token 值转写**;
    `wire_api` deprecated 但校验一致性、否则 fail fast(评审 m5);codex 走非
    Responses provider 时 adapter 启动前 fail fast(评审 m6,R15.5)。
  - _验收：`tests/test_model_provider_protocol_migration.py` —— 旧 kind 规范化 +
    legacy-input test;既有 lane.yaml 零改动加载通过(现有 provider 测试回归);
    bearer/api-key 注入互斥且无转写;codex+anthropic-messages 组合被拒。_

- [x] **W4-5 前端 payload/blob 展示（policy 门控）**　★
  - _已完成（2026-07-14）：HopBody/BlobView 按 policy 区分——parsed 档只给「解析视图
    （已脱敏）」；full 档给「解析 / 协议原文」切换（可解析则默认 pretty JSON，否则退原文）。
    metadata 档明确「不采集报文正文」；blob 404 明确降级（不回退 events/stderr）；截断
    blob 显式「内容已截断/连续前缀」警告。与 W1-8 token 表联动、trajectory 锚点。测试
    覆盖 metadata（无 body）/parsed（无原文切换）/full（解析↔原文切换）三档 + 404 + 截断。
    tsc + build + vitest 29 passed。_

**W4-1~4 反代能力已实现并通过多轮评审（2026-07-14）**：`backend/wire/sources/http_proxy.py`
（转发引擎 + bounded queue + 单 writer + seal/drain registry）、`sources/parse.py`
（三协议 → 公共 IR + provider call ID 提取，覆盖非流式 JSON + SSE）、`proxy_api.py`
（路由 + capture token 授权 + SSRF + 413 上限）、`capture_token.py`；lifecycle
finalize/abort 调 close_attempt + revoke。评审修复：内部头/token 前缀剥离不转发、SSE
结构化脱敏 fail-closed（含非 data 字段 id/event/retry/comment 安全处理）、custom_headers
reserved denylist、压缩编码一致性、正文大小上限 + 截断连续前缀 + canonical/前端消费、
hop_id 一致、response 脱敏状态不掩盖、重复响应头 multi_items 保留、bytearray 读 body、
所有 policy 的 correlation probe（metadata 下也提 provider id 关联、不落 body）。
**W4-6a（Claude 第三方接线）已完成：dispatch 已注入 http-proxy source，Claude+第三方
provider 的 comparison run 会自动采集并把 hop 桥接到 native call。W4-5（前端 parsed 视图）
与 W4-6b（Codex 调用级 anchor）未完成——详见下方。**

- [x] **W4-6a Claude 第三方模型反代接线 + comparison capture policy**　★★★
  - _已完成（2026-07-14）：dispatch 已把 http-proxy source 注入 Claude+命名第三方 provider
    的 attempt。#1（Blocking）capture token 进真实请求头——CC 合进 ANTHROPIC_CUSTOM_HEADERS
    的 X-Lane-Capture-Token，加 adapter 配置→模拟 CLI 请求→proxy 授权 200 的 e2e 测试。
    provider call ID 关联：extract_provider_response_id 覆盖**非流式 JSON + SSE**
    （Anthropic message_start.message.id 等，流中断保留已得 id）；反代**同时**写
    provider_response_id + producer_call_id（同值，Anthropic msg_id 两者同源），correlate
    的 union-find 桥接反代 provider-response 空间与 CC normalizer 的 producer-call 空间——
    单边会落进不相交 namespace 永远 unmatched。**所有 policy 均有 correlation probe**：
    metadata 默认档也在内存扫前几个 SSE 事件提 id（限量、绝不落 body），保证默认模式下
    call↔hop 也能联动。用**真实 ClaudeCodeNormalizer 输出 + 默认 metadata 档**验收桥接
    （不人工塞字段）。capture_policy 只做 run 级（task 级留待）。_

- [ ] **W4-6b Codex 调用级 anchor（provider call anchor）**　★★（阻塞于上游能力）
  - **代理侧已完成**：反代从 Codex 的 Responses SSE（`response.created`/`response.completed`
    的 `response.id`=resp_...）提取 provider_response_id 写进 hop（见 test_codex_responses_
    hop_carries_resp_id）——hop 已是 Codex 调用最细粒度的记录，携带 provider/model/protocol/
    body。
  - **阻塞点（上游能力缺口，非本层 bug）**：Codex CLI 的 stdout 事件流只有 turn 级 aggregate
    （`turn.completed` 仅 usage、无 id），**根本不暴露逐调用 resp_id**，且 Codex 是
    `aggregate-only`——normalizer 产 AggregateUsageEvidence、无逐调用 native llm_call。因此
    **没有 native 端锚点可供桥接**：不是 normalizer 没解析，而是 Codex CLI 事件里就没有这个
    数据。故 Codex hop 目前 unmatched（能看 HTTP hop 与原始交互，但挂不到 native call/
    trajectory）。
  - _待上游：需 Codex CLI 在事件流暴露逐调用 response.id（或每个 model 调用产独立事件带 id），
    normalizer 才能补 producer_call_id 与反代 hop 桥接。当前用 provider_response_id 关联到
    turn aggregate 也不成立（aggregate 不参与 logical-call 对账）。留待 Codex 侧能力就绪。_
  - _已实现（2026-07-14）：`backend/wire/sources/http_proxy_source.py` HttpProxySource
    （CaptureSource，start 签发 capture token + 构造 proxy base URL injection，token 走
    WireInjection.capture_token 专用字段——LANE_WIRE_CAPTURE_TOKEN 含 "TOKEN" 会被
    _validate_injection 当 secret 拒，故不进 process_env）；CC/Codex adapter 读该字段注入
    子进程 env、base URL 由 wi.llm_base_url 覆盖；dispatch `_build_wire_sources` 用
    parse_model_ref 判定命名 provider，只给 CC/Codex 挂 source，官方 provider 不挂；
    run API（CreateRunRequest）+ LaneSection 加 capture_policy / wire_capture_max_policy，
    resolve_effective_policy 求最严格交集固化到每 attempt。close/revoke 由 lifecycle
    attempt_end/abort 兜底。tests/test_w46_proxy_wiring.py（13）+ e2e 验证 prepare→finalize
    token 生命周期。后端全量 635 passed（4 基线失败无关）。_
  - **能力边界（选项 A，评审 #9）**：HTTP 反代只能观测**能被改 base URL 指到代理、且
    走 HTTP/1.1+SSE 的进程**——即 Claude Code / Codex 子进程访问命名第三方 provider 的
    LLM 流量。因此本任务只给 **CC/Codex 且模型是 `provider/model`
    命名第三方 provider** 的 attempt 注入代理；官方默认 provider、历史 run
    一律显式标 transport 未观测，不伪造原始报文。
  - dispatch 在解析出 CC/Codex 的 `provider/model` 后，为命名第三方 provider 启用真实
    `lane-http` source：生成 attempt-scoped proxy base URL 与短期 capture token，并通过
    `WireInjection` 在 `adapter.run()` 前注入。Claude Code 使用受控动态 header；Codex 使用
    attempt-scoped credential env key；真实 upstream credential 只由 proxy 从 server-side
    provider config 注入，不进入 URL、wire 或 agent 可见的 capture metadata。
  - **run API** 增加 `capture_policy=off|metadata|parsed|full`，与 server maximum 求最严格
    交集（评审 #3：当前只做 run 级；task 级 policy 需 TaskModel 字段 + task JSON 持久化，
    留待有 per-task 需求时再加。resolve_effective_policy 已预留 task_requested 参数）。
    fan-out（same-model 下多个 agent、或同一 agent 多个 attempt）的每个 attempt 固化相同
    requested policy 和各自 effective policy。policy 对所有 attempt 生效，但只有走代理的
    CC/Codex attempt 才有 body 可采（如实标未观测，不降级为错误）。默认仍为 metadata，
    只有显式 full 才保存写盘前脱敏的协议原生 request/response；SSE 保存完整事件流 blob +
    chunk timing，中断保存已收到部分并标 partial。
  - 每个 transport hop 携带 provider/protocol/model/body refs，并尽可能用 provider response ID
    关联 native `llm_call`；无法关联仍可按 attempt 查看正文，但不得按时间伪配成调用。
    默认官方 provider 和历史 run 未经过 proxy 时明确显示 transport 未观测，
    不从 `events.jsonl`/stderr 回填伪“原始报文”。
  - _验收：真实 dispatch 集成测试覆盖 Claude+Anthropic Messages、Codex+Responses；并发两个
    CC/Codex attempt，断言 proxy URL/token/blob/provider 完全隔离、prompt 不串、token 在
    finalize 后失效；metadata 零 body、full 可从 W4-5 查看脱敏后的 system/messages/tools/
    assistant output；429/retry/SSE partial 保持主通信语义，验证能力边界。_

## W5 · Sandbox transparent redirect spike（design §23 Phase 5,高风险)

- [ ] **W5-1 沙盒透明重定向 spike（metadata 档）**　★★★（时间盒)
  - 仅在 agent-lane 可控 container 拓扑下:shared-netns sidecar + nftables redirect
    (design §14.1,不改宿主机全局 nftables);metadata 档 SNI/target 元数据不解 TLS。
  - _验收：spike 报告 —— 受控容器内出站 TCP 被无感重定向、采到连接元数据;
    task 显式声明 network_mode 的 service 列入 manifest excluded、coverage 不报
    complete；control channel/phase-state 热切换产生带 sequence 的 `phase_change`，状态
    缺失/过期时 evidence 为 unknown；沙盒不可控时明确 `not-applicable`。_

- [ ] **W5-2 MITM CA capability matrix spike**　★★（gated 在 W5-1)
  - parsed/full 档:ephemeral CA、私钥仅 sidecar、公开 cert 注入 workload;
    逐 TLS 栈(Node `NODE_EXTRA_CA_CERTS`/Rust/OpenSSL/系统 CA)信任探测
    (design §14.2)。
  - _验收：capability matrix 文档 —— 每类被测进程 CA 注入成功/失败/pinning 降级
    结论;失败自动降 metadata、cleanup 删 key/cert 只留 fingerprint。_

## W6 · 完整分析与扩展（design §23 Phase 6）

- [x] **W6-1 compaction detector**　★★
  - `backend/wire/aggregate.py`:相邻 main call 的 token/message/hash diff 分型
    (design §10.4);`context_compaction` record + versioned analyzer config;
    区分「压缩」与「新 session」(按 producer_session_id 分段)。
  - _验收：单测 —— 四档 confidence(explicit/high/medium/low)+ new-session 不误判;
    分型(selective-summary/sliding-window/full-summary)靠 semantic hash diff
    (依赖 W0-1 semantic hash 与 W1 source mapping；domain 不同降 size 启发并降 confidence)。_

- [x] **W6-2 context / tool-result / 并发度曲线**　★★
  - input/cache token、message count/bytes、tool-result size 曲线;工具结果回传
    形态判定(full/truncated/summarized/reduced,design §10.4 末);并发度由
    call 区间重叠算(仅完成时间的 native call 只报 sequence)。
  - _验收：单测 + 前端 —— 曲线从 canonical wire 重建不写主 DB;回传形态四态有
    证据支撑不臆断 summarized;并发度对 native-only 降级为 sequence。_

- [x] **W6-3 sub-agent topology**　★
  - 子 agent 独立 `agent_id` + `parent_agent_id`,独立 trajectory 由父 step 引用
    (R2.2.5、R4.8);不压成普通 tool result。
  - _验收：单测 —— 含子 agent 的 fixture 产出父子分离的 trajectory + correlation。_

**W6-1~3 已实现（2026-07-14）**：`backend/wire/compaction.py`（被动检测：按
(agent_id, session) 分段避免 new-session 误判；相邻 main call token/message/hash diff
分四档 confidence high/medium/low；versioned AnalyzerConfig 阈值；strategy 靠 size 启发
——逐消息 hash 缺失时不臆断，full-summary/unknown）；finalize 集成产 canonical
context_compaction record。`backend/wire/curves.py`（从 canonical wire 派生 context/
tool-result size/并发度曲线，纯函数不写主 DB；回传形态 full/truncated/error 有证据支撑、
truncated 不可得时 unknown 不臆断 summarized；并发度 native-only 无 started_at → 降级
sequence，有区间 → interval 峰值）。sub-agent topology：normalizer 读 CC 事件的
parent_tool_use_id → 子 agent 独立 agent_id（sub-<tool_use_id>）+ parent_agent_id，call
与 trajectory step 父子分离不压平；finalize 从 extensions 读 agent_id 填 canonical。
测试：test_wire_compaction(11)/test_wire_curves(7)/test_wire_subagent(4)。
_W6-2 前端曲线渲染留作后续（后端派生已就绪，前端可经新 API 消费）。_

- [~] **W6-4a Responses compatibility source（experimental API complete）**　★★★
  - Responses↔Chat 转换作为独立 source/sidecar，不内嵌 adapter/schema（R15.6）；
    是否启用不依赖 MITM CA capability。
  - _**实验性接口完成，非运行时完成**（2026-07-14）：`backend/wire/sources/
    responses_compat.py` ResponsesCompatSource 提供 recorder + 数据契约，但**尚未接入
    真实转换器/gateway/lifecycle**——用户跑 run 不会自动生效，接入真实转换层后才是
    运行时能力。首版有意收窄：只记 request + semantic summary（不落 response body、
    不做 parsed/full blob、无 timing/stream）。转换层调 record_conversion，产两条独立
    http_exchange evidence（inbound 转换前 + outbound 转换后，各自 direction/status），
    两跳共享同一 conversion request_id（uuid4 全局唯一）→ finalizer union-find 用
    proxy-request anchor 并成**同一 logical call**（R15.7）。两跳各按协议走 W4-2 parser
    算 request semantic summary、跨协议**同 hash**。协议名走 extensions 不占 schema。
    coverage llm_transport 轴识别 responses-compat。测试 test_wire_responses_compat.py
    （10，含 conversion_id 全局唯一、inbound/outbound direction+status、fake-converter
    真做 Responses→Chat 转换的集成测试）。_
  - _待运行时完成：接入真实转换器 + 补 response body / parsed-full / timing。_

- [ ] **W6-4b HTTPS MITM parsed/full 落地**　★★★（gated 在 W5-2）
  - 仅在 CA matrix 通过的进程接入 W4 parser；pinning/信任失败自动降 metadata。
  - _验收：通过的 TLS 栈能解析并按 policy 记录；未通过的 TLS 栈不拦断主通信且 manifest
    明确 downgrade reason；cleanup 后无 CA private key 残留。_

## W7 · RunDetail Office 产物预览（横向前端能力，非 wire blob）

> 当前 artifact API 把除图片/音视频外的文件统一当 UTF-8 文本返回，`.pptx/.docx/.xlsx`
> 会显示压缩包乱码。W7 面向 agent 生成的业务产物，复用 artifact 权限与路径边界，但与
> W4-5 的 LLM request/response blob 严格分离；Office 原文件始终保留下载入口。

- [x] **W7-1 Office preview contract + 安全边界**　★★★
  - _已完成（2026-07-14）：已完成 `lane-artifact-preview-v1` descriptor、内容/MIME 分类、
    OOXML bounded ZIP preflight、宏/外链/路径穿越/zip bomb 检测、原文件字节级下载、统一前端
    loading/error/unsupported shell 与 ADR；artifact list/download/preview 已统一校验 run→attempt，
    阻断隐藏/dot/framework 路径和文件/目录/workspace symlink，扫描、hash、正文读取已移出事件
    循环。PPTX/DOCX/XLSX renderer 均运行于独立 `python -I` worker：最小无凭证环境、禁
    socket、POSIX
    CPU/内存/文件/FD 限制、15 秒父进程硬超时、32 MiB 输出上限；timeout/crash/非法输出映射
    稳定错误且不缓存，原文件仍可下载。成功/确定性失败按 contract+renderer+源文件复合 hash
    原子缓存到 framework `artifact-previews/`，拒绝 cache root/entry symlink；renderer 只读取
    与 hash 一致的临时快照，文件仍在写入时返回可重试 `artifact_changed`。>20 MiB Office 文件
    使用 bounded background executor（2 workers/256 jobs）和同 path+content key 去重，首次
    返回 `rendering + poll_after_ms`；队列满稳定返回 `renderer_queue_full`。前端按 hint 轮询，
    切换/关闭文件会 abort fetch 与 timer，最多 120 次后明确失败。未来若接 LibreOffice，
    必须沿用同一部署级 sandbox contract；它只是像素保真增强，不阻塞内建静态 renderer。_
  - 定义 preview descriptor/API：真实 MIME（扩展名只作提示）、原文件 ref、preview 状态
    (`ready|rendering|unsupported|failed`)、页/slide/sheet 数、renderer/version、错误码、
    缓存 key；决定同步小文件与异步大文件的阈值及 SSE/轮询更新方式。
  - 选定服务端转换/浏览器渲染边界并写 ADR：优先复用可靠 renderer，不允许浏览器直接把
    OOXML ZIP 当文本。转换进程必须有 timeout、CPU/内存/解压大小/文件数限制，阻止 zip
    bomb、路径穿越、外链拉取；禁止执行 VBA、公式、OLE/ActiveX、嵌入脚本和宏。
    `.pptm/.docm/.xlsm` 首期只允许安全静态预览或下载，不执行宏。
  - _验收：contract fixture + 安全测试覆盖伪扩展名、损坏 ZIP、zip bomb、`../` entry、
    external relationship、宏文件、超大文件、renderer timeout/crash；失败只影响预览，
    不影响原文件下载和 RunDetail 其他区域。_

- [x] **W7-2 PPT/PPTX slide viewer**　★★★
  - _完成范围（2026-07-14）：`.pptx/.pptm` 经隔离 worker 生成 bounded static layout IR，
    保留 slide 顺序/尺寸/宽高比、元素坐标、文本角色与字号、表格、安全 PNG/JPEG/GIF 和
    speaker notes；宏、OLE/ActiveX、外链、SVG 均不执行/不加载。前端包含缩略图、上一页/
    下一页、页码、缩放、适应窗口、全屏、原文件下载以及三 Agent 页码锁定。21-slide fixture
    固定顺序，DTD/entity 与危险图片类型 fail-closed。由于本机/部署未提供 LibreOffice，
    theme/group transform/图表/动画/切换效果明确列 capability gap，不冒充 Office 像素级保真；
    旧 `.ppt` 仍 download-only。_
  - 后端把 `.pptx` 生成有序 bounded static layout IR（未来可升级为安全 PDF page），保留
    slide count、宽高比、页码、元素坐标和安全栅格图片；老 `.ppt` 标 unsupported，不静默乱码。
  - 前端提供缩略图栏、上一页/下一页、缩放、适应窗口、全屏、当前页/总页数和原文件下载；
    三 agent 对比时可锁定相同页码并排查看，缺页独立占位。
  - _验收：固定 PPTX 覆盖横版/竖版、文本、表格、安全图片、中文和备注、20+ slides；
    DOM/结构快照验证无空白页、错序、拉伸。图表、透明度/theme、动画/切换在没有可信
    LibreOffice renderer 时必须显式列 gap，不伪造内容；真实 `polished.pptx` 作手工验收。_

- [x] **W7-3 DOC/DOCX document viewer**　★★★
  - _完成范围（2026-07-14）：`.docx/.docm` 经隔离 worker 输出 semantic document IR，按原文
    顺序展示标题、段落、粗体/斜体 run、列表、表格、安全 http/https/mailto 超链接、页眉页脚
    和页面尺寸；`javascript:` 等危险 URL 被丢弃，外链从不自动加载，DTD/entity fail-closed。
    前端提供连续滚动、缩放、文本搜索/匹配块、原文件下载；批注/脚注/修订和嵌入图片以
    capability gap/警告显式降级，分页标 approximate，旧 `.doc` download-only。React 结构化
    渲染不使用 `dangerouslySetInnerHTML`。_
  - `.docx` 转安全结构化 DOM/连续 preview，展示标题/段落、列表、表格、页眉页脚和安全
    超链接；图片、批注/修订、目录、脚注若不支持必须显式列 capability gap。
    老 `.doc` 明确转换或 unsupported。
  - 前端支持页导航/连续滚动、缩放、文本搜索、页码和原文件下载；HTML 必须 sanitize，
    禁止脚本、事件属性、危险 URL 和外链资源自动加载。
  - _验收：固定 DOCX 覆盖中文、列表、表格、页眉页脚、安全/危险超链接和修订/批注降级；
    DOM/snapshot + XSS fixture，原文顺序与表格结构可核对，未渲染图片明确计数。_

- [x] **W7-4 XLS/XLSX workbook viewer**　★★★
  - _已完成（2026-07-14）：已完成 XLSX 安全结构化预览纵向闭环：后端仅解析通过 OOXML
    preflight 的已知 workbook/worksheet/shared-string/style 部件，拒绝 DTD/entity，不跟随外链，
    公式只返回公式文本与文件已有 cached value、绝不计算；sheet/row/column/cell/XML/shared
    string/merge 均有硬上限并显式标 truncated。descriptor `status=ready` 内返回 bounded workbook
    IR，解析运行在 W7-1 隔离 worker并使用复合内容缓存；number format 支持日期/时间/百分比/
    常见货币 display value，原始数值仍保留。前端提供 sheet tabs、冻结表头/行列坐标、值/公式
    搜索、缩放、0/空值/公式错误区分、超过 100 行的窗口虚拟化、截断提示、原文件下载和
    三 Agent sheet 锁定。10,001-row fixture 验证安全截为 500 rows 且显式 truncated。_
  - 解析 workbook/sheet metadata、单元格显示值、类型、公式文本、合并单元格、行列尺寸和
    基础样式；公式只显示已有 cached value/公式字符串，绝不在服务端或浏览器执行。
    外部链接、宏、数据连接、Power Query/pivot refresh 禁止执行并显示 capability gap。
  - 前端提供 sheet tabs、冻结表头、行列坐标、虚拟滚动、搜索、缩放和原文件下载；明确
    区分空单元格、0、公式错误和截断。设置最大 sheet/row/column/cell 上限，超限时提供
    安全的局部预览与说明。
  - _验收：固定 XLSX 覆盖多 sheet、中文、日期/货币/百分比、公式 cached value、合并单元格、
    隐藏行列、10k+ rows、损坏公式和外部链接；断言不执行公式/宏、虚拟滚动不卡主线程。_

- [x] **W7-5 ArtifactPanel 统一接入 + 对比体验**　★★
  - _已完成（2026-07-14）：八类 artifact 使用统一 preview shell 和准确 MIME/type；Office
    loading/rendering/error/unsupported/ready、后台 polling、能力缺口、原文件下载统一处理。
    快速切换会 abort 旧 fetch/timer，不出现陈旧 preview。compare 页保持每个 attempt 独立
    viewer state；显式开启同步后 PPT 页码/XLSX sheet 才跨三 Agent 联动，关闭时互不串状态。
    Testing Library 覆盖 PPTX、DOCX、XLSX、polling、快速切换、虚拟行窗口和隔离/同步。_
  - artifact list 返回准确类型 `presentation|document|spreadsheet|image|video|audio|text|binary`，
    未知 binary 不再 `read_text(errors="replace")`；`ArtifactsPanel` 使用统一 preview shell，
    包含 loading/progress/error/unsupported/download，并在切换 attempt/file 时取消旧请求。
  - 三 agent 产物支持同类型并排比较；PPT 按页、DOCX 按页/章节、XLSX 按 sheet/区域保存各自
    viewer 状态，不把一个 attempt 的选中页串到另一个 attempt。
  - _验收：API MIME/范围/权限/path traversal 测试；Testing Library 覆盖三种 Office 类型、
    loading/error/fallback/download、快速切换无陈旧内容；生产构建通过，真实 run 的原文件
    下载结果与 preview 前完全一致。_

---

## 关键路径与并行建议

- **立即启动(第一周)**:W2-1 外部协商任务尽早推进——对方响应
  周期决定 W2 何时解锁,晚启动就是纯等待。
- **主线**:W0 → W1(到此即达成 roadmap P0 核心产出,建议部署 49 做端到端验收)。
- **W1 后按价值/风险分叉**:W2(gateway,等外部 API)、W3(MCP,系统编程 spike)、
  W4(反代,系统编程)、W5(沙盒/CA spike)可并行,互不阻塞。
- **W6 gated**:W6-1~3 在 W1 source mapping 后可做；W6-4a
  不依赖 CA，W6-4b 等 W5-2 CA matrix。
- **W7 可独立并行**：先做 W7-1 安全/renderer ADR，再并行 PPTX、DOCX、XLSX；它复用
  artifact API，不依赖 W2/W3/W4，也不得复用 wire blob 权限绕过 artifact 边界。
- **高风险任务已前置 spike**:W1-2(Codex 日志)、W3-1(透明 pump)、W4-1(SSE 转发)、
  W5-1/W5-2(沙盒/CA)都是"先验证可行性再投入实现"的时间盒任务,结论不乐观时
  对应 Phase 降级而非硬扛。

## 完成定义(对齐 design §28)

最小完成 = W0 全部 + W1 全部 + (W2 或 W4 之一) + W3-1~3 + W1-8/W1-9
调用检查器。达成后:
CC/Codex 有调用级 `llm_call`(或诚实 aggregate-only)、MCP tool result size 可见、
至少一种 transport source 与 native call 关联、RunDetail 可逐条读取 call/hop/token/conflict、
默认 fail-open+metadata 无 secret 落盘、两个并发 attempt 不串、移除 gateway 后
Foundation/native/MCP/历史 UI 仍工作。W5(沙盒 MITM)、W6-4b(HTTPS)
属后续 coverage,不卡基础层。W7 是业务产物体验的独立完成门，不阻塞 wire 基础层，
但在宣称 RunDetail 可完整验收 PPT/Word/Excel 任务前必须完成。

## 验证记录

> 每完成一个任务,在此补一条:任务号、日期、验收命令与结果、49 实测(如适用)、
> 偏离设计的决议。

最低命令基线：

```text
uv run pytest <该任务列出的 tests/test_wire_*.py> -x
uv run pytest -q
npm --prefix web run build               # 涉及前端时
git diff --check
```

### W0-1（2026-07-10）

- 新增 `backend/wire/` 包：`ids.py`（uuid5 确定性 ID，含 NUL 分隔防拼接歧义）、
  `hashing.py`（semantic IR + NFC + RFC 8785 JCS + SHA-256，依赖新增的 `rfc8785==0.1.4`）、
  `models.py`（canonical envelope + 六类 record data，`extra="allow"` 向后兼容读取）、
  `evidence.py`（WireEvidence v1，`extra="forbid"` 跨进程契约 + `extensions` 命名空间口子）。
- 导出 `docs/specs/wire_observability/wire-evidence-v1.schema.json`（`additionalProperties:false`），
  供后续 Go/Node sidecar contract test 引用同一份 schema。
- 验收通过：`uv run pytest tests/test_wire_models.py tests/test_wire_hashing.py` → 30 passed；
  ruff 干净；现有测试无回归（抽样 t06/mm01 通过，未跑需 SELECTED_SKILLS_DIR 的集成用例）。
- semantic hash 的 IR→hash 是协议无关部分；各协议→IR 映射按计划留 W1（native）/W4（HTTP parser）。
- 无偏离设计。

### W0-2（2026-07-13）

- 新增 `backend/wire/policy.py`（四档 rank + `resolve_effective_policy` 最严格交集，
  requested=task/run 中更严者，降档时记 `downgrade_reason=server_max|source_capability`
  供 W0-5 manifest nit4 使用）、`redaction.py`（五类 header 黑名单值替换、默认 JSON key
  pattern 递归脱敏、自由文本 secret pattern，`scrub_text` 供日志/错误消息共用满足 R11.9/m7；
  `safe_redact_payload` 异常收敛 metadata-only + `redaction_failed`，错误消息自身先 scrub）、
  `paths.py`（wire 布局单一出口、blob ref 白名单 `sha256-<64hex>.json.(gz|zst)`、
  attempt_id/instance 分量白名单 + resolved-path containment 双重穿越防护）。
- 正则用 `\Z` 而非 `$`（`$` 接受尾部换行，会给文件名注入留缝）——测试
  `test_blob_ref_whitelist` 的尾部注入用例暴露了这一点。
- 验收通过：`uv run pytest tests/test_wire_redaction.py` → 29 passed；ruff 干净。
  path traversal 用例已在本文件预演，W0-6 API 层再补 HTTP 404 断言。
- 无偏离设计。

### W0-3（2026-07-13）

- 新增 `backend/wire/spool.py`（`.partial` → close 时 rename `.jsonl`；append-only、
  逐行 flush 满足 m8 的 SIGKILL 存活；单行上限超限抛 `SpoolLineTooLarge` 拒写不截断；
  `read_spool` 跳过截断尾行/损坏中间行并如实报 partial/truncated_tail/parse_errors；
  重开同 instance 追加不覆盖，供 recovery）、`writer.py`（`atomic_write_bytes/jsonl/json`
  临时文件+fsync+rename 原子重写；`BlobWriter` 对**未压缩 JSON 字节**算 SHA-256 命名
  `sha256-<hex>.json.gz`，gzip `mtime=0` 产物可复现，同 attempt 同内容去重，
  `BlobRef` 记录 hash/raw/stored/codec/dedup 满足 R11.4）。
- 验收通过：`uv run pytest tests/test_wire_spool.py` → 15 passed（含崩溃截断尾行、
  双 source 并发线程写不互扰、blob 命名/去重/gzip magic 回读）；ruff 干净。
- 无偏离设计；zstd 依赖按 §16.3 留待后续，codec 字段已自描述。

### W0-4（2026-07-13）

- 新增 `backend/wire/injection.py`（WireInjection/CommandRewrite/PhaseStateRef 冻结数据对象，
  `capture_token` repr=False；PhaseStateRef 强制单 transport；不 import adapter/lifecycle
  避免循环依赖）、`lifecycle.py`（`WireCaptureSession.prepare()` 严格时序
  spool→start event→source.start→ready→合并→ready event；`merge_injections` 标量冲突不
  last-wins、保留名/secret 校验（RESERVED_ENV_KEYS + DEFAULT_KEY_PATTERN + BLOCKED_HEADERS）、
  capability gap 在 agent 启动前丢弃并登记；`AttemptObserver`/`NullAttemptObserver`；
  abort 保留 `.partial`；`ADAPTER_CAPTURE_CAPABILITIES` 静态 registry，ssh-claude-code
  显式 `{"wire": "not-applicable"}` 落实 m10，adapter 类加 `WIRE_SUPPORT` 注记）。
- 接线：`AdapterRunInput.wire_injection`（默认零注入）；CC 在 subprocess_env 构造后消费
  process_env/llm_base_url/llm_headers（合入 ANTHROPIC_CUSTOM_HEADERS），
  `_write_mcp_config` 应用 mcp rewrite（wrapper 前置、原命令后置）；Codex
  `_provider_cli_args(model_ref, injection)` 覆盖本次 base_url、
  `_mcp_command_and_args` 应用 rewrite（cmd 与 snapshot 同源）、最后合并 process_env。
  `runner.run_attempt` 增可选 observer，
  全流程包外层 try/finally 保证 early return 也走 `attempt_end()`（fail-open 吞错）；
  `dispatch()` prepare 固定先于 run，`CapturePreparationError` 记
  `capture_preparation_failed` 独立 outcome，异常路径 abort。
- 验收通过：`uv run pytest tests/test_wire_lifecycle.py` → 26 passed（时序/合并冲突/
  保留名/fail-open 未污染 env/strict fail-closed/abort flush/adapter 消费点/空注入零变化）；
  ruff 干净；`SELECTED_SKILLS_DIR=… uv run pytest -q` 全量回归与 HEAD 基线一致
  （仅 4 个遗留失败：ad_placement×2、t16 carrier、t17 codex `env_token not in cmd`——
  已用 git stash 验证在 HEAD 即失败，与本任务无关；t17 暴露的 env_token 进 `-c` 参数
  是既有行为，待单独 issue 处理）。
- 无偏离设计；codex `llm_headers` capability 声明为 False（design §8.1 表未定义该通道，
  W2-2 再评估）。

### W0-4 评审修复（2026-07-13，第二轮）

评审发现两项缺陷，全部修复并补回归测试（`uv run pytest tests/test_wire_*.py` →
123 passed；adapter/dispatch 定向回归 45 passed + 遗留 t17）：

1. **injection 可覆盖 provider credential + header 注入**：
   - `merge_injections` 增加 `protected_env_keys` 动态保护集；dispatch 把
     `settings.model_providers` 全部 `api_key_env`（任意名称如 UP_KEY）传入；
   - header name 强制 RFC 7230 token、value 拒绝 CR/LF/控制字符（CC 换行拼接
     `ANTHROPIC_CUSTOM_HEADERS` 的注入面）；process_env value 同样拒控制字符；
   - header merge 大小写不敏感（同名不同值→冲突，同值合一保留首见大小写）；
   - CC `_merge_custom_headers` 解析既有 "Name: value" 行去重，静态保留 header
     胜出（§11.2），同名注入丢弃而非追加；
   - `llm_base_url` 校验：http(s) 绝对 URL + 安全字符集（拒引号/反斜杠/空白/
     控制字符），同时封死 Codex `-c ...base_url="…"` 的 TOML 逃逸；
   - 所有新正则用 `\Z` 不用 `$`（尾部换行缝）。
2. **WireEvidence 升级为真正的严格跨进程契约**（属 W0-1 范围的返工）：
   - envelope 改为按 `evidence_type` 的 discriminated union（7 个 variant 类），
     payload 是封闭 versioned 模型（§8.2 表的最小字段，不可得写 null）；
   - `phase`/`evidence_schema_version` 改 Literal，`phase="totally_invalid"` 等被拒；
     evidence.Phase 与 models.Phase 一致性有测试防漂移；
   - `extensions` key 强制 namespace 前缀（`x-<ns>.`）validator；
   - `EvidenceRedaction` 补 `hash_algorithm`（Literal["sha256"]）；
   - capture_event payload 补 `source_instance`/`message`，lifecycle 写入改用
     `CaptureEventEvidence` + `CaptureEventPayload`（不再裸 dict kwargs）；
   - 重新导出 `wire-evidence-v1.schema.json`（oneOf + discriminator，全部对象
     $defs additionalProperties=false），schema 测试改为逐 $defs 断言。

### W0-1~W0-4 评审修复（2026-07-13，第三轮）

Blocking×2 + Major×6，全部修复；wire 测试 135 passed，dispatch/adapter 定向回归
44 passed，schema 文件重新导出。

- **B1 WireEvidence 可执行契约**：payload 最小字段与 envelope 的
  `raw_ref/correlation_hints/capabilities/errors/extensions` 全部改
  **required-but-nullable**（无默认值——省略即校验失败，不可得必须显式 null）；
  `redaction.policy` 改 Literal 四档；`SpoolWriter.append` 对 dict/模型一律先
  `validate_evidence()`，并新增 `expected_attempt_id`（attempt 归属）与
  `max_policy`（redaction.policy 越档拒写）校验，违规抛 `SpoolValidationError`；
  新增 `null_payload()` 模板；测试改为断言空 payload / 缺单字段 / 缺 envelope
  字段 / `policy="save-all-secrets"` 均被拒。
- **B2 capture prepare 失败独立终态**：dispatch 改写
  `status="capture_infrastructure_failed"`（新终态，进 `_refresh_run_status`
  terminal 集合）（R14.2）。
- **M1 header 优先级**：`x-eval-*`/`x-lane-*` 前缀 attempt 值胜出（覆盖静态
  旧值），其余同名静态保留；有 stale `X-Eval-Session-Id` 被覆盖的测试。
- **M2 phase_state 实装**：prepare 后原子写
  `wire-sources/phase-state.json`（attempt_id/phase/sequence/updated_at），ref
  自动填入 `injection.phase_state`；`phase()` 进入前先原子更新、退出时恢复并
  传播，sequence 单调；写失败降级 `phase_attribution=degraded` + gap +
  capture_event，不中断主流程。
- **M3 spool 文件名歧义**：`<kind>-<instance>` 有确定性碰撞（`a-b`+`c` ==
  `a`+`b-c`），改 `<kind>@<instance>.jsonl`（`@` 不在分量白名单字符集，拼接
  无歧义）；design §5/§8.3 已同步更新。
- **M4 重开丢历史**：SpoolWriter init 处理三态——仅 `.partial` 追加；仅
  `.jsonl`（正常关闭后重开）先 rename 回 `.partial` 再追加；两者并存合并为
  final→partial 顺序。补 clean-close 重开与双文件合并测试。
- **M5 统一 abort 边界**：dispatch 把 prepare 之后到 `run_attempt` 返回的整段
  （AdapterRunInput 构造、materials/uploads 拷贝、approval seed、bound/scorer
  构造）包进单一 try/except BaseException → abort；abort 对已 finalize 的
  session 是 no-op（幂等增强）。
- **M6 strict 范围收窄**：merge 冲突/格式错误/capability 缺口一律 fail-open 进
  capture completeness（含 strict），同步 fail-closed 仅保留「改写型 source
  无法 ready」（design §9.1/§21）；有 strict 下 merge 冲突不阻塞 agent 的测试。

### W0-5（2026-07-13）

- 新增 `backend/wire/correlate.py`：namespaced 显式 anchor
  （`producer-call:`/`provider-response:`/`proxy-request:`/`source-seq:`，按 §7.2
  优先级）；`CorrelationMap` 持久化 anchor→lc 映射到
  `wire-sources/correlation-map.json`，任一已知 anchor 复用旧 lc（重建幂等、
  后到 gateway evidence 不产生第二个 call）；`pair_mcp_frames` 只在
  (instance, jsonrpc_id) 空间配对，`heuristic_match` 留 W2-3 接口显式返回 None。
- 新增 `backend/wire/finalize.py`：`_scan_sources` 逐 spool 校验
  （validate_evidence + attempt 归属，坏行计 parse_errors/dropped 不中断）；
  七类 evidence 的确定性映射（aggregate_usage→manifest.aggregates 不伪造 call；
  compaction_hint→manifest.compaction_hints 不伪造 context_compaction；
  http_exchange 显式 call anchor 才关联 lc、无 anchor 标 unmatched 不强配；
  stream_chunk 经 hop_anchor 挂 hop；jsonrpc_id 不进 lc 空间）；
  `select_agent_run_calls` 只取 phase=agent_run 的 llm_call（R3.6）；
  manifest 双层状态（per-source complete/partial/failed + 整体
  complete/partial/failed/not-applicable/in-progress/recovered）、
  policy.downgrade_reason（nit4）、phase_attribution、generation 每次 finalize
  单调递增（nit5 的计数来源）；「零通信」（干净关闭 0 行→complete/records=0）
  与「source 没工作」（declared 无 spool→failed+failure_reason）可区分（R12.1）。
- lifecycle 集成（§9.3）：prepare 成功后写 in-progress manifest（W0-7 recovery
  的扫描锚点）；`attempt_end` 停 source→close spool→finalize（fail-open）；
  零 source 的 noop session 仍不落任何 wire 文件。
- 验收通过：`uv run pytest tests/test_wire_manifest.py` → 13 passed（fake source
  端到端 lifecycle、R12.1 区分、R3.6 排除、七类映射、jsonrpc 与 producer_call_id
  撞值不合并、correlation-map 重建复用、generation 递增）；wire 全套 148 passed；
  ruff 干净。
- 无偏离设计；heuristic 评分与 native normalizer 输入按计划留 W2-3/W1。

### W0-6（2026-07-13）

- `backend/db.py` 新增 `_migrate_attempts_wire`（五列逐列幂等：wire_status/
  wire_record_count/wire_call_count/wire_error_count/wire_manifest_version）。
- 新增 `backend/wire/api.py`：三条路由（wire 列表 / manifest / blob）；cursor 为
  base64url `{"offset","generation"}`，generation 来自 manifest finalize 计数
  （nit5）——rebuild 后 record count/文件大小相同旧 cursor 也 409 `wire_changed`；
  过滤 record_type/phase/protocol/logical_call_id/after/before；limit 默认 100
  上限 500；blob 走 ref 白名单 + resolved containment，effective policy 非
  parsed/full 一律 404（R11.8/R12.6）；无 manifest 时 `not_available`；错误文本
  过 scrub。经 `register_routes` 挂到 /api。
- `api.py`：`_attempt_change_signature` 加 manifest generation（读取失败回退
  mtime/size），wire spool/blob 不进签名（§19.4 防高频 SSE）；
  `_ATTEMPT_ROOT_FRAMEWORK_FILES` 加 wire.jsonl/wire-manifest.json，新增
  `_ATTEMPT_FRAMEWORK_DIRS`（wire-sources/wire-blobs）从 artifact 列表与
  artifact 文件接口双向排除（评审 m1）。
- finalize 增 `update_db_summary`；lifecycle attempt_end 在 finalize 后回写
  DB 摘要（runtime_state 不可用时静默跳过）。
- 验收通过：`uv run pytest tests/test_wire_api.py` → 14 passed（分页/过滤/
  400/404/409/blob policy/traversal/artifact 隔离/迁移幂等/SSE 签名变化）。

### W0-7（2026-07-13）

- 新增 `backend/wire/recovery.py`：扫描 in-progress wire-manifest，attempt 已
  终态 → 重新 finalize 标 `recovered` + 回写 DB 摘要；finalize 失败写 `failed`
  （绝不长期伪装 in-progress）；running/queued 不动；已 finalize 的 manifest
  不重复处理。`main.py` lifespan 在既有 `schedule_startup_recovery` 之后调用，
  异常只记日志不影响启动（两套 recovery 正交不互扰）。
- 验收通过：`tests/test_wire_manifest.py` 增补 4 用例（crashed→recovered 且
  .partial 完整行恢复、running 跳过、completed 不重复 finalize、finalize 失败
  标 failed）→ 17 passed；wire 全套 166 passed。

### W0-5~W0-7 评审修复（2026-07-13，第四轮）

Blocking×3 + Major×6，全部修复；wire 测试 176 passed，recovery/dispatch/models
定向回归 16 passed。

- **B1 split-brain logical call**：`CorrelationMap.resolve_groups` 改为
  union-find——旧映射同 lc 的 anchor 预并集 + 本批 group 边，集合内已有多个旧
  lc 时取字典序最小者为 canonical 并重指全部成员 anchor（确定性、与到达顺序
  无关）；finalizer `_map_evidence` 改两阶段：先收集全部 call anchor 组统一
  解析、再产出 record。测试覆盖跨 finalize 桥接与同一 pass 内桥接两种场景。
- **B2 recovery 丢失 prepare 快照**：in-progress manifest 持久化
  `declared_sources`/`gaps`/`phase_attribution`；recovery 复原后传给 finalizer，
  「source 建 spool 前失败」恢复后正确判 failed 而非 not-applicable；
  `recovered` override 只在 complete/partial 时生效，全 failed 仍报 failed。
  异步恢复竞态：`recover_wire_manifests` 支持单 attempt 过滤，
  `backend/recovery.py` 恢复任务收尾（finally）补触发该 attempt 的 wire 收敛。
- **B3 blob API 默认公开**：新增 `octagon.wire_blob_api_enabled`（默认
  False，§19.3——无用户级 auth 时禁用 blob 下载），关闭时 404 不泄漏存在性；
  测试断言默认关闭、显式打开后 policy 门控照旧。
- **M1 capture_event 驱动 source status**：error/drop 事件按
  `payload.source_instance` 归属，error→source partial（含整体降 partial）、
  drop→dropped 计数；source entry 增 `errors` 字段并计入 DB wire_error_count。
- **M2 capabilities/coverage**：evidence envelope `capabilities` 聚到 manifest
  source entry；canonical capture_event 带 `effective_capabilities`；coverage
  补 §17 三轴（agent_semantics/llm_transport/mcp，按 kind 前缀聚合，
  缺轴如实 not-observed）。
- **M3 recovery failed 不同步 DB**：写 failed manifest 后调用
  `update_db_summary`，DB 与磁盘一致。
- **M4 cursor 边界**：offset 负数/越界/非行边界（前一字节非 \n）→ 400。
- **M5 读后一致性**：扫描完成后重读 manifest generation，变化→409
  `wire_changed`，杜绝新旧混合快照配旧 cursor。
- **M6 AttemptStatus**：`capture_infrastructure_failed` 加入
  `backend/models.py` 的 Literal。

### W0-5~W0-7 评审修复（2026-07-13，第五轮）

Blocking×2 + Major×4，全部修复；wire 测试 185 passed。

- **B1 recovery 提前 finalize**：`wire/recovery.py` 改为显式 _TERMINAL
  集合（与 dispatch 状态机对齐，含 capture_infrastructure_failed），
  DB 查不到（None）不触发提前 finalize；补 skip 测试。
- **B2 payload.producer_call_id 被忽略**：`_native_call_hints` 把 v1 payload 的
  producer_call_id 并入 anchor 输入（hints 缺失时补齐）；两边不一致时取 hints 并
  在 record.conflicts 登记（rule=hints-over-payload），计入 totals.conflicts；
  两 source 只按 payload 填同 ID 时正确合并为一个 logical call。
- **M1 一致性快照**：manifest 内嵌 `wire_file` 指纹（bytes/sha256/records）；
  API 扫描前尺寸预检 + 读后重读 generation/指纹，任一不符 409——覆盖「新 wire +
  旧 manifest」窗口；finalize 加 per-attempt threading 锁，进程内并发 finalizer
  串行、generation 不撞。
- **M2 kind 扇出污染**：lifecycle 事件/gap 一律携带 resolved instance；finalizer
  的 capture_stats 只按 instance 归属、删除 kind fallback；p1 出错不再把 p2 标
  partial（有双实例测试）。
- **M3 null/零值混淆**：finalizer 停止补业务事实——http partial、stream
  terminal/dropped_before、mcp direction/is_error/truncated 均保留 null；
  canonical models 对应字段放宽为 `| None`（R1.4）。
- **M4 counters 不进 completeness**：CaptureEventPayload.counters 明确为
  cumulative 语义（文档化），finalizer 对同名 counter 取 max 不重复相加，
  records_dropped/parse_errors 并入 source dropped/parse_errors 与状态判定
  （stop 报 dropped=100 时 manifest 为 partial）；source entry 增 counters 字段。

### W0-6 评审修复（2026-07-13，第六轮）

- **B：wire 指纹只比尺寸**：API 改为一次性读入 wire.jsonl 字节，对**实际读到的
  内容**算 SHA-256 与 manifest 指纹比对（长度 + hash 双检），扫描直接在这份被
  钉住的内存字节上进行——「同尺寸替换 + manifest 未换」窗口不再漏过；读后仅需
  复核 generation。补同尺寸替换（翻转 record_id 一个字符）409 测试。
- **M：call_role=null 伪造为 main**：finalizer 改映射为 `unknown`（canonical
  枚举已含），不污染依赖 call_role=main 的聚合/compaction 分析；补 null→unknown
  与显式 compaction 保留的单测。
- wire 全套 187 passed。

### W1-1（2026-07-13）

- 新增 `backend/wire/normalizers/claude_code.py`：读 events.jsonl 状态机
  （system/init 记 session/model/version 不建 call；assistant.message.id 建
  candidate call，同 id 流式重复事件合并取信息更全的 usage；tool_use→trajectory
  adjacency 不算 call；result→aggregate_usage 不建额外 call；无 id 按 call 序号
  建 inferred sequence anchor）；content parts → semantic IR + response content
  hash；解析失败计 parse_errors 不中断（保留 parser version）。
- `normalizers/runner.py`：run_native_normalizer 写 native-event spool +
  原子写 trajectory.json（§10.6 两阶段，step_id 由 producer event ref 派生，
  finalize 后 _reconcile_trajectory 把 step.logical_call_id 重指 union 后的
  canonical lc 并做 referential-integrity check）；lifecycle.agent_result 接入
  （fail-open）。修正：SOURCE_INSTANCE 必须与 spool 文件名一致（native-event），
  否则 orphan call 的 sequence anchor 在 normalizer/finalize 间算出不同 lc。
- 验收：`uv run pytest tests/test_wire_claude_normalizer.py` → 10 passed
  （多轮/工具/流式合并/无 id/解析失败/幂等/step lc 前后一致端到端）；
  离线脱敏 fixture 提交 `tests/fixtures/wire/claude/events.jsonl`；真实本地
  Claude attempt（att_e5f15208427c）附加验证产出 8 个调用级 llm_call + aggregate。
- 已知 fidelity 限制：raw events 只捕获流式 start 的 usage 时 per-call
  output_tokens 偏小，aggregate 保留真值——如实反映不伪造，W2/W4 transport
  source 可补真实 per-call。

### W1-4（2026-07-13）

- 新增 `backend/wire/aggregate.py`：`backfill_token_usage` 聚合 phase=agent_run
  的 llm_call usage → token_usage_json + external_refs.token_usage_source=
  wire|adapter；有 canonical calls 用 wire，无则回退 adapter 不改
  token_usage_json；adapter/wire 冲突时双保留（external_refs.token_usage_conflict，
  不覆盖 adapter 值、不改历史 score，§18.4）。lifecycle finalize 后接入。
- 验收：`uv run pytest tests/test_wire_aggregate_rebuild.py` → 有/无 call 两分支
  + 冲突双保留。

### W1-5（2026-07-13）

- 新增 `backend/wire/rebuild.py`（`python -m backend.wire.rebuild <attempt_id>`）：
  从 raw events 重跑 normalizer → 重写 native-event spool → finalize，产调用级
  llm_call；agent 从 DB 推断，policy 复用既有 manifest；不触碰原始 events
  （finalize 原子写）；回填 DB 摘要 + token 聚合。
- 验收：重建产 3 个 llm_call、重复幂等（wire 字节一致）、generation 递增、
  raw events 不变、非 native agent 拒绝。

**W1 核心（W1-1 调用级 token 曲线）已达成——roadmap P0 核心产出到位。**

### W0-6 评审修复（2026-07-13，第七轮）

- **M：分页整份读入内存**：`/wire` 改为全程持有单个 fd——分块（1MiB）算
  bytes+SHA-256 校验 manifest 指纹、cursor 边界校验后 `seek(offset)`、
  `readline` 逐行过滤到 limit、最后复核 generation。既钉住 atomic rename 前后
  同一 inode，又不把 wire.jsonl 无界聚合进内存（§19.2）。补「禁用
  read_bytes(wire.jsonl) 仍能分页」的守卫测试；同尺寸替换 409、cursor 边界
  用例照旧通过。

### W1-1/W1-4/W1-5 评审修复（2026-07-13，第八轮）

Blocking×2 + Major×4，全部修复；wire 测试 213 passed。

- **B1 真实 dispatch 不跑 native normalizer**：claude-code 有 native
  normalizer 时，即便零 injection-source，`prepare()` 也建 spool（noop 门改为
  「无 source 且无 native normalizer」才 noop），`agent_result` 才能产 native
  wire。真实 CC attempt（dispatch 不传 source）现在端到端产 8 个调用级
  llm_call（本地 att_e5f15208427c 验证）。
- **B2 rebuild 无 staging / 缺 raw 会重建成零调用**：runner 改
  staging（`.rebuild` 临时档 → read_spool 校验 → 原子替换，不先删正式档）；
  raw events 缺失或无任何证据时返回 False 且**不触碰**已有派生数据。
- **M3 parse error 未进契约**：normalizer 把 adapter 包装的
  `{"raw_line":...}` 坏行也计 parse error；runner 将 parse_errors 写成
  `capture_event(error, counters.parse_errors)` 进 spool → finalize 标 native
  source partial + 计入 wire_error_count。
- **M4 adapter 累计 usage 未对账**：`agent_result` 把 adapter token_usage 写成
  `scope="adapter"` aggregate evidence；finalize `_reconcile_adapter_usage`
  比对 native call 聚合，差异写 manifest `scope="reconciliation"` conflict +
  token_usage gap +totals.conflicts（§10.1 不静默修正）。
- **M5 trajectory gap 晚于状态计算**：`_reconcile_trajectory` 改为返回 gap，
  在 `_build_manifest` 之前并入——dangling step 现在正确使 manifest partial。
- **M6 W1-4 全文件内存聚合 + 零值混淆**：`aggregate_agent_run_usage` 接受行
  迭代器逐条累计（`_iter_wire_records` 逐行 yield 不整份读入）；adapter usage
  的「有数据」判定改 isinstance（显式 0 是有效上报，区分 null）。

（保留的 fidelity 限制：流式 raw events 只捕获 start usage 时 per-call output
偏小——现在由 reconciliation conflict 如实暴露，不再静默。）

### W1 评审修复（2026-07-13，第九轮）

Blocking×1 + Major×4，全部修复；wire 测试 218 passed。

- **B1 staging 残留混入**：runner 启动前清理 `.rebuild` **及** `.rebuild.partial`
  （SpoolWriter append 打开 partial 会续写残留 → 重复行/重复计 token）；写入包
  try/except，异常路径 `abandon()` + 删 staging；校验增行数吻合 + evidence ID
  无重复。补「手工造 .rebuild.partial 残留后重跑仍幂等」测试。
- **M2 native source 只在成功后声明**：prepare 确认 normalizer 存在即
  `_native_expected=True` → 进 declared_sources；agent_result 未产出/异常时登记
  gap + error 事件。raw 缺失时 native source 标 failed、coverage
  agent_semantics=failed，不伪装 complete/not-observed。
- **M3 schema drift 静默/中断**：normalize 改 per-event try/except（message 变
  list/string 等畸形只计 parse error 继续，不整次 fail-open）；顶层非 object /
  `{"raw_line"}` 均计错；parse-error evidence 带精确出错行号（raw_ref.line +
  message 行号列表）与确定性 UTC observed_at（用 raw 最后时间戳，回退固定 epoch
  ISO，不再空串）。
- **M4 rebuild 未带 adapter usage**：rebuild 从 DB token_usage_json 读历史
  adapter usage 传入 normalizer → 在线 finalize 与 offline rebuild 得到相同的
  reconciliation conflict。
- **M5 API 每页同步 hash 阻塞事件循环**：fd 校验 + 分块 hash + 分页扫描抽成
  `_scan_page` 同步函数，async route 用 `asyncio.to_thread` 执行；仍单 fd 钉住
  快照、不整份读入。

### W1-4 评审修复（2026-07-13，第十轮）

- **M：token conflict 重建后不收敛**：`backfill_token_usage` 是幂等重算，改为
  每次先无条件清掉上次的 `wire_token_usage`/`token_usage_conflict`，再按本次
  结果重建——parser 升级后 conflict→resolved、wire→adapter fallback 都能正确
  收敛，不残留陈旧 conflict。补两个状态转换测试（陈旧 conflict 在一致后被清除、
  wire 消失回退 adapter 时清 wire 派生字段）。wire 全套 133 passed。

### W1-2（2026-07-13，spike，codex-cli 0.144.1）

对照实验（隔离 CODEX_HOME，同一 prompt，ephemeral vs 非 ephemeral，均带真实
auth+config）结论已写入 design §27.1。核心事实：

- `--ephemeral`（agent-lane 现状）**不生成** session rollout JSONL；非 ephemeral
  生成 `sessions/.../rollout-*.jsonl`；两者可见结果（agent_message 文本）等价。
- `codex exec --json` stdout 每次 exec 仅 1 个 `turn.completed`（整个 turn 累计
  usage），20-40 个 agent_message（≈逐次 API call）**不带 per-call usage**——
  仅 stdout 只能得 attempt 级 aggregate，无法切逐调用曲线。
- 逐调用边界只在 internal rollout 的 `event_msg/token_count.info.last_token_usage`
  （Harbor 依赖），ephemeral 下不落盘。

**决议（→ W1-3 形态）**：保留 `--ephemeral`，Codex normalizer 首期只吃 stdout，
manifest 标 `call_boundary=aggregate-only`，用 turn.completed.usage 产一条
aggregate_usage，不伪造逐调用曲线；逐调用增强走「去 ephemeral + 事后删 auth」
对照路径（已验证行为等价），留给需要 Codex 逐调用曲线的 benchmark。W1-3 输入源
优先级据此固定为 stdout(②) 首落、CODEX_HOME rollout(①) 可选。
- 验收：spike 脚本与对照数据在 scratchpad（codex_spike.sh / codex_equiv.sh）；
  design §27.1 决议 + §27 划掉待决项；不新增 CI 前置（spike 命令不进离线套件）。

### W1-3（2026-07-13）

- 新增 `backend/wire/normalizers/codex.py`（按 §27.1 决议 aggregate-only）：读
  `codex exec --json` stdout；`turn.completed.usage` → 一条 `aggregate_usage`
  （scope=attempt，producer_event_type=`turn.completed` 保留 R2.1.5，
  cached_input→cache_read、无 cache_write 写 null）；capabilities 声明
  `call_boundary=aggregate-only` → finalize 落 manifest source capability；
  item.completed 的 agent_message→assistant step、mcp_tool_call/command_execution
  →tool_call step（todo_list/file_change 不建 step）；**不产 native_llm_call、不
  伪造逐调用曲线**；无 turn.completed（中断）不伪造 usage；per-event try/except
  兜 schema drift 计 parse error。多 turn usage 累加仍是 attempt 级 aggregate。
- runner 注册 codex normalizer；派生 evidence（parse-error/adapter aggregate）的
  producer 改为反映实际 agent（不再硬编码 claude-code，R2.1.5 溯源）。
- 验收：`uv run pytest tests/test_wire_codex_normalizer.py` → 7 passed（多调用/
  仅 aggregate/schema 漂移/aggregate-only 不伪造/producer event type/trajectory
  映射/端到端 manifest call_boundary）；真实本地 codex attempt
  （att_2d29ef3d82fd）附加验证：0 llm_call + 1 aggregate（430784/2505 累计）+
  25 trajectory step；codex rebuild 走通，aggregate-only 下 reconciliation 正确
  跳过（无 native call 可比）不误报 conflict。fixture
  `tests/fixtures/wire/codex/events.jsonl`。

### W1-6（2026-07-13）

- 新增 `backend/wire/env_capture.py`：`record_inbound_tool_call` 把 inbound 工具
  请求的 size/timing/attempt 归属写成一条 `http_exchange` evidence 到
  `wire-sources/env-inbound.jsonl`；`_SpoolRegistry` 每 (data_path, attempt_id)
  一个 SpoolWriter + 锁，并发请求串行 append、不同 attempt 各自独立；phase 从
  lifecycle 原子写的 `phase-state.json` 在请求到达时快照，缺失/attempt 不匹配/
  非法一律 `unknown`（禁止默认 agent_run）；全程 fail-open。
- `env_attempt_server.py` `call_tool` 接线：独立 try/finally 保证成功/500 两路
  都记录 metadata（先记再抛，不吞工具错误）；size 用 request/response JSON 字节，
  timing 用 monotonic，seq 线程安全去重。零新增鉴权（复用现有 Bearer 校验）。
- 验收：`uv run pytest tests/test_wire_env_inbound.py` → 9 passed（单调用一条
  evidence + attempt 归属、phase 快照/缺失/不匹配→unknown、并发 attempts 不串、
  同 attempt 并发串行 75 行无损、unknown 不进 agent_run 聚合、真实路由端到端产
  evidence 且 trace 照常写）；`tests/test_t07_env_attempt_server.py` 无回归。

### W1-7（2026-07-13）

- 后端：`_get_run_sync` 的 attempts 查询加 wire_status/wire_record_count/
  wire_call_count/wire_error_count，run detail 直接暴露（badge 数据源）。
- 前端（`web/`，无新图表依赖）：
  - `api/client.ts`：AttemptSummary 加 wire_* 字段；新增 WireRecord/WirePage/
    WireManifest 类型 + `getWireManifest`/`getWire`（按需加载，record_type=
    llm_call + cursor 分页）。
  - `RunDetail.tsx`：`WireBadge`（agent 卡片头，无采集不占位）；「通信时序」tab
    按需加载 `WirePanel`——completeness banner（status/policy/降档/coverage/
    partial 原因）；`TokenCurve` SVG polyline（x=logical call 顺序，
    y=input/cache/output，无依赖）；aggregate-only 明确文案（不画曲线，显累计）；
    unmatched call 单独分组不混入曲线；空态/加载/错误三态文案。
- 验收：`npx tsc --noEmit` 干净；`npm run build` 成功；wire API 19 passed；
  API/SSE 回归 14 passed；e2e 验证 run detail 暴露 wire_status=partial/calls=3、
  manifest coverage、llm_call 曲线 3 点（explicit+inferred confidence 正确）。

**W1 完成——roadmap P0 核心（调用级 token 曲线）端到端可视化到位。**

### W1-2/W1-3 评审修复（2026-07-13）

- **B1（W1-3）Codex 中断丢 trajectory**：runner 产出门改为「有 evidence **或**
  parse error **或** trajectory step」；Codex 观察到 item 但无 turn.completed 时
  写明确 usage-gap capture_event（capability usage=not-observed），不伪造
  aggregate；trajectory + spool 照常落盘。
- **M2（W1-3）坏行不计 parse error**：Codex normalizer 识别 adapter 包装的
  `{"raw_line"}`（对齐 claude）；turn.completed.usage 缺失/非 object 等 schema
  drift 计 parse error，不静默变「无 usage」。
- **M3（W1-3）semantic IR 未实现**：`_item_semantic_hash` 把 agent_message.text
  → messages IR、mcp_tool_call/command_execution → tools IR，走公共
  `hashing.semantic_hash`；trajectory step 带 content_hash + content_bytes。
- **M4（W1-2）spike 缺可审计产物**：提交 `spikes/w1-2-codex/`（README + 脱敏
  结果 + codex_spike.sh/codex_equiv.sh；真实 auth/session 不入库），含 CLI/
  config/prompt、四项等价性、auth 清理策略、rollout event/count + last_token_usage
  样例；design §27.1 引用该目录。

### W1-6/W1-7 评审修复（2026-07-13）

- **B1（W1-6）spool 生产路径不关闭**：lifecycle `attempt_end` 在 finalize 前、
  `abort` 路径都调 `close_attempt_spool`；env-inbound source 正常 complete
  （非 partial），有 lifecycle 集成测试（非测试手工 close）。
- **B2（W1-6）policy off 仍落盘**：phase-state 加 `capture_enabled`+`policy`
  control claim（off 时 prepare noop 不写文件）；env server 请求到达时
  `snapshot_capture_state`，未启用则不采集零落盘（R11）。
- **M3（W1-6）phase 在结束时读**：改为 `_wire_inbound_start` 请求到达时快照
  phase，传入 finish——长耗时工具跨 phase 不误归属。
- **M4（W1-6）inbound 被写成 outbound**：evidence HttpExchangePayload 加
  `direction`，env capture 写 inbound，finalize 取 evidence direction 不硬编码；
  canonical + e2e 测试。
- **M5（W1-6）unknown 不降级**：finalizer 见非 capture_event 的 unknown-phase
  record 自动把 phase_attribution 降 degraded + 记 gap，不只信 lifecycle 参数。
- **M6（W1-7）null 画成 0**：TokenCurve 用 null 断点分段（不画该点），aggregate
  文案缺失显「未知」；R1.4 null≠0。
- **M7（W1-7）只读第一页**：WirePanel 自动翻页拉全 llm_call + http_exchange
  （封顶 40 页），曲线点与 canonical 一致。
- **M8（W1-7）banner 不显 gaps**：banner 展示 manifest.gaps + phase_attribution
  degraded + 对账冲突（source 全 complete 但 partial 时原因可见）；unmatched
  分组覆盖 http_exchange hop，不只 llm_call。

### W1 评审修复（2026-07-13，第三轮：2 Blocking + 7 Major）

- **B1（W1-6）finalize 后可重开 spool**：`_SpoolRegistry` 加 seal 状态机
  （active→sealed）：close 时先 seal 拒绝新建 writer，持 entry 锁 drain in-flight
  append 再 close；record 见 sealed 直接丢弃——finalize 后请求不再把 .jsonl 移回
  .partial。补 sealed-no-reopen 测试。
- **B2（W1-6）无 native/injection source 时不启用采集**：prepare noop 门收窄为
  **仅 policy off**；policy!=off 时即便无 native/injection source 也建
  capture context + 写 `capture_enabled` phase-state，Env inbound 可采集。补
  对应 capture 测试。
- **M3（W1-3）semantic hash 不可跨 source**：Codex 工具调用改用 messages IR 的
  `tool_call` content part（§10.5 的 `tools` kind 是工具声明，非调用）；Claude/
  Codex 共用 `_part_semantic_hash`，trajectory step 带 content_hash/bytes。补
  Claude↔Codex 同内容 hash 相等的 parity 测试。
- **B4（W1-6）破坏性 schema 变更**：`direction` 改为 v1 内追加**可选**字段
  （默认 None），旧 http_exchange evidence 不带该键仍 validate，finalizer
  fallback outbound；不升 schema version。补旧 evidence validate 测试。
- **M5（W1-6）control claim 缺失=off 误判**：`snapshot_capture_state` 区分
  文件缺失（未启用/off，不采集）与损坏/读失败（基础设施故障，采集+unknown→
  degraded）。补损坏→degraded 测试。
- **M6（W1-7）unmatched 口径冲突**：finalizer 统一——inferred 是已匹配 call
  （有 lc，进曲线，不计 unmatched）；无 anchor 的 http hop 计入 unmatched_calls。
  banner 数字与 UI 分组一致。补口径一致测试。
- **M7（W1-7）40 页后静默截断**：loadAll 触封顶返回 truncated 标记，曲线标题
  显「已截断」，不再声称精确一致。
- **M8（W1-7）aggregate-only 隐藏其他 source**：改为「有逐调用 llm_call 就画
  曲线」，aggregate-only 仅作 per-source 补充说明，不全局隐藏（未来 gateway
  call-level + Codex aggregate-only 并存时曲线仍展示）。
- **M9（W1-2）spike 未安全清理凭证**：脚本改 mktemp 隔离 + trap 清理（含 auth/
  config 副本）+ codex_version/config_sha256 + 机器可验证断言（ephemeral=0/
  persistent≥1、agent_message 等价）+ 脱敏结果落文件。

### W1 评审修复（2026-07-14，第四轮：6 项）

- **R1（W1-6）seal 静默丢弃 active 请求**：`_SpoolRegistry` 加 in-flight 计数 +
  Condition；`begin_request`/`end_request` 成对包裹整个请求窗口，close 时
  seal→**drain 等 active 请求结束**→close；drain 超时/sealed 抢先的请求计
  dropped，写 drop capture_event（records_dropped counter）让 manifest 标 partial。
  env_attempt_server 改 begin/record/end 流程。补 drain-waits + drain-timeout-drop
  测试。
- **R2（W1-6）env-inbound 未进 declared/无空 spool**：lifecycle `_declared_sources`
  加 env-inbound；prepare 建空 env-inbound spool——「零通信」（空 .jsonl）与
  「采集器没工作」（无 spool）可区分。补 zero-comm=complete 测试。
- **R3（W1-6）序号进程内计数、重启撞 ID**：`_next_inbound_seq` 首次访问从已落盘
  spool 行数恢复（append-only 行数=已用序号），重启续接不从 0 重来。补恢复测试。
- **R4（W1-3）hash 未用规定的 messages IR**：`_part_semantic_hash` 改用 §10.5 的
  `[{role, content:[part]}]` 形状（不是裸 parts）；Claude/Codex 仍跨 source 相等。
  补 IR 形状 + 与裸 parts 不同的测试。
- **R5（W1-2）equiv 脚本 || true 吞失败**：改记录退出码；断言两 case rc=0、
  agent_message **非空**且一致——两次都失败得空数组不再误报 EQUIV PASS。
- **R6（W1-7）截断合成一个 bool 只在标题**：拆 truncCalls/truncHops 分别标注；
  banner 独立展示「llm_call/http_exchange 超上限已截断」，仅 HTTP 截断也正确归因。

### W1 评审修复（2026-07-14，第五轮：1 Blocking + 4 Major）

- **B1（W1-6）async lifecycle 被同步 drain 阻塞**：attempt_end/abort 的
  `close_attempt_spool` 改 `await _to_thread(...)`——drain 在线程池等待，请求协程
  仍能在事件循环上跑到 end_request，不被超时丢弃。补 async 并发不阻塞测试。
- **M2（W1-6）稀疏序号重建冲突**：seq 存进 evidence raw_ref.line；重启后从
  `max(seq)+1` 恢复（不数行）——[0,2] 恢复得 3 不复用 2。补稀疏序号测试。
- **M3（W1-3）canonical response hash 是裸 parts**：`_call_evidence` 改用
  `_part_semantic_hash`（§10.5 [{role,content}] IR）；response_summary.content_hash
  现在符合规范。补 canonical evidence hash 断言。
- **M4（W1-2）equiv 只验 agent_message**：断言扩展到 turn_usage 字段结构 +
  item event 类型序列都非空等价；README 同步。
- **M5（W1-7）无固定 fixture 验收**：抽 `web/src/wire/curve.ts`（曲线断点分段、
  matched/unmatched 拆分、视图状态推导）纯逻辑，RunDetail 改用它；加 vitest
  `curve.test.ts` 固定 fixture 覆盖曲线点（null≠0 断点）、空态、降级态（gaps/
  phase_degraded/conflicts）、截断分别归因、aggregate-only 不全隐藏——`npm test`
  10 passed。

### W1 评审修复（2026-07-14，第六轮：3 Major）

- **M1（W1-6）旧 spool 升级从 0 恢复 + raw_ref 造假**：seq 改存
  `extensions["x-lane.env-inbound-seq"]`（raw_ref.line 造假 provenance）；恢复
  用 `max(新格式 seq)+1`；旧格式（无该 extension）回退 http_exchange 计数；混合
  取更大者，杜绝撞旧 seq。补旧格式升级不重复 ID 测试 + seq 存 extensions 测试。
- **M2（W1-7）测的是死代码 helper**：RunDetail 真正消费 `deriveWireView`（空态/
  banner gaps/aggregate-only/截断归因全由它推导），并 export WirePanel；新增
  Testing Library + jsdom 渲染测试（WirePanel.test.tsx）覆盖空态/曲线/降级
  banner/截断独立标注/aggregate-only 文案的生产 DOM——vitest 15 passed（10 纯
  逻辑 + 5 渲染）。
- **M3（W1-2）equiv 未验完整四项**：脚本断言 usage_keys **非空**、**无工具类
  item**、stdout 有 model 时断言 model 等价（无则 README 降为「配置输入相同」）；
  README 补验证方式列。

### W1 评审修复（2026-07-14，第七轮：2 Major + 1 验证缺口）

- **M1（W1-6）旧格式稀疏 seq 仍冲突**：evidence ID 唯一性改由**进程 generation
  anchor**（启动时随机 UUID 混入 raw_ref）保证——env-inbound 是 live source（不
  离线重建），加随机 anchor 不破幂等；即便旧数据稀疏、seq 恢复不准也不撞 ID。
  seq 仅存 extensions 用于排序。补旧稀疏 [0,2] 重启后 ID 唯一测试。
- **M2（W1-7）前端重构丢 completeness 明细**：`deriveWireView.gaps` 从 string[]
  改结构化 `WireGap[]`（保留 source/status/failureReason/field/count）；RunDetail
  的 gapLabel 渲染「哪个采集器、什么状态、为什么失败」+「哪个字段」。curve.test
  断言结构化明细、WirePanel.test 加 failure_reason/field DOM 断言。
- **验证缺口（W1-2）工具断言是 denylist**：equiv 脚本改**允许列表**
  （ALLOWED_ITEMS={agent_message,reasoning}），任何列表外 item type（含未来/未知
  工具）判 unexpected 需人工确认，不再是三个已知工具的 denylist。

### W1-8/W1-9 实现验收（2026-07-14）

- canonical `llm_call.data.finish_reason` 和 `http_exchange.time.duration_ms` 已补齐；新增受
  run/attempt guard 保护的 trajectory 只读 API。后端完整 wire 回归：
  `uv run pytest tests/test_wire_*.py -q` → **291 passed**。
- RunDetail 已增加可键盘操作的曲线点、逐调用检查表、aggregate-only 累计卡片、
  native/result/adapter 三方对账、完整 hop 时间线以及 call↔trajectory 双向链接；
  metadata/full body viewer 明确区分 policy，并能读取 JSON 与 SSE 文本 blob。
- 前端回归：`npm test -- --run` → **21 passed**；`npm run build` → tsc + Vite production
  build 通过。
- 真实数据只读核验 `run_6ef6ca56e62c`：Claude `att_0d878d8b0673` 为 24 calls / 4 hops，
  68 个 trajectory step 带 logical call；Codex `att_f8d3749a7672` 为 0 个伪 call / 4 hops，
  attempt aggregate 为 input 1,131,205、output 9,584、cache-read 1,028,480、reasoning 2,330。
  旧 canonical hop 需 rebuild 后才会出现新补的 duration 字段，未修改历史 run 产物。
- 第三方模型真实 prompt/response 的 body 写盘/展示底座已存在，但真实 dispatch 尚未把
  http-proxy source 注入 CC/Codex，也没有 run/task `capture_policy=full` 入口；已新增 W4-6，
  在它完成前不得宣称 comparison run 已能采集原始交互。

系统编程任务还必须记录对应的 timeout/signal/slow-stream 集成命令；spike/远端命令放验证
记录但不加入默认离线测试套件。任何偏离 requirements/design 的决议必须先更新对应文档，
不能只写在本节。
