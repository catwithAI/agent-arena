# 批量实验

Experiment 是 `Run → Attempt` 上方的批量编排层。它不改变现有数据库模型，而是通过
公开 API 提交 Run，并将可恢复状态写入：

```text
data/experiments/<experiment_id>/
├── manifest.json    # 配置快照、哈希、代码版本和预检目录
├── jobs.jsonl       # 追加式作业状态日志
├── results.jsonl    # 每个 Attempt 的结果和公开 Agent Manifest
├── summary.json     # 机器可读聚合
└── report.md        # 人可读报告
```

这种边界允许 Experiment 驱动本机或远程 Arena，也不会绕过 Agent Registry、
兼容性预检、环境评分、安全扫描或 Wire 采集。

## 运行

先启动 Arena，然后创建配置：

```bash
cp experiment.yaml.example experiment.yaml
uv run python scripts/run_experiment.py --config experiment.yaml
```

配置把以下维度展开为确定性的作业：

```text
task × variant × repeat
```

一个 variant 就是一份合法的 `POST /api/runs` 比较请求，可以使用 `multi-agent`、
`same-model` 或 `multi-model`。单个 Run 内部的 `serial`/`parallel` 仍由 Arena
执行；`max_parallel_runs` 控制 Experiment 同时提交多少个 Run。

`all_tasks: true` 从目标 Arena 的 `/api/envs/<name>/tasks` 读取任务，因此控制端
不需要和服务端共享环境目录。

## 中断恢复

命令输出 Experiment ID。进程中断后使用同一配置恢复：

```bash
uv run python scripts/run_experiment.py \
  --config experiment.yaml \
  --resume exp_20260723_120000
```

- `completed` 作业直接跳过；
- 已取得 `run_id` 的非终态作业继续轮询原 Run，不重复提交；
- 提交阶段中断、尚无 `run_id` 的作业重新提交；
- 失败作业默认保留，不自动重跑。

显式重跑失败作业：

```bash
uv run python scripts/run_experiment.py \
  --config experiment.yaml \
  --resume exp_20260723_120000 \
  --retry-failed
```

旧一代结果保留在 `results.jsonl` 供审计；`summary.json` 和 `report.md` 默认只统计
每个 Job 最新的 Run。

为了避免错误续接，恢复时配置的 RFC 8785 哈希必须与原 Experiment 完全一致。

## 聚合

运行命令结束后会自动聚合。也可以单独重建报告：

```bash
uv run python scripts/aggregate_experiment.py exp_20260723_120000
```

当前聚合包括：

- 按 Agent、Agent/模型、环境和 variant 的完成率、通过率、均值、中位数、
  标准差、95% 均值置信区间、耗时、Token 和成本；
- 按评分维度的均值、标准差和置信区间；
- 同一任务和重复轮次下的配对胜负、平局、平均分差和分差置信区间；
- Git commit、配置哈希、AgentSpec 哈希、有效模型、Agent 版本和降级信息。

95% 区间使用正态近似。样本量很小时应把它看作诊断提示，而不是严格统计推断。

## 持久化与并发保证

- `manifest.json`、`summary.json` 使用临时文件和原子替换；
- Job 与结果使用 append-only JSONL，每次追加后执行 `fsync`；
- 同一进程内的并发写入通过异步锁串行化；
- `(job_id, attempt_id)` 防止恢复时重复写入同一个 Attempt。

目前不支持多个 Experiment 控制进程同时写同一个 Experiment 目录。需要分布式调度时，
应把相同状态机迁移到服务端数据库，并增加租约/所有权机制。
