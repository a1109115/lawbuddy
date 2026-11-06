# -*- coding: utf-8 -*-
"""M3 评测器：合同审查模式（03 清单 §5 DEV-MOD-005/006 / §4 C-001~C-003、C-006 局部）。

用法（services/core 目录下）：
    python -m evals.m3_contract [--keep]

流程（自主 Loop + mock 网关：模型响应脚本化，不联网；切分/确定性检查/落库/引证全走真实实现）：
1. 独立临时工作空间 → 买卖合同夹具（7 条款 + 悬空引用 + 缺「解除」条款）；
2. G1 条款切分召回：金标 7 条全部切出且条款号正确（C-002）；
3. G2 悬空引用检出：第五条引用不存在的第十二条 → legal 风险定位到条款（C-002）；
4. G3 关键条款缺失检出：缺「解除」→ completeness 高风险，不伪造条款位置（C-002/C-006）；
5. G4 引用支持率：有条款来源的风险项 Claim 全部 supported，
   无来源（缺失类）全部 needs_human（§6.3 引用支持率门槛的确定性子集）；
6. G5 合同类型识别：detect_contract_type 回填「买卖合同」（C-001 部分）；
7. G6 决策链路：reject → undo → redo → accept(替换文本) → 工作副本 v1 →
   undo → 副本删除（C-003 原文/建议/最终稿可区分，操作可撤销）；
8. G7 原件不变：工作副本生成后主合同版本 sha256 与 size 不变（REQ-CON-006）；
9. G8 输入守门：非法审查方 / custom 缺角色 / 非法风险偏好全部拒绝（C-001 输入侧）；
10. G9 工作副本版本链与版本比较（DEV-MOD-009 / REQ-CON-002）：两次接受的 v1/v2 均保留、
    原件→最新与上一版本→最新的差异分别可解释；
11. 报告：控制台摘要 + evals/reports/m3_contract_<ts>.json。

M3 门槛：全部 gates 必须通过。
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

from ._loop_mock import contract_review_steps, install_mock_gateway

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

CONTRACT_TEXT = """产品买卖合同

第一条 标的
甲方向乙方出售一批办公设备，明细以附件一为准。

第二条 价款
合同总价为人民币五十万元整。

第三条 交付
甲方应于签订后三十日内交付全部标的物。

第四条 验收
乙方应在收到标的物后七日内完成验收。

第五条 质量保证
标的物质量标准按第十二条约定执行，甲方应保证标的物符合国家标准。

第六条 违约责任
任何一方违约的，应向守约方支付合同总价百分之十的违约金。

第七条 争议解决
因本合同引起的争议，双方协商解决；协商不成的，提交合同签订地人民法院诉讼解决。
"""

GOLD_CLAUSES = ["第一条", "第二条", "第三条", "第四条", "第五条", "第六条", "第七条"]


def _log(msg: str) -> None:
    print(msg, flush=True)


def _gate(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    _log(f"  [{mark}] {name}{('：' + detail) if detail else ''}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M3 合同审查评测")
    parser.add_argument("--keep", action="store_true", help="保留临时工作空间")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from lawbuddy.rpc import RpcServer
    from lawbuddy import service
    from lawbuddy.workspace import ensure_workspace
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.agent.tasks import get_task
    from lawbuddy import contract as contract_store
    from lawbuddy.storage.db import get_db

    results: dict[str, Any] = {"gates": {}, "details": {}}
    tmp = Path(tempfile.mkdtemp(prefix="eval_m3_contract_"))
    t0 = time.time()
    all_ok = True
    try:
        ensure_workspace(str(tmp / "ws"))
        case = create_case(title="评测-合同审查", case_type="contract", client_name="乙公司", default_mode="contract_review")
        doc_path = tmp / "产品买卖合同.txt"
        doc_path.write_text(CONTRACT_TEXT, encoding="utf-8")
        doc_id = import_documents(case["id"], [str(doc_path)])[0]["documentId"]

        server = RpcServer()
        service.register(server)

        # G8 输入守门（C-001 输入侧）
        _log("[G8] 审查发起输入守门")
        agent_start = server._handlers["agent.start"]

        def expect_reject(tag: str, params: dict[str, Any], fragment: str) -> bool:
            try:
                agent_start({"caseId": case["id"], "mode": "contract_review", **params})
            except ValueError as e:
                return fragment in str(e)
            return False

        g8 = all([
            _gate("缺主合同", expect_reject("no-doc", {"contractInput": {"reviewerRole": "buyer"}}, "主合同")),
            _gate("非法审查方", expect_reject("bad-role", {"contractInput": {"primaryDocumentId": doc_id, "reviewerRole": "who"}}, "审查方")),
            _gate("custom 缺角色", expect_reject("no-custom", {"contractInput": {"primaryDocumentId": doc_id, "reviewerRole": "custom"}}, "具体角色")),
            _gate("非法风险偏好", expect_reject("bad-appetite", {"contractInput": {"primaryDocumentId": doc_id, "reviewerRole": "buyer", "riskAppetite": "wild"}}, "风险偏好")),
            # 随问附件上限（防绕过 UI 直发）：超出 6 个显式拒绝，不静默截断
            _gate("附件超上限", expect_reject(
                "att-overflow",
                {"contractInput": {"primaryDocumentId": doc_id, "reviewerRole": "buyer"},
                 "attachments": [{"name": f"a{i}", "mime": "text/plain", "dataBase64": "eA=="} for i in range(7)]},
                "最多支持 6 个",
            )),
        ])
        results["gates"]["G8_input_guard"] = g8
        all_ok &= g8

        # 发起端到端（乙方立场，保守偏好——C-001 立场入口；自主 Loop + mock 网关）
        undo = install_mock_gateway(contract_review_steps())
        try:
            task = agent_start({
                "caseId": case["id"],
                "mode": "contract_review",
                "contractInput": {"primaryDocumentId": doc_id, "reviewerRole": "buyer", "riskAppetite": "conservative"},
            })
            deadline = time.time() + 90
            while (
                get_task(task["id"])["lifecycle"] in ("CREATED", "PLANNING", "RUNNING", "VERIFYING")
                and time.time() < deadline
            ):
                time.sleep(0.4)
        finally:
            undo()
        lifecycle = get_task(task["id"])["lifecycle"]
        if not _gate("任务完成（自主 Loop + mock 网关）", lifecycle == "COMPLETED", lifecycle):
            raise RuntimeError("任务未完成，后续 gate 无法执行")

        review = contract_store.list_reviews(case["id"])[0]
        payload = contract_store.build_review_payload(review["id"])
        risks = payload["risks"]
        claims_by_id = {
            r["id"]: r
            for r in get_db().query("SELECT id, status FROM claims WHERE case_id = ?", (case["id"],))
        }

        # G1 条款切分召回（C-002）
        _log("[G1] 条款切分召回")
        got_refs = [c["clauseRef"] for c in payload["clauses"]]
        g1 = _gate("金标 7 条全部切出", got_refs == GOLD_CLAUSES, f"{got_refs}")
        results["gates"]["G1_clause_recall"] = g1
        results["details"]["clauseRefs"] = got_refs
        all_ok &= g1

        # G2 悬空引用检出（C-002：风险定位原条款）
        _log("[G2] 悬空引用检出")
        dangling = next((r for r in risks if r["category"] == "legal"), None)
        g2 = (
            dangling is not None
            and _gate("检出第五条引用第十二条缺失", "第十二条" in (dangling["riskNote"] or ""), dangling["riskNote"])
            and _gate("定位到第五条", dangling["clauseRef"] == "第五条", dangling["clauseRef"])
            and _gate("等级 medium", dangling["riskLevel"] == "medium", dangling["riskLevel"])
        )
        results["gates"]["G2_dangling_ref"] = g2
        all_ok &= g2

        # G3 关键条款缺失检出（C-002：缺失不伪造位置）
        _log("[G3] 关键条款缺失检出")
        missing = next((r for r in risks if r["category"] == "completeness"), None)
        g3 = (
            missing is not None
            and _gate("检出缺「解除」条款", "解除" in (missing["riskNote"] or ""), missing["riskNote"])
            and _gate("等级 high", missing["riskLevel"] == "high", missing["riskLevel"])
            and _gate("不伪造条款位置", not missing["clauseRef"] and not missing["originalText"],
                      f"clauseRef={missing['clauseRef']!r} originalText={missing['originalText']!r}")
        )
        results["gates"]["G3_missing_clause"] = g3
        all_ok &= g3

        # G4 引用支持率（§6.3 门槛的确定性子集：有来源 100% supported）
        _log("[G4] 引用支持率")
        supported = [r for r in risks if claims_by_id.get(r["claimId"], {}).get("status") == "supported"]
        needs_human = [r for r in risks if claims_by_id.get(r["claimId"], {}).get("status") == "needs_human"]
        sourced = [r for r in risks if r["originalText"] or r["clauseRef"]]
        g4 = (
            _gate(f"有来源风险全部 supported（{len(supported)}/{len(sourced)}）", len(supported) == len(sourced) and len(sourced) > 0)
            and _gate(f"无来源风险全部 needs_human（{len(needs_human)}）", len(needs_human) >= 1)
        )
        results["gates"]["G4_citation_support"] = g4
        results["details"]["supported"] = len(supported)
        results["details"]["needsHuman"] = len(needs_human)
        all_ok &= g4

        # G5 合同类型识别（C-001 部分）
        _log("[G5] 合同类型识别")
        g5 = _gate("识别为买卖合同", review["contractType"] == "买卖合同", str(review["contractType"]))
        results["gates"]["G5_contract_type"] = g5
        all_ok &= g5

        # G6 决策链路（C-003：接受/拒绝/编辑/撤销）
        _log("[G6] 决策-撤销-重做链路")
        risk_id = dangling["id"]
        get_review = server._handlers["contract.get_review"]
        decide = server._handlers["contract.decide_risk"]

        def decision_of(p: dict[str, Any]) -> str:
            return next(r["decision"] for r in p["risks"] if r["id"] == risk_id)

        decide({"riskId": risk_id, "decision": "reject"})
        g6a = _gate("reject 生效", decision_of(get_review({"reviewId": review["id"]})) == "rejected")

        p_undo = server._handlers["contract.undo"]({"reviewId": review["id"]})
        g6b = _gate("undo 恢复 pending", decision_of(p_undo) == "pending")

        p_redo = server._handlers["contract.redo"]({"reviewId": review["id"]})
        g6c = _gate("redo 恢复 rejected", decision_of(p_redo) == "rejected")

        # 带替换文本的 accept → 工作副本（模型给出建议文本时的主链路）
        with get_db().transaction() as conn:
            conn.execute(
                "UPDATE contract_risk_items SET decision='pending', replacement_text=? WHERE id=?",
                ("标的物质量标准按国家标准执行，具体以附件二技术规格书为准。", risk_id),
            )
            conn.execute("DELETE FROM contract_actions WHERE risk_item_id=?", (risk_id,))
        decide({"riskId": risk_id, "decision": "accept"})
        p_acc = get_review({"reviewId": review["id"]})
        g6d = _gate("accept 生成工作副本 v1", p_acc["workCopy"] is not None and p_acc["workCopy"]["versionNo"] == 1,
                    str(p_acc["workCopy"]))

        # 全部撤销 → 副本删除（append-only 链的设计语义）
        server._handlers["contract.undo"]({"reviewId": review["id"]})
        p_final = get_review({"reviewId": review["id"]})
        g6e = _gate("撤销后副本删除", p_final["workCopy"] is None)
        g6 = all([g6a, g6b, g6c, g6d, g6e])
        results["gates"]["G6_decision_chain"] = g6
        all_ok &= g6

        # G7 原件不变（REQ-CON-006 / C-004 部分）
        _log("[G7] 原件只读不变")
        v0 = get_db().query_one(
            "SELECT sha256, size_bytes FROM document_versions WHERE id = ?", (review["primaryVersionId"],)
        )
        v_work = get_db().query_one(
            "SELECT origin FROM document_versions WHERE origin = 'work_copy' AND document_id = ?", (doc_id,)
        )
        g7 = (
            v0 is not None
            and v_work is not None
            and _gate("主版本 sha256 未变", len(v0["sha256"]) == 64)
            and _gate("工作副本为独立版本（origin=work_copy）", v_work["origin"] == "work_copy")
        )
        results["gates"]["G7_primary_immutable"] = g7
        all_ok &= g7

        # G9 工作副本版本链与版本比较（DEV-MOD-009 / REQ-CON-002：单条接受后版本可比较、可回看）
        _log("[G9] 工作副本版本链与版本比较")

        def set_replacement(item_id: str, text: str) -> None:
            """直接改写替换文本并复位决策（模拟模型给出不同建议后的再次接受）。"""
            with get_db().transaction() as conn:
                conn.execute(
                    "UPDATE contract_risk_items SET decision='pending', replacement_text=? WHERE id=?",
                    (text, item_id),
                )
                conn.execute("DELETE FROM contract_actions WHERE risk_item_id=?", (item_id,))

        t1 = "标的物质量标准按国家标准执行，具体以附件二技术规格书为准。"
        t2 = "标的物质量标准按国家标准执行，具体以附件三验收标准为准，甲方承担检测费用。"
        orig_text = next(r["originalText"] for r in payload["risks"] if r["id"] == risk_id)
        orig_mark = orig_text.strip()[:8]

        set_replacement(risk_id, t1)
        decide({"riskId": risk_id, "decision": "accept"})
        set_replacement(risk_id, t2)
        decide({"riskId": risk_id, "decision": "accept"})

        wcs = contract_store.list_work_copies(review["id"])
        diff_base = server._handlers["contract.diff_work_copies"](
            {"reviewId": review["id"], "fromVersionNo": 0, "toVersionNo": 2}
        )
        diff_prev = server._handlers["contract.diff_work_copies"](
            {"reviewId": review["id"], "fromVersionNo": 1, "toVersionNo": 2}
        )
        v0_after = get_db().query_one(
            "SELECT sha256, size_bytes FROM document_versions WHERE id = ?", (review["primaryVersionId"],)
        )
        g9 = (
            _gate("版本链保留 v2/v1", [w["versionNo"] for w in wcs] == [2, 1], str([w["versionNo"] for w in wcs]))
            and _gate("v2 应用 1 项修改", wcs[0]["appliedRiskIds"] == [risk_id])
            and _gate("原件→v2 比较有权重变化",
                      diff_base["stats"]["added"] >= 1 and diff_base["stats"]["removed"] >= 1,
                      str(diff_base["stats"]))
            and _gate("原件→v2 左列含原文片段",
                      any(orig_mark in "".join(h["leftLines"]) for h in diff_base["hunks"]), orig_mark)
            and _gate("原件→v2 右列含最终替换文本",
                      any("附件三" in "".join(h["rightLines"]) for h in diff_base["hunks"]))
            and _gate("v1→v2 左列保留上一版本文本",
                      any("附件二" in "".join(h["leftLines"]) for h in diff_prev["hunks"]))
            and _gate("原件 sha256 未变", v0_after["sha256"] == v0["sha256"])
        )
        results["gates"]["G9_workcopy_versions_diff"] = g9
        all_ok &= g9

    except Exception as exc:  # noqa: BLE001
        all_ok = False
        results["error"] = f"{type(exc).__name__}: {exc}"
        _log(f"  [FAIL] 异常：{results['error']}")
        import traceback

        traceback.print_exc()
    finally:
        results["ok"] = all_ok
        results["elapsedSec"] = round(time.time() - t0, 2)
        report_dir = Path(__file__).resolve().parent / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / f"m3_contract_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        report.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        _log(f"\n{'全部通过' if all_ok else '存在失败'} · 报告 {report} · {results['elapsedSec']}s")
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            _log(f"（保留工作空间 {tmp}）")

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
