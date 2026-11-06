"""原件内容会话（REQ-DOC-014：按原始格式预览）。

右侧阅读视图要按文档格式原样渲染（PDF / Word / Excel / Markdown / HTML / 图片），
Renderer 需要读到**原始字节**，但不能接触文件路径，也不能绕过只读与加密边界。

- `open_content`：把 `COALESCE(unlocked_blob_uri, blob_uri)` 指向的内容物料化到
  案件专属临时目录（`tempstore`，权限 0700），返回不透明 `contentId`；
  加密文档未解锁时明确拒绝，不返回密文（REQ-DOC-004）；
- `read_content`：按偏移分块返回 base64 字节，避免整份大文件挤进单条 JSON-RPC；
- `close_content` / `close_all`：释放明文副本；空闲超时与 Core 退出都会清理；
- 原件永远只读：会话只读取 blob，不写回、不覆盖（REQ-DOC-002）。
- TIFF 浏览器无法直接解码，按帧转 PNG 后返回页级会话（多帧图片 = 多页）。
"""

from __future__ import annotations

import base64
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import tempstore
from .documents import (
    KIND_MIME,
    ImportError_,
    _resolve_blob_uri,
    _workspace_root,
    sniff_kind,
)
from .storage import get_db

# 单次返回上限：1 MiB 原始字节（base64 后约 1.4 MB），限制单条 RPC 消息体积
MAX_CHUNK_BYTES = 1024 * 1024
# 会话空闲超时与最长存活（到期即删除临时明文）
SESSION_TTL_SEC = 15 * 60.0
SESSION_MAX_SEC = 60 * 60.0


@dataclass
class _Session:
    content_id: str
    document_id: str
    case_id: str
    kind: str
    mime: str
    root: Path
    path: Path
    size: int
    page_no: int | None
    unlocked: bool
    created_at: float
    touched_at: float
    source_path: Path | None = field(default=None)


_sessions: dict[str, _Session] = {}
_lock = threading.Lock()


def _release(session: _Session) -> None:
    """删除物料化产生的临时明文（对历史明文 blob 是空操作，不会动原件）。"""
    try:
        tempstore.release(session.root, session.path)
    except Exception:
        pass


def _prune_locked(now: float) -> None:
    for content_id, session in list(_sessions.items()):
        idle = now - session.touched_at
        alive = now - session.created_at
        if idle > SESSION_TTL_SEC or alive > SESSION_MAX_SEC:
            _sessions.pop(content_id, None)
            _release(session)


def prune() -> int:
    """清理过期会话；返回清理数量。"""
    now = time.time()
    with _lock:
        before = len(_sessions)
        _prune_locked(now)
        return before - len(_sessions)


def close_all() -> int:
    """Core 退出时释放全部明文副本（AC-SEC-007）。"""
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        _release(session)
    return len(sessions)


def _tiff_frame_to_png(src: Path, page_no: int, root: Path, case_id: str) -> Path:
    """把 TIFF 指定帧转 PNG（浏览器无 TIFF 解码能力）；多帧即多页。"""
    from PIL import Image

    with Image.open(src) as im:
        frames = int(getattr(im, "n_frames", 1))
        if page_no < 1 or page_no > frames:
            raise ImportError_(f"第 {page_no} 页不存在（该文件共 {frames} 页）")
        if frames > 1:
            im.seek(page_no - 1)
        frame = im.convert("RGB")
        dst = tempstore.case_temp_dir(root, case_id) / f"{uuid.uuid4().hex}.png"
        frame.save(dst, format="PNG")
    return dst


def open_content(document_id: str, page_no: int | None = None) -> dict[str, Any]:
    """打开原件内容会话；返回不透明 contentId 与元数据。"""
    db = get_db()
    doc = db.query_one(
        """
        SELECT d.id, d.case_id, d.kind, d.mime, d.status, d.title,
               v.id AS version_id, v.blob_uri, v.unlocked_blob_uri, v.size_bytes
        FROM documents d LEFT JOIN document_versions v ON v.id = d.active_version_id
        WHERE d.id = ?
        """,
        (document_id,),
    )
    if doc is None or doc["version_id"] is None:
        raise ImportError_("文档不存在或没有可用版本")
    if doc["status"] == "need_password":
        raise ImportError_("文档已加密，请先输入密码解锁后再预览")

    kind = str(doc["kind"] or "")
    readable_uri = _resolve_blob_uri(doc["version_id"], doc["blob_uri"])
    unlocked = bool(doc["unlocked_blob_uri"])
    mime = doc["mime"] or ""

    root = _workspace_root()
    materialized = tempstore.materialize(root, readable_uri, str(doc["case_id"]), kind)
    source_path = materialized

    session_page: int | None = None
    if kind == "tiff":
        # 页级会话：按帧转 PNG（原件保持只读，转码产物随会话清理）
        page = int(page_no or 1)
        try:
            png = _tiff_frame_to_png(materialized, page, root, str(doc["case_id"]))
        finally:
            tempstore.release(root, materialized)
        materialized = png
        mime = "image/png"
        session_page = page
        source_path = None

    try:
        size = materialized.stat().st_size
    except OSError as exc:  # 物料化后不可读：明确失败，不静默降级
        tempstore.release(root, materialized)
        raise ImportError_("原件内容不可读") from exc

    now = time.time()
    content_id = str(uuid.uuid4())
    session = _Session(
        content_id=content_id,
        document_id=document_id,
        case_id=str(doc["case_id"]),
        kind=kind,
        mime=mime,
        root=root,
        path=materialized,
        size=size,
        page_no=session_page,
        unlocked=unlocked,
        created_at=now,
        touched_at=now,
        source_path=source_path,
    )
    with _lock:
        _prune_locked(now)
        _sessions[content_id] = session

    return {
        "contentId": content_id,
        "documentId": document_id,
        "kind": kind,
        "mime": mime,
        "sizeBytes": size,
        "pageNo": session_page,
        "unlocked": unlocked,
    }


def _require_session(content_id: str) -> _Session:
    with _lock:
        session = _sessions.get(content_id)
        if session is None:
            raise ImportError_("预览会话已失效，请重新打开文档")
        session.touched_at = time.time()
        return session


def read_content(content_id: str, offset: int = 0, length: int | None = None) -> dict[str, Any]:
    """按偏移读取一段字节（base64）；`eof` 为真表示已到末尾。"""
    session = _require_session(content_id)
    try:
        start = int(offset)
    except (TypeError, ValueError):
        raise ImportError_("offset 必须为整数")
    if start < 0:
        raise ImportError_("offset 不能为负")
    size = min(int(length) if length else MAX_CHUNK_BYTES, MAX_CHUNK_BYTES)
    if size <= 0:
        raise ImportError_("length 必须为正整数")

    try:
        with session.path.open("rb") as fh:
            fh.seek(start)
            data = fh.read(size)
    except OSError as exc:
        raise ImportError_("原件内容读取失败") from exc

    return {
        "contentId": content_id,
        "offset": start,
        "dataBase64": base64.b64encode(data).decode("ascii"),
        "eof": start + len(data) >= session.size,
    }


def close_content(content_id: str) -> dict[str, Any]:
    """关闭会话并释放临时明文（重复关闭是幂等的）。"""
    with _lock:
        session = _sessions.pop(content_id, None)
    if session is not None:
        _release(session)
    return {"closed": bool(session), "contentId": content_id}


def open_artifact_content(artifact_id: str) -> dict[str, Any]:
    """打开文件类产物（kind=file）的内容会话：AI 生成的 Word/Excel/PPT 等按原格式预览。

    与 document.open_content 共用同一套会话机制（read/close 按内容ID操作）：
    - 文件复制进案件临时目录，随会话释放，工作空间原文件只读不动；
    - 产物登记时 body 只存文件名与工作空间目录名（不存绝对路径，防路径泄漏），
      这里按当前工作空间根解析；文件名必须是纯名称（无路径分隔符，防穿越）；
    - 类型优先按扩展名（产物登记白名单内的文档格式），魔数嗅探兜底。
    """
    import shutil

    from . import artifacts as artifact_store
    from .sandbox import workspace_root

    art = artifact_store.get_artifact(artifact_id)
    if art["kind"] != "file":
        raise ImportError_("该产物不是文件类产物")
    body = art.get("body") or {}
    name = str(body.get("fileName") or "")
    if not name or name in (".", "..") or Path(name).name != name:
        raise ImportError_("产物文件名缺失或非法")
    root = workspace_root()
    src = root / name
    if not src.is_file():
        raise ImportError_("产物文件已不在工作空间中，无法预览")

    ext = src.suffix.lower().lstrip(".")
    if ext in KIND_MIME:
        kind, mime = ext, KIND_MIME[ext]
    else:
        sniffed = sniff_kind(src)
        if sniffed is None:
            raise ImportError_("该文件格式暂不支持预览")
        kind, mime = sniffed

    # 复制进案件临时目录（release 按路径在 temp_root 之下判定，会随会话清理）
    dst = tempstore.case_temp_dir(root, str(art["caseId"])) / f"{uuid.uuid4().hex}{src.suffix.lower()}"
    try:
        shutil.copy2(src, dst)
        size = dst.stat().st_size
    except OSError as exc:
        dst.unlink(missing_ok=True)
        raise ImportError_("产物内容不可读") from exc

    now = time.time()
    content_id = str(uuid.uuid4())
    session = _Session(
        content_id=content_id,
        document_id=artifact_id,
        case_id=str(art["caseId"]),
        kind=kind,
        mime=mime,
        root=root,
        path=dst,
        size=size,
        page_no=None,
        unlocked=True,
        created_at=now,
        touched_at=now,
        source_path=None,
    )
    with _lock:
        _prune_locked(now)
        _sessions[content_id] = session

    return {
        "contentId": content_id,
        "documentId": artifact_id,
        "kind": kind,
        "mime": mime,
        "sizeBytes": size,
        "pageNo": None,
        "unlocked": True,
    }