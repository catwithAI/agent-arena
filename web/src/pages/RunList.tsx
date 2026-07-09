import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, type RunRow } from "../api/client";

export function RunList() {
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listRuns().then(setRuns).catch((e) => setError(String(e)));
  }, []);

  return (
    <div className="panel">
      <h2>Run history</h2>
      {error && <p className="error-box">{error}</p>}
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Env</th>
            <th>Status</th>
            <th>Attempts</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.run_id}>
              <td>
                <Link to={`/runs/${r.run_id}`}>{r.run_id}</Link>
              </td>
              <td>{r.env_name}</td>
              <td>
                <span className={`badge ${r.run_status}`}>{r.run_status}</span>
              </td>
              <td>{r.attempt_count}</td>
              <td className="muted">{r.created_at}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
