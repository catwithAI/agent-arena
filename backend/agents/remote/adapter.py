"""AgentAdapter bridge for configured vendor-neutral remote services."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import rfc8785

from ...adapters.base import AdapterCapabilities, AdapterResult, AdapterRunInput, build_security_meta
from ...conversation.plan import effective_conversation
from ...conversation.turns import render_turn_prompt
from ..launch import RenderedLaunchPlan
from ..manifest import AgentManifestStore
from ..prompt import render_task_prompt
from ..transports.adapter import _stage_uploaded_files
from .client import RemoteRunResult, RemoteTransportClient, RemoteTransportError


class RemoteTransportAdapter:
    def __init__(
        self,
        *,
        settings: Any,
        spec: Any,
        model: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.spec = spec
        self.model = model
        self.config = settings.agents.remote[spec.id]
        self.http_client = http_client
        self.capabilities = AdapterCapabilities(
            execution_locus="remote-host",
            network_required="public_internet",
            interaction_answer=False,
        )

    async def run(self, task: AdapterRunInput, env: Any, data_path: Path) -> AdapterResult:
        started = time.monotonic()
        attempt_dir = Path(data_path) / "attempts" / task.attempt_id
        workspace = attempt_dir / "skill_workspace"
        control = attempt_dir / ".agent-control"
        workspace.mkdir(parents=True, exist_ok=True)
        control.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest = AgentManifestStore(control / "agent-manifest.json")
        try:
            if task.mcp_servers:
                raise ValueError("remote transport does not support task-local MCP servers")
            _stage_uploaded_files(task, workspace, env=env, data_path=Path(data_path))
            request, request_summary = self._request(task, workspace)
            plan = self._logical_plan(workspace, request_summary)
            manifest.prepare(
                attempt_id=task.attempt_id,
                spec=self.spec,
                plan=plan,
                agent_version=None,
                requested_model=self.model,
                provider=None,
                components={
                    "transport": "arena-remote-http@1",
                    "driver": "remote-session@1",
                    "parser": "remote-snapshot@1",
                },
                config_summary={
                    **request_summary,
                    "endpoint": str(self.config.endpoint),
                    "data_residency": self.config.data_residency,
                    "uploads_source_files": self.config.upload_files,
                    "cancellation_semantics": self.config.cancellation_semantics,
                },
                path_aliases={workspace: "skill_workspace", control: "attempt_control"},
            )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code=(
                    "agent_remote_upload_not_allowed"
                    if "upload" in str(exc).lower()
                    else "agent_launch_plan_invalid"
                ),
                error_message=str(exc),
                security_meta=self._security_meta(),
            )

        api_key = os.environ.get(self.config.api_key_env) if self.config.api_key_env else None
        client = RemoteTransportClient(
            str(self.config.endpoint),
            client=self.http_client,
            api_key=api_key,
            poll_interval_seconds=self.config.poll_interval_seconds,
            max_artifact_bytes=self.config.max_artifact_bytes,
        )
        try:
            result = await client.run(
                request,
                workspace=workspace,
                timeout_seconds=task.timeout_seconds,
            )
        except asyncio.CancelledError:
            manifest.finalize(
                effective_model=None,
                effective_model_known=False,
                coverage={},
                cleanup={"status": "cancel_requested_remote_unknown"},
                outcome={"status": "cancelled", "error_code": "agent_cancelled"},
                degradations=("local task cancelled; remote termination was not observed",),
            )
            raise
        except RemoteTransportError as exc:
            cancellation = exc.details.get("cancellation", "not_requested")
            status = "timeout" if exc.code == "agent_timeout" else "cli_error"
            manifest.finalize(
                effective_model=None,
                effective_model_known=False,
                coverage={},
                cleanup={"status": cancellation},
                outcome={"status": status, "error_code": exc.code},
                degradations=(str(exc),),
            )
            return AdapterResult(
                attempt_id=task.attempt_id,
                status=status,
                external_refs={
                    "agent_manifest": str(manifest.path),
                    "remote_cancellation": cancellation,
                },
                error_code=exc.code,
                error_message=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
                security_meta=self._security_meta(),
            )

        return self._complete(
            task=task,
            attempt_dir=attempt_dir,
            manifest=manifest,
            plan=plan,
            result=result,
            started=started,
        )

    def _request(self, task: AdapterRunInput, workspace: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        conversation = effective_conversation(task)
        if conversation.interaction_turns:
            raise ValueError("remote transport does not support interaction answer turns")
        base = render_task_prompt(task)
        turns = [
            {
                "turnId": turn.turn_id,
                "text": render_turn_prompt(task, turn, base_prompt=base.text),
            }
            for turn in conversation.send_message_turns
        ]
        files: list[dict[str, Any]] = []
        total = 0
        uploaded = task.task_context.get("uploaded_files") or []
        if uploaded and not self.config.upload_files:
            raise ValueError("remote source-file upload is disabled for this Agent")
        for item in uploaded:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError("invalid remote upload filename")
            content = (workspace / name).read_bytes()
            total += len(content)
            if total > self.config.max_upload_bytes:
                raise ValueError("remote upload exceeds configured byte limit")
            files.append(
                {
                    "path": name,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "contentBase64": base64.b64encode(content).decode("ascii"),
                }
            )
        request = {
            "protocolVersion": "arena-remote-v1",
            "attemptId": task.attempt_id,
            "turns": turns,
            "model": self.model,
            "files": files,
        }
        summary = {
            "prompt_hashes": [
                f"sha256:{hashlib.sha256(turn['text'].encode()).hexdigest()}" for turn in turns
            ],
            "file_count": len(files),
            "file_bytes": total,
            "file_hashes": [item["sha256"] for item in files],
        }
        return request, summary

    def _logical_plan(self, workspace: Path, summary: dict[str, Any]) -> RenderedLaunchPlan:
        logical = {
            "agent": self.spec.id,
            "spec_hash": self.spec.spec_hash,
            "endpoint": str(self.config.endpoint),
            "request": summary,
            "model": self.model,
        }
        digest = hashlib.sha256(rfc8785.dumps(logical)).hexdigest()
        return RenderedLaunchPlan(
            argv=("remote-session", str(self.config.endpoint)),
            cwd=workspace,
            env={},
            env_redacted={},
            stdin_data=None,
            prompt_mode="driver-owned",
            plan_hash=f"sha256:{digest}",
        )

    def _complete(
        self,
        *,
        task: AdapterRunInput,
        attempt_dir: Path,
        manifest: AgentManifestStore,
        plan: RenderedLaunchPlan,
        result: RemoteRunResult,
        started: float,
    ) -> AdapterResult:
        events_path = attempt_dir / "events.jsonl"
        with events_path.open("w", encoding="utf-8") as file:
            for event in result.events:
                file.write(json.dumps(dict(event), ensure_ascii=False, default=str) + "\n")
        if result.final_text is not None:
            (attempt_dir / "agent_final.txt").write_text(result.final_text, encoding="utf-8")
        status = {
            "completed": "completed",
            "failed": "cli_error",
            "cancelled": "cancelled",
        }[result.status]
        error_code = {
            "completed": None,
            "failed": "agent_nonzero_exit",
            "cancelled": "agent_cancelled",
        }[result.status]
        artifact_status = (
            "partial" if result.artifact_failures else "verified" if result.artifacts else "unknown"
        )
        coverage = {
            "trajectory": "verified" if result.events else "unknown",
            "token_usage": "verified" if result.usage else "unknown",
            "artifacts": artifact_status,
            "wire": "unsupported",
        }
        effective_model = result.server_metadata.get("model")
        effective_known = isinstance(effective_model, str) and bool(effective_model)
        manifest.finalize(
            effective_model=effective_model if effective_known else None,
            effective_model_known=effective_known,
            coverage=coverage,
            cleanup={"status": "remote_terminal_observed"},
            outcome={"status": status, "error_code": error_code, "remote_status": result.status},
            sessions=({"session_id": result.session_id},) if result.session_id else (),
            degradations=result.artifact_failures,
        )
        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                "agent_manifest": str(manifest.path),
                "spec_hash": self.spec.spec_hash,
                "plan_hash": plan.plan_hash,
                "remote_session_id": result.session_id,
                "artifact_sync": {
                    "status": artifact_status,
                    "downloaded": [path.name for path in result.artifacts],
                    "failures": list(result.artifact_failures),
                },
            },
            error_code=error_code,
            error_message=(f"remote session ended as {result.status}" if error_code else None),
            events_count=len(result.events),
            token_usage=dict(result.usage),
            duration_ms=int((time.monotonic() - started) * 1000),
            security_meta=self._security_meta(),
        )

    def _security_meta(self) -> dict[str, Any]:
        return build_security_meta(
            execution_locus="remote-host",
            permission_mode="remote-service-defined",
            workspace_root=None,
        )


def build_remote_adapter(
    *, settings: Any, spec: Any, model: str | None = None
) -> RemoteTransportAdapter:
    return RemoteTransportAdapter(settings=settings, spec=spec, model=model)
