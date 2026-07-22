"""Versioned headless bridge for the pinned DeerFlow harness."""

from __future__ import annotations

import argparse
import importlib.metadata
import inspect
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from . import DEERFLOW_PACKAGE, DEERFLOW_REVISION, DEERFLOW_VERSION, RUNNER_VERSION

MAX_EVENT_BYTES = 1024 * 1024
MAX_SUMMARY_BYTES = 64 * 1024


def probe_harness() -> tuple[int, str]:
    try:
        installed = importlib.metadata.version(DEERFLOW_PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return 20, f"{DEERFLOW_PACKAGE} is not installed"
    if installed != DEERFLOW_VERSION:
        return 21, f"{DEERFLOW_PACKAGE} {installed} is unsupported; expected {DEERFLOW_VERSION}"
    try:
        from deerflow.client import DeerFlowClient
    except Exception as exc:
        return 22, f"DeerFlowClient import failed: {type(exc).__name__}"
    parameters = inspect.signature(DeerFlowClient.__init__).parameters
    required = {"config_path", "model_name", "thinking_enabled", "subagent_enabled", "plan_mode"}
    missing = sorted(required - parameters.keys())
    if missing:
        return 22, "DeerFlowClient is incompatible; missing parameters: " + ", ".join(missing)
    stream_parameters = inspect.signature(DeerFlowClient.stream).parameters
    stream_required = {"message", "thread_id"}
    stream_missing = sorted(stream_required - stream_parameters.keys())
    accepts_overrides = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in stream_parameters.values()
    )
    if "recursion_limit" not in stream_parameters and not accepts_overrides:
        stream_missing.append("recursion_limit")
    if stream_missing:
        return 22, "DeerFlowClient.stream is incompatible; missing parameters: " + ", ".join(
            stream_missing
        )
    return 0, f"{DEERFLOW_PACKAGE} {installed}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--summary")
    parser.add_argument("--thread-id")
    parser.add_argument("--subagent", action="store_true")
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument("--plan-mode", action="store_true")
    parser.add_argument("--recursion-limit", type=int, default=1000)
    args = parser.parse_args(argv)
    if args.version:
        print(
            f"deerflow-arena-runner {RUNNER_VERSION} "
            f"(deerflow {DEERFLOW_VERSION}, revision {DEERFLOW_REVISION})"
        )
        return 0
    if args.probe:
        code, message = probe_harness()
        stream = sys.stdout if code == 0 else sys.stderr
        print(message, file=stream)
        return code
    if not args.config or not args.summary:
        parser.error("--config and --summary are required for a run")
    if not 1 <= args.recursion_limit <= 10_000:
        parser.error("--recursion-limit must be between 1 and 10000")
    return run_headless(
        config_path=Path(args.config),
        summary_path=Path(args.summary),
        prompt=sys.stdin.read(),
        thread_id=args.thread_id or f"arena-{uuid.uuid4().hex}",
        subagent=args.subagent,
        thinking=not args.no_thinking,
        plan_mode=args.plan_mode,
        recursion_limit=args.recursion_limit,
    )


def run_headless(
    *,
    config_path: Path,
    summary_path: Path,
    prompt: str,
    thread_id: str,
    subagent: bool,
    thinking: bool,
    plan_mode: bool,
    recursion_limit: int,
    client_factory: Callable[..., Any] | None = None,
    output=None,
) -> int:
    output = output or sys.stdout
    summary: dict[str, Any] = {
        "schema_version": "1",
        "runner_version": RUNNER_VERSION,
        "status": "running",
        "thread_id": thread_id,
        "usage": None,
        "final_text": None,
        "error": None,
        "diagnostics": [],
    }
    usage = {"input_tokens": 0, "output_tokens": 0}
    usage_seen = False
    message_text: dict[str, str] = {}
    try:
        if client_factory is None:
            from deerflow.client import DeerFlowClient

            client_factory = DeerFlowClient
        client = client_factory(
            config_path=str(Path(config_path).resolve()),
            model_name="arena-model",
            thinking_enabled=thinking,
            subagent_enabled=subagent,
            plan_mode=plan_mode,
        )
        events: Iterable[Any] = client.stream(
            prompt,
            thread_id=thread_id,
            recursion_limit=recursion_limit,
        )
        for event in events:
            normalized = _normalize_event(event)
            if normalized is None:
                summary["diagnostics"].append("invalid_stream_event")
                _write_event(
                    output,
                    {
                        "type": "runner_diagnostic",
                        "data": {"code": "invalid_stream_event"},
                    },
                )
                continue
            _write_event(output, normalized)
            data = normalized["data"]
            if normalized["type"] == "messages-tuple" and data.get("type") == "ai":
                content = data.get("content")
                if isinstance(content, str) and content:
                    message_id = data.get("id")
                    if isinstance(message_id, str) and message_id:
                        message_text[message_id] = message_text.get(message_id, "") + content
                        summary["final_text"] = message_text[message_id]
                    else:
                        summary["final_text"] = content
            event_usage = data.get("usage_metadata")
            if isinstance(event_usage, dict):
                for target, keys in {
                    "input_tokens": ("input_tokens", "prompt_tokens"),
                    "output_tokens": ("output_tokens", "completion_tokens"),
                }.items():
                    value = next((event_usage[key] for key in keys if key in event_usage), None)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        usage[target] += value
                        usage_seen = True
            provider_event = normalized
            message_id = data.get("id")
            if (
                normalized["type"] == "messages-tuple"
                and data.get("type") == "ai"
                and isinstance(message_id, str)
                and message_id in message_text
            ):
                provider_event = {
                    **normalized,
                    "data": {**data, "content": message_text[message_id]},
                }
            provider_error = _provider_error(provider_event)
            if provider_error:
                summary.update(
                    status="provider_error",
                    error={
                        "code": _provider_error_code(provider_error),
                        "message": provider_error,
                    },
                )
                summary["usage"] = usage if usage_seen else None
                _atomic_summary(summary_path, summary)
                return 25
        summary["status"] = "completed"
        summary["usage"] = usage if usage_seen else None
        _atomic_summary(summary_path, summary)
        return 0
    except Exception as exc:
        message = str(exc)[:4000]
        recursion = (
            "recursion" in type(exc).__name__.lower() or "recursion limit" in message.lower()
        )
        summary["status"] = "recursion_limit" if recursion else "provider_error"
        summary["error"] = {
            "code": ("agent_nonzero_exit" if recursion else _provider_error_code(message)),
            "message": message or type(exc).__name__,
        }
        _write_event(
            output,
            {
                "type": "runner_diagnostic",
                "data": {
                    "code": "recursion_limit" if recursion else "provider_exception",
                    "message": message or type(exc).__name__,
                },
            },
        )
        summary["usage"] = usage if usage_seen else None
        _atomic_summary(summary_path, summary)
        return 24 if recursion else 25


def _normalize_event(event: Any) -> dict[str, Any] | None:
    event_type = getattr(event, "type", None)
    data = getattr(event, "data", None)
    if isinstance(event, dict):
        event_type = event.get("type")
        data = event.get("data")
    if event_type not in {"values", "messages-tuple", "custom", "end"}:
        return None
    if not isinstance(data, dict):
        return None
    try:
        json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return {"type": event_type, "data": data}


def _write_event(output, event: dict[str, Any]) -> None:
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) + 1 > MAX_EVENT_BYTES:
        encoded = json.dumps(
            {
                "type": "runner_diagnostic",
                "data": {"code": "stream_event_too_large"},
            },
            separators=(",", ":"),
        )
    output.write(encoded + "\n")
    output.flush()


def _provider_error(event: dict[str, Any]) -> str | None:
    data = event["data"]
    error = data.get("error")
    if isinstance(error, str) and error:
        return error
    content = data.get("content")
    if not isinstance(content, str):
        return None
    lowered = content.lower()
    markers = ("provider error", "authentication failed", "api key invalid", "rate limit")
    return content[:4000] if any(marker in lowered for marker in markers) else None


def _provider_error_code(message: str) -> str:
    lowered = message.lower()
    if any(marker in lowered for marker in ("unauthorized", "api key", "authentication")):
        return "agent_auth_failed"
    if any(marker in lowered for marker in ("rate limit", "too many requests", "429")):
        return "agent_rate_limited"
    if any(marker in lowered for marker in ("quota", "usage limit", "insufficient credits")):
        return "agent_usage_limit"
    if any(
        marker in lowered
        for marker in ("connection refused", "connection reset", "timed out connecting")
    ):
        return "agent_network_error"
    if any(
        marker in lowered for marker in ("unknown model", "model not found", "unsupported model")
    ):
        return "agent_model_unsupported"
    return "agent_internal_error"


def _atomic_summary(path: Path, summary: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = dict(summary)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_SUMMARY_BYTES:
        payload["final_text"] = None
        payload["diagnostics"] = ["summary_truncated"]
        if isinstance(payload.get("error"), dict):
            payload["error"] = {
                **payload["error"],
                "message": str(payload["error"].get("message", ""))[:4096],
            }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_SUMMARY_BYTES:
        raise ValueError("DeerFlow summary exceeds 64 KiB after truncation")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.chmod(temporary_name, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(encoded + b"\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
