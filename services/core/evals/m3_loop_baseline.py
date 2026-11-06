# -*- coding: utf-8 -*-
"""M3 评测器：阅卷主 Loop 质量与成本基线（05-阅卷Agent升级方案 §10.1 A1 / §11.4）。

用法（services/core 目录下）：
    python -m evals.m3_loop_baseline [--keep]

目的：在引入 Task Contract 与完成守门（阶段 B）之前，先量出当前自主 Loop 在
三类任务深度上的真实成本与引用质量，作为后续优化前后对照的基线。本评测
不做“必须更省”的门槛，只固定两类事实：

1. 三类深度都能跑完并产出可核验引用（质量不因深度不同而退化）；
2. 记录轮次、模型调用、token、工具调用、读取覆盖与耗时（成本可比较）。

任务深度（阶段 B 的 Task Contract 落地前的等价划分）：
- direct      ：单点定位，1 次检索 → 直接作答；
- focused     ：单争点，材料清单 + 读一份材料全部页 + 一次检索；
- full_review ：全卷阅卷，材料清单 + 逐份读原文 + 记忆 + 多点检索。

成本口径来自 turn_complete 与工具事件（真实实现），模型侧为确定性 mock
（每次调用固定 100/40 token），因此 token 数值只用于前后对照，不代表真实模型消耗。

报告：控制台摘要 + evals/reports/m3_loop_baseline_<ts>.json。
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

# 三类深度：请求文本与期望读取规模（用于断言读取确实随深度增加）
DEPTHS = ("direct", "focused", "full_review")


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


def _wait_terminal(task_id: str, timeout: float = 120.0) -> dict[str, Any]:
    from lawbuddy.agent.tasks import get_task

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = get_task(task_id)
        if task["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS", "CANCELLED", "FAILED"):
            return task
        time.sleep(0.05)
    return get_task(task_id)


def _seed_case() -> tuple[str, dict[str, str]]:
    """造一个三份材料的小案件，返回 (caseId, {docKey: documentId})。"""
    case, ids = _seed_docs("M3评测-Loop基线", {
        "contract": ("买卖合同", {
            1: "第一条 甲方杭州甲方向乙方上海乙方出售办公设备一批，总价人民币三十万元整。",
            2: "第二条 乙方应于二〇二五年十二月三十一日前支付全部价款，逾期按日万分之五计收违约金。",
        }),
        "delivery": ("送货单", {
            1: "今收到办公设备一批，共计人民币三十万元整，收货人签章齐全。",
        }),
        "chat": ("微信聊天记录", {
            1: "乙方称：设备已于二〇二五年六月交付，但发现两台打印机存在故障。",
        }),
    })
    return case["id"], ids


def _seed_docs(
    title: str, fixtures: dict[str, tuple[str, dict[int, str]]]
) -> tuple[dict[str, Any], dict[str, str]]:
    """造一个案件并写入指定语料形态，返回 (case, {docKey: documentId})。

    语料形态（页数与单页长度）是 §11.4 第 5 条的自变量，因此夹具必须可参数化：
    只测单一「3 文档 4 页」形态无法暴露长页续读、大量短页游标与多文档覆盖问题。
    """
    from lawbuddy.storage import get_db
    from lawbuddy.cases import create_case

    case = create_case(title=title, case_type="civil", client_name="丙", client_role="原告代理人")
    cid = case["id"]
    db = get_db()
    ts = "2026-10-28T00:00:00+00:00"
    ids: dict[str, str] = {}
    for key, (doc_title, pages) in fixtures.items():
        doc_id = f"doc-{key}"
        version_id = f"ver-{key}"
        ids[key] = doc_id
        with db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO documents(id, case_id, kind, title, status, active_version_id, "
                "created_at, updated_at) VALUES (?, ?, 'md', ?, 'ready', ?, ?, ?)",
                (doc_id, cid, doc_title, version_id, ts, ts),
            )
            conn.execute(
                "INSERT OR REPLACE INTO document_versions(id, document_id, blob_uri, sha256, size_bytes, "
                "parse_status, page_count, created_at) VALUES (?, ?, ?, ?, 0, 'ready', ?, ?)",
                (version_id, doc_id, f"blob://{doc_id}", "0" * 64, len(pages), ts),
            )
            for page_no, text in pages.items():
                conn.execute(
                    "INSERT OR REPLACE INTO document_pages(id, version_id, page_no, text_content, "
                    "text_quality, ocr_status, ocr_confidence) VALUES (?, ?, ?, ?, 'native', 'none', NULL)",
                    (f"{version_id}-p{page_no}", version_id, page_no, text),
                )
                conn.execute(
                    "INSERT INTO pages_fts(text_content, case_id, document_id, document_title, version_id, "
                    "page_no, text_quality) VALUES (?, ?, ?, ?, ?, ?, 'native')",
                    (text, cid, doc_id, doc_title, version_id, page_no),
                )
    return case, ids


def _steps(depth: str, ids: dict[str, str]) -> list:
    """按任务深度构造 mock 脚本：direct 单点、focused 单争点、full_review 全卷。"""
    from evals._loop_mock import ScriptStep

    final_direct = (
        "《送货单》第1页，原文「今收到办公设备一批，共计人民币三十万元整」，可证交付与价款。"
    )
    final_focused = (
        "争点：价款是否已付。\n"
        "1. 《买卖合同》第2页，原文「乙方应于二〇二五年十二月三十一日前支付全部价款」，"
        "可见付款期限为二〇二五年十二月三十一日。\n"
        "2. 《送达回证》未在材料中出现，付款凭证缺失，需补充银行流水。"
    )
    final_full = (
        "一、合同关系：《买卖合同》第1页，原文「甲方杭州甲方向乙方上海乙方出售办公设备一批，"
        "总价人民币三十万元整」。\n"
        "二、交付事实：《送货单》第1页，原文「今收到办公设备一批，共计人民币三十万元整」。\n"
        "三、抗辩线索：《微信聊天记录》第1页，原文「发现两台打印机存在故障」，"
        "乙方可能主张质量抗辩，与交付单的签章齐全形成张力，待核实故障是否在交付时已存在。\n"
        "四、待核实：付款凭证缺失，违约金计算起点尚不能从原文确认。"
    )
    if depth == "direct":
        return [
            ScriptStep.tool("document_search", {"query": "办公设备", "limit": 6}),
            ScriptStep.final(final_direct),
        ]
    if depth == "focused":
        return [
            ScriptStep.tool("documents_list", {}),
            ScriptStep.tool("document_get_pages", {"documentId": ids["contract"], "pageFrom": 1, "pageTo": 2}),
            ScriptStep.tool("document_search", {"query": "价款", "limit": 6}),
            ScriptStep.final(final_focused),
        ]
    return [
        ScriptStep.tool("documents_list", {}),
        ScriptStep.tool("document_get_pages", {"documentId": ids["contract"], "pageFrom": 1, "pageTo": 2}),
        ScriptStep.tool("document_get_pages", {"documentId": ids["delivery"], "pageFrom": 1, "pageTo": 1}),
        ScriptStep.tool("document_get_pages", {"documentId": ids["chat"], "pageFrom": 1, "pageTo": 1}),
        ScriptStep.tool("document_search", {"query": "付款", "limit": 6}),
        ScriptStep.tool("document_search", {"query": "故障", "limit": 6}),
        ScriptStep.final(final_full),
    ]


def _measure(cid: str, ids: dict[str, str], depth: str) -> dict[str, Any]:
    """跑一次任务并采集质量与成本。"""
    request = {
        "direct": "送货单能不能证明交付和价款？",
        "focused": "分析价款是否已支付的争点",
        "full_review": "全卷阅卷：梳理合同关系、交付事实与抗辩线索",
    }[depth]
    return _measure_steps(cid, request, _steps(depth, ids), depth)


def _measure_steps(
    cid: str, request: str, steps: list, depth: str = "",
) -> dict[str, Any]:
    """按给定脚本跑一次任务，采集质量、成本与完成守门结论（05 方案 §7）。"""
    from evals._loop_mock import install_mock_gateway, last_efforts
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import create_task, get_events

    task = create_task(case_id=cid, mode="legal_review", request_text=request)
    undo = install_mock_gateway(steps)
    started = time.monotonic()
    try:
        AgentRunner.instance().start(task["id"])
        final_task = _wait_terminal(task["id"])
    finally:
        undo()
        efforts = last_efforts()
    wall = time.monotonic() - started

    events = get_events(task["id"])
    loop_events = [e for e in events if e["type"] in
                   ("text", "tool_use_start", "tool_use_done", "assistant_message",
                    "tool_result_message", "turn_complete")]
    turns = [e for e in loop_events if e["type"] == "turn_complete"]
    turn = turns[-1]["payload"] if turns else {}
    assistant = [e for e in loop_events if e["type"] == "assistant_message"]
    answer = str(assistant[-1]["payload"].get("content") or "") if assistant else ""

    # 工具成本与读取覆盖：按 document_get_pages 实际返回的页数统计
    starts = [e["payload"] for e in loop_events if e["type"] == "tool_use_start"]
    results = [e["payload"] for e in loop_events if e["type"] == "tool_result_message"]
    read_pages: set[tuple[str, int]] = set()
    read_chars = 0
    read_call_chars: list[int] = []
    read_slices: list[dict[str, Any]] = []
    for r in results:
        body = r.get("result") or {}
        if not isinstance(body, dict) or "pages" not in body:
            continue
        doc_id = str(body.get("documentId") or "")
        call_chars = 0
        for p in body.get("pages") or []:
            if int(p.get("textTotal") or 0) > 0:
                read_pages.add((doc_id, int(p.get("pageNo") or 0)))
            text = str(p.get("text") or "")
            call_chars += len(text)
            if text:
                read_slices.append({
                    "documentId": doc_id, "pageNo": int(p.get("pageNo") or 0),
                    "textStart": int(p.get("textStart") or 0), "chars": len(text),
                    "textTotal": int(p.get("textTotal") or 0),
                })
        read_chars += call_chars
        read_call_chars.append(call_chars)

    verification = next((e["payload"] for e in events if e["type"] == "answer_verification"), {})
    finalize = turn.get("finalize") or {}
    return {
        "depth": depth,
        "taskId": task["id"],
        "lifecycle": final_task["lifecycle"],
        "rounds": turn.get("rounds"),
        "toolCalls": len(starts),
        "toolNames": [str(s.get("name") or "") for s in starts],
        "readPages": len(read_pages),
        "readChars": read_chars,
        "readCallChars": read_call_chars,
        "readSlices": read_slices,
        "promptTokens": turn.get("promptTokens"),
        "completionTokens": turn.get("completionTokens"),
        "durationMs": turn.get("durationMs"),
        "wallMs": int(wall * 1000),
        "answerChars": len(answer),
        "answer": answer[:120],
        "citeTotal": verification.get("total"),
        "citeSupported": verification.get("supported"),
        "citePending": verification.get("pendingCount"),
        "forcedFinal": turn.get("forcedFinal"),
        # 分阶段推理档位（§10.4 D3）：逐次调用的档位序列 + 各阶段调用次数与顺序
        "efforts": [e.get("effort") for e in efforts],
        "reasoningStages": turn.get("reasoningStages") or {},
        "stageTrace": turn.get("reasoningStageTrace") or [],
        # 效率指标（§10.4 D4）：重复读取、无效检索、子 Agent 采用率
        "efficiency": turn.get("efficiency") or {},
        # 上下文成本（§10.4 D6）：逐次模型的 prompt 字符量与工具条数
        "promptChars": sum(int(e.get("promptChars") or 0) for e in efforts),
        "promptCharsPerCall": round(
            sum(int(e.get("promptChars") or 0) for e in efforts) / len(efforts), 1
        ) if efforts else None,
        "toolCount": max((int(e.get("toolCount") or 0) for e in efforts), default=0),
        "toolSchemaChars": max((int(e.get("toolSchemaChars") or 0) for e in efforts), default=0),
        # 完成守门（§7.1/§7.3）：深度、是否干净完成、警告原因码、覆盖统计
        "taskDepth": finalize.get("taskDepth"),
        "finalizeOk": finalize.get("ok"),
        "warningCodes": [str(w.get("code") or "") for w in finalize.get("warnings") or []],
        "coverage": finalize.get("coverage"),
    }


def _run_baseline(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    cid, ids = _seed_case()
    rows = [_measure(cid, ids, depth) for depth in DEPTHS]
    total_pages = 4  # 夹具页数：合同 2 + 送货单 1 + 聊天 1

    for row in rows:
        row["ok"] = (
            row["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS")
            and bool(row["answerChars"])
            and (row["citeSupported"] or 0) >= 1
        )
        row["readCoverage"] = round(row["readPages"] / total_pages, 3)

    direct, focused, full = rows
    # 深度差异自检：脚本层面的读取规模必须体现 direct < focused ≤ full_review
    depth_gradient = (direct["toolCalls"] < focused["toolCalls"] <= full["toolCalls"]
                      and direct["readPages"] < full["readPages"])
    report["baseline"] = {
        "caseId": cid,
        "totalPages": total_pages,
        "rows": [{k: v for k, v in r.items() if k != "ok"} for r in rows],
        "totalToolCalls": sum(r["toolCalls"] for r in rows),
        "totalModelCalls": sum(int(r["rounds"] or 0) for r in rows),
        "totalTokens": sum(int(r["promptTokens"] or 0) + int(r["completionTokens"] or 0) for r in rows),
        "totalDurationMs": sum(int(r["durationMs"] or 0) for r in rows),
        "effortsByDepth": {r["depth"]: r["efforts"] for r in rows},
        "stageTraceByDepth": {r["depth"]: r["stageTrace"] for r in rows},
        "notes": [
            "模型侧为确定性 mock（每次调用固定 100/40 token），token 仅用于前后对照",
            "阶段 B 引入 Task Contract 与完成守门后，用同一夹具复测本报告各项数值",
            "阶段 D 起记录逐次推理档位与阶段序列（§10.4 D3）：读页轮降档、修复轮升档",
        ],
    }
    rows.append({"step": "depthGradient", "ok": depth_gradient,
                 "toolCalls": [r["toolCalls"] for r in rows[:3]],
                 "readPages": [r["readPages"] for r in rows[:3]]})
    rows.extend(_gate_rows(rows[:3]))
    # 反例（§7.2）：声称全卷阅卷却只检索不读页，必须落为带警告完成而不是干净完成。
    # 脚本重复给出同一终稿，模拟模型在修复轮里仍不补读——覆盖缺口应保留为警告。
    shallow_steps = _steps("focused", ids)
    shallow_steps += [_steps("focused", ids)[-1]] * 2
    shallow = _measure_steps(
        cid, "全卷阅卷：梳理合同关系、交付事实与抗辩线索",
        shallow_steps, "full_review_shallow",
    )
    report["shallowFullReview"] = {k: v for k, v in shallow.items() if k != "ok"}
    rows.extend(_stage_effort_rows(rows[:3], shallow))
    rows.extend(_efficiency_rows(rows[:3]))
    rows.extend(_decomposed_search_rows(cid, ids, full))
    rows.extend(_exclusion_rows(cid, ids))
    rows.extend(_ledger_state_rows(cid, ids))
    rows.extend(_lifecycle_rows(cid, ids))
    # 反例（§10.4 D4）：同一页重复读 + 同一检索词重复搜，指标必须测得出来，
    # 且重复检索直接复用上次结果（不重复查询、不重复消耗）
    from evals._loop_mock import ScriptStep

    waste_steps = _steps("focused", ids)
    waste_steps = waste_steps[:-1] + [
        ScriptStep.tool("document_get_pages", {"documentId": ids["contract"], "pageFrom": 1, "pageTo": 2}),
        ScriptStep.tool("document_search", {"query": "价款", "limit": 6}),
        waste_steps[-1],
    ]
    waste = _measure_steps(cid, "分析价款是否已支付的争点", waste_steps, "focused_wasteful")
    report["wastefulFocused"] = {k: v for k, v in waste.items() if k != "ok"}
    rows.extend(_waste_rows(waste))
    rows.extend(_before_after_rows(cid, ids, rows[:3]))
    shape_rows, long_row = _corpus_shape_rows()
    rows.extend(shape_rows)
    # §11.4 第 2 条：重复读取率/无效检索率与续读成本口径（含长页续读实测）
    rows.extend(_cost_metric_rows(rows[:3] + [long_row]))
    rows.append({
        "step": "unreadFullReviewWarns",
        "ok": shallow["lifecycle"] == "COMPLETED_WITH_WARNINGS"
        and "UNREAD_PAGES" in shallow["warningCodes"]
        and bool(shallow["answerChars"]),
        "lifecycle": shallow["lifecycle"], "warnings": shallow["warningCodes"],
        "coverage": shallow["coverage"], "rounds": shallow["rounds"],
    })
    return rows, all(r["ok"] for r in rows)


def _stage_effort_rows(rows: list[dict[str, Any]], shallow: dict[str, Any]) -> list[dict[str, Any]]:
    """D3 验收：推理档位按阶段收敛，且模型能力不支持时不强发档位。

    以当前服务商/默认模型实际下发的档位为准做自适应断言：
    - 默认模型不接受档位参数时，所有调用都必须是 None（不下发，保持模型默认行为）；
    - 默认模型接受档位时：direct 的结论轮保持用户设定档（不因降档伤及单点问答）；
      focused / full_review 的读页轮降到最低档；守门修复轮用最高档。
    """
    from lawbuddy.agent import model_gateway as mg
    from lawbuddy.agent.autonomous import _stage_effort

    model = mg.resolve_model()
    base = mg.effective_reasoning_effort(None, model)
    low = mg.weakest_reasoning_effort(model)
    high = mg.strongest_reasoning_effort(model)
    direct, focused, full = rows
    if base is None:
        supports = all(e is None for r in (direct, focused, full) for e in r["efforts"])
        extract_downgraded = True
        repair_upgraded = True
    else:
        supports = True
        # direct 结论轮不降档：全部保持 base
        direct_ok = bool(direct["efforts"]) and all(e == base for e in direct["efforts"])
        # 读页轮降到最低档：focused 至少一次、full_review 至少两次（其脚本读 3 次页）
        extract_downgraded = (
            direct_ok
            and low is not None
            and low in focused["efforts"]
            and focused["reasoningStages"].get("extract", 0) >= 1
            and full["reasoningStages"].get("extract", 0) >= 2
        )
        # 修复轮升到最高档：反例脚本触发两次守门修复
        repair_efforts = [
            e for e, st in zip(shallow["efforts"], shallow["stageTrace"]) if st == "repair"
        ]
        repair_upgraded = (
            shallow["reasoningStages"].get("repair", 0) >= 1
            and high is not None
            and bool(repair_efforts)
            and all(e == high for e in repair_efforts)
        )
    # 模型能力收敛与降级路径（纯函数，直接断言）
    if model:
        caps = (
            # 未在目录声明的模型：三阶段都不下发参数（按不支持档位处理）
            _stage_effort("extract", None, "not-in-catalog-model") is None
            and _stage_effort("repair", None, "not-in-catalog-model") is None
            and _stage_effort("extract", None, model) == low
            and _stage_effort("synthesis", "medium", model) == "medium"
            and _stage_effort("repair", None, model) == high
            # 降档重试优先于阶段策略：即使处于修复阶段也走最低档
            and _stage_effort("repair", base, model, downgraded=True) == low
            and _stage_effort("synthesis", base, model, downgraded=True) == low
        )
    else:
        # 尚未添加任何模型（内置目录已清空）：阶段策略一律不下发档位，
        # 也不能把空模型误判成「支持档位但取不到值」
        caps = all(
            _stage_effort(stage, base, model) is None
            for stage in ("extract", "synthesis", "repair")
        ) and (low is None and high is None)
    return [
        {"step": "modelEffortCapability", "ok": supports,
         "model": model, "base": base, "low": low, "high": high},
        {"step": "extractStageDowngraded", "ok": extract_downgraded,
         "direct": direct["efforts"], "focused": focused["efforts"],
         "full": full["efforts"], "stages": full["reasoningStages"]},
        {"step": "repairStageUpgraded", "ok": repair_upgraded,
         "efforts": shallow["efforts"], "stages": shallow["reasoningStages"]},
        {"step": "stageEffortConvergence", "ok": caps},
    ]


def _efficiency_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """D4 验收：无浪费的脚本不得被判为重复读取/无效检索，且指标口径可量化。

    - 三类深度的脚本每一步都是必要调用，repeatedReads / duplicateSearches
      必须为 0（否则指标本身在误报）；
    - 检索命中页、读页次数与工具调用次数必须自洽（指标不是独立估算的旁路数字）；
    - 子 Agent 采用率口径可算：无派发时 findings=0、adoptionRate 为 None。
    """
    direct, focused, full = rows
    eff_full = full.get("efficiency") or {}
    effs = [r.get("efficiency") or {} for r in rows]
    clean = all(
        int(e.get("repeatedReads") or 0) == 0 and int(e.get("duplicateSearches") or 0) == 0
        for e in effs
    )
    consistent = (
        int(eff_full.get("toolCalls") or 0) == int(full.get("toolCalls") or 0)
        and int(eff_full.get("reads") or 0) * 1 >= 1
        and int(eff_full.get("searches") or 0) >= 1
        and int(eff_full.get("uniqueHitPages") or 0) >= 1
    )
    sub = eff_full.get("subagents") or {}
    subagent_ok = (
        int(sub.get("dispatched") or 0) == 0
        and int(sub.get("findings") or 0) == 0
        and sub.get("adoptionRate") is None
    )
    return [
        {"step": "noSelfInflictedWaste", "ok": clean,
         "repeatedReads": [e.get("repeatedReads") for e in effs],
         "duplicateSearches": [e.get("duplicateSearches") for e in effs]},
        {"step": "efficiencyConsistent", "ok": consistent, "full": eff_full,
         "toolCalls": full.get("toolCalls")},
        {"step": "subagentAdoptionAccounted", "ok": subagent_ok, "subagents": sub},
    ]


def _cost_metric_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """§11.4 第 2 条：重复读取率、无效检索率与续读成本必须可量化且口径自洽。

    三个率都由同一份效率账本导出，分母为 0 时记为 None（不伪造 0%）；
    续读成本按每次读取的切片核对：同页切片不得重叠、页内字符总量不得超出
    该页原文长度，且逐行 readChars 必须等于切片字符之和——否则"读了 14,000 字"
    只是估算，不能用来判断续读是否划算。
    """
    def rate(num: int, den: int) -> float | None:
        return round(num / den, 3) if den else None

    rates: list[dict[str, Any]] = []
    for r in rows:
        eff = r.get("efficiency") or {}
        reads = int(eff.get("reads") or 0)
        searches = int(eff.get("searches") or 0)
        rates.append({
            "depth": r["depth"],
            "repeatedReadRate": rate(int(eff.get("repeatedReads") or 0), reads),
            "emptySearchRate": rate(int(eff.get("emptySearches") or 0), searches),
            "duplicateSearchRate": rate(int(eff.get("duplicateSearches") or 0), searches),
            "reads": reads, "searches": searches,
        })
    rates_ok = (
        all(
            0.0 <= v <= 1.0
            for item in rates
            for k, v in item.items() if k.endswith("Rate") and v is not None
        )
        and any(item["repeatedReadRate"] is not None for item in rates)
        and any(item["emptySearchRate"] is not None for item in rates)
    )

    cost: list[dict[str, Any]] = []
    cost_ok = True
    for r in rows:
        spans: dict[tuple[str, int], list[tuple[int, int, int]]] = {}
        for s in r.get("readSlices") or []:
            spans.setdefault(
                (str(s["documentId"]), int(s["pageNo"])), []
            ).append((int(s["textStart"]), int(s["chars"]), int(s["textTotal"])))
        slice_chars = 0
        overlaps = 0
        overshoot = 0
        for items in spans.values():
            items.sort()
            end = 0
            for start, chars, total in items:
                if start < end or chars <= 0:
                    overlaps += 1
                if total and start + chars > total:
                    overshoot += 1
                end = max(end, start + chars)
                slice_chars += chars
        call_chars = sum(int(c) for c in (r.get("readCallChars") or []))
        consistent = (
            overlaps == 0
            and overshoot == 0
            and slice_chars == int(r.get("readChars") or 0)
            and call_chars == slice_chars
        )
        cost_ok = cost_ok and consistent
        cost.append({
            "depth": r["depth"], "readCalls": len(r.get("readCallChars") or []),
            "readChars": r.get("readChars"), "sliceChars": slice_chars,
            "overlaps": overlaps, "overshoot": overshoot, "consistent": consistent,
        })

    return [
        {"step": "wasteRatesComputable", "ok": rates_ok, "rates": rates},
        {"step": "continuationReadCostMeasured", "ok": cost_ok, "pages": cost},
    ]


def _decomposed_search_rows(cid: str, ids: dict[str, str], full: dict[str, Any]) -> list[dict[str, Any]]:
    """D5 验收：模型一次多路检索替代多次单查询，检索与轮次成本下降、质量不变。

    对照脚本只把 full_review 的两个单查询合并成一个 queries 调用（同一批证据），
    断言：轮次与检索次数更少、命中页不少于逐词相加、完成状态与引用数不退化。
    """
    from evals._loop_mock import ScriptStep

    final = (
        "一、合同关系：《买卖合同》第1页，原文「甲方杭州甲方向乙方上海乙方出售办公设备一批，"
        "总价人民币三十万元整」。\n"
        "二、交付事实：《送货单》第1页，原文「今收到办公设备一批，共计人民币三十万元整」。\n"
        "三、抗辩线索：《微信聊天记录》第1页，原文「发现两台打印机存在故障」。\n"
        "四、待核实：付款凭证缺失，违约金计算起点尚不能从原文确认。"
    )
    steps = [
        ScriptStep.tool("documents_list", {}),
        ScriptStep.tool("document_get_pages", {"documentId": ids["contract"], "pageFrom": 1, "pageTo": 2}),
        ScriptStep.tool("document_get_pages", {"documentId": ids["delivery"], "pageFrom": 1, "pageTo": 1}),
        ScriptStep.tool("document_get_pages", {"documentId": ids["chat"], "pageFrom": 1, "pageTo": 1}),
        ScriptStep.tool("document_search", {"queries": ["付款", "故障"], "limit": 6}),
        ScriptStep.final(final),
    ]
    merged = _measure_steps(cid, "全卷阅卷：梳理合同关系、交付事实与抗辩线索", steps, "full_review_multi")
    eff_full = full.get("efficiency") or {}
    eff_multi = merged.get("efficiency") or {}
    tool_full = eff_full.get("toolCallsByName") or {}
    tool_multi = eff_multi.get("toolCallsByName") or {}
    return [
        # 一次多路检索替代两次单查询：工具调用数与检索次数都更少
        {"step": "decomposedSearchSavesCalls",
         "ok": int(tool_multi.get("document_search") or 0) == 1
         and int(tool_full.get("document_search") or 0) == 2
         and int(merged["toolCalls"]) < int(full["toolCalls"])
         and int(merged["rounds"] or 0) < int(full["rounds"] or 0),
         "searchCalls": {"single": tool_full.get("document_search"),
                         "multi": tool_multi.get("document_search")},
         "toolCalls": {"single": full["toolCalls"], "multi": merged["toolCalls"]},
         "rounds": {"single": full["rounds"], "multi": merged["rounds"]}},
        # 质量不变：仍干净完成、引用数不减少、命中页覆盖两条检索词的信息
        {"step": "decomposedSearchKeepsQuality",
         "ok": merged["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS")
         and (merged["citeSupported"] or 0) >= (full["citeSupported"] or 0)
         and merged["readPages"] == full["readPages"]
         and int(eff_multi.get("searches") or 0) == 2
         and int(eff_multi.get("uniqueHitPages") or 0) >= 1
         and int(eff_multi.get("duplicateSearches") or 0) == 0,
         "lifecycle": merged["lifecycle"], "cites": merged["citeSupported"],
         "readPages": merged["readPages"], "efficiency": eff_multi},
    ]


def _exclusion_rows(cid: str, ids: dict[str, str]) -> list[dict[str, Any]]:
    """§5.4 excluded 终态验收：用户明确排除的页带理由通过，其余未读页仍须警告。

    覆盖三条边界：
    - 请求点名「《微信聊天记录》不用读」→ 该文档计入 excluded 而非 read，可干净完成；
    - 请求点名「第2页不用读」→ 只排除该页，同文档其他页仍需读；
    - 请求把所有材料都排除 → 忽略排除，仍要求覆盖（不得零阅读干净完成）。
    """
    from evals._loop_mock import ScriptStep

    def _run(request: str, steps: list, label: str) -> dict[str, Any]:
        return _measure_steps(cid, request, steps, label)

    base = [
        ScriptStep.tool("documents_list", {}),
        ScriptStep.tool("document_get_pages", {"documentId": ids["contract"], "pageFrom": 1, "pageTo": 2}),
        ScriptStep.tool("document_get_pages", {"documentId": ids["delivery"], "pageFrom": 1, "pageTo": 1}),
        ScriptStep.final(
            "一、合同关系：《买卖合同》第1页，原文「甲方杭州甲方向乙方上海乙方出售办公设备一批，"
            "总价人民币三十万元整」。\n"
            "二、交付事实：《送货单》第1页，原文「今收到办公设备一批，共计人民币三十万元整」。\n"
            "三、未读说明：按用户要求未阅读指定的聊天记录材料，相关争点未纳入本次结论。"
        ),
    ]
    doc_level = _run("全卷阅卷：梳理合同关系与交付事实，《微信聊天记录》不用读", base, "full_review_excluded")
    page_level = _run(
        "全卷阅卷：梳理合同关系与交付事实，《买卖合同》第2页不用读，"
        "《微信聊天记录》第1页也不用读", [
            ScriptStep.tool("documents_list", {}),
            ScriptStep.tool("document_get_pages", {"documentId": ids["contract"], "pageFrom": 1, "pageTo": 1}),
            ScriptStep.tool("document_get_pages", {"documentId": ids["delivery"], "pageFrom": 1, "pageTo": 1}),
            ScriptStep.final(
                "一、合同关系：《买卖合同》第1页，原文「甲方杭州甲方向乙方上海乙方出售办公设备一批，"
                "总价人民币三十万元整」。\n"
                "二、交付事实：《送货单》第1页，原文「今收到办公设备一批，共计人民币三十万元整」。\n"
                "三、未读说明：按用户要求未阅读《买卖合同》第2页与《微信聊天记录》第1页。"
            ),
        ], "full_review_excluded_pages")
    all_excluded = _run(
        "全卷阅卷：梳理合同关系与交付事实，《买卖合同》不用读，《送货单》不用读，"
        "《微信聊天记录》不用读",
        [ScriptStep.tool("documents_list", {}), ScriptStep.final("材料均按用户要求未读，无法形成结论。")],
        "full_review_excluded_all",
    )
    doc_cov = doc_level["coverage"] or {}
    page_cov = page_level["coverage"] or {}
    all_cov = all_excluded["coverage"] or {}
    return [
        # 整份文档被用户排除 → 计入 excluded，不再作为未读阻塞
        {"step": "userExcludedDocPasses",
         "ok": doc_cov.get("excluded") == 1 and int(doc_cov.get("unread") or 0) == 0
         and doc_level["lifecycle"] == "COMPLETED"
         and int(doc_cov.get("readPages") or 0) == 3,
         "coverage": doc_cov, "lifecycle": doc_level["lifecycle"]},
        # 只排除指定页 → 只记该页 excluded，同文档其他页仍需读
        {"step": "userExcludedPageScoped",
         "ok": page_cov.get("excluded") == 2 and int(page_cov.get("unread") or 0) == 0
         and int(page_cov.get("readPages") or 0) == 2
         and page_level["lifecycle"] == "COMPLETED",
         "coverage": page_cov, "lifecycle": page_level["lifecycle"]},
        # 全部材料都被"排除" → 不采信该排除，未读仍触发警告（不得零阅读干净完成）
        {"step": "excludeAllNotAccepted",
         "ok": all_cov.get("excluded") in (0, None)
         and int(all_cov.get("unread") or 0) > 0
         and all_excluded["lifecycle"] == "COMPLETED_WITH_WARNINGS",
         "coverage": all_cov, "lifecycle": all_excluded["lifecycle"],
         "warnings": all_excluded["warningCodes"]},
    ]


def _ledger_state_rows(cid: str, ids: dict[str, str]) -> list[dict[str, Any]]:
    """§5.4 阅读账本四态验收：部分读取、完整读取、排除、不可读互不混淆。

    直接驱动真实的 `_record_reading` 与 `_reading_coverage`，不经过模型，
    确保"调用过工具"不等于"读完了"。
    """
    from lawbuddy.agent.autonomous import _reading_coverage, _record_reading

    state: dict[str, Any] = {
        "case_id": cid,
        "request": "全卷阅卷：梳理合同关系与交付事实，《微信聊天记录》第1页不用读",
        "task_contract": {"taskDepth": "full_review"},
        "reading_ledger": {},
    }
    # 部分读取：本页共 100 字，只返回前 10 字且未读完
    _record_reading(state, ids["contract"], "ver-contract", {"pages": [
        {"pageNo": 1, "textTotal": 100, "textStart": 0, "text": "甲" * 10, "truncated": True},
    ]})
    # 完整读取：一次返回全部字符
    _record_reading(state, ids["contract"], "ver-contract", {"pages": [
        {"pageNo": 2, "textTotal": 20, "textStart": 0, "text": "乙" * 20, "truncated": False},
    ]})
    # 不可读：该页没有可提取文本
    _record_reading(state, ids["delivery"], "ver-delivery", {"pages": [
        {"pageNo": 1, "textTotal": 0, "textStart": 0, "text": "", "truncated": False},
    ]})
    coverage = _reading_coverage(state, state["task_contract"])
    ledger = state["reading_ledger"]
    states = {k: v.get("state") for k, v in ledger.items()}
    return [{
        "step": "readingLedgerFourStates",
        "ok": states.get(f"{ids['contract']}:1") == "read_partial"
        and states.get(f"{ids['contract']}:2") == "read_complete"
        and states.get(f"{ids['delivery']}:1") == "unreadable"
        and coverage.get("excluded") == 1
        and [u["pageNo"] for u in coverage.get("unread") or []] == [1]
        and int(coverage.get("readPages") or 0) == 2,
        "states": states, "coverage": coverage,
    }]


def _lifecycle_rows(cid: str, ids: dict[str, str]) -> list[dict[str, Any]]:
    """§11.1「完成与生命周期」验收：过程文本、修复预算、产物声明三条边界。"""
    from evals._loop_mock import ScriptStep

    good = (
        "一、合同关系：《买卖合同》第1页，原文「甲方杭州甲方向乙方上海乙方出售办公设备一批，"
        "总价人民币三十万元整」。\n"
        "二、交付事实：《送货单》第1页，原文「今收到办公设备一批，共计人民币三十万元整」。\n"
        "三、未读页按全卷要求补齐后再交付。"
    )
    # 1) 同时输出过程文本与工具调用：该文本只是过程说明，不能当作终答
    interim = _measure_steps(cid, "全卷阅卷：梳理合同关系与交付事实", [
        ScriptStep.tool_with_text(
            "documents_list", {}, "我先看一下材料构成，再逐页读原文。",
        ),
        ScriptStep.tool("document_get_pages", {"documentId": ids["contract"], "pageFrom": 1, "pageTo": 2}),
        ScriptStep.tool("document_get_pages", {"documentId": ids["delivery"], "pageFrom": 1, "pageTo": 1}),
        ScriptStep.tool("document_get_pages", {"documentId": ids["chat"], "pageFrom": 1, "pageTo": 1}),
        ScriptStep.final(good),
    ], "full_review_interim")
    # 2) 修复预算耗尽：草稿始终不满足覆盖，连续提交都被守门打回，最终必须带警告收束
    starved = _measure_steps(cid, "全卷阅卷：梳理合同关系与交付事实", [
        ScriptStep.tool("document_get_pages", {"documentId": ids["contract"], "pageFrom": 1, "pageTo": 1}),
        ScriptStep.final("一、合同关系：《买卖合同》第1页，原文「甲方杭州甲方向乙方上海乙方出售办公设备一批」。"),
        ScriptStep.final("一、合同关系：《买卖合同》第1页，原文「甲方杭州甲方向乙方上海乙方出售办公设备一批」。"),
        ScriptStep.final("一、合同关系：《买卖合同》第1页，原文「甲方杭州甲方向乙方上海乙方出售办公设备一批」。"),
        ScriptStep.final("一、合同关系：《买卖合同》第1页，原文「甲方杭州甲方向乙方上海乙方出售办公设备一批」。"),
    ], "full_review_repair_starved")
    # 3) 声明生成的文件不存在 → 产物守门失败，不得干净完成
    phantom = _measure_steps(cid, "整理全案材料并生成阅卷报告文档", [
        ScriptStep.tool("documents_list", {}),
        ScriptStep.final("已生成阅卷报告，保存为 ghost_review_report.docx，可直接打开查看。"),
        ScriptStep.final("已生成阅卷报告，保存为 ghost_review_report.docx，可直接打开查看。"),
        ScriptStep.final("已生成阅卷报告，保存为 ghost_review_report.docx，可直接打开查看。"),
    ], "deliverable_missing")
    # §11.4 第 6 条：预算/修复耗尽后仍保留来源、明确缺口，且可从最近检查点续跑
    from lawbuddy.agent.tasks import latest_checkpoint

    ckpt = latest_checkpoint(starved["taskId"]) or {}
    ckpt_state = ckpt.get("state") or {}
    resumable = (
        str(ckpt.get("node") or "").startswith("round_")
        and bool(ckpt_state.get("messages"))
        and bool((ckpt_state.get("readingLedger") or {}))
        and (ckpt_state.get("taskContract") or {}).get("taskDepth") == "full_review"
    )
    return [
        # 过程文本不被当作终答：循环继续执行工具直到覆盖满足后才干净完成
        {"step": "interimTextNotFinal",
         "ok": interim["lifecycle"] == "COMPLETED"
         and int(interim["rounds"] or 0) >= 5
         and int(interim["readPages"] or 0) == 4
         and "UNREAD_PAGES" not in interim["warningCodes"],
         "lifecycle": interim["lifecycle"], "rounds": interim["rounds"],
         "readPages": interim["readPages"], "warnings": interim["warningCodes"]},
        # 修复预算耗尽：以带警告完成收束，不无限重试、不隐藏未读
        {"step": "repairBudgetExhaustedWarns",
         "ok": starved["lifecycle"] == "COMPLETED_WITH_WARNINGS"
         and "UNREAD_PAGES" in starved["warningCodes"]
         and int(starved["rounds"] or 0) <= 5,
         "lifecycle": starved["lifecycle"], "rounds": starved["rounds"],
         "warnings": starved["warningCodes"]},
        # 预算耗尽仍保留已核验来源、显式列出缺口，并留下可续跑检查点
        {"step": "budgetExhaustedRetainsSources",
         "ok": int(starved["citeSupported"] or 0) >= 1
         and "UNREAD_PAGES" in starved["warningCodes"],
         "citeSupported": starved["citeSupported"],
         "citePending": starved["citePending"],
         "warnings": starved["warningCodes"]},
        {"step": "budgetExhaustedResumable",
         "ok": resumable,
         "node": ckpt.get("node"),
         "messages": len(ckpt_state.get("messages") or []),
         "readingLedger": len(ckpt_state.get("readingLedger") or {}),
         "taskDepth": (ckpt_state.get("taskContract") or {}).get("taskDepth")},
        # 声称生成的文件不存在 → MISSING_DELIVERABLE 警告，不能干净完成
        {"step": "phantomDeliverableWarns",
         "ok": phantom["lifecycle"] == "COMPLETED_WITH_WARNINGS"
         and "MISSING_DELIVERABLE" in phantom["warningCodes"],
         "lifecycle": phantom["lifecycle"], "warnings": phantom["warningCodes"]},
    ]


def _before_after_rows(cid: str, ids: dict[str, str], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """D6 验收：把 D1/D2/D3 的优化临时关闭，用同一夹具重跑，做前后对照。

    关闭方式（只在评测进程内 monkeypatch，不改实现）：
    - D2：`_exposed_tool_names` 返回 None → 全量下发所有工具；
    - D1：`_wants_presentation` 恒 True → 无论任务深度都注入呈现格式层；
    - D3：`_stage_effort` 恒返回用户设定档 → 所有阶段同一推理成本。
    对照要求：优化后质量（完成状态、已核验引用、full_review 阅读页数）不下降，
    且工具 schema 与 prompt 体积确有缩减——效率不能靠隐藏阅读缺口换来。
    """
    from lawbuddy.agent import autonomous as A

    originals = (A._exposed_tool_names, A._wants_presentation, A._stage_effort)

    def full_tools(_state):
        return None

    def always_present(_state):
        return True

    def flat_effort(_stage, base, _model_id, *, downgraded=False):
        return base

    A._exposed_tool_names = full_tools
    A._wants_presentation = always_present
    A._stage_effort = flat_effort
    try:
        before = [_measure(cid, ids, depth) for depth in DEPTHS]
    finally:
        A._exposed_tool_names, A._wants_presentation, A._stage_effort = originals

    def total(rows: list[dict[str, Any]], key: str) -> int:
        return sum(int(r.get(key) or 0) for r in rows)

    quality_held = all(
        b["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS")
        and (b["citeSupported"] or 0) >= (a["citeSupported"] or 0)
        and b["readPages"] >= a["readPages"]
        for a, b in zip(after, before)
    )
    schema_saved = total(before, "toolSchemaChars") - total(after, "toolSchemaChars")
    prompt_saved = total(before, "promptChars") - total(after, "promptChars")

    def cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "rounds": total(rows, "rounds"),
            "toolCalls": total(rows, "toolCalls"),
            "promptTokens": total(rows, "promptTokens"),
            "completionTokens": total(rows, "completionTokens"),
            "tokens": total(rows, "promptTokens") + total(rows, "completionTokens"),
            "durationMs": total(rows, "durationMs"),
            "promptChars": total(rows, "promptChars"),
            "toolSchemaChars": total(rows, "toolSchemaChars"),
        }

    cost_before, cost_after = cost(before), cost(after)
    return [
        {"step": "beforeAfterContextCost",
         "ok": schema_saved > 0 and prompt_saved > 0,
         "toolSchemaChars": {"before": total(before, "toolSchemaChars"),
                             "after": total(after, "toolSchemaChars"),
                             "saved": schema_saved},
         "promptChars": {"before": total(before, "promptChars"),
                         "after": total(after, "promptChars"), "saved": prompt_saved},
         "toolCount": {"before": max(r["toolCount"] for r in before),
                       "after": max(r["toolCount"] for r in after)}},
        # §11.4 第 1 条：同一夹具下的质量与成本五项（质量分数/轮次/token/耗时/工具调用）
        {"step": "beforeAfterCostLedger",
         "ok": all(v >= 0 for v in (cost_before["tokens"], cost_after["tokens"]))
               and cost_after["rounds"] <= cost_before["rounds"]
               and cost_after["toolCalls"] == cost_before["toolCalls"],
         "before": cost_before, "after": cost_after,
         "delta": {k: cost_after[k] - cost_before[k]
                   for k in ("rounds", "toolCalls", "tokens", "durationMs",
                             "promptChars", "toolSchemaChars")},
         "quality": {"before": [{"depth": r["depth"], "lifecycle": r["lifecycle"],
                                "cites": r["citeSupported"], "readPages": r["readPages"]}
                               for r in before],
                     "after": [{"depth": r["depth"], "lifecycle": r["lifecycle"],
                                "cites": r["citeSupported"], "readPages": r["readPages"]}
                               for r in after]}},
        {"step": "optimizationKeepsQuality", "ok": quality_held,
         "before": [{"depth": r["depth"], "lifecycle": r["lifecycle"],
                     "cites": r["citeSupported"], "readPages": r["readPages"],
                     "rounds": r["rounds"]} for r in before],
         "after": [{"depth": r["depth"], "lifecycle": r["lifecycle"],
                    "cites": r["citeSupported"], "readPages": r["readPages"],
                    "rounds": r["rounds"]} for r in after]},
        {"step": "beforeAfterEffort",
         # 默认模型不支持档位调节（内置模型目录已清空、用户未添加模型时一律不下发）
         # 时该对照不适用：不能因为「两边都是 None」判失败
         "ok": (
             _efforts_vary(after)
             if any(e for r in after for e in r["efforts"])
             else True
         ),
         "before": {r["depth"]: r["efforts"] for r in before},
         "after": {r["depth"]: r["efforts"] for r in after}},
    ]


def _efforts_vary(rows: list[dict[str, Any]]) -> bool:
    """D3 生效判据：优化后同一深度的逐次调用档位确有差异（读页降档/修复升档）。"""
    return any(len(set(r["efforts"])) > 1 for r in rows)


def _waste_rows(waste: dict[str, Any]) -> list[dict[str, Any]]:
    """D4 反例验收：确有浪费时指标必须显形，且重复检索走缓存复用。

    防止"指标恒为 0"式的假通过——这里脚本故意重复读同一页、重复搜同一词。
    """
    eff = waste.get("efficiency") or {}
    return [
        {"step": "repeatedReadDetected", "ok": int(eff.get("repeatedReads") or 0) >= 1
         and int(eff.get("repeatedReadPages") or 0) >= 2,
         "repeatedReads": eff.get("repeatedReads"),
         "repeatedReadPages": eff.get("repeatedReadPages")},
        {"step": "duplicateSearchDetected", "ok": int(eff.get("duplicateSearches") or 0) >= 1,
         "duplicateSearches": eff.get("duplicateSearches"),
         "searches": eff.get("searches")},
        # 指标只提示不拦截：重复读取仍如实返回原文，结论与引用质量不受影响
        {"step": "wasteDoesNotBreakQuality",
         "ok": waste["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS")
         and bool(waste["answerChars"]) and (waste["citeSupported"] or 0) >= 1,
         "lifecycle": waste["lifecycle"], "cites": waste["citeSupported"]},
    ]


def _corpus_shape_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """§11.4 第 5 条验收：长页、大量短页、多文档三种语料形态分别跑全卷阅卷。

    单一「3 文档 4 页」夹具看不出语料形态的影响：长页会暴露续读是否无损，
    大量短页会暴露是否被迫一页一次调用，多文档会暴露覆盖统计是否按文档漏算。
    三段都要求：干净完成、覆盖全部页面、无重复读取、读到的字符数与原文字数一致。

    返回 (门槛行, 长页那次实测)，后者供续读成本口径复核。
    """
    from evals._loop_mock import ScriptStep

    # 形态 1：单份材料单页超长（14,000 字）——必须多次游标续读才能读完
    base = "第一条 甲方杭州甲方向乙方上海乙方出售办公设备一批，总价人民币三十万元整。"
    long_text = (base * 400)[:14000]
    case_long, ids_long = _seed_docs("M3评测-长页", {
        "long": ("长篇合同", {1: long_text}),
    })
    long_row = _measure_steps(case_long["id"], "全卷阅卷：通读长篇合同", [
        ScriptStep.tool("documents_list", {}),
        ScriptStep.tool("document_get_pages", {"documentId": ids_long["long"], "pageFrom": 1, "pageTo": 1}),
        ScriptStep.tool("document_get_pages", {"documentId": ids_long["long"], "pageFrom": 1, "pageTo": 1,
                                              "charOffset": 5880}),
        ScriptStep.tool("document_get_pages", {"documentId": ids_long["long"], "pageFrom": 1, "pageTo": 1,
                                              "charOffset": 11760}),
        ScriptStep.final(
            "全卷阅卷：长篇合同第1页，原文「甲方杭州甲方向乙方上海乙方出售办公设备一批」，"
            "该页共 14000 字，已分三次游标读至末尾，无未读片段。"
        ),
    ], "full_review_long_page")

    # 形态 2：单份材料 30 页短页——一次调用应覆盖全部页，不应退化为一页一调
    short_pages = {i: f"第{i}页 送货记录：收到办公设备一批，价款三十万元整。" for i in range(1, 31)}
    case_short, ids_short = _seed_docs("M3评测-短页", {
        "ledger": ("送货台账", short_pages),
    })
    short_row = _measure_steps(case_short["id"], "全卷阅卷：通读送货台账", [
        ScriptStep.tool("documents_list", {}),
        ScriptStep.tool("document_get_pages", {"documentId": ids_short["ledger"], "pageFrom": 1, "pageTo": 30}),
        ScriptStep.final(
            "全卷阅卷：送货台账第1页，原文「收到办公设备一批，价款三十万元整」，"
            "全卷 30 页均为同类记录，已全部读取。"
        ),
    ], "full_review_many_short")

    # 形态 3：8 份材料各 2 页——覆盖统计须按文档逐页累计，不能只算最后一份
    multi_pages = {
        f"m{i}": (f"材料{i}", {
            1: f"材料{i}第1页：甲方交付设备{i}号，价款三十万元整。",
            2: f"材料{i}第2页：验收记录载明设备{i}号运行正常。",
        })
        for i in range(1, 9)
    }
    case_multi, ids_multi = _seed_docs("M3评测-多文档", multi_pages)
    multi_steps = [ScriptStep.tool("documents_list", {})]
    for i in range(1, 9):
        multi_steps.append(ScriptStep.tool(
            "document_get_pages",
            {"documentId": ids_multi[f"m{i}"], "pageFrom": 1, "pageTo": 2},
        ))
    multi_steps.append(ScriptStep.final(
        "全卷阅卷：材料1第1页，原文「甲方交付设备1号」，8 份材料共 16 页已逐份读完，"
        "其余材料为同类交付与验收记录。"
    ))
    multi_row = _measure_steps(case_multi["id"], "全卷阅卷：梳理全部材料", multi_steps, "full_review_multi_doc")

    def slices(row: dict[str, Any]) -> list[dict[str, Any]]:
        return [s for s in (row.get("readSlices") or []) if s.get("chars")]

    def no_waste(row: dict[str, Any]) -> bool:
        eff = row.get("efficiency") or {}
        return int(eff.get("repeatedReads") or 0) == 0 and int(eff.get("duplicateSearches") or 0) == 0

    long_slices = slices(long_row)
    long_chars = sum(int(s["chars"]) for s in long_slices)
    long_offsets = [int(s["textStart"]) for s in long_slices]
    short_calls = len(short_row.get("readCallChars") or [])
    short_chars = sum(int(c) for c in (short_row.get("readCallChars") or []))
    multi_slices = slices(multi_row)
    multi_docs = {s["documentId"] for s in multi_slices}

    rows = [
        # 长页：多次游标续读，偏移递增且拼起来正好 14,000 字（无损，无重复读）
        {"step": "corpusLongPageLossless",
         "ok": long_row["lifecycle"] == "COMPLETED"
         and int(long_row["readPages"] or 0) == 1
         and len(long_slices) >= 3
         and long_offsets == sorted(long_offsets)
         and long_chars == 14000
         and no_waste(long_row),
         "lifecycle": long_row["lifecycle"], "reads": long_slices,
         "readChars": long_chars, "efficiency": long_row.get("efficiency")},
        # 大量短页：一次调用覆盖 30 页，不退化为一页一调，字符数不丢
        {"step": "corpusManyShortPagesBatched",
         "ok": short_row["lifecycle"] == "COMPLETED"
         and int(short_row["readPages"] or 0) == 30
         and int((short_row.get("coverage") or {}).get("readPages") or 0) == 30
         and short_calls == 1
         and short_chars == sum(len(t) for t in short_pages.values())
         and no_waste(short_row),
         "lifecycle": short_row["lifecycle"], "readPages": short_row["readPages"],
         "coverage": short_row.get("coverage"), "readCalls": short_calls,
         "readChars": short_chars},
        # 多文档：8 份 16 页全部计入覆盖，不是只算最后读的那份
        {"step": "corpusMultiDocumentCoverage",
         "ok": multi_row["lifecycle"] == "COMPLETED"
         and int(multi_row["readPages"] or 0) == 16
         and len(multi_docs) == 8
         and int((multi_row.get("coverage") or {}).get("totalPages") or 0) == 16
         and int((multi_row.get("coverage") or {}).get("readPages") or 0) == 16
         and no_waste(multi_row),
         "lifecycle": multi_row["lifecycle"], "readPages": multi_row["readPages"],
         "documents": len(multi_docs), "coverage": multi_row.get("coverage")},
    ]
    # 三种形态必须真的不同，否则「分别测试」名不副实
    shapes = {
        "longPage": (1, round(long_chars / max(1, len(long_slices)), 1)),
        "manyShort": (30, round(short_chars / 30, 1)),
        "multiDoc": (16, round(sum(int(s["chars"]) for s in multi_slices) / max(1, len(multi_slices)), 1)),
    }
    report_shapes = [
        {"name": k, "pages": v[0], "avgPageChars": v[1]} for k, v in shapes.items()
    ]
    rows.append({
        "step": "corpusShapesDistinct",
        "ok": len({(s["pages"], s["avgPageChars"]) for s in report_shapes}) == 3
              and all(s["pages"] > 0 and s["avgPageChars"] > 0 for s in report_shapes),
        "shapes": report_shapes,
    })
    return rows, long_row


def _gate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """B8 验收：守门不误伤简单任务，也不放过未读完的完整阅卷。"""
    direct, focused, full = rows
    return [
        # 简单问答不被强制扩展：深度判定正确、无未读警告、保持干净完成
        {"step": "directNotEscalated",
         "ok": direct["taskDepth"] == "direct" and not direct["warningCodes"],
         "taskDepth": direct["taskDepth"], "warnings": direct["warningCodes"]},
        # 完整阅卷按页覆盖守门：全部页面有终态才允许干净完成
        {"step": "fullReviewCoverageGate",
         "ok": full["taskDepth"] == "full_review" and full["coverage"] is not None
         and full["coverage"]["required"] is True
         and full["coverage"]["readPages"] == full["coverage"]["totalPages"],
         "coverage": full["coverage"], "warnings": full["warningCodes"]},
        # 聚焦任务不因未读全卷被警告（覆盖守门只在 full_review 生效）
        {"step": "focusedNoCoverageWarn",
         "ok": "UNREAD_PAGES" not in focused["warningCodes"],
         "warnings": focused["warningCodes"]},
    ]


def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m3_loop_baseline",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "mock_gateway",
        "depths": list(DEPTHS),
    }
    try:
        ensure_workspace(str(tmp / "ws"))
        g1 = _seg(report, "baseline", _run_baseline)
        report["gates"] = {"depthBaselineA1": g1}
        report["passed"] = all(report["gates"].values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · 阅卷 Loop 质量与成本基线（{r['mode']}） ===")
    entry = r.get("baseline", {})
    for row in entry.get("rows", []):
        if "depth" not in row:
            continue
        print(f"  [{'完成' if row['lifecycle'] != 'FAILED' else '失败'}] {row['depth']}: "
              f"{row['rounds']} 轮 / {row['toolCalls']} 工具 / 读 {row['readPages']} 页"
              f"（覆盖 {row.get('readCoverage')}）/ {row['answerChars']} 字"
              f" / 引用 {row['citeSupported']}/{row['citeTotal']} 已核验、{row['citePending']} 待核实"
              f" / {row['durationMs']}ms")
        print(f"      工具序列：{' → '.join(row['toolNames'])}")
        print(f"      推理档位：{row.get('efforts')} 阶段：{row.get('stageTrace')}")
        eff = row.get("efficiency") or {}
        print(f"      效率：工具 {eff.get('toolCalls')} / 读页 {eff.get('reads')}"
              f"（重复 {eff.get('repeatedReads')} 次 {eff.get('repeatedReadPages')} 页）"
              f" / 检索 {eff.get('searches')}（无命中 {eff.get('emptySearches')}、"
              f"重复 {eff.get('duplicateSearches')}）/ 子代理 {eff.get('subagents')}")
        print(f"      守门：深度 {row.get('taskDepth')} / 干净完成 {row.get('finalizeOk')}"
              f" / 警告 {row.get('warningCodes')} / 覆盖 {row.get('coverage')}")
    for row in entry.get("rows", []):
        if "depth" in row:
            continue
        print(f"  {'✓' if row.get('ok') else '✗'} {row.get('step')}: "
              f"{json.dumps({k: v for k, v in row.items() if k not in ('step', 'ok')}, ensure_ascii=False)}")
    if entry:
        print(f"  合计：模型 {entry.get('totalModelCalls')} 次 / 工具 {entry.get('totalToolCalls')} 次"
              f" / token {entry.get('totalTokens')} / 耗时 {entry.get('totalDurationMs')}ms")
    print("门槛判定：")
    for g, ok in r.get("gates", {}).items():
        print(f"  {'✓' if ok else '✗'} {g}")
    if r.get("baseline", {}).get("notes"):
        for n in r["baseline"]["notes"]:
            print(f"  · {n}")
    print(f"结论：{'基线已记录' if r.get('passed') else '基线记录失败（见上方 ✗ 项）'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="阅卷 Loop 质量与成本基线")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_loop_baseline_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
