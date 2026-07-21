import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { api, type ArtifactStep, type AttemptDetail, type AttemptSummary, type RunDetail as RunDetailModel } from "../api/client";
import { WirePanel } from "../wire/WirePanel";
import { ArtifactsPanel } from "../artifacts/ArtifactsPanel";
import { ConversationPanel } from "./ConversationPanel";

// 供测试直接从 pages/RunDetail 引入（WirePanel.test.tsx / ArtifactsPanel.test.tsx
// mock ../api/client 后 import { WirePanel } from "../pages/RunDetail"）：
// 真正实现在各自目录下，这里只是重新导出，避免把 2604 行内联实现塞回本文件。
export { WirePanel } from "../wire/WirePanel";
export { ArtifactsPanel } from "../artifacts/ArtifactsPanel";

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
  if (blocks.length === 0) return <p className="muted">暂无对话记录。</p>;
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

// ── shared attempt helpers ──

type TabKey = "transcript" | "turns" | "scores" | "artifacts" | "wire";

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: "transcript", label: "对话记录" },
  { key: "turns", label: "对话轮次" },
  { key: "scores", label: "评分" },
  { key: "artifacts", label: "产物" },
  { key: "wire", label: "通信详情" },
];

const ACTIVE_STATUSES = ["queued", "running"];

function isActive(status: string): boolean {
  return ACTIVE_STATUSES.includes(status);
}

function fmtDuration(ms: number): string {
  if (!ms) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function fmtCost(cost: number | null | undefined): string | null {
  if (cost === null || cost === undefined) return null;
  return `$${cost.toFixed(3)}`;
}

function useAttemptDetail(runId: string, attempt: AttemptSummary) {
  const [detail, setDetail] = useState<AttemptDetail | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactStep[]>([]);

  const load = useCallback(() => {
    api.getAttempt(runId, attempt.id).then(setDetail);
    api.listArtifacts(runId, attempt.id).then(setArtifacts).catch(() => setArtifacts([]));
  }, [runId, attempt.id]);

  useEffect(() => {
    load();
    if (!isActive(attempt.status)) return;
    const iv = setInterval(load, 3000);
    return () => clearInterval(iv);
  }, [load, attempt.status]);

  return { detail, artifacts };
}

function dotClass(attempt: AttemptSummary, leader: boolean): string {
  if (isActive(attempt.status)) return "rank-dot busy";
  if (attempt.status !== "completed") return "rank-dot bad";
  if (leader) return "rank-dot ok";
  return "rank-dot";
}

function AttemptTabBody({
  runId,
  attempt,
  tab,
  detail,
  artifacts,
}: {
  runId: string;
  attempt: AttemptSummary;
  tab: TabKey;
  detail: AttemptDetail | null;
  artifacts: ArtifactStep[];
}) {
  if (tab === "transcript") {
    if (!detail) return <p className="muted">加载中…</p>;
    if (detail.events.length === 0 && isActive(attempt.status))
      return <div className="mx-empty">刚启动，等待第一次事件…</div>;
    return <Transcript events={detail.events} />;
  }
  if (tab === "turns") {
    if (!detail) return <p className="muted">加载中…</p>;
    return <ConversationPanel conversation={detail.conversation} />;
  }
  if (tab === "scores") {
    if (!detail) return <p className="muted">加载中…</p>;
    return (
      <div className="score-list">
        {detail.scores.length === 0 && <p className="muted">暂无评分。</p>}
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
    );
  }
  if (tab === "artifacts") {
    if (artifacts.length === 0 && isActive(attempt.status))
      return <div className="mx-empty">尚未导出产物，agent 仍在执行中</div>;
    return <ArtifactsPanel runId={runId} attemptId={attempt.id} steps={artifacts} />;
  }
  return <WirePanel runId={runId} attemptId={attempt.id} label={attempt.agent_name} />;
}

function attemptStatLine(attempt: AttemptSummary, detail: AttemptDetail | null) {
  const tokenUsage = detail?.token_usage ?? {};
  const cost = fmtCost(attempt.cost_estimate);
  return (
    <>
      <span>
        <b>model</b> {detail?.model_used ?? attempt.model ?? "—"}
      </span>
      <span>
        <b>时长</b> {fmtDuration(attempt.duration_ms)}
      </span>
      {cost && (
        <span>
          <b>成本</b> {cost}
        </span>
      )}
      <span>
        <b>tokens</b> in {tokenUsage.input_tokens ?? 0} / out {tokenUsage.output_tokens ?? 0}
      </span>
      {isActive(attempt.status) && (
        <span className="mx-running-pulse">
          <span className="dot" />
          {attempt.status}
        </span>
      )}
    </>
  );
}

// ── rank view: expanded rack under the leaderboard ──

function AttemptRack({
  runId,
  attempt,
  rank,
  leader,
}: {
  runId: string;
  attempt: AttemptSummary;
  rank: number | null;
  leader: boolean;
}) {
  const [tab, setTab] = useState<TabKey>("transcript");
  const { detail, artifacts } = useAttemptDetail(runId, attempt);

  return (
    <div className={`rack${leader ? " leader" : ""}`}>
      <div className="rack-head">
        <span className="rack-name">
          <span className={dotClass(attempt, leader)} />
          {attempt.agent_name}
          {rank !== null && <span className="mx-rank">#{rank}</span>}
        </span>
        <span className={`state-tag ${attempt.status}`}>{attempt.status}</span>
      </div>
      <div className="rack-stats">{attemptStatLine(attempt, detail)}</div>
      {attempt.error_message && (
        <div className="rack-body" style={{ paddingBottom: 0 }}>
          <div className="error-box">{attempt.error_message}</div>
        </div>
      )}
      <div className="tab-row">
        {TABS.map((t) => (
          <button key={t.key} className={tab === t.key ? "active" : ""} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="rack-body">
        <AttemptTabBody runId={runId} attempt={attempt} tab={tab} detail={detail} artifacts={artifacts} />
      </div>
    </div>
  );
}

// ── matrix view: all attempts side by side with one synced tab ──

function MatrixCell({
  runId,
  attempt,
  rank,
  leader,
  tab,
}: {
  runId: string;
  attempt: AttemptSummary;
  rank: number | null;
  leader: boolean;
  tab: TabKey;
}) {
  const { detail, artifacts } = useAttemptDetail(runId, attempt);

  return (
    <div className={`mx-cell${leader ? " leader" : ""}`}>
      <div className="mx-head">
        <span className="mx-name">
          <span className={dotClass(attempt, leader)} />
          {attempt.agent_name}
        </span>
        <span className="mx-rank">
          {rank !== null ? `#${rank} · ${attempt.score_total}` : attempt.status}
        </span>
      </div>
      <div className="mx-stats">{attemptStatLine(attempt, detail)}</div>
      <div className="mx-body">
        {attempt.error_message && <div className="error-box">{attempt.error_message}</div>}
        <AttemptTabBody runId={runId} attempt={attempt} tab={tab} detail={detail} artifacts={artifacts} />
      </div>
    </div>
  );
}

// ── leaderboard row ──

function RankRow({
  attempt,
  rank,
  leader,
  open,
  onToggle,
}: {
  attempt: AttemptSummary;
  rank: number | null;
  leader: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const busy = isActive(attempt.status);
  const failed = !busy && attempt.status !== "completed";
  const cost = fmtCost(attempt.cost_estimate);
  const rowClass = [
    "rankrow",
    leader ? "leader" : "",
    busy ? "busy" : "",
    failed ? "bad" : "",
    open ? "open" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={rowClass} onClick={onToggle}>
      <div className="rank-num">{rank !== null ? String(rank).padStart(2, "0") : "—"}</div>
      <div className="rank-agent">
        <span className={dotClass(attempt, leader)} />
        <span className="rank-name">{attempt.agent_name}</span>
        <span className="rank-model">{attempt.model_used ?? attempt.model ?? ""}</span>
      </div>
      <div className="rank-counters">
        <span>
          ev <b>{attempt.event_count}</b>
        </span>
        <span>
          think <b>{attempt.thinking_count}</b>
        </span>
        <span>
          tool <b>{attempt.tool_call_count}</b>
        </span>
      </div>
      <div className="rank-meta">
        {busy
          ? `${attempt.status} · ${fmtDuration(attempt.duration_ms)}`
          : [fmtDuration(attempt.duration_ms), cost].filter(Boolean).join(" · ")}
      </div>
      <div className="rank-score">
        {busy ? "跑分中" : failed ? attempt.status : attempt.score_total ?? "—"}
      </div>
      <div className="expand-arrow">▾</div>
    </div>
  );
}

// ── page ──

export function RunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const [run, setRun] = useState<RunDetailModel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"rank" | "matrix">("rank");
  const [openAttempt, setOpenAttempt] = useState<string | null>(null);
  const [matrixTab, setMatrixTab] = useState<TabKey>("transcript");
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
  if (!run) return <p className="muted">加载中…</p>;

  // completed attempts ranked by score desc; active/failed rows keep submission order after them
  const ranked = run.attempts
    .filter((a) => a.status === "completed" && a.score_total !== null)
    .sort((a, b) => (b.score_total ?? 0) - (a.score_total ?? 0));
  const rankOf = new Map(ranked.map((a, i) => [a.id, i + 1]));
  const leaderId = ranked[0]?.id;
  const ordered = [...run.attempts].sort((a, b) => {
    const ra = rankOf.get(a.id) ?? Number.MAX_SAFE_INTEGER;
    const rb = rankOf.get(b.id) ?? Number.MAX_SAFE_INTEGER;
    return ra - rb;
  });

  const completed = run.attempts.filter((a) => !isActive(a.status)).length;
  const runningCount = run.attempts.length - completed;
  const openedAttempt = ordered.find((a) => a.id === openAttempt);

  return (
    <div>
      <div className="run-header">
        <div className="run-meta">
          <span className="run-id">{run.id}</span>
          <span className={`state-tag ${run.status}`}>{run.status}</span>
          {run.status === "running" && (
            <button className="abort-btn" onClick={() => api.stopRun(run.id).then(load)}>
              中止评测
            </button>
          )}
        </div>
        <h1 className="run-title">
          {run.env_name} · {run.attempts.length} 个 Agent 并发评测
        </h1>
      </div>

      <div className="board">
        <div className="board-head">
          <div className="board-head-left">
            <span className="board-eyebrow">Leaderboard</span>
            <span className="board-count">
              {run.attempts.length} agents · {completed} completed
              {runningCount > 0 && ` · ${runningCount} running`}
            </span>
          </div>
          <div className="view-switch">
            <button className={view === "rank" ? "active" : ""} onClick={() => setView("rank")}>
              排行榜
            </button>
            <button className={view === "matrix" ? "active" : ""} onClick={() => setView("matrix")}>
              矩阵视图
            </button>
          </div>
        </div>

        {view === "rank" && (
          <>
            {ordered.map((a) => (
              <RankRow
                key={a.id}
                attempt={a}
                rank={rankOf.get(a.id) ?? null}
                leader={a.id === leaderId}
                open={openAttempt === a.id}
                onToggle={() => setOpenAttempt(openAttempt === a.id ? null : a.id)}
              />
            ))}
            {!openedAttempt && (
              <p className="collapsed-note">
                点击排行榜任意一行展开完整对话记录；或切到 <b>矩阵视图</b> 同时并排查看全部{" "}
                {run.attempts.length} 个 agent。
              </p>
            )}
            {openedAttempt && (
              <div className="floor">
                <AttemptRack
                  key={openedAttempt.id}
                  runId={run.id}
                  attempt={openedAttempt}
                  rank={rankOf.get(openedAttempt.id) ?? null}
                  leader={openedAttempt.id === leaderId}
                />
              </div>
            )}
          </>
        )}

        {view === "matrix" && (
          <>
            <div className="matrix-toolbar">
              <div className="sync-tabs">
                {TABS.map((t) => (
                  <button
                    key={t.key}
                    className={matrixTab === t.key ? "active" : ""}
                    onClick={() => setMatrixTab(t.key)}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
              <span className="matrix-hint">
                全部 <b>{run.attempts.length}</b> 个 agent 同步显示同一个标签页，便于逐格比对
              </span>
            </div>
            <div className={`matrix${run.attempts.length > 4 ? " dense" : ""}`}>
              {ordered.map((a) => (
                <MatrixCell
                  key={a.id}
                  runId={run.id}
                  attempt={a}
                  rank={rankOf.get(a.id) ?? null}
                  leader={a.id === leaderId}
                  tab={matrixTab}
                />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
