# agent-arena

[中文文档](README-cn.md)

An open benchmark harness for comparing coding agents on the same tasks —
same prompt, same tools, same scoring. Ships with reference adapters for
**Claude Code**, **Codex**, **Kimi Code**, **MiMo Code** and **DeerFlow** as
built-in integrations, plus extension points for
plugging in *any* other agent — config-only for CLI-based agents, or a
small Python adapter for full control.

Every comparison run captures execution (tool calls, errors, timing),
reasoning (thinking traces, where the agent exposes them), and the final
result (score, code, artifacts) — side by side, for as many agents as you
want to compare.

This is an open project for the community, not limited to Claude Code and
Codex — the adapter interface exists so any agent (open-source, commercial,
or a research prototype) can be plugged in. It's also built to scale past
one-shot comparisons: running N agents concurrently, each repeated across
multiple trials on the same task, to get statistically meaningful results
rather than a single noisy run.

## Quick start

```bash
./start.sh
```

The script creates the gitignored `arena.yaml` and installs missing dependencies
on first run. Use `./start.sh --help` for port overrides and backend/frontend-only
modes.

Open the frontend (default `http://127.0.0.1:5173`), pick an environment,
select the agents you have installed (`claude`, `codex`, `kimi`, `mimo`, or
the DeerFlow runner must be on `PATH`), and run.

To route claude-code/codex through a third-party model provider (see
`model_providers` in `arena.yaml.example`), make sure its API key is
available before starting the backend — either export the env var named by
`api_key_env` (`cp .env.example .env`, fill it in, then `source .env`), or
fill in the provider's `api_key` field directly in `arena.yaml` (it's
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
- [docs/experiments.md](docs/experiments.md) — batch experiments and reports

## Batch experiments

Expand tasks, comparison variants and repetitions into a resumable experiment:

```bash
cp experiment.yaml.example experiment.yaml
uv run python scripts/run_experiment.py --config experiment.yaml
```

The output under `data/experiments/<id>/` includes an append-only job journal,
per-attempt results, `summary.json`, and a Markdown report. See
[the experiment guide](docs/experiments.md) for resume and retry behavior.

## License

Apache-2.0 — see [LICENSE](LICENSE).
