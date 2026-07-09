from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app


@pytest.fixture
def data_path(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def settings(data_path: Path) -> Settings:
    return Settings(
        lane={
            "data_path": data_path,
            "envs_path": Path("envs"),
            "public_base_url": "http://127.0.0.1:8100",
        }
    )


@pytest.fixture
def test_app(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield app, client


@pytest.fixture
def test_client(test_app):
    return test_app[1]
