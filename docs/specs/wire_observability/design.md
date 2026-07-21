# 通信观测基础层——设计文档

> 状态：Proposed
>
> 日期：2026-07-10
>
> 需求：`docs/specs/wire_observability/requirements.md`

## 1. 设计结论

本设计采用“**一个基础层、多个证据源、一次离线归一**”的结构：

```text
raw events / gateway calls / HTTP hops / MCP frames
                              │
                              ▼
                     source-specific evidence
                              │
                      normalize + correlate
                              │
                              ▼
             wire.jsonl + wire-manifest.json + blobs
                              │
                  API / UI / context analysis
```

关键决策：

1. agent-arena 持有 canonical schema、文件、生命周期、API 和 UI；
2. native event 与网络 gateway 是并列 source，不是互相替代；
3. source 运行时写各自 spool，只有 finalizer 写 canonical `wire.jsonl`；
4. 第一交付阶段是 Foundation + CC/Codex native normalizer；
5. llm-gateway connector 是第二个生产 source，可拔除；
6. MCP wrapper 和 agent-arena 反代属于 agent-arena 自有 capture；
7. Harbor 式透明 redirect 只用于 agent-arena 能控制网络拓扑的 sandbox；
8. 通用 HTTPS MITM 不阻塞基础层交付。

## 2. 需求评审结果

修改后的 requirements 整体合理，需求边界和实施顺序可以形成闭环。新增两部分尤其必要：

- R8.1 明确了不信任 `HTTP_PROXY` 的 sandbox 插入点；
- R15.9 避免 wire observability 再造一套 provider/protocol 配置。

设计阶段对以下要求作实现限定，不改变其意图：

1. **透明 redirect 的适用范围**：Harbor 的真实拓扑不是普通 compose sidecar，而是
   workload 设置 `network_mode: service:<sidecar>`，共享 sidecar network namespace；
   nftables 和 `NET_ADMIN` 只在 sidecar。agent-arena 仅在能创建/修改该拓扑时启用。
2. **CA 私钥边界**：attempt CA 私钥只存在 sidecar 文件系统；workload 只挂载公开 CA
   certificate。共享 netns 不等于共享 filesystem。
3. **性能门槛**：R14 的 20ms 是 metadata capture 的验收目标，不适用于跨机 gateway、
   full payload 或 MITM；每个 source 单独报告 overhead。
4. **“完整通信证据”不是 full payload 同义词**：metadata policy 下的完整表示覆盖状态
   完整，payload 仍可因 policy 不保存。

## 3. 现状与改动锚点

当前代码中的主要锚点：

```text
backend/run_dispatch.py          创建 AdapterRunInput、绑定 adapter、调用 run_attempt
backend/runner.py                adapter → scorer → terminal status
backend/adapters/base.py         AdapterRunInput / AdapterResult / AgentAdapter
backend/adapters/claude_code.py  Claude stream-json → events.jsonl
backend/adapters/codex.py        Codex --json → events.jsonl
backend/adapters/token_usage.py  attempt 级 usage 解析/估算
backend/model_providers.py       provider kind/base_url/auth/custom headers
backend/config.py                Settings / YAML 配置
backend/api.py                   attempt detail、SSE、artifact API
backend/db.py                    attempts 摘要列和迁移
web/src/api/client.ts            API 类型
web/src/pages/RunDetail.tsx      attempt 对比和详情
```

当前 CC/Codex adapter 一边写 raw `events.jsonl`，一边在运行时累加 attempt 总 token。
这个总量继续保留作兼容字段，但以后由 canonical logical calls 聚合值回填；adapter 内
累计值只作为 evidence/交叉校验，不再是唯一事实源。

## 4. 模块结构

新增 Python 包：

```text
backend/wire/
├── __init__.py
├── models.py                 # canonical envelope / payload / manifest models
├── evidence.py               # WireEvidence v1 + JSON Schema validation
├── injection.py              # WireInjection / CommandRewrite，adapter 唯一注入接口
├── hashing.py                # semantic IR / NFC / JCS / hash domains
├── ids.py                    # deterministic evidence/call/hop IDs
├── paths.py                  # attempt wire 路径与安全解析
├── policy.py                 # capture policy、effective policy
├── redaction.py              # header/JSON/text scrub
├── spool.py                  # 每 source/process 独立 JSONL spool
├── writer.py                 # atomic canonical writer / blob writer
├── lifecycle.py              # WireCaptureSession / phase hooks
├── finalize.py               # normalize → correlate → merge → manifest
├── correlate.py              # explicit + heuristic correlation
├── aggregate.py              # attempt token/context summary
├── trajectory.py             # native event → 最小 step index
├── api.py                    # wire routes，可由 backend/api.py include
├── normalizers/
│   ├── base.py
│   ├── claude_code.py
│   ├── codex.py
│   ├── llm_gateway.py
│   ├── openai.py
│   └── anthropic.py
├── sources/
│   ├── base.py
│   ├── native_event.py
│   ├── llm_gateway.py
│   └── http_proxy.py
└── mcp_tap.py                # `python -m backend.wire.mcp_tap`
```

前端新增：

```text
web/src/components/WirePanel.tsx
web/src/components/WireTimeline.tsx
web/src/components/TokenCurve.tsx
web/src/api/client.ts          # WireRecord / WireManifest / page API
web/src/pages/RunDetail.tsx    # 通信时序 tab，按需加载
```

透明 sandbox sidecar 后期独立目录：

```text
wire-sidecar/
├── Dockerfile
├── entrypoint.sh
├── bin/network-policy
└── config/
```

metadata redirect 可先复用 pinned GOST binary 验证网络拓扑；MITM 数据面使用
mitmproxy addon 或后续独立二进制。无论底层实现是什么，sidecar 只能写 source spool
contract，不能定义新的 canonical schema。

## 5. 文件布局

每个 attempt：

```text
data/attempts/<attempt_id>/
├── events.jsonl                    # 既有 raw agent events
├── thinking.jsonl                  # 既有
├── trace.jsonl                     # 既有 env semantic trace
├── trajectory.json                 # Phase 1 生成的最小 step index
├── wire.jsonl                      # finalized canonical records
├── wire-manifest.json              # finalized 或 recovered manifest
├── wire-blobs/
│   └── sha256-<hex>.json.zst        # parsed/full policy 才存在
└── wire-sources/
    ├── native-event.jsonl          # normalized source evidence
    ├── llm-gateway.jsonl
    ├── http-proxy@<instance>.jsonl
    ├── mcp-stdio@<instance>.jsonl
    └── capture-events.jsonl
```

设计选择：保留 `wire-sources/`，原因是：

- parser/correlation 升级后可离线重建 canonical 文件；
- source 冲突可审计；
- 多进程不需要争抢同一个 `wire.jsonl`；
- finalizer 崩溃后仍能恢复。

`wire-sources/` 是 framework metadata，加入 `_ATTEMPT_ROOT_FRAMEWORK_FILES`/artifact
排除规则，不作为 agent 提交产物显示。通过 wire API 按 policy 读取，而不是走普通
artifact 文本接口。

## 6. 规范数据结构

### 6.1 信封结构（Envelope）

所有 canonical record 使用共同 envelope：

```json
{
  "schema_version": "lane-wire-v1",
  "record_id": "wr_01...",
  "record_type": "llm_call",
  "attempt_id": "att_...",
  "phase": "agent_run",
  "source": {
    "kind": "native-event",
    "instance": "claude-code",
    "version": "2.1.0",
    "parser_version": "claude-v1"
  },
  "time": {
    "timestamp": "2026-07-10T01:02:03.456Z",
    "started_at": null,
    "finished_at": null,
    "duration_ms": null
  },
  "correlation": {
    "logical_call_id": "lc_...",
    "hop_id": null,
    "parent_hop_id": null,
    "trajectory_step_id": null,
    "tool_call_id": null,
    "agent_id": "main",
    "parent_agent_id": null,
    "producer_session_id": "cli-session-...",
    "confidence": "explicit"
  },
  "provenance": [{
    "evidence_id": "we_...",
    "raw_ref": {"file": "events.jsonl", "line": 17},
    "confidence": "producer-reported"
  }],
  "conflicts": [],
  "data": {}
}
```

时间统一 UTC ISO 8601 毫秒。size 使用 bytes，duration 使用 milliseconds，token 使用
provider/tokenizer 报告的整数。

### 6.2 `llm_call`

```json
{
  "protocol": "anthropic-messages",
  "call_role": "main",
  "model_requested": "custom-provider/upstream/z-ai/glm-5.2",
  "model_resolved": "upstream/z-ai/glm-5.2",
  "provider": "custom-provider",
  "routing_path": "explicit",
  "streamed": true,
  "partial": false,
  "request": {
    "bytes": 187234,
    "messages_count": 23,
    "messages": [
      {"role": "tool", "kind": "tool_result", "bytes": 9421, "hash": "...", "hash_domain": "lane-semantic-jcs-nfc-v1"}
    ],
    "system_bytes": 8312,
    "system_hash": "...",
    "system_hash_domain": "lane-semantic-jcs-nfc-v1",
    "tools_schema_bytes": 12204,
    "tools_schema_hash": "...",
    "tools_schema_hash_domain": "lane-semantic-jcs-nfc-v1",
    "body_ref": null
  },
  "response": {
    "bytes": 3401,
    "finish_reason": "tool_use",
    "tool_calls_count": 1,
    "body_ref": null
  },
  "usage": {
    "input_tokens": 43120,
    "output_tokens": 411,
    "cache_read_tokens": 39000,
    "cache_write_tokens": null,
    "reasoning_tokens": null,
    "estimated": false,
    "estimator": null
  },
  "timing": {
    "routing_ms": 2,
    "ttft_ms": 380,
    "total_ms": 2120
  },
  "transport": {
    "http_status": 200,
    "retry_count": 0,
    "upstream_attempt_count": 1,
    "translation": "anthropic-to-openai",
    "translation_ms": 1.2
  },
  "evidence_refs": ["we_native_...", "we_gateway_..."],
  "hop_refs": ["hop_..."]
}
```

未知字段为 null，不写 `0`。例如 native event 无 request bytes 时必须为 null。

### 6.3 `http_exchange`

表示一次具体 hop，而不是业务调用：

```json
{
  "hop_id": "hop_...",
  "direction": "outbound",
  "protocol": "anthropic-messages",
  "method": "POST",
  "scheme": "http",
  "authority": "127.0.0.1:30000",
  "path": "/v1/messages",
  "status_code": 200,
  "request_bytes": 187234,
  "response_bytes": 3401,
  "streamed": true,
  "chunk_count": 51,
  "partial": false,
  "request_body_ref": null,
  "response_body_ref": null,
  "headers": {
    "request": {"content-type": "application/json"},
    "response": {"content-type": "text/event-stream"}
  }
}
```

Authorization、Cookie、API key 字段在 model 层不存在，redactor 之后才允许构造 record。

### 6.4 `mcp_frame`

```json
{
  "direction": "client-to-server",
  "jsonrpc_id": "42",
  "message_kind": "request",
  "method": "tools/call",
  "tool_name": "lane-drone-strike-hitl__release_weapon",
  "bytes": 812,
  "paired_record_id": "wr_...",
  "is_error": false,
  "truncated": false,
  "payload_ref": null
}
```

### 6.5 `stream_chunk`

`stream_chunk` 是可选的 canonical transport record，用来表达 chunk timing、中途断流和
丢弃情况；它不等同于保存完整 SSE body：

```json
{
  "hop_id": "hop_...",
  "sequence": 17,
  "relative_ms": 438.2,
  "event_type": "content_block_delta",
  "bytes": 312,
  "content_hash": "sha256:...",
  "hash_domain": "raw-bytes-v1",
  "payload_ref": null,
  "is_terminal": false,
  "partial": false,
  "dropped_before": 0
}
```

metadata policy 只保存时间、大小和 hash；parsed/full 才允许 `payload_ref`。若 source 只
能给出 chunk aggregate，则不伪造逐 chunk record，而是在 `http_exchange` 保存
`chunk_count` 并在 manifest 声明 `stream_timing=aggregate-only`。

### 6.6 `capture_event`

`capture_event` 是 capture 控制面的 canonical record，用来区分“source 正常但没有通信”
和“采集器没有工作”：

```json
{
  "event": "ready",
  "source_instance": "http-proxy-3f2a",
  "status": "ok",
  "reason_code": null,
  "error_class": null,
  "message": null,
  "counters": {
    "records_written": 0,
    "records_dropped": 0,
    "parse_errors": 0
  },
  "effective_capabilities": {
    "request_metadata": true,
    "stream_timing": "per-chunk"
  }
}
```

`event` 枚举为 `start|ready|phase_change|drop|error|stop|finalize`。错误 message 必须先
脱敏；状态事实进入 manifest，事件则提供时间线和审计依据。

### 6.7 数据结构扩展

`context_compaction` 是本设计对 R1.2 “至少支持”的显式扩展，用于承载 finalizer 推导的
压缩事件；它不是某个 source 可私自写入的类型。未来新增 canonical record type 必须升级
reader capability/JSON Schema，不得把任意 source payload 直接塞进 `data` 绕过版本管理。

### 6.8 来源证明与冲突

canonical flat value 旁边不为每个字段套一层 `ObservedValue`，避免 API/UI 过度复杂。
改用：

```json
{
  "field_sources": {
    "usage.input_tokens": "we_gateway_1",
    "model_resolved": "we_gateway_1",
    "correlation.trajectory_step_id": "we_native_3"
  },
  "conflicts": [{
    "field": "usage.input_tokens",
    "selected": 43120,
    "candidates": [
      {"value": 43120, "evidence_id": "we_gateway_1"},
      {"value": 43098, "evidence_id": "we_native_3"}
    ],
    "rule": "provider-response-over-cli"
  }]
}
```

## 7. ID 生成与关联

### 7.1 证据 ID

Evidence ID 必须能离线重建：

```text
we_<uuid5(attempt_id, source_kind, source_instance, raw_ref, producer_id)>
```

`raw_ref` 使用文件相对路径 + 行号/事件稳定 ID，不使用绝对路径。

### 7.2 逻辑调用 ID

优先级：

1. 能跨 evidence 出现的 producer/provider request、response、message 或 call ID；
2. native event 稳定 turn/message ID；
3. 反向代理在**收到每个请求时**生成的 source request ID；
4. attempt 内 source 顺序锚点。

生成：

```text
lc_<uuid5(attempt_id, chosen_call_anchor)>
```

finalizer 一次性读取本次可用 source 后选择 anchor，因此添加 gateway evidence 不会在同一次
finalize 中产生第二个 call。离线重建时读取旧 `correlation-map.json`（放在
`wire-sources/`）优先复用已有 logical ID。

`x-lane-call-id` 只适用于 agent-arena 自己逐次发起 HTTP/SDK 调用，或确实位于每次调用
路径上的 runtime hook。CC 的 `ANTHROPIC_CUSTOM_HEADERS`、Codex provider `http_headers`
都是进程级静态配置，一个 attempt 内所有请求会复用，**不得**作为 logical call anchor。
这两类 adapter 只静态注入 `x-lane-attempt-id`/`x-eval-session-id`。

agent-arena 反向代理为每个入站请求生成 `proxy_request_id`，并从响应 header/body 提取
provider `request-id`、OpenAI response ID、Anthropic message ID 等 producer anchor。若这些
ID 没有出现在 native event 中，它们仍只提供 transport/source-local 的稳定性，跨 source
关联继续走 §7.3，不将 attempt-level header 当作 call-level 证据。

### 7.3 启发式匹配

没有显式 ID 时为 native call 和 transport call 建候选边，评分：

| 信号 | 权重 |
|---|---:|
| 同模型/alias 可解析为同模型 | 3 |
| input/output usage 相等 | 4 |
| 完成时间差 ≤ 2s | 3 |
| attempt 内顺序一致 | 2 |
| tool/finish reason 一致 | 2 |
| 时间区间重叠 | 2 |

匹配规则：

- score ≥ 10 且唯一最佳：`high`；
- score 7-9 且领先次优 ≥ 2：`medium`；
- 其他不合并，保留 `unmatched`；
- 不允许多条 native call 匹配同一 transport call；retry hop 除外；
- 并行请求没有显式 ID 时宁可 unmatched，不按顺序强配。

阈值通过 golden fixtures 固定，变更需升级 correlator version。

## 8. 数据源协议

### 8.1 Python 接口

```python
class CaptureSource(Protocol):
    kind: str

    async def start(self, ctx: CaptureContext) -> WireInjection: ...
    async def collect(self, ctx: CaptureContext) -> SourceSummary: ...
    async def stop(self, ctx: CaptureContext) -> SourceSummary: ...


class NativeNormalizer(Protocol):
    producer: str

    def normalize(
        self, *, attempt_id: str, attempt_dir: Path
    ) -> Iterable[WireEvidence]: ...
```

`WireInjection` 是 source 进入 adapter 的唯一接口；它定义在 `backend/wire/injection.py`，
`backend/adapters/base.py` 只依赖这个无副作用的数据对象：

```python
@dataclass(frozen=True)
class WireInjection:
    enabled: bool = False
    phase: str = "agent_run"
    process_env: Mapping[str, str] = field(default_factory=dict)
    llm_base_url: str | None = None
    llm_headers: Mapping[str, str] = field(default_factory=dict)
    mcp_rewrites: Mapping[str, CommandRewrite] = field(default_factory=dict)
    phase_state: PhaseStateRef | None = None
    capture_token: str | None = field(default=None, repr=False)
```

```python
@dataclass(frozen=True)
class CommandRewrite:
    command: str
    args_prefix: tuple[str, ...]

@dataclass(frozen=True)
class PhaseStateRef:
    path: Path | None = None
    control_url: str | None = None
```

`PhaseStateRef` 必须且只能设置一种 transport；path 必须位于 attempt framework 目录且以
只读方式传给被测进程。`capture_token` 不序列化到 manifest、spool 或 dataclass repr。
`injection.py` 不导入 adapter、source 或 lifecycle，避免 `adapters.base → wire.injection`
形成循环依赖。

`AdapterRunInput` 增加一个带默认值的字段，`AgentAdapter` Protocol、`run()` 签名和
`build_adapter()` 签名保持不变：

```python
@dataclass
class AdapterRunInput:
    # existing fields unchanged
    wire_injection: WireInjection = field(default_factory=WireInjection)
```

source 不能直接修改 adapter 全局状态。lifecycle 先 start/merge 所有 source，再把一个已
校验的 `WireInjection` 放入 `AdapterRunInput`；adapter 只消费，不负责 source 生命周期。
merge 按 source 配置顺序执行，但同一标量有两个非空值即配置错误，不做 last-wins。
`llm_headers/process_env` 禁止包含 provider auth key；认证仍来自 `ModelProviderConfig`。

两个 adapter 的固定消费点如下：

| adapter | 消费点 | 行为 |
|---|---|---|
| Claude Code | `run()` 构造 `subprocess_env` 后、启动 subprocess 前 | 合并 `process_env`；用 `task.wire_injection.llm_base_url` 覆盖本次进程的 provider base；把 attempt 级 `llm_headers` 合入 `ANTHROPIC_CUSTOM_HEADERS`；现有 `_write_mcp_config(task, attempt_dir)` 从 task 按 server name 应用 `mcp_rewrites` |
| Codex | `run()` 生成 provider `-c` 参数和 MCP `-c mcp_servers.*` 参数前 | 把现有 helper 改为 `_provider_cli_args(model_ref, task.wire_injection)` 和 `_mcp_args(task, task.wire_injection)`；`llm_base_url` 进入本次 `model_providers.<id>.base_url`，MCP command/args 被 rewrite；随后合并 `process_env`。静态 provider header 只能是 attempt header |

未知字段、adapter 不支持的非空 injection 必须在 agent 启动前生成 capability gap；fail-open
模式丢弃该项并记录 `capture_event`，strict 模式使 capture completeness 失败。禁止 adapter
静默忽略 injection。

### 8.2 `WireEvidence v1`：跨进程 spool schema

source spool 的每行必须是下述最小 envelope，而不是 canonical record 或 producer 私有
JSON。正式实现将 Pydantic schema 导出并提交为
`docs/specs/wire_observability/wire-evidence-v1.schema.json`，Python/Go/Node/sidecar 以该
JSON Schema 做 contract test。

```json
{
  "evidence_schema_version": "lane-wire-evidence-v1",
  "evidence_id": "we_...",
  "attempt_id": "att_...",
  "phase": "agent_run",
  "evidence_type": "http_exchange",
  "source": {"kind": "lane-http", "instance": "proxy-3f2a", "version": "0.1.0"},
  "producer": {"name": "lane-http", "version": "0.1.0", "event_id": "req-17"},
  "time": {
    "observed_at": "2026-07-10T01:02:03.456Z",
    "started_at": null,
    "finished_at": null
  },
  "raw_ref": {"kind": "spool-line", "file": "wire-sources/http-proxy-3f2a.jsonl", "line": 17},
  "correlation_hints": {
    "producer_session_id": null,
    "producer_call_id": null,
    "request_id": "req-17",
    "provider_response_id": null,
    "jsonrpc_id": null,
    "model": null,
    "sequence": 17
  },
  "capabilities": {"request_metadata": true, "response_payload": false},
  "redaction": {"policy": "metadata", "status": "applied", "hash_algorithm": "sha256", "hash_domain": "raw-bytes-v1"},
  "payload": {},
  "errors": []
}
```

约束：

- `evidence_type` 为 `native_llm_call|aggregate_usage|http_exchange|stream_chunk|mcp_frame|capture_event|compaction_hint`；
- `payload` 的形状由 `evidence_type` 对应的 versioned variant 定义，不能装任意私有字段；
- source 私有 raw event 留在既有 raw 文件，`raw_ref` 指向它；spool 内只能出现已脱敏值；
- `evidence_id` 按 §7.1 生成；writer 在 append 前完成 schema、attempt、phase 和 policy 校验；
- unknown phase 合法但必须写成字符串 `unknown`，不能省略。

各 `payload` variant 的 v1 最小字段（字段不可得时写 null，不用 0 冒充）：

| evidence type | payload 最小字段 |
|---|---|
| `native_llm_call` | `producer_call_id, model, call_role, request_summary, response_summary, usage, finish_reason` |
| `aggregate_usage` | `scope, usage, producer_event_type` |
| `http_exchange` | `method, scheme, authority, path, status_code, request_bytes, response_bytes, streamed, partial, timing` |
| `stream_chunk` | `hop_anchor, sequence, relative_ms, event_type, bytes, content_hash, terminal, dropped_before` |
| `mcp_frame` | `direction, jsonrpc_id, message_kind, method, tool_name, bytes, is_error, truncated` |
| `capture_event` | `event, status, reason_code, counters, effective_capabilities` |
| `compaction_hint` | `producer_call_id, before_anchor, after_anchor, strategy, confidence` |

`request_summary/response_summary/usage/timing/counters` 也在 JSON Schema `$defs` 中封闭定义并
设 `additionalProperties=false`；producer 扩展只能放带命名空间且 schema 已登记的
`extensions`，finalizer 默认忽略但保留 provenance。

finalizer 的确定性映射：

| evidence type | canonical 输出 |
|---|---|
| `native_llm_call` | 新建/合并 `llm_call`，提供 semantic/usage 字段 |
| `aggregate_usage` | manifest/attempt aggregate conflict evidence，不单独伪造 call |
| `http_exchange` | `http_exchange` hop，并关联/补充 `llm_call` |
| `stream_chunk` | `stream_chunk`；同时更新对应 hop aggregate |
| `mcp_frame` | `mcp_frame`，按 JSON-RPC ID 配对 |
| `capture_event` | `capture_event` 并更新 manifest source status |
| `compaction_hint` | 与相邻 calls 合并推导 `context_compaction`；不足时只保留 hint provenance |

### 8.3 Spool 协议

每个进程/instance 独立写：

```text
wire-sources/<kind>@<instance>.jsonl.partial
```

关闭时 rename 为 `.jsonl`。进程崩溃留下 `.partial`，finalizer 读取完整行、标记 partial。

写入规则：

- 一行一个 `WireEvidence`；
- 单行最大值受 policy 限制；
- append 后不原地修改；
- source 自己串行写，不做跨进程共享 file lock；
- blob 先写临时文件、fsync、按 hash rename，再写 evidence ref。

## 9. 生命周期集成

### 9.1 新对象

```python
session = WireCaptureSession(
    attempt_id=attempt_id,
    attempt_dir=attempt_dir,
    agent_name=agent_name,
    env_name=env_name,
    model=model,
    provider_config=resolved_provider_config,
    adapter_capabilities=capture_capabilities_for(agent_name),
    settings=settings.wire_observability,
)
```

`capture_capabilities_for()` 是 dispatch 侧的无 I/O 静态 registry（如
`llm_base_url=true`、`mcp_rewrite=true`）；它不修改
`AgentAdapter` Protocol。未知/第三方 adapter 得到全 false 的默认声明，因此不会收到它
无法消费的 injection。

`dispatch()` 的调用顺序固定为以下单一路径；这是满足 R3.2 的接口时序，不允许 adapter
在 `run()` 后再补 injection：

```python
adapter = build_adapter(agent_name, settings, model=model)
capture = WireCaptureSession(...)
injection = await capture.prepare(phase="agent_run")  # start sources, wait ready, merge
run_input = AdapterRunInput(..., wire_injection=injection)
bound_adapter = _BoundAdapter(
    adapter=adapter, task=run_input, env=env, data_path=data_path
)

try:
    await run_attempt(adapter=bound_adapter, scorer=scorer, observer=capture)
except BaseException:
    await capture.abort_before_or_during_run()
    raise
```

`prepare()` 内部严格执行：创建 spool → 写 source `start` event → 逐 source
`start(ctx)` → 等待 ready/capability probe → 合并/校验 injection → 写 `ready` event → 返回。
因此 source.start() 一定先于两个 adapter 的 `run()` 和子进程/session 创建。prepare 失败时
通常返回安全降级后的 injection 并记 gap，由批量验收层判断 strict completeness；唯有
strict 模式下已选择改变 base URL/command 的 source 无法 ready 时，prepare 在 agent 启动前
抛 `CapturePreparationError`，dispatch 记录独立 capture/infrastructure outcome，不生成伪造的
agent failure。`run_attempt()` 不再调用 source.start()，只推进 phase、flush 和 finalize。

如果 `build_adapter()` 本身未来需要运行时 capability probe，probe 只能读取静态配置；任何
会创建 agent 进程/session 的动作必须后移到 `prepare()` 之后的 adapter `run()`。observer
由 dispatch 在 prepare 前创建，`_BoundAdapter` 只持有已经包含 injection 的 input。

### 9.2 `runner.py` 改造

`run_attempt` 增加可选 observer：

```python
async def run_attempt(*, adapter, scorer, observer: AttemptObserver | None = None):
    observer = observer or NullAttemptObserver()
    try:
        async with observer.phase("agent_run"):
            adapter_result = await adapter.run()
        await observer.agent_result(adapter_result)

        async with observer.phase("verification"):
            outcome = await evaluate(...)
        ...
    finally:
        await observer.attempt_end()
```

这里不再调用 `observer.attempt_start()`：start/ready 已由 dispatch 的 `prepare()` 完成，避免
同一 source 启动两次。`attempt_end()` 接受 `PREPARED|RUNNING|FINALIZING` 状态并幂等；若
异常发生在 `prepare()` 后、进入 `run_attempt()` 前，则由 dispatch 的
`abort_before_or_during_run()` 负责 stop/finalize。

实际重构时不能让当前多个 early return 绕过 `finally`。做法是：

1. 保留现有 terminal decision helper；
2. 将整个 adapter/scorer 决策包进外层 `try/finally`；
3. `attempt_end()` 捕获并吞掉 fail-open 错误；
4. strict 模式只改变 capture completeness，不复用 agent status；批量验收层决定该 run 是否
   满足 strict benchmark。

`dispatch()` 创建 observer 并传入 `run_attempt`。`_BoundAdapter` 保持 adapter 兼容，不让
每个 adapter 自己管理 manifest。

### 9.3 最终化时机

`agent_result()`：

1. flush MCP/HTTP source；
2. 运行 native normalizer；
3. 拉取 llm-gateway 增量记录；
4. 可生成 agent phase 的临时 canonical view。

`attempt_end()`：

1. 停止所有 source；
2. 运行最终 normalize/correlate/merge；
3. 原子写 `wire.jsonl`；
4. finalize manifest；
5. 更新 attempts wire 摘要列。

verification phase 产生的 capture 单独保留，但不参与 agent token 汇总。

### 9.4 独立进程的 phase 归属

phase 不能由 finalizer 事后按时间猜。归属机制固定如下：

| source | phase 传播方式 |
|---|---|
| native-event、gateway connector | adapter 注入路径专用于 `agent_run`，启动参数/header 写死该 phase；verification 使用另一 capture context |
| MCP tap | rewrite 参数显式加入 `--attempt-id ... --phase agent_run` |
| agent-arena HTTP proxy | 每个 listener 绑定 attempt；进程内 observer registry 在请求到达时快照当前 phase |
| 独立 proxy/sidecar | `WireInjection` 提供只读 phase-state 文件或 control socket；observer phase 切换先更新它，再启动该 phase 工作 |

phase-state 文件采用 atomic rename，内容含 `attempt_id`、`phase`、monotonic sequence 和
更新时间；sidecar 收到更新后先写 `capture_event:phase_change` 再处理新流量。跨 phase 的
长连接保留连接建立 phase，但每条可分辨 request/frame 使用到达时 phase。source 无控制通道、
重启后 phase 文件过期或 attempt 不匹配时必须写 `phase=unknown`，manifest 记
`phase_attribution=degraded`，该 evidence 从 `agent_run` token/通信指标中排除。

## 10. 原生事件规范化器

### 10.1 Claude Code

输入：现有 `events.jsonl`。

状态机：

1. `system/init`：记录 CLI session/model/version，不生成 call；
2. `assistant.message.id` 首次出现：建立 candidate call；
3. 相同 message ID 的重复/增量事件合并；
4. assistant message usage 作为本次 call producer-reported usage；
5. tool_use blocks 记录 tool adjacency，但不把工具执行当 LLM call；
6. `result` usage 作为 attempt aggregate evidence，不生成额外 call；
7. 没有 message ID 时按 assistant event sequence 建 call，confidence=`inferred`；
8. timestamp 是 stdout 到达时间，只能作为完成时间近似，request start/duration 为 null。

现有 adapter 的 `total_input_tokens += assistant usage` 与最终 result 覆盖逻辑保留一个版本作为
兼容，但 canonical 总量由 normalizer call 聚合，并与 result aggregate 比较。差异写 manifest
conflict，不静默修正。

### 10.2 Codex

输入优先级：

1. Codex internal session JSONL（若能在隔离 `CODEX_HOME` 下保存）；
2. 当前 `codex exec --json` stdout events；
3. attempt aggregate usage（最低精度）。

Harbor 依赖 internal `token_count.last_token_usage` 识别一次 API call。agent-arena 先做 spike：

```text
CODEX_HOME=<attempt_dir>/.codex-runtime
codex exec --ephemeral --json ...
```

检查 ephemeral 模式是否仍产生可读取 session log。如果没有：

- 使用隔离 CODEX_HOME、取消 `--ephemeral`；
- run 完成后删除 auth/secret，只保留经 policy 处理的 session evidence；
- 每个 attempt 新目录，禁止跨 attempt 续接；
- 对照测试确认取消 ephemeral 不改变 prompt、model、tools 和结果。

解析规则借鉴 Harbor：

- `token_count` 关闭当前 API call；
- 使用 `last_token_usage`，不用累计 `total_token_usage` 创建单次 call；
- response/tool events 绑定当前 call；
- `total_token_usage` 只作 aggregate validation；
- producer 没有稳定 call ID 时生成 source sequence anchor。

如果 spike 证明无法安全保存 internal log，Phase 1 允许 Codex manifest 标记
`call_boundary=aggregate-only`，但不得伪造逐调用曲线。

### 10.3 离线重建入口

提供内部函数和后续 CLI：

```text
lane wire rebuild <attempt_id> [--normalizer-version latest]
```

首期可只实现 Python 函数/管理脚本，不要求前端按钮。重建先写 `.rebuild` 文件，通过校验后
原子替换，不修改原始 events。

### 10.4 上下文与压缩分析

`aggregate.py` 只消费 canonical calls/evidence，不重新解析 Claude/Codex/gateway 私有格式。
按 `(attempt_id, agent_id, producer_session_id)` 分段，避免把新 session 当成压缩。

对每个 `call_role=main` 的相邻调用计算：

```text
input token delta
cache token delta
message count/bytes delta
message hash longest-common-prefix/suffix
system/tools schema delta
tool-result bytes delta
```

压缩证据优先级：

1. producer 显式 compaction event → `confidence=explicit`；
2. summary/compaction call 后出现 token/message 大幅下降 → `high`；
3. token 下降且 message hash diff 显示中段删除/摘要插入 → `medium`；
4. 只有 token 突降 → `low`；
5. agent session ID 改变 → `new-session`，不记 compaction。

被动检测初始阈值沿用 llm-gateway 已验证的保守值，并做成 versioned analyzer config：

```text
current_input / previous_input < 0.6
previous_input - current_input > 5000
summary lookback <= 5s
```

message diff 使用最长公共前缀 + 最长公共后缀，推断：

- 大量中段删除并插入少量消息：`selective-summary`；
- 仅保留后缀：`sliding-window`；
- 大量删除并插入一条摘要：`full-summary`；
- 证据不足：`unknown`。

输出 canonical `context_compaction` record：

```json
{
  "record_type": "context_compaction",
  "data": {
    "before_call_id": "lc_...",
    "after_call_id": "lc_...",
    "summary_call_id": "lc_...",
    "before_tokens": 90000,
    "after_tokens": 28000,
    "dropped_messages": 31,
    "inserted_messages": 1,
    "kept_prefix": 2,
    "kept_suffix": 4,
    "strategy": "full-summary",
    "source": "passive",
    "confidence": "medium",
    "analyzer_version": "compaction-v1"
  }
}
```

工具结果回传形态通过 MCP result 和下一次 LLM request 中的 tool-result message 对比：

- bytes/hash 完全一致：`full`；
- 明确 truncation marker 或可证明的前缀截断：`truncated`；
- provider/native event 显式声明 summary：`summarized`；
- 只有 size 显著缩小但无内容证据：`reduced/low-confidence`，不能直接叫 summarized；
- metadata 不足：`unknown`。

并发度由 call 的 `[started_at, finished_at]` 区间重叠计算；只有完成时间的 native call 不参与
精确并发度，只报告 sequence。所有 analyzer 结果均可从 canonical wire 重建，不写主 DB。

### 10.5 跨 source semantic hash 规范

所有 hash 必须同时携带 `hash_algorithm` 和 `hash_domain`。v1 只定义两类：

1. `raw-bytes-v1`：`SHA-256(exact_received_bytes)`，只用于完整性、同一 hop 重放和同一
   domain 内比较；
2. `lane-semantic-jcs-nfc-v1`：协议 parser 先映射到下面的 semantic IR，再递归执行
   Unicode NFC，按 RFC 8785 JCS 序列化为 UTF-8，最后 SHA-256。输出为 64 位小写 hex。

semantic IR 的形状固定为：

```json
{
  "kind": "messages|system|tools|tool_result",
  "value": {}
}
```

- messages：有序 `[{role, content:[part...]}]`；message 与 content part 顺序保持原协议
  顺序，role 映射为 `system|developer|user|assistant|tool|unknown`；
- content part：映射为 `{type:text,text}`、`{type:tool_call,name,arguments}`、
  `{type:tool_result,content}` 或 `{type:media,media_type,content_hash}`；文本做 NFC 但保留
  原始空白，不 trim、不改换行；
- tool call/result 的 producer call ID 是 correlation 元数据，不进入内容 hash，避免不同
  source 分配不同 ID 导致内容永不相等；
- system：使用与 message content 相同的有序 part 数组，不把某协议的外层包装算入；
- tools：每项为 `{name,description,input_schema}`，单项字段按 JCS；工具列表按 NFC 后的
  `name` 排序，因为协议间工具声明顺序不具语义；同名工具保持原顺序并记 parse gap；
- JSON arguments、schema 和结构化 tool result 保留 JSON 类型，由 JCS 处理 object key；
  字符串中的 JSON 不二次解析，除非 producer schema 明确声明该字段为 JSON object。

每个 protocol normalizer 必须用 golden cross-protocol fixture 证明 Anthropic/OpenAI/Responses
的等价输入得到同一 semantic hash。无法取得完整语义内容时只写 bytes/null 或 producer 私有
domain，不能生成 `lane-semantic-jcs-nfc-v1`。

llm-gateway 当前若只返回其 raw JSON hash，connector 标记
`llm-gateway-raw-json-v1`，只在 gateway source 内比较；只有 API 返回足够的脱敏结构、使
agent-arena 能重建 semantic IR 时，connector 才重算 canonical semantic hash。§10.4 的
跨 source message diff 和 tool-result `full/truncated/summarized` 判定只接受相同 canonical
domain；domain 不同则降级 size/usage 启发式并明确降低 confidence。

### 10.6 最小 trajectory step 产物

Phase 1 的 native normalizer 同时原子生成 `trajectory.json`，避免
`trajectory_step_id` 只有消费者没有生产者：

```json
{
  "schema_version": "lane-trajectory-v1",
  "attempt_id": "att_...",
  "steps": [{
    "step_id": "ts_...",
    "sequence": 12,
    "timestamp": "2026-07-10T01:02:03.456Z",
    "agent_id": "main",
    "parent_agent_id": null,
    "kind": "assistant|tool_call|tool_result|system",
    "producer_event_refs": [{"file": "events.jsonl", "line": 17}],
    "tool_call_id": null,
    "logical_call_id": "lc_..."
  }]
}
```

`step_id = ts_<uuid5(attempt_id, agent_id, producer_event_ref)>`。Claude/Codex 原生事件能证明
邻接时生产 step；只有 HTTP/gateway evidence 时不伪造 step。wire envelope 的
`trajectory_step_id` 只能引用该文件中存在的 ID，finalizer 做 referential-integrity check。
这只是 agent-arena 最小索引，不宣称已采用 ATIF；ATIF 是可选 export mapping。

## 11. llm-gateway 连接器

### 11.1 外部 API 协议

llm-gateway 需要增加 versioned 只读接口：

```http
GET /api/v1/observe/sessions/{session_id}/calls?cursor=&limit=200
GET /api/v1/observe/sessions/{session_id}/compactions?cursor=&limit=200
```

响应：

```json
{
  "schema_version": "llm-gateway-observe-v1",
  "items": [],
  "next_cursor": null,
  "complete": true
}
```

要求：

- 按 `(ts_request, id)` 稳定排序；
- cursor opaque；
- limit 最大 1000；
- 只返回指定 session；
- API auth 与 gateway admin/read policy 一致；
- 返回 call ID、session、agent、protocol、model、source、timing、usage、message hashes、
  retry、translation、error；
- prompt/response preview 默认不返回，显式 `include_payload` 也受服务端 policy 限制。

agent-arena connector 不读取 SQLite，不依赖 gateway Web UI。

### 11.2 身份注入

Claude provider 在 attempt 启动前合并：

```text
x-eval-session-id: <attempt_id>
x-eval-agent-id: main
x-eval-agent-name: claude-code
x-eval-agent-kind: main
x-eval-call-role: main
x-user-id: <既有静态值，不覆盖>
```

合并函数：

```python
merge_custom_headers(static: str | None, attempt_headers: dict[str, str]) -> str
```

规则：

- header name case-insensitive；
- `x-eval-*` / `x-lane-*` 为保留名，attempt headers 胜出；
- Authorization/x-api-key 不允许进入 custom headers；
- 静态其他 header 原样保留；
- 输出使用换行分隔的 `Header: value`。

Codex 在 Responses gateway 可用前不接该 source。以后优先通过 agent-arena internal reverse
proxy 注入 correlation，避免依赖 Codex 私有 custom-header 配置。

### 11.3 拉取与失败

- agent start 前不要求 connector 写控制状态；header 首次请求即可建立 session；
- agent end 后分页拉取，空结果重试 3 次：100ms/300ms/1s，等待 gateway sink flush；
- 网络/API 错误写 source summary，fail-open；
- strict 模式下 source status=`failed`，但 adapter result 不改；
- gateway call 转成 evidence，finalizer 与 native call 关联；
- 移除 connector 配置后所有本地读取逻辑不变。

## 12. MCP stdio tap

### 12.1 命令重写

Claude 当前 MCP：

```text
uv run --project <repo> python <env>/mcp_server.py
```

改为：

```text
uv run --project <repo> python -m backend.wire.mcp_tap \
  --attempt-id <id> \
  --phase agent_run \
  --spool-dir <attempt>/wire-sources \
  --policy metadata \
  -- uv run --project <repo> python <env>/mcp_server.py
```

Codex 的 `mcp_servers.*.command/args` 做同样重写。wrapper 只包 agent-arena 注入的 MCP
server，不扫描或修改用户全局 MCP 配置。

### 12.2 双向 pump

父进程启动 child，并运行：

```text
parent stdin  ── pump/capture ──> child stdin
child stdout ── pump/capture ──> parent stdout
child stderr ──────────────────> parent stderr
```

实现约束：

- 使用 bytes pump，不 decode 后再转发；
- capture parser 维护独立 buffer，以换行 framing 解析当前 stdio JSON-RPC；
- 超过 `max_frame_bytes` 后继续透明转发，但 capture 标记 dropped；
- stdout 写入必须及时 drain；
- SIGTERM/SIGINT 传给 child process group；
- 日志只写独立 spool，绝不写 stdout；
- payload 在写 spool 前 redaction；
- request map 用 `(jsonrpc_id)` 配对，完成后释放，设 TTL/上限。

MCP SDK 若未来改 framing，wrapper capability probe 失败后退化为 byte metadata，不破坏通信。

## 13. agent-arena HTTP 反向采集

### 13.1 路由形态

第一版不为每个 attempt 开独立端口，而是在现有 FastAPI 服务挂内部路由：

```http
POST /internal/wire-proxy/{attempt_id}/{provider}/{path:path}
```

adapter 将 base URL 注入为：

```text
http://127.0.0.1:8100/internal/wire-proxy/<attempt>/<provider>
```

设计理由：

- 不需要端口池；
- 复用已有 uvicorn 生命周期；
- 本机 CC/Codex 可直接访问；
- upstream 只从 server-side provider config 查找，客户端不能提交任意 URL，避免 SSRF；
- attempt/provider/path 天然 correlation。

内部路由使用独立短期 capture token；不能复用 URL 中 attempt ID 作为授权。token 只注入
子进程环境/header，不落 wire。

### 13.2 转发

- `httpx.AsyncClient.stream()` 转发；
- request body metadata 模式流式计数+hash；parsed 模式在协议上限内缓存解析；
- response 用 `StreamingResponse` 逐 chunk 转发并异步记录 timing；
- hop spool 使用 bounded asyncio queue + 单 writer task；
- queue 满时优先保主通信、drop capture chunk 并计数；
- hop 完成后写 summary evidence；
- upstream auth 由 provider config 注入，inbound credential 不转发；
- correlation/internal headers 不转发第三方；
- client disconnect 取消 upstream request，并写 partial。

Phase 1 解析器：Anthropic Messages、Chat Completions、Responses。解析失败仍透明转发，
record capability 退化 metadata。

### 13.3 HTTPS 与外部 URL

客户端到 agent-arena internal proxy 可以先用同机 HTTP；proxy 到 provider 使用 HTTPS。这样已经
能看到明文 LLM payload，不需要 MITM。只有 base URL 不可改的进程才进入 R8.1/MITM。

## 14. 沙盒透明重定向

### 14.1 准确复用 Harbor 拓扑

生成 compose overlay：

```yaml
services:
  lane-wire-sidecar:
    image: <pinned image>
    cap_add: [NET_ADMIN, NET_RAW]
    # sidecar 保存 CA private key 和 source spool

  main:
    network_mode: service:lane-wire-sidecar
    depends_on:
      lane-wire-sidecar:
        condition: service_healthy
    # main 只读挂公开 CA cert，不挂 private key
```

sidecar netns 中 nftables：

- sidecar 自身发出的已 mark 流量直接放行，防止递归；
- local destination 放行；
- controlled TCP redirect 到 capture port；
- 非 TCP 的处理按 policy 显式记录；
- phase 切换通过 sidecar control command 热更新。

与 Harbor 一样，task 显式声明 `network_mode`/`networks` 的 service 不强制覆盖；manifest
列出 excluded services，coverage 不能报 complete。

### 14.2 两档实现

1. `metadata`：transparent TCP + SNI/target metadata，不解 TLS；
2. `parsed/full`：MITM，解析 HTTP/LLM payload。

MITM：

- attempt start 生成 ephemeral CA；
- private key 仅 sidecar 可读；
- public CA 挂载/注入 workload；
- Node/OpenSSL/Rust/系统 CA 分别 capability probe；
- pinning 或信任失败自动降级 metadata（strict 模式标失败）；
- cleanup 删除 key/cert，manifest 只保存 fingerprint。

## 15. 模型服务配置统一

### 15.1 协议命名决策

Wire 层规范名称使用：

```text
openai-chat-completions
anthropic-messages
openai-responses
```

配置到 Wire 协议的映射：

| `ModelProviderSection.kind` | Wire 协议 |
|---|---|
| `openai-chat` | `openai-chat-completions` |
| `anthropic` | `anthropic-messages` |
| `openai-responses` | `openai-responses` |

`ModelProviderSection.kind` 保存配置层枚举，`wire_protocol()` 在观测边界转换为 Wire
规范名称。Codex 还会检查 `wire_api == "responses"`；不兼容的 Provider 在 Agent
启动前失败。当前配置文件名为 `arena.yaml`。

### 15.2 认证模式

```python
class ModelProviderSection(BaseModel):
    kind: WireProtocol
    base_url: str
    api_key_env: str | None = None
    auth_mode: Literal["bearer", "api-key"] | None = None
    custom_headers: str | None = None
```

默认：

- `anthropic-messages` → `bearer`（保持 agent-arena 当前行为）；
- OpenAI protocols → `bearer`。

Claude 注入：

| auth_mode | env |
|---|---|
| bearer | `ANTHROPIC_AUTH_TOKEN`，删除子进程 env 中 `ANTHROPIC_API_KEY` |
| api-key | `ANTHROPIC_API_KEY`，删除 `ANTHROPIC_AUTH_TOKEN` |

不做 token 值转写。Codex/OpenAI 使用 provider env key，并由 Codex provider config 发送
Bearer。custom headers 经过保留名/secret 校验后与 dynamic correlation headers 合并。

`wire_api` 旧字段暂时接受但 deprecated；其值必须与 canonical `kind` 一致，否则配置加载
fail fast。后续删除前保留至少一个配置迁移周期。

## 16. 采集策略与脱敏

### 16.1 生效策略

来源：

```text
server maximum policy
task requested policy
run requested policy
source capability
```

effective policy 取最严格交集。客户端不能请求超过 server maximum 的档位。

优先级：

```text
off < metadata < parsed < full
```

### 16.2 脱敏管线

```text
raw bytes in memory
  → protocol parser
  → structural secret removal
  → configurable JSON path rules
  → text secret patterns
  → size limit/truncation
  → blob/spool write
```

永不持久化的 header：

```text
authorization
proxy-authorization
x-api-key
cookie
set-cookie
```

默认 JSON key pattern：`api_key|token|secret|password|authorization|cookie`，大小写不敏感。
redactor 异常返回 metadata-only evidence，并增加 `redaction_failed`；不能 fallback raw。

### 16.3 大对象（Blob）

- 序列化为 UTF-8 JSON；
- redaction 后计算 SHA-256；
- zstd 是否新增依赖在实现阶段决定；首期可 gzip，manifest 明确 codec；
- 文件名只接受 `sha256-[0-9a-f]{64}.json.<codec>`；
- 同 attempt 内容去重；
- API 只通过 ref 查找，不接受任意 path。

## 17. 清单（Manifest）

状态分两层：

```json
{
  "schema_version": "lane-wire-manifest-v1",
  "attempt_id": "att_...",
  "generation": 3,
  "status": "partial",
  "strict": false,
  "policy": {"requested": "parsed", "effective": "metadata"},
  "sources": [{
    "kind": "native-event",
    "status": "complete",
    "capabilities": {},
    "records": 8,
    "dropped": 0,
    "parse_errors": 0,
    "failure_reason": null
  }],
  "coverage": {
    "agent_semantics": "complete",
    "llm_transport": "partial",
    "mcp": "complete",
    "correlated_calls": 7,
    "unmatched_calls": 1
  },
  "totals": {
    "records": 31,
    "logical_calls": 8,
    "hops": 7,
    "blobs": 0,
    "bytes": 18342,
    "conflicts": 1
  },
  "started_at": "...",
  "finished_at": "..."
}
```

整体 status：

- `complete`：所有 enabled/required source 达到声明 capability；
- `partial`：至少有可用证据，但 source/coverage 有缺口；
- `failed`：所有 required source 失败或 canonical finalize 失败；
- `not-applicable`：policy off 或该 agent 无适用 source；
- `in-progress`：未 finalize；
- `recovered`：后端重启后从 spool 恢复完成。

应用启动 recovery 扫描 status=`in-progress` 且 attempt 已 terminal 的 manifest，读取完整 spool
行重新 finalize；无法恢复则写 failed，绝不长期伪装 in-progress。

## 18. DB 摘要

attempts 新增轻量列：

```sql
wire_status TEXT NOT NULL DEFAULT 'not_available',
wire_record_count INTEGER NOT NULL DEFAULT 0,
wire_call_count INTEGER NOT NULL DEFAULT 0,
wire_error_count INTEGER NOT NULL DEFAULT 0,
wire_manifest_version TEXT
```

不把 call/hop/payload 放主 DB。migration 复用 `backend/db.py` 的逐列幂等模式。

`token_usage_json` 兼容策略：

1. 有 canonical calls 时，从 `phase=agent_run` calls 聚合；
2. 没有 canonical calls 时继续使用 adapter result；
3. 记录 `external_refs.token_usage_source=wire|adapter`；
4. 不因 canonical 重建自动改历史 score。

## 19. API

### 19.1 路由

按现有 `/api` router 实际前缀注册：

```text
GET /api/runs/{run_id}/attempts/{attempt_id}/wire
GET /api/runs/{run_id}/attempts/{attempt_id}/wire/manifest
GET /api/runs/{run_id}/attempts/{attempt_id}/wire/blobs/{ref}
```

### 19.2 分页

`wire` query：

```text
record_type
phase
protocol
logical_call_id
after
before
cursor
limit (default 100, max 500)
```

cursor 是 base64url 编码的：

```json
{"offset": 12345, "generation": 3}
```

读取器 seek 到 byte offset，逐行过滤，返回下一条完整行后的 offset。manifest generation
在每次成功 atomic finalize/rebuild 后单调递增；cursor generation 与当前值不同时返回 409
`wire_changed`，客户端从头刷新。这样即使 rebuild 后 record count/文件大小相同也不会复用
旧 cursor。

响应：

```json
{"items": [], "next_cursor": null, "manifest_status": "complete"}
```

### 19.3 安全

- 先验证 attempt 属于 URL 中 run；
- blob ref regex 白名单，不 join 任意 path；
- metadata policy 下 blob endpoint 返回 404；
- parsed/full 仍经过应用现有 auth；若当前 agent-arena 没有用户级 auth，生产配置默认禁用 blob
  API，直到权限模型明确；
- wire/source spool 不进入普通 artifact API；
- parse error 返回 scrub 后文本。

### 19.4 SSE 变更签名

`_attempt_change_signature()` 增加 manifest `generation`（读取失败时才回退
`wire-manifest.json` 的 mtime/size），使 RunDetail 在 finalize/rebuild 后收到 attempt
update。不监控每个 spool，避免运行中高频 SSE 刷新。

## 20. 前端

RunDetail 每个 attempt 增加 `wire_status` badge。详情增加“通信时序”视图，首次打开才请求
wire API。

M1 UI：

1. completeness banner：source、policy、partial reason；
2. token curve：x=logical call 顺序，y=input/cache/output；
3. calls table：role/model/provider/tokens/TTFT/duration/retry/status；
4. call 展开：provenance、conflicts、hops、关联 step/tool；
5. compaction marker；
6. unmatched evidence 单独分组，不混入曲线；
7. payload/blob 仅 policy 和 API 允许时展示。

不在首版引入图表依赖；token curve 用 SVG polyline/points，保持当前前端依赖轻量。数据量大时
只加载 `llm_call`，展开单 call 再按 logical_call_id 请求 hop/frame。

## 21. 失败模型

capture failure 独立分类：

```text
source_start_failed
source_collect_failed
source_timeout
parse_failed
redaction_failed
spool_write_failed
correlation_partial
finalize_failed
cleanup_failed
```

默认行为：

- adapter/scorer status 不变；
- manifest/source status 记录；
- DB `wire_status` 更新；
- 后端 structured log 告警；
- strict benchmark 在聚合层判 capture acceptance failed。

如果 capture injection 本身已经改变 provider base URL，而 proxy 启动失败，不能继续用坏 URL。
必须在启动 agent 前：

1. healthcheck capture source；
2. fail-open 模式恢复原 base URL/env；
3. strict 模式不启动 agent并返回独立 infrastructure failure（实现阶段需增加 terminal code），
   不能把它伪装成 agent auth/network failure。

## 22. 性能设计

- canonical merge 在 `asyncio.to_thread` 执行；
- runtime source 使用 bounded queue；
- queue 满 drop capture，不阻塞主数据；
- metadata source 不保存 chunk body，只计数/时间/hash；
- `wire.jsonl` 一次 atomic rewrite，运行时不争锁；
- MCP 每进程独立 spool；
- gateway 拉取分页；
- UI 分页和按类型加载；
- request/response parser 有最大 body/frame/message count。

基准场景：1KB 非流式、128KB 非流式、1000 SSE chunks、10MB MCP result。报告 p50/p95
延迟、TTFT、CPU、RSS、磁盘和 dropped records。

## 23. 实施阶段

### 阶段 0：基础能力

范围：

- models/IDs/paths/policy/redaction；
- `WireEvidence v1` JSON Schema、跨语言 contract fixtures；
- spool/writer/manifest/finalizer，包括 `stream_chunk`/`capture_event` 映射；
- lifecycle hook、`AdapterRunInput.wire_injection` 和两个 adapter 的启动前消费点；
- DB 摘要；
- manifest/wire API；
- recovery。

验收：fake source 写 `WireEvidence v1`，经过完整生命周期生成可分页读取的 canonical 文件；
测试证明 `source.start/ready` 先于 adapter `run`；失败/取消能生成 partial/failed manifest。

### 阶段 1：原生事件

范围：

- Claude normalizer；
- Codex stdout normalizer；
- Codex internal session spike；
- canonical token 聚合；
- `lane-trajectory-v1` 最小 step index 与 wire 引用完整性；
- semantic IR/JCS/NFC hash golden fixtures；
- offline rebuild；
- 基础 token curve UI。

验收：不配置任何 gateway，CC/Codex attempt 仍有调用级或诚实标记 aggregate-only 的
`llm_call`，不会把 result aggregate 误当额外调用；有 native step 的 call 可跳到真实存在
的 `trajectory.json` step。

### 阶段 2：llm-gateway 连接器

范围：

- 外部 calls/compactions API；
- connector；
- Claude attempt-scoped headers（不作为 call ID）；
- native+gateway correlation/conflict；
- routing/TTFT/retry UI。

验收：同一 CC 调用只出现一个 logical call，同时显示 native step 和 gateway hop；关闭
connector 后历史/新 native view 正常。

### 阶段 3：MCP 标准输入输出

范围：

- tap wrapper；
- CC/Codex command rewrite；
- tool call/result pairing、size、truncation；
- trace/trajectory link。

验收：包装前后任务结果一致；timeout/cancel 无孤儿；至少一个 tool result size 可见。

### 阶段 4：agent-arena HTTP 反向采集

范围：

- internal proxy；
- 三种 LLM protocol parser；
- SSE timing；
- body policy/blob；
- provider auth/canonical protocol migration。

验收：不使用 llm-gateway 也能让 base URL 可注入的 CC/Responses client 产生 transport
evidence。

### 阶段 5：沙盒透明重定向预研

范围：

- Harbor-style shared-netns sidecar spike；
- metadata redirect；
- MITM CA capability matrix。

验收：sidecar 仅在受控 container 环境启用，不修改宿主机全局 nftables。

### 阶段 6：完整分析与扩展

- compaction detector；
- context/message/tool-result curves；
- sub-agent topology；
- Responses compatibility source；
- HTTPS MITM parsed/full（通过 spike 后）。

## 24. 测试设计

### 24.1 测试夹具

固定脱敏后的 golden fixtures：

```text
tests/fixtures/wire/claude/*.jsonl
tests/fixtures/wire/codex/*.jsonl
tests/fixtures/wire/gateway/*.json
tests/fixtures/wire/mcp/*.jsonl
tests/fixtures/wire/http/*.sse
```

fixture 标注 producer/version，避免一个 fixture 被误认为所有 CLI 版本。

### 24.2 单元测试文件

```text
tests/test_wire_models.py
tests/test_wire_evidence_contract.py
tests/test_wire_redaction.py
tests/test_wire_spool.py
tests/test_wire_injection.py
tests/test_wire_hashing.py
tests/test_wire_trajectory.py
tests/test_wire_claude_normalizer.py
tests/test_wire_codex_normalizer.py
tests/test_wire_correlation.py
tests/test_wire_manifest.py
tests/test_wire_api.py
tests/test_wire_mcp_tap.py
tests/test_wire_http_proxy.py
tests/test_model_provider_protocol_migration.py
```

### 24.3 集成测试

- fake provider routes 覆盖三协议；
- streaming/non-streaming；
- 429/5xx failover；
- chunk 中断；
- two concurrent attempts；
- gateway unavailable；
- parser version mismatch；
- kill/cancel；
- metadata policy disk scan 确认没有测试 secret；
- path traversal；
- source spool partial recovery。
- fake adapter 断言 source `ready` 严格先于 `run()`，并覆盖 CC/Codex injection
  capability matrix；
- Go/Node fixture 写出的 evidence 通过同一 JSON Schema，并能映射为 canonical；
- source 正常零请求与 source 未 ready 分别产生可区分的 `capture_event`/manifest；
- independent MCP/proxy source 的显式 phase、phase 切换和 `unknown` 降级；
- Anthropic/OpenAI/Responses 等价 semantic IR 的 hash 相同，raw/private domain 不参与
  跨 source compaction/tool-result 判定。

### 24.4 行为等价

同一任务在 capture off/on 各跑 N 次，比较：

- 成功率；
- tool call 序列；
- final output/scorer；
- duration/TTFT；
- 子进程退出情况。

随机模型输出场景不能逐字比较，使用 fake deterministic provider 做严格等价，真实 provider
只比较统计分布。

## 25. 迁移与兼容

1. 历史 attempt：manifest API 返回 `not_available`；
2. 可对有 raw `events.jsonl` 的历史 CC/Codex attempt 运行 offline rebuild；
3. `token_usage_json` 保留，wire 聚合逐步成为新来源；
4. 旧 provider kind 全部接受并规范化；
5. `wire_api` 暂时兼容并校验；
6. 不修改现有 trace/scorer 输入；
7. wire 文件加入 framework metadata 排除列表；
8. capture 默认 `metadata` 且 fail-open，升级后不会因外部 gateway 缺失阻止运行。

## 26. 外部改动清单

### llm-gateway

- 增加 versioned calls/compactions query API；
- API 输出遵守其自身 redaction policy；
- 保持 `x-eval-*` 不转发上游；
- 可选增加 `x-lane-*` alias，但不是 agent-arena connector 前置。

这些外部改动只影响对应 source coverage，不影响 Foundation/native/MCP 的运行。

## 27. 暂不决定

以下在对应 spike 后决定，不阻塞 Phase 0/1：

- `lane-trajectory-v1` 到 ATIF 的 export mapping 及稳定版本；
- transparent metadata sidecar 最终使用 GOST 还是 agent-arena 自有二进制；
- MITM 使用 mitmproxy addon 还是独立实现；
- full blob 使用 gzip 还是 zstd；
- ~~Codex 是否能在 `--ephemeral` 下保留 internal session events~~
  **（W1-2 spike 已决议，见 §27.1）**；
- 通用 WebSocket/Socket.IO capture 的优先级。

### 27.1 W1-2 Codex session-log spike 决议（2026-07-13，codex-cli 0.144.1）

可复核脚本 + 脱敏结果见 `spikes/w1-2-codex/`（README + codex_spike.sh /
codex_equiv.sh；真实 auth/session 内容不入库）。

对照实验（隔离 `CODEX_HOME`，同一 prompt，ephemeral vs 非 ephemeral，均带真实
auth+config）：

| 维度 | `--ephemeral`（agent-arena 现状） | 非 ephemeral |
|---|---|---|
| session rollout JSONL | **不生成**（`sessions/` 目录不存在） | 生成 `sessions/YYYY/MM/DD/rollout-*.jsonl` |
| 可见结果（agent_message 文本） | 一致 | 一致 |
| stdout `turn.completed.usage` | 有，**整个 exec/turn 累计** | 有，同样累计 |
| 逐调用 `token_count.last_token_usage` | 无 | rollout 内每次 API call 一个增量 |

关键事实：

1. `codex exec --json` 的 stdout **每次 exec 只有 1 个 `turn.completed`**，带全程
   累计 usage（`input_tokens/cached_input_tokens/output_tokens/reasoning_output_tokens`）；
   20-40 个 `agent_message`（每个≈一次 API call）**不带 per-call usage**。因此仅
   凭 stdout 只能得到 **attempt 级 aggregate**，无法切逐调用曲线。
2. Harbor 依赖的逐调用边界只存在于 internal rollout 的
   `event_msg/token_count.info.last_token_usage`；而 `--ephemeral` 明确
   "Run without persisting session files to disk"，实测零 session 文件。
3. 去 ephemeral 能拿到 rollout，但代价：额外落 session/skills/shell_snapshots，
   需事后清理 auth/secret；可见行为（prompt/model/结果）与 ephemeral 等价。

**决议 → W1-3 输入源优先级**：

- **不改现状默认**：保留 `--ephemeral`，Codex normalizer 首期只吃 stdout
  events → 逐调用 `llm_call` **无法**从 stdout 切分，manifest 标
  `call_boundary=aggregate-only`（design §10.2 已允许），用 `turn.completed.usage`
  产一条 `aggregate_usage`，**不伪造逐调用曲线**。
- **W1-3 输入源优先级**（design §10.2 三档）：① 若后续需要逐调用曲线，走
  「隔离 CODEX_HOME + 去 ephemeral + run 后删 auth/secret，只留 policy 处理过的
  session evidence」的对照路径（已验证可行、行为等价）；② stdout events（当前，
  aggregate-only）；③ attempt aggregate usage（最低）。首期落 ②，把 ① 作为
  可选增强留给需要逐调用 Codex 曲线的 benchmark。
- 逐调用切分不是 Codex 的默认能力，agent-arena 不为它牺牲 ephemeral 隔离性；诚实
  aggregate-only 优于伪造曲线（对齐 roadmap「诚实 aggregate-only」原则）。

## 28. 完成定义

本 spec 的基础能力完成，不要求所有后期 source 全部实现。最小完成定义是：

1. canonical schema、spool、finalizer、manifest、API 可用；
2. CC/Codex native evidence 进入调用级 `llm_call`，无法逐调用时明确 aggregate-only；
3. MCP stdio 能记录 tool request/result size；
4. 至少一种 transport source（llm-gateway connector 或 agent-arena reverse proxy）与 native
   call 成功关联；
5. RunDetail 能显示调用级 token 曲线和 capture coverage；
6. capture 默认 fail-open、metadata，无 secret 落盘；
7. 两个并发 attempts 不串数据；
8. 移除 llm-gateway 后上述 Foundation/native/MCP/历史 UI 仍工作。

R8.1 sandbox MITM 和通用 HTTPS 属于后续 source coverage，不应把基础层长期
卡在外部 runtime 权限上。
