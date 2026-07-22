# ADR: Agent runtime installation and isolation

- Status: Accepted for the first release
- Date: 2026-07-22
- Scope: DeerFlow, ACP distributions, Python SDK plugins and future local Agents

## Decision

Ordinary runs remain unable to install or update Agent software. Deployment
administrators provide pinned host executables/environments for the first
release. Agent Arena probes them read-only and records package/revision/spec
hashes. ACP registry entries remain metadata and never authorize npx/uvx/binary
downloads. Python plugins are imported only from the server's managed Python
environment.

We do not introduce a shared mutable per-Agent uvx/npx cache yet. A versioned
runtime image is the preferred future isolation mechanism for Agents with
conflicting dependencies, native packages, untrusted code, or reproducibility
requirements that host preinstallation cannot meet. Such an image must be
selected before Attempt creation and pinned by immutable digest in the manifest.

## Comparison

| Option | Fairness/reproducibility | Startup/performance | Supply-chain and isolation cost | Decision |
|---|---|---|---|---|
| Pinned host preinstall | acceptable when version probe and manifest agree; host drift remains possible | fastest, no per-run setup | administrator owns installation; weakest dependency isolation | current default |
| Shared uvx/npx tool cache | cache warmth can differ between Attempts and mutable tags can drift | cold downloads are slow; warm starts good | lock/checksum, concurrency, eviction and poisoned-cache controls required | deferred |
| Immutable runtime image | strongest repeatability and dependency separation | image pull/cold start cost; warm execution predictable | image build, SBOM, signature, digest pin and sandbox operations required | preferred future path |

## Evidence from current integrations

DeerFlow needs a pinned Python harness, private project/home/config and a
workspace bridge; installing it during a timed Attempt would mix setup speed
with Agent quality. ACP registry npx/uvx entries may trigger network resolution
and lifecycle scripts, and binary entries require archive verification. Both
confirm that implicit installation would undermine fairness and expand the
supply-chain boundary.

## Consequences

- availability may report not installed/version unsupported before a run;
- deployment documentation must list exact prerequisites;
- no fallback from a missing pinned runtime to a package-manager command;
- future cache/image work requires an administrator API or build pipeline,
  immutable lock/checksum/digest, cleanup ownership and manifest fields;
- in-process Python plugins remain explicitly trusted and are not described as
  isolated.
