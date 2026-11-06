# -*- coding: utf-8 -*-
"""M4 评测器：证据卷宗装订（封面 / 目录 / 连续页码 / 书签 / 失败清单）。

用法（services/core 目录下）：
    python -m evals.m4_evidence_bundle

覆盖：
1. 多份 PDF + 文本 + 图片按所选顺序合并为一个 PDF；
2. 输出含封面与目录、每页连续页码、每份材料的书签；
3. 不支持转换的材料（音频）进入失败清单，不静默跳过，也不阻断其余材料；
4. 所有原件字节不变（只读）；
5. 审计记录只含数量/页数，不含路径。

门槛：全部断言通过。
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from pathlib import Path


def _pdf_bytes(pages: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    try:
        pdfmetrics.getFont("STSong-Light")
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    stream = io.BytesIO()
    c = canvas.Canvas(stream, pagesize=A4)
    for text in pages:
        c.setFont("STSong-Light", 16)
        c.drawString(80, A4[1] - 120, text)
        c.showPage()
    c.save()
    return stream.getvalue()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    from PIL import Image
    from pypdf import PdfReader
    from lawbuddy import bundle
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents, list_documents
    from lawbuddy.storage import get_db
    from lawbuddy.workspace import ensure_workspace

    with tempfile.TemporaryDirectory(prefix="lawbuddy_bundle_eval_", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        ensure_workspace(str(root / "ws"))
        case = create_case("装订评测", "civil", "委托人")

        pdf1 = root / "证据一.pdf"
        pdf1.write_bytes(_pdf_bytes(["证据一：合同原件第一页", "证据一：第二页"]))
        pdf2 = root / "证据二.pdf"
        pdf2.write_bytes(_pdf_bytes(["证据二：转账凭证"]))
        txt = root / "说明.txt"
        txt.write_text("文字材料：情况说明正文", encoding="utf-8")
        png = root / "现场.png"
        Image.new("RGB", (60, 40), (30, 90, 200)).save(png)
        audio = root / "录音.mp3"
        audio.write_bytes(b"ID3" + b"\x00" * 64)

        sources = [pdf1, pdf2, txt, png]
        originals = {p.name: _sha(p) for p in sources}
        results = import_documents(case["id"], [str(pdf1), str(pdf2), str(txt), str(png), str(audio)])
        assert all(r["status"] == "imported" for r in results), results

        docs = {d["title"]: d["id"] for d in list_documents(case["id"])}
        selected = [
            docs["证据一"],
            docs["证据二"],
            docs["说明"],
            docs["现场"],
            docs["录音"],
        ]
        out = root / "证据卷宗.pdf"
        result = bundle.export_bundle(case["id"], selected, str(out))

        assert result["ok"] and out.exists()
        assert result["pageCount"] == len(PdfReader(str(out)).pages), result
        assert len(result["items"]) == 4, result["items"]
        assert len(result["failures"]) == 1 and result["failures"][0]["title"] == "录音", result["failures"]
        assert "不支持转换" in result["failures"][0]["reason"], result["failures"]
        print("[PASS] 材料装订且失败清单可见（音频未装入）")

        reader = PdfReader(str(out))
        texts = [(page.extract_text() or "") for page in reader.pages]
        all_text = "\n".join(texts)
        assert "证 据 卷 宗" in texts[0], "缺少封面"
        assert "目 录" in texts[1], "缺少目录"
        assert "证据一：合同原件第一页" in all_text, "PDF 原件内容未装入"
        assert "文字材料：情况说明正文" in all_text, "文本材料未转换装入"
        assert "第 1 页 / 共" in all_text and f"第 {result['pageCount']} 页 / 共" in all_text, "连续页码缺失"
        for item in result["items"]:
            assert f"第 {item['startPage']} 页" in texts[1] or item["startPage"] != item["endPage"], item
        print("[PASS] 封面/目录/连续页码/正文齐全")

        def outline_titles(items) -> list[str]:
            titles: list[str] = []
            for item in items:
                if isinstance(item, list):
                    titles.extend(outline_titles(item))
                else:
                    titles.append(item.title)
            return titles

        titles = outline_titles(reader.outline)
        for item in result["items"]:
            assert item["title"] in titles, (item["title"], titles)
        print("[PASS] 每份材料均有 PDF 书签")

        for path in sources:
            assert _sha(path) == originals[path.name], f"原件被修改：{path.name}"
        print("[PASS] 原件字节未改变（只读）")

        entries = get_db().query(
            "SELECT metadata_json FROM audit_logs WHERE action = 'document.export_bundle' ORDER BY created_at DESC LIMIT 1"
        )
        assert entries, "缺少装订审计"
        metadata = entries[0]["metadata_json"]
        assert str(out) not in metadata and "证据卷宗" not in metadata, metadata
        assert '"pageCount"' in metadata
        print("[PASS] 审计仅记录数量/页数，不含路径")

    print("M4 证据卷宗装订：PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
