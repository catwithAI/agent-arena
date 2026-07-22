"""Read-only, cached availability probes for registered agents."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .models import AgentSpec


AvailabilityStatus = Literal[
    "available",
    "not_installed",
    "version_unsupported",
    "missing_auth",
    "missing_dependency",
    "misconfigured",
    "unknown",
]


@dataclass(frozen=True)
class AvailabilityResult:
    status: AvailabilityStatus
    version: str | None = None
    reason: str | None = None
    cli_path: str | None = None

    @property
    def available(self) -> bool:
        return self.status == "available"

    def as_dict(self) -> dict[str, str | None]:
        return {"status": self.status, "version": self.version, "reason": self.reason}


@dataclass
class _CacheEntry:
    expires_at: float
    result: AvailabilityResult


class AvailabilityService:
    """Probe executables without importing plugins or contacting model APIs."""

    def __init__(self, *, ttl_seconds: float = 30.0) -> None:
        self.ttl_seconds = max(0.0, ttl_seconds)
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def probe(self, spec: AgentSpec, *, refresh: bool = False) -> AvailabilityResult:
        key = self._cache_key(spec)
        now = time.monotonic()
        cached = self._cache.get(key)
        if not refresh and cached and cached.expires_at > now:
            return cached.result

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._cache.get(key)
            if not refresh and cached and cached.expires_at > now:
                return cached.result
            result = await self._probe_uncached(spec)
            self._cache[key] = _CacheEntry(now + self.ttl_seconds, result)
            return result

    async def probe_all(self, specs: tuple[AgentSpec, ...]) -> dict[str, AvailabilityResult]:
        results = await asyncio.gather(*(self.probe(spec) for spec in specs))
        return {spec.id: result for spec, result in zip(specs, results, strict=True)}

    def clear(self) -> None:
        self._cache.clear()

    def _cache_key(self, spec: AgentSpec) -> str:
        auth_presence = tuple((ref.env_var, ref.env_var in os.environ) for ref in spec.auth)
        path_digest = hashlib.sha256(os.environ.get("PATH", "").encode()).hexdigest()
        return repr((spec.spec_hash, path_digest, auth_presence))

    async def _probe_uncached(self, spec: AgentSpec) -> AvailabilityResult:
        probe = spec.availability
        executable_path = _resolve_executable(probe.executable)
        if probe.executable and executable_path is None:
            return AvailabilityResult(
                status="not_installed",
                reason=f"{probe.executable!r} not found in PATH",
            )

        missing_dependencies = [
            dependency
            for dependency in probe.system_dependencies
            if _resolve_executable(dependency) is None
        ]
        if missing_dependencies:
            return AvailabilityResult(
                status="missing_dependency",
                reason="missing system dependencies: " + ", ".join(missing_dependencies),
                cli_path=executable_path,
            )

        missing_auth = [
            ref.env_var for ref in spec.auth if ref.required and ref.env_var not in os.environ
        ]
        if missing_auth:
            return AvailabilityResult(
                status="missing_auth",
                reason="missing required environment variables: " + ", ".join(missing_auth),
                cli_path=executable_path,
            )

        if not probe.version_command:
            if probe.configured_available:
                return AvailabilityResult(
                    status="available",
                    reason="remote endpoint is configured; no network probe was sent",
                )
            if executable_path is None:
                return AvailabilityResult(
                    status="unknown",
                    reason="no executable or read-only availability probe declared",
                )
            return AvailabilityResult(status="available", cli_path=executable_path)

        argv = [
            executable_path if token == "{executable}" else token for token in probe.version_command
        ]
        if not argv or argv[0] is None:
            return AvailabilityResult(status="misconfigured", reason="invalid version command")
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=probe.timeout_seconds
            )
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return AvailabilityResult(
                status="unknown",
                reason=f"version probe timed out after {probe.timeout_seconds:g}s",
                cli_path=executable_path,
            )
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except (FileNotFoundError, OSError) as exc:
            return AvailabilityResult(
                status="misconfigured",
                reason=f"version probe could not start: {type(exc).__name__}",
                cli_path=executable_path,
            )

        assert process is not None
        output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            probe_failures = {
                20: "not_installed",
                21: "version_unsupported",
                22: "misconfigured",
            }
            status = probe_failures.get(process.returncode, "misconfigured")
            reason = output[:1000] or f"version probe exited with code {process.returncode}"
            return AvailabilityResult(
                status=status,
                reason=reason,
                cli_path=executable_path,
            )
        version = _extract_version(output, probe.version_scheme, probe.version_regex)
        if version is None:
            return AvailabilityResult(
                status="unknown",
                reason="version probe output could not be parsed",
                cli_path=executable_path,
            )
        if probe.version_constraint and not _version_matches(
            version, probe.version_constraint, probe.version_scheme
        ):
            return AvailabilityResult(
                status="version_unsupported",
                version=version,
                reason=f"version {version} does not satisfy {probe.version_constraint}",
                cli_path=executable_path,
            )
        return AvailabilityResult(status="available", version=version, cli_path=executable_path)


def _resolve_executable(executable: str | None) -> str | None:
    if not executable:
        return None
    resolved = shutil.which(executable)
    if resolved:
        return resolved
    path = Path(executable)
    return str(path.resolve()) if path.is_file() and os.access(path, os.X_OK) else None


_GENERIC_VERSION = re.compile(r"(?<!\d)[vV]?(\d+(?:\.\d+){1,3}(?:[-+._][0-9A-Za-z.-]+)?)")


def _extract_version(output: str, scheme: str, version_regex: str | None) -> str | None:
    pattern = re.compile(version_regex) if version_regex else _GENERIC_VERSION
    match = pattern.search(output[:65536])
    if not match:
        return None
    value = match.group(1) if match.lastindex else match.group(0)
    value = value.removeprefix("v").removeprefix("V")
    if scheme == "regex":
        return value
    try:
        return str(Version(value))
    except InvalidVersion:
        return None


def _version_matches(version: str, constraint: str, scheme: str) -> bool:
    if scheme == "regex":
        try:
            return re.fullmatch(constraint, version) is not None
        except re.error:
            return False
    normalized = _normalize_semver_constraint(constraint) if scheme == "semver" else constraint
    try:
        return Version(version) in SpecifierSet(normalized)
    except (InvalidSpecifier, InvalidVersion):
        return False


def _normalize_semver_constraint(constraint: str) -> str:
    """Translate the common npm caret/tilde forms into PEP 440 bounds."""
    value = constraint.strip()
    match = re.fullmatch(r"([~^])(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        return value
    operator, major_raw, minor_raw, patch_raw = match.groups()
    major, minor, patch = int(major_raw), int(minor_raw), int(patch_raw)
    lower = f">={major}.{minor}.{patch}"
    if operator == "~":
        upper = f"<{major}.{minor + 1}.0"
    elif major > 0:
        upper = f"<{major + 1}.0.0"
    elif minor > 0:
        upper = f"<0.{minor + 1}.0"
    else:
        upper = f"<0.0.{patch + 1}"
    return f"{lower},{upper}"
