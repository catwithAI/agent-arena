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
    custom_headers: str | None = None
    # codex only: which wire protocol the endpoint speaks.
    wire_api: Literal["chat", "responses"] = "responses"


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
    if not provider.api_key_env:
        return None
    return os.environ.get(provider.api_key_env)
