# -*- coding: utf-8 -*-
"""证据卷宗装订：多份原件合并为一个 PDF（封面、目录、连续页码、书签）。

- 原件只读：PDF 直接读取可读字节（含已解锁版本），不修改源文件；
- DOCX/XLSX/PPTX/文本/图片：先转换为 PDF 再装入（结果标记 converted，界面可见）；
- 无法转换或解析失败的材料列出失败原因，不静默跳过；
- 输出写审计（只记数量/页数，不记目标路径与原文）。
"""

from __future__ import annotations

import io
import math
import tempfile
from pathlib import Path
from typing import Any

from . import documents, render, tempstore
from .storage import get_db
from .workspace import now_iso

PDF_FONT = "STSong-Light"


class BundleError(RuntimeError):
    pass


# 可直接合并的 PDF 源类型；其余先转换为 PDF
_PDF_KINDS = {"pdf"}
_IMAGE_KINDS = {"png", "jpg", "tiff", "gif", "webp", "bmp"}


def _readable_bytes(row: dict[str, Any]) -> bytes:
    """读取可读原件字节（优先已解锁版本；临时明文随上下文释放）。"""
    root = documents._workspace_root()
    uri = documents._resolve_blob_uri(row["version_id"], row["blob_uri"])
    with tempstore.materialized(root, uri, str(row["case_id"]), str(row["kind"])) as path:
        return path.read_bytes()


def _image_to_pdf(data: bytes, dest: Path, title: str) -> None:
    """图片 → 单页 A4 PDF（等比缩放居中）。"""
    from PIL import Image
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    try:
        pdfmetrics.getFont(PDF_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT))

    with Image.open(io.BytesIO(data)) as im:
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        rgb = im.convert("RGB")
        tmp_png = dest.with_suffix(".png")
        rgb.save(tmp_png, format="PNG")
        img_w, img_h = rgb.size

    c = canvas.Canvas(str(dest), pagesize=A4)
    max_w, max_h = A4[0] - 3 * cm, A4[1] - 4 * cm
    scale = min(max_w / max(1, img_w), max_h / max(1, img_h))
    draw_w, draw_h = img_w * scale, img_h * scale
    c.drawImage(
        str(tmp_png),
        (A4[0] - draw_w) / 2,
        (A4[1] - draw_h) / 2 + 1.5 * cm,
        width=draw_w,
        height=draw_h,
        preserveAspectRatio=True,
        mask="auto",
    )
    c.setFont(PDF_FONT, 9)
    c.drawCentredString(A4[0] / 2, 1.2 * cm, title[:80])
    c.showPage()
    c.save()
    tmp_png.unlink(missing_ok=True)


def _make_cover(path: Path, case_title: str, total_docs: int) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    try:
        pdfmetrics.getFont(PDF_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT))

    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont(PDF_FONT, 26)
    c.drawCentredString(A4[0] / 2, A4[1] - 8 * cm, "证 据 卷 宗")
    c.setFont(PDF_FONT, 15)
    c.drawCentredString(A4[0] / 2, A4[1] - 11 * cm, case_title[:60])
    c.setFont(PDF_FONT, 11)
    c.drawCentredString(A4[0] / 2, A4[1] - 14 * cm, f"材料 {total_docs} 份")
    c.drawCentredString(A4[0] / 2, A4[1] - 15.4 * cm, now_iso()[:19].replace("T", " "))
    c.setFont(PDF_FONT, 9)
    c.drawCentredString(A4[0] / 2, 3 * cm, "本卷由 LawBuddy 在本地生成；各份原件未被修改。")
    c.showPage()
    c.save()


def _toc_pages(items: list[dict[str, Any]]) -> int:
    return max(1, math.ceil(len(items) / 18))


def _make_toc(path: Path, items: list[dict[str, Any]], total_pages: int) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    try:
        pdfmetrics.getFont(PDF_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT))

    per_page = 18
    pages = _toc_pages(items)
    c = canvas.Canvas(str(path), pagesize=A4)
    for page_index in range(pages):
        c.setFont(PDF_FONT, 16)
        c.drawString(2.5 * cm, A4[1] - 3.2 * cm, "目 录")
        c.setFont(PDF_FONT, 10.5)
        y = A4[1] - 5 * cm
        for item in items[page_index * per_page:(page_index + 1) * per_page]:
            label = item["title"][:42]
            suffix = "（转换件）" if item.get("converted") else ""
            pages_text = f"第 {item['startPage']} 页" if item["startPage"] == item["endPage"] else f"第 {item['startPage']}-{item['endPage']} 页"
            c.drawString(2.5 * cm, y, f"· {label}{suffix}")
            c.drawRightString(A4[0] - 2.5 * cm, y, pages_text)
            y -= 0.92 * cm
        c.setFont(PDF_FONT, 9)
        c.showPage()
    c.save()


def _make_stamps(path: Path, total_pages: int) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    try:
        pdfmetrics.getFont(PDF_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT))

    c = canvas.Canvas(str(path), pagesize=A4)
    for index in range(total_pages):
        c.setFont(PDF_FONT, 8.5)
        c.drawCentredString(A4[0] / 2, 1.25 * cm, f"第 {index + 1} 页 / 共 {total_pages} 页")
        c.showPage()
    c.save()


def export_bundle(case_id: str, document_ids: list[str], dest_path: str) -> dict[str, Any]:
    """装订证据卷并写入目标路径；返回页数、目录项与失败清单（不返回路径）。"""
    from pypdf import PdfReader, PdfWriter

    db = get_db()
    case = db.query_one("SELECT id, title FROM cases WHERE id = ?", (case_id,))
    if not case:
        raise BundleError("案件不存在")
    if not document_ids:
        raise BundleError("请选择要装订的材料")

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, document_id in enumerate(document_ids, start=1):
        row = db.query_one(
            """
            SELECT d.id, d.case_id, d.kind, d.title, d.status,
                   v.id AS version_id, v.blob_uri, v.unlocked_blob_uri
            FROM documents d LEFT JOIN document_versions v ON v.id = d.active_version_id
            WHERE d.id = ? AND d.case_id = ?
            """,
            (document_id, case_id),
        )
        if row is None or row["version_id"] is None:
            failures.append({"documentId": document_id, "title": f"材料 {index}", "reason": "材料不存在或没有可用版本"})
            continue
        if row["status"] == "need_password" and not row["unlocked_blob_uri"]:
            failures.append({"documentId": document_id, "title": row["title"], "reason": "文档已加密，需先解锁"})
            continue
        rows.append(row)

    if not rows:
        raise BundleError("没有可装订的材料（所选文件均不可读）")

    with tempfile.TemporaryDirectory(prefix="lawbuddy_bundle_") as temp:
        temp_dir = Path(temp)
        parts: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            kind = str(row["kind"] or "")
            title = str(row["title"] or f"材料 {index}")
            part_path = temp_dir / f"part_{index}.pdf"
            converted = False
            try:
                data = _readable_bytes(row)
                if kind in _PDF_KINDS:
                    reader = PdfReader(io.BytesIO(data))
                    if reader.is_encrypted and not row["unlocked_blob_uri"]:
                        raise BundleError("PDF 已加密，需先解锁")
                    part_path.write_bytes(data)
                elif kind in _IMAGE_KINDS:
                    _image_to_pdf(data, part_path, title)
                    converted = True
                else:
                    from . import artifacts

                    source = temp_dir / f"source_{index}.{kind}"
                    source.write_bytes(data)
                    blocks = artifacts._file_to_blocks(source, kind, title)
                    render.blocks_to_pdf(blocks, str(part_path))
                    converted = True
                if not part_path.exists() or part_path.stat().st_size == 0:
                    raise BundleError("转换为 PDF 失败")
                page_count = len(PdfReader(str(part_path)).pages)
                if page_count <= 0:
                    raise BundleError("材料没有可装订页面")
                parts.append({"row": row, "path": part_path, "pages": page_count, "converted": converted})
            except Exception as exc:  # 单份失败不阻断其余材料，但必须列出原因
                failures.append({
                    "documentId": str(row["id"]),
                    "title": title,
                    "reason": str(exc) or type(exc).__name__,
                })

        if not parts:
            raise BundleError("所选材料均无法转换为 PDF，请检查失败清单")

        toc_count = _toc_pages(parts)
        body_start = 1 + toc_count + 1  # 封面 1 页 + 目录页
        cursor = body_start
        items: list[dict[str, Any]] = []
        for part in parts:
            start = cursor
            end = cursor + part["pages"] - 1
            items.append({
                "documentId": str(part["row"]["id"]),
                "title": str(part["row"]["title"]),
                "startPage": start,
                "endPage": end,
                "converted": part["converted"],
            })
            cursor = end + 1

        total_pages = cursor - 1
        cover_path = temp_dir / "cover.pdf"
        toc_path = temp_dir / "toc.pdf"
        stamp_path = temp_dir / "stamps.pdf"
        _make_cover(cover_path, str(case["title"]), len(parts))
        _make_toc(toc_path, items, total_pages)
        _make_stamps(stamp_path, total_pages)

        writer = PdfWriter()
        for base in (cover_path, toc_path):
            for page in PdfReader(str(base)).pages:
                writer.add_page(page)
        for item, part in zip(items, parts):
            reader = PdfReader(str(part["path"]))
            for page in reader.pages:
                writer.add_page(page)
            writer.add_outline_item(item["title"], item["startPage"] - 1)

        stamps = PdfReader(str(stamp_path)).pages
        for index, page in enumerate(writer.pages):
            page.merge_page(stamps[index])

        try:
            with open(dest_path, "wb") as fh:
                writer.write(fh)
        except OSError as exc:
            raise BundleError(f"无法写入目标文件（{type(exc).__name__}）") from exc

    size = Path(dest_path).stat().st_size
    from . import audit

    with get_db().transaction() as conn:
        audit.append(
            conn,
            action="document.export_bundle",
            resource_type="case",
            resource_id=case_id,
            case_id=case_id,
            metadata={
                "documentCount": len(items),
                "failedCount": len(failures),
                "pageCount": total_pages,
                "format": "pdf",
            },
        )

    return {
        "ok": True,
        "fileName": Path(dest_path).name,
        "sizeBytes": size,
        "pageCount": total_pages,
        "items": items,
        "failures": failures,
    }
