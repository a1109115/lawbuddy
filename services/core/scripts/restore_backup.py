# -*- coding: utf-8 -*-
"""离线恢复脚本（DEV-FND-006 / DB-006 / 技术方案 §19.2 回滚演练）。

用法（services/core 目录下，**必须先关闭应用**）：

    python scripts/restore_backup.py --backup <备份目录> --target <目标工作空间目录>

要点：
- 恢复口令经交互输入（`getpass`），**不得**作为命令行参数、环境变量或写入任何文件；
- 目标工作空间已有数据库时默认拒绝覆盖，需显式 `--overwrite`；
- 恢复后自动校验：副本完整性、审计链与清单一致性（与备份时逐项比对）。

退出码：0 = 恢复并校验通过；非 0 = 失败（不改动目标目录或明确提示已回滚）。
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lawbuddy.storage import BackupError, restore_backup, verify_restored  # noqa: E402
from lawbuddy.storage.backup import unwrap_dek  # noqa: E402


def _pick_backup_files(backup_dir: Path) -> tuple[Path, Path, Path | None]:
    """在备份目录中挑选最新的 .db 副本与其配套 .key.json / .manifest.json。"""
    copies = sorted(backup_dir.glob("lawbuddy-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not copies:
        raise BackupError(f"备份目录中没有副本文件：{backup_dir}")
    db = copies[0]
    key_file = db.with_suffix(".key.json")
    if not key_file.exists():
        raise BackupError(f"缺少配套恢复密钥文件：{key_file.name}")
    manifest = db.with_suffix(".manifest.json")
    return db, key_file, manifest if manifest.exists() else None


def main() -> None:
    parser = argparse.ArgumentParser(description="离线恢复加密备份到目标工作空间（口令交互输入）")
    parser.add_argument("--backup", required=True, help="备份目录（包含 lawbuddy-*.db 与 .key.json）")
    parser.add_argument("--target", required=True, help="目标工作空间目录（数据库将写入 <target>/.lawbuddy/）")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖目标已存在的数据库")
    args = parser.parse_args()

    backup_dir = Path(args.backup)
    target_root = Path(args.target)
    if not backup_dir.is_dir():
        print(f"备份目录不存在：{backup_dir}")
        sys.exit(2)

    try:
        db_path, key_file, manifest_path = _pick_backup_files(backup_dir)
    except BackupError as exc:
        print(f"无法定位备份文件：{exc}")
        sys.exit(2)

    print(f"备份副本：{db_path.name}")
    print(f"恢复密钥：{key_file.name}")
    print(f"目标工作空间：{target_root}")
    passphrase = getpass.getpass("请输入恢复口令（不显示、不落盘）：")
    if not passphrase:
        print("未输入口令，已取消")
        sys.exit(2)

    try:
        payload = json.loads(key_file.read_text(encoding="utf-8"))
        dek = unwrap_dek(payload, passphrase)  # 先验证口令，避免误建目录
    except BackupError as exc:
        print(f"恢复失败：{exc}")
        sys.exit(3)

    try:
        result = restore_backup(
            db_path=str(db_path), key_file=str(key_file), passphrase=passphrase,
            target_root=str(target_root), overwrite=args.overwrite,
        )
    except BackupError as exc:
        print(f"恢复失败：{exc}")
        sys.exit(3)

    expected = None
    if manifest_path is not None:
        try:
            expected = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            expected = None

    check = verify_restored(result["dbPath"], dek, expected_manifest=expected)
    print("\n恢复校验：")
    print(f"  · 完整性检查：{check['integrity']}")
    print(f"  · 审计链：{'完整' if check['auditChain'].get('ok') else '损坏'} "
          f"（{check['auditChain'].get('total')} 条，尾哈希 {str(check['auditChain'].get('tailHash'))[:12]}…）")
    for key in ("cases", "documents", "document_versions", "permission_grants", "audit_logs"):
        if key in check["counts"]:
            print(f"  · {key}：{check['counts'][key]}")
    if expected:
        mismatch = {t: (v, check["counts"].get(t)) for t, v in (expected.get("counts") or {}).items()
                    if check["counts"].get(t) != v}
        if mismatch:
            print(f"  ✗ 与备份清单不一致：{mismatch}")
            sys.exit(4)
        print("  ✓ 与备份清单逐项一致")

    if not check["ok"]:
        sys.exit(4)

    print("\n恢复完成。请启动应用并确认案件、引用与权限与备份时一致；")
    print(f"恢复密钥已用本机密钥环重新包装：{target_root / '.lawbuddy'}")
    sys.exit(0)


if __name__ == "__main__":
    main()