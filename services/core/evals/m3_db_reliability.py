# -*- coding: utf-8 -*-
"""M3 评测器：SQLite 与数据可靠性（03 清单 §10 DB-001~DB-010 的确定性可自动化部分）。

用法（services/core 目录下）：
    python -m evals.m3_db_reliability [--keep]

覆盖：
1. DB-001 外键 / 唯一 / CHECK 约束拒绝孤儿引用、非法状态与重复幂等键；
2. DB-002 并发写入（OCR / Agent / UI 同源连接串行化）无持续锁错误，完整性检查通过；
3. DB-003 事务故障整体回滚（要么全部完成，要么全部回滚）；
3.5 案件删除：RESTRICT 外键（conversations/contract_reviews）纳入删除清单，删除后无残留无外键违规；
4. DB-004/DB-005 迁移：全部版本已应用且可重复执行；校验和被篡改时拒绝启动且旧库仍可用；
5. DB-007 数据库损坏：损坏副本打开即失败（不静默覆盖），原库不受影响；
6. DB-009 静态明文扫描：正文、WAL 与内容存储中测试正文命中为 0；
7. DB-010 单实例（静态检查 Main 进程单实例锁）。

未覆盖（当前实现缺口，见报告 gaps，不得以绕行方案掩盖）：
- DB-006 备份恢复：尚无在线备份与恢复校验实现（DEV-FND-006 未完成部分）。

报告：控制台摘要 + evals/reports/m3_db_reliability_<ts>.json。门槛：全部 gates 通过。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
MAIN_TS = REPO_ROOT / "apps/desktop/src/main/index.ts"

# DB-009 扫描用标记（评测临时工作空间，仅用于本次评测）
PLAINTEXT_MARKER = "静态明文扫描标记-两会一解-20260920"


def _workspace_root() -> Path:
    from lawbuddy.storage import get_db

    row = get_db().query_one("SELECT root_uri FROM workspaces ORDER BY created_at LIMIT 1")
    if row is None:
        raise RuntimeError("workspace not initialized")
    return Path(row["root_uri"])


def _seg(report: dict[str, Any], name: str, fn, *args) -> bool:
    try:
        rows, ok = fn(report, *args)
        entry = report.get(name) or {}
        entry.update({"ok": bool(ok), "rows": rows})
        report[name] = entry
        return bool(ok)
    except Exception:  # noqa: BLE001
        report[name] = {"ok": False, "rows": [], "error": traceback.format_exc(limit=8)}
        return False


# ---------------------------------------------------------------------------
# 段 1：约束（DB-001）
# ---------------------------------------------------------------------------

def _run_constraints(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.storage import get_db
    from lawbuddy.storage.db import load_driver
    from lawbuddy.workspace import now_iso

    driver, _ = load_driver()  # 驱动可能是 sqlcipher3，异常类与内置 sqlite3 不同
    db = get_db()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-约束", case_type="civil", client_name="吴十", client_role="原告代理人")
    cid = case["id"]

    # 外键：孤儿文档
    fk_rejected = False
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO documents(id, case_id, kind, title, status, created_at, updated_at) "
                "VALUES ('orphan-doc', 'no-such-case', 'md', '孤儿文档', 'ready', ?, ?)",
                (now_iso(), now_iso()),
            )
    except driver.IntegrityError:
        fk_rejected = True
    rows.append({"step": "foreignKeyRejected", "ok": fk_rejected})

    # CHECK：非法案件状态
    check_rejected = False
    try:
        with db.transaction() as conn:
            conn.execute("UPDATE cases SET status = 'not-a-status' WHERE id = ?", (cid,))
    except driver.IntegrityError:
        check_rejected = True
    rows.append({"step": "checkRejected", "ok": check_rejected})

    # 唯一：重复的工作空间根目录（workspaces.root_uri UNIQUE）
    now = now_iso()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO workspaces(id, name, root_uri, created_at) "
            "VALUES ('eval-ws-1', '评测空间', 'C:/lawbuddy-eval-unique', ?)",
            (now,),
        )
    dup_rejected = False
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO workspaces(id, name, root_uri, created_at) "
                "VALUES ('eval-ws-2', '评测空间副本', 'C:/lawbuddy-eval-unique', ?)",
                (now,),
            )
    except driver.IntegrityError:
        dup_rejected = True
    rows.append({"step": "uniqueRootUriRejected", "ok": dup_rejected})

    # 外键声明确实开启（PRAGMA foreign_keys）
    fk_on = db.query_one("PRAGMA foreign_keys")
    rows.append({"step": "foreignKeysEnabled", "ok": bool(list(fk_on.values())[0]) if fk_on else False})

    report["constraints"] = {"caseId": cid}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 2：并发与完整性（DB-002）
# ---------------------------------------------------------------------------

def _run_concurrency(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.storage import get_db
    from lawbuddy.workspace import now_iso

    db = get_db()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-并发", case_type="civil", client_name="郑十一", client_role="原告代理人")
    cid = case["id"]

    # 并发写入：模拟 OCR / Agent / UI 三路同时写事件与设置
    errors: list[str] = []
    writes_per_thread = 12

    def writer(tag: str) -> None:
        from lawbuddy.agent import tasks as task_store

        for i in range(writes_per_thread):
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                        (f"eval.concurrent.{tag}.{i}", json.dumps({"i": i}, ensure_ascii=False), now_iso()),
                    )
                task_store.set_setting(f"eval.thread.{tag}", i)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{tag}:{type(exc).__name__}")

    threads = [threading.Thread(target=writer, args=(f"t{n}",)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    integrity = db.query("PRAGMA integrity_check")
    integrity_ok = bool(integrity) and str(list(integrity[0].values())[0]).lower() == "ok"
    written = int(db.query_one(
        "SELECT COUNT(*) AS n FROM settings WHERE key LIKE 'eval.concurrent.%'")["n"])
    rows.append({"step": "concurrentWritesNoLockError", "ok": not errors, "errors": errors[:5],
                 "written": written, "expected": 3 * writes_per_thread})
    rows.append({"step": "integrityCheckOk", "ok": integrity_ok, "result": list(integrity[0].values())[0] if integrity else None})

    # 外键完整性复核（DB-002 强杀场景的静态等价检查）
    fk_violations = db.query("PRAGMA foreign_key_check")
    rows.append({"step": "foreignKeyCheckClean", "ok": not fk_violations, "violations": len(fk_violations)})

    report["concurrency"] = {"caseId": cid, "written": written, "threads": 3}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 3：事务故障（DB-003）
# ---------------------------------------------------------------------------

def _run_transactions(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.storage import get_db
    from lawbuddy.workspace import now_iso

    db = get_db()
    rows: list[dict[str, Any]] = []
    before = int(db.query_one("SELECT COUNT(*) AS n FROM cases")["n"])

    marker = "eval-rollback-case"
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO cases(id, workspace_id, title, case_type, default_mode, status, created_at, updated_at) "
                "SELECT ?, workspace_id, '回滚样本', 'civil', 'legal_review', 'active', ?, ? FROM cases LIMIT 1",
                (marker, now_iso(), now_iso()),
            )
            conn.execute("INSERT INTO settings(key, value_json, updated_at) VALUES ('eval.rollback', '1', ?)", (now_iso(),))
            raise RuntimeError("注入事务故障")
    except RuntimeError:
        pass

    after = int(db.query_one("SELECT COUNT(*) AS n FROM cases")["n"])
    leftover = db.query_one("SELECT COUNT(*) AS n FROM settings WHERE key = 'eval.rollback'")["n"]
    rows.append({"step": "rollbackAllOrNothing", "ok": after == before and int(leftover) == 0,
                 "casesBefore": before, "casesAfter": after, "leftoverSettings": int(leftover)})

    # 回滚后连接仍可用（后续写入成功）
    ok_after = False
    try:
        create_case(title="M3评测-回滚后写入", case_type="civil", client_name="冯十二", client_role="原告代理人")
        ok_after = True
    except Exception:  # noqa: BLE001
        ok_after = False
    rows.append({"step": "connectionUsableAfterRollback", "ok": ok_after})

    report["transactions"] = {"casesBefore": before, "casesAfter": after}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 3.5：案件删除外键完整性（case.delete 曾因 conversations/contract_reviews
# 的 RESTRICT 外键未纳入删除清单而报 IntegrityError）
# ---------------------------------------------------------------------------

def _run_case_delete(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case, delete_case
    from lawbuddy.storage import get_db
    from lawbuddy.workspace import now_iso

    db = get_db()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-案件删除", case_type="civil", client_name="周十四", client_role="原告代理人")
    cid = case["id"]
    now = now_iso()

    # 一个文档（合同审查的 primary_document_id 指向它）
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO documents(id, case_id, kind, title, status, created_at, updated_at) "
            "VALUES ('eval-del-doc', ?, 'pdf', '删除评测合同', 'ready', ?, ?)",
            (cid, now, now),
        )
        # 合同审查会话（V11：primary_document_id RESTRICT）
        conn.execute(
            "INSERT INTO contract_reviews(id, case_id, primary_document_id, status, created_at, updated_at) "
            "VALUES ('eval-del-review', ?, 'eval-del-doc', 'completed', ?, ?)",
            (cid, now, now),
        )
        # 对话会话与消息（V26：conversations.case_id RESTRICT）
        conn.execute(
            "INSERT INTO conversations(id, case_id, created_at, updated_at) "
            "VALUES ('eval-del-conv', ?, ?, ?)",
            (cid, now, now),
        )
        conn.execute(
            "INSERT INTO conversation_messages(id, conversation_id, role, content, created_at) "
            "VALUES ('eval-del-msg', 'eval-del-conv', 'user', '删除评测消息', ?)",
            (now,),
        )
        # 编辑会话（无外键，验证孤儿清理）
        conn.execute(
            "INSERT INTO editor_sessions(id, target_kind, target_id, case_id, opened_at) "
            "VALUES ('eval-del-session', 'draft', 'eval-del-doc', ?, ?)",
            (cid, now),
        )

    result = delete_case(cid)
    rows.append({"step": "deleteWithRestrictedRefsSucceeds", "ok": result.get("deleted") is True,
                 "stats": {k: v for k, v in result.items() if k not in ("deleted", "caseId", "title")}})

    def _count(sql: str) -> int:
        return int(db.query_one(sql)["n"])

    leftovers = {
        "cases": _count("SELECT COUNT(*) AS n FROM cases WHERE title = 'M3评测-案件删除'"),
        "documents": _count("SELECT COUNT(*) AS n FROM documents WHERE id = 'eval-del-doc'"),
        "contractReviews": _count("SELECT COUNT(*) AS n FROM contract_reviews WHERE id = 'eval-del-review'"),
        "conversations": _count("SELECT COUNT(*) AS n FROM conversations WHERE id = 'eval-del-conv'"),
        "messages": _count("SELECT COUNT(*) AS n FROM conversation_messages WHERE id = 'eval-del-msg'"),
        "editorSessions": _count("SELECT COUNT(*) AS n FROM editor_sessions WHERE id = 'eval-del-session'"),
    }
    rows.append({"step": "allRelatedRowsRemoved", "ok": all(v == 0 for v in leftovers.values()), "leftovers": leftovers})

    fk_violations = db.query("PRAGMA foreign_key_check")
    rows.append({"step": "foreignKeyCheckClean", "ok": not fk_violations, "violations": len(fk_violations)})

    report["caseDelete"] = {"caseId": cid}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 4：迁移（DB-004 / DB-005）
# ---------------------------------------------------------------------------

def _run_migrations(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.storage import get_db
    from lawbuddy.storage.migrations import MIGRATIONS, apply_migrations

    db = get_db()
    rows: list[dict[str, Any]] = []

    applied = {r["version"] for r in db.query("SELECT version FROM schema_migrations")}
    expected = {v for v, _ in MIGRATIONS}
    rows.append({"step": "allMigrationsApplied", "ok": applied == expected,
                 "applied": len(applied), "expected": len(expected)})

    # 重复执行是幂等的（DB-004：升级后可再次安全启动）
    again = apply_migrations(db)
    rows.append({"step": "migrationsIdempotent", "ok": again == [], "reapplied": again})

    # 校验和被篡改 → 拒绝启动（DB-005），且旧库仍可用
    with db.transaction() as conn:
        conn.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
    tamper_rejected = ""
    try:
        apply_migrations(db)
    except RuntimeError as exc:
        tamper_rejected = str(exc)
    usable = bool(db.query_one("SELECT COUNT(*) AS n FROM cases"))
    rows.append({"step": "tamperRejectedAndDbUsable", "ok": "checksum mismatch" in tamper_rejected and usable,
                 "message": tamper_rejected[:80]})

    # 恢复真实校验和（评测后可继续使用；真实场景由用户恢复备份）
    with db.transaction() as conn:
        from lawbuddy.storage.migrations import _checksum

        conn.execute("UPDATE schema_migrations SET checksum = ? WHERE version = 1",
                     (_checksum(dict(MIGRATIONS)[1]),))
    rows.append({"step": "checksumRestored", "ok": apply_migrations(db) == []})

    report["migrations"] = {"versions": sorted(expected)}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 5：损坏检测（DB-007）
# ---------------------------------------------------------------------------

def _run_corruption(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.storage import get_db
    from lawbuddy.storage.db import Database

    db = get_db()
    rows: list[dict[str, Any]] = []
    src = db.path
    if not src.exists():
        rows.append({"step": "dbFileExists", "ok": False, "path": str(src)})
        return rows, False

    damaged = src.parent / "corrupt-copy.db"
    data = src.read_bytes()
    damaged.write_bytes(data[:4096])  # 截断副本：模拟损坏

    opened_failed = ""
    broken = None
    try:
        broken = Database(damaged, key=_active_key())
        broken.query("SELECT COUNT(*) AS n FROM cases")
    except Exception as exc:  # noqa: BLE001
        opened_failed = f"{type(exc).__name__}"
    finally:
        if broken is not None:
            try:
                broken.close()
            except Exception:  # noqa: BLE001
                pass
    rows.append({"step": "corruptCopyFailsToOpen", "ok": bool(opened_failed), "error": opened_failed})

    # 原库不受影响
    integrity = db.query("PRAGMA integrity_check")
    still_ok = bool(integrity) and str(list(integrity[0].values())[0]).lower() == "ok"
    rows.append({"step": "originalDbUnaffected", "ok": still_ok})
    try:
        damaged.unlink(missing_ok=True)
    except OSError:
        pass  # 损坏副本清理失败不影响结论（Windows 可能短暂占用）

    report["corruption"] = {"probe": opened_failed}
    ok = all(r["ok"] for r in rows)
    return rows, ok


def _active_key() -> bytes | None:
    from lawbuddy.keyring import get_active_dek

    return get_active_dek()


# ---------------------------------------------------------------------------
# 段 6：静态明文扫描（DB-009）
# ---------------------------------------------------------------------------

def _run_plaintext_scan(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents

    rows: list[dict[str, Any]] = []
    root = _workspace_root()
    case = create_case(title="M3评测-明文扫描", case_type="civil", client_name="陈十三", client_role="原告代理人")

    sample = root / "plaintext-eval" / "扫描样本.txt"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(f"{PLAINTEXT_MARKER}：正文内容必须在落盘后不可明文检索到。", encoding="utf-8")
    outcomes = import_documents(case["id"], [str(sample)])
    rows.append({"step": "docImported", "ok": bool(outcomes) and outcomes[0].get("status") == "imported"})

    # 强制 WAL 检查点，确保数据已落主库（模拟应用关闭后的静态扫描）
    from lawbuddy.storage import get_db

    db = get_db()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 强制检查点（模拟应用关闭后的静态扫描）

    marker = PLAINTEXT_MARKER.encode("utf-8")
    hits: list[str] = []
    scanned = 0
    for p in [db.path, Path(str(db.path) + "-wal"), Path(str(db.path) + "-shm")]:
        if p.exists():
            scanned += 1
            if marker in p.read_bytes():
                hits.append(p.name)
    # 内容存储（原件）与临时明文目录
    blobs = root / "blobs"
    if blobs.exists():
        for f in blobs.rglob("*"):
            if f.is_file():
                scanned += 1
                if marker in f.read_bytes():
                    hits.append(str(f.relative_to(root)))
    rows.append({"step": "noPlaintextInDbWalBlobs", "ok": not hits, "scanned": scanned, "hits": hits})

    # 数据库内也不存在明文列（解密后查询才可读到，说明是密文落盘）
    text_row = db.query_one(
        "SELECT text_content FROM document_pages WHERE text_content LIKE ? LIMIT 1",
        (f"%{PLAINTEXT_MARKER}%",),
    )
    rows.append({"step": "decryptedReadWorks", "ok": text_row is not None})

    report["plaintextScan"] = {"scanned": scanned, "hits": hits}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 7：单实例（DB-010，静态检查）
# ---------------------------------------------------------------------------

def _run_single_instance(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    src = MAIN_TS.read_text(encoding="utf-8")
    checks = {
        "singleInstanceLockAcquired": "app.requestSingleInstanceLock()" in src,
        "secondInstanceHandled": "second-instance" in src,
        "secondInstanceSkipsCore": "if (!singleInstance) return" in src,
        "secondInstanceQuits": "app.quit()" in src,
    }
    for name, ok in checks.items():
        rows.append({"step": f"main:{name}", "ok": bool(ok)})
    report["singleInstance"] = checks
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 8：加密备份与恢复（DB-006）
# ---------------------------------------------------------------------------

BACKUP_PASSPHRASE = "eval-backup-passphrase-2026"  # 评测专用口令（非真实用户口令）


def _run_backup_restore(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.storage import backup as bk

    rows: list[dict[str, Any]] = []
    backup_dir = tmp / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 在线备份：不停机，源库不受影响
    created = bk.create_backup(str(backup_dir), passphrase=BACKUP_PASSPHRASE)
    db_copy = Path(created["dbPath"])
    rows.append({"step": "backupCreated", "ok": db_copy.exists() and Path(created["keyFilePath"]).exists()
                 and Path(created["manifestPath"]).exists(),
                 "sizeBytes": created["manifest"]["dbSizeBytes"], "counts": len(created["manifest"]["counts"])})
    rows.append({"step": "recoveryKeyNotReturnedWithPassphrase", "ok": created["recoveryKey"] is None})

    # 副本不含明文（静态扫描同 DB-009 标记）
    raw = db_copy.read_bytes()
    leaked = PLAINTEXT_MARKER.encode("utf-8") in raw
    key_raw = Path(created["keyFilePath"]).read_text(encoding="utf-8")
    rows.append({"step": "backupCiphertextOnly", "ok": not leaked and BACKUP_PASSPHRASE not in key_raw})

    # 逐项一致：完整性 + 数量 + 抽样哈希 + 权限 + 审计链（与源库比对）
    verified = bk.verify_backup(created["dbPath"], created["keyFilePath"], BACKUP_PASSPHRASE)
    failed = [c["name"] for c in verified["checks"] if not c["ok"]]
    rows.append({"step": "backupMatchesLive100pct", "ok": bool(verified["ok"]) and not failed,
                 "checks": [c["name"] for c in verified["checks"]], "failed": failed,
                 "auditTotal": verified["counts"].get("audit_logs")})

    # 口令错误 → 拒绝（不泄露细节）
    wrong = ""
    try:
        bk.verify_backup(created["dbPath"], created["keyFilePath"], "wrong-passphrase-000")
    except bk.BackupError as exc:
        wrong = str(exc)
    rows.append({"step": "wrongPassphraseRejected", "ok": "口令错误" in wrong or "损坏" in wrong, "message": wrong[:40]})

    # 副本被篡改/截断 → 校验失败
    tampered = backup_dir / "tampered.db"
    tampered.write_bytes(db_copy.read_bytes()[:4096])
    tamper_rejected = ""
    try:
        bk.verify_backup(str(tampered), created["keyFilePath"], BACKUP_PASSPHRASE)
    except bk.BackupError as exc:
        tamper_rejected = str(exc)
    tampered.unlink(missing_ok=True)
    rows.append({"step": "tamperedCopyRejected", "ok": bool(tamper_rejected), "message": tamper_rejected[:40]})

    # 新机恢复：还原到新工作空间目录 → DEK 用本机密钥环重新包装 → 与清单逐项一致
    target_root = tmp / "restored-ws"
    restored = bk.restore_backup(
        created["dbPath"], created["keyFilePath"], BACKUP_PASSPHRASE, str(target_root),
    )
    dek = bk.unwrap_dek(json.loads(Path(created["keyFilePath"]).read_text(encoding="utf-8")), BACKUP_PASSPHRASE)
    check = bk.verify_restored(restored["dbPath"], dek, expected_manifest=created["manifest"])
    dek_file = target_root / ".lawbuddy" / "dek.key"
    rows.append({"step": "restoredCountsMatchManifest", "ok": bool(check["ok"]) and check["integrity"] == "ok",
                 "cases": check["counts"].get("cases"), "audit": check["auditChain"].get("total")})
    rows.append({"step": "dekRewrappedForNewMachine", "ok": restored["dekRewrapped"] and dek_file.exists()
                 and dek_file.read_text(encoding="ascii").startswith("enc:v1:")})
    rows.append({"step": "restoredAuditChainIntact", "ok": bool(check["auditChain"].get("ok")),
                 "tail": str(check["auditChain"].get("tailHash"))[:16]})

    # 覆盖保护：目标已存在数据库时默认拒绝
    overwrite_rejected = ""
    try:
        bk.restore_backup(created["dbPath"], created["keyFilePath"], BACKUP_PASSPHRASE, str(target_root))
    except bk.BackupError as exc:
        overwrite_rejected = str(exc)
    rows.append({"step": "restoreRefusesOverwriteByDefault", "ok": "已有数据库" in overwrite_rejected})

    report["backupRestore"] = {"dbCopy": db_copy.name, "checks": len(verified["checks"]),
                               "cases": check["counts"].get("cases")}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m3_db_reliability",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "local_deterministic",
        "gaps": [
            "DB-002 强杀恢复：本评测以并发写入 + integrity_check 覆盖，真实强杀（kill -9）在 E2E 阶段验证",
        ],
    }
    try:
        ensure_workspace(str(tmp / "ws"))

        g1 = _seg(report, "constraints", _run_constraints)
        g2 = _seg(report, "concurrency", _run_concurrency)
        g3 = _seg(report, "transactions", _run_transactions)
        g3b = _seg(report, "caseDelete", _run_case_delete)
        g4 = _seg(report, "migrations", _run_migrations)
        g5 = _seg(report, "corruption", _run_corruption)
        g6 = _seg(report, "plaintextScan", _run_plaintext_scan)
        g7 = _seg(report, "singleInstance", _run_single_instance)
        g8 = _seg(report, "backupRestore", _run_backup_restore, tmp)

        gates = {
            "constraintsDB001": g1,
            "concurrencyDB002": g2,
            "transactionsDB003": g3,
            "caseDeleteFk": g3b,
            "migrationsDB004DB005": g4,
            "corruptionDB007": g5,
            "plaintextDB009": g6,
            "singleInstanceDB010": g7,
            "backupRestoreDB006": g8,
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · SQLite 与数据可靠性（{r['mode']}） ===")
    titles = {
        "constraints": "约束(DB-001)", "concurrency": "并发与完整性(DB-002)", "transactions": "事务故障(DB-003)", "caseDelete": "案件删除外键", "migrations": "迁移(DB-004/DB-005)", "corruption": "损坏检测(DB-007)",
        "plaintextScan": "静态明文扫描(DB-009)", "singleInstance": "单实例(DB-010)",
        "backupRestore": "加密备份与恢复(DB-006)",
    }
    for seg, title in titles.items():
        entry = r.get(seg, {})
        print(f"{title}：{'通过' if entry.get('ok') else '未通过'}")
        for row in entry.get("rows", []):
            print(f"  {'✓' if row.get('ok') else '✗'} {row.get('step')}: "
                  f"{json.dumps({k: v for k, v in row.items() if k not in ('step', 'ok')}, ensure_ascii=False)}")
        if entry.get("error"):
            print(f"  ✗ 段异常：\n{entry['error']}")
    print("门槛判定：")
    for g, ok in r.get("gates", {}).items():
        print(f"  {'✓' if ok else '✗'} {g}")
    print(f"结论：{'M3 数据可靠性评测通过' if r.get('passed') else 'M3 数据可靠性评测未通过（见上方 ✗ 项）'}")
    if r.get("gaps"):
        print("范围说明与已知缺口：")
        for g in r["gaps"]:
            print(f"  · {g}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M3 SQLite 与数据可靠性评测")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_db_reliability_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()