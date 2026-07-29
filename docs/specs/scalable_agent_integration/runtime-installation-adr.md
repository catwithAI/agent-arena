# ADR：Agent runtime 安装与隔离

- 状态：首版已接受
- 日期：2026-07-22
- 范围：DeerFlow、ACP distribution、Python SDK plugin 和未来本地 Agent

## 决策

普通 Run 不能安装或更新 Agent 软件。首版由部署管理员提供固定版本的宿主机 executable
和环境；Agent Arena 只读探测，并记录 package/revision/spec hash。ACP registry 条目
仅是 metadata，不能授权 npx/uvx/binary 下载。Python plugin 只能从 server 管理的
Python 环境导入。

当前不引入共享、可变的 Agent 级 uvx/npx cache。对于依赖冲突、原生 package、不可信
代码或宿主机预装无法满足的复现要求，固定版本 runtime image 是首选未来隔离机制。
Image 必须在创建 Attempt 前选定，并以不可变 digest 写入 manifest。

## 方案比较

| 方案 | 公平性/可复现性 | 启动/性能 | 供应链与隔离成本 | 决策 |
|---|---|---|---|---|
| 固定版本宿主机预装 | version probe 与 manifest 一致时可接受，仍可能存在宿主机漂移 | 最快，无每轮 setup | 管理员负责安装；依赖隔离最弱 | 当前默认 |
| 共享 uvx/npx tool cache | cache 冷热可能因 Attempt 不同，可变 tag 可能漂移 | 冷下载慢，热启动快 | 需要 lock/checksum、并发、淘汰与投毒防护 | 延后 |
| 不可变 runtime image | 复现性和依赖隔离最强 | 有 image pull/冷启动成本，热执行可预测 | 需要 image build、SBOM、签名、digest pin 和 sandbox 运维 | 未来首选 |

## 当前集成的证据

DeerFlow 需要固定 Python harness、私有 project/home/config 和工作区 bridge。在计时
Attempt 内安装会把 setup 速度混入 Agent 质量。ACP registry 的 npx/uvx 条目可能触发
网络解析和 lifecycle script，binary 条目则需要归档校验。两者都说明隐式安装会破坏
公平性并扩大供应链边界。

## 后果

- Run 前 availability 可能报告未安装或版本不支持；
- 部署文档必须列出精确前置条件；
- 缺少固定 runtime 时不能回退到 package manager 命令；
- 未来 cache/image 工作需要管理员 API 或 build pipeline、不可变
  lock/checksum/digest、清理责任和 manifest 字段；
- 进程内 Python plugin 必须明确标记为可信，不能描述成已隔离。
