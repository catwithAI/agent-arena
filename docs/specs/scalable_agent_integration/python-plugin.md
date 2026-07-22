# Framework-wrapped Python Agent plugins

`agents.python_plugins` registers a data-only descriptor containing an external
`module:attribute` entrypoint. Catalog construction does not import that module;
selection builds the shared wrapper and only then imports optional dependencies.

The entrypoint returns or exposes an object with `run(PythonAgentContext)`. It
returns `PythonAgentOutput` (or an equivalent mapping). The wrapper owns prompt
rendering, uploaded-file staging, exact task MCP resolution, manifest/result
generation, secret redaction, output limits and artifact validation. Reported
artifacts must be existing, relative files resolving inside `skill_workspace`.

This is a correctness boundary, not a hostile-code sandbox. In-process plugins
are trusted Python and could call arbitrary OS APIs. Untrusted SDKs require a
future subprocess/container transport rather than this template.

See [`examples/python_agent_plugin`](../../../examples/python_agent_plugin) for
the minimal implementation and configuration.
