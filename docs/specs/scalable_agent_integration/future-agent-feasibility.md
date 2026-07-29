# 后续 SDK 与远程 Agent 可行性

状态：A8-3 决策记录。Phase 0/1 不要求再增加内置 Agent。

## 准入路径

| 候选形态 | 必需接入路径 | 准入证据 |
|---|---|---|
| 带可信依赖的本地 Python SDK | 框架封装的 `python_plugins` descriptor | 固定 package/version、lazy import 失败隔离、fake E2E、工作区产物检查 |
| 托管异步 Agent API | `RemoteTransportAdapter` 或转换到其 HTTP 契约的 vendor translator | 上传/驻留地披露、server session、timeout/cancel 结果、部分产物 fixture |
| ACP 兼容进程 | 共享 ACP v1 transport | 精确 registry ID/version/hash、预装 executable、permission 与 crash fixture |
| CLI 进程 | profile runtime 或专用 plugin | 确定性 LaunchPlan、生命周期清理、parser/manifest 与 MCP dialect 证据 |

## 具体内置 Agent 的 go/no-go 门

候选项独立排期。只有官方 distribution 可固定、认证可通过引用提供而不保存 secret、
模型选择行为经过测量，并且离开宿主机的所有材料都已披露后，才能进入内置目录。
未确认的远程取消保持 `cancel_requested_remote_unknown`；缺失 usage 或 trajectory
表示 unknown，而不是 0。

Python SDK 只适合管理员预装的可信代码。存在依赖冲突、原生系统 package 或不可信执行
边界时，必须使用子进程/runtime image。无法约束 artifact path 或报告稳定终态 session
的托管服务不具备内置资格，但第三方实验 plugin 可以显式暴露这些限制。

本记录不批准任何 DeerFlow 之后的具体 Agent；每个候选都需要自己的固定版本 spike
和契约 fixture。
