"""OCR 隔离 Worker（技术方案 §2.2：OCR 按页提交到隔离 Worker，单页崩溃不拖垮应用）。

独立进程运行：`python -m lawbuddy.ocr.worker`
- 启动后加载 PaddleOCR（引擎加载完成输出 ready 行）；
- 协议（每行一条 JSON）：请求 {"imagePath": "..."} → 响应 {"ok": true, lines/fullText/avgConfidence}
  或 {"ok": false, "error": "..."}；
- Paddle 的 stdout 输出经 engine 的 fd 重定向进入 stderr，协议通道保持纯净；
- 单页失败重试 2 次后返回错误，由 Core 侧决定隔离（FR-005）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _with_extension(image_path: Path, kind: str | None) -> tuple[Path, Path | None]:
    """PaddleOCR 对无扩展名路径返回空结果；blob 文件名是 SHA-256（无后缀），
    按文档类型复制为带扩展名的临时文件，返回 (识别路径, 待清理临时文件|None)。"""
    if image_path.suffix or not kind:
        return image_path, None
    fd, tmp_name = tempfile.mkstemp(suffix=f".{kind}")
    os.close(fd)
    tmp = Path(tmp_name)
    shutil.copyfile(image_path, tmp)
    return tmp, tmp


def _restore_table_safe(image_path: Path, lines: list[dict]) -> str | None:
    """表格结构还原（table.restore_table 已全量 try/except，此处再兜一层，
    保证结构还原的任何异常都不会把本页变成识别失败）。"""
    try:
        from .table import restore_table

        return restore_table(image_path, lines)
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    from .engine import run_page

    sys.stdout.write(json.dumps({"ready": True}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        tmp_to_clean: Path | None = None
        try:
            req = json.loads(line)
            image, tmp_to_clean = _with_extension(Path(req["imagePath"]), req.get("kind"))
            result = run_page(image)
            # O-015 阶段 4：表格候选页追加结构还原（RapidTable）；内部已全量
            # 降级保护，失败/不适用返回 None，页文本保持纯 OCR 结果
            restored = _restore_table_safe(image, result["lines"])
            if restored:
                result["fullText"] = restored
            sys.stdout.write(
                json.dumps({"ok": True, **result}, ensure_ascii=True) + "\n"
            )
        except Exception as exc:
            sys.stdout.write(
                json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=True) + "\n"
            )
        finally:
            if tmp_to_clean is not None:
                try:
                    tmp_to_clean.unlink()
                except OSError:
                    pass
        sys.stdout.flush()


if __name__ == "__main__":
    main()
