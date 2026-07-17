async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) init.body = JSON.stringify(body);
  const resp = await fetch(url, init);
  if (!resp.ok) {
    const text = await resp.text();
    let detail = text;
    try {
      detail = JSON.stringify(JSON.parse(text));
    } catch {
      /* use raw text */
    }
    throw new Error(`${method} ${url} -> ${resp.status}: ${detail}`);
  }
  return resp.json() as Promise<T>;
}

export type AgentInfo = {
  name: string;
  status: "available" | "not_found";
  detail?: string | null;
  cli_path?: string | null;
};

export type EnvDimension = {
  name: string;
  weight: number;
  description: string;
};

export type EnvSummary = {
  name: string;
  skill_id: string;
  description: string;
  category: string;
  test_focus: string;
  pass_threshold: number | null;
  dimensions: EnvDimension[];
  tool_count: number;
  task_count: number;
};

export type TaskJson = {
  id: string;
  env_name: string;
  prompt: string;
  context: Record<string, unknown>;
  constraints: Record<string, unknown>;
  timeout_seconds: number;
};

export type CreateRunResponse = {
  run_id: string;
  task_id: string;
  env_name: string;
  agents: string[];
  attempts: Array<{ attempt_id: string; agent: string; model?: string | null; status: string }>;
};

export type ModelProvidersConfig = {
  providers: string[];
  suggested: string[];
};

export type OpenRouterModel = {
  id: string;
  name: string;
  context_length: number | null;
};

export type OpenRouterModelsConfig = {
  models: OpenRouterModel[];
  error: string | null;
  stale?: boolean;
};

export type RunRow = {
  run_id: string;
  task_id: string;
  env_name: string;
  run_status: string;
  created_at: string;
  attempt_count: number;
};

export type AttemptSummary = {
  id: string;
  agent_name: string;
  model: string | null;
  status: string;
  score_total: number | null;
  event_count: number;
  thinking_count: number;
  tool_call_count: number;
  token_usage_json: string | null;
  cost_estimate: number | null;
  duration_ms: number;
  started_at: string | null;
  ended_at: string | null;
  error_code?: string | null;
  error_message?: string | null;
  execution_locus?: string | null;
  model_used: string | null;
};

export type RunDetail = {
  id: string;
  task_id: string;
  env_name: string;
  status: string;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  attempts: AttemptSummary[];
};

export type ScoreRow = { dimension: string; value: number; detail: string };

export type ExecutionMeta = {
  execution_locus: string | null;
  permission_mode: string | null;
  workspace_root: string | null;
};

export type AttemptDetail = AttemptSummary & {
  run_id: string;
  task_id: string;
  env_name: string;
  session_id: string;
  external_refs: Record<string, unknown>;
  token_usage: { input_tokens?: number; output_tokens?: number };
  scores: ScoreRow[];
  execution: ExecutionMeta;
  tool_calls: Array<Record<string, unknown>>;
  events: Array<Record<string, unknown>>;
  final_state: Record<string, unknown>;
};

export type ArtifactStep = {
  step: string;
  files: Array<{
    name: string;
    size: number;
    type: "image" | "video" | "audio" | "text" | "presentation" | "document" | "spreadsheet" | "binary";
    media_type?: string;
  }>;
};

// ── wire communication observability (backend/wire/) ──

export type WireUsage = {
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_write_tokens?: number | null;
  reasoning_tokens?: number | null;
  estimated?: boolean;
  estimator?: string | null;
};

export type WireSource = {
  kind: string;
  instance: string;
  status: string;
  failure_reason?: string | null;
  capabilities?: Record<string, unknown>;
  [key: string]: unknown;
};

export type WireGapEntry = { field: string; reason: string };

export type WireAggregate = {
  scope: string;
  producer_event_type?: string;
  usage?: WireUsage;
  conflict?: { native?: WireUsage; adapter?: WireUsage };
  [key: string]: unknown;
};

export type WireManifest = {
  status: string;
  schema_version?: string;
  phase_attribution?: string;
  policy?: { requested?: string; effective?: string; downgrade_reason?: string | null };
  sources?: WireSource[];
  coverage?: Record<string, unknown>;
  totals?: { conflicts?: number; [key: string]: unknown };
  gaps?: WireGapEntry[];
  aggregates?: WireAggregate[];
  compaction_hints?: unknown[];
  [key: string]: unknown;
};

export type WireRecord = {
  record_id: string;
  record_type: "llm_call" | "http_exchange" | "stream_chunk" | "mcp_frame" | "capture_event" | "context_compaction";
  phase: string;
  source?: { kind: string; instance: string; [key: string]: unknown };
  correlation?: {
    logical_call_id?: string | null;
    hop_id?: string | null;
    confidence: string;
    [key: string]: unknown;
  };
  time?: { timestamp?: string | null; duration_ms?: number | null; [key: string]: unknown };
  data?: Record<string, unknown> & { usage?: WireUsage };
  [key: string]: unknown;
};

export type WirePage = {
  items: WireRecord[];
  next_cursor: string | null;
  manifest_status: string | null;
};

export type WireTrajectoryStep = {
  step_id: string;
  sequence: number;
  kind: string;
  logical_call_id?: string | null;
  tool_call_id?: string | null;
  [key: string]: unknown;
};

export type WireTrajectory = {
  status: string;
  steps: WireTrajectoryStep[];
};

export type WireBlobResult = { status: "ok"; body: unknown } | { status: "unavailable" };

// ── artifact office preview (backend/artifact_preview.py) ──

export type ArtifactPreviewDescriptor = {
  version: string;
  artifact: { ref: string; name: string; size: number; media_type: string; type: string };
  status: "ready" | "rendering" | "unsupported" | "failed";
  counts: Record<string, number | null>;
  renderer: { name: string; version: string };
  error: { code: string; message?: string } | null;
  cache_key: string;
  poll_after_ms: number | null;
  security: Record<string, boolean>;
  capability_gaps: string[];
  content?: Record<string, unknown> | null;
};

export const api = {
  agents: () => req<AgentInfo[]>("GET", "/api/agents"),
  envs: () => req<EnvSummary[]>("GET", "/api/envs"),
  envTasks: (name: string) => req<TaskJson[]>("GET", `/api/envs/${name}/tasks`),
  modelProviders: () => req<ModelProvidersConfig>("GET", "/api/models/providers"),
  openrouterModels: () => req<OpenRouterModelsConfig>("GET", "/api/openrouter/models"),
  createRun: (body: {
    env_name: string;
    task_id?: string;
    prompt?: string;
    agents: string[];
    model?: string;
    models?: Record<string, string>;
    // Omitted -> backend keeps its existing default. Explicit `null` ->
    // unlimited: no time-budget notice is injected, no deadline enforced.
    timeout_seconds?: number | null;
  }) => req<CreateRunResponse>("POST", "/api/runs", body),
  listRuns: () => req<RunRow[]>("GET", "/api/runs"),
  getRun: (runId: string) => req<RunDetail>("GET", `/api/runs/${runId}`),
  getAttempt: (runId: string, attemptId: string) =>
    req<AttemptDetail>("GET", `/api/runs/${runId}/attempts/${attemptId}`),
  getThinking: (runId: string, attemptId: string) =>
    req<Array<Record<string, unknown>>>("GET", `/api/runs/${runId}/attempts/${attemptId}/thinking`),
  listArtifacts: (runId: string, attemptId: string) =>
    req<ArtifactStep[]>("GET", `/api/runs/${runId}/attempts/${attemptId}/artifacts`),
  // 支持两种调用形态：既有 (runId, attemptId, step, name) 也支持
  // (runId, attemptId, path) 传入已拼好的相对路径（如 "./deck.pptx"）。
  artifactUrl: (runId: string, attemptId: string, stepOrPath: string, name?: string) => {
    const path = name === undefined ? stepOrPath : `${stepOrPath}/${name}`;
    return `/api/runs/${runId}/attempts/${attemptId}/artifacts/${path}`;
  },
  stopRun: (runId: string) => req<{ stopped: number; run_id: string }>("POST", `/api/runs/${runId}/stop`),

  // ── wire communication observability ──
  getWireManifest: (runId: string, attemptId: string) =>
    req<WireManifest>("GET", `/api/runs/${runId}/attempts/${attemptId}/wire/manifest`),
  getWireTrajectory: (runId: string, attemptId: string) =>
    req<WireTrajectory>("GET", `/api/runs/${runId}/attempts/${attemptId}/wire/trajectory`),
  getWire: (
    runId: string,
    attemptId: string,
    params?: { record_type?: string; cursor?: string; limit?: number },
  ) => {
    const search = new URLSearchParams();
    if (params?.record_type) search.set("record_type", params.record_type);
    if (params?.cursor) search.set("cursor", params.cursor);
    if (params?.limit !== undefined) search.set("limit", String(params.limit));
    const qs = search.toString();
    return req<WirePage>(
      "GET",
      `/api/runs/${runId}/attempts/${attemptId}/wire${qs ? `?${qs}` : ""}`,
    );
  },
  getWireBlob: async (runId: string, attemptId: string, ref: string): Promise<WireBlobResult> => {
    const resp = await fetch(`/api/runs/${runId}/attempts/${attemptId}/wire/blobs/${encodeURIComponent(ref)}`);
    if (!resp.ok) return { status: "unavailable" };
    const body = await resp.json();
    return { status: "ok", body };
  },

  // ── artifact office preview ──
  getArtifactPreview: async (
    runId: string,
    attemptId: string,
    path: string,
    signal?: AbortSignal,
  ): Promise<ArtifactPreviewDescriptor> => {
    const resp = await fetch(
      `/api/runs/${runId}/attempts/${attemptId}/artifact-previews/${path}`,
      { signal },
    );
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`GET artifact-previews -> ${resp.status}: ${text}`);
    }
    return resp.json() as Promise<ArtifactPreviewDescriptor>;
  },
};
