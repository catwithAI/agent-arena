# agent-arena

[中文说明](README-cn.md)

An open benchmark harness for comparing coding agents and models on the same
tasks, inputs, budgets, and scoring rules. It includes first-party adapters for
**Claude Code** and **Codex**, supports configuration-only CLI agents, and can
run multi-agent, same-model, or multi-model comparisons.

Each attempt records the agent's execution, exposed reasoning, tool trace,
token usage, artifacts, scores, security observations, and—when enabled—wire
evidence for model and MCP calls. Multi-turn tasks keep a resumable conversation
record and expose context-compaction diagnostics.

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js/npm, and
at least one supported agent CLI (`claude` or `codex`) on `PATH`.

```bash
uv sync
cp arena.yaml.example arena.yaml
uv run uvicorn backend.main:create_app --factory --port 8100

cd web
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. The API is available at
`http://127.0.0.1:8100`; `GET /api/selfcheck` reports configuration,
environment loading, token authentication, and trace-write health.

For third-party model providers, configure `model_providers` in
`arena.yaml`. Keep the API key in the environment variable named by
`api_key_env` (see `.env.example`) or in the gitignored local `arena.yaml`.
Provider references use `<provider>/<model>`, for example
`openrouter/openai/gpt-5`.

## Included environments

| Environment | Focus | Extra requirements |
|---|---|---|
| `order-desk` | MCP tool use and budget constraints | None |
| `cpp-optimizer` | C++17 correctness and optimization | C++ compiler |
| `ad-placement` | Batch-scored heuristic optimization | Linux + C++17 toolchain |
| `apple-incremental-game` | Long-horizon strategy optimization | Python 3 |
| `edgebench-juliet` | Facts-level static vulnerability analysis | Python 3 + Bash |
| `gdpval-prepaid-amortization-db` | Deterministic accounting extraction | Python 3 |
| `gdpval-prepaid-amortization-official` | Official rubric judged Excel deliverable | Anthropic-compatible judge |
| `ppt-visual-repair` | Presentation usability and design taste | LibreOffice + multimodal judge |
| `context-compaction-benchmark` | Multi-turn retention and compaction observability | Multi-turn-capable adapter recommended |

Environment prerequisites are warn-only and shown before submission. Run the
contract linter after adding or changing an environment:

```bash
uv run python scripts/lint_env.py --all
```

## Documentation

- [Chinese documentation index](docs/README.md)
- [Architecture](docs/architecture.md)
- [Agent integration](docs/agents.md)
- [Environment development](docs/environments.md)
- [Environment prerequisites](docs/env-prerequisites.md)

All documents under `docs/` are maintained in Chinese. Environment material
under `envs/*/materials/` may intentionally stay in the task's delivery
language because it is part of the benchmark input.

## Verification

```bash
uv run pytest
uv run ruff check backend lane tests scripts
cd web && npm test && npm run build
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
