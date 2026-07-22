# Arena Remote Agent HTTP contract v1

This contract is the boundary between Agent Arena and a vendor adapter. It is
not presented as an industry protocol. Vendor SDK plugins translate their API
to these semantics and still return the standard `AdapterResult` and agent
manifest.

## Session lifecycle

`POST /v1/sessions` receives `protocolVersion: arena-remote-v1`, Attempt ID,
ordered turns, optional requested model, and explicitly permitted file payloads.
It returns `sessionId`, `status`, and optionally same-origin `pollUrl` or
`streamUrl`. Poll snapshots and newline-delimited stream snapshots use
`queued|running|completed|failed|cancelled`; only the last three are terminal.
Events and usage remain evidence supplied by the service and are never inferred.

`DELETE /v1/sessions/{id}` returns `confirmed: true` only when server-side stop
is known. Any false, malformed, network-failed or missing response maps to
`cancel_requested_remote_unknown`. A local timeout remains `agent_timeout` and
records this independent remote cancellation state.

## Data and artifacts

Catalog UI discloses endpoint, declared data residency, whether source upload is
enabled, and cancellation semantics before selection. File bytes are sent only
when that Agent's administrator configuration enables uploads. Request summaries
and manifests contain file names/count/size/hash, not bytes or API keys.

Artifact URLs must remain on the configured service origin. Paths must resolve
inside `skill_workspace`; declared size, configured limit and SHA-256 are checked
before acceptance. One failed artifact creates `artifacts=partial` without
discarding already verified artifacts or changing a completed task into a false
execution failure.

Remote services cannot access task-local MCP servers in v1. They must not bypass
Attempt identity, workspace artifact boundaries, timeout handling, or manifest
redaction through a vendor plugin.
