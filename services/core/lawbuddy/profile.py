"""设备级配置库（profile）：与案件工作空间分离的固定位置存储（REQ-CASE-004）。

模型服务商 / 模型目录 / API Key、MCP 连接器（含认证头）、已安装技能都属于
「本机设备」配置，不属于某一个案件工作空间：

- 固定位置存放（Electron 传入 `LAWBUDDY_PROFILE_DIR`，默认落在用户应用数据目录
  下的 `profile/`），切换 / 新建工作空间时不会丢失，也不会被写进案件目录；
- 独立加密：`profile.db` 是 SQLCipher 加密库，DEK 由系统密钥环包装后落在
  `<profile>/.lawbuddy/dek.key`，与工作空间 DEK 互不复用（fail-closed，不落明文）；
- 工作空间库只保留案件、文档、会话、产物等任务资料。

首次创建 profile 时执行一次「随包种子导入」（技能文件 + MCP 清单）与
「旧工作空间配置迁移」（此前模型配置/连接器/技能存在工作空间库里）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

PROFILE_DB_FILENAME = "profile.db"
SKILLS_DIRNAME = "skills"
MIN_FREE_BYTES = 200 * 1024 * 1024  # 设备级配置体积很小，只做基本可用性检查

_override_dir: Path | None = None


class ProfileError(RuntimeError):
    pass


def _default_dir() -> Path:
    """无 Electron 传参时的兜底目录（裸跑 Core / 脚本场景）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "LawBuddy" / "profile"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LawBuddy" / "profile"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "lawbuddy" / "profile"


def _legacy_default_dir() -> Path:
    """改名前的兜底目录（旧版 Slvyue），用于首次启动时迁移设备级配置。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Slvyue" / "profile"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Slvyue" / "profile"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "slvyue" / "profile"


def _migrate_legacy_dir(root: Path) -> None:
    """新目录不存在而旧目录存在时整体搬一次，避免模型配置与 API Key 配置丢失。"""
    if root.exists():
        return
    legacy = _legacy_default_dir()
    if not legacy.is_dir():
        return
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(root))
    except OSError:
        # 搬迁失败按未配置处理：用户可在设置中重新配置，不落明文、不覆盖旧目录
        return


def profile_dir() -> Path:
    """设备级配置目录：显式设置 > 环境变量 > 平台默认。"""
    if _override_dir is not None:
        return _override_dir
    env = os.environ.get("LAWBUDDY_PROFILE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    target = _default_dir()
    # 改名兼容：旧版 Slvyue 目录存在而新目录不存在时整目录搬一次
    _migrate_legacy_dir(target)
    return target


def set_profile_dir(path: str | Path | None) -> None:
    """重定向设备级配置目录（评测隔离 / 多实例部署使用）。"""
    global _override_dir
    _override_dir = Path(path).expanduser().resolve() if path else None


def profile_root() -> Path:
    """已初始化的设备级配置根目录。"""
    from .storage import get_profile_db

    return get_profile_db().path.parent.resolve()


def skills_root() -> Path:
    """设备级技能目录（`<profile>/skills/**/SKILL.md`）。"""
    return profile_root() / SKILLS_DIRNAME


def profile_db_path() -> Path:
    return profile_dir() / PROFILE_DB_FILENAME


def ensure_profile(
    seed_dir: str | Path | None = None,
    legacy_db: Any = None,
    legacy_root: Path | None = None,
) -> dict[str, Any]:
    """初始化或打开设备级配置库；首次创建时导入种子并迁移旧工作空间配置。

    legacy_db / legacy_root：旧版本把模型配置、连接器、技能存在工作空间库与其
    `skills/` 目录里；首次创建 profile 时一次性搬过来，避免用户升级后配置丢失。
    迁移只在 profile 首次创建时执行，之后以 profile 为准，不反向覆盖。

    补充兜底：迁移按「来源工作空间」记录（`profile.migratedFrom` 存路径列表），
    同一个旧工作空间只迁一次；升级后先打开了别的空工作空间、或换了新目录时，
    后续打开真正带配置的旧工作空间仍会补迁一次，不会让旧配置永久卡在原处。
    """
    from . import keyring
    from .storage import ensure_encrypted, init_profile_database

    root = profile_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProfileError(f"设备级配置目录不可写：{exc.strerror or exc}") from exc
    free = shutil.disk_usage(root).free
    if free < MIN_FREE_BYTES:
        raise ProfileError(f"磁盘剩余空间不足（需 ≥200MB，当前 {free // (1024 * 1024)}MB）")

    db_path = root / PROFILE_DB_FILENAME
    fresh = not db_path.exists()
    # 密钥环不可用时在此阻断（fail-closed），不落明文配置
    dek = keyring.load_or_create_dek(root)
    ensure_encrypted(db_path, dek)
    db = init_profile_database(db_path, dek)

    stats: dict[str, Any] = {"created": fresh}
    # 顺序要紧：先迁移旧工作空间配置，再导入随包种子。种子里的 MCP 服务/技能会
    # 让 profile 看起来「已有配置」，先导种子会掩盖真正的旧配置。
    # 迁移按来源工作空间去重：同一个旧工作空间不会重复导入（避免用户在新 profile
    # 里删掉的旧配置被再次搬回来），但不同旧工作空间各自仍能补迁一次。
    migrated: dict[str, int] = {}
    already = _migrated_roots(db)
    legacy_key = str(_resolved(legacy_root))
    if legacy_key and legacy_key not in already:
        migrated = _migrate_legacy(db, legacy_db, legacy_root, root)
        stats.update(migrated)
        if any(migrated.get(k) for k in ("migratedSettings", "migratedSkills")):
            _set_migration_marker(db, legacy_root, already)
    if fresh:
        stats.update(_import_seed(root, Path(seed_dir) if seed_dir else None))

    if fresh or any(v for v in migrated.values()):
        payload = {k: v for k, v in stats.items() if isinstance(v, int)}
        if any(payload.values()):
            from . import audit

            audit.log_standalone_profile(
                action="profile.initialize",
                resource_type="profile",
                resource_id=str(root),
                metadata=payload,
            )
    return stats


def reset_profile() -> None:
    """关闭 profile 连接（评测/切换环境用；不删除磁盘数据）。"""
    from .storage import reset_profile_database

    reset_profile_database()


# ---------------------------------------------------------------------------
# 首次创建：旧工作空间配置迁移
# ---------------------------------------------------------------------------

_MIGRATION_MARKER_KEY = "profile.migratedFrom"


def _resolved(path: Path | None) -> str:
    """规范化路径用于比较（大小写/短长名差异不应造成重复迁移）。"""
    if path is None:
        return ""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def _migrated_roots(db: Any) -> set[str]:
    """已迁移过配置的旧工作空间根目录集合（只存路径，不含配置值）。"""
    row = db.query_one("SELECT value_json FROM settings WHERE key = ?", (_MIGRATION_MARKER_KEY,))
    if row is None:
        return set()
    try:
        data = json.loads(row["value_json"])
    except (TypeError, ValueError):
        return set()
    if isinstance(data, str):  # 兼容早期单值写法
        return {data} if data else set()
    if isinstance(data, list):
        return {str(v) for v in data if str(v or "").strip()}
    return set()


def _set_migration_marker(db: Any, legacy_root: Path | None, roots: set[str]) -> None:
    from .workspace import now_iso

    value = sorted({*roots, _resolved(legacy_root)} - {""})
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (_MIGRATION_MARKER_KEY, json.dumps(value, ensure_ascii=False), now_iso()),
        )


def _migrate_legacy(db: Any, legacy_db: Any, legacy_root: Path | None, root: Path) -> dict[str, int]:
    """把旧工作空间库里的设备级配置搬到 profile（只做一次）。"""
    stats = {"migratedSettings": 0, "migratedSkills": 0, "migratedSkillFiles": 0}
    if legacy_db is None:
        return stats

    # 1) 设置：只搬设备级前缀（model.* 及其他非任务级键由调用方保证）
    try:
        rows = legacy_db.query("SELECT key, value_json, updated_at FROM settings")
    except Exception:  # noqa: BLE001 — 旧库结构异常不阻断 profile 初始化
        rows = []
    settings = [r for r in rows if _is_device_setting(str(r["key"]))]
    if settings:
        with db.transaction() as conn:
            for r in settings:
                conn.execute(
                    "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO NOTHING",
                    (r["key"], r["value_json"], r["updated_at"]),
                )
        stats["migratedSettings"] = len(settings)

    # 2) 技能行 + 技能文件目录
    try:
        skills = legacy_db.query("SELECT * FROM skills")
    except Exception:  # noqa: BLE001
        skills = []
    if skills:
        with db.transaction() as conn:
            for r in skills:
                conn.execute(
                    "INSERT INTO skills(id, name, description, source_url, prompt_md, allowed_tools_json,"
                    " content_sha256, enabled, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO NOTHING",
                    (r["id"], r["name"], r["description"], r["source_url"], r["prompt_md"],
                     r["allowed_tools_json"] or "[]", r["content_sha256"], r["enabled"],
                     r["created_at"], r["updated_at"]),
                )
        stats["migratedSkills"] = len(skills)

    if legacy_root is not None:
        src = Path(legacy_root) / SKILLS_DIRNAME
        if src.is_dir():
            dst = root / SKILLS_DIRNAME
            for path in sorted(src.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                target = dst / path.relative_to(src)
                if target.exists():
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    if path.name == "SKILL.md":
                        stats["migratedSkillFiles"] += 1
                except OSError:
                    continue
    return stats


def _is_device_setting(key: str) -> bool:
    """设备级设置键：模型服务商/目录/密钥、Agent 并发等本机偏好。

    案件相关的运行态不在此列；`model.*` 是最关键的一类（切空间后必须还在）。
    判定直接复用 `agent.tasks.is_device_setting`，避免迁移过滤与读写路由两处
    各维护一份前缀清单后发生漂移。
    """
    from .agent.tasks import is_device_setting

    return is_device_setting(key)


# ---------------------------------------------------------------------------
# 首次创建：随包种子导入
# ---------------------------------------------------------------------------

def _import_seed(root: Path, seed_dir: Path | None) -> dict[str, int]:
    """导入随包分发的技能文件（不覆盖既有数据，不阻断初始化）。

    seed/skills/** 整树拷入 `<profile>/skills/**`。
    """
    stats = {"seedSkillFiles": 0, "seedSkillErrors": 0}
    if seed_dir is None or not seed_dir.is_dir():
        return stats

    src_skills = seed_dir / SKILLS_DIRNAME
    if src_skills.is_dir():
        dst_skills = root / SKILLS_DIRNAME
        for src in sorted(src_skills.rglob("*")):
            if not src.is_file():
                continue
            target = dst_skills / src.relative_to(src_skills)
            if target.exists():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                if src.name == "SKILL.md":
                    stats["seedSkillFiles"] += 1
            except OSError:
                stats["seedSkillErrors"] += 1
    return stats
