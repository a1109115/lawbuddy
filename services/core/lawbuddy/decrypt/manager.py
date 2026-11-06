"""加密文档解锁服务（REQ-DOC-004/011/012/013 / 技术方案 §11.2 / DEC-004）。

- 加密检测在导入期完成（PDF / CFB Office），命中标记 `need_password`；
- `unlock` 走隔离 Worker 解密，密码只经 stdin 受控通道（REQ-DOC-011）；
- 解密后的明文经 AES-256-GCM 重新加密单独落盘（`unlocked_blob_uri`），
  原件（加密文件）保持不可变；解析/阅读取 `COALESCE(unlocked_blob_uri, blob_uri)`；
- Worker 返回供后续使用，不需要长期持有解密密文的 Worker 生命周期；
- 密码错误、证书加密 / IRM / DRM / 未知算法一律明确失败，不尝试绕过（REQ-DOC-013）；
- 单次请求即启动一个 Worker 进程，进程退出即释放密码内存。
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .. import crypto, tempstore
from ..keyring import get_active_dek
from ..storage import get_db
from ..workspace import BLOB_DIRNAME, now_iso

CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
WORKER_TIMEOUT_S = 60


class DecryptError(RuntimeError):
    """面向用户的解锁错误（密码错误 / 不支持 / 容器损坏）。"""


def _is_ooxml(blob: Path) -> bool:
    """CFB 复合文档魔数即加密 Office 容器（解密后才是 zip）。"""
    try:
        with blob.open("rb") as f:
            return f.read(len(CFB_MAGIC)) == CFB_MAGIC
    except OSError:
        return False


def _detect_pdf_encryption(blob: Path) -> bool:
    from pypdf import PdfReader

    try:
        return bool(PdfReader(str(blob)).is_encrypted)
    except Exception:
        return False


def detect_encryption(blob: Path, kind: str) -> bool:
    """判断原件是否加密（导入期调用）。"""
    if kind == "pdf":
        return _detect_pdf_encryption(blob)
    if kind in ("docx", "xlsx", "pptx"):
        return _is_ooxml(blob)
    return False


def _workspace_root() -> Path:
    db = get_db()
    ws = db.query_one("SELECT root_uri FROM workspaces ORDER BY created_at LIMIT 1")
    if ws is None:
        raise DecryptError("workspace not initialized")
    return Path(ws["root_uri"])


def _run_worker(blob_path: str, out_path: str, password: str, kind: str) -> dict[str, Any]:
    # PyInstaller 冻结后 sys.executable 是 lawbuddy-core.exe 自身，`-m` 不可用，
    # 以子命令分发（见 lawbuddy.__main__._run_worker）
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "decrypt-worker"]
    else:
        cmd = [sys.executable, "-m", "lawbuddy.decrypt.worker"]
    proc = subprocess.run(
        cmd,
        input=json.dumps({"blobPath": blob_path, "outPath": out_path,
                          "password": password, "kind": kind}, ensure_ascii=False) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=WORKER_TIMEOUT_S,
    )
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "解密进程异常退出", "unsupported": False}


def unlock_document(document_id: str, password: str) -> dict[str, Any]:
    """解锁加密文档：解密原件 → 重新加密入库 → 标记版本文档可解析。

    仅处理用户提供的密码；密码不入库、不进日志、不进入 Prompt（REQ_DOC-011）。
    """
    from .. import audit
    from ..cases import require_active_case

    db = get_db()
    doc = db.query_one(
        """
        SELECT d.id, d.case_id, d.kind, v.id AS version_id, v.blob_uri
        FROM documents d
        JOIN document_versions v ON v.id = d.active_version_id
        WHERE d.id = ?
        """,
        (document_id,),
    )
    if doc is None:
        raise DecryptError("文档不存在")
    require_active_case(doc["case_id"])

    root = _workspace_root()
    blob = root / doc["blob_uri"]
    if not blob.is_file():
        raise DecryptError("文档原件缺失，无法解锁")

    # 1) 先解开本产品的内容存储加密，再交给隔离 Worker 做文档解密（REQ-DOC-012）
    temp_dir = tempstore.case_temp_dir(root, doc["case_id"])
    plain = temp_dir / f"unlock_{uuid.uuid4().hex}"
    kind = "pdf" if doc["kind"] == "pdf" else "ooxml"
    try:
        with tempstore.materialized(root, doc["blob_uri"], doc["case_id"], doc["kind"]) as src_blob:
            result = _run_worker(str(src_blob), str(plain), password, kind)
    except subprocess.TimeoutExpired:
        raise DecryptError("解密超时，请重试") from None
    if not result.get("ok"):
        plain.unlink(missing_ok=True)
        error = str(result.get("error") or "未知错误")
        raise DecryptError(error)

    # 2) 明文重新加密入库（原件不变）
    new_sha = _sha256(plain)
    unlocked_uri = _store_unlocked_blob(root, plain, new_sha)
    plain.unlink(missing_ok=True)

    # 3) 字段更新 + 审计（只记元数据，不写正文与密码）
    with db.transaction() as conn:
        conn.execute(
            "UPDATE document_versions SET unlocked_blob_uri = ? WHERE id = ?",
            (unlocked_uri, doc["version_id"]),
        )
        audit.append(
            conn,
            action="document.unlock",
            resource_type="document",
            resource_id=doc["id"],
            case_id=doc["case_id"],
            metadata={"kind": doc["kind"], "sha256": new_sha[:12]},
        )

    # 4) 解锁后立即解析（REQ-DOC-004：解密后文档进入可用/部分可用状态）
    from .. import documents as docs

    row = db.query_one("SELECT title, kind FROM documents WHERE id = ?", (document_id,))
    kind_now = _resniff_kind(root, unlocked_uri, doc["case_id"], row["kind"])
    if kind_now != row["kind"]:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET kind = ?, updated_at = ? WHERE id = ?",
                (kind_now, now_iso(), document_id),
            )
    try:
        page_count, _ = docs.parse_document_version(
            case_id=doc["case_id"],
            document_id=document_id,
            version_id=doc["version_id"],
            title=row["title"],
            kind=kind_now,
            blob_uri=doc["blob_uri"],
            root=root,
        )
    except Exception as exc:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET status = 'failed', updated_at = ? WHERE id = ?",
                (now_iso(), document_id),
            )
        raise DecryptError(f"解密成功但解析失败：{type(exc).__name__}") from exc
    finally:
        # 解密与解析结束即清理案件临时明文（REQ-DOC-012）
        tempstore.purge_case_temp(root, doc["case_id"])

    return {
        "unlocked": True,
        "documentId": document_id,
        "sha256": new_sha,
        "pageCount": page_count,
    }


def _resniff_kind(root: Path, blob_uri: str, case_id: str, fallback: str) -> str:
    """解锁后重新嗅探真实类型（加密期按扩展名推断，可能不准）。"""
    from ..documents import sniff_kind

    try:
        with tempstore.materialized(root, blob_uri, case_id) as readable:
            sniffed = sniff_kind(readable)
    except Exception:
        return fallback
    return sniffed[0] if sniffed else fallback


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _store_unlocked_blob(root: Path, src: Path, sha: str) -> str:
    dst = root / BLOB_DIRNAME / sha[:2] / sha
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        crypto.encrypt_file(src, tmp, crypto.derive_file_key(get_active_dek()))
        tmp.replace(dst)
    return f"{BLOB_DIRNAME}/{sha[:2]}/{sha}"