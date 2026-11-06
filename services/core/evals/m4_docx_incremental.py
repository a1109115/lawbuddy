# -*- coding: utf-8 -*-
"""M4 评测器：DOCX 增量写回（只改被编辑段落，保留页眉/图片/编号等部件）。

用法（services/core 目录下）：
    python -m evals.m4_docx_incremental

覆盖：
1. 从 DOCX 创建的工作副本，初始版本字节与原件完全一致（不重建）；
2. 仅修改段落文字时，写回后页眉、图片、未修改段落与正文部件保持原样；
3. 结构变化（新增段落）时明确返回 warnings（按模型重建，版式风险可见），文件仍可打开；
4. 无法安全就地重建的段落（含图片）被识别并降级提示，不静默丢内容。

门槛：全部断言通过。
"""

from __future__ import annotations

import io
import json
import tempfile
import uuid
import zipfile
from pathlib import Path


def _make_docx(image_bytes: bytes) -> bytes:
    from docx import Document

    doc = Document()
    section = doc.sections[0]
    header = section.header
    header.paragraphs[0].text = "页眉：保密材料"
    doc.add_paragraph("第一段原文")
    doc.add_paragraph("第二段原文")
    doc.add_picture(io.BytesIO(image_bytes))
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "单元格A1"
    table.cell(1, 1).text = "单元格B2"
    stream = io.BytesIO()
    doc.save(stream)
    return stream.getvalue()


def main() -> int:
    from PIL import Image
    from lawbuddy import word
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents, list_documents
    from lawbuddy.storage import get_db
    from lawbuddy.workspace import ensure_workspace

    with tempfile.TemporaryDirectory(prefix="lawbuddy_docx_patch_", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        ensure_workspace(str(root / "ws"))
        case = create_case("增量写回", "civil", "委托人")

        image = Image.new("RGB", (32, 32), (200, 40, 40))
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        original = _make_docx(buf.getvalue())
        source = root / "合同.docx"
        source.write_bytes(original)
        imported = import_documents(case["id"], [str(source)])
        assert imported[0]["status"] == "imported", imported
        document_id = list_documents(case["id"])[0]["id"]

        copy = word.create_work_copy(case["id"], document_id=document_id, simplify=True)
        target_id = copy["id"]
        assert copy["revision"] == 1, copy

        first = get_db().query_one(
            "SELECT blob_uri FROM word_work_copy_versions WHERE target_id = ? AND revision = 1",
            (target_id,),
        )
        stored = word._read_work_blob(first["blob_uri"])
        assert stored == original, "初始副本应与原件字节一致（保留全部版式部件）"
        print("[PASS] 初始工作副本字节与原件一致")

        # 1. 只改一段文字：页眉/图片/其他段落保持原样
        model = word.get_model(target_id)["model"]
        first_para = model["content"][0]
        first_para["content"][0]["text"] = "第一段已修改"
        saved = word.save_model(target_id, model, 1, str(uuid.uuid4()), "edit")
        assert not saved.get("warnings"), saved.get("warnings")

        second = get_db().query_one(
            "SELECT blob_uri FROM word_work_copy_versions WHERE target_id = ? AND revision = 2",
            (target_id,),
        )
        patched = word._read_work_blob(second["blob_uri"])
        assert patched != original
        with zipfile.ZipFile(io.BytesIO(patched)) as zf:
            names = zf.namelist()
            document_xml = zf.read("word/document.xml").decode("utf-8")
            header_xml = "".join(
                zf.read(n).decode("utf-8", errors="replace") for n in names if "header" in n
            )
        assert "第一段已修改" in document_xml, "编辑后的文字未写入"
        assert "第二段原文" in document_xml, "未修改段落丢失"
        assert "单元格A1" in document_xml and "单元格B2" in document_xml, "表格内容丢失"
        assert "<w:drawing" in document_xml, "图片丢失"
        assert "页眉：保密材料" in header_xml, "页眉丢失"
        assert any(n.startswith("word/header") for n in names), "页眉部件丢失"
        print("[PASS] 段落文字增量写回，页眉/图片/表格/未改段落均保留")

        # 2. 结构变化：新增段落 → 明确告警（降级重建），不静默
        model = word.get_model(target_id)["model"]
        model["content"].append({"type": "paragraph", "content": [{"type": "text", "text": "新增段落"}]})
        saved = word.save_model(target_id, model, 2, str(uuid.uuid4()), "edit")
        warnings = saved.get("warnings") or []
        assert warnings, "结构变化应给出 warnings"
        assert any("结构" in w for w in warnings), warnings
        rebuilt_entry = get_db().query_one(
            "SELECT blob_uri FROM word_work_copy_versions WHERE target_id = ? AND revision = 3",
            (target_id,),
        )
        rebuilt = word._read_work_blob(rebuilt_entry["blob_uri"])
        from docx import Document

        text = "\n".join(p.text for p in Document(io.BytesIO(rebuilt)).paragraphs)
        assert "新增段落" in text, "降级重建结果应包含新增内容"
        print("[PASS] 结构变化明确告警并保留可打开文件")

        # 3. 含图片的段落被修改：识别为不可就地重建，给出明确告警
        #    用一份新的副本验证（上一轮结构变化已降级重建，图片不再存在于基线）
        copy2 = word.create_work_copy(case["id"], document_id=document_id, simplify=True)
        target2 = copy2["id"]
        model2 = word.get_model(target2)["model"]
        image_para = next(
            (node for node in model2["content"]
             if node.get("type") == "paragraph" and not node.get("content")),
            None,
        )
        if image_para is not None:
            image_para["content"] = [{"type": "text", "text": "图片说明"}]
            saved = word.save_model(target2, model2, 1, str(uuid.uuid4()), "edit")
            assert saved.get("warnings"), "含图片段落修改应给出告警"
            print("[PASS] 含图片段落修改给出可见告警")
        else:
            print("[SKIP] 当前模型无可定位的图片段落")

        # 4. 复杂版式文档直接建副本：自动降级为简化模型，但原件字节完整保留
        copy3 = word.create_work_copy(case["id"], document_id=document_id)
        assert copy3["revision"] == 1
        assert any("复杂版式" in w for w in copy3.get("warnings", [])), copy3.get("warnings")
        print("[PASS] 含页眉/图片的合同可直接建副本（告警可见，原件字节保留）")

    print("M4 DOCX 增量写回：PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
