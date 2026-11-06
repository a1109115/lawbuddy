"""SQLCipher 可行性验证（DEC-004 落定前置检查）。

验证项：
1. sqlcipher3 可导入，SQLCipher 版本可读；
2. PRAGMA key 生效，错误密钥无法打开；
3. FTS5 与 trigram 分词在 SQLCipher 构建中可用（pages_fts 依赖）；
4. 关闭后磁盘文件不含明文正文（DB-009 门禁基础）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SECRET = "被告人张三于二〇二四年五月十二日签署工程保证金协议"


def main() -> int:
    try:
        from sqlcipher3 import dbapi2 as sqlcipher
    except Exception as exc:  # pragma: no cover - 环境缺失时直接失败
        print(f"FAIL import sqlcipher3: {exc}")
        return 1

    print(f"OK  import sqlcipher3 (sqlite {sqlcipher.sqlite_version})")

    tmpdir = Path(tempfile.mkdtemp(prefix="lawbuddy-sqlcipher-"))
    db_path = tmpdir / "enc.db"
    key = "correct-horse-battery-staple"

    # ---- 建库 + FTS5 + 写入 -------------------------------------------------
    con = sqlcipher.connect(str(db_path))
    con.execute(f"PRAGMA key = '{key}'")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("CREATE TABLE pages(id INTEGER PRIMARY KEY, text_content TEXT)")
    try:
        con.execute(
            "CREATE VIRTUAL TABLE pages_fts USING fts5(text_content, tokenize = 'trigram')"
        )
    except sqlite3.OperationalError as exc:
        print(f"FAIL fts5/trigram unavailable: {exc}")
        return 1
    print("OK  fts5 + trigram available")

    con.execute("INSERT INTO pages(text_content) VALUES (?)", (SECRET,))
    con.execute("INSERT INTO pages_fts(text_content) VALUES (?)", (SECRET,))
    con.commit()
    hit = con.execute(
        "SELECT count(*) FROM pages_fts WHERE pages_fts MATCH ?", ("保证金",)
    ).fetchone()[0]
    print(f"OK  fts match rows = {hit}")
    con.close()

    # ---- 错误密钥必须打不开 --------------------------------------------------
    bad = sqlcipher.connect(str(db_path))
    bad.execute("PRAGMA key = 'wrong-key'")
    try:
        bad.execute("SELECT count(*) FROM pages").fetchone()
        print("FAIL wrong key opened the database")
        return 1
    except Exception:
        print("OK  wrong key rejected")
    finally:
        bad.close()

    # ---- 正确密钥可读回 ------------------------------------------------------
    good = sqlcipher.connect(str(db_path))
    good.execute(f"PRAGMA key = '{key}'")
    back = good.execute("SELECT text_content FROM pages").fetchone()[0]
    assert back == SECRET, "roundtrip mismatch"
    print("OK  correct key roundtrip")
    good.close()

    # ---- 静态明文扫描（DB-009） ---------------------------------------------
    leaked: list[str] = []
    for f in tmpdir.rglob("*"):
        if not f.is_file():
            continue
        blob = f.read_bytes()
        for needle in (SECRET.encode("utf-8"), "保证金".encode("utf-8")):
            if needle in blob:
                leaked.append(f"{f.name}: {needle[:9]!r}")
    if leaked:
        print(f"FAIL plaintext leaked on disk: {leaked}")
        return 1
    print("OK  no plaintext on disk (db + wal + shm)")

    for f in sorted(tmpdir.rglob("*")):
        if f.is_file():
            print(f"    {f.name:24} {f.stat().st_size:>8} B")
    print(f"\nartifacts dir: {tmpdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
