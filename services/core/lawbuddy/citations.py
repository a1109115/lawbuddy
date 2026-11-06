"""Claim Ledger：SourceRef / Claim 落库与引用校验（DEV-AGT-008，技术方案 §9）。

- SourceRef：页级原文引用（quote + 规范化引语 + OCR 置信度 + corpus 版本）；
- 引语校验（Q-010）：规范化后 quote 必须是页文本子串——只允许空白/断行差异；
- Claim 状态判定（技术方案 §9.3/9.4）：
  * 页文本匹配 + OCR 置信达标 → supported（exact_quote）
  * 引语不匹配页文本        → needs_human（QUOTE_MISMATCH）
  * OCR 置信 < 0.85          → needs_human（LOW_OCR_CONFIDENCE）
  * 要点无检索来源支撑       → needs_human（OUT_OF_EVIDENCE_SCOPE）
- stale：文档删除/版本变化标记（documents.delete_document），recheck_claim 重验；
- conflict：矛盾证据（R-004）——报告 contradictions 双侧、证据卡间矛盾互指
  （mark_conflict），人工裁决经 resolve_conflict 入审计。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from .storage import get_db
from .workspace import now_iso

LOW_OCR_CONFIDENCE = 0.85

STATUS_SUPPORTED = "supported"
STATUS_NEEDS_HUMAN = "needs_human"


class CitationError(RuntimeError):
    pass


def normalize_text(text: str) -> str:
    """规范化：空白、断行、全半角空格统一（Q-010 只允许此类差异）。"""
    return re.sub(r"[\s\u3000]+", "", text or "")


def clean_snippet(snippet: str) -> str:
    """去掉检索摘要中的高亮标记、省略号与首尾残缺标点（FTS 噪音，非原文内容）。"""
    text = (snippet or "").replace("【", "").replace("】", "")
    text = text.replace("…", "").replace("...", "")
    return text.strip("　 \t\n\r，。；、")


def make_source_ref(hit: dict[str, Any]) -> dict[str, Any]:
    """从检索命中构建 SourceRef（含该页 OCR 置信度）。"""
    db = get_db()
    quote = clean_snippet(str(hit.get("snippet") or ""))
    page = db.query_one(
        """
        SELECT p.ocr_confidence, v.id AS version_id
        FROM document_pages p
        JOIN document_versions v ON v.id = p.version_id
        WHERE p.version_id = ? AND p.page_no = ?
        """,
        (hit["versionId"], hit["pageNo"]),
    )
    ref = {
        "id": str(uuid.uuid4()),
        "documentId": hit["documentId"],
        "documentVersionId": hit["versionId"],
        "pageNo": hit["pageNo"],
        "quote": quote,
        "quoteNorm": normalize_text(quote),
        "ocrConfidence": (page["ocr_confidence"] if page else None),
        "corpusVersion": 1,
    }
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO source_refs(id, document_id, document_version_id, page_no, quote, quote_norm, ocr_confidence, corpus_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ref["id"], ref["documentId"], ref["documentVersionId"], ref["pageNo"],
             ref["quote"], ref["quoteNorm"], ref["ocrConfidence"], ref["corpusVersion"], now_iso()),
        )
    return ref


def verify_quote(quote_norm: str, version_id: str, page_no: int) -> bool:
    """引语校验：规范化 quote 是否出现在该页规范化文本中（Q-010）。"""
    db = get_db()
    page = db.query_one(
        "SELECT text_content FROM document_pages WHERE version_id = ? AND page_no = ?",
        (version_id, page_no),
    )
    if page is None or not page["text_content"]:
        return False
    return quote_norm in normalize_text(page["text_content"])


def create_claim(
    task_id: str,
    case_id: str,
    claim_type: str,
    text: str,
    status: str,
    reason_code: str | None,
    confidence_basis: str | None,
    source_ref: dict[str, Any] | None,
) -> dict[str, Any]:
    """落库 Claim 并关联 SourceRef，返回 UI 引证数据。"""
    db = get_db()
    claim_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO claims(id, task_id, case_id, type, text, status, reason_code, confidence_basis, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (claim_id, task_id, case_id, claim_type, text, status, reason_code, confidence_basis, now_iso()),
        )
        if source_ref is not None:
            conn.execute(
                "INSERT INTO claim_sources(claim_id, source_ref_id, relation) VALUES (?, ?, 'supports')",
                (claim_id, source_ref["id"]),
            )
    cite: dict[str, Any] = {
        "claimId": claim_id,
        "claimStatus": status,
        "reasonCode": reason_code,
    }
    if source_ref is not None:
        cite.update(
            {
                "documentId": source_ref["documentId"],
                "documentVersionId": source_ref["documentVersionId"],
                "pageNo": source_ref["pageNo"],
                "label": f"{hit_title(source_ref)} · 第{source_ref['pageNo']}页",
                "quote": source_ref["quote"][:120],
            }
        )
    return cite


def hit_title(source_ref: dict[str, Any]) -> str:
    db = get_db()
    row = db.query_one("SELECT title FROM documents WHERE id = ?", (source_ref["documentId"],))
    return row["title"] if row else "文档"


def judge_claim(
    task_id: str,
    case_id: str,
    text: str,
    source_hit: dict[str, Any] | None,
) -> dict[str, Any]:
    """质量门禁（技术方案 §9.3）：为单条要点建 Claim 并判定状态。"""
    if source_hit is None:
        # 输出超出已检索证据（9.4 OUT_OF_EVIDENCE_SCOPE）
        return create_claim(
            task_id, case_id, "inference", text,
            STATUS_NEEDS_HUMAN, "OUT_OF_EVIDENCE_SCOPE", None, None,
        )

    ref = make_source_ref(source_hit)
    if not ref["quoteNorm"] or not verify_quote(ref["quoteNorm"], ref["documentVersionId"], ref["pageNo"]):
        return create_claim(
            task_id, case_id, "fact", text,
            STATUS_NEEDS_HUMAN, "QUOTE_MISMATCH", None, ref,
        )
    if ref["ocrConfidence"] is not None and ref["ocrConfidence"] < LOW_OCR_CONFIDENCE:
        return create_claim(
            task_id, case_id, "fact", text,
            STATUS_NEEDS_HUMAN, "LOW_OCR_CONFIDENCE", None, ref,
        )
    return create_claim(
        task_id, case_id, "fact", text,
        STATUS_SUPPORTED, None, "exact_quote", ref,
    )


def list_claims(task_id: str | None = None, case_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    """查询 Claim（含来源），供待人工核实队列与追踪使用（PRD 11.4 基础）。"""
    db = get_db()
    sql = """
        SELECT c.*, GROUP_CONCAT(sr.document_id || ':' || sr.page_no) AS sources
        FROM claims c
        LEFT JOIN claim_sources cs ON cs.claim_id = c.id
        LEFT JOIN source_refs sr ON sr.id = cs.source_ref_id
    """
    conds, params = [], []
    if task_id:
        conds.append("c.task_id = ?")
        params.append(task_id)
    if case_id:
        conds.append("c.case_id = ?")
        params.append(case_id)
    if status:
        conds.append("c.status = ?")
        params.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " GROUP BY c.id ORDER BY c.created_at ASC"
    rows = db.query(sql, tuple(params))
    result = []
    for r in rows:
        item = {
            "id": r["id"], "taskId": r["task_id"], "caseId": r["case_id"],
            "type": r["type"], "text": r["text"], "status": r["status"],
            "reasonCode": r["reason_code"], "confidenceBasis": r["confidence_basis"],
            "sources": (r["sources"] or "").split(",") if r["sources"] else [],
            "createdAt": r["created_at"],
            "conflictsWith": r["conflicts_with"],
        }
        if r["conflicts_with"]:
            counter = db.query_one("SELECT text FROM claims WHERE id = ?", (r["conflicts_with"],))
            item["counterpartText"] = counter["text"] if counter else None
        else:
            item["counterpartText"] = None
        result.append(item)
    return result


def mark_conflict(claim_a: str, claim_b: str) -> bool:
    """矛盾证据互指（R-004）：双侧当前均为 supported 时置 conflict 并互指。

    任一侧不是 supported（如 needs_human）时不覆盖——保持待人工核实语义，
    由用户在队列中先行处理；返回是否成功建立冲突关联。
    """
    db = get_db()
    row = db.query_one(
        "SELECT a.status AS sa, b.status AS sb FROM claims a, claims b WHERE a.id = ? AND b.id = ?",
        (claim_a, claim_b),
    )
    if row is None or row["sa"] != "supported" or row["sb"] != "supported":
        return False
    with db.transaction() as conn:
        conn.execute(
            "UPDATE claims SET status = 'conflict', reason_code = 'SOURCE_CONFLICT', conflicts_with = ? WHERE id = ?",
            (claim_b, claim_a),
        )
        conn.execute(
            "UPDATE claims SET status = 'conflict', reason_code = 'SOURCE_CONFLICT', conflicts_with = ? WHERE id = ?",
            (claim_a, claim_b),
        )
    return True


def resolve_conflict(claim_id: str, decision: str, note: str = "") -> dict[str, Any]:
    """矛盾裁决（R-004 人工核实）：采纳一侧或整体驳回，入审计。

    - confirmed：本条 → supported / user_confirmed；对侧 → needs_human（CONFLICT_REJECTED），
      保持可见供用户后续单独处理；
    - rejected：双条删除（claim_sources 级联），审计留痕。
    """
    db = get_db()
    row = db.query_one("SELECT case_id, status, conflicts_with FROM claims WHERE id = ?", (claim_id,))
    if row is None:
        raise CitationError("待裁决的矛盾项不存在")
    if row["status"] != "conflict" or not row["conflicts_with"]:
        raise CitationError("该项不是矛盾证据，请用核实/重验操作处理")
    if decision not in ("confirmed", "rejected"):
        raise CitationError(f"无效裁决决定：{decision}")
    from . import audit

    counterpart = row["conflicts_with"]
    ts = now_iso()
    meta = {"note": (note or "").strip()[:200], "decidedBy": "local_owner", "counterpartId": counterpart}
    with db.transaction() as conn:
        if decision == "confirmed":
            conn.execute(
                "UPDATE claims SET status = 'supported', reason_code = NULL, conflicts_with = NULL, "
                "confidence_basis = 'user_confirmed' WHERE id = ?",
                (claim_id,),
            )
            conn.execute(
                "UPDATE claims SET status = 'needs_human', reason_code = 'CONFLICT_REJECTED', conflicts_with = NULL "
                "WHERE id = ?",
                (counterpart,),
            )
        else:
            conn.execute("DELETE FROM claims WHERE id IN (?, ?)", (claim_id, counterpart))
        audit.append(conn, action="claim.conflict.resolve", resource_type="claim",
                     resource_id=claim_id, case_id=row["case_id"], metadata=meta)
    return {"claimId": claim_id, "decision": decision, "counterpartId": counterpart, "decidedAt": ts}


def verify_claim(claim_id: str, decision: str, note: str = "") -> dict[str, Any]:
    """人工核实（R-008）：记录人员、时间与意见，入审计并更新状态。

    - confirmed：标记用户确认 → supported / user_confirmed（PRD 11.4 处理方式之一）；
    - rejected：驳回 → 从 Ledger 删除该 Claim（claim_sources 级联），审计留痕。
    """
    db = get_db()
    row = db.query_one("SELECT case_id, status FROM claims WHERE id = ?", (claim_id,))
    if row is None:
        raise CitationError("待核实项不存在")
    if decision not in ("confirmed", "rejected"):
        raise CitationError(f"无效核实决定：{decision}")
    from . import audit

    ts = now_iso()
    meta = {"note": (note or "").strip()[:200], "decidedBy": "local_owner", "prevStatus": row["status"]}
    with db.transaction() as conn:
        if decision == "confirmed":
            conn.execute(
                "UPDATE claims SET status = 'supported', reason_code = NULL, "
                "confidence_basis = 'user_confirmed' WHERE id = ?",
                (claim_id,),
            )
            audit.append(conn, action="claim.confirm", resource_type="claim",
                         resource_id=claim_id, case_id=row["case_id"], metadata=meta)
        else:
            conn.execute("DELETE FROM claims WHERE id = ?", (claim_id,))  # claim_sources 级联
            audit.append(conn, action="claim.reject", resource_type="claim",
                         resource_id=claim_id, case_id=row["case_id"], metadata=meta)
    return {"claimId": claim_id, "decision": decision, "decidedAt": ts}


def recheck_claim(claim_id: str) -> dict[str, Any]:
    """失效重验（PRD 11.3：重新定位成功前不能保持已核实）。

    对 stale Claim 重跑引语校验：来源已删除或引语不再匹配则保持 stale；
    校验通过则恢复 supported（exact_quote）。
    """
    db = get_db()
    row = db.query_one("SELECT case_id, status FROM claims WHERE id = ?", (claim_id,))
    if row is None:
        raise CitationError("待核实项不存在")
    if row["status"] != "stale":
        return {"claimId": claim_id, "status": row["status"], "recovered": False, "reasonCode": None}

    ref = db.query_one(
        """
        SELECT sr.document_version_id, sr.page_no, sr.quote_norm, sr.ocr_confidence
        FROM source_refs sr
        JOIN claim_sources cs ON cs.source_ref_id = sr.id
        WHERE cs.claim_id = ? ORDER BY sr.created_at ASC LIMIT 1
        """,
        (claim_id,),
    )
    recovered = False
    if ref is None:
        new_status, reason = "stale", "SOURCE_UNAVAILABLE"  # 来源文档已删除
    elif not ref["quote_norm"] or not verify_quote(
        ref["quote_norm"], ref["document_version_id"], ref["page_no"]
    ):
        new_status, reason = "stale", "QUOTE_MISMATCH"
    elif ref["ocr_confidence"] is not None and ref["ocr_confidence"] < LOW_OCR_CONFIDENCE:
        new_status, reason = "stale", "LOW_OCR_CONFIDENCE"
    else:
        new_status, reason, recovered = "supported", None, True

    from . import audit

    with db.transaction() as conn:
        conn.execute(
            "UPDATE claims SET status = ?, reason_code = ?, confidence_basis = ? WHERE id = ?",
            (new_status, reason, "exact_quote" if recovered else None, claim_id),
        )
        audit.append(conn, action="claim.recheck", resource_type="claim",
                     resource_id=claim_id, case_id=row["case_id"],
                     metadata={"recovered": recovered, "reason": reason})
    return {"claimId": claim_id, "status": new_status, "recovered": recovered, "reasonCode": reason}
