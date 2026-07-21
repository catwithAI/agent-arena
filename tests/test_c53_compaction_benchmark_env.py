"""C5-3 验收：标准压缩 benchmark env（env 自检 + 生成幂等 + scorer 单测）。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from backend.env_loader import load_env

ROOT = Path(__file__).parents[1]
ENV = ROOT / "envs/context-compaction-benchmark"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 注册进 sys.modules 再 exec：dataclass 解析注解时要按 __module__ 反查模块。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _scorer():
    return _load_module("compaction_benchmark_scorer", ENV / "scorer.py")


def _materials():
    return _load_module("compaction_benchmark_materials", ENV / "materials.py")


# ---------- env 自检 --------------------------------------------------------


def test_env_loads_with_two_tasks():
    env = load_env(ENV)
    assert env.name == "context-compaction-benchmark"
    task_ids = sorted(t.id for t in env.tasks)
    assert task_ids == ["compaction_main_001", "compaction_subagent_001"]


def test_main_task_has_three_turn_conversation():
    env = load_env(ENV)
    main = next(t for t in env.tasks if t.id == "compaction_main_001")
    conv = main.context["_conversation"]
    assert [t["purpose"] for t in conv] == ["setup", "pressure", "probe"]
    # 最后一轮 score_after，与 plan 校验一致。
    assert conv[-1]["score_after"] is True
    assert conv[-1]["id"] == "probe"


def test_facts_manifest_present_and_hashed():
    for name in ("facts_main_001.json", "facts_subagent_001.json"):
        raw = json.loads((ENV / "inputs" / name).read_text(encoding="utf-8"))
        assert raw["generator_version"]
        assert raw["facts"]
        for f in raw["facts"]:
            # 只存 hash，绝不含明文答案。
            assert f["answer_hash"].startswith("sha256:")
            assert "answer" not in f and "value" not in f


# ---------- 生成幂等 --------------------------------------------------------


def test_generator_is_idempotent():
    m = _materials()
    a = m.generate(seed=20260720)
    b = m.generate(seed=20260720)
    assert a.manifest == b.manifest
    assert a.setup_text == b.setup_text
    assert a.pressure_text == b.pressure_text


def test_generator_seed_variance():
    m = _materials()
    a = m.generate(seed=1)
    b = m.generate(seed=2)
    assert a.manifest["content_sha256"] != b.manifest["content_sha256"]


def test_manifest_records_size_not_model_window():
    m = _materials()
    gen = m.generate(seed=20260720)
    # 记材料规模（bytes/estimated_tokens），不含任何 model context window 字段。
    assert gen.manifest["bytes"] > 0
    assert gen.manifest["estimated_tokens"] > 0
    assert "context_window" not in gen.manifest
    assert "model" not in gen.manifest


def test_committed_tasks_match_generator():
    # 入库的 task/manifest 必须与当前生成器一致（防漂移：改生成器忘 rebuild）。
    m = _materials()
    main = m.generate(seed=20260720)
    committed = json.loads(
        (ENV / "inputs" / "facts_main_001.json").read_text(encoding="utf-8")
    )
    assert committed == main.manifest


# ---------- scorer 单测 -----------------------------------------------------


def _write_probe(workspace: Path, answers: dict) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "probe_answers.json").write_text(
        json.dumps(answers), encoding="utf-8"
    )


def _main_facts():
    return json.loads(
        (ENV / "inputs" / "facts_main_001.json").read_text(encoding="utf-8")
    )["facts"]


def _correct_answers():
    # 从生成器拿明文答案（scorer 侧只有 hash；测试侧用生成器复算明文构造正确答案）。
    gen = _materials().generate(seed=20260720)
    return {f.id: f.answer for f in gen.facts}


def _run_scorer(tmp_path, attempt_id, answers):
    workspace = tmp_path / attempt_id / "skill_workspace"
    _write_probe(workspace, answers)
    return _scorer().score(
        attempt_id=attempt_id,
        task={"id": "compaction_main_001",
              "context": {"facts_manifest_file": "facts_main_001.json"}},
        env_db=tmp_path / attempt_id / "env.db",
    )


def _dim(scores, name):
    return next(s for s in scores if s["dimension"] == name)


def test_scorer_perfect_retention(tmp_path):
    scores = _run_scorer(tmp_path, "att-perfect", _correct_answers())
    assert _dim(scores, "retention")["value"] == 100
    assert _dim(scores, "task_completion")["value"] == 100
    # 无 wire → compaction 诊断为非 observed（incomplete），不算任务失败。
    comp = json.loads(_dim(scores, "compaction_observability")["detail"])
    assert comp["compaction_status"] == "incomplete"


def test_scorer_partial_retention(tmp_path):
    correct = _correct_answers()
    facts = list(correct.keys())
    # 只答对一半。
    half = {fid: (correct[fid] if i < len(facts) // 2 else "wrong")
            for i, fid in enumerate(facts)}
    scores = _run_scorer(tmp_path, "att-partial", half)
    ret = _dim(scores, "retention")["value"]
    assert 0 < ret < 100
    # 至少答对一个 → task_completion 满分。
    assert _dim(scores, "task_completion")["value"] == 100


def test_scorer_missing_probe_answers(tmp_path):
    # 未产出 probe_answers.json → task_completion=0、retention=0。
    attempt_id = "att-missing"
    (tmp_path / attempt_id / "skill_workspace").mkdir(parents=True)
    scores = _scorer().score(
        attempt_id=attempt_id,
        task={"id": "compaction_main_001",
              "context": {"facts_manifest_file": "facts_main_001.json"}},
        env_db=tmp_path / attempt_id / "env.db",
    )
    assert _dim(scores, "task_completion")["value"] == 0
    assert _dim(scores, "retention")["value"] == 0


def test_scorer_hallucinated_answers(tmp_path):
    wrong = {f["id"]: "deadbeef" for f in _main_facts()}
    scores = _run_scorer(tmp_path, "att-halluc", wrong)
    assert _dim(scores, "retention")["value"] == 0
    # 产出了答案但全错 → task_completion 50（产出但无正确）。
    assert _dim(scores, "task_completion")["value"] == 50


def test_scorer_reads_answers_not_reasoning(tmp_path):
    # 多余的 reasoning 键被忽略，只按 fact id 评分。
    answers = _correct_answers()
    answers["reasoning"] = "我先猜了很多次"
    scores = _run_scorer(tmp_path, "att-reasoning", answers)
    assert _dim(scores, "retention")["value"] == 100


def test_scorer_compaction_observed_with_wire(tmp_path):
    attempt_id = "att-observed"
    workspace = tmp_path / attempt_id / "skill_workspace"
    _write_probe(workspace, _correct_answers())
    attempt_dir = tmp_path / attempt_id
    # 造一条 wire manifest + context_compaction record → observed。
    (attempt_dir / "wire-manifest.json").write_text(
        json.dumps({"status": "complete", "sources": [], "gaps": []}), encoding="utf-8"
    )
    (attempt_dir / "wire.jsonl").write_text(
        json.dumps({"record_type": "context_compaction", "attempt_id": attempt_id,
                    "data": {"before_call_id": "lc1", "after_call_id": "lc2"}}) + "\n",
        encoding="utf-8",
    )
    scores = _scorer().score(
        attempt_id=attempt_id,
        task={"id": "compaction_main_001",
              "context": {"facts_manifest_file": "facts_main_001.json"}},
        env_db=attempt_dir / "env.db",
    )
    comp = json.loads(_dim(scores, "compaction_observability")["detail"])
    assert comp["compaction_status"] == "observed"
    assert _dim(scores, "compaction_observability")["value"] == 100
