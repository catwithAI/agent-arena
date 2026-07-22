"""Regenerate the checked-in AgentSpec v1 JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path

from .models import agent_spec_json_schema


def main() -> None:
    target = Path(__file__).with_name("agent-spec-v1.schema.json")
    target.write_text(
        json.dumps(agent_spec_json_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
