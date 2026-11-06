"""打包种子导出：把已安装技能固化到 services/core/seed/。

用途：Windows 打包（npm run dist:win）前运行，产物随 electron-builder
extraResources 分发；安装后首次创建设备级配置库（profile）时导入
（skills/** 拷贝后经 sync_workspace_skills 自动注册）。

设备级配置（技能）固定存放在 profile 目录，不随工作空间切换。

安全边界（AGENTS.md「API Key 与打包安全」）：
- 本脚本不导出任何凭据；技能提示词不包含密钥，可随包分发。

用法（在 services/core 下）：
  .venv/Scripts/python.exe scripts/export_seed.py [--profile <profile目录>] [--out <seed目录>]

默认：--profile 取 LAWBUDDY_PROFILE_DIR，否则取平台应用数据目录下的 LawBuddy/profile；
--out 取 services/core/seed。profile 不存在时告警并退出 0（不阻断打包）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# 脚本位于 services/core/scripts/，把 services/core 加入 import 路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lawbuddy.skills import (  # noqa: E402
    SkillError,
    _build_skill_fields,
    _parse_lenient_page,
    parse_frontmatter,
)
from lawbuddy.storage import ensure_encrypted  # noqa: E402

SNAPSHOT_SUBDIR = "_installed"  # URL 安装技能固化为 seed/skills/_installed/<slug>/SKILL.md
SKILLS_DIRNAME = "skills"
PROFILE_DB_FILENAME = "profile.db"
MAX_DB_SKILLS = 64

_WIN_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _slugify(name: str) -> str:
    slug = _WIN_UNSAFE.sub("_", name.strip())[:60].strip(". ")
    return slug or "skill"


def _default_profile() -> Path:
    from lawbuddy.profile import profile_dir

    return profile_dir()


def _open_db(root: Path):
    """只读打开设备级配置库（profile.db），不注册为进程内单例。

    导出脚本独立运行，若把连接注册成全局 profile 单例，会干扰同一进程内后续
    对其它 profile 目录的访问（评测/打包流程会复用进程）。
    """
    from lawbuddy import keyring
    from lawbuddy.storage import Database, apply_migrations
    from lawbuddy.storage.migrations import PROFILE_MIGRATIONS

    dek = keyring.load_or_create_dek(root)
    db_path = root / PROFILE_DB_FILENAME
    ensure_encrypted(db_path, dek)
    db = Database(db_path, dek)
    apply_migrations(db, PROFILE_MIGRATIONS)
    return db


def export_seed(profile: Path, out: Path) -> dict[str, int]:
    stats: dict[str, int] = {"skillFiles": 0, "skillSnapshots": 0, "skillSkipped": 0}
    db = _open_db(profile)

    # 目标目录：每次全量重建，避免上次导出残留已被卸载的技能 / 服务
    if out.exists():
        shutil.rmtree(out)
    seed_skills = out / SKILLS_DIRNAME
    seed_skills.mkdir(parents=True)

    # 1) 已安装技能（DB 快照，排除工作空间文件同步来的）→ seed/skills/_installed/<slug>/SKILL.md
    #    （同名文件技能让位于快照，与 sync_workspace_skills 的优先级一致）
    rows = db.query(
        "SELECT name, description, source_url, prompt_md, allowed_tools_json, created_at"
        " FROM skills WHERE source_url IS NULL OR source_url NOT LIKE 'workspace://%'"
        " ORDER BY created_at"
    )
    url_names: set[str] = set()
    for row in list(rows)[:MAX_DB_SKILLS]:
        name = str(row["name"] or "").strip()
        prompt_md = str(row["prompt_md"] or "")
        if not name or not prompt_md.strip():
            stats["skillSkipped"] += 1
            continue
        url_names.add(name)
        allowed = json.loads(row["allowed_tools_json"] or "[]")
        lines = [f"name: {name}", f"description: {str(row['description'] or '').strip()[:300]}"]
        if allowed:
            lines.append("allowedTools: [" + ", ".join(str(a) for a in allowed) + "]")
        snapshot = "---\n" + "\n".join(lines) + "\n---\n\n" + prompt_md
        target_dir = seed_skills / SNAPSHOT_SUBDIR / _slugify(name)
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "SKILL.md").write_text(snapshot, encoding="utf-8")
        stats["skillSnapshots"] += 1

    # 2) 设备级技能文件 → seed/skills/ 整树拷贝（同名技能优先时跳过；
    #    . 开头的本机状态文件如 .skills_store_lock.json 不跨机器分发）
    src_skills = profile / SKILLS_DIRNAME
    if src_skills.is_dir():
        for src in sorted(src_skills.rglob("*")):
            if not src.is_file() or src.name.startswith("."):
                continue
            rel = src.relative_to(src_skills)
            target = seed_skills / rel
            if src.name == "SKILL.md":
                try:
                    raw = src.read_text(encoding="utf-8")
                    rel_url = rel.as_posix()
                    try:
                        meta, prompt_md = parse_frontmatter(raw)
                    except SkillError:
                        # 与 sync_workspace_skills 一致：严格 frontmatter 优先，页面兜底
                        meta, prompt_md = _parse_lenient_page(raw, f"workspace://skills/{rel_url}")
                    name, _, _ = _build_skill_fields(meta, prompt_md)
                    if name in url_names:
                        stats["skillSkipped"] += 1
                        continue
                except (OSError, UnicodeDecodeError, SkillError):
                    stats["skillSkipped"] += 1
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            if src.name == "SKILL.md":
                stats["skillFiles"] += 1

    try:
        db.close()
    except Exception:  # noqa: BLE001
        pass
    return stats


def main() -> int:
    core_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=_default_profile())
    parser.add_argument("--out", type=Path, default=core_dir / "seed")
    args = parser.parse_args()

    profile: Path = args.profile
    if not (profile / PROFILE_DB_FILENAME).exists():
        # 不阻断打包，但保底创建空 seed 目录（electron-builder extraResources 需要）
        print(f"[seed] 设备级配置不存在（{profile}），跳过种子导出", flush=True)
        args.out.mkdir(parents=True, exist_ok=True)
        return 0

    stats = export_seed(profile, args.out)
    print(
        f"[seed] 导出完成 → {args.out}\n"
        f"[seed] 技能：文件 {stats['skillFiles']} 个 + 快照 {stats['skillSnapshots']} 个"
        f"（跳过 {stats['skillSkipped']}）",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
