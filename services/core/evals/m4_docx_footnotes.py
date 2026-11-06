# -*- coding: utf-8 -*-
"""M4 评测器：Word 原生脚注引证（P3.1）。

用法（services/core 目录下）：
    python -m evals.m4_docx_footnotes

覆盖：
1. 报告/可视化产物中的引证生成结构化 cite 块（不再混入正文文本）；
2. DOCX 导出把引证写成 Word 原生脚注：footnotes.xml、w:footnoteReference、
   内容类型 Override、document.xml.rels 关系、settings.xml footnotePr；
3. 无引证时不生成空 footnotes 部件（不产生误导性空脚注）；
4. Markdown / TXT / HTML / XLSX / PPTX 导出仍保留来源信息（其他格式不丢出处）；
5. 脚注 XML 结构可解析，脚注数量与引证数量一致，DOCX 可被 python-docx 重新打开。

门槛：全部断言通过。
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


def main() -> int:
    from lawbuddy import artifacts, render

    body = {
        "report": {
            "overview": "概述段落",
            "parties": [{
                "name": "张三", "role": "原告", "note": "身份证号已脱敏",
                "cite": {"label": "《民事起诉状》 · 第1页", "documentId": "doc-1", "pageNo": 1},
            }],
            "facts": [{
                "text": "被告逾期付款", "stance": "favorable", "status": "confirmed",
                "cite": {"label": "《购销合同》 · 第2页", "documentId": "doc-2", "pageNo": 2},
            }],
            "legalBasis": [{
                "title": "民法典第五百七十七条", "point": "违约责任",
                "cite": {"label": "《民法典》 · 第577页", "documentId": "doc-3", "pageNo": 577},
            }],
        }
    }
    labels = ["《民事起诉状》 · 第1页", "《购销合同》 · 第2页", "《民法典》 · 第577页"]

    # ---- 1. 结构化引证块 ----
    blocks = artifacts._body_to_blocks("测试报告", body)
    cites = [val for kind, val in blocks if kind == "cite"]
    assert len(cites) == 3, cites
    assert all(isinstance(c, dict) and c.get("label") for c in cites), cites
    texts = [str(val) for kind, val in blocks if kind in ("p", "li")]
    assert all("来源：" not in text for text in texts), texts
    print("[PASS] 引证生成结构化 cite 块（不在正文里拼接来源）")

    with tempfile.TemporaryDirectory(prefix="lawbuddy_footnotes_") as temp:
        root = Path(temp)

        # ---- 2. DOCX 原生脚注 ----
        docx_path = root / "报告.docx"
        render.blocks_to_docx(blocks, str(docx_path))
        assert docx_path.exists() and docx_path.stat().st_size > 0
        with zipfile.ZipFile(docx_path) as archive:
            names = set(archive.namelist())
            document = archive.read("word/document.xml").decode("utf-8")
            footnotes = archive.read("word/footnotes.xml").decode("utf-8")
            content_types = archive.read("[Content_Types].xml").decode("utf-8")
            rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
            settings = archive.read("word/settings.xml").decode("utf-8") if "word/settings.xml" in names else ""

        assert document.count("<w:footnoteReference") == 3, document[-400:]
        for footnote_id in (1, 2, 3):
            assert f'w:id="{footnote_id}"' in document, footnote_id
        assert 'PartName="/word/footnotes.xml"' in content_types
        assert "relationships/footnotes" in rels and 'Target="footnotes.xml"' in rels
        assert "<w:footnotePr" in settings, settings[:300]
        # 2 个分隔符 + 3 条实际脚注
        assert footnotes.count("<w:footnote ") == 5, footnotes[:400]
        assert footnotes.count('w:type="') == 2, footnotes[:400]
        for label in labels:
            assert label in footnotes, label
        ET.fromstring(footnotes)  # XML 必须可解析
        print("[PASS] DOCX 写入原生脚注（footnotes.xml / 引用 / 内容类型 / 关系 / settings）")

        # ---- 3. 无引证不生成空脚注 ----
        plain_path = root / "无引证.docx"
        render.blocks_to_docx([("h1", "无引证"), ("p", "正文段落")], str(plain_path))
        with zipfile.ZipFile(plain_path) as archive:
            plain_names = set(archive.namelist())
            plain_document = archive.read("word/document.xml").decode("utf-8")
        assert "word/footnotes.xml" not in plain_names
        assert "<w:footnoteReference" not in plain_document
        print("[PASS] 无引证文档不生成空 footnotes 部件")

        # ---- 4. 其他格式保留来源 ----
        markdown = artifacts._render_markdown(blocks)
        text = artifacts._render_text(blocks)
        html = artifacts._render_html(blocks, "测试报告")
        for output in (markdown, text):
            assert labels[0] in output, output[-300:]
        assert labels[0] in html
        print("[PASS] Markdown / TXT / HTML 保留来源标签")

        xlsx_path = root / "报告.xlsx"
        render.blocks_to_xlsx(blocks, str(xlsx_path))
        from openpyxl import load_workbook

        workbook = load_workbook(str(xlsx_path))
        values = [str(cell.value) for row in workbook.worksheets[0].iter_rows() for cell in row if cell.value]
        assert any(labels[0] in value for value in values), values[:10]
        print("[PASS] XLSX 保留来源标签")

        pptx_path = root / "报告.pptx"
        render.blocks_to_pptx(blocks, str(pptx_path))
        from pptx import Presentation

        presentation = Presentation(str(pptx_path))
        slide_text = "\n".join(
            shape.text_frame.text
            for slide in presentation.slides
            for shape in slide.shapes if getattr(shape, "has_text_frame", False)
        )
        assert labels[0] in slide_text, slide_text[-300:]
        print("[PASS] PPTX 保留来源标签")

        # ---- 5. 文档可重新打开且正文完整 ----
        from docx import Document

        reopened = Document(str(docx_path))
        content = "\n".join(paragraph.text for paragraph in reopened.paragraphs)
        assert "张三" in content and "被告逾期付款" in content, content
        assert "来源：" not in content, "来源应在脚注而不是正文"
        print("[PASS] DOCX 可重新打开，正文不含内联来源")

    print("M4 Word 原生脚注引证：PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
