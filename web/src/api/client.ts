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
  files: Array<{ name: string; size: number; type: "image" | "video" | "audio" | "text" }>;
};

export const api = {
  agents: () => req<AgentInfo[]>("GET", "/api/agents"),
  envs: () => req<EnvSummary[]>("GET", "/api/envs"),
  envTasks: (name: string) => req<TaskJson[]>("GET", `/api/envs/${name}/tasks`),
  modelProviders: () => req<ModelProvidersConfig>("GET", "/api/models/providers"),
  createRun: (body: {
    env_name: string;
    task_id?: string;
    prompt?: string;
    agents: string[];
    model?: string;
    models?: Record<string, string>;
    timeout_seconds?: number;
  }) => req<CreateRunResponse>("POST", "/api/runs", body),
  listRuns: () => req<RunRow[]>("GET", "/api/runs"),
  getRun: (runId: string) => req<RunDetail>("GET", `/api/runs/${runId}`),
  getAttempt: (runId: string, attemptId: string) =>
    req<AttemptDetail>("GET", `/api/runs/${runId}/attempts/${attemptId}`),
  getThinking: (runId: string, attemptId: string) =>
    req<Array<Record<string, unknown>>>("GET", `/api/runs/${runId}/attempts/${attemptId}/thinking`),
  listArtifacts: (runId: string, attemptId: string) =>
    req<ArtifactStep[]>("GET", `/api/runs/${runId}/attempts/${attemptId}/artifacts`),
  artifactUrl: (runId: string, attemptId: string, step: string, name: string) => {
    const path = step === "attempt-root" ? name : `${step}/${name}`;
    return `/api/runs/${runId}/attempts/${attemptId}/artifacts/${path}`;
  },
  stopRun: (runId: string) => req<{ stopped: number; run_id: string }>("POST", `/api/runs/${runId}/stop`),
};
