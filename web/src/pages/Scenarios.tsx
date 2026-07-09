import { useEffect, useState } from "react";

import { api, type EnvSummary } from "../api/client";

export function Scenarios() {
  const [envs, setEnvs] = useState<EnvSummary[]>([]);

  useEffect(() => {
    api.envs().then(setEnvs);
  }, []);

  return (
    <div>
      {envs.map((env) => (
        <div className="panel" key={env.name}>
          <h2>{env.name}</h2>
          <p className="muted">
            {env.category} · pass_threshold={env.pass_threshold} · {env.tool_count} tools · {env.task_count} tasks
          </p>
          <p>{env.description}</p>
          <p>
            <strong>What this tests:</strong> {env.test_focus}
          </p>
          <table>
            <thead>
              <tr>
                <th>Dimension</th>
                <th>Weight</th>
                <th>Description</th>
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
      ))}
    </div>
  );
}
