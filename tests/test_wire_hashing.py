"""W0-1 验收：跨 source semantic hash（design §10.5）。

覆盖 tasks.md W0-1 验收清单：
- key 序、JSON 空白、Unicode 转义不影响 semantic hash；
- NFC 后 key 冲突时拒绝生成 canonical semantic hash；
- tool 列表按 name 排序；
- raw-bytes hash 与 semantic hash 是不同 domain。
"""

import json

import pytest

from backend.wire import hashing


def test_key_order_does_not_affect_hash():
    a = hashing.semantic_hash("tools", {"name": "f", "description": "d", "input_schema": {}})
    b = hashing.semantic_hash("tools", {"input_schema": {}, "description": "d", "name": "f"})
    assert a == b


def test_json_whitespace_does_not_affect_hash():
    """两个语义相同、只是空白不同的 JSON 结构 → 同 hash。

    (semantic_hash 吃的是已解析的 Python 对象，等价于反序列化后再 JCS，
    因此源 JSON 文本的空白天然不影响；此测试锚定该性质。)
    """
    obj1 = json.loads('{"a":1,   "b":[1,2]}')
    obj2 = json.loads('{ "a" : 1 , "b" : [ 1 , 2 ] }')
    assert hashing.semantic_hash("system", obj1) == hashing.semantic_hash("system", obj2)


def test_unicode_escape_forms_hash_equal_after_nfc():
    """e-acute 的两种编码（预组合 U+00E9 vs e+U+0301 组合重音）NFC 后相等。

    用 \\u 转义显式构造，避免源文件被编辑器/FS 统一规范化导致测试失真。
    """
    precomposed = "caf\u00e9"       # café，单码点 U+00E9
    decomposed = "cafe\u0301"       # café，e + 组合重音 U+0301
    assert precomposed != decomposed   # 字节层不同
    h1 = hashing.semantic_hash("system", [{"type": "text", "text": precomposed}])
    h2 = hashing.semantic_hash("system", [{"type": "text", "text": decomposed}])
    assert h1 == h2                    # 语义层相同


def test_nfc_key_collision_raises():
    """两个不同码点序列的 key，NFC 后规范化成同一字符串 → 拒绝生成 hash。"""
    collide = {"caf\u00e9": 1, "cafe\u0301": 2}
    with pytest.raises(hashing.SemanticHashError):
        hashing.semantic_hash("system", collide)


def test_tools_sorted_by_name():
    tools_a = [
        {"name": "zebra", "description": "z", "input_schema": {}},
        {"name": "alpha", "description": "a", "input_schema": {}},
    ]
    tools_b = list(reversed(tools_a))
    sorted_a = hashing.sort_tools_ir(tools_a)
    sorted_b = hashing.sort_tools_ir(tools_b)
    assert [t["name"] for t in sorted_a] == ["alpha", "zebra"]
    # 排序后两种输入顺序得到同一 hash
    assert hashing.semantic_hash("tools", sorted_a) == hashing.semantic_hash("tools", sorted_b)


def test_raw_bytes_hash_is_different_domain():
    payload = b'{"a":1}'
    rb = hashing.raw_bytes_hash(payload)
    sem = hashing.semantic_hash("system", {"a": 1})
    # 都是 64 位 hex 但语义不同 domain，值不应巧合相等
    assert len(rb) == 64 and len(sem) == 64
    assert rb != sem


def test_different_content_different_hash():
    h1 = hashing.semantic_hash("system", [{"type": "text", "text": "hello"}])
    h2 = hashing.semantic_hash("system", [{"type": "text", "text": "world"}])
    assert h1 != h2


# ---------- W4-2：三协议 parser → 公共 semantic IR（cross-protocol golden）----
#
# 同一逻辑请求/响应在 Anthropic Messages / Chat Completions / Responses 三协议下
# 表达不同，但映射到 §10.5 公共 messages IR 后必须产出**同一** semantic hash。
# key 序 / 空白 / unicode 转义差异不影响；工具按 name 排序；无完整语义内容时
# 只写 bytes/null、不伪造 semantic hash。

from backend.wire.sources import parse as _parse  # noqa: E402


def _req(kind, obj):
    return _parse.parse_request(kind, json.dumps(obj, ensure_ascii=False).encode())


# 同一逻辑对话：system="be helpful"，user="héllo 世界"，一个 tool "search"。
_ANTHROPIC_REQ = {
    "model": "m",
    "system": "be helpful",
    "messages": [{"role": "user", "content": "héllo 世界"}],
    "tools": [{"name": "search", "description": "d", "input_schema": {"type": "object"}}],
}
_CHAT_REQ = {
    "model": "m",
    "messages": [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "héllo 世界"},
    ],
    "tools": [{"type": "function", "function": {
        "name": "search", "description": "d", "parameters": {"type": "object"}}}],
}
_RESPONSES_REQ = {
    "model": "m",
    "instructions": "be helpful",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "héllo 世界"}]}],
    "tools": [{"type": "function", "name": "search",
               "description": "d", "parameters": {"type": "object"}}],
}


def test_cross_protocol_request_semantic_hash_equal():
    a = _req("anthropic-messages", _ANTHROPIC_REQ)
    c = _req("openai-chat-completions", _CHAT_REQ)
    r = _req("openai-responses", _RESPONSES_REQ)
    assert a.messages_hash == c.messages_hash == r.messages_hash is not None
    assert a.system_hash == c.system_hash == r.system_hash is not None
    assert a.tools_hash == c.tools_hash == r.tools_hash is not None
    assert a.hash_domain == c.hash_domain == r.hash_domain == hashing.DOMAIN_SEMANTIC


def test_cross_protocol_unicode_escape_does_not_affect_hash():
    # 同内容，一个用原字符、一个用 \uXXXX 转义——JSON 解析后等价。
    a1 = _req("anthropic-messages", _ANTHROPIC_REQ)
    a2 = _parse.parse_request(
        "anthropic-messages",
        json.dumps(_ANTHROPIC_REQ, ensure_ascii=True).encode(),  # \uXXXX 转义形式
    )
    assert a1.messages_hash == a2.messages_hash


def test_tools_sorted_by_name_across_protocols():
    # 工具声明顺序不同，hash 相同（按 name 排序）。
    two_tools_anthropic = {
        "model": "m", "messages": [{"role": "user", "content": "x"}],
        "tools": [
            {"name": "zebra", "description": "", "input_schema": {}},
            {"name": "apple", "description": "", "input_schema": {}},
        ],
    }
    reordered = {
        "model": "m", "messages": [{"role": "user", "content": "x"}],
        "tools": [
            {"name": "apple", "description": "", "input_schema": {}},
            {"name": "zebra", "description": "", "input_schema": {}},
        ],
    }
    assert _req("anthropic-messages", two_tools_anthropic).tools_hash == \
        _req("anthropic-messages", reordered).tools_hash


def test_cross_protocol_response_content_hash_equal():
    ra = {"content": [{"type": "text", "text": "hello"}]}
    rc = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
    rr = {"output": [{"content": [{"type": "output_text", "text": "hello"}]}]}
    pa = _parse.parse_response("p", json.dumps(ra).encode())
    pc = _parse.parse_response("p", json.dumps(rc).encode())
    pr = _parse.parse_response("p", json.dumps(rr).encode())
    assert pa.content_hash == pc.content_hash == pr.content_hash is not None
    assert pa.hash_domain == hashing.DOMAIN_SEMANTIC


def test_non_json_body_no_fake_semantic_hash():
    # SSE / 非 JSON body：只记 bytes，绝不伪造 semantic hash（hash_domain=None）。
    r = _parse.parse_request("anthropic-messages", b"data: not json\n\n")
    assert r is not None
    assert r.messages_hash is None
    assert r.hash_domain is None
    assert r.message_bytes == len(b"data: not json\n\n")

    resp = _parse.parse_response("p", b"data: [DONE]\n\n")
    assert resp.content_hash is None
    assert resp.hash_domain is None


def test_unknown_protocol_degrades_to_bytes_only():
    r = _parse.parse_request("some-future-protocol", json.dumps({"model": "m"}).encode())
    assert r is not None
    assert r.messages_hash is None and r.hash_domain is None
    assert r.model == "m"  # 仍尽力提取 model


def test_tool_call_array_args_normalized_across_protocols():
    # 评审 m8：tool_call arguments 是 JSON 数组时，Anthropic(input) 与
    # Chat(function.arguments 字符串) 两路径必须归一到同一形状 → 同 hash。
    anthropic = {"model": "m", "messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "name": "f", "input": [1, 2, 3]}]}]}
    chat = {"model": "m", "messages": [{"role": "assistant", "content": None,
        "tool_calls": [{"function": {"name": "f", "arguments": "[1, 2, 3]"}}]}]}
    a = _req("anthropic-messages", anthropic)
    c = _req("openai-chat-completions", chat)
    assert a.messages_hash == c.messages_hash is not None
