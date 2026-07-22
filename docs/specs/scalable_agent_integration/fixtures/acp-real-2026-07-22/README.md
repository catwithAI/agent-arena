# ACP real smoke evidence · 2026-07-22

- execution: macOS arm64, local preinstalled subprocesses
- registry schema: `1.0.0`
- registry SHA-256: `799d71d53cbf5af4d9a0b38a68a402f8e135f6dd0002f58eaf6a184cbbaa03d1`
- provider: OpenRouter
- model: `openrouter/google/gemini-2.5-flash`
- prompt: reply exactly `ACP_SMOKE_OK`, without tools or file changes

| Agent | License | Archive SHA-256 | Result | Transcript SHA-256 |
|---|---|---|---|---|
| `acp:opencode@1.18.4` | MIT | `04fb881b632b323c712dfda6dcbbc6fce736394f07ba76176e52d6665925d4e6` | completed, `ACP_SMOKE_OK` | `084f42d3c866786769b577cb736f52f9c8f4dcbbce306166ed0b02608656ed96` |
| `acp:kilo@7.4.11` | MIT | `14a030a354f3b51f0241662627702e7b06cddf3fcb6e0f1415279e9d3a3b8998` | completed, `ACP_SMOKE_OK` | `be5241415bef8f09f40b0caeaf34dfd5f2f1c110036b819335a9797ba0bff31d` |

Each Attempt retains `agent_final.txt`, normalized `events.jsonl`/`thinking.jsonl`, an owner-only
redacted ACP transcript, and an owner-only final manifest. The evidence was scanned for the exact
credential, generic `sk-or-` values, user-home paths and temporary installation paths; none remain.
Agent private HOME/cache directories were removed after transport cleanup.

The opt-in command remains outside default CI:

```bash
ARENA_ACP_SMOKE_CONFIG=/absolute/path/to/smoke.json \
  uv run pytest -q tests/smoke/test_acp_real_smoke.py
```
