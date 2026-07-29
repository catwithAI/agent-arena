# agent-arena

[中文文档](README-cn.md)

An open benchmark harness for comparing coding agents on the same tasks —
same prompt, same tools, same scoring. Ships with reference adapters for
**Claude Code**, **Codex**, **Kimi Code**, **OpenCode**, **MiMo Code** and
**DeerFlow** as built-in integrations, plus extension points for
plugging in *any* other agent through a local CLI profile, ACP server, remote
service, or trusted Python plugin.

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
and select the agents you have installed. The corresponding executable
(`claude`, `codex`, `kimi`, `opencode`, `mimo`, or
`deerflow-arena-runner`) must be on `PATH`.

To route claude-code/codex through a third-party model provider (see
`model_providers` in `arena.yaml.example`), make sure its API key is
available before starting the backend — either export the env var named by
`api_key_env` (`cp .env.example .env`, fill it in, then `source .env`), or
fill in the provider's `api_key` field directly in `arena.yaml` (it's
gitignored). If neither is set for a provider a run references, the attempt
fails immediately with a clear `provider_api_key_missing` error instead of a
confusing CLI login error.

## Included environments

- **order-desk** — constrained tool use against a mock book catalog.
- **cpp-optimizer** and **ad-placement** — batch-graded C++17 optimization.
- **apple-incremental-game** — long-horizon Python strategy optimization.
- **edgebench-juliet** — facts-based C/C++ vulnerability analysis.
- **context-compaction-benchmark** — multi-turn context retention and
  compaction observability.
- **gdpval-prepaid-amortization-db** and
  **gdpval-prepaid-amortization-official** — multi-file accounting work,
  scored deterministically or with the official rubric.
- **ppt-visual-repair** — presentation usability and visual-quality repair.

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
