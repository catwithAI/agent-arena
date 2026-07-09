# Writing an environment

An environment is a directory under `envs/<name>/` that bundles a task, the
tools an agent may need to complete it, and how to score the result.

## Minimal layout

```
envs/<name>/
├── meta.yaml         # required: type, pass_threshold, dimensions, entrypoints
├── core.py           # tool implementations (@env_tool), or empty for pure-coding tasks
├── scorer.py          # required: score(...) -> list[dict]
├── mcp_server.py       # required only if core.py registers tools
├── schema.sql          # optional: per-attempt sqlite schema, if tools need state
└── tasks/*.json         # pre-defined tasks
```

## `meta.yaml`

```yaml
name: my-env
type: skill              # skill (has tools) | coding (submission-only)
category: baseline
description: One-line summary shown in the UI.
test_focus: What this environment is actually testing.
pass_threshold: 60        # score_total >= this -> attempt status "completed"

entrypoints:
  mcp:
    enabled: true          # false for pure-coding envs with no tools
    transport: stdio
    command: ["uv", "run", "--project", ".", "python", "envs/my-env/mcp_server.py"]

dimensions:
  - name: task_completion
    weight: 60
    description: ...
  - name: constraint_compliance
    weight: 40
    description: ...
```

Weights determine the aggregation of `scorer.py`'s output into
`score_total` (weighted average, 0-100). If all weights are 0/missing, a
simple average is used instead.

## Tools (`core.py`)

Only needed if `type: skill`. Decorate plain functions with `@env_tool`;
the wrapper handles trace-writing and timing automatically — your function
just returns a JSON-serializable value.

```python
from lane.env_api import EnvContext, env_tool

@env_tool(
    name="my_tool",
    description="What this tool does, shown to the agent.",
    parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
)
def my_tool(ctx: EnvContext, x: str) -> dict:
    # ctx.db is a sqlite3.Connection scoped to this attempt (schema.sql applied lazily)
    # ctx.trace is handled automatically — you don't call it directly
    return {"result": x.upper()}
```

Persist state via `ctx.db`, backed by `schema.sql` in the same directory,
applied idempotently on first use.

## `mcp_server.py`

A thin MCP stdio wrapper that forwards each tool call to the attempt server
over HTTP — copy `envs/order-desk/mcp_server.py` and adjust the tool
signatures to match `core.py`. This file is required whenever `core.py`
registers tools; it's what Claude Code / Codex actually talk to.

## Scorer (`scorer.py`)

```python
def score(*, attempt_id, task, env_db, trace, final_state) -> list[dict]:
    # env_db: Path to this attempt's sqlite file (populated by your tools)
    # trace: parsed trace.jsonl — every tool call, in order, with timing
    # final_state: parsed final_state.json, if your env writes one
    return [{"dimension": "task_completion", "value": 90, "detail": "..."}]
```

For pure-coding environments (no tools), the scorer typically compiles and
runs whatever the agent wrote into the attempt's working directory — see
`envs/cpp-optimizer/scorer.py` for a batch-graded example (compile once,
run against N hidden fixtures, normalize to 0-100).

## Tasks

`tasks/*.json`:

```json
{
  "id": "my_task_001",
  "prompt": "What the agent should do.",
  "context": {},
  "constraints": { "any_key": "used_by_your_scorer" },
  "timeout_seconds": 600
}
```

`context` is rendered into the agent's prompt (minus internal/uploaded-file
bookkeeping, which adapters render separately); `constraints` is opaque to
the framework and read directly by your `scorer.py`.

## Reference examples

- `envs/order-desk/` — tool-using environment: mock catalog search + order
  placement, budget-constraint scoring.
- `envs/cpp-optimizer/` — pure-coding environment: no tools, agent submits
  `solution.cpp`, scorer compiles and batch-grades it against fixed hidden
  cases.
