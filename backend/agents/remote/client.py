"""Bounded HTTP session contract for remote agents.

This is an Agent Arena protocol boundary, not an assertion that third-party
SDKs share an API. Vendor plugins translate their service into this contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

import httpx


TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


class RemoteTransportError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class RemoteArtifact:
    path: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class RemoteRunResult:
    session_id: str | None
    status: str
    final_text: str | None = None
    events: tuple[Mapping[str, Any], ...] = ()
    usage: Mapping[str, int] = field(default_factory=dict)
    artifacts: tuple[Path, ...] = ()
    artifact_failures: tuple[str, ...] = ()
    cancellation: str = "not_requested"
    server_metadata: Mapping[str, Any] = field(default_factory=dict)


class RemoteTransportClient:
    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        poll_interval_seconds: float = 0.25,
        max_response_bytes: int = 4 * 1024 * 1024,
        max_artifact_bytes: int = 100 * 1024 * 1024,
        allow_insecure: bool = False,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in ({"https", "http"} if allow_insecure else {"https"}):
            raise ValueError("remote Agent endpoint must use HTTPS")
        if not parsed.netloc:
            raise ValueError("remote Agent endpoint must be absolute")
        self.endpoint = endpoint.rstrip("/") + "/"
        self.origin = (parsed.scheme, parsed.netloc)
        self._owned_client = client is None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = client or httpx.AsyncClient(headers=headers, timeout=30.0)
        self.poll_interval_seconds = max(0.0, poll_interval_seconds)
        self.max_response_bytes = max_response_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self._session_id: str | None = None

    async def run(
        self,
        request: Mapping[str, Any],
        *,
        workspace: Path,
        timeout_seconds: float | None,
    ) -> RemoteRunResult:
        operation = self._run(request, workspace=Path(workspace).resolve())
        try:
            if timeout_seconds is None:
                return await operation
            return await asyncio.wait_for(operation, timeout=max(0.001, timeout_seconds))
        except asyncio.TimeoutError as exc:
            cancellation = await self.cancel()
            raise RemoteTransportError(
                "agent_timeout",
                f"remote Agent timed out; cancellation={cancellation}",
                details={"cancellation": cancellation},
            ) from exc
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except httpx.HTTPError as exc:
            raise RemoteTransportError(
                "agent_network_error", f"remote Agent HTTP failure: {type(exc).__name__}"
            ) from exc
        finally:
            if self._owned_client:
                await self.client.aclose()

    async def _run(self, request: Mapping[str, Any], *, workspace: Path) -> RemoteRunResult:
        response = await self.client.post(self._url("v1/sessions"), json=dict(request))
        payload = self._json_response(response)
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RemoteTransportError("agent_output_parse_degraded", "remote session omitted sessionId")
        self._session_id = session_id
        events: list[Mapping[str, Any]] = []
        snapshot = payload

        stream_url = payload.get("streamUrl")
        if isinstance(stream_url, str):
            snapshot = await self._consume_stream(stream_url, events=events, initial=payload)

        while snapshot.get("status") not in TERMINAL_STATES:
            poll_url = snapshot.get("pollUrl") or f"v1/sessions/{session_id}"
            response = await self.client.get(self._url(str(poll_url)))
            snapshot = self._json_response(response)
            updates = snapshot.get("events")
            if isinstance(updates, list):
                events.extend(item for item in updates if isinstance(item, Mapping))
            if snapshot.get("status") not in TERMINAL_STATES and self.poll_interval_seconds:
                await asyncio.sleep(self.poll_interval_seconds)

        status = snapshot.get("status")
        if status not in TERMINAL_STATES:
            raise RemoteTransportError("agent_output_parse_degraded", "invalid remote terminal state")
        artifact_paths, artifact_failures = await self._sync_artifacts(
            snapshot.get("artifacts"), workspace=workspace
        )
        usage = {
            key: value
            for key, value in (snapshot.get("usage") or {}).items()
            if isinstance(key, str) and isinstance(value, int) and value >= 0
        }
        final_text = snapshot.get("finalText")
        return RemoteRunResult(
            session_id=session_id,
            status=status,
            final_text=final_text if isinstance(final_text, str) else None,
            events=tuple(events),
            usage=usage,
            artifacts=artifact_paths,
            artifact_failures=artifact_failures,
            server_metadata=(
                snapshot.get("metadata") if isinstance(snapshot.get("metadata"), Mapping) else {}
            ),
        )

    async def cancel(self) -> str:
        if not self._session_id:
            return "not_created"
        try:
            response = await self.client.delete(self._url(f"v1/sessions/{self._session_id}"))
            payload = self._json_response(response)
        except Exception:
            return "cancel_requested_remote_unknown"
        return (
            "cancel_confirmed"
            if payload.get("confirmed") is True
            else "cancel_requested_remote_unknown"
        )

    async def _consume_stream(
        self,
        stream_url: str,
        *,
        events: list[Mapping[str, Any]],
        initial: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        latest: Mapping[str, Any] = initial
        total = 0
        async with self.client.stream("GET", self._url(stream_url)) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                total += len(line.encode("utf-8")) + 1
                if total > self.max_response_bytes:
                    raise RemoteTransportError(
                        "agent_output_parse_degraded", "remote update stream exceeded size limit"
                    )
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RemoteTransportError(
                        "agent_output_parse_degraded", "remote update stream contains invalid JSON"
                    ) from exc
                if not isinstance(item, Mapping):
                    continue
                if item.get("type") == "snapshot" and isinstance(item.get("data"), Mapping):
                    latest = item["data"]
                else:
                    events.append(item)
        return latest

    async def _sync_artifacts(
        self, raw: Any, *, workspace: Path
    ) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        if not isinstance(raw, list):
            return (), ()
        downloaded: list[Path] = []
        failures: list[str] = []
        workspace.mkdir(parents=True, exist_ok=True)
        for item in raw:
            try:
                artifact = RemoteArtifact(**item)
                destination = (workspace / artifact.path).resolve()
                if not destination.is_relative_to(workspace) or artifact.path.startswith("/"):
                    raise ValueError("artifact path escapes workspace")
                if artifact.size < 0 or artifact.size > self.max_artifact_bytes:
                    raise ValueError("artifact declared size exceeds limit")
                response = await self.client.get(self._url(artifact.url))
                response.raise_for_status()
                content = response.content
                if len(content) != artifact.size or len(content) > self.max_artifact_bytes:
                    raise ValueError("artifact size mismatch")
                digest = hashlib.sha256(content).hexdigest()
                if digest != artifact.sha256.removeprefix("sha256:").lower():
                    raise ValueError("artifact checksum mismatch")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                downloaded.append(destination)
            except (TypeError, ValueError, KeyError, httpx.HTTPError, RemoteTransportError) as exc:
                label = item.get("path", "unknown") if isinstance(item, Mapping) else "unknown"
                failures.append(f"{label}: {exc}")
        return tuple(downloaded), tuple(failures)

    def _json_response(self, response: httpx.Response) -> Mapping[str, Any]:
        response.raise_for_status()
        if len(response.content) > self.max_response_bytes:
            raise RemoteTransportError(
                "agent_output_parse_degraded", "remote response exceeded size limit"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RemoteTransportError(
                "agent_output_parse_degraded", "remote response is not JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise RemoteTransportError("agent_output_parse_degraded", "remote response is not an object")
        return payload

    def _url(self, value: str) -> str:
        resolved = urljoin(self.endpoint, value)
        parsed = urlparse(resolved)
        if (parsed.scheme, parsed.netloc) != self.origin:
            raise RemoteTransportError(
                "agent_internal_error", "remote URL crossed the configured service origin"
            )
        return resolved
