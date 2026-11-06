"""OCR 引擎封装（DEV-DOC-006 / 技术方案 §11.1）。

- 双引擎（05-OCR迁移方案 §3.3，律阅助手 OnnxOCRClient 配方）：rapidocr（ONNX）
  优先——Windows 走 DirectML GPU（免 CUDA），不可用回退 Paddle；
  `LAWBUDDY_OCR_ENGINE=onnx|paddle|auto` 强制指定，`LAWBUDDY_OCR_PROVIDER=dml|cuda|cpu`
  指定 ONNX 后端（默认 win32→dml，其余 cpu），GPU 失败自动回退 CPU；
- Paddle 路径：惰性单例 + 引擎配方三层降级链（05-OCR迁移方案 §3.1，PP-OCRv5
  mobile + MKLDNN，最坏情况等同旧默认构造）；
- 扫描 PDF 页渲染（05-OCR迁移方案 §3.2）：pypdfium2 直渲染 + 文档级缓存；
- 按页返回逐行文本 + 归一化 bbox + 置信度（坐标统一存储为页面宽高归一化值，技术方案 §9.1）；
- 单页失败最多重试 2 次（技术方案 §17）；
- 多帧 TIFF 拆帧为单页图片；
- 首版能力限制（PRD NOGOAL/技术方案 §11.1）：复杂公式、严重手写与签名不做猜测，
  低置信行由管线按阈值标记待人工核实。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

_engine: Any = None
_engine_lock = threading.Lock()
_engine_tier: int = 0  # 实际生效层级：1=生产配方(MKLDNN) 2=同配方无MKLDNN 3=默认构造；0=未加载
_engine_model: str = ""  # 模型版本标识（status 上报，技术方案 §11.1）

# ONNX 引擎（rapidocr）：优先于 Paddle（更快、省约一半内存，模型随包内置免联网）
_onnx: Any = None
_onnx_provider: str = ""  # dml / cuda / cpu（status 上报）
_onnx_model = "PP-OCRv4_mobile(rapidocr)"  # rapidocr_onnxruntime 内置模型

# 置信度低于该阈值的行进入待人工核实（技术方案 §9.4 LOW_OCR_CONFIDENCE）
LOW_CONFIDENCE = 0.85


class OcrEngineError(RuntimeError):
    pass


def _cpu_threads() -> int:
    """引擎 CPU 线程数：LAWBUDDY_OCR_CPU_THREADS 覆盖（多 Worker 并行时由管线
    按 Worker 数均分配传入，见 pipeline._worker）；默认 cpu//2。"""
    env = os.environ.get("LAWBUDDY_OCR_CPU_THREADS", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return max(4, (os.cpu_count() or 8) // 2)


def _engine_tiers() -> list[tuple[int, str, dict[str, Any]]]:
    """三层构造配方（05-OCR迁移方案 §3.1）：任一层构造/首跑失败自动落到下一层。

    - 第 1 层：律阅助手生产验证组合（PP-OCRv5 mobile + paddlepaddle 3.x + MKLDNN
      长期在产环境运行）；kill-switch LAWBUDDY_OCR_NO_MKLDNN=1 跳过本层；
    - 第 2 层：同配方但 enable_mkldnn=False（目标侧历史顾虑：paddlepaddle 3.x 的
      oneDNN PIR 执行器对部分模型属性报 Unimplemented）；
    - 第 3 层：改造前默认构造（不指定模型版本），最坏情况等同现状。
    """
    mobile = {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "ocr_version": "PP-OCRv5",
        "text_detection_model_name": "PP-OCRv5_mobile_det",
        "text_recognition_model_name": "PP-OCRv5_mobile_rec",
        "cpu_threads": _cpu_threads(),
    }
    tiers: list[tuple[int, str, dict[str, Any]]] = []
    if os.environ.get("LAWBUDDY_OCR_NO_MKLDNN", "").strip() != "1":
        tiers.append((1, "PP-OCRv5_mobile", {**mobile, "enable_mkldnn": True}))
    tiers.append((2, "PP-OCRv5_mobile", {**mobile, "enable_mkldnn": False}))
    tiers.append((3, "default", {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "enable_mkldnn": False,
    }))
    return tiers


def _warmup(engine: Any) -> None:
    """首跑验证（Paddle 路径）：小图跑一遍 predict，推理链路崩溃时由降级链接管。"""
    from PIL import Image

    fd, name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        Image.new("RGB", (64, 64), "white").save(name, format="PNG")
        engine.predict(name)
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# ONNX 引擎（05-OCR迁移方案 §3.3，律阅助手 OnnxOCRClient 生产配方）
# ---------------------------------------------------------------------------

def _engine_pref() -> str:
    """引擎偏好：LAWBUDDY_OCR_ENGINE=onnx|paddle|auto（默认 auto，ONNX 优先）。"""
    raw = os.environ.get("LAWBUDDY_OCR_ENGINE", "").strip().lower()
    return raw if raw in ("onnx", "paddle") else "auto"


def _onnx_provider_pref() -> str:
    """ONNX 后端：LAWBUDDY_OCR_PROVIDER=dml|cuda|cpu；默认 Windows 走 dml
    （DirectML 免 CUDA/cuDNN，任何 DX12 GPU 可用），其余平台 cpu。"""
    raw = os.environ.get("LAWBUDDY_OCR_PROVIDER", "").strip().lower()
    if raw in ("dml", "cuda", "cpu"):
        return raw
    return "dml" if sys.platform == "win32" else "cpu"


def _get_onnx() -> Any:
    """构造 rapidocr 单例：GPU（dml/cuda）优先，失败回退 CPU。

    det 输入长边 640（律阅助手实测：该档法律文书 -22% 耗时且不丢行，
    默认 736/min）；1×1 预热图验证推理链路（对应律阅助手 probeOnnxEngineAvailable）。
    """
    global _onnx, _onnx_provider
    with _engine_lock:
        if _onnx is not None:
            return _onnx
        from rapidocr_onnxruntime import RapidOCR

        base: dict[str, Any] = {"det_limit_side_len": 640, "det_limit_type": "min"}
        provider = _onnx_provider_pref()
        if provider in ("dml", "cuda"):
            try:
                gpu_kwargs = {
                    **base,
                    **{f"{key}_use_{provider}": True for key in ("det", "rec", "cls")},
                }
                candidate = RapidOCR(**gpu_kwargs)
                _warmup_onnx(candidate)
                _onnx, _onnx_provider = candidate, provider
                return _onnx
            except Exception:  # noqa: BLE001  # GPU 不可用（驱动/显存/运行库缺失）→ CPU
                pass
        # CPU：兼容不同版本参数名（全局 intra_op_num_threads → 分模型 → 默认）
        try:
            candidate = RapidOCR(intra_op_num_threads=_cpu_threads(), **base)
        except TypeError:
            candidate = RapidOCR(**{
                **base,
                "det_intra_op_num_threads": _cpu_threads(),
                "rec_intra_op_num_threads": _cpu_threads(),
                "cls_intra_op_num_threads": _cpu_threads(),
            })
        _warmup_onnx(candidate)
        _onnx, _onnx_provider = candidate, "cpu"
        return _onnx


def _warmup_onnx(engine: Any) -> None:
    """1×1 预热图验证 ONNX 推理链路（引擎级故障时由上层回退 Paddle）。"""
    import numpy as np

    engine(np.zeros((1, 1, 3), dtype=np.uint8))


def _onnx_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return True
    except Exception:  # noqa: BLE001  # 依赖缺失（含 VC++ 运行库）→ 不可用
        return False


def _get_engine() -> Any:
    global _engine, _engine_tier, _engine_model
    with _engine_lock:
        if _engine is None:
            from paddleocr import PaddleOCR

            last_err: Exception | None = None
            for tier, model, kwargs in _engine_tiers():
                try:
                    candidate = PaddleOCR(**kwargs)
                    _warmup(candidate)
                    _engine, _engine_tier, _engine_model = candidate, tier, model
                    return _engine
                except Exception as exc:  # noqa: PERF203
                    last_err = exc
            raise OcrEngineError(f"OCR engine init failed on all tiers: {last_err}")
        return _engine


def status() -> dict[str, Any]:
    """引擎可用性探测（Worker 进程内加载后含实际生效引擎信息）。

    engineKind：auto 偏好下实际会用的引擎（onnx 安装即优先）；
    强制 LAWBUDDY_OCR_ENGINE 时按指定值上报。
    """
    pref = _engine_pref()
    onnx_ok = _onnx_available()
    kind = "onnx" if (pref in ("onnx", "auto") and onnx_ok) or pref == "onnx" else "paddle"
    if not onnx_ok:
        try:
            import paddleocr  # noqa: F401
        except Exception as exc:
            return {"available": False, "reason": type(exc).__name__}
    from .table import table_restore_enabled

    return {
        "available": True,
        "engineKind": kind,
        "model": (_onnx_model if kind == "onnx" else _engine_model) or None,
        "engineTier": None if kind == "onnx" else (_engine_tier or None),
        "provider": _onnx_provider or None,  # ONNX 后端（加载后有效）
        "tableRestore": table_restore_enabled(),  # O-015 阶段 4：扫描件表格结构还原
    }


def iter_page_images(blob_path: Path):
    """把输入文件展开为可 OCR 的单页图片路径列表。

    PNG/JPG 原样；多帧 TIFF 拆帧为临时 PNG（临时文件由调用方清理）。
    """
    suffix = blob_path.suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg"):
        return [blob_path]
    if suffix == ".tiff" or suffix == ".tif":
        from PIL import Image

        out: list[Path] = []
        with Image.open(blob_path) as im:
            for i in range(getattr(im, "n_frames", 1)):
                im.seek(i)
                tmp = blob_path.parent / f"{blob_path.stem}.frame{i}.png"
                im.convert("RGB").save(tmp, format="PNG")
                out.append(tmp)
        return out
    return []


def run_page(image_path: Path) -> dict[str, Any]:
    """对单页图片执行 OCR（双引擎分发：ONNX 优先，回退 Paddle）。

    返回 { lines: [{text, bbox(归一化[x1,y1,x2,y2]), confidence}], fullText, avgConfidence }。
    失败重试 2 次后抛 OcrEngineError。
    """
    last_err: Exception | None = None
    for attempt in range(3):  # 1 次执行 + 2 次重试（技术方案 §17）
        try:
            return _run_once(image_path)
        except Exception as exc:  # noqa: PERF203
            last_err = exc
    raise OcrEngineError(f"OCR failed after retries: {last_err}")


def _run_once(image_path: Path) -> dict[str, Any]:
    pref = _engine_pref()
    if pref in ("onnx", "auto"):
        try:
            return _run_onnx_once(_get_onnx(), image_path)
        except Exception:
            if pref == "onnx":
                raise
            # auto：ONNX 页级失败回退 Paddle 重试本页（引擎级失败已在 _get_onnx 内
            # 回退 CPU，此处兜 GPU 掉卡/驱动等运行期故障）
    return _run_paddle_once(_get_engine(), image_path)


def _run_onnx_once(engine: Any, image_path: Path) -> dict[str, Any]:
    """rapidocr 单页识别：[[box(像素四点), text, score]] → 统一结果结构。"""
    from PIL import Image

    with Image.open(image_path) as im:
        width, height = im.size
    res = engine(str(image_path))
    if isinstance(res, tuple) and len(res) >= 1:  # 多数版本返回 (result, elapse)
        res = res[0]
    lines: list[dict[str, Any]] = []
    for row in res or []:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        text = str(row[1]).strip() if row[1] is not None else ""
        if not text:
            continue
        score = float(row[2]) if isinstance(row[2], (int, float)) else 0.0
        bbox = None
        poly = row[0]
        if isinstance(poly, (list, tuple)) and len(poly) >= 2:
            xs = [float(p[0]) for p in poly if isinstance(p, (list, tuple)) and len(p) >= 2]
            ys = [float(p[1]) for p in poly if isinstance(p, (list, tuple)) and len(p) >= 2]
            if len(xs) >= 2:
                # 归一化为页面宽高比例（技术方案 §9.1，与 Paddle 路径一致）
                bbox = [
                    round(min(xs) / width, 4),
                    round(min(ys) / height, 4),
                    round(max(xs) / width, 4),
                    round(max(ys) / height, 4),
                ]
        lines.append({"text": text, "bbox": bbox, "confidence": round(score, 4)})

    full_text = "\n".join(l["text"] for l in lines)
    avg_conf = round(sum(l["confidence"] for l in lines) / len(lines), 4) if lines else 0.0
    return {"lines": lines, "fullText": full_text, "avgConfidence": avg_conf}


def _run_paddle_once(engine: Any, image_path: Path) -> dict[str, Any]:
    from PIL import Image

    with Image.open(image_path) as im:
        width, height = im.size

    # PaddleOCR/Paddle 会向 stdout 打印进度与日志，会污染 JSON-RPC 协议通道。
    # 识别期间把 fd1 重定向到 fd2（stderr），C 层与 Python 层输出全部入 stderr。
    sys.stdout.flush()
    saved_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        results = engine.predict(str(image_path))
    finally:
        sys.stdout.flush()
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
    lines: list[dict[str, Any]] = []
    for result in results or []:
        texts = list(result.get("rec_texts", []) or [])
        scores = list(result.get("rec_scores", []) or [])
        polys = list(result.get("rec_polys", []) or [])
        for i, text in enumerate(texts):
            if not text or not text.strip():
                continue
            conf = float(scores[i]) if i < len(scores) else 0.0
            bbox = None
            if i < len(polys):
                poly = polys[i]
                xs = [float(p[0]) for p in poly]
                ys = [float(p[1]) for p in poly]
                # 归一化为页面宽高比例，左上角为原点（技术方案 §9.1）
                bbox = [
                    round(min(xs) / width, 4),
                    round(min(ys) / height, 4),
                    round(max(xs) / width, 4),
                    round(max(ys) / height, 4),
                ]
            lines.append({"text": text.strip(), "bbox": bbox, "confidence": round(conf, 4)})

    full_text = "\n".join(l["text"] for l in lines)
    avg_conf = round(sum(l["confidence"] for l in lines) / len(lines), 4) if lines else 0.0
    return {"lines": lines, "fullText": full_text, "avgConfidence": avg_conf}


# ---------------------------------------------------------------------------
# 扫描 PDF 页渲染（05-OCR迁移方案 §3.2，参考律阅助手 pdf_doc_cache 生产实现）
# ---------------------------------------------------------------------------

_pdf_doc_lock = threading.Lock()
_pdf_doc_cache: dict[str, Any] = {}  # 路径 → PdfDocument（文档级缓存，至多一个）


def render_pdf_page(pdf_path: Path, page_no: int, scale: float = 1.2, out_path: Path | None = None) -> Path:
    """pypdfium2 渲染 PDF 当前页为 PNG，返回产物路径。

    - 文档级缓存：同一 PDF 多页连跑不重复打开/解析；新文档打开时关闭并清空旧文档；
    - out_path 应位于案件临时目录（纳入 tempstore.release 清理链，REQ-DOC-012），
      未指定时落在 PDF 同目录（与 TIFF 拆帧约定一致）；
    - 页码越界或渲染失败抛 OcrEngineError，由管线隔离该页（FR-005）。
    """
    import pypdfium2 as pdfium

    src = Path(pdf_path)
    target = Path(out_path) if out_path else src.parent / f"{src.stem}.page{page_no}.png"
    key = str(src.resolve())
    with _pdf_doc_lock:
        doc = _pdf_doc_cache.get(key)
        if doc is None:
            for old in _pdf_doc_cache.values():
                try:
                    old.close()
                except Exception:
                    pass
            _pdf_doc_cache.clear()
            doc = pdfium.PdfDocument(key)
            _pdf_doc_cache[key] = doc
        if page_no < 1 or page_no > len(doc):
            raise OcrEngineError(f"pdf page out of range: {page_no}")
        bitmap = doc[page_no - 1].render(scale=scale)
        try:
            bitmap.to_pil().convert("RGB").save(target, format="PNG")
        finally:
            try:
                bitmap.close()
            except Exception:
                pass
    return target


def close_pdf(pdf_path: Path) -> None:
    """关闭并移除缓存的 PDF 文档句柄。

    删除临时 PDF 前必须先关句柄，否则 Windows 下文件被占用导致删除失败、留下明文残留。
    """
    key = str(Path(pdf_path).resolve())
    with _pdf_doc_lock:
        doc = _pdf_doc_cache.pop(key, None)
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
