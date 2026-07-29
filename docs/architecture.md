# Architecture

## Layout

```
agent-arena/
├── backend/            # FastAPI app: dispatch, execution, evaluation
│   ├── agents/           # registry, shared runtimes, transports and plugins
│   ├── adapters/         # native and legacy compatibility adapters
│   └── *.py              # main/api/config/db/models/runner/evaluator/...
├── lane/                # tiny SDK for environment authors (@env_tool etc.)
├── envs/                # evaluation environments (task defs + tools + scorer)
├── web/                 # React + Vite + TS frontend
├── data/                # runtime data (gitignored): sqlite + attempt files
├── arena.yaml(.example)
└── pyproject.toml
```

## Core concepts

`Task` → `Run` → `Attempt` → `Score`.

- **Task**: a prompt plus context/constraints, either loaded from
  `envs/<name>/tasks/*.json` or created ad hoc from a free-form prompt.
- **Run**: one comparison — a task dispatched to one or more agents.
- **Attempt**: one agent's execution of the task. Its isolated container is
  `data/attempts/<attempt_id>/`; agent-visible files live in the
  `skill_workspace/` child, alongside private runtime/control data, a session
  token and traces that remain outside the workspace.
- **Score**: per-dimension values (0-100) produced by the environment's
  scorer, aggregated into `score_total` by weight.

## Request flow

1. `POST /api/runs` creates a `Task` (if needed), a `Run`, and one `Attempt`
   per requested agent, then dispatches them concurrently as background
   tasks (`backend/run_dispatch.py`).
2. `AgentRegistry` resolves the requested built-in or configured AgentSpec,
   checks task/model/MCP/conversation compatibility, and constructs the
   matching profile runtime, ACP/remote transport, Python plugin, or native
   compatibility adapter. All paths implement the common `AgentAdapter`
   result contract.
3. If the scenario's `meta.yaml` declares an `entrypoints.mcp` server (see
   [environments.md](environments.md)) and the agent chooses to call it, the
   MCP call forwards to the **attempt server**
   (`backend/env_attempt_server.py`) via `POST
   /attempts/{attempt_id}/tools/{tool_name}`, authenticated with a
   per-attempt bearer token. This is the same HTTP path regardless of which
   agent is calling — traces line up across agents for free. The MCP process
   receives the real Attempt workspace as `LANE_WORKSPACE` while its declared
   command is resolved from the project root. Scenarios that declare no MCP
   server give the agent no environment tools at all; the framework never
   fabricates one.
4. Once the adapter finishes, `backend/runner.py` calls the environment's
   `scorer.py` (`backend/evaluator.py`), writes scores, and finalizes the
   attempt's status.
5. The frontend polls `GET /api/runs/{id}` and `GET
   /api/runs/{id}/attempts/{id}` to show live progress and, once done, the
   full transcript/scores/artifacts.

## Isolation

Every attempt has an agent-visible
`data/attempts/<attempt_id>/skill_workspace/`. Local runtimes use it as the
process `cwd` (and pass an explicit workspace flag when the CLI requires one),
so submissions land where scorers expect them. Framework metadata, manifests,
events and private Agent configuration stay in sibling paths under the Attempt
root. Nothing is shared between attempts — not even within the same run.

Built-in local integrations point their Agent-specific HOME/config/session
locations at Attempt-private directories. A run therefore does not pick up the
operator's global skills, plugins, MCP servers, memories or sessions. This is
local-state isolation, not a capability restriction — see "Capability
fairness" below.

## Capability fairness

agent-arena compares each agent's **full native capability set**. "Fair"
means the same task, input materials, time budget, and external-resource
boundaries — not the same tool set. Faced with the same task, Claude Code
might reach for WebSearch, Codex might reach for shell/Python, and a
third-party agent plugged in via `custom_agents` might use whatever it has;
that difference is itself part of the result, not noise to eliminate.

- Adapters must not disable an agent's native tools, skills, or
  task-decomposition ability in order to make agents "comparable."
- Adapters must not hardcode a preferred solving method (MCP, curl, Python,
  ...) into the prompt.
- MCP/skill capability is only ever wired up when a scenario's `meta.yaml`
  explicitly declares it (`entrypoints.mcp`) — the framework wires up
  exactly what's declared and never infers or fabricates a server.
- Host-local state (private configs, credentials, plugins) is still
  isolated so one operator's machine doesn't bias the comparison.

## Extension points

- **New agent**: add a strict AgentSpec profile for a regular local CLI, or
  configure an ACP server, remote service, or trusted Python plugin under
  `agents` in `arena.yaml`. Legacy `custom_agents` remains available during
  migration. Implement a focused adapter only when a new runtime contract
  cannot fit an existing transport — see [agents.md](agents.md).
- **New environment**: add a directory under `envs/` — see
  [environments.md](environments.md).
