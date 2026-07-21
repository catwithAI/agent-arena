// 多轮 conversation 展示——turn 分段 + 压缩评测状态 + capability gap。
//
// 数据来自 attempt detail 的 conversation 块：summary / turns / evaluation。
// 关键约束：
// - 五种 evaluation 状态显式区分（observed / not_observed_under_budget /
//   unsupported / incomplete / insufficient_calls），不把"未触发"当失败；
// - capability gap（limitations）必须可见（aggregate-only、unattributed、session
//   broken 等）；
// - aggregate-only / unsupported 时**不绘制伪调用曲线**——只展示状态与 gap。
import type { AttemptConversation, CompactionStatus } from "../api/client";

const STATUS_LABEL: Record<CompactionStatus, string> = {
  observed: "已观察到压缩",
  not_observed_under_budget: "预算内未触发",
  unsupported: "证据不足（不支持判定）",
  incomplete: "采集不完整",
  insufficient_calls: "可比较调用不足",
};

// 状态语气：positive=检出、neutral=如实未触发、warn=证据缺口。用 class 而非颜色
// 硬编码，配色走 styles.css。
const STATUS_TONE: Record<CompactionStatus, "ok" | "neutral" | "warn"> = {
  observed: "ok",
  not_observed_under_budget: "neutral",
  unsupported: "warn",
  incomplete: "warn",
  insufficient_calls: "neutral",
};

const CONTINUITY_LABEL: Record<string, string> = {
  continuous: "会话连续",
  broken: "会话断裂",
  unknown: "会话未知",
};

const LIMITATION_LABEL: Record<string, string> = {
  "aggregate-only-usage": "仅有累计用量（无逐调用边界）",
  "subagent-identity-unattributed": "子 agent 身份不可归属",
  "session-continuity-broken": "会话连续性断裂",
  "capture-incomplete": "采集不完整",
  "pressure-below-declared-window": "压力未达声明窗口",
};

function limitationText(raw: string): string {
  return LIMITATION_LABEL[raw] ?? raw;
}

export function ConversationPanel({ conversation }: { conversation?: AttemptConversation }) {
  if (!conversation) {
    return null;
  }
  const { summary, turns, evaluation } = conversation;

  // legacy 单轮 attempt：无多轮 conversation，只提示，不渲染空 turn 表。
  if (summary.is_legacy) {
    return (
      <section className="conversation-panel" aria-label="对话轮次">
        <div className="conversation-title">对话轮次</div>
        <p className="conversation-legacy">单轮任务（无多轮 conversation trace）。</p>
      </section>
    );
  }

  return (
    <section className="conversation-panel" aria-label="对话轮次">
      <div className="conversation-title">
        对话轮次 · {summary.completed_turn_count ?? 0}/{summary.turn_count} 完成
      </div>

      <div className="conversation-summary-row">
        <span className={`chip chip-${summary.session_continuity === "broken" ? "warn" : "neutral"}`}>
          {CONTINUITY_LABEL[summary.session_continuity] ?? summary.session_continuity}
        </span>
        {summary.score_turn_id && (
          <span className="chip chip-neutral">评分轮：{summary.score_turn_id}</span>
        )}
        {summary.partial && <span className="chip chip-warn">trace 截断</span>}
      </div>

      <TurnTable turns={turns} scoreTurnId={summary.score_turn_id ?? null} />

      <EvaluationCard evaluation={evaluation} />
    </section>
  );
}

function TurnTable({
  turns,
  scoreTurnId,
}: {
  turns: AttemptConversation["turns"];
  scoreTurnId: string | null;
}) {
  if (turns.length === 0) {
    return <p className="conversation-empty">无 turn 记录。</p>;
  }
  return (
    <table className="conversation-turns">
      <thead>
        <tr>
          <th>#</th>
          <th>Turn</th>
          <th>用途</th>
          <th>状态</th>
          <th>prompt</th>
        </tr>
      </thead>
      <tbody>
        {turns.map((t) => (
          <tr key={t.turn_id} data-turn-id={t.turn_id}>
            <td>{t.turn_index ?? "?"}</td>
            <td>
              {t.turn_id}
              {t.turn_id === scoreTurnId && <span className="chip chip-ok mini">评分</span>}
            </td>
            <td>{t.purpose ?? "—"}</td>
            <td>
              <span className={`chip chip-${turnTone(t.status)} mini`}>{turnStatusLabel(t.status)}</span>
              {t.status === "failed" && t.error_summary && (
                <span className="turn-error"> · {t.error_summary}</span>
              )}
            </td>
            <td>{t.prompt_bytes != null ? `${t.prompt_bytes} B` : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function turnStatusLabel(status: string): string {
  switch (status) {
    case "completed":
      return "完成";
    case "failed":
      return "失败";
    case "interaction_answered":
      return "已应答";
    case "started":
      return "进行中";
    default:
      return status;
  }
}

function turnTone(status: string): "ok" | "neutral" | "warn" {
  if (status === "completed" || status === "interaction_answered") return "ok";
  if (status === "failed") return "warn";
  return "neutral";
}

function EvaluationCard({ evaluation }: { evaluation: AttemptConversation["evaluation"] }) {
  const tone = STATUS_TONE[evaluation.compaction_status];
  // aggregate-only / 证据不足：不绘制任何调用曲线（那会伪造边界）——只显示状态 + gap。
  const showsGaps = evaluation.limitations.length > 0;
  return (
    <div className="conversation-evaluation" aria-label="压缩评测">
      <div className="conversation-eval-header">
        <span className={`chip chip-${tone}`} data-status={evaluation.compaction_status}>
          {STATUS_LABEL[evaluation.compaction_status]}
        </span>
        {evaluation.compaction_status === "observed" && (
          <span className="chip chip-neutral">{evaluation.compaction_count} 次</span>
        )}
        <span className="chip chip-neutral">范围：{scopeLabel(evaluation.agent_scope)}</span>
        <span className={`chip chip-${evaluation.observability_completeness === "complete" ? "ok" : "warn"}`}>
          证据{completenessLabel(evaluation.observability_completeness)}
        </span>
      </div>

      <div className="conversation-eval-scores">
        <span>
          保真度：{evaluation.retention_score != null ? `${Math.round(evaluation.retention_score * 100)}%` : "—"}
        </span>
        <span>任务分：{evaluation.task_score != null ? evaluation.task_score : "—"}</span>
      </div>

      {showsGaps && (
        <div className="conversation-eval-gaps" aria-label="能力缺口">
          <span className="gap-title">能力缺口：</span>
          {evaluation.limitations.map((l) => (
            <span key={l} className="chip chip-warn mini" data-gap={l}>
              {limitationText(l)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function scopeLabel(scope: string): string {
  switch (scope) {
    case "main":
      return "主 agent";
    case "subagent":
      return "子 agent";
    case "mixed":
      return "主+子 agent";
    default:
      return "无";
  }
}

function completenessLabel(c: string): string {
  switch (c) {
    case "complete":
      return "完整";
    case "partial":
      return "部分";
    default:
      return "不完整";
  }
}
