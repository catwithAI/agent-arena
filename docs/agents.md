# Plugging in an agent

agent-arena ships with reference adapters for **Claude Code**, **Codex**,
**Kimi Code**, **MiMo Code** and the pinned **DeerFlow 2** integration, plus
registry-backed extension points for CLI profiles, ACP, trusted Python plugins
and remote services.

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

## Built-in: DeerFlow

The stable `deerflow` descriptor targets `deerflow-harness==2.0.0` at revision
`7e7f0410797693cf882594555ba414e0361d4c6f`. The package and
`deerflow-arena-runner` must be installed by an administrator; a run never
installs or updates them. Each Attempt receives a private DeerFlow project,
home and config plus a checked bridge to `skill_workspace`.

The current integration supports its verified single-turn runner and local
sandbox event stream. Lane MCP, cross-Attempt resume and observable child Agent
identity remain unsupported. See
[the pinned spike](specs/scalable_agent_integration/deerflow-spike.md).

## Built-in (experimental): Kimi Code

The `kimi-code` descriptor uses Kimi Code CLI 0.29 or newer through the shared
local profile runtime:

```text
kimi -p "<prompt>" --output-format stream-json \
  [-m <model>] [--mcp-config-file <generated mcp.json>]
```

- Structured JSONL is mapped into the common event/final-text contract.
- Multi-turn scenarios resume only the explicit `session_id` emitted by the
  first turn; the adapter never selects a "latest" session.
- Declared scenario MCP servers use Kimi's `mcpServers` JSON-file dialect.
- The CLI receives an Attempt-private home, so global Kimi login, sessions,
  skills, plugins and configuration are not inherited. Supply credentials by
  environment. At minimum, set `KIMI_MODEL_NAME` and `KIMI_MODEL_API_KEY`;
  optional `KIMI_MODEL_*` variables select the endpoint and provider protocol.
  When explicitly overriding the arena model, use a Kimi config alias available
  inside the isolated runtime (the environment-defined alias is
  `__kimi_env_model__`).

Install Kimi Code following the
[official repository](https://github.com/MoonshotAI/kimi-code) and ensure
`kimi` is on `PATH`.

## Built-in (experimental): MiMo Code

The `mimo-code` descriptor uses MiMo Code CLI 0.1.7 or newer through the same
profile runtime:

```text
mimo run --format json --dangerously-skip-permissions \
  [--model <provider/model>] "<prompt>"
```

- JSON events expose final text, reasoning, tool activity, aggregate usage and
  an explicit `sessionID` used for safe multi-turn resume.
- Runs use Attempt-private HOME/XDG directories and do not inherit global
  MiMoCode authentication, memory, skills or sessions. `MIMOCODE_AUTH_CONTENT`
  may be supplied by an administrator; MiMo Auto can run without login when
  the installed release makes that channel available.
- Lane MCP injection is currently marked unsupported for this headless CLI
  path; native MiMoCode MCP support is not claimed until its per-run injection
  lifecycle is validated.

Install MiMo Code following the
[official repository](https://github.com/XiaomiMiMo/MiMo-Code) and ensure
`mimo` is on `PATH`.

## Registry-backed Agent configuration

`AgentRegistry` is the source of truth used by the catalog, compatibility
preflight and dispatch. `agents.profiles` describes local CLIs with strict
AgentSpec v1 fields; `agents.acp`, `agents.remote`, and
`agents.python_plugins` use focused configuration shown in
[`arena.yaml.example`](../arena.yaml.example). Legacy `custom_agents` remains
available for migration and appears with `source=legacy` plus a warning.

### ACP v1

ACP entries use exact IDs such as `acp:my-agent@1.2.3`. The configured command
must already be installed and registry metadata must be pinned by SHA-256.
Normal runs never execute package installation from registry `binary`, `npx`,
or `uvx` metadata. One shared transport handles every entry. Unmatched
permission requests are cancelled and fail the Attempt; no allow option is
selected implicitly.

### Remote services

Remote entries disclose endpoint, data residency, source upload policy and
cancellation semantics in the picker. Endpoints require HTTPS. Files are sent
only when `upload_files` is enabled; returned artifacts must be same-origin,
size/checksum verified, and resolve within the Attempt workspace. An
unconfirmed server-side cancellation is recorded as
`cancel_requested_remote_unknown`. See the
[remote contract](specs/scalable_agent_integration/remote-transport.md).

### Trusted Python plugins

`agents.python_plugins` points at an external `module:attribute`, imported only
when selected. The shared wrapper owns prompt/MCP inputs, manifests, redaction,
output limits and artifact validation. These plugins execute inside the backend
process and are trusted code, not a sandbox. Start from the
[example package](../examples/python_agent_plugin/README.md).

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

### 2. A framework-wrapped Python plugin

Implement the small Python plugin contract:

```python
from backend.agents.python_plugin import PythonAgentOutput

class MyAgent:
    async def run(self, context):
        output = context.artifact_path("answer.txt")
        output.write_text("done")
        return PythonAgentOutput(
            final_text="Created answer.txt",
            artifacts=("answer.txt",),
        )
```

Register the external entrypoint under `agents.python_plugins`; do not edit
dispatch. If you truly need a new transport, add one registry builder and keep
its runtime/parser behavior behind the standard `AgentAdapter` result contract.

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
