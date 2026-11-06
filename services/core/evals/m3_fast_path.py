# -*- coding: utf-8 -*-
"""M3 评测器：简单对话快速通道（效率与质量分工）。

用法（services/core 目录下）：
    python -m evals.m3_fast_path

背景：此前任何输入（含「你好」「谁创造了你」）都创建 Agent 任务并进入完整
阅卷循环——分层 system prompt + 工具 schema + 全文历史 + 多轮模型调用 +
完成守门，简单问题也要等近一分钟。快速通道让明显轻量的输入走单轮直答：

- 问候/元问题/与案件无关的通用问题：无工具、无案件上下文、小 prompt、
  一轮完成（reason=fast_path），工具 schema 体积为 0；
- 模型自省回退：fast path 中模型判断需要查卷时输出 [[NEED_CASE_REVIEW]]，
  Core 自动进入完整 Agent Loop（案件问题质量不让步）；
- 确定性排除：强案件指向词（本案/证据/笔录…）、显式选材、附件、preset、
  技能/MCP、非 legal_review 模式、续跑任务一律不走快速通道。

报告：控制台摘要 + evals/reports/m3_fast_path_<ts>.json。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def _run_task(cid: str, request: str, steps: list, selected: list[str] | None = None) -> dict[str, Any]:
    """按脚本跑一次任务，采集快速通道判定、轮次、工具与上下文成本。"""
    from evals._loop_mock import install_mock_gateway, last_efforts
    from evals.m3_loop_baseline import _wait_terminal
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import create_task, get_events

    task = create_task(case_id=cid, mode="legal_review", request_text=request,
                       selected_document_ids=selected)
    undo = install_mock_gateway(steps)
    try:
        AgentRunner.instance().start(task["id"])
        final_task = _wait_terminal(task["id"])
    finally:
        undo()
        efforts = last_efforts()

    events = get_events(task["id"])
    turns = [e["payload"] for e in events if e["type"] == "turn_complete"]
    turn = turns[-1] if turns else {}
    starts = [e["payload"] for e in events if e["type"] == "tool_use_start"]
    assistant = [e["payload"] for e in events if e["type"] == "assistant_message"]
    answer = str(assistant[-1].get("content") or "") if assistant else ""
    finalize = turn.get("finalize") or {}
    return {
        "taskId": task["id"],
        "lifecycle": final_task["lifecycle"],
        "reason": turn.get("reason"),
        "rounds": turn.get("rounds"),
        "toolCalls": len(starts),
        "toolNames": [str(s.get("name") or "") for s in starts],
        "answer": answer[:160],
        "finalizeOk": finalize.get("ok"),
        "taskDepth": finalize.get("taskDepth"),
        "modelCalls": len(efforts),
        "firstToolCount": efforts[0]["toolCount"] if efforts else None,
        "firstToolSchemaChars": efforts[0]["toolSchemaChars"] if efforts else None,
        "firstPromptChars": efforts[0]["promptChars"] if efforts else None,
        "promptChars": sum(int(e["promptChars"]) for e in efforts),
        "promptTokens": turn.get("promptTokens"),
    }


def _scenarios() -> list[dict[str, Any]]:
    from evals.m3_loop_baseline import _seed_case
    from evals._loop_mock import ScriptStep

    cid, ids = _seed_case()
    rows: list[dict[str, Any]] = []

    # A 问候：单轮直答、零工具、零工具 schema
    a = _run_task(cid, "你好", [
        ScriptStep.final("你好！我是 LawBuddy。有什么可以帮你？"),
    ])
    rows.append({"step": "greetingFastPath", **a})

    # B 元问题（谁创造了你）：同样走快速通道
    b = _run_task(cid, "谁创造了你？", [
        ScriptStep.final("我是 LawBuddy，本地优先的法律 AI 桌面应用；底层模型由你在设置中配置。"),
    ])
    rows.append({"step": "metaQuestionFastPath", **b})

    # C 与案件无关的通用法律问题：一般性说明即可直答
    c = _run_task(cid, "什么是诉讼时效？", [
        ScriptStep.final("诉讼时效是权利人在法定期间内不行使权利，即丧失请求法院强制保护其权利的制度。"
                         "普通诉讼时效期间通常为三年。这是一般性说明，具体适用需结合案件事实。"),
    ])
    rows.append({"step": "generalKnowledgeFastPath", **c})

    # C2 与案件无关的非法律问题：同样直答，不套用阅卷流程
    c2 = _run_task(cid, "用一句话解释什么是复利", [
        ScriptStep.final("复利是把已产生的利息计入本金，再共同计算下一期利息的计息方式。"),
    ])
    rows.append({"step": "nonLegalFastPath", **c2})

    # D 模型自省回退：输入无强指向词，但需查卷 → 标记回退后进入完整循环
    d = _run_task(cid, "诉讼时效过了没有？", [
        ScriptStep.final("[[NEED_CASE_REVIEW]]"),
        ScriptStep.tool("documents_list", {}),
        ScriptStep.final("是否已过诉讼时效需结合卷宗中的履行期限与催告记录判断：已读取材料清单，"
                         "下一步定位付款期限与催告时间的原文记载。"),
    ])
    rows.append({"step": "caseQuestionFallsBack", **d})

    # E 强案件指向词：前置排除，第一次模型调用就是带工具的完整循环
    e = _run_task(cid, "帮我分析本案的争议焦点", [
        ScriptStep.tool("documents_list", {}),
        ScriptStep.final("争点一：价款是否已付；争点二：交付是否验收。需逐条定位原文。"),
    ])
    rows.append({"step": "caseHintExcluded", **e})

    # E2 未说「本案」但明确要求分析案件争点：直接完整循环，避免多一次轻调用
    e2 = _run_task(cid, "分析价款是否已支付的争点", [
        ScriptStep.tool("documents_list", {}),
        ScriptStep.final("价款是否已付需要定位付款期限、付款凭证与催告记录，再逐项核验。"),
    ])
    rows.append({"step": "implicitCaseHintExcluded", **e2})

    # F 显式选材：即使是「你好」也不走快速通道（用户意图涉及选定材料）
    f = _run_task(cid, "你好", [
        ScriptStep.tool("document_get_pages", {"documentId": ids["delivery"], "pageFrom": 1, "pageTo": 1}),
        ScriptStep.final("你好！已看到你选定的《送货单》，可继续提问。"),
    ], selected=[ids["delivery"]])
    rows.append({"step": "selectedDocsExcluded", **f})

    return rows


def _gates(rows: list[dict[str, Any]]) -> dict[str, bool]:
    by = {r["step"]: r for r in rows}
    a, b, c, d, e, f = (by[k] for k in (
        "greetingFastPath", "metaQuestionFastPath", "generalKnowledgeFastPath",
        "caseQuestionFallsBack", "caseHintExcluded", "selectedDocsExcluded"))
    nonlegal = by["nonLegalFastPath"]
    implicit = by["implicitCaseHintExcluded"]

    def direct(r: dict[str, Any]) -> bool:
        return (r["reason"] == "fast_path" and r["rounds"] == 1
                and r["toolCalls"] == 0 and r["modelCalls"] == 1
                and r["firstToolSchemaChars"] == 0
                and r["lifecycle"] == "COMPLETED" and r["finalizeOk"] is True)

    return {
        # 简单问题：单轮、无工具、无工具 schema、干净完成
        "greetingFastPath": direct(a) and "LawBuddy" in a["answer"],
        "metaQuestionFastPath": direct(b),
        "generalKnowledgeFastPath": direct(c),
        "nonLegalFastPath": direct(nonlegal),
        # 自省回退：fast path 1 次 + 完整循环 2 轮（读清单 + 作答），不是快速通道收尾
        "caseQuestionFallsBack": (d["reason"] != "fast_path" and d["modelCalls"] == 3
                                  and d["rounds"] == 2 and d["toolCalls"] == 1
                                  and d["lifecycle"] == "COMPLETED"),
        # 强指向词：首次调用即完整循环（带工具 schema），深度按意图推导
        "caseHintExcluded": (e["reason"] != "fast_path" and e["modelCalls"] == 2
                             and (e["firstToolSchemaChars"] or 0) > 0
                             and e["taskDepth"] == "focused"),
        "implicitCaseHintExcluded": (implicit["reason"] != "fast_path"
                                     and implicit["modelCalls"] == 2
                                     and (implicit["firstToolSchemaChars"] or 0) > 0
                                     and implicit["taskDepth"] == "focused"),
        # 显式选材：不进入快速通道
        "selectedDocsExcluded": (f["reason"] != "fast_path"
                                 and (f["firstToolSchemaChars"] or 0) > 0
                                 and f["toolCalls"] == 1),
        # 效率对照：快速通道首轮 prompt 不到完整循环的一半，工具 schema 为 0
        "fastPathCostMuchLower": (
            (a["firstToolSchemaChars"] == 0 and (e["firstToolSchemaChars"] or 0) > 0)
            and a["firstPromptChars"] is not None and e["firstPromptChars"] is not None
            and a["firstPromptChars"] * 2 < e["firstPromptChars"]
        ),
    }


def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-fastpath-"))
    report: dict[str, Any] = {
        "evalName": "m3_fast_path",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        ensure_workspace(str(tmp / "ws"))
        rows = _scenarios()
        gates = _gates(rows)
        report["scenarios"] = rows
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print("\n=== M3 评测 · 简单对话快速通道 ===")
    for row in r.get("scenarios", []):
        print(f"  · {row['step']}: reason={row['reason']} rounds={row['rounds']} "
              f"toolCalls={row['toolCalls']} modelCalls={row['modelCalls']} "
              f"firstPromptChars={row['firstPromptChars']} firstToolSchemaChars={row['firstToolSchemaChars']}")
    print("门槛判定：")
    for g, ok in r.get("gates", {}).items():
        print(f"  {'✓' if ok else '✗'} {g}")
    a = next((x for x in r["scenarios"] if x["step"] == "greetingFastPath"), {})
    e = next((x for x in r["scenarios"] if x["step"] == "caseHintExcluded"), {})
    if a and e:
        print(f"  效率对照：快速通道首轮 prompt {a['firstPromptChars']} 字符 / 工具 schema 0，"
              f"完整循环首轮 prompt {e['firstPromptChars']} 字符 / 工具 schema {e['firstToolSchemaChars']}")
    print(f"结论：{'全部通过' if r.get('passed') else '存在失败项（见上方 ✗）'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="简单对话快速通道评测")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_fast_path_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
