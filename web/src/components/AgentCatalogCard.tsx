import type { AgentInfo } from "../api/client";

const CAPABILITIES = [
  ["single_turn", "single"],
  ["resume_send_message", "multi"],
  ["mcp", "MCP"],
  ["structured_events", "events"],
  ["token_usage", "tokens"],
  ["wire", "Wire"],
] as const;

type Props = {
  agent: AgentInfo;
  index: number;
  inputType: "checkbox" | "radio";
  checked: boolean;
  onChange: () => void;
  inputName?: string;
};

export function agentIsAvailable(agent: AgentInfo): boolean {
  return agent.availability?.status
    ? agent.availability.status === "available"
    : agent.status === "available";
}

export function agentCompatibilityWarnings(
  agent: AgentInfo,
  options: { explicitModel: boolean; multiTurn: boolean },
): string[] {
  const warnings: string[] = [];
  if (options.explicitModel && agent.model_support?.binding === "unsupported") {
    warnings.push(`${agent.name}: this Agent does not support an explicit model`);
  }
  if (
    options.multiTurn &&
    agent.capabilities?.resume_send_message?.state === "unsupported"
  ) {
    warnings.push(`${agent.name}: this Agent does not support multi-turn tasks`);
  }
  return warnings;
}

export function AgentCatalogCard({
  agent,
  index,
  inputType,
  checked,
  onChange,
  inputName,
}: Props) {
  const available = agentIsAvailable(agent);
  const availability = agent.availability?.status ?? agent.status;
  const reason = agent.availability?.reason ?? agent.detail;

  return (
    <label
      className={`chan-cell agent-card${checked ? " on" : ""}${available ? "" : " off-avail"}`}
      title={reason ?? undefined}
    >
      <input
        type={inputType}
        name={inputName}
        checked={checked}
        disabled={!available}
        onChange={onChange}
      />
      <div className="chan-cell-id">{String(index + 1).padStart(2, "0")}</div>
      <div className="chan-cell-name">{agent.display_name || agent.name}</div>
      <div className="agent-card-id">{agent.name}</div>
      <div className="agent-card-meta">
        {agent.source} · {agent.transport}
        {agent.version ? ` · v${agent.version}` : ""}
      </div>
      {agent.transport === "acp" && agent.metadata?.registry_sha256 && (
        <div className="agent-card-acp" aria-label={`${agent.name} ACP registry pin`}>
          registry · {agent.metadata.registry_sha256.replace("sha256:", "").slice(0, 12)}
          {agent.metadata.distribution
            ? ` · ${Object.keys(agent.metadata.distribution).join("/")}`
            : ""}
        </div>
      )}
      {agent.transport === "acp" && agent.metadata?.data_boundary && (
        <div className="agent-card-boundary">data · {agent.metadata.data_boundary}</div>
      )}
      {agent.transport === "remote" && (
        <div className="agent-card-remote" aria-label={`${agent.name} remote data disclosure`}>
          <div>endpoint · {agent.metadata?.remote_endpoint ?? "unknown"}</div>
          <div>residency · {agent.metadata?.data_residency ?? "unknown"}</div>
          <div>
            source upload · {agent.metadata?.uploads_source_files ? "enabled" : "disabled"}
          </div>
          <div>cancel · {agent.metadata?.cancellation_semantics ?? "unknown"}</div>
        </div>
      )}
      <div className={`chan-cell-state${available ? "" : " err"}`}>
        {available ? "ready" : availability.replace(/_/g, " ")}
      </div>
      <div className="agent-capabilities" aria-label={`${agent.name} capabilities`}>
        {CAPABILITIES.map(([key, label]) => {
          const state = agent.capabilities?.[key]?.state ?? "unsupported";
          return (
            <span key={key} className={`agent-capability ${state}`} title={`${label}: ${state}`}>
              {label} · {state === "verified" ? "V" : state === "declared" ? "D" : "—"}
            </span>
          );
        })}
      </div>
      {!available && reason && <div className="agent-card-reason">{reason}</div>}
      {!available && agent.metadata?.installation_url && (
        <a
          className="agent-install-link"
          href={agent.metadata.installation_url}
          target="_blank"
          rel="noreferrer"
          onClick={(event) => event.stopPropagation()}
        >
          installation guide ↗
        </a>
      )}
    </label>
  );
}
