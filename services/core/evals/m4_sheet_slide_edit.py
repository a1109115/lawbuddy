# -*- coding: utf-8 -*-
"""M4 评测器：AI 产物 Excel 单元格编辑 / PPTX 文本框编辑（P2）。

用法（services/core 目录下）：
    python -m evals.m4_sheet_slide_edit

覆盖：
1. XLSX：open_editable 返回可编辑网格（值/公式/合并/冻结）；save_sheet_edits
   只改指定单元格，未改单元格样式与公式保持原样；createVersion 追加版本；
2. 含图表的工作簿：未确认时拒绝保存（SHEET_LOSS_CONFIRM），确认后才允许；
3. 保存边界：工作表不存在、行列为非整数、内容超长均被拒绝；
4. PPTX：open_editable 返回每页文本框；save_slide_texts 更新文字并保留字号/加粗，
   形状与版式不变；非法幻灯片/文本框被拒绝；
5. 非 xlsx/pptx 产物不会进入对应编辑器（明确报错）。

门槛：全部断言通过。
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def main() -> int:
    from openpyxl import Workbook, load_workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import PatternFill
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches, Pt

    from lawbuddy import artifacts
    from lawbuddy.cases import create_case
    from lawbuddy.storage import get_db
    from lawbuddy.workspace import ensure_workspace

    def rejects(action, fragment: str) -> None:
        try:
            action()
        except (ValueError, RuntimeError) as exc:
            assert fragment in str(exc), str(exc)
        else:
            raise AssertionError(fragment)

    with tempfile.TemporaryDirectory(
        prefix="lawbuddy_sheet_slide_", ignore_cleanup_errors=True
    ) as temp:
        root = Path(temp)
        ws = root / "ws"
        ensure_workspace(str(ws))
        case = create_case("表格与演示", "civil", "委托人")

        # ---- 1. XLSX 可编辑网格与增量写回 ----
        book = Workbook()
        sheet = book.active
        sheet.title = "明细"
        sheet["A1"] = "姓名"
        sheet["B1"] = "金额"
        sheet["A2"] = "张三"
        sheet["B2"] = "100"
        sheet["C2"] = "=B2*2"
        sheet["A1"].fill = PatternFill("solid", start_color="2B6FD4", end_color="2B6FD4")
        sheet.freeze_panes = "A2"
        sheet.merge_cells("A4:B4")
        sheet["A4"] = "合并标题"
        second = book.create_sheet("汇总")
        second["A1"] = "合计"
        second["B1"] = 10
        second["C1"] = "待核"
        book.save(ws / "台账.xlsx")
        xlsx_art = artifacts.save_artifact(None, case["id"], "file", "台账.xlsx", {
            "fileName": "台账.xlsx", "sizeBytes": (ws / "台账.xlsx").stat().st_size,
        })

        opened = artifacts.open_editable(xlsx_art)
        assert opened["kind"] == "sheet" and len(opened["sheets"]) == 2, opened
        first = opened["sheets"][0]
        assert first["name"] == "明细" and first["freezeRows"] == 1
        assert first["rows"][1][2] == "=B2*2", first["rows"][:3]
        assert any(m["rowSpan"] == 1 and m["colSpan"] == 2 for m in first["merges"]), first["merges"]

        saved = artifacts.save_sheet_edits(
            xlsx_art,
            [{"sheet": "明细", "row": 1, "col": 1, "value": "120"},
             {"sheet": "汇总", "row": 0, "col": 1, "value": "12"},
             {"sheet": "汇总", "row": 0, "col": 2, "value": "已完成"}],
            create_version=True,
        )
        assert saved["ok"] and saved["versionNo"] == 2, saved
        reopened = load_workbook(ws / "台账.xlsx")
        assert reopened["明细"]["B2"].value == "120", reopened["明细"]["B2"].value
        assert reopened["汇总"]["B1"].value == 12, "原为数字的单元格应保留数字类型"
        assert reopened["汇总"]["C1"].value == "已完成"
        assert reopened["明细"]["C2"].value == "=B2*2", "公式应保持原样"
        assert reopened["明细"]["A2"].value == "张三", "未修改单元格应保持原样"
        assert reopened["明细"]["A1"].fill.fgColor.rgb == "002B6FD4", "未修改单元格样式应保留"
        assert "A4:B4" in [str(rng) for rng in reopened["明细"].merged_cells.ranges]
        print("[PASS] XLSX 网格读取 / 增量写回 / 公式与样式保留 / 版本追加")

        # ---- 2. 含图表需确认 ----
        chart_book = Workbook()
        chart_sheet = chart_book.active
        chart_sheet.title = "图表页"
        for index, value in enumerate([3, 5, 8], start=1):
            chart_sheet.cell(index, 1, f"项{index}")
            chart_sheet.cell(index, 2, value)
        chart = BarChart()
        chart.add_data(Reference(chart_sheet, min_col=2, min_row=1, max_row=3), titles_from_data=False)
        chart_sheet.add_chart(chart, "D2")
        chart_book.save(ws / "含图表.xlsx")
        chart_art = artifacts.save_artifact(None, case["id"], "file", "含图表.xlsx", {
            "fileName": "含图表.xlsx", "sizeBytes": (ws / "含图表.xlsx").stat().st_size,
        })
        assert artifacts.open_editable(chart_art)["risky"] is True
        rejects(
            lambda: artifacts.save_sheet_edits(
                chart_art, [{"sheet": "图表页", "row": 0, "col": 0, "value": "改"}]
            ),
            "SHEET_LOSS_CONFIRM",
        )
        confirmed = artifacts.save_sheet_edits(
            chart_art,
            [{"sheet": "图表页", "row": 0, "col": 0, "value": "已确认"}],
            confirm_loss=True,
        )
        assert confirmed["ok"] and confirmed.get("warnings"), confirmed
        print("[PASS] 含图表工作簿保存前必须显式确认")

        # ---- 3. 保存边界 ----
        rejects(
            lambda: artifacts.save_sheet_edits(
                xlsx_art, [{"sheet": "不存在", "row": 0, "col": 0, "value": "x"}]
            ),
            "工作表不存在",
        )
        rejects(
            lambda: artifacts.save_sheet_edits(
                xlsx_art, [{"sheet": "明细", "row": "0", "col": 0, "value": "x"}]
            ),
            "整数",
        )
        rejects(
            lambda: artifacts.save_sheet_edits(
                xlsx_art, [{"sheet": "明细", "row": 0, "col": 0, "value": "x" * 40000}]
            ),
            "长度",
        )
        print("[PASS] 工作表/行列/长度边界校验")

        # ---- 4. PPTX 文本框编辑 ----
        presentation = Presentation()
        presentation.slide_width = Inches(13.333)
        presentation.slide_height = Inches(7.5)
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "原始标题"
        box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1.2))
        frame = box.text_frame
        frame.text = "正文第一段"
        run = frame.paragraphs[0].runs[0]
        run.font.bold = True
        run.font.size = Pt(20)
        frame.add_paragraph().text = "正文第二段"
        slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(8), Inches(2), Inches(2), Inches(1))
        presentation.save(ws / "演示.pptx")
        slide_art = artifacts.save_artifact(None, case["id"], "file", "演示.pptx", {
            "fileName": "演示.pptx", "sizeBytes": (ws / "演示.pptx").stat().st_size,
        })

        opened_slide = artifacts.open_editable(slide_art)
        assert opened_slide["kind"] == "slide", opened_slide
        boxes = opened_slide["slides"][0]["boxes"]
        assert any(box_item["text"] == "原始标题" for box_item in boxes), boxes
        body = next(box_item for box_item in boxes if "正文第一段" in box_item["text"])
        assert "正文第二段" in body["text"]
        shape_count = len(Presentation(str(ws / "演示.pptx")).slides[0].shapes)

        slide_saved = artifacts.save_slide_texts(
            slide_art,
            [{"slideIndex": 0, "shapeId": body["shapeId"], "text": "改后第一段\n改后第二段\n新增第三段"}],
            create_version=True,
        )
        assert slide_saved["ok"] and slide_saved["versionNo"] == 2, slide_saved
        edited = Presentation(str(ws / "演示.pptx"))
        edited_slide = edited.slides[0]
        assert len(edited_slide.shapes) == shape_count, "形状数量不应变化"
        edited_box = next(item for item in edited_slide.shapes if item.shape_id == body["shapeId"])
        assert edited_box.text_frame.text == "改后第一段\n改后第二段\n新增第三段"
        edited_run = edited_box.text_frame.paragraphs[0].runs[0]
        assert edited_run.font.bold is True and edited_run.font.size == Pt(20), "首段格式应保留"
        print("[PASS] PPTX 文本框编辑 / 保留形状与首段格式 / 版本追加")

        rejects(
            lambda: artifacts.save_slide_texts(
                slide_art, [{"slideIndex": 9, "shapeId": body["shapeId"], "text": "x"}]
            ),
            "幻灯片序号",
        )
        rejects(
            lambda: artifacts.save_slide_texts(
                slide_art, [{"slideIndex": 0, "shapeId": 99999, "text": "x"}]
            ),
            "文本框不存在",
        )
        print("[PASS] PPTX 幻灯片/文本框边界校验")

        # ---- 5. 格式错配拒绝 ----
        rejects(
            lambda: artifacts.save_slide_texts(
                xlsx_art, [{"slideIndex": 0, "shapeId": 1, "text": "x"}]
            ),
            "仅支持编辑 .pptx",
        )
        rejects(
            lambda: artifacts.save_sheet_edits(
                slide_art, [{"sheet": "明细", "row": 0, "col": 0, "value": "x"}]
            ),
            "仅支持编辑 .xlsx",
        )
        print("[PASS] 格式错配拒绝")

        # ---- 6. 导入原件的可编辑副本（原件只读） ----
        from lawbuddy.cases import archive_case
        from lawbuddy.documents import import_documents

        imported = import_documents(case["id"], [str(ws / "台账.xlsx")])[0]
        doc_id = imported["documentId"]
        original_version = get_db().query_one(
            "SELECT active_version_id FROM documents WHERE id = ?", (doc_id,)
        )["active_version_id"]
        original_sha = get_db().query_one(
            "SELECT sha256 FROM document_versions WHERE id = ?", (original_version,)
        )["sha256"]
        copy_info = artifacts.create_editable_copy(doc_id)
        assert copy_info["title"].endswith("工作副本.xlsx"), copy_info
        assert get_db().query_one(
            "SELECT sha256 FROM document_versions WHERE id = ?", (original_version,)
        )["sha256"] == original_sha, "原件版本不应被修改"
        copy_opened = artifacts.open_editable(copy_info["artifactId"])
        assert copy_opened["kind"] == "sheet" and copy_opened["sheets"], copy_opened
        artifacts.save_sheet_edits(
            copy_info["artifactId"],
            [{"sheet": "明细", "row": 0, "col": 0, "value": "姓名（副本）"}],
        )
        reopened_copy = load_workbook(ws / copy_info["title"])
        assert reopened_copy["明细"]["A1"].value == "姓名（副本）"
        assert copy_opened["sheets"][0]["rows"][0][1] == "金额"
        print("[PASS] 导入 Excel 创建可编辑副本（原件版本哈希不变）")

        (ws / "说明.txt").write_text("非表格原件", encoding="utf-8")
        txt_doc = import_documents(case["id"], [str(ws / "说明.txt")])[0]["documentId"]
        rejects(lambda: artifacts.create_editable_copy(txt_doc), "仅 Excel/PPT")

        archive_case(case["id"])
        rejects(lambda: artifacts.create_editable_copy(doc_id), "归档")
        print("[PASS] 非 Excel/PPT 与归档案件拒绝创建副本")

        get_db().close()

    print("M4 Excel 单元格 / PPTX 文本框编辑：PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
