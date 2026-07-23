"""Deterministic aggregation and Markdown reporting for Experiment results."""

from __future__ import annotations

import itertools
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .storage import atomic_write_json, read_jsonl


def aggregate_experiment(
    experiment_dir: Path, *, pass_threshold: float | None = None
) -> dict[str, Any]:
    experiment_dir = Path(experiment_dir)
    manifest_rows = read_jsonl(experiment_dir / "results.jsonl")
    rows = _current_results(
        _deduplicate(manifest_rows),
        read_jsonl(experiment_dir / "jobs.jsonl"),
    )
    manifest = _load_json(experiment_dir / "manifest.json")
    threshold = (
        float(pass_threshold)
        if pass_threshold is not None
        else float((manifest.get("config") or {}).get("pass_threshold", 60))
    )
    summary = {
        "schema_version": "1",
        "experiment_id": manifest.get("experiment_id") or experiment_dir.name,
        "config_hash": manifest.get("config_hash"),
        "source": manifest.get("source", {}),
        "pass_threshold": threshold,
        "attempts": len(rows),
        "jobs": len({row.get("job_id") for row in rows}),
        "by_agent": _group_stats(rows, lambda row: str(row.get("agent"))),
        "by_model": _group_stats(rows, _entrant),
        "by_environment": _group_stats(rows, lambda row: str(row.get("env"))),
        "by_variant": _group_stats(rows, lambda row: str(row.get("variant"))),
        "by_dimension": _dimension_stats(rows),
        "head_to_head": _head_to_head(rows),
        "security_by_agent": _security_stats(rows),
        "reproducibility": _reproducibility(rows, manifest),
    }
    for section in ("by_agent", "by_model", "by_environment", "by_variant"):
        for stats in summary[section].values():
            _add_pass_rate(stats, threshold)
    atomic_write_json(experiment_dir / "summary.json", summary)
    (experiment_dir / "report.md").write_text(
        render_markdown(summary), encoding="utf-8"
    )
    return summary


def _deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        job_id, attempt_id = row.get("job_id"), row.get("attempt_id")
        if isinstance(job_id, str) and isinstance(attempt_id, str):
            latest[(job_id, attempt_id)] = row
    return list(latest.values())


def _current_results(
    rows: list[dict[str, Any]], journal: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Use only the latest run generation for each job.

    Failed generations remain in results.jsonl as audit evidence, while the
    default report represents the final state after ``--retry-failed``.
    """
    latest_runs: dict[str, str] = {}
    for event in journal:
        job_id, run_id = event.get("job_id"), event.get("run_id")
        if isinstance(job_id, str) and isinstance(run_id, str):
            latest_runs[job_id] = run_id
    return [
        row
        for row in rows
        if latest_runs.get(str(row.get("job_id")), row.get("run_id"))
        == row.get("run_id")
    ]


def _load_json(path: Path) -> dict[str, Any]:
    import json

    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _group_stats(
    rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], str]
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return {
        name: _stats(group)
        for name, group in sorted(groups.items())
    }


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = _numbers(row.get("score_total") for row in rows)
    durations = _numbers(row.get("duration_ms") for row in rows)
    input_tokens = _numbers(
        (row.get("token_usage") or {}).get("input_tokens") for row in rows
    )
    output_tokens = _numbers(
        (row.get("token_usage") or {}).get("output_tokens") for row in rows
    )
    costs = _numbers(row.get("cost_estimate") for row in rows)
    completed = sum(row.get("status") == "completed" for row in rows)
    return {
        "attempts": len(rows),
        "completed": completed,
        "completion_rate": _round(completed / len(rows)) if rows else None,
        "scored": len(scores),
        "avg_score": _mean(scores),
        "score_std": _stdev(scores),
        "score_ci95": _mean_ci95(scores),
        "median_score": _median(scores),
        "min_score": min(scores) if scores else None,
        "max_score": max(scores) if scores else None,
        "avg_duration_ms": _mean(durations),
        "avg_input_tokens": _mean(input_tokens),
        "avg_output_tokens": _mean(output_tokens),
        "avg_cost": _mean(costs),
        "statuses": _counts(str(row.get("status")) for row in rows),
        "error_codes": _counts(
            str(row.get("error_code")) for row in rows if row.get("error_code")
        ),
        "_scores": scores,
    }


def _add_pass_rate(stats: dict[str, Any], threshold: float) -> None:
    scores = stats.pop("_scores", [])
    stats["passed"] = sum(score >= threshold for score in scores)
    stats["pass_rate"] = (
        _round(stats["passed"] / len(scores)) if scores else None
    )


def _dimension_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        entrant = _entrant(row)
        for score in row.get("scores") or []:
            if not isinstance(score, dict):
                continue
            dimension, value = score.get("dimension"), score.get("value")
            if isinstance(dimension, str) and _is_number(value):
                values[dimension][entrant].append(float(value))
    return {
        dimension: {
            entrant: {
                "n": len(samples),
                "avg": _mean(samples),
                "std": _stdev(samples),
                "ci95": _mean_ci95(samples),
            }
            for entrant, samples in sorted(per_entrant.items())
        }
        for dimension, per_entrant in sorted(values.items())
    }


def _head_to_head(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paired_units: dict[tuple[str, str, int], dict[str, float]] = defaultdict(dict)
    for row in rows:
        score = row.get("score_total")
        if _is_number(score):
            key = (
                str(row.get("env")),
                str(row.get("task_id")),
                int(row.get("repeat", -1)),
            )
            paired_units[key][_comparison_entrant(row)] = float(score)
    pairs: dict[tuple[str, str], list[float]] = defaultdict(list)
    for entrants in paired_units.values():
        for left, right in itertools.combinations(sorted(entrants), 2):
            pairs[(left, right)].append(entrants[left] - entrants[right])
    output = []
    for (left, right), deltas in sorted(pairs.items()):
        output.append(
            {
                "left": left,
                "right": right,
                "paired_runs": len(deltas),
                "left_wins": sum(delta > 0 for delta in deltas),
                "right_wins": sum(delta < 0 for delta in deltas),
                "ties": sum(delta == 0 for delta in deltas),
                "left_win_rate": _round(
                    sum(delta > 0 for delta in deltas) / len(deltas)
                ),
                "avg_score_delta": _mean(deltas),
                "delta_ci95": _mean_ci95(deltas),
            }
        )
    return output


def _reproducibility(
    rows: list[dict[str, Any]], manifest: dict[str, Any]
) -> dict[str, Any]:
    agents: dict[str, dict[str, Any]] = {}
    degradations: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        public = row.get("agent_manifest")
        if not isinstance(public, dict):
            continue
        agent = public.get("agent") or {}
        agent_id = agent.get("id")
        if not isinstance(agent_id, str):
            continue
        agents[agent_id] = {
            "version": agent.get("version"),
            "spec_hash": agent.get("spec_hash"),
            "transport": agent.get("transport"),
        }
        degradations[agent_id].update(
            str(value) for value in (public.get("degradations") or [])
        )
    return {
        "config_hash": manifest.get("config_hash"),
        "git_commit": (manifest.get("source") or {}).get("git_commit"),
        "agents": agents,
        "degradations": {
            agent: sorted(values) for agent, values in sorted(degradations.items())
        },
        "manifest_coverage": {
            "available": sum(isinstance(row.get("agent_manifest"), dict) for row in rows),
            "total": len(rows),
        },
    }


def _security_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("agent"))].append(row)
    output: dict[str, Any] = {}
    for agent, agent_rows in sorted(groups.items()):
        categories: dict[str, int] = defaultdict(int)
        hitl: dict[str, int] = defaultdict(int)
        severities: dict[str, int] = defaultdict(int)
        total_events = 0
        attempts_with_events = 0
        for row in agent_rows:
            security = row.get("security") or {}
            event_count = int(security.get("event_count") or 0)
            total_events += event_count
            attempts_with_events += event_count > 0
            severity = security.get("max_severity")
            if severity:
                severities[str(severity)] += 1
            for category, count in (security.get("by_category") or {}).items():
                categories[str(category)] += int(count)
            hitl_block = security.get("hitl") or {}
            for status, count in (hitl_block.get("counts") or {}).items():
                hitl[str(status)] += int(count)
        output[agent] = {
            "attempts": len(agent_rows),
            "attempts_with_events": attempts_with_events,
            "event_count": total_events,
            "events_by_category": dict(sorted(categories.items())),
            "max_severity_attempts": dict(sorted(severities.items())),
            "hitl": dict(sorted(hitl.items())),
        }
    return output


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# Experiment report — {summary['experiment_id']}",
        "",
        f"- Attempts: {summary['attempts']}",
        f"- Jobs with results: {summary['jobs']}",
        f"- Pass threshold: {summary['pass_threshold']}",
        f"- Config: `{summary.get('config_hash') or 'unknown'}`",
        f"- Source commit: `{(summary.get('source') or {}).get('git_commit') or 'unknown'}`",
        "",
        "## Agent summary",
        "",
        "| Agent | Attempts | Completion | Pass rate | Mean score | 95% CI | Std | Duration |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, stats in summary["by_agent"].items():
        lines.append(
            f"| {name} | {stats['attempts']} | {_pct(stats['completion_rate'])} |"
            f" {_pct(stats['pass_rate'])} | {_fmt(stats['avg_score'])} |"
            f" {_fmt_ci(stats['score_ci95'])} | {_fmt(stats['score_std'])} |"
            f" {_duration(stats['avg_duration_ms'])} |"
        )
    lines.extend(["", "## Model / Agent combinations", ""])
    lines.extend(_stats_table(summary["by_model"]))
    lines.extend(["", "## Environments", ""])
    lines.extend(_stats_table(summary["by_environment"]))
    lines.extend(["", "## Head-to-head", ""])
    if summary["head_to_head"]:
        lines.extend(
            [
                "| Left | Right | Paired | W-L-T | Left win rate | Mean delta | 95% CI |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for pair in summary["head_to_head"]:
            lines.append(
                f"| {pair['left']} | {pair['right']} | {pair['paired_runs']} |"
                f" {pair['left_wins']}-{pair['right_wins']}-{pair['ties']} |"
                f" {_pct(pair['left_win_rate'])} | {_fmt(pair['avg_score_delta'])} |"
                f" {_fmt_ci(pair['delta_ci95'])} |"
            )
    else:
        lines.append("No paired scored attempts were available.")
    lines.extend(["", "## Security observations", ""])
    lines.extend(
        [
            "| Agent | Attempts with events | Events | Max severity distribution |",
            "|---|---:|---:|---|",
        ]
    )
    for agent, security in summary["security_by_agent"].items():
        severity = ", ".join(
            f"{name}={count}"
            for name, count in security["max_severity_attempts"].items()
        ) or "-"
        lines.append(
            f"| {agent} | {security['attempts_with_events']}/{security['attempts']} |"
            f" {security['event_count']} | {severity} |"
        )
    lines.extend(["", "## Reproducibility", ""])
    coverage = summary["reproducibility"]["manifest_coverage"]
    lines.append(
        f"- Public Agent Manifests: {coverage['available']}/{coverage['total']} attempts"
    )
    for agent, value in summary["reproducibility"]["agents"].items():
        lines.append(
            f"- `{agent}`: version={value.get('version') or 'unknown'}, "
            f"spec={value.get('spec_hash') or 'unknown'}, "
            f"transport={value.get('transport') or 'unknown'}"
        )
    lines.append("")
    return "\n".join(lines)


def _stats_table(groups: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "| Name | Attempts | Completion | Pass rate | Mean score | 95% CI |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, stats in groups.items():
        lines.append(
            f"| {name} | {stats['attempts']} | {_pct(stats['completion_rate'])} |"
            f" {_pct(stats['pass_rate'])} | {_fmt(stats['avg_score'])} |"
            f" {_fmt_ci(stats['score_ci95'])} |"
        )
    return lines


def _entrant(row: dict[str, Any]) -> str:
    agent = str(row.get("agent"))
    model = row.get("effective_model") or row.get("requested_model")
    return f"{agent} · {model}" if model else agent


def _comparison_entrant(row: dict[str, Any]) -> str:
    return f"{row.get('variant')} · {_entrant(row)}"


def _numbers(values: Any) -> list[float]:
    return [float(value) for value in values if _is_number(value)]


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _mean(values: list[float]) -> float | None:
    return _round(statistics.mean(values)) if values else None


def _median(values: list[float]) -> float | None:
    return _round(statistics.median(values)) if values else None


def _stdev(values: list[float]) -> float:
    return _round(statistics.stdev(values)) if len(values) > 1 else 0.0


def _mean_ci95(values: list[float]) -> list[float] | None:
    if not values:
        return None
    mean = statistics.mean(values)
    if len(values) == 1:
        return [_round(mean), _round(mean)]
    margin = 1.96 * statistics.stdev(values) / math.sqrt(len(values))
    return [_round(mean - margin), _round(mean + margin)]


def _round(value: float) -> float:
    return round(float(value), 4)


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _pct(value: Any) -> str:
    return "-" if value is None else f"{100 * float(value):.1f}%"


def _fmt(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def _fmt_ci(value: Any) -> str:
    if not isinstance(value, list) or len(value) != 2:
        return "-"
    return f"[{value[0]:.2f}, {value[1]:.2f}]"


def _duration(value: Any) -> str:
    return "-" if value is None else f"{float(value) / 1000:.1f}s"
