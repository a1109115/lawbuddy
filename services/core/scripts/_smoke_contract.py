# -*- coding: utf-8 -*-
"""临时冒烟脚本：contract.py 数据层全链路。验证后删除。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import shutil
import tempfile
from datetime import datetime

from lawbuddy.workspace import ensure_workspace
from lawbuddy.cases import create_case
from lawbuddy.documents import import_documents
from lawbuddy import contract as ct

CONTRACT_TEXT = """设备买卖合同

第一条 合同标的
甲方向乙方出售精密数控机床一台（规格型号 XK-7136），设备应符合本合同附件一约定的技术标准。

第二条 价款与支付
设备总价款为人民币100万元。乙方应在本合同签署后10日内一次性支付全款。
逾期付款的，甲方有权解除合同。

第三条 交付与验收
甲方应于收到全部价款后15日内交付设备。乙方应在收到设备后7日内完成验收，
逾期未提出异议的视为验收合格。

第四条 质量保证
甲方对设备提供12个月质保。质保期内因设备自身缺陷造成的维修费用由甲方承担。

第五条 违约责任
乙方逾期付款的，每逾期一日按合同总价款的百分之一向甲方支付违约金。
甲方逾期交付的，每逾期一日按合同总价款的百分之一向乙方支付违约金。

第六条 争议解决
因本合同引起的争议，双方应友好协商解决；协商不成的，提交北京仲裁委员会仲裁。"""

def main():
    tmp = Path(tempfile.mkdtemp(prefix="ct-smoke-"))
    try:
        ensure_workspace(str(tmp / "ws"))
        case = create_case(title="冒烟-合同审查", case_type="contract", client_name="乙公司")
        case_id = case["id"]

        p = tmp / "contract.txt"
        p.write_text(CONTRACT_TEXT, encoding="utf-8")
        docs = import_documents(case_id, [str(p)])
        assert docs[0]["status"] == "imported", docs[0]
        doc_id = docs[0]["documentId"]

        # 1. 创建会话：非法输入校验
        for kw in (
            dict(reviewer_role="hacker"),
            dict(reviewer_role="custom", custom_role="  "),
            dict(risk_appetite="wild"),
        ):
            try:
                ct.create_review(case_id, doc_id, **kw)
                raise AssertionError(f"应拒绝 {kw}")
            except ct.ContractError:
                pass
        # 跨案件文档校验
        case2 = create_case(title="冒烟-另一案件", case_type="contract", client_name="丙公司")
        try:
            ct.create_review(case2["id"], doc_id)
            raise AssertionError("应拒绝跨案件文档")
        except ct.ContractError:
            pass

        review = ct.create_review(
            case_id, doc_id, reviewer_role="buyer",
            risk_appetite="conservative", transaction_goal="采购设备并控制付款风险",
        )
        rid = review["id"]
        assert review["contractType"] is None
        print("1. create_review OK")

        # 2. 条款切分 + 落库（含页码定位）
        from lawbuddy.documents import get_pages
        pages = get_pages(doc_id)
        full = ct.join_pages(pages)
        clauses = ct.split_clauses(full)
        assert len(clauses) == 6, [(c["clauseRef"], c["heading"]) for c in clauses]
        assert clauses[0]["heading"] == "合同标的"
        assert clauses[4]["heading"] == "违约责任"
        for c in clauses:
            c["pageNo"] = ct.page_of_offset(pages, full, c["charStart"])
            c["refs"] = ct.extract_cross_refs(c["text"], c["clauseRef"])
        assert ct.detect_contract_type(full) == "买卖合同"
        ct.save_clauses(rid, clauses)
        assert len(ct.list_clauses(rid)) == 6
        print("2. split_clauses + save OK（6 条，类型=买卖合同）")

        # 3. 风险项落库
        clause5 = [c for c in ct.list_clauses(rid) if c["clauseRef"] == "第五条"][0]
        items = [
            {
                "riskLevel": "severe", "category": "business", "clauseRef": "第五条",
                "originalText": "每逾期一日按合同总价款的百分之一向甲方支付违约金",
                "riskNote": "日百分之一即年化36.5%，远超司法保护上限，作为买方极可能被认定过高",
                "impact": "乙方逾期付款时违约金成本失控，且诉讼中法院调减存在不确定性",
                "suggestion": "将违约金调整为日万分之五并设置总额上限（不超过合同总价款的10%）",
                "replacementText": "每逾期一日按合同总价款的万分之五向甲方支付违约金，违约金总额不超过合同总价款的百分之十",
                "basis": "民法典第五百八十五条：约定违约金过分高于造成损失的，人民法院可以予以适当减少",
                "confidence": "verified",
            },
            {
                "riskLevel": "high", "category": "completeness", "clauseRef": "第二条",
                "originalText": "逾期付款的，甲方有权解除合同。",
                "riskNote": "付款义务缺少宽限期与催告程序，轻微逾期即触发解除权，对买方过苛",
                "impact": "短期迟延付款可能导致合同被解除，设备采购计划中断",
                "suggestion": "增加30日宽限期与书面催告前置程序",
                "replacementText": "乙方逾期付款的，经甲方书面催告后30日内仍未支付的，甲方有权解除合同。",
                "basis": "民法典第五百六十三条：经催告后在合理期限内仍未履行的，方可解除合同",
                "confidence": "high",
            },
        ]
        n = ct.save_risk_items(rid, items)
        risks = ct.list_risk_items(rid)
        assert n == 2 and len(risks) == 2
        assert risks[0]["riskLevel"] == "severe" and risks[0]["clauseHeading"] == "违约责任"
        assert risks[0]["clausePage"] == 1
        assert all(r["decision"] == "pending" for r in risks)
        assert ct.get_review(rid)["riskStats"]["total"] == 2
        print("3. save_risk_items OK（排序 severe 优先）")

        # 4. 接受 → 工作副本生成，原件不变
        r_severe = risks[0]["id"]
        ct.decide_risk(r_severe, "accept")
        wc = ct.get_work_copy(rid)
        assert wc is not None and wc["versionNo"] == 1, wc
        assert len(wc["appliedRiskIds"]) == 1
        from lawbuddy.documents import get_pages as _gp
        orig_page = _gp(doc_id)[0]["text"]
        assert orig_page == CONTRACT_TEXT, "原件必须保持不变"
        assert ct.get_risk_item(r_severe)["decision"] == "accepted"
        # 工作副本文本：替换已生效
        wv = None
        from lawbuddy.storage import get_db
        row = get_db().query_one(
            "SELECT text_content FROM document_pages WHERE version_id = ?", (wc["documentVersionId"],))
        assert "万分之五" in row["text_content"], row["text_content"][:100]
        print("4. accept → work_copy v1 OK（原件未动，替换已写入副本）")

        # 5. 第二次接受 → 副本 v2 包含两处修改
        r_grace = ct.list_risk_items(rid)[1]["id"]
        ct.decide_risk(r_grace, "accept")
        wc2 = ct.get_work_copy(rid)
        assert wc2["versionNo"] == 2 and len(wc2["appliedRiskIds"]) == 2
        row2 = get_db().query_one(
            "SELECT text_content FROM document_pages WHERE version_id = ?", (wc2["documentVersionId"],))
        assert "万分之五" in row2["text_content"] and "书面催告后30日" in row2["text_content"]
        print("5. accept ×2 → work_copy v2 OK")

        # 6. undo → 恢复 pending，副本为仅含一处修改的新快照（版本链 append-only）
        ct.undo(rid)
        assert ct.get_risk_item(r_grace)["decision"] == "pending"
        wc3 = ct.get_work_copy(rid)
        assert wc3["versionNo"] == 3 and len(wc3["appliedRiskIds"]) == 1
        print("6. undo OK（最近操作回滚，副本新快照仅含 1 处修改）")

        # 7. redo → 恢复 accepted
        ct.redo(rid)
        assert ct.get_risk_item(r_grace)["decision"] == "accepted"
        wc4 = ct.get_work_copy(rid)
        assert wc4["versionNo"] == 4, wc4
        print("7. redo OK（副本 v4）")

        # 8. undo 全部 → 副本删除
        ct.undo(rid)
        ct.undo(rid)
        assert ct.get_risk_item(r_severe)["decision"] == "pending"
        assert ct.get_work_copy(rid) is None
        try:
            ct.undo(rid)
            raise AssertionError("空栈 undo 应拒绝")
        except ct.ContractError:
            pass
        print("8. undo×2 → 副本删除 OK；空栈拒绝")

        # 9. redo 后插入新正向操作 → redo 被拒
        ct.redo(rid)
        ct.decide_risk(r_severe, "edit", edited_text="逾期付款按日万分之三支付违约金，上限为合同总价款的5%")
        try:
            ct.redo(rid)
            raise AssertionError("撤销后有新操作时 redo 应拒绝")
        except ct.ContractError:
            pass
        edited = ct.get_risk_item(r_severe)
        assert edited["decision"] == "edited" and "万分之三" in edited["editedText"]
        print("9. edit + redo-guard OK")

        # 10. 汇总载荷
        payload = ct.build_review_payload(rid)
        assert payload["review"]["riskStats"]["total"] == 2
        assert len(payload["risks"]) == 2 and payload["workCopy"] is not None
        stats = ct.get_review(rid)["riskStats"]
        assert stats["accepted"] == 1 and stats["pending"] == 0, stats  # 另一项为 edited
        print(f"10. build_review_payload OK（stats={stats}）")

        # 11. 列表
        reviews = ct.list_reviews(case_id)
        assert len(reviews) == 1 and reviews[0]["primaryDocumentTitle"] == "contract"
        print("11. list_reviews OK")
        print("\nCONTRACT SMOKE: ALL OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
