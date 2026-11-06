# -*- coding: utf-8 -*-
"""DOCX 文书模板薄适配层（升级方案 §6.3 / §10 阶段三）。

复用 `word.py` 的 OOXML 能力边界与 `docxpatch.py` 的「只改文本节点、其余部件
原样复制」思路，为文书模板提供三件事：

1. `extract_template`：从用户上传的 .docx 提取结构化模板信息——正文 Markdown
   （供提纲推导、槽位识别与预览）、显式变量字段、字段在段落/表格/页眉页脚中的
   位置映射，以及无法安全保留内容的格式警告；
2. `render_strict`：在原 DOCX 字节上做跨 run 的最小范围变量替换，未填值写 `xx`；
   非变量文字、样式、页眉页脚、图片、编号等部件逐字节保留；
3. `render_reference`：以模板副本为基底（保留页面设置、样式、页眉页脚），
   用生成正文替换文档主体，适用于无显式变量的范本。

约束（fail-closed）：
- 只读原件：调用方以模板原件字节为输入，本模块不写回原件；
- 跨 run 变量按段落/单元格合并识别后再替换，避免仅按单 run 查找漏替换；
- 无法安全保留的内容（文本框、域代码、修订记录、内容控件）产生明确警告，
  不静默丢弃。
"""

from __future__ import annotations

import io
import re
from typing import Any

from docx import Document
from docx.oxml.ns import qn

# 与 docgen 保持同一变量语法（此处独立定义以避免模块循环依赖）
_VAR_DOUBLE = re.compile(r"\{\{\s*([^{}\n]+?)\s*\}\}")
_VAR_CN = re.compile(r"【((?!待补充)[^【】\n]{1,24})】")
_VAR_ANY = re.compile(r"\{\{\s*([^{}\n]+?)\s*\}\}|【((?!待补充)[^【】\n]{1,24})】")

# 格式警告标记（标记存在于 OOXML 时提示用户版式可能未完全保留）
_FEATURE_MARKERS = (
    ("<w:txbxContent", "文本框"),
    ("<w:fldChar", "域代码"),
    ("<w:instrText", "域代码"),
    ("<w:ins", "修订记录"),
    ("<w:del", "修订记录"),
    ("<w:sdt", "内容控件"),
    ("<w:drawing", "图片"),
)


class DocxTemplateError(ValueError):
    """DOCX 模板无法解析（加密/损坏/非 docx）。"""


def _var_names(text: str) -> list[str]:
    names: list[str] = []
    for m in _VAR_ANY.finditer(text or ""):
        name = (m.group(1) or m.group(2) or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _paragraph_text(p: Any) -> str:
    return "".join(node.text or "" for node in p.iter(qn("w:t")))


def _part_features(root: Any) -> list[str]:
    import lxml.etree as etree

    xml = etree.tostring(root, encoding="unicode")
    return [label for marker, label in _FEATURE_MARKERS if marker in xml]


def _iter_parts(doc: Any):
    """正文 + 各节页眉页脚（模板变量可能在页眉页脚中声明）。"""
    yield "body", doc.element.body
    for index, section in enumerate(doc.sections):
        for kind, part in (("header", section.header), ("footer", section.footer)):
            try:
                root = part._element
            except Exception:  # noqa: BLE001 - 缺部件时跳过（不阻断导入）
                continue
            if root is not None:
                yield f"{kind}{index}", root


def extract_template(data: bytes) -> dict[str, Any]:
    """解析 DOCX 模板：返回 contentMd / fields / mapping / warnings / suggestedMode。"""
    if not data:
        raise DocxTemplateError("模板文件为空")
    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - python-docx 抛出的异常类型不统一
        raise DocxTemplateError(
            "无法解析该 Word 文件：请确认是未加密的 .docx（.doc 请先另存为 .docx）"
        ) from exc

    fields: list[str] = []
    mapping: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_warn: set[str] = set()

    def record(part: str, kind: str, index: int, text: str) -> None:
        for name in _var_names(text):
            if name not in fields:
                fields.append(name)
            mapping.append({
                "field": name, "part": part, "kind": kind, "index": index,
            })

    # —— 正文：段落与表格转 Markdown（供提纲推导/槽位识别/预览） ——
    blocks: list[str] = []
    body = doc.element.body
    p_index = 0
    t_index = 0
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            text = _paragraph_text(child).strip()
            record("body", "paragraph", p_index, text)
            p_index += 1
            if not text:
                continue
            style_el = child.find(qn("w:pPr") + "/" + qn("w:pStyle"))
            style = style_el.get(qn("w:val")) if style_el is not None else ""
            level = None
            m = re.match(r"^(?:Heading|标题)\s*(\d)$", style or "", re.I)
            if m:
                level = max(1, min(3, int(m.group(1))))
            blocks.append(f"{'#' * level} {text}" if level else text)
        elif tag == "tbl":
            rows: list[list[str]] = []
            for tr in child.iterchildren(qn("w:tr")):
                cells = []
                for tc in tr.iterchildren(qn("w:tc")):
                    cell_text = " ".join(
                        _paragraph_text(p).strip() for p in tc.iterchildren(qn("w:p"))
                    ).strip()
                    cells.append(cell_text)
                rows.append(cells)
            for cell in [c for row in rows for c in row]:
                record("body", "tableCell", t_index, cell)
            t_index += 1
            if rows:
                blocks.append("| " + " | ".join(rows[0]) + " |")
                blocks.append("| " + " | ".join("---" for _ in rows[0]) + " |")
                for row in rows[1:]:
                    blocks.append("| " + " | ".join(row) + " |")

    # —— 页眉页脚：变量参与替换，但不进入正文 Markdown ——
    for index, section in enumerate(doc.sections):
        for kind, part in (("header", section.header), ("footer", section.footer)):
            try:
                root = part._element
            except Exception:  # noqa: BLE001
                continue
            if root is None:
                continue
            for p_index2, p in enumerate(root.iter(qn("w:p"))):
                record(f"{kind}{index}", kind, p_index2, _paragraph_text(p))

    # —— 格式警告：无法安全保留的内容必须显式告知（不静默丢失） ——
    for part_name, root in _iter_parts(doc):
        for label in _part_features(root):
            key = f"{part_name}:{label}"
            if key in seen_warn:
                continue
            seen_warn.add(key)
            where = "页眉页脚" if part_name != "body" else "正文"
            warnings.append(f"{where}包含{label}，生成时以原件为基底保留；如需改写请人工确认")

    content_md = "\n\n".join(blocks).strip()
    if not content_md:
        raise DocxTemplateError("模板正文为空：无法提取可识别的文书内容")
    suggested_mode = "strict" if fields else "reference"
    if suggested_mode == "reference":
        warnings.append("未检测到 {{字段}} 或 【字段】 变量：按参考范本使用，AI 按原结构生成新正文")
    return {
        "contentMd": content_md,
        "fields": [{"name": name, "required": False} for name in fields],
        "fieldMapping": mapping,
        "formatWarnings": warnings,
        "suggestedMode": suggested_mode,
    }


def _replace_in_paragraph(p: Any, values: dict[str, str], unresolved: str) -> bool:
    """段落内跨 run 变量替换：合并识别后在首个受影响文本节点落值，其余清空。"""
    nodes = [node for node in p.iter(qn("w:t"))]
    if not nodes:
        return False
    combined = "".join(node.text or "" for node in nodes)
    if not _VAR_ANY.search(combined):
        return False

    def _sub(m: re.Match[str]) -> str:
        name = (m.group(1) or m.group(2) or "").strip()
        value = str(values.get(name) or "").strip()
        return value if value else unresolved

    replaced = _VAR_ANY.sub(_sub, combined)
    if replaced == combined:
        return False
    # 最小范围替换：整体写入首个文本节点，清空其余节点，保留首个 run 的格式
    nodes[0].text = replaced
    nodes[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for node in nodes[1:]:
        node.text = ""
    return True


def render_strict(base: bytes, values: dict[str, str], unresolved: str = "xx") -> tuple[bytes, int]:
    """在原 DOCX 字节上替换变量（strict 模式）。

    返回 (新字节, 替换段落数)。除被改写段落的文本节点外，其余 ZIP 部件逐字节复制，
    页眉页脚、图片、样式、编号与页面设置保持原样。
    """
    import zipfile

    from lxml import etree

    if not base:
        raise DocxTemplateError("模板原件缺失，无法按 strict 模式生成")
    try:
        with zipfile.ZipFile(io.BytesIO(base)) as archive:
            names = archive.namelist()
            targets = [
                name for name in names
                if name == "word/document.xml"
                or re.match(r"^word/(header|footer)\d*\.xml$", name)
            ]
            if "word/document.xml" not in targets:
                raise DocxTemplateError("模板原件缺少 document.xml")
            contents = {name: archive.read(name) for name in names}
    except zipfile.BadZipFile as exc:
        raise DocxTemplateError("模板原件不是有效的 DOCX（ZIP）文件") from exc

    changed = 0
    for name in targets:
        try:
            root = etree.fromstring(contents[name])
        except etree.XMLSyntaxError:
            continue
        for p in list(root.iter(qn("w:p"))):
            if _replace_in_paragraph(p, values, unresolved):
                changed += 1
        contents[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        with zipfile.ZipFile(io.BytesIO(base)) as source:
            for info in source.infolist():
                target.writestr(info, contents[info.filename])
    return out.getvalue(), changed


def render_reference(base: bytes, markdown: str, title: str | None = None) -> bytes:
    """以模板副本为基底替换正文（reference 模式）：保留页面设置、样式与页眉页脚。"""
    from .render import md_to_blocks

    try:
        doc = Document(io.BytesIO(base))
    except Exception as exc:  # noqa: BLE001
        raise DocxTemplateError("模板原件无法作为生成基底，请重新上传 .docx 模板") from exc

    body = doc.element.body
    sect_pr = body.find(qn("w:sectPr"))
    for child in list(body):
        if child is sect_pr:
            continue
        body.remove(child)

    blocks = md_to_blocks(markdown or "", title=title)
    for kind, payload in blocks:
        if kind == "h1":
            # 用模板自带的标题样式（保留字体/间距），缺样式时回落普通段落
            try:
                doc.add_heading(str(payload), level=1)
            except (KeyError, ValueError):
                doc.add_paragraph(str(payload))
        elif kind == "h2":
            try:
                doc.add_heading(str(payload), level=2)
            except (KeyError, ValueError):
                doc.add_paragraph(str(payload))
        elif kind == "li":
            doc.add_paragraph(f"· {payload}")
        elif kind == "table":
            header = list(payload.get("header") or [])
            rows = [list(row) for row in payload.get("rows") or []]
            if not header:
                continue
            table = doc.add_table(rows=1, cols=len(header))
            for index, cell in enumerate(header):
                table.rows[0].cells[index].text = str(cell)
            for row in rows:
                cells = table.add_row().cells
                for index, cell in enumerate(row[: len(cells)]):
                    cells[index].text = str(cell)
        else:
            doc.add_paragraph(str(payload))

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
