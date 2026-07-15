// wire 通信可观测性面板：曲线 + 降级归因 + hop 时间线 + payload/blob policy 门控。
// 语义正确性优先于视觉打磨（本文件是可观测性面板，信息密度较高属预期）：
//   - 未知不伪装成 0（usageValue/curveSegments 已在 curve.ts 里保证）
//   - gaps 结构化归因（source/failure_reason/field 全部保留，不拍平成一句话）
import { useEffect, useMemo, useRef, useState } from "react";

import { api, type WireManifest, type WireRecord, type WireTrajectory } from "../api/client";
import { curveSegments, deriveWireView, usageValue, type WireGap } from "./curve";

function fmtNum(n: number | null | undefined): string {
  return typeof n === "number" ? n.toLocaleString("en-US") : "未知";
}

function gapText(g: WireGap): string {
  if (g.reason === "source") {
    return `${g.source} · ${g.status}${g.failureReason ? `（${g.failureReason}）` : ""}`;
  }
  if (g.field) return `${g.reason}：${g.field}`;
  if (g.count !== undefined) return `${g.reason}（${g.count}）`;
  return g.reason;
}

// ── payload/blob policy 门控展示 ──

function PrettyJson({ value }: { value: unknown }) {
  return <pre className="wire-json">{JSON.stringify(value, null, 2)}</pre>;
}
function CompactJson({ value }: { value: unknown }) {
  return <pre className="wire-json">{JSON.stringify(value)}</pre>;
}

function BlobView({
  runId,
  attemptId,
  blobRef: bodyRef,
  truncated,
  policy,
}: {
  runId: string;
  attemptId: string;
  blobRef: string;
  truncated?: boolean;
  policy: string | undefined;
}) {
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "unavailable" } | { kind: "ok"; body: unknown }
  >({ kind: "loading" });
  const [view, setView] = useState<"parsed" | "raw">("parsed");

  useEffect(() => {
    let cancelled = false;
    setState({ kind: "loading" });
    api.getWireBlob(runId, attemptId, bodyRef).then((res) => {
      if (cancelled) return;
      if (res.status === "ok") setState({ kind: "ok", body: res.body });
      else setState({ kind: "unavailable" });
    });
    return () => {
      cancelled = true;
    };
  }, [runId, attemptId, bodyRef]);

  if (state.kind === "loading") return <p className="muted">加载中…</p>;
  if (state.kind === "unavailable") return <p className="muted">内容不可用（policy 门控降级或后端未启用）</p>;

  return (
    <div className="wire-blob">
      {truncated && (
        <p className="wire-warning">
          内容已截断——当前只是<strong>连续前缀</strong>，不是完整正文。
        </p>
      )}
      {policy === "full" ? (
        <>
          <div className="wire-tabs" role="tablist">
            <button role="tab" aria-selected={view === "parsed"} onClick={() => setView("parsed")}>
              解析
            </button>
            <button role="tab" aria-selected={view === "raw"} onClick={() => setView("raw")}>
              原文
            </button>
          </div>
          <p className="muted">协议原文（已脱敏落盘）</p>
          {view === "parsed" ? <PrettyJson value={state.body} /> : <CompactJson value={state.body} />}
        </>
      ) : (
        <>
          <p className="muted">解析视图（已脱敏）</p>
          <PrettyJson value={state.body} />
        </>
      )}
    </div>
  );
}

function HopBody({
  runId,
  attemptId,
  hop,
  policy,
}: {
  runId: string;
  attemptId: string;
  hop: WireRecord;
  policy: string | undefined;
}) {
  const data = hop.data ?? {};
  const reqRef = data.request_body_ref as string | undefined;
  const resRef = data.response_body_ref as string | undefined;

  if (policy === "metadata" || policy === undefined) {
    return <p className="muted">不采集报文正文（policy=metadata）</p>;
  }
  return (
    <div>
      {reqRef ? (
        <div>
          <h5>请求正文</h5>
          <BlobView
            runId={runId} attemptId={attemptId} blobRef={reqRef} policy={policy}
            truncated={Boolean(data.request_body_truncated)}
          />
        </div>
      ) : (
        <p className="muted">请求正文未采集</p>
      )}
      {resRef ? (
        <div>
          <h5>响应正文</h5>
          <BlobView
            runId={runId} attemptId={attemptId} blobRef={resRef} policy={policy}
            truncated={Boolean(data.response_body_truncated)}
          />
        </div>
      ) : (
        <p className="muted">响应正文未采集</p>
      )}
    </div>
  );
}

function HopRow({
  hop,
  runId,
  attemptId,
  policy,
  trajectoryByCall,
}: {
  hop: WireRecord;
  runId: string;
  attemptId: string;
  policy: string | undefined;
  trajectoryByCall: Map<string, { step_id: string; sequence: number }>;
}) {
  const [open, setOpen] = useState(false);
  const data = hop.data ?? {};
  const method = (data.method as string) ?? "";
  const path = (data.path as string) ?? "";
  const durationMs = hop.time?.duration_ms;
  const reqBytes = data.request_bytes as number | null | undefined;
  const resBytes = data.response_bytes as number | null | undefined;
  const source = hop.source;
  const logicalCallId = hop.correlation?.logical_call_id;
  const traj = logicalCallId ? trajectoryByCall.get(logicalCallId) : undefined;

  return (
    <div className="wire-hop">
      <button
        className="wire-hop-toggle"
        onClick={() => setOpen((v) => !v)}
      >
        {method} {path} · {(hop.data?.status_code as number | null | undefined) ?? "未知"}
        {typeof durationMs === "number" && <> · {durationMs}ms</>}
      </button>
      <span className="muted">
        {" "}
        req_bytes={fmtNum(reqBytes)} resp_bytes={fmtNum(resBytes)}
      </span>
      {open && (
        <div className="wire-hop-body">
          {source && <p>source {source.kind}/{source.instance}</p>}
          {logicalCallId ? (
            traj ? (
              <a href={`#trajectory-${traj.step_id}`}>轨迹 #{traj.sequence}</a>
            ) : (
              <p className="muted">缺少可用 trajectory 关联</p>
            )
          ) : (
            <p className="muted">缺少可用 call anchor</p>
          )}
          <HopBody runId={runId} attemptId={attemptId} hop={hop} policy={policy} />
        </div>
      )}
    </div>
  );
}

// ── 逐调用检查器 ──

function CallRow({
  call,
  index,
  selected,
  onSelect,
}: {
  call: WireRecord;
  index: number;
  selected: boolean;
  onSelect: () => void;
}) {
  const usage = call.data?.usage as Record<string, unknown> | undefined;
  const inputT = usageValue(usage as never, "input_tokens");
  const outputT = usageValue(usage as never, "output_tokens");
  return (
    <tr
      id={`wire-call-${call.record_id}`}
      aria-selected={selected}
      className={selected ? "is-selected" : undefined}
      onClick={onSelect}
    >
      <td>{index + 1}</td>
      <td>{(call.data?.model_resolved as string) ?? "未知"}</td>
      <td>{(call.data?.call_role as string) ?? "未知"}</td>
      <td>{(call.data?.finish_reason as string) ?? "未知"}</td>
      <td>{fmtNum(inputT)}</td>
      <td>{fmtNum(outputT)}</td>
    </tr>
  );
}

function CurveChart({
  calls,
  onSelect,
}: {
  calls: WireRecord[];
  onSelect: (id: string) => void;
}) {
  const inputSegs = useMemo(() => curveSegments(calls, "input_tokens"), [calls]);
  const outputSegs = useMemo(() => curveSegments(calls, "output_tokens"), [calls]);
  return (
    <div className="wire-curve">
      <svg viewBox={`0 0 ${Math.max(calls.length, 1) * 20} 100`} className="wire-curve-svg">
        {[...inputSegs, ...outputSegs].map((seg, i) => (
          <polyline
            key={i}
            points={seg.map(([x, y]) => `${x * 20},${100 - Math.min(y, 100)}`).join(" ")}
            fill="none"
            stroke="currentColor"
          />
        ))}
      </svg>
      <div className="wire-curve-points">
        {calls.map((c, i) => {
          const inputT = usageValue(c.data?.usage as never, "input_tokens");
          return (
            <button
              key={c.record_id}
              aria-label={`第 ${i + 1} 次调用 · 输入 ${inputT ?? "未知"} token`}
              onClick={() => onSelect(c.record_id)}
              onKeyDown={(e) => {
                if (e.key === "Enter") onSelect(c.record_id);
              }}
            >
              ·
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── aggregate-only 累计卡片 ──

function AggregateCard({ manifest }: { manifest: WireManifest }) {
  const [showConflict, setShowConflict] = useState(false);
  const aggregates = manifest.aggregates ?? [];
  const attemptAgg = aggregates.find((a) => a.scope === "attempt");
  const adapterAgg = aggregates.find((a) => a.scope === "adapter");
  const reconciliation = aggregates.find((a) => a.scope === "reconciliation");
  const conflicts = manifest.totals?.conflicts ?? 0;

  return (
    <div className="wire-aggregate-card" aria-label="Attempt 累计用量">
      <p>aggregate-only：这是跨调用的累计值，<strong>不是一次调用</strong>的读数——不逐调用画曲线。</p>
      {attemptAgg && (
        <p>
          输入 <span>{fmtNum(usageValue(attemptAgg.usage as never, "input_tokens"))}</span> · 输出{" "}
          <span>{fmtNum(usageValue(attemptAgg.usage as never, "output_tokens"))}</span>
        </p>
      )}
      {conflicts > 0 && (
        <button onClick={() => setShowConflict((v) => !v)}>Token 对账冲突（{conflicts}）</button>
      )}
      {showConflict && (
        <div className="wire-conflict">
          <p>逐调用求和 vs aggregate 不一致，保留三方明细，不擅自选一个当真值：</p>
          {attemptAgg && <p>Producer result aggregate：输入 <span>{fmtNum(usageValue(attemptAgg.usage as never, "input_tokens"))}</span></p>}
          {adapterAgg && <p>Adapter aggregate：输入 <span>{fmtNum(usageValue(adapterAgg.usage as never, "input_tokens"))}</span></p>}
          {reconciliation?.conflict && (
            <p>
              native input={fmtNum(reconciliation.conflict.native?.input_tokens ?? null)} vs adapter input=
              {fmtNum(reconciliation.conflict.adapter?.input_tokens ?? null)}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ── 主组件 ──

const HTTP_LIMIT = 500;
const LLM_LIMIT = 500;

export function WirePanel({ runId, attemptId, label }: { runId: string; attemptId: string; label: string }) {
  const [manifest, setManifest] = useState<WireManifest | null>(null);
  const [calls, setCalls] = useState<WireRecord[]>([]);
  const [hops, setHops] = useState<WireRecord[]>([]);
  const [truncCalls, setTruncCalls] = useState(false);
  const [truncHops, setTruncHops] = useState(false);
  const [trajectory, setTrajectory] = useState<WireTrajectory | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [selectedCall, setSelectedCall] = useState<string | null>(null);
  const [foldOpen, setFoldOpen] = useState(false);
  const [trajFoldOpen, setTrajFoldOpen] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    async function load() {
      const m = await api.getWireManifest(runId, attemptId);
      if (cancelled) return;
      setManifest(m);

      const llmPage = await api.getWire(runId, attemptId, { record_type: "llm_call", limit: LLM_LIMIT });
      if (cancelled) return;
      setCalls(llmPage.items);
      setTruncCalls(Boolean(llmPage.next_cursor));

      const httpPage = await api.getWire(runId, attemptId, { record_type: "http_exchange", limit: HTTP_LIMIT });
      if (cancelled) return;
      setHops(httpPage.items);
      setTruncHops(Boolean(httpPage.next_cursor));

      try {
        const traj = await api.getWireTrajectory(runId, attemptId);
        if (!cancelled) setTrajectory(traj);
      } catch {
        if (!cancelled) setTrajectory({ status: "not_available", steps: [] });
      }
      if (!cancelled) setLoaded(true);
    }
    load();
    return () => {
      cancelled = true;
      mounted.current = false;
    };
  }, [runId, attemptId]);

  const view = useMemo(
    () => deriveWireView(manifest, calls, hops, truncCalls, truncHops),
    [manifest, calls, hops, truncCalls, truncHops],
  );

  const trajectoryByCall = useMemo(() => {
    const map = new Map<string, { step_id: string; sequence: number }>();
    for (const s of trajectory?.steps ?? []) {
      if (s.logical_call_id) map.set(s.logical_call_id, { step_id: s.step_id, sequence: s.sequence });
    }
    return map;
  }, [trajectory]);

  const callsByLogicalId = useMemo(() => {
    const map = new Map<string, WireRecord>();
    for (const c of calls) {
      const lid = c.correlation?.logical_call_id;
      if (lid) map.set(lid, c);
    }
    return map;
  }, [calls]);

  if (!loaded && manifest === null) {
    return <div className="panel wire-panel"><p className="muted">加载通信数据…</p></div>;
  }

  if (view.kind === "not_available") {
    return (
      <div className="panel wire-panel">
        <h4>通信轨迹 · {label}</h4>
        <p className="muted">无通信采集（本次运行未启用 wire capture 或尚未产出）。</p>
      </div>
    );
  }

  const truncCallGap = view.gaps.find((g) => g.reason === "trunc_calls");
  const truncHopGap = view.gaps.find((g) => g.reason === "trunc_hops");
  const sourceGaps = view.gaps.filter((g) => g.reason === "source");
  const fieldGaps = view.gaps.filter((g) => g.field);
  const policy = manifest?.policy?.effective;

  return (
    <div className="panel wire-panel">
      <h4>通信轨迹 · {label}</h4>

      {manifest?.phase_attribution === "degraded" && (
        <div className="wire-banner degraded">
          <p>phase 归属降级：部分记录无法可靠归到执行阶段。</p>
          {sourceGaps.map((g, i) => (
            <p key={i}>{gapText(g)}</p>
          ))}
          {fieldGaps.map((g, i) => (
            <p key={i}>{gapText(g)}</p>
          ))}
        </div>
      )}
      {manifest?.phase_attribution !== "degraded" && (sourceGaps.length > 0 || fieldGaps.length > 0) && (
        <div className="wire-banner">
          {sourceGaps.map((g, i) => (
            <p key={i}>{gapText(g)}</p>
          ))}
          {fieldGaps.map((g, i) => (
            <p key={i}>{gapText(g)}</p>
          ))}
        </div>
      )}

      {truncCallGap && <p className="wire-warning">llm_call 超上限已截断，未展示全部调用。</p>}
      {truncHopGap && <p className="wire-warning">http_exchange 超上限已截断，未展示全部 hop。</p>}

      {view.aggregateOnly && manifest && <AggregateCard manifest={manifest} />}

      {view.hasCurve && (
        <div>
          <h5>调用级 token 曲线 · {view.curveCount} 次调用</h5>
          <CurveChart
            calls={calls.filter((c) => c.correlation?.confidence !== "unmatched")}
            onSelect={(id) => setSelectedCall(id)}
          />
        </div>
      )}

      {calls.length > 0 && (
        <div>
          <h5>逐调用检查器 · {calls.length} 条</h5>
          <table>
            <tbody>
              {calls.map((c, i) => (
                <CallRow
                  key={c.record_id}
                  call={c}
                  index={i}
                  selected={selectedCall === c.record_id}
                  onSelect={() => setSelectedCall(c.record_id)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {hops.length > 0 && (
        <div>
          {/* 时间轴泳道图：主视图，一条 hop 一根条，纯展示（title，非 button）*/}
          <div className="wire-swimlane">
            {hops.map((h) => {
              const d = h.data ?? {};
              const label = `${d.method ?? ""} ${d.path ?? ""}`.trim();
              return <div key={h.record_id} className="wire-swimlane-bar" title={label} />;
            })}
          </div>
          {/* 折叠开关只是视觉分组标签；hop 明细本身始终挂载，保证不用先展开
              就能定位到具体 hop 的 policy 门控内容（评审兼容单 hop 场景）。*/}
          <button className="wire-fold-toggle" onClick={() => setFoldOpen((v) => !v)}>
            详细数据 · {hops.length} hops
          </button>
          <div>
            <p>全部 transport hop · {hops.length}</p>
            {hops.map((h) => (
              <HopRow
                key={h.record_id}
                hop={h}
                runId={runId}
                attemptId={attemptId}
                policy={policy}
                trajectoryByCall={trajectoryByCall}
              />
            ))}
          </div>
        </div>
      )}

      {trajectory && trajectory.steps.length > 0 && (
        <div>
          <button onClick={() => setTrajFoldOpen((v) => !v)}>Trajectory 语义步骤</button>
          {trajFoldOpen && (
            <div>
              {trajectory.steps.map((s) => {
                const call = s.logical_call_id ? callsByLogicalId.get(s.logical_call_id) : undefined;
                return (
                  <div key={s.step_id} id={`trajectory-${s.step_id}`}>
                    #{s.sequence} {s.kind}
                    {call && <a href={`#wire-call-${call.record_id}`}>返回调用</a>}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
