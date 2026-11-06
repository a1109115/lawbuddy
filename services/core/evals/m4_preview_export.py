# -*- coding: utf-8 -*-
"""M4 评测器：右侧文档区预览类型识别与产物导出格式（P0 预览内核/导出升级）。

用法（services/core 目录下）：
    python -m evals.m4_preview_export

覆盖：
1. 文件类型嗅探：GBK 文本、CSV、JSON、SVG、GIF/WebP、MP4/MP3 正确识别，二进制不被误判；
2. 导出渲染器：HTML / TXT / CSV 均可从块序列生成真实原生文件内容；
3. 无表格数据时不生成伪 CSV（明确报错）；
4. 不可转换的文件格式不做文本化转换（避免导出乱码伪文件）。

门槛：全部断言通过。
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def main() -> int:
    from lawbuddy import artifacts
    from lawbuddy.documents import sniff_kind

    with tempfile.TemporaryDirectory(prefix="lawbuddy_preview_export_") as temp:
        root = Path(temp)

        # ---- 1. 类型嗅探 ----
        gbk = root / "起诉书.txt"
        gbk.write_bytes("起诉书 被告人 张三".encode("gb18030"))
        assert sniff_kind(gbk) == ("txt", "text/plain"), sniff_kind(gbk)

        csv = root / "明细.csv"
        csv.write_text("姓名,金额\n张三,100\n", encoding="utf-8")
        assert sniff_kind(csv) == ("csv", "text/csv"), sniff_kind(csv)

        js = root / "配置.json"
        js.write_text('{"a":1}', encoding="utf-8")
        assert sniff_kind(js) == ("json", "application/json"), sniff_kind(js)

        svg = root / "图示.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8")
        assert sniff_kind(svg) == ("svg", "image/svg+xml"), sniff_kind(svg)

        gif = root / "动图.gif"
        gif.write_bytes(b"GIF89a" + b"\x00" * 20)
        assert sniff_kind(gif) == ("gif", "image/gif"), sniff_kind(gif)

        webp = root / "照片.webp"
        webp.write_bytes(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 8)
        assert sniff_kind(webp) == ("webp", "image/webp"), sniff_kind(webp)

        mp4 = root / "讯问录像.mp4"
        mp4.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20)
        assert sniff_kind(mp4) == ("mp4", "video/mp4"), sniff_kind(mp4)

        mp3 = root / "录音.mp3"
        mp3.write_bytes(b"ID3" + b"\x00" * 20)
        assert sniff_kind(mp3) == ("mp3", "audio/mpeg"), sniff_kind(mp3)

        binary = root / "未知.bin"
        binary.write_bytes(bytes(range(0, 32)) * 4)
        assert sniff_kind(binary) is None, "二进制文件不应被误判为文本"
        print("[PASS] 文件类型嗅探（GBK/CSV/JSON/SVG/GIF/WebP/MP4/MP3，二进制拒绝）")

        # ---- 2. 导出渲染器 ----
        blocks = [
            ("h1", "阅卷报告"),
            ("p", "正文段落"),
            ("li", "要点一"),
            ("table", {"header": ["日期", "事件"], "rows": [["2026-01-01", "立案"]]}),
        ]
        html = artifacts._render_html(blocks, "阅卷报告")
        assert html.startswith("<!doctype html>") and "<table>" in html and "<li>要点一</li>" in html
        assert "阅卷报告" in artifacts._render_text(blocks)
        assert "日期,事件" in artifacts._render_csv(blocks)
        print("[PASS] HTML/TXT/CSV 原生渲染")

        # ---- 3. CSV 无表格拒绝 ----
        try:
            artifacts._render_csv([("p", "只有段落")])
        except artifacts.ArtifactError as exc:
            assert "表格" in str(exc)
        else:
            raise AssertionError("无表格时应拒绝导出 CSV")
        print("[PASS] 无表格数据拒绝生成伪 CSV")

        # ---- 4. 不可转换格式拒绝 ----
        try:
            artifacts._file_to_blocks(mp4, "mp4", "讯问录像")
        except artifacts.ArtifactError as exc:
            assert "不支持转换" in str(exc)
        else:
            raise AssertionError("媒体文件不应被文本化转换")
        print("[PASS] 音视频不做文本化转换")

    print("M4 预览类型与产物导出：PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
