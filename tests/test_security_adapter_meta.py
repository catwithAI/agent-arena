"""adapter 落 security_meta + 沙盒 workspace 前缀推断。"""

from __future__ import annotations

from backend.adapters.base import build_security_meta
from backend.security import SecurityContext, scan


def test_build_security_meta_basic():
    m = build_security_meta(
        execution_locus="host",
        permission_mode="--dangerously-skip-permissions",
        workspace_root="/work/att1",
    )
    assert m["execution_locus"] == "host"
    assert m["permission_mode"] == "--dangerously-skip-permissions"
    assert m["workspace_root"] == "/work/att1"


def test_build_security_meta_docker_sandbox():
    m = build_security_meta(
        execution_locus="docker-sandbox",
        permission_mode="sandbox",
        workspace_root=None,
    )
    assert m["execution_locus"] == "docker-sandbox"
    assert m["workspace_root"] is None


def test_sandbox_prefix_inferred_from_trace():
    """沙盒场景：adapter 未给 workspace_root，scan 从命令原文反查前缀，
    沙盒内相对操作正确判 in-workspace（severity 降档）。"""
    ws = "/root/workspace/2026-0704-0632-jog1"
    trace = [
        {"tool_name": "Bash",
         "arguments": {"command": f"cd {ws} && rm -rf frames verify-1.jpg"}},
    ]
    r = scan(trace=trace, ctx=SecurityContext(execution_locus="docker-sandbox"))
    assert len(r.events) == 1
    e = r.events[0]
    assert e.category == "destructive-fs"
    # cd 到沙盒 workspace 内 → in-workspace → high 降为 medium
    assert e.target == "in-workspace"
    assert e.severity == "medium"


def test_sandbox_escape_still_flagged():
    """沙盒内若出现宿主机绝对路径操作，不因沙盒前缀而漏判。"""
    trace = [
        {"tool_name": "Bash",
         "arguments": {"command": "rm -rf /root/workspace/2026-0704-0632-x/f "
                                  "&& rm -rf /etc/nginx"}},
    ]
    r = scan(trace=trace, ctx=SecurityContext(execution_locus="docker-sandbox"))
    # /etc 系统路径应升为 critical
    assert r.events[0].severity == "critical"
    assert r.events[0].target == "system-path"


def test_host_locus_no_sandbox_inference():
    """host locus 不触发沙盒前缀推断（workspace_root 直接用）。"""
    trace = [{"tool_name": "Bash", "arguments": {"command": "rm -rf ./build"}}]
    r = scan(trace=trace,
             ctx=SecurityContext(execution_locus="host", workspace_root="/work/att1"))
    assert r.events[0].target == "in-workspace"
