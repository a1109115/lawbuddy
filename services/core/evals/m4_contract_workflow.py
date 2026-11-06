"""Overview/checklist version, source-readiness and confirmation gates."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch


def main() -> int:
    from lawbuddy import contract_workflow as workflow
    from lawbuddy.cases import archive_case, create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.storage import get_db
    from lawbuddy.storage.db import reset_database
    from lawbuddy.workspace import ensure_workspace

    def rejects(action, fragment: str) -> None:
        try:
            action()
        except (ValueError, RuntimeError) as exc:
            assert fragment in str(exc), str(exc)
        else:
            raise AssertionError(f"Expected {fragment}")

    with tempfile.TemporaryDirectory(prefix="lawbuddy_checklist_") as temp:
        try:
            ensure_workspace(str(Path(temp) / "ws"))
            case = create_case("Review", "contract", "", default_mode="contract_review")
            overview = workflow.create_overview(case["id"])
            assert workflow.create_overview(case["id"])["id"] == overview["id"]
            assert overview["parseStatus"] == "no_document"
            rejects(lambda: workflow.require_ready(overview["id"]), "立场")
            path = Path(temp) / "contract.txt"
            path.write_text("第一条 价款\n合同价款为十万元。", encoding="utf-8")
            doc = import_documents(case["id"], [str(path)])[0]["documentId"]
            config = {"primaryDocumentId": doc, "stance": "乙方", "reviewIntensity": "standard"}
            overview = workflow.update_overview(overview["id"], config, overview["revision"])
            assert overview["parseStatus"] == "ready"
            rejects(lambda: workflow.update_overview(overview["id"], config, 1), "VERSION_CONFLICT")
            rejects(lambda: workflow.update_overview(overview["id"], {**config, "unexpected": True}, 2), "配置")
            print("[PASS] Overview idempotency, readiness and optimistic concurrency")

            items = [{"id": "price", "title": "核对价款", "criterion": "核对支付条件与金额",
                      "category": "business", "enabled": True}]
            snapshot = workflow.update_checklist(overview["id"], items, 0)
            confirmed = workflow.confirm_checklist(overview["id"], snapshot["revision"])
            assert confirmed["status"] == "confirmed" and len(confirmed["hash"]) == 64
            assert workflow.confirm_checklist(overview["id"], snapshot["revision"]) == confirmed
            next_snapshot = workflow.update_checklist(overview["id"], [{**items[0], "title": "核对支付时间"}], 1)
            assert next_snapshot["id"] != confirmed["id"]
            assert workflow.get_checklist(overview["id"], confirmed["id"])["items"][0]["title"] == "核对价款"
            rejects(lambda: workflow.confirm_checklist(overview["id"], 1), "VERSION_CONFLICT")
            rejects(lambda: workflow.update_checklist(overview["id"], items * 2, 2), "ID")
            disabled = workflow.update_checklist(overview["id"], [{**items[0], "enabled": False}], 2)
            rejects(lambda: workflow.confirm_checklist(overview["id"], disabled["revision"]), "启用")
            print("[PASS] Immutable snapshots, stable IDs and confirmation gates")

            overview = workflow.update_overview(overview["id"], {**config, "stance": "中立"}, 2)
            assert workflow.get_checklist(overview["id"], confirmed["id"])["status"] == "stale"
            with get_db().transaction() as conn:
                conn.execute("UPDATE documents SET status = 'partial' WHERE id = ?", (doc,))
            assert workflow.get_overview(overview["id"])["parseStatus"] == "limited_scope"
            rejects(lambda: workflow.require_ready(overview["id"]), "PARSE_INCOMPLETE")
            overview = workflow.update_overview(
                overview["id"], {**config, "limitedScopeAcknowledged": True}, overview["revision"])
            assert workflow.require_ready(overview["id"])["parseStatus"] == "limited_scope"
            logs = get_db().query("SELECT metadata_json FROM audit_logs WHERE action LIKE 'contract.%'")
            assert all("支付条件" not in row["metadata_json"] and "乙方" not in row["metadata_json"] for row in logs)
            state = {"task_id": "mock", "case_id": case["id"], "contract_input": {
                "overviewId": overview["id"], "overviewRevision": overview["revision"], "checklistRevision": 3,
            }}
            generated_response = {"items": [{"title": "核对价款", "criterion": "比较价款和支付条件", "category": "business"}]}
            events = []
            with patch("lawbuddy.contract_workflow._model_json", return_value=generated_response):
                result = workflow.run_checklist(state, lambda kind, payload: events.append(kind), lambda: False)
            assert result["answer"]
            generated = workflow.get_checklist(overview["id"])
            assert generated["revision"] == 4 and generated["items"][0]["origin"] == "ai"
            assert "turn_complete" in events
            with patch("lawbuddy.contract_workflow._model_json", return_value={"items": [{"title": "Invalid"}]}) as invalid:
                rejects(lambda: workflow.run_checklist(state, lambda *_: None, lambda: False), "MODEL_SCHEMA_INVALID")
                assert invalid.call_count == 3
            assert workflow.get_checklist(overview["id"])["revision"] == 4
            print("[PASS] Fixed LangGraph generation, AI origin and invalid-schema rejection")
            from lawbuddy.agent.tasks import create_task
            import uuid

            confirmed = workflow.confirm_checklist(overview["id"], 4)
            risk_task = create_task(case["id"], "contract_review", "test")
            with get_db().transaction() as conn:
                conn.execute(
                    "INSERT INTO contract_review_runs(id, overview_id, snapshot_id, task_id, request_id, created_at) "
                    "VALUES ('risk-run', ?, ?, ?, 'risk-request', ?)",
                    (overview["id"], confirmed["id"], risk_task["id"], overview["updatedAt"]),
                )
                # 多页合同回归：txt 导入只有单页，补第二页模拟多页 PDF/DOCX。
                # 历史缺陷：逐页切分 seq 跨页重复，撞 contract_clauses 的 UNIQUE(review_id, seq)。
                conn.execute(
                    "INSERT INTO document_pages(id, version_id, page_no, text_content, text_quality, ocr_status) "
                    "VALUES (?, ?, 2, ?, 'native', 'none')",
                    (str(uuid.uuid4()), overview["config"]["primaryVersionId"],
                     "第二条 交付\n乙方应在签约后三十日内交付全部货物。"),
                )
                conn.execute(
                    "UPDATE document_versions SET page_count = 2 WHERE id = ?",
                    (overview["config"]["primaryVersionId"],),
                )
            risk_state = {"task_id": risk_task["id"], "case_id": case["id"],
                          "contract_input": {"runId": "risk-run"}}
            with patch("lawbuddy.contract_workflow._model_json", side_effect=RuntimeError("synthetic unavailable")):
                rejects(lambda: workflow.run_risk_review(risk_state, lambda *_: None, lambda: False), "unavailable")
            incomplete = workflow.get_risk_run(overview["id"])
            assert incomplete["status"] == "partial"
            assert incomplete["items"][0]["reasonCode"] == "REVIEW_INCOMPLETE"
            assert get_db().query("SELECT * FROM contract_item_results WHERE run_id = 'risk-run'") == []
            with patch("lawbuddy.contract_workflow._model_json", return_value={
                "status": "pass", "reason": "价款已记载", "originalText": "合同价款为十万元。", "pageNo": 1, "risks": [],
            }):
                workflow.run_risk_review(risk_state, lambda *_: None, lambda: False)
            risk_run = workflow.get_risk_run(overview["id"])
            assert risk_run["status"] == "completed" and len(risk_run["items"]) == risk_run["total"] == 1
            review_id = get_db().query_one(
                "SELECT review_id FROM contract_review_runs WHERE id = 'risk-run'")["review_id"]
            seqs = [row["seq"] for row in get_db().query(
                "SELECT seq FROM contract_clauses WHERE review_id = ? ORDER BY seq", (review_id,))]
            assert len(seqs) == 2 and seqs == sorted(set(seqs)), seqs
            with patch("lawbuddy.contract_workflow._model_json", side_effect=AssertionError("Must not repeat completed item")):
                workflow.run_risk_review(risk_state, lambda *_: None, lambda: False)
            assert len(workflow.get_risk_run(overview["id"])["items"]) == 1
            print("[PASS] Fixed risk coverage, quote validation and completed-item replay")
            locate_pages = [
                {"pageNo": 1, "text": "第一条 价款\n合同价款为十万元。"},
                {"pageNo": 2, "text": "第二条 交付\n乙方应在签约后三十日内交付全部货物。"},
            ]
            # 模型页码错误：引文实际在第 2 页，按原文纠正
            fixed = workflow.locate_quote(locate_pages, "乙方应在签约后三十日内交付全部货物。", 1)
            assert fixed["pageNo"] == 2 and fixed["exact"] and not fixed["ambiguous"], fixed
            # 引文含省略号：近似定位到可匹配片段，但不得视为 exact
            fuzzy = workflow.locate_quote(locate_pages, "乙方应在签约后……交付全部货物。", 1)
            assert fuzzy["pageNo"] == 2 and not fuzzy["exact"] and fuzzy["quote"], fuzzy
            # 首尾省略号只是截断标记：去掉后仍是连续原文片段，可判为 exact
            truncated = workflow.locate_quote(locate_pages, "合同价款为十万元……", 1)
            assert truncated["pageNo"] == 1 and truncated["exact"], truncated
            assert truncated["quote"] == "合同价款为十万元", truncated
            # 多页重复且模型页码未命中：保留页码但标记歧义
            duplicate = [{"pageNo": 1, "text": "甲方应支付十万元。"},
                         {"pageNo": 2, "text": "甲方应支付十万元。"}]
            ambiguous = workflow.locate_quote(duplicate, "甲方应支付十万元。", 3)
            assert ambiguous["pageNo"] == 1 and ambiguous["exact"] and ambiguous["ambiguous"], ambiguous
            # 模型页码命中重复页：可消歧
            resolved = workflow.locate_quote(duplicate, "甲方应支付十万元。", 2)
            assert resolved["pageNo"] == 2 and not resolved["ambiguous"], resolved
            print("[PASS] Quote locator corrects page drift, tolerates ellipsis and flags page ambiguity")
            from lawbuddy import citations, word
            import json

            source_version = overview["config"]["primaryVersionId"]
            claim = citations.judge_claim(risk_task["id"], case["id"], "核对合成价款", {
                "documentId": doc, "versionId": source_version, "pageNo": 1, "snippet": "合同价款为十万元。",
            })
            with get_db().transaction() as conn:
                # Synthetic parsed text has no OCR risk; verify the test's supported source explicitly.
                conn.execute("UPDATE document_pages SET ocr_confidence = NULL WHERE version_id = ?", (source_version,))
            claim = citations.judge_claim(risk_task["id"], case["id"], "核对合成价款", {
                "documentId": doc, "versionId": source_version, "pageNo": 1, "snippet": "合同价款为十万元。",
            })
            result = workflow.get_risk_run(overview["id"])["items"][0]
            result["risks"] = [{"id": "synthetic-risk", "claimId": claim["claimId"], "cite": claim,
                                "originalText": "合同价款为十万元。", "replacementText": "合同价款以双方确认表为准。"}]
            with get_db().transaction() as conn:
                conn.execute("UPDATE contract_item_results SET result_json = ? WHERE run_id = 'risk-run'",
                             (json.dumps(result, ensure_ascii=False),))
            work_copy = word.create_work_copy(case["id"], document_id=doc)
            applied = workflow.apply_risk_suggestion(overview["id"], "synthetic-risk", work_copy["id"],
                                                    work_copy["revision"], "合同价款以双方确认表为准。", "apply-1")
            assert applied["applyState"] == "applied"
            entry = workflow.get_risk_run(overview["id"])["items"][0]["risks"][0]["applications"][0]
            assert entry["revision"] == applied["workCopy"]["revision"]
            print("[PASS] Risk application and work-copy version committed together / persisted application record")
            archive_case(case["id"])
            assert workflow.get_overview(overview["id"])["id"] == overview["id"]
            rejects(lambda: workflow.update_checklist(overview["id"], items, 3), "只读")
            print("[PASS] Invalidation, limited-scope acknowledgement, audit minimization and archive")
        finally:
            reset_database()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
