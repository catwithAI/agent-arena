from __future__ import annotations

from pathlib import Path

from backend.config import Settings, load_settings


def test_default_settings_have_loopback_paths():
    s = Settings()
    assert isinstance(s.lane.data_path, Path)
    assert s.lane.public_base_url == "http://127.0.0.1:8100"


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LANE_DATA_PATH", str(tmp_path / "custom"))
    monkeypatch.setenv("LANE_PUBLIC_BASE_URL", "http://127.0.0.1:9999")
    settings = load_settings(config_path=tmp_path / "nonexistent.yaml")
    assert str(settings.lane.data_path) == str(tmp_path / "custom")
    assert settings.lane.public_base_url == "http://127.0.0.1:9999"


def test_model_providers_never_leak_api_key(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-should-not-appear-in-logs")
    config_path = tmp_path / "arena.yaml"
    config_path.write_text(
        "model_providers:\n"
        "  myprov:\n"
        "    kind: anthropic\n"
        "    base_url: http://127.0.0.1:1234\n"
        "    api_key_env: MY_PROVIDER_KEY\n"
    )
    import logging

    with caplog.at_level(logging.INFO):
        settings = load_settings(config_path=config_path)
    assert "sk-should-not-appear-in-logs" not in caplog.text
    assert settings.model_providers["myprov"].kind == "anthropic"
