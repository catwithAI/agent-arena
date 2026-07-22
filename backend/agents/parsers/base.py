"""Parser contract shared by text, JSONL and native session parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class EvidenceSet:
    root: Path
    stdout_path: Path
    stderr_path: Path
    evidence_path: Path | None = None

    @classmethod
    def from_runtime_dir(cls, root: Path) -> "EvidenceSet":
        root = Path(root)
        return cls(
            root=root,
            stdout_path=root / "raw" / "stdout.log",
            stderr_path=root / "raw" / "stderr.log",
            evidence_path=root / "evidence.jsonl",
        )


@dataclass(frozen=True)
class ParseDiagnostic:
    code: str
    message: str
    line: int | None = None


@dataclass(frozen=True)
class ParseResult:
    final_text: str | None
    events: tuple[Mapping[str, Any], ...] = ()
    thinking: tuple[Mapping[str, Any], ...] = ()
    tool_refs: tuple[Mapping[str, Any], ...] = ()
    usage: Mapping[str, int | None] | None = None
    session_id: str | None = None
    coverage: Mapping[str, str] = field(default_factory=dict)
    diagnostics: tuple[ParseDiagnostic, ...] = ()
    degraded: bool = False


def dotted_get(data: Mapping[str, Any], path: str | None) -> Any:
    if not path:
        return None
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current
