# ACP v1 transport decision record

Status: accepted for A7 implementation (2026-07-22).

## Pin and client boundary

Agent Arena pins the stable ACP major protocol `protocolVersion: 1`. ACP v2 is
currently Draft and is not negotiated. The transport is a small asynchronous
JSON-RPC 2.0 client over stdio; each UTF-8 message is one newline-delimited JSON
object. We intentionally do not add the Python SDK as a runtime dependency:
the required client surface is initialize, session/new, session/prompt,
session/update, session/request_permission and session/cancel, and keeping that
surface local makes process cleanup and evidence capture use the arena's shared
runtime rules.

One ACP subprocess and session belong to one Attempt. Multi-turn prompts reuse
that session. Timeout or cancellation sends the session/cancel notification
before terminating the process group. An agent may emit final updates before
the prompt response; they remain accepted.

## Permission and client capabilities

The client advertises neither filesystem nor terminal capabilities in the
first release. A permission answer must explicitly select an option ID for the
specific tool call. If no configured answer matches, the client returns ACP's
`cancelled` permission outcome and records degraded permission coverage. It
never guesses an allow option.

## Registry and distribution supply chain

Only stable identifiers of the form `acp:<id>@<version>` resolve. Registry
documents must use HTTPS, validate against the supported v1 shape, and are
stored byte-for-byte in a content-addressed cache. The resolved SHA-256 is part
of the descriptor/run pin. Offline resolution re-hashes the raw cache blob and
fails closed on a missing reference, a checksum mismatch, duplicate metadata,
an absent exact version, or schema corruption.

Registry distribution metadata is not trusted code. Normal runs never download,
extract, invoke npx/uvx installation, or mutate package state. An administrator
must preinstall and configure an executable separately. Binary archive URLs and
checksums remain metadata for that administrative workflow.

## Transcript coverage

Contract fake servers exercise two transcript shapes: normal single/multi-turn
updates with thinking/tool/usage events, and permission requests with explicit
deny or unconfigured cancellation. Separate cases cover protocol mismatch,
server crash, and timeout cleanup. Real registry agent smoke tests are optional
and excluded from default CI.

Official references:

- https://agentclientprotocol.com/protocol/v1/overview
- https://agentclientprotocol.com/protocol/v1/transports
- https://agentclientprotocol.com/protocol/v1/initialization
- https://agentclientprotocol.com/protocol/v1/session-setup
- https://agentclientprotocol.com/protocol/v1/prompt-turn
- https://github.com/agentclientprotocol/registry/blob/main/FORMAT.md
