from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from backend.experiments.aggregate import aggregate_experiment
from backend.experiments.models import ExperimentConfig
from backend.experiments.runner import ExperimentRunner


def _config() -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        {
            "name": "test",
            "repeats": 1,
            "max_parallel_runs": 2,
            "poll_interval_seconds": 0.05,
            "poll_timeout_seconds": 2,
            "tasks": [{"env": "order-desk", "all_tasks": True}],
            "variants": [
                {
                    "name": "agents",
                    "agents": ["agent-a", "agent-b"],
                    "compare_mode": "multi-agent",
                }
            ],
        }
    )


def _catalog_response() -> list[dict]:
    return [
        {
            "id": agent,
            "availability": {"status": "available"},
        }
        for agent in ("agent-a", "agent-b")
    ]


def _attempt_detail(attempt_id: str) -> dict:
    is_a = attempt_id.endswith("a")
    return {
        "id": attempt_id,
        "agent_name": "agent-a" if is_a else "agent-b",
        "model": "model-1",
        "status": "completed",
        "score_total": 80 if is_a else 60,
        "scores": [{"dimension": "quality", "value": 90 if is_a else 50}],
        "duration_ms": 1000 if is_a else 2000,
        "token_usage": {"input_tokens": 100, "output_tokens": 20},
        "cost_estimate": 0.01,
        "security": {"event_count": 0},
    }


@pytest.mark.asyncio
async def test_experiment_runs_resumes_and_aggregates(tmp_path: Path):
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        path = request.url.path
        if path == "/api/healthz":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/agents":
            return httpx.Response(200, json=_catalog_response())
        if path == "/api/envs":
            return httpx.Response(
                200, json=[{"name": "order-desk", "available": True}]
            )
        if path == "/api/envs/order-desk/tasks":
            return httpx.Response(
                200, json=[{"id": "task-1", "timeout_seconds": 30}]
            )
        if path == "/api/runs" and request.method == "POST":
            posts += 1
            body = json.loads(request.content)
            assert body["agents"] == ["agent-a", "agent-b"]
            return httpx.Response(200, json={"run_id": "run-1"})
        if path == "/api/runs/run-1":
            return httpx.Response(
                200,
                json={
                    "id": "run-1",
                    "status": "completed",
                    "attempts": [
                        {"id": "attempt-a", "agent_name": "agent-a"},
                        {"id": "attempt-b", "agent_name": "agent-b"},
                    ],
                },
            )
        if path.startswith("/api/runs/run-1/attempts/") and path.endswith(
            "/agent-manifest"
        ):
            attempt_id = path.split("/")[-2]
            agent = "agent-a" if attempt_id.endswith("a") else "agent-b"
            return httpx.Response(
                200,
                json={
                    "status": "available",
                    "manifest": {
                        "agent": {
                            "id": agent,
                            "version": "1.0",
                            "spec_hash": f"sha256:{agent}",
                            "transport": "local-cli",
                        },
                        "model": {"effective": "model-1"},
                        "degradations": [],
                    },
                },
            )
        if path.startswith("/api/runs/run-1/attempts/"):
            return httpx.Response(200, json=_attempt_detail(path.split("/")[-1]))
        raise AssertionError(f"unexpected request: {request.method} {path}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://arena.test"
    ) as client:
        runner = ExperimentRunner(
            _config(),
            experiments_root=tmp_path,
            experiment_id="exp-test",
            client=client,
        )
        first = await runner.run()
        assert first["statuses"] == {"completed": 1}
        assert posts == 1

        resumed = ExperimentRunner(
            _config(),
            experiments_root=tmp_path,
            experiment_id="exp-test",
            client=client,
        )
        second = await resumed.run()
        assert second["jobs_scheduled"] == 0
        assert posts == 1

    summary = aggregate_experiment(tmp_path / "exp-test")
    assert summary["attempts"] == 2
    assert summary["by_agent"]["agent-a"]["avg_score"] == 80
    assert summary["by_agent"]["agent-b"]["avg_score"] == 60
    assert summary["head_to_head"][0]["left_wins"] == 1
    assert summary["reproducibility"]["manifest_coverage"] == {
        "available": 2,
        "total": 2,
    }
    assert (tmp_path / "exp-test" / "summary.json").is_file()
    assert "Agent summary" in (
        tmp_path / "exp-test" / "report.md"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_retry_failed_submits_new_run_and_reports_latest(tmp_path: Path):
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        path = request.url.path
        if path == "/api/healthz":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/agents":
            return httpx.Response(200, json=_catalog_response())
        if path == "/api/envs":
            return httpx.Response(
                200, json=[{"name": "order-desk", "available": True}]
            )
        if path == "/api/envs/order-desk/tasks":
            return httpx.Response(200, json=[{"id": "task-1"}])
        if path == "/api/runs" and request.method == "POST":
            posts += 1
            return httpx.Response(200, json={"run_id": f"run-{posts}"})
        if path in {"/api/runs/run-1", "/api/runs/run-2"}:
            run_id = path.rsplit("/", 1)[-1]
            succeeded = run_id == "run-2"
            return httpx.Response(
                200,
                json={
                    "id": run_id,
                    "status": "completed" if succeeded else "failed",
                    "attempts": [
                        {"id": f"attempt-{posts}-a", "agent_name": "agent-a"},
                        {"id": f"attempt-{posts}-b", "agent_name": "agent-b"},
                    ],
                },
            )
        if path.endswith("/agent-manifest"):
            return httpx.Response(404)
        if "/attempts/" in path:
            attempt_id = path.rsplit("/", 1)[-1]
            detail = _attempt_detail(attempt_id)
            detail["score_total"] = 90 if posts == 2 else 10
            return httpx.Response(200, json=detail)
        raise AssertionError(f"unexpected request: {request.method} {path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://arena.test"
    ) as client:
        first = ExperimentRunner(
            _config(),
            experiments_root=tmp_path,
            experiment_id="exp-retry",
            client=client,
        )
        assert (await first.run())["statuses"] == {"run_failed": 1}
        retry = ExperimentRunner(
            _config(),
            experiments_root=tmp_path,
            experiment_id="exp-retry",
            retry_failed=True,
            client=client,
        )
        assert (await retry.run())["statuses"] == {"completed": 1}
    assert posts == 2
    summary = aggregate_experiment(tmp_path / "exp-retry")
    assert summary["attempts"] == 2
    assert {item["avg_score"] for item in summary["by_agent"].values()} == {90}


@pytest.mark.asyncio
async def test_poll_timeout_resumes_existing_run_without_resubmission(tmp_path: Path):
    posts = 0
    ready = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        path = request.url.path
        if path == "/api/healthz":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/agents":
            return httpx.Response(200, json=_catalog_response())
        if path == "/api/envs":
            return httpx.Response(
                200, json=[{"name": "order-desk", "available": True}]
            )
        if path == "/api/envs/order-desk/tasks":
            return httpx.Response(200, json=[{"id": "task-1"}])
        if path == "/api/runs" and request.method == "POST":
            posts += 1
            return httpx.Response(200, json={"run_id": "run-resume"})
        if path == "/api/runs/run-resume":
            return httpx.Response(
                200,
                json={
                    "id": "run-resume",
                    "status": "completed" if ready else "running",
                    "attempts": (
                        [{"id": "attempt-a", "agent_name": "agent-a"}]
                        if ready
                        else []
                    ),
                },
            )
        if path.endswith("/agent-manifest"):
            return httpx.Response(404)
        if path.endswith("/attempt-a"):
            return httpx.Response(200, json=_attempt_detail("attempt-a"))
        raise AssertionError(f"unexpected request: {request.method} {path}")

    config = _config().model_copy(update={"poll_timeout_seconds": 1})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://arena.test"
    ) as client:
        first = ExperimentRunner(
            config,
            experiments_root=tmp_path,
            experiment_id="exp-poll-resume",
            client=client,
        )
        assert (await first.run())["statuses"] == {"poll_timeout": 1}
        ready = True
        resumed = ExperimentRunner(
            config,
            experiments_root=tmp_path,
            experiment_id="exp-poll-resume",
            client=client,
        )
        assert (await resumed.run())["statuses"] == {"completed": 1}
    assert posts == 1


def test_experiment_config_rejects_invalid_mode_shapes():
    with pytest.raises(ValidationError, match="multi-model requires exactly one"):
        ExperimentConfig.model_validate(
            {
                "name": "bad",
                "tasks": [{"env": "x", "task_id": "t"}],
                "variants": [
                    {
                        "name": "bad",
                        "compare_mode": "multi-model",
                        "agents": ["a", "b"],
                        "models": ["m1", "m2"],
                    }
                ],
            }
        )
