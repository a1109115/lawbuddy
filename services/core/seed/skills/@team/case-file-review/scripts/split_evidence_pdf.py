#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
作者：陆凌燕（北京德恒（无锡）律师事务所）；版权所有 © 2026；随 case-file-review skill 以 MIT 许可分发

大证据册 PDF 压缩拆分（法院系统 <100MB 限制）——可复用脚本。

用途：单个 PDF 超过法院系统单文件上限（通常 100MB）时，整页重编码压缩 +
      贪心切页拆分为多份编号分册。经验与参数依据见
      references/evidence-pdf-split.md。

用法：
    python3 split_evidence_pdf.py --src 大.pdf --outdir 输出目录
    python3 split_evidence_pdf.py --src 大.pdf --outdir 输出目录 --merge   # 额外合并完整压缩版
    python3 split_evidence_pdf.py --src 大.pdf --outdir 输出目录 --limit-mb 90 --dpi 150 --quality 85

依赖：pymupdf（渲染/切分）、qpdf（--merge 时需要）、pdfinfo（poppler，自检用）。
"""
import argparse
import datetime
import os
import subprocess
import sys

import fitz


def parse_args():
    ap = argparse.ArgumentParser(description="大证据册 PDF 压缩拆分为 <100MB 分册")
    ap.add_argument("--src", required=True, help="源 PDF 路径")
    ap.add_argument("--outdir", required=True, help="输出目录（自动创建）")
    ap.add_argument("--limit-mb", type=float, default=95.0,
                    help="每份上限 MB（默认 95，留 5% 余量覆盖 100MB 的两种口径）")
    ap.add_argument("--dpi", type=int, default=200,
                    help="整页渲染 DPI（默认 200；证据册 1240x1754≈150dpi 起，200dpi 为质量/体积平衡点）")
    ap.add_argument("--quality", type=int, default=82,
                    help="JPEG 质量（默认 82；印章/高亮无可见伪影）")
    ap.add_argument("--merge", action="store_true",
                    help="拆分后额外用 qpdf 合并一份完整压缩版（供留存/打印/归档）")
    return ap.parse_args()


def main():
    args = parse_args()
    SRC = os.path.abspath(args.src)
    OUTDIR = os.path.abspath(args.outdir)
    LIMIT = int(args.limit_mb * 1_000_000)
    DPI, Q = args.dpi, args.quality
    MAT = fitz.Matrix(DPI / 72.0, DPI / 72.0)

    if not os.path.exists(SRC):
        sys.exit(f"[err] 源文件不存在: {SRC}")
    if not SRC.lower().endswith(".pdf"):
        sys.exit(f"[err] 源文件不是 PDF: {SRC}")
    os.makedirs(OUTDIR, exist_ok=True)

    src = fitz.open(SRC)
    TOTAL = src.page_count
    src_size = os.path.getsize(SRC)
    base = os.path.splitext(os.path.basename(SRC))[0]
    print(f"[start] {base}  pages={TOTAL}  size={src_size/1e6:.1f}MB  "
          f"params: dpi={DPI} q={Q} limit={args.limit_mb:.0f}MB")

    parts = []          # (part_no, first_page, last_page, n_pages, size)
    buf = []            # (pno, jpeg_bytes, w_pt, h_pt)
    buf_size = 0
    part_no = 1

    def flush():
        nonlocal buf, buf_size, part_no
        if not buf:
            return
        p0, p1 = buf[0][0] + 1, buf[-1][0] + 1
        fname = f"{base}_第{part_no:02d}册.pdf"
        fpath = os.path.join(OUTDIR, fname)
        doc = fitz.open()
        doc.set_metadata({
            "title": f"{base} 第{part_no}册",
            "subject": f"原书第{p0}-{p1}页，共{TOTAL}页（{DPI}dpi JPEG压缩版）",
            "author": "证据材料拆分版",
        })
        for _, jpg, w, h in buf:
            page = doc.new_page(width=w, height=h)
            page.insert_image(page.rect, stream=jpg)
        doc.save(fpath, garbage=4, deflate=True)
        doc.close()
        real = os.path.getsize(fpath)
        parts.append((part_no, p0, p1, len(buf), real))
        print(f"  [第{part_no:02d}册] 原书第{p0:>4d}-{p1:>4d}页 ({len(buf)}页) -> {real/1e6:5.1f}MB  {fname}")
        buf, buf_size = [], 0
        part_no += 1

    for pno in range(TOTAL):
        pix = src[pno].get_pixmap(matrix=MAT, colorspace=fitz.csRGB, alpha=False)
        jpg = pix.tobytes("jpeg", jpg_quality=Q)
        # 提前检测：单页超过上限（如 limit 设得比单页还小）→ 立即报错，不产出半成品
        if len(jpg) > LIMIT:
            sys.exit(f"[err] 第{pno+1}页单页已超过上限（{len(jpg)/1e6:.2f}MB > {args.limit_mb:.0f}MB），"
                     f"请提高 --limit-mb 或降低 --dpi/--quality")
        if buf and buf_size + len(jpg) > LIMIT:
            flush()
        rect = src[pno].rect
        buf.append((pno, jpg, rect.width, rect.height))
        buf_size += len(jpg)
        if (pno + 1) % 100 == 0 or pno == TOTAL - 1:
            print(f"  progress: {pno+1}/{TOTAL}  (当前册 {buf_size/1e6:.1f}MB)")

    flush()
    src.close()

    if not parts:
        sys.exit("[err] 未生成任何分册，请检查源文件")
    if any(sz >= LIMIT for _, _, _, _, sz in parts):
        sys.exit("[err] 存在超过上限的分册，请降低 --limit-mb 或调低 --dpi/--quality")

    # 拆分说明
    total_pages = sum(n for _, _, _, n, _ in parts)
    total_size = sum(sz for _, _, _, _, sz in parts)
    readme = os.path.join(OUTDIR, "拆分说明.txt")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(f"{base} 拆分说明\n")
        f.write(f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"源文件：{os.path.basename(SRC)}（{src_size/1e6:.1f}MB，{TOTAL} 页）\n")
        f.write(f"输出：{len(parts)} 份 PDF，每份 < {args.limit_mb:.0f}MB\n")
        f.write(f"压缩参数：{DPI}dpi 整页渲染 + JPEG 质量 {Q}\n")
        f.write(f"总输出：{total_size/1e6:.1f}MB\n")
        f.write("=" * 60 + "\n")
        f.write(f"{'分册':<8}{'原书页码':<14}{'页数':<8}{'大小':<10}{'文件名'}\n")
        f.write("-" * 70 + "\n")
        for pn, p0, p1, n, sz in parts:
            rng = f"第{p0}-{p1}页" if p0 != p1 else f"第{p0}页"
            f.write(f"第{pn:02d}册   {rng:<12} {n:<7} {sz/1e6:>6.1f}MB   {base}_第{pn:02d}册.pdf\n")
        f.write("=" * 60 + "\n")
        f.write("\n上传说明：请按分册序号依次上传；每份 PDF 元数据已标注「原书第X-Y页」。\n")
        f.write(f"总页数核对：{' + '.join(str(n) for _, _, _, n, _ in parts)} = {total_pages} 页\n")

    # 自检
    print(f"\n[check] 总页数 {total_pages} (目标 {TOTAL}) -> {'PASS' if total_pages == TOTAL else 'FAIL'}")
    print(f"[check] 每份 <{args.limit_mb:.0f}MB -> {'PASS' if all(sz < LIMIT for *_, sz in parts) else 'FAIL'}")
    print(f"[done] {len(parts)} 份分册，总 {total_pages} 页，总 {total_size/1e6:.1f}MB，说明见 {readme}")

    # 合并完整版（可选）
    if args.merge:
        merged = os.path.join(OUTDIR, f"{base}_完整压缩版.pdf")
        pages = " ".join(f'"{os.path.join(OUTDIR, f"{base}_第{i:02d}册.pdf")}"' for i in range(1, len(parts) + 1))
        subprocess.run(f'qpdf --empty --pages {pages} -- "{merged}"', shell=True, check=True)
        msz = os.path.getsize(merged)
        print(f"[merge] {os.path.basename(merged)} -> {msz/1e6:.1f}MB")


if __name__ == "__main__":
    main()
