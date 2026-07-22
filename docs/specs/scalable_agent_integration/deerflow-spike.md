# DeerFlow v2 integration spike

Date: 2026-07-22

This note freezes the evidence boundary used by agent-arena's first DeerFlow
integration. It does not make the upstream repository a runtime dependency and it
does not claim capabilities that were only observed in another adapter.

## Pin and installation boundary

- Official repository: <https://github.com/bytedance/deer-flow>
- Stable tag: [`v2.0.0`](https://github.com/bytedance/deer-flow/tree/v2.0.0)
- Commit: `7e7f0410797693cf882594555ba414e0361d4c6f`
- Python distribution: `deerflow-harness==2.0.0`
- Required Python: 3.12 or newer, as declared by the pinned harness
  [`pyproject.toml`](https://github.com/bytedance/deer-flow/blob/v2.0.0/backend/packages/harness/pyproject.toml)

Normal attempts never clone or install DeerFlow. Deployment must preinstall the pinned
distribution in the Python environment containing `deerflow-arena-runner`. The runner's
read-only `--probe` checks the distribution version, imports `DeerFlowClient`, and checks
the constructor and stream signatures before an attempt is accepted.

## Probe matrix

| Surface | Pinned evidence | Decision |
| --- | --- | --- |
| Embedded API | [`deerflow.client.DeerFlowClient`](https://github.com/bytedance/deer-flow/blob/v2.0.0/backend/packages/harness/deerflow/client.py) exposes explicit config/model and feature arguments plus streaming | **Go:** use a small versioned runner |
| Model config | Pinned example/config code uses named models and LangChain integration import paths | **Go:** generate one Attempt-private `arena-model` |
| OpenAI chat | `langchain_openai:ChatOpenAI` | **Go:** supported by golden config fixture |
| OpenAI Responses | `ChatOpenAI` with `use_responses_api` and `output_version` | **Go:** supported by golden config fixture |
| Anthropic | `langchain_anthropic:ChatAnthropic` | **Go:** supported by golden config fixture |
| Other providers | No agent-arena fixture at this pin | **No-go:** reject rather than silently map to OpenAI |
| Stream events | Client exposes streamed typed events | **Go:** bounded NDJSON plus an independent bounded summary |
| Local sandbox | Pinned config supports local provider, mounts, and `allow_host_bash` | **Go:** mount only the validated Attempt `skill_workspace` |
| Subagent | Constructor has an explicit enable switch | **Go for execution:** pass the option; identity coverage remains unsupported |
| Thinking / plan mode | Constructor has explicit switches | **Go:** pass typed options |
| Summarization | No stable embedded-client switch was found at this pin | **No-go:** default false; reject `summarize=true` explicitly |
| Session/resume | A thread ID exists, but durable cross-Attempt resume was not validated | **No-go:** reliable single-turn only |
| Extension / Lane MCP | Upstream has extension/MCP concepts, but an Attempt-private embedded-client lifecycle and Lane server ownership contract were not validated | **No-go:** `mcp=unsupported`; compatibility preflight rejects MCP tasks |
| Wire interception | No pinned end-to-end fixture for the embedded client | **No-go:** strict Wire requests are rejected |
| Provider fallback | Stream content can carry provider failure text without a useful successful result | **Go with guard:** runner converts recognized fallback errors to nonzero exit |
| Recursion limit | Stream invocation accepts the limit | **Go:** typed bound 1–10,000 and an explainable terminal summary |

## Security and state boundary

Each attempt receives a private DeerFlow project, home, and YAML config beneath
`.agent-runtime/deerflow`. The generated model config contains only
`$DEERFLOW_ARENA_MODEL_API_KEY`; the value is supplied in the child environment and is
redacted from raw logs and the manifest. `HOME`, XDG config/cache, project root, and
DeerFlow config variables all point at Attempt-private paths.

The workspace bridge accepts only the real `<attempt>/skill_workspace`, rejects a root
symlink and nested symlinks, and records the host execution locus and effective
`allow_host_bash` permission. Prompt steering tells DeerFlow to use
`/mnt/arena-workspace`, but filesystem validation and the generated mount are the
security boundary.

## Offline reproducibility evidence

The following fixtures require no model account and contain no upstream secrets:

- `tests/test_deerflow_profile.py`: pin, probe compatibility, auth and MCP no-go preflight.
- `tests/test_deerflow_config.py`: provider config goldens, private state, workspace traversal
  and symlink rejection.
- `tests/test_deerflow_runner.py`: completed/provider/recursion/bad-event and summary bounds.
- `tests/test_deerflow_parser.py`: offline replay, truncation, usage deduplication and untrusted
  summary handling.
- `tests/test_deerflow_adapter.py`: parameterized single/subagent workspace E2E, manifest
  reconciliation, secret absence, timeout and cancellation process-group cleanup.

These fixtures verify agent-arena's side of the frozen contract. A real-account smoke test
against the pinned distribution remains an optional deployment check and is intentionally
not part of default CI.
