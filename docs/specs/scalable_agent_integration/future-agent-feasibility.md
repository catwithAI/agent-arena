# Follow-up SDK and remote Agent feasibility

Status: A8-3 decision record. No additional built-in Agent is a Phase 0/1
release requirement.

## Admission paths

| Candidate shape | Required integration path | Admission evidence |
|---|---|---|
| Local Python SDK with trusted dependencies | framework-wrapped `python_plugins` descriptor | pinned package/version, lazy import failure isolation, fake E2E, workspace artifact checks |
| Hosted async Agent API | `RemoteTransportAdapter` or vendor translator to its HTTP contract | upload/residency disclosure, server session, timeout/cancel result, partial artifact fixture |
| ACP-compatible process | shared ACP v1 transport | exact registry ID/version/hash, preinstalled executable, permission and crash fixtures |
| CLI process | profile runtime or a focused plugin | deterministic LaunchPlan, lifecycle cleanup, parser/manifest and MCP dialect evidence |

## Go/no-go gate for a concrete built-in

A candidate is scheduled independently and is not added to the built-in catalog
until its official distribution can be pinned, authentication can be referenced
without storing a secret, model selection behavior is measured, and all material
leaving the host is disclosed. Remote cancellation that is not confirmed remains
`cancel_requested_remote_unknown`; missing usage or trajectory remains unknown,
not zero.

Python SDK integration is suitable only for trusted administrator-installed
code. An SDK with conflicting dependencies, native system packages, or an
untrusted execution boundary must use a subprocess/runtime image instead. A
hosted service that cannot constrain artifact paths or report a stable terminal
session is a no-go for built-in status, though a third-party experimental plugin
may still expose its limitations.

No concrete post-DeerFlow Agent is approved by this record; each receives its
own pinned spike and contract fixtures.
