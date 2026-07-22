# Python Agent plugin example

Place the package on the Agent Arena Python path (or install it in the server's
managed environment), then configure:

```yaml
agents:
  python_plugins:
    example-python:
      entrypoint: example_agent:ExampleAgent
      display_name: Example Python Agent
      package_name: example-agent
      package_version: 0.1.0
```

The catalog reads only this descriptor. The module and its optional dependencies
are imported when `example-python` is selected. Plugins are trusted in-process
code, not a security sandbox. Use `context.artifact_path()` and report every
artifact as a workspace-relative path.
