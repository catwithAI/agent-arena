# Plugging in an agent

agent-arena ships with two reference adapters — **Claude Code** and
**Codex** — and an open extension point for anything else.

Both reference adapters preserve each agent's full native capability set
(WebSearch, subagent/task delegation, skills, slash commands, whatever the
CLI ships with). Adapters only isolate the *host's* local state — see
[Fairness notes](#fairness-notes) below.

## Built-in: Claude Code

`backend/adapters/claude_code.py` spawns:

```
claude -p "<prompt>" --output-format stream-json --verbose \
  --model <model> --max-budget-usd <budget> \
  --dangerously-skip-permissions \
  [--mcp-config <generated mcp_config.json>]
```

- Parses `stream-json` stdout line by line: `type=assistant` turns carry
  `thinking`/`text`/`tool_use`/`tool_result` blocks, `type=result` is the
  final summary (cost, token usage, success/error).
- `CLAUDE_CONFIG_DIR`/`HOME` point at a clean, per-attempt directory so the
  CLI never reads the host operator's global `~/.claude` (skills, plugins,
  MCP servers, memory, `CLAUDE.md`, settings). This is state isolation, not
  a capability restriction — the CLI's own native tools are untouched.
- `--mcp-config` is only passed when the scenario's `meta.yaml` declares an
  `entrypoints.mcp` server (see [environments.md](environments.md)); the
  adapter never guesses a server path or name from `env_name`. Scenarios
  without a declared MCP server run with no MCP config at all.
- To route through a third-party model provider, prefix the model with the
  provider name configured in `arena.yaml`'s `model_providers` (e.g.
  `"openrouter/glm-5"`) — the adapter injects `ANTHROPIC_BASE_URL` /
  `ANTHROPIC_AUTH_TOKEN` into the subprocess env rather than touching your
  global `claude` settings.

## Built-in: Codex

`backend/adapters/codex.py` spawns:

```
codex exec --json --skip-git-repo-check --ephemeral --ignore-rules \
  --dangerously-bypass-approvals-and-sandbox \
  -C <attempt_dir> -o <final_message_path> \
  [-c mcp_servers.<name>.command=... ...] [-c model_providers.*...] \
  "<prompt>"
```

- Uses one-shot `-c key=value` overrides instead of a config file, so it
  never touches your global `~/.codex/config.toml`.
- `CODEX_HOME` points at a clean, per-attempt directory for the same reason
  as Claude Code's `HOME` isolation above — no global `config.toml`,
  skills, plugins, memories or history leak into the run.
- `mcp_servers.*` overrides are only emitted when the scenario declares an
  MCP server; attempt credentials (`LANE_ATTEMPT_ID`/`LANE_SESSION_TOKEN`/
  `LANE_BASE_URL`) are never placed in a `-c` argument (visible to anything
  reading the process list) — they only reach the subprocess environment,
  and only when an MCP child that needs them is actually going to spawn.
- Third-party providers require an endpoint that speaks the OpenAI
  Responses API (`wire_api: responses`) — Codex no longer supports the
  legacy `chat` wire protocol for custom providers.

## Built-in (optional): Claude Code over SSH

`backend/adapters/ssh_claude_code.py` runs the same `claude -p ... --output-format
stream-json` flow as the local Claude Code adapter, but on a remote machine
reached via `ssh`/`scp` instead of a local subprocess — useful when the agent
needs to run in a different network/filesystem context than this backend
(e.g. a dedicated worker host).

It is disabled by default and only registers as the `"ssh-claude-code"` agent
once `ssh_claude_code.ssh_host` is set in `arena.yaml` (or via
`LANE_SSH_CLAUDE_HOST` / `LANE_SSH_CLAUDE_USER` / `LANE_SSH_CLAUDE_PASSWORD`):

```yaml
ssh_claude_code:
  ssh_host: "10.0.0.5"
  ssh_user: "ai"
  ssh_password: "..."   # prefer LANE_SSH_CLAUDE_PASSWORD instead of committing this
  max_budget_usd: 5.0
```

- The prompt and MCP config are written to local files and uploaded via SCP
  rather than interpolated into the SSH command line, so a task prompt
  containing quotes/newlines/backticks can never be interpreted as shell
  syntax on the remote end.
- The remote MCP server is expected to run from a fixed venv path
  (`/tmp/lane-mcp-venv/bin/python`) that must be provisioned on the remote
  host ahead of time; the Python entrypoint named in the scenario's declared
  `entrypoints.mcp.command` is copied there per attempt (this adapter
  currently supports exactly one declared MCP server per scenario).
- Wire observability does not apply: the remote CLI has no local
  spool/injection channel, so `wire_capture_capabilities` declares every
  field unsupported.

## Bringing your own agent

Two ways in, from least to most control:

### 1. Config-only, via `CustomCliAdapter`

If your agent is a CLI that takes a prompt and prints output, no Python
required — describe it in `arena.yaml`:

```yaml
custom_agents:
  my-agent:
    command: ["my-agent-cli", "--prompt-file", "{prompt_file}"]
    prompt_mode: file        # stdin | file | arg — how the prompt reaches the CLI
    output_format: text      # text | jsonl
    # If your agent emits JSONL events and you want thinking/usage extracted:
    # output_format: jsonl
    # jsonl_fields:
    #   type_field: "type"
    #   thinking_type_value: "reasoning"
    #   text_field: "text"
    #   usage_field: "usage"
    # If your agent supports MCP and you want it to reach env tools:
    # mcp_config_flag: "--mcp-config"
```

The agent then shows up as `"my-agent"` in `POST /runs`'s `agents` list and
in the frontend's agent picker, exactly like `claude-code`/`codex`. See
`backend/adapters/custom_cli.py` for the full field reference.

### 2. A Python adapter, for full control

Implement the `AgentAdapter` protocol (`backend/adapters/base.py`):

```python
class MyAdapter:
    async def run(self, task: AdapterRunInput, env, data_path: Path) -> AdapterResult:
        # spawn your agent, feed it task.task_prompt / task.task_context,
        # and if your CLI supports MCP, wire up whatever's in
        # task.mcp_servers (each a base.McpServerSpec: name/command/args/cwd,
        # taken verbatim from the scenario's entrypoints.mcp declaration —
        # don't invent a server from task.env_name). Credentials for env
        # vars: LANE_ATTEMPT_ID, LANE_SESSION_TOKEN, LANE_BASE_URL.
        # Never raise — wrap failures into AdapterResult(status=...).
        ...
```

Register it in `backend/run_dispatch.py::build_adapter`. This is the path
`ClaudeCodeAdapter`/`CodexAdapter` themselves take — reach for it when you
need behavior `CustomCliAdapter`'s config surface can't express (custom
retry logic, a non-CLI transport, bespoke usage accounting).

## Fairness notes

"Fair comparison" in agent-arena means the same task, the same input
materials, the same time/budget limits and the same external-resource
boundaries — it does not mean trimming every agent down to an identical
tool set. Claude Code, Codex, and whatever you plug in via `custom_agents`
keep their full native capabilities (WebSearch, subagent delegation,
skills, slash commands, ...); which of those an agent chooses to use for a
given task is itself part of what the comparison measures.

What adapters *do* normalize:

- **Prompt shape** — every adapter renders the task prompt through the same
  `prompt_context()` helper (`backend/adapters/base.py`) so agents see
  identically-shaped input, and no adapter hardcodes a preferred solving
  method ("you must use tool X") into the prompt.
- **Host isolation** — `CLAUDE_CONFIG_DIR`/`HOME` (Claude Code) and
  `CODEX_HOME` (Codex) point at a clean, per-attempt directory so a run
  never inherits whoever operates the box's personal global config. This is
  about not leaking private local state into results, not about limiting
  what the agent can do.
- **Attempt isolation** — each attempt gets its own working directory,
  session token, and env server session; nothing about one attempt is
  visible to another, even within the same run.

What adapters do *not* do: they don't disable an agent's built-in tools,
skills, or task-decomposition ability to make agents "comparable," and they
don't invent MCP servers. Environment tools reach an agent only if the
scenario's `meta.yaml` explicitly declares an `entrypoints.mcp` server (see
[environments.md](environments.md)) — the framework wires up exactly what's
declared and nothing more.
