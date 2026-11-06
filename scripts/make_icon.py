"""生成应用图标 build/icon.ico。

用法：python make_icon.py [源图片] [--strip-white]
- 提供源图片（推荐项目根目录 lawbuddy头像.png）：等比填充到正方形后转多尺寸 ico；
- --strip-white：源图带白色圆角底卡时，先做白卡移除（全图「不透明近白」
  连通域取最大域置透明 → 保留最大非透明连通域丢阴影环 → 边界羽化），
  输出 <源图名>-trans.png 与源图同目录；
- 未提供源图：绘制"律"字占位图标（深蓝底圆角方块）。

用 venv Pillow 执行；产物提交进仓库后无需重复执行。
"""

import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ICO_SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
WHITE_MIN = 240  # 近白阈值（白卡≈254，Logo 渐变体低于此值）


def strip_white_card(src: Path) -> Path:
    """移除源图白色圆角底卡，返回处理后的 PNG 路径。"""
    img = Image.open(src).convert("RGBA")
    rgba = np.array(img)
    h, w = rgba.shape[:2]
    rgb = rgba[:, :, :3].astype(np.int16)
    near_white = (rgb >= WHITE_MIN).all(axis=2)

    # 1) 「不透明且近白」全图连通域取最大域 = 白卡
    target = near_white & (rgba[:, :, 3] == 255)
    labels = np.zeros((h, w), dtype=np.int32)
    current, best_id, best_size = 0, 0, 0
    for sy in range(h):
        for sx in range(w):
            if target[sy, sx] and labels[sy, sx] == 0:
                current += 1
                size = 0
                labels[sy, sx] = current
                q: deque[tuple[int, int]] = deque([(sy, sx)])
                while q:
                    y, x = q.popleft()
                    size += 1
                    for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                        if 0 <= ny < h and 0 <= nx < w and target[ny, nx] and labels[ny, nx] == 0:
                            labels[ny, nx] = current
                            q.append((ny, nx))
                if size > best_size:
                    best_id, best_size = current, size
    rgba[labels == best_id] = (255, 255, 255, 0)

    # 2) 保留最大非透明连通域（Logo 本体），丢弃阴影环等残留
    solid = rgba[:, :, 3] > 0
    labels = np.zeros((h, w), dtype=np.int32)
    current, best_id, best_size = 0, 0, 0
    for sy in range(h):
        for sx in range(w):
            if solid[sy, sx] and labels[sy, sx] == 0:
                current += 1
                size = 0
                labels[sy, sx] = current
                q: deque[tuple[int, int]] = deque([(sy, sx)])
                while q:
                    y, x = q.popleft()
                    size += 1
                    for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                        if 0 <= ny < h and 0 <= nx < w and solid[ny, nx] and labels[ny, nx] == 0:
                            labels[ny, nx] = current
                            q.append((ny, nx))
                if size > best_size:
                    best_id, best_size = current, size
    drop = solid & (labels != best_id)
    rgba[drop] = (255, 255, 255, 0)

    # 3) 边界 1px 羽化（弱化白边）
    opaque = rgba[:, :, 3] > 0
    grown = opaque.copy()
    grown[1:, :] |= opaque[:-1, :]
    grown[:-1, :] |= opaque[1:, :]
    grown[:, 1:] |= opaque[:, :-1]
    grown[:, :-1] |= opaque[:, 1:]
    rgba[:, :, 3][grown & ~opaque] = 120

    out = src.with_name(f"{src.stem}-trans.png")
    Image.fromarray(rgba).save(out)
    return out


def from_source(src: Path) -> Image.Image:
    """源图等比缩放并居中到透明正方形画布。"""
    img = Image.open(src).convert("RGBA")
    base = 256
    canvas = Image.new("RGBA", (base, base), (0, 0, 0, 0))
    scale = min(base / img.width, base / img.height)
    resized = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    canvas.paste(resized, ((base - resized.width) // 2, (base - resized.height) // 2), resized)
    return canvas


def fallback_drawn() -> Image.Image:
    """无源图时的占位图标：深蓝底圆角方块 + "律"字 + 书页折角。"""
    base = 1024
    img = Image.new("RGBA", (base, base), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([32, 32, base - 32, base - 32], radius=200, fill=(23, 55, 117, 255))
    font = None
    for name in ("msyhbd.ttc", "msyh.ttc", "simhei.ttf"):
        try:
            font = ImageFont.truetype(name, 560)
            break
        except OSError:
            continue
    if font is None:
        raise SystemExit("no Chinese font found")
    bbox = d.textbbox((0, 0), "律", font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((base - w) / 2 - bbox[0], (base - h) / 2 - bbox[1] - 20), "律", font=font, fill=(240, 246, 255, 255))
    d.polygon([(base - 300, base - 32), (base - 32, base - 32), (base - 32, base - 300)], fill=(96, 165, 250, 255))
    return img


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--strip-white"]
    do_strip = "--strip-white" in sys.argv
    ROOT_APP = Path(__file__).resolve().parents[1] / "apps" / "desktop" / "build"
    ROOT_APP.mkdir(parents=True, exist_ok=True)

    if args:
        src = Path(args[0])
        if not src.is_file():
            raise SystemExit(f"source image not found: {src}")
        if do_strip:
            src = strip_white_card(src)
            print(f"white card stripped -> {src.name}")
        img = from_source(src)
        print(f"from source: {src} ({Image.open(src).size})")
    else:
        img = fallback_drawn()
    img.save(ROOT_APP / "icon.ico", sizes=ICO_SIZES)
    print(f"written {ROOT_APP / 'icon.ico'}")


if __name__ == "__main__":
    main()
