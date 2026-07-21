import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, type AgentInfo, type EnvSummary, type OpenRouterModel, type TaskJson } from "../api/client";
import { ModalityBadges, ModalityChip, modalityOptionMark } from "../components/ModalityChips";

export function Submit() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [envs, setEnvs] = useState<EnvSummary[]>([]);
  const [tasks, setTasks] = useState<TaskJson[]>([]);
  const [envName, setEnvName] = useState("");
  const [taskId, setTaskId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [selectedAgents, setSelectedAgents] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [orModels, setOrModels] = useState<OpenRouterModel[]>([]);
  const [modelFilter, setModelFilter] = useState("");
  // 任务超时（分钟）；留空 = 不限时（不向 agent 发送时间预算提示，也不强制截止）。
  const [timeoutMinutes, setTimeoutMinutes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.agents().then(setAgents).catch((e) => setError(String(e)));
    api.envs().then((list) => {
      setEnvs(list);
      if (list.length > 0) setEnvName(list[0].name);
    });
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
    setSelectedAgents((prev) => (prev.includes(name) ? prev.filter((a) => a !== name) : [...prev, name]));
  }

  async function onSubmit() {
    setError(null);
    if (selectedAgents.length === 0) {
      setError("请至少选择一个 agent。");
      return;
    }
    setSubmitting(true);
    try {
      const body: Parameters<typeof api.createRun>[0] = {
        env_name: envName,
        agents: selectedAgents,
      };
      if (taskId) body.task_id = taskId;
      else body.prompt = prompt;
      if (model.trim()) body.model = model.trim();
      body.timeout_seconds = timeoutMinutes.trim() === "" ? null : Math.round(Number(timeoutMinutes) * 60);
      const resp = await api.createRun(body);
      navigate(`/runs/${resp.run_id}`);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  }

  const currentEnv = envs.find((e) => e.name === envName);
  const currentTask = tasks.find((t) => t.id === taskId);
  const onlineAgents = agents.filter((a) => a.status === "available").length;

  const filteredModels = (() => {
    const q = modelFilter.trim().toLowerCase();
    if (!q) return [];
    return orModels
      .filter((m) => m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
      .slice(0, 50);
  })();

  function fmtCtx(len: number | null): string {
    if (!len) return "";
    if (len >= 1_000_000) return `${Math.round(len / 1_000_000)}M`;
    if (len >= 1_000) return `${Math.round(len / 1_000)}K`;
    return String(len);
  }

  return (
    <div>
      <div className="readout">
        <span>
          ENVS <b>{envs.length}</b>
        </span>
        <span>
          AGENTS ONLINE <b>{onlineAgents}</b>/{agents.length}
        </span>
        <span>
          MODELS INDEXED <b>{orModels.length}</b>
        </span>
      </div>

      <h1>配置一次对比评测</h1>
      <p className="lede">
        选定环境、任务与参与的 agent，系统会为每个 agent 各起独立的执行会话，采集执行轨迹、思考过程与最终评分，供并排核对。
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
          {currentTask && <div className="env-desc">{currentTask.prompt}</div>}
        </div>
      </div>

      <div className="channel">
        <div className="channel-head">
          <span className="channel-tag">CH.02</span>
          <span className="channel-title">
            参与 Agent <span className="soft">/ 已选 {selectedAgents.length}</span>
          </span>
        </div>
        <div className="channel-body">
          <div className="chan-grid">
            {agents.map((a, i) => {
              const available = a.status === "available";
              const on = selectedAgents.includes(a.name);
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

          <div className="two-col" style={{ marginTop: "1.1rem" }}>
            <div>
              <label htmlFor="model">模型覆盖（可选，作用于所有已选 agent）</label>
              <input
                type="text"
                id="model"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="模型 ID，如 sonnet、gpt-5"
              />
            </div>
            <div>
              <label htmlFor="model-search">搜索模型库（共 {orModels.length} 个）</label>
              <input
                type="text"
                id="model-search"
                value={modelFilter}
                onChange={(e) => setModelFilter(e.target.value)}
                placeholder="claude / gpt / gemini …"
              />
              {modelFilter.trim() && (
                <div className="model-results">
                  {filteredModels.map((m) => (
                    <div
                      key={m.id}
                      className={`model-row${m.id === model ? " picked" : ""}`}
                      onClick={() => {
                        setModel(m.id);
                        setModelFilter("");
                      }}
                    >
                      <span>{m.id}</span>
                      <ModalityBadges input={m.input_modalities} output={m.output_modalities} />
                      <span className="ctx">{fmtCtx(m.context_length)}</span>
                    </div>
                  ))}
                  {filteredModels.length === 0 && <div className="model-row muted">无匹配</div>}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="channel">
        <div className="channel-head">
          <span className="channel-tag">CH.03</span>
          <span className="channel-title">执行参数</span>
        </div>
        <div className="channel-body">
          <div className="two-col">
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
          READY · <b>{selectedAgents.length}</b> agents queued on <b>{envName || "—"}</b>
          {model.trim() && (
            <>
              {" "}
              · model <b>{model.trim()}</b>
            </>
          )}
        </div>
        <button className="trigger" onClick={onSubmit} disabled={submitting || !envName}>
          {submitting ? "启动中…" : "启动评测"}
        </button>
      </div>
    </div>
  );
}
