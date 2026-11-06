"""文档导入与查询管线（REQ-DOC-001/002/007/008/014，DEV-DOC-003/004）。

流程：魔数嗅探 → SHA-256 → 案件内查重 → 原件只读落入内容存储 →
建 document + 原始 version → 按类型提取页文本 → 状态落库。

- 原件按内容哈希全局去重存储（同哈希物理复用），案件内重复导入返回 duplicate；
- blob 以 AES-256-GCM 加密后只读保存（技术方案 §14 第 2 条），解析时经
  `tempstore.materialized` 还原到案件专属临时目录并在用后清理；
- 任何派生版本后续单独生成（REQ-DOC-002 原始文件保护）；
- PDF 优先文本层，空文本页标记 ocr_pending（REQ-DOC-008），OCR 在后续迭代接管。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from . import crypto, tempstore
from .cases import CaseError, readable_case_ids, require_active_case
from .keyring import get_active_dek
from .storage import get_db
from .workspace import BLOB_DIRNAME, now_iso

CHUNK = 1024 * 1024

# OLE2 / CFB 复合文档魔数：加密 OOXML 的容器格式（解密后才是 zip）
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class ImportError_(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 魔数嗅探（DEV-DOC-003：不能只信扩展名）
# ---------------------------------------------------------------------------

KIND_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
    "tiff": "image/tiff",
    "txt": "text/plain",
    "md": "text/markdown",
    "html": "text/html",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "json": "application/json",
    "xml": "application/xml",
    "yaml": "text/yaml",
    "code": "text/plain",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "ogg": "audio/ogg",
    "mp4": "video/mp4",
    "webm": "video/webm",
}

# 文本后缀 → kind（内容已确认可解码为文本后使用）
_TEXT_SUFFIX_KINDS = {
    ".md": "md",
    ".markdown": "md",
    ".html": "html",
    ".htm": "html",
    ".csv": "csv",
    ".tsv": "tsv",
    ".json": "json",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".js": "code",
    ".mjs": "code",
    ".cjs": "code",
    ".ts": "code",
    ".tsx": "code",
    ".jsx": "code",
    ".py": "code",
    ".java": "code",
    ".c": "code",
    ".h": "code",
    ".cpp": "code",
    ".go": "code",
    ".rs": "code",
    ".css": "code",
    ".scss": "code",
    ".sql": "code",
    ".sh": "code",
    ".ps1": "code",
    ".ini": "code",
    ".conf": "code",
    ".log": "txt",
}


def _decode_text_prefix(path: Path, limit: int = 8192) -> str | None:
    """读取文件头并按 UTF-8 → GB18030 顺序解码；二进制内容返回 None。

    - UTF-8 成功即采用（含 BOM）；
    - UTF-8 失败时仅对已知文本后缀尝试 GB18030（国内卷宗常见 GBK/GB18030），
      并对可打印字符占比做校验，避免把二进制误判成文本；
    """
    try:
        raw = path.open("rb").read(limit)
    except OSError:
        return None
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        if path.suffix.lower() not in _TEXT_SUFFIX_KINDS and path.suffix.lower() not in ("", ".txt"):
            return None
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError:
            return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    if len(text) > 0 and printable / len(text) < 0.9:
        return None
    return text


def sniff_kind(path: Path) -> tuple[str, str] | None:
    """返回 (kind, mime)；无法识别返回 None。"""
    try:
        head = path.open("rb").read(16)
    except OSError:
        return None

    if head.startswith(b"%PDF-"):
        return ("pdf", KIND_MIME["pdf"])
    if head.startswith(b"\x89PNG"):
        return ("png", KIND_MIME["png"])
    if head.startswith(b"\xff\xd8\xff"):
        return ("jpg", KIND_MIME["jpg"])
    # TIFF 魔数为 4 字节，须用 startswith 前缀匹配
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return ("tiff", KIND_MIME["tiff"])
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ("gif", KIND_MIME["gif"])
    if head.startswith(b"BM") and path.suffix.lower() == ".bmp":
        return ("bmp", KIND_MIME["bmp"])
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ("webp", KIND_MIME["webp"])
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ("wav", KIND_MIME["wav"])
    if head.startswith(b"OggS"):
        return ("ogg", KIND_MIME["ogg"])
    # MP4 / M4A / MOV 家族：偏移 4 起为 ftyp
    if head[4:8] == b"ftyp":
        ext = path.suffix.lower().lstrip(".")
        return (ext if ext in ("mp4", "m4a", "mov") else "mp4",
                KIND_MIME.get(ext if ext in ("mp4", "m4a") else "mp4", KIND_MIME["mp4"]))
    # WebM / Matroska（EBML 头）：仅 WebM 由浏览器直接播放
    if head.startswith(b"\x1aE\xdf\xa3"):
        return ("webm", KIND_MIME["webm"])
    # MP3：ID3 标签或 MPEG 帧同步
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return ("mp3", KIND_MIME["mp3"])
    if head.startswith(b"PK\x03\x04"):
        # OOXML 均为 zip 容器，按内部目录判别 docx / xlsx / pptx
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
            if any(n.startswith("word/") for n in names):
                return ("docx", KIND_MIME["docx"])
            if any(n.startswith("xl/") for n in names):
                return ("xlsx", KIND_MIME["xlsx"])
            if any(n.startswith("ppt/") for n in names):
                return ("pptx", KIND_MIME["pptx"])
        except zipfile.BadZipFile:
            return None
        return None
    if head[:8] == CFB_MAGIC:
        # 加密 OOXML 为 CFB/OLE2 容器（REQ-DOC-004）：解密前无法判别内部结构，
        # 按扩展名给出初值；旧版二进制 Office 与未知 CFB 不在 P0 范围（REQ-DOC-001）
        ext = path.suffix.lower().lstrip(".")
        if ext in ("docx", "xlsx", "pptx"):
            return (ext, KIND_MIME[ext])
        return None
    # 文本类：先确认可解码（UTF-8 / GB18030），再按后缀区分类型
    text = _decode_text_prefix(path)
    if text is None:
        return None
    suffix = path.suffix.lower()
    if suffix == ".svg" and "<svg" in text.lower():
        return ("svg", KIND_MIME["svg"])
    kind = _TEXT_SUFFIX_KINDS.get(suffix, "txt")
    return (kind, KIND_MIME[kind])


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 原件只读存储
# ---------------------------------------------------------------------------

def _blob_path(root: Path, sha: str) -> Path:
    return root / BLOB_DIRNAME / sha[:2] / sha


def _store_blob(root: Path, src: Path, sha: str) -> str:
    """原件加密写入内容存储；同哈希物理复用，文件设为只读。"""
    dst = _blob_path(root, sha)
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        crypto.encrypt_file(src, tmp, crypto.derive_file_key(get_active_dek()))
        os.replace(tmp, dst)
        os.chmod(dst, 0o444)  # 原件只读（Windows 下同样生效为只读属性）
    return f"{BLOB_DIRNAME}/{sha[:2]}/{sha}"


def _workspace_root() -> Path:
    db = get_db()
    ws = db.query_one("SELECT root_uri FROM workspaces ORDER BY created_at LIMIT 1")
    if ws is None:
        raise ImportError_("workspace not initialized")
    return Path(ws["root_uri"])


# ---------------------------------------------------------------------------
# 页文本提取（DEV-DOC-004：PDF 文本层 / TXT / Markdown / DOCX 基础解析）
# ---------------------------------------------------------------------------

# 文本层质量判定阈值（O-011：隐藏乱码文本层不得当作有效文本送入 Agent）
_GARBLED_MIN_CHARS = 20
_GARBLED_BAD_RATIO = 0.20   # 替换符/控制符/私用区占比上限
_GARBLED_READABLE_RATIO = 0.40  # 字母数字（含 CJK）占比下限

# 解压资源上限（O-012：压缩炸弹与畸形文件必须在上限内终止）
MAX_ZIP_ENTRIES = 5_000
MAX_ZIP_ENTRY_BYTES = 80 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ZIP_RATIO = 200


def _looks_garbled(text: str) -> bool:
    """乱码文本层判定（O-011）：替换符/控制符/私用区过多，或可读字符占比过低。"""
    stripped = (text or "").strip()
    if len(stripped) < _GARBLED_MIN_CHARS:
        return False
    bad = sum(
        1 for ch in stripped
        if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\n\r") or 0xE000 <= ord(ch) <= 0xF8FF
    )
    if bad / len(stripped) >= _GARBLED_BAD_RATIO:
        return True
    readable = sum(1 for ch in stripped if ch.isalnum())
    return readable / len(stripped) < _GARBLED_READABLE_RATIO


def _assert_zip_safe(path: Path) -> None:
    """压缩炸弹守卫（O-012）：按中央目录声明的条目数、单条目大小、总解压量与压缩比拦截。"""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise ImportError_("文件不是有效的 OOXML/ZIP 容器") from exc

    if len(infos) > MAX_ZIP_ENTRIES:
        raise ImportError_(f"压缩包条目过多（{len(infos)}），已按资源上限终止")
    total = 0
    for info in infos:
        if info.file_size > MAX_ZIP_ENTRY_BYTES:
            raise ImportError_(f"压缩包单条目解压后过大（{info.filename}），已按资源上限终止")
        total += info.file_size
        if total > MAX_ZIP_TOTAL_BYTES:
            raise ImportError_("压缩包解压总量超过资源上限，已终止")
        if info.compress_size > 0 and info.file_size / info.compress_size > MAX_ZIP_RATIO:
            raise ImportError_(f"压缩比异常（{info.filename}），疑似压缩炸弹，已终止")


# PDF 表格重建预算（O-015 阶段 2）：超过 300 页的文件只扫描前 100 页，
# 控制大文件导入耗时；超预算页保留 pypdf 纯文本流（表格文字仍在，只是无结构）
_PDF_TABLE_SCAN_PAGES = 100
_PDF_TABLE_SCAN_MAX_FILE_PAGES = 300


def _pdf_tables_to_markdown(tables: list[Any]) -> list[str]:
    """pdfplumber 表格对象 → Markdown 管道块（首行作表头，竖线转义，空行剔除）。"""
    blocks: list[str] = []
    for table in tables:
        rows = [r for r in (table.extract() or []) if any(c and str(c).strip() for c in r)]
        if len(rows) < 2:  # 无数据行（仅表头或空壳）不产出
            continue
        cells = [
            [" ".join(str(c).split()).replace("|", "\\|") if c else "" for c in row]
            for row in rows
        ]
        width = max(len(r) for r in cells)
        cells = [r + [""] * (width - len(r)) for r in cells]
        lines = ["| " + " | ".join(cells[0]) + " |",
                 "| " + " | ".join("---" for _ in cells[0]) + " |"]
        for row in cells[1:]:
            lines.append("| " + " | ".join(row) + " |")
        blocks.append("\n".join(lines))
    return blocks


def _rebuild_pdf_page_text(page: Any, original: str) -> str:
    """单页表格重建：bbox 裁剪表格外正文 + Markdown 表格按 bbox 纵序合并。

    无表格、无可提取内容或裁剪异常时返回原文本，保证不丢任何文字。
    """
    tables = page.find_tables()
    if not tables:
        return original
    blocks = _pdf_tables_to_markdown(tables)
    if not blocks:
        return original  # 表格存在但无可提取行：保留原文本流防丢内容
    outside = page
    for t in tables:
        outside = outside.outside_bbox(t.bbox) if outside else None
    body = ""
    if outside is not None:
        body = (outside.extract_text() or "").strip()
        if body is None:
            body = ""
    parts = ([body] if body else []) + blocks
    return "\n\n".join(parts).strip()


def _rebuild_pdf_tables(
    blob: Path, page_count: int, pages: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """对 native 文本页做线框表格检测与重建（两级探测，无表格页零结构化开销）。

    pdfplumber 整体不可用或单页异常时回退 pypdf 原文本，导入永不因此失败。
    """
    if not any(q == "native" and t for t, q in pages):
        return pages
    scan_limit = (
        _PDF_TABLE_SCAN_PAGES if page_count > _PDF_TABLE_SCAN_MAX_FILE_PAGES else len(pages)
    )
    rebuilt = 0
    try:
        import pdfplumber

        with pdfplumber.open(blob) as pdf:
            for i in range(min(scan_limit, len(pages))):
                text, pq = pages[i]
                if pq != "native" or not text:
                    continue
                try:
                    new_text = _rebuild_pdf_page_text(pdf.pages[i], text)
                except Exception:
                    continue  # 单页异常：该页回退原文本
                if new_text != text:
                    pages[i] = (new_text, pq)
                    rebuilt += 1
    except Exception:
        return pages  # 文件损坏等整体异常：全部保持原文本
    if rebuilt:
        print(f"[parse] pdf table rebuild: {rebuilt} pages -> markdown", file=sys.stderr)
    return pages


def _extract_pdf(blob: Path) -> tuple[list[tuple[str, str]], str]:
    """pypdf 提取文本层；空文本页与**乱码文本层**页均标记 ocr_pending 交给 OCR（O-011）；
    native 页经 pdfplumber 重建线框表格为 Markdown（O-015 阶段 2）。"""
    from pypdf import PdfReader

    pages: list[tuple[str, str]] = []
    reader = PdfReader(str(blob))
    for page in reader.pages:
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if text and _looks_garbled(text):
            # 乱码不回退为「有文本」：清空内容并标记待 OCR，避免乱码进入索引与 Agent
            pages.append(("", "ocr_pending"))
            continue
        pages.append((text, "native" if text else "ocr_pending"))
    pages = _rebuild_pdf_tables(blob, len(reader.pages), pages)
    quality = "partial" if any(q != "native" for _, q in pages) else "ready"
    return pages, quality


def _docx_heading_level(para: Any) -> int | None:
    """标题段落级别（1-6）；依据 style_id 与样式名双重匹配（中英文模板均可命中）。"""
    import re as _re

    try:
        style = para.style
    except Exception:
        return None
    haystack = " ".join(
        str(x).lower() for x in (getattr(style, "style_id", None), getattr(style, "name", None)) if x
    )
    m = _re.search(r"heading\s*([1-6])", haystack)
    return int(m.group(1)) if m else None


def _docx_cell_text(cell: Any) -> str:
    """单元格文本：内嵌换行折叠为空格，嵌套表格平铺追加（只展开一层）。"""
    text = " ".join(cell.text.split())
    for nested in cell.tables:
        nested_text = "；".join(
            " ".join(c.text.split())
            for row in nested.rows
            for c in row.cells
            if c.text.strip()
        )
        if nested_text:
            text = f"{text}：{nested_text}" if text else nested_text
    return text.replace("|", "\\|")


def _docx_table_markdown(table: Any) -> str:
    """表格 → Markdown 管道格式；首行作表头。全空表格返回空串（不产出噪音）。"""
    rows: list[list[str]] = []
    for row in table.rows:
        cells = [_docx_cell_text(c) for c in row.cells]
        if any(c for c in cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |",
             "| " + " | ".join("---" for _ in rows[0]) + " |"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _extract_docx(blob: Path) -> tuple[list[tuple[str, str]], str]:
    """python-docx 按文档体顺序提取段落与表格（O-015：表格内容不丢失）。

    标题样式映射 Markdown `#`-`######` 级别；表格重建为 Markdown 管道表格。
    DOCX 渲染前无物理页码，先以全文单页存储。任何增强解析异常时回退为
    纯段落提取，保证导入永不因此失败。
    """
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(blob))
    try:
        parts: list[str] = []
        for child in doc.element.body.iterchildren():
            if child.tag == qn("w:p"):
                para = Paragraph(child, doc)
                text = para.text.strip()
                if not text:
                    continue
                level = _docx_heading_level(para)
                parts.append(f"{'#' * level} {text}" if level else text)
            elif child.tag == qn("w:tbl"):
                md = _docx_table_markdown(Table(child, doc))
                if md:
                    parts.append(md)
    except Exception:
        parts = [p.text for p in doc.paragraphs if p.text.strip()]  # 兜底：段落-only
    text = "\n\n".join(parts).strip()
    return ([(text, "native")] if text else [("", "empty")]), "ready"


def _read_text_any(blob: Path) -> str:
    """读取文本：UTF-8 → GB18030 → 替换解码（国内卷宗常见 GBK 编码）。"""
    raw = blob.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_plain(blob: Path) -> tuple[list[tuple[str, str]], str]:
    text = _read_text_any(blob).strip()
    return ([(text, "native")] if text else [("", "empty")]), "ready"


# HTML 结构标签：作为换行处理，保留段落边界（供阅读与检索定位）
_HTML_BLOCK_TAGS = {
    "p", "div", "br", "hr", "li", "tr", "td", "th", "table", "section", "article",
    "header", "footer", "main", "aside", "blockquote", "pre", "figure", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
# 永不进入索引与 Agent 的内容（脚本/样式）；HTML 仅作纯文本提取，不执行任何标记
_HTML_SKIP_TAGS = {"script", "style", "noscript", "template", "head"}


class _HtmlTextExtractor(HTMLParser):
    """HTML → 纯文本（O-013：脚本/样式一律丢弃，不做任何执行）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs) -> None:
        if tag in _HTML_SKIP_TAGS:
            self._skip += 1
        elif tag in _HTML_BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _HTML_BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        lines = [ln.strip() for ln in "".join(self._parts).splitlines()]
        return "\n".join(ln for ln in lines if ln)


def _extract_html(blob: Path) -> tuple[list[tuple[str, str]], str]:
    """HTML 提取可读文本用于检索与文本视图；原件预览走沙箱 iframe（不执行脚本）。"""
    raw = blob.read_text(encoding="utf-8", errors="replace")
    parser = _HtmlTextExtractor()
    parser.feed(raw)
    parser.close()
    text = parser.text()
    return ([(text, "native")] if text else [("", "empty")]), "ready"


_XLSX_MAX_COLS = 200


def _xlsx_cell(value: Any) -> str:
    """单元格转 Markdown 表格单元格：竖线转义、内嵌换行折叠为空格。"""
    return " ".join(str(value).split()).replace("|", "\\|") if value is not None else ""


def _extract_xlsx(blob: Path) -> tuple[list[tuple[str, str]], str]:
    """openpyxl 提取工作表单元格，每 sheet 一页并重建为 Markdown 管道表格
    （银行流水/清单类证据可检索、可被 Agent 结构化理解）。

    首个非空行作表头；行内空单元格补位保持列对齐（不再丢弃 None 列）；
    超过 `_XLSX_MAX_COLS` 截断并标注。只读模式不执行公式与宏
    （REQ-DOC-001 宏永不执行）；空表标记 empty。
    内容存储 blob 无扩展名，须经 BytesIO 传入（openpyxl 按内容解析）。
    """
    from io import BytesIO

    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(blob.read_bytes()), read_only=True, data_only=True)
    pages: list[tuple[str, str]] = []
    try:
        for ws in wb.worksheets:
            lines: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = [_xlsx_cell(c) for c in row]
                while cells and not cells[-1]:
                    cells.pop()  # 裁掉行尾空列
                if not any(cells):
                    continue
                if len(cells) > _XLSX_MAX_COLS:
                    truncated = len(cells) - _XLSX_MAX_COLS
                    cells = cells[:_XLSX_MAX_COLS] + [f"…（超宽截断 {truncated} 列）"]
                lines.append("| " + " | ".join(cells) + " |")
                if len(lines) == 1:  # 首个非空行后插入表头分隔行
                    lines.append("| " + " | ".join("---" for _ in cells) + " |")
            pages.append(("\n".join(lines), "native" if lines else "empty"))
    finally:
        wb.close()
    return (pages if pages else [("", "empty")]), "ready"


def _image_frame_count(blob: Path) -> int:
    """图片帧数：PNG/JPG 为 1，TIFF 按 n_frames。"""
    if blob.suffix.lower() in (".png", ".jpg", ".jpeg"):
        return 1
    try:
        from PIL import Image

        with Image.open(blob) as im:
            return int(getattr(im, "n_frames", 1))
    except Exception:
        return 1


_EXTRACTORS = {
    "pdf": _extract_pdf,
    "docx": _extract_docx,
    "xlsx": _extract_xlsx,
    "txt": _extract_plain,
    "md": _extract_plain,
    "html": _extract_html,
    # 文本类数据文件：提取全文供检索，预览仍按原格式渲染
    "csv": _extract_plain,
    "tsv": _extract_plain,
    "json": _extract_plain,
    "xml": _extract_plain,
    "yaml": _extract_plain,
    "code": _extract_plain,
    # 图片（GIF/WebP/BMP/SVG）与音视频：无可检索文本，预览按原格式渲染
}


def _resolve_blob_uri(version_id: str, blob_uri: str) -> str:
    """已解锁的版本读取解锁内容，否则读取原件（REQ-DOC-004）。"""
    row = get_db().query_one(
        "SELECT unlocked_blob_uri FROM document_versions WHERE id = ?", (version_id,)
    )
    return row["unlocked_blob_uri"] if row and row["unlocked_blob_uri"] else blob_uri


def parse_document_version(
    *,
    case_id: str,
    document_id: str,
    version_id: str,
    title: str,
    kind: str,
    blob_uri: str,
    root: Path,
) -> tuple[int, str]:
    """解析指定版本，落库页文本与全文索引，返回 (页数, 质量)。

    加密文档须先经 `decrypt.unlock_document` 解锁；解析统一读取
    `COALESCE(unlocked_blob_uri, blob_uri)` 指向的内容。幂等：先清空该版本已有页与索引。
    """
    from .search import index_page

    readable_uri = _resolve_blob_uri(version_id, blob_uri)
    pages: list[tuple[str, str]] = []
    quality = "ready"

    extractor = _EXTRACTORS.get(kind)
    if extractor is not None:
        # 解密到案件专属临时目录解析，用后即清理（REQ-DOC-012）
        with tempstore.materialized(root, readable_uri, case_id, kind) as readable:
            if kind in ("docx", "xlsx", "pptx"):
                _assert_zip_safe(readable)  # O-012：压缩炸弹在上限内终止
            pages, quality = extractor(readable)
    elif kind in ("png", "jpg", "tiff"):
        # 图片：建 OCR 待处理页（png/jpg 单页；tiff 按帧数），OCR 管线接管
        with tempstore.materialized(root, readable_uri, case_id, kind) as readable:
            frame_count = _image_frame_count(readable)
        pages = [("", "ocr_pending") for _ in range(frame_count)]
        quality = "partial"
    # 其余类型（PPTX）：仅元数据导入，本评测集无 pptx

    # 两档清洗（O-016）：第一档无损 + 第二档页眉页脚/页码行；统计只进 stderr 日志
    from .textclean import clean_document_pages

    pages, stats = clean_document_pages(pages)
    if any(stats.values()):
        print(
            f"[parse] document {document_id}: cleaned chars={stats['chars']} "
            f"pageNumbers={stats['pageNumbers']} headerFooterLines={stats['headerFooterLines']}",
            file=sys.stderr,
        )

    db = get_db()
    with db.transaction() as conn:
        conn.execute("DELETE FROM document_pages WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM pages_fts WHERE version_id = ?", (version_id,))
        for idx, (text, pq) in enumerate(pages, start=1):
            conn.execute(
                """
                INSERT INTO document_pages(id, version_id, page_no, text_content, text_quality, ocr_status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), version_id, idx, text, pq,
                 "pending" if pq == "ocr_pending" else "none"),
            )
            # 同事务同步全文索引（DEV-DOC-008：导入即可检索）
            index_page(
                conn,
                text=text,
                case_id=case_id,
                document_id=document_id,
                document_title=title,
                version_id=version_id,
                page_no=idx,
                text_quality=pq,
            )
        conn.execute(
            "UPDATE document_versions SET parse_status = ?, page_count = ?, error_text = NULL WHERE id = ?",
            (quality, len(pages), version_id),
        )
        conn.execute(
            "UPDATE documents SET status = ?, updated_at = ? WHERE id = ?",
            ("ready" if quality == "ready" else "partial", now_iso(), document_id),
        )
    return len(pages), quality


# ---------------------------------------------------------------------------
# 导入主流程
# ---------------------------------------------------------------------------

def import_documents(case_id: str, paths: list[str]) -> list[dict[str, Any]]:
    """批量导入文件到案件。返回每个文件的导入结果（成功/重复/失败）。"""
    from . import audit

    try:
        require_active_case(case_id)
    except CaseError as exc:
        # 拒绝也要留痕（REQ-AUD-001：授权、外发和删除记录只能追加）
        audit.log_standalone(
            action="document.import",
            resource_type="case",
            resource_id=case_id,
            case_id=case_id,
            result="denied",
            metadata={"fileCount": len(paths), "reason": str(exc)},
        )
        raise

    root = _workspace_root()
    db = get_db()
    results: list[dict[str, Any]] = []

    for raw in paths:
        src = Path(raw)
        title = src.stem or src.name
        try:
            if not src.is_file():
                raise ImportError_("文件不存在或不可读")

            sniffed = sniff_kind(src)
            if sniffed is None:
                results.append({
                    "name": src.name, "status": "failed",
                    "reason": "不支持的文件类型（支持 PDF/Office 文档/Markdown/HTML/文本/CSV/JSON/图片/音视频）",
                })
                continue
            kind, mime = sniffed

            sha = sha256_file(src)
            size = src.stat().st_size

            # 案件内查重（REQ-DOC-007：同一文件不静默生成多个原件）
            dup = db.query_one(
                """
                SELECT d.id AS document_id, d.title FROM documents d
                JOIN document_versions v ON v.id = d.active_version_id
                WHERE d.case_id = ? AND v.sha256 = ?
                """,
                (case_id, sha),
            )
            if dup is not None:
                results.append({
                    "name": src.name, "status": "duplicate",
                    "documentId": dup["document_id"], "title": dup["title"],
                    "reason": "内容相同的文件已在本案件中",
                })
                continue

            blob_uri = _store_blob(root, src, sha)

            doc_id = str(uuid.uuid4())
            version_id = str(uuid.uuid4())
            ts = now_iso()

            # 加密检测（REQ-DOC-004）：命中则不进入解析，等待用户经专用窗口提供密码
            from .decrypt import detect_encryption

            encrypted = detect_encryption(src, kind)

            with db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO documents(id, case_id, kind, title, mime, status, active_version_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (doc_id, case_id, kind, title, mime,
                     "need_password" if encrypted else "parsing", version_id, ts, ts),
                )
                conn.execute(
                    """
                    INSERT INTO document_versions(id, document_id, blob_uri, sha256, size_bytes,
                                                  parse_status, origin, created_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', 'original', ?)
                    """,
                    (version_id, doc_id, blob_uri, sha, size, ts),
                )
                # 审计只记录元数据摘要：文件名、类型、哈希前缀、大小、加密状态（不写正文）
                audit.append(
                    conn,
                    action="document.import",
                    resource_type="document",
                    resource_id=doc_id,
                    case_id=case_id,
                    result="ok",
                    metadata={"fileName": src.name, "kind": kind, "sha256": sha[:12],
                              "sizeBytes": size, "encrypted": encrypted,
                              "status": "need_password" if encrypted else "parsing"},
                )

            if encrypted:
                results.append({
                    "name": src.name, "status": "need_password",
                    "documentId": doc_id, "title": title, "kind": kind,
                    "reason": "文件已加密，请输入密码后使用",
                })
                continue

            # 提取页文本
            try:
                page_count, quality = parse_document_version(
                    case_id=case_id, document_id=doc_id, version_id=version_id,
                    title=title, kind=kind, blob_uri=blob_uri, root=root,
                )
            except ImportError_ as exc:
                # 域内拒绝（类型不支持 / 资源上限 / 压缩炸弹，O-009/O-012）：
                # 原因原样回传，供用户判断（不提示"稍后重试"）
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE document_versions SET parse_status = 'failed', error_text = ? WHERE id = ?",
                        (str(exc), version_id),
                    )
                    conn.execute(
                        "UPDATE documents SET status = 'failed', updated_at = ? WHERE id = ?",
                        (now_iso(), doc_id),
                    )
                    audit.append(
                        conn,
                        action="document.import",
                        resource_type="document",
                        resource_id=doc_id,
                        case_id=case_id,
                        result="denied",
                        metadata={"fileName": src.name, "kind": kind, "sha256": sha[:12],
                                  "reason": str(exc)},
                    )
                results.append({"name": src.name, "status": "failed", "documentId": doc_id,
                                "reason": str(exc)})
                continue
            except Exception as exc:  # 解析失败保留原件，标记 failed（可重试）
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE document_versions SET parse_status = 'failed', error_text = ? WHERE id = ?",
                        (f"{type(exc).__name__}: {exc}", version_id),
                    )
                    conn.execute(
                        "UPDATE documents SET status = 'failed', updated_at = ? WHERE id = ?",
                        (now_iso(), doc_id),
                    )
                    audit.append(
                        conn,
                        action="document.import",
                        resource_type="document",
                        resource_id=doc_id,
                        case_id=case_id,
                        result="failed",
                        metadata={"fileName": src.name, "kind": kind, "sha256": sha[:12],
                                  "reason": f"parse failed: {type(exc).__name__}"},
                    )
                results.append({"name": src.name, "status": "failed", "documentId": doc_id,
                                "reason": "文件解析失败，原件已保存，可稍后重试"})
                continue

            results.append({
                "name": src.name, "status": "imported",
                "documentId": doc_id, "title": title, "kind": kind,
                "pageCount": page_count,
            })
        except (ImportError_, CaseError) as exc:
            results.append({"name": src.name, "status": "failed", "reason": str(exc)})

    return results


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def _row_to_doc(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "caseId": row["case_id"],
        "kind": row["kind"],
        "title": row["title"],
        "mime": row["mime"],
        "status": row["status"],
        "activeVersionId": row["active_version_id"],
        "pageCount": row["page_count"],
        "ocrPendingPages": row.get("ocr_pending_pages", 0),
        "isRedacted": bool(row["is_redacted"]) if "is_redacted" in row.keys() else False,
        "sha256": row["sha256"],
        "sizeBytes": row["size_bytes"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def list_documents(case_id: str) -> list[dict[str, Any]]:
    return _list_documents([case_id])


def list_task_documents(case_id: str) -> list[dict[str, Any]]:
    """任务可读材料：本任务文档 + 关联案件只读共享的文档（文书生成）。"""
    return _list_documents(readable_case_ids(case_id))


def _list_documents(case_ids: list[str]) -> list[dict[str, Any]]:
    ids = [str(case_id) for case_id in case_ids if case_id]
    if not ids:
        return []
    db = get_db()
    placeholders = ",".join("?" for _ in ids)
    rows = db.query(
        f"""
        SELECT d.*, v.page_count, v.sha256, v.size_bytes,
               COALESCE(ocr.n, 0) AS ocr_pending_pages
        FROM documents d
        LEFT JOIN document_versions v ON v.id = d.active_version_id
        LEFT JOIN (
            SELECT v2.document_id, COUNT(*) AS n
            FROM document_pages p
            JOIN document_versions v2 ON v2.id = p.version_id
            WHERE p.ocr_status IN ('pending', 'running')
            GROUP BY v2.document_id
        ) ocr ON ocr.document_id = d.id
        WHERE d.case_id IN ({placeholders})
        ORDER BY d.created_at ASC
        """,
        tuple(ids),
    )
    return [_row_to_doc(r) for r in rows]


def get_pages(document_id: str) -> list[dict[str, Any]]:
    db = get_db()
    doc = db.query_one(
        "SELECT d.active_version_id FROM documents d WHERE d.id = ?", (document_id,)
    )
    if doc is None or doc["active_version_id"] is None:
        return []
    rows = db.query(
        """
        SELECT p.page_no, p.printed_page, p.text_content, p.text_quality, p.ocr_confidence,
               (SELECT COUNT(*) FROM ocr_corrections c WHERE c.page_id = p.id) AS corrections
        FROM document_pages p WHERE p.version_id = ? ORDER BY p.page_no ASC
        """,
        (doc["active_version_id"],),
    )
    return [
            {
                "pageNo": r["page_no"],
                "printedPage": r["printed_page"],
                "text": r["text_content"] or "",
                "quality": r["text_quality"],
                "ocrConfidence": r["ocr_confidence"],
                # O-008：是否经过人工更正（更正历史在 ocr_corrections，原值保留）
                "corrected": int(r["corrections"] or 0) > 0,
                "correctionCount": int(r["corrections"] or 0),
            }
            for r in rows
        ]


def get_page_text_blocks(document_id: str, page_no: int) -> list[dict[str, Any]]:
    """当前版本某一页的 OCR 文本行（含页面归一化 bbox）。

    扫描件 PDF / 图片没有文本层，只能靠 OCR 行坐标定位原文片段；
    bbox 为 [x1, y1, x2, y2]，相对页面宽高的比例（技术方案 §9.1）。
    原生文本页没有 text_blocks 时返回空数组。
    """
    db = get_db()
    doc = db.query_one(
        "SELECT active_version_id FROM documents WHERE id = ?", (document_id,)
    )
    if doc is None or doc["active_version_id"] is None:
        return []
    page = db.query_one(
        "SELECT id FROM document_pages WHERE version_id = ? AND page_no = ?",
        (doc["active_version_id"], page_no),
    )
    if page is None:
        return []
    rows = db.query(
        """
        SELECT block_order, type, bbox_json, text_enc, confidence
        FROM text_blocks WHERE page_id = ? ORDER BY block_order ASC
        """,
        (page["id"],),
    )
    blocks: list[dict[str, Any]] = []
    for row in rows:
        try:
            bbox = json.loads(row["bbox_json"]) if row["bbox_json"] else None
        except (TypeError, ValueError):
            bbox = None
        blocks.append({
            "order": row["block_order"],
            "type": row["type"],
            "bbox": bbox,
            "text": row["text_enc"] or "",
            "confidence": row["confidence"],
        })
    return blocks


def redact_document(document_id: str, task_id: str | None = None) -> dict[str, Any]:
    """对文档解析文本原地脱敏（一键脱敏 / 文档列表「脱敏处理」按钮）。

    - 原始卷宗二进制不动（blob 只读，AGENTS.md 硬约束）：星号打码直接写入
      已解析页文本（document_pages），原件预览仍按原始文件渲染；
    - 打码后页文本同步 FTS 全文索引 —— Agent 阅卷 / 检索 / 文本视图统一
      使用打码后文本，敏感信息不进模型；
    - 幂等：is_redacted=1 时直接返回，不重复打码；
    - 仅 ready/partial 且有已解析页面的文档可脱敏；逐页经 redaction.mask_text
      部分打码（王志远→王*远、身份证保留前 6 末 4，只遮关键段，
      保留可读上下文便于阅卷分析；不改页数）；
    - 打码使页内引语可能无法再定位：相关 Claim 转 stale（Q-009，不静默错位）；
    - 审计记录元数据（类型计数不含被脱敏原文）。
    """
    from . import audit
    from .redaction import mask_text
    from .search import index_page

    db = get_db()
    doc = db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
    if doc is None or doc["active_version_id"] is None:
        raise ImportError_("文档不存在或尚未解析")
    if doc["status"] not in ("ready", "partial"):
        raise ImportError_("文档尚未完成解析，暂不能脱敏")
    if bool(doc.get("is_redacted", 0)):
        return {
            "documentId": document_id, "title": doc["title"],
            "pageCount": 0, "maskedCount": 0, "existing": True,
        }
    case_id = doc["case_id"]
    version_id = doc["active_version_id"]
    pages = get_pages(document_id)
    if not pages:
        raise ImportError_("文档无已解析页面，暂不能脱敏")

    masked_total = 0
    with db.transaction() as conn:
        for p in pages:
            res = mask_text(case_id, str(p.get("text") or ""))
            masked_total += int(res["total"])
            page_no = int(p["pageNo"])
            conn.execute(
                "UPDATE document_pages SET text_content = ? WHERE version_id = ? AND page_no = ?",
                (res["maskedText"], version_id, page_no),
            )
            # 全文索引同步（删旧插新，与 OCR 更正同模式）
            row = conn.execute(
                "SELECT rowid FROM pages_fts WHERE document_id = ? AND page_no = ?",
                (document_id, page_no),
            ).fetchone()
            if row is not None:
                conn.execute("DELETE FROM pages_fts WHERE rowid = ?", (row["rowid"],))
            index_page(
                conn,
                text=res["maskedText"],
                case_id=case_id,
                document_id=document_id,
                document_title=doc["title"],
                version_id=version_id,
                page_no=page_no,
                text_quality=p.get("quality") or "native",
            )
        conn.execute(
            "UPDATE documents SET is_redacted = 1, updated_at = ? WHERE id = ?",
            (now_iso(), document_id),
        )
        audit.append(
            conn,
            action="document.redact_inplace",
            resource_type="document",
            resource_id=document_id,
            case_id=case_id,
            result="ok",
            metadata={"pageCount": len(pages), "maskedCount": masked_total, "taskId": task_id},
        )
    # 事务提交后再校验引用（打码后页内引语可能无法定位）
    stale_total = 0
    for p in pages:
        stale_total += _invalidate_page_references(document_id, int(p["pageNo"]))
    return {
        "documentId": document_id, "title": doc["title"],
        "pageCount": len(pages), "maskedCount": masked_total,
        "existing": False, "staleClaims": stale_total,
    }


def list_page_corrections(document_id: str, page_no: int) -> list[dict[str, Any]]:
    """某一页的更正历史（OCR 原值与更正值并存，追加式，供阅读器与审计追溯）。"""
    db = get_db()
    doc = db.query_one("SELECT active_version_id FROM documents WHERE id = ?", (document_id,))
    if doc is None or doc["active_version_id"] is None:
        raise ImportError_("文档不存在")
    rows = db.query(
        """
        SELECT c.id, c.original_text, c.corrected_text, c.original_confidence, c.note, c.created_at
        FROM ocr_corrections c
        JOIN document_pages p ON p.id = c.page_id
        WHERE c.document_id = ? AND p.version_id = ? AND p.page_no = ?
        ORDER BY c.created_at DESC, c.rowid DESC
        """,
        (document_id, doc["active_version_id"], page_no),
    )
    return [
        {"id": r["id"], "originalText": r["original_text"], "correctedText": r["corrected_text"],
         "originalConfidence": r["original_confidence"], "note": r["note"], "createdAt": r["created_at"]}
        for r in rows
    ]


def correct_page_text(document_id: str, page_no: int, corrected_text: str, note: str = "") -> dict[str, Any]:
    """OCR 人工更正（DEV-DOC-010 / O-008 / Q-009）。

    - 原件与页图不变（只读），OCR 原值与更正值同时保留在 `ocr_corrections`（追加式）；
    - 页文本与全文索引同步为更正值，Agent/检索使用更正后的文本；
    - 该页上不再能定位到原文的引用 → 相关 Claim 转 stale（reason=OCR_CORRECTED），
      不静默错位（Q-009）；用户可在待核实队列重新校验或人工确认。
    """
    from . import audit

    db = get_db()
    corrected_text = (corrected_text or "").strip()
    if not corrected_text:
        raise ImportError_("更正文本不能为空")
    doc = db.query_one(
        "SELECT d.case_id, d.title, v.id AS version_id FROM documents d "
        "JOIN document_versions v ON v.id = d.active_version_id WHERE d.id = ?",
        (document_id,),
    )
    if doc is None:
        raise ImportError_("文档不存在")
    page = db.query_one(
        "SELECT id, page_no, text_content, ocr_confidence FROM document_pages "
        "WHERE version_id = ? AND page_no = ?",
        (doc["version_id"], page_no),
    )
    if page is None:
        raise ImportError_(f"第 {page_no} 页不存在")

    original_text = page["text_content"] or ""
    from .search import index_page

    correction_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ocr_corrections(id, page_id, document_id, version_id, case_id,
                                        original_text, corrected_text, original_confidence, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (correction_id, page["id"], document_id, doc["version_id"], doc["case_id"],
             original_text, corrected_text, page["ocr_confidence"], note or None, now_iso()),
        )
        conn.execute(
            "UPDATE document_pages SET text_content = ? WHERE id = ?", (corrected_text, page["id"])
        )
        # 全文索引同步（删旧插新）
        row = conn.execute(
            "SELECT rowid FROM pages_fts WHERE document_id = ? AND page_no = ?", (document_id, page_no)
        ).fetchone()
        if row is not None:
            conn.execute("DELETE FROM pages_fts WHERE rowid = ?", (row["rowid"],))
        if corrected_text:
            index_page(
                conn, text=corrected_text, case_id=doc["case_id"], document_id=document_id,
                document_title=doc["title"], version_id=doc["version_id"], page_no=page_no,
                text_quality="ocr",
            )
        audit.append(
            conn, action="document.ocr_correct", resource_type="document_page",
            resource_id=page["id"], case_id=doc["case_id"],
            metadata={"documentId": document_id, "pageNo": page_no,
                      "originalLen": len(original_text), "correctedLen": len(corrected_text),
                      "hasNote": bool(note)},
        )
    stale_claims = _invalidate_page_references(document_id, page_no)
    return {"documentId": document_id, "pageNo": page_no, "correctionId": correction_id,
            "staleClaims": stale_claims}


def _invalidate_page_references(document_id: str, page_no: int) -> int:
    """更正后重校验该页引用：引语不再可定位的 Claim 转 stale（Q-009，不静默错位）。"""
    from .citations import verify_quote

    db = get_db()
    refs = db.query(
        "SELECT DISTINCT cs.claim_id AS claim_id, sr.document_version_id AS version_id, "
        "sr.page_no AS page_no, sr.quote_norm AS quote_norm "
        "FROM source_refs sr JOIN claim_sources cs ON cs.source_ref_id = sr.id "
        "WHERE sr.document_id = ? AND sr.page_no = ?",
        (document_id, page_no),
    )
    stale = 0
    for ref in refs:
        if ref["quote_norm"] and verify_quote(ref["quote_norm"], ref["version_id"], ref["page_no"]):
            continue  # 更正后仍可定位 → 引用有效
        with db.transaction() as conn:
            changed = conn.execute(
                "UPDATE claims SET status = 'stale', reason_code = 'OCR_CORRECTED' "
                "WHERE id = ? AND status IN ('supported','needs_human','conflict','stale')",
                (ref["claim_id"],),
            ).rowcount
            if changed:
                stale += 1
                from . import audit

                audit.append(
                    conn, action="claim.stale", resource_type="claim", resource_id=ref["claim_id"],
                    metadata={"reason": "OCR_CORRECTED", "documentId": document_id, "pageNo": page_no},
                )
    return stale


# ---------------------------------------------------------------------------
# 页批注（DEV-DOC-010）：派生数据，原件与页图不变；quote 锚定语义与引用一致
# （Q-010 规范化），页文本变化后不可定位时读取端标记 quote_missing（Q-009 不静默错位）
# ---------------------------------------------------------------------------

def _annotation_to_row(r: dict[str, Any], quote_missing: bool) -> dict[str, Any]:
    return {
        "id": r["id"],
        "documentId": r["document_id"],
        "versionId": r["version_id"],
        "pageNo": r["page_no"],
        "quote": r["quote"],
        "quoteMissing": quote_missing,
        "body": r["body"],
        "color": r["color"],
        "status": r["status"],
        "resolvedAt": r["resolved_at"],
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def _norm_annotation_color(color: str | None) -> str | None:
    """荧光标注色只接受红/黄/蓝；其余（含历史 NULL）一律存 NULL。"""
    return color if color in ("red", "yellow", "blue") else None


def _quote_still_locatable(quote: str | None, version_id: str, page_no: int) -> bool:
    """批注 quote 是否仍能在当前页文本定位（规范化语义与引用校验一致）。"""
    if not (quote or "").strip():
        return True  # 未锚定原文的批注不参与定位判定
    from .citations import normalize_text, verify_quote

    return verify_quote(normalize_text(quote), version_id, page_no)


def _get_annotation(annotation_id: str) -> dict[str, Any] | None:
    db = get_db()
    r = db.query_one(
        "SELECT id, document_id, version_id, page_no, quote, body, color, status, resolved_at, "
        "created_at, updated_at FROM document_annotations WHERE id = ?",
        (annotation_id,),
    )
    if r is None:
        return None
    return _annotation_to_row(r, not _quote_still_locatable(r["quote"], r["version_id"], r["page_no"]))


def list_annotations(document_id: str) -> list[dict[str, Any]]:
    """文档全部批注（阅读视图展示；quote_missing 读取时计算，不落库）。"""
    db = get_db()
    doc = db.query_one("SELECT active_version_id FROM documents WHERE id = ?", (document_id,))
    if doc is None or doc["active_version_id"] is None:
        raise ImportError_("文档不存在")
    rows = db.query(
        "SELECT id, document_id, version_id, page_no, quote, body, color, status, resolved_at, "
        "created_at, updated_at FROM document_annotations "
        "WHERE document_id = ? AND version_id = ? ORDER BY page_no, created_at",
        (document_id, doc["active_version_id"]),
    )
    return [
        _annotation_to_row(r, not _quote_still_locatable(r["quote"], r["version_id"], r["page_no"]))
        for r in rows
    ]


def add_annotation(document_id: str, page_no: int, body: str, quote: str = "", color: str = "") -> dict[str, Any]:
    """新增页批注：派生数据，原件与页图不变；审计只记长度与标记，不落正文。"""
    from . import audit

    db = get_db()
    body = (body or "").strip()
    if not body:
        raise ImportError_("批注内容不能为空")
    quote = (quote or "").strip() or None
    color_val = _norm_annotation_color((color or "").strip() or None)
    doc = db.query_one(
        "SELECT d.case_id, v.id AS version_id FROM documents d "
        "JOIN document_versions v ON v.id = d.active_version_id WHERE d.id = ?",
        (document_id,),
    )
    if doc is None:
        raise ImportError_("文档不存在")
    require_active_case(doc["case_id"])
    page = db.query_one(
        "SELECT id FROM document_pages WHERE version_id = ? AND page_no = ?",
        (doc["version_id"], page_no),
    )
    if page is None:
        raise ImportError_(f"第 {page_no} 页不存在")

    ann_id = str(uuid.uuid4())
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO document_annotations(id, page_id, document_id, version_id, case_id, "
            "page_no, quote, body, color, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (ann_id, page["id"], document_id, doc["version_id"], doc["case_id"],
             page_no, quote, body, color_val, ts, ts),
        )
        audit.append(
            conn, action="document.annotate_add", resource_type="document_annotation",
            resource_id=ann_id, case_id=doc["case_id"],
            metadata={"documentId": document_id, "pageNo": page_no,
                      "bodyLen": len(body), "hasQuote": quote is not None},
        )
    return _get_annotation(ann_id)


def update_annotation(annotation_id: str, body: str, color: str = "") -> dict[str, Any]:
    """编辑批注内容与荧光色：body/color 与 updated_at 更新，锚定与状态不变；审计记前后长度。"""
    from . import audit

    db = get_db()
    body = (body or "").strip()
    if not body:
        raise ImportError_("批注内容不能为空")
    row = db.query_one(
        "SELECT document_id, page_no, case_id, body FROM document_annotations WHERE id = ?",
        (annotation_id,),
    )
    if row is None:
        raise ImportError_("批注不存在")
    require_active_case(row["case_id"])
    with db.transaction() as conn:
        if color:
            conn.execute(
                "UPDATE document_annotations SET body = ?, color = ?, updated_at = ? WHERE id = ?",
                (body, _norm_annotation_color((color or "").strip() or None), now_iso(), annotation_id),
            )
        else:
            conn.execute(
                "UPDATE document_annotations SET body = ?, updated_at = ? WHERE id = ?",
                (body, now_iso(), annotation_id),
            )
        audit.append(
            conn, action="document.annotate_update", resource_type="document_annotation",
            resource_id=annotation_id, case_id=row["case_id"],
            metadata={"documentId": row["document_id"], "pageNo": row["page_no"],
                      "oldLen": len(row["body"]), "newLen": len(body)},
        )
    return _get_annotation(annotation_id)


def set_annotation_resolved(annotation_id: str, resolved: bool) -> dict[str, Any]:
    """批注状态流转 open ↔ resolved（核实流转语义 Q-007：记录时间与审计）。"""
    from . import audit

    db = get_db()
    row = db.query_one(
        "SELECT document_id, page_no, case_id, status FROM document_annotations WHERE id = ?",
        (annotation_id,),
    )
    if row is None:
        raise ImportError_("批注不存在")
    require_active_case(row["case_id"])
    new_status = "resolved" if resolved else "open"
    with db.transaction() as conn:
        conn.execute(
            "UPDATE document_annotations SET status = ?, resolved_at = ?, updated_at = ? WHERE id = ?",
            (new_status, now_iso() if resolved else None, now_iso(), annotation_id),
        )
        audit.append(
            conn, action="document.annotate_resolve", resource_type="document_annotation",
            resource_id=annotation_id, case_id=row["case_id"],
            metadata={"documentId": row["document_id"], "pageNo": row["page_no"],
                      "from": row["status"], "to": new_status},
        )
    return _get_annotation(annotation_id)


def delete_annotation(annotation_id: str) -> dict[str, Any]:
    """删除批注（用户派生数据可删）；审计记录长度标记，不落正文。"""
    from . import audit

    db = get_db()
    row = db.query_one(
        "SELECT document_id, page_no, case_id, body, quote FROM document_annotations WHERE id = ?",
        (annotation_id,),
    )
    if row is None:
        raise ImportError_("批注不存在")
    require_active_case(row["case_id"])
    with db.transaction() as conn:
        conn.execute("DELETE FROM document_annotations WHERE id = ?", (annotation_id,))
        audit.append(
            conn, action="document.annotate_delete", resource_type="document_annotation",
            resource_id=annotation_id, case_id=row["case_id"],
            metadata={"documentId": row["document_id"], "pageNo": row["page_no"],
                      "bodyLen": len(row["body"]), "hasQuote": bool(row["quote"])},
        )
    return {"deleted": True, "annotationId": annotation_id}


def delete_document(document_id: str) -> dict[str, Any]:
    """删除单份文档（R-007 补充材料：删除文档 → 受影响 Claim 失效，旧产物保留）。

    - 删除范围：FTS 索引、source_refs、版本树（页/文本块级联）；
    - 保留：引用该文档的 Claim 置为 stale（reason_code=DOCUMENT_DELETED），
      历史任务消息与审计不受影响（R-007：旧产物保留，只重算依赖项）；
    - blob 按内容哈希跨案件共享，不删除物理文件。
    """
    from . import audit

    db = get_db()
    doc = db.query_one("SELECT case_id, title FROM documents WHERE id = ?", (document_id,))
    if doc is None:
        raise ImportError_("文档不存在")
    # 先停止该文档的 OCR 并等待线程真正退出：页/块随文档级联删除，若后台仍
    # 在落库会触发外键失败并把消费者线程打崩，剩余页永远停在 pending
    try:
        from .ocr.pipeline import OcrManager

        OcrManager.instance().stop_and_wait(document_id)
    except Exception:  # noqa: BLE001  # OCR 未启动或已结束：删除流程不受影响
        pass
    with db.transaction() as conn:
        # 受影响 Claim → stale（保留记录，旧消息可追溯失效原因）
        stale = conn.execute(
            """
            UPDATE claims SET status = 'stale', reason_code = 'DOCUMENT_DELETED'
            WHERE id IN (
                SELECT cs.claim_id FROM claim_sources cs
                JOIN source_refs sr ON sr.id = cs.source_ref_id
                WHERE sr.document_id = ?
            )
            """,
            (document_id,),
        ).rowcount
        conn.execute("DELETE FROM pages_fts WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM source_refs WHERE document_id = ?", (document_id,))  # claim_sources 级联
        conn.execute(
            "DELETE FROM document_versions WHERE parent_id IS NOT NULL AND document_id = ?",
            (document_id,),
        )
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        audit.append(
            conn,
            action="document.delete",
            resource_type="document",
            resource_id=document_id,
            case_id=doc["case_id"],
            metadata={"title": doc["title"], "staleClaims": stale},
        )
    # M-003：来源文档删除后，相关记忆立即失效（重新计算前不得召回为有效事实）
    from . import memory

    stale_memories = memory.invalidate_by_document(document_id, reason="DOCUMENT_DELETED")
    return {"deleted": True, "documentId": document_id, "staleClaims": stale, "staleMemories": stale_memories}
