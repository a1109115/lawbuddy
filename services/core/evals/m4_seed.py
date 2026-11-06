"""M4 评测器：打包种子导出与首次初始化导入（技能随包分发）。

用法（services/core 目录下）：
    python -m evals.m4_seed [--keep]

背景：开发与生产工作空间目录分离（DEV-FND-005），DB 又是每空间独立 DEK 的
SQLCipher 加密库，安装包无法直接搬库。分发语义 = 打包前 export_seed 把已装
技能固化到 services/core/seed/，安装后首次创建设备级配置库（profile）时由
profile.ensure_profile(seed_dir=...) 导入。

设备级配置（技能 / 模型配置）固定存放在 profile 目录，不随工作空间切换；
工作空间只保存案件与生成的文档。

覆盖：
1. 导出 → 导入闭环：技能快照 + 技能文件，经 export_seed 固化后在全新
   profile 首次创建时自动出现；
2. 快照固化为 seed/skills/_installed/<slug>/SKILL.md 并能被同步语义读回；
3. 幂等：profile 已存在时不重复导入，列表不重复；
4. 容错：种子目录缺失 / 损坏不阻断 profile / 工作空间初始化。

报告：控制台摘要 + evals/reports/m4_seed_<ts>.json。门槛：全部 gates 通过。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

STRICT_SKILL_MD = """---
name: seed-review-strict
description: 随包分发的技能快照
allowedTools: [document_search, document_get_pages]
---

# 阅卷操作指引

按页码分批阅读，结论必须带原文出处。
"""

WS_SKILL_MD = """# 案卷阅卷与证据分析

这是技能文件：按安装说明写到 skills/ 分组目录的版本。

## 使用方式

逐份阅读卷宗并记录关键事实，输出带 sourceRef 的报告。
"""


def _seg(report: dict[str, Any], name: str, fn, *args) -> bool:
    """执行一个评测段（异常不中断整体，写入 report[name].error）。"""
    try:
        rows, ok = fn(report, *args)
        entry = report.get(name) or {}
        entry.update({"ok": bool(ok), "rows": rows})
        report[name] = entry
        return bool(ok)
    except Exception:  # noqa: BLE001 — 评测器需捕获任意失败留证
        report[name] = {"ok": False, "rows": [], "error": traceback.format_exc(limit=8)}
        return False


def _use_profile(root: Path) -> None:
    """把设备级配置目录指向评测临时目录（避免读写本机真实配置）。"""
    from lawbuddy import profile
    from lawbuddy.storage import reset_profile_database

    reset_profile_database()
    profile.set_profile_dir(root)


# ---------------------------------------------------------------------------
# 段 1：导出 → 首次创建 profile 导入闭环（技能）
# ---------------------------------------------------------------------------

def _run_export_import_roundtrip(
    report: dict[str, Any], skills, export_seed, tmp: Path
) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import profile

    rows: list[dict[str, Any]] = []
    ok = True

    # 1) 造一个"开发机设备级配置"：技能快照 + 技能文件
    dev_profile = tmp / "dev-profile"
    _use_profile(dev_profile)
    profile.ensure_profile()
    installed = skills.install_from_text(STRICT_SKILL_MD, source="file://seed-review.md")
    ok &= installed["name"] == "seed-review-strict"

    group_dir = dev_profile / "skills" / "@team" / "case-review"
    group_dir.mkdir(parents=True, exist_ok=True)
    (group_dir / "SKILL.md").write_text(WS_SKILL_MD, encoding="utf-8")
    ok &= any(s["name"] == "案卷阅卷与证据分析" for s in skills.list_skills())

    # 2) 导出种子（等价于 npm run seed:export 的库内逻辑）
    seed_dir = tmp / "seed"
    stats = export_seed(dev_profile, seed_dir)
    ok &= stats["skillFiles"] == 1 and stats["skillSnapshots"] == 1
    rows.append({"case": "export", **stats})

    # 导出产物结构
    ok &= (seed_dir / "skills" / "@team" / "case-review" / "SKILL.md").is_file()
    snapshot = seed_dir / "skills" / "_installed" / "seed-review-strict" / "SKILL.md"
    ok &= snapshot.is_file()
    ok &= "name: seed-review-strict" in snapshot.read_text(encoding="utf-8")
    rows.append({"case": "seedLayout", "snapshots": stats["skillSnapshots"]})

    # 3) 全新设备（空 profile）+ 首次打开工作空间 → 种子自动导入
    prod_profile = tmp / "prod-profile"
    _use_profile(prod_profile)
    from lawbuddy.workspace import ensure_workspace

    prod_ws = tmp / "prod-ws"
    info = ensure_workspace(str(prod_ws), seed_dir=str(seed_dir))
    ok &= bool(info["id"])

    listed = {s["name"]: s for s in skills.list_skills()}
    ok &= "seed-review-strict" in listed
    ok &= "案卷阅卷与证据分析" in listed
    ok &= listed.get("seed-review-strict", {}).get("sourceUrl", "").startswith(
        "workspace://skills/_installed/"
    )
    ok &= listed.get("案卷阅卷与证据分析", {}).get("sourceUrl") == "workspace://skills/@team/case-review"
    rows.append({
        "case": "firstInitImport",
        "skills": sorted(listed),
        "snapshotSource": listed.get("seed-review-strict", {}).get("sourceUrl"),
    })
    return rows, ok


# ---------------------------------------------------------------------------
# 段 2：幂等——profile 已存在时不重复导入
# ---------------------------------------------------------------------------

def _run_idempotent(report: dict[str, Any], skills, tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.workspace import ensure_workspace

    rows: list[dict[str, Any]] = []
    ok = True

    prod_ws = tmp / "prod-ws"
    info = ensure_workspace(str(prod_ws), seed_dir=str(tmp / "seed"))
    ok &= bool(info["id"])
    ok &= sum(1 for s in skills.list_skills() if s["name"] == "seed-review-strict") == 1
    rows.append({"case": "reopenNoDup", "skills": len(skills.list_skills())})

    # 再次全量导出（模拟重新打包）+ 新建工作空间，设备级配置数量不变
    from export_seed import export_seed

    stats = export_seed(tmp / "dev-profile", tmp / "seed")
    ok &= stats["skillSnapshots"] == 1
    fresh = ensure_workspace(str(tmp / "prod-ws-2"), seed_dir=str(tmp / "seed"))
    ok &= bool(fresh["id"])
    ok &= sum(1 for s in skills.list_skills() if s["name"] == "seed-review-strict") == 1
    rows.append({"case": "reexportReimport", "skills": len(skills.list_skills())})
    return rows, ok


# ---------------------------------------------------------------------------
# 段 3：容错——种子缺失 / 损坏不阻断初始化
# ---------------------------------------------------------------------------

def _run_tolerant(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.workspace import ensure_workspace

    rows: list[dict[str, Any]] = []
    ok = True

    info = ensure_workspace(str(tmp / "ws-noseed"))
    ok &= bool(info["id"])
    rows.append({"case": "missingSeedDir", "result": "ok"})

    bad = tmp / "badseed"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "skills").mkdir(parents=True, exist_ok=True)
    (bad / "skills" / "broken").mkdir(parents=True, exist_ok=True)
    (bad / "skills" / "broken" / "SKILL.md").write_bytes(b"\xff\xfe\x00broken")
    info = ensure_workspace(str(tmp / "ws-bad"), seed_dir=str(bad))
    ok &= bool(info["id"])
    rows.append({"case": "brokenSkillNotBlocking", "result": "ok"})
    return rows, ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy import skills
    from lawbuddy.storage import reset_database, reset_profile_database
    from export_seed import export_seed

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m4_seed",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "local_only",  # 零网络请求
    }
    try:
        g1 = _seg(report, "exportImportRoundtrip", _run_export_import_roundtrip, skills, export_seed, tmp)
        g3 = _seg(report, "idempotentReimport", _run_idempotent, skills, tmp)
        g4 = _seg(report, "tolerantToBadSeed", _run_tolerant, tmp)

        gates = {
            "seedRoundtripREQFND005": g1,
            "noDuplicateOnReopen": g3,
            "badSeedNotBlocking": g4,
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        reset_database()
        reset_profile_database()
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(report: dict[str, Any]) -> None:
    print(f"== {report['evalName']} ==")
    for name, entry in report.items():
        if isinstance(entry, dict) and "ok" in entry:
            print(f"  [{'PASS' if entry['ok'] else 'FAIL'}] {name}")
            for row in entry.get("rows") or []:
                print(f"        {row}")
            if entry.get("error"):
                print(entry["error"])
    gates = report.get("gates") or {}
    print(f"  gates: {gates}")
    print(f"  passed: {report.get('passed')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M4 打包种子导出与首次初始化导入评测")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m4_seed_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
