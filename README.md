# agent-lane

[中文文档](README-cn.md)

An open benchmark harness for comparing coding agents on the same tasks —
same prompt, same tools, same scoring. Ships with reference adapters for
**Claude Code** and **Codex** as baselines, plus an extension point for
plugging in *any* other agent — config-only for CLI-based agents, or a
small Python adapter for full control.

Every comparison run captures execution (tool calls, errors, timing),
reasoning (thinking traces, where the agent exposes them), and the final
result (score, code, artifacts) — side by side, for as many agents as you
want to compare.

## Quick start

```bash
uv sync
cp agentlane.yaml.example agentlane.yaml   # defaults work for local, single-machine use
uv run uvicorn backend.main:create_app --factory --port 8100

cd web && npm install && npm run dev
```

Open the frontend (default `http://127.0.0.1:5173`), pick an environment,
select the agents you have installed (`claude-code`/`codex` must be on
`PATH`), and run.

To route claude-code/codex through a third-party model provider (see
`model_providers` in `agentlane.yaml.example`), make sure its API key is
available before starting the backend — either export the env var named by
`api_key_env` (`cp .env.example .env`, fill it in, then `source .env`), or
fill in the provider's `api_key` field directly in `agentlane.yaml` (it's
gitignored). If neither is set for a provider a run references, the attempt
fails immediately with a clear `provider_api_key_missing` error instead of a
confusing CLI login error.

## Included environments

- **order-desk** — a tool-using environment: search a mock book catalog and
  place an order under a budget constraint.
- **cpp-optimizer** — a pure-coding environment: submit a C++17 solution,
  scored by compiling and batch-running it against hidden test cases.

See [docs/environments.md](docs/environments.md) to add your own.

## Docs

- [docs/README.md](docs/README.md) — full design overview
- [docs/architecture.md](docs/architecture.md) — how the pieces fit together
- [docs/environments.md](docs/environments.md) — writing a new evaluation environment
- [docs/agents.md](docs/agents.md) — plugging in a new agent

## License

Apache-2.0 — see [LICENSE](LICENSE).
