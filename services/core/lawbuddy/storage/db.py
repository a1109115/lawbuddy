"""数据库连接管理：PRAGMA 配置、串行写队列、事务上下文。

安全基线（技术方案 §13.2 / §14）：
- WAL + synchronous=FULL + busy_timeout=5s + secure_delete=ON；
- foreign_keys 强制开启；
- 所有写事务串行执行，失败时整体回滚；
- 整库加密（DEC-004）：优先使用 SQLCipher 驱动，`PRAGMA key` 注入工作空间 DEK；
  无 SQLCipher 时只允许明文库，且拒绝以密钥模式打开（fail-closed）。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..keyring import sqlcipher_key_pragma

_PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA secure_delete = ON",
)


def load_driver() -> tuple[Any, bool]:
    """返回 (dbapi2 驱动, 是否支持加密)。优先 SQLCipher，缺失时回退标准库。"""
    try:
        from sqlcipher3 import dbapi2 as cipher_driver

        return cipher_driver, True
    except ImportError:
        return sqlite3, False


class Database:
    def __init__(self, path: Path, key: bytes | None = None) -> None:
        driver, has_cipher = load_driver()
        if key is not None and not has_cipher:
            raise RuntimeError("SQLCipher 未安装，无法打开加密工作空间（DEC-004）")

        self._path = path
        self._driver = driver
        self._encrypted = key is not None
        # check_same_thread=False：Core 内部线程统一经过 _lock 串行访问
        self._conn = driver.connect(str(path), check_same_thread=False)
        self._conn.row_factory = getattr(driver, "Row", sqlite3.Row)
        self._lock = threading.RLock()

        if key is not None:
            # PRAGMA key 必须是连接上的第一条语句
            self._conn.execute(f'PRAGMA key = "{sqlcipher_key_pragma(key)}"')
            # 立即校验密钥：错误密钥在此抛错，而不是推迟到首次业务查询
            self._conn.execute("SELECT count(*) FROM sqlite_master").fetchone()

        for pragma in _PRAGMAS:
            self._conn.execute(pragma)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def encrypted(self) -> bool:
        return self._encrypted

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """只读查询，返回字典列表。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """单条写入，返回 lastrowid。调用方通常应使用 transaction()。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid or 0

    def raw_connection(self) -> sqlite3.Connection:
        """底层连接（只读校验与在线备份使用；写入必须走 transaction()）。"""
        return self._conn

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        """持有串行写锁但不开启事务。

        用于 SQLite 禁止在事务内执行的语句（如在线备份的 ATTACH / sqlcipher_export /
        DETACH）；持锁期间不会被本进程的其他写入打断。
        """
        with self._lock:
            yield self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """串行写事务：异常时整体回滚（DB-003 事务故障要么全部完成要么全部回滚）。"""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# 模块级单例：工作空间初始化后全局复用
# ---------------------------------------------------------------------------

_db: Database | None = None


def init_database(path: Path, key: bytes | None = None) -> Database:
    """打开（或创建）工作空间数据库并应用迁移。"""
    global _db
    if _db is not None and _db.path == path:
        return _db
    if _db is not None:
        _db.close()
    db = Database(path, key)
    from .migrations import apply_migrations

    try:
        apply_migrations(db)
    except Exception:
        # 迁移失败时必须丢弃连接：若把半初始化连接残留为全局单例，后续
        # init_database 会按 path 短路返回并跳过迁移，把启动期的迁移错误
        # 掩盖成运行期 no such table（DB-005：迁移失败旧库仍可恢复重试）
        db.close()
        raise
    _db = db
    return _db


def get_db() -> Database:
    """获取当前数据库；未初始化时抛出异常（调用方应先 workspace.ensure）。"""
    if _db is None:
        raise RuntimeError("workspace not initialized")
    return _db


def reset_database() -> None:
    """测试与关闭时使用。"""
    global _db
    if _db is not None:
        _db.close()
        _db = None


# ---------------------------------------------------------------------------
# 设备级配置库（profile）：与工作空间库并列的第二个单例
#
# 模型服务商 / 模型目录 / API Key、MCP 连接器（含认证头）、已安装技能属于
# 本机设备配置，放在固定位置（Electron userData/profile），切换工作空间时
# 不随工作空间库重建，因此不会被清空。
# ---------------------------------------------------------------------------

_profile_db: Database | None = None


def init_profile_database(path: Path, key: bytes | None = None) -> Database:
    """打开（或创建）设备级配置库并应用 profile 迁移。"""
    global _profile_db
    if _profile_db is not None and _profile_db.path == path:
        return _profile_db
    if _profile_db is not None:
        _profile_db.close()
    db = Database(path, key)
    from .migrations import PROFILE_MIGRATIONS, apply_migrations

    try:
        apply_migrations(db, PROFILE_MIGRATIONS)
    except Exception:
        db.close()
        raise
    _profile_db = db
    return _profile_db


def get_profile_db() -> Database:
    """获取设备级配置库；未初始化时抛出异常（调用方应先 profile.ensure）。"""
    if _profile_db is None:
        raise RuntimeError("profile not initialized")
    return _profile_db


def has_profile_database() -> bool:
    return _profile_db is not None


def reset_profile_database() -> None:
    """测试与切换时使用（设备级配置本身不随工作空间切换而清空）。"""
    global _profile_db
    if _profile_db is not None:
        _profile_db.close()
        _profile_db = None
