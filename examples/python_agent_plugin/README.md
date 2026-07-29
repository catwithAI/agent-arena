# Python Agent plugin 示例

将 package 放到 Agent Arena 的 Python path 中，或安装到 server 管理的环境，然后配置：

```yaml
agents:
  python_plugins:
    example-python:
      entrypoint: example_agent:ExampleAgent
      display_name: Example Python Agent
      package_name: example-agent
      package_version: 0.1.0
```

目录只读取此 descriptor。选中 `example-python` 时才导入模块及其可选依赖。Plugin 是
受信任的进程内代码，不是安全 sandbox。请使用 `context.artifact_path()`，并把每个
artifact 上报为相对工作区的路径。
