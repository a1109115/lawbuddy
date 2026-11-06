# -*- coding: utf-8 -*-
"""M4 评测器：本地工作空间真实切换（REQ-CASE-004）。

用法（services/core 目录下）：
    python -m evals.m4_workspace_switch [--keep]

背景：此前桌面端「本地工作空间」只有 UI 假选择器——路径硬编码、Core 没有切换
入口、数据库是进程级单例（workspaces ORDER BY created_at LIMIT 1）。本评测针对
新增的 workspace.open 链路，验证：

1. 切换生效：DB 连接与进程内 DEK 都指向新目录，current_workspace_root 随之变化；
2. 数据隔离：A 空间建的案件不出现在 B 空间，切回 A 仍在（各自独立加密库）；
3. 独立密钥：两个工作空间的 DEK 不同，切换后活动 DEK 跟着换；
4. 非法目标被拒：相对路径 / 指向普通文件的路径不改变当前工作空间；
5. 后台任务未退出时中断切换：suspend 返回 False 时不关闭旧库、不换空间；
6. 失败回滚：新目录初始化中途失败时恢复原工作空间，后续 RPC 仍可用；
7. 设备级配置不随切换丢失：模型服务商/API Key/技能固定存在
   profile 库，切到全新工作空间后依然可用（用户换本地工作空间不得清零配置）；
8. 升级补迁移：profile 已被别的空工作空间建出来时，再打开带配置的旧工作空间
   仍会补迁一次，且同一来源不重复迁移（用户删掉的旧配置不会复活）。

报告：控制台摘要 + evals/reports/m4_workspace_switch_<ts>.json。门槛：全部 gates 通过。
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


def _new_case(title: str) -> dict[str, Any]:
    from lawbuddy.cases import create_case

    return create_case(
        title=title,
        case_type="other",
        client_name="评测委托人",
        default_mode="legal_review",
    )


def _case_titles() -> list[str]:
    from lawbuddy.cases import list_cases

    return sorted(c["title"] for c in list_cases(include_archived=True))


# ---------------------------------------------------------------------------
# 段 1：切换生效 —— DB 路径 / 工作空间根 / 活动 DEK
# ---------------------------------------------------------------------------

def _run_switch_effective(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import keyring
    from lawbuddy.storage import get_db
    from lawbuddy.workspace import current_workspace_root, ensure_workspace, open_workspace

    rows: list[dict[str, Any]] = []
    ok = True

    ws_a = tmp / "ws-a"
    ws_b = tmp / "ws-b"
    ensure_workspace(str(ws_a))
    dek_a = keyring.get_active_dek()
    db_a = get_db().path
    ok &= db_a == ws_a / "lawbuddy.db"

    info = open_workspace(str(ws_b))
    rows.append({"case": "switchToB", "root": info["root_uri"]})
    ok &= Path(info["root_uri"]) == ws_b
    ok &= get_db().path == ws_b / "lawbuddy.db"  # DB 单例已重开
    ok &= current_workspace_root() == ws_b
    dek_b = keyring.get_active_dek()
    ok &= dek_a != dek_b  # 每空间独立 DEK
    rows.append({
        "case": "dbAndKeyFollowSwitch",
        "dbPath": str(get_db().path),
        "dekChanged": dek_a != dek_b,
    })

    back = open_workspace(str(ws_a))
    ok &= Path(back["root_uri"]) == ws_a
    ok &= keyring.get_active_dek() == dek_a  # 切回后复用原 DEK，能解旧库
    ok &= get_db().path == ws_a / "lawbuddy.db"
    rows.append({"case": "switchBackReusesDek", "root": back["root_uri"]})
    return rows, ok


# ---------------------------------------------------------------------------
# 段 2：数据隔离 —— A 空间的案件不出现在 B，切回 A 仍在
# ---------------------------------------------------------------------------

def _run_isolation(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.workspace import open_workspace

    rows: list[dict[str, Any]] = []
    ok = True

    ws_a = tmp / "ws-a"
    ws_b = tmp / "ws-b"

    open_workspace(str(ws_a))
    _new_case("A 空间案件")
    titles_a = _case_titles()
    ok &= "A 空间案件" in titles_a
    rows.append({"case": "caseInA", "titles": titles_a})

    open_workspace(str(ws_b))
    titles_b = _case_titles()
    ok &= "A 空间案件" not in titles_b
    ok &= len(titles_b) > 0  # 新空间有自己的演示案件
    rows.append({"case": "caseNotInB", "titles": titles_b})

    open_workspace(str(ws_a))
    titles_back = _case_titles()
    ok &= "A 空间案件" in titles_back
    rows.append({"case": "caseStillInA", "titles": titles_back})
    return rows, ok


# ---------------------------------------------------------------------------
# 段 3：非法目标被拒 —— 相对路径 / 普通文件路径
# ---------------------------------------------------------------------------

def _run_reject_invalid(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.workspace import WorkspaceError, current_workspace_root, open_workspace

    rows: list[dict[str, Any]] = []
    ok = True
    before = current_workspace_root()

    try:
        open_workspace("relative/dir")
        ok = False
        rows.append({"case": "relativeRejected", "result": "accepted(FAIL)"})
    except WorkspaceError as exc:
        rows.append({"case": "relativeRejected", "message": str(exc)})

    not_dir = tmp / "plain-file"
    not_dir.write_text("x", encoding="utf-8")
    try:
        open_workspace(str(not_dir))
        ok = False
        rows.append({"case": "filePathRejected", "result": "accepted(FAIL)"})
    except WorkspaceError as exc:
        rows.append({"case": "filePathRejected", "message": str(exc)})

    ok &= current_workspace_root() == before
    rows.append({"case": "workspaceUnchanged", "root": str(before)})
    return rows, ok


# ---------------------------------------------------------------------------
# 段 4：后台任务未退出时中断切换
# ---------------------------------------------------------------------------

def _run_busy_blocks_switch(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.workspace import WorkspaceError, current_workspace_root, open_workspace

    rows: list[dict[str, Any]] = []
    ok = True
    before = current_workspace_root()

    runner = AgentRunner.instance()
    original = runner.suspend
    runner.suspend = lambda timeout=10.0: False  # type: ignore[method-assign]  # 模拟任务不肯收尾
    try:
        try:
            open_workspace(str(tmp / "ws-busy"))
            ok = False
            rows.append({"case": "busyRejected", "result": "switched(FAIL)"})
        except WorkspaceError as exc:
            rows.append({"case": "busyRejected", "message": str(exc)})
        ok &= current_workspace_root() == before  # 旧库连接未被关闭
        rows.append({"case": "keptOldWorkspace", "root": str(before)})
    finally:
        runner.suspend = original  # type: ignore[method-assign]
    return rows, ok


# ---------------------------------------------------------------------------
# 段 5：失败回滚 —— 新目录初始化中途失败，恢复到原工作空间
# ---------------------------------------------------------------------------

def _run_rollback(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import workspace as ws_mod
    from lawbuddy.storage import get_db
    from lawbuddy.workspace import WorkspaceError, current_workspace_root, open_workspace

    rows: list[dict[str, Any]] = []
    ok = True
    before = current_workspace_root()

    original = ws_mod.ensure_workspace
    calls: list[str] = []

    def flaky(root_uri: str, name: str = "默认工作空间", seed_dir: str | None = None):
        calls.append(root_uri)
        if Path(root_uri) == (tmp / "ws-broken").resolve():
            raise WorkspaceError("模拟首次初始化失败")
        return original(root_uri, name=name, seed_dir=seed_dir)

    ws_mod.ensure_workspace = flaky  # type: ignore[assignment]
    try:
        try:
            open_workspace(str(tmp / "ws-broken"))
            ok = False
            rows.append({"case": "brokenRejected", "result": "switched(FAIL)"})
        except WorkspaceError as exc:
            rows.append({"case": "brokenRejected", "message": str(exc)})
    finally:
        ws_mod.ensure_workspace = original  # type: ignore[assignment]

    ok &= current_workspace_root() == before
    ok &= get_db().path == Path(before) / "lawbuddy.db"
    # 回滚后 RPC 仍可用（案件列表能查）
    rows.append({"case": "rolledBack", "root": str(before), "titles": len(_case_titles())})
    ok &= len(_case_titles()) > 0
    return rows, ok


# ---------------------------------------------------------------------------
# 段 6：设备级配置跨工作空间保持（模型 / 连接器 / 技能不因换空间丢失）
# ---------------------------------------------------------------------------

def _run_device_config_survives_switch(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import skills
    from lawbuddy.agent import tasks as task_store
    from lawbuddy.profile import profile_db_path, skills_root
    from lawbuddy.workspace import open_workspace

    rows: list[dict[str, Any]] = []
    ok = True

    open_workspace(str(tmp / "cfg-ws-a"))
    profile_before = profile_db_path()

    # 在本机配置模型服务商 + API Key + 技能
    task_store.set_setting("model.provider", "custom-eval")
    task_store.set_setting("model.evalApiKey", "eval-switch-probe-placeholder")
    installed = skills.install_from_text(
        "---\nname: switch-eval-skill\ndescription: 跨空间保持评测\n---\n\n正文。\n",
        source="file://switch.md",
    )

    # 切到全新的本地工作空间（用户换目录）
    open_workspace(str(tmp / "cfg-ws-b"))
    ok &= profile_db_path() == profile_before  # profile 位置不随工作空间变化
    ok &= task_store.get_setting("model.provider") == "custom-eval"
    ok &= task_store.get_setting("model.evalApiKey") == "eval-switch-probe-placeholder"
    ok &= any(s["id"] == installed["id"] for s in skills.list_skills())
    ok &= (profile_before.parent / "skills").resolve() == skills_root().resolve()
    rows.append({
        "case": "deviceConfigKeptAfterSwitch",
        "profile": str(profile_before),
        "skills": len(skills.list_skills()),
    })

    # 由工作空间 skills/ 提升而来的技能同样要跨空间保留：切到另一个还没有
    # skills/ 目录的工作空间时，不能把上一个空间的提升技能当成「文件已删除」清掉
    ws_b_skills = tmp / "cfg-ws-b" / "skills" / "@team" / "ws-promoted-skill"
    ws_b_skills.mkdir(parents=True, exist_ok=True)
    (ws_b_skills / "SKILL.md").write_text(
        "---\nname: ws-promoted-skill\ndescription: 工作空间提升评测\n---\n\n正文。\n",
        encoding="utf-8",
    )
    ok &= any(s["name"] == "ws-promoted-skill" for s in skills.list_skills())
    open_workspace(str(tmp / "cfg-ws-c"))  # 全新空间，没有 skills/ 目录
    kept = [s["name"] for s in skills.list_skills()]
    ok &= "ws-promoted-skill" in kept
    ok &= (skills_root() / "@team" / "ws-promoted-skill" / "SKILL.md").is_file()
    rows.append({"case": "workspacePromotedSkillKeptAfterSwitch", "skills": kept})

    # 清理探针配置，避免影响同进程后续评测
    task_store.set_setting("model.provider", "")
    task_store.set_setting("model.evalApiKey", "")
    for s in skills.list_skills():
        if s["name"] in ("switch-eval-skill", "ws-promoted-skill"):
            skills.remove_skill(s["id"])
    return rows, ok


# ---------------------------------------------------------------------------
# 段 7：升级补迁移（profile 已被空工作空间建出来时，旧配置仍能迁入一次）
# ---------------------------------------------------------------------------

def _run_legacy_config_backfill(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import tasks as task_store
    from lawbuddy.storage import get_db, get_profile_db
    from lawbuddy.workspace import open_workspace

    rows: list[dict[str, Any]] = []
    ok = True

    # 模拟「旧版本工作空间」：设备级配置还写在工作空间库的 settings 里
    legacy_ws = tmp / "legacy-ws"
    open_workspace(str(legacy_ws))
    # 清理前序用例留下的同名设备级键：补迁移以 profile 为准、不覆盖既有值，
    # 残留值会让本段误判为「未迁移」
    with get_profile_db().transaction() as conn:
        conn.execute("DELETE FROM settings WHERE key = 'model.provider'")
    with get_db().transaction() as conn:
        conn.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            ("model.provider", '"legacy-provider"', "2026-01-01T00:00:00+00:00"),
        )
        # 非设备级键必须留在工作空间库里，不能被搬走
        conn.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            ("case.localPref", '"keep"', "2026-01-01T00:00:00+00:00"),
        )

    # 先让 profile 被另一个没有配置的工作空间建出来
    open_workspace(str(tmp / "backfill-empty-ws"))
    ok &= get_profile_db().query_one(
        "SELECT 1 AS v FROM settings WHERE key = 'model.provider'") is None
    rows.append({"case": "emptyProfileFirst", "migrated": False})

    # 再打开旧工作空间：应补迁一次，且非设备级键留在工作空间库
    open_workspace(str(legacy_ws))
    migrated = task_store.get_setting("model.provider")
    local_pref = task_store.get_setting("case.localPref")
    ok &= migrated == "legacy-provider"
    ok &= local_pref == "keep"
    rows.append({"case": "backfilledOnce", "provider": migrated, "localPref": local_pref})

    # 同一来源不重复迁移：用户在新 profile 里删掉后不应被再次搬回来
    with get_profile_db().transaction() as conn:
        conn.execute("DELETE FROM settings WHERE key = 'model.provider'")
    open_workspace(str(legacy_ws))
    ok &= task_store.get_setting("model.provider") is None
    rows.append({"case": "notBackfilledTwice", "provider": task_store.get_setting("model.provider")})
    return rows, ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.storage import reset_database, reset_profile_database
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m4_workspace_switch",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        ensure_workspace(str(tmp / "ws-a"))  # 基准工作空间
        g1 = _seg(report, "switchEffective", _run_switch_effective, tmp)
        g2 = _seg(report, "caseIsolation", _run_isolation, tmp)
        g3 = _seg(report, "rejectInvalidTarget", _run_reject_invalid, tmp)
        g4 = _seg(report, "busyBlocksSwitch", _run_busy_blocks_switch, tmp)
        g5 = _seg(report, "failureRollback", _run_rollback, tmp)
        g6 = _seg(report, "deviceConfigSurvivesSwitch", _run_device_config_survives_switch, tmp)
        g7 = _seg(report, "legacyConfigBackfill", _run_legacy_config_backfill, tmp)

        gates = {
            "switchChangesDbAndDek": g1,
            "casesIsolatedPerWorkspace": g2,
            "invalidTargetRejected": g3,
            "busyTaskBlocksSwitch": g4,
            "failureRollsBack": g5,
            "deviceConfigSurvivesSwitch": g6,
            "legacyConfigBackfill": g7,
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
    print(f"  gates: {report.get('gates')}")
    print(f"  passed: {report.get('passed')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M4 本地工作空间真实切换评测")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m4_workspace_switch_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
