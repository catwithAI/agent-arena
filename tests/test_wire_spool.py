"""W0-3 验收：spool + blob writer。

覆盖 tasks.md W0-3 验收清单：
- 崩溃模拟（截断尾行）后 finalizer 读完整行并标 partial；
- 并发两个 source 写各自 spool 不互扰；
- blob hash 命名/去重/codec 记录；
- canonical 原子重写。
"""

import gzip
import json
import threading

import pytest

from backend.wire import evidence, paths, spool, writer


def _ev(i: int, attempt_id: str = "att_x") -> dict:
    """合法 WireEvidence（capture_event）：spool writer append 前强制 schema 校验。"""
    return {
        "evidence_id": f"we_{i}",
        "attempt_id": attempt_id,
        "phase": "agent_run",
        "evidence_type": "capture_event",
        "source": {"kind": "capture-events", "instance": "t"},
        "producer": {"name": "test"},
        "time": {"observed_at": "2026-07-13T00:00:00.000Z"},
        "raw_ref": None,
        "correlation_hints": {},
        "capabilities": {},
        "redaction": {"policy": "metadata", "status": "applied"},
        "errors": [],
        "extensions": {},
        "payload": {**evidence.null_payload("capture_event"), "counters": {"seq": i}},
    }


def _seq(record: dict) -> int:
    return record["payload"]["counters"]["seq"]


# ---------- spool：.partial → rename 与逐行 flush ---------------------------

def test_spool_normal_lifecycle(tmp_path):
    final = tmp_path / "native-event.jsonl"
    w = spool.SpoolWriter(final)
    assert w.partial_path.name == "native-event.jsonl.partial"
    w.append(_ev(1))
    w.append(_ev(2))
    # 关闭前只有 .partial，且已写行逐行 flush 可见（SIGKILL 存活性的可观测代理）
    assert not final.exists()
    assert len(w.partial_path.read_bytes().splitlines()) == 2
    closed = w.close()
    assert closed == final and final.exists() and not w.partial_path.exists()
    result = spool.read_spool(final)
    assert [_seq(r) for r in result.records] == [1, 2]
    assert not result.partial and not result.truncated_tail


def test_spool_close_is_idempotent(tmp_path):
    w = spool.SpoolWriter(tmp_path / "s.jsonl")
    w.append(_ev(1))
    assert w.close() == w.close()
    with pytest.raises(spool.SpoolError):
        w.append(_ev(2))


def test_crash_truncated_tail_reads_complete_lines_and_marks_partial(tmp_path):
    """崩溃模拟：进程死在写第三行中途，.partial 残留 + 尾行截断。"""
    final = tmp_path / "native-event.jsonl"
    w = spool.SpoolWriter(final)
    w.append(_ev(1))
    w.append(_ev(2))
    w.abandon()  # 不 rename，模拟没走到 close
    # 再截断尾行：把第三行写一半
    with open(w.partial_path, "ab") as fh:
        fh.write(b'{"evidence_id": "we_3", "payl')
    found = spool.find_spool_file(final)
    assert found == w.partial_path
    result = spool.read_spool(found)
    assert [r["evidence_id"] for r in result.records] == ["we_1", "we_2"]
    assert result.partial and result.truncated_tail


def test_partial_suffix_alone_marks_partial(tmp_path):
    """source 没正常关闭但尾行完整：partial 但无 truncated_tail。"""
    w = spool.SpoolWriter(tmp_path / "s.jsonl")
    w.append(_ev(1))
    w.abandon()
    result = spool.read_spool(w.partial_path)
    assert result.partial and not result.truncated_tail
    assert len(result.records) == 1


def test_corrupt_middle_line_counts_parse_error(tmp_path):
    final = tmp_path / "s.jsonl"
    final.write_text('{"a": 1}\nnot-json\n{"b": 2}\n')
    result = spool.read_spool(final)
    assert len(result.records) == 2
    assert result.parse_errors == 1
    assert not result.partial


def test_oversized_line_rejected_not_truncated(tmp_path):
    w = spool.SpoolWriter(tmp_path / "s.jsonl", max_line_bytes=1024)
    big = _ev(99)
    big["payload"]["message"] = "x" * 4096
    with pytest.raises(spool.SpoolLineTooLarge):
        w.append(big)
    w.append(_ev(1))  # 正常行不受影响
    w.close()
    result = spool.read_spool(tmp_path / "s.jsonl")
    assert len(result.records) == 1 and result.parse_errors == 0


# ---------- append 前校验（design §8.2/§8.3）--------------------------------

def test_append_rejects_non_conforming_dict(tmp_path):
    w = spool.SpoolWriter(tmp_path / "s.jsonl")
    with pytest.raises(spool.SpoolValidationError):
        w.append({"evidence_id": "we_1", "payload": {"free": "form"}})
    bad_phase = _ev(1)
    bad_phase["phase"] = "totally_invalid"
    with pytest.raises(spool.SpoolValidationError):
        w.append(bad_phase)
    w.close()


def test_append_enforces_attempt_ownership(tmp_path):
    w = spool.SpoolWriter(tmp_path / "s.jsonl", expected_attempt_id="att_a")
    w.append(_ev(1, attempt_id="att_a"))
    with pytest.raises(spool.SpoolValidationError):
        w.append(_ev(2, attempt_id="att_b"))
    w.close()


def test_append_enforces_policy_ceiling(tmp_path):
    w = spool.SpoolWriter(tmp_path / "s.jsonl", max_policy="metadata")
    w.append(_ev(1))  # metadata 档
    escalated = _ev(2)
    escalated["redaction"]["policy"] = "full"
    with pytest.raises(spool.SpoolValidationError):
        w.append(escalated)
    w.close()


def test_two_sources_write_independently(tmp_path):
    """并发两个 source 各写各的 spool，互不干扰（R12.3）。"""
    w1 = spool.SpoolWriter(tmp_path / "native-event.jsonl")
    w2 = spool.SpoolWriter(tmp_path / "mcp-stdio-env1.jsonl")

    def pump(w: spool.SpoolWriter, base: int):
        for i in range(50):
            w.append(_ev(base + i))

    t1 = threading.Thread(target=pump, args=(w1, 0))
    t2 = threading.Thread(target=pump, args=(w2, 1000))
    t1.start(), t2.start()
    t1.join(), t2.join()
    w1.close(), w2.close()
    r1 = spool.read_spool(tmp_path / "native-event.jsonl")
    r2 = spool.read_spool(tmp_path / "mcp-stdio-env1.jsonl")
    assert [_seq(r) for r in r1.records] == list(range(50))
    assert [_seq(r) for r in r2.records] == list(range(1000, 1050))
    assert r1.parse_errors == r2.parse_errors == 0


def test_reopen_appends_not_truncates(tmp_path):
    """recovery 场景同 instance 重开：追加而非覆盖已有 .partial。"""
    final = tmp_path / "s.jsonl"
    w = spool.SpoolWriter(final)
    w.append(_ev(1))
    w.abandon()
    w2 = spool.SpoolWriter(final)
    w2.append(_ev(2))
    w2.close()
    result = spool.read_spool(final)
    assert [_seq(r) for r in result.records] == [1, 2]


def test_reopen_after_clean_close_keeps_history(tmp_path):
    """正常关闭（.jsonl 已存在）后同 instance 重开：历史行不得被新 partial 覆盖。"""
    final = tmp_path / "s.jsonl"
    w = spool.SpoolWriter(final)
    w.append(_ev(1))
    w.close()
    w2 = spool.SpoolWriter(final)
    w2.append(_ev(2))
    w2.close()
    result = spool.read_spool(final)
    assert [_seq(r) for r in result.records] == [1, 2]


def test_reopen_with_both_final_and_partial_merges(tmp_path):
    """旧 final + 另一次崩溃残留的 partial 并存：合并为 final→partial 顺序。"""
    final = tmp_path / "s.jsonl"
    w = spool.SpoolWriter(final)
    w.append(_ev(1))
    w.close()
    w2 = spool.SpoolWriter(final)
    w2.append(_ev(2))
    w2.abandon()  # 崩溃：留 .partial
    # 手工恢复 final 与 partial 并存的病态（模拟历史遗留）
    # 此时磁盘上只有 .partial（w2 init 已把 final 并入）；再造一个 final
    (tmp_path / "s.jsonl").write_bytes(b"")
    w3 = spool.SpoolWriter(final)
    w3.append(_ev(3))
    w3.close()
    result = spool.read_spool(final)
    assert [_seq(r) for r in result.records] == [1, 2, 3]


def test_spool_filename_no_collision():
    """kind/instance 用 @ 分隔：a-b + c 与 a + b-c 必须不同路径。"""
    p1 = paths.source_spool_file(paths.Path("/d"), "att_1", "a-b", "c")
    p2 = paths.source_spool_file(paths.Path("/d"), "att_1", "a", "b-c")
    assert p1 != p2
    assert p1.name == "a-b@c.jsonl" and p2.name == "a@b-c.jsonl"


# ---------- writer：canonical 原子重写 -------------------------------------

def test_atomic_jsonl_rewrite(tmp_path):
    dest = tmp_path / "wire.jsonl"
    n = writer.atomic_write_jsonl(dest, [{"a": 1}, {"b": 2}])
    assert n == 2
    writer.atomic_write_jsonl(dest, [{"a": 1}])  # 重写不追加
    lines = dest.read_text().splitlines()
    assert lines == ['{"a":1}']
    assert not list(tmp_path.glob("*.tmp"))  # 无临时文件残留


def test_atomic_json_manifest(tmp_path):
    dest = tmp_path / "wire-manifest.json"
    writer.atomic_write_json(dest, {"status": "complete"})
    assert json.loads(dest.read_text())["status"] == "complete"


# ---------- blob：content-addressed 命名 / 去重 / codec ----------------------

def test_blob_hash_naming_and_roundtrip(tmp_path):
    bw = writer.BlobWriter(tmp_path, "att_1")
    ref = bw.write_json({"messages": [{"role": "user", "content": "hi"}]})
    assert paths.BLOB_REF_RE.match(ref.ref)
    assert ref.ref == f"sha256-{ref.sha256}.json.gz"
    assert ref.codec == "gz" and not ref.deduplicated
    assert ref.raw_bytes > 0 and ref.stored_bytes > 0
    # hash 是对未压缩 JSON 字节算的
    import hashlib

    raw = bw.read_bytes(ref.ref)
    assert hashlib.sha256(raw).hexdigest() == ref.sha256
    assert json.loads(raw)["messages"][0]["content"] == "hi"


def test_blob_dedup_same_content(tmp_path):
    bw = writer.BlobWriter(tmp_path, "att_1")
    a = bw.write_json({"x": 1})
    b = bw.write_json({"x": 1})
    assert a.ref == b.ref
    assert not a.deduplicated and b.deduplicated
    blob_files = list((tmp_path / "attempts" / "att_1" / "wire-blobs").iterdir())
    assert len(blob_files) == 1


def test_blob_different_content_different_ref(tmp_path):
    bw = writer.BlobWriter(tmp_path, "att_1")
    assert bw.write_json({"x": 1}).ref != bw.write_json({"x": 2}).ref


def test_blob_file_is_gzip_on_disk(tmp_path):
    bw = writer.BlobWriter(tmp_path, "att_1")
    ref = bw.write_json({"large": "z" * 4096})
    on_disk = (tmp_path / "attempts" / "att_1" / "wire-blobs" / ref.ref).read_bytes()
    assert on_disk[:2] == b"\x1f\x8b"  # gzip magic
    assert ref.stored_bytes == len(on_disk) < ref.raw_bytes
    assert gzip.decompress(on_disk) == bw.read_bytes(ref.ref)


def test_blob_read_rejects_bad_ref(tmp_path):
    bw = writer.BlobWriter(tmp_path, "att_1")
    bw.write_json({"x": 1})
    with pytest.raises(FileNotFoundError):
        bw.read_bytes("../wire.jsonl")
    with pytest.raises(FileNotFoundError):
        bw.read_bytes("sha256-" + "f" * 64 + ".json.gz")
