# DeerFlow v2 接入 spike

日期：2026-07-22

本文固定 agent-arena 首次接入 DeerFlow 时采用的证据边界。它不会把上游仓库变成
runtime 依赖，也不会宣称只在其他 adapter 中观察到的能力。

## 固定版本与安装边界

- 官方仓库：<https://github.com/bytedance/deer-flow>
- 稳定 tag：[`v2.0.0`](https://github.com/bytedance/deer-flow/tree/v2.0.0)
- Commit：`7e7f0410797693cf882594555ba414e0361d4c6f`
- Python distribution：`deerflow-harness==2.0.0`
- Python 要求：3.12+，依据固定版本的
  [`pyproject.toml`](https://github.com/bytedance/deer-flow/blob/v2.0.0/backend/packages/harness/pyproject.toml)

普通 Attempt 不会 clone 或安装 DeerFlow。部署必须在包含
`deerflow-arena-runner` 的 Python 环境中预装固定 distribution。Runner 的只读
`--probe` 会检查 distribution version、导入 `DeerFlowClient`，并验证 constructor
与 stream signature；通过后才允许 Attempt。

## Probe 矩阵

| Surface | 固定证据 | 决策 |
|---|---|---|
| Embedded API | [`deerflow.client.DeerFlowClient`](https://github.com/bytedance/deer-flow/blob/v2.0.0/backend/packages/harness/deerflow/client.py) 暴露显式 config/model、feature 参数和 stream | **Go**：使用小型固定版本 runner |
| Model config | 固定示例/config 使用命名模型与 LangChain integration import path | **Go**：生成 Attempt 私有 `arena-model` |
| OpenAI chat | `langchain_openai:ChatOpenAI` | **Go**：golden config fixture 覆盖 |
| OpenAI Responses | 带 `use_responses_api`、`output_version` 的 `ChatOpenAI` | **Go**：golden config fixture 覆盖 |
| Anthropic | `langchain_anthropic:ChatAnthropic` | **Go**：golden config fixture 覆盖 |
| 其他 provider | 此固定版本没有 agent-arena fixture | **No-go**：拒绝，不静默映射到 OpenAI |
| Stream event | Client 暴露带类型的流式 event | **Go**：有界 NDJSON 与独立有界 summary |
| 本地 sandbox | 固定 config 支持 local provider、mount、`allow_host_bash` | **Go**：只挂载已校验的 `skill_workspace` |
| Subagent | Constructor 有显式启用开关 | **执行 Go**：透传 option；identity coverage 仍 unsupported |
| Thinking/plan mode | Constructor 有显式开关 | **Go**：透传强类型 option |
| Summarization | 未找到稳定 embedded-client 开关 | **No-go**：默认 false，显式拒绝 `summarize=true` |
| Session/resume | 有 thread ID，但未验证跨 Attempt 持久恢复 | **No-go**：仅可靠单轮 |
| Extension/Lane MCP | 上游有相关概念，但未验证 Attempt 私有生命周期与 Lane ownership | **No-go**：`mcp=unsupported`，预检拒绝 MCP 任务 |
| Wire interception | 没有 embedded client 的固定 E2E fixture | **No-go**：拒绝严格 Wire 请求 |
| Provider fallback | Stream 内容可能携带 provider 失败文本但无有效成功结果 | **带 guard 的 Go**：runner 将已识别 fallback error 转为非零退出 |
| Recursion limit | Stream 调用接受 limit | **Go**：1–10,000 的强类型边界及可解释终态 summary |

## 安全与状态边界

每个 Attempt 在 `.agent-runtime/deerflow` 下获得私有 DeerFlow project、home 和 YAML
config。生成的 model config 只包含 `$DEERFLOW_ARENA_MODEL_API_KEY`；实际值通过子进程
环境提供，并从 raw log 与 manifest 脱敏。`HOME`、XDG config/cache、project root 和
DeerFlow config 变量全部指向 Attempt 私有路径。

Workspace bridge 只接受真实 `<attempt>/skill_workspace`，拒绝根 symlink 和嵌套
symlink，并记录 host execution locus 与有效 `allow_host_bash` permission。Prompt
引导 DeerFlow 使用 `/mnt/arena-workspace`，但真正的安全边界是文件系统校验和生成的
mount。

## 离线复现证据

以下 fixture 不需要模型账号，也不包含上游 secret：

- `tests/test_deerflow_profile.py`：pin、probe compatibility、auth 和 MCP no-go 预检。
- `tests/test_deerflow_config.py`：provider config golden、私有状态、路径穿越与 symlink 拒绝。
- `tests/test_deerflow_runner.py`：completed/provider/recursion/bad-event 和 summary 上限。
- `tests/test_deerflow_parser.py`：离线 replay、截断、usage 去重和不可信 summary。
- `tests/test_deerflow_adapter.py`：single/subagent workspace E2E、manifest reconciliation、
  secret 缺失、timeout 与 cancellation process-group cleanup。

这些 fixture 验证固定契约中 agent-arena 一侧。带真实账号、固定 distribution 的 smoke
test 仍是可选部署检查，不进入默认 CI。
