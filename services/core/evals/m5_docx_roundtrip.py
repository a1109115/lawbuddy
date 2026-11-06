# -*- coding: utf-8 -*-
"""M5 评测器：DOCX 往返保真（open → 不改 → save 零差异；改一段只动一段）。

用法（services/core 目录下）：
    python -m evals.m5_docx_roundtrip

覆盖（对应升级方案 P1.1「往返保真样本库」）：
1. 生成 10 份典型材料（标题/表格/列表/页眉页脚/图片/合并单元格/分页/格式/长文）；
2. from_docx 读取后原样保存：docxpatch 必须返回原字节（document.xml 差异为 0）；
3. 只改一个段落：除 document.xml 外所有 zip 部件字节不变（页眉、页脚、图片等保真）；
4. 修改后的 document.xml 含新正文且仍可被 python-docx 打开；
5. 原件字节不被修改（只读边界）。

门槛：全部断言通过。
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path


def _build_samples(root: Path) -> list[tuple[str, Path]]:
    from docx import Document
    from docx.shared import Inches, RGBColor

    samples: list[tuple[str, Path]] = []

    def save(name: str, doc) -> None:
        path = root / name
        doc.save(path)
        samples.append((name, path))

    # 1. 标题 + 段落
    doc = Document()
    doc.add_heading("民事起诉状", level=1)
    doc.add_paragraph("原告与被告因买卖合同纠纷，向贵院提起诉讼。")
    save("01_标题段落.docx", doc)

    # 2. 表格
    doc = Document()
    doc.add_paragraph("证据清单")
    table = doc.add_table(rows=3, cols=3)
    for row in range(3):
        for col in range(3):
            table.cell(row, col).text = f"R{row}C{col}"
    save("02_表格.docx", doc)

    # 3. 项目符号列表
    doc = Document()
    doc.add_paragraph("诉讼请求", style="List Bullet")
    doc.add_paragraph("判令被告支付货款", style="List Bullet")
    save("03_项目列表.docx", doc)

    # 4. 编号列表
    doc = Document()
    doc.add_paragraph("第一步：提交证据", style="List Number")
    doc.add_paragraph("第二步：等待开庭", style="List Number")
    save("04_编号列表.docx", doc)

    # 5. 页眉页脚
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "某某律师事务所"
    doc.sections[0].footer.paragraphs[0].text = "第 1 页"
    doc.add_paragraph("页眉页脚样本正文")
    save("05_页眉页脚.docx", doc)

    # 6. 图片
    from PIL import Image

    image_path = root / "stamp.png"
    Image.new("RGB", (80, 40), (200, 30, 30)).save(image_path)
    doc = Document()
    doc.add_paragraph("盖章页")
    doc.add_picture(str(image_path), width=Inches(1.2))
    save("06_图片.docx", doc)

    # 7. 合并单元格
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(0, 0).text = "合并标题"
    table.cell(1, 0).text = "左"
    table.cell(1, 1).text = "右"
    save("07_合并单元格.docx", doc)

    # 8. 分页
    doc = Document()
    doc.add_paragraph("第一页正文")
    doc.add_page_break()
    doc.add_paragraph("第二页正文")
    save("08_分页.docx", doc)

    # 9. 字符格式
    doc = Document()
    paragraph = doc.add_paragraph()
    paragraph.add_run("加粗").bold = True
    paragraph.add_run("斜体").italic = True
    colored = paragraph.add_run("红色")
    colored.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
    save("09_字符格式.docx", doc)

    # 10. 长合同
    doc = Document()
    doc.add_heading("购销合同", level=1)
    for index in range(1, 13):
        doc.add_paragraph(f"第{index}条 双方约定第{index}项权利义务，具体以附件为准。")
    save("10_长合同.docx", doc)
    return samples


def main() -> int:
    from docx import Document

    from lawbuddy import docxpatch, word

    with tempfile.TemporaryDirectory(prefix="lawbuddy_docx_roundtrip_") as temp:
        root = Path(temp)
        samples = _build_samples(root)
        assert len(samples) == 10, len(samples)

        editable_sample: tuple[str, Path, bytes, dict] | None = None
        for name, path in samples:
            original = path.read_bytes()
            model, _warnings = word.from_docx(original, simplify=True)
            patched, changed = docxpatch.patch_docx(original, model, model)
            assert changed is False, f"{name}：未修改不应触发写回"
            assert patched == original, f"{name}：未修改保存后字节不一致"
            assert path.read_bytes() == original, f"{name}：原件被修改"
            if editable_sample is None:
                for block in model.get("content") or []:
                    if block.get("type") not in ("paragraph", "heading"):
                        continue
                    texts = [child for child in block.get("content") or [] if child.get("type") == "text"]
                    if texts and len(texts[0].get("text") or "") >= 4:
                        texts[0]["text"] = "编辑后的正文内容"
                        editable_sample = (name, path, original, model)
                        break
        print("[PASS] 10 份 DOCX 原样保存零差异（document.xml 未重建，原件只读）")

        assert editable_sample is not None
        name, path, original, model = editable_sample
        base_model, _warnings = word.from_docx(original, simplify=True)
        edited, changed = docxpatch.patch_docx(original, base_model, model)
        assert changed is True, f"{name}：段落修改未写回"

        with zipfile.ZipFile(io.BytesIO(original)) as before, zipfile.ZipFile(io.BytesIO(edited)) as after:
            assert set(before.namelist()) == set(after.namelist()), "zip 部件集合不应变化"
            for part in before.namelist():
                if part == "word/document.xml":
                    continue
                assert before.read(part) == after.read(part), f"{name}：{part} 被意外改写"
            document_xml = after.read("word/document.xml").decode("utf-8")
        assert "编辑后的正文内容" in document_xml
        assert path.read_bytes() == original, "原件不应被写回修改"
        reopened = Document(io.BytesIO(edited))
        assert any("编辑后的正文内容" in paragraph.text for paragraph in reopened.paragraphs)
        print(f"[PASS] 只改一段：除 document.xml 外全部部件字节不变（{name}）")

        print("M5 DOCX 往返保真：PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
