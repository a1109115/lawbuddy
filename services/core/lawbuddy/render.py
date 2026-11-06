"""导出渲染：块序列 → DOCX / PDF / XLSX / PPTX（DEV-MOD-010 / G-008 / C-007 / MOD-010）。

块序列与 `artifacts._body_to_blocks` 一致：h1 / h2 / p / li / table。
- DOCX：python-docx，中文按系统宋体回退（PRD §10.2 字体策略）；
- PDF：reportlab 内置中文 CID 字体 STSong-Light（Adobe CID，不捆绑字体文件，
  规避中文字体授权问题；三平台渲染一致）；
- XLSX：openpyxl 原生 SpreadsheetML 工作簿（非改扩展名的假文件），
  表格落为真实单元格区域，标题/小节/段落为合并单元格；
- PPTX：python-pptx 原生 PresentationML 演示文稿（非改扩展名的假文件），
  h1 → 封面页，h2 → 小节页，table → 原生表格形状，超长表格分页续排；
  可视化产物（viz_to_pptx）用原生形状/连接线绘制时间线、人物关系图和流程图，
  引证写入演讲者备注；
- 导出文件由 Python Core 写入磁盘，目标路径不进入 Renderer（REQ-DOC-005）。
"""

from __future__ import annotations

import math
import re
from typing import Any

# 块类型：("h1"|"h2"|"p"|"li", str) 或 ("table", {"header": [...], "rows": [[...]]})
Block = tuple[str, Any]

_PIPE_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DIVIDER = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def md_to_blocks(md: str, title: str | None = None) -> list[Block]:
    """受限 Markdown → 块序列（标题 / 段落 / 列表 / 管道表格）。

    仅覆盖文书与模板实际产出的子集；复杂排版（嵌套列表、图片、HTML）不做解析，
    按纯文本段落保留，避免静默丢内容。
    """
    blocks: list[Block] = []
    if title:
        blocks.append(("h1", title))
    para: list[str] = []
    rows: list[list[str]] = []

    def flush_para() -> None:
        if para:
            blocks.append(("p", "\n".join(para).strip()))
            para.clear()

    def flush_table() -> None:
        if rows:
            blocks.append(("table", {"header": rows[0], "rows": rows[1:]}))
            rows.clear()

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if _PIPE_TABLE_ROW.match(line):
            if _TABLE_DIVIDER.match(line):
                continue  # markdown 表格分隔行
            flush_para()
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
            continue
        flush_table()
        if not line.strip():
            flush_para()
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            flush_para()
            blocks.append(("h1" if len(m.group(1)) == 1 else "h2", m.group(2).strip()))
            continue
        if re.match(r"^\s*([-*·]|\d+[.、])\s+", line):
            flush_para()
            blocks.append(("li", re.sub(r"^\s*([-*·]|\d+[.、])\s+", "", line).strip()))
            continue
        para.append(line.strip())
    flush_table()
    flush_para()
    return blocks


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

# 法律文书版式（与 docgen.DEFAULT_LAYOUT 一致）：A4、法定页边距、
# 标题宋体 22pt 加粗居中、正文仿宋 16pt 1.5 倍行距。
# 版式必须落在 DOCX 样式里：原件预览（docx-preview）按文件真实样式渲染，
# 只在界面侧加样式会让「预览」与「编辑/导出」两套观感不一致。
PAGE_WIDTH_CM = 21.0
PAGE_HEIGHT_CM = 29.7
PAGE_MARGINS_CM = {"top": 3.7, "bottom": 3.5, "left": 2.8, "right": 2.6}
TITLE_FONT = "SimSun"
TITLE_SIZE_PT = 22
BODY_FONT = "FangSong"
BODY_SIZE_PT = 16
BODY_LINE_SPACING = 1.5


def _set_style_fonts(style: Any, font_name: str) -> None:
    """统一设置样式的西文/中文/复杂文种字体，并清掉主题字体引用。

    python-docx 默认模板的标题样式同时带 `w:asciiTheme` 等主题字体属性；Word 会优先用
    显式字体，但 docx-preview 会优先解析主题字体（表现为标题回退成 Calibri），所以必须
    显式移除这些属性才能保证预览与 Word 一致。
    """
    from docx.oxml.ns import qn

    style.font.name = font_name
    rfonts = style.element.rPr.rFonts
    for attr in ("w:asciiTheme", "w:eastAsiaTheme", "w:hAnsiTheme", "w:cstheme"):
        rfonts.attrib.pop(qn(attr), None)
    rfonts.set(qn("w:eastAsia"), font_name)


def apply_doc_layout(doc: Any) -> None:
    """把法律文书版式写入 python-docx 文档：页面尺寸、页边距、标题与正文样式。"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    section = doc.sections[0]
    section.page_width = Cm(PAGE_WIDTH_CM)
    section.page_height = Cm(PAGE_HEIGHT_CM)
    section.top_margin = Cm(PAGE_MARGINS_CM["top"])
    section.bottom_margin = Cm(PAGE_MARGINS_CM["bottom"])
    section.left_margin = Cm(PAGE_MARGINS_CM["left"])
    section.right_margin = Cm(PAGE_MARGINS_CM["right"])

    normal = doc.styles["Normal"]
    _set_style_fonts(normal, BODY_FONT)
    normal.font.size = Pt(BODY_SIZE_PT)
    normal.paragraph_format.line_spacing = BODY_LINE_SPACING

    # 内置 Heading 样式默认是主题蓝 + 西文字体；逐级改成黑色中文标题。
    # 段落样式与链接的字符样式（Heading N Char）都要改：正文里没有直接格式时，
    # 预览器会读链接字符样式，只改段落样式仍会显示蓝色小标题。
    heading_specs = (
        ("Heading 1", TITLE_FONT, TITLE_SIZE_PT, WD_ALIGN_PARAGRAPH.CENTER),
        ("Heading 2", BODY_FONT, BODY_SIZE_PT, WD_ALIGN_PARAGRAPH.LEFT),
        ("Heading 3", BODY_FONT, BODY_SIZE_PT, WD_ALIGN_PARAGRAPH.LEFT),
    )
    for style_name, font_name, size_pt, align in heading_specs:
        style = doc.styles[style_name]
        _set_style_fonts(style, font_name)
        style.font.size = Pt(size_pt)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.alignment = align
        style.paragraph_format.line_spacing = BODY_LINE_SPACING

        char_style_name = f"{style_name} Char"
        if char_style_name in [s.name for s in doc.styles]:
            char_style = doc.styles[char_style_name]
            _set_style_fonts(char_style, font_name)
            char_style.font.size = Pt(size_pt)
            char_style.font.bold = True
            char_style.font.color.rgb = RGBColor(0, 0, 0)


def _cite_label(cite: Any) -> str:
    """引证块的来源标签（文档名 + 页码，由 Claim Ledger 生成）。"""
    if isinstance(cite, dict):
        return str(cite.get("label") or "").strip()
    return ""


def _add_footnote_reference(paragraph: Any, footnote_id: int) -> None:
    """在段落末尾插入 Word 原生脚注引用（w:footnoteReference）。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    run = paragraph.add_run()
    rpr = run._r.get_or_add_rPr()
    vert = OxmlElement("w:vertAlign")
    vert.set(qn("w:val"), "superscript")
    rpr.append(vert)
    ref = OxmlElement("w:footnoteReference")
    ref.set(qn("w:id"), str(footnote_id))
    run._r.append(ref)


_FOOTNOTES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
)
_FOOTNOTES_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
)


def _footnotes_xml(labels: list[str]) -> bytes:
    """生成 word/footnotes.xml：分隔符 + 每条引证一个原生脚注。"""
    from xml.sax.saxutils import escape

    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    empty_p = '<w:p><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n',
        f'<w:footnotes xmlns:w="{ns}">',
        f'<w:footnote w:type="separator" w:id="-1">{empty_p}<w:r><w:separator/></w:r></w:p></w:footnote>',
        f'<w:footnote w:type="continuationSeparator" w:id="0">{empty_p}'
        '<w:r><w:continuationSeparator/></w:r></w:p></w:footnote>',
    ]
    fonts = '<w:rFonts w:ascii="FangSong" w:eastAsia="FangSong" w:hAnsi="FangSong"/>'
    for index, label in enumerate(labels, start=1):
        text = escape(f" {label}", {'"': "&quot;"})
        parts.append(
            f'<w:footnote w:id="{index}"><w:p><w:pPr>'
            '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
            f'<w:r><w:rPr>{fonts}<w:sz w:val="18"/><w:vertAlign w:val="superscript"/></w:rPr>'
            '<w:footnoteRef/></w:r>'
            f'<w:r><w:rPr>{fonts}<w:sz w:val="18"/></w:rPr>'
            f'<w:t xml:space="preserve">{text}</w:t></w:r></w:p></w:footnote>'
        )
    parts.append("</w:footnotes>")
    return "".join(parts).encode("utf-8")


def _inject_footnotes(dest_path: str, labels: list[str]) -> None:
    """把 footnotes.xml、内容类型、关系和 settings 写进已保存的 DOCX。

    python-docx 不支持脚注，只能在保存后补部件；除新增/需要的四个条目外，
    其余 zip 部件字节保持不变（不重建正文，避免破坏原有版式）。
    """
    import re as _re
    import zipfile
    from pathlib import Path

    path = Path(dest_path)
    with zipfile.ZipFile(path, "r") as archive:
        items = [(info, archive.read(info.filename)) for info in archive.infolist()]
    data = {info.filename: payload for info, payload in items}

    content_types = data.get("[Content_Types].xml", b"").decode("utf-8")
    if "/word/footnotes.xml" not in content_types:
        override = (
            f'<Override PartName="/word/footnotes.xml" ContentType="{_FOOTNOTES_CONTENT_TYPE}"/>'
        )
        content_types = content_types.replace("</Types>", override + "</Types>")

    rels_name = "word/_rels/document.xml.rels"
    rels = data.get(rels_name, b"").decode("utf-8")
    if _FOOTNOTES_REL_TYPE not in rels:
        used = {int(m) for m in _re.findall(r'Id="rId(\d+)"', rels)}
        rel = (
            f'<Relationship Id="rId{max(used, default=0) + 1}" Type="{_FOOTNOTES_REL_TYPE}" '
            'Target="footnotes.xml"/>'
        )
        rels = rels.replace("</Relationships>", rel + "</Relationships>")

    settings_name = "word/settings.xml"
    settings = data.get(settings_name, b"").decode("utf-8")
    if settings and "<w:footnotePr" not in settings:
        note_pr = '<w:footnotePr><w:footnote w:id="-1"/><w:footnote w:id="0"/></w:footnotePr>'
        # w:footnotePr 在 CT_Settings 中位于 w:compat 之前；没有则追加到末尾
        if "<w:compat" in settings:
            settings = settings.replace("<w:compat", note_pr + "<w:compat", 1)
        else:
            settings = settings.replace("</w:settings>", note_pr + "</w:settings>")

    data["[Content_Types].xml"] = content_types.encode("utf-8")
    data[rels_name] = rels.encode("utf-8")
    if settings:
        data[settings_name] = settings.encode("utf-8")
    has_part = "word/footnotes.xml" in data
    data["word/footnotes.xml"] = _footnotes_xml(labels)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, _payload in items:
            archive.writestr(info.filename, data[info.filename])
        if not has_part:
            archive.writestr("word/footnotes.xml", data["word/footnotes.xml"])


def blocks_to_docx(blocks: list[Block], dest_path: str) -> None:
    from docx import Document

    doc = Document()
    apply_doc_layout(doc)
    labels: list[str] = []
    last_paragraph = None

    for kind, val in blocks:
        if kind == "h1":
            last_paragraph = doc.add_heading(str(val), level=1)
        elif kind == "h2":
            last_paragraph = doc.add_heading(str(val), level=2)
        elif kind == "p":
            last_paragraph = doc.add_paragraph(str(val))
        elif kind == "li":
            last_paragraph = doc.add_paragraph(f"· {val}", style="List Bullet")
        elif kind == "cite":
            label = _cite_label(val)
            if label and last_paragraph is not None:
                labels.append(label)
                _add_footnote_reference(last_paragraph, len(labels))
        elif kind == "table":
            header = val["header"]
            table = doc.add_table(rows=1, cols=len(header))
            table.style = "Table Grid"
            for i, h in enumerate(header):
                table.rows[0].cells[i].text = str(h)
            for row in val["rows"]:
                cells = table.add_row().cells
                for i, c in enumerate(row):
                    cells[i].text = str(c)
            last_paragraph = None
    doc.save(dest_path)
    if labels:
        _inject_footnotes(dest_path, labels)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

PDF_FONT = "STSong-Light"  # reportlab 内置 Adobe CID 中文字体（无需外部字体文件）
_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def _esc(text: Any) -> str:
    return "".join(_ESC.get(ch, ch) for ch in str(text)).replace("\n", "<br/>")


def blocks_to_pdf(blocks: list[Block], dest_path: str) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    try:
        pdfmetrics.getFont(PDF_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT))

    body = ParagraphStyle("body", fontName=PDF_FONT, fontSize=10.5, leading=18, spaceAfter=6)
    h1 = ParagraphStyle("h1", fontName=PDF_FONT, fontSize=17, leading=24, spaceAfter=10)
    h2 = ParagraphStyle("h2", fontName=PDF_FONT, fontSize=13, leading=20, spaceBefore=6, spaceAfter=6)
    li = ParagraphStyle("li", parent=body, leftIndent=14, bulletIndent=4)
    cell = ParagraphStyle("cell", fontName=PDF_FONT, fontSize=9.5, leading=14)
    src = ParagraphStyle("src", fontName=PDF_FONT, fontSize=8.5, leading=12,
                         textColor=colors.HexColor("#5B6B84"), leftIndent=12)

    flow: list[Any] = []
    for kind, val in blocks:
        if kind == "h1":
            flow.append(Paragraph(_esc(val), h1))
        elif kind == "h2":
            flow.append(Paragraph(_esc(val), h2))
        elif kind == "p":
            flow.append(Paragraph(_esc(val), body))
        elif kind == "li":
            flow.append(Paragraph(_esc(val), li, bulletText="·"))
        elif kind == "cite":
            label = _cite_label(val)
            if label:
                flow.append(Paragraph(_esc(f"来源：{label}"), src))
        elif kind == "table":
            header = [Paragraph(_esc(h), cell) for h in val["header"]]
            data = [header] + [[Paragraph(_esc(c), cell) for c in row] for row in val["rows"]]
            width = (A4[0] - 4 * cm) / max(1, len(header))
            table = Table(data, colWidths=[width] * len(header), repeatRows=1)
            table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#b9c6da")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f6fd")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            flow.append(Spacer(1, 4))
            flow.append(table)
            flow.append(Spacer(1, 8))

    SimpleDocTemplate(
        dest_path, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
        title="LawBuddy 导出",
    ).build(flow)


# ---------------------------------------------------------------------------
# XLSX（openpyxl 原生 SpreadsheetML：表格为真实单元格区域）
# ---------------------------------------------------------------------------

_SHEET_NAME_BAD = re.compile(r"[\\/*?:\[\]]")


def _xlsx_sheet_name(blocks: list[Block]) -> str:
    title = next((str(v) for k, v in blocks if k == "h1"), "") or "导出"
    name = _SHEET_NAME_BAD.sub("_", title).strip() or "导出"
    return name[:31]  # Excel 工作表名上限 31 字符


def blocks_to_xlsx(blocks: list[Block], dest_path: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = _xlsx_sheet_name(blocks)

    # 布局宽度：取最宽表格列数（至少 4 列），标题/段落行横向合并到该宽度
    width = max([4] + [len(v["header"]) for k, v in blocks if k == "table"])
    thin = Side(style="thin", color="B9C6DA")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    fill_title = PatternFill("solid", start_color="2B6FD4", end_color="2B6FD4")
    fill_head = PatternFill("solid", start_color="F2F6FD", end_color="F2F6FD")
    wrap_top = Alignment(wrap_text=True, vertical="top")

    def merge_row(row: int) -> None:
        if width > 1:
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)

    row = 1
    last_cell = None
    for kind, val in blocks:
        if kind == "h1":
            cell = ws.cell(row=row, column=1, value=str(val))
            cell.font = Font(bold=True, size=14, color="FFFFFF")
            cell.fill = fill_title
            cell.alignment = Alignment(vertical="center")
            merge_row(row)
            ws.row_dimensions[row].height = 28
            row += 2
            last_cell = None
        elif kind == "h2":
            cell = ws.cell(row=row, column=1, value=str(val))
            cell.font = Font(bold=True, size=12)
            cell.fill = fill_head
            merge_row(row)
            row += 1
            last_cell = None
        elif kind in ("p", "li"):
            text = str(val) if kind == "p" else f"· {val}"
            cell = ws.cell(row=row, column=1, value=text)
            cell.alignment = wrap_top
            merge_row(row)
            row += 1
            last_cell = cell
        elif kind == "cite":
            label = _cite_label(val)
            if label and last_cell is not None:
                last_cell.value = f"{last_cell.value or ''}（来源：{label}）"
        elif kind == "table":
            header = [str(h) for h in val["header"]]
            for i, h in enumerate(header, start=1):
                cell = ws.cell(row=row, column=i, value=h)
                cell.font = Font(bold=True)
                cell.fill = fill_head
                cell.border = border
                cell.alignment = wrap_top
            row += 1
            for data_row in val["rows"]:
                for i in range(1, len(header) + 1):
                    raw = data_row[i - 1] if i - 1 < len(data_row) else ""
                    cell = ws.cell(row=row, column=i, value=str(raw))
                    cell.border = border
                    cell.alignment = wrap_top
                row += 1
            row += 1  # 表格后留一空行
            last_cell = None

    ws.column_dimensions["A"].width = 42
    for col in range(2, width + 1):
        from openpyxl.utils import get_column_letter

        ws.column_dimensions[get_column_letter(col)].width = 20
    ws.freeze_panes = "A2"
    wb.save(dest_path)


# ---------------------------------------------------------------------------
# PPTX（python-pptx 原生 PresentationML：h1 封面 / h2 小节页 / table 原生表格）
# ---------------------------------------------------------------------------

PPTX_FONT = "Microsoft YaHei"  # 演示文稿默认中文字体（系统自带，宋体在投影场景可读性差）
_PPTX_TABLE_MAX_ROWS = 10  # 单页表格数据行上限，超出分页续排（16:9 版面高度约束）


def _pptx_style_run(run: Any, size: int, bold: bool = False, color: str | None = None) -> None:
    """设置 run 字体（latin + 东亚字形，保证中文按微软雅黑渲染）。"""
    from pptx.dml.color import RGBColor
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    run.font.name = PPTX_FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        rPr.append(ea)
    ea.set("typeface", PPTX_FONT)


def _pptx_fill_cell(cell: Any, text: str, size: int, bold: bool = False,
                    bg: str | None = None, color: str | None = None) -> None:
    if bg:
        from pptx.dml.color import RGBColor

        cell.fill.solid()
        cell.fill.fore_color.rgb = RGBColor.from_string(bg)
    cell.margin_left = cell.margin_right = 45720  # 0.05in
    cell.margin_top = cell.margin_bottom = 22860
    cell.vertical_anchor = 1  # MSO_ANCHOR.MIDDLE
    tf = cell.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = str(text)
    for run in p.runs:
        _pptx_style_run(run, size, bold, color)


def blocks_to_pptx(blocks: list[Block], dest_path: str) -> None:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    title = next((str(v) for k, v in blocks if k == "h1"), None)
    cover = prs.slides.add_slide(prs.slide_layouts[0])
    cover.shapes.title.text = title or "导出"
    for para in cover.shapes.title.text_frame.paragraphs:
        for run in para.runs:
            _pptx_style_run(run, 32, bold=True, color="1F3864")
    subtitle = cover.placeholders[1].text_frame.paragraphs[0]
    subtitle.text = "LawBuddy · 导出"
    for run in subtitle.runs:
        _pptx_style_run(run, 14, color="5B6B84")

    def add_section_slide(title_text: str) -> Any:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title_text
        for para in slide.shapes.title.text_frame.paragraphs:
            for run in para.runs:
                _pptx_style_run(run, 24, bold=True, color="1F3864")
        body = slide.placeholders[1].text_frame
        body.word_wrap = True
        body.clear()
        return slide

    def add_table_slide(table: dict, title_text: str) -> None:
        header = [str(h) for h in table["header"]]
        rows = [[str(c) for c in r] for r in table["rows"]]
        chunks = [rows[i:i + _PPTX_TABLE_MAX_ROWS]
                  for i in range(0, len(rows), _PPTX_TABLE_MAX_ROWS)] or [[]]
        for page, chunk in enumerate(chunks):
            slide_title = title_text if page == 0 else f"{title_text}（续 {page}）"
            slide = prs.slides.add_slide(prs.slide_layouts[5])  # Title Only
            slide.shapes.title.text = slide_title
            for para in slide.shapes.title.text_frame.paragraphs:
                for run in para.runs:
                    _pptx_style_run(run, 24, bold=True, color="1F3864")
            n_rows = len(chunk) + 1
            n_cols = max(1, len(header))
            gf = slide.shapes.add_table(
                n_rows, n_cols, Inches(0.6), Inches(1.5),
                Inches(12.1), Inches(0.5 + 0.45 * len(chunk)),
            )
            tbl = gf.table
            for i, h in enumerate(header):
                _pptx_fill_cell(tbl.cell(0, i), h, 12, bold=True, bg="2B6FD4", color="FFFFFF")
            for r, data_row in enumerate(chunk, start=1):
                for c in range(n_cols):
                    raw = data_row[c] if c < len(data_row) else ""
                    _pptx_fill_cell(tbl.cell(r, c), raw, 11)
            tbl.columns[0].width = Inches(min(4.0, 12.1 / n_cols))

    def flush_pending(pending: list[tuple[str, Any]], title_text: str) -> None:
        """把累积的 p/li 落成一页小节（无小节标题时归入「概述」页）。"""
        if not pending:
            return
        slide = add_section_slide(title_text)
        tf = slide.placeholders[1].text_frame
        first = True
        for kind, val in pending:
            para = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            if kind == "src":
                para.text = f"来源：{val}"
                para.level = 1
                para.space_after = Pt(6)
                for run in para.runs:
                    _pptx_style_run(run, 10, color="5B6B84")
                continue
            para.text = str(val) if kind == "p" else f"· {val}"
            para.level = 0 if kind == "p" else 1
            para.space_after = Pt(6)
            for run in para.runs:
                _pptx_style_run(run, 14)
        pending.clear()

    current_section: str | None = None
    pending: list[tuple[str, Any]] = []  # 当前小节累积的 p/li
    for kind, val in blocks:
        if kind == "h1":
            continue  # 已上封面
        if kind == "h2":
            flush_pending(pending, current_section or "概述")
            current_section = str(val)
        elif kind == "table":
            flush_pending(pending, current_section or "概述")
            add_table_slide(val, current_section or "概述")
        elif kind == "cite":
            label = _cite_label(val)
            if label:
                pending.append(("src", label))
        else:
            pending.append((kind, val))
    flush_pending(pending, current_section or "概述")

    prs.save(dest_path)


# ---------------------------------------------------------------------------
# 可视化产物 → PPTX 原生形状（时间线 / 人物关系图 / 流程图）
#
# 不再用「表格 + 要点」近似图形：时间线落为轴线 + 节点卡片，关系图落为
# 椭圆/矩形节点 + 带箭头连接线，流程图按层级自动布局并落为原生形状。
# 每条节点/边的 cite 写入演讲者备注，保证导出后仍能回溯原文出处；
# needs_human / pending 节点在文字上追加「（待核实）」，不单靠颜色表达。
# ---------------------------------------------------------------------------

_VIZ_SLIDE_W = 13.333
_VIZ_SLIDE_H = 7.5
_VIZ_PAGE_NODES = 16
_VIZ_VERIFY_SUFFIX = "（待核实）"
_VIZ_TEXT_COLOR = "1F3864"
_VIZ_LINE_COLOR = "7A8BA3"


def _viz_cite_labels(items: list[Any]) -> list[str]:
    labels: list[str] = []
    for item in items:
        cite = item.get("cite") if isinstance(item, dict) else None
        if isinstance(cite, dict) and str(cite.get("label") or "").strip():
            labels.append(str(cite["label"]).strip())
    return list(dict.fromkeys(labels))


def _viz_add_notes(slide: Any, items: list[Any], extra: list[str] | None = None) -> None:
    labels = _viz_cite_labels(items)
    lines = [f"- {label}" for label in labels]
    for text in extra or []:
        if text:
            lines.append(f"- {text}")
    if lines:
        slide.notes_slide.notes_text_frame.text = "来源与说明：\n" + "\n".join(lines)


def _viz_node_label(node: dict[str, Any], key: str = "label") -> str:
    text = str(node.get(key) or node.get("name") or node.get("id") or "").strip()
    if node.get("status") in ("needs_human", "pending") and _VIZ_VERIFY_SUFFIX not in text:
        text += _VIZ_VERIFY_SUFFIX
    return text


def _viz_set_title(slide: Any, text: str) -> None:
    slide.shapes.title.text = text
    for paragraph in slide.shapes.title.text_frame.paragraphs:
        for run in paragraph.runs:
            _pptx_style_run(run, 22, bold=True, color="1F3864")


def _viz_add_shape(slide: Any, shape_kind: str, x: float, y: float, w: float, h: float,
                   text: str, fill: str, line: str, size: int = 9) -> Any:
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt

    shape_map = {
        "diamond": MSO_SHAPE.DIAMOND,
        "circle": MSO_SHAPE.OVAL,
        "person": MSO_SHAPE.OVAL,
        "stadium": MSO_SHAPE.ROUNDED_RECTANGLE,
        "round": MSO_SHAPE.ROUNDED_RECTANGLE,
        "org": MSO_SHAPE.ROUNDED_RECTANGLE,
    }
    shape = slide.shapes.add_shape(
        shape_map.get(shape_kind, MSO_SHAPE.RECTANGLE),
        Inches(x), Inches(y), Inches(w), Inches(h),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(fill)
    shape.line.color.rgb = RGBColor.from_string(line)
    shape.line.width = Pt(1)
    try:
        shape.shadow.inherit = False
    except Exception:
        pass
    frame = shape.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = Inches(0.05)
    frame.margin_top = frame.margin_bottom = Inches(0.02)
    frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    frame.text = text
    for paragraph in frame.paragraphs:
        paragraph.alignment = PP_ALIGN.CENTER
        for run in paragraph.runs:
            _pptx_style_run(run, size, color=_VIZ_TEXT_COLOR)
    return shape


def _viz_add_text(slide: Any, x: float, y: float, w: float, h: float, text: str,
                  size: int = 8) -> Any:
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches

    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = Inches(0.02)
    frame.margin_top = frame.margin_bottom = 0
    paragraph = frame.paragraphs[0]
    paragraph.alignment = PP_ALIGN.CENTER
    paragraph.text = text
    for run in paragraph.runs:
        _pptx_style_run(run, size, color=_VIZ_LINE_COLOR)
    return box


def _viz_connector(slide: Any, x1: float, y1: float, x2: float, y2: float,
                   arrow: bool = True, dashed: bool = False, trim: float = 0.0) -> Any:
    from pptx.dml.color import RGBColor
    from pptx.enum.dml import MSO_LINE_DASH_STYLE
    from pptx.enum.shapes import MSO_CONNECTOR
    from pptx.oxml.ns import qn
    from pptx.util import Inches, Pt

    if trim:
        dx, dy = x2 - x1, y2 - y1
        distance = math.hypot(dx, dy) or 1.0
        ux, uy = dx / distance, dy / distance
        x1, y1 = x1 + ux * trim, y1 + uy * trim
        x2, y2 = x2 - ux * trim, y2 - uy * trim

    connector = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2)
    )
    connector.line.color.rgb = RGBColor.from_string(_VIZ_LINE_COLOR)
    connector.line.width = Pt(1.25)
    if dashed:
        connector.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    if arrow:
        ln = connector.line._get_or_add_ln()
        ln.append(ln.makeelement(qn("a:tailEnd"), {"type": "triangle", "w": "med", "len": "med"}))
    return connector


def _viz_timeline_slides(prs: Any, timeline: dict[str, Any]) -> None:
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    nodes = [
        node for node in (timeline.get("nodes") or [])
        if isinstance(node, dict) and str(node.get("event") or "").strip()
    ]
    if not nodes:
        return
    per_slide = 6
    for page, offset in enumerate(range(0, len(nodes), per_slide)):
        chunk = nodes[offset:offset + per_slide]
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        _viz_set_title(slide, "时间线" if page == 0 else f"时间线（续 {page}）")
        _viz_add_notes(slide, chunk)

        axis_y = 3.75
        count = len(chunk)
        _viz_connector(slide, 0.75, axis_y, 12.58, axis_y, arrow=False)

        box_w = min(1.9, max(1.1, 11.83 / count - 0.25))
        box_h = 1.3
        for index, node in enumerate(chunk):
            cx = 0.75 + 11.83 * (index + 0.5) / count
            above = index % 2 == 0
            box_y = axis_y - 0.38 - box_h if above else axis_y + 0.38
            _viz_connector(slide, cx, axis_y, cx, box_y + box_h if above else box_y, arrow=False)
            marker = slide.shapes.add_shape(
                MSO_SHAPE.OVAL,
                Inches(cx - 0.09), Inches(axis_y - 0.09), Inches(0.18), Inches(0.18),
            )
            marker.fill.solid()
            marker.fill.fore_color.rgb = _viz_rgb("2B6FD4")
            marker.line.fill.background()

            date = str(node.get("date") or "").strip()
            event = _viz_node_label(node, "event")
            text = f"{date}\n{event}" if date else event
            shape = _viz_add_shape(
                slide, "round", cx - box_w / 2, box_y, box_w, box_h, text,
                "F2F6FD", "2B6FD4", size=8,
            )
            # 日期加粗，事件保持常规；两段在卡片内居中
            paragraphs = shape.text_frame.paragraphs
            if date and len(paragraphs) >= 2:
                for run in paragraphs[0].runs:
                    run.font.bold = True


def _viz_relation_slides(prs: Any, graph: dict[str, Any]) -> None:
    nodes = [
        node for node in (graph.get("nodes") or [])
        if isinstance(node, dict) and str(node.get("name") or "").strip()
    ]
    edges = [edge for edge in (graph.get("edges") or []) if isinstance(edge, dict)]
    if not nodes:
        return
    per_slide = 10
    for page, offset in enumerate(range(0, len(nodes), per_slide)):
        chunk = nodes[offset:offset + per_slide]
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        _viz_set_title(slide, "人物关系图" if page == 0 else f"人物关系图（续 {page}）")

        center_x, center_y = 6.66, 4.15
        radius_x, radius_y = 4.35, 2.2
        positions: dict[str, tuple[float, float]] = {}
        for index, node in enumerate(chunk):
            if len(chunk) == 1:
                px, py = center_x, center_y
            else:
                angle = -math.pi / 2 + 2 * math.pi * index / len(chunk)
                px = center_x + radius_x * math.cos(angle)
                py = center_y + radius_y * math.sin(angle)
            positions[str(node["name"])] = (px, py)

        visible_edges = [
            edge for edge in edges
            if str(edge.get("from")) in positions and str(edge.get("to")) in positions
        ]
        for edge in visible_edges:
            _viz_connector(slide, *positions[str(edge["from"])], *positions[str(edge["to"])],
                           trim=0.5)

        for node in chunk:
            name = str(node["name"])
            kind = str(node.get("kind") or "")
            width, height = (1.25, 0.95) if kind == "person" else (1.6, 0.85)
            px, py = positions[name]
            _viz_add_shape(
                slide, "person" if kind == "person" else "org",
                px - width / 2, py - height / 2, width, height,
                _viz_node_label(node, "name"),
                "EAF2FF" if kind == "person" else "F2F6FD", "2B6FD4", size=9,
            )

        for edge in visible_edges:
            label = str(edge.get("label") or "").strip()
            if not label:
                continue
            (x1, y1), (x2, y2) = positions[str(edge["from"])], positions[str(edge["to"])]
            _viz_add_text(slide, (x1 + x2) / 2 - 0.8, (y1 + y2) / 2 - 0.16, 1.6, 0.32, label, size=8)

        extra = []
        if len(visible_edges) < len(edges):
            extra.append(f"另有 {len(edges) - len(visible_edges)} 条关系因分页未在本页绘制")
        _viz_add_notes(slide, chunk + visible_edges, extra)


def _viz_flow_levels(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, int]:
    """拓扑分层：无入边为 0 层；环/不可达节点放到末层，避免与上游重叠。"""
    ids = [str(node.get("id")) for node in nodes]
    indegree = {node_id: 0 for node_id in ids}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in edges:
        source, target = str(edge.get("from")), str(edge.get("to"))
        if source in indegree and target in indegree:
            indegree[target] += 1
            outgoing[source].append(target)

    levels = {node_id: 0 for node_id in ids}
    queue = [node_id for node_id in ids if indegree[node_id] == 0]
    visited = set(queue)
    while queue:
        current = queue.pop(0)
        for target in outgoing[current]:
            levels[target] = max(levels[target], levels[current] + 1)
            indegree[target] -= 1
            if indegree[target] == 0 and target not in visited:
                visited.add(target)
                queue.append(target)

    if len(visited) < len(ids):
        tail = max(levels.values(), default=0) + 1
        for node_id in ids:
            if node_id not in visited:
                levels[node_id] = tail
    return levels


def _viz_flow_slides(prs: Any, flow: dict[str, Any]) -> None:
    nodes = [
        node for node in (flow.get("nodes") or [])
        if isinstance(node, dict) and str(node.get("id") or "").strip()
    ]
    edges = [edge for edge in (flow.get("edges") or []) if isinstance(edge, dict)]
    if not nodes:
        return
    title = str(flow.get("title") or "流程图").strip() or "流程图"
    direction = str(flow.get("direction") or "TB").upper()
    levels = _viz_flow_levels(nodes, edges)

    groups: dict[int, list[dict[str, Any]]] = {}
    for node in nodes:
        groups.setdefault(levels[str(node["id"])], []).append(node)

    pages: list[list[int]] = []
    current: list[int] = []
    count = 0
    for level in sorted(groups):
        size = len(groups[level])
        if current and (count + size > _VIZ_PAGE_NODES or len(current) >= 5):
            pages.append(current)
            current, count = [], 0
        current.append(level)
        count += size
    if current:
        pages.append(current)

    for page_index, page_levels in enumerate(pages):
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        _viz_set_title(slide, title if page_index == 0 else f"{title}（续 {page_index}）")

        area_x, area_y, area_w, area_h = 0.7, 1.55, 11.93, 5.45
        max_in_level = max(len(groups[level]) for level in page_levels)
        if direction == "LR":
            node_w = min(2.0, area_w / max(1, len(page_levels)) - 0.35)
            node_h = min(0.85, area_h / max(1, max_in_level) - 0.3)
        else:
            node_w = min(2.0, area_w / max(1, max_in_level) - 0.35)
            node_h = min(0.8, area_h / max(1, len(page_levels)) - 0.4)

        positions: dict[str, tuple[float, float]] = {}
        for row, level in enumerate(page_levels):
            members = groups[level]
            for column, node in enumerate(members):
                if direction == "LR":
                    cx = area_x + area_w * (row + 0.5) / len(page_levels)
                    cy = area_y + area_h * (column + 0.5) / len(members)
                else:
                    cy = area_y + area_h * (row + 0.5) / len(page_levels)
                    cx = area_x + area_w * (column + 0.5) / len(members)
                positions[str(node["id"])] = (cx, cy)

        page_ids = set(positions)
        visible_edges = [
            edge for edge in edges
            if str(edge.get("from")) in page_ids and str(edge.get("to")) in page_ids
        ]
        for edge in visible_edges:
            _viz_connector(
                slide, *positions[str(edge["from"])], *positions[str(edge["to"])],
                dashed=bool(edge.get("dashed")), trim=min(node_w, node_h) / 2 + 0.04,
            )

        page_nodes = [node for node in nodes if str(node["id"]) in page_ids]
        for node in page_nodes:
            cx, cy = positions[str(node["id"])]
            _viz_add_shape(
                slide, str(node.get("shape") or "rect"),
                cx - node_w / 2, cy - node_h / 2, node_w, node_h,
                _viz_node_label(node), "F2F6FD", "2B6FD4", size=9,
            )

        for edge in visible_edges:
            label = str(edge.get("label") or "").strip()
            if not label:
                continue
            (x1, y1), (x2, y2) = positions[str(edge["from"])], positions[str(edge["to"])]
            _viz_add_text(slide, (x1 + x2) / 2 - 0.8, (y1 + y2) / 2 - 0.16, 1.6, 0.32, label, size=8)

        extra = []
        if len(visible_edges) < len(edges):
            extra.append(f"另有 {len(edges) - len(visible_edges)} 条流转因分页未在本页绘制")
        _viz_add_notes(slide, page_nodes + visible_edges, extra)


def _viz_rgb(hex_color: str) -> Any:
    from pptx.dml.color import RGBColor

    return RGBColor.from_string(hex_color)


def viz_to_pptx(viz: dict[str, Any], dest_path: str, title: str = "") -> None:
    """可视化产物 → PPTX（原生形状/连接线；cite 写入演讲者备注）。"""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width = Inches(_VIZ_SLIDE_W)
    prs.slide_height = Inches(_VIZ_SLIDE_H)

    cover = prs.slides.add_slide(prs.slide_layouts[0])
    cover.shapes.title.text = title or "可视化产物"
    for paragraph in cover.shapes.title.text_frame.paragraphs:
        for run in paragraph.runs:
            _pptx_style_run(run, 30, bold=True, color="1F3864")
    subtitle = cover.placeholders[1].text_frame.paragraphs[0]
    subtitle.text = "LawBuddy · 时间线 / 人物关系 / 流程"
    for run in subtitle.runs:
        _pptx_style_run(run, 13, color="5B6B84")

    warnings = [str(item) for item in (viz.get("warnings") or []) if str(item).strip()]
    if warnings:
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        _viz_set_title(slide, "待核实提示")
        box = slide.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(11.7), Inches(4.8))
        frame = box.text_frame
        frame.word_wrap = True
        for index, warning in enumerate(warnings):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.text = f"· {warning}"
            for run in paragraph.runs:
                _pptx_style_run(run, 13, color="8A5B00")

    _viz_timeline_slides(prs, viz.get("timeline") or {})
    _viz_relation_slides(prs, viz.get("relationGraph") or {})
    for flow in viz.get("flows") or []:
        if isinstance(flow, dict):
            _viz_flow_slides(prs, flow)

    if len(prs.slides) == 1:
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        _viz_set_title(slide, "暂无可视化内容")
        _viz_add_text(slide, 0.8, 2.0, 11.7, 0.6, "该产物没有时间线、关系图或流程数据。", size=14)

    prs.save(dest_path)