"""工作空间服务（REQ-CASE-001 / REQ-CASE-004，技术方案 §2.1、§14）。

ensure 流程：目录可写检查 → 剩余空间检查 → 密钥环（DEK，fail-closed）→
明文库迁移为 SQLCipher 加密库 → 打开数据库并迁移 → 打开设备级配置库 →
登记工作空间行 → 首次初始化时预置演示案件。

设备级配置（模型服务商/目录/API Key、MCP 连接器、技能）不存于工作空间库，
由 `profile` 模块放在固定位置；工作空间只保存案件、文档、会话与产物。

任一检查失败时抛异常，不留下半初始化数据库（PRD 8.2）。
"""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import ensure_encrypted, init_database

MIN_FREE_BYTES = 500 * 1024 * 1024  # 500MB 空间门槛
DB_FILENAME = "lawbuddy.db"
# 改名前的库文件名：打开旧工作空间时就地改名，避免既有案件「消失」
LEGACY_DB_FILENAME = "slvyue.db"
BLOB_DIRNAME = "blobs"
SKILLS_DIRNAME = "skills"  # 旧版工作空间技能目录名（仅用于迁移存量配置）


class WorkspaceError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_writable(root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".lawbuddy-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise WorkspaceError(f"工作目录不可写：{exc.strerror or exc}") from exc


def _check_space(root: Path) -> None:
    free = shutil.disk_usage(root).free
    if free < MIN_FREE_BYTES:
        raise WorkspaceError(f"磁盘剩余空间不足（需 ≥500MB，当前 {free // (1024 * 1024)}MB）")


def _rename_legacy_db(root: Path) -> None:
    """旧版工作空间库 slvyue.db（含 -wal/-shm）就地改名为 lawbuddy.db。"""
    legacy = root / LEGACY_DB_FILENAME
    current = root / DB_FILENAME
    if not legacy.is_file() or current.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        src = root / f"{LEGACY_DB_FILENAME}{suffix}"
        dst = root / f"{DB_FILENAME}{suffix}"
        if src.exists() and not dst.exists():
            try:
                os.replace(src, dst)
            except OSError:
                return


def ensure_workspace(
    root_uri: str, name: str = "默认工作空间", seed_dir: str | None = None
) -> dict[str, Any]:
    """初始化或打开工作空间，返回工作空间信息。

    顺序：目录可写 → 剩余空间 → 密钥环（fail-closed）→ 明文库迁移为加密库 →
    打开数据库并迁移 → 打开设备级配置库（首次时迁移旧空间配置并导入随包种子）→
    登记工作空间行 → 首次初始化时预置演示案件。
    任一检查失败时抛异常，不留下半初始化数据库（PRD 8.2 / REQ-CASE-004）。
    """
    from . import keyring

    root = Path(root_uri)
    _check_writable(root)
    _check_space(root)
    # 密钥环不可用时在此阻断，不创建数据库（技术方案 §14 第 4 条）
    dek = keyring.load_or_create_dek(root)
    (root / BLOB_DIRNAME).mkdir(exist_ok=True)

    db_path = root / DB_FILENAME
    # 改名兼容：旧版本的 slvyue.db（含 -wal/-shm）就地改名为 lawbuddy.db
    _rename_legacy_db(root)
    # 既有明文库一次性迁移为 SQLCipher 加密库（DEC-004）
    ensure_encrypted(db_path, dek)

    db = init_database(db_path, dek)
    keyring.set_active_dek(dek)
    # 设备级配置库（固定位置）：首次创建时迁移旧工作空间里的模型/连接器/技能，
    # 并导入随包种子；此后切换工作空间不再影响这些配置
    _ensure_device_profile(db, root, seed_dir)
    # 清理上次异常退出遗留的临时明文（技术方案 §11.2 / AC-SEC-007）
    from .tempstore import purge_all_temp

    purge_all_temp(root)

    row = db.query_one("SELECT * FROM workspaces WHERE root_uri = ?", (str(root),))
    first_init = row is None
    if row is None:
        ws_id = str(uuid.uuid4())
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO workspaces(id, name, root_uri, created_at) VALUES (?, ?, ?, ?)",
                (ws_id, name, str(root), now_iso()),
            )
            # 首次初始化记审计（REQ-AUD-001：初始化、归档、外发、删除只追加）
            from . import audit

            audit.append(
                conn,
                action="workspace.initialize",
                resource_type="workspace",
                resource_id=ws_id,
                metadata={"name": name},
            )
        row = db.query_one("SELECT * FROM workspaces WHERE id = ?", (ws_id,))

    # 仅工作空间首次创建时预置内置虚构演示案件（PRD 8.1：示例材料必须来自内置虚构样例）。
    # 不能以「案件表为空」为条件：用户删光任务后每次启动都会重新种入演示案件。
    if first_init:
        from .fixtures import seed_demo_case

        seed_demo_case(row["id"])

    return dict(row)


def current_workspace_root() -> Path | None:
    """当前已打开工作空间的根目录；尚未初始化时返回 None。"""
    from .storage import get_db

    try:
        db = get_db()
    except RuntimeError:
        return None
    row = db.query_one("SELECT root_uri FROM workspaces ORDER BY created_at LIMIT 1")
    if row is None:
        return None
    try:
        return Path(row["root_uri"]).resolve()
    except OSError:
        return Path(row["root_uri"])


def _suspend_background(previous_root: Path | None = None) -> bool:
    """切换工作空间前停止一切仍在写旧库的后台任务。

    返回 False 表示有任务在超时内没有退出——此时库连接不能关闭，
    调用方必须中断切换并提示用户稍后重试（否则在跑线程会写已关闭的连接）。
    """
    ok = True
    from .agent.runner import AgentRunner

    if not AgentRunner.instance().suspend():
        ok = False
    from .ocr.pipeline import OcrManager

    if not OcrManager.instance().suspend():
        ok = False
    if not ok:
        return False
    # 编辑会话与原件预览临时明文都绑定旧工作空间，切换前统一释放（AC-SEC-007）
    try:
        from . import content, editor
        from .ocr.pipeline import release_all_pdf_sources

        editor.close_all_open()
        content.close_all()
        if previous_root is not None:
            release_all_pdf_sources(previous_root)
    except Exception:
        pass
    return True


def open_workspace(
    root_uri: str, name: str = "默认工作空间", seed_dir: str | None = None
) -> dict[str, Any]:
    """切换到指定目录的工作空间（已存在则打开，不存在则初始化）。

    切换语义：停止 Agent / OCR 后台任务并等待其退出 → 释放编辑会话与临时明文 →
    关闭旧库连接、清空进程内 DEK → 按新目录重新走 ensure_workspace（可写 / 空间 /
    密钥环 / 加密库 / 迁移 / 首次种子）。

    后台任务未能退出时中断切换并抛错；切换失败时回滚到原工作空间，
    避免应用停留在「workspace not initialized」状态。
    """
    from . import keyring
    from .storage import reset_database

    target = Path(root_uri)
    if not target.is_absolute():
        raise WorkspaceError("工作空间目录必须是绝对路径")
    resolved_target = target.resolve()

    previous = current_workspace_root()
    if previous is not None and previous == resolved_target:
        # 同目录重复打开：幂等，只补齐迁移 / 首次种子
        return ensure_workspace(str(resolved_target), name=name, seed_dir=seed_dir)

    # 先做最便宜的目标校验：目录不合法时不应先把正在跑的任务停掉再失败
    _check_writable(resolved_target)
    _check_space(resolved_target)

    if not _suspend_background(previous):
        raise WorkspaceError("仍有任务在收尾，请先停止运行中的任务后重试")

    if previous is not None:
        from .tempstore import purge_all_temp

        try:
            purge_all_temp(previous)
        except Exception:
            pass
    reset_database()
    keyring.set_active_dek(None)

    try:
        return ensure_workspace(str(resolved_target), name=name, seed_dir=seed_dir)
    except Exception:
        # 回滚：新目录不可用（不可写 / 空间不足 / 密钥环异常 / 不是本应用库）时
        # 尽力恢复到原工作空间，保证后续 RPC 仍可工作
        try:
            from .ocr.pipeline import OcrManager

            OcrManager.instance().resume_after_switch()
        except Exception:
            pass
        if previous is not None:
            try:
                ensure_workspace(str(previous))
            except Exception:
                pass
        raise


def _ensure_device_profile(db, root: Path, seed_dir: str | None) -> None:
    """打开（必要时创建）设备级配置库。

    设备级配置（模型服务商/模型目录/API Key、MCP 连接器、技能）固定存在应用
    数据目录下的 `profile/`，不属于任何案件工作空间。首次创建时导入随包种子；
    每个旧工作空间首次被打开时把其库里的旧配置补迁一次（按来源路径去重），
    之后切换工作空间不再影响它们，也不会重复导入。
    """
    from . import keyring, profile
    from .storage import has_profile_database

    # 即使 profile 已存在也要调用：ensure_profile 内部按「来源工作空间」去重，
    # 保证升级后首次打开带配置的旧工作空间不会因为 profile 先前被别的空工作
    # 空间建出来而永久丢失配置。
    try:
        profile.ensure_profile(
            seed_dir=seed_dir,
            legacy_db=db,
            legacy_root=root,
        )
    except profile.ProfileError:
        raise
    except keyring.KeyringError:
        raise
    except Exception as exc:  # noqa: BLE001 — profile 已建好但后续导入失败不阻断工作空间
        if not has_profile_database():
            raise WorkspaceError(f"设备级配置初始化失败：{type(exc).__name__}") from exc
        return
