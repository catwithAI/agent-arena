"""Validated configuration and durable record models for experiments."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSelection(StrictModel):
    env: str
    task_id: str | None = None
    all_tasks: bool = False
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def exactly_one_selector(self) -> "TaskSelection":
        if bool(self.task_id) == self.all_tasks:
            raise ValueError("task selection requires exactly one of task_id or all_tasks=true")
        return self


class RunVariant(StrictModel):
    name: str
    agents: list[str]
    compare_mode: Literal["multi-agent", "same-model", "multi-model"] = "multi-agent"
    model: str | None = None
    models: dict[str, str] | list[str] | None = None
    execution: Literal["serial", "parallel"] | None = None
    capture_policy: Literal["off", "metadata", "parsed", "full"] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "RunVariant":
        if not self.agents or len(self.agents) != len(set(self.agents)):
            raise ValueError("agents must be a non-empty list of distinct ids")
        if self.compare_mode == "same-model":
            if len(self.agents) < 2:
                raise ValueError("same-model requires at least two agents")
            if isinstance(self.models, list):
                raise ValueError("same-model models must be an agent-to-model mapping")
            if isinstance(self.models, dict) and set(self.models) != set(self.agents):
                raise ValueError("same-model models must cover every selected agent")
            if self.model is None and self.models is None:
                raise ValueError("same-model requires model or models")
        if self.compare_mode == "multi-model":
            if len(self.agents) != 1:
                raise ValueError("multi-model requires exactly one agent")
            if not isinstance(self.models, list) or len(self.models) < 2:
                raise ValueError("multi-model requires a list of at least two models")
        return self

    def request_fields(self) -> dict[str, Any]:
        payload = self.model_dump(exclude={"name"}, exclude_none=True)
        return payload


class ExperimentConfig(StrictModel):
    schema_version: Literal["1"] = "1"
    name: str
    arena_base_url: str = "http://127.0.0.1:8100"
    repeats: int = Field(default=1, ge=1, le=1000)
    max_parallel_runs: int = Field(default=1, ge=1, le=100)
    poll_interval_seconds: float = Field(default=2.0, ge=0.05, le=60)
    poll_timeout_seconds: float = Field(default=7200, ge=1)
    pass_threshold: float = 60
    tasks: list[TaskSelection]
    variants: list[RunVariant]

    @model_validator(mode="after")
    def validate_collections(self) -> "ExperimentConfig":
        if not self.tasks:
            raise ValueError("tasks must not be empty")
        if not self.variants:
            raise ValueError("variants must not be empty")
        names = [variant.name for variant in self.variants]
        if len(names) != len(set(names)):
            raise ValueError("variant names must be unique")
        self.arena_base_url = self.arena_base_url.rstrip("/")
        return self

    @property
    def config_hash(self) -> str:
        canonical = rfc8785.dumps(self.model_dump(mode="json"))
        return "sha256:" + hashlib.sha256(canonical).hexdigest()


class ExpandedTask(StrictModel):
    env: str
    task_id: str
    timeout_seconds: int | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class ExperimentJob(StrictModel):
    job_id: str
    variant: str
    env: str
    task_id: str
    repeat: int
    labels: dict[str, str] = Field(default_factory=dict)
    request: dict[str, Any]

    @classmethod
    def create(
        cls,
        *,
        experiment_hash: str,
        variant: RunVariant,
        task: ExpandedTask,
        repeat: int,
    ) -> "ExperimentJob":
        identity = {
            "experiment_hash": experiment_hash,
            "variant": variant.name,
            "env": task.env,
            "task_id": task.task_id,
            "repeat": repeat,
            "request": variant.request_fields(),
        }
        digest = hashlib.sha256(rfc8785.dumps(identity)).hexdigest()[:20]
        return cls(
            job_id=f"job_{digest}",
            variant=variant.name,
            env=task.env,
            task_id=task.task_id,
            repeat=repeat,
            labels=task.labels,
            request={
                "env_name": task.env,
                "task_id": task.task_id,
                **variant.request_fields(),
            },
        )


RETRY_ONLY_JOB_STATUSES = frozenset({"run_failed", "submission_failed"})
