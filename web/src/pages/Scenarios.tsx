import { useEffect, useState } from "react";

import { api, type EnvSummary } from "../api/client";

export function Scenarios() {
  const [envs, setEnvs] = useState<EnvSummary[]>([]);

  useEffect(() => {
    api.envs().then(setEnvs);
  }, []);

  return (
    <div>
      <div className="readout">
        <span>
          ENVS <b>{envs.length}</b>
        </span>
      </div>

      <h1>场景与评分</h1>
      <p className="lede">每个评测环境的考察内容、可用工具与评分维度权重。</p>

      {envs.map((env, i) => (
        <div className="channel" key={env.name}>
          <div className="channel-head">
            <span className="channel-tag">ENV.{String(i + 1).padStart(2, "0")}</span>
            <span className="channel-title">
              {env.name} <span className="soft">/ {env.category}</span>
            </span>
          </div>
          <div className="channel-body">
            <div className="readout" style={{ marginBottom: "0.8rem" }}>
              <span>
                及格线 <b>{env.pass_threshold ?? "—"}</b>
              </span>
              <span>
                工具 <b>{env.tool_count}</b>
              </span>
              <span>
                任务 <b>{env.task_count}</b>
              </span>
            </div>
            <p style={{ marginTop: 0 }}>{env.description}</p>
            <div className="env-desc">考察内容：{env.test_focus}</div>
            <table style={{ marginTop: "1rem" }}>
              <thead>
                <tr>
                  <th>评分维度</th>
                  <th>权重</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {env.dimensions.map((d) => (
                  <tr key={d.name}>
                    <td>{d.name}</td>
                    <td>{d.weight}</td>
                    <td>{d.description}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}
