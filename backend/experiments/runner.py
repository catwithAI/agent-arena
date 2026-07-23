"""Asynchronous Experiment runner that drives the public Arena API."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from .models import (
    RETRY_ONLY_JOB_STATUSES,
    ExpandedTask,
    ExperimentConfig,
    ExperimentJob,
)
from .storage import (
    append_jsonl,
    atomic_write_json,
    latest_job_states,
    result_keys,
)

RUN_TERMINAL = frozenset({"completed", "failed"})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_config(path: Path) -> ExperimentConfig:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ExperimentConfig.model_validate(loaded)


def default_experiment_id() -> str:
    return datetime.now(timezone.utc).strftime("exp_%Y%m%d_%H%M%S")


class ExperimentRunner:
    def __init__(
        self,
        config: ExperimentConfig,
        *,
        experiments_root: Path,
        experiment_id: str | None = None,
        retry_failed: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.experiment_id = experiment_id or default_experiment_id()
        self.experiment_dir = Path(experiments_root) / self.experiment_id
        self.retry_failed = retry_failed
        self._provided_client = client
        self._journal_lock = asyncio.Lock()
        self._result_lock = asyncio.Lock()
        self.jobs_path = self.experiment_dir / "jobs.jsonl"
        self.results_path = self.experiment_dir / "results.jsonl"
        self.manifest_path = self.experiment_dir / "manifest.json"
        self._result_keys: set[tuple[str, str]] = set()

    async def run(self) -> dict[str, Any]:
        self.experiment_dir.mkdir(parents=True, exist_ok=True)
        owns_client = self._provided_client is None
        client = self._provided_client or httpx.AsyncClient(
            base_url=self.config.arena_base_url,
            timeout=60,
            trust_env=False,
        )
        try:
            preflight = await self._preflight(client)
            tasks = await self._expand_tasks(client, preflight["environments"])
            jobs = self._expand_jobs(tasks)
            self._prepare_manifest(preflight, jobs)
            states = latest_job_states(self.jobs_path)
            self._result_keys = result_keys(self.results_path)
            runnable = [job for job in jobs if self._should_run(job, states.get(job.job_id))]
            semaphore = asyncio.Semaphore(self.config.max_parallel_runs)

            async def guarded(job: ExperimentJob) -> None:
                async with semaphore:
                    await self._run_job(client, job, states.get(job.job_id))

            await asyncio.gather(*(guarded(job) for job in runnable))
            final_states = latest_job_states(self.jobs_path)
            summary = {
                "experiment_id": self.experiment_id,
                "jobs_total": len(jobs),
                "jobs_scheduled": len(runnable),
                "statuses": _counts(row.get("status") for row in final_states.values()),
                "results_path": str(self.results_path),
            }
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest["last_run_at"] = now_iso()
            manifest["run_summary"] = summary
            atomic_write_json(self.manifest_path, manifest)
            return summary
        finally:
            if owns_client:
                await client.aclose()

    async def _preflight(self, client: httpx.AsyncClient) -> dict[str, Any]:
        health, agents, environments = await asyncio.gather(
            self._get_json(client, "/api/healthz"),
            self._get_json(client, "/api/agents"),
            self._get_json(client, "/api/envs"),
        )
        if not isinstance(health, dict) or not health.get("ok"):
            raise RuntimeError("Arena health check did not return ok=true")
        if not isinstance(agents, list) or not isinstance(environments, list):
            raise RuntimeError("Arena catalog endpoints returned malformed responses")
        configured = {agent for variant in self.config.variants for agent in variant.agents}
        catalog = {
            row.get("id") or row.get("name"): row
            for row in agents
            if isinstance(row, dict) and (row.get("id") or row.get("name"))
        }
        missing = sorted(configured - set(catalog))
        if missing:
            raise RuntimeError(f"agents absent from Arena catalog: {missing}")
        unavailable = sorted(
            agent
            for agent in configured
            if (catalog.get(agent, {}).get("availability") or {}).get("status")
            not in (None, "available")
        )
        if unavailable:
            raise RuntimeError(f"agents unavailable in Arena catalog: {unavailable}")
        return {
            "checked_at": now_iso(),
            "health": health,
            "agents": {agent: catalog[agent] for agent in sorted(configured)},
            "environments": environments,
        }

    async def _expand_tasks(
        self, client: httpx.AsyncClient, environments: list[dict[str, Any]]
    ) -> list[ExpandedTask]:
        catalog = {
            str(row.get("name")): row for row in environments if isinstance(row, dict)
        }
        expanded: list[ExpandedTask] = []
        seen: set[tuple[str, str]] = set()
        for selection in self.config.tasks:
            env = catalog.get(selection.env)
            if env is None:
                raise RuntimeError(f"environment absent from Arena catalog: {selection.env}")
            if env.get("available") is False:
                raise RuntimeError(
                    f"environment unavailable: {selection.env}: {env.get('load_error')}"
                )
            if selection.all_tasks:
                rows = await self._get_json(
                    client, f"/api/envs/{selection.env}/tasks"
                )
                if not isinstance(rows, list) or not rows:
                    raise RuntimeError(f"environment has no tasks: {selection.env}")
            else:
                rows = [{"id": selection.task_id}]
            for row in rows:
                task_id = row.get("id") if isinstance(row, dict) else None
                if not isinstance(task_id, str) or not task_id:
                    raise RuntimeError(f"malformed task catalog for environment {selection.env}")
                key = (selection.env, task_id)
                if key in seen:
                    continue
                seen.add(key)
                expanded.append(
                    ExpandedTask(
                        env=selection.env,
                        task_id=task_id,
                        timeout_seconds=row.get("timeout_seconds"),
                        labels=selection.labels,
                    )
                )
        return expanded

    def _expand_jobs(self, tasks: list[ExpandedTask]) -> list[ExperimentJob]:
        return [
            ExperimentJob.create(
                experiment_hash=self.config.config_hash,
                variant=variant,
                task=task,
                repeat=repeat,
            )
            for task in tasks
            for variant in self.config.variants
            for repeat in range(self.config.repeats)
        ]

    def _prepare_manifest(
        self, preflight: dict[str, Any], jobs: list[ExperimentJob]
    ) -> None:
        if self.manifest_path.is_file():
            current = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if current.get("config_hash") != self.config.config_hash:
                raise RuntimeError(
                    f"experiment {self.experiment_id} exists with a different config"
                )
            return
        manifest = {
            "schema_version": "1",
            "experiment_id": self.experiment_id,
            "created_at": now_iso(),
            "config_hash": self.config.config_hash,
            "config": self.config.model_dump(mode="json"),
            "source": {"git_commit": _git_commit()},
            "preflight": preflight,
            "job_count": len(jobs),
            "job_ids": [job.job_id for job in jobs],
        }
        atomic_write_json(self.manifest_path, manifest)

    def _should_run(
        self, job: ExperimentJob, state: dict[str, Any] | None
    ) -> bool:
        if state is None:
            return True
        status = state.get("status")
        if status == "completed":
            return False
        if status in RETRY_ONLY_JOB_STATUSES:
            return self.retry_failed
        return True

    async def _run_job(
        self,
        client: httpx.AsyncClient,
        job: ExperimentJob,
        previous: dict[str, Any] | None,
    ) -> None:
        previous_status = previous.get("status") if previous else None
        retrying_terminal = (
            self.retry_failed and previous_status in RETRY_ONLY_JOB_STATUSES
        )
        run_id = None if retrying_terminal else (
            previous.get("run_id") if previous else None
        )
        try:
            if not isinstance(run_id, str):
                await self._record_job(job, "submitting")
                response = await client.post("/api/runs", json=job.request)
                response.raise_for_status()
                body = response.json()
                run_id = body.get("run_id")
                if not isinstance(run_id, str):
                    raise RuntimeError("POST /api/runs returned no run_id")
                await self._record_job(job, "running", run_id=run_id)

            run = await self._poll_run(client, run_id)
            if run is None:
                await self._record_job(job, "poll_timeout", run_id=run_id)
                return
            await self._collect_results(client, job, run_id, run)
            status = "completed" if run.get("status") == "completed" else "run_failed"
            await self._record_job(job, status, run_id=run_id)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:2000]
            await self._record_job(
                job,
                "submission_failed" if run_id is None else "collection_failed",
                run_id=run_id,
                error=f"HTTP {exc.response.status_code}: {detail}",
            )
        except Exception as exc:
            await self._record_job(
                job,
                "submission_failed" if run_id is None else "collection_failed",
                run_id=run_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _poll_run(
        self, client: httpx.AsyncClient, run_id: str
    ) -> dict[str, Any] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.poll_timeout_seconds
        while loop.time() < deadline:
            run = await self._get_json(client, f"/api/runs/{run_id}")
            if isinstance(run, dict) and run.get("status") in RUN_TERMINAL:
                return run
            await asyncio.sleep(self.config.poll_interval_seconds)
        return None

    async def _collect_results(
        self,
        client: httpx.AsyncClient,
        job: ExperimentJob,
        run_id: str,
        run: dict[str, Any],
    ) -> None:
        rows: list[dict[str, Any]] = []
        for attempt in run.get("attempts", []):
            attempt_id = attempt.get("id")
            if not isinstance(attempt_id, str):
                continue
            key = (job.job_id, attempt_id)
            if key in self._result_keys:
                continue
            detail, manifest = await asyncio.gather(
                self._get_json(client, f"/api/runs/{run_id}/attempts/{attempt_id}"),
                self._get_json(
                    client,
                    f"/api/runs/{run_id}/attempts/{attempt_id}/agent-manifest",
                    allow_not_found=True,
                ),
            )
            detail = detail if isinstance(detail, dict) else {}
            public_manifest = (
                manifest.get("manifest")
                if isinstance(manifest, dict) and manifest.get("status") == "available"
                else None
            )
            rows.append(
                {
                    "schema_version": "1",
                    "experiment_id": self.experiment_id,
                    "job_id": job.job_id,
                    "variant": job.variant,
                    "repeat": job.repeat,
                    "labels": job.labels,
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "env": job.env,
                    "task_id": job.task_id,
                    "agent": detail.get("agent_name") or attempt.get("agent_name"),
                    "requested_model": detail.get("model") or attempt.get("model"),
                    "effective_model": (
                        (public_manifest or {}).get("model", {}).get("effective")
                        or attempt.get("model_used")
                    ),
                    "status": detail.get("status") or attempt.get("status"),
                    "score_total": detail.get("score_total"),
                    "scores": detail.get("scores", []),
                    "duration_ms": detail.get("duration_ms"),
                    "token_usage": detail.get("token_usage", {}),
                    "cost_estimate": detail.get("cost_estimate"),
                    "error_code": detail.get("error_code"),
                    "security": detail.get("security", {}),
                    "agent_manifest": public_manifest,
                    "recorded_at": now_iso(),
                }
            )
        if rows:
            async with self._result_lock:
                unseen = [
                    row
                    for row in rows
                    if (row["job_id"], row["attempt_id"]) not in self._result_keys
                ]
                append_jsonl(self.results_path, unseen)
                self._result_keys.update(
                    (row["job_id"], row["attempt_id"]) for row in unseen
                )

    async def _record_job(
        self,
        job: ExperimentJob,
        status: str,
        *,
        run_id: str | None = None,
        error: str | None = None,
    ) -> None:
        row = {
            "timestamp": now_iso(),
            "job_id": job.job_id,
            "variant": job.variant,
            "env": job.env,
            "task_id": job.task_id,
            "repeat": job.repeat,
            "status": status,
            "run_id": run_id,
            "error": error,
        }
        async with self._journal_lock:
            append_jsonl(self.jobs_path, [row])

    @staticmethod
    async def _get_json(
        client: httpx.AsyncClient, path: str, *, allow_not_found: bool = False
    ) -> Any:
        response = await client.get(path)
        if allow_not_found and response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))
