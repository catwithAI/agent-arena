"""Validated, content-addressed cache for the official ACP registry v1."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import tempfile
import contextlib
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


DEFAULT_REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA_RE = re.compile(r"^[a-f0-9]{64}$")


class AcpRegistryError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PackageDistribution(_Strict):
    package: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)


class BinaryTarget(_Strict):
    archive: str
    cmd: str = Field(min_length=1)
    sha256: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("archive")
    @classmethod
    def archive_is_https(cls, value: str) -> str:
        if urlparse(value).scheme != "https":
            raise ValueError("binary archive URL must use HTTPS")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_checksum(cls, value: str | None) -> str | None:
        if value is not None and not _SHA_RE.fullmatch(value.lower()):
            raise ValueError("sha256 must contain 64 hexadecimal characters")
        return value.lower() if value else None


class Distribution(_Strict):
    binary: dict[str, BinaryTarget] | None = None
    npx: PackageDistribution | None = None
    uvx: PackageDistribution | None = None

    @model_validator(mode="after")
    def at_least_one_distribution(self) -> "Distribution":
        if self.binary is None and self.npx is None and self.uvx is None:
            raise ValueError("distribution must contain binary, npx, or uvx")
        return self


class RegistryAgent(_Strict):
    id: str
    name: str = Field(min_length=1)
    version: str
    description: str = Field(min_length=1)
    distribution: Distribution
    repository: str | None = None
    website: str | None = None
    authors: tuple[str, ...] = ()
    license: str | None = None
    icon: str | None = None

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not re.fullmatch(r"^[a-z][a-z0-9-]*$", value):
            raise ValueError("invalid ACP registry agent id")
        return value

    @field_validator("version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        if not _VERSION_RE.fullmatch(value):
            raise ValueError("invalid ACP registry semantic version")
        return value


class RegistryDocument(_Strict):
    version: str
    agents: tuple[RegistryAgent, ...]
    # The live v1 CDN began emitting an empty extensions array before the
    # published registry schema documented its item shape. Accept the empty
    # compatibility field, but fail closed if entries appear.
    extensions: tuple[dict[str, Any], ...] = ()

    @field_validator("version")
    @classmethod
    def schema_v1(cls, value: str) -> str:
        if not value.startswith("1.") or not _VERSION_RE.fullmatch(value):
            raise ValueError("only ACP registry schema v1 is supported")
        return value

    @field_validator("extensions")
    @classmethod
    def extensions_are_not_yet_supported(
        cls, value: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        if value:
            raise ValueError("non-empty ACP registry extensions are not supported")
        return value


class ResolvedAcpAgent(_Strict):
    stable_id: str
    registry_url: str
    registry_version: str
    registry_sha256: str
    metadata: RegistryAgent
    platform_target: str

    @property
    def descriptor(self) -> dict[str, Any]:
        return {
            "id": self.stable_id,
            "display_name": self.metadata.name,
            "transport": "acp",
            "version": self.metadata.version,
            "registry": {
                "url": self.registry_url,
                "schema_version": self.registry_version,
                "sha256": self.registry_sha256,
            },
            "distribution": self.metadata.distribution.model_dump(mode="json", exclude_none=True),
            "metadata": self.metadata.model_dump(
                mode="json", exclude={"distribution"}, exclude_none=True
            ),
            "availability": {
                "status": "unknown",
                "reason": "ACP distributions must be preinstalled by an administrator",
            },
            "data_boundary": "local ACP subprocess; agent-specific network behavior applies",
        }


FetchBytes = Callable[[str], Awaitable[bytes]]


class AcpRegistryResolver:
    """Resolve exact ``acp:<id>@<version>`` identifiers without installing."""

    def __init__(self, cache_dir: Path, *, fetch: FetchBytes | None = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.fetch = fetch or self._fetch_https

    async def resolve(
        self,
        stable_id: str,
        *,
        registry_url: str = DEFAULT_REGISTRY_URL,
        expected_sha256: str | None = None,
        offline: bool = False,
    ) -> ResolvedAcpAgent:
        agent_id, version = _parse_stable_id(stable_id)
        _require_https(registry_url)
        if expected_sha256 is not None:
            expected_sha256 = expected_sha256.removeprefix("sha256:").lower()
            if not _SHA_RE.fullmatch(expected_sha256):
                raise AcpRegistryError("expected registry checksum is invalid")

        ref_path = self._reference_path(registry_url, agent_id, version)
        if offline:
            raw, digest = self._read_cached(ref_path, expected_sha256)
        else:
            try:
                raw = await self.fetch(registry_url)
                digest = hashlib.sha256(raw).hexdigest()
                if expected_sha256 is not None and digest != expected_sha256:
                    raise AcpRegistryError("registry checksum mismatch")
                document = _validate_document(raw)
                agent = _find_agent(document, agent_id, version)
                self._store(raw, digest=digest, ref_path=ref_path)
                return _resolved(stable_id, registry_url, digest, document, agent)
            except AcpRegistryError:
                raise
            except Exception:
                # Network failure may use an already pinned raw document.  A
                # schema/checksum failure never silently falls back.
                raw, digest = self._read_cached(ref_path, expected_sha256)

        document = _validate_document(raw)
        agent = _find_agent(document, agent_id, version)
        return _resolved(stable_id, registry_url, digest, document, agent)

    def _store(self, raw: bytes, *, digest: str, ref_path: Path) -> None:
        blob_path = self.cache_dir / "blobs" / f"{digest}.json"
        _atomic_write(blob_path, raw)
        reference = json.dumps({"sha256": digest}, separators=(",", ":")).encode() + b"\n"
        _atomic_write(ref_path, reference)

    def _read_cached(self, ref_path: Path, expected: str | None) -> tuple[bytes, str]:
        try:
            reference = json.loads(ref_path.read_text(encoding="utf-8"))
            digest = reference["sha256"]
        except (OSError, KeyError, json.JSONDecodeError, TypeError) as exc:
            raise AcpRegistryError("ACP registry is unavailable and no valid cache exists") from exc
        if not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
            raise AcpRegistryError("ACP registry cache reference is corrupted")
        if expected is not None and digest != expected:
            raise AcpRegistryError("cached registry checksum does not match requested pin")
        blob_path = self.cache_dir / "blobs" / f"{digest}.json"
        try:
            raw = blob_path.read_bytes()
        except OSError as exc:
            raise AcpRegistryError("ACP registry cache blob is missing") from exc
        if hashlib.sha256(raw).hexdigest() != digest:
            raise AcpRegistryError("ACP registry cache blob checksum mismatch")
        return raw, digest

    def _reference_path(self, url: str, agent_id: str, version: str) -> Path:
        source = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self.cache_dir / "refs" / source / agent_id / f"{version}.json"

    @staticmethod
    async def _fetch_https(url: str) -> bytes:
        _require_https(url)
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            if urlparse(str(response.url)).scheme != "https":
                raise AcpRegistryError("registry redirected to a non-HTTPS URL")
            return response.content


def _parse_stable_id(stable_id: str) -> tuple[str, str]:
    match = re.fullmatch(r"acp:([a-z][a-z0-9-]*)@(.+)", stable_id)
    if not match or not _VERSION_RE.fullmatch(match.group(2)):
        raise AcpRegistryError("ACP id must be acp:<id>@<semantic-version>")
    return match.group(1), match.group(2)


def _require_https(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise AcpRegistryError("ACP registry URL must use HTTPS")


def _validate_document(raw: bytes) -> RegistryDocument:
    try:
        loaded = json.loads(raw)
        document = RegistryDocument.model_validate(loaded)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise AcpRegistryError(f"invalid ACP registry document: {exc}") from exc
    identities = [(agent.id, agent.version) for agent in document.agents]
    if len(identities) != len(set(identities)):
        raise AcpRegistryError("ACP registry contains duplicate id/version entries")
    return document


def _find_agent(document: RegistryDocument, agent_id: str, version: str) -> RegistryAgent:
    for agent in document.agents:
        if agent.id == agent_id and agent.version == version:
            return agent
    raise AcpRegistryError(f"ACP registry has no {agent_id!r} version {version!r}")


def _resolved(
    stable_id: str,
    registry_url: str,
    digest: str,
    document: RegistryDocument,
    agent: RegistryAgent,
) -> ResolvedAcpAgent:
    return ResolvedAcpAgent(
        stable_id=stable_id,
        registry_url=registry_url,
        registry_version=document.version,
        registry_sha256=f"sha256:{digest}",
        metadata=agent,
        platform_target=_platform_target(),
    )


def _platform_target() -> str:
    os_name = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(
        platform.system(), platform.system().lower()
    )
    machine = platform.machine().lower()
    arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
    return f"{os_name}-{arch}"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
