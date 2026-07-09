"""API/DB-facing models (Pydantic v2).

Only models used for "external or DB serialization" live here. The env
loader's own `Task` dataclass is an internal structure with matching fields,
converted via `to_db_row()` / `from_row()`.

`AttemptModel.external_refs_json` is a string column (raw JSON in the DB); API
responses expose the parsed `external_refs` dict. Never put the env session
token into `external_refs` — `backend.db._validate_external_refs` rejects it.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ---------- Task ----------------------------------------------------------


class TaskModel(BaseModel):
    id: str
    env_name: str
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 600
    source: Literal["file", "adhoc"] = "file"
    created_at: str = ""

    def to_db_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "env_name": self.env_name,
            "prompt": self.prompt,
            "context_json": json.dumps(self.context, ensure_ascii=False),
            "constraints_json": json.dumps(self.constraints, ensure_ascii=False),
            "timeout_seconds": self.timeout_seconds,
            "source": self.source,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "TaskModel":
        return cls(
            id=row["id"],
            env_name=row["env_name"],
            prompt=row["prompt"],
            context=json.loads(row.get("context_json") or "{}"),
            constraints=json.loads(row.get("constraints_json") or "{}"),
            timeout_seconds=row.get("timeout_seconds", 600),
            source=row.get("source", "file"),
            created_at=row.get("created_at", ""),
        )


# ---------- Run -------------------------------------------------------------

RunStatus = Literal["queued", "running", "completed", "failed"]


class RunModel(BaseModel):
    id: str
    task_id: str
    env_name: str
    status: RunStatus = "queued"
    created_at: str = ""
    started_at: str | None = None
    ended_at: str | None = None


# ---------- Attempt ----------------------------------------------------------

AttemptStatus = Literal[
    "queued",
    "running",
    "completed",
    "gave_up",
    "timeout",
    "agent_unavailable",
    "auth_failed",
    "session_create_failed",
    "chat_failed",
    "scoring_failed",
    "cli_not_found",
    "cli_error",
]


class AttemptModel(BaseModel):
    id: str
    run_id: str
    task_id: str
    env_name: str
    agent_name: str = "claude-code"
    # Model requested for this attempt (differs per agent in a multi-agent run).
    model: str | None = None
    status: AttemptStatus = "queued"
    session_id: str
    session_token_hash: str
    external_refs: dict[str, Any] = Field(default_factory=dict)
    event_count: int = 0
    last_event_at: str | None = None
    thinking_count: int = 0
    tool_call_count: int = 0
    token_usage: dict[str, int] = Field(default_factory=dict)
    cost_estimate: float | None = None
    duration_ms: int = 0
    score_total: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    created_at: str = ""
    # Execution context snapshot (adapter fills at launch time).
    execution_locus: str | None = None
    permission_mode: str | None = None
    workspace_root: str | None = None

    @model_validator(mode="after")
    def _no_token_in_external_refs(self) -> "AttemptModel":
        leaks = {"session_token", "session_token_hash"} & set(self.external_refs)
        if leaks:
            raise ValueError(f"external_refs must not contain sensitive fields: {sorted(leaks)}")
        return self

    def to_db_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "env_name": self.env_name,
            "agent_name": self.agent_name,
            "model": self.model,
            "status": self.status,
            "session_id": self.session_id,
            "session_token_hash": self.session_token_hash,
            "external_refs_json": json.dumps(self.external_refs, ensure_ascii=False),
            "event_count": self.event_count,
            "last_event_at": self.last_event_at,
            "thinking_count": self.thinking_count,
            "tool_call_count": self.tool_call_count,
            "token_usage_json": json.dumps(self.token_usage, ensure_ascii=False),
            "cost_estimate": self.cost_estimate,
            "duration_ms": self.duration_ms,
            "score_total": self.score_total,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "created_at": self.created_at,
            "execution_locus": self.execution_locus,
            "permission_mode": self.permission_mode,
            "workspace_root": self.workspace_root,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AttemptModel":
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            env_name=row["env_name"],
            agent_name=row.get("agent_name", "claude-code"),
            model=row.get("model"),
            status=row.get("status", "queued"),
            session_id=row.get("session_id", ""),
            session_token_hash=row.get("session_token_hash", ""),
            external_refs=json.loads(row.get("external_refs_json") or "{}"),
            event_count=row.get("event_count", 0),
            last_event_at=row.get("last_event_at"),
            thinking_count=row.get("thinking_count", 0),
            tool_call_count=row.get("tool_call_count", 0),
            token_usage=json.loads(row.get("token_usage_json") or "{}"),
            cost_estimate=row.get("cost_estimate"),
            duration_ms=row.get("duration_ms", 0),
            score_total=row.get("score_total"),
            error_code=row.get("error_code"),
            error_message=row.get("error_message"),
            started_at=row.get("started_at"),
            ended_at=row.get("ended_at"),
            created_at=row.get("created_at", ""),
            execution_locus=row.get("execution_locus"),
            permission_mode=row.get("permission_mode"),
            workspace_root=row.get("workspace_root"),
        )


# ---------- Score -----------------------------------------------------------


class ScoreModel(BaseModel):
    id: int | None = None
    attempt_id: str
    dimension: str
    value: int
    detail: str = ""
