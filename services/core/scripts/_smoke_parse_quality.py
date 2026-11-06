# -*- coding: utf-8 -*-
"""冒烟脚本：文档解析升级质量验证（阶段 1：DOCX/XLSX Markdown 化 + 第一档清洗）。

覆盖：
1. DOCX 表格不再丢失 → Markdown 管道表格；Heading 样式 → # 标题；竖线转义；
2. XLSX → Markdown 表格（表头分隔行、列对齐、超宽截断、空 sheet=empty）；
3. textclean 第一档：零宽/控制字符/tab/全角空格/连续空行；幂等；正常文本不误伤；
4. 全链路：导入后 get_pages 文本断言（解析 + 清洗接入点生效）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shutil
import tempfile

from lawbuddy.textclean import clean_page_text, clean_native_pages


def make_docx(path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading("民事起诉状", level=1)
    doc.add_paragraph("原告张三因合同纠纷，向本院提起诉讼。")
    doc.add_heading("当事人信息", level=2)
    table = doc.add_table(rows=2, cols=3)
    for i, v in enumerate(["姓名", "角色", "备注"]):
        table.rows[0].cells[i].text = v
    for i, v in enumerate(["张三", "原告", "持股|50%"]):
        table.rows[1].cells[i].text = v
    doc.add_paragraph("以上信息以法院送达回证为准。")
    doc.save(str(path))


def make_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "流水"
    ws.append(["日期", "金额", "对手方"])
    ws.append(["2026-01-05", 10000, "甲公司"])
    ws.append([None, None, None])  # 空行应跳过
    ws.append(["2026-02-11", 2500.5, "乙公司"])
    ws2 = wb.create_sheet("空表")
    ws2["A1"] = None
    wb.save(str(path))


def test_clean_page_text() -> None:
    raw = (
        "\ufeff\u200b原告\u200c主张\x0c如下：\n"
        "第一项\t请求\u3000支付货款。  \n"
        "\n\n\n"
        "第二项 请求利息。"
    )
    out, removed = clean_page_text(raw)
    assert "\ufeff" not in out and "\u200b" not in out and "\u200c" not in out, out
    assert "\x0c" not in out and "\t" not in out and "\u3000" not in out, out
    assert "第一项 请求 支付货款。" in out, out  # tab/全角归一、行尾空白剥离
    assert "\n\n\n" not in out and "\n\n" in out, out  # 空行压缩为 1 个
    out2, removed2 = clean_page_text(out)
    assert out2 == out and removed2 == 0, "清洗必须幂等"
    assert removed > 0
    # 正常文本不误伤
    normal = "第一条 合同标的\n甲方向乙方出售设备一台。\n\n第二条 价款"
    out3, removed3 = clean_page_text(normal)
    assert out3 == normal and removed3 == 0
    # 空文本透传
    assert clean_page_text("") == ("", 0)
    print("1. clean_page_text OK（零宽/控制字符/tab/空行/幂等/不误伤）")


def test_clean_native_pages_scope() -> None:
    pages = [("正文", "native"), ("", "empty"), ("", "ocr_pending")]
    out, total = clean_native_pages(pages)
    assert out == pages and total == 0, "native 正常文本与空页必须原样透传"
    out2, _ = clean_native_pages([("\u200bx\t", "native"), ("y", "ocr_pending")])
    assert out2[0][0] == "x" and out2[0][1] == "native"
    assert out2[1] == ("y", "ocr_pending"), "ocr_pending 页不得清洗"
    print("2. clean_native_pages OK（仅 native 页清洗，质量标记不动）")


def test_docx_extract(path: Path) -> None:
    from lawbuddy.documents import _extract_docx

    pages, quality = _extract_docx(path)
    assert quality == "ready" and len(pages) == 1
    text = pages[0][0]
    assert "# 民事起诉状" in text, text[:200]
    assert "## 当事人信息" in text, text[:200]
    assert "| 姓名 | 角色 | 备注 |" in text, text  # 表头行
    assert "| --- | --- | --- |" in text, text  # 分隔行
    assert "| 张三 | 原告 | 持股\\|50% |" in text, text  # 内容行 + 竖线转义
    # 体顺序：段落 → 表格 → 段落
    assert text.index("原告张三") < text.index("| 姓名") < text.index("以上信息"), text
    print("3. DOCX 提取 OK（标题层级/表格 Markdown/转义/体顺序）")


def test_xlsx_extract(path: Path) -> None:
    from lawbuddy.documents import _extract_xlsx

    pages, quality = _extract_xlsx(path)
    assert quality == "ready" and len(pages) == 2
    t1 = pages[0][0]
    assert "| 日期 | 金额 | 对手方 |" in t1, t1
    assert "| --- | --- | --- |" in t1, t1
    assert "| 2026-01-05 | 10000 | 甲公司 |" in t1, t1
    assert "| 2026-02-11 | 2500.5 | 乙公司 |" in t1, t1  # None 补空位保持列对齐
    assert pages[1] == ("", "empty"), pages[1]  # 空 sheet
    print("4. XLSX 提取 OK（表头/列对齐/空 sheet）")


def test_xlsx_wide_truncation(path: Path) -> None:
    from openpyxl import Workbook

    from lawbuddy.documents import _XLSX_MAX_COLS, _extract_xlsx

    wb = Workbook()
    ws = wb.active
    ws.append([f"c{i}" for i in range(_XLSX_MAX_COLS + 5)])
    wide = path.parent / "wide.xlsx"
    wb.save(str(wide))
    pages, _ = _extract_xlsx(wide)
    assert f"超宽截断 5 列" in pages[0][0], pages[0][0][-80:]
    wide.unlink()
    print("5. XLSX 超宽截断 OK")


def test_full_chain(tmp: Path) -> None:
    from lawbuddy.workspace import ensure_workspace
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents, get_pages

    ensure_workspace(str(tmp / "ws"))
    case_id = create_case(title="冒烟-解析质量", case_type="civil", client_name="张三")["id"]

    docx_path = tmp / "起诉状.docx"
    make_docx(docx_path)
    xlsx_path = tmp / "流水.xlsx"
    make_xlsx(xlsx_path)
    # 含噪音 txt：BOM + 控制字符 + 连续空行（清洗接入点全链路验证）
    txt_path = tmp / "笔录.txt"
    txt_path.write_text("\ufeff开场\u200b陈述。\n\n\n\n正文开始。", encoding="utf-8")

    docs = import_documents(case_id, [str(docx_path), str(xlsx_path), str(txt_path)])
    by_name = {d["documentId"]: d for d in docs}
    assert all(d["status"] == "imported" for d in docs), docs

    docx_text = "\n".join(p["text"] for p in get_pages(docs[0]["documentId"]))
    assert "# 民事起诉状" in docx_text and "| 张三 | 原告 | 持股\\|50% |" in docx_text
    xlsx_text = "\n".join(p["text"] for p in get_pages(docs[1]["documentId"]))
    assert "| 日期 | 金额 | 对手方 |" in xlsx_text
    txt_text = "\n".join(p["text"] for p in get_pages(docs[2]["documentId"]))
    assert "\ufeff" not in txt_text and "\u200b" not in txt_text, txt_text
    assert "\n\n\n" not in txt_text and "开场陈述" in txt_text and "正文开始" in txt_text, txt_text
    print("6. 全链路导入 OK（DOCX/XLSX Markdown 与清洗均落入 document_pages）")


def make_table_pdf(path: Path) -> None:
    """reportlab 构造带 GRID 线框表格 + 前后正文的 PDF（英文内容免字体依赖）。"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    doc = SimpleDocTemplate(str(path), pagesize=A4)
    styles = getSampleStyleSheet()
    table = Table(
        [
            ["Item", "Amount", "Due"],
            ["Consulting", "10000", "2026-01-05"],
            ["Travel", "2500.5", "2026-02-11"],
        ]
    )
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    doc.build(
        [
            Paragraph("Exhibit A: Payment Schedule.", styles["Normal"]),
            Spacer(1, 12),
            table,
            Spacer(1, 12),
            Paragraph("End of exhibit.", styles["Normal"]),
        ]
    )


def make_plain_pdf(path: Path) -> None:
    """无表格纯文本 PDF：验证不触发重建、文本原样透传。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    doc = SimpleDocTemplate(str(path), pagesize=A4)
    doc.build([Paragraph("Plain page without any table cell.", getSampleStyleSheet()["Normal"])])


def test_pdf_extract(table_pdf: Path, plain_pdf: Path) -> None:
    from lawbuddy.documents import _extract_pdf

    pages, quality = _extract_pdf(table_pdf)
    assert quality == "ready" and len(pages) == 1
    text = pages[0][0]
    assert "| Item | Amount | Due |" in text, text
    assert "| --- | --- | --- |" in text, text
    assert "| Consulting | 10000 | 2026-01-05 |" in text, text
    assert "Exhibit A: Payment Schedule." in text, text  # 表格外正文保留
    assert "End of exhibit." in text, text
    assert text.count("Consulting") == 1, "bbox 裁剪后表格文字不得重复进上下文"
    # 无表格页：纯文本透传，不产出管道行
    plain_pages, _ = _extract_pdf(plain_pdf)
    plain_text = plain_pages[0][0]
    assert "Plain page without any table cell." in plain_text, plain_text
    assert "|" not in plain_text, plain_text
    print("7. PDF 表格重建 OK（Markdown 表格/正文保留/无重复/无表格页透传）")


def test_structural_clean() -> None:
    from lawbuddy.textclean import clean_document_pages

    def page(body: str, header: str = "某某区人民法院 综合审判庭",
             footer: str = "内部资料 请勿外传", num: int = 0) -> tuple[str, str]:
        lines = [header, body]
        if num:
            lines.append(f"— {num} —")
        lines.append(footer)
        return ("\n".join(lines), "native")

    # 6 页重复页眉/页脚 + 独立页码行 → 全部清除，正文保留
    pages = [page(f"证据第{i}项：借款合同原件扫描件。", num=i) for i in range(1, 7)]
    out, stats = clean_document_pages(pages)
    assert stats["headerFooterLines"] == 12 and stats["pageNumbers"] == 6, stats
    for text, _ in out:
        assert "某某区人民法院" not in text and "内部资料" not in text and "— " not in text, text
        assert "借款合同原件扫描件" in text, text
    # 中文页码模式
    out2, stats2 = clean_document_pages([("正文甲。\n第 3 页，共 10 页\n正文乙。", "native")])
    assert stats2["pageNumbers"] == 1 and "第 3 页" not in out2[0][0], (stats2, out2)
    assert "正文甲" in out2[0][0] and "正文乙" in out2[0][0]
    # 防误删：6 页中仅 2 页首行为「证据清单」（< 2/3）→ 不删
    scarce = [("证据清单", "native")] * 2 + [("正文丙。", "native")] * 4
    out3, stats3 = clean_document_pages(scarce)
    assert stats3["headerFooterLines"] == 0, stats3
    assert out3[0][0] == "证据清单" and out3[1][0] == "证据清单"
    # 页数不足（2 页同首行）→ 不做页眉统计
    two = [("重复标题行", "native")] * 2
    out4, stats4 = clean_document_pages(two)
    assert stats4["headerFooterLines"] == 0 and out4[0][0] == "重复标题行"
    # 清空保护：整页仅由页眉构成时保留原文本
    guarded = [("法院抬头", "native")] * 6 + [("正文页。", "native")]
    out5, stats5 = clean_document_pages(guarded)
    assert stats5["headerFooterLines"] == 0, stats5  # 「法院抬头」只有 5/6 < 2/3 也不删
    pure = [("同一抬头", "native")] * 6  # 6/6 命中但删除会清空 → 保留
    out6, stats6 = clean_document_pages(pure)
    assert stats6["headerFooterLines"] == 0 and out6[0][0] == "同一抬头", (stats6, out6)
    # 正常无噪音多页文档零改动
    normal = [(f"第{i}页正文内容，无重复页眉。", "native") for i in range(4)]
    out7, stats7 = clean_document_pages(normal)
    assert all(s == 0 for s in stats7.values()) and out7 == normal
    print("8. 第二档清洗 OK（页眉/页脚/页码/防误删/页数不足/清空保护/零改动）")


def make_marginal_pdf(path: Path) -> None:
    """6 页英文页眉 + 独立页码行 PDF（全链路第二档验证）。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    for i in range(1, 7):
        c.drawString(72, 780, "SUPERIOR COURT INTERNAL COPY")
        c.drawString(72, 750, f"Evidence body paragraph {i}: contract signed on 2026-01-05.")
        c.drawCentredString(297, 40, f"- {i} -")
        c.showPage()
    c.save()


def test_marginal_pdf_full_chain(tmp: Path) -> None:
    from lawbuddy.workspace import ensure_workspace
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents, get_pages

    ensure_workspace(str(tmp / "ws2"))
    case_id = create_case(title="冒烟-页眉清洗", case_type="civil", client_name="李四")["id"]
    pdf_path = tmp / "marginal.pdf"
    make_marginal_pdf(pdf_path)
    docs = import_documents(case_id, [str(pdf_path)])
    assert docs[0]["status"] == "imported", docs
    pages = get_pages(docs[0]["documentId"])
    joined = "\n".join(p["text"] for p in pages)
    assert "SUPERIOR COURT INTERNAL COPY" not in joined, joined[:300]
    assert "- 1 -" not in joined and "\n- " not in joined, joined[:300]
    for i in range(1, 7):
        assert f"Evidence body paragraph {i}" in joined, joined[:500]
    print("9. 多页 PDF 页眉/页码全链路清洗 OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="parse-quality-smoke-"))
    try:
        test_clean_page_text()
        test_clean_native_pages_scope()
        docx_path = tmp / "起诉状.docx"
        make_docx(docx_path)
        test_docx_extract(docx_path)
        xlsx_path = tmp / "流水.xlsx"
        make_xlsx(xlsx_path)
        test_xlsx_extract(xlsx_path)
        test_xlsx_wide_truncation(tmp)
        test_full_chain(tmp)
        table_pdf = tmp / "exhibit.pdf"
        make_table_pdf(table_pdf)
        plain_pdf = tmp / "plain.pdf"
        make_plain_pdf(plain_pdf)
        test_pdf_extract(table_pdf, plain_pdf)
        test_structural_clean()
        test_marginal_pdf_full_chain(tmp)
        print("\nPARSE QUALITY SMOKE: ALL OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
