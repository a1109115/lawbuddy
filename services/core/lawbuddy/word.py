"""Local basic Word work copies with optimistic revisions and encrypted blobs."""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
import copy
from typing import Any

from . import audit, crypto, documents
from .cases import require_active_case
from .keyring import get_active_dek
from .storage import get_db
from .workspace import now_iso

MAX_MODEL_BYTES = 20_000_000
NODE_TYPES = {"doc", "paragraph", "heading", "text", "hardBreak", "bulletList", "orderedList",
              "listItem", "table", "tableRow", "tableCell", "tableHeader"}
MARK_TYPES = {"bold", "italic", "underline", "strike", "textStyle",
              "insertion", "deletion", "comment"}
_REVISION_MARKS = {"insertion", "deletion", "comment"}
_MAX_REVISION_FIELD = 2000

# 允许的本地字体与字号（法律文书常用仿宋/楷体/黑体；均为本机常见字体，不引入网络字体）
_ALLOWED_FONTS = {
    "SimSun", "Microsoft YaHei", "Times New Roman",
    "FangSong", "KaiTi", "SimHei",
}
_ALLOWED_SIZES = {"10.5pt", "12pt", "14pt", "16pt", "18pt", "22pt", "24pt", "28pt"}
# DOCX 常用中文字体别名 → 允许列表中的规范名；保证预览（eastAsia 字体）与编辑态字体一致
_FONT_ALIASES = {
    "宋体": "SimSun", "仿宋": "FangSong", "仿宋_GB2312": "FangSong",
    "FangSong_GB2312": "FangSong", "楷体": "KaiTi", "楷体_GB2312": "KaiTi",
    "黑体": "SimHei", "微软雅黑": "Microsoft YaHei",
}
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class WordError(ValueError):
    pass


def validate_model(model: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(model, dict) or model.get("type") != "doc":
        raise WordError("Word 模型必须是文档节点")
    if len(json.dumps(model, ensure_ascii=False).encode("utf-8")) > MAX_MODEL_BYTES:
        raise WordError("Word 模型超出大小限制")
    count = 0

    def walk(node, depth=0):
        nonlocal count
        count += 1
        if depth > 30 or count > 100_000 or not isinstance(node, dict) or node.get("type") not in NODE_TYPES:
            raise WordError("Word 模型节点不支持或超限")
        if set(node) - {"type", "attrs", "content", "text", "marks"}:
            raise WordError("Word 模型包含未知字段")
        text = node.get("text")
        if text is not None and (not isinstance(text, str) or len(text) > 1_000_000):
            raise WordError("Word 文本不合法")
        attrs = node.get("attrs") or {}
        if not isinstance(attrs, dict) or set(attrs) - {
            "level", "textAlign", "start", "colspan", "rowspan", "colwidth",
            "firstLineIndent", "spaceBefore", "spaceAfter", "lineSpacing",
        }:
            raise WordError("Word 样式不支持")
        if attrs.get("textAlign") not in (None, "left", "right", "center", "justify"):
            raise WordError("Word 对齐不支持")
        for key in ("firstLineIndent", "spaceBefore", "spaceAfter", "lineSpacing"):
            value = attrs.get(key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 400
            ):
                raise WordError("Word 段落间距/缩进不合法")
        if "level" in attrs and attrs["level"] not in (1, 2, 3):
            raise WordError("仅支持标题 1 至 3")
        if attrs.get("colspan", 1) != 1 or attrs.get("rowspan", 1) != 1:
            raise WordError("暂不支持合并单元格")
        if attrs.get("colwidth") is not None and (
            not isinstance(attrs["colwidth"], list) or
            any(not isinstance(width, int) or not 1 <= width <= 10000 for width in attrs["colwidth"])
        ):
            raise WordError("表格宽度不合法")
        marks = node.get("marks") or []
        if not isinstance(marks, list):
            raise WordError("Word 格式标记不合法")
        for mark in marks:
            if not isinstance(mark, dict) or mark.get("type") not in MARK_TYPES:
                raise WordError("Word 格式标记不支持")
            if set(mark) - {"type", "attrs"}:
                raise WordError("Word 格式字段不支持")
            style = mark.get("attrs") or {}
            if not isinstance(style, dict):
                raise WordError("Word 文本样式不支持")
            if mark["type"] in _REVISION_MARKS:
                if set(style) - {"author", "date", "revisionId", "body"}:
                    raise WordError("修订/批注标记不支持文本样式字段")
                for field in ("author", "date", "revisionId", "body"):
                    value = style.get(field)
                    if value is not None and (
                        not isinstance(value, str) or len(value) > _MAX_REVISION_FIELD
                    ):
                        raise WordError("修订/批注字段不合法")
            else:
                if set(style) - {"fontFamily", "fontSize", "lineHeight", "color", "backgroundColor"}:
                    raise WordError("Word 文本样式不支持")
            if style.get("fontFamily") not in (None, *_ALLOWED_FONTS):
                raise WordError("仅支持已批准的本地字体")
            if style.get("fontSize") not in (None, *_ALLOWED_SIZES):
                raise WordError("字号不支持")
            if style.get("lineHeight") not in (None, "1", "1.5", "2"):
                raise WordError("行距不支持")
            for key in ("color", "backgroundColor"):
                value = style.get(key)
                if value is not None and (not isinstance(value, str) or not _HEX_COLOR_RE.match(value)):
                    raise WordError("文字颜色格式不支持（应为 #RRGGBB）")
        children = node.get("content") or []
        if not isinstance(children, list):
            raise WordError("Word 子节点不合法")
        if node["type"] == "table" and depth > 1:
            raise WordError("暂不支持嵌套表格")
        allowed_children = {
            "doc": {"paragraph", "heading", "bulletList", "orderedList", "table"},
            "paragraph": {"text", "hardBreak"}, "heading": {"text", "hardBreak"},
            "bulletList": {"listItem"}, "orderedList": {"listItem"},
            "listItem": {"paragraph", "heading", "bulletList", "orderedList"},
            "table": {"tableRow"}, "tableRow": {"tableCell", "tableHeader"},
            "tableCell": {"paragraph", "heading", "bulletList", "orderedList"},
            "tableHeader": {"paragraph", "heading", "bulletList", "orderedList"},
            "text": set(), "hardBreak": set(),
        }
        for child in children:
            if not isinstance(child, dict) or child.get("type") not in allowed_children[node["type"]]:
                raise WordError("Word 节点层级不合法")
            walk(child, depth + 1)

    walk(model)
    return model


def text_model(text: str) -> dict[str, Any]:
    return {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}] if line else []}
        for line in text.replace("\r\n", "\n").split("\n")
    ]}


def model_text(model: dict[str, Any]) -> str:
    def walk(node):
        if node["type"] == "text":
            # 删除修订在「接受」前仍保留在模型里；纯文本视图按接受后的结果取，
            # 避免文书生成/差异比较把待删内容当成有效正文。
            if any(mark.get("type") == "deletion" for mark in node.get("marks") or []):
                return ""
            return node.get("text", "")
        if node["type"] == "hardBreak":
            return "\n"
        separator = "" if node["type"] in ("paragraph", "heading") else "\n"
        return separator.join(walk(child) for child in node.get("content", []))

    return walk(model)


def _paragraph_runs(node: dict[str, Any]):
    """枚举容器内连续的段落/标题子节点区段；表格等结构会切断区段。"""
    runs: list[list[int]] = []
    current: list[int] = []
    for index, child in enumerate(node.get("content", [])):
        # 中间夹带非段落节点（如表格）时区段必须断开，避免跨表格拼出虚假连续文本
        if child.get("type") in ("paragraph", "heading"):
            current.append(index)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    if runs:
        yield node, runs
    for child in node.get("content", []):
        yield from _paragraph_runs(child)


def _splice_content(node: dict[str, Any], start: int, end: int, replacement: str) -> list[dict[str, Any]]:
    """按 model_text 偏移替换段落内 [start, end) 片段，返回新内容数组。"""
    updated: list[dict[str, Any]] = []
    offset = 0
    inserted = False
    for child in node.get("content", []):
        value = child.get("text", "") if child["type"] == "text" else "\n"
        child_end = offset + len(value)
        if child_end <= start or offset >= end:
            if offset >= end and not inserted:
                if replacement:
                    updated.append({"type": "text", "text": replacement})
                inserted = True
            updated.append(child)
        else:
            if offset < start:
                updated.append({**child, "text": value[:start - offset]})
            if not inserted:
                if replacement:
                    updated.append({"type": "text", "text": replacement,
                                    **({"marks": copy.deepcopy(child["marks"])} if child.get("marks") else {})})
                inserted = True
            if child_end > end:
                updated.append({**child, "text": value[end - offset:]})
        offset = child_end
    if not inserted and replacement:
        updated.append({"type": "text", "text": replacement})
    return updated


def _normalize_ws(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", text or "")


def _locate_quote(model: dict[str, Any], quote: str):
    """忽略空白在段落区段内唯一定位引用，允许跨行（PDF 文本层每行一段）。

    返回 (容器节点, 区段内起始序号, 起始偏移, 结束序号, 结束偏移)；不唯一或不存在返回 None。
    """
    needle = _normalize_ws(quote)
    if not needle:
        return None
    found = []
    for container, runs in _paragraph_runs(model):
        for run in runs:
            haystack = ""
            positions: list[tuple[int, int]] = []
            for order, child_index in enumerate(run):
                text = model_text(container["content"][child_index])
                for char_index, char in enumerate(text):
                    if char.isspace():
                        continue
                    haystack += char
                    positions.append((order, char_index))
            start = haystack.find(needle)
            while start >= 0:
                found.append((container, run, positions[start], positions[start + len(needle) - 1]))
                if len(found) > 1:
                    return None
                start = haystack.find(needle, start + 1)
    return found[0] if len(found) == 1 else None


def _text_node(text: str, marks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def _risk_comment_body(risk: dict[str, Any]) -> str:
    """风险说明批注正文（含出处，便于导出后在 Word 里回溯）。"""
    parts = []
    if risk.get("riskNote"):
        parts.append(f"风险：{risk['riskNote']}")
    if risk.get("suggestion"):
        parts.append(f"建议：{risk['suggestion']}")
    cite = risk.get("cite")
    if isinstance(cite, dict) and cite.get("label"):
        parts.append(f"来源：{cite['label']}")
    if risk.get("claimStatus") and risk.get("claimStatus") != "supported":
        parts.append("状态：待人工核实（来源未完全核验）")
    return "；".join(parts)[:1900]


def _mark_span(paragraph: dict[str, Any], start: int, end: int,
               deletion_mark: dict[str, Any], replacement: str,
               insertion_mark: dict[str, Any],
               comment_mark: dict[str, Any] | None) -> None:
    """在单个段落的字符区间 [start, end) 上标记删除，并在区间末尾插入替换文本。"""
    children = paragraph.get("content") or []
    output: list[dict[str, Any]] = []
    offset = 0
    inserted = False
    for child in children:
        if child.get("type") != "text":
            output.append(child)
            offset += 1  # hardBreak 在 model_text 中记为换行
            if not inserted and offset >= end:
                if replacement:
                    marks = [insertion_mark] + ([comment_mark] if comment_mark else [])
                    output.append(_text_node(replacement, marks))
                inserted = True
            continue
        text = str(child.get("text") or "")
        marks = child.get("marks") or []
        node_start, node_end = offset, offset + len(text)
        inside_start = min(len(text), max(0, start - node_start))
        inside_end = min(len(text), max(0, end - node_start))
        if inside_end < inside_start:
            inside_end = inside_start
        if inside_start > 0:
            output.append(_text_node(text[:inside_start], marks))
        if inside_end > inside_start:
            deleted_marks = list(marks) + [deletion_mark]
            if comment_mark is not None and not replacement:
                deleted_marks.append(comment_mark)
            output.append(_text_node(text[inside_start:inside_end], deleted_marks))
        if not inserted and node_end >= end:
            if replacement:
                marks = [insertion_mark] + ([comment_mark] if comment_mark else [])
                output.append(_text_node(replacement, marks))
            inserted = True
        if inside_end < len(text):
            output.append(_text_node(text[inside_end:], marks))
        offset = node_end
    if not inserted and replacement:
        marks = [insertion_mark] + ([comment_mark] if comment_mark else [])
        output.append(_text_node(replacement, marks))
    paragraph["content"] = output


def _attach_revision_marks(model: dict[str, Any], quote: str, replacement: str,
                           author: str, date: str, revision_id: str,
                           comment_body: str) -> dict[str, Any]:
    """把唯一命中的原文片段标记为删除修订，并在其后插入替换文本（插入修订 + 批注）。"""
    located = _locate_quote(model, quote)
    if located is None:
        raise WordError("QUOTE_NOT_FOUND：无法在正文中唯一定位待修改条款")
    container, run, (start_order, start_char), (end_order, end_char) = located
    if start_order != end_order:
        raise WordError("CROSS_PARAGRAPH_REVISION：跨段落修订暂不支持，请分段应用")
    paragraph = container["content"][run[start_order]]
    deletion_mark = {"type": "deletion", "attrs": {
        "author": author, "date": date, "revisionId": revision_id,
    }}
    insertion_mark = {"type": "insertion", "attrs": {
        "author": author, "date": date, "revisionId": revision_id,
    }}
    comment_mark = {"type": "comment", "attrs": {
        "author": author, "date": date, "revisionId": f"c-{revision_id}",
        "body": comment_body,
    }} if comment_body else None
    _mark_span(paragraph, start_char, end_char + 1, deletion_mark,
               str(replacement or ""), insertion_mark, comment_mark)
    return model


def suggest_replacement(target_id: str, quote: str, replacement: str,
                        expected_revision: int, request_id: str,
                        risk: dict[str, Any] | None = None) -> dict[str, Any]:
    """合同风险建议以 Word 修订 + 批注写入工作副本（不直接改正文）。"""
    current = get_model(target_id)
    if current["revision"] != expected_revision:
        raise WordError("VERSION_CONFLICT")
    risk = risk or {}
    revision_id = f"rv-{uuid.uuid4().hex[:12]}"
    model = copy.deepcopy(current["model"])
    _attach_revision_marks(
        model, quote, str(replacement or ""),
        author=str(risk.get("author") or "LawBuddy")[:80],
        date=now_iso(),
        revision_id=revision_id,
        comment_body=_risk_comment_body(risk),
    )
    validate_model(model)
    application = None
    if risk.get("runId") and risk.get("riskId"):
        application = {"runId": str(risk["runId"]), "riskId": str(risk["riskId"])}
    return save_model(target_id, model, expected_revision, request_id, "edit",
                      _risk_application=application)


def _resolve_marks(node: dict[str, Any], action: str, revision_ids: set[str],
                   drop_types: set[str]) -> list[dict[str, Any]]:
    """递归处理修订：返回新的子节点列表（插入/删除文本按接受/拒绝增删）。"""
    children: list[dict[str, Any]] = []
    for child in node.get("content") or []:
        if child.get("type") == "text":
            marks = child.get("marks") or []
            revision = next(
                (mark for mark in marks if mark.get("type") in ("insertion", "deletion")), None
            )
            if revision is not None:
                attrs = revision.get("attrs") or {}
                revision_id = str(attrs.get("revisionId") or "")
                if not revision_ids or revision_id in revision_ids:
                    if revision["type"] == "insertion" and action == "reject":
                        continue  # 拒绝插入：移除新增文本
                    if revision["type"] == "deletion" and action == "accept":
                        continue  # 接受删除：移除原文
                    marks = [mark for mark in marks
                             if mark.get("type") not in drop_types]
            if marks != (child.get("marks") or []):
                child = {**child, "marks": marks} if marks else {
                    key: value for key, value in child.items() if key != "marks"
                }
            children.append(child)
        else:
            resolved = {**child}
            resolved["content"] = _resolve_marks(child, action, revision_ids, drop_types)
            children.append(resolved)
    return children


def resolve_revisions(model: dict[str, Any], action: str,
                      revision_ids: list[str] | None = None) -> dict[str, Any]:
    """接受或拒绝修订；未指定 revisionIds 时处理全部修订。返回新模型（不改原对象）。"""
    if action not in ("accept", "reject"):
        raise WordError("修订处理动作不支持（accept / reject）")
    resolved = copy.deepcopy(model)
    resolved["content"] = _resolve_marks(
        resolved, action, {str(item) for item in revision_ids or []},
        {"insertion", "deletion"},
    )
    validate_model(resolved)
    return resolved


def resolve_comments(model: dict[str, Any], comment_ids: list[str] | None = None) -> dict[str, Any]:
    """删除批注锚点（正文保留）；未指定 id 时清除全部批注。"""
    targets = {str(item) for item in comment_ids or []}

    def strip(node: dict[str, Any]) -> list[dict[str, Any]]:
        children = []
        for child in node.get("content") or []:
            if child.get("type") == "text":
                marks = child.get("marks") or []
                kept = [
                    mark for mark in marks
                    if mark.get("type") != "comment"
                    or (targets and str((mark.get("attrs") or {}).get("revisionId") or "") not in targets)
                ]
                if kept != marks:
                    child = {**child, "marks": kept} if kept else {
                        key: value for key, value in child.items() if key != "marks"
                    }
                children.append(child)
            else:
                children.append({**child, "content": strip(child)})
        return children

    resolved = copy.deepcopy(model)
    resolved["content"] = strip(resolved)
    validate_model(resolved)
    return resolved


_COMMENTS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
)
_COMMENTS_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
)


def _comments_xml(comments: list[dict[str, Any]]) -> bytes:
    from xml.sax.saxutils import escape

    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n',
        f'<w:comments xmlns:w="{ns}">',
    ]
    for comment in comments:
        author = escape(str(comment["author"]), {'"': "&quot;"})
        date = escape(str(comment["date"]), {'"': "&quot;"})
        body = escape(str(comment["body"]))
        parts.append(
            f'<w:comment w:id="{comment["id"]}" w:author="{author}" w:date="{date}">'
            '<w:p><w:r><w:t xml:space="preserve">'
            f'{body}'
            "</w:t></w:r></w:p></w:comment>"
        )
    parts.append("</w:comments>")
    return "".join(parts).encode("utf-8")


def _inject_comments(data: bytes, comments: list[dict[str, Any]]) -> bytes:
    """给已生成的 DOCX 补 comments.xml、内容类型与关系（python-docx 不支持批注）。"""
    import re as _re

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        items = [(info, archive.read(info.filename)) for info in archive.infolist()]
    files = {info.filename: payload for info, payload in items}

    content_types = files["[Content_Types].xml"].decode("utf-8")
    if "/word/comments.xml" not in content_types:
        override = (
            f'<Override PartName="/word/comments.xml" ContentType="{_COMMENTS_CONTENT_TYPE}"/>'
        )
        content_types = content_types.replace("</Types>", override + "</Types>")

    rels_name = "word/_rels/document.xml.rels"
    rels = files[rels_name].decode("utf-8")
    if _COMMENTS_REL_TYPE not in rels:
        used = {int(value) for value in _re.findall(r'Id="rId(\d+)"', rels)}
        rel = (
            f'<Relationship Id="rId{max(used, default=0) + 1}" Type="{_COMMENTS_REL_TYPE}" '
            'Target="comments.xml"/>'
        )
        rels = rels.replace("</Relationships>", rel + "</Relationships>")

    files["[Content_Types].xml"] = content_types.encode("utf-8")
    files[rels_name] = rels.encode("utf-8")
    has_part = "word/comments.xml" in files
    files["word/comments.xml"] = _comments_xml(comments)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, _payload in items:
            archive.writestr(info.filename, files[info.filename])
        if not has_part:
            archive.writestr("word/comments.xml", files["word/comments.xml"])
    return out.getvalue()


def to_docx(model: dict[str, Any]) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    from .render import apply_doc_layout

    validate_model(model)
    doc = Document()
    # 与导出/预览共用同一套法律文书版式（A4、页边距、标题与正文字号行距）
    apply_doc_layout(doc)
    alignments = {"left": WD_ALIGN_PARAGRAPH.LEFT, "right": WD_ALIGN_PARAGRAPH.RIGHT,
                  "center": WD_ALIGN_PARAGRAPH.CENTER, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}

    def apply_background(run, hex_color: str) -> None:
        """文字背景高亮：写入 w:shd（Word 任意底色），不依赖有限的高亮枚举。"""
        rpr = run._element.get_or_add_rPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color.lstrip("#").upper())
        rpr.append(shd)

    comments: list[dict[str, Any]] = []
    revision_seq = [0]
    comment_seq = [0]

    def revision_meta(mark: dict[str, Any]) -> tuple[str, str]:
        attrs = mark.get("attrs") or {}
        author = str(attrs.get("author") or "LawBuddy")[:80].replace("\n", " ")
        date = str(attrs.get("date") or now_iso())[:40]
        return author, date

    def paragraph(container, node, style=None):
        p = container.add_paragraph(style=style)
        attrs = node.get("attrs") or {}
        p.alignment = alignments.get(attrs.get("textAlign"))
        # 段落级排版（首行缩进 / 段前段后 / 固定行距）按模型写回，保证再次预览时版式不变
        if attrs.get("firstLineIndent"):
            p.paragraph_format.first_line_indent = Pt(float(attrs["firstLineIndent"]))
        if attrs.get("spaceBefore"):
            p.paragraph_format.space_before = Pt(float(attrs["spaceBefore"]))
        if attrs.get("spaceAfter"):
            p.paragraph_format.space_after = Pt(float(attrs["spaceAfter"]))
        if attrs.get("lineSpacing"):
            p.paragraph_format.line_spacing = Pt(float(attrs["lineSpacing"]))
        open_comment: dict[str, Any] | None = None
        open_key: str | None = None

        def close_comment() -> None:
            nonlocal open_comment
            if open_comment is None:
                return
            end = OxmlElement("w:commentRangeEnd")
            end.set(qn("w:id"), str(open_comment["id"]))
            p._p.append(end)
            ref_run = OxmlElement("w:r")
            rpr = OxmlElement("w:rPr")
            vert = OxmlElement("w:vertAlign")
            vert.set(qn("w:val"), "superscript")
            rpr.append(vert)
            ref_run.append(rpr)
            reference = OxmlElement("w:commentReference")
            reference.set(qn("w:id"), str(open_comment["id"]))
            ref_run.append(reference)
            p._p.append(ref_run)
            open_comment = None

        def open_comment_range(mark: dict[str, Any]) -> str | None:
            nonlocal open_comment
            mark_attrs = mark.get("attrs") or {}
            body = str(mark_attrs.get("body") or "").strip()
            if not body:
                return None
            comment_seq[0] += 1
            info = {
                "id": comment_seq[0],
                "author": str(mark_attrs.get("author") or "LawBuddy")[:80],
                "date": str(mark_attrs.get("date") or now_iso())[:40],
                "body": body[:1900],
            }
            comments.append(info)
            start = OxmlElement("w:commentRangeStart")
            start.set(qn("w:id"), str(info["id"]))
            p._p.append(start)
            open_comment = info
            return str(info["id"])

        for child in node.get("content", []):
            if child["type"] == "hardBreak":
                close_comment()
                open_key = None
                p.add_run().add_break()
                continue
            if child["type"] != "text":
                raise WordError("段落包含不支持的节点")
            marks = child.get("marks") or []
            comment = next((mark for mark in marks if mark["type"] == "comment"), None)
            key: str | None = None
            if comment is not None:
                comment_attrs = comment.get("attrs") or {}
                key = str(comment_attrs.get("revisionId") or comment_attrs.get("body") or "") or None
            if key != open_key:
                close_comment()
                open_key = key
                if comment is not None and open_comment_range(comment) is None:
                    open_key = None

            run = p.add_run(str(child.get("text") or ""))
            for mark in marks:
                kind = mark["type"]
                if kind in ("insertion", "deletion", "comment"):
                    continue
                if kind == "strike":
                    # python-docx 的删除线挂在 font 上，且不提供 Run.strike
                    run.font.strike = True
                elif kind in ("bold", "italic", "underline"):
                    setattr(run, kind, True)
                else:
                    style_attrs = mark.get("attrs") or {}
                    if style_attrs.get("fontFamily"):
                        run.font.name = style_attrs["fontFamily"]
                        # python-docx 只写 ascii/hAnsi；中文按 eastAsia 渲染，必须同步写入，
                        # 否则编辑后中文会回退到默认字体，与预览的字体不一致。
                        rfonts = run._element.get_or_add_rPr().get_or_add_rFonts()
                        rfonts.set(qn("w:eastAsia"), style_attrs["fontFamily"])
                    if style_attrs.get("fontSize"):
                        run.font.size = Pt(float(style_attrs["fontSize"].replace("pt", "")))
                    if style_attrs.get("lineHeight"):
                        p.paragraph_format.line_spacing = float(style_attrs["lineHeight"])
                    if style_attrs.get("color"):
                        run.font.color.rgb = RGBColor.from_string(style_attrs["color"].lstrip("#").upper())
                    if style_attrs.get("backgroundColor"):
                        apply_background(run, style_attrs["backgroundColor"])

            revision = next((mark for mark in marks if mark["type"] in ("insertion", "deletion")), None)
            if revision is not None:
                author, date = revision_meta(revision)
                revision_seq[0] += 1
                wrapper = OxmlElement("w:ins" if revision["type"] == "insertion" else "w:del")
                wrapper.set(qn("w:id"), str(revision_seq[0]))
                wrapper.set(qn("w:author"), author)
                wrapper.set(qn("w:date"), date)
                p._p.remove(run._r)
                wrapper.append(run._r)
                if revision["type"] == "deletion":
                    for text_el in list(run._r.iter(qn("w:t"))):
                        text_el.tag = qn("w:delText")
                p._p.append(wrapper)
        close_comment()

    def blocks(container, nodes, list_style=None):
        for node in nodes:
            kind = node["type"]
            if kind in ("paragraph", "heading"):
                style = f"Heading {node.get('attrs', {}).get('level', 1)}" if kind == "heading" else list_style
                paragraph(container, node, style)
            elif kind in ("bulletList", "orderedList"):
                blocks(container, node.get("content", []), "List Bullet" if kind == "bulletList" else "List Number")
            elif kind == "listItem":
                blocks(container, node.get("content", []), list_style)
            elif kind == "table":
                rows = node.get("content", [])
                cols = len(rows[0].get("content", [])) if rows else 0
                if not rows or not cols or any(len(row.get("content", [])) != cols for row in rows):
                    raise WordError("表格必须为非空矩形")
                table = container.add_table(rows=len(rows), cols=cols)
                table.style = "Table Grid"
                for r, row in enumerate(rows):
                    for c, cell in enumerate(row["content"]):
                        target = table.cell(r, c)
                        blocks(target, cell.get("content", []))
                        if len(target.paragraphs) > 1 and not target.paragraphs[0].text:
                            target._element.remove(target.paragraphs[0]._element)
            else:
                raise WordError("Word 正文节点不支持")

    blocks(doc, model.get("content", []))
    stream = io.BytesIO()
    doc.save(stream)
    data = stream.getvalue()
    if comments:
        data = _inject_comments(data, comments)
    return data


def from_docx(data: bytes, simplify: bool = False) -> tuple[dict[str, Any], list[str]]:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    if len(data) > 30_000_000:
        raise WordError("DOCX 超过 30MB 限制")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 10000 or sum(info.file_size for info in infos) > 200_000_000:
            raise WordError("DOCX 解压体积超限")
        if any("vbaProject" in info.filename for info in infos):
            raise WordError("不支持宏文档")
        xml = archive.read("word/document.xml").decode("utf-8")
        comments_xml = (
            archive.read("word/comments.xml").decode("utf-8")
            if "word/comments.xml" in archive.namelist() else ""
        )
    unsupported = []
    for marker, label in (("<w:drawing", "图片"), ("<w:fldChar", "域代码"), ("<w:gridSpan", "合并单元格"),
                          ("<w:vMerge", "合并单元格"), ("<w:headerReference", "页眉"),
                          ("<w:footerReference", "页脚")):
        if marker in xml and label not in unsupported:
            unsupported.append(label)
    if unsupported and not simplify:
        raise WordError("UNSUPPORTED_WORD_FEATURE：" + "、".join(unsupported) + "；请确认创建简化副本")
    doc = Document(io.BytesIO(data))
    alignments = {WD_ALIGN_PARAGRAPH.LEFT: "left", WD_ALIGN_PARAGRAPH.RIGHT: "right",
                  WD_ALIGN_PARAGRAPH.CENTER: "center", WD_ALIGN_PARAGRAPH.JUSTIFY: "justify"}

    comment_map: dict[str, dict[str, str]] = {}
    if comments_xml:
        from lxml import etree

        comment_root = etree.fromstring(comments_xml.encode("utf-8"))
        for element in comment_root.iter():
            if etree.QName(element).localname != "comment":
                continue
            comment_id = element.get(qn("w:id"))
            if comment_id is None:
                continue
            body = "".join(
                node.text or "" for node in element.iter()
                if etree.QName(node).localname == "t"
            )
            comment_map[str(comment_id)] = {
                "author": element.get(qn("w:author")) or "未知",
                "date": element.get(qn("w:date")) or "",
                "body": body,
            }

    def para(p):
        from lxml import etree
        from docx.text.run import Run

        level = documents._docx_heading_level(p)
        content: list[dict[str, Any]] = []
        active_comments: list[str] = []

        def run_node(element, revision: dict[str, Any] | None, text: str) -> None:
            run = Run(element, p)
            marks = [{"type": name} for name in ("bold", "italic", "underline")
                     if getattr(run, name)]
            if run.font.strike:
                marks.append({"type": "strike"})
            style = {}
            # Word 对中文按 rFonts@eastAsia 渲染（docx-preview 也如此）。只看 run.font.name
            # 会读到西文字体（如 Times New Roman），导致编辑态中文回退成宋体、与预览不一致。
            rfonts = run._element.rPr.rFonts if run._element.rPr is not None else None
            east_asia = rfonts.get(qn("w:eastAsia")) if rfonts is not None else None
            font_name = east_asia if east_asia and _CJK_RE.search(text) else run.font.name
            normalized = _FONT_ALIASES.get(font_name or "", font_name)
            if normalized in _ALLOWED_FONTS:
                style["fontFamily"] = normalized
            if run.font.size and f"{run.font.size.pt:g}pt" in _ALLOWED_SIZES:
                style["fontSize"] = f"{run.font.size.pt:g}pt"
            spacing = p.paragraph_format.line_spacing
            if isinstance(spacing, float) and spacing in (1, 1.5, 2):
                style["lineHeight"] = str(spacing).rstrip("0").rstrip(".")
            try:
                rgb = run.font.color.rgb if run.font.color and run.font.color.type is not None else None
            except (AttributeError, ValueError):
                rgb = None
            if rgb is not None and _HEX_COLOR_RE.match(f"#{rgb}"):
                style["color"] = f"#{rgb}"
            rpr = run._element.rPr
            if rpr is not None:
                shd = rpr.find(qn("w:shd"))
                fill = shd.get(qn("w:fill")) if shd is not None else None
                if fill and _HEX_COLOR_RE.match(f"#{fill}"):
                    style["backgroundColor"] = f"#{fill.lower()}"
            if style:
                marks.append({"type": "textStyle", "attrs": style})
            if revision is not None:
                marks.append(revision)
            for comment_id in active_comments:
                info = comment_map.get(str(comment_id))
                if info is None:
                    continue
                marks.append({"type": "comment", "attrs": {
                    "author": info["author"], "date": info["date"],
                    "revisionId": str(comment_id), "body": info["body"],
                }})
            content.append({"type": "text", "text": text, **({"marks": marks} if marks else {})})

        def walk(element, revision: dict[str, Any] | None = None) -> None:
            tag = etree.QName(element).localname
            if tag == "commentRangeStart":
                comment_id = element.get(qn("w:id"))
                if comment_id and comment_id not in active_comments:
                    active_comments.append(comment_id)
            elif tag == "commentRangeEnd":
                comment_id = element.get(qn("w:id"))
                if comment_id in active_comments:
                    active_comments.remove(comment_id)
            elif tag in ("ins", "del"):
                rev = {
                    "type": "insertion" if tag == "ins" else "deletion",
                    "attrs": {
                        "author": element.get(qn("w:author")) or "未知",
                        "date": element.get(qn("w:date")) or "",
                        "revisionId": element.get(qn("w:id")) or "",
                    },
                }
                for child in element:
                    walk(child, rev)
            elif tag == "r":
                texts = [
                    node.text or "" for node in element.iter()
                    if etree.QName(node).localname in ("t", "delText") and node.text
                ]
                text = "".join(texts)
                if text:
                    run_node(element, revision, text)
            elif tag in ("hyperlink", "smartTag", "sdt", "sdtContent", "fldSimple", "customXml"):
                for child in element:
                    walk(child, revision)

        for child in p._p:
            walk(child)

        attrs = {"level": level} if level and level <= 3 else {}
        if p.alignment in alignments:
            attrs["textAlign"] = alignments[p.alignment]
        fram = p.paragraph_format
        first = fram.first_line_indent
        if first is not None and first > 0:
            attrs["firstLineIndent"] = round(first.pt, 1)
        else:
            # 中文 Word 常用 w:ind@firstLineChars（python-docx 不解析），按字符数换算成 pt
            ppr = p._p.pPr
            ind = ppr.find(qn("w:ind")) if ppr is not None else None
            chars = ind.get(qn("w:firstLineChars")) if ind is not None else None
            if chars:
                try:
                    chars_count = float(chars) / 100
                except ValueError:
                    chars_count = 0
                if chars_count > 0:
                    size_pt = next((r.font.size.pt for r in p.runs if r.font.size), 16.0)
                    attrs["firstLineIndent"] = round(chars_count * size_pt, 1)
        for key, value in (("spaceBefore", fram.space_before), ("spaceAfter", fram.space_after)):
            if value is not None and value.pt > 0:
                attrs[key] = round(value.pt, 1)
        spacing_pt = getattr(fram.line_spacing, "pt", None)
        if spacing_pt:
            attrs["lineSpacing"] = round(spacing_pt, 1)
        return {"type": "heading" if level and level <= 3 else "paragraph",
                **({"attrs": attrs} if attrs else {}), "content": content}

    content = []
    for element in doc.element.body:
        if element.tag.endswith("}p"):
            paragraph = Paragraph(element, doc)
            node = para(paragraph)
            style_name = paragraph.style.name if paragraph.style else ""
            list_type = "bulletList" if style_name.startswith("List Bullet") else (
                "orderedList" if style_name.startswith("List Number") else None
            )
            if list_type:
                item = {"type": "listItem", "content": [node]}
                if content and content[-1]["type"] == list_type:
                    content[-1]["content"].append(item)
                else:
                    content.append({"type": list_type, "content": [item]})
            else:
                content.append(node)
        elif element.tag.endswith("}tbl"):
            table = Table(element, doc)
            if any(cell.tables for row in table.rows for cell in row.cells):
                if not simplify:
                    raise WordError("UNSUPPORTED_WORD_FEATURE：嵌套表格；请确认创建简化副本")
                unsupported.append("嵌套表格")
            content.append({"type": "table", "content": [
                {"type": "tableRow", "content": [
                    {"type": "tableCell", "content": [para(p) for p in cell.paragraphs] or [{"type": "paragraph"}]}
                    for cell in row.cells
                ]} for row in table.rows
            ]})
    model = {"type": "doc", "content": content or [{"type": "paragraph"}]}
    validate_model(model)
    return model, unsupported


def _target(target_id: str, writable: bool = False) -> dict[str, Any]:
    row = get_db().query_one("SELECT * FROM word_work_copies WHERE id = ?", (target_id,))
    if not row:
        raise WordError("工作副本不存在")
    if writable:
        require_active_case(row["case_id"])
        if row["draft_id"]:
            from .docgen import _require_writable_draft
            _require_writable_draft(row["draft_id"])
    return row


def get_model(target_id: str) -> dict[str, Any]:
    row = _target(target_id)
    version = get_db().query_one(
        "SELECT * FROM word_work_copy_versions WHERE target_id = ? AND revision = ?", (target_id, row["revision"]),
    )
    if not version:
        raise WordError("工作副本版本不存在")
    return {"id": target_id, "caseId": row["case_id"], "title": row["title"], "revision": row["revision"],
            "model": json.loads(version["model_json"]), "sourceVersionId": row["source_version_id"],
            "documentId": row["document_id"], "draftId": row["draft_id"]}


def open_editor(target_id: str) -> dict[str, Any]:
    from . import editor

    row = _target(target_id, writable=True)
    return editor.open_session("document_work_copy", target_id, row["case_id"])


def create_work_copy(case_id: str, document_id: str | None = None, draft_id: str | None = None,
                     simplify: bool = False) -> dict[str, Any]:
    require_active_case(case_id)
    if bool(document_id) == bool(draft_id):
        raise WordError("请选择一个原文或文书草稿")
    warnings = []
    version_id = None
    base_bytes: bytes | None = None
    if draft_id:
        from . import docgen
        draft = docgen.get_draft(draft_id)
        if draft["caseId"] != case_id or not draft.get("bodyMd"):
            raise WordError("草稿归属不符或尚无正文")
        docgen._require_writable_draft(draft_id)
        model, title = text_model(draft["bodyMd"]), draft["docType"]
    else:
        doc = get_db().query_one("SELECT * FROM documents WHERE id = ? AND case_id = ?", (document_id, case_id))
        if not doc or doc["status"] not in ("ready", "partial"):
            raise WordError("原文归属不符或未解析")
        version_id = doc["active_version_id"]
        title = doc["title"]
        if doc["kind"] == "docx":
            version = get_db().query_one("SELECT blob_uri FROM document_versions WHERE id = ?", (version_id,))
            uri = documents._resolve_blob_uri(version_id, version["blob_uri"])
            path = documents._workspace_root() / uri
            data = crypto.decrypt_bytes(path, crypto.derive_file_key(get_active_dek())) if crypto.is_encrypted(path) else path.read_bytes()
            try:
                model, warnings = from_docx(data, simplify)
            except WordError as exc:
                # 复杂版式（图片/页眉页脚/修订）在简化模型下不可编辑，但原件字节会完整保留，
                # 增量写回只更新被改动的段落，其余部件不动；因此可自动降级为简化副本。
                if not simplify and str(exc).startswith("UNSUPPORTED_WORD_FEATURE"):
                    model, warnings = from_docx(data, simplify=True)
                    warnings = [
                        *warnings,
                        "原件含复杂版式：仅被编辑的段落会更新，页眉页脚、图片等其余部件保持原样",
                    ]
                else:
                    raise
            # 初始副本直接保留原件字节：页眉页脚、图片、编号等在第一次保存时也不丢
            base_bytes = data
        else:
            model = text_model("\n".join(page["text"] for page in documents.get_pages(document_id)))
            warnings = ["基于解析文本创建基础排版副本，不保留原版式"]
    target_id = str(uuid.uuid4())
    with get_db().transaction() as conn:
        conn.execute("INSERT INTO word_work_copies(id, case_id, document_id, source_version_id, draft_id, title, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (target_id, case_id, document_id, version_id, draft_id, title, now_iso()))
        if draft_id:
            conn.execute("UPDATE word_work_copies SET draft_base_updated_at = ? WHERE id = ?",
                         (draft["updatedAt"], target_id))
    try:
        saved = save_model(
            target_id,
            model,
            0,
            str(uuid.uuid4()),
            "create",
            _base_bytes=base_bytes,
            _base_model=model if base_bytes is not None else None,
        )
    except Exception:
        with get_db().transaction() as conn:
            conn.execute("DELETE FROM word_work_copies WHERE id = ?", (target_id,))
        raise
    return {**saved, "warnings": warnings}


def _read_work_blob(uri: str) -> bytes:
    """读取工作副本历史 blob（加密则解密）。"""
    path = documents._workspace_root() / uri
    if crypto.is_encrypted(path):
        return crypto.decrypt_bytes(path, crypto.derive_file_key(get_active_dek()))
    return path.read_bytes()


def _patch_base(
    row: dict[str, Any],
    expected_revision: int,
    base_bytes: bytes | None,
    base_model: dict[str, Any] | None,
) -> tuple[bytes | None, dict[str, Any] | None]:
    """增量写回的基线：(上一版 DOCX 字节, 上一版模型)；不可得时返回 (None, None)。"""
    if expected_revision <= 0:
        return base_bytes, base_model
    previous = get_db().query_one(
        "SELECT blob_uri, model_json FROM word_work_copy_versions WHERE target_id = ? AND revision = ?",
        (row["id"], expected_revision),
    )
    if not previous:
        return None, None
    try:
        return _read_work_blob(str(previous["blob_uri"])), json.loads(previous["model_json"])
    except (OSError, ValueError):
        return None, None


def save_model(target_id: str, model: dict[str, Any], expected_revision: int, request_id: str,
               kind: str = "edit", session_id: str | None = None,
               _risk_application: dict[str, str] | None = None,
               _base_bytes: bytes | None = None,
               _base_model: dict[str, Any] | None = None) -> dict[str, Any]:
    row = _target(target_id, writable=True)
    validate_model(model)
    if kind not in ("create", "edit", "autosave", "restore"):
        raise WordError("保存类型不支持")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise WordError("clientRequestId 不合法")
    model_json = json.dumps(model, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(model_json.encode("utf-8")).hexdigest()
    from .contract import _store_blob_bytes
    from . import docxpatch

    warnings: list[str] = []
    data: bytes | None = None
    if kind == "create" and _base_bytes is not None:
        # 初次建副本：直接以原件字节为基线（页眉页脚/图片/编号等原样保留）
        data = _base_bytes
    elif kind != "restore":
        base_bytes, base_model = _patch_base(row, expected_revision, _base_bytes, _base_model)
        if base_bytes is not None and base_model is not None:
            try:
                data, _changed = docxpatch.patch_docx(base_bytes, base_model, model)
            except docxpatch.PatchNotPossible as exc:
                # 结构变化/不可重建内容：不阻断保存，但明确告知版式可能未保留
                warnings.append(f"{exc}；本次保存按模型重建文档，页眉页脚、图片等版式可能未保留")
                data = None
    if data is None:
        data = to_docx(model)
    blob_uri = _store_blob_bytes(documents._workspace_root(), data, hashlib.sha256(data).hexdigest())
    ts = now_iso()
    with get_db().transaction() as conn:
        if session_id:
            session = conn.execute(
                "SELECT target_id, target_kind, closed_at FROM editor_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session or session["target_id"] != target_id or session["target_kind"] != "document_work_copy" or session["closed_at"]:
                raise WordError("编辑会话归属不符或已关闭")
        prior = conn.execute("SELECT payload_hash FROM word_work_copy_versions WHERE target_id = ? AND request_id = ?",
                             (target_id, request_id)).fetchone()
        if prior:
            if prior["payload_hash"] != digest:
                raise WordError("IDEMPOTENCY_CONFLICT")
            return get_model(target_id)
        changed = conn.execute("UPDATE word_work_copies SET revision = revision + 1 WHERE id = ? AND revision = ?",
                               (target_id, expected_revision))
        if changed.rowcount != 1:
            raise WordError("VERSION_CONFLICT")
        if row["draft_id"] and kind != "create":
            from . import docgen

            draft = conn.execute("SELECT * FROM docgen_drafts WHERE id = ?", (row["draft_id"],)).fetchone()
            if draft["updated_at"] != row["draft_base_updated_at"]:
                raise WordError("VERSION_CONFLICT：草稿已被其他编辑或 AI 更新")
            body = model_text(model)
            blocking = docgen.audit_placeholders(body)
            last = conn.execute("SELECT MAX(version_no) AS version_no FROM docgen_draft_versions WHERE draft_id = ?",
                                (row["draft_id"],)).fetchone()["version_no"] or 0
            conn.execute(
                "INSERT INTO docgen_draft_versions(id, draft_id, version_no, body_md, outline_json, slots_json, kind, "
                "created_at, word_model_json, word_blob_uri) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), row["draft_id"], last + 1, body, draft["outline_json"], draft["slots_json"],
                 "autosave" if kind == "autosave" else "edit", ts, model_json, blob_uri),
            )
            conn.execute("UPDATE docgen_drafts SET body_md = ?, blocking_count = ?, status = 'draft', updated_at = ? WHERE id = ?",
                         (body, len(blocking), ts, row["draft_id"]))
            conn.execute("UPDATE word_work_copies SET draft_base_updated_at = ? WHERE id = ?", (ts, target_id))
        conn.execute("INSERT INTO word_work_copy_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (str(uuid.uuid4()), target_id, expected_revision + 1, model_json, blob_uri,
                      kind, request_id, digest, ts))
        if _risk_application:
            previous = conn.execute(
                "SELECT revision FROM word_risk_applications WHERE run_id = ? AND risk_id = ? AND target_id = ?",
                (_risk_application["runId"], _risk_application["riskId"], target_id),
            ).fetchone()
            if previous:
                raise WordError("PATCH_ALREADY_APPLIED")
            conn.execute("INSERT INTO word_risk_applications VALUES (?, ?, ?, ?, ?, ?)",
                         (request_id, _risk_application["runId"], _risk_application["riskId"],
                          target_id, expected_revision + 1, ts))
        audit.append(conn, action="word.save", resource_type="word_work_copy", resource_id=target_id,
                     case_id=row["case_id"], metadata={"revision": expected_revision + 1, "kind": kind})
        if session_id and kind == "autosave":
            from . import editor

            editor.mark_autosave(conn, session_id, ts)
    result = get_model(target_id)
    if warnings:
        result["warnings"] = warnings
    return result


def list_versions(target_id: str) -> list[dict[str, Any]]:
    _target(target_id)
    return get_db().query("SELECT revision, kind, created_at AS createdAt FROM word_work_copy_versions "
                          "WHERE target_id = ? ORDER BY revision DESC", (target_id,))


def list_work_copies(case_id: str) -> list[dict[str, Any]]:
    rows = get_db().query("SELECT id FROM word_work_copies WHERE case_id = ? AND revision > 0 ORDER BY created_at DESC",
                         (case_id,))
    return [get_model(row["id"]) for row in rows]


def restore_version(target_id: str, revision: int, expected_revision: int) -> dict[str, Any]:
    _target(target_id, writable=True)
    historical = get_db().query_one(
        "SELECT model_json FROM word_work_copy_versions WHERE target_id = ? AND revision = ?", (target_id, revision),
    )
    if not historical:
        raise WordError("工作副本历史版本不存在")
    return save_model(target_id, json.loads(historical["model_json"]), expected_revision, str(uuid.uuid4()), "restore")


def diff_versions(target_id: str, from_revision: int, to_revision: int) -> dict[str, Any]:
    from .editor import diff_texts

    _target(target_id)
    versions = []
    for revision in (from_revision, to_revision):
        row = get_db().query_one("SELECT model_json FROM word_work_copy_versions WHERE target_id = ? AND revision = ?",
                                (target_id, revision))
        if not row:
            raise WordError("工作副本历史版本不存在")
        versions.append(model_text(json.loads(row["model_json"])))
    return {**diff_texts(*versions), "fromLabel": f"v{from_revision}", "toLabel": f"v{to_revision}"}


def apply_patches(target_id: str, patches: list[dict[str, Any]], expected_revision: int, request_id: str,
                  *, _risk_application: dict[str, str] | None = None) -> dict[str, Any]:
    target = _target(target_id, writable=True)
    saved = get_model(target_id)
    if saved["revision"] != expected_revision:
        raise WordError("VERSION_CONFLICT")
    if not isinstance(patches, list) or not 1 <= len(patches) <= 100:
        raise WordError("补丁数量不合法")
    prior = get_db().query_one("SELECT revision FROM word_work_copy_versions WHERE target_id = ? AND request_id = ?",
                              (target_id, request_id))
    if prior:
        raise WordError("PATCH_ALREADY_APPLIED：此请求已应用，请刷新版本，不重复修改")
    model = copy.deepcopy(saved["model"])
    touched_nodes: set[int] = set()

    for patch in patches:
        quote, replacement = patch.get("quote"), patch.get("replacement")
        if not isinstance(quote, str) or not quote or not isinstance(replacement, str) or len(replacement) > 100_000:
            raise WordError("补丁原文和替换文本不合法")
        located = _locate_quote(model, quote)
        if located is None:
            raise WordError("SOURCE_MISMATCH：补丁原文无法唯一定位")
        container, run, (start_order, start_char), (end_order, end_char) = located
        first_index, last_index = run[start_order], run[end_order]
        children = container["content"]
        first, last = children[first_index], children[last_index]
        span = {id(child) for child in children[first_index:last_index + 1]}
        if touched_nodes & span:
            raise WordError("PATCH_CONFLICT：同一段落存在重叠修改，请逐项确认")
        touched_nodes |= span
        if start_order == end_order:
            first["content"] = _splice_content(first, start_char, end_char + 1, replacement)
            continue
        # 跨行引用（PDF 文本层每行一段）：首段保留前缀并写入替换文本，
        # 被完整覆盖的中间段落删除，末段保留后缀。
        # 末段后缀若与新文本尾部重复（替换文本常已包含该句），先裁掉重叠再拼接，
        # 否则会出现“……全部费用。\n，并承担由此产生的全部费用。”这类重复语句。
        tail = model_text(last)[end_char + 1:]
        if replacement and tail:
            replacement_norm = _normalize_ws(replacement)
            for size in range(len(tail), 5, -1):
                head_norm = _normalize_ws(tail[:size])
                if len(head_norm) >= 6 and replacement_norm.endswith(head_norm):
                    tail = tail[size:]
                    break
        first["content"] = _splice_content(first, start_char, len(model_text(first)), replacement)
        last["content"] = [{"type": "text", "text": tail}] if tail else []
        drop = set(range(first_index + 1, last_index))
        for index in (first_index, last_index):
            if not model_text(children[index]):
                drop.add(index)
        if drop:
            container["content"] = [child for index, child in enumerate(children) if index not in drop]
    return save_model(target_id, model, expected_revision, request_id, "edit", _risk_application=_risk_application)


def export(target_id: str, dest_path: str) -> dict[str, Any]:
    from pathlib import Path

    row = _target(target_id)
    version = get_db().query_one("SELECT blob_uri FROM word_work_copy_versions WHERE target_id = ? AND revision = ?",
                                (target_id, row["revision"]))
    blob = documents._workspace_root() / version["blob_uri"]
    Path(dest_path).write_bytes(crypto.decrypt_bytes(blob, crypto.derive_file_key(get_active_dek())))
    return {"revision": row["revision"], "exported": True}
