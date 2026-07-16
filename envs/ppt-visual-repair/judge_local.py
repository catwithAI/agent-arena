"""PPT human-taste visual judge — direct Anthropic Messages API call.

Replaces the previous multi-turn agent-session judge with a single
multimodal request: the three PNG previews (reference/draft/candidate) are
attached as image blocks and Claude returns the rubric JSON directly. No
sandbox, no session, no file uploads.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

PROMPT_VERSION = "lane_ppt_human_taste_judge_v3_direct_api"
DEFAULT_MODEL = "claude-opus-4-5-20251101"
DEFAULT_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

DIMENSIONS = [
    "可视性与无遮挡",
    "内容与主题保真",
    "图像与对象完整性",
    "文件与页面完整性",
    "视觉层级与信息焦点",
    "构图与视觉平衡",
    "图片位置、角度与空间关系",
    "对齐、间距与留白",
    "字体、色彩与风格一致性",
    "整体人类偏好",
]
USABILITY_DIMENSIONS = DIMENSIONS[:4]
TASTE_DIMENSIONS = DIMENSIONS[4:]

CRITICAL_PATTERNS = [
    "无法阅读",
    "完全看不清",
    "关键内容缺失",
    "无法交付",
]
SPATIAL_PATTERNS = [
    "位置不自然",
    "位置怪异",
    "位置突兀",
    "角度不自然",
    "角度怪异",
    "旋转异常",
    "倾斜异常",
    "空间关系失衡",
]
BALANCE_PATTERNS = [
    "重心偏左",
    "重心偏右",
    "重心失衡",
    "右侧留白过多",
    "左侧留白过多",
    "大面积无功能留白",
    "空间利用失衡",
]


class JudgeConfig:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_API_URL,
        model: str = DEFAULT_MODEL,
        timeout: int = 300,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout


def load_config() -> JudgeConfig | None:
    config = _load_yaml_config()
    shared = config.get("llm_judge") if isinstance(config.get("llm_judge"), dict) else {}
    legacy = _judge_yaml(config)
    api_key = (
        os.environ.get("PPT_JUDGE_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or str(legacy.get("api_key") or shared.get("api_key") or "")
    )
    if not api_key:
        return None
    return JudgeConfig(
        api_key=api_key,
        base_url=(
            os.environ.get("PPT_JUDGE_BASE_URL")
            or str(legacy.get("base_url") or shared.get("base_url") or DEFAULT_API_URL)
        ),
        model=(
            os.environ.get("PPT_JUDGE_MODEL")
            or os.environ.get("LLM_JUDGE_MODEL")
            or str(legacy.get("model") or shared.get("model") or DEFAULT_MODEL)
        ),
        timeout=int(
            os.environ.get("PPT_JUDGE_TIMEOUT")
            or os.environ.get("LLM_JUDGE_TIMEOUT")
            or legacy.get("timeout")
            or shared.get("timeout")
            or 300
        ),
    )


def run_visual_judge(
    *,
    reference_png: Path,
    draft_png: Path,
    candidate_png: Path,
    public_rubric: str,
    corruption_summary: str,
    design_notes: str,
    artifact_dir: Path,
) -> dict[str, Any]:
    config = load_config()
    if config is None:
        return {
            "ok": False,
            "skipped": True,
            "error": "judge is not configured; set agentlane.yaml llm_judge or PPT_JUDGE_*/ANTHROPIC_API_KEY env vars",
            "score_100": 0,
        }

    prompt = build_prompt(public_rubric=public_rubric, corruption_summary=corruption_summary, design_notes=design_notes)
    try:
        return _run(
            config=config,
            prompt=prompt,
            reference_png=reference_png,
            draft_png=draft_png,
            candidate_png=candidate_png,
            artifact_dir=artifact_dir,
        )
    except Exception as exc:
        return {"ok": False, "error": f"judge failed: {exc}", "score_100": 0}


def _run(
    *,
    config: JudgeConfig,
    prompt: str,
    reference_png: Path,
    draft_png: Path,
    candidate_png: Path,
    artifact_dir: Path,
) -> dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for label, path in (("IMAGE 1 / REFERENCE", reference_png), ("IMAGE 2 / DRAFT", draft_png), ("IMAGE 3 / CANDIDATE", candidate_png)):
        content.append({"type": "text", "text": f"\n{label}:"})
        content.append(_image_block(path))

    payload = {
        "model": config.model,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {
        "x-api-key": config.api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    resp = httpx.post(config.base_url, json=payload, headers=headers, timeout=config.timeout)
    (artifact_dir / "judge_http_status.txt").write_text(str(resp.status_code), encoding="utf-8")
    if resp.status_code >= 400:
        return {"ok": False, "error": f"judge API error {resp.status_code}: {resp.text[:500]}", "score_100": 0}

    data = resp.json()
    raw_text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()
    (artifact_dir / "judge_prompt.txt").write_text(prompt, encoding="utf-8")
    (artifact_dir / "judge_raw_response.txt").write_text(raw_text, encoding="utf-8")
    if not raw_text:
        return {"ok": False, "error": "judge produced no text response", "score_100": 0}
    try:
        result = normalize_result(parse_model_json(raw_text), config.model)
    except Exception as exc:
        return {"ok": False, "error": f"judge response parse/validation failed: {exc}", "score_100": 0}
    result["latency_ms"] = int((time.time() - start) * 1000)
    result["provider"] = "anthropic"
    (artifact_dir / "judge_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "score_100": score_100(result), "judge_review": result}


def _image_block(path: Path) -> dict[str, Any]:
    media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def build_prompt(*, public_rubric: str, corruption_summary: str, design_notes: str) -> str:
    return f"""你是 PPT 设计审美与人类偏好 Judge。请根据随消息附带的三张图片，评估 Agent 的视觉判断是否接近成熟的人类演示文稿设计师。

图片固定顺序：
1. IMAGE 1 = REFERENCE：人类设计参考，用于校准专业水准；
2. IMAGE 2 = DRAFT：可正常打开、但视觉质量欠佳的待润色草稿；
3. IMAGE 3 = CANDIDATE：Agent 的润色结果。

不要交换 DRAFT 和 CANDIDATE。评测包含两条不可互相抵消的轴：
1. USABILITY：文件与页面完整、内容可见、没有遮挡/裁切/拉伸等客观可用性问题；
2. TASTE：视觉层级、构图、图片位置与角度、间距、排版、风格和整体人类偏好。

REFERENCE 是人类审美锚点，不是像素级标准答案；CANDIDATE 可以采用不同但合理的布局。你需要判断 CANDIDATE 是否明显优于 DRAFT、是否具有专业设计的层级与克制、以及人类设计师是否愿意直接展示它。

重要约束：
- 按公开 rubric 的 4 个可用性维度和 6 个审美维度评分，每项只能取 0、1、2 分，总分 20 分。
- 0 = 明显欠佳或变差；1 = 有改善但仍需专业人员调整；2 = 成熟、协调、可直接展示。
- 可用性是硬门槛，不能用审美分补偿。遮挡、裁切、缺失、失真应只按其客观影响评分，不要把“无遮挡”本身描述成审美优秀。
- 必须先做 DRAFT 与 CANDIDATE 的成对偏好判断，再参考 REFERENCE 校准“专业可交付”的门槛。
- 不因 CANDIDATE 与 REFERENCE 坐标、字号或构图不同而扣分；只在其设计判断本身较差时扣分。
- 单纯重新保存、几乎没有可感知视觉变化，不算润色成功。
- 图片的位置、旋转角度和与其他元素的空间关系是核心设计判断，不能被更好看的字体或颜色抵消。请明确比较 DRAFT 与 CANDIDATE 的主要图片；如果位置/角度没有实质改善，将 `major_image_placement_or_rotation_improved` 设为 false。如果主要图片仍处于明显奇怪的位置或角度，将 `major_spatial_issues_resolved` 设为 false，并将“图片位置、角度与空间关系”评为 0；整体结果不能标记为 accept。
- 必须严格检查页面视觉重心和空间利用：比较左右视觉密度、主要图片与文本组的整体重心，以及是否存在大面积无功能留白。如果 Agent 只是缩小字体来避开遮挡，但没有移动图片，导致右侧仍有大量留白或页面重心明显偏向一侧，将 `visual_center_balanced` 设为 false、`excessive_unused_whitespace_remaining` 设为 true、`font_shrink_substituted_for_layout_fix` 设为 true。此类修改只能算局部可读性改善，不能算构图修复。
- 遮挡必须分级，不能全有或全无地评分：如果 Agent 通过调整字号消除了主标题/副标题遮挡，但正文仍被图片遮挡，应将 `readability_improved_over_draft` 设为 true、`remaining_occlusion_severity` 设为 major。此时“可视性与无遮挡”可得 1 分，体现局部改善；但因为正文仍受影响且图片位置根因未解决，不能直接交付。
- 若内容不可读、关键内容缺失、大面积遮挡/错位/裁剪，必须对相关维度低分。
- design_notes 仅用于理解设计意图，不能推翻图片事实；没有提交 notes 不扣分。
- 如果差异可能是轻微字体渲染、抗锯齿、阴影、边框细节，不要直接判为失败，可写入 human_attention_points。

公开审美评审标准：
{public_rubric[:6000]}

私有视觉缺陷提示（只用于帮助观察，不是唯一正确改法）：
{corruption_summary[:3000]}

Agent design_notes.md（如未提交则为空）：
{design_notes[:3000]}

必须只输出 JSON，不要输出 Markdown，结构如下：
{{
  "schema_version": "llm_judge.v1",
  "judge_type": "human_taste_alignment",
  "model": "<模型名>",
  "prompt_version": "{PROMPT_VERSION}",
  "overall": {{
    "total_score": 0,
    "max_score": 20,
    "usability_score": 0,
    "usability_max_score": 8,
    "taste_score": 0,
    "taste_max_score": 12,
    "final_judgement": "审美成熟，可直接展示 / 基本可用，仍需人工微调 / 可用性未达标 / 可用但审美未达标 / 未形成有效视觉改进",
    "accept_recommendation": "accept | needs_human_tuning | reject",
    "confidence": "high | medium | low"
  }},
  "rubric_scores": [
    {{"dimension": "可视性与无遮挡", "score": 0, "reason": "...", "evidence": ["..."]}},
    {{"dimension": "内容与主题保真", "score": 0, "reason": "...", "evidence": ["..."]}},
    {{"dimension": "图像与对象完整性", "score": 0, "reason": "...", "evidence": ["..."]}},
    {{"dimension": "文件与页面完整性", "score": 0, "reason": "...", "evidence": ["..."]}},
    {{"dimension": "视觉层级与信息焦点", "score": 0, "reason": "...", "evidence": ["..."]}},
    {{"dimension": "构图与视觉平衡", "score": 0, "reason": "...", "evidence": ["..."]}},
    {{"dimension": "图片位置、角度与空间关系", "score": 0, "reason": "...", "evidence": ["..."]}},
    {{"dimension": "对齐、间距与留白", "score": 0, "reason": "...", "evidence": ["..."]}},
    {{"dimension": "字体、色彩与风格一致性", "score": 0, "reason": "...", "evidence": ["..."]}},
    {{"dimension": "整体人类偏好", "score": 0, "reason": "...", "evidence": ["..."]}}
  ],
  "failure_types": [{{"key": "...", "label": "...", "reason": "..."}}],
  "visual_assessment": {{
    "candidate_preferred_over_draft": true,
    "meaningful_visual_change": true,
    "candidate_presentable": true,
    "content_preserved": true,
    "readability_improved_over_draft": true,
    "remaining_occlusion_severity": "none | minor | major | critical",
    "key_content_usable": true,
    "occlusion_root_cause_fixed": true,
    "visual_center_balanced": true,
    "excessive_unused_whitespace_remaining": false,
    "font_shrink_substituted_for_layout_fix": false,
    "major_image_placement_or_rotation_improved": true,
    "major_spatial_issues_resolved": true,
    "remaining_awkward_position_or_rotation": false,
    "candidate_vs_reference": "better | comparable | worse",
    "remaining_major_issues": ["..."],
    "new_errors": ["..."],
    "ambiguous_differences": ["..."]
  }},
  "human_attention_points": ["..."],
  "review_note_draft": "..."
}}"""


def parse_model_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("no JSON object found")


def normalize_result(result: dict[str, Any], model: str) -> dict[str, Any]:
    result["schema_version"] = "llm_judge.v1"
    result["judge_type"] = "human_taste_alignment"
    result["model"] = result.get("model") or model
    result["prompt_version"] = result.get("prompt_version") or PROMPT_VERSION
    rubric = result.get("rubric_scores")
    if not isinstance(rubric, list):
        raise ValueError("rubric_scores must be a list")
    by_dim = {item.get("dimension"): item for item in rubric if isinstance(item, dict)}
    normalized = []
    for dim in DIMENSIONS:
        item = by_dim.get(dim)
        if not item:
            raise ValueError(f"missing rubric dimension: {dim}")
        score = coerce_score(item.get("score"))
        if score is None:
            raise ValueError(f"invalid score for {dim}: {item.get('score')!r}")
        item["score"] = score
        normalized.append(item)
    result["rubric_scores"] = normalized
    apply_visual_safety_caps(result)
    total = sum(item["score"] for item in result["rubric_scores"])
    max_score = len(result["rubric_scores"]) * 2
    overall = result.setdefault("overall", {})
    overall["total_score"] = total
    overall["max_score"] = max_score
    usability_score = sum(by_dim[dim]["score"] for dim in USABILITY_DIMENSIONS)
    taste_score = sum(by_dim[dim]["score"] for dim in TASTE_DIMENSIONS)
    overall["usability_score"] = usability_score
    overall["usability_max_score"] = len(USABILITY_DIMENSIONS) * 2
    overall["taste_score"] = taste_score
    overall["taste_max_score"] = len(TASTE_DIMENSIONS) * 2
    overall["final_judgement"] = final_judgement(total, max_score, usability_score, taste_score)
    overall["accept_recommendation"] = recommendation(total, max_score, usability_score, taste_score)
    assessment = result.get("visual_assessment")
    hard_fail = isinstance(assessment, dict) and (
        assessment.get("content_preserved") is False
        or assessment.get("candidate_presentable") is False
        or assessment.get("remaining_occlusion_severity") == "critical"
        or assessment.get("key_content_usable") is False
    )
    if hard_fail:
        overall["final_judgement"] = "可用性未达标"
        overall["accept_recommendation"] = "reject"
    elif spatial_gate_failed(result):
        overall["final_judgement"] = "基本可用，仍需人工调整图片位置与角度"
        if overall["accept_recommendation"] == "accept":
            overall["accept_recommendation"] = "needs_human_tuning"
    if not hard_fail and balance_gate_failed(result):
        overall["final_judgement"] = "基本可用，页面重心与空间利用仍需调整"
        if total >= 14 and usability_score >= 6:
            overall["accept_recommendation"] = "needs_human_tuning"
        elif overall["accept_recommendation"] == "accept":
            overall["accept_recommendation"] = "needs_human_tuning"
    overall["confidence"] = overall.get("confidence") if overall.get("confidence") in {"high", "medium", "low"} else "medium"
    result.setdefault("failure_types", [])
    result.setdefault("visual_assessment", {})
    result.setdefault("human_attention_points", [])
    result.setdefault("review_note_draft", "")
    return result


def coerce_score(value: Any) -> int | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0:
        return None
    if numeric <= 2:
        return 2 if numeric >= 1.5 else 1 if numeric >= 0.5 else 0
    if numeric <= 10:
        return 2 if numeric >= 7.5 else 1 if numeric >= 4 else 0
    return None


def apply_visual_safety_caps(result: dict[str, Any]) -> None:
    assessment = result.get("visual_assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    by_dim = {item.get("dimension"): item for item in result.get("rubric_scores", [])}

    if assessment.get("candidate_preferred_over_draft") is False:
        for dim in TASTE_DIMENSIONS:
            by_dim[dim]["score"] = min(by_dim[dim].get("score", 0), 1)
        by_dim["整体人类偏好"]["score"] = 0
    if assessment.get("meaningful_visual_change") is False:
        for dim in TASTE_DIMENSIONS:
            by_dim[dim]["score"] = min(by_dim[dim].get("score", 0), 1)
        by_dim["整体人类偏好"]["score"] = 0
    if assessment.get("candidate_presentable") is False:
        by_dim["整体人类偏好"]["score"] = 0
    if assessment.get("content_preserved") is False:
        by_dim["内容与主题保真"]["score"] = 0
        by_dim["整体人类偏好"]["score"] = min(by_dim["整体人类偏好"].get("score", 0), 1)
    occlusion_severity = assessment.get("remaining_occlusion_severity")
    if occlusion_severity in {"minor", "major"}:
        by_dim["可视性与无遮挡"]["score"] = min(by_dim["可视性与无遮挡"].get("score", 0), 1)
        by_dim["整体人类偏好"]["score"] = min(by_dim["整体人类偏好"].get("score", 0), 1)
    if occlusion_severity == "major":
        by_dim["图像与对象完整性"]["score"] = min(by_dim["图像与对象完整性"].get("score", 0), 1)
        if assessment.get("readability_improved_over_draft") is False:
            by_dim["可视性与无遮挡"]["score"] = 0
            by_dim["整体人类偏好"]["score"] = 0
    if occlusion_severity == "critical" or assessment.get("key_content_usable") is False:
        by_dim["可视性与无遮挡"]["score"] = 0
        by_dim["整体人类偏好"]["score"] = 0
        by_dim["图像与对象完整性"]["score"] = min(by_dim["图像与对象完整性"].get("score", 0), 1)
    if spatial_gate_failed(result):
        by_dim["图片位置、角度与空间关系"]["score"] = 0
        by_dim["构图与视觉平衡"]["score"] = min(by_dim["构图与视觉平衡"].get("score", 0), 1)
        by_dim["整体人类偏好"]["score"] = min(by_dim["整体人类偏好"].get("score", 0), 1)
    if balance_gate_failed(result):
        by_dim["构图与视觉平衡"]["score"] = 0
        by_dim["图片位置、角度与空间关系"]["score"] = min(by_dim["图片位置、角度与空间关系"].get("score", 0), 1)
        by_dim["对齐、间距与留白"]["score"] = min(by_dim["对齐、间距与留白"].get("score", 0), 1)
        by_dim["整体人类偏好"]["score"] = min(by_dim["整体人类偏好"].get("score", 0), 1)

    text = json.dumps(
        {
            "remaining_major_issues": assessment.get("remaining_major_issues", []),
            "new_errors": assessment.get("new_errors", []),
            "failure_types": result.get("failure_types", []),
        },
        ensure_ascii=False,
    )
    if not any(pattern in text for pattern in CRITICAL_PATTERNS):
        return
    for item in result.get("rubric_scores", []):
        if item.get("dimension") in {"可视性与无遮挡", "整体人类偏好"}:
            item["score"] = min(item.get("score", 0), 0)
        elif item.get("dimension") in {"图像与对象完整性", "构图与视觉平衡"}:
            item["score"] = min(item.get("score", 0), 1)


def final_judgement(total: int, max_score: int, usability_score: int, taste_score: int) -> str:
    if usability_score < 6:
        return "可用性未达标"
    if taste_score < 7:
        return "可用但审美未达标"
    ratio = total / max_score if max_score else 0
    if ratio >= 0.9:
        return "审美成熟，可直接展示"
    if ratio >= 0.7:
        return "基本可用，仍需人工微调"
    if ratio >= 0.5:
        return "有局部改善，但审美未达标"
    return "未形成有效视觉改进"


def recommendation(total: int, max_score: int, usability_score: int, taste_score: int) -> str:
    ratio = total / max_score if max_score else 0
    if ratio >= 0.85 and usability_score >= 7 and taste_score >= 10:
        return "accept"
    if ratio >= 0.6 and usability_score >= 6 and taste_score >= 7:
        return "needs_human_tuning"
    return "reject"


def score_100(result: dict[str, Any]) -> int:
    overall = result["overall"]
    raw_score = round(100 * overall["total_score"] / overall["max_score"])
    if overall.get("accept_recommendation") == "reject":
        return min(raw_score, 59)
    if spatial_gate_failed(result):
        return min(raw_score, 79)
    return raw_score


def spatial_gate_failed(result: dict[str, Any]) -> bool:
    assessment = result.get("visual_assessment")
    if not isinstance(assessment, dict):
        return False
    if assessment.get("major_spatial_issues_resolved") is False:
        return True
    if assessment.get("major_image_placement_or_rotation_improved") is False:
        return True
    if assessment.get("remaining_awkward_position_or_rotation") is True:
        return True
    issue_text = json.dumps(
        {
            "remaining_major_issues": assessment.get("remaining_major_issues", []),
            "new_errors": assessment.get("new_errors", []),
        },
        ensure_ascii=False,
    )
    return any(pattern in issue_text for pattern in SPATIAL_PATTERNS)


def balance_gate_failed(result: dict[str, Any]) -> bool:
    assessment = result.get("visual_assessment")
    if not isinstance(assessment, dict):
        return False
    if assessment.get("visual_center_balanced") is False:
        return True
    if assessment.get("excessive_unused_whitespace_remaining") is True:
        return True
    issue_text = json.dumps(
        {
            "remaining_major_issues": assessment.get("remaining_major_issues", []),
            "new_errors": assessment.get("new_errors", []),
        },
        ensure_ascii=False,
    )
    return any(pattern in issue_text for pattern in BALANCE_PATTERNS)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_yaml_config() -> dict[str, Any]:
    path = Path(os.environ.get("LANE_CONFIG_PATH") or _repo_root() / "agentlane.yaml")
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _judge_yaml(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("ppt_visual_repair")
    if not isinstance(section, dict):
        return {}
    judge = section.get("judge")
    return judge if isinstance(judge, dict) else {}
