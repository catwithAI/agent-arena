from __future__ import annotations

import hashlib
import json

import pytest

from backend.agents.acp.registry import AcpRegistryError, AcpRegistryResolver


def _registry(*, version: str = "2.3.4") -> bytes:
    return json.dumps(
        {
            "version": "1.0.0",
            "extensions": [],
            "agents": [
                {
                    "id": "fake-agent",
                    "name": "Fake Agent",
                    "version": version,
                    "description": "fixture",
                    "repository": "https://example.test/repo",
                    "license": "MIT",
                    "distribution": {
                        "npx": {"package": "@example/fake-agent@2.3.4", "args": ["--acp"]}
                    },
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


@pytest.mark.asyncio
async def test_resolves_exact_version_and_caches_raw_metadata(tmp_path):
    raw = _registry()

    async def fetch(url: str) -> bytes:
        assert url.startswith("https://")
        return raw

    resolver = AcpRegistryResolver(tmp_path, fetch=fetch)
    resolved = await resolver.resolve("acp:fake-agent@2.3.4")

    assert resolved.stable_id == "acp:fake-agent@2.3.4"
    assert resolved.registry_sha256 == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert resolved.descriptor["transport"] == "acp"
    assert resolved.descriptor["availability"]["status"] == "unknown"
    assert list((tmp_path / "blobs").glob("*.json"))


@pytest.mark.asyncio
async def test_offline_resolve_uses_content_addressed_cache(tmp_path):
    raw = _registry()
    resolver = AcpRegistryResolver(tmp_path, fetch=lambda _: _async_bytes(raw))
    first = await resolver.resolve("acp:fake-agent@2.3.4")

    offline = AcpRegistryResolver(tmp_path, fetch=_fail_fetch)
    second = await offline.resolve(
        "acp:fake-agent@2.3.4",
        expected_sha256=first.registry_sha256,
        offline=True,
    )
    assert second.registry_sha256 == first.registry_sha256


@pytest.mark.asyncio
async def test_bad_checksum_and_missing_version_are_rejected(tmp_path):
    resolver = AcpRegistryResolver(tmp_path, fetch=lambda _: _async_bytes(_registry()))
    with pytest.raises(AcpRegistryError, match="checksum mismatch"):
        await resolver.resolve("acp:fake-agent@2.3.4", expected_sha256="0" * 64)
    with pytest.raises(AcpRegistryError, match="has no"):
        await resolver.resolve("acp:fake-agent@9.9.9")


@pytest.mark.asyncio
async def test_offline_missing_and_corrupt_cache_fail_closed(tmp_path):
    resolver = AcpRegistryResolver(tmp_path, fetch=_fail_fetch)
    with pytest.raises(AcpRegistryError, match="no valid cache"):
        await resolver.resolve("acp:fake-agent@2.3.4", offline=True)

    online = AcpRegistryResolver(tmp_path, fetch=lambda _: _async_bytes(_registry()))
    resolved = await online.resolve("acp:fake-agent@2.3.4")
    blob = next((tmp_path / "blobs").glob("*.json"))
    blob.write_bytes(b"corrupt")
    with pytest.raises(AcpRegistryError, match="checksum mismatch"):
        await resolver.resolve(
            "acp:fake-agent@2.3.4",
            expected_sha256=resolved.registry_sha256,
            offline=True,
        )


@pytest.mark.asyncio
async def test_registry_requires_https_and_valid_schema(tmp_path):
    resolver = AcpRegistryResolver(tmp_path, fetch=lambda _: _async_bytes(b"{}"))
    with pytest.raises(AcpRegistryError, match="HTTPS"):
        await resolver.resolve("acp:fake-agent@2.3.4", registry_url="http://example.test/x")
    with pytest.raises(AcpRegistryError, match="invalid ACP registry"):
        await resolver.resolve("acp:fake-agent@2.3.4")


@pytest.mark.asyncio
async def test_unknown_nonempty_registry_extensions_fail_closed(tmp_path):
    document = json.loads(_registry())
    document["extensions"] = [{"id": "future-extension"}]
    raw = json.dumps(document).encode()
    resolver = AcpRegistryResolver(tmp_path, fetch=lambda _: _async_bytes(raw))

    with pytest.raises(AcpRegistryError, match="extensions are not supported"):
        await resolver.resolve("acp:fake-agent@2.3.4")


async def _async_bytes(value: bytes) -> bytes:
    return value


async def _fail_fetch(_: str) -> bytes:
    raise OSError("offline")
