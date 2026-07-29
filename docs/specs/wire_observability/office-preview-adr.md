# ADR：W7 Office artifact 预览边界

- 状态：W7 实现已接受
- 日期：2026-07-14
- 契约：`lane-artifact-preview-v1`

## 决策

Office 文件是不可信业务 artifact。agent-arena 在服务端分类和渲染；浏览器不会把
OOXML ZIP 当文本接收，也不会执行 formula、macro、embedded object、script、data
connection 或 external relationship。

公共 endpoint 刻意分离：

- `GET .../artifacts`：按内容推导的 type 与 MIME 列出文件；
- `GET .../artifacts/{ref}`：返回逐字节一致的原始文件；
- `GET .../artifact-previews/{ref}`：返回带版本的 preview descriptor，不返回原始
  Office bytes；
- 未来渲染资源只允许通过 descriptor 拥有的不透明 ref 访问。

Descriptor 包含原 artifact identity、状态
`ready|rendering|unsupported|failed`、可观测的 slide/page/sheet 数、renderer
名称/版本、content hash cache key、稳定 error code、安全观察和明确的 capability gap。
预览失败不会删除或改变原文件下载。

## Renderer 划分

- **PPTX**：内置隔离 worker 为 slide、定位文本、table、安全 raster image 和 speaker
  note 生成有界静态 layout IR。不执行 transition/animation，不加载外部资源。
  Theme/group transform 和 chart 明确记为 fidelity gap。未来可增加 sandboxed
  LibreOffice/PDF renderer。
- **DOCX**：内置隔离 worker 为 heading、paragraph、run、list、table、安全外链、
  header/footer 生成语义 IR。Pagination 为近似值；embedded image/comment/note 是明确
  gap。未来 PDF renderer 只是可选 fidelity 增强。
- **XLSX**：有界结构 parser 生成 workbook/sheet/cell JSON。可展示 formula string
  和已有 cached value，但绝不计算 formula；浏览器渲染有界 grid 并虚拟化 row。
- **旧 PPT/DOC/XLS**：在相同 sandbox worker 可用且 fidelity fixture 通过前只允许
  下载，不使用进程内旧 Office parser。
- **启用 macro 的 OOXML**：只有从 renderer 输入移除 active part 后才允许静态预览；
  macro 和 embedded OLE/ActiveX 永不执行。

Renderer 输出缓存在 Attempt 的框架目录 `artifact-previews/` 下，不进入普通 artifact
列表或下载。Cache identity 包含源码 SHA-256、契约版本、renderer/版本和 option；
源码变化必然产生新 key。

## 限制与调度

任何 renderer 前先运行 preflight scanner，当前限制为：

- 源文件：128 MiB；
- ZIP entry：10,000；
- 总解压字节：512 MiB；
- 单个解压 entry：64 MiB；
- 每个 entry 压缩比：200:1；
- relationship XML：每个 entry 最多检查 2 MiB。

不超过 20 MiB 的文件可在 15 秒 deadline 内同步渲染。更大但可接受的文件使用后台
job 与 descriptor polling（`status=rendering`、`poll_after_ms`）；W7 不新增 SSE。
Worker 有 renderer 专属 hard timeout（当前结构化 XLSX worker 为 15 秒，未来 converter
最多 60 秒），部署还必须限制 CPU、memory、process、filesystem 并禁网。隔离控制或
renderer 不可用时返回 `unsupported`/`renderer_unavailable`，不得静默运行未 sandbox
的 converter。

读取前检查每个 ZIP member 的 absolute path 和 `..` traversal。External relationship
只报告、不获取；XML parser 必须 entity-safe 且有界。每次 renderer 使用新的临时
cwd、最小环境且不继承凭证。父进程复制 artifact，并在 worker 打开前按 cache hash
校验 snapshot；并发变化返回可重试的 `artifact_changed`，不污染 cache。Timeout/crash
只产生稳定 preview error，不得使 Attempt 或 RunDetail 页面失败。

内置 PPTX、DOCX、XLSX renderer 复用同一隔离 Python 子进程契约：`python -I`、新的
临时 cwd/HOME/TMPDIR、最小环境、禁用 socket、可用时设置 POSIX
CPU/address-space/file-size/file-descriptor limit，并由父进程执行 15 秒 hard timeout。
结果写入 output-file envelope，不走 stdout，上限 32 MiB。稳定结果原子缓存到
`artifact-previews/<composite-sha256>/`；忽略 symlink cache root/entry。
Timeout/crash/invalid-output 是瞬时结果，不缓存。未来 LibreOffice worker 仍需要
container 级 seccomp/network/filesystem 加固；内置 worker 不调用第三方 binary。

大型 XLSX 使用最多两个 thread、最多 256 个去重 job 的有界 executor。Identity 是解析
后的 artifact path 与 composite content key。首次请求返回 `rendering` 和 500 ms
polling hint；并发 poll 复用同一 future，完成 descriptor 留在有界 job table 中直到
正常淘汰。队列饱和返回 `renderer_queue_full`，不得无限排队。浏览器将 hint 限制在
10 ms–5 s，最多 poll 120 次，并在用户切换或关闭 artifact 时取消 timer/fetch。

## 发布步骤

W7-1 交付 descriptor、内容分类、有界 OOXML preflight、逐字节一致下载，以及 UI 的
loading/error/unsupported shell。W7-2、W7-3、W7-4 在同一契约后增加各 renderer
资源；W7-5 完成导航与对比行为，不改变 artifact trust 或 wire-blob policy 边界。
