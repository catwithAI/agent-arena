# Harbor Agent 架构映射

研究快照：2026-07-22。上游仓库
`https://github.com/harbor-framework/harbor`，revision
`1393655243125f1d63f81f9bd2f217eefaba3633`（2026-07-20）。

本文是设计比较，不是 runtime 或源码依赖；当前实现没有复制 Harbor 源码。

## 可复用边界

| Harbor 概念 | 可借鉴思路 | Agent Arena 映射 |
|---|---|---|
| `BaseAgent` | 小型生命周期契约（`setup`、`run`、可选 `resume`）及稳定 identity/context | `AgentAdapter`、`AdapterRunInput`、`AdapterResult`、driver contract |
| `BaseInstalledAgent` | 共享版本探测、强类型 CLI/env descriptor、错误分类和 Prompt 渲染 | `AvailabilityService`、`AgentSpec`、`LaunchPlan`、共享错误 taxonomy |
| `AgentFactory` | 名称/import path 解析和 lazy class loading | `AgentRegistry` 与 `ResolvedAgent.build_adapter()` |
| ACP registry shorthand | 一个协议 adapter 解析多个纯数据 registry 条目 | `acp:<id>@<version>` 与共享 `AcpTransportAdapter` |
| Agent context/trajectory | 即使执行失败也保留部分证据和 metadata | raw runtime evidence、`ParseResult`、Agent manifest、Wire coverage |

可复用的是 descriptor、构造、生命周期和证据的分离，不复制实现 class。

## 明确不同的边界

Harbor 的 `BaseAgent` 接收 `BaseEnvironment`；installed Agent 会在任务环境中运行 setup
命令、创建 `/installed-agent`，并可能以 root 或任务用户执行安装。Agent Arena 当前
execution locus 是宿主机或显式声明的远程主机，因此不能照搬这些假设：

- 不允许每轮 package-manager 安装、root setup、NVM/uvx/npx bootstrap 或修改
  container image；
- AgentSpec/runtime 契约不引入 Harbor container path、默认用户、log sync 或环境
  `exec()` API；
- 不拼接 shell string；保持 tokenized argv 和 Attempt 所有的 process group；
- 不隐式继承全局环境，也不让 registry metadata 获得执行授权；
- 不根据 Harbor 的 ACP/DeerFlow 行为推定 Agent Arena 固定版本与工作区拓扑拥有同样能力。

预装 binary、隔离 runtime image 和未来管理员管理的 tool cache 属于独立部署决策。
普通 Run 对 package installation 保持只读。

## Apache-2.0 义务

所检查 revision 包含 Apache License 2.0 `LICENSE`，仓库根目录没有 `NOTICE`。只借鉴
设计并 clean-room 重写，无需复制源码 notice。未来若复制或修改 Harbor 源码，必须：

1. 保留适用的源码版权和 license notice；
2. 随 distribution 包含 Apache-2.0 license；
3. 明确标记修改过的文件；
4. 若后续固定 revision 含 `NOTICE`，复制其中与再分发内容相关的 notice；
5. 增加 attribution，注明上游仓库、固定 commit 和复制文件。

接收复制代码前必须重新审查精确 revision；本文不是对后续 revision 或依赖的通用
license audit。
