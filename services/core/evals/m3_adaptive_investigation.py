# -*- coding: utf-8 -*-
"""M3 评测器：证据/争点账本、子代理合并与可恢复自适应调查（05 方案 §10.3 C1-C8）。

用法（services/core 目录下）：
    python -m evals.m3_adaptive_investigation [--keep]

本评测全部为确定性断言（不联网、不依赖真实模型），逐项验证阶段 C 的实质能力：

1. 证据账本回查（C1）：短引真实出现在指定页 → supported；错页/错引 → needs_human
   且给出正确 reason code；超范围文档 → OUT_OF_EVIDENCE_SCOPE；低 OCR → LOW_OCR_CONFIDENCE。
2. 同源重复（C1）：同页多条引文被识别为同一来源组，不得当作独立印证；
   完全相同的条目合并为一条（不重复占用账本）。
3. 相反方向与竞争解释（C1/C2）：同一争点同时存在支持与反对证据时被标记；
   争点的构成要件、证明责任、竞争解释与缺口被保留。
4. 子代理结构化合并（C5）：findings 只是候选，经账本回查后才可能 supported；
   伪造引文仍为 needs_human；子代理自然语言结论不会自动进入账本。
5. 子代理预算与复用（C6）：共享总预算耗尽后拒绝派发；相同问题命中缓存不重复调用模型；
   多问题并行执行且各自记账。
6. 缺口驱动补查（C7）：未关闭争点进入守门修复项与警告；direct 任务不被拖入补查。
7. 跨轮恢复与 Schema 兼容（C3）：检查点恢复证据/争点账本、修复预算与子代理预算；
   Schema 版本不兼容时丢弃旧账本并在事件中标记 stale。
8. 压缩后状态可见（C4）：压缩历史后每轮重装的状态摘要仍包含证据计数与未解决争点。

报告：控制台摘要 + evals/reports/m3_adaptive_investigation_<ts>.json。门槛：全部 gates 通过。
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

DOC_TITLE = "证人证言"
PAGE_TEXT = "证人王某陈述：二〇二五年五月一日在和平路见到被告驾车离开现场。"


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


def _seed_case() -> str:
    """造一个含 3 份材料的小案件：正常页 / 低 OCR 页 / 冲突陈述页。"""
    from lawbuddy.cases import create_case
    from lawbuddy.storage import get_db

    case = create_case(title="C阶段-自适应调查", case_type="civil",
                       client_name="丁", client_role="原告代理人")
    cid = case["id"]
    fixtures = {
        "witness": (DOC_TITLE, {1: PAGE_TEXT}, "native", None),
        "ocr": ("扫描件笔录", {1: "笔录记载：被告于当日到过现场附近。"}, "ocr", 0.62),
        "complaint": ("起诉状", {1: "原告主张被告当日并不在现场，与此前陈述不一致。"}, "native", None),
    }
    db = get_db()
    ts = "2026-10-28T00:00:00+00:00"
    for key, (title, pages, quality, confidence) in fixtures.items():
        doc_id, version_id = f"doc-{key}", f"ver-{key}"
        with db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO documents(id, case_id, kind, title, status, active_version_id, "
                "created_at, updated_at) VALUES (?, ?, 'md', ?, 'ready', ?, ?, ?)",
                (doc_id, cid, title, version_id, ts, ts),
            )
            conn.execute(
                "INSERT OR REPLACE INTO document_versions(id, document_id, blob_uri, sha256, size_bytes, "
                "parse_status, page_count, created_at) VALUES (?, ?, ?, ?, 0, 'ready', ?, ?)",
                (version_id, doc_id, f"blob://{doc_id}", "0" * 64, len(pages), ts),
            )
            for page_no, text in pages.items():
                conn.execute(
                    "INSERT OR REPLACE INTO document_pages(id, version_id, page_no, text_content, "
                    "text_quality, ocr_status, ocr_confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (f"{version_id}-p{page_no}", version_id, page_no, text, quality,
                     "done" if quality == "ocr" else "none", confidence),
                )
    return cid


def _state(case_id: str, depth: str = "focused", task_id: str = "task-c") -> dict[str, Any]:
    """构造 Loop 运行期 state（与 run_autonomous 内的键保持一致）。"""
    from lawbuddy.agent.autonomous import _derive_task_contract

    state: dict[str, Any] = {
        "task_id": task_id, "case_id": case_id, "mode": "legal_review",
        "request": "分析被告是否在场", "workflow_preset": "",
        "reading_ledger": {}, "evidence_ledger": {}, "issue_ledger": {},
        "_round": 3, "_usage_total": {"promptTokens": 0, "completionTokens": 0},
        "_emit": lambda *_a, **_k: None,
    }
    contract = _derive_task_contract(state)
    contract["taskDepth"] = depth
    state["task_contract"] = contract
    return state


# ---------------------------------------------------------------------------
# 1. 证据账本回查（C1）
# ---------------------------------------------------------------------------

def _run_evidence_verification(report: dict[str, Any], case_id: str) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.autonomous import _ledger_stats, _record_evidence

    state = _state(case_id)
    result = _record_evidence(state, [
        {"claim": "被告当日出现在现场", "documentTitle": DOC_TITLE, "pageNo": 1,
         "quote": "二〇二五年五月一日在和平路见到被告驾车离开现场"},
        {"claim": "被告在场（错引）", "documentTitle": DOC_TITLE, "pageNo": 1,
         "quote": "被告承认当日不在现场"},
        {"claim": "跨案外文档", "documentTitle": "不存在的卷宗", "pageNo": 1, "quote": "任意片段"},
        {"claim": "OCR 页命题", "documentTitle": "扫描件笔录", "pageNo": 1, "quote": "被告于当日到过现场附近"},
        {"claim": "分析推断（无来源）", "layer": "inference", "quote": ""},
    ], docs=None)
    entries = {(e["claim"], e["status"], e["reasonCode"]) for e in result["entries"]}
    expectations = {
        ("被告当日出现在现场", "supported", None),
        ("被告在场（错引）", "needs_human", "QUOTE_MISMATCH"),
        ("跨案外文档", "needs_human", "OUT_OF_EVIDENCE_SCOPE"),
        ("OCR 页命题", "needs_human", "LOW_OCR_CONFIDENCE"),
        ("分析推断（无来源）", "needs_human", "OUT_OF_EVIDENCE_SCOPE"),
    }
    stats = _ledger_stats(state)
    rows = [{
        "step": "reasonCodes",
        "ok": expectations.issubset(entries),
        "entries": sorted(f"{c}|{s}|{r}" for c, s, r in entries),
        "supported": stats["evidenceSupported"], "needsHuman": stats["evidenceNeedsHuman"],
    }]
    # 无来源的推断不得因为"写了 inference"就被当作已支持事实
    rows.append({
        "step": "inferenceNotSupported",
        "ok": stats["evidenceSupported"] == 1,
        "supported": stats["evidenceSupported"],
    })
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# 2. 同源重复（C1）
# ---------------------------------------------------------------------------

def _run_same_source(report: dict[str, Any], case_id: str) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.autonomous import _ledger_stats, _record_evidence

    state = _state(case_id)
    first = _record_evidence(state, [
        {"claim": "见到被告驾车离开", "documentTitle": DOC_TITLE, "pageNo": 1,
         "quote": "见到被告驾车离开现场", "issueId": "是否在场"},
        {"claim": "时间点为五月一日", "documentTitle": DOC_TITLE, "pageNo": 1,
         "quote": "二〇二五年五月一日在和平路", "issueId": "是否在场"},
    ])
    # 完全相同的条目再次提交：合并，不新增
    second = _record_evidence(state, [
        {"claim": "见到被告驾车离开", "documentTitle": DOC_TITLE, "pageNo": 1,
         "quote": "见到被告驾车离开现场", "issueId": "是否在场"},
    ])
    stats = _ledger_stats(state)
    rows = [
        {"step": "dedupeIdentical", "ok": second["added"] == 0 and second["updated"] == 1
         and len(state["evidence_ledger"]) == 2,
         "added": second["added"], "updated": second["updated"],
         "total": len(state["evidence_ledger"])},
        {"step": "sameSourceGrouped", "ok": bool(stats["sameSourceGroups"])
         and "第1页" in stats["sameSourceGroups"][0],
         "groups": stats["sameSourceGroups"]},
        {"step": "firstSubmitAdded", "ok": first["added"] == 2, "added": first["added"]},
    ]
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# 3. 相反方向与竞争解释（C1/C2）
# ---------------------------------------------------------------------------

def _run_issue_ledger(report: dict[str, Any], case_id: str) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.autonomous import _ledger_stats, _record_evidence, _record_issues

    state = _state(case_id)
    issues = _record_issues(state, [{
        "id": "是否在场", "issue": "被告是否于当日出现在现场",
        "claimBasis": "侵权责任构成要件之一：行为",
        "burden": "原告承担举证责任",
        "alternatives": ["证人记忆错误", "在场但未实施行为"],
        "gaps": ["缺少监控录像", "证人未出庭作证"],
        "status": "open",
    }])
    _record_evidence(state, [
        {"claim": "证人见到被告驾车离开", "documentTitle": DOC_TITLE, "pageNo": 1,
         "quote": "见到被告驾车离开现场", "issueId": "是否在场", "direction": "supports"},
        {"claim": "起诉状称被告不在现场", "documentTitle": "起诉状", "pageNo": 1,
         "quote": "原告主张被告当日并不在现场", "issueId": "是否在场", "direction": "opposes"},
    ])
    stats = _ledger_stats(state)
    issue = state["issue_ledger"]["是否在场"]
    rows = [
        {"step": "oppositeDirectionFlagged",
         "ok": "是否在场" in stats["oppositeDirectionIssues"],
         "flagged": stats["oppositeDirectionIssues"]},
        {"step": "competitionKept",
         "ok": issue["alternatives"] == ["证人记忆错误", "在场但未实施行为"]
         and issue["gaps"] and issue["burden"] == "原告承担举证责任"
         and issue["claimBasis"].startswith("侵权责任"),
         "issue": issue},
        {"step": "openIssueReported",
         "ok": stats["openIssueCount"] == 1 and stats["openIssues"][0].startswith("被告是否"),
         "open": stats["openIssues"]},
        {"step": "issuesRecorded", "ok": issues["added"] == 1, "added": issues["added"]},
    ]
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# 4-5. 子代理结构化合并、预算与复用（C5/C6）
# ---------------------------------------------------------------------------

def _fake_gateway(script: list[dict[str, Any]], counter: dict[str, int]):
    """返回脚本化 chat_with_tools（按调用顺序推进，耗尽后重复最后一步）。"""
    from lawbuddy.agent.model_gateway import ToolCall

    def fake(messages, tools=None, model=None, **_kwargs):
        idx = min(counter["n"], len(script) - 1)
        counter["n"] += 1
        step = script[idx]
        calls = [ToolCall(f"c{idx}-{i}", name, json.dumps(args, ensure_ascii=False))
                 for i, (name, args) in enumerate(step.get("tools") or [])]
        return {
            "content": step.get("content") or "",
            "toolCalls": calls,
            "usage": {"promptTokens": 100, "completionTokens": 40},
        }

    return fake


def _run_subagent_merge(report: dict[str, Any], case_id: str) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import model_gateway
    from lawbuddy.agent.autonomous import _record_evidence, _run_case_explore

    payload = {
        "findings": [
            {"claim": "证人见到被告驾车离开", "documentTitle": DOC_TITLE, "pageNo": 1,
             "quote": "见到被告驾车离开现场", "direction": "supports", "layer": "record"},
            {"claim": "被告承认不在现场", "documentTitle": DOC_TITLE, "pageNo": 1,
             "quote": "被告承认不在现场", "direction": "opposes", "layer": "record"},
        ],
        "conflicts": [{"issue": "是否在场", "sides": ["证人证言", "起诉状陈述"]}],
        "unresolved": ["缺少监控录像"],
        "suggestedChecks": ["核对证人是否出庭"],
    }
    content = "汇总结论：证人与起诉状陈述相反。\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    state = _state(case_id)
    counter = {"n": 0}
    original = model_gateway.chat_with_tools
    model_gateway.chat_with_tools = _fake_gateway([{"content": content}], counter)
    try:
        explored = _run_case_explore(state, "查证被告是否在场", "outer-1")
        again = _run_case_explore(state, "查证被告是否在场", "outer-1")
    finally:
        model_gateway.chat_with_tools = original

    merged = _record_evidence(state, explored["findings"], origin="subagent")
    statuses = {(e["claim"], e["status"], e["reasonCode"]) for e in merged["entries"]}
    from lawbuddy.agent.autonomous import _efficiency_summary

    eff = _efficiency_summary(state)
    sub = eff.get("subagents") or {}
    rows = [
        {"step": "structuredReturn",
         "ok": explored["structured"] is True and len(explored["findings"]) == 2
         and explored["conflicts"] and explored["unresolved"] == ["缺少监控录像"],
         "structured": explored["structured"], "findings": len(explored["findings"])},
        {"step": "candidateNotAutoSupported",
         "ok": ("证人见到被告驾车离开", "supported", None) in statuses
         and ("被告承认不在现场", "needs_human", "QUOTE_MISMATCH") in statuses,
         "statuses": sorted(f"{c}|{s}|{r}" for c, s, r in statuses)},
        {"step": "conclusionNotInLedger",
         "ok": len(state["evidence_ledger"]) == 2,
         "ledger": len(state["evidence_ledger"])},
        {"step": "duplicateQuestionCached",
         "ok": again.get("cached") is True and counter["n"] == 1,
         "calls": counter["n"]},
        # 子 Agent 采用率（§10.4 D4）：候选 2 条均以同一出处进入证据账本；
        # 缓存命中不重复计入派发，但可单独计量
        {"step": "subagentAdoptionMeasured",
         "ok": sub.get("dispatched") == 1 and sub.get("cached") == 1
         and sub.get("findings") == 2 and sub.get("findingsAdopted") == 2
         and sub.get("adoptionRate") == 1.0 and int(sub.get("rounds") or 0) >= 1,
         "subagents": sub, "efficiency": {k: v for k, v in eff.items() if k != "subagents"}},
    ]
    return rows, all(r["ok"] for r in rows)


def _run_explore_budget(report: dict[str, Any], case_id: str) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import model_gateway
    from lawbuddy.agent.autonomous import (
        EXPLORE_TOTAL_ROUND_BUDGET, _run_case_explore, _run_case_explore_many,
    )
    from lawbuddy.agent.tasks import TaskCancelled

    original = model_gateway.chat_with_tools
    rows: list[dict[str, Any]] = []
    try:
        # 预算耗尽：直接拒绝，且不得调用模型
        exhausted = _state(case_id)
        exhausted["_explore_rounds_used"] = EXPLORE_TOTAL_ROUND_BUDGET
        counter = {"n": 0}
        model_gateway.chat_with_tools = _fake_gateway([{"content": "x"}], counter)
        denied = _run_case_explore(exhausted, "再查一次", "c")
        rows.append({"step": "budgetExhaustedRejected",
                     "ok": "error" in denied and counter["n"] == 0,
                     "error": denied.get("error"), "calls": counter["n"]})

        # 并行：两个独立问题各 1 轮，共享预算累计 2 轮
        parallel_state = _state(case_id)
        counter = {"n": 0}
        model_gateway.chat_with_tools = _fake_gateway([{"content": "分支结论（无来源）"}], counter)
        parallel = _run_case_explore_many(parallel_state, ["问题甲", "问题乙", "问题甲"], "outer-2")
        rows.append({
            "step": "parallelIndependent",
            "ok": parallel.get("parallel") == 2 and len(parallel.get("results") or []) == 2
            and parallel_state["_explore_rounds_used"] == 2,
            "parallel": parallel.get("parallel"),
            "roundsUsed": parallel_state["_explore_rounds_used"],
            "calls": counter["n"],
        })

        # 取消传播：并行分支同样要把取消抛出
        cancel_state = _state(case_id)
        cancel_state["_cancel_check"] = lambda: True
        counter = {"n": 0}
        model_gateway.chat_with_tools = _fake_gateway([{"content": "x"}], counter)
        try:
            _run_case_explore_many(cancel_state, ["问题甲", "问题乙"], "outer-3")
        except TaskCancelled:
            cancelled = True
        else:
            cancelled = False
        rows.append({"step": "cancelPropagates", "ok": cancelled})
    finally:
        model_gateway.chat_with_tools = original
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# 6. 缺口驱动补查（C7）
# ---------------------------------------------------------------------------

def _run_gap_driven_repair(report: dict[str, Any], case_id: str) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.autonomous import _finalize_gate, _record_issues

    answer = f"《{DOC_TITLE}》第1页，原文「{PAGE_TEXT}」。"
    focused = _state(case_id, depth="focused")
    _record_issues(focused, [{
        "id": "是否在场", "issue": "被告是否于当日出现在现场",
        "gaps": ["缺少监控录像"], "status": "open",
    }])
    gate = _finalize_gate(focused, answer, allow_repairs=True)
    codes = [w["code"] for w in gate["warnings"]]
    repair_codes = [r["code"] for r in gate["repairs"]]
    rows = [
        {"step": "openIssueWarns", "ok": "OPEN_ISSUES" in codes, "codes": codes},
        {"step": "gapRepairTargeted",
         "ok": "OPEN_ISSUES" in repair_codes
         and any("缺少监控录像" in str(r.get("instruction") or "") for r in gate["repairs"]),
         "repairs": repair_codes},
    ]
    # direct 定位任务：即使账本里有 open 争点，也不应被拖入额外调查
    direct = _state(case_id, depth="direct")
    _record_issues(direct, [{"id": "x", "issue": "某个争点", "status": "open"}])
    direct_gate = _finalize_gate(direct, answer, allow_repairs=True)
    rows.append({
        "step": "directNotDragged",
        "ok": all(w["code"] != "OPEN_ISSUES" for w in direct_gate["warnings"]),
        "codes": [w["code"] for w in direct_gate["warnings"]],
    })
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# 7. 跨轮恢复与 Schema 兼容（C3）
# ---------------------------------------------------------------------------

def _run_checkpoint_restore(report: dict[str, Any], case_id: str) -> tuple[list[dict[str, Any]], bool]:
    from unittest.mock import patch

    from lawbuddy.agent.autonomous import LEDGER_SCHEMA_VERSION
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import create_task, get_events, save_checkpoint, update_lifecycle

    evidence = {
        f"doc-witness:1:{'quote'}": {
            "id": "doc-witness:1:quote", "claim": "证人见到被告", "layer": "record",
            "direction": "supports", "issueId": "是否在场",
            "source": {"documentId": "doc-witness", "documentTitle": DOC_TITLE, "pageNo": 1,
                       "quote": "见到被告驾车离开现场"},
            "status": "supported", "reasonCode": None, "origin": "model", "round": 2,
        }
    }
    issues = {"是否在场": {"id": "是否在场", "issue": "被告是否在场", "status": "open",
                           "claimBasis": None, "burden": None, "alternatives": [], "gaps": [],
                           "round": 2}}

    def _prepared(version: int) -> str:
        task = create_task(case_id, "legal_review", "恢复测试")
        save_checkpoint(task["id"], "round_2", {
            "messages": [{"role": "system", "content": "（基础提示）"},
                         {"role": "user", "content": "分析被告是否在场"}],
            "taskContract": {"taskDepth": "focused", "objective": "分析被告是否在场"},
            "readingLedger": {},
            "taskPlan": [{"text": "读证人证言", "status": "done"},
                         {"text": "核对争点", "status": "pending"}],
            "evidenceLedger": evidence,
            "issueLedger": issues,
            "ledgerSchemaVersion": version,
            "repairRoundsLeft": 1,
            "budget": {"roundsUsed": 2, "exploreRoundsUsed": 5},
        })
        update_lifecycle(task["id"], "FAILED")
        return task["id"]

    captured: list[dict[str, Any]] = []

    def capture(state, emit, cancel_check):
        captured.append(dict(state))
        return {"answer": ""}

    runner = AgentRunner()
    rows: list[dict[str, Any]] = []
    with patch.object(runner, "_ensure_workers"), \
            patch("lawbuddy.agent.runner.run_autonomous", side_effect=capture):
        ok_task = _prepared(LEDGER_SCHEMA_VERSION)
        runner.start(ok_task)
        runner._run(ok_task)
        state_ok = captured[-1]
        events = get_events(ok_task)
        resume_evt = next((e["payload"] for e in events if e["type"] == "resume"), {})
        rows.append({
            "step": "ledgersRestored",
            "ok": len(state_ok.get("evidence_ledger") or {}) == 1
            and "是否在场" in (state_ok.get("issue_ledger") or {})
            and state_ok.get("_repair_rounds_left") == 1
            and state_ok.get("_explore_rounds_used") == 5
            and [p.get("text") for p in (state_ok.get("task_plan") or [])] == ["读证人证言", "核对争点"]
            and resume_evt.get("restoredEvidenceLedger") == 1
            and resume_evt.get("restoredIssueLedger") == 1
            and resume_evt.get("ledgerSchemaStale") is False,
            "resumeEvent": resume_evt,
            "repairRoundsLeft": state_ok.get("_repair_rounds_left"),
            "exploreUsed": state_ok.get("_explore_rounds_used"),
            "plan": state_ok.get("task_plan"),
        })

        stale_task = _prepared(LEDGER_SCHEMA_VERSION + 99)
        runner.start(stale_task)
        runner._run(stale_task)
        state_stale = captured[-1]
        stale_evt = next((e["payload"] for e in get_events(stale_task) if e["type"] == "resume"), {})
        rows.append({
            "step": "incompatibleSchemaDropped",
            "ok": not (state_stale.get("evidence_ledger") or {})
            and not (state_stale.get("issue_ledger") or {})
            and stale_evt.get("ledgerSchemaStale") is True,
            "staleEvent": stale_evt,
        })
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# 8. 压缩后状态可见（C4）
# ---------------------------------------------------------------------------

def _run_compaction_state(report: dict[str, Any], case_id: str) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.autonomous import (
        _STATE_MARK, _compact_messages, _ledger_summary, _plan_section, _record_evidence,
        _record_issues,
    )

    state = _state(case_id, depth="full_review")
    _record_evidence(state, [
        {"claim": "证人见到被告驾车离开", "documentTitle": DOC_TITLE, "pageNo": 1,
         "quote": "见到被告驾车离开现场", "issueId": "是否在场"},
    ])
    _record_issues(state, [{"id": "是否在场", "issue": "被告是否当日出现在现场",
                            "gaps": ["缺少监控录像"], "status": "open"}])
    base = "你是 LawBuddy。"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": base + _plan_section([{"text": "读材料", "status": "done"}])},
    ]
    for i in range(12):
        messages.append({"role": "assistant", "content": f"第 {i} 轮过程说明" + "字" * 8000,
                         "tool_calls": [{"id": f"t{i}", "type": "function",
                                         "function": {"name": "document_search",
                                                      "arguments": "{\"query\":\"在场\"}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"t{i}", "content": "结果" * 1000})
    compacted, stats = _compact_messages(messages)
    # 每轮重装：基础 system + 清单 + 状态摘要（与 run_autonomous 内的做法一致）
    compacted[0] = {"role": "system",
                    "content": base + _plan_section([{"text": "读材料", "status": "done"}])
                    + _ledger_summary(state)}
    content = str(compacted[0]["content"])
    rows = [
        {"step": "historyCompacted", "ok": stats["compactedMessages"] > 0,
         "compacted": stats["compactedMessages"], "after": stats["after"]},
        {"step": "stateSummaryAfterCompaction",
         "ok": _STATE_MARK in content and "证据账本：1 条" in content
         and "未解决争点 1 项" in content,
         "summary": _ledger_summary(state)},
        {"step": "sourceNotFromDigest",
         "ok": "见到被告驾车离开现场" in (state["evidence_ledger"][
             next(iter(state["evidence_ledger"]))]["source"]["quote"]),
         "ledgerKeys": list(state["evidence_ledger"])},
    ]
    return rows, all(r["ok"] for r in rows)


def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-c-"))
    report: dict[str, Any] = {
        "evalName": "m3_adaptive_investigation",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "deterministic",
    }
    try:
        ensure_workspace(str(tmp / "ws"))
        cid = _seed_case()
        report["caseId"] = cid
        gates = {
            "evidenceVerification": _seg(report, "evidenceVerification",
                                         _run_evidence_verification, cid),
            "sameSource": _seg(report, "sameSource", _run_same_source, cid),
            "issueLedger": _seg(report, "issueLedger", _run_issue_ledger, cid),
            "subagentMerge": _seg(report, "subagentMerge", _run_subagent_merge, cid),
            "exploreBudget": _seg(report, "exploreBudget", _run_explore_budget, cid),
            "gapDrivenRepair": _seg(report, "gapDrivenRepair", _run_gap_driven_repair, cid),
            "checkpointRestore": _seg(report, "checkpointRestore", _run_checkpoint_restore, cid),
            "compactionState": _seg(report, "compactionState", _run_compaction_state, cid),
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · 自适应调查与账本（{r['mode']}） ===")
    for name, entry in r.items():
        if name in ("evalName", "startedAt", "finishedAt", "mode", "gates", "passed", "caseId"):
            continue
        print(f"  [{'✓' if entry.get('ok') else '✗'}] {name}")
        for row in entry.get("rows") or []:
            detail = {k: v for k, v in row.items() if k not in ("step", "ok")}
            print(f"      {'✓' if row.get('ok') else '✗'} {row.get('step')}: "
                  f"{json.dumps(detail, ensure_ascii=False)[:400]}")
        if entry.get("error"):
            print("      " + entry["error"].replace("\n", "\n      ")[:2000])
    print("门槛判定：")
    for g, ok in r.get("gates", {}).items():
        print(f"  {'✓' if ok else '✗'} {g}")
    print(f"结论：{'全部通过' if r.get('passed') else '存在失败项（见上方 ✗）'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="自适应调查与账本评测")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_adaptive_investigation_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
