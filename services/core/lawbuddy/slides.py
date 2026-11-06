"""结构化幻灯片 → 原生可编辑 PPTX 打包（每页文本框/矩形/线条/表格）。

设计：
- render_pptx 工具接收结构化页面 JSON（不是 SVG/图片）：画布 1280×720（16:9），
  每页一个 {background?, elements:[...]} 对象；
- python-pptx 生成原生 PowerPoint shapes：文本框、矩形、直线、表格——用户在
  PowerPoint/WPS 中可直接改字、移动、调色，而不是一张整页图片；
- 坐标单位 px（1px = 9525 EMU，1280px 精确等于 16:9 版面宽），中文统一微软
  雅黑（latin + eastAsian 双写保证中文字体生效）；
- 纯格式打包，不解析内容语义；页面文本不进日志、不进审计 metadata（只记
  页数与字节数）。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

# 16:9 版面（EMU）与 px 换算：1280×720px @96dpi = 12192000×6858000 EMU
SLIDE_W_EMU = 12_192_000
SLIDE_H_EMU = 6_858_000
EMU_PER_PX = 9525

MAX_SLIDES = 30                  # 单份 PPT 页数上限（防失控长任务）
MAX_ELEMENTS_PER_SLIDE = 150     # 单页元素上限
TEXT_MAX_CHARS = 2_000           # 单个文本元素上限
CELL_MAX_CHARS = 200             # 表格单元格上限

_FONT = "Microsoft YaHei"

_ELEMENT_TYPES = {"text", "rect", "line", "table"}
_ALIGN = {"left": "PP_ALIGN.LEFT", "center": "PP_ALIGN.CENTER", "right": "PP_ALIGN.RIGHT"}


def _fail(page_i: int, msg: str) -> None:
    raise ValueError(f"第 {page_i} 页{msg}")


def _num(page_i: int, el_i: int, el: dict[str, Any], key: str, *, positive: bool = False) -> float:
    v = el.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        _fail(page_i, f"元素 {el_i + 1}（{el.get('type')}）的 {key} 必须是数字")
    if v < 0 or v > 20_000 or (positive and v <= 0):
        _fail(page_i, f"元素 {el_i + 1}（{el.get('type')}）的 {key}={v} 超出范围")
    return float(v)


def _color(page_i: int, el_i: int, el: dict[str, Any], key: str, default: str | None = None) -> str:
    raw = el.get(key, default)
    if raw is None:
        _fail(page_i, f"元素 {el_i + 1}（{el.get('type')}）缺少 {key}")
    text = str(raw).strip().lstrip("#")
    if len(text) != 6 or any(c not in "0123456789abcdefABCDEF" for c in text):
        _fail(page_i, f"元素 {el_i + 1}（{el.get('type')}）的 {key}={raw!r} 不是 #RRGGBB 颜色")
    return text.upper()


def _text_str(page_i: int, el_i: int, el: dict[str, Any], key: str, max_chars: int) -> str:
    raw = str(el.get(key) or "").strip()
    if not raw:
        _fail(page_i, f"元素 {el_i + 1}（{el.get('type')}）的 {key} 不能为空")
    if len(raw) > max_chars:
        _fail(page_i, f"元素 {el_i + 1}（{el.get('type')}）的 {key} 超过 {max_chars} 字上限")
    return raw


def validate_slides(slides: Any) -> list[dict[str, Any]]:
    """校验 render_pptx 的 slides 参数：页对象数组，逐页元素类型与取值校验。"""
    if not isinstance(slides, list) or not slides:
        raise ValueError("slides 不能为空：请提供每页一个对象的数组")
    if len(slides) > MAX_SLIDES:
        raise ValueError(f"页数超限（{len(slides)} 页，上限 {MAX_SLIDES}）；请拆分为多份或精简")

    pages: list[dict[str, Any]] = []
    for i, page in enumerate(slides, start=1):
        if not isinstance(page, dict):
            _fail(i, "必须是对象：{background?, elements: [...]}")
        elements = page.get("elements")
        if not isinstance(elements, list) or not elements:
            _fail(i, "的 elements 不能为空：至少一个元素")
        if len(elements) > MAX_ELEMENTS_PER_SLIDE:
            _fail(i, f"元素过多（{len(elements)} 个，上限 {MAX_ELEMENTS_PER_SLIDE}）；请精简")

        checked: list[dict[str, Any]] = []
        for j, el in enumerate(elements, start=1):
            if not isinstance(el, dict):
                _fail(i, f"元素 {j} 必须是对象")
            etype = str(el.get("type") or "")
            if etype not in _ELEMENT_TYPES:
                _fail(i, f"元素 {j} 的 type={etype!r} 不受支持（可选：text/rect/line/table）")
            if etype == "text":
                _num(i, j, el, "x"), _num(i, j, el, "y")
                _num(i, j, el, "w", positive=True), _num(i, j, el, "h", positive=True)
                _text_str(i, j, el, "text", TEXT_MAX_CHARS)
                size = el.get("size", 18)
                if not isinstance(size, (int, float)) or isinstance(size, bool) or not 6 <= size <= 300:
                    _fail(i, f"元素 {j}（text）的 size={size!r} 超出范围（6-300）")
                if str(el.get("align") or "left") not in _ALIGN:
                    _fail(i, f"元素 {j}（text）的 align={el.get('align')!r} 不受支持（left/center/right）")
                _color(i, j, el, "color", "333333")
            elif etype == "rect":
                _num(i, j, el, "x"), _num(i, j, el, "y")
                _num(i, j, el, "w", positive=True), _num(i, j, el, "h", positive=True)
                _color(i, j, el, "fill")
            elif etype == "line":
                _num(i, j, el, "x1"), _num(i, j, el, "y1"), _num(i, j, el, "x2"), _num(i, j, el, "y2")
                _color(i, j, el, "color")
                th = el.get("thickness", 2)
                if not isinstance(th, (int, float)) or isinstance(th, bool) or not 0.5 <= th <= 50:
                    _fail(i, f"元素 {j}（line）的 thickness={th!r} 超出范围（0.5-50）")
            elif etype == "table":
                _num(i, j, el, "x"), _num(i, j, el, "y")
                _num(i, j, el, "w", positive=True)
                cols = el.get("colWidths")
                if not isinstance(cols, list) or len(cols) < 2 or not all(
                    isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0 for c in cols
                ):
                    _fail(i, f"元素 {j}（table）的 colWidths 必须是正数数组（相对列宽，至少 2 列）")
                rows = el.get("rows")
                if not isinstance(rows, list) or not rows:
                    _fail(i, f"元素 {j}（table）的 rows 不能为空")
                if len(rows) > 30:
                    _fail(i, f"元素 {j}（table）行数超限（{len(rows)} 行，上限 30）")
                for r in rows:
                    if not isinstance(r, dict) or not isinstance(r.get("cells"), list) or not r.get("cells"):
                        _fail(i, f"元素 {j}（table）每行必须是 {{cells:[...]}}")
                    if len(r["cells"]) != len(cols):
                        _fail(i, f"元素 {j}（table）行单元格数（{len(r['cells'])}）与列数（{len(cols)}）不一致")
                    for cell in r["cells"]:
                        if not str(cell or "").strip():
                            _fail(i, f"元素 {j}（table）存在空单元格")
                        if len(str(cell)) > CELL_MAX_CHARS:
                            _fail(i, f"元素 {j}（table）单元格超过 {CELL_MAX_CHARS} 字上限")
                    if r.get("fill") is not None:
                        _color(i, j, {"type": "table", "fill": r["fill"]}, "fill")
                size = el.get("size", 18)
                if not isinstance(size, (int, float)) or isinstance(size, bool) or not 6 <= size <= 300:
                    _fail(i, f"元素 {j}（table）的 size={size!r} 超出范围（6-300）")
            checked.append(el)
        pages.append({"background": page.get("background"), "elements": checked})
    return pages


def _px(v: float):
    from pptx.util import Emu

    return Emu(int(round(v * EMU_PER_PX)))


def _style_run(run: Any, *, size: float, bold: bool, color: str) -> None:
    """统一字体样式：微软雅黑（latin + eastAsian 双写，保证中文生效）。"""
    from pptx.dml.color import RGBColor
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    f = run.font
    f.name = _FONT
    f.size = Pt(size)
    f.bold = bool(bold)
    f.color.rgb = RGBColor.from_string(color)
    r_pr = run._r.get_or_add_rPr()
    ea = r_pr.find(qn("a:ea"))
    if ea is None:
        ea = r_pr.makeelement(qn("a:ea"), {})
        r_pr.append(ea)
    ea.set("typeface", _FONT)


def _render_text(slide: Any, el: dict[str, Any]) -> None:
    from pptx.enum.text import PP_ALIGN

    box = slide.shapes.add_textbox(_px(el["x"]), _px(el["y"]), _px(el["w"]), _px(el["h"]))
    tf = box.text_frame
    tf.word_wrap = True
    lines = str(el["text"]).split("\n")
    align = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}[
        str(el.get("align") or "left")
    ]
    common = {
        "size": el.get("size", 18),
        "bold": el.get("bold", False),
        "color": str(el.get("color") or "333333").lstrip("#"),
    }
    for k, line in enumerate(lines):
        p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
        p.alignment = align
        if el.get("lineSpacing"):
            p.line_spacing = float(el["lineSpacing"])
        run = p.add_run()
        run.text = line
        _style_run(run, **common)


def _render_rect(slide: Any, el: dict[str, Any]) -> None:
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE

    shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if el.get("radius") else MSO_SHAPE.RECTANGLE
    shape = slide.shapes.add_shape(shape_type, _px(el["x"]), _px(el["y"]), _px(el["w"]), _px(el["h"]))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(str(el["fill"]).lstrip("#"))
    shape.line.fill.background()
    shape.shadow.inherit = False


def _render_line(slide: Any, el: dict[str, Any]) -> None:
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_CONNECTOR
    from pptx.util import Pt

    conn = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, _px(el["x1"]), _px(el["y1"]), _px(el["x2"]), _px(el["y2"])
    )
    conn.line.color.rgb = RGBColor.from_string(str(el["color"]).lstrip("#"))
    conn.line.width = Pt(float(el.get("thickness", 2)))
    conn.shadow.inherit = False


def _render_table(slide: Any, el: dict[str, Any]) -> None:
    from pptx.dml.color import RGBColor
    from pptx.enum.text import MSO_ANCHOR
    from pptx.util import Emu

    cols = [float(c) for c in el["colWidths"]]
    rows = el["rows"]
    total_w = float(el["w"])
    row_h = float(el.get("rowHeight", 44))
    frame = slide.shapes.add_table(len(rows), len(cols), _px(el["x"]), _px(el["y"]), _px(total_w), _px(row_h * len(rows)))
    table = frame.table
    table.first_row = False
    table.horz_banding = False
    scale = total_w / sum(cols)
    for i, c in enumerate(cols):
        table.columns[i].width = Emu(int(round(c * scale * EMU_PER_PX)))
    for i in range(len(rows)):
        table.rows[i].height = _px(row_h)

    size = el.get("size", 18)
    for r_i, row in enumerate(rows):
        is_header = bool(row.get("header"))
        fill = str(row.get("fill") or ("EAF1FA" if is_header else "FFFFFF")).lstrip("#")
        for c_i, cell_text in enumerate(row["cells"]):
            cell = table.cell(r_i, c_i)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.fill.solid()
            cell.fill.fore_color.rgb = RGBColor.from_string(fill)
            run = cell.text_frame.paragraphs[0].add_run()
            run.text = str(cell_text)
            _style_run(run, size=size, bold=is_header or bool(row.get("bold")), color=str(el.get("color") or "333333"))


_RENDERERS = {"text": _render_text, "rect": _render_rect, "line": _render_line, "table": _render_table}


def pages_to_pptx(pages: list[dict[str, Any]], dest_path: str | Path) -> dict[str, Any]:
    """把结构化页面渲染为 16:9 原生可编辑 PPTX；返回 {path, slides, bytes}。"""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu

    prs = Presentation()
    prs.slide_width = Emu(SLIDE_W_EMU)
    prs.slide_height = Emu(SLIDE_H_EMU)
    blank = prs.slide_layouts[6]

    for page in pages:
        slide = prs.slides.add_slide(blank)
        bg = page.get("background")
        if bg:
            fill = slide.background.fill
            fill.solid()
            fill.fore_color.rgb = RGBColor.from_string(str(bg).lstrip("#"))
        for el in page["elements"]:
            _RENDERERS[str(el["type"])](slide, el)

    path = Path(dest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    return {"path": str(path), "slides": len(pages), "bytes": path.stat().st_size}
