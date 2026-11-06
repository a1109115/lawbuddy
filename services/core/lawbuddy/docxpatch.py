# -*- coding: utf-8 -*-
"""DOCX 增量写回：只重写被编辑的段落/单元格，其余 OOXML 原样保留。

背景：`word.to_docx` 会从空白文档重建整份 DOCX，页眉页脚、图片、编号、
域代码等部件会丢失。真实编辑通常只改少量段落文字，此时应直接修改原
`word/document.xml` 中的对应节点，其余 zip 部件逐字节复制。

约束（fail-closed）：
- 只有模型结构（节点类型与层级）完全一致时才做增量写回；
- 段落内含图片/域代码/修订标记等不可安全重建的内容时，拒绝就地替换；
- 结构发生变化（增删段落/表格行列）时抛出 PatchNotPossible，由调用方决定降级。
"""

from __future__ import annotations

import io
import copy
import zipfile
from typing import Any

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"


class PatchNotPossible(RuntimeError):
    """无法安全增量写回（结构变化或含不可重建内容）。"""


def _q(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _shape(node: dict[str, Any]) -> Any:
    """模型结构指纹：只保留节点类型与层级，忽略文字与格式。"""
    kind = node.get("type")
    children = node.get("content") or []
    if kind == "text" or kind == "hardBreak":
        return kind
    if kind in ("bulletList", "orderedList", "listItem"):
        return (kind, tuple(_shape(child) for child in children))
    if kind == "table":
        return ("table", tuple(_shape(row) for row in children))
    if kind in ("tableRow", "tableCell", "tableHeader"):
        return (kind, tuple(_shape(child) for child in children))
    return (kind,)


def _canonical(node: dict[str, Any]) -> str:
    import json

    return json.dumps(
        {"type": node.get("type"), "attrs": node.get("attrs") or {}, "content": [
            {"text": child.get("text", ""), "marks": child.get("marks") or [], "type": child.get("type")}
            if child.get("type") in ("text", "hardBreak")
            else _canonical(child)
            for child in node.get("content") or []
        ]},
        ensure_ascii=False,
        sort_keys=True,
    )


def _has_unpatchable(xml: str) -> bool:
    """段落内是否含无法安全重建的内联内容。"""
    markers = ("<w:drawing", "<w:pict", "<w:fldChar", "<w:instrText", "<w:ins", "<w:del",
               "<w:footnoteReference", "<w:commentReference")
    return any(marker in xml for marker in markers)


def _set_paragraph_props(p: etree._Element, node: dict[str, Any]) -> None:
    """更新段落属性（对齐 / 标题样式 / 行距），保留其余 pPr 子节点。"""
    ppr = p.find(_q("pPr"))
    if ppr is None:
        ppr = etree.Element(_q("pPr"))
        p.insert(0, ppr)

    align = (node.get("attrs") or {}).get("textAlign")
    jc = ppr.find(_q("jc"))
    if align:
        want = {"left": "left", "right": "right", "center": "center", "justify": "both"}.get(align, align)
        if jc is None:
            jc = etree.SubElement(ppr, _q("jc"))
        jc.set(_q("val"), want)
    elif jc is not None:
        ppr.remove(jc)

    if node.get("type") == "heading":
        level = int((node.get("attrs") or {}).get("level") or 1)
        style = ppr.find(_q("pStyle"))
        if style is None:
            style = etree.Element(_q("pStyle"))
            ppr.insert(0, style)
        style.set(_q("val"), f"Heading{max(1, min(3, level))}")

    # 行距来自 textStyle.mark.attrs.lineHeight
    line_height = None
    for child in node.get("content") or []:
        for mark in child.get("marks") or []:
            if mark.get("type") == "textStyle":
                value = (mark.get("attrs") or {}).get("lineHeight")
                if value:
                    line_height = value
    if line_height:
        spacing = ppr.find(_q("spacing"))
        if spacing is None:
            spacing = etree.SubElement(ppr, _q("spacing"))
        try:
            spacing.set(_q("line"), str(int(float(line_height) * 240)))
            spacing.set(_q("lineRule"), "auto")
        except ValueError:
            pass


def _make_run(text: str, marks: list[dict[str, Any]]) -> etree._Element:
    run = etree.Element(_q("r"))
    rpr = etree.SubElement(run, _q("rPr"))
    for mark in marks:
        kind = mark.get("type")
        if kind == "bold":
            etree.SubElement(rpr, _q("b"))
        elif kind == "italic":
            etree.SubElement(rpr, _q("i"))
        elif kind == "underline":
            underline = etree.SubElement(rpr, _q("u"))
            underline.set(_q("val"), "single")
        elif kind == "strike":
            etree.SubElement(rpr, _q("strike"))
        elif kind == "textStyle":
            attrs = mark.get("attrs") or {}
            if attrs.get("fontFamily"):
                fonts = etree.SubElement(rpr, _q("rFonts"))
                for key in ("ascii", "hAnsi", "eastAsia", "cs"):
                    fonts.set(_q(key), attrs["fontFamily"])
            if attrs.get("fontSize"):
                size = str(attrs["fontSize"]).replace("pt", "")
                try:
                    half_points = str(int(round(float(size) * 2)))
                except ValueError:
                    half_points = None
                if half_points:
                    sz = etree.SubElement(rpr, _q("sz"))
                    sz.set(_q("val"), half_points)
                    szcs = etree.SubElement(rpr, _q("szCs"))
                    szcs.set(_q("val"), half_points)
            if attrs.get("color"):
                color = etree.SubElement(rpr, _q("color"))
                color.set(_q("val"), str(attrs["color"]).lstrip("#").upper())
            if attrs.get("backgroundColor"):
                shd = etree.SubElement(rpr, _q("shd"))
                shd.set(_q("val"), "clear")
                shd.set(_q("color"), "auto")
                shd.set(_q("fill"), str(attrs["backgroundColor"]).lstrip("#").upper())
    text_el = etree.SubElement(run, _q("t"))
    text_el.set(f"{{{XML_NS}}}space", "preserve")
    text_el.text = text
    return run


def _rebuild_paragraph(p: etree._Element, node: dict[str, Any]) -> None:
    """保留 pPr 与书签/批注范围标记，重写正文 runs。"""
    _set_paragraph_props(p, node)
    keep: list[etree._Element] = []
    for child in list(p):
        tag = etree.QName(child).localname
        if tag in ("pPr", "bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd"):
            keep.append(child)
            continue
        p.remove(child)

    insertion_index = len(keep)
    for child in node.get("content") or []:
        if child.get("type") == "hardBreak":
            run = etree.Element(_q("r"))
            etree.SubElement(run, _q("br"))
            p.insert(insertion_index, run)
            insertion_index += 1
        elif child.get("type") == "text":
            p.insert(insertion_index, _make_run(str(child.get("text") or ""), child.get("marks") or []))
            insertion_index += 1


def _has_revision_marks(node: dict[str, Any]) -> bool:
    """模型是否含修订/批注标记（必须整篇按模型重建，不能丢失修订语义）。"""
    for child in node.get("content") or []:
        for mark in child.get("marks") or []:
            if mark.get("type") in ("insertion", "deletion", "comment"):
                return True
        if child.get("type") not in ("text", "hardBreak") and _has_revision_marks(child):
            return True
    return False


def _patch_paragraph(p: etree._Element, old_node: dict[str, Any], new_node: dict[str, Any]) -> bool:
    if _canonical(old_node) == _canonical(new_node):
        return False
    if _has_revision_marks(new_node):
        # 增量重建不写 w:ins / w:del / comments，静默降级会让 DOCX 与模型不一致
        raise PatchNotPossible("段落包含修订/批注标记，需整篇按模型重建")
    xml = etree.tostring(p, encoding="unicode")
    if _has_unpatchable(xml):
        raise PatchNotPossible("段落包含图片/域代码/修订标记，暂不支持在线覆盖")
    _rebuild_paragraph(p, new_node)
    return True


def _patch_table(tbl: etree._Element, old_node: dict[str, Any], new_node: dict[str, Any]) -> bool:
    changed = False
    rows = [row for row in tbl if etree.QName(row).localname == "tr"]
    old_rows = old_node.get("content") or []
    new_rows = new_node.get("content") or []
    if len(rows) != len(old_rows) or len(old_rows) != len(new_rows):
        raise PatchNotPossible("表格行列发生变化")
    for row_el, old_row, new_row in zip(rows, old_rows, new_rows):
        cells = [cell for cell in row_el if etree.QName(cell).localname == "tc"]
        old_cells = old_row.get("content") or []
        new_cells = new_row.get("content") or []
        if len(cells) != len(old_cells) or len(old_cells) != len(new_cells):
            raise PatchNotPossible("表格单元格数量发生变化")
        for cell_el, old_cell, new_cell in zip(cells, old_cells, new_cells):
            paras = [p for p in cell_el if etree.QName(p).localname == "p"]
            old_paras = old_cell.get("content") or []
            new_paras = new_cell.get("content") or []
            if len(paras) != len(old_paras) or len(old_paras) != len(new_paras):
                raise PatchNotPossible("表格单元格段落数量发生变化")
            for p_el, old_p, new_p in zip(paras, old_paras, new_paras):
                changed = _patch_paragraph(p_el, old_p, new_p) or changed
    return changed


def _patch_container(container: etree._Element, old_nodes: list[dict[str, Any]], new_nodes: list[dict[str, Any]]) -> bool:
    if len(old_nodes) != len(new_nodes):
        raise PatchNotPossible("文档段落数量发生变化")
    if [_shape(node) for node in old_nodes] != [_shape(node) for node in new_nodes]:
        raise PatchNotPossible("文档结构发生变化")

    elements = [child for child in container if etree.QName(child).localname in ("p", "tbl")]
    cursor = 0
    changed = False
    for old_block, new_block in zip(old_nodes, new_nodes):
        kind = old_block.get("type")
        if kind == "table":
            if cursor >= len(elements) or etree.QName(elements[cursor]).localname != "tbl":
                raise PatchNotPossible("表格位置与原件不一致")
            changed = _patch_table(elements[cursor], old_block, new_block) or changed
            cursor += 1
        elif kind in ("bulletList", "orderedList"):
            old_items = old_block.get("content") or []
            new_items = new_block.get("content") or []
            for old_item, new_item in zip(old_items, new_items):
                old_paras = old_item.get("content") or []
                new_paras = new_item.get("content") or []
                if len(old_paras) != len(new_paras):
                    raise PatchNotPossible("列表项结构发生变化")
                for old_p, new_p in zip(old_paras, new_paras):
                    if cursor >= len(elements) or etree.QName(elements[cursor]).localname != "p":
                        raise PatchNotPossible("列表段落位置与原件不一致")
                    changed = _patch_paragraph(elements[cursor], old_p, new_p) or changed
                    cursor += 1
        elif kind in ("paragraph", "heading"):
            if cursor >= len(elements) or etree.QName(elements[cursor]).localname != "p":
                raise PatchNotPossible("段落位置与原件不一致")
            changed = _patch_paragraph(elements[cursor], old_block, new_block) or changed
            cursor += 1
        else:
            raise PatchNotPossible(f"不支持的模型节点：{kind}")
    if cursor != len(elements):
        raise PatchNotPossible("原件存在模型未覆盖的段落/表格")
    return changed


def patch_docx(original: bytes, old_model: dict[str, Any], new_model: dict[str, Any]) -> tuple[bytes, bool]:
    """在原件字节上就地更新被修改的段落；返回 (新字节, 是否发生变化)。

    无法安全写回时抛 PatchNotPossible（调用方应降级并提示版式风险）。
    """
    if _canonical(old_model) == _canonical(new_model):
        # 模型完全一致（自动保存/打开后未改）：直接返回原件字节，
        # 避免合并单元格等结构差异导致不必要的「无法写回」降级重建。
        return original, False
    old_nodes = old_model.get("content") or []
    new_nodes = new_model.get("content") or []
    if [_shape(node) for node in old_nodes] != [_shape(node) for node in new_nodes]:
        raise PatchNotPossible("文档结构发生变化")

    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        try:
            document_xml = archive.read("word/document.xml")
        except KeyError as exc:
            raise PatchNotPossible("原件缺少 document.xml") from exc
        root = etree.fromstring(document_xml)
        body = root.find(_q("body"))
        if body is None:
            raise PatchNotPossible("原件缺少文档主体")
        changed = _patch_container(body, old_nodes, new_nodes)
        if not changed:
            return original, False
        new_xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
            for info in archive.infolist():
                data = new_xml if info.filename == "word/document.xml" else archive.read(info.filename)
                target.writestr(info, data)
        return out.getvalue(), True


def clone_model(model: dict[str, Any]) -> dict[str, Any]:
    """深拷贝模型（调用方修改前使用，避免原地污染历史版本）。"""
    return copy.deepcopy(model)
