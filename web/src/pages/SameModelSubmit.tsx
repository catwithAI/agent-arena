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

/** Same-model comparison: N distinct agents all run one shared model.
 * The final model ref is `<provider>/<bare>` when a provider is picked
 * (routes CC/Codex through the configured endpoint) or the bare id.
 */
export function SameModelSubmit() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [envs, setEnvs] = useState<EnvSummary[]>([]);
  const [tasks, setTasks] = useState<TaskJson[]>([]);
  const [envName, setEnvName] = useState("");
  const [taskId, setTaskId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bareModel, setBareModel] = useState("");
  const [provider, setProvider] = useState("");
  const [providers, setProviders] = useState<string[]>([]);
  const [suggested, setSuggested] = useState<string[]>([]);
  const [orModels, setOrModels] = useState<OpenRouterModel[]>([]);
  const [orFilter, setOrFilter] = useState("");
  const [execution, setExecution] = useState<"serial" | "parallel">("serial");
  const [capturePolicy, setCapturePolicy] =
    useState<"off" | "metadata" | "parsed" | "full">("metadata");
  const [timeoutMinutes, setTimeoutMinutes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.agents().then((list) => {
      setAgents(list);
      // Preselect every available agent: same-model needs >= 2 anyway.
      setSelected(new Set(list.filter((a) => a.status === "available").map((a) => a.name)));
    }).catch((e) => setError(String(e)));
    api.envs().then((list) => {
      setEnvs(list);
      if (list.length > 0) setEnvName(list[0].name);
    });
    api.modelProviders().then((config) => {
      setProviders(config.providers ?? []);
      setSuggested(config.suggested ?? []);
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

  function toggleAgent(name: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  const selectedList = agents.map((a) => a.name).filter((n) => selected.has(n));
  const modelRef = (() => {
    const bare = bareModel.trim();
    if (!bare) return "";
    return provider ? `${provider}/${bare}` : bare;
  })();
  const canSubmit =
    !!envName && selectedList.length >= 2 && !!modelRef && !submitting && (!!taskId || !!prompt.trim());

  async function onSubmit() {
    setError(null);
    setSubmitting(true);
    try {
      const resp = await api.createRun({
        env_name: envName,
        agents: selectedList,
        task_id: taskId || undefined,
        prompt: taskId ? undefined : prompt,
        compare_mode: "same-model",
        model: modelRef,
        execution,
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
  const pickedModel = orModels.find((m) => m.id === bareModel.trim());
  const missing = missingModalities(currentEnv?.agent_modalities, pickedModel?.input_modalities);

  const filteredModels = (() => {
    const q = orFilter.trim().toLowerCase();
    if (!q) return [];
    return orModels
      .filter((m) => m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
      .slice(0, 50);
  })();

  return (
    <div>
      <div className="readout">
        <span>
          MODE <b>SAME-MODEL</b>
        </span>
        <span>
          AGENTS SELECTED <b>{selectedList.length}</b>
        </span>
        <span>
          MODELS INDEXED <b>{orModels.length}</b>
        </span>
      </div>

      <h1>同模型对比</h1>
      <p className="lede">
        多个 agent 框架共用同一个模型跑同一任务——控制变量到「框架层」，比较的是 agent
        的调度与工具使用能力，而不是底层模型的差异。
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
          <span className="channel-title">
            参与 Agent <span className="soft">/ 已选 {selectedList.length}（至少 2 个）</span>
          </span>
        </div>
        <div className="channel-body">
          <div className="chan-grid">
            {agents.map((a, i) => {
              const available = a.status === "available";
              const on = selected.has(a.name);
              return (
                <label
                  key={a.name}
                  className={`chan-cell${on ? " on" : ""}${available ? "" : " off-avail"}`}
                >
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={!available}
                    onChange={() => toggleAgent(a.name)}
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
          {selectedList.length < 2 && <p className="hint">同模型对比至少需要 2 个 agent。</p>}
        </div>
      </div>

      <div className="channel">
        <div className="channel-head">
          <span className="channel-tag">CH.03</span>
          <span className="channel-title">共用模型</span>
        </div>
        <div className="channel-body">
          <div className="two-col">
            <div>
              <label htmlFor="bare-model">模型 ID（全员共用）</label>
              <input
                type="text"
                id="bare-model"
                value={bareModel}
                onChange={(e) => setBareModel(e.target.value)}
                placeholder="如 anthropic/claude-sonnet-5"
              />
              {pickedModel && (
                <div className="seg-row" style={{ marginTop: "0.4rem" }}>
                  <span className="seg-label">Modalities</span>
                  <ModalityBadges
                    input={pickedModel.input_modalities}
                    output={pickedModel.output_modalities}
                  />
                </div>
              )}
            </div>
            <div>
              <label htmlFor="provider">Provider 前缀</label>
              <select id="provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
                <option value="">无前缀（agent 默认 provider）</option>
                {providers.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
              <p className="hint">最终模型串 = 前缀/模型 ID，路由 agent 到配置的第三方端点。</p>
            </div>
          </div>

          {missing.length > 0 && (
            <div className="modality-warn" role="alert">
              ⛔ 场景 {envName} 需要模型支持 {missing.join("/")} 输入，{bareModel.trim()}
              的输入能力为 {(pickedModel?.input_modalities ?? []).join("/") || "未知"}
              ——跑起来会在读图步骤失败。
            </div>
          )}

          <label htmlFor="model-search" style={{ marginTop: "0.9rem" }}>
            搜索模型库（共 {orModels.length} 个）
          </label>
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
                  className={`model-row${m.id === bareModel ? " picked" : ""}`}
                  onClick={() => {
                    setBareModel(m.id);
                    setOrFilter("");
                  }}
                >
                  <span>{m.id}</span>
                  <ModalityBadges input={m.input_modalities} output={m.output_modalities} />
                </div>
              ))}
              {filteredModels.length === 0 && <div className="model-row muted">无匹配</div>}
            </div>
          )}

          {suggested.length > 0 && (
            <div className="suggest-row">
              {suggested.map((s) => {
                // Suggestions may carry a provider prefix — strip it so the
                // bare-model + provider-select mental model stays consistent.
                const parts = s.split("/");
                const bare = parts.length > 1 && providers.includes(parts[0])
                  ? parts.slice(1).join("/")
                  : s;
                return (
                  <button key={s} type="button" className="suggest-chip" onClick={() => setBareModel(bare)}>
                    {bare}
                  </button>
                );
              })}
            </div>
          )}

          {modelRef && (
            <p className="hint">
              提交时：{selectedList.map((a) => `${a} → ${modelRef}`).join("；") || "—"}
            </p>
          )}
        </div>
      </div>

      <div className="channel">
        <div className="channel-head">
          <span className="channel-tag">CH.04</span>
          <span className="channel-title">执行参数</span>
        </div>
        <div className="channel-body">
          <div className="seg-row">
            <span className="seg-label">执行方式</span>
            {([
              ["serial", "排队执行"],
              ["parallel", "并发执行"],
            ] as const).map(([value, label]) => (
              <label key={value}>
                <input
                  type="radio"
                  name="execution"
                  checked={execution === value}
                  onChange={() => setExecution(value)}
                />
                {label}
              </label>
            ))}
          </div>
          <p className="seg-note">
            {execution === "serial"
              ? "已选 agent 依次运行（前一个完成后再启动下一个）。本地部署模型建议排队，独占算力互不抢占，耗时数据才可比。"
              : "已选 agent 同时启动。云端模型服务无本地算力竞争，可并发跑，更快出结果。"}
          </p>

          <div className="seg-row" style={{ marginTop: "0.9rem" }}>
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
            {capturePolicy === "off" && "不采集通信。"}
            {capturePolicy === "metadata" && "仅记 size/timing/token/hash，不落报文正文（默认）。"}
            {capturePolicy === "parsed" && "落写盘前脱敏的解析后 request/response。"}
            {capturePolicy === "full" && "落写盘前脱敏的协议原生 request/response（可在运行详情查看真实 prompt/response）。"}
            {" "}实际生效值与服务端 wire_capture_max_policy 求最严格交集；仅走反代的第三方 provider attempt 才有正文可采。
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
          SAME-MODEL · <b>{selectedList.length}</b> agents × <b>{modelRef || "—"}</b> on{" "}
          <b>{envName || "—"}</b> · {execution}
        </div>
        <button className="trigger" onClick={onSubmit} disabled={!canSubmit}>
          {submitting ? "启动中…" : "运行同模型对比"}
        </button>
      </div>
    </div>
  );
}
