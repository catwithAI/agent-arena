# Arena Remote Agent HTTP 契约 v1

本契约是 Agent Arena 与 vendor adapter 之间的边界，不宣称是行业协议。Vendor SDK
plugin 将自身 API 转换为这些语义，同时仍返回标准 `AdapterResult` 与 Agent manifest。

## Session 生命周期

`POST /v1/sessions` 接收 `protocolVersion: arena-remote-v1`、Attempt ID、有序 turn、
可选 requested model，以及明确允许上传的 file payload。响应包含 `sessionId`、
`status`，以及可选的同源 `pollUrl` 或 `streamUrl`。

轮询 snapshot 和换行分隔 stream snapshot 使用
`queued|running|completed|failed|cancelled`，只有后三种是终态。Event 和 usage 始终
是服务提供的证据，框架不会推断。

`DELETE /v1/sessions/{id}` 只有在确定服务端已停止时才返回 `confirmed: true`。
false、格式错误、网络失败或缺少响应都映射为
`cancel_requested_remote_unknown`。本地超时仍记为 `agent_timeout`，并独立记录
远端取消状态。

## 数据与 artifact

选择前，目录 UI 会披露 endpoint、声明的数据驻留地、是否允许源码上传和取消语义。
只有管理员配置允许上传时才发送文件内容。Request summary 和 manifest 只记录文件名、
数量、大小和 hash，不记录文件内容或 API key。

Artifact URL 必须与服务同源。路径必须解析到 `skill_workspace` 内，并在接收前校验声明
size、配置限制和 SHA-256。单个 artifact 失败会产生 `artifacts=partial`，不会丢弃
已经验证的 artifact，也不会把已完成任务改成虚假的执行失败。

远程服务在 v1 中不能访问 task-local MCP server。Vendor plugin 不得绕过 Attempt
identity、工作区 artifact 边界、timeout 处理或 manifest 脱敏。
