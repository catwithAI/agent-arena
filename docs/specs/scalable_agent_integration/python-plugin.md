# 框架封装的 Python Agent plugin

`agents.python_plugins` 注册只含数据的 descriptor，其中包含外部
`module:attribute` entrypoint。构建目录时不导入模块；只有选中 Agent、创建共享
wrapper 后才导入可选依赖。

Entrypoint 返回或暴露带 `run(PythonAgentContext)` 的对象，并返回
`PythonAgentOutput` 或等价 mapping。Wrapper 负责 Prompt 渲染、上传文件 staging、
精确的 task MCP 解析、manifest/result 生成、secret 脱敏、输出限制和 artifact 校验。
上报的 artifact 必须是已存在的相对路径，并解析到 `skill_workspace` 内。

这是正确性边界，不是针对恶意代码的 sandbox。进程内 plugin 属于可信 Python，可调用
任意 OS API。不可信 SDK 应使用未来的子进程/container transport。

最小实现与配置见
[`examples/python_agent_plugin`](../../../examples/python_agent_plugin)。
