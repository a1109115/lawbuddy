"""内容存储生命周期（WFA-LAW 优化方案 §4.8 / P2-11）。

两个长期存在但一直被静默吞掉的问题：

1. **删除工作空间失败**：blob 落盘时被设为只读（原件不可篡改），Windows 上
   这会让 `rmtree` 直接失败并留下半删目录；下次打开时库与内容目录不一致。
   解决：删除前先清只读属性，再按文件逐个删除，失败项汇总上报。

2. **重建后 blob 与 DEK 不匹配**：工作空间目录复用但 `.lawbuddy/dek.key` 被
   重建（或用户手动拷回旧内容目录）时，旧 blob 用新 DEK 解密必然失败。此前
   统一报成 "parse failed"，用户看到的是解析失败而非"密钥不匹配，请重新导入"。
   解决：解密失败时做一次明确的密钥匹配判定，给出可执行的恢复指引，绝不
   降级为明文存储。
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any

# 密钥不匹配的错误码：前端与审计据此区分"文件坏了"与"密钥换了"
REASON_DEK_MISMATCH = "BLOB_DEK_MISMATCH"
REASON_BLOB_MISSING = "BLOB_MISSING"
_DEK_MISMATCH_HINT = (
    "原件与当前工作空间密钥不匹配（工作空间可能被重建或内容目录被替换）。"
    "请重新导入该原件；已生成的页文本与分析结果如需保留，可先导出。"
)


def clear_readonly(path: Path) -> None:
    """清除只读属性（Windows 上是文件属性，POSIX 上是写权限位）。"""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass


def remove_tree(target: Path) -> dict[str, Any]:
    """删除目录树（先清只读属性），返回失败项而不是静默忽略。

    用于工作空间删除/清理：只读 blob 不清属性会让 Windows 的删除失败，
    进而留下"目录还在但库已删"的半删状态。
    """
    result: dict[str, Any] = {"removed": 0, "failed": []}
    if not target.exists():
        return result
    if target.is_file():
        clear_readonly(target)
        try:
            target.unlink()
            result["removed"] = 1
        except OSError as exc:
            result["failed"].append({"path": target.name, "error": type(exc).__name__})
        return result

    for dirpath, _dirnames, filenames in os.walk(target):
        for name in filenames:
            clear_readonly(Path(dirpath) / name)
    try:
        shutil.rmtree(target)
        result["removed"] = 1
    except OSError:
        # 逐项重试：单个文件仍被占用时不影响其余文件删除
        for dirpath, dirnames, filenames in os.walk(target, topdown=False):
            for name in filenames:
                p = Path(dirpath) / name
                clear_readonly(p)
                try:
                    p.unlink()
                    result["removed"] += 1
                except OSError as exc:
                    result["failed"].append({"path": p.name, "error": type(exc).__name__})
            for name in dirnames:
                try:
                    (Path(dirpath) / name).rmdir()
                except OSError:
                    pass
        try:
            target.rmdir()
        except OSError:
            pass
    return result


def probe_key_match(path: Path, dek: bytes) -> bool | None:
    """只解密首个数据块判定密钥是否匹配（True/False）；非加密文件返回 None。

    大文件（几十 MB 的 PDF）全量解密太贵，而密钥不匹配在第一个块的 GCM 校验
    就会失败——因此只读加密头 + 首块即可确定性判定，不产生明文落盘。
    """
    from . import crypto

    if not crypto.is_encrypted(path):
        return None
    try:
        with path.open("rb") as fh:
            header_len = crypto.HEADER_LEN
            head = fh.read(header_len)
            if len(head) < header_len:
                return False
            chunk_size, nonce_base = crypto._parse_header(head)  # noqa: SLF001 — 同包内部协议
            lenb = fh.read(4)
            if len(lenb) < 4:
                return True  # 空内容：无数据块可校验，按可解密处理
            size = int.from_bytes(lenb, "big")
            ct = fh.read(size)
            if len(ct) != size:
                return False
        aes = crypto.AESGCM(crypto.derive_file_key(dek))
        aes.decrypt(crypto._nonce(nonce_base, 0), ct, crypto._aad(0))  # noqa: SLF001
        return True
    except Exception:
        return False


def diagnose_blob(root: Path, blob_uri: str) -> dict[str, Any]:
    """判定一个 blob 当前能否用活动 DEK 解开，返回可展示的诊断结果。

    只读加密头 + 第一个数据块，不落明文、不解全量内容；密钥不匹配时立即返回，
    不会把异常升级为"解析失败"这种误导性结论。
    """
    from . import crypto
    from .keyring import KeyringError, get_active_dek

    path = (root / blob_uri).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return {"ok": False, "code": REASON_BLOB_MISSING, "message": "内容存储路径越界"}
    if not path.exists():
        return {"ok": False, "code": REASON_BLOB_MISSING, "message": "内容存储文件缺失，请重新导入原件"}
    if not crypto.is_encrypted(path):
        # 历史明文 blob：不需要 DEK，按可用处理（迁移时会加密）
        return {"ok": True, "encrypted": False}
    try:
        dek = get_active_dek()
    except KeyringError as exc:
        return {"ok": False, "code": REASON_DEK_MISMATCH, "message": str(exc)}
    matched = probe_key_match(path, dek)
    if matched:
        return {"ok": True, "encrypted": True}
    return {"ok": False, "code": REASON_DEK_MISMATCH, "message": _DEK_MISMATCH_HINT}


def is_dek_mismatch(exc: BaseException) -> bool:
    """异常是否属于"密钥不匹配"（区别于文件损坏/格式错误）。"""
    text = str(exc)
    return "密钥不匹配" in text or "密钥" in text and "解密失败" in text


def mismatch_hint() -> str:
    return _DEK_MISMATCH_HINT


def validate_workspace_blobs(root: Path, limit: int = 50) -> dict[str, Any]:
    """工作空间打开时的轻量自检：抽样判定 blob 与当前 DEK 是否匹配。

    只查少量样本（默认 50 个），命中不匹配立即返回结论；不做全量扫描，
    避免大工作空间启动变慢。结果用于在打开阶段给出明确提示，而不是等用户
    打开某份文档时才看到"解析失败"。
    """
    from .workspace import BLOB_DIRNAME

    base = root / BLOB_DIRNAME
    if not base.is_dir():
        return {"checked": 0, "mismatch": []}
    checked = 0
    mismatch: list[str] = []
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix == ".tmp":
            continue
        try:
            rel = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        diag = diagnose_blob(root, rel)
        checked += 1
        if not diag.get("ok") and diag.get("code") == REASON_DEK_MISMATCH:
            mismatch.append(rel)
            if len(mismatch) >= 3:
                break
        if checked >= limit:
            break
    return {"checked": checked, "mismatch": mismatch, "message": _DEK_MISMATCH_HINT if mismatch else ""}
