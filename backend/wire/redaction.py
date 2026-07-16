"""Redaction pipeline（design §16.2，R11）。

写盘前的脱敏是硬边界：secret 必须在进入 spool/blob 之前被移除（R11.2），
redactor 自身抛异常时丢弃 payload 只留 metadata + ``redaction_failed``
（R11.3），绝不能 fallback 到未脱敏原文。

三层规则：

1. header 黑名单——五类敏感 header 的值永不落盘；
2. JSON key pattern——递归匹配 ``api_key|token|secret|password|authorization|cookie``
   （大小写不敏感），命中 key 的整个值替换为占位符；
3. 自由文本 secret pattern——payload 字符串值和日志/错误消息里的
   ``sk-...`` / ``Bearer ...`` / AWS key / JWT 等形状。

``scrub_text`` 同时供日志和错误消息使用（R11.9，评审 m7）：parse error、
manifest failure_reason 等任何可能内嵌请求片段的文本都必须先过它。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

REDACTED = "[REDACTED]"

# design §16.2：永不持久化的 header（小写比较）。
BLOCKED_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
    }
)

# design §16.2 默认 JSON key pattern，大小写不敏感；命中即整值替换。
DEFAULT_KEY_PATTERN = re.compile(
    r"api[_-]?key|token|secret|password|authorization|cookie", re.IGNORECASE
)

# 自由文本 secret 形状。保守起见宁可多替换：这里的输出只用于观测展示，
# 误伤可读性的代价远小于泄漏。
TEXT_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI/Anthropic 风格 key：sk- 前缀
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    # Bearer/Basic 凭据
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9\-._~+/=]{8,}", re.IGNORECASE),
    # AWS access key id
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # GitHub token
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    # JWT（三段 base64url）
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
    # 显式 key=value / key: value 形式的赋值（值部分替换）
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;\"']{6,}"
    ),
)


def scrub_text(text: str) -> str:
    """自由文本 secret scrub——payload 字符串、日志、错误消息共用（R11.9）。"""
    for pattern in TEXT_SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """header 黑名单：命中的 header 值替换为占位符，key 保留供 metadata 观测。

    非命中 header 的值仍过 ``scrub_text``（自定义 header 可能内嵌 token）。
    """
    out: dict[str, Any] = {}
    for key, value in headers.items():
        if key.lower() in BLOCKED_HEADERS or DEFAULT_KEY_PATTERN.search(key):
            out[key] = REDACTED
        elif isinstance(value, str):
            out[key] = scrub_text(value)
        else:
            out[key] = value
    return out


def redact_json(
    value: Any, *, extra_key_patterns: tuple[re.Pattern[str], ...] = ()
) -> Any:
    """递归脱敏 JSON 结构：命中 key pattern 的值整体替换，字符串值过文本 scrub。

    ``extra_key_patterns`` 是可配置的追加规则（design §16.2 "configurable
    JSON path rules"）——只能追加收紧，不能移除默认规则。
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if DEFAULT_KEY_PATTERN.search(key_str) or any(
                p.search(key_str) for p in extra_key_patterns
            ):
                out[k] = REDACTED
            else:
                out[k] = redact_json(v, extra_key_patterns=extra_key_patterns)
        return out
    if isinstance(value, list):
        return [redact_json(v, extra_key_patterns=extra_key_patterns) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


RedactionStatus = Literal["applied", "skipped", "failed"]


@dataclass
class RedactionResult:
    """``safe_redact_payload`` 的结果，与 ``EvidenceRedaction.status`` 对齐。

    - applied：payload 已脱敏，可落盘；
    - skipped：policy 不落 payload（off/metadata），payload 为 None；
    - failed：redactor 异常，payload 已丢弃（R11.3），错误信息本身已 scrub。
    """

    payload: Any = None
    status: RedactionStatus = "applied"
    error: str | None = None
    flags: list[str] = field(default_factory=list)


def safe_redact_payload(
    payload: Any,
    *,
    policy: str,
    extra_key_patterns: tuple[re.Pattern[str], ...] = (),
) -> RedactionResult:
    """policy 感知的 payload 脱敏总入口。

    off/metadata 档不落 payload（R11：metadata 只留 endpoint/size/timing/
    usage/hash），parsed/full 档返回脱敏后的结构。任何异常都收敛为
    metadata-only + ``redaction_failed``，异常消息先过 scrub 再保留（R11.9）。
    """
    if policy in ("off", "metadata"):
        return RedactionResult(payload=None, status="skipped")
    try:
        cleaned = redact_json(payload, extra_key_patterns=extra_key_patterns)
        return RedactionResult(payload=cleaned, status="applied")
    except Exception as exc:  # noqa: BLE001 —— 任何失败都不能让原文落盘
        return RedactionResult(
            payload=None,
            status="failed",
            error=scrub_text(f"{type(exc).__name__}: {exc}"),
            flags=["redaction_failed"],
        )
