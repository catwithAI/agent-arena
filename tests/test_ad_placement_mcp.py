from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[1] / "envs/ad-placement/mcp_server.py"
    spec = importlib.util.spec_from_file_location("ad_placement_mcp_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workspace_status_uses_declared_agent_workspace(monkeypatch, tmp_path: Path):
    project = tmp_path / "project"
    workspace = tmp_path / "custom-data" / "attempts" / "att-1" / "skill_workspace"
    project.mkdir()
    workspace.mkdir(parents=True)
    (project / "solution.cpp").write_text("wrong", encoding="utf-8")
    (workspace / "solution.cpp").write_text("correct", encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setenv("LANE_WORKSPACE", str(workspace))
    module = _load_module()

    result = module.workspace_status()

    assert result["cwd"] == str(workspace.resolve())
    assert result["files"]["solution.cpp"] == {"exists": True, "size": 7}
