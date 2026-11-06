"""明文库 → SQLCipher 加密库的一次性迁移（DEC-004 / 技术方案 §13.2、§14）。

流程（原子替换，源库在替换前不被修改，因此无需留明文备份）：

1. 识别当前库状态：absent / plaintext / encrypted / unreadable；
2. 明文库先 `wal_checkpoint(TRUNCATE)`，保证 WAL 内容并入主库；
3. `ATTACH ... KEY` + `sqlcipher_export()` 生成加密副本（`*.enc-migrating`）；
4. 校验副本：integrity_check、迁移版本、核心表行数逐一比对；
5. 校验通过后 `os.replace` 原子替换，并清理旧库的 -wal / -shm 明文残留；
6. 任一步失败：删除副本，源库保持原样并抛出，绝不留下半迁移状态。

注：旧明文文件的磁盘块在替换后由文件系统回收，不做物理擦除（需全盘擦除能力，
超出应用范围）；发布门禁只要求"当前磁盘文件扫描无明文"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..keyring import sqlcipher_key_pragma
from .db import load_driver

# 迁移后校验行数的核心表（存在才比对）
_CORE_TABLES = (
    "workspaces",
    "cases",
    "documents",
    "document_versions",
    "document_pages",
    "text_blocks",
    "tasks",
    "claims",
    "artifacts",
    "audit_logs",
    "schema_migrations",
)


class MigrationError(RuntimeError):
    pass


def _connect(path: Path, dek: bytes | None) -> Any:
    driver, _ = load_driver()
    con = driver.connect(str(path))
    if dek is not None:
        con.execute(f'PRAGMA key = "{sqlcipher_key_pragma(dek)}"')
    return con


def _readable(path: Path, dek: bytes | None) -> bool:
    """能否以指定密钥模式读出 schema（用于状态识别）。"""
    try:
        con = _connect(path, dek)
    except Exception:
        return False
    try:
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return True
    except Exception:
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass


def detect(path: Path, dek: bytes | None = None) -> str:
    """识别数据库状态：absent / plaintext / encrypted / unreadable。"""
    if not path.exists() or path.stat().st_size == 0:
        return "absent"
    if _readable(path, None):
        return "plaintext"
    if dek is not None and _readable(path, dek):
        return "encrypted"
    return "unreadable"


def _table_counts(con: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _CORE_TABLES:
        try:
            counts[table] = int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        except Exception:
            continue
    return counts


def _verify(src_counts: dict[str, int], enc_path: Path, dek: bytes) -> dict[str, Any]:
    """校验加密副本：完整性、迁移版本、核心表行数。"""
    con = _connect(enc_path, dek)
    try:
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise MigrationError(f"加密库完整性校验失败：{integrity}")
        enc_counts = _table_counts(con)
    finally:
        con.close()

    if enc_counts != src_counts:
        diff = {
            t: (src_counts.get(t), enc_counts.get(t))
            for t in set(src_counts) | set(enc_counts)
            if src_counts.get(t) != enc_counts.get(t)
        }
        raise MigrationError(f"加密库行数校验失败（表: 源→加密）：{diff}")
    return {"integrity": integrity, "tables": len(enc_counts), "rows": sum(enc_counts.values())}


def migrate_to_encrypted(path: Path, dek: bytes) -> dict[str, Any]:
    """把明文库迁移为 SQLCipher 加密库。"""
    driver, has_cipher = load_driver()
    if not has_cipher:
        raise MigrationError("SQLCipher 未安装，无法迁移为加密库（DEC-004）")

    tmp = path.with_name(path.name + ".enc-migrating")
    if tmp.exists():
        tmp.unlink()

    src_counts: dict[str, int] = {}
    con = _connect(path, None)
    try:
        # WAL 内容并入主库，避免副本漏掉未落盘的已提交事务
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        src_counts = _table_counts(con)
        # 参数绑定文件名，KEY 子句以 raw key 字面量注入（不进口令 KDF）
        con.execute(
            f'ATTACH DATABASE ? AS encrypted KEY "{sqlcipher_key_pragma(dek)}"', (str(tmp),)
        )
        con.execute("SELECT sqlcipher_export('encrypted')")
        con.execute("DETACH DATABASE encrypted")
    except Exception as exc:
        try:
            con.close()
        except Exception:
            pass
        if tmp.exists():
            tmp.unlink()
        raise MigrationError(f"加密导出失败：{type(exc).__name__}: {exc}") from exc
    finally:
        try:
            con.close()
        except Exception:
            pass

    try:
        verified = _verify(src_counts, tmp, dek)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise

    # 源连接已关闭，可安全原子替换（Windows 下文件被占用会失败）
    import os

    os.replace(tmp, path)
    for suffix in ("-wal", "-shm"):
        stale = path.with_name(path.name + suffix)
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass

    return {
        "migrated": True,
        "from": "plaintext",
        "to": "encrypted",
        "verified": verified,
    }


def ensure_encrypted(path: Path, dek: bytes) -> dict[str, Any]:
    """工作空间初始化入口：按需把明文库迁移为加密库。"""
    state = detect(path, dek)
    if state in ("absent", "encrypted"):
        return {"state": state, "migrated": False}
    if state == "plaintext":
        result = migrate_to_encrypted(path, dek)
        result["state"] = "encrypted"
        return result
    raise MigrationError(
        "数据库文件既不能以明文打开，也不能以当前工作空间密钥打开；"
        "请确认是否使用了正确的工作空间目录或密钥"
    )
