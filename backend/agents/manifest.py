"""Atomic, redacted ``agent-manifest.json`` lifecycle."""

from __future__ import annotations

import copy
import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .launch import RenderedLaunchPlan
from .models import AgentSpec
from .secrets import redact_value


class AgentManifestError(ValueError):
    pass


class AgentManifestStore:
    schema_version = "1"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def prepare(
        self,
        *,
        attempt_id: str,
        spec: AgentSpec,
        plan: RenderedLaunchPlan,
        agent_version: str | None,
        requested_model: str | None,
        provider: str | None,
        components: Mapping[str, str],
        config_summary: Mapping[str, Any] | None = None,
        path_aliases: Mapping[Path, str] | None = None,
        secrets: Sequence[str] = (),
    ) -> dict[str, Any]:
        if self.path.exists():
            raise AgentManifestError(f"manifest already exists: {self.path}")
        aliases = _normalized_aliases(path_aliases or {})
        request_mode = "explicit"
        if requested_model is None:
            request_mode = "agent-default" if spec.model.default_model else "unspecified"
        argv = [_sanitize_text(value, aliases) for value in plan.argv_redacted]
        payload = {
            "schema_version": self.schema_version,
            "attempt_id": attempt_id,
            "agent": {
                "id": spec.id,
                "display_name": spec.display_name,
                "source": spec.source,
                "spec_hash": spec.spec_hash,
                "version": agent_version,
                "transport": spec.transport,
            },
            "model": {
                "requested": requested_model,
                "request_mode": request_mode,
                "effective": None,
                "effective_status": "unknown",
                "provider": provider,
            },
            "launch": {
                "plan_hash": plan.plan_hash,
                "argv_redacted": argv,
                "cwd": spec.launch.cwd if spec.launch else "driver-owned",
                "env_names": list(plan.env_names),
                "env_redacted": dict(plan.env_redacted),
                "prompt_mode": plan.prompt_mode,
            },
            "config_summary": _sanitize_value(config_summary or {}, aliases),
            "capabilities": spec.capabilities.model_dump(mode="json"),
            "components": dict(sorted(components.items())),
            "coverage": {},
            "sessions": [],
            "cleanup": {},
            "degradations": [],
            "outcome": None,
            "status": "prepared",
        }
        payload = redact_value(_sanitize_value(payload, aliases), secrets)
        self._atomic_write(payload)
        return payload

    def finalize(
        self,
        *,
        effective_model: str | None,
        effective_model_known: bool,
        coverage: Mapping[str, Any],
        cleanup: Mapping[str, Any],
        outcome: Mapping[str, Any],
        sessions: Sequence[Mapping[str, Any]] = (),
        degradations: Sequence[str] = (),
        path_aliases: Mapping[Path, str] | None = None,
        secrets: Sequence[str] = (),
    ) -> dict[str, Any]:
        current = self.read()
        if current is None:
            raise AgentManifestError("cannot finalize a missing manifest")
        finalized = copy.deepcopy(current)
        finalized["model"]["effective"] = effective_model if effective_model_known else None
        finalized["model"]["effective_status"] = "confirmed" if effective_model_known else "unknown"
        finalized["coverage"] = dict(coverage)
        finalized["cleanup"] = dict(cleanup)
        finalized["outcome"] = dict(outcome)
        finalized["sessions"] = [dict(item) for item in sessions]
        finalized["degradations"] = list(degradations)
        finalized["status"] = "final"
        finalized = redact_value(
            _sanitize_value(finalized, _normalized_aliases(path_aliases or {})), secrets
        )

        if current.get("status") == "final":
            if current != finalized:
                raise AgentManifestError("manifest was already finalized with different data")
            return current
        if current.get("status") != "prepared":
            raise AgentManifestError(f"invalid manifest status: {current.get('status')!r}")
        self._atomic_write(finalized)
        return finalized

    def read(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentManifestError(f"invalid manifest {self.path}: {exc}") from exc
        if not isinstance(loaded, dict) or loaded.get("schema_version") != self.schema_version:
            raise AgentManifestError("unsupported or malformed agent manifest")
        return loaded

    def _atomic_write(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            os.chmod(temporary_name, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            with contextlib.suppress(OSError):
                os.unlink(temporary_name)
            raise


def _normalized_aliases(path_aliases: Mapping[Path, str]) -> tuple[tuple[str, str], ...]:
    aliases: set[tuple[str, str]] = set()
    for path, alias in path_aliases.items():
        raw = str(Path(path))
        aliases.add((raw, alias))
        aliases.add((str(Path(path).resolve()), alias))
    return tuple(sorted(aliases, key=lambda item: len(item[0]), reverse=True))


def _sanitize_text(value: str, aliases: tuple[tuple[str, str], ...]) -> str:
    sanitized = value
    for path, alias in aliases:
        sanitized = sanitized.replace(path, f"<{alias}>")
    return sanitized


def _sanitize_value(value: Any, aliases: tuple[tuple[str, str], ...]) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value, aliases)
    if isinstance(value, Mapping):
        return {str(key): _sanitize_value(item, aliases) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_sanitize_value(item, aliases) for item in value)
    if isinstance(value, list):
        return [_sanitize_value(item, aliases) for item in value]
    return value
