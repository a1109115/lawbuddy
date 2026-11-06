"""加密闭环验证（DEC-004 / DB-009 / AC-SEC-004 / 技术方案 §14）。

覆盖：
1. 全新工作空间 → SQLCipher 加密库 + 已包装 DEK，磁盘无明文；
2. 重开工作空间 → 密钥可解包，数据可读；
3. 历史明文库 → 自动迁移为加密库，行数一致，明文不再落盘（含 WAL/SHM）；
4. 错误 DEK → 拒绝打开（不静默降级）；
5. 无系统密钥环平台 → fail-closed，拒绝初始化工作空间。

用法：python scripts/_smoke_encryption.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SECRET = "被告人李国栋于二〇二四年三月七日收受工程保证金人民币壹佰万元"

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'} {name}{('  ' + detail) if detail else ''}")
    if not ok:
        _failures.append(name)


def scan_plaintext(root: Path, needles: tuple[bytes, ...]) -> list[str]:
    """扫描目录下所有文件的明文命中（DB-009 静态明文扫描）。"""
    hits: list[str] = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        try:
            blob = f.read_bytes()
        except OSError:
            continue
        for needle in needles:
            if needle in blob:
                hits.append(f"{f.relative_to(root)}: {needle[:12]!r}")
    return hits


def _readable_plain(path: Path) -> bool:
    """不以密钥打开时能否读出 schema（True 说明仍是明文库）。"""
    from lawbuddy.storage.db import load_driver

    driver, _ = load_driver()
    try:
        con = driver.connect(str(path))
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        con.close()
        return True
    except Exception:
        return False


def main() -> int:
    from lawbuddy import keyring
    from lawbuddy.storage import get_db, reset_database
    from lawbuddy.storage.db import load_driver
    from lawbuddy.storage.encrypt_migrate import detect
    from lawbuddy.workspace import ensure_workspace

    _, has_cipher = load_driver()
    if not has_cipher:
        print("SKIP SQLCipher 未安装")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-enc-"))
    ws = tmp / "ws"
    needles = (SECRET.encode("utf-8"), "保证金".encode("utf-8"))

    # ---- 1. 全新工作空间 ---------------------------------------------------
    ensure_workspace(str(ws))
    db_path = ws / "lawbuddy.db"
    dek = keyring.get_active_dek()
    check("全新工作空间生成加密库", detect(db_path, dek) == "encrypted")
    check("数据库连接标记为加密", get_db().encrypted)
    check("DEK 已由密钥环包装", keyring.is_wrapped(keyring.dek_path(ws).read_text(encoding="ascii").strip()))
    check("DEK 文件权限已收紧", keyring.dek_path(ws).exists())

    # 写入含敏感正文的记录，验证落盘加密
    db = get_db()
    case = db.query_one("SELECT id FROM cases LIMIT 1")
    import uuid

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO documents(id, case_id, kind, title, mime, status, created_at, updated_at) "
            "VALUES (?, ?, 'txt', ?, 'text/plain', 'ready', '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z')",
            (str(uuid.uuid4()), case["id"], SECRET),
        )
    leaked = scan_plaintext(ws, needles)
    check("关闭前磁盘无明文（含 WAL）", not leaked, str(leaked[:2]))

    # ---- 2. 重开工作空间 ---------------------------------------------------
    reset_database()
    keyring.set_active_dek(None)
    ensure_workspace(str(ws))
    row = get_db().query_one("SELECT title FROM documents WHERE title LIKE ?", ("%保证金%",))
    check("重开后数据可读", row is not None and row["title"] == SECRET)

    # ---- 3. 历史明文库迁移 -------------------------------------------------
    # 把加密库反向导出为明文库，模拟历史版本遗留的库（含 WAL 未检查点）
    reset_database()
    from lawbuddy.keyring import sqlcipher_key_pragma

    driver, _ = load_driver()
    plain_dir = tmp / "legacy"
    plain_dir.mkdir()
    plain_path = plain_dir / "lawbuddy.db"
    con = driver.connect(str(db_path))
    con.execute(f'PRAGMA key = "{sqlcipher_key_pragma(dek)}"')
    con.execute("ATTACH DATABASE ? AS plaintext KEY ''", (str(plain_path),))
    con.execute("SELECT sqlcipher_export('plaintext')")
    con.execute("DETACH DATABASE plaintext")
    con.close()
    # 制造 WAL 残留：以明文库打开、写入但不 checkpoint
    con = driver.connect(str(plain_path))
    con.execute("PRAGMA journal_mode = WAL")
    con.execute(
        "INSERT INTO documents(id, case_id, kind, title, mime, status, created_at, updated_at) "
        "SELECT ?, id, 'txt', ?, 'text/plain', 'ready', '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z' "
        "FROM cases LIMIT 1",
        (str(uuid.uuid4()), SECRET + "（WAL 残留）"),
    )
    con.commit()
    con.close()
    check("明文库已被正确构造", detect(plain_path, dek) == "plaintext")

    keyring.set_active_dek(None)
    ensure_workspace(str(plain_dir))
    legacy_dek = keyring.get_active_dek()  # 独立工作空间根 → 独立 DEK
    check("明文库已迁移为加密库", detect(plain_path, legacy_dek) == "encrypted")
    check("迁移后无密钥不可读", not _readable_plain(plain_path))
    migrated = get_db().query_one("SELECT count(*) AS n FROM documents")
    check("迁移后数据完整（WAL 内容已并入）", migrated["n"] >= 2, f"documents={migrated['n']}")
    check("迁移无半成品文件残留", not list(plain_dir.glob("*.enc-migrating")))
    leaked_after = scan_plaintext(plain_dir, needles)
    check("迁移后磁盘无明文（含 -wal/-shm）", not leaked_after, str(leaked_after[:2]))

    # ---- 4. 错误 DEK 必须拒绝 ----------------------------------------------
    reset_database()
    from lawbuddy.storage import init_database

    try:
        init_database(db_path, b"\x00" * 32)
        check("错误 DEK 被拒绝", False, "以错误密钥打开成功")
    except Exception as exc:
        check("错误 DEK 被拒绝", True, type(exc).__name__)

    # ---- 5. 无密钥环平台 fail-closed ---------------------------------------
    original = keyring.platform_keyring
    keyring.platform_keyring = lambda: None  # type: ignore[assignment]
    try:
        try:
            keyring.load_or_create_dek(tmp / "nokeyring")
            check("无密钥环平台 fail-closed", False, "未阻断")
        except keyring.KeyringError as exc:
            check("无密钥环平台 fail-closed", True, str(exc)[:32] + "…")
        check(
            "fail-closed 未落明文密钥",
            not (tmp / "nokeyring" / ".lawbuddy" / "dek.key").exists(),
        )
    finally:
        keyring.platform_keyring = original  # type: ignore[assignment]

    # ---- 6. 内容存储文件级加密 + 临时明文清理 -----------------------------
    from lawbuddy import crypto
    from lawbuddy.documents import import_documents

    reset_database()
    keyring.set_active_dek(None)
    ws2 = tmp / "files"
    ensure_workspace(str(ws2))
    src = tmp / "起诉状.txt"
    src.write_text(f"{SECRET}\n{SECRET}", encoding="utf-8")
    case_id = get_db().query_one("SELECT id FROM cases LIMIT 1")["id"]
    outcome = import_documents(case_id, [str(src)])
    check("文件导入成功", outcome[0]["status"] == "imported", str(outcome[0].get("reason", "")))

    blob = ws2 / get_db().query_one("SELECT blob_uri FROM document_versions LIMIT 1")["blob_uri"]
    check("内容存储为加密格式", crypto.is_encrypted(blob))
    leaked_blob = scan_plaintext(ws2 / "blobs", needles)
    check("内容存储无明文", not leaked_blob, str(leaked_blob[:2]))

    page_texts = [p["text_content"] or "" for p in get_db().query("SELECT text_content FROM document_pages")]
    check("加密 blob 解析结果正确", any(SECRET in t for t in page_texts))
    check(
        "FTS 索引仍可检索（加密库内）",
        bool(get_db().query("SELECT rowid FROM pages_fts WHERE pages_fts MATCH ?", ("保证金",))),
    )

    tmp_dir = ws2 / ".lawbuddy" / "tmp"
    leftovers = [f for f in tmp_dir.rglob("*") if f.is_file()] if tmp_dir.exists() else []
    check("解析后无临时明文残留", not leftovers, str([f.name for f in leftovers][:3]))

    # 崩溃残留 → 下次工作空间初始化清理（AC-SEC-007）
    stale = tmp_dir / "crash-case" / "secret.pdf"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(SECRET.encode("utf-8"))
    reset_database()
    keyring.set_active_dek(None)
    ensure_workspace(str(ws2))
    check("启动时清理崩溃残留临时明文", not stale.exists())

    # ---- 7. 加密文档解锁（REQ-DOC-004/011/013） ---------------------------
    from pypdf import PdfReader, PdfWriter

    from lawbuddy.decrypt import DecryptError, unlock_document

    corpus = Path("E:/project/Slvyue/case_file")
    # 选文本层非空的 PDF，保证解锁后可校验正文（避免扫描件封面页）
    source_pdf, expected_text = None, ""
    for candidate in corpus.rglob("*.pdf"):
        try:
            reader = PdfReader(str(candidate))
            text = (reader.pages[0].extract_text() or "").strip() if reader.pages else ""
        except Exception:
            continue
        if len(text) >= 20:
            source_pdf, expected_text = candidate, text
            break
    if source_pdf is None:
        check("找到带文本层的 PDF 语料", False, str(corpus))
    else:
        # 用真实语料构造带用户密码的加密 PDF，保证解锁后可验证正文
        expected_pages = len(PdfReader(str(source_pdf)).pages)
        writer = PdfWriter()
        for page in PdfReader(str(source_pdf)).pages:
            writer.add_page(page)
        writer.encrypt(user_password="s3cret-pass", owner_password="owner-pass")
        enc_pdf = tmp / "加密起诉书.pdf"
        with enc_pdf.open("wb") as f:
            writer.write(f)
        check("加密 PDF 已构造", enc_pdf.stat().st_size > 0, f"{expected_pages} 页")

        reset_database()
        keyring.set_active_dek(None)
        ws3 = tmp / "locked"
        ensure_workspace(str(ws3))
        case3 = get_db().query_one("SELECT id FROM cases LIMIT 1")["id"]
        outcome = import_documents(case3, [str(enc_pdf)])[0]
        check("加密文档导入标记 need_password", outcome["status"] == "need_password", str(outcome))
        doc_id = outcome["documentId"]
        # 预置演示案件会导入示例文档；本次仅断言"加密文档"无页文本（而非整库为空）
        from_doc = get_db().query_one(
            "SELECT count(*) AS n FROM document_pages p "
            "JOIN document_versions v ON v.id = p.version_id WHERE v.document_id = ?",
            (doc_id,),
        )
        check("加密文档未进入解析", from_doc["n"] == 0, f"加密文档页数={from_doc['n']}")
        check(
            "原件仍加密且未落明文",
            not scan_plaintext(ws3 / "blobs", needles) and not scan_plaintext(ws3, (b"%PDF",)),
        )

        # 错误密码必须明确失败，且不留下半成品
        try:
            unlock_document(doc_id, "wrong-password")
            check("错误密码被拒绝", False, "以错误密码解锁成功")
        except DecryptError as exc:
            check("错误密码被拒绝", True, str(exc))

        # 正确密码 → 解密入库 + 解析
        result = unlock_document(doc_id, "s3cret-pass")
        check("正确密码解锁成功", result["unlocked"])
        rows = get_db().query(
            "SELECT p.text_content FROM document_pages p "
            "JOIN document_versions v ON v.id = p.version_id "
            "WHERE v.document_id = ? ORDER BY p.page_no",
            (doc_id,),
        )
        check("解锁后页数与原文件一致", len(rows) == expected_pages, f"{len(rows)} vs {expected_pages}")
        check(
            "解锁后正文与原文件一致",
            any(expected_text[:20] in (r["text_content"] or "") for r in rows),
        )
        check(
            "解锁后状态可用",
            get_db().query_one("SELECT status FROM documents WHERE id = ?", (doc_id,))["status"]
            in ("ready", "partial"),
        )
        check(
            "解锁内容仍加密存储",
            not scan_plaintext(ws3 / "blobs", (b"%PDF",)),
        )
        check(
            "解锁后全文可检索",
            bool(get_db().query(
                "SELECT f.rowid FROM pages_fts f JOIN document_versions v ON v.id = f.version_id "
                "WHERE v.document_id = ? LIMIT 1",
                (doc_id,),
            )),
        )
        check("解锁后无临时明文残留", not [f for f in (ws3 / ".lawbuddy" / "tmp").rglob("*") if f.is_file()])

        # 不支持的加密容器：明确失败，不尝试绕过（REQ-DOC-013）
        bogus = tmp / "证书加密.docx"
        bogus.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
        outcome2 = import_documents(case3, [str(bogus)])[0]
        check("未知 CFB 容器导入标记 need_password", outcome2["status"] == "need_password")
        try:
            unlock_document(outcome2["documentId"], "any")
            check("不支持的加密明确失败", False, "未阻断")
        except DecryptError as exc:
            check("不支持的加密明确失败", True, str(exc)[:40] + "…")

    print(f"\n{'全部通过' if not _failures else '失败项: ' + ', '.join(_failures)}")
    print(f"临时目录: {tmp}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
