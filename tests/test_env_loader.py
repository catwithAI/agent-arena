from __future__ import annotations

from pathlib import Path

import pytest

from backend.env_loader import EnvLoadError, load_all_envs, load_env


def test_load_all_envs_discovers_bundled_envs():
    envs = load_all_envs(Path("envs"))
    assert "order-desk" in envs
    assert "cpp-optimizer" in envs


def test_order_desk_has_tools_and_tasks():
    envs = load_all_envs(Path("envs"))
    env = envs["order-desk"]
    assert "catalog_search" in env.tools
    assert "place_order" in env.tools
    assert len(env.tasks) >= 1
    assert env.scorer is not None


def test_cpp_optimizer_has_no_tools():
    envs = load_all_envs(Path("envs"))
    env = envs["cpp-optimizer"]
    assert env.tools == {}
    assert env.scorer is not None


def test_missing_meta_yaml_raises(tmp_path: Path):
    env_dir = tmp_path / "broken-env"
    env_dir.mkdir()
    with pytest.raises(EnvLoadError) as exc_info:
        load_env(env_dir)
    assert exc_info.value.env_name == "broken-env"
    assert exc_info.value.stage == "meta"


def test_scorer_missing_score_function_raises(tmp_path: Path):
    env_dir = tmp_path / "broken-env"
    env_dir.mkdir()
    (env_dir / "meta.yaml").write_text("name: broken-env\ntype: coding\n")
    (env_dir / "scorer.py").write_text("def not_score(): pass\n")
    with pytest.raises(EnvLoadError) as exc_info:
        load_env(env_dir)
    assert exc_info.value.stage == "scorer"
