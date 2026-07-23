import { useEffect, useState } from "react";

import { api, type EnvSummary } from "../api/client";

const CATEGORIES = [
  {
    key: "general-assistant",
    label: "通用助理",
    blurb: "检索、文件阅读、多模态理解和开放问题求解",
  },
  {
    key: "office-productivity",
    label: "办公与内容生产",
    blurb: "表格、会计材料、演示文稿与多来源业务信息处理",
  },
  {
    key: "real-skill",
    label: "真实作战 Skill",
    blurb: "接入真实业务 Skill，考察确定性计算链的严格复现",
  },
  {
    key: "complex-workflow",
    label: "复杂长链路",
    blurb: "多步工具编排、规划质量、产物生成与错误恢复",
  },
  {
    key: "coding",
    label: "编程与算法",
    blurb: "代码实现、约束求解、算法优化与隐藏测试表现",
  },
  {
    key: "agent-system",
    label: "Agent 系统能力",
    blurb: "多轮记忆、上下文压缩、子 Agent 调度与可观测性",
  },
  {
    key: "safety-hitl",
    label: "安全 · 人在回路",
    blurb: "不可逆高后果操作前的批准、安全替代与行为评估",
  },
  {
    key: "baseline",
    label: "基础 · 约束遵守",
    blurb: "基础工具使用与预算、日期、偏好等明确约束的遵守",
  },
] as const;

export function Scenarios() {
  const [envs, setEnvs] = useState<EnvSummary[]>([]);

  useEffect(() => {
    api.envs().then(setEnvs);
  }, []);

  const knownCategories = new Set<string>(CATEGORIES.map((category) => category.key));
  const groups = [
    ...CATEGORIES.map((category) => ({
      ...category,
      envs: envs.filter((env) => env.category === category.key),
    })),
    {
      key: "uncategorized",
      label: "未分类",
      blurb: "尚未迁移到稳定一级分类的场景",
      envs: envs.filter((env) => !knownCategories.has(env.category)),
    },
  ].filter((group) => group.envs.length > 0);

  return (
    <div>
      <div className="readout">
        <span>
          ENVS <b>{envs.length}</b>
        </span>
      </div>

      <h1>场景与评分</h1>
      <p className="lede">
        每个评测环境的考察内容、可用工具与评分维度权重；场景按主要评测能力归入八个稳定一级分类。
      </p>

      {groups.map((group) => (
        <section className="scenario-group" key={group.key}>
          <div className="scenario-group-head">
            <h2>{group.label}</h2>
            <span className="soft">{group.blurb}</span>
            <b>{group.envs.length} 个场景</b>
          </div>
          {group.envs.map((env, i) => (
            <div className="channel" key={env.name}>
              <div className="channel-head">
                <span className="channel-tag">
                  {group.key.toUpperCase()}.{String(i + 1).padStart(2, "0")}
                </span>
                <span className="channel-title">{env.name}</span>
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
                    {env.dimensions.map((dimension) => (
                      <tr key={dimension.name}>
                        <td>{dimension.name}</td>
                        <td>{dimension.weight}</td>
                        <td>{dimension.description}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}
