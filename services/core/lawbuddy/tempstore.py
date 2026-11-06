"""案件专属受限临时目录与明文物料化（技术方案 §11.2 / REQ-DOC-012 / AC-SEC-007）。

- 解密后的明文只允许存在于 `<工作空间>/.lawbuddy/tmp/<案件ID>/`，权限收紧为 0700；
- 正常结束、取消与异常重启都执行残留清理：同步场景用 `materialized()` 上下文管理器，
  异步场景（OCR 后台线程）用 `materialize()` + `release()`；
- Core 启动时调用 `purge_all_temp()` 清理上次异常退出的残留（AC-SEC-007）。

历史明文 blob 直接返回原路径，不产生副本，便于增量迁移。
"""

from __future__ import annotations

import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import crypto
from .keyring import get_active_dek

TMP_DIRNAME = "tmp"
_RESTRICTED_DIR = ".lawbuddy"


class TempStoreError(RuntimeError):
    pass


def _safe_component(value: str) -> str:
    """路径段白名单（S-005）：仅允许字母数字与 -_，其余（含 `.`、`..`、分隔符）归入 _orphan。"""
    token = (value or "").strip()
    if token and token not in (".", "..") and all(ch.isalnum() or ch in "-_" for ch in token):
        return token
    return "_orphan"


def temp_root(root: Path) -> Path:
    return root / _RESTRICTED_DIR / TMP_DIRNAME


def case_temp_dir(root: Path, case_id: str) -> Path:
    """返回并创建案件专属临时目录（权限 0700）；路径段经白名单规范化（S-005）。"""
    path = temp_root(root) / _safe_component(case_id)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _suffix_for(kind: str | None) -> str:
    return {
        "pdf": ".pdf",
        "docx": ".docx",
        "xlsx": ".xlsx",
        "pptx": ".pptx",
        "png": ".png",
        "jpg": ".jpg",
        "tiff": ".tiff",
        "txt": ".txt",
        "md": ".md",
        "html": ".html",
    }.get(kind or "", "")


def _blob_within_root(root: Path, blob_uri: str) -> Path:
    """解析 blob URI 并强制留在工作空间内（S-005：拒绝 ../、绝对路径与逃逸）。"""
    candidate = (root / blob_uri).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise TempStoreError("内容存储路径越界（S-005）")
    return candidate


def materialize(root: Path, blob_uri: str, case_id: str, kind: str | None = None) -> Path:
    """把内容存储中的 blob 还原为可读文件路径。

    加密 blob → 解密到案件临时目录（调用方负责 `release()`）；
    历史明文 blob → 直接返回原路径（不复制）；
    blob URI 越界（`../`、绝对路径、符号链接逃逸）一律拒绝（S-005）。
    """
    blob = _blob_within_root(root, blob_uri)
    if not blob.exists():
        raise TempStoreError(f"内容存储文件缺失：{blob_uri}")
    if not crypto.is_encrypted(blob):
        return blob

    dst = case_temp_dir(root, case_id) / f"{uuid.uuid4().hex}{_suffix_for(kind)}"
    try:
        crypto.decrypt_file(blob, dst, crypto.derive_file_key(get_active_dek()))
    except Exception:
        dst.unlink(missing_ok=True)
        raise
    return dst


def release(root: Path, path: Path) -> None:
    """清理物料化产生的临时明文（对历史明文 blob 是空操作）。"""
    try:
        if temp_root(root) in path.parents:
            path.unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def materialized(root: Path, blob_uri: str, case_id: str, kind: str | None = None) -> Iterator[Path]:
    """同步场景的物料化上下文：退出时必定清理临时明文。"""
    path = materialize(root, blob_uri, case_id, kind)
    try:
        yield path
    finally:
        release(root, path)


def purge_case_temp(root: Path, case_id: str) -> None:
    """清理某案件的全部临时明文（任务完成 / 取消 / 解密结束）。"""
    target = temp_root(root) / _safe_component(case_id)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def purge_all_temp(root: Path) -> int:
    """清理全部临时明文；返回清理的条目数（Core 启动时调用，AC-SEC-007）。"""
    base = temp_root(root)
    if not base.exists():
        return 0
    count = 0
    for child in list(base.iterdir()):
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            count += 1
        except OSError:
            pass
    return count
