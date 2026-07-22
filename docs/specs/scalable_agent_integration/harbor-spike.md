# Harbor agent architecture mapping

Research snapshot: 2026-07-22. Upstream repository:
`https://github.com/harbor-framework/harbor`, revision
`1393655243125f1d63f81f9bd2f217eefaba3633` (2026-07-20).

This is a design comparison, not a runtime or source dependency. No Harbor
source is copied by the current implementation.

## Reusable boundaries

| Harbor concept | Useful idea | Agent Arena mapping |
|---|---|---|
| `BaseAgent` | Small lifecycle contract (`setup`, `run`, optional `resume`) plus stable identity/context | `AgentAdapter`, `AdapterRunInput`, `AdapterResult`, driver contracts |
| `BaseInstalledAgent` | Shared version detection, typed CLI/env descriptors, error classification and prompt rendering | `AvailabilityService`, `AgentSpec`, `LaunchPlan`, shared error taxonomy |
| `AgentFactory` | Name/import-path resolution and lazy class loading | `AgentRegistry` and `ResolvedAgent.build_adapter()` |
| ACP registry shorthand | One protocol adapter resolves many data-only registry entries | `acp:<id>@<version>` plus shared `AcpTransportAdapter` |
| Agent context/trajectory | Preserve partial evidence and metadata even when execution fails | raw runtime evidence, `ParseResult`, agent manifest and Wire coverage |

The reusable part is separation of descriptor, construction, lifecycle and
evidence. The implementation classes are not copied.

## Deliberately different boundaries

Harbor's `BaseAgent` receives a `BaseEnvironment`; its installed agents run
setup commands inside a task environment, may create `/installed-agent`, and
can execute installation commands as root or the task agent user. Agent Arena's
current execution locus is the host or an explicitly declared remote host. It
therefore must not copy these assumptions:

- no per-run package-manager installation, root setup, NVM/uvx/npx bootstrap or
  mutation of a container image;
- no Harbor container paths, default user, log synchronization or environment
  `exec()` API in AgentSpec/runtime contracts;
- no shell-string command composition; Agent Arena keeps tokenized argv and a
  process group owned by the Attempt;
- no implicit global environment inheritance or registry metadata becoming
  execution authority;
- no inference that Harbor's ACP/DeerFlow behavior proves the same capability
  for Agent Arena's pinned revision and workspace topology.

Preinstalled binaries, isolated runtime images, and future administrator-owned
tool caches are separate deployment decisions. Ordinary runs remain read-only
with respect to package installation.

## Apache-2.0 obligations

The inspected Harbor revision contains an Apache License 2.0 `LICENSE` and no
repository-root `NOTICE` file. Design ideas and clean-room reimplementation do
not require copying source notices. If future work copies or modifies Harbor
source, that change must:

1. retain applicable source copyright and license notices;
2. include the Apache-2.0 license with the distribution;
3. clearly mark modified files;
4. reproduce any upstream `NOTICE` content if a later pinned revision includes
   one, while retaining only notices relevant to the redistributed work;
5. add an attribution entry identifying the upstream repository, pinned commit
   and copied files.

Before accepting copied code, review the exact pinned revision again; this
document is not a blanket license audit for later revisions or dependencies.
