"""W0-1 验收：canonical models + WireEvidence v1 + ID 生成。

覆盖 tasks.md W0-1 验收清单：
- 全类型序列化 round-trip；
- 旧版本文件多字段/缺可选字段可读（R1.6）；
- 零值与 null 语义区分（R1.4）；
- evidence JSON Schema 校验通过与拒绝未知字段；
- 同输入 ID 生成幂等。
"""

import json

import pytest
from pydantic import ValidationError

from backend.wire import evidence, ids, models


# ---------- canonical record round-trip ----------------------------------

def _minimal_record(record_type: str, data: dict) -> models.WireRecord:
    return models.WireRecord(
        record_id="wr_1",
        record_type=record_type,
        attempt_id="att_x",
        phase="agent_run",
        source=models.SourceRef(kind="native-event", instance="claude-code"),
        data=data,
    )


@pytest.mark.parametrize("record_type", list(models.DATA_MODELS.keys()))
def test_all_record_types_round_trip(record_type):
    rec = _minimal_record(record_type, {})
    restored = models.WireRecord.model_validate_json(rec.model_dump_json())
    assert restored.record_type == record_type
    assert restored.record_id == "wr_1"
    assert restored.attempt_id == "att_x"


@pytest.mark.parametrize(
    "data_model,sample",
    [
        (models.LlmCallData, {"protocol": "anthropic-messages", "model_resolved": "glm"}),
        (models.HttpExchangeData, {"hop_id": "hop_1", "status_code": 200}),
        (models.StreamChunkData, {"hop_id": "hop_1", "sequence": 3}),
        (models.McpFrameData, {"direction": "client-to-server", "method": "tools/call"}),
        (models.CaptureEventData, {"event": "ready", "source_instance": "cc"}),
        (models.ContextCompactionData, {"strategy": "full-summary"}),
    ],
)
def test_data_payload_round_trip(data_model, sample):
    obj = data_model.model_validate(sample)
    restored = data_model.model_validate_json(obj.model_dump_json())
    assert restored.model_dump() == obj.model_dump()


# ---------- R1.6 向后兼容读取 --------------------------------------------

def test_canonical_reader_tolerates_unknown_fields():
    """新版本写的多余字段，旧 reader 必须能读而不是拒绝。"""
    raw = {
        "schema_version": "lane-wire-v1",
        "record_id": "wr_1",
        "record_type": "llm_call",
        "attempt_id": "att_x",
        "phase": "agent_run",
        "source": {"kind": "native-event", "instance": "cc"},
        "data": {},
        "future_top_level_field": {"nested": 1},  # 未来新增
    }
    rec = models.WireRecord.model_validate(raw)
    assert rec.record_id == "wr_1"
    # 未知字段被保留（extra=allow），不丢失
    assert rec.model_dump().get("future_top_level_field") == {"nested": 1}


def test_optional_fields_missing_is_readable():
    """缺可选字段（time/correlation/provenance）时用默认值，不报错。"""
    raw = {
        "record_id": "wr_2",
        "record_type": "capture_event",
        "attempt_id": "att_x",
        "phase": "agent_run",
        "source": {"kind": "lane-http", "instance": "proxy-1"},
    }
    rec = models.WireRecord.model_validate(raw)
    assert rec.time.timestamp is None
    assert rec.correlation.agent_id == "main"
    assert rec.provenance == []


# ---------- R1.4 零值 vs null 语义区分 -----------------------------------

def test_zero_and_null_usage_are_distinct():
    """usage=0 是真实观测（无 token），usage=null 是未观测——不能混淆。"""
    observed_zero = models.Usage(input_tokens=0)
    unobserved = models.Usage(input_tokens=None)
    assert observed_zero.input_tokens == 0
    assert unobserved.input_tokens is None
    # 序列化后仍可区分
    assert json.loads(observed_zero.model_dump_json())["input_tokens"] == 0
    assert json.loads(unobserved.model_dump_json())["input_tokens"] is None


# ---------- ID 幂等 ------------------------------------------------------

def test_evidence_id_deterministic():
    kw = dict(
        attempt_id="att_x",
        source_kind="native-event",
        source_instance="claude-code",
        raw_ref="events.jsonl:17",
    )
    assert ids.evidence_id(**kw) == ids.evidence_id(**kw)
    assert ids.evidence_id(**kw).startswith("we_")


def test_logical_call_id_deterministic_and_anchor_sensitive():
    a = ids.logical_call_id(attempt_id="att_x", call_anchor="msg_1")
    b = ids.logical_call_id(attempt_id="att_x", call_anchor="msg_1")
    c = ids.logical_call_id(attempt_id="att_x", call_anchor="msg_2")
    assert a == b and a != c
    assert a.startswith("lc_")


def test_id_separator_avoids_concatenation_collision():
    """'a'+'b/c' 与 'a/b'+'c' 必须得到不同 evidence_id。"""
    id1 = ids.evidence_id(
        attempt_id="att", source_kind="a", source_instance="b/c", raw_ref="r"
    )
    id2 = ids.evidence_id(
        attempt_id="att", source_kind="a/b", source_instance="c", raw_ref="r"
    )
    assert id1 != id2


# ---------- evidence JSON Schema 校验 ------------------------------------

def _valid_evidence_dict(evidence_type: str = "http_exchange") -> dict:
    """最小合法 evidence：所有 required 字段显式给出，payload 全 null 模板。"""
    return {
        "evidence_id": "we_1",
        "attempt_id": "att_x",
        "phase": "agent_run",
        "evidence_type": evidence_type,
        "source": {"kind": "lane-http", "instance": "proxy-1"},
        "producer": {"name": "lane-http"},
        "time": {"observed_at": "2026-07-10T00:00:00.000Z"},
        "raw_ref": None,
        "correlation_hints": {},
        "capabilities": {},
        "redaction": {"policy": "metadata", "status": "applied"},
        "errors": [],
        "extensions": {},
        "payload": evidence.null_payload(evidence_type),
    }


def test_valid_evidence_accepted():
    ev = evidence.validate_evidence(_valid_evidence_dict())
    assert ev.evidence_schema_version == evidence.EVIDENCE_SCHEMA_VERSION
    assert isinstance(ev, evidence.HttpExchangeEvidence)


def test_evidence_rejects_unknown_top_level_field():
    bad = _valid_evidence_dict()
    bad["rogue_field"] = 1
    with pytest.raises(ValidationError):
        evidence.validate_evidence(bad)


def test_evidence_rejects_unknown_nested_field():
    bad = _valid_evidence_dict()
    bad["source"]["rogue"] = 1
    with pytest.raises(ValidationError):
        evidence.validate_evidence(bad)


def test_evidence_rejects_invalid_phase_and_schema_version():
    bad = _valid_evidence_dict()
    bad["phase"] = "totally_invalid"
    with pytest.raises(ValidationError):
        evidence.validate_evidence(bad)
    bad2 = _valid_evidence_dict()
    bad2["evidence_schema_version"] = "lane-wire-evidence-v999"
    with pytest.raises(ValidationError):
        evidence.validate_evidence(bad2)
    # unknown phase 合法但必须显式写 "unknown"
    ok = _valid_evidence_dict()
    ok["phase"] = "unknown"
    assert evidence.validate_evidence(ok).phase == "unknown"


def test_evidence_payload_is_versioned_variant_not_any_dict():
    """payload 由 evidence_type 的 versioned variant 封闭定义，不装任意私有字段。"""
    bad = _valid_evidence_dict()
    bad["payload"]["unexpected"] = "rejected"
    with pytest.raises(ValidationError):
        evidence.validate_evidence(bad)
    ok = _valid_evidence_dict()
    ok["payload"].update({"method": "POST", "status_code": 429, "streamed": True})
    ev = evidence.validate_evidence(ok)
    assert ev.payload.status_code == 429
    # 未知 evidence_type 整体拒绝
    bad2 = _valid_evidence_dict()
    bad2["evidence_type"] = "mystery_type"
    with pytest.raises(ValidationError):
        evidence.validate_evidence(bad2)


def test_evidence_min_fields_required_but_nullable():
    """最小字段不可省略：不可得时必须显式 null——省略无法区分 producer 版本
    过旧/实现遗漏/不可观测三种情况（design §8.2）。"""
    # 空 payload（全部省略）拒绝
    bad = _valid_evidence_dict()
    bad["payload"] = {}
    with pytest.raises(ValidationError):
        evidence.validate_evidence(bad)
    # 缺任意一个最小字段拒绝
    bad2 = _valid_evidence_dict()
    del bad2["payload"]["status_code"]
    with pytest.raises(ValidationError):
        evidence.validate_evidence(bad2)
    # envelope 的 raw_ref/correlation_hints/capabilities/errors/extensions 同样不可省略
    for key in ("raw_ref", "correlation_hints", "capabilities", "errors", "extensions"):
        bad3 = _valid_evidence_dict()
        del bad3[key]
        with pytest.raises(ValidationError):
            evidence.validate_evidence(bad3)


def test_evidence_redaction_policy_is_enum():
    bad = _valid_evidence_dict()
    bad["redaction"]["policy"] = "save-all-secrets"
    with pytest.raises(ValidationError):
        evidence.validate_evidence(bad)


def test_evidence_all_seven_variants_round_trip():
    for etype, cls in evidence.EVIDENCE_VARIANTS.items():
        d = _valid_evidence_dict(etype)
        ev = evidence.validate_evidence(d)
        assert isinstance(ev, cls)
        again = evidence.validate_evidence(json.loads(ev.model_dump_json()))
        assert again == ev


def test_evidence_extensions_must_be_namespaced():
    ok = _valid_evidence_dict()
    ok["extensions"] = {"x-vendor.session": "s1"}
    ev = evidence.validate_evidence(ok)
    assert ev.extensions == {"x-vendor.session": "s1"}
    bad = _valid_evidence_dict()
    bad["extensions"] = {"not_namespaced": "rejected"}
    with pytest.raises(ValidationError):
        evidence.validate_evidence(bad)


def test_evidence_redaction_supports_hash_algorithm():
    ok = _valid_evidence_dict()
    ok["redaction"] = {
        "policy": "metadata", "status": "applied",
        "hash_algorithm": "sha256", "hash_domain": "raw-bytes-v1",
    }
    ev = evidence.validate_evidence(ok)
    assert ev.redaction.hash_algorithm == "sha256"
    ok["redaction"]["hash_algorithm"] = "md5"
    with pytest.raises(ValidationError):
        evidence.validate_evidence(ok)


def test_evidence_phase_matches_canonical_phase():
    """evidence.Phase 与 models.Phase 是同一集合（两处显式列出，防漂移）。"""
    from typing import get_args

    assert set(get_args(evidence.Phase)) == set(get_args(models.Phase))


def test_json_schema_exported_and_forbids_additional():
    schema = evidence.evidence_json_schema()
    # discriminated union → oneOf/anyOf + discriminator；每个 variant 及其
    # $defs 都必须 additionalProperties=false，Go/Node producer 才被真正约束。
    assert schema.get("oneOf") or schema.get("anyOf")
    assert schema.get("discriminator", {}).get("propertyName") == "evidence_type"
    defs = schema.get("$defs", {})
    assert defs, "期望 $defs 内含 envelope/variant 定义"
    object_defs = [d for d in defs.values() if d.get("type") == "object"]
    assert object_defs
    for d in object_defs:
        # dict[str, ...] 自由字段（counters/capabilities/extensions）除外，
        # 其余对象定义一律封闭。
        if "properties" in d:
            assert d.get("additionalProperties") is False, d.get("title")
