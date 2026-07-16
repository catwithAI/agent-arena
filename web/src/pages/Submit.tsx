import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, type AgentInfo, type EnvSummary, type TaskJson } from "../api/client";

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
  // Task timeout in minutes; empty = unlimited (no time-budget notice sent
  // to the agent, no deadline enforced by the adapter).
  const [timeoutMinutes, setTimeoutMinutes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.agents().then(setAgents).catch((e) => setError(String(e)));
    api.envs().then((list) => {
      setEnvs(list);
      if (list.length > 0) setEnvName(list[0].name);
    });
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
      setError("Select at least one agent.");
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

  return (
    <div>
      <div className="panel">
        <h2>New comparison run</h2>

        <label>Environment</label>
        <select value={envName} onChange={(e) => setEnvName(e.target.value)}>
          {envs.map((e) => (
            <option key={e.name} value={e.name}>
              {e.name}
            </option>
          ))}
        </select>
        {currentEnv && <p className="muted">{currentEnv.description}</p>}

        <label>Task</label>
        <select value={taskId} onChange={(e) => setTaskId(e.target.value)}>
          <option value="">— free-form prompt —</option>
          {tasks.map((t) => (
            <option key={t.id} value={t.id}>
              {t.id}
            </option>
          ))}
        </select>

        {!taskId && (
          <>
            <label>Prompt</label>
            <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Describe the task…" />
          </>
        )}
        {taskId && tasks.find((t) => t.id === taskId) && (
          <p className="muted">{tasks.find((t) => t.id === taskId)?.prompt}</p>
        )}

        <label>Agents</label>
        <div className="checkbox-row">
          {agents.map((a) => (
            <label key={a.name}>
              <input
                type="checkbox"
                checked={selectedAgents.includes(a.name)}
                onChange={() => toggleAgent(a.name)}
                disabled={a.status !== "available"}
              />
              {a.name} {a.status !== "available" && <span className="muted">(unavailable)</span>}
            </label>
          ))}
        </div>

        <label>Model override (optional, applies to all selected agents)</label>
        <input type="text" value={model} onChange={(e) => setModel(e.target.value)} placeholder="e.g. sonnet, gpt-5" />

        <label>Task timeout (minutes)</label>
        <input
          type="number"
          min={0}
          step={0.5}
          value={timeoutMinutes}
          onChange={(e) => setTimeoutMinutes(e.target.value)}
          placeholder="leave blank = unlimited"
        />
        <p className="muted" style={{ fontSize: 12, marginTop: -4 }}>
          {timeoutMinutes.trim() === ""
            ? "No time limit is told to the agent, and the run has no overall deadline."
            : `The agent is told it has ${timeoutMinutes} minute(s) and the run is force-stopped after that.`}
        </p>

        {error && <p className="error-box">{error}</p>}

        <div style={{ marginTop: "1rem" }}>
          <button onClick={onSubmit} disabled={submitting || !envName}>
            {submitting ? "Starting…" : "Run"}
          </button>
        </div>
      </div>
    </div>
  );
}
