# -*- coding: utf-8 -*-
"""M2 基线评测器（03 清单 §2 里程碑：阅卷 E2E 与关键引用门槛）。

用法（services/core 目录下）：
    python -m evals.m2_baseline [--case-dir ../case_file]

流程（mock 网关注入：模型响应脚本化，工具执行/落库/检索全走真实实现，
不联网、不依赖模型 API Key，结果可复现）：
1. 独立临时工作空间 → 建立金标案件（被告人辩护立场）；
2. 导入 case_file 全部卷宗 → 导入成功率 / 解析状态 / 页数统计；
3. 检索召回：金标主体在库性 + 金标检索点 Recall@6；
4. 阅卷 E2E：四个工作流预设经自主 Loop（mock 脚本驱动）全部执行，
   校验任务完成、6 种事件协议不变量与最终回答内容；
5. Claim Ledger 总览（阅卷路径不产 Claim，信息性统计）；
6. 报告：控制台摘要 + evals/reports/m2_baseline_<ts>.json。

M2 门槛：导入成功率 100%；主体在库性 100%；Recall@6 ≥ 90%；E2E 4/4；
事件协议不变量 4/4（turn_complete / 工具闭环 / 回答基于工具结果 / 事件类型白名单）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .gold_facts import GOLD_PARTIES, GOLD_RETRIEVALS, WORKFLOW_REQUESTS
from ._loop_mock import ScriptStep, install_mock_gateway, last_tool_result

# M2 门槛
GATE_IMPORT_RATE = 1.0
GATE_PARTY_RATE = 1.0
GATE_RECALL = 0.9
GATE_WORKFLOWS = 4

# 阅卷路径允许出现的事件类型：
# - LOOP_EVENT_TYPES：6 种循环协议事件（UI 唯一消费面）
# - CONTROL_EVENT_TYPES：控制面事件（status=生命周期变更必现；resume=断点续跑时出现；
#   answer_verification=收尾引用校验结果，前端角标数据源）
# error / message / plan / tool_call / budget / model_call 等旧协议或异常事件健康运行不得出现
LOOP_EVENT_TYPES = {
    "text", "tool_use_start", "tool_use_done",
    "assistant_message", "tool_result_message", "turn_complete",
}
CONTROL_EVENT_TYPES = {"status", "resume", "answer_verification"}

# 各预设最终回答必须包含的主题关键词（mock 脚本确定性注入）
PRESET_ANSWER_KEYS = {
    "review_analysis": "阅卷报告",
    "visualization": "时间线",
    "evidence_analysis": "证据分析",
    "strategy": "辩护策略",
}

# 复合问题夹具（05 方案 §6.7 / §10.4 D5）：真实用户/模型提问往往是多概念短语，
# 整串短语匹配会落空（FTS 短语匹配 + LIKE 都要求连续出现），必须拆词才能召回。
COMPOUND_QUERIES: list[tuple[str, list[str], list[str]]] = [
    ("王志远 工程保证金 银行流水", ["工程保证金", "银行流水", "王志远"], ["银行流水", "转账记录"]),
    ("杨大成 市政道路改造 合同", ["杨大成", "市政道路", "合同"], ["杨大成", "起诉书"]),
    ("赵小东 取保候审 保证人", ["赵小东", "取保候审", "保证人"], ["取保候审决定书"]),
    ("司法会计鉴定 涉案金额 380万", ["司法会计鉴定", "380万", "涉案金额"], ["司法会计鉴定", "鉴定意见通知书"]),
    ("扣押物品清单 搜查笔录", ["扣押物品", "搜查笔录", "扣押决定书"], ["扣押物品清单", "扣押决定书", "搜查笔录"]),
]

# 同义词拆解夹具：金标检索词 → 同义表达/相关术语（不引入卷宗外概念）
QUERY_DECOMPOSITIONS: dict[str, list[str]] = {
    "合同诈骗": ["合同诈骗", "诈骗", "非法占有"],
    "工程保证金": ["工程保证金", "保证金", "工程款"],
    "市政道路改造": ["市政道路", "道路改造", "杨大成"],
    "取保候审": ["取保候审", "保证人", "担保"],
    "抓获经过": ["抓获经过", "到案", "抓获"],
    "司法会计鉴定": ["司法会计鉴定", "鉴定意见", "审计"],
    "量刑建议": ["量刑建议", "量刑", "从轻"],
    "案件来源": ["案件来源", "报案", "移送"],
    "扣押物品": ["扣押物品", "扣押清单", "搜查"],
    "转账记录": ["转账记录", "银行流水", "汇款"],
    "现场勘查": ["现场勘查", "勘查笔录", "现场"],
    "辩护意见": ["辩护意见", "辩护词", "无罪"],
}


def _preset_steps(preset: str) -> list[ScriptStep]:
    """单个预设的 mock 脚本：材料清单 → 金标检索 → 汇总回答。

    检索词取自金标检索点（Recall gate 保证命中），最终回答回引命中文档标题
    ——为「回答基于工具结果」断言提供确定性依据。
    """

    def final_text(messages: list[dict[str, Any]]) -> str:
        r = last_tool_result(messages)
        titles = sorted({str(h.get("documentTitle") or "") for h in (r.get("hits") or [])} - {""})
        return (
            f"【{PRESET_ANSWER_KEYS[preset]}】已完成本轮工作："
            f"依据检索命中的 {len(titles)} 份材料（{('、'.join(titles)) or '无命中'}），"
            "结合全案材料清单形成上述结论；没有可靠来源的判断已提示人工核实。"
        )

    return [
        ScriptStep.tool("documents_list", {}),
        ScriptStep.tool("document_search", {"query": GOLD_RETRIEVALS[0][0], "limit": 6}),
        ScriptStep.final(final_text),
    ]


def _fmt_rate(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def _first_rank(hits: list[dict[str, Any]], expects: list[str]) -> int | None:
    """首个命中预期文档的位次（1 起）；None 表示前 limit 条内没有预期文档。

    位次即"要读几屏才找到目标页"的粗略代理：位次越小，后续阅读成本越低。
    """
    for i, h in enumerate(hits):
        if any(e in str(h.get("documentTitle") or "") for e in expects):
            return i + 1
    return None


def _decomposed_retrieval(case_id: str) -> dict[str, Any]:
    """查询拆解对照（05 方案 §6.7 / §10.4 D5）。

    同一组金标检索点分别用「单查询」和「拆成 2-3 个实词的多路查询」检索：
    - Recall@6 不得因拆解而下降（拆解只是多召回同义表达，不能反而漏掉）；
    - 目标文档的首命中位次不得变差（否则召回变多但排序变差，阅读成本上升）；
    - 合并必须真去重：命中项带 matchedTerms，页数少于各词命中数之和。
    不引入向量库或额外服务，只用现有 FTS/LIKE 与按页合并。
    """
    from lawbuddy.agent.autonomous import _execute_tool
    from lawbuddy.search import search_pages, search_pages_multi

    rows: list[dict[str, Any]] = []
    single_hit = multi_hit = 0
    single_rank_sum = multi_rank_sum = 0
    rank_compared = 0
    deduped_queries = 0
    all_tagged = True
    for query, expects in GOLD_RETRIEVALS:
        terms = QUERY_DECOMPOSITIONS.get(query) or [query]
        single = search_pages(case_id, query, limit=6)
        multi = search_pages_multi(case_id, terms, limit=6)
        s_rank, m_rank = _first_rank(single, expects), _first_rank(multi, expects)
        single_hit += s_rank is not None
        multi_hit += m_rank is not None
        if s_rank is not None and m_rank is not None:
            single_rank_sum += s_rank
            multi_rank_sum += m_rank
            rank_compared += 1
        per_term_total = sum(len(search_pages(case_id, t, limit=6)) for t in terms)
        merged_keys = {(h["documentId"], h["pageNo"]) for h in multi}
        if merged_keys and len(merged_keys) < per_term_total:
            deduped_queries += 1
        all_tagged = all_tagged and all(h.get("matchedTerms") for h in multi)
        rows.append({
            "query": query, "terms": terms,
            "singleHit": s_rank is not None, "multiHit": m_rank is not None,
            "singleRank": s_rank, "multiRank": m_rank,
            "singlePages": len({(h["documentId"], h["pageNo"]) for h in single}),
            "multiPages": len(merged_keys), "termHitTotal": per_term_total,
            "topTerms": [h.get("matchedTerms") for h in multi[:2]],
        })

    # 工具层接入：document_search 的 queries 参数走同一合并逻辑并计一次检索成本
    state: dict[str, Any] = {"case_id": case_id}
    tool_out = _execute_tool(
        state, "document_search",
        {"queries": ["工程保证金", "银行流水"], "limit": 6},
    )
    tool_keys = {(h["documentId"], h["pageNo"]) for h in (tool_out.get("hits") or [])}
    tool_ok = (
        isinstance(tool_out, dict)
        and tool_out.get("queries") == ["工程保证金", "银行流水"]
        and bool(tool_keys)
        and all(h.get("matchedTerms") for h in tool_out.get("hits") or [])
    )
    # 单查询仍走原路径（向后兼容：不要求模型改写既有调用）
    legacy = _execute_tool({"case_id": case_id}, "document_search",
                           {"query": "工程保证金", "limit": 6})
    legacy_ok = isinstance(legacy, dict) and bool(legacy.get("hits")) \
        and "queries" not in legacy

    total = len(GOLD_RETRIEVALS)
    # 复合问题对照：整串短语直接搜 vs 拆词后多路合并。
    # 这是拆解真正不可替代的场景——短语匹配要求连续出现，多概念问句必然落空。
    compound_rows = []
    compound_single_hit = compound_multi_hit = 0
    for question, terms, expects in COMPOUND_QUERIES:
        single = search_pages(case_id, question, limit=6)
        multi = search_pages_multi(case_id, terms, limit=6)
        s_ok = bool(single) and _first_rank(single, expects) is not None
        m_ok = _first_rank(multi, expects) is not None
        compound_single_hit += s_ok
        compound_multi_hit += m_ok
        compound_rows.append({
            "question": question, "terms": terms,
            "singleHit": s_ok, "multiHit": m_ok,
            "singlePages": len(single), "multiPages": len({(h["documentId"], h["pageNo"]) for h in multi}),
        })
    # 总阅读成本：按"从检索结果往下翻到目标页"的累计位次计（越接近 1 越省），
    # 以及各词命中页之和 vs 合并去重后的页数（拆解不得让阅读量按词数倍增）。
    single_pages = sum(r["singlePages"] for r in rows)
    multi_pages = sum(r["multiPages"] for r in rows)
    term_pages = sum(r["termHitTotal"] for r in rows)
    return {
        "total": total,
        "singleRecallAt6": _fmt_rate(single_hit, total),
        "multiRecallAt6": _fmt_rate(multi_hit, total),
        "avgSingleRank": round(single_rank_sum / rank_compared, 2) if rank_compared else None,
        "avgMultiRank": round(multi_rank_sum / rank_compared, 2) if rank_compared else None,
        "rankSumSingle": single_rank_sum,
        "rankSumMulti": multi_rank_sum,
        "rankCompared": rank_compared,
        "pagesSingle": single_pages,
        "pagesMulti": multi_pages,
        "pagesPerTermTotal": term_pages,
        "dedupedQueries": deduped_queries,
        "allTagged": all_tagged,
        "compound": {
            "total": len(COMPOUND_QUERIES),
            "singleHit": compound_single_hit, "multiHit": compound_multi_hit,
            "rows": compound_rows,
        },
        "toolQueriesOk": tool_ok,
        "legacyQueryOk": legacy_ok,
        "toolHitPages": len(tool_keys),
        "rows": rows,
    }


def run_baseline(case_dir: Path, keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents, list_documents
    from lawbuddy.search import search_pages
    from lawbuddy.agent.tasks import create_task, get_task, get_events
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.citations import list_claims
    from lawbuddy.storage import get_db

    files = sorted(p for p in case_dir.rglob("*") if p.is_file() and not p.parent.name.startswith("_scripts"))
    if not files:
        raise SystemExit(f"评测目录无文档：{case_dir}")

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m2_baseline",
        "dataset": str(case_dir),
        "fileCount": len(files),
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "mock_gateway",  # 确定性 mock 网关（脚本驱动；工具/落库/检索走真实实现）
    }
    try:
        ensure_workspace(str(tmp / "ws"))
        case = create_case(
            title="金标评测-王志远等合同诈骗案",
            case_type="criminal",
            client_name="王志远",
            client_role="被告人（王志远）辩护人",
        )
        case_id = case["id"]

        # ---- 1) 全量导入 ----
        outcomes = import_documents(case_id, [str(p) for p in files])
        imported = [o for o in outcomes if o["status"] == "imported"]
        failed = [o for o in outcomes if o["status"] == "failed"]
        kind_stats: dict[str, dict[str, int]] = {}
        for o in imported:
            k = str(o.get("kind") or "?")
            kind_stats.setdefault(k, {"files": 0, "pages": 0})
            kind_stats[k]["files"] += 1
            kind_stats[k]["pages"] += int(o.get("pageCount") or 0)
        docs = list_documents(case_id)
        total_pages = sum(d["pageCount"] or 0 for d in docs)
        ready = sum(1 for d in docs if d["status"] in ("ready", "partial"))
        report["import"] = {
            "total": len(files), "imported": len(imported), "failed": len(failed),
            "successRate": _fmt_rate(len(imported), len(files)),
            "kindStats": kind_stats, "indexedPages": total_pages,
            "parseReadyDocs": ready, "docCount": len(docs),
            "failures": [{"name": f["name"], "reason": f.get("reason")} for f in failed],
        }

        # ---- 2) 主体在库性 ----
        party_missing = []
        for p in GOLD_PARTIES:
            if not search_pages(case_id, p, limit=1):
                party_missing.append(p)
        report["parties"] = {
            "total": len(GOLD_PARTIES), "found": len(GOLD_PARTIES) - len(party_missing),
            "coverage": _fmt_rate(len(GOLD_PARTIES) - len(party_missing), len(GOLD_PARTIES)),
            "missing": party_missing,
        }

        # ---- 3) 检索召回 Recall@6 ----
        retrieval_rows = []
        hit_n = 0
        for query, expects in GOLD_RETRIEVALS:
            hits = search_pages(case_id, query, limit=6)
            ok = any(any(e in h["documentTitle"] for e in expects) for h in hits)
            hit_n += ok
            retrieval_rows.append({
                "query": query, "expected": expects, "hit": ok,
                "topDocs": [h["documentTitle"] for h in hits[:3]],
            })
        report["retrieval"] = {
            "total": len(GOLD_RETRIEVALS), "hit": hit_n,
            "recallAt6": _fmt_rate(hit_n, len(GOLD_RETRIEVALS)),
            "rows": retrieval_rows,
        }

        # ---- 3b) 查询拆解对照（05 方案 §6.7 / §10.4 D5） ----
        report["decomposedRetrieval"] = _decomposed_retrieval(case_id)

        # ---- 4) 四工作流 E2E（自主 Loop + mock 网关） ----
        doc_titles = {str(d.get("title") or "") for d in docs} - {""}
        wf_rows = []
        for preset, request in WORKFLOW_REQUESTS.items():
            undo = install_mock_gateway(_preset_steps(preset))
            try:
                task = create_task(
                    case_id=case_id, mode="legal_review",
                    request_text=request, workflow_preset=preset,
                )
                AgentRunner.instance().start(task["id"])
                for _ in range(150):
                    t = get_task(task["id"])
                    if t["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS", "CANCELLED", "FAILED"):
                        break
                    time.sleep(0.2)
            finally:
                undo()
            events = get_events(task["id"])
            loop_events = [e for e in events if e["type"] in LOOP_EVENT_TYPES]

            # 6 事件协议不变量
            starts = [e["payload"].get("callId") for e in loop_events if e["type"] == "tool_use_start"]
            dones = [e for e in loop_events if e["type"] == "tool_use_done"]
            turns = [e for e in loop_events if e["type"] == "turn_complete"]
            assistant = [e for e in loop_events if e["type"] == "assistant_message"]
            answer = str(assistant[-1]["payload"].get("content") or "") if assistant else ""
            protocol = {
                # 事件类型白名单（健康运行不得出现 message/error 等旧协议或异常事件）
                "eventWhitelist": all(e["type"] in LOOP_EVENT_TYPES | CONTROL_EVENT_TYPES for e in events),
                # 工具闭环：每个 tool_use_start 都有同 callId 的 tool_use_done 且状态 ok
                "toolClosedLoop": sorted(starts) == sorted(d["payload"].get("callId") for d in dones)
                                  and all(d["payload"].get("status") == "ok" for d in dones),
                # 循环以 final_answer 收束（非轮数耗尽）
                "turnCompleteFinal": bool(turns) and turns[-1]["payload"].get("reason") == "final_answer",
                # 回答基于工具结果：包含预设主题词，且回引了案内文档标题（检索命中）
                "answerGrounded": PRESET_ANSWER_KEYS.get(preset, "") in answer
                                  and "无命中" not in answer
                                  and any(t in answer for t in doc_titles),
            }
            claims = list_claims(task_id=task["id"])
            supported = sum(1 for c in claims if c["status"] == "supported")
            pending = sum(1 for c in claims if c["status"] == "needs_human")
            with_source = sum(1 for c in claims if c["sources"])
            wf_rows.append({
                "preset": preset, "lifecycle": t["lifecycle"],
                "completed": t["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS"),
                "rounds": turns[-1]["payload"].get("rounds") if turns else None,
                "protocol": protocol, "protocolOk": all(protocol.values()),
                "answerChars": len(answer),
                "claims": len(claims),
                "supported": supported, "needsHuman": pending, "withSource": with_source,
            })
        report["workflows"] = {
            "rows": wf_rows,
            "completed": sum(1 for r in wf_rows if r["completed"]),
            "protocolOk": sum(1 for r in wf_rows if r["protocolOk"]),
        }

        # ---- 5) Claim Ledger 总览（阅卷路径不产 Claim，信息性统计） ----
        all_claims = list_claims(case_id=case_id)
        supported = [c for c in all_claims if c["status"] == "supported"]
        db = get_db()
        fts_pages = db.query_one("SELECT COUNT(*) AS n FROM pages_fts WHERE case_id = ?", (case_id,))["n"]
        report["citations"] = {
            "claims": len(all_claims),
            "supported": len(supported),
            "needsHuman": sum(1 for c in all_claims if c["status"] == "needs_human"),
            "stale": sum(1 for c in all_claims if c["status"] == "stale"),
            "withSource": sum(1 for c in all_claims if c["sources"]),
            # 覆盖率：supported + needs_human（无来源即待核实）—— REQ-REV-008
            "coverage": _fmt_rate(len(supported) + sum(1 for c in all_claims if c["status"] == "needs_human"),
                                 len(all_claims)) if all_claims else None,
            "supportRate": _fmt_rate(len(supported), len(all_claims)),
            "ftsIndexedPages": fts_pages,
        }

        # ---- 6) 门槛判定（引证统计不设门：阅卷路径不产 Claim） ----
        dec = report["decomposedRetrieval"]
        gates = {
            "import100": report["import"]["successRate"] >= GATE_IMPORT_RATE,
            "parties100": report["parties"]["coverage"] >= GATE_PARTY_RATE,
            "recallAt6>=90%": report["retrieval"]["recallAt6"] >= GATE_RECALL,
            "e2e4of4": report["workflows"]["completed"] >= GATE_WORKFLOWS
                       and report["workflows"]["protocolOk"] >= GATE_WORKFLOWS,
            # D5：拆解不降召回、不变差首命中位次、结果真去重且工具层可用
            "decomposedRecallHeld": dec["multiRecallAt6"] >= dec["singleRecallAt6"],
            "decomposedRankHeld": (dec["avgMultiRank"] is None or dec["avgSingleRank"] is None
                                   or dec["avgMultiRank"] <= dec["avgSingleRank"]),
            "decomposedDeduped": dec["dedupedQueries"] >= 1 and dec["allTagged"],
            # D5：拆解的阅读成本必须低于"逐词各搜一遍再自行翻看"的合计页数
            "decomposedReadingCost": dec["pagesMulti"] < dec["pagesPerTermTotal"],
            # D5：多概念问句整串短语必落空，拆词后必须能召回（拆解的不可替代价值）
            "compoundQueryNeedsSplit": dec["compound"]["multiHit"] > dec["compound"]["singleHit"],
            "decomposedToolWired": dec["toolQueriesOk"] and dec["legacyQueryOk"],
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    imp = r["import"]
    print(f"\n=== M2 基线评测 · {r['dataset']} ===")
    print(f"文档：{imp['total']} 份（导入 {imp['imported']} / 失败 {imp['failed']}），"
          f"索引页 {imp['indexedPages']}，解析就绪 {imp['parseReadyDocs']}/{imp['docCount']}")
    for k, v in imp["kindStats"].items():
        print(f"  · {k}: {v['files']} 份 / {v['pages']} 页")
    for f in imp["failures"]:
        print(f"  ✗ 失败：{f['name']} —— {f['reason']}")
    p = r["parties"]
    print(f"主体在库性：{p['found']}/{p['total']}"
          + (f"（缺失：{'、'.join(p['missing'])}）" if p["missing"] else ""))
    ret = r["retrieval"]
    print(f"检索召回 Recall@6：{ret['hit']}/{ret['total']}（{ret['recallAt6']:.0%}）")
    for row in ret["rows"]:
        if not row["hit"]:
            print(f"  ✗ 未命中：『{row['query']}』→ 预期 {row['expected']}，实际 top3：{row['topDocs']}")
    dec = r.get("decomposedRetrieval") or {}
    if dec:
        print(f"查询拆解对照：单查询 Recall@6 {dec['singleRecallAt6']:.0%}"
              f" / 拆解 Recall@6 {dec['multiRecallAt6']:.0%}"
              f"（首命中位次 {dec['avgSingleRank']} → {dec['avgMultiRank']}，"
              f"去重生效 {dec['dedupedQueries']}/{dec['total']} 组，"
              f"多路命中页 {dec['toolHitPages']}）")
        print(f"  阅读成本：单查询合计 {dec['pagesSingle']} 页 / 拆解合并后 {dec['pagesMulti']} 页"
              f"（若逐词各看一遍为 {dec['pagesPerTermTotal']} 页）")
        comp = dec.get("compound") or {}
        if comp:
            print(f"  复合问句：整串短语命中 {comp['singleHit']}/{comp['total']}"
                  f" → 拆词后 {comp['multiHit']}/{comp['total']}")
            for row in comp["rows"]:
                if row["multiHit"] and not row["singleHit"]:
                    print(f"    + 拆解才召回：『{row['question']}』→ {row['terms']}")
        for row in dec["rows"]:
            if not row["multiHit"] and row["singleHit"]:
                print(f"  ✗ 拆解后漏召回：『{row['query']}』→ {row['terms']}")
            elif row["multiRank"] is not None and row["singleRank"] is not None \
                    and row["multiRank"] > row["singleRank"]:
                print(f"  ! 拆解后位次变差：『{row['query']}』{row['singleRank']} → {row['multiRank']}")
    wf = r["workflows"]
    print(f"阅卷 E2E：{wf['completed']}/4 完成，{wf['protocolOk']}/4 事件协议不变量通过")
    for row in wf["rows"]:
        proto = row["protocol"]
        flags = "".join(
            "✓" if proto[k] else "✗"
            for k in ("eventWhitelist", "toolClosedLoop", "turnCompleteFinal", "answerGrounded")
        )
        rounds = row.get("rounds")
        print(f"  · {row['preset']}: {row['lifecycle']} 协议[{flags}]"
              f"（{rounds if rounds is not None else '?'} 轮 · 回答 {row['answerChars']} 字）"
              f" · Claim {row['claims']}（supported {row['supported']} / 待核实 {row['needsHuman']}）")
    c = r["citations"]
    cov = f"{c['coverage']:.0%}" if c["coverage"] is not None else "—（无 Claim）"
    print(f"Claim Ledger：{c['claims']} 条，覆盖率 {cov}，支持率 {c['supportRate']:.0%}，"
          f"带来源 {c['withSource']}，FTS 页 {c['ftsIndexedPages']}")
    print("门槛判定：")
    for g, ok in r["gates"].items():
        print(f"  {'✓' if ok else '✗'} {g}")
    print(f"结论：{'M2 基线通过' if r['passed'] else 'M2 基线未通过（见上方 ✗ 项）'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M2 金标基线评测")
    parser.add_argument("--case-dir", default="../../case_file", help="卷宗目录（默认仓库根 case_file）")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    case_dir = Path(args.case_dir).resolve()
    if not case_dir.is_dir():
        raise SystemExit(f"目录不存在：{case_dir}")

    report = run_baseline(case_dir, keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m2_baseline_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
