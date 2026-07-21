"""压缩 benchmark scorer（design §7.3/§8，tasks.md C5-3）。

三个维度：

- ``task_completion``（**计分**，weight>0）：probe 是否产出可解析的 `probe_answers.json`
  且至少答对一个 fact——衡量任务基本完成度；
- ``retention``（**诊断**，weight=0）：`backend.wire.retention.score_retention` 对
  facts manifest 的保真度；默认不进总分（design §522），只作独立诊断；
- ``compaction_observability``（**诊断**，weight=0）：`backend.wire.evaluation` 的
  五态压缩状态（observed/not_observed_under_budget/unsupported/incomplete/
  insufficient_calls）；不把「未触发」当失败。

**只读 probe 答案文件与 facts manifest，不读 reasoning**（design §521）：scorer 从
workspace 的 `probe_answers.json`（结构化 `{fact_id: answer}`）取答案，不解析 trace
里的思考过程。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# scorer.py 被 env_loader exec；用绝对导入拿 backend 纯函数。
try:
    from backend.wire.evaluation import evaluate_compaction, inputs_from_wire
    from backend.wire.retention import Fact, FactsManifest, score_retention
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.wire.evaluation import evaluate_compaction, inputs_from_wire
    from backend.wire.retention import Fact, FactsManifest, score_retention

ENV_DIR = Path(__file__).resolve().parent


def score(
    *,
    attempt_id: str,
    task: dict,
    env_db: Path | None = None,
    **_kwargs: Any,
) -> list[dict[str, Any]]:
    attempt_dir = _attempt_dir(attempt_id, env_db)
    workspace = _workspace(attempt_dir)
    context = task.get("context") or {}

    probe_answers = _load_probe_answers(workspace)
    manifest = _load_facts_manifest(context)

    retention = _score_retention(manifest, probe_answers)
    task_completion = _score_task_completion(retention, probe_answers)
    compaction = _score_compaction(attempt_dir)
    return [task_completion, retention, compaction]


# ---------- 读取输入 --------------------------------------------------------


def _attempt_dir(attempt_id: str, env_db: Path | None) -> Path:
    if env_db and env_db.parent.name == attempt_id:
        return env_db.parent
    return Path("data/attempts") / attempt_id


def _workspace(attempt_dir: Path) -> Path:
    ws = attempt_dir / "skill_workspace"
    return ws if ws.is_dir() else attempt_dir


def _load_probe_answers(workspace: Path) -> dict[str, Any]:
    """probe 产出的结构化答案 `{fact_id: answer}`；缺失/损坏 → 空（视作全缺失）。"""
    path = workspace / "probe_answers.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_facts_manifest(context: dict[str, Any]) -> FactsManifest | None:
    name = context.get("facts_manifest_file")
    if not name:
        return None
    path = ENV_DIR / "inputs" / name
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    facts = [
        Fact(id=f["id"], answer_hash=f.get("answer_hash"), expected=f.get("expected"))
        for f in raw.get("facts", [])
        if isinstance(f, dict) and f.get("id")
    ]
    return FactsManifest(
        facts=facts,
        seed=raw.get("seed"),
        content_sha256=raw.get("content_sha256"),
        bytes=raw.get("bytes"),
        estimated_tokens=raw.get("estimated_tokens"),
        generator_version=raw.get("generator_version"),
    )


# ---------- 维度评分 --------------------------------------------------------


def _score_retention(
    manifest: FactsManifest | None, probe_answers: dict[str, Any]
) -> dict[str, Any]:
    if manifest is None or not manifest.facts:
        return _dim("retention", 0, "no facts manifest")
    res = score_retention(manifest, probe_answers)
    pct = 0 if res.retention_score is None else round(res.retention_score * 100)
    detail = {
        "retention_score": res.retention_score,
        "per_fact": [
            {"fact_id": fs.fact_id, "score": fs.score, "verdict": fs.verdict}
            for fs in res.per_fact
        ],
        "normalizer_version": res.normalizer_version,
    }
    return _dim("retention", pct, json.dumps(detail, ensure_ascii=False))


def _score_task_completion(
    retention: dict[str, Any], probe_answers: dict[str, Any]
) -> dict[str, Any]:
    # 任务基本完成 = 产出了可解析的 probe_answers 且至少答对一个 fact。
    got_answers = any(str(v).strip() for v in probe_answers.values())
    ret_score = 0.0
    try:
        ret_score = json.loads(retention.get("detail") or "{}").get(
            "retention_score"
        ) or 0.0
    except (json.JSONDecodeError, AttributeError, TypeError):
        ret_score = 0.0
    if not probe_answers:
        return _dim("task_completion", 0, "no probe_answers.json produced")
    if not got_answers:
        return _dim("task_completion", 0, "probe_answers.json empty")
    # 有答案：完成度按是否至少答对一个 fact 给二值（100/50）——本 env 的重点是
    # retention/compaction 诊断，task_completion 只做基本闸门。
    passed = ret_score > 0
    return _dim(
        "task_completion", 100 if passed else 50,
        "answered facts" if passed else "produced answers but none correct",
    )


def _score_compaction(attempt_dir: Path) -> dict[str, Any]:
    manifest = _read_json(attempt_dir / "wire-manifest.json")
    records = _read_jsonl(attempt_dir / "wire.jsonl")
    # session continuity 从 conversation summary 推（若有 conversation.jsonl）。
    continuity = _session_continuity(attempt_dir)
    inp = inputs_from_wire(
        manifest=manifest, records=records, session_continuity=continuity,
    )
    summary = evaluate_compaction(inp)
    # compaction 是诊断维度：observed 才给 100，其余状态给 0（但不代表任务失败，
    # 只表示「本次未观察到可判定压缩」——真实结论看 detail 的 status）。
    value = 100 if summary["compaction_status"] == "observed" else 0
    return _dim("compaction_observability", value, json.dumps(summary, ensure_ascii=False))


def _session_continuity(attempt_dir: Path) -> str | None:
    try:
        from backend.conversation.summary import summarize_conversation

        return summarize_conversation(attempt_dir).get("session_continuity")
    except Exception:
        return None


# ---------- 小工具 ----------------------------------------------------------


def _dim(name: str, value: int, detail: str) -> dict[str, Any]:
    return {"dimension": name, "value": int(value), "detail": detail}


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
