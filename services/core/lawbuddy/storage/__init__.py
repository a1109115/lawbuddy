"""SQLite 存储层（技术方案 §13 / DEV-FND-006）。

Python Core 是 SQLite 唯一写入者（ADR-001）：所有连接集中在此模块，
写操作走串行锁，避免多线程竞争。

两个并列的库：
- 工作空间库（`lawbuddy.db`）：案件、文档、会话、产物等任务资料；
- 设备级配置库（`profile.db`）：模型服务商/目录/API Key、MCP 连接器、
  已安装技能；固定位置，不随工作空间切换而重建。
"""

from .db import (
    Database,
    get_db,
    get_profile_db,
    has_profile_database,
    init_database,
    init_profile_database,
    reset_database,
    reset_profile_database,
)
from .backup import (
    BackupError,
    create_backup,
    restore_backup,
    unwrap_dek,
    verify_backup,
    verify_restored,
    wrap_dek,
)
from .encrypt_migrate import ensure_encrypted, migrate_to_encrypted
from .migrations import PROFILE_MIGRATIONS, apply_migrations

__all__ = [
    "Database",
    "get_db",
    "get_profile_db",
    "has_profile_database",
    "init_database",
    "init_profile_database",
    "reset_database",
    "reset_profile_database",
    "apply_migrations",
    "PROFILE_MIGRATIONS",
    "ensure_encrypted",
    "migrate_to_encrypted",
    "BackupError",
    "create_backup",
    "verify_backup",
    "restore_backup",
    "verify_restored",
    "wrap_dek",
    "unwrap_dek",
]
