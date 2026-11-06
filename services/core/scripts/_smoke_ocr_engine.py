# -*- coding: utf-8 -*-
"""OCR 引擎层级 smoke：实证三层降级链在当前环境实际生效的层级与耗时。

用法（services/core 目录下）：
    python scripts/_smoke_ocr_engine.py [--no-mkldnn]

--no-mkldnn 等效 kill-switch LAWBUDDY_OCR_NO_MKLDNN=1，用于对比跳过第 1 层的表现。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _sample_image(tmp: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = ImageFont.load_default(size=36)
    img = tmp / "sample.png"
    im = Image.new("RGB", (860, 260), "white")
    draw = ImageDraw.Draw(im)
    draw.text((40, 50), "Hello OCR 12345", fill="black", font=font)
    draw.text((40, 120), "Contract No. 2026-09-21", fill="black", font=font)
    draw.text((40, 190), "Amount: 1,000,000.00 USD", fill="black", font=font)
    im.save(img, format="PNG")
    return img


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-mkldnn", action="store_true", help="等效 LAWBUDDY_OCR_NO_MKLDNN=1")
    args = parser.parse_args()
    if args.no_mkldnn:
        os.environ["LAWBUDDY_OCR_NO_MKLDNN"] = "1"
    # 对齐 Worker 子进程环境（pipeline._OcrWorker）
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "MODELSCOPE_OFFLINE"):
        os.environ.setdefault(key, "1")

    from lawbuddy.ocr import engine

    with tempfile.TemporaryDirectory(prefix="ocr-smoke-") as td:
        img = _sample_image(Path(td))

        t0 = time.monotonic()
        engine._get_engine()  # noqa: SLF001 -- 触发加载与降级链
        load_s = time.monotonic() - t0
        t1 = time.monotonic()
        result = engine.run_page(img)
        first_s = time.monotonic() - t1
        t2 = time.monotonic()
        result2 = engine.run_page(img)
        warm_s = time.monotonic() - t2

    st = engine.status()
    print(f"engineTier={st.get('engineTier')} model={st.get('model')} load={load_s:.1f}s")
    print(f"firstPage={first_s:.2f}s warmPage={warm_s:.2f}s")
    print(f"lines={len(result['lines'])} avgConfidence={result['avgConfidence']}")
    for line in result["lines"][:6]:
        print(f"  {line['confidence']:.3f}  {line['text']}")
    assert result["fullText"] == result2["fullText"], "两次识别结果不一致"
    print("smoke OK")


if __name__ == "__main__":
    main()
