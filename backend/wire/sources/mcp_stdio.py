"""McpStdioSource：把 CC/Codex 的 MCP server 命令改写成经 mcp_tap 包装（W3-3）。

这是 lifecycle 的 `CaptureSource`（design §8.1/§12.1）。`start()` 产一个
`WireInjection.mcp_rewrites`，key 为 adapter 约定的 `lane-<env_name>`，值是
`CommandRewrite`——adapter 会把原 MCP command 后置成：

    <tap_command> <tap_args...> -- <原 MCP command...>

tap（`python -m backend.wire.mcp_tap`）在 CC/Codex 与真实 MCP server 之间做透明
双向 pump + JSON-RPC 帧 capture（见 mcp_tap.py / mcp_frames.py）。

只包 agent-lane 注入的那个 server（key 精确匹配 `lane-<env_name>`），不动用户全局
MCP 配置。spool 落在 `<attempt>/wire-sources/`（tap 是独立进程，直接写该目录）。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from backend.wire import paths
from backend.wire.injection import CommandRewrite, WireInjection
from backend.wire.lifecycle import CaptureContext

logger = logging.getLogger(__name__)

SOURCE_KIND = "mcp-stdio"


class McpStdioSource:
    """CaptureSource：为 CC/Codex 的 MCP server 命令注入 mcp_tap 包装。"""

    kind = "mcp-stdio"
    rewrites_transport = True  # 改写 command

    def __init__(self, *, attempt_id: str, env_name: str, data_path: Any) -> None:
        self.attempt_id = attempt_id
        self.env_name = env_name
        self.data_path = data_path
        # adapter 约定的 MCP server key。
        self.server_key = f"lane-{env_name}"
        self.instance = env_name

    def _tap_rewrite(self, ctx: CaptureContext) -> CommandRewrite:
        # 必须用**绝对**路径：tap 是 adapter（codex/CC）以 cwd=attempt_dir 拉起的独立
        # 子进程，若传相对 spool-dir，会被 tap 的 CWD 二次解析成
        # attempt_dir/data/attempts/<att>/wire-sources（双重嵌套），finalize 扫不到 →
        # mcp_frame 恒为 0。resolve() 钉死绝对路径，与 tap 子进程的 CWD 无关。
        spool_dir = paths.sources_dir(self.data_path, self.attempt_id).resolve()
        # tap 前缀：python -m backend.wire.mcp_tap <args> --。adapter 会把原 command
        # 整体后置到 `--` 之后（[*args_prefix, orig_command, *orig_args]）。
        args_prefix = (
            "-m", "backend.wire.mcp_tap",
            "--attempt-id", self.attempt_id,
            "--phase", ctx.phase,
            "--spool-dir", str(spool_dir),
            "--policy", ctx.policy.effective,
            "--instance", self.instance,
            "--",
        )
        # command 用当前 Python 解释器（与 backend 同环境，import 得到 backend.wire）。
        return CommandRewrite(command=sys.executable, args_prefix=args_prefix)

    async def start(self, ctx: CaptureContext) -> WireInjection:
        rewrite = self._tap_rewrite(ctx)
        logger.info(
            "mcp-stdio source ready attempt=%s server=%s → tap 包装",
            self.attempt_id, self.server_key,
        )
        return WireInjection(
            enabled=True,
            phase=ctx.phase,
            mcp_rewrites={self.server_key: rewrite},
        )

    async def collect(self, ctx: CaptureContext) -> dict[str, Any]:
        return {}

    async def stop(self, ctx: CaptureContext) -> dict[str, Any]:
        # tap 是 adapter 进程树的一部分，随 MCP server 生命周期退出；spool 由 tap
        # 自己 close（逐行 flush + .partial 恢复），本 source 无需收尾。
        return {}
