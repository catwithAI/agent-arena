# agent-arena

An open benchmark harness for comparing coding agents on the same tasks.

The goal isn't a public leaderboard — it's giving anyone a way to compare
*their own* agent against real baselines using a fair, repeatable setup:
same prompt, same tools, same scoring.

Every comparison run captures three things:

1. **Execution** — tool calls, errors, retries, timing.
2. **Reasoning** — task understanding, planning, decision points, corrections
   (where the agent exposes it — e.g. Claude Code's `thinking` blocks).
3. **Final product** — resulting state, code files, test results, a score.

## Baselines

**Claude Code**, **Codex**, **Kimi Code**, **MiMo Code** and **DeerFlow** ship
as built-in integrations. They use their native CLI or pinned runner behind
the common adapter contract — no special model access.
Anything else — your own agent, a research prototype, an internal tool —
plugs in through the same `AgentAdapter` interface, either by writing one
small Python file or, for CLI-based agents, through pure YAML config (see
[agents.md](agents.md)).

## How a run works

1. Pick an **environment** (`envs/<name>/`) — a task definition plus
   whatever tools the task needs (or none, for pure coding tasks).
2. Pick one or more **agents** to run the same task.
3. Each attempt runs isolated: its own working directory, its own session
   token, its own trace file. Agents cannot see or interfere with each
   other's runs.
4. A per-environment **scorer** reads the trace and final state, and
   produces a weighted 0-100 score.
5. Compare side by side: transcripts, tool calls, timing, token usage,
   scores.

## Stack

Python 3.11+ / FastAPI / SQLite / uv (backend) — React + Vite + TypeScript
(frontend).

## Docs

- [architecture.md](architecture.md) — how the pieces fit together
- [environments.md](environments.md) — how to write a new evaluation
  environment
- [agents.md](agents.md) — how to plug in a new agent

## Quick start

```bash
./start.sh
```

Open the frontend, pick the `order-desk` environment, select one or more
installed Agents, and run. A built-in Agent reports as available only when its
CLI or runner is installed on `PATH` at a supported version.
