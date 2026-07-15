"""W7-1 Office preview contract and hostile OOXML boundary tests."""

from __future__ import annotations

import zipfile
import threading
import time
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest
from fastapi import HTTPException

from backend import runtime_state
from backend.api import _resolve_artifact_path
from backend.artifact_preview import PREVIEW_CONTRACT_VERSION, inspect_artifact


RUN_ID = "run_office"
ATTEMPT_ID = "att_office"


@pytest.fixture(autouse=True)
def _seed_run_attempt(test_client):
    """Every W7 endpoint is authorized by the URL run→attempt relation."""
    from backend.db import _open_sync

    state = runtime_state.get()
    with _open_sync(state.db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(id, env_name, prompt, created_at) "
            "VALUES('task_office', 'office-preview', 'p', 'now')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO runs(id, task_id, env_name, status, created_at) "
            "VALUES(?, 'task_office', 'office-preview', 'completed', 'now')",
            (RUN_ID,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO attempts(id, run_id, task_id, env_name, agent_name, status, "
            "session_id, session_token_hash, created_at) "
            "VALUES(?, ?, 'task_office', 'office-preview', 'claude-code', 'completed', "
            "'sess-office', 'h', 'now')",
            (ATTEMPT_ID, RUN_ID),
        )
        conn.commit()


def _workspace(attempt_id: str = ATTEMPT_ID) -> Path:
    root = runtime_state.get().data_path / "attempts" / attempt_id / "skill_workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ooxml(
    path: Path,
    marker: str,
    *,
    members: dict[str, bytes] | None = None,
) -> bytes:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr(marker, b"<root/>")
        for name, body in (members or {}).items():
            archive.writestr(name, body)
    return path.read_bytes()


def _xlsx(path: Path, *, unsafe_shared_strings: bool = False) -> bytes:
    """Small but structurally valid workbook; formula cached values are fixtures, not evaluated."""
    members = {
        "[Content_Types].xml": b"<Types/>",
        "xl/workbook.xml": (
            b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            b'<sheets><sheet name="Summary" sheetId="1" r:id="rId1"/>'
            b'<sheet name="Data" sheetId="2" r:id="rId2"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
            b'<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
            b'</Relationships>'
        ),
        "xl/sharedStrings.xml": (
            b'<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///etc/passwd">]>'
            b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            b'<si><t>&leak;</t></si></sst>'
            if unsafe_shared_strings else (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<si><t>项目</t></si><si><t>收入</t></si><si><t>测试</t></si></sst>'
            ).encode("utf-8")
        ),
        "xl/styles.xml": (
            b'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            b'<numFmts count="1"><numFmt numFmtId="164" formatCode="\xc2\xa5#,##0.00"/></numFmts>'
            b'<cellXfs count="4"><xf numFmtId="0"/><xf numFmtId="10"/>'
            b'<xf numFmtId="14"/><xf numFmtId="164"/></cellXfs>'
            b'</styleSheet>'
        ),
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<dimension ref="A1:E3"/><sheetViews><sheetView><pane state="frozen" '
            'ySplit="1" topLeftCell="A2"/></sheetView></sheetViews>'
            '<cols><col min="2" max="2" width="18"/></cols><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" s="1"><v>0.125</v></c>'
            '<c r="C2"><f>SUM(B2:B2)</f><v>0</v></c><c r="D2" s="2"><v>45292</v></c>'
            '<c r="E2" s="3"><v>1234.5</v></c></row>'
            '<row r="3" hidden="1"><c r="A3" t="inlineStr"><is><t>隐藏</t></is></c>'
            '<c r="B3" t="b"><v>1</v></c></row></sheetData>'
            '<mergeCells count="1"><mergeCell ref="A1:A2"/></mergeCells></worksheet>'
        ).encode("utf-8"),
        "xl/worksheets/sheet2.xml": (
            b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            b'<dimension ref="A1:A1"/><sheetData><row r="1"><c r="A1" t="str">'
            b'<v>second</v></c></row></sheetData></worksheet>'
        ),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return path.read_bytes()


def _pptx(path: Path) -> bytes:
    members = {
        "[Content_Types].xml": b"<Types/>",
        "ppt/presentation.xml": (
            b'<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            b'<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
            b'<p:sldSz cx="12192000" cy="6858000"/></p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" Target="slides/slide1.xml"/></Relationships>'
        ),
        "ppt/slides/slide1.xml": (
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="1" name="Title"/>'
            '<p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="1000000" y="500000"/>'
            '<a:ext cx="10000000" cy="1000000"/></a:xfrm></p:spPr>'
            '<p:txBody><a:bodyPr/><a:p><a:r><a:rPr sz="3200"/>'
            '<a:t>季度总结</a:t></a:r></a:p></p:txBody></p:sp>'
            '<p:pic><p:nvPicPr><p:cNvPr id="2" name="Image"/><p:cNvPicPr/><p:nvPr/>'
            '</p:nvPicPr><p:blipFill><a:blip r:embed="rIdImage"/></p:blipFill>'
            '<p:spPr><a:xfrm><a:off x="1000000" y="2000000"/>'
            '<a:ext cx="3000000" cy="2000000"/></a:xfrm></p:spPr></p:pic>'
            '</p:spTree></p:cSld></p:sld>'
        ).encode("utf-8"),
        "ppt/slides/_rels/slide1.xml.rels": (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rIdImage" Target="../media/image1.png"/></Relationships>'
        ),
        "ppt/media/image1.png": b"\x89PNG\r\n\x1a\nfixture",
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return path.read_bytes()


def _docx(path: Path) -> bytes:
    members = {
        "[Content_Types].xml": b"<Types/>",
        "word/document.xml": (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            '<w:r><w:rPr><w:b/></w:rPr><w:t>执行摘要</w:t></w:r></w:p>'
            '<w:p><w:hyperlink r:id="rId1"><w:r><w:t>安全链接</w:t></w:r></w:hyperlink>'
            '<w:hyperlink r:id="rId2"><w:r><w:t>危险链接</w:t></w:r></w:hyperlink></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>指标</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr></w:body></w:document>'
        ).encode("utf-8"),
        "word/styles.xml": (
            b'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b'<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
            b'</w:style></w:styles>'
        ),
        "word/header1.xml": (
            '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:p><w:r><w:t>机密报告</w:t></w:r></w:p></w:hdr>'
        ).encode("utf-8"),
        "word/footer1.xml": (
            '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:p><w:r><w:t>第 1 页</w:t></w:r></w:p></w:ftr>'
        ).encode("utf-8"),
        "word/_rels/document.xml.rels": (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" TargetMode="External" Target="https://example.test/"/>'
            b'<Relationship Id="rId2" TargetMode="External" Target="javascript:alert(1)"/>'
            b'</Relationships>'
        ),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return path.read_bytes()


def _many_slide_pptx(path: Path, count: int) -> None:
    ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i}"/>' for i in range(1, count + 1))
    rels = "".join(f'<Relationship Id="rId{i}" Target="slides/slide{i}.xml"/>' for i in range(1, count + 1))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "ppt/presentation.xml",
            ('<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
             'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
             f'<p:sldIdLst>{ids}</p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/>'
             '</p:presentation>'),
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             f'{rels}</Relationships>'),
        )
        for i in range(1, count + 1):
            archive.writestr(
                f"ppt/slides/slide{i}.xml",
                ('<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
                 '<p:sp><p:nvSpPr><p:cNvPr id="1" name="Text"/><p:cNvSpPr/><p:nvPr/>'
                 '</p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:p><a:r>'
                 f'<a:t>第 {i} 页</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'),
            )


def _ten_k_xlsx(path: Path) -> None:
    rows = "".join(
        f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>row-{i}</t></is></c></row>'
        for i in range(1, 10_002)
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Large" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<dimension ref="A1:A10001"/><sheetData>{rows}</sheetData></worksheet>',
        )


@pytest.mark.parametrize(
    ("name", "marker", "expected_type", "count_key"),
    [
        ("deck.pptx", "ppt/presentation.xml", "presentation", "slides"),
        ("report.docx", "word/document.xml", "document", "pages"),
        ("book.xlsx", "xl/workbook.xml", "spreadsheet", "sheets"),
    ],
)
def test_descriptor_classifies_ooxml_by_content(
    test_client, name, marker, expected_type, count_key
):
    path = _workspace() / name
    members = {
        "ppt/slides/slide1.xml": b"<slide/>",
        "xl/worksheets/sheet1.xml": b"<sheet/>",
    }
    _ooxml(path, marker, members=members)

    listing = test_client.get(f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts").json()
    listed = next(f for step in listing for f in step["files"] if f["name"] == name)
    assert listed["type"] == expected_type
    assert listed["media_type"].startswith("application/")

    descriptor = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/{name}"
    )
    assert descriptor.status_code == 200
    body = descriptor.json()
    assert body["version"] == PREVIEW_CONTRACT_VERSION
    assert body["artifact"]["type"] == expected_type
    # Marker-only containers pass classification but fail their renderer's
    # stricter internal-part contract.
    assert body["status"] == "failed"
    assert body["error"]["code"].endswith(
        ("part_missing", "slide_missing", "slide_invalid", "body_missing")
    )
    assert count_key in body["counts"]
    assert body["cache_key"].startswith("sha256:")


def test_xlsx_structural_preview_is_ready_and_never_evaluates_formulas(test_client):
    _xlsx(_workspace() / "metrics.xlsx")
    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/metrics.xlsx"
    ).json()
    assert body["status"] == "ready"
    assert body["renderer"]["name"] == "lane-xlsx-structural"
    assert body["error"] is None
    assert body["content"]["kind"] == "workbook"
    assert body["content"]["formulas_evaluated"] is False
    assert [sheet["name"] for sheet in body["content"]["sheets"]] == ["Summary", "Data"]
    summary = body["content"]["sheets"][0]
    assert summary["frozen"]["top_left_cell"] == "A2"
    assert summary["merges"] == ["A1:A2"]
    cells = {cell["ref"]: cell for row in summary["rows"] for cell in row["cells"]}
    assert cells["A1"]["value"] == "项目"
    assert cells["B2"]["value"] == 0.125
    assert cells["B2"]["display_value"] == "12.50%"
    assert cells["C2"]["formula"] == "SUM(B2:B2)"
    assert cells["C2"]["value"] == 0  # cached value is displayed, never recalculated
    assert cells["D2"]["display_value"] == "2024-01-01"
    assert cells["E2"]["display_value"] == "¥1,234.50"
    assert summary["rows"][2]["hidden"] is True
    assert "formulas-not-evaluated" in body["capability_gaps"]


def test_pptx_static_preview_preserves_slide_layout_and_text(test_client):
    _pptx(_workspace() / "deck.pptx")
    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/deck.pptx"
    ).json()
    assert body["status"] == "ready"
    assert body["renderer"]["name"] == "lane-pptx-static"
    assert body["content"]["kind"] == "presentation"
    assert body["content"]["active_content_executed"] is False
    slide = body["content"]["slides"][0]
    title = next(item for item in slide["elements"] if item["kind"] == "text")
    assert title["text"] == "季度总结"
    assert title["role"] == "title"
    assert 0 < title["x"] < 1 and 0 < title["width"] <= 1
    image = next(item for item in slide["elements"] if item["kind"] == "image")
    assert image["data_uri"].startswith("data:image/png;base64,")


def test_pptx_twenty_plus_slides_keep_order(test_client):
    _many_slide_pptx(_workspace() / "many.pptx", 21)
    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/many.pptx"
    ).json()
    assert body["status"] == "ready"
    assert len(body["content"]["slides"]) == 21
    assert body["content"]["slides"][20]["elements"][0]["text"] == "第 21 页"


def test_docx_structural_preview_preserves_blocks_and_drops_unsafe_links(test_client):
    _docx(_workspace() / "report.docx")
    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/report.docx"
    ).json()
    assert body["status"] == "ready"
    assert body["renderer"]["name"] == "lane-docx-structural"
    assert body["content"]["kind"] == "document"
    heading = body["content"]["blocks"][0]
    assert heading["heading"] == 1
    links = body["content"]["blocks"][1]["runs"]
    assert links[0]["href"] == "https://example.test/"
    assert links[1]["href"] is None
    table = body["content"]["blocks"][2]
    assert table["rows"][0][1]["text"] == "42"
    assert body["content"]["headers"][0]["text"] == "机密报告"
    assert body["content"]["footers"][0]["text"] == "第 1 页"


def test_docx_table_cell_limit_is_global_across_tables(tmp_path, monkeypatch):
    from backend import artifact_docx

    monkeypatch.setattr(artifact_docx, "MAX_TABLE_CELLS", 2)
    cells = "".join(
        f"<w:tc><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc>"
        for value in ("a", "b")
    )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:tbl><w:tr>{cells}</w:tr></w:tbl>"
        f"<w:tbl><w:tr>{cells}</w:tr></w:tbl></w:body></w:document>"
    )
    path = tmp_path / "tables.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
    result = artifact_docx.render_document(path)
    rendered_cells = sum(
        len(row) for block in result["blocks"] if block["kind"] == "table"
        for row in block["rows"]
    )
    assert rendered_cells == 2
    assert result["truncated"] is True


def test_xlsx_1904_dates_and_metadata_limits_are_explicit(tmp_path, monkeypatch):
    from backend import artifact_xlsx

    monkeypatch.setattr(artifact_xlsx, "MAX_MERGES_PER_SHEET", 1)
    monkeypatch.setattr(artifact_xlsx, "MAX_COLUMN_METADATA", 1)
    path = tmp_path / "date1904.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<workbookPr date1904="1"/><sheets><sheet name="Dates" sheetId="1" '
            'r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/styles.xml",
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<cellXfs count="1"><xf numFmtId="14"/></cellXfs></styleSheet>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<cols><col min="1" max="1"/><col min="2" max="2"/></cols>'
            '<sheetData><row r="1"><c r="A1" s="0"><v>0</v></c></row></sheetData>'
            '<mergeCells><mergeCell ref="A1:A2"/><mergeCell ref="B1:B2"/></mergeCells>'
            '</worksheet>',
        )
    result = artifact_xlsx.render_workbook(path)
    sheet = result["sheets"][0]
    assert sheet["rows"][0]["cells"][0]["display_value"] == "1904-01-01"
    assert len(sheet["merges"]) == 1
    assert len(sheet["columns"]) == 1
    assert sheet["truncated"] is True
    assert result["truncated"] is True


def test_xlsx_ten_k_rows_is_bounded_and_explicitly_truncated(test_client):
    _ten_k_xlsx(_workspace() / "ten-k.xlsx")
    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/ten-k.xlsx"
    ).json()
    assert body["status"] == "ready"
    assert body["content"]["truncated"] is True
    assert len(body["content"]["sheets"][0]["rows"]) == 500
    assert body["content"]["sheets"][0]["rows"][-1]["cells"][0]["value"] == "row-500"


def test_xlsx_renderer_rejects_entity_declarations(test_client):
    path = _workspace() / "unsafe.xlsx"
    _xlsx(path, unsafe_shared_strings=True)
    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/unsafe.xlsx"
    ).json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "xlsx_unsafe_xml"


@pytest.mark.parametrize(
    ("name", "marker", "code"),
    [
        ("unsafe.pptx", "ppt/presentation.xml", "pptx_unsafe_xml"),
        ("unsafe.docx", "word/document.xml", "docx_unsafe_xml"),
    ],
)
def test_pptx_docx_renderers_reject_entity_declarations(test_client, name, marker, code):
    path = _workspace() / name
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            marker,
            '<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///etc/passwd">]><root>&leak;</root>',
        )
    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/{name}"
    ).json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == code


def test_xlsx_worker_result_is_cached_outside_artifact_namespace(test_client, monkeypatch):
    from backend import artifact_preview

    _xlsx(_workspace() / "cached.xlsx")
    url = f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/cached.xlsx"
    first = test_client.get(url)
    assert first.json()["status"] == "ready"

    def must_not_spawn(*_args, **_kwargs):
        raise AssertionError("cache hit unexpectedly spawned a renderer")

    monkeypatch.setattr(artifact_preview.subprocess, "run", must_not_spawn)
    second = test_client.get(url)
    assert second.status_code == 200
    assert second.json()["content"] == first.json()["content"]

    attempt_dir = runtime_state.get().data_path / "attempts" / ATTEMPT_ID
    assert any((attempt_dir / "artifact-previews").rglob("xlsx-structural.json"))
    listing = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts"
    ).json()
    assert "artifact-previews" not in {step["step"] for step in listing}


def test_renderer_rejects_snapshot_that_no_longer_matches_hashed_source(tmp_path, monkeypatch):
    from backend import artifact_preview

    source = tmp_path / "changing.xlsx"
    _xlsx(source)
    expected = artifact_preview.cache_key(source)

    def mismatched_copy(_source, destination):
        Path(destination).write_bytes(b"different bytes")

    monkeypatch.setattr(artifact_preview.shutil, "copyfile", mismatched_copy)
    monkeypatch.setattr(
        artifact_preview.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("unstable snapshot must not reach renderer"),
    )
    result = artifact_preview._worker_result(
        source, renderer="xlsx-structural", cache_dir=None,
        expected_cache_key=expected,
    )
    assert result["ok"] is False
    assert result["transient"] is True
    assert result["error"]["code"] == "artifact_changed"


def test_renderer_timeout_is_stable_and_original_remains_downloadable(test_client, monkeypatch):
    from backend import artifact_preview

    original = _xlsx(_workspace() / "timeout.xlsx")

    def timeout(*args, **kwargs):
        raise TimeoutExpired(args[0], kwargs.get("timeout", 15))

    monkeypatch.setattr(artifact_preview.subprocess, "run", timeout)
    preview = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/timeout.xlsx"
    )
    assert preview.status_code == 200
    assert preview.json()["status"] == "failed"
    assert preview.json()["error"]["code"] == "renderer_timeout"
    download = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts/timeout.xlsx"
    )
    assert download.content == original


def test_renderer_crash_is_stable_and_not_cached(test_client, monkeypatch):
    from backend import artifact_preview

    _xlsx(_workspace() / "crash.xlsx")
    monkeypatch.setattr(
        artifact_preview.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 70),
    )
    url = f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/crash.xlsx"
    body = test_client.get(url).json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "renderer_crashed"
    cache_root = runtime_state.get().data_path / "attempts" / ATTEMPT_ID / "artifact-previews"
    assert not any(cache_root.rglob("xlsx-structural.json"))


def test_symlinked_preview_cache_root_is_never_written(test_client, tmp_path):
    _xlsx(_workspace() / "safe-cache.xlsx")
    attempt_dir = runtime_state.get().data_path / "attempts" / ATTEMPT_ID
    cache_root = attempt_dir / "artifact-previews"
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    try:
        cache_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/safe-cache.xlsx"
    ).json()
    assert body["status"] == "ready"
    assert list(outside.iterdir()) == []


def test_symlinked_preview_cache_entry_is_never_read(test_client, tmp_path):
    from backend.artifact_preview import cache_key

    source = _workspace() / "cache-entry.xlsx"
    _xlsx(source)
    attempt_dir = runtime_state.get().data_path / "attempts" / ATTEMPT_ID
    cache_root = attempt_dir / "artifact-previews"
    key = cache_key(source).split(":", 1)[1]
    entry_dir = cache_root / key
    outside = tmp_path / "poisoned-cache"
    outside.mkdir()
    (outside / "xlsx-structural.json").write_text(
        '{"ok":true,"content":{"kind":"workbook","secret":"leaked"}}',
        encoding="utf-8",
    )
    cache_root.mkdir()
    try:
        entry_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/cache-entry.xlsx"
    ).json()
    assert body["status"] == "ready"
    assert "secret" not in body["content"]


def test_large_xlsx_renders_in_background_and_polling_is_deduplicated(
    test_client, monkeypatch
):
    from backend import artifact_preview

    _xlsx(_workspace() / "large.xlsx")
    monkeypatch.setattr(artifact_preview, "SYNC_PREVIEW_MAX_BYTES", 1)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_worker(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        return {
            "ok": True,
            "content": {
                "kind": "workbook", "sheets": [], "truncated": False,
                "limits": {}, "formulas_evaluated": False,
            },
        }

    monkeypatch.setattr(artifact_preview, "_worker_result", slow_worker)
    url = f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/large.xlsx"
    first = test_client.get(url).json()
    assert first["status"] == "rendering"
    assert first["poll_after_ms"] == artifact_preview.PREVIEW_POLL_AFTER_MS
    assert started.wait(timeout=1)
    second = test_client.get(url).json()
    assert second["status"] == "rendering"
    assert calls == 1

    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        final = test_client.get(url).json()
        if final["status"] != "rendering":
            break
        time.sleep(0.01)
    assert final["status"] == "ready"
    assert final["content"]["kind"] == "workbook"
    assert final["poll_after_ms"] is None
    assert calls == 1


def test_large_xlsx_background_exception_becomes_stable_failure(test_client, monkeypatch):
    from backend import artifact_preview

    _xlsx(_workspace() / "background-crash.xlsx")
    monkeypatch.setattr(artifact_preview, "SYNC_PREVIEW_MAX_BYTES", 1)

    def crash(*_args, **_kwargs):
        raise RuntimeError("must not escape into API")

    monkeypatch.setattr(artifact_preview, "preview_descriptor", crash)
    url = (
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/"
        "background-crash.xlsx"
    )
    first = test_client.get(url).json()
    assert first["status"] == "rendering"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        final = test_client.get(url).json()
        if final["status"] != "rendering":
            break
        time.sleep(0.01)
    assert final["status"] == "failed"
    assert final["error"]["code"] == "renderer_crashed"


def test_large_xlsx_background_queue_is_bounded(test_client, monkeypatch):
    from backend import artifact_preview

    _xlsx(_workspace() / "queue-full.xlsx")
    monkeypatch.setattr(artifact_preview, "SYNC_PREVIEW_MAX_BYTES", 1)
    monkeypatch.setattr(artifact_preview, "MAX_BACKGROUND_JOBS", 0)
    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/queue-full.xlsx"
    ).json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "renderer_queue_full"
    assert body["poll_after_ms"] is None


def test_fake_extension_and_corrupt_zip_fail_closed(test_client):
    workspace = _workspace()
    (workspace / "fake.pptx").write_text("not a zip", encoding="utf-8")
    (workspace / "broken.docx").write_bytes(b"PK\x03\x04broken")

    for name in ("fake.pptx", "broken.docx"):
        body = test_client.get(
            f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/{name}"
        ).json()
        assert body["status"] == "failed"
        assert body["error"]["code"] == "invalid_ooxml_zip"


def test_zip_path_traversal_is_rejected(test_client):
    path = _workspace() / "traversal.pptx"
    _ooxml(path, "ppt/presentation.xml", members={"../escape": b"no"})
    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/traversal.pptx"
    ).json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "zip_path_traversal"


def test_zip_bomb_ratio_is_rejected(tmp_path):
    path = tmp_path / "bomb.xlsx"
    _ooxml(
        path,
        "xl/workbook.xml",
        members={"xl/worksheets/sheet1.xml": b"0" * (2 * 1024 * 1024)},
    )
    result = inspect_artifact(path)
    assert result.status == "failed"
    assert result.error_code == "zip_compression_ratio"


def test_external_relationship_and_macro_are_reported_not_executed(test_client):
    path = _workspace() / "active.pptm"
    _ooxml(
        path,
        "ppt/presentation.xml",
        members={
            "ppt/_rels/presentation.xml.rels": (
                b'<Relationships><Relationship TargetMode="External" '
                b'Target="https://example.invalid/payload"/></Relationships>'
            ),
            "ppt/vbaProject.bin": b"macro bytes",
        },
    )
    body = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/active.pptm"
    ).json()
    assert body["artifact"]["media_type"] == (
        "application/vnd.ms-powerpoint.presentation.macroEnabled.12"
    )
    assert body["security"] == {
        "macros_present": True,
        "macros_executed": False,
        "external_relationships_present": True,
        "external_resources_loaded": False,
    }
    assert "macros-disabled" in body["capability_gaps"]
    assert "external-resources-blocked" in body["capability_gaps"]


def test_office_download_is_byte_identical_and_never_text_decoded(test_client):
    path = _workspace() / "slides.pptx"
    original = _ooxml(path, "ppt/presentation.xml")
    response = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts/slides.pptx"
    )
    assert response.status_code == 200
    assert response.content == original
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )


def test_binary_is_not_listed_as_text_and_attempt_root_ref_downloads(test_client):
    attempt_dir = runtime_state.get().data_path / "attempts" / ATTEMPT_ID
    attempt_dir.mkdir(parents=True, exist_ok=True)
    payload = b"\x00\xff\x01binary"
    (attempt_dir / "result.bin").write_bytes(payload)

    listing = test_client.get(f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts").json()
    listed = next(f for step in listing for f in step["files"] if f["name"] == "result.bin")
    assert listed["type"] == "binary"
    response = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts/attempt-root/result.bin"
    )
    assert response.status_code == 200
    assert response.content == payload


def test_non_office_type_uses_content_not_a_misleading_extension(test_client):
    workspace = _workspace()
    (workspace / "not-an-image.png").write_text("plain UTF-8 text", encoding="utf-8")
    (workspace / "actual-image.bin").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    listing = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts"
    ).json()
    files = {
        item["name"]: item
        for step in listing for item in step["files"]
    }
    assert files["not-an-image.png"]["type"] == "text"
    assert files["not-an-image.png"]["media_type"] == "text/plain"
    assert files["actual-image.bin"]["type"] == "image"
    assert files["actual-image.bin"]["media_type"] == "image/png"


def test_preview_path_cannot_escape_artifact_root(test_client, tmp_path):
    _workspace()
    response = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifact-previews/%2E%2E/secret"
    )
    assert response.status_code == 404


def test_artifact_listing_ignores_symlink_outside_root(test_client, tmp_path):
    workspace = _workspace()
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = workspace / "outside.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable")
    listing = test_client.get(f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts").json()
    assert "outside.txt" not in {f["name"] for step in listing for f in step["files"]}


def test_hidden_files_and_directories_are_not_artifacts(test_client):
    workspace = _workspace()
    (workspace / ".env").write_text("API_KEY=secret", encoding="utf-8")
    hidden_dir = workspace / ".ssh"
    hidden_dir.mkdir()
    (hidden_dir / "id_rsa").write_text("private", encoding="utf-8")

    listing = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts"
    ).json()
    listed = {(step["step"], f["name"]) for step in listing for f in step["files"]}
    assert (".", ".env") not in listed
    assert not any(step["step"] == ".ssh" for step in listing)

    for suffix in (
        "artifacts/.env",
        "artifacts/.ssh/id_rsa",
        "artifact-previews/.env",
    ):
        response = test_client.get(
            f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/{suffix}"
        )
        assert response.status_code == 404, suffix


def test_dot_traversal_cannot_bypass_framework_exclusion():
    attempt_dir = runtime_state.get().data_path / "attempts" / ATTEMPT_ID
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "result.txt").write_text("visible", encoding="utf-8")
    (attempt_dir / "wire.jsonl").write_text("sensitive", encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        _resolve_artifact_path(attempt_dir, "x/../wire.jsonl")
    assert exc.value.status_code == 404


def test_symlink_directory_outside_root_is_not_scanned_or_downloaded(test_client, tmp_path):
    workspace = _workspace()
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    listing = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts"
    ).json()
    assert not any(step["step"] == "linked" for step in listing)
    response = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts/linked/secret.txt"
    )
    assert response.status_code == 404


def test_symlinked_workspace_root_is_rejected(test_client, tmp_path):
    workspace = _workspace()
    workspace.rmdir()
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        workspace.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    listing = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts"
    ).json()
    assert listing == []
    response = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts/secret.txt"
    )
    assert response.status_code == 404


def test_attempt_root_directory_only_artifacts_are_listed(test_client):
    attempt_dir = runtime_state.get().data_path / "attempts" / ATTEMPT_ID
    output = attempt_dir / "output"
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.txt").write_text("ok", encoding="utf-8")
    listing = test_client.get(
        f"/api/runs/{RUN_ID}/attempts/{ATTEMPT_ID}/artifacts"
    ).json()
    assert any(
        step["step"] == "output" and step["files"][0]["name"] == "result.txt"
        for step in listing
    )


@pytest.mark.parametrize(
    "suffix",
    [
        "artifacts",
        "artifacts/deck.pptx",
        "artifact-previews/deck.pptx",
    ],
)
def test_run_attempt_mismatch_is_hidden_by_404(test_client, suffix):
    _ooxml(_workspace() / "deck.pptx", "ppt/presentation.xml")
    response = test_client.get(
        f"/api/runs/run_unrelated/attempts/{ATTEMPT_ID}/{suffix}"
    )
    assert response.status_code == 404
