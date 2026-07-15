# Plugging in an agent

agent-lane ships with two reference adapters — **Claude Code** and
**Codex** — and an open extension point for anything else.

## Built-in: Claude Code

`backend/adapters/claude_code.py` spawns:

```
claude -p "<prompt>" --output-format stream-json --verbose \
  --mcp-config <generated mcp_config.json> --model <model> \
  --max-budget-usd <budget> --dangerously-skip-permissions
```

- Parses `stream-json` stdout line by line: `type=assistant` turns carry
  `thinking`/`text`/`tool_use`/`tool_result` blocks, `type=result` is the
  final summary (cost, token usage, success/error).
- Environment tools are exposed via a per-attempt MCP stdio server
  (`envs/<env>/mcp_server.py`), configured through a generated
  `mcp_config.json` so concurrent attempts never share state.
- To route through a third-party model provider, prefix the model with the
  provider name configured in `agentlane.yaml`'s `model_providers` (e.g.
  `"openrouter/glm-5"`) — the adapter injects `ANTHROPIC_BASE_URL` /
  `ANTHROPIC_AUTH_TOKEN` into the subprocess env rather than touching your
  global `claude` settings.

## Built-in: Codex

`backend/adapters/codex.py` spawns:

```
codex exec --json --skip-git-repo-check --ephemeral --ignore-rules \
  --dangerously-bypass-approvals-and-sandbox \
  -C <attempt_dir> -o <final_message_path> \
  -c mcp_servers.<name>.command=... [-c model_providers.*...] \
  "<prompt>"
```

- Uses one-shot `-c key=value` overrides instead of a config file, so it
  never touches your global `~/.codex/config.toml`.
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
once `ssh_claude_code.ssh_host` is set in `agentlane.yaml` (or via
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
  host ahead of time; `envs/<env>/mcp_server.py` is copied there per attempt.
- Wire observability does not apply: the remote CLI has no local
  spool/injection channel, so `wire_capture_capabilities` declares every
  field unsupported.

## Bringing your own agent

Two ways in, from least to most control:

### 1. Config-only, via `CustomCliAdapter`

If your agent is a CLI that takes a prompt and prints output, no Python
required — describe it in `agentlane.yaml`:

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
        # let it call envs/<task.env_name>/mcp_server.py if it supports MCP
        # (env vars: LANE_ATTEMPT_ID, LANE_SESSION_TOKEN, LANE_BASE_URL),
        # and never raise — wrap failures into AdapterResult(status=...).
        ...
```

Register it in `backend/run_dispatch.py::build_adapter`. This is the path
`ClaudeCodeAdapter`/`CodexAdapter` themselves take — reach for it when you
need behavior `CustomCliAdapter`'s config surface can't express (custom
retry logic, a non-CLI transport, bespoke usage accounting).

## Fairness notes

Every adapter renders the task prompt through the same
`prompt_context()` helper (`backend/adapters/base.py`) so agents see
identically-shaped input regardless of which one is running. Attempts are
isolated by directory and session token — nothing about one attempt is
visible to another, even within the same run.
