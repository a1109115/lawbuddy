"""加密备份与恢复（DEV-FND-006 备份部分 / DB-006 / 技术方案 §19.2）。

- **在线备份**：对活动连接执行 `ATTACH ... KEY` + `sqlcipher_export`，生成同密钥加密副本，
  无需停机，源库不受影响（技术方案 §13.1 在线备份）；
- **恢复密钥**：工作空间 DEK 由本机密钥环（DPAPI 等）包装，无法跨机解包；因此备份同时导出
  「恢复密钥文件」——DEK 经**用户口令**派生的 KEK（PBKDF2-HMAC-SHA256 + AES-256-GCM）包装，
  口令不落盘、不进日志与命令行（调用方须经交互方式获取）；
- **恢复校验**（DB-006）：副本可打开 + `integrity_check` + 关键表数量、文档版本哈希、
  权限授权、审计链逐项与源库比对，要求 100% 一致；
- **离线恢复**：应用关闭后在目标工作空间还原副本，并用目标机器的密钥环重新包装 DEK。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import get_db, load_driver
from .migrations import MIGRATIONS
from ..keyring import DEK_BYTES, dek_path, sqlcipher_key_pragma, wrap_bytes

# 口令派生参数（恢复密钥文件格式版本 1）
KDF = "pbkdf2-hmac-sha256"
KDF_ITERATIONS = 200_000
KEYFILE_VERSION = 1

# 一致性比对的关键表（DB-006：抽样哈希、引用、权限 100% 一致）
COUNT_TABLES = (
    "cases", "documents", "document_versions", "document_pages", "source_refs",
    "claims", "claim_sources", "artifacts", "permission_grants", "approval_requests",
    "memories", "audit_logs",
)
HASH_SAMPLE_TABLE = "document_versions"  # 抽样哈希来源（sha256 列）
HASH_SAMPLE_LIMIT = 200


class BackupError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 恢复密钥文件（DEK 口令包装）
# ---------------------------------------------------------------------------

def _derive_kek(passphrase: str, salt: bytes, iterations: int = KDF_ITERATIONS) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    if not passphrase or len(passphrase) < 8:
        raise BackupError("恢复口令至少 8 个字符")
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(
        passphrase.encode("utf-8")
    )


def wrap_dek(dek: bytes, passphrase: str) -> dict[str, Any]:
    """用口令派生的 KEK（AES-256-GCM）包装 DEK，返回可序列化的恢复密钥文件内容。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(dek) != DEK_BYTES:
        raise BackupError("DEK 长度必须为 32 字节")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    kek = _derive_kek(passphrase, salt)
    ciphertext = AESGCM(kek).encrypt(nonce, dek, b"slvyue-backup-v1")
    return {
        "version": KEYFILE_VERSION,
        "kdf": KDF,
        "iterations": KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def unwrap_dek(payload: dict[str, Any], passphrase: str) -> bytes:
    """还原 DEK；口令错误或文件损坏统一失败（GCM 校验），不泄露细节。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        if int(payload.get("version") or 0) != KEYFILE_VERSION or payload.get("kdf") != KDF:
            raise BackupError("恢复密钥文件版本或算法不受支持")
        salt = base64.b64decode(str(payload["salt"]))
        nonce = base64.b64decode(str(payload["nonce"]))
        ciphertext = base64.b64decode(str(payload["ciphertext"]))
        kek = _derive_kek(passphrase, salt, int(payload.get("iterations") or KDF_ITERATIONS))
        dek = AESGCM(kek).decrypt(nonce, ciphertext, b"slvyue-backup-v1")
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BackupError("恢复口令错误或恢复密钥文件损坏") from exc
    if len(dek) != DEK_BYTES:
        raise BackupError("恢复密钥长度非法")
    return dek


# ---------------------------------------------------------------------------
# 只读打开副本 / 源库
# ---------------------------------------------------------------------------

def _open_readonly(db_path: Path, dek: bytes) -> Any:
    """以 raw key 只读打开数据库（不应用迁移、不写 PRAGMA）。"""
    driver, _ = load_driver()
    if not db_path.exists():
        raise BackupError(f"数据库文件不存在：{db_path.name}")
    conn = driver.connect(str(db_path))
    try:
        # PRAGMA key 必须是连接上的第一条语句；raw key 需以双引号包裹（与 Database 一致）
        conn.execute(f'PRAGMA key = "{sqlcipher_key_pragma(dek)}"')
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except Exception as exc:  # noqa: BLE001
        conn.close()
        raise BackupError(f"数据库无法打开（密钥错误或文件损坏）：{type(exc).__name__}") from exc
    return conn


def _counts(conn: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in COUNT_TABLES:
        try:
            out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except Exception:  # noqa: BLE001 — 旧库可能缺表，按缺失记为 -1
            out[table] = -1
    return out


def _sample_hashes(conn: Any) -> list[str]:
    try:
        rows = conn.execute(
            f"SELECT sha256 FROM {HASH_SAMPLE_TABLE} WHERE sha256 IS NOT NULL ORDER BY id LIMIT ?",
            (HASH_SAMPLE_LIMIT,),
        ).fetchall()
        return sorted(str(r[0]) for r in rows)
    except Exception:  # noqa: BLE001
        return []


def _audit_chain(conn: Any) -> dict[str, Any]:
    """审计链总览（条数与尾哈希）；完整重算由 verify 段执行。"""
    try:
        row = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()
        total = int(row[0])
        tail = conn.execute("SELECT event_hash FROM audit_logs ORDER BY seq DESC LIMIT 1").fetchone()
        head = conn.execute("SELECT prev_hash FROM audit_logs ORDER BY seq ASC LIMIT 1").fetchone()
        return {"total": total, "tailHash": str(tail[0]) if tail else None,
                "headPrevHash": str(head[0]) if head else None}
    except Exception:  # noqa: BLE001
        return {"total": -1, "tailHash": None, "headPrevHash": None}


def _verify_chain(conn: Any) -> dict[str, Any]:
    """复算审计哈希链（与 audit.verify 同算法，作用于副本连接）。"""
    import hashlib

    from ..audit import GENESIS, _event_hash

    prev = GENESIS
    expected_seq = 1
    total = 0
    for row in conn.execute(
        "SELECT seq, created_at, actor, action, resource_type, resource_id, case_id, result, "
        "metadata_json, prev_hash, event_hash FROM audit_logs ORDER BY seq ASC"
    ):
        total += 1
        if int(row[0]) != expected_seq or row[9] != prev:
            return {"ok": False, "total": total, "brokenAt": int(row[0]), "reason": "chain link mismatch"}
        recomputed = _event_hash(
            row[9], int(row[0]), row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]
        )
        if recomputed != row[10]:
            return {"ok": False, "total": total, "brokenAt": int(row[0]), "reason": "hash mismatch"}
        prev = row[10]
        expected_seq += 1
    return {"ok": True, "total": total, "tailHash": prev}


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------

def create_backup(dest_dir: str, passphrase: str | None = None) -> dict[str, Any]:
    """创建加密备份（在线，不停机）。

    返回副本路径、恢复密钥文件路径与清单；未提供口令时生成一次性恢复密钥
    （仅在返回值中出现，调用方必须引导用户离线保存，不得写入日志）。
    """
    db = get_db()
    target = Path(dest_dir)
    if not target.is_dir():
        raise BackupError(f"备份目录不存在：{dest_dir}")

    recovery_key = passphrase or secrets.token_urlsafe(18)
    dek = get_active_dek_or_raise()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stem = f"lawbuddy-{stamp}-{uuid.uuid4().hex[:6]}"
    db_copy = target / f"{stem}.db"
    key_file = target / f"{stem}.key.json"
    manifest_file = target / f"{stem}.manifest.json"

    # 在线导出：ATTACH 同密钥副本 → sqlcipher_export（源库保持可用）
    # 注意：ATTACH / DETACH 不能在事务内执行，故持串行写锁但不开启事务
    with db.locked() as conn:
        conn.execute("ATTACH DATABASE ? AS backup KEY ?", (str(db_copy), sqlcipher_key_pragma(dek)))
        try:
            cur = conn.execute("SELECT sqlcipher_export('backup')")
            cur.fetchall()  # 完整消费结果，避免附带库句柄占用导致 DETACH 失败
            cur.close()
        finally:
            conn.execute("DETACH DATABASE backup")

    key_file.write_text(json.dumps(wrap_dek(dek, recovery_key), ensure_ascii=False, indent=2), encoding="utf-8")
    _restrict(db_copy)
    _restrict(key_file)

    manifest = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "schemaVersion": max(v for v, _ in MIGRATIONS),
        "dbFile": db_copy.name,
        "keyFile": key_file.name,
        "dbSizeBytes": db_copy.stat().st_size,
        "counts": _counts(_main_conn()),
    }
    manifest["audit"] = _audit_chain(_main_conn())
    manifest["hashSamples"] = len(_sample_hashes(_main_conn()))
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _restrict(manifest_file)

    from .. import audit

    with db.transaction() as conn:
        audit.append(
            conn, action="db.backup", resource_type="workspace", resource_id=db_copy.name,
            metadata={"schemaVersion": manifest["schemaVersion"], "dbSizeBytes": manifest["dbSizeBytes"],
                      "keyFile": key_file.name, "tables": len(COUNT_TABLES)},
        )

    return {
        "dbPath": str(db_copy),
        "keyFilePath": str(key_file),
        "manifestPath": str(manifest_file),
        "manifest": manifest,
        # 一次性恢复密钥：仅本次返回；口令由用户离线保存，服务端不落盘
        "recoveryKey": recovery_key if passphrase is None else None,
    }


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_active_dek_or_raise() -> bytes:
    from ..keyring import get_active_dek

    dek = get_active_dek()
    if not dek:
        raise BackupError("工作空间密钥未加载，备份已阻断（fail-closed）")
    return dek


def _main_conn() -> Any:
    """主库的底层连接（只读查询用；仅在事务外调用）。"""
    return get_db().raw_connection()


# ---------------------------------------------------------------------------
# 校验（DB-006：100% 一致）
# ---------------------------------------------------------------------------

def verify_backup(
    db_path: str, key_file: str, passphrase: str, compare_live: bool = True,
) -> dict[str, Any]:
    """校验备份副本：完整性、数量、抽样哈希、权限授权与审计链（逐项与源库比对）。"""
    payload = json.loads(Path(key_file).read_text(encoding="utf-8"))
    dek = unwrap_dek(payload, passphrase)
    checks: list[dict[str, Any]] = []

    conn = _open_readonly(Path(db_path), dek)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        checks.append({"name": "integrityCheck", "ok": str(integrity).lower() == "ok", "value": str(integrity)})

        backup_counts = _counts(conn)
        backup_hashes = _sample_hashes(conn)
        chain = _verify_chain(conn)
        checks.append({"name": "auditChainIntact", "ok": bool(chain.get("ok")), "value": chain})
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        schema_version = int(row[0]) if row and row[0] is not None else 0

        if compare_live:
            live = _main_conn()
            live_counts = _counts(live)
            # 内容表严格一致；audit_logs 为追加式，备份后新增（如备份自身的审计事件）不回写副本
            diff = {
                t: (live_counts.get(t), backup_counts.get(t))
                for t in COUNT_TABLES
                if t != "audit_logs" and live_counts.get(t) != backup_counts.get(t)
            }
            rows_ok = not diff and live_counts.get("audit_logs", -1) >= backup_counts.get("audit_logs", 0)
            checks.append({"name": "tableCountsMatch", "ok": rows_ok,
                           "value": diff or {"content": "all-match", "auditLogs": "prefix"}})

            live_hashes = _sample_hashes(live)
            checks.append({
                "name": "sampledHashesMatch",
                "ok": live_hashes == backup_hashes,
                "value": {"live": len(live_hashes), "backup": len(backup_hashes)},
            })

            # 备份是源库审计链的前缀（备份时刻起可完整复算，之后的追加不影响副本）
            backup_total = int(chain.get("total") or 0)
            prefix_ok = True
            if backup_total > 0:
                row = live.execute("SELECT event_hash FROM audit_logs WHERE seq = ?", (backup_total,)).fetchone()
                prefix_ok = row is not None and str(row[0]) == chain.get("tailHash")
            checks.append({"name": "auditIsPrefixOfLive", "ok": prefix_ok,
                           "value": {"backupTotal": backup_total, "liveTotal": live_counts.get("audit_logs")}})

            grants_live = _permission_fingerprint(live)
            grants_backup = _permission_fingerprint(conn)
            checks.append({"name": "permissionGrantsMatch", "ok": grants_live == grants_backup,
                           "value": {"live": len(grants_live), "backup": len(grants_backup)}})
    finally:
        conn.close()

    return {"ok": all(c["ok"] for c in checks), "checks": checks, "counts": backup_counts,
            "schemaVersion": schema_version}


def _permission_fingerprint(conn: Any) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT capability, scope_type, scope_id FROM permission_grants ORDER BY capability, scope_type, scope_id"
        ).fetchall()
        return [f"{r[0]}|{r[1]}|{r[2]}" for r in rows]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# 离线恢复（新机 / 回滚演练）
# ---------------------------------------------------------------------------

def restore_backup(
    db_path: str, key_file: str, passphrase: str, target_root: str, overwrite: bool = False,
) -> dict[str, Any]:
    """在目标工作空间还原备份（**必须离线执行**，应用关闭状态）。

    步骤：解口令 → 复制加密副本 → 以目标机器密钥环重新包装 DEK（新机换钥）
    → 校验还原结果与原备份一致。目标已存在数据库时默认拒绝覆盖。
    """
    root = Path(target_root)
    root.mkdir(parents=True, exist_ok=True)
    payload = json.loads(Path(key_file).read_text(encoding="utf-8"))
    dek = unwrap_dek(payload, passphrase)

    from ..workspace import DB_FILENAME

    dest = root / ".lawbuddy" / DB_FILENAME
    if dest.exists() and not overwrite:
        raise BackupError("目标工作空间已有数据库，需显式允许覆盖")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(db_path, dest)
    _restrict(dest)

    # 新机密钥环：用本机 KEK 重新包装 DEK（原机 DPAPI 包装不可跨机解包）
    key_path = dek_path(root)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = key_path.with_suffix(".tmp")
    tmp.write_text(wrap_bytes(dek), encoding="ascii")
    _restrict(tmp)
    os.replace(tmp, key_path)
    _restrict(key_path)

    # 还原校验：副本可打开且审计链完整
    conn = _open_readonly(dest, dek)
    try:
        chain = _verify_chain(conn)
        counts = _counts(conn)
    finally:
        conn.close()
    return {"ok": bool(chain.get("ok")), "dbPath": str(dest), "dekRewrapped": True,
            "auditChain": chain, "counts": counts}


def verify_restored(db_path: str, dek: bytes, expected_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """恢复后校验：以给定 DEK 打开还原库并比对清单（测试与恢复向导共用）。"""
    conn = _open_readonly(Path(db_path), dek)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower()
        counts = _counts(conn)
        chain = _verify_chain(conn)
    finally:
        conn.close()
    ok = integrity == "ok" and bool(chain.get("ok"))
    if expected_manifest:
        expected = expected_manifest.get("counts") or {}
        ok = ok and all(counts.get(t) == v for t, v in expected.items() if t in counts)
    return {"ok": ok, "integrity": integrity, "counts": counts, "auditChain": chain}