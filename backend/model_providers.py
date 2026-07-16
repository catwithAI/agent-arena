"""Third-party model provider configuration for CC/Codex adapters.

A model reference has the shape `"<provider>/<model>"` (e.g.
`"openrouter/glm-5"`) or just `"<model>"` to use the agent's own default
provider (e.g. `"sonnet"`, `"gpt-5"`). Provider sections tell the adapters how
to route the subprocess to that endpoint without touching global CLI config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel


class ModelProviderSection(BaseModel):
    kind: Literal["anthropic", "openai-chat", "openai-responses"] = "anthropic"
    base_url: str
    api_key_env: str | None = None
    # Key filled in directly (agentlane.yaml is gitignored, so it's safe to
    # put a real secret here). Resolution order lives in resolve_api_key:
    # the env var wins when set, this is the fallback. Avoids "the process
    # that spawned the backend forgot to export the key" surprises.
    api_key: str | None = None
    custom_headers: str | None = None
    # codex only: which wire protocol the endpoint speaks.
    wire_api: Literal["chat", "responses"] = "responses"
    # Auth header style (claude-code only): bearer sends
    # `Authorization: Bearer <key>` (ANTHROPIC_AUTH_TOKEN), api-key sends
    # `x-api-key: <key>` (ANTHROPIC_API_KEY). None -> default to bearer; most
    # gateways only accept one of the two and they're mutually exclusive, no
    # value rewriting happens between them.
    auth_mode: Literal["bearer", "api-key"] | None = None

    def effective_auth_mode(self) -> Literal["bearer", "api-key"]:
        return self.auth_mode or "bearer"

    def wire_protocol(self) -> str:
        """Maps `kind` to the wire-layer protocol vocabulary consumed by
        `backend/wire/sources/parse.py` (backend/wire/sources/parse.py:34-36)."""
        return {
            "anthropic": "anthropic-messages",
            "openai-chat": "openai-chat-completions",
            "openai-responses": "openai-responses",
        }[self.kind]


@dataclass
class ModelRef:
    provider: str | None
    model: str


def parse_model_ref(raw: str, providers: dict[str, ModelProviderSection]) -> ModelRef:
    if "/" in raw:
        prefix, rest = raw.split("/", 1)
        if prefix in providers:
            return ModelRef(provider=prefix, model=rest)
    return ModelRef(provider=None, model=raw)


def resolve_api_key(provider: ModelProviderSection) -> str | None:
    if provider.api_key_env:
        from_env = os.environ.get(provider.api_key_env)
        if from_env:
            return from_env
    return provider.api_key
