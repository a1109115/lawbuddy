# -*- coding: utf-8 -*-
"""M3 评测器：预算记账与自主 Loop 收束（03 清单 §6.1 A-002 / A-003 / A-004 / A-013）。

用法（services/core 目录下）：
    python -m evals.m3_budget [--keep]

覆盖（自主 Loop 下预算已降级为纯记账层，不再阻断执行）：
1. A-002 收束语义：Loop 不受预算阻断，仅固定轮数上限与累计工具调用上限为硬限制；
   mock 单步 final 脚本 → turn_complete(reason=final_answer) 且任务正常完成；
2. A-003 无进展记账：连续三轮无新增 → `NO_PROGRESS`（记账层单元语义，不阻断 Loop）；
3. A-004 重复调用记账：同工具同参数第三次拦截（记账层单元语义，不阻断 Loop）；
4. A-013 预算预警与统计：达 80% 预警、token 统计与模型调用记录一致；
5. 预算随任务持久化与 RPC 快照（`agent.budget` 仍可读）。

报告：控制台摘要 + evals/reports/m3_budget_<ts>.json。门槛：全部 gates 通过。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any


def _seg(report: dict[str, Any], name: str, fn, *args) -> bool:
    try:
        rows, ok = fn(report, *args)
        entry = report.get(name) or {}
        entry.update({"ok": bool(ok), "rows": rows})
        report[name] = entry
        return bool(ok)
    except Exception:  # noqa: BLE001
        report[name] = {"ok": False, "rows": [], "error": traceback.format_exc(limit=8)}
        return False


def _wait_terminal(task_id: str, timeout: float = 60.0) -> dict[str, Any]:
    from lawbuddy.agent.tasks import get_task

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = get_task(task_id)
        if task["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS", "CANCELLED", "FAILED"):
            return task
        time.sleep(0.05)
    return get_task(task_id)


def _events(task_id: str, event_type: str) -> list[dict[str, Any]]:
    from lawbuddy.agent.tasks import get_events

    return [e for e in get_events(task_id) if e["type"] == event_type]


# ---------------------------------------------------------------------------
# 段 1：Loop 收束与预算记账（A-002 / A-013）
# ---------------------------------------------------------------------------

def _run_limits(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from evals._loop_mock import ScriptStep, install_mock_gateway

    from lawbuddy.agent import budget
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import create_task
    from lawbuddy.cases import create_case

    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-预算上限", case_type="civil", client_name="甲", client_role="原告代理人")

    # A-002 新语义：自主 Loop 不受预算阻断，仅固定轮数上限与累计工具调用上限为硬限制；
    # mock 单步 final 脚本 → 一轮收束，turn_complete(reason=final_answer)，任务正常完成
    task = create_task(case_id=case["id"], mode="legal_review", request_text="预算上限评测：检索本案材料并形成结论")
    undo = install_mock_gateway([ScriptStep.final("预算上限评测结论：预算不再阻断执行，Loop 自主收束。")])
    try:
        AgentRunner.instance().start(task["id"])
        final_task = _wait_terminal(task["id"])
    finally:
        undo()
    turns = _events(task["id"], "turn_complete")
    last = turns[-1]["payload"] if turns else {}
    rows.append({"step": "finalAnswerWrapUp",
                 "ok": final_task["lifecycle"] == "COMPLETED" and bool(turns)
                 and last.get("reason") == "final_answer" and last.get("rounds") == 1,
                 "lifecycle": final_task["lifecycle"], "turnComplete": len(turns),
                 "reason": last.get("reason"), "rounds": last.get("rounds")})

    # 预警阈值记账：达上限 80% 即预警（A-013，纯记账层单元语义，不阻断 Loop）
    warn_task = create_task(case_id=case["id"], mode="legal_review", request_text="预算预警评测")
    budget.ensure(warn_task["id"], {"maxToolCalls": 5})
    for _ in range(4):
        budget.spend(warn_task["id"], "tool_call")
    warn_snap = budget.snapshot(warn_task["id"])
    rows.append({"step": "warnAt80Percent",
                 "ok": warn_snap["stage"] == "warn" and bool(warn_snap["reasons"]),
                 "stage": warn_snap["stage"], "usedToolCalls": warn_snap["used"].get("tool_call"),
                 "limit": warn_snap["limits"].get("maxToolCalls"),
                 "reason": warn_snap["reasons"][0] if warn_snap["reasons"] else None})
    # 未达上限时不预警、不阻断
    fresh = create_task(case_id=case["id"], mode="legal_review", request_text="预算正常态评测")
    budget.ensure(fresh["id"], {"maxToolCalls": 5})
    budget.spend(fresh["id"], "tool_call")
    rows.append({"step": "noWarnBelowThreshold", "ok": budget.snapshot(fresh["id"])["stage"] == "ok"})

    # 预算台账仍按任务持久化（记账层；Loop 本身不再读写它）
    budget.ensure(task["id"], {"maxToolCalls": 1})
    budget.spend(task["id"], "tool_call")
    snap = budget.snapshot(task["id"])
    rows.append({"step": "budgetPersistedPerTask", "ok": snap["used"].get("tool_call", 0) >= 1
                 and snap["limits"].get("maxToolCalls") == 1,
                 "used": snap["used"], "limits": snap["limits"]})

    report["limits"] = {"taskId": task["id"], "warnTaskId": warn_task["id"]}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 2：重复调用（A-004）
# ---------------------------------------------------------------------------

def _run_repeat(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import budget
    from lawbuddy.agent.tasks import create_task
    from lawbuddy.cases import create_case

    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-重复调用", case_type="civil", client_name="乙", client_role="原告代理人")
    task = create_task(case_id=case["id"], mode="legal_review", request_text="重复调用评测")
    budget.ensure(task["id"])

    first = budget.register_call(task["id"], "document.search", "争议焦点")
    second = budget.register_call(task["id"], "document.search", "争议焦点")
    blocked_reason = ""
    try:
        budget.register_call(task["id"], "document.search", "争议焦点")
    except budget.RepeatedCallBlocked as exc:
        blocked_reason = str(exc)

    other = budget.register_call(task["id"], "document.search", "合同")  # 不同参数不受影响
    rows.append({"step": "thirdRepeatBlocked", "ok": first == 1 and second == 2 and bool(blocked_reason),
                 "first": first, "second": second, "reason": blocked_reason})
    rows.append({"step": "differentArgsNotBlocked", "ok": other == 1})
    rows.append({"step": "repeatCountPersisted",
                 "ok": int(budget.snapshot(task["id"])["used"].get("calls", {}).get("document.search|争议焦点", 0)) >= 2})

    report["repeat"] = {"taskId": task["id"]}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 3：无进展（A-003）
# ---------------------------------------------------------------------------

def _run_no_progress(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import budget
    from lawbuddy.agent.tasks import create_task
    from lawbuddy.cases import create_case

    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-无进展", case_type="civil", client_name="丙", client_role="原告代理人")
    task = create_task(case_id=case["id"], mode="legal_review", request_text="无进展评测")
    budget.ensure(task["id"])

    r1 = budget.note_progress(task["id"], claims_added=0, evidence_added=0)
    r2 = budget.note_progress(task["id"], claims_added=0, evidence_added=0)
    r3 = budget.note_progress(task["id"], claims_added=0, evidence_added=0)
    snap = budget.snapshot(task["id"])
    rows.append({"step": "noProgressAfterThreeRounds", "ok": r1 == "ok" and r2 == "ok" and r3 == "NO_PROGRESS",
                 "rounds": [r1, r2, r3], "noProgressRounds": snap["noProgressRounds"]})
    rows.append({"step": "noProgressBlocksFurtherCalls", "ok": snap["stage"] == "blocked",
                 "stage": snap["stage"], "reason": budget.blocked_reason(task["id"])[:60]})

    # 有新增即重置：再次有进展后计数归零并恢复
    progress = budget.note_progress(task["id"], claims_added=1, evidence_added=2)
    snap2 = budget.snapshot(task["id"])
    rows.append({"step": "progressResetsCounter", "ok": progress == "ok" and snap2["noProgressRounds"] == 0,
                 "noProgressRounds": snap2["noProgressRounds"]})

    report["noProgress"] = {"taskId": task["id"], "blockedReason": budget.blocked_reason(task["id"])[:60]}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 4：统计与快照（A-013）
# ---------------------------------------------------------------------------

def _run_accounting(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import budget
    from lawbuddy.agent.tasks import create_task
    from lawbuddy.cases import create_case
    from lawbuddy.storage import get_db

    db = get_db()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-预算统计", case_type="civil", client_name="丁", client_role="原告代理人")
    task = create_task(case_id=case["id"], mode="legal_review", request_text="预算统计评测")
    budget.ensure(task["id"])

    # 用量记账只在预算侧（task_budget）：model_calls 计费账本已随计费体系移除
    from lawbuddy.agent.tasks import get_task

    case_id = get_task(task["id"])["caseId"]
    calls = [
        budget.spend(task["id"], "model_call", tokens=1500),
        budget.spend(task["id"], "model_call", tokens=2500),
    ]
    snap = calls[-1]

    expected_tokens = 4000
    token_error = abs(snap["used"]["tokens"] - expected_tokens) / expected_tokens
    rows.append({"step": "accountingWithin5Percent", "ok": token_error <= 0.05,
                 "tokens": snap["used"]["tokens"],
                 "errors": {"tokens": round(token_error, 4)}})
    legacy_tables = {
        r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
            "('model_calls','credit_batches','credit_transactions','billing_orders','model_credit_rates')"
        )
    }
    rows.append({"step": "billingTablesRemoved", "ok": legacy_tables == set(),
                 "found": sorted(legacy_tables)})
    rows.append({"step": "snapshotShapeComplete",
                 "ok": set(snap.keys()) >= {"taskId", "stage", "limits", "used", "reasons", "noProgressRounds"}
                 and set(snap["limits"]) >= {"maxRounds", "maxWallSeconds", "maxModelCalls",
                                            "maxToolCalls", "maxTokens"},
                 "limits": snap["limits"]})

    # RPC 快照可读（服务层暴露给 Renderer 的入口）
    from lawbuddy import service as service_module
    from lawbuddy.rpc import RpcServer

    server = RpcServer()
    service_module.register(server)
    snap_rpc = server._handlers["agent.budget"]({"taskId": task["id"]})  # noqa: SLF001 — 评测直调处理器
    rows.append({"step": "rpcSnapshotExposed", "ok": snap_rpc.get("taskId") == task["id"]
                 and "used" in snap_rpc and "limits" in snap_rpc})
    report["accounting"] = {"taskId": task["id"], "caseId": case_id,
                            "used": snap["used"], "limits": snap["limits"]}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m3_budget",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "mock_gateway",
    }
    try:
        ensure_workspace(str(tmp / "ws"))

        g1 = _seg(report, "limits", _run_limits)
        g2 = _seg(report, "repeat", _run_repeat)
        g3 = _seg(report, "noProgress", _run_no_progress)
        g4 = _seg(report, "accounting", _run_accounting)

        gates = {
            "wrapUpAndBudgetA002A013": g1,
            "repeatedCallA004": g2,
            "noProgressA003": g3,
            "accountingA013": g4,
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · 任务预算与无进展控制（{r['mode']}） ===")
    for seg, title in (("limits", "Loop收束与预算记账(A-002/A-013)"), ("repeat", "重复调用(A-004)"),
                       ("noProgress", "无进展(A-003)"), ("accounting", "预算统计(A-013)")):
        entry = r.get(seg, {})
        print(f"{title}：{'通过' if entry.get('ok') else '未通过'}")
        for row in entry.get("rows", []):
            print(f"  {'✓' if row.get('ok') else '✗'} {row.get('step')}: "
                  f"{json.dumps({k: v for k, v in row.items() if k not in ('step', 'ok')}, ensure_ascii=False)}")
        if entry.get("error"):
            print(f"  ✗ 段异常：\n{entry['error']}")
    print("门槛判定：")
    for g, ok in r.get("gates", {}).items():
        print(f"  {'✓' if ok else '✗'} {g}")
    print(f"结论：{'M3 预算评测通过' if r.get('passed') else 'M3 预算评测未通过（见上方 ✗ 项）'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M3 任务预算与无进展控制评测")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_budget_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()