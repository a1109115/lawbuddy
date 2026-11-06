# -*- coding: utf-8 -*-
"""M3 评测器：技能安装与 Skill Hub 宽松解析（REQ-AI-003 / REQ-AI-007）。

用法（services/core 目录下）：
    python -m evals.m3_skills [--keep]

安装通道为**本地文本安装**（零网络请求）：评测直接调用
skills.install_from_text，覆盖解析与入库语义。

覆盖：
1. 严格 SKILL.md（frontmatter name/description/allowedTools）正常安装；
2. Skill Hub 清单页（无 frontmatter）：名称回退一级标题、描述回退正文首行；
3. Skill Hub 方言 frontmatter（连字符字段 allowed-tools、未知键、嵌套结构）；
4. 名称回退 URL slug（variant 查询参数）；
5. 拒绝 HTML/XML 响应（防止把网页/错误页误装成技能）；
6. 同名幂等（同内容）与同名冲突（不同内容拒绝）；
7. 列表/移除：技能列表可见、可删除。
8. 工作空间同步：Agent 用沙箱写 skills/**/SKILL.md 后自动注册进列表；
   内容变化就地更新；同名 URL 技能不被覆盖；文件删除随同步移除；
   remove_skill 对 workspace 来源技能连目录删除（不复活）。
9. 任务绑定注入：create_task(skills=[...]) 落库 → _system_prompt 注入技能正文
   与执行要求（显式绑定、路由提问、外部工具映射）；无技能不注入；
   技能禁用后不再注入且 create_task 拒绝绑定（fail-closed）。

报告：控制台摘要 + evals/reports/m3_skills_<ts>.json。门槛：全部 gates 通过。
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

STRICT_SKILL_MD = """---
name: case-review-strict
description: 严格 SKILL.md 格式的阅卷技能
allowedTools: [document_search, document_get_pages]
---

# 阅卷操作指引

按页码分批阅读，结论必须带原文出处。
"""

HUB_MANIFEST_MD = """# 案件阅卷审查（Case File Review）

这是一个技能商店清单页：没有 frontmatter，直接是安装说明与操作指引正文。

## 使用方式

1. 通读卷宗目录；
2. 按文档逐份阅读并记录关键事实；
3. 输出带 sourceRef 的审查报告。
"""

HUB_DIALECT_MD = """---
name: hub-dialect-skill
title: 方言字段技能
allowed-tools:
  - read_file
version: 1.2.0
license: MIT
metadata:
  name: 嵌套名不应覆盖顶层
  author: someone
---

技能商店常见方言：连字符列表字段 + 未知键 + 嵌套结构。
"""

HUB_SLUG_MD = """这是正文第一行，充当描述。

页面没有任何标题与元数据，名称应回退自 URL 的 variant 参数。
"""

HTML_PAGE = """<!DOCTYPE html>
<html><head><title>skill hub</title></head><body><p>网页不是技能文件</p></body></html>
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


def _install(skills, source: str, body: str) -> dict[str, Any]:
    """从本地文本安装（零网络；source 仅用于审计来源标识）。"""
    return skills.install_from_text(body, source=source)


# ---------------------------------------------------------------------------
# 段 1：严格 SKILL.md 与 Skill Hub 清单页都能进入技能列表
# ---------------------------------------------------------------------------

def _run_install_paths(report: dict[str, Any], skills) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    ok = True

    strict = _install(skills, "https://hub.example/skills/strict.md", STRICT_SKILL_MD)
    ok &= strict["name"] == "case-review-strict"
    ok &= strict["allowedTools"] == ["document_search", "document_get_pages"]
    rows.append({"case": "strict", "name": strict["name"], "allowedTools": strict["allowedTools"]})

    manifest = _install(skills, "https://hub.example/miao/skillhub.md?variant=case-file-review", HUB_MANIFEST_MD)
    ok &= manifest["name"] == "案件阅卷审查（Case File Review）"
    ok &= bool(manifest["description"])
    rows.append({"case": "hub_manifest", "name": manifest["name"], "description": manifest["description"][:40]})

    dialect = _install(skills, "https://hub.example/skills/dialect.md", HUB_DIALECT_MD)
    ok &= dialect["name"] == "hub-dialect-skill"
    ok &= dialect["allowedTools"] == ["read_file"]
    rows.append({"case": "hub_dialect", "name": dialect["name"], "allowedTools": dialect["allowedTools"]})

    slug = _install(skills, "https://hub.example/miao/skillhub.md?variant=case-file-review-pro", HUB_SLUG_MD)
    ok &= slug["name"] == "case-file-review-pro"
    rows.append({"case": "hub_slug", "name": slug["name"]})

    listed = skills.list_skills()
    ok &= {s["name"] for s in listed} >= {
        "case-review-strict", "案件阅卷审查（Case File Review）", "hub-dialect-skill", "case-file-review-pro",
    }
    ok &= all(s["sourceUrl"] and s["promptMd"].strip() for s in listed)
    rows.append({"case": "list", "count": len(listed)})
    return rows, ok


# ---------------------------------------------------------------------------
# 段 2：安全拒绝与同名语义
# ---------------------------------------------------------------------------

def _run_reject_and_conflict(report: dict[str, Any], skills) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    ok = True

    # HTML 网页拒绝
    try:
        _install(skills, "https://hub.example/page", HTML_PAGE)
        ok = False
        rows.append({"case": "reject_html", "result": "NOT_REJECTED"})
    except skills.SkillError as exc:
        ok &= "网页" in str(exc) or "Markdown" in str(exc)
        rows.append({"case": "reject_html", "result": "rejected"})

    # 空正文拒绝
    try:
        _install(skills, "https://hub.example/empty", "---\nname: empty-skill\ndescription: x\n---\n   \n")
        ok = False
        rows.append({"case": "reject_empty", "result": "NOT_REJECTED"})
    except skills.SkillError:
        rows.append({"case": "reject_empty", "result": "rejected"})

    # 同名同内容幂等
    again = _install(skills, "https://hub.example/skills/strict.md", STRICT_SKILL_MD)
    ok &= again["id"] == skills.list_skills()[0]["id"] or True  # id 一致性单独断言
    first_strict = next(s for s in skills.list_skills() if s["name"] == "case-review-strict")
    ok &= again["id"] == first_strict["id"]
    rows.append({"case": "idempotent", "id": again["id"]})

    # 同名不同内容拒绝
    try:
        _install(skills, "https://hub.example/skills/strict-v2.md", STRICT_SKILL_MD.replace("带原文出处", "带页码出处"))
        ok = False
        rows.append({"case": "name_conflict", "result": "NOT_REJECTED"})
    except skills.SkillError as exc:
        ok &= "同名" in str(exc)
        rows.append({"case": "name_conflict", "result": "rejected"})

    return rows, ok


# ---------------------------------------------------------------------------
# 段 3：列表/校验/移除闭环
# ---------------------------------------------------------------------------

def _run_list_validate_remove(report: dict[str, Any], skills) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    ok = True

    listed = skills.list_skills()
    target = next(s for s in listed if s["name"] == "case-file-review-pro")

    got = skills.get_skills([target["id"], "nonexistent-id"])
    ok &= len(got) == 1 and got[0]["id"] == target["id"]
    rows.append({"case": "get_skills_skips_missing", "count": len(got)})

    ok &= skills.validate_skills([target["id"], target["id"]]) == [target["id"]]
    rows.append({"case": "validate_dedupe", "result": "ok"})

    ok &= skills.remove_skill(target["id"]) is True
    ok &= skills.remove_skill(target["id"]) is False
    ok &= all(s["id"] != target["id"] for s in skills.list_skills())
    rows.append({"case": "remove", "result": "ok"})

    return rows, ok


# ---------------------------------------------------------------------------
# 段 4：工作空间 skills/**/SKILL.md 自动同步（Agent 沙箱落盘 → 技能列表可见）
# ---------------------------------------------------------------------------

# 复刻真实故障：Agent 照 Skill Hub 安装说明把清单页写到嵌套分组目录
WS_SKILL_BODY = HUB_MANIFEST_MD.replace("案件阅卷审查（Case File Review）", "案卷阅卷与证据分析")


def _run_workspace_sync(report: dict[str, Any], skills, ws_root: Path) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    ok = True

    # Agent 通过沙箱只能写到工作空间内；同步时会提升到设备级技能目录
    skill_dir = ws_root / "skills" / "@team" / "case-file-review"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(WS_SKILL_BODY, encoding="utf-8")

    listed = skills.list_skills()
    ws_skill = next((s for s in listed if s["sourceUrl"].startswith("workspace://")), None)
    ok &= ws_skill is not None and ws_skill["name"] == "案卷阅卷与证据分析"
    ok &= bool(ws_skill and ws_skill["promptMd"].strip())
    from lawbuddy.profile import skills_root

    ok &= (skills_root() / "@team" / "case-file-review" / "SKILL.md").is_file()
    rows.append({
        "case": "sync_register",
        "name": ws_skill["name"] if ws_skill else None,
        "sourceUrl": (ws_skill["sourceUrl"][:60] if ws_skill else None),
    })

    # 幂等：再次列表不重复注册
    again = skills.list_skills()
    ok &= sum(1 for s in again if s["name"] == "案卷阅卷与证据分析") == 1
    rows.append({"case": "sync_idempotent", "total": len(again)})

    # 内容变化：同 id 就地更新
    skill_file.write_text(WS_SKILL_BODY + "\n## 核对清单\n\n新增核对清单一节。\n", encoding="utf-8")
    updated = next(s for s in skills.list_skills() if s["name"] == "案卷阅卷与证据分析")
    ok &= updated["id"] == ws_skill["id"] and "核对清单" in updated["promptMd"]
    rows.append({"case": "sync_update", "sameId": updated["id"] == ws_skill["id"]})

    # 同名 URL 技能不被工作空间文件覆盖
    conflict_dir = ws_root / "skills" / "strict-copy"
    conflict_dir.mkdir(parents=True, exist_ok=True)
    (conflict_dir / "SKILL.md").write_text(STRICT_SKILL_MD.replace("带原文出处", "工作空间改写版"), encoding="utf-8")
    strict = next(s for s in skills.list_skills() if s["name"] == "case-review-strict")
    ok &= strict["sourceUrl"].startswith("https://") and "带原文出处" in strict["promptMd"]
    rows.append({"case": "url_not_overwritten", "sourceUrl": strict["sourceUrl"][:40]})

    # 文件删除：无 DB 行残留（同名 URL 技能原样保留）
    shutil.rmtree(conflict_dir)
    after = skills.list_skills()
    ok &= all(not (s["name"] == "case-review-strict" and s["sourceUrl"].startswith("workspace://")) for s in after)
    ok &= next(s for s in after if s["name"] == "case-review-strict")["sourceUrl"].startswith("https://")
    rows.append({"case": "stale_conflict_cleaned", "result": "ok"})

    # remove_skill 对 workspace 技能连目录删除 → 下次同步不复活
    ok &= skills.remove_skill(ws_skill["id"]) is True
    ok &= not skill_file.exists()
    ok &= not skill_dir.parent.exists()  # 清空后的分组父目录一并清理
    ok &= all(s["name"] != "案卷阅卷与证据分析" for s in skills.list_skills())
    rows.append({"case": "remove_removes_dir", "result": "ok"})

    return rows, ok


# ---------------------------------------------------------------------------
# 段 5：任务绑定技能 → system prompt 注入（/ 菜单选中后技能真正生效的端到端语义）
# ---------------------------------------------------------------------------

BINDING_SKILL_MD = """---
name: task-binding-skill
description: 验证任务绑定注入语义的技能
allowedTools: [document_search]
---

# 案卷路由工作流

第一步必须先询问用户身份（原告/被告）与审查意图，再决定后续分支。
"""


def _run_task_binding(report: dict[str, Any], skills) -> tuple[list[dict[str, Any]], bool]:
    # 延迟导入：Core 任务链（cases/tasks/autonomous）仅在评测期使用
    from lawbuddy.cases import create_case
    from lawbuddy.agent.tasks import TaskError, create_task
    from lawbuddy.agent.autonomous import _system_prompt

    rows: list[dict[str, Any]] = []
    ok = True

    # tasks.case_id 外键约束 → 先建案件
    case = create_case("技能绑定评测案", "civil", "评测委托人")
    installed = _install(skills, "https://hub.example/skills/task-binding.md", BINDING_SKILL_MD)
    task = create_task(
        case_id=case["id"],
        mode="legal_review",
        request_text="请帮我分析这个案件的核心争议点是什么？",
        skills=[installed["id"]],
    )
    ok &= task["skills"] == [installed["id"]]
    rows.append({"case": "create_task_binds_skills", "skills": task["skills"]})

    # runner 语义复刻：state["skills"] → get_skills 加载详情 → _system_prompt 注入
    state = {
        "case_title": case["title"],
        "mode": "legal_review",
        "skill_details": skills.get_skills(task["skills"]),
    }
    prompt = _system_prompt(state)
    ok &= "[本次任务绑定的技能：" in prompt
    ok &= "[技能执行要求]" in prompt
    ok &= installed["name"] in prompt
    ok &= "案卷路由工作流" in prompt  # 技能正文整体注入
    ok &= "documents_list" in prompt  # 外部工具 → 本应用等价能力的映射指引
    rows.append({"case": "prompt_injection", "name": installed["name"], "promptLen": len(prompt)})

    # 无技能任务不出现注入段
    plain = _system_prompt({"mode": "legal_review"})
    ok &= "[本次任务绑定的技能：" not in plain
    rows.append({"case": "no_skill_no_injection", "result": "ok"})

    # 禁用后：get_skills 不再返回 → 不注入；create_task 拒绝绑定（fail-closed）
    from lawbuddy.storage import get_profile_db

    with get_profile_db().transaction() as conn:
        conn.execute("UPDATE skills SET enabled = 0 WHERE id = ?", (installed["id"],))
    ok &= skills.get_skills([installed["id"]]) == []
    disabled_prompt = _system_prompt({**state, "skill_details": skills.get_skills([installed["id"]])})
    ok &= installed["name"] not in disabled_prompt
    try:
        create_task(case_id=case["id"], mode="legal_review", request_text="复查", skills=[installed["id"]])
        ok = False
        rows.append({"case": "disabled_rejected", "result": "NOT_REJECTED"})
    except (TaskError, ValueError):  # validate_skills 抛 ValueError，tasks 未包装
        rows.append({"case": "disabled_rejected", "result": "rejected"})

    return rows, ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace
    from lawbuddy import skills

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m3_skills",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "local_text_install",  # 本地文本安装：零网络请求
    }
    try:
        ensure_workspace(str(tmp / "ws"))

        g1 = _seg(report, "installPaths", _run_install_paths, skills)
        g2 = _seg(report, "rejectAndConflict", _run_reject_and_conflict, skills)
        g3 = _seg(report, "listValidateRemove", _run_list_validate_remove, skills)
        g4 = _seg(report, "workspaceSync", _run_workspace_sync, skills, tmp / "ws")
        g5 = _seg(report, "taskBindingPromptInjection", _run_task_binding, skills)

        gates = {
            "strictAndHubInstallREQAI003": g1,
            "rejectHtmlAndNameSemantics": g2,
            "listValidateRemove": g3,
            "workspaceSkillSync": g4,
            "taskBindingPromptInjection": g5,
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
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
    parser = argparse.ArgumentParser(description="M3 技能安装与 Skill Hub 宽松解析评测（模拟抓取）")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_skills_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
