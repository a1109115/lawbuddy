"""解析文本自动清洗（第一档：无损规范化）。

定位：提取之后、落库之前的质量关。原则是**只删噪音，永不改写内容**——
Agent 引用靠原文片段在 text_content 上定位（`_quote_still_locatable`），
任何同义改写/错字更正都属于人工 OCR 修正职责，机器不越权。

第一档规则（对 native 文本页无条件执行，全部 O(n) 字符串操作）：
- 零宽字符 / BOM 删除（复制粘贴与部分 PDF 提取器的常见杂质）；
- 控制字符删除（保留换行；pypdf 偶发输出 \\x0c 换页符等）；
- 全角空格、tab 归一为普通空格（Markdown 表格内不应有 tab）；
- 行尾空白剥离（行首缩进保留，不假设其无语义）；
- 连续空行压缩为 1 个空行（PDF 双栏/分节提取常见）。

OCR 页与手写/水印类噪音不在本模块处理（属人工 OCR 修正与专项管线职责）。
"""

from __future__ import annotations

import math
import re
from collections import Counter

# 零宽与方向控制字符（不含软连字符 \xad——它可能承载断词语义，保守保留）
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
# 控制字符：保留 \n（\x0a）与 \t（\x09，下一行统一归一为空格）
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# ---------------------------------------------------------------------------
# 第二档：保守结构清洗（需跨页统计或模式强约束才执行）
# ---------------------------------------------------------------------------
# 独立页码行："- 3 -" / "— 3 —" / "3" / "第 3 页" / "第 3 页，共 10 页"。
# 模式固定、独立成行才命中，误判率极低；不做罗马数字/英文 Page x of y（误删风险）
_PAGENUM_RE = re.compile(r"^\s*[-—–―]?\s*\d{1,4}\s*[-—–―]?\s*$")
_PAGENUM_CN_RE = re.compile(r"^\s*第\s*\d{1,4}\s*页(\s*[,，]?\s*共\s*\d{1,4}\s*页)?\s*$")
# 页眉/页脚统计门槛：页数 ≥ 3 才统计（样本过少误删风险大）；
# 某行在 ≥ 2/3 页面的首/尾非空行位置逐字重复且行长 < 50 才判定为页眉/页脚
_HEADER_MIN_PAGES = 3
_HEADER_MIN_RATIO = 2 / 3
_HEADER_MAX_LEN = 50


def clean_page_text(text: str) -> tuple[str, int]:
    """页级无损清洗。返回 (清洗后文本, 删除的字符数)。

    幂等：对已清洗文本再清洗结果不变、删除数为 0（空行压缩与 rstrip 天然幂等）。
    """
    if not text:
        return text, 0
    before = len(text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    cleaned: list[str] = []
    for line in text.split("\n"):
        line = line.replace("\u3000", " ").replace("\t", " ").rstrip()
        if not line and cleaned and cleaned[-1] == "":
            continue  # 连续空行压缩为 1 个空行
        cleaned.append(line)
    out = "\n".join(cleaned).strip()
    return out, before - len(out)


def clean_native_pages(pages: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], int]:
    """仅清洗 native 文本页；empty / ocr_pending 页原样透传（质量标记不动）。

    返回 (清洗后页面列表, 删除字符总数)，供导入日志统计（只记数量，不含内容）。
    """
    total = 0
    out: list[tuple[str, str]] = []
    for text, quality in pages:
        if quality == "native" and text:
            text, removed = clean_page_text(text)
            total += removed
        out.append((text, quality))
    return out, total


def _is_pagenum_line(line: str) -> bool:
    return bool(_PAGENUM_RE.match(line) or _PAGENUM_CN_RE.match(line))


def _nonempty(lines: list[str]) -> list[int]:
    return [idx for idx, ln in enumerate(lines) if ln]


def _replace_page(
    pages: list[tuple[str, str]], index: int, new_text: str
) -> None:
    pages[index] = (new_text, "native")


def clean_document_pages(
    pages: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """两档清洗组合入口（导入路径统一调用，仅作用于 native 页）。

    第一档逐页无损清洗 → 独立页码行删除（逐页模式判定）→ 页眉/页脚跨页统计。
    保守护栏：删除导致整页为空时放弃删除（防止把单行页清成空页）；
    删除后重新过第一档（幂等）消除新产生的连续空行。

    返回 (清洗后页面列表, 统计)：chars（删除字符数）、pageNumbers（页码行数）、
    headerFooterLines（页眉/页脚行数）。只记数量，不含任何文本内容。
    """
    pages, chars = clean_native_pages(pages)
    stats = {"chars": chars, "pageNumbers": 0, "headerFooterLines": 0}
    native_idx = [i for i, (t, q) in enumerate(pages) if q == "native" and t]
    if not native_idx:
        return pages, stats

    # 1) 独立页码行：模式固定，逐页独立判定
    for i in native_idx:
        lines = pages[i][0].split("\n")
        kept = [ln for ln in lines if not (ln and _is_pagenum_line(ln))]
        removed = len(lines) - len(kept)
        if not removed:
            continue
        candidate = clean_page_text("\n".join(kept))[0]
        if not candidate:
            continue  # 整页都是页码样式：保守保留原文本，防清空
        stats["pageNumbers"] += removed
        _replace_page(pages, i, candidate)

    # 2) 页眉/页脚：≥3 页时统计首/尾非空行的跨页逐字重复
    if len(native_idx) >= _HEADER_MIN_PAGES:
        first_counter: Counter[str] = Counter()
        last_counter: Counter[str] = Counter()
        page_lines: dict[int, list[str]] = {}
        for i in native_idx:
            lines = pages[i][0].split("\n")
            page_lines[i] = lines
            ne = _nonempty(lines)
            if ne:
                first_counter[lines[ne[0]]] += 1
                last_counter[lines[ne[-1]]] += 1
        threshold = math.ceil(len(native_idx) * _HEADER_MIN_RATIO)
        headers = {
            ln for ln, c in first_counter.items()
            if c >= threshold and 0 < len(ln) < _HEADER_MAX_LEN
        }
        footers = {
            ln for ln, c in last_counter.items()
            if c >= threshold and 0 < len(ln) < _HEADER_MAX_LEN
        }
        if headers or footers:
            for i in native_idx:
                lines = page_lines[i]
                ne = _nonempty(lines)
                if not ne:
                    continue
                drop: set[int] = set()
                if lines[ne[0]] in headers:
                    drop.add(ne[0])
                if ne[-1] != ne[0] and lines[ne[-1]] in footers:
                    drop.add(ne[-1])
                if not drop:
                    continue
                candidate = clean_page_text(
                    "\n".join(ln for idx, ln in enumerate(lines) if idx not in drop)
                )[0]
                if not candidate:
                    continue  # 删除后整页为空：保守保留
                stats["headerFooterLines"] += len(drop)
                _replace_page(pages, i, candidate)
    return pages, stats
