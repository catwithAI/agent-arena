# ADR: W7 Office artifact preview boundary

- Status: accepted for W7 implementation
- Date: 2026-07-14
- Contract: `lane-artifact-preview-v1`

## Decision

Office files are untrusted business artifacts. agent-arena will classify and render them on the server;
the browser never receives an OOXML ZIP as text and never executes formulas, macros, embedded
objects, scripts, data connections, or external relationships.

The public endpoints are separated deliberately:

- `GET .../artifacts` lists files with a content-derived type and MIME;
- `GET .../artifacts/{ref}` returns the original byte-identical file;
- `GET .../artifact-previews/{ref}` returns a versioned preview descriptor, never raw Office bytes;
- future rendered resources are addressed only through descriptor-owned, opaque refs.

The descriptor contains original artifact identity, status (`ready|rendering|unsupported|failed`),
slide/page/sheet counts where observable, renderer name/version, a content hash cache key, a stable
error code, security observations and explicit capability gaps. Preview failure never removes or
changes the original download.

## Renderer split

- PPTX: the built-in isolated worker produces a bounded static layout IR for slides, positioned text,
  tables, safe raster images and speaker notes. It never executes transitions/animations or loads
  external resources. Theme/group transforms and charts are explicit fidelity gaps. A future
  sandboxed LibreOffice/PDF renderer may improve pixel fidelity without changing the public contract.
- DOCX: the built-in isolated worker produces semantic document IR for headings, paragraphs, runs,
  lists, tables, safe external hyperlinks, headers and footers. Pagination is approximate and embedded
  images/comments/notes are explicit gaps. A future PDF renderer is an optional fidelity enhancement,
  not required for safe content access.
- XLSX: a bounded structural parser produces workbook/sheet/cell JSON. Formula strings and existing
  cached values may be displayed, but formulas are never evaluated. The browser renders the bounded
  grid and virtualizes rows.
- Legacy PPT/DOC/XLS: download-only until the same sandboxed worker is available and fidelity fixtures
  pass. There is no in-process legacy Office parser.
- Macro-enabled OOXML: static preview is allowed only after active parts have been removed from the
  renderer input. Macros and embedded OLE/ActiveX content are never executed.

Renderer output is cached below the attempt's `artifact-previews/` framework directory and excluded
from normal artifact listing/download. The cache identity includes source SHA-256, contract version,
renderer/version and renderer options. A source change always produces a different cache key.

## Limits and scheduling

The preflight scanner runs before any renderer and currently enforces:

- source file: 128 MiB;
- ZIP entries: 10,000;
- total uncompressed bytes: 512 MiB;
- a single uncompressed entry: 64 MiB;
- compression ratio per entry: 200:1;
- relationship XML inspected only within a 2 MiB per-entry bound.

Files up to 20 MiB may render synchronously within a 15-second deadline. Larger accepted files use a
background job and descriptor polling (`status=rendering`, `poll_after_ms`); W7 uses polling rather
than adding another SSE protocol. A worker has a renderer-specific hard timeout (15 seconds for the
current structural XLSX worker; at most 60 seconds for future converters) and deployment-enforced CPU,
memory, process, filesystem and no-network limits. If those isolation controls or the renderer are not
available, the descriptor reports `unsupported`/`renderer_unavailable`; agent-arena must not silently run
an unsandboxed converter.

Every ZIP member is checked for absolute paths and `..` traversal before reads. External relationships
are reported but never fetched. XML parsing must be entity-safe and bounded. Each renderer runs with
a fresh temporary working directory, a minimal environment and no inherited credentials. The parent
copies the artifact to that directory and verifies the snapshot against the cache hash before the
worker opens it; a concurrently changing artifact returns the retryable `artifact_changed` error
instead of poisoning the cache. Renderer output stays in the temporary directory. Timeout/crash
produces a stable preview error and cannot fail the attempt or RunDetail page.

The built-in PPTX, DOCX and XLSX renderers use the same isolated Python subprocess contract:
`python -I`, a fresh temporary cwd/HOME/TMPDIR, a minimal environment, disabled socket creation,
POSIX CPU/address-space/file-size/file-descriptor limits where available, and a 15-second parent-side
hard timeout. Results use an output-file envelope rather than stdout and are capped at 32 MiB. Stable
results are atomically cached below `artifact-previews/<composite-sha256>/`; cache roots and entries
that are symlinks are ignored. Timeout/crash/invalid-output results are transient and never cached.
Container-level seccomp/network/filesystem isolation remains a deployment hardening requirement for
future LibreOffice workers; the built-in PPTX/DOCX/XLSX workers do not invoke third-party binaries.

Large XLSX scheduling is implemented with a bounded two-thread executor and at most 256 deduplicated
jobs. Identity is the resolved artifact path plus composite content key. The first request returns
`rendering` with a 500 ms polling hint; concurrent polls reuse the same future, and the completed
descriptor remains available in the bounded job table until normal eviction. A saturated queue fails
with `renderer_queue_full` instead of silently adding unbounded work. The browser follows the polling
hint (clamped to 10 ms–5 s), stops after 120 polls, and aborts timers/fetches when the user switches or
closes an artifact.

## Rollout

W7-1 ships the descriptor, content classification, bounded OOXML preflight, byte-identical download,
and UI loading/error/unsupported shell. W7-2, W7-3 and W7-4 add renderer-specific resources behind the
same contract. W7-5 completes navigation and comparison behavior without changing artifact trust or
wire-blob policy boundaries.
