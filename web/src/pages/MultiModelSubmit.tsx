import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  api,
  type AgentInfo,
  type EnvSummary,
  type OpenRouterModel,
  type TaskJson,
} from "../api/client";
import {
  ModalityBadges,
  ModalityChip,
  missingModalities,
  modalityOptionMark,
} from "../components/ModalityChips";

/** Multi-model comparison: one agent framework runs the same task once per
 * selected model — controls the variable down to the model layer.
 */
export function MultiModelSubmit() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [agentName, setAgentName] = useState("");
  const [envs, setEnvs] = useState<EnvSummary[]>([]);
  const [tasks, setTasks] = useState<TaskJson[]>([]);
  const [envName, setEnvName] = useState("");
  const [taskId, setTaskId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [customModel, setCustomModel] = useState("");
  const [provider, setProvider] = useState("");
  const [providers, setProviders] = useState<string[]>([]);
  const [orModels, setOrModels] = useState<OpenRouterModel[]>([]);
  const [orFilter, setOrFilter] = useState("");
  const [capturePolicy, setCapturePolicy] =
    useState<"off" | "metadata" | "parsed" | "full">("metadata");
  const [timeoutMinutes, setTimeoutMinutes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.agents().then((list) => {
      setAgents(list);
      const first = list.find((a) => a.status === "available");
      if (first) setAgentName(first.name);
    }).catch((e) => setError(String(e)));
    api.envs().then((list) => {
      setEnvs(list);
      if (list.length > 0) setEnvName(list[0].name);
    });
    api.modelProviders().then((config) => {
      setProviders(config.providers ?? []);
      if ((config.providers ?? []).length > 0) setProvider(config.providers[0]);
    }).catch(() => setProviders([]));
    api.openrouterModels().then((config) => setOrModels(config.models ?? [])).catch(() => setOrModels([]));
  }, []);

  useEffect(() => {
    if (!envName) return;
    api.envTasks(envName).then((list) => {
      setTasks(list);
      setTaskId(list.length > 0 ? list[0].id : "");
    });
  }, [envName]);

  function toggleModel(id: string) {
    setSelectedModels((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function addCustomModel() {
    const id = customModel.trim();
    if (!id) return;
    setSelectedModels((prev) => new Set(prev).add(id));
    setCustomModel("");
  }

  // Final refs: prefix each bare id with the picked provider so the agent is
  // routed to the configured endpoint; ids that already carry the prefix
  // stay untouched.
  const finalRef = (id: string) => (provider && !id.startsWith(`${provider}/`) ? `${provider}/${id}` : id);
  const modelList = [...selectedModels];
  const canSubmit =
    !!envName && !!agentName && modelList.length >= 2 && !submitting && (!!taskId || !!prompt.trim());

  async function onSubmit() {
    setError(null);
    setSubmitting(true);
    try {
      const resp = await api.createRun({
        env_name: envName,
        agents: [agentName],
        task_id: taskId || undefined,
        prompt: taskId ? undefined : prompt,
        compare_mode: "multi-model",
        models: modelList.map(finalRef),
        capture_policy: capturePolicy,
        timeout_seconds:
          timeoutMinutes.trim() === "" ? null : Math.round(Number(timeoutMinutes) * 60),
      });
      navigate(`/runs/${resp.run_id}`);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  }

  const currentEnv = envs.find((e) => e.name === envName);

  const filteredModels = (() => {
    const q = orFilter.trim().toLowerCase();
    if (!q) return [];
    return orModels
      .filter((m) => m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
      .slice(0, 50);
  })();

  // Per-model modality warnings against the env's declared requirements.
  const modalityWarnings = modelList.flatMap((id) => {
    const m = orModels.find((x) => x.id === id);
    const missing = missingModalities(currentEnv?.agent_modalities, m?.input_modalities);
    return missing.length > 0 ? [{ id, missing, input: m?.input_modalities ?? [] }] : [];
  });

  return (
    <div>
      <div className="readout">
        <span>
          MODE <b>MULTI-MODEL</b>
        </span>
        <span>
          MODELS SELECTED <b>{modelList.length}</b>
        </span>
        <span>
          MODELS INDEXED <b>{orModels.length}</b>
        </span>
      </div>

      <h1>多模型对比</h1>
      <p className="lede">
        固定一个 agent 框架，每个所选模型各跑一遍同一任务——控制变量到「模型层」，比较的是底层模型在同一框架下的真实差异。
      </p>

      <div className="channel">
        <div className="channel-head">
          <span className="channel-tag">CH.01</span>
          <span className="channel-title">评测环境</span>
        </div>
        <div className="channel-body">
          <label htmlFor="env">环境</label>
          <select id="env" value={envName} onChange={(e) => setEnvName(e.target.value)}>
            {envs.map((e) => (
              <option key={e.name} value={e.name}>
                {((e.prerequisite_warnings?.length ?? 0) > 0 ? `⚠ ${e.name}` : e.name)
                  + modalityOptionMark(e.agent_modalities)}
                {" — "}
                {e.category}
              </option>
            ))}
          </select>
          {currentEnv && <div className="env-desc">{currentEnv.description}</div>}
          {(currentEnv?.agent_modalities?.length ?? 0) > 0 && (
            <div className="seg-row" style={{ marginTop: "0.4rem" }}>
              <span className="seg-label">场景需求</span>
              {currentEnv!.agent_modalities!.map((m) => (
                <ModalityChip key={m} modality={m} />
              ))}
              <span>输入能力</span>
            </div>
          )}
          {(currentEnv?.prerequisite_warnings?.length ?? 0) > 0 && (
            <div className="prereq-warn" role="alert">
              {currentEnv!.prerequisite_warnings!.map((w) => (
                <div key={w}>⚠ 本机依赖缺失：{w}</div>
              ))}
            </div>
          )}

          <label htmlFor="task">任务</label>
          <select id="task" value={taskId} onChange={(e) => setTaskId(e.target.value)}>
            <option value="">— 自由输入 Prompt —</option>
            {tasks.map((t) => (
              <option key={t.id} value={t.id}>
                {t.id}
              </option>
            ))}
          </select>
          {!taskId && (
            <>
              <label htmlFor="prompt">提示词（Prompt）</label>
              <textarea
                id="prompt"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="描述这个任务…"
              />
            </>
          )}
        </div>
      </div>

      <div className="channel">
        <div className="channel-head">
          <span className="channel-tag">CH.02</span>
          <span className="channel-title">执行 Agent（固定一个）</span>
        </div>
        <div className="channel-body">
          <div className="chan-grid">
            {agents.map((a, i) => {
              const available = a.status === "available";
              const on = agentName === a.name;
              return (
                <label
                  key={a.name}
                  className={`chan-cell${on ? " on" : ""}${available ? "" : " off-avail"}`}
                >
                  <input
                    type="radio"
                    name="agent"
                    checked={on}
                    disabled={!available}
                    onChange={() => setAgentName(a.name)}
                  />
                  <div className="chan-cell-id">{String(i + 1).padStart(2, "0")}</div>
                  <div className="chan-cell-name">{a.name}</div>
                  <div className={`chan-cell-state${available ? "" : " err"}`}>
                    {available ? "ready" : a.status}
                  </div>
                </label>
              );
            })}
            {agents.length === 0 && <div className="mx-empty">未发现任何 agent</div>}
          </div>
          <p className="hint">每个所选模型都会以该 agent 各起一个独立 attempt，并发执行。</p>
        </div>
      </div>

      <div className="channel">
        <div className="channel-head">
          <span className="channel-tag">CH.03</span>
          <span className="channel-title">
            参赛模型 <span className="soft">/ 已选 {modelList.length}（至少 2 个）</span>
          </span>
        </div>
        <div className="channel-body">
          <div className="two-col">
            <div>
              <label htmlFor="model-search">搜索模型库（共 {orModels.length} 个）</label>
              <input
                type="text"
                id="model-search"
                value={orFilter}
                onChange={(e) => setOrFilter(e.target.value)}
                placeholder="claude / gpt / gemini …"
              />
              {orFilter.trim() && (
                <div className="model-results">
                  {filteredModels.map((m) => (
                    <div
                      key={m.id}
                      className={`model-row${selectedModels.has(m.id) ? " picked" : ""}`}
                      onClick={() => toggleModel(m.id)}
                    >
                      <span>{m.id}</span>
                      <ModalityBadges input={m.input_modalities} output={m.output_modalities} />
                    </div>
                  ))}
                  {filteredModels.length === 0 && <div className="model-row muted">无匹配</div>}
                </div>
              )}
            </div>
            <div>
              <label htmlFor="custom-model">手动输入模型 ID</label>
              <input
                type="text"
                id="custom-model"
                value={customModel}
                onChange={(e) => setCustomModel(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && addCustomModel()}
                placeholder="回车添加"
              />
              <label htmlFor="provider" style={{ marginTop: "0.7rem" }}>
                Provider 前缀
              </label>
              <select id="provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
                <option value="">无前缀（agent 默认 provider）</option>
                {providers.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
              <p className="hint">提交时统一为每个模型拼前缀，路由到配置的第三方端点。</p>
            </div>
          </div>

          {modelList.length > 0 && (
            <div className="model-pick-row">
              {modelList.map((id) => (
                <span key={id} className="model-pick" onClick={() => toggleModel(id)}>
                  <span>{finalRef(id)}</span>
                  <span className="x">✕</span>
                </span>
              ))}
            </div>
          )}
          {modelList.length === 1 && <p className="hint">至少选择 2 个模型才有对比意义。</p>}

          {modalityWarnings.map((w) => (
            <div key={w.id} className="modality-warn" role="alert">
              ⛔ 场景 {envName} 需要模型支持 {w.missing.join("/")} 输入，{w.id} 的输入能力为{" "}
              {w.input.join("/") || "未知"}——跑起来会在读图步骤失败。
            </div>
          ))}
        </div>
      </div>

      <div className="channel">
        <div className="channel-head">
          <span className="channel-tag">CH.04</span>
          <span className="channel-title">执行参数</span>
        </div>
        <div className="channel-body">
          <div className="seg-row">
            <span className="seg-label">通信采集档</span>
            {(["metadata", "parsed", "full", "off"] as const).map((value) => (
              <label key={value}>
                <input
                  type="radio"
                  name="capture_policy"
                  checked={capturePolicy === value}
                  onChange={() => setCapturePolicy(value)}
                />
                {value}
              </label>
            ))}
          </div>
          <p className="seg-note">
            metadata 仅记 size/timing/token/hash（默认）；parsed/full 落写盘前脱敏的报文正文。
            实际生效值与服务端 wire_capture_max_policy 求最严格交集。
          </p>

          <div className="two-col" style={{ marginTop: "0.9rem" }}>
            <div>
              <label htmlFor="timeout">任务超时（分钟）</label>
              <input
                type="number"
                id="timeout"
                min={0}
                step={0.5}
                value={timeoutMinutes}
                onChange={(e) => setTimeoutMinutes(e.target.value)}
                placeholder="留空 = 不限时"
              />
              <p className="hint">
                {timeoutMinutes.trim() === ""
                  ? "不会向 agent 发送时间限制提示，本次评测也没有整体截止时间。"
                  : `会告知每个 agent 有 ${timeoutMinutes} 分钟，超时后本次评测将被强制停止。`}
              </p>
            </div>
          </div>
        </div>
      </div>

      {error && <p className="error-box">{error}</p>}

      <div className="submit-row">
        <div className="submit-readout">
          MULTI-MODEL · <b>{agentName || "—"}</b> × <b>{modelList.length}</b> models on{" "}
          <b>{envName || "—"}</b>
        </div>
        <button className="trigger" onClick={onSubmit} disabled={!canSubmit}>
          {submitting ? "启动中…" : "运行多模型对比"}
        </button>
      </div>
    </div>
  );
}
