# -*- coding: utf-8 -*-
"""Harness 专项评测（LB-HARNESS-2026-01 §6 C-03 / HAR-T-001~015）。

用法（services/core 目录下）：
    python -m evals.m3_harness [--keep]

覆盖可确定性自动化的部分（其余项在 C-04/C-05 用真实模型跑）：

| 编号 | 场景 | 本评测断言 |
| --- | --- | --- |
| HAR-T-001 | 300 格契约编译 | 20×15=300 唯一单元；重复编译数量不变、ID 一致 |
| HAR-T-002 | 大账本 | 1,865 页阅读记录 + 300+ 答案格无淘汰 |
| HAR-T-003 | 同词不同范围 | 缓存键含范围：A/B 文档集合互不串用 |
| HAR-T-004 | 预算取消与恢复 | 轮次/工具/模型/Token 连续计数，不重置 |
| HAR-T-005 | 30% 中断 | 部分提交后物化，主/派生文件可解析、行全集 100% |
| HAR-T-006 | 60% 中断 | 已提交结果保留，重复提交不新增行 |
| HAR-T-007 | 90% 中断 | 交付保留预算独立于探索预算 |
| HAR-T-011 | 连续失败 | 证据类两次 → needs_human；服务类 → blocked |
| HAR-T-012 | 版本变化 | 旧版本已提交格标 stale |
| HAR-T-013 | 未确认值聚合 | 空值不参与派生计数 |
| HAR-T-014 | 交付不完整 | 缺口可见，不判 complete |
| HAR-T-015 | 跨案件/原件 | 检索强制案件过滤（由 m3_security 覆盖，本评测复核 SQL） |

报告：控制台摘要 + evals/reports/m3_harness_<ts>.json。门槛：全部 gates 通过。
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

GRID_AGREEMENTS = 20
GRID_QUESTIONS = 15


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


def _case(title: str) -> dict[str, Any]:
    from lawbuddy.cases import create_case

    return create_case(title=title, case_type="commercial",
                       client_name="评测", client_role="原告代理人")


def _task(case_id: str, request: str, deliverables: Any = None,
          budget: dict[str, Any] | None = None) -> dict[str, Any]:
    from lawbuddy.agent.tasks import create_task

    return create_task(case_id=case_id, mode="legal_review", request_text=request,
                       deliverables=deliverables, budget=budget)


# ---------------------------------------------------------------------------
# HAR-T-001 / HAR-T-002：契约编译与大账本
# ---------------------------------------------------------------------------

def _grid_spec() -> dict[str, Any]:
    return {
        "name": "deal_points.csv",
        "format": "csv",
        "columns": ["agreement", "deal_point", "answer"],
        "key": ["agreement", "deal_point"],
        "expectedEntities": [f"A{i:02d}" for i in range(1, GRID_AGREEMENTS + 1)],
        "questions": [{"id": f"q{j:02d}", "text": f"第{j}个 deal point"}
                      for j in range(1, GRID_QUESTIONS + 1)],
        "derivedArtifacts": [{
            "name": "deal_summary.csv",
            "dependsOn": ["deal_points.csv"],
            "groupBy": ["deal_point"],
            "columns": ["answer"],
        }],
    }


def _run_contract(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import work_contract
    from lawbuddy.agent import deliverables as deliverable_store

    rows: list[dict[str, Any]] = []
    spec = deliverable_store._normalize_one(_grid_spec())  # noqa: SLF001 — 评测直接验规范化
    rows.append({"step": "normalizeKeepsEntityFields",
                 "ok": len(spec.get("expectedEntities") or []) == GRID_AGREEMENTS
                 and len(spec.get("questions") or []) == GRID_QUESTIONS
                 and len(spec.get("derivedArtifacts") or []) == 1,
                 "entities": len(spec.get("expectedEntities") or []),
                 "questions": len(spec.get("questions") or [])})

    contract = work_contract.compile_contract([spec], [])
    units = contract["workUnits"]
    cell_units = [u for u in units if u["scopeType"] == "entity_question"]
    ids = [u["workUnitId"] for u in units]
    rows.append({"step": "compiles300UniqueUnits",
                 "ok": len(cell_units) == GRID_AGREEMENTS * GRID_QUESTIONS == 300
                 and len(ids) == len(set(ids))
                 and len(contract["cells"]) == 300,
                 "units": len(units), "cells": len(cell_units),
                 "unique": len(set(ids)),
                 "expectedRows": GRID_AGREEMENTS * GRID_QUESTIONS})
    rows.append({"step": "rowsAreEntityQuestionPairs",
                 "ok": len(contract["artifacts"][0]["rowKeys"]) == 300,
                 "sample": contract["artifacts"][0]["rowKeys"][:2]})
    rows.append({"step": "derivedArtifactDeclared",
                 "ok": any(u["scopeType"] == "derive" for u in units)
                 and contract["summary"]["derivedArtifacts"] == 1})

    again = work_contract.compile_contract([spec], [])
    rows.append({"step": "recompileIsIdempotent",
                 "ok": [u["workUnitId"] for u in again["workUnits"]] == ids
                 and again["fingerprint"] == contract["fingerprint"],
                 "fingerprint": contract["fingerprint"]})

    # 来源列不改变布局：带 documentId/pageNo/quote 的长表仍必须是 300 格，
    # 否则声明来源列会把长表退化成「每实体一行」，答案格与覆盖全部失真。
    sourced = dict(_grid_spec())
    sourced["columns"] = ["agreement", "deal_point", "answer",
                          "documentId", "pageNo", "quote"]
    sourced_contract = work_contract.compile_contract(
        [deliverable_store._normalize_one(sourced)], [])  # noqa: SLF001
    sourced_cells = [c for c in sourced_contract["cells"]]
    rows.append({"step": "sourceColumnsKeepRowLayout",
                 "ok": len(sourced_cells) == GRID_AGREEMENTS * GRID_QUESTIONS
                 and len(sourced_contract["artifacts"][0]["rowKeys"]) == 300,
                 "cells": len(sourced_cells),
                 "rowKeys": len(sourced_contract["artifacts"][0]["rowKeys"])})

    report["contract"] = {"units": len(units), "fingerprint": contract["fingerprint"]}
    return rows, all(r["ok"] for r in rows)


def _run_large_ledger(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import work_ledger
    from lawbuddy.agent.autonomous import _record_issues, _record_reading

    case = _case("Harness-大账本")
    task = _task(case["id"], "大账本评测")
    task_id = task["id"]
    state: dict[str, Any] = {"case_id": case["id"], "task_id": task_id,
                             "reading_ledger": {}, "task_contract": {},
                             "evidence_ledger": {}, "issue_ledger": {}}
    pages = 1865
    _record_reading(state, "doc-big", "ver-big", {"pages": [
        {"pageNo": n, "textTotal": 20, "textStart": 0, "text": "甲" * 20, "truncated": False}
        for n in range(1, pages + 1)
    ]})
    ledger_size = len(state["reading_ledger"])

    cells = 320
    for i in range(cells):
        work_ledger.commit_cell(
            task_id, "answers.csv", f"row{i:04d}::q1", value=f"v{i}",
            status="committed", document_id="doc-big", version_id="ver-big",
            page_no=(i % pages) + 1, quote="甲" * 10,
        )
    stored = work_ledger.list_cells(task_id, artifact_name="answers.csv")

    # 证据账本同样不淘汰：300 条证据必须全部保留（B-02 大账本要求）
    evidence_count = 0
    from lawbuddy.agent.autonomous import _record_evidence

    for i in range(300):
        _record_evidence(state, [{
            "claim": f"事实{i}", "status": "needs_human",
            "reasonCode": "INSUFFICIENT_EVIDENCE", "direction": "neutral",
        }])
        evidence_count += 1
    _record_issues(state, [{"issue": f"争点{i}", "status": "open"} for i in range(60)])
    rows = [
        {"step": "readingLedgerNoEviction", "ok": ledger_size == pages,
         "pages": pages, "kept": ledger_size},
        {"step": "cellLedgerNoEviction", "ok": len(stored) == cells,
         "cells": cells, "kept": len(stored)},
        {"step": "evidenceLedgerNoEviction",
         "ok": len(state.get("evidence_ledger") or {}) == evidence_count,
         "evidence": evidence_count,
         "kept": len(state.get("evidence_ledger") or {})},
        {"step": "issueLedgerNoEviction",
         "ok": len(state.get("issue_ledger") or {}) == 60,
         "kept": len(state.get("issue_ledger") or {})},
        {"step": "progressRecomputable",
         "ok": work_ledger.cell_progress(task_id, "answers.csv")["committed"] == cells},
    ]
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# HAR-T-003：检索范围缓存键
# ---------------------------------------------------------------------------

def _run_search_scope(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.autonomous import _corpus_stamp, _search_scope

    state = {"case_id": "case-1", "_corpus_stamp": "abc123"}
    scope_a = _search_scope(state, ("doc-a",))
    scope_b = _search_scope(state, ("doc-b",))
    scope_ab = _search_scope(state, ("doc-a", "doc-b"))
    scope_ba = _search_scope(state, ("doc-b", "doc-a"))
    state2 = {"case_id": "case-1", "_corpus_stamp": "def456"}
    scope_ver = _search_scope(state2, ("doc-a",))
    rows = [
        {"step": "differentDocsDifferentScope", "ok": scope_a != scope_b},
        {"step": "docSetOrderStable", "ok": scope_ab == scope_ba},
        {"step": "differentCaseDifferentScope",
         "ok": _search_scope({"case_id": "case-2", "_corpus_stamp": "abc123"}, ("doc-a",)) != scope_a},
        {"step": "versionChangeInvalidatesScope", "ok": scope_ver != scope_a,
         "before": scope_a, "after": scope_ver},
    ]
    report["searchScope"] = {"scopeA": scope_a, "scopeB": scope_b}
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# HAR-T-004：预算与恢复连续性
# ---------------------------------------------------------------------------

def _run_budget_recovery(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import budget, work_ledger

    case = _case("Harness-预算恢复")
    task = _task(case["id"], "预算恢复评测", budget={"maxToolCalls": 12})
    task_id = task["id"]
    budget.ensure(task_id, {"maxToolCalls": 12})
    for _ in range(5):
        budget.spend(task_id, "tool_call")
    for _ in range(2):
        budget.spend(task_id, "model_call", tokens=500)
    before = budget.snapshot(task_id)

    work_ledger.ensure_runtime(task_id, "v2")
    work_ledger.save_runtime(task_id, {
        "roundsUsed": 3, "toolCallsUsed": 5, "modelCallsUsed": 2,
        "promptTokens": 600, "completionTokens": 400,
    })
    runtime = work_ledger.get_runtime(task_id) or {}
    # 模拟续跑：新计数更小不得回退（取最大值语义）
    work_ledger.save_runtime(task_id, {"toolCallsUsed": 1, "roundsUsed": 1})
    after = work_ledger.get_runtime(task_id) or {}

    # 再次 ensure 不重置已用量
    again = budget.ensure(task_id, {})
    rows = [
        {"step": "toolCallsCounted", "ok": before["used"].get("tool_call") == 5,
         "used": before["used"]},
        {"step": "tokensCounted", "ok": before["used"].get("tokens") == 1000,
         "tokens": before["used"].get("tokens")},
        {"step": "runtimeCountersPersisted",
         "ok": runtime.get("roundsUsed") == 3 and runtime.get("toolCallsUsed") == 5
         and runtime.get("modelCallsUsed") == 2 and runtime.get("promptTokens") == 600},
        {"step": "countersNeverRegress",
         "ok": after.get("toolCallsUsed") == 5 and after.get("roundsUsed") == 3,
         "after": {"rounds": after.get("roundsUsed"), "tools": after.get("toolCallsUsed")}},
        {"step": "ensureDoesNotReset", "ok": again["used"].get("tool_call") == 5,
         "used": again["used"].get("tool_call")},
        {"step": "runtimeVersionPinned", "ok": runtime.get("runtimeVersion") == "v2"},
    ]
    report["budget"] = {"taskId": task_id, "used": before["used"]}
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# HAR-T-005/006/007/013/014：交付物化、中断与派生
# ---------------------------------------------------------------------------

def _seed_grid_submission(task_id: str, contract: dict[str, Any], root: Path,
                          answered: int, with_source: int) -> None:
    """按契约写入 answered 行（前 with_source 行带来源列），其余留空。"""
    import csv as _csv
    import io as _io

    artifact = contract["artifacts"][0]
    path = root / "submission" / artifact["name"]
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(artifact["columns"]) + ["documentId", "pageNo"]
    buf = _io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for index, row_key in enumerate(artifact["rowKeys"][:answered]):
        agreement, deal_point = row_key.split("::", 1)
        row = {"agreement": agreement, "deal_point": deal_point, "answer": f"opt-{index % 3}"}
        if index < with_source:
            row["documentId"] = f"doc-{agreement}"
            row["pageNo"] = str((index % 5) + 1)
        writer.writerow(row)
    path.write_text(buf.getvalue(), encoding="utf-8")


def _run_delivery(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import delivery_controller, work_contract, work_ledger
    from lawbuddy.agent import deliverables as deliverable_store
    from lawbuddy.sandbox import workspace_root

    case = _case("Harness-交付控制器")
    task = _task(case["id"], "交付控制器评测")
    task_id = task["id"]
    spec = deliverable_store._normalize_one(_grid_spec())  # noqa: SLF001
    contract = work_contract.compile_contract([spec], [])
    work_ledger.seed_units(task_id, contract["workUnits"])
    # 答案格必须预登记：否则「300 行里有 210 行从没被处理」在账本里不可见，
    # 交付物看起来 100% 覆盖却没有任何未终态格。
    work_ledger.seed_cells(task_id, contract["cells"])
    root = workspace_root()
    (root / "submission").mkdir(parents=True, exist_ok=True)

    # 30% 中断：只答 90 行（其中 60 行有来源），剩余行必须由物化补齐
    _seed_grid_submission(task_id, contract, root, answered=90, with_source=60)
    first = delivery_controller.commit_artifact(task_id, contract, "deal_points.csv", root)
    rows = [
        {"step": "interrupt30RowsComplete",
         "ok": first["status"] == "complete" and first["rows"] == 300
         and first["missingRows"] == 0,
         "status": first["status"], "rows": first["rows"]},
        {"step": "interrupt30FileParseable",
         "ok": delivery_controller.inspect_artifact(contract, contract["artifacts"][0],
                                                    root)["parseable"]},
        {"step": "sourcedCellsCommitted", "ok": first["cells"]["committed"] == 60,
         "cells": first["cells"]},
        {"step": "unsourcedCellsNeedHuman",
         "ok": first["cells"]["needsHuman"] == 30, "cells": first["cells"]},
    ]

    # 答案格终态必须回写到对应工作单元；长表 scope 缺 cellKey 时单元会永远 pending
    units = work_ledger.list_units(task_id)
    cell_units = [u["status"] for u in units if u["scopeType"] == "entity_question"]
    automation_units = [u["status"] for u in units
                        if u["scopeType"] in ("materialize", "derive")]
    rows.append({"step": "unitsFollowCellTerminal",
                 "ok": cell_units.count("committed") == 60
                 and cell_units.count("needs_human") == 30
                 and cell_units.count("pending") == 210,
                 "committed": cell_units.count("committed"),
                 "needsHuman": cell_units.count("needs_human"),
                 "pending": cell_units.count("pending")})
    rows.append({"step": "automationUnitsWaitForCells",
                 # 答案格未全部终态时，物化/派生还没执行过，不得提前记成 staged。
                 "ok": len(automation_units) == 2
                 and all(s == "pending" for s in automation_units),
                 "states": automation_units})

    # 60% 中断：再次提交同一批（幂等），行数不增、已提交结果保留
    before_rows = first["rows"]
    second = delivery_controller.commit_artifact(task_id, contract, "deal_points.csv", root)
    rows.append({"step": "repeatCommitIdempotent",
                 "ok": second["rows"] == before_rows
                 and second["cells"]["committed"] == 60,
                 "rows": second["rows"], "cells": second["cells"]})
    stored = work_ledger.list_cells(task_id, artifact_name="deal_points.csv")
    touched = [c for c in stored if c["status"] != "pending"]
    rows.append({"step": "noDuplicateCells",
                 # 预登记 300 格；只有被文件内容覆盖的 90 格进入非 pending，
                 # 重复提交不得新增行或新增已处理格。
                 "ok": len(stored) == 300 and len(touched) == 90,
                 "cells": len(stored), "touched": len(touched)})

    # 90% 中断 + 派生聚合：派生表只统计已提交值，不把空值当否定
    delivery = delivery_controller.finalize(task_id, contract, root)
    derived = next((d for d in delivery["derived"] if d["name"] == "deal_summary.csv"), None)
    summary_rows = delivery_controller._read_rows(  # noqa: SLF001 — 评测校验派生结果
        root / "submission" / "deal_summary.csv")
    total_counts = sum(int(r.get("answer") or 0) for r in summary_rows)
    rows.append({"step": "derivedAggregateIgnoresEmpty",
                 "ok": derived is not None and total_counts == 90,
                 "derived": derived, "counted": total_counts})
    rows.append({"step": "deliveryCompleteAfterMaterialize",
                 "ok": delivery["structuralStatus"] == "complete",
                 "status": delivery["status"], "gaps": delivery["gaps"]})

    # HAR-T-014：删掉行 → 缺口可见，不判 complete
    artifact = contract["artifacts"][0]
    path = root / "submission" / artifact["name"]
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:51]) + "\n", encoding="utf-8")
    broken = delivery_controller.inspect_artifact(contract, artifact, root)
    broken_status = ("complete" if delivery_controller._coverage_ok(broken)  # noqa: SLF001
                     else "partial")
    rows.append({"step": "incompleteDeliveryVisible",
                 "ok": broken_status != "complete" and len(broken["missingRowKeys"]) > 0,
                 "missing": len(broken["missingRowKeys"])})

    # 恢复完整：物化再次补齐
    restored = delivery_controller.commit_artifact(task_id, contract, "deal_points.csv", root)
    rows.append({"step": "materializeRestoresRows",
                 "ok": restored["status"] == "complete" and restored["rows"] == 300})

    # §1.1-7：结构交付完整但答案格大量未终态时，交付状态不得显示 complete。
    progress = work_ledger.delivery_progress(task_id)
    rows.append({"step": "structuralCompleteDoesNotFakeDone",
                 "ok": progress["structuralStatus"] == "complete"
                 and progress["status"] == "partial"
                 and progress["workDone"] is False,
                 "structural": progress["structuralStatus"],
                 "status": progress["status"],
                 "cellsPending": progress["cellsPending"]})

    # HAR-T-005/014（JSON 交付物）：13 被告里只答 9 名，缺的被告必须补进文件，
    # 且「有内容」与「已确认」必须分开统计，不能把空壳当完成。
    rows.extend(_delivery_json_rows(root))

    report["delivery"] = {"taskId": task_id, "first": first["status"],
                          "derived": derived}
    return rows, all(r["ok"] for r in rows)


def _delivery_json_rows(root: Path) -> list[dict[str, Any]]:
    """JSON 交付物（多被告裁判）覆盖与来源同步的确定性验证。"""
    import json as _json

    from lawbuddy.agent import delivery_controller, work_contract, work_ledger
    from lawbuddy.agent import deliverables as deliverable_store

    defendants = [f"D{i:02d}" for i in range(1, 14)]
    fields = ["charges", "articles", "final_term"]
    spec = deliverable_store._normalize_one({  # noqa: SLF001 — 评测直接验规范化
        "name": "judgment.json",
        "format": "json",
        "jsonPath": "defendants",
        "skeleton": {"defendants": {d: {f: "" for f in fields} for d in defendants}},
        "expectedEntities": [{"id": d, "label": d, "fields": fields} for d in defendants],
    })
    contract = work_contract.compile_contract([spec], [])
    units = [u for u in contract["workUnits"] if u["scopeType"] == "entity_field"]
    out: list[dict[str, Any]] = [
        {"step": "jsonEntityFieldUnits", "ok": len(units) == 13 * 3,
         "units": len(units)},
    ]

    case = _case("Harness-JSON 交付")
    task = _task(case["id"], "多被告交付评测")
    task_id = task["id"]
    work_ledger.seed_units(task_id, contract["workUnits"])
    work_ledger.seed_cells(task_id, contract["cells"])
    sub = root / "submission"
    sub.mkdir(parents=True, exist_ok=True)

    # 只答 9 名被告：前 6 名带实体级来源，后 3 名只有内容没有来源
    payload: dict[str, Any] = {"defendants": {}}
    for index, d in enumerate(defendants[:9]):
        entry = {"charges": ["盗窃罪"], "articles": [264],
                 "final_term": {"type": "有期徒刑", "months": 12}}
        if index < 6:
            entry["documentId"] = "doc-1"
            entry["pageNo"] = 3
        payload["defendants"][d] = entry
    # 第 10 名显式标注无法确认：应算已处理，但不算已确认答案
    payload["defendants"][defendants[9]] = {
        "charges": [], "articles": [], "final_term": {"type": "", "months": None},
        "status": "needs_human", "reasonCode": "INSUFFICIENT_EVIDENCE",
    }
    (sub / "judgment.json").write_text(
        _json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    result = delivery_controller.commit_artifact(task_id, contract, "judgment.json", root)
    stored = _json.loads((sub / "judgment.json").read_text(encoding="utf-8"))
    entries = stored.get("defendants") or {}
    out.append({"step": "jsonMaterializeAddsMissingEntities",
                 "ok": sorted(entries) == defendants,
                 "entities": len(entries)})
    out.append({"step": "jsonStructureCoverageComplete",
                 "ok": result["status"] == "complete",
                 "status": result["status"]})
    out.append({"step": "jsonCellsSourcedCommitted",
                 "ok": result["cells"]["committed"] == 6 * 3,
                 "cells": result["cells"]})
    out.append({"step": "jsonCellsUnsourcedNeedHuman",
                 "ok": result["cells"]["needsHuman"] == 4 * 3,
                 # D7~D9 有内容无来源 + D10 显式标注无法确认，都记 needs_human
                 "cells": result["cells"]})
    out.append({"step": "jsonEmptyEntitiesStayPending",
                 # D11~D13 从未作答也未标注，不能算已处理
                 "ok": result["cells"]["pending"] == 3 * 3,
                 "cells": result["cells"]})
    pending_cells = [c for c in work_ledger.list_cells(task_id, artifact_name="judgment.json")
                     if c["status"] == "pending"]
    out.append({"step": "jsonPendingHaveNoValue",
                 "ok": pending_cells and all(c.get("value") in (None, "", [], {})
                                             for c in pending_cells),
                 "pending": len(pending_cells)})
    inspection = delivery_controller.inspect_artifact(contract, contract["artifacts"][0], root)
    out.append({"step": "jsonInspectSeparatesFilledFromCoverage",
                 # 有内容 9 名 + 显式待核实 1 名 + 从未作答 3 名
                 "ok": inspection["rows"] == 13
                 and len(inspection["rowsUnfilled"]) == 3
                 and inspection["rowsNeedsHuman"] == [defendants[9]],
                 "rows": inspection["rows"],
                 "unfilled": inspection["rowsUnfilled"],
                 "needsHuman": inspection["rowsNeedsHuman"]})
    committed = [c for c in work_ledger.list_cells(task_id, artifact_name="judgment.json")
                 if c["status"] == "committed"]
    out.append({"step": "jsonCommittedHaveSource",
                 "ok": committed and all(c.get("documentId") and c.get("pageNo")
                                         for c in committed),
                 "committed": len(committed)})
    # 无答案格的整体交付物（如聚合 answers.json）必须能进入终态：它没有格子可同步，
    # 若按「等全部格终态」处理会永远 pending，workDone 永远到不了 100%。
    bare = deliverable_store._normalize_one(  # noqa: SLF001
        {"name": "answers.json", "format": "json", "skeleton": {}})
    bare_contract = work_contract.compile_contract([bare], [])
    bare_units = [u for u in bare_contract["workUnits"]
                  if u["scope"]["artifact"] == "answers.json"]
    out.append({"step": "bareArtifactHasSingleUnit",
                 "ok": len(bare_units) == 1 and bare_units[0]["scopeType"] == "artifact",
                 "units": [u["scopeType"] for u in bare_units]})
    work_ledger.seed_units(task_id, bare_contract["workUnits"])
    (sub / "answers.json").write_text('{"M01": 1}\n', encoding="utf-8")
    delivery_controller.commit_artifact(task_id, bare_contract, "answers.json", root)
    states = [u["status"] for u in work_ledger.list_units(task_id)
              if (u.get("scope") or {}).get("artifact") == "answers.json"]
    out.append({"step": "bareArtifactUnitReachesTerminal",
                 "ok": states == ["committed"], "states": states})
    return out


# ---------------------------------------------------------------------------
# HAR-T-011：失败分类与调度
# ---------------------------------------------------------------------------

def _run_scheduler(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import scheduler, work_contract, work_ledger

    from lawbuddy.agent import deliverables as deliverable_store

    case = _case("Harness-调度")
    task = _task(case["id"], "调度评测")
    task_id = task["id"]
    spec = deliverable_store._normalize_one({  # noqa: SLF001 — 与运行期同一条规范化路径
        "name": "answers.csv", "format": "csv",
        "columns": ["defendant", "answer"], "key": ["defendant"],
        "expectedEntities": ["甲", "乙", "丙"],
        "questions": [{"id": "roles", "text": "角色"}],
    })
    contract = work_contract.compile_contract([spec], [])
    work_ledger.seed_units(task_id, contract["workUnits"])

    units = [u for u in work_ledger.list_units(task_id) if u["scopeType"] == "entity_field"]
    first = units[0]
    work_ledger.lease_unit(task_id, first["workUnitId"])
    scheduler.record_failure(task_id, first["workUnitId"], "NO_HITS", streak=1)
    status_after_one = work_ledger.get_unit(task_id, first["workUnitId"])["status"]
    work_ledger.lease_unit(task_id, first["workUnitId"])
    scheduler.record_failure(task_id, first["workUnitId"], "NO_HITS", streak=2)
    status_after_two = work_ledger.get_unit(task_id, first["workUnitId"])["status"]

    second = units[1]
    work_ledger.lease_unit(task_id, second["workUnitId"])
    scheduler.record_failure(task_id, second["workUnitId"], "NETWORK_ERROR", streak=1)
    status_service = work_ledger.get_unit(task_id, second["workUnitId"])["status"]

    nxt = scheduler.select_next(task_id)
    rows = [
        {"step": "evidenceFailureNeedsTwoStrikes",
         "ok": status_after_one == "leased" and status_after_two == "needs_human",
         "afterOne": status_after_one, "afterTwo": status_after_two},
        {"step": "serviceFailureBlockedImmediately",
         "ok": status_service == "blocked", "status": status_service},
        {"step": "otherUnitsStillSchedulable",
         "ok": nxt is not None and nxt["workUnitId"] not in
               (first["workUnitId"],),
         "next": (nxt or {}).get("workUnitId")},
        {"step": "deliveryReserveExcludesExploration",
         "ok": scheduler.select_next(task_id, reserve_for_delivery=True)["scopeType"]
               in ("materialize", "derive")},
        {"step": "budgetSplitKeepsReserve",
         "ok": scheduler.split_budget(100)["deliveryReservedRounds"] >= 8
         and scheduler.split_budget(100)["exploreRounds"]
             + scheduler.split_budget(100)["deliveryReservedRounds"] == 100},
    ]
    report["scheduler"] = {"taskId": task_id, "next": (nxt or {}).get("workUnitId")}
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# HAR-T-012：版本失效
# ---------------------------------------------------------------------------

def _run_version_stale(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import work_ledger

    case = _case("Harness-版本失效")
    task = _task(case["id"], "版本失效评测")
    task_id = task["id"]
    work_ledger.commit_cell(task_id, "deal_points.csv", "A01::q1", value="甲",
                            status="committed", document_id="doc-a",
                            version_id="ver-old", page_no=1, quote="原文")
    work_ledger.commit_cell(task_id, "deal_points.csv", "A02::q1", value="乙",
                            status="committed", document_id="doc-b",
                            version_id="ver-old", page_no=2, quote="原文")
    work_ledger.commit_cell(task_id, "deal_points.csv", "A03::q1", value="丙",
                            status="needs_human", reason_code="SOURCE_MISSING")
    stale = work_ledger.invalidate_by_version(task_id, "doc-a", "ver-old")
    cells = {c["cellKey"]: c for c in work_ledger.list_cells(task_id)}
    rows = [
        {"step": "onlyAffectedCellsStale",
         "ok": stale == 1 and cells["A01::q1"]["status"] == "stale",
         "stale": stale, "status": cells["A01::q1"]["status"]},
        {"step": "otherDocumentUnaffected",
         "ok": cells["A02::q1"]["status"] == "committed"},
        {"step": "needsHumanUntouched",
         "ok": cells["A03::q1"]["status"] == "needs_human"},
        {"step": "staleHasReasonCode",
         "ok": cells["A01::q1"]["reasonCode"] == "VERSION_CHANGED"},
    ]
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# HAR-T-010 / 幂等键
# ---------------------------------------------------------------------------

def _run_idempotency(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import work_ledger

    case = _case("Harness-幂等")
    task = _task(case["id"], "幂等评测")
    task_id = task["id"]
    for _ in range(3):
        work_ledger.commit_cell(task_id, "answers.json", "q1", value="同一答案",
                                status="committed", document_id="doc-a",
                                version_id="ver-1", page_no=1, quote="原文")
    cells = work_ledger.list_cells(task_id)
    rows = [
        {"step": "repeatedCommitSingleRow", "ok": len(cells) == 1, "cells": len(cells)},
        {"step": "lastValueWins", "ok": cells[0]["value"] == "同一答案"},
    ]

    # committed 缺来源必须被拒绝（结论须可回溯原文）
    rejected = False
    try:
        work_ledger.commit_cell(task_id, "answers.json", "q2", value="无源",
                                status="committed")
    except work_ledger.LedgerError:
        rejected = True
    rows.append({"step": "committedRequiresSource", "ok": rejected})

    # 非法状态迁移被拒绝
    illegal = False
    try:
        work_ledger.transition_unit(task_id, "no-such-unit", "committed")
    except work_ledger.LedgerError:
        illegal = True
    rows.append({"step": "unknownUnitRejected", "ok": illegal})
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# HAR-T-008/009/010：并发熔断、断连分类、子代理幂等
# ---------------------------------------------------------------------------

def _run_concurrency(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """并发闸门：初始 2 并发、失败降为 1、连续失败熔断冷却。"""
    from lawbuddy.agent import concurrency

    gate = concurrency.ConcurrencyGate()
    initial = gate.limit
    gate.on_failure("rate_limited")
    after_rate = gate.limit
    gate.on_success()
    recovered = gate.snapshot()
    for _ in range(concurrency.BREAKER_THRESHOLD):
        gate.on_failure("timeout")
    open_state = gate.snapshot()
    # 熔断期内 acquire 必须等待而不是返回空结果（短超时证明它不会静默放行）
    blocked = False
    try:
        with gate.acquire(timeout=0.2):
            blocked = False
    except TimeoutError:
        blocked = True
    gate.reset()
    rows = [
        {"step": "initialConcurrencyTwo", "ok": initial == 2, "limit": initial},
        {"step": "transientFailureDegrades", "ok": after_rate == 1, "limit": after_rate},
        {"step": "successRecoversConcurrency",
         "ok": recovered["limit"] >= 1 and not recovered["degraded"],
         "snapshot": recovered},
        {"step": "breakerOpensAfterStreak", "ok": open_state["open"] is True},
        {"step": "breakerNeverYieldsSilently", "ok": blocked},
        {"step": "resetRestoresInitial", "ok": gate.limit == 2},
        {"step": "errorClassification",
         "ok": concurrency.classify_error(TimeoutError("timed out")) == "timeout"
         and concurrency.classify_error(ConnectionError("RemoteDisconnected")) == "disconnect"
         and concurrency.classify_error(RuntimeError("429 限流")) == "rate_limited"},
    ]
    return rows, all(r["ok"] for r in rows)


def _run_disconnect(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """断连分类（HAR-T-008/009）：首字前可重试、正文可续写、工具参数必须整轮丢弃。"""
    from lawbuddy.agent.model_gateway import StreamInterrupted

    head = StreamInterrupted("", "URLError", resumable=False, partial_tool_calls=0)
    body = StreamInterrupted("半截正文", "RemoteDisconnected", resumable=True)
    args = StreamInterrupted("半截正文", "IncompleteRead", resumable=False, partial_tool_calls=2)
    rows = [
        {"step": "headInterruptNotResumable", "ok": head.resumable is False},
        {"step": "bodyInterruptResumable", "ok": body.resumable is True},
        {"step": "toolArgsInterruptMarked",
         "ok": args.resumable is False and args.partial_tool_calls == 2},
    ]
    return rows, all(r["ok"] for r in rows)


def _run_subagent_idempotent(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """子代理重复提交（HAR-T-010）：同一单元重试不产生重复行，且只写暂存区。"""
    from lawbuddy.agent import delivery_controller, work_contract, work_ledger, worker
    from lawbuddy.agent import deliverables as deliverable_store
    from lawbuddy.sandbox import workspace_root

    case = _case("Harness-子代理幂等")
    task = _task(case["id"], "子代理幂等评测")
    task_id = task["id"]
    spec = deliverable_store._normalize_one({  # noqa: SLF001
        "name": "answers.csv", "format": "csv",
        "columns": ["defendant", "answer"], "key": ["defendant"],
        "expectedEntities": ["甲", "乙"],
        "questions": [{"id": "roles", "text": "角色"}],
    })
    contract = work_contract.compile_contract([spec], [])
    work_ledger.seed_units(task_id, contract["workUnits"])
    unit = next(u for u in work_ledger.list_units(task_id)
                if u["scopeType"] == "entity_field")
    leased = work_ledger.lease_unit(task_id, unit["workUnitId"])
    parsed = {
        "answer": "主犯", "documentId": "doc-1", "pageNo": 3,
        "quote": "被告人甲系主犯", "confidence": "high",
    }
    first = worker.commit_staged_result(task_id, leased, parsed)
    second = worker.commit_staged_result(task_id, leased, parsed)
    cells = work_ledger.list_cells(task_id, artifact_name="answers.csv")

    # 暂存区写入：只落在 .staging 下，正式交付物未被写
    staged = worker.write_staged(task_id, unit["workUnitId"], {"result": parsed})
    submission = workspace_root() / "submission"
    staged_file = submission / staged["path"].split("submission/", 1)[1]
    rows = [
        {"step": "stagedOutsideFormalArtifacts",
         "ok": staged_file.is_file() and ".staging" in staged["path"],
         "path": staged["path"]},
        {"step": "formalArtifactUntouched",
         "ok": not (submission / "answers.csv").exists()
               or (submission / "answers.csv").stat().st_size == 0},
        {"step": "repeatCommitNoDuplicateRow",
         "ok": len(cells) == 1 and first["status"] == second["status"] == "committed",
         "cells": len(cells)},
        {"step": "sourcelessBecomesNeedsHuman",
         "ok": worker.commit_staged_result(
             task_id, {**leased, "scope": {**leased["scope"], "cellKey": "乙::roles"}},
             {"answer": "从犯", "documentId": "", "pageNo": 0, "quote": ""},
         )["status"] == "needs_human"},
    ]
    # 子代理工具白名单：不含任何交付物写入工具
    allowed = set(worker._WORKER_TOOLS)  # noqa: SLF001 — 评测直接校验白名单
    rows.append({"step": "workerCannotWriteFormalArtifacts",
                 "ok": not (allowed & {"deliverable_patch", "write_file", "edit_file"})})

    # 正式交付物只能由控制器写入
    result = delivery_controller.commit_artifact(task_id, contract, "answers.csv", workspace_root())
    rows.append({"step": "controllerWritesFormalArtifact",
                 "ok": result["registered"] and (submission / "answers.csv").is_file(),
                 "rows": result["rows"]})
    report["subagent"] = {"taskId": task_id, "staged": staged["path"]}
    return rows, all(r["ok"] for r in rows)


def _run_context_builder(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """上下文组装（HAR-T-002 补充）：压缩后状态仍从账本重建，单元简报只含本单元。"""
    from lawbuddy.agent import context_builder, work_contract, work_ledger
    from lawbuddy.agent import deliverables as deliverable_store

    case = _case("Harness-上下文")
    task = _task(case["id"], "上下文组装评测")
    task_id = task["id"]
    spec = deliverable_store._normalize_one({  # noqa: SLF001
        "name": "answers.csv", "format": "csv",
        "columns": ["defendant", "answer"], "key": ["defendant"],
        "expectedEntities": ["甲", "乙", "丙"],
        "questions": [{"id": "roles", "text": "角色"}],
        "validValues": {"answer": ["主犯", "从犯", "无罪"]},
    })
    contract = work_contract.compile_contract([spec], [])
    work_ledger.seed_units(task_id, contract["workUnits"])
    state = {
        "task_id": task_id, "case_id": case["id"],
        "task_contract": {"taskDepth": "deep", "objective": "评测"},
        "_work_contract": contract,
        "evidence_ledger": {},
    }
    work_ledger.commit_cell(task_id, "answers.csv", "甲::roles", value="主犯",
                            status="committed", document_id="doc-1", page_no=2,
                            quote="被告人甲系主犯")
    state_block = context_builder.build_state_block(state)
    unit = next(u for u in work_ledger.list_units(task_id)
                if (u["scope"] or {}).get("cellKey") == "乙::roles")
    brief = context_builder.build_unit_brief(state, unit)
    rows = [
        {"step": "stateBlockFromLedger",
         "ok": "工作单元" in state_block and "答案格" in state_block},
        {"step": "unitBriefScoped",
         "ok": "乙" in brief and "丙" not in brief, "briefHead": brief[:120]},
        {"step": "validOptionsInjected",
         "ok": "主犯" in brief and "从犯" in brief and "无罪" in brief},
        {"step": "committedExampleFlaggedAsReference",
         "ok": "不得直接套用" in brief},
        {"step": "stablePrefixUnchanged",
         "ok": context_builder.build_stable_prefix(state, "BASE") == "BASE"},
    ]
    report["context"] = {"taskId": task_id, "unit": unit["workUnitId"]}
    return rows, all(r["ok"] for r in rows)


def _run_work_unit_flow(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """端到端：mock 模型网关驱动 work_unit_run（B-04）。

    验证：单元 leased→staged→committed、答案格带来源提交、暂存文件落盘、
    重复调用不再产生第二条答案格、无来源结论进 needs_human。
    """
    from lawbuddy.agent import model_gateway, work_contract, work_ledger
    from lawbuddy.agent import deliverables as deliverable_store
    from lawbuddy.agent.autonomous import _run_work_units
    from lawbuddy.sandbox import workspace_root

    case = _case("Harness-工作单元执行")
    task = _task(case["id"], "工作单元执行评测")
    task_id = task["id"]
    spec = deliverable_store._normalize_one({  # noqa: SLF001
        "name": "deal_points.csv", "format": "csv",
        "columns": ["agreement", "deal_point", "answer"],
        "key": ["agreement", "deal_point"],
        "expectedEntities": ["A01", "A02"],
        "questions": [{"id": "q1", "text": "适用法律"}],
    })
    contract = work_contract.compile_contract([spec], [])
    work_ledger.seed_units(task_id, contract["workUnits"])
    work_ledger.seed_cells(task_id, contract["cells"])
    state: dict[str, Any] = {
        "task_id": task_id, "case_id": case["id"], "mode": "legal_review",
        "_work_contract": contract, "deliverable_specs": [spec],
        "evidence_ledger": {}, "reading_ledger": {},
        "_cancel_check": lambda: False,
        "_emit": lambda *_a, **_k: None,
        "model_policy": {},
    }

    replies = [
        # 第一个单元：有来源的完整结论
        '结论：适用中国法。\n```json\n{"answer":"中国法","documentId":"doc-1",'
        '"pageNo":2,"quote":"本协议适用中华人民共和国法律","confidence":"high"}\n```',
        # 第二个单元：无来源 → 必须落到 needs_human
        '未找到明确约定。\n```json\n{"answer":"","documentId":"","pageNo":0,'
        '"quote":"","confidence":"low","reasonCode":"NO_HITS"}\n```',
    ]
    original = model_gateway.chat_with_tools
    calls = {"n": 0}

    def fake_chat(messages, tools=None, model="", **kwargs):  # noqa: ANN001, ARG001
        index = min(calls["n"], len(replies) - 1)
        calls["n"] += 1
        return {"content": replies[index], "toolCalls": [], "usage": {}}

    model_gateway.chat_with_tools = fake_chat  # type: ignore[assignment]
    try:
        first = _run_work_units(state, {"limit": 1})
        second = _run_work_units(state, {"limit": 1})
        third = _run_work_units(state, {"limit": 1})
    finally:
        model_gateway.chat_with_tools = original  # type: ignore[assignment]

    cells = work_ledger.list_cells(task_id, artifact_name="deal_points.csv")
    units = work_ledger.list_units(task_id)
    entity_units = [u for u in units if u["scopeType"] == "entity_question"]
    committed = [u for u in entity_units if u["status"] == "committed"]
    needs_human = [u for u in entity_units if u["status"] == "needs_human"]
    staged_dir = workspace_root() / "submission" / ".staging" / task_id
    rows = [
        {"step": "firstUnitCommitted", "ok": first["units"][0]["status"] == "committed",
         "status": first["units"][0]["status"]},
        {"step": "stagedFileWritten", "ok": staged_dir.is_dir()
         and any(staged_dir.glob("*.json")), "dir": str(staged_dir.name)},
        {"step": "sourcedCellCommitted",
         "ok": any(c["status"] == "committed" and c["documentId"] == "doc-1" for c in cells),
         "cells": [(c["cellKey"], c["status"]) for c in cells]},
        {"step": "sourcelessUnitNeedsHuman", "ok": len(needs_human) == 1,
         "needsHuman": len(needs_human)},
        {"step": "allCellsInTerminalState",
         "ok": len(committed) + len(needs_human) == 2, "committed": len(committed)},
        {"step": "noDuplicateCells", "ok": len(cells) == 2, "cells": len(cells)},
        {"step": "exhaustedUnitsReportGap",
         "ok": "error" in third and "没有可继续执行的工作单元" in str(third["error"])},
    ]
    report["workUnitFlow"] = {
        "taskId": task_id, "modelCalls": calls["n"],
        "cells": [(c["cellKey"], c["status"]) for c in cells],
    }
    return rows, all(r["ok"] for r in rows)


def _run_runtime_pinning(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """运行时版本钉住（§4.4 / D-01）：设置 agent.runtimeVersion 决定是否启用工作账本。

    v1 只登记运行时状态（对拍旧基线），v2 登记工作单元与答案格；同一任务重复
    初始化沿用已钉住版本，不跨 V1/V2 改写。
    """
    from lawbuddy.agent import tasks as task_store
    from lawbuddy.agent import work_ledger
    from lawbuddy.agent.autonomous import _runtime_version_setting, _seed_work_ledger

    case = _case("Harness-运行时钉住")
    spec = {
        "name": "a.csv", "format": "csv",
        "columns": ["x", "q", "answer"], "key": ["x", "q"],
        "expectedEntities": ["A1", "A2"],
        "questions": [{"id": "q1", "text": "问题1"}],
    }
    rows: list[dict[str, Any]] = []
    pinned: dict[str, str] = {}
    for version in ("v1", "v2"):
        task_store.set_setting("agent.runtimeVersion", version)
        task = _task(case["id"], f"运行时 {version}", deliverables=[spec])
        state = {"task_id": task["id"], "case_id": case["id"], "deliverables": [spec]}
        _seed_work_ledger(state, lambda *_a, **_k: None)
        runtime = work_ledger.get_runtime(task["id"]) or {}
        pinned[version] = str(runtime.get("runtimeVersion"))
        units = work_ledger.unit_progress(task["id"])["total"]
        rows.append({
            "step": f"{version}LedgerState",
            "ok": pinned[version] == version and (units == 0 if version == "v1" else units > 0),
            "pinned": pinned[version], "units": units,
        })
    # 已钉住 v1 的任务再次初始化（模拟续跑）不得被改写成 v2
    task_store.set_setting("agent.runtimeVersion", "v1")
    task = _task(case["id"], "运行时续跑", deliverables=[spec])
    state = {"task_id": task["id"], "case_id": case["id"], "deliverables": [spec]}
    _seed_work_ledger(state, lambda *_a, **_k: None)
    task_store.set_setting("agent.runtimeVersion", "v2")
    _seed_work_ledger(state, lambda *_a, **_k: None)
    reread = (work_ledger.get_runtime(task["id"]) or {}).get("runtimeVersion")
    rows.append({"step": "pinnedVersionNotRewritten", "ok": reread == "v1",
                 "pinned": reread})
    rows.append({"step": "invalidSettingFallsBackToV2",
                 "ok": (task_store.set_setting("agent.runtimeVersion", "bogus")
                        or _runtime_version_setting()) == "v2"})
    task_store.set_setting("agent.runtimeVersion", "v2")
    report["runtime"] = pinned
    return rows, all(r["ok"] for r in rows)


def _run_sandbox_boundary(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """跨案与原件旁路（HAR-T-015）：shell 不得越出工作空间。

    实测缺陷（2026-11-16，C-04 复测时发现）：`bash` 只拦截 `inputs/` 等只读区，
    不限制工作区外路径，被测 Agent 因此用 `copy` / `type` 读到评测 reference 的
    answers.json 与 verify.py，出现「17/17 聚合题与真值逐字相同」的污染结果。
    跨案读取同样可行，会使「可回溯原文」与「跨案召回 0」失效。
    """
    from lawbuddy.sandbox import _guard_bash_command, read_file

    root = Path(tempfile.mkdtemp(prefix="lawbuddy-sandbox-root-"))
    outside = root.parent
    state = {"tool_policy": {}, "task_id": "", "case_id": ""}
    rows: list[dict[str, Any]] = []

    def blocked(command: str) -> bool:
        try:
            _guard_bash_command(root, command, state)
            return False
        except ValueError:
            return True

    cases = [
        (f"type {outside / 'answers.json'}", True, "absoluteOutside"),
        ("type ..\\other\\copy_answers.json", True, "parentTraversal"),
        ("cd ..", True, "cdParent"),
        ("copy ..\\..\\x.json .", True, "doubleTraversal"),
        ("type %USERPROFILE%\\answers.json", True, "envExpansion"),
        ("dir \\\\server\\share", True, "uncPath"),
        ("type inputs/A01.txt", True, "readonlyZone"),
        ("python -c \"print(1)\"", False, "plainCommand"),
        ("type report.txt", False, "insideRelative"),
        ("type sub/../report.txt", False, "insideNormalized"),
    ]
    for command, expect_block, step in cases:
        got = blocked(command)
        rows.append({"step": step, "ok": got == expect_block,
                     "blocked": got, "expect": expect_block})
    # 文件工具侧的同一边界（`_safe_path`）：越界读取同样必须拒绝
    for step, target in (("fileToolOutside", str(outside / "answers.json")),
                         ("fileToolParent", "..\\answers.json")):
        try:
            read_file(root, target, 1, 10, state)
            rows.append({"step": step, "ok": False, "blocked": False})
        except (ValueError, OSError):
            rows.append({"step": step, "ok": True, "blocked": True})
    report["sandboxBoundary"] = {
        "root": str(root), "checks": len(rows),
        "failed": [r["step"] for r in rows if not r["ok"]],
    }
    return rows, all(r["ok"] for r in rows)


def _run_value_domain(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """合法值与扁平键图（B-01 / §1.1-3）：写入前校验选项，聚合题按键登记。

    实测缺陷（2026-11-16，C-04 复测）：契约未声明 validValues 时，模型把
    "Natural disaster" 写成 "natural disasters"、"No" 写成 "None of the above"，
    官方按逐字匹配判分，整列归零；answers.json 未声明题号全集时，模型用
    deal point id 当键，17 道聚合题全判 0。
    """
    from lawbuddy.agent import deliverables as deliverable_store
    from lawbuddy.agent import work_contract
    from lawbuddy.sandbox import workspace_root

    case = _case("Harness-合法值与扁平键图")
    _task(case["id"], "合法值校验")
    root = workspace_root()
    (root / "submission").mkdir(parents=True, exist_ok=True)
    raw = {
        "name": "dp.csv", "format": "csv",
        "columns": ["agreement", "deal_point", "answer"],
        "key": ["agreement", "deal_point"],
        "questions": [{"id": "dp_multi", "text": "多选"}],
        "multiPoints": ["dp_multi"],
        "validValues": {"dp_multi": ["Natural disaster", "War or terrorism", "No"]},
        "optionKey": "deal_point", "valueColumn": "answer",
    }
    spec = deliverable_store._normalize_one(raw)  # noqa: SLF001
    rows: list[dict[str, Any]] = []

    def patch(row: dict[str, str]) -> str:
        try:
            deliverable_store.patch(root, [spec], "dp.csv", {"rows": [row]})
            return "written"
        except ValueError as exc:
            return "rejected" if "standard option" in str(exc) else f"other:{str(exc)[:60]}"

    rows.append({"step": "paraphrasedOptionRejected",
                 "ok": patch({"agreement": "A01", "deal_point": "dp_multi",
                              "answer": "natural disasters"}) == "rejected"})
    rows.append({"step": "unlistedOptionRejected",
                 "ok": patch({"agreement": "A02", "deal_point": "dp_multi",
                              "answer": "None of the above"}) == "rejected"})
    rows.append({"step": "partialMultiRejected",
                 "ok": patch({"agreement": "A03", "deal_point": "dp_multi",
                              "answer": "Natural disaster | natural disasters"}) == "rejected"})
    rows.append({"step": "exactOptionsAccepted",
                 "ok": patch({"agreement": "A04", "deal_point": "dp_multi",
                              "answer": "Natural disaster | War or terrorism"}) == "written"})
    rows.append({"step": "blankValueAccepted",
                 # 空值代表"还没答"，不能因为没过选项校验而写不进去
                 "ok": patch({"agreement": "A05", "deal_point": "dp_multi",
                              "answer": ""}) == "written"})

    # 扁平键图：JSON + 只有问题 → 题数格，不与材料数量相乘
    json_spec = deliverable_store._normalize_one({  # noqa: SLF001
        "name": "answers.json", "format": "json", "skeleton": {},
        "questions": [{"id": "M01", "text": "题1"}, {"id": "M02", "text": "题2"}],
    })
    docs = [{"documentId": f"doc{i}", "title": f"材料{i}", "versionId": "v1"}
            for i in range(16)]
    contract_json = work_contract.compile_contract([json_spec], docs)
    compiled = contract_json["artifacts"][0]
    keys = [c["cellKey"] for c in contract_json["cells"]]
    rows.append({"step": "flatJsonUsesQuestionKeys",
                 "ok": compiled["layout"] == "question_keys"
                 and len(contract_json["cells"]) == 2
                 and keys == ["M01", "M02"],
                 "layout": compiled["layout"], "cells": len(contract_json["cells"])})
    report["valueDomain"] = {"checks": len(rows)}
    return rows, all(r["ok"] for r in rows)


SEGMENTS = (
    ("contract", "契约编译(HAR-T-001/002)", _run_contract),
    ("largeLedger", "大账本无淘汰(HAR-T-002)", _run_large_ledger),
    ("searchScope", "检索范围键(HAR-T-003)", _run_search_scope),
    ("budgetRecovery", "预算恢复(HAR-T-004)", _run_budget_recovery),
    ("delivery", "交付控制器(HAR-T-005/006/007/013/014)", _run_delivery),
    ("scheduler", "调度与失败分类(HAR-T-011)", _run_scheduler),
    ("versionStale", "版本失效(HAR-T-012)", _run_version_stale),
    ("idempotency", "幂等与来源校验(HAR-T-010)", _run_idempotency),
    ("concurrency", "并发与熔断(HAR-T-008)", _run_concurrency),
    ("disconnect", "断连分类(HAR-T-008/009)", _run_disconnect),
    ("subagent", "工作型子代理幂等(HAR-T-010)", _run_subagent_idempotent),
    ("workUnitFlow", "工作单元端到端(B-04)", _run_work_unit_flow),
    ("context", "上下文组装(B-04)", _run_context_builder),
    ("runtimePinning", "运行时版本钉住(§4.4/D-01)", _run_runtime_pinning),
    ("sandboxBoundary", "跨案与原件旁路(HAR-T-015)", _run_sandbox_boundary),
    ("valueDomain", "合法值与扁平键图(B-01)", _run_value_domain),
)


def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m3_harness",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "deterministic",
    }
    try:
        ensure_workspace(str(tmp / "ws"))
        gates: dict[str, bool] = {}
        for key, _title, fn in SEGMENTS:
            gates[key] = _seg(report, key, fn)
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · Harness 专项（{r['mode']}） ===")
    for key, title, _fn in SEGMENTS:
        entry = r.get(key, {})
        print(f"{title}：{'通过' if entry.get('ok') else '未通过'}")
        for row in entry.get("rows", []):
            print(f"  {'✓' if row.get('ok') else '✗'} {row.get('step')}: "
                  f"{json.dumps({k: v for k, v in row.items() if k not in ('step', 'ok')}, ensure_ascii=False)}")
        if entry.get("error"):
            print(f"  ✗ 段异常：\n{entry['error']}")
    print("门槛判定：")
    for gate, ok in r.get("gates", {}).items():
        print(f"  {'✓' if ok else '✗'} {gate}")
    print(f"结论：{'Harness 专项通过' if r.get('passed') else 'Harness 专项未通过（见上方 ✗ 项）'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Harness 专项评测（HAR-T-001~015）")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_harness_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
