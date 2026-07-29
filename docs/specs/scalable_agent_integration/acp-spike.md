# ACP v1 transport 决策记录

状态：A7 实现已接受（2026-07-22）。

## 版本固定与 client 边界

Agent Arena 固定稳定版 ACP major protocol `protocolVersion: 1`，不协商仍处于 Draft
阶段的 ACP v2。Transport 是基于 stdio 的轻量异步 JSON-RPC 2.0 client，每条 UTF-8
消息是一个换行分隔 JSON object。

运行时不额外依赖 Python SDK。所需 client surface 仅包括 `initialize`、
`session/new`、`session/prompt`、`session/update`、
`session/request_permission` 和 `session/cancel`。将其保留在项目内，便于进程清理和
证据采集复用 Arena 的共享 runtime 规则。

一个 ACP 子进程和 session 只属于一个 Attempt，多轮 Prompt 复用该 session。超时或
取消时，先发送 `session/cancel` notification，再终止 process group。Agent 可以在
Prompt response 前发出 final update，client 仍会接受。

## Permission 与 client capability

首版 client 不声明 filesystem 或 terminal capability。Permission answer 必须为指定
tool call 显式选择 option ID。没有匹配配置时，client 返回 ACP 的 `cancelled`
permission outcome，并记录 permission coverage 降级；绝不猜测 allow option。

## Registry 与分发供应链

只解析 `acp:<id>@<version>` 形式的稳定 ID。Registry 文档必须使用 HTTPS、通过支持的
v1 schema 校验，并逐字节存入 content-addressed cache。解析出的 SHA-256 进入
descriptor/run pin。

离线解析会重新哈希原始 cache blob；缺少引用、checksum 不匹配、metadata 重复、
精确版本不存在或 schema 损坏时均 fail closed。

Registry distribution metadata 不是可信代码。普通 Run 不会下载或解压归档，不会执行
npx/uvx 安装，也不会修改 package 状态。管理员必须另行预装和配置 executable；
binary archive URL 与 checksum 只供管理员安装流程参考。

## Transcript 覆盖

契约 fake server 覆盖两类 transcript：带 thinking/tool/usage event 的正常单轮/多轮
update，以及需要显式 deny 或未配置 cancellation 的 permission request。另有用例覆盖
protocol mismatch、server crash 和 timeout cleanup。真实 registry Agent smoke test
为可选项，不进入默认 CI。

官方参考：

- https://agentclientprotocol.com/protocol/v1/overview
- https://agentclientprotocol.com/protocol/v1/transports
- https://agentclientprotocol.com/protocol/v1/initialization
- https://agentclientprotocol.com/protocol/v1/session-setup
- https://agentclientprotocol.com/protocol/v1/prompt-turn
- https://github.com/agentclientprotocol/registry/blob/main/FORMAT.md
