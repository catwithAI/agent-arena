from __future__ import annotations

import html
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

DEFAULT_SLIDE_WIDTH = 12192000
DEFAULT_SLIDE_HEIGHT = 6858000
DEFAULT_MIN_SIZE = 12000
SLIDE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"


def _xml_attr(tag: str, attr: str) -> str | None:
    match = re.search(rf'\b{re.escape(attr)}="([^"]*)"', tag)
    return match.group(1) if match else None


def _xml_int_attr(tag: str, attr: str) -> int | None:
    value = _xml_attr(tag, attr)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _relationships(rels_xml: str) -> dict[str, tuple[str, str]]:
    rows = {}
    for match in re.finditer(r"<Relationship\b[^>]*/>", rels_xml):
        tag = match.group(0)
        rid = _xml_attr(tag, "Id")
        rel_type = _xml_attr(tag, "Type")
        target = _xml_attr(tag, "Target")
        if rid and rel_type and target:
            rows[rid] = (rel_type, target)
    return rows


def _slide_size(presentation_xml: str) -> tuple[int, int]:
    match = re.search(r"<p:sldSz\b[^>]*/?>", presentation_xml)
    if not match:
        return DEFAULT_SLIDE_WIDTH, DEFAULT_SLIDE_HEIGHT
    return (
        _xml_int_attr(match.group(0), "cx") or DEFAULT_SLIDE_WIDTH,
        _xml_int_attr(match.group(0), "cy") or DEFAULT_SLIDE_HEIGHT,
    )


def _slide_paths(archive: zipfile.ZipFile) -> list[str]:
    names = set(archive.namelist())
    if "ppt/presentation.xml" in names and "ppt/_rels/presentation.xml.rels" in names:
        presentation_xml = archive.read("ppt/presentation.xml").decode("utf-8")
        rels = _relationships(archive.read("ppt/_rels/presentation.xml.rels").decode("utf-8"))
        paths = []
        for rid in re.findall(r'<p:sldId\b[^>]*\br:id="([^"]+)"[^>]*/>', presentation_xml):
            rel = rels.get(rid)
            if rel and rel[0] == SLIDE_REL_TYPE:
                target = rel[1].lstrip("/")
                path = target if target.startswith("ppt/") else f"ppt/{target}"
                if path in names:
                    paths.append(path)
        if paths:
            return paths
    return sorted(name for name in names if re.match(r"ppt/slides/slide\d+\.xml$", name))


def _object_blocks(slide_xml: str) -> list[tuple[str, str]]:
    blocks = []
    for match in re.finditer(r"<p:(sp|pic|grpSp)\b.*?</p:\1>", slide_xml, re.DOTALL):
        block = match.group(0)
        if "lane annotation" in block:
            continue
        blocks.append(({"sp": "shape", "pic": "picture", "grpSp": "group"}[match.group(1)], block))
    return blocks


def _bbox(block: str) -> dict[str, int] | None:
    xfrm = re.search(r"<a:xfrm\b.*?</a:xfrm>", block, re.DOTALL)
    if not xfrm:
        return None
    off = re.search(r"<a:off\b[^>]*/?>", xfrm.group(0))
    ext = re.search(r"<a:ext\b[^>]*/?>", xfrm.group(0))
    if not off or not ext:
        return None
    x = _xml_int_attr(off.group(0), "x")
    y = _xml_int_attr(off.group(0), "y")
    cx = _xml_int_attr(ext.group(0), "cx")
    cy = _xml_int_attr(ext.group(0), "cy")
    if None in {x, y, cx, cy}:
        return None
    return {"x": int(x), "y": int(y), "cx": int(cx), "cy": int(cy)}


def _shape_name(block: str) -> str:
    match = re.search(r"<p:cNvPr\b[^>]*>", block)
    return html.unescape(_xml_attr(match.group(0), "name") or "") if match else ""


def _text_preview(block: str, limit: int = 80) -> str:
    parts = [html.unescape(item) for item in re.findall(r"<a:t\b[^>]*>(.*?)</a:t>", block, re.DOTALL)]
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _max_cnvpr_id(slide_xml: str) -> int:
    values = []
    for match in re.finditer(r"<p:cNvPr\b[^>]*>", slide_xml):
        value = _xml_int_attr(match.group(0), "id")
        if value is not None:
            values.append(value)
    return max(values or [0])


def _rect_shape(shape_id: int, name: str, bbox: dict[str, int], line_color: str = "FF2D55", line_width: int = 19050) -> str:
    x, y, cx, cy = bbox["x"], bbox["y"], bbox["cx"], bbox["cy"]
    return f"""
<p:sp>
  <p:nvSpPr><p:cNvPr id="{shape_id}" name="{html.escape(name)}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
  <p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln w="{line_width}"><a:solidFill><a:srgbClr val="{line_color}"/></a:solidFill></a:ln></p:spPr>
  <p:style/><p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody>
</p:sp>"""


def _text_box_shape(shape_id: int, name: str, bbox: dict[str, int], text: str, fill_color: str = "FF2D55", text_color: str = "FFFFFF") -> str:
    x, y, cx, cy = bbox["x"], bbox["y"], bbox["cx"], bbox["cy"]
    return f"""
<p:sp>
  <p:nvSpPr><p:cNvPr id="{shape_id}" name="{html.escape(name)}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
  <p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="{fill_color}"/></a:solidFill><a:ln w="6350"><a:solidFill><a:srgbClr val="{fill_color}"/></a:solidFill></a:ln></p:spPr>
  <p:style/><p:txBody><a:bodyPr wrap="none" lIns="25000" tIns="12000" rIns="25000" bIns="12000"/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="900" b="1"><a:solidFill><a:srgbClr val="{text_color}"/></a:solidFill></a:rPr><a:t>{html.escape(text)}</a:t></a:r><a:endParaRPr lang="en-US" sz="900"/></a:p></p:txBody>
</p:sp>"""


def _label_bbox(object_bbox: dict[str, int], slide_width: int, slide_height: int) -> dict[str, int]:
    label_w = max(520000, slide_width // 18)
    label_h = max(170000, slide_height // 34)
    return {
        "x": max(0, min(object_bbox["x"], slide_width - label_w)),
        "y": max(0, min(object_bbox["y"], slide_height - label_h)),
        "cx": label_w,
        "cy": label_h,
    }


def _scale_shapes(next_id: int, slide_width: int, slide_height: int) -> tuple[str, list[dict[str, Any]]]:
    margin = max(180000, slide_width // 50)
    ruler_w = slide_width // 10
    ruler_h = max(65000, slide_height // 120)
    label_w = max(1100000, slide_width // 9)
    label_h = max(180000, slide_height // 34)
    x = max(0, slide_width - margin - ruler_w)
    y = max(0, slide_height - margin - label_h - ruler_h)
    xml = _rect_shape(next_id, "lane annotation scale bar", {"x": x, "y": y + label_h, "cx": ruler_w, "cy": ruler_h}, "111111", 6350)
    xml += _text_box_shape(next_id + 1, "lane annotation scale label", {"x": max(0, slide_width - margin - label_w), "y": y, "cx": label_w, "cy": label_h}, "10% slide width", "111111", "FFFFFF")
    return xml, [{"annotation_id": next_id, "kind": "scale_bar", "bbox_emu": {"x": x, "y": y + label_h, "cx": ruler_w, "cy": ruler_h}}, {"annotation_id": next_id + 1, "kind": "scale_label", "text": "10% slide width"}]


def annotate_pptx(input_path: str | Path, output_path: str | Path, manifest_path: str | Path, *, min_size: int = DEFAULT_MIN_SIZE) -> dict[str, Any]:
    source = Path(input_path)
    target = Path(output_path)
    manifest_target = Path(manifest_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    manifest: list[dict[str, Any]] = []
    with zipfile.ZipFile(source, "r") as archive:
        names = set(archive.namelist())
        if "ppt/presentation.xml" not in names:
            raise ValueError("input is not a valid PowerPoint package")
        slide_paths = _slide_paths(archive)
        slide_size = _slide_size(archive.read("ppt/presentation.xml").decode("utf-8"))
        slide_lookup = {path: index + 1 for index, path in enumerate(slide_paths)}
        for info in archive.infolist():
            data = archive.read(info.filename)
            slide_index = slide_lookup.get(info.filename)
            if slide_index:
                text = data.decode("utf-8")
                text, slide_manifest = _annotate_slide(text, slide_index, info.filename, slide_size, min_size)
                data = text.encode("utf-8")
                manifest.append(slide_manifest)
            entries.append((info, data))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, data in entries:
            archive.writestr(info, data)
    shutil.move(str(tmp), str(target))
    result = {"ok": True, "input": str(source), "output": str(target), "slides": manifest, "object_count": sum(s["object_count"] for s in manifest)}
    manifest_target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["manifest"] = str(manifest_target)
    return result


def _annotate_slide(slide_xml: str, slide_index: int, slide_path: str, slide_size: tuple[int, int], min_size: int) -> tuple[str, dict[str, Any]]:
    slide_width, slide_height = slide_size
    objects = []
    annotation_xml = []
    next_shape_id = _max_cnvpr_id(slide_xml) + 1
    for object_type, block in _object_blocks(slide_xml):
        bbox = _bbox(block)
        if not bbox or bbox["cx"] < min_size or bbox["cy"] < min_size:
            continue
        object_id = f"S{slide_index:02d}-O{len(objects) + 1:03d}"
        objects.append({"id": object_id, "type": object_type, "name": _shape_name(block), "xml_path": slide_path, "bbox_emu": bbox, "text_preview": _text_preview(block)})
        annotation_xml.append(_rect_shape(next_shape_id, f"lane annotation bbox {object_id}", bbox))
        next_shape_id += 1
        annotation_xml.append(_text_box_shape(next_shape_id, f"lane annotation label {object_id}", _label_bbox(bbox, slide_width, slide_height), object_id))
        next_shape_id += 1
    scale_xml, scale_manifest = _scale_shapes(next_shape_id, slide_width, slide_height)
    annotation_xml.append(scale_xml)
    insert_at = slide_xml.rfind("</p:spTree>")
    if insert_at >= 0:
        slide_xml = slide_xml[:insert_at] + "\n".join(annotation_xml) + slide_xml[insert_at:]
    return slide_xml, {"slide": slide_index, "xml_path": slide_path, "objects": objects, "object_count": len(objects), "scale": scale_manifest}
