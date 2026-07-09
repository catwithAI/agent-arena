import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { api, type ArtifactStep, type AttemptDetail, type AttemptSummary, type RunDetail as RunDetailModel } from "../api/client";

// ── event parsing (claude-code stream-json / codex --json) ──

type EventBlock =
  | { kind: "thinking"; content: string }
  | { kind: "text"; content: string }
  | { kind: "tool_use"; name: string; input: Record<string, unknown>; id?: string }
  | { kind: "tool_result"; toolUseId: string; content: string; isError?: boolean }
  | { kind: "result"; subtype: string; cost?: number; duration?: string };

function parseBlocks(contentArr: Array<Record<string, unknown>>, blocks: EventBlock[]) {
  for (const b of contentArr) {
    const bt = b.type as string;
    if (bt === "thinking") {
      const text = (b.thinking ?? b.content ?? "") as string;
      if (text.trim()) blocks.push({ kind: "thinking", content: text });
    } else if (bt === "text") {
      const text = (b.text ?? b.content ?? "") as string;
      if (text.trim()) blocks.push({ kind: "text", content: text });
    } else if (bt === "tool_use") {
      const name = (b.name ?? b.tool_name ?? "tool") as string;
      const input = (b.input ?? {}) as Record<string, unknown>;
      blocks.push({ kind: "tool_use", name, input, id: (b.id ?? b.tool_call_id) as string | undefined });
    } else if (bt === "tool_result") {
      const rc = b.content;
      let text = "";
      if (typeof rc === "string") text = rc;
      else if (Array.isArray(rc)) {
        text = (rc as Array<Record<string, unknown>>)
          .filter((x) => x.type === "text")
          .map((x) => x.text as string)
          .join("\n");
      }
      blocks.push({
        kind: "tool_result",
        toolUseId: (b.tool_use_id ?? b.tool_call_id ?? "") as string,
        content: text,
        isError: b.is_error as boolean | undefined,
      });
    }
  }
}

function parseEvents(events: Array<Record<string, unknown>>): EventBlock[] {
  const blocks: EventBlock[] = [];
  for (const ev of events) {
    const t = ev.type as string | undefined;

    // codex --json: {type: "item.completed", item: {...}}
    if (t === "item.completed") {
      const item = (ev.item ?? {}) as Record<string, unknown>;
      const it = item.type as string;
      if (it === "agent_message" || it === "reasoning") {
        const text = (item.text ?? "") as string;
        if (text.trim()) {
          blocks.push(it === "reasoning" ? { kind: "thinking", content: text } : { kind: "text", content: text });
        }
      } else if (it === "mcp_tool_call") {
        blocks.push({
          kind: "tool_use",
          name: (item.tool ?? item.name ?? "tool") as string,
          input: (item.arguments ?? {}) as Record<string, unknown>,
        });
        const output = item.output;
        if (output !== undefined) {
          blocks.push({
            kind: "tool_result",
            toolUseId: "",
            content: typeof output === "string" ? output : JSON.stringify(output),
            isError: Boolean(item.error),
          });
        }
      }
      continue;
    }

    // claude-code stream-json: {type: "assistant", message: {content: [...]}}
    if (t === "assistant" || t === "user") {
      const message = (ev.message ?? {}) as Record<string, unknown>;
      const content = message.content;
      if (Array.isArray(content)) parseBlocks(content as Array<Record<string, unknown>>, blocks);
      continue;
    }

    if (t === "result") {
      blocks.push({
        kind: "result",
        subtype: (ev.subtype ?? "") as string,
        cost: ev.total_cost_usd as number | undefined,
        duration: ev.duration_ms ? `${ev.duration_ms}ms` : undefined,
      });
    }
  }
  return blocks;
}

function Transcript({ events }: { events: Array<Record<string, unknown>> }) {
  const blocks = parseEvents(events);
  if (blocks.length === 0) return <p className="muted">No transcript recorded.</p>;
  return (
    <div className="transcript">
      {blocks.map((b, i) => {
        if (b.kind === "thinking") return <div className="block thinking" key={i}>{b.content}</div>;
        if (b.kind === "text") return <div className="block text" key={i}>{b.content}</div>;
        if (b.kind === "tool_use")
          return (
            <div className="block tool_use" key={i}>
              → {b.name}({JSON.stringify(b.input)})
            </div>
          );
        if (b.kind === "tool_result")
          return (
            <div className={`block tool_result${b.isError ? " error" : ""}`} key={i}>
              {b.content.slice(0, 2000)}
            </div>
          );
        return (
          <div className="block text muted" key={i}>
            [{b.subtype}] {b.duration} {b.cost !== undefined ? `$${b.cost.toFixed(4)}` : ""}
          </div>
        );
      })}
    </div>
  );
}

function AttemptColumn({ runId, attempt }: { runId: string; attempt: AttemptSummary }) {
  const [detail, setDetail] = useState<AttemptDetail | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactStep[]>([]);
  const [tab, setTab] = useState<"transcript" | "scores" | "artifacts">("transcript");

  const load = useCallback(() => {
    api.getAttempt(runId, attempt.id).then(setDetail);
    api.listArtifacts(runId, attempt.id).then(setArtifacts).catch(() => setArtifacts([]));
  }, [runId, attempt.id]);

  useEffect(() => {
    load();
    const isTerminal = !["queued", "running"].includes(attempt.status);
    if (isTerminal) return;
    const iv = setInterval(load, 3000);
    return () => clearInterval(iv);
  }, [load, attempt.status]);

  const tokenUsage = detail?.token_usage ?? {};

  return (
    <div className="panel">
      <h3>
        {attempt.agent_name} <span className={`badge ${attempt.status}`}>{attempt.status}</span>
      </h3>
      <p className="muted">
        model={detail?.model_used ?? attempt.model ?? "—"} · score={attempt.score_total ?? "—"} · {attempt.duration_ms}ms ·
        tokens in={tokenUsage.input_tokens ?? 0} out={tokenUsage.output_tokens ?? 0}
      </p>
      {attempt.error_message && <div className="error-box">{attempt.error_message}</div>}

      <div className="checkbox-row" style={{ marginBottom: "0.5rem" }}>
        <button className={tab === "transcript" ? "" : "secondary"} onClick={() => setTab("transcript")}>
          Transcript
        </button>
        <button className={tab === "scores" ? "" : "secondary"} onClick={() => setTab("scores")}>
          Scores
        </button>
        <button className={tab === "artifacts" ? "" : "secondary"} onClick={() => setTab("artifacts")}>
          Artifacts
        </button>
      </div>

      {tab === "transcript" && detail && <Transcript events={detail.events} />}
      {tab === "scores" && detail && (
        <div className="score-list">
          {detail.scores.length === 0 && <p className="muted">No scores yet.</p>}
          {detail.scores.map((s) => (
            <div key={s.dimension}>
              <strong>{s.dimension}</strong>: {s.value} — <span className="muted">{s.detail}</span>
            </div>
          ))}
          <p className="muted" style={{ marginTop: "0.5rem" }}>
            execution_locus={detail.execution.execution_locus ?? "—"} · permission_mode=
            {detail.execution.permission_mode ?? "—"}
          </p>
        </div>
      )}
      {tab === "artifacts" && (
        <div>
          {artifacts.length === 0 && <p className="muted">No artifacts.</p>}
          {artifacts.map((step) => (
            <div key={step.step}>
              <p className="muted">{step.step}</p>
              <ul>
                {step.files.map((f) => (
                  <li key={f.name}>
                    <a href={api.artifactUrl(runId, attempt.id, step.step, f.name)} target="_blank" rel="noreferrer">
                      {f.name}
                    </a>{" "}
                    <span className="muted">({f.size}B)</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function RunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const [run, setRun] = useState<RunDetailModel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const load = useCallback(() => {
    if (!runId) return;
    api.getRun(runId).then(setRun).catch((e) => setError(String(e)));
  }, [runId]);

  useEffect(() => {
    load();
    pollRef.current = window.setInterval(load, 4000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [load]);

  if (error) return <p className="error-box">{error}</p>;
  if (!run) return <p className="muted">Loading…</p>;

  return (
    <div>
      <div className="panel">
        <h2>{run.id}</h2>
        <p className="muted">
          env={run.env_name} · status=<span className={`badge ${run.status}`}>{run.status}</span> · started={run.started_at ?? "—"}
        </p>
        {run.status === "running" && (
          <button className="secondary" onClick={() => api.stopRun(run.id).then(load)}>
            Stop
          </button>
        )}
      </div>

      <div className="compare-columns">
        {run.attempts.map((a) => (
          <AttemptColumn key={a.id} runId={run.id} attempt={a} />
        ))}
      </div>
    </div>
  );
}
