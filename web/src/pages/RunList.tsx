import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, type RunRow } from "../api/client";

export function RunList() {
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listRuns().then(setRuns).catch((e) => setError(String(e)));
  }, []);

  const runningCount = runs.filter((r) => ["queued", "running"].includes(r.run_status)).length;

  return (
    <div>
      <div className="readout">
        <span>
          RUNS <b>{runs.length}</b>
        </span>
        <span>
          ACTIVE <b>{runningCount}</b>
        </span>
      </div>

      <h1>历史记录</h1>
      <p className="lede">已提交的全部对比评测。点击任意一条查看排行榜、逐 agent 对话记录与产物。</p>

      {error && <p className="error-box">{error}</p>}

      <div className="channel">
        <div className="channel-head">
          <span className="channel-tag">LOG</span>
          <span className="channel-title">评测记录</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>评测</th>
              <th>环境</th>
              <th>状态</th>
              <th>尝试数</th>
              <th>创建时间</th>
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
            {runs.length === 0 && !error && (
              <tr>
                <td colSpan={5} className="muted">
                  暂无评测记录。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
