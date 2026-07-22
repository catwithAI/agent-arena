"""Text-only parser with offline replay support."""

from __future__ import annotations

from pathlib import Path

from .base import EvidenceSet, ParseDiagnostic, ParseResult


class TextParser:
    parser_id = "text"
    parser_version = "1"

    def __init__(self, *, output_file: Path | None = None, max_bytes: int = 100 * 1024 * 1024):
        self.output_file = output_file
        self.max_bytes = max_bytes

    async def parse(self, evidence: EvidenceSet) -> ParseResult:
        source = self._source(evidence)
        diagnostics: list[ParseDiagnostic] = []
        try:
            raw = source.read_bytes()
        except OSError as exc:
            return ParseResult(
                final_text=None,
                coverage={"final_text": "unknown", "structured_events": "unsupported"},
                diagnostics=(ParseDiagnostic("text_read_failed", type(exc).__name__),),
                degraded=True,
            )
        if len(raw) > self.max_bytes:
            raw = raw[: self.max_bytes]
            diagnostics.append(
                ParseDiagnostic("text_truncated", f"text evidence exceeded {self.max_bytes} bytes")
            )
        text = raw.decode("utf-8", errors="replace")
        if "\ufffd" in text:
            diagnostics.append(ParseDiagnostic("text_decode_replaced", "invalid UTF-8 replaced"))
        lines = [line for line in text.splitlines() if line.strip()]
        events = tuple(
            {"kind": "message", "sequence": index, "text": line}
            for index, line in enumerate(lines, start=1)
        )
        final_text = text.strip() or None
        return ParseResult(
            final_text=final_text,
            events=events,
            coverage={
                "final_text": "verified" if final_text is not None else "unknown",
                "structured_events": "unsupported",
                "token_usage": "unsupported",
                "thinking": "unsupported",
                "tools": "unsupported",
            },
            diagnostics=tuple(diagnostics),
            degraded=bool(diagnostics),
        )

    def _source(self, evidence: EvidenceSet) -> Path:
        if self.output_file is None:
            return evidence.stdout_path
        root = evidence.root.resolve()
        candidate = self.output_file
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("text parser output_file escapes the evidence root")
        return candidate
