"""扫描件表格结构还原（O-015 阶段 4，RapidTable / SLANet-plus，Apache-2.0）。

OCR 页识别完成后，对表格候选页追加结构还原：SLANet-plus ONNX 预测单元格与
表格结构，OCR 文本行按坐标匹配填回，输出 Markdown 管道表格并融合回页文本。

降级与护栏约定（防丢内容优先于结构化收益，OCR 主流程永不受影响）：
- 全程 try/except 包裹：依赖缺失、模型缺失、推理异常、内容丢失一律返回 None，
  由调用方回退纯 OCR 文本；
- 防丢护栏：每个参与表格的 OCR 行文本必须仍能在 Markdown 输出中定位（与引用
  定位 `_quote_still_locatable` 同一精神），不满足即整体回退；
- SLANet 为单表模型：页内文本行落入单一表格区域时才结构化，其余行按原顺序
  保留在表格前后；多表/复杂版式匹配不全时被护栏拦截回退；
- 惰性单例：模型 7.4MB，worker 进程内首次使用才加载；构造失败在进程内记住，
  Worker 重启（管线现有机制）后自然重试；
- 模型路径可用 LAWBUDDY_TABLE_MODEL_PATH 指定本地 onnx（离线部署），缺省用
  rapid_table 内置下载（modelscope，首次联网下载后离线可用）；
- LAWBUDDY_OCR_TABLE=0 可整体关闭（诊断用）。
"""

from __future__ import annotations

import os
import threading
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# 候选页门槛：文本行不足 6 行不值得跑结构还原（纯文本页零开销）
_MIN_LINES = 6
# 视觉行聚类阈值（行高中位数比例）：同一视觉行的 y 中心差小于该值
_ROW_CLUSTER_RATIO = 0.6
# 视觉行内 ≥2 个文本块的行数达到该值才判定为表格候选
_MIN_MULTI_COLUMN_ROWS = 3

_engine: Any = None
_engine_lock = threading.Lock()
_init_failed = False


def table_restore_enabled() -> bool:
    """开关：LAWBUDDY_OCR_TABLE=0 关闭；rapid_table 未安装时视为不可用。"""
    if os.environ.get("LAWBUDDY_OCR_TABLE", "").strip() == "0":
        return False
    try:
        import rapid_table  # noqa: F401

        return True
    except Exception:  # noqa: BLE001  依赖缺失（含模型下载库）→ 不可用
        return False


def _get_engine() -> Any:
    """RapidTable 惰性单例；不可用返回 None（进程内记住失败，Worker 重启后重试）。"""
    global _engine, _init_failed
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _init_failed:
            return None
        try:
            from rapid_table import RapidTable, RapidTableInput
            from rapid_table.utils.typings import ModelType

            model_path = os.environ.get("LAWBUDDY_TABLE_MODEL_PATH", "").strip()
            # use_ocr=True 才会执行 cell 匹配产出 pred_htmls；配合传入
            # ocr_results 时复用管线已有识别结果，不触发内部 OCR 引擎
            kwargs: dict[str, Any] = {"model_type": ModelType.SLANETPLUS}
            if model_path:
                kwargs["model_dir_or_path"] = model_path
            _engine = RapidTable(RapidTableInput(**kwargs))
            return _engine
        except Exception:  # noqa: BLE001  构造失败（依赖/模型缺失）→ 永久回退
            _init_failed = True
            return None


# ---------------------------------------------------------------------------
# 表格候选页启发式（纯内存，纯文本页零结构化开销）
# ---------------------------------------------------------------------------

def looks_like_table_page(lines: list[dict[str, Any]]) -> bool:
    """OCR 行 bbox 呈"多列对齐"分布才进入结构还原。

    视觉行聚类（y 中心差 < 行高中位数的 0.6 倍）后，含 ≥2 个文本块的视觉行
    数量达标即判定为候选。误判代价可控：SLANet 还原失败或内容丢失会被护栏
    拦截，回退纯 OCR 文本。
    """
    items: list[tuple[float, float]] = []  # (y 中心, 行高)
    for ln in lines:
        bbox = ln.get("bbox") or []
        if len(bbox) >= 4:
            items.append(((bbox[1] + bbox[3]) / 2, max(bbox[3] - bbox[1], 0.005)))
    if len(items) < _MIN_LINES:
        return False
    items.sort()
    h_med = sorted(h for _, h in items)[len(items) // 2]
    threshold = _ROW_CLUSTER_RATIO * h_med
    rows: list[list[tuple[float, float]]] = []
    for yc, h in items:
        if rows and yc - rows[-1][-1][0] <= threshold:
            rows[-1].append((yc, h))
        else:
            rows.append([(yc, h)])
    multi = sum(1 for row in rows if len(row) >= 2)
    return multi >= _MIN_MULTI_COLUMN_ROWS


# ---------------------------------------------------------------------------
# HTML → Markdown（html.parser 小转换器，不引入额外依赖）
# ---------------------------------------------------------------------------

class _TableHTMLParser(HTMLParser):
    """提取 <table> 内 rows：tr → 行，td/th → 单元格（单元格内标签忽略取文本）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _parse_html_table(html: str) -> list[list[str]]:
    parser = _TableHTMLParser()
    parser.feed(html)
    return parser.rows


def _rows_to_markdown(rows: list[list[str]]) -> str | None:
    """行数组 → Markdown 管道表格（首行作表头，竖线转义，列数按最大宽度补空）。"""
    cells = [[c.replace("|", "\\|") for c in row] for row in rows]
    width = max(len(r) for r in cells)
    if width < 2:  # 单列不是表格
        return None
    cells = [r + [""] * (width - len(r)) for r in cells]
    lines = ["| " + " | ".join(cells[0]) + " |",
             "| " + " | ".join("---" for _ in cells[0]) + " |"]
    for row in cells[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _norm(text: str) -> str:
    """定位比对归一化：去空白与表格分隔符，只留实义字符。"""
    return "".join(ch for ch in text if not ch.isspace() and ch != "|")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def restore_table(image_path: Path, lines: list[dict[str, Any]]) -> str | None:
    """表格候选页结构还原。返回融合后的新页文本；不适用/失败返回 None（回退原文本）。

    只改写页文本（Agent 阅读视图）；text_blocks 仍存原始 OCR 行——引用定位
    用的行级 bbox 证据不受结构还原影响。
    """
    try:
        if not lines or not table_restore_enabled():
            return None
        if not looks_like_table_page(lines):
            return None
        engine = _get_engine()
        if engine is None:
            return None

        from PIL import Image

        with Image.open(image_path) as im:
            width, height = im.size

        usable = [ln for ln in lines if ln.get("bbox") and len(ln["bbox"]) >= 4 and ln.get("text")]
        if not usable:
            return None
        boxes = [
            [[round(ln["bbox"][0] * width), round(ln["bbox"][1] * height)],
             [round(ln["bbox"][2] * width), round(ln["bbox"][3] * height)]]
            for ln in usable
        ]
        texts = tuple(ln["text"] for ln in usable)
        scores = tuple(float(ln.get("confidence") or 0.0) for ln in usable)

        out = engine(str(image_path), ocr_results=[(boxes, texts, scores)])
        html = (out.pred_htmls or [""])[0] if hasattr(out, "pred_htmls") else ""
        rows = _parse_html_table(html)
        md = _rows_to_markdown(rows) if len(rows) >= 2 else None
        if not md:
            return None

        # 表格区域（SLANet 预测单元格的并集，归一化），未返回 cell_bboxes 时
        # 保守视为整页表格（护栏仍按内容定位拦截内容丢失）
        region = (0.0, 0.0, 1.0, 1.0)
        cell_bboxes = getattr(out, "cell_bboxes", None)
        if cell_bboxes is not None and len(cell_bboxes) > 0:
            import numpy as np

            arr = np.asarray(cell_bboxes[0], dtype=float).reshape(-1, 2)
            region = (
                max(0.0, arr[:, 0].min() / width - 0.01),
                max(0.0, arr[:, 1].min() / height - 0.01),
                min(1.0, arr[:, 0].max() / width + 0.01),
                min(1.0, arr[:, 1].max() / height + 0.01),
            )

        md_text = _norm(md)
        inside: list[dict[str, Any]] = []
        outside: list[tuple[float, dict[str, Any]]] = []
        for ln in usable:
            x1, y1, x2, y2 = ln["bbox"][:4]
            if x1 >= region[0] and x2 <= region[2] and y1 >= region[1] and y2 <= region[3]:
                inside.append(ln)
            else:
                outside.append(((y1 + y2) / 2, ln))
        if not inside:
            return None
        # 防丢护栏：参与表格的每一行文本必须仍可在 Markdown 中定位
        if any(_norm(ln["text"]) not in md_text for ln in inside):
            return None

        # 表外行按 y 序保留（其中文本已被表格覆盖的行——cell 区域略小于文本
        # 范围的边界行——不再重复输出），Markdown 表格插在表格区域之后的
        # 第一个表外行前
        out_lines: list[str] = []
        inserted = False
        for _, ln in sorted(outside, key=lambda t: t[0]):
            if _norm(ln["text"]) in md_text:
                continue  # 已被表格覆盖：cell 区域边界行去重
            yc = (ln["bbox"][1] + ln["bbox"][3]) / 2
            if not inserted and yc > region[3] * 0.5:
                out_lines.append(md)
                inserted = True
            out_lines.append(ln["text"])
        if not inserted:
            out_lines.append(md)
        return "\n".join(out_lines).strip()
    except Exception:  # noqa: BLE001  结构还原永不拖垮 OCR 主流程
        return None
