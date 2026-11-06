"""冻结 ocr-worker 端到端推理验证：生成含文字图片 → 子命令 worker 识别。

覆盖两条链路：
- 英文图：ONNX 推理链路可用性；
- 中文图（PIL + 系统字体渲染）：中文识别结果经 stdout 协议返回的编码完整性
  （冻结模式标准流编码坑，见 _smoke_frozen_encoding.py）。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont


def _render_cn(text: str) -> Path:
    """PIL + 系统中文字体渲染测试图（cv2.putText 不支持中文）。"""
    img = Image.new("RGB", (760, 130), "white")
    d = ImageDraw.Draw(img)
    font = None
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
        try:
            font = ImageFont.truetype(name, 48)
            break
        except OSError:
            continue
    if font is None:
        raise SystemExit("no Chinese font found")
    d.text((12, 36), text, font=font, fill="black")
    tmp = Path(tempfile.mkstemp(suffix=".png")[1])
    img.save(tmp)
    return tmp


def _ocr_once(exe: str, img_path: Path) -> dict:
    env = {**os.environ, "LAWBUDDY_OCR_ENGINE": "onnx"}
    w = subprocess.Popen(
        [exe, "ocr-worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert w.stdout.readline().strip(), "no ready"
    w.stdin.write(json.dumps({"imagePath": str(img_path), "kind": "png"}) + "\n")
    w.stdin.flush()
    line = w.stdout.readline()
    w.stdin.close()
    w.wait(timeout=120)
    try:
        img_path.unlink(missing_ok=True)
    except PermissionError:
        pass
    return json.loads(line)


def main() -> int:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist_core") / "lawbuddy-core"
    exe = str(dist / "lawbuddy-core.exe")

    # 1) 英文图：推理链路
    img = np.full((120, 600, 3), 255, dtype=np.uint8)
    cv2.putText(img, "Contract Amount: 10000 CNY", (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 2, cv2.LINE_AA)
    en_png = Path(tempfile.mkstemp(suffix=".png")[1])
    cv2.imwrite(str(en_png), img)
    resp = _ocr_once(exe, en_png)
    full = resp.get("fullText", "")
    print(f"[e2e/en] ok={resp.get('ok')} fullText={full!r}")
    if not resp.get("ok") or "10000" not in full:
        print("[e2e/en] FAIL")
        return 1

    # 2) 中文图：识别 + stdout 编码往返
    cn_png = _render_cn("借款金额人民币壹佰万元整")
    resp = _ocr_once(exe, cn_png)
    full = resp.get("fullText", "")
    print(f"[e2e/cn] ok={resp.get('ok')} fullText={full!r}")
    if not resp.get("ok") or "壹佰万" not in full:
        print("[e2e/cn] FAIL: 中文识别/编码链路损坏")
        return 1
    print("[e2e] PASS: 冻结 OCR 推理与中文编码链路完整")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
