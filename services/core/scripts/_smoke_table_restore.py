# -*- coding: utf-8 -*-
"""冒烟脚本：扫描件表格结构还原（O-015 阶段 4，RapidTable / SLANet-plus）。

覆盖：
1. HTML → Markdown（thead/tbody、竖线转义、列数补齐、单列拒绝）；
2. 表格候选页启发式（多列对齐判定 / 单列文本流拒绝 / 行数不足拒绝）；
3. 融合顺序与边界行去重（stub 引擎：页眉 + 表格 + 页脚）；
4. 防丢护栏（参与表格的行不可在 Markdown 中定位 → 整体回退纯 OCR 文本）；
5. 引擎不可用 / 推理异常 → None 回退（OCR 主流程不受影响）；
6. 真实引擎端到端：合成表格图 → rapidocr → RapidTable → Markdown（依赖或模型
   缺失时 SKIP，保持离线环境可跑）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile

import lawbuddy.ocr.table as table_mod
from lawbuddy.ocr.table import (
    _parse_html_table,
    _rows_to_markdown,
    looks_like_table_page,
    restore_table,
)


def mk_line(text: str, x1: float, y1: float, x2: float, y2: float) -> dict:
    return {"text": text, "bbox": [x1, y1, x2, y2], "confidence": 0.99}


class _StubOut:
    def __init__(self, html: str, cell_bbox: tuple[float, float, float, float]):
        self.pred_htmls = [html]
        import numpy as np

        self.cell_bboxes = [np.array([
            [cell_bbox[0], cell_bbox[1]], [cell_bbox[2], cell_bbox[1]],
            [cell_bbox[0], cell_bbox[3]], [cell_bbox[2], cell_bbox[3]],
        ], dtype=float)]


class _StubEngine:
    def __init__(self, html: str, cell_bbox: tuple[float, float, float, float]):
        self._out = _StubOut(html, cell_bbox)

    def __call__(self, image_path, ocr_results=None):
        return self._out


def test_html_to_markdown() -> None:
    html = ("<html><body><table><thead><tr><td>项目</td><td>金额</td></tr></thead>"
            "<tbody><tr><td>合同款</td><td>120000</td></tr>"
            "<tr><td>利息</td><td>8000</td></tr></tbody></table></body></html>")
    rows = _parse_html_table(html)
    assert rows == [["项目", "金额"], ["合同款", "120000"], ["利息", "8000"]], rows
    md = _rows_to_markdown(rows)
    assert md == "| 项目 | 金额 |\n| --- | --- |\n| 合同款 | 120000 |\n| 利息 | 8000 |", md
    assert _rows_to_markdown([["a"], ["b"]]) is None  # 单列不是表格
    md2 = _rows_to_markdown([["a|b", "c"], ["d"]])
    assert "\\|b" in md2 and "| d |" in md2, md2  # 竖线转义 + 列数补齐
    print("1. HTML→Markdown OK（thead/tbody/转义/补齐/单列拒绝）")


def test_table_page_heuristic() -> None:
    grid = [mk_line("x", 0.1 + c * 0.3, 0.1 + r * 0.1, 0.35 + c * 0.3, 0.15 + r * 0.1)
            for r in range(4) for c in range(3)]
    assert looks_like_table_page(grid) is True  # 多列对齐
    single_col = [mk_line("x", 0.1, 0.05 * i, 0.9, 0.05 * i + 0.03) for i in range(10)]
    assert looks_like_table_page(single_col) is False  # 单列文本流
    assert looks_like_table_page(single_col[:5]) is False  # 行数不足
    print("2. 候选页启发式 OK（多列对齐/单列拒绝/行数门槛）")


def _with_stub(html: str, cell_bbox: tuple[float, float, float, float]):
    """注入 stub 引擎单例，返回旧值供恢复。"""
    orig_engine, orig_failed = table_mod._engine, table_mod._init_failed
    table_mod._engine, table_mod._init_failed = _StubEngine(html, cell_bbox), False
    return orig_engine, orig_failed


def _restore_stub(orig_engine, orig_failed) -> None:
    table_mod._engine, table_mod._init_failed = orig_engine, orig_failed


def _temp_png() -> Path:
    """最小有效 1×1 PNG（stub 引擎不读图，但 restore_table 需用 PIL 读取页面尺寸）。"""
    fd, name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    from PIL import Image

    Image.new("RGB", (800, 500), "white").save(name, format="PNG")
    return Path(name)


def test_fusion_order_and_dedup() -> None:
    img = _temp_png()
    orig = _with_stub(
        "<table><tr><td>项目</td><td>金额</td></tr><tr><td>合同款</td><td>120000</td></tr>"
        "<tr><td>利息</td><td>8000</td></tr></table>",
        (80, 60, 480, 380),  # 像素坐标（800×500 图），覆盖全部表格行
    )
    try:
        lines = [
            mk_line("某某法院 送达回证", 0.3, 0.01, 0.7, 0.05),  # 页眉（表格外）
            mk_line("项目", 0.11, 0.13, 0.30, 0.19),
            mk_line("金额", 0.31, 0.13, 0.60, 0.19),
            mk_line("合同款", 0.11, 0.25, 0.30, 0.31),
            mk_line("120000", 0.31, 0.25, 0.60, 0.31),
            mk_line("利息", 0.11, 0.37, 0.30, 0.43),
            mk_line("8000", 0.31, 0.37, 0.60, 0.43),
            mk_line("第 1 页 共 2 页", 0.3, 0.90, 0.7, 0.95),  # 页脚（表格外）
        ]
        out = restore_table(img, lines)
        assert out is not None, "表格页应结构化成功"
        assert out.startswith("某某法院 送达回证"), out  # 页眉保留在前
        assert "| 项目 | 金额 |" in out and "| 合同款 | 120000 |" in out and "| 利息 | 8000 |" in out
        assert out.endswith("第 1 页 共 2 页"), out  # 页脚保留在后
        assert out.index("送达回证") < out.index("| 项目") < out.index("第 1 页"), out
        assert out.count("利息") == 1 and out.count("8000") == 1, out  # 边界行去重
        print("3. 融合顺序与去重 OK（页眉→表格→页脚，无重复内容）")
    finally:
        _restore_stub(*orig)
        img.unlink(missing_ok=True)


def test_loss_guard_and_fallbacks() -> None:
    img = _temp_png()
    full: list[dict] = []
    try:
        # 防丢护栏：参与表格的行文本无法在 Markdown 中定位 → 回退
        orig = _with_stub(
            "<table><tr><td>项目</td><td>金额</td></tr><tr><td>合同款</td><td>120000</td></tr>"
            "<tr><td>利息</td><td>8000</td></tr></table>",
            (80, 60, 480, 380),
        )
        try:
            ghost = [
                mk_line("项目", 0.11, 0.13, 0.30, 0.19),
                mk_line("金额", 0.31, 0.13, 0.60, 0.19),
                mk_line("幽灵文本不存在于表格", 0.11, 0.25, 0.30, 0.31),
                mk_line("120000", 0.31, 0.25, 0.60, 0.31),
                mk_line("利息", 0.11, 0.37, 0.30, 0.43),
                mk_line("8000", 0.31, 0.37, 0.60, 0.43),
            ]
            assert restore_table(img, ghost) is None, "护栏必须拦截内容丢失"
            # 整页表格（无表外行）→ 纯 Markdown
            full = ghost[:2] + [mk_line("合同款", 0.11, 0.25, 0.30, 0.31),
                                mk_line("120000", 0.31, 0.25, 0.60, 0.31)] + ghost[4:]
            out = restore_table(img, full)
            assert out is not None and out.startswith("| 项目 | 金额 |"), out
        finally:
            _restore_stub(*orig)
        # 引擎不可用（构造失败记忆）→ 回退
        table_mod._engine, table_mod._init_failed = None, True
        try:
            assert restore_table(img, full) is None
        finally:
            table_mod._engine, table_mod._init_failed = None, False
        # 推理异常 → 回退
        class _Boom:
            def __call__(self, *args, **kwargs):
                raise RuntimeError("boom")

        table_mod._engine, table_mod._init_failed = _Boom(), False
        try:
            assert restore_table(img, full) is None
        finally:
            table_mod._engine, table_mod._init_failed = None, False
        print("4. 防丢护栏与降级 OK（内容丢失/引擎不可用/推理异常均回退）")
    finally:
        img.unlink(missing_ok=True)


def test_real_engine_end_to_end(tmp: Path) -> None:
    """真实引擎端到端：合成表格图 → rapidocr → RapidTable → Markdown。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:  # noqa: BLE001
        print("5. 真实引擎端到端 SKIP（PIL 不可用）")
        return
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
    except OSError:
        font = ImageFont.load_default()

    img_path = tmp / "table.png"
    rows = ["项目/金额/日期", "合同款/120000/2025-01-15", "利息/8000/2025-06-30",
            "违约金/20000/2025-08-10"]
    x0, y0, cw, ch = 80, 60, 220, 70
    img = Image.new("RGB", (800, 500), "white")
    d = ImageDraw.Draw(img)
    cols = [x0 + i * cw for i in range(4)]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row.split("/")):
            d.text((cols[c] + 12, y0 + r * ch + 20), cell, fill="black", font=font)
    for c in cols:
        d.line([c, y0, c, y0 + len(rows) * ch], fill="black", width=3)
    for r in range(len(rows) + 1):
        d.line([x0, y0 + r * ch, x0 + 3 * cw, y0 + r * ch], fill="black", width=3)
    img.save(img_path)

    try:
        from rapidocr_onnxruntime import RapidOCR
        ocr = RapidOCR(det_limit_side_len=640, det_limit_type="min")
        res = ocr(str(img_path))
        if isinstance(res, tuple):
            res = res[0]
        lines = []
        if res:
            w, h = img.size
            for row in res:
                if not (isinstance(row, (list, tuple)) and len(row) >= 3 and str(row[1]).strip()):
                    continue
                xs = [float(p[0]) for p in row[0]]
                ys = [float(p[1]) for p in row[0]]
                lines.append({"text": str(row[1]).strip(),
                              "bbox": [round(min(xs) / w, 4), round(min(ys) / h, 4),
                                       round(max(xs) / w, 4), round(max(ys) / h, 4)],
                              "confidence": float(row[2])})
        assert len(lines) >= 6, f"合成表格识别行数不足: {len(lines)}"
        out = restore_table(img_path, lines)
        assert out is not None, "真实表格页应结构化成功"
        assert "| 合同款 | 120000 | 2025-01-15 |" in out, out
        assert "| 利息 | 8000 | 2025-06-30 |" in out, out
        assert out.count("120000") == 1, out  # 无重复
        print("5. 真实引擎端到端 OK（rapidocr→RapidTable→Markdown，无重复）")
    except (ImportError, OSError) as exc:  # noqa: BLE001  依赖/模型缺失 → SKIP
        print(f"5. 真实引擎端到端 SKIP（{type(exc).__name__}: {exc}）")
    finally:
        try:
            img_path.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="table-restore-smoke-") as td:
        test_html_to_markdown()
        test_table_page_heuristic()
        test_fusion_order_and_dedup()
        test_loss_guard_and_fallbacks()
        test_real_engine_end_to_end(Path(td))
        print("\nTABLE RESTORE SMOKE: ALL OK")


if __name__ == "__main__":
    main()
