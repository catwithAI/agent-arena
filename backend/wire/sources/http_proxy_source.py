"""HttpProxySource：把 CC/Codex 的第三方 provider 流量接到反向代理（W4-6）。

这是 lifecycle 的 `CaptureSource`（design §8.1）——只负责在 agent 启动前生成
injection：签发短期 capture token、构造 attempt-scoped proxy base URL，让 adapter
把模型请求打到 agent-arena 内部反代（`proxy_api.py` 的 `/internal/wire-proxy/...`）。
真实转发/采集由 `http_proxy.py` 的被动 `forward` 完成。

能力边界（评审 #9 选项 A）：只用于 **CC/Codex 且模型是命名第三方 provider** 的
attempt——它们走纯 HTTP+SSE、base URL 可改。走 SDK（REST+Socket.IO 等）、模型
调用发生在独立 agent 进程内部、反代够不着的 agent 不挂本 source（design
§14.3）。dispatch 负责判定。

生命周期：
- `start()`：issue token + 建 injection（token 走 `WireInjection.capture_token`
  字段，**不放 process_env**——process_env 的 secret-key 校验会拒绝含 "TOKEN" 的
  env 名；adapter 读该字段自行注入 `LANE_WIRE_CAPTURE_TOKEN`）；
- `stop()` 空实现：spool 的 seal/drain/close 与 token revoke 已由 lifecycle 的
  attempt_end/abort 无条件调 `http_proxy.close_attempt` + `capture_token.revoke`
  兜底，本 source 不重复。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.wire import capture_token
from backend.wire.injection import WireInjection
from backend.wire.lifecycle import CaptureContext
from backend.wire.sources.http_proxy import SOURCE_KIND

logger = logging.getLogger(__name__)


class HttpProxySource:
    """CaptureSource：为一个 CC/Codex 第三方 provider attempt 注入反代 base URL。"""

    kind = SOURCE_KIND  # "lane-http"
    rewrites_transport = True  # 改写 base URL

    def __init__(self, *, attempt_id: str, provider: str, public_base_url: str) -> None:
        self.attempt_id = attempt_id
        self.provider = provider
        self.public_base_url = public_base_url.rstrip("/")
        # instance 用 provider 名：finalizer 按 instance 归属，多 provider 不互串。
        self.instance = provider

    def _proxy_base_url(self) -> str:
        # design §13.1：base URL = {public}/internal/wire-proxy/{attempt}/{provider}。
        # adapter（CC 的 ANTHROPIC_BASE_URL / Codex 的 -c base_url）把模型 API 的
        # 相对路径 append 到它。
        return (
            f"{self.public_base_url}/internal/wire-proxy/"
            f"{self.attempt_id}/{self.provider}"
        )

    async def start(self, ctx: CaptureContext) -> WireInjection:
        token = capture_token.issue(self.attempt_id)
        base = self._proxy_base_url()
        logger.info(
            "lane-http source ready attempt=%s provider=%s → %s",
            self.attempt_id, self.provider, base,
        )
        return WireInjection(
            enabled=True,
            phase=ctx.phase,
            llm_base_url=base,
            # capture_token 走专用字段（repr=False，不落 manifest/spool）；adapter
            # 读它注入 LANE_WIRE_CAPTURE_TOKEN，绕过 process_env 的 secret 校验。
            capture_token=token,
        )

    async def collect(self, ctx: CaptureContext) -> dict[str, Any]:
        return {}

    async def stop(self, ctx: CaptureContext) -> dict[str, Any]:
        # close/revoke 已由 lifecycle 兜底，这里不重复。
        return {}
