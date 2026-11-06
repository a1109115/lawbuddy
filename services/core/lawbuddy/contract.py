"""合同审查模块（DEV-MOD-005/006 / PRD §14 / 技术方案 §5.2、§6.3）。

- 解析层（纯函数）：条款切分、定义与交叉引用解析、合同类型识别，
  是 `contract.split_clauses` 工具与 Agent 合同子图的内核；
- 数据层：审查会话、条款、风险项落库，接受 / 拒绝 / 编辑 / 撤销 / 重做操作历史
  （REQ-CON-007），接受修改只写工作副本版本、原件只读不变（REQ-CON-006）。
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any

from . import crypto
from .keyring import get_active_dek
from .storage import get_db
from .workspace import BLOB_DIRNAME, now_iso


class ContractError(RuntimeError):
    pass


REVIEWER_ROLES = (
    "seller", "buyer", "lessor", "lessee", "lender", "borrower",
    "employer", "contractor", "custom",
)
REVIEWER_ROLE_LABELS = {
    "seller": "卖方", "buyer": "买方", "lessor": "出租方", "lessee": "承租方",
    "lender": "出借方", "borrower": "借款方", "employer": "用人单位", "contractor": "承包方",
    "custom": "自定义",
}
RISK_APPETITES = ("conservative", "balanced", "aggressive")
RISK_LEVELS = ("severe", "high", "medium", "low")
RISK_LEVEL_LABELS = {"severe": "严重", "high": "高", "medium": "中", "low": "低"}
RISK_CATEGORIES = ("business", "legal", "completeness")
CONFIDENCES = ("verified", "high", "medium", "low")


# ===========================================================================
# 解析层：条款切分（contract.split_clauses 内核 / PRD 14.3 步骤3）
# ===========================================================================

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}


def cn_num_to_int(s: str) -> int | None:
    """中文数字（含混排阿拉伯）转整数；解析失败返回 None。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    total, num = 0, 0
    for ch in s:
        if ch.isdigit():
            num = num * 10 + int(ch)
            continue
        v = _CN_DIGITS.get(ch)
        if v is None:
            return None
        if v >= 10:
            num = (num or 1) * v
            if v >= 100:
                total += num
                num = 0
        else:
            num += v
    return total + num


# 条款头模式：第X条 / 多级数字（1.1） / （一） / 一、
_HEAD_ARTICLE = re.compile(r"第([零〇一二三四五六七八九十百千\d]+)条")
_HEAD_NUM = re.compile(r"^(\d+(?:\.\d+)+)[、.．:：\s]")
_HEAD_CN_MARK = re.compile(r"^([一二三四五六七八九十百]+)、")


def _heading_of(rest: str) -> str | None:
    """条款头同行剩余文本若为短标题则提取（"第三条 违约责任" → 违约责任）。"""
    rest = rest.strip().strip("：: 　")
    if not rest or len(rest) > 30:
        return None
    if re.search(r"[。；;]", rest):
        return None
    return rest


def split_clauses(full_text: str) -> list[dict[str, Any]]:
    """按条款头切分合同文本。

    优先级：第X条 > 多级数字编号 > 中文顿号编号；无编号时按空行段落兜底。
    返回 [{seq, clauseRef, heading, text, charStart, charEnd}]。
    """
    lines = full_text.splitlines(keepends=True)
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)

    # 收集三种模式的候选条款头
    marks: list[tuple[int, int, str, str]] = []  # (offset, priority, clause_ref, line_rest)
    for i, ln in enumerate(lines):
        stripped = ln.lstrip(" \t　")
        lead = ln[: len(ln) - len(stripped)]
        off = offsets[i] + len(lead)
        if not stripped:
            continue

        m = re.match(rf"(第[零〇一二三四五六七八九十百千\d]+条)\s*(.*)", stripped)
        if m:
            marks.append((off, 0, m.group(1), m.group(2)))
            continue
        m = _HEAD_NUM.match(stripped)
        if m:
            marks.append((off, 1, m.group(1), stripped[m.end(1):]))
            continue
        m = _HEAD_CN_MARK.match(stripped)
        if m:
            marks.append((off, 2, m.group(1) + "、", stripped[m.end(1):]))
            continue

    # 选定切分模式：优先级最高且数量 >= 2 的那种
    for prio in (0, 1, 2):
        heads = [mk for mk in marks if mk[1] == prio]
        if len(heads) >= 2:
            return _emit_clauses(full_text, heads)

    # 兜底：按非空段落切分
    paras: list[tuple[int, int]] = []
    start: int | None = None
    for i, ln in enumerate(lines):
        if ln.strip():
            if start is None:
                start = offsets[i]
        elif start is not None:
            paras.append((start, offsets[i]))
            start = None
    if start is not None:
        paras.append((start, len(full_text)))
    if not paras:
        return []
    return [
        {
            "seq": idx,
            "clauseRef": f"段落{idx}",
            "heading": None,
            "text": full_text[s:e],
            "charStart": s,
            "charEnd": e,
        }
        for idx, (s, e) in enumerate(paras)
    ]


def _emit_clauses(full_text: str, heads: list[tuple[int, int, str, str]]) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    for idx, (off, _p, ref, rest) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(full_text)
        text = full_text[off:end]
        clauses.append({
            "seq": idx,
            "clauseRef": ref,
            "heading": _heading_of(rest),
            "text": text.strip(),
            "charStart": off,
            "charEnd": end,
        })
    return clauses


def page_of_offset(pages: list[dict[str, Any]], full_text: str, offset: int) -> int | None:
    """全文字符偏移 → 页码。pages 为 documents.get_pages 输出（按页顺序）。"""
    start = 0
    for p in pages:
        end = start + len(p["text"]) + 1  # 拼接时每页补一个换行
        if offset < end:
            return p["pageNo"]
        start = end
    return pages[-1]["pageNo"] if pages else None


def join_pages(pages: list[dict[str, Any]]) -> str:
    """多页文本拼成全文（页间以换行连接，与 page_of_offset 的偏移口径一致）。"""
    return "\n".join(p["text"] for p in pages)


# ---------------------------------------------------------------------------
# 定义与交叉引用解析
# ---------------------------------------------------------------------------

_DEF_MEANS = re.compile(r"[\"“]([^\"”]{2,24})[\"”][，,]?\s*(?:系指|是指|指的是)([^。；;\n]{2,150})")
_DEF_ALIAS = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9（）()]{2,24}?)（以下简称[\"“]?([^\"”）)]{2,24})[\"”？]?）")
_DEF_COMMA = re.compile(r"([^，。；,;\n]{2,24})[，,]?\s*(?:系指|是指)([^。；;\n]{2,150})")


def extract_definitions(text: str) -> list[dict[str, str]]:
    """识别定义条款中的术语定义（PRD 14.3 步骤3：定义与交叉引用解析）。"""
    found: dict[str, dict[str, str]] = {}
    for pat in (_DEF_MEANS, _DEF_ALIAS, _DEF_COMMA):
        for m in pat.finditer(text):
            term = m.group(1).strip(" \"“”")
            if not term:
                continue
            item = {"term": term, "definition": m.group(2).strip()}
            if len(pat.groups) >= 2 and pat is _DEF_ALIAS:
                item["alias"] = m.group(2).strip(" \"“”")
            found.setdefault(term, item)
    return list(found.values())[:80]


def extract_cross_refs(clause_text: str, own_ref: str) -> list[str]:
    """条款文本中对其他条款的引用（"详见第X条"），去重保持出现顺序。"""
    refs: list[str] = []
    for m in _HEAD_ARTICLE.finditer(clause_text):
        ref = m.group(0)
        if ref != own_ref and ref not in refs:
            refs.append(ref)
    return refs


_CONTRACT_TYPE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("买卖合同", ("买卖", "购销", "采购", "销售", "出售")),
    ("租赁合同", ("租赁", "出租", "承租")),
    ("借款合同", ("借款", "借贷", "贷款")),
    ("劳动合同", ("劳动合同", "试用期", "用人单位")),
    ("服务合同", ("服务", "委托", "代理")),
    ("技术开发合同", ("技术开发", "技术转让", "专利", "知识产权")),
    ("建设工程合同", ("建设工程", "施工", "工程承包", "监理")),
    ("合作合同", ("合作", "合伙", "合资")),
    ("股权转让合同", ("股权转让")),
    ("担保合同", ("保证", "抵押", "质押", "担保")),
    ("运输合同", ("运输", "承运")),
    ("保险合同", ("保险")),
]


def detect_contract_type(text: str) -> str | None:
    """从合同开头文本识别合同类型（PRD 14.3 步骤2）。"""
    head = text[:1500]
    for name, keys in _CONTRACT_TYPE_HINTS:
        for k in keys:
            if k in head:
                return name
    return None


_MISSING_CLAUSE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("违约", "缺少违约责任条款，违约后难以追责"),
    ("争议解决", "缺少争议解决条款，纠纷处理方式与管辖不明"),
    ("解除", "缺少解除/终止条款，合同退出机制不明"),
)


def detect_deterministic_risks(
    clauses: list[dict[str, Any]], full_text: str
) -> list[dict[str, Any]]:
    """确定性合同风险检查（不依赖模型，PRD 14.3）：
    1. 交叉引用悬空：条款引用的「第X条」在切分结果中不存在（medium/legal）；
    2. 关键条款缺失：全文检索不到违约责任/争议解决/解除相关表述（high/completeness）。
    纯函数，不落库不截断（上限由调用方决定）；返回风险项列表可直接传
    `save_risk_items`。悬空项携带条款原文可回溯（supported），
    缺失项无来源（needs_human），由 Claim 绑定逻辑区分。"""
    ref_set = {str(c.get("clauseRef") or "") for c in clauses}
    risks: list[dict[str, Any]] = []

    for c in clauses:
        dangling = [r for r in (c.get("refs") or []) if r not in ref_set]
        if not dangling:
            continue
        risks.append({
            "riskLevel": "medium",
            "category": "legal",
            "clauseRef": str(c.get("clauseRef") or ""),
            "originalText": str(c.get("text") or "")[:120],
            "riskNote": f"引用的{dangling[0]}在合同中不存在",
            "impact": "被引条款缺失，权利义务指向不明",
            "suggestion": f"核对{dangling[0]}编号或补充该条款",
            "replacementText": "",
            "basis": "条款切分结果中无该编号",
            "confidence": "verified",
        })
    for kw, note in _MISSING_CLAUSE_KEYWORDS:
        if kw not in full_text:
            risks.append({
                "riskLevel": "high",
                "category": "completeness",
                "clauseRef": "",
                "originalText": "",
                "riskNote": f"未检索到「{kw}」相关条款",
                "impact": note,
                "suggestion": f"补充{kw}相关条款",
                "replacementText": "",
                "basis": "合同全文关键词检索",
                "confidence": "verified",
            })
    return risks


# ===========================================================================
# 数据层：审查会话
# ===========================================================================

def _require_document(case_id: str, document_id: str) -> dict[str, Any]:
    from . import documents as docs

    row = get_db().query_one(
        "SELECT id, case_id, title, status, active_version_id FROM documents WHERE id = ?",
        (document_id,),
    )
    if row is None:
        raise ContractError("文档不存在")
    if row["case_id"] != case_id:
        raise ContractError("文档不属于当前案件")
    if row["status"] not in ("ready", "partial"):
        raise ContractError(f"文档尚未解析完成（当前状态：{row['status']}）")
    pages = docs.get_pages(document_id)
    if not any(p["text"].strip() for p in pages):
        raise ContractError("文档无可提取文本，请等待 OCR 完成后重试")
    return row


def create_review(
    case_id: str,
    primary_document_id: str,
    reviewer_role: str = "custom",
    custom_role: str | None = None,
    risk_appetite: str = "balanced",
    contract_type: str | None = None,
    transaction_goal: str | None = None,
    governing_law: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """创建合同审查会话（PRD 14.2 输入表：主合同/审查方/风险偏好必填）。"""
    from .cases import require_case_mode

    require_case_mode(case_id, "contract_review")
    if reviewer_role not in REVIEWER_ROLES:
        raise ContractError("审查方角色不合法")
    if reviewer_role == "custom" and not (custom_role or "").strip():
        raise ContractError("自定义审查方必须填写具体角色")
    if risk_appetite not in RISK_APPETITES:
        raise ContractError("风险偏好不合法")

    doc = _require_document(case_id, primary_document_id)

    review_id = str(uuid.uuid4())
    ts = now_iso()
    db = get_db()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO contract_reviews(
              id, case_id, task_id, primary_document_id, primary_version_id,
              reviewer_role, custom_role, risk_appetite, contract_type,
              transaction_goal, governing_law, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'analyzing', ?, ?)
            """,
            (review_id, case_id, task_id, primary_document_id, doc["active_version_id"],
             reviewer_role, custom_role, risk_appetite, contract_type,
             transaction_goal, governing_law, ts, ts),
        )
    return get_review(review_id)


def _row_to_review(row: dict[str, Any]) -> dict[str, Any]:
    db = get_db()
    doc = db.query_one("SELECT title FROM documents WHERE id = ?", (row["primary_document_id"],))
    stats = db.query_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN risk_level = 'severe' THEN 1 ELSE 0 END) AS severe,
               SUM(CASE WHEN decision = 'accepted' THEN 1 ELSE 0 END) AS accepted,
               SUM(CASE WHEN decision = 'rejected' THEN 1 ELSE 0 END) AS rejected,
               SUM(CASE WHEN decision = 'pending' THEN 1 ELSE 0 END) AS pending
        FROM contract_risk_items WHERE review_id = ?
        """,
        (row["id"],),
    )
    return {
        "id": row["id"],
        "caseId": row["case_id"],
        "taskId": row["task_id"],
        "primaryDocumentId": row["primary_document_id"],
        "primaryVersionId": row["primary_version_id"],
        "primaryDocumentTitle": doc["title"] if doc else None,
        "reviewerRole": row["reviewer_role"],
        "reviewerRoleLabel": REVIEWER_ROLE_LABELS.get(row["reviewer_role"], row["reviewer_role"]),
        "customRole": row["custom_role"],
        "riskAppetite": row["risk_appetite"],
        "contractType": row["contract_type"],
        "transactionGoal": row["transaction_goal"],
        "governingLaw": row["governing_law"],
        "status": row["status"],
        "degradedReason": row["degraded_reason"],
        "riskStats": {
            "total": stats["total"] or 0,
            "severe": stats["severe"] or 0,
            "accepted": stats["accepted"] or 0,
            "rejected": stats["rejected"] or 0,
            "pending": stats["pending"] or 0,
        },
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get_review(review_id: str) -> dict[str, Any]:
    row = get_db().query_one("SELECT * FROM contract_reviews WHERE id = ?", (review_id,))
    if row is None:
        raise ContractError("审查会话不存在")
    return _row_to_review(row)


def list_reviews(case_id: str) -> list[dict[str, Any]]:
    rows = get_db().query(
        "SELECT * FROM contract_reviews WHERE case_id = ? ORDER BY updated_at DESC",
        (case_id,),
    )
    return [_row_to_review(r) for r in rows]


def _require_writable_review(review_id: str) -> dict[str, Any]:
    from .cases import require_case_mode

    review = get_review(review_id)
    require_case_mode(review["caseId"], "contract_review")
    run = get_db().query_one("SELECT overview_id, snapshot_id FROM contract_review_runs WHERE review_id = ?", (review_id,))
    if run:
        from .contract_workflow import get_checklist

        snapshot = get_checklist(run["overview_id"], run["snapshot_id"])
        if not snapshot or snapshot["status"] != "confirmed":
            raise ContractError("SNAPSHOT_STALE")
    return review


def update_review_status(review_id: str, status: str, degraded_reason: str | None = None) -> None:
    _require_writable_review(review_id)
    with get_db().transaction() as conn:
        conn.execute(
            "UPDATE contract_reviews SET status = ?, degraded_reason = ?, updated_at = ? WHERE id = ?",
            (status, degraded_reason, now_iso(), review_id),
        )


def set_contract_type(review_id: str, contract_type: str | None) -> None:
    """切分后识别出合同类型时回填（用户未显式指定时）。"""
    _require_writable_review(review_id)
    with get_db().transaction() as conn:
        conn.execute(
            "UPDATE contract_reviews SET contract_type = ?, updated_at = ? WHERE id = ?",
            (contract_type, now_iso(), review_id),
        )


# ===========================================================================
# 数据层：条款与风险项
# ===========================================================================

def save_clauses(review_id: str, clauses: list[dict[str, Any]]) -> int:
    """条款切分结果落库（含定义/交叉引用）。"""
    _require_writable_review(review_id)
    ts = now_iso()
    db = get_db()
    with db.transaction() as conn:
        conn.execute("DELETE FROM contract_clauses WHERE review_id = ?", (review_id,))
        for idx, c in enumerate(clauses):
            conn.execute(
                """
                INSERT INTO contract_clauses(
                  id, review_id, seq, clause_ref, heading, text,
                  page_no, char_start, char_end, refs_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), review_id, c.get("seq", idx), c.get("clauseRef", ""),
                 c.get("heading"), c.get("text", ""), c.get("pageNo"),
                 c.get("charStart"), c.get("charEnd"),
                 _json(c.get("refs", [])), ts),
            )
    return len(clauses)


def _risk_row(r: dict[str, Any], review_id: str) -> dict[str, Any]:
    level = r.get("riskLevel", "medium")
    if level not in RISK_LEVELS:
        raise ContractError(f"风险等级不合法：{level}")
    conf = r.get("confidence", "medium")
    if conf not in CONFIDENCES:
        conf = "medium"
    cat = r.get("category", "legal")
    if cat not in RISK_CATEGORIES:
        cat = "legal"
    return {
        "risk_level": level, "category": cat,
        "clause_ref": r.get("clauseRef", ""), "original_text": r.get("originalText", ""),
        "risk_note": r.get("riskNote", ""), "impact": r.get("impact"),
        "suggestion": r.get("suggestion"), "replacement_text": r.get("replacementText"),
        "basis": r.get("basis"), "confidence": conf, "claim_id": r.get("claimId"),
        "clause_id": r.get("clauseId"),
    }


def save_risk_items(review_id: str, items: list[dict[str, Any]]) -> int:
    """Agent 风险分析结果落库（PRD 14.4 输出字段）。"""
    _require_writable_review(review_id)
    ts = now_iso()
    db = get_db()
    clause_by_ref = {
        r["clause_ref"]: r["id"]
        for r in db.query("SELECT id, clause_ref FROM contract_clauses WHERE review_id = ?", (review_id,))
    }
    with db.transaction() as conn:
        conn.execute("DELETE FROM contract_risk_items WHERE review_id = ?", (review_id,))
        for r in items:
            v = _risk_row(r, review_id)
            clause_id = v["clause_id"] or clause_by_ref.get(v["clause_ref"])
            conn.execute(
                """
                INSERT INTO contract_risk_items(
                  id, review_id, claim_id, risk_level, category, clause_id, clause_ref,
                  original_text, risk_note, impact, suggestion, replacement_text, basis,
                  confidence, decision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (str(uuid.uuid4()), review_id, v["claim_id"], v["risk_level"], v["category"],
                 clause_id, v["clause_ref"], v["original_text"], v["risk_note"], v["impact"],
                 v["suggestion"], v["replacement_text"], v["basis"], v["confidence"], ts, ts),
            )
    return len(items)


def _row_to_risk(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "reviewId": row["review_id"],
        "claimId": row["claim_id"],
        "clauseId": row["clause_id"],
        "riskLevel": row["risk_level"],
        "riskLevelLabel": RISK_LEVEL_LABELS.get(row["risk_level"], row["risk_level"]),
        "category": row["category"],
        "clauseRef": row["clause_ref"],
        "originalText": row["original_text"],
        "riskNote": row["risk_note"],
        "impact": row["impact"],
        "suggestion": row["suggestion"],
        "replacementText": row["replacement_text"],
        "basis": row["basis"],
        "confidence": row["confidence"],
        "decision": row["decision"],
        "editedText": row["edited_text"],
        "decidedAt": row["decided_at"],
    }


def list_risk_items(review_id: str) -> list[dict[str, Any]]:
    rows = get_db().query(
        """
        SELECT r.*, c.heading AS clause_heading, c.page_no AS clause_page
        FROM contract_risk_items r
        LEFT JOIN contract_clauses c ON c.id = r.clause_id
        WHERE r.review_id = ?
        ORDER BY CASE r.risk_level WHEN 'severe' THEN 0 WHEN 'high' THEN 1
                 WHEN 'medium' THEN 2 ELSE 3 END, r.created_at
        """,
        (review_id,),
    )
    out = []
    for row in rows:
        item = _row_to_risk(row)
        item["clauseHeading"] = row["clause_heading"]
        item["clausePage"] = row["clause_page"]
        out.append(item)
    return out


def get_risk_item(risk_id: str) -> dict[str, Any]:
    row = get_db().query_one("SELECT * FROM contract_risk_items WHERE id = ?", (risk_id,))
    if row is None:
        raise ContractError("风险项不存在")
    return _row_to_risk(row)


def list_clauses(review_id: str) -> list[dict[str, Any]]:
    rows = get_db().query(
        "SELECT * FROM contract_clauses WHERE review_id = ? ORDER BY seq", (review_id,)
    )
    return [
        {
            "id": r["id"], "seq": r["seq"], "clauseRef": r["clause_ref"],
            "heading": r["heading"], "text": r["text"], "pageNo": r["page_no"],
            "charStart": r["char_start"], "charEnd": r["char_end"],
            "refs": _loads(r["refs_json"], []),
        }
        for r in rows
    ]


# ===========================================================================
# 数据层：接受 / 拒绝 / 编辑 / 撤销 / 重做（REQ-CON-007）
# ===========================================================================

def decide_risk(risk_id: str, decision: str, edited_text: str | None = None) -> dict[str, Any]:
    """对风险项做接受 / 拒绝 / 编辑决策，并记录操作历史。

    - accept：接受建议修改（写入工作副本，不动原件 REQ-CON-006）；
    - reject：拒绝该风险项；
    - edit：用户改写替换文本后接受。
    """
    if decision not in ("accept", "reject", "edit"):
        raise ContractError("不支持的决策类型")
    if decision == "edit" and not (edited_text or "").strip():
        raise ContractError("编辑决策必须提供修改后的文本")

    db = get_db()
    row = db.query_one("SELECT * FROM contract_risk_items WHERE id = ?", (risk_id,))
    if row is None:
        raise ContractError("风险项不存在")
    review_id = row["review_id"]
    _require_writable_review(review_id)

    after = {"accept": "accepted", "reject": "rejected", "edit": "edited"}[decision]
    before_text = row["edited_text"]
    after_text = edited_text.strip() if decision == "edit" else None
    ts = now_iso()

    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE contract_risk_items
            SET decision = ?, edited_text = ?, decided_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (after, after_text, ts, ts, risk_id),
        )
        conn.execute(
            """
            INSERT INTO contract_actions(
              id, review_id, risk_item_id, action, before_decision, after_decision,
              before_text, after_text, undone, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (str(uuid.uuid4()), review_id, risk_id, decision,
             row["decision"], after, before_text, after_text, ts),
        )
        conn.execute("UPDATE contract_reviews SET updated_at = ? WHERE id = ?", (ts, review_id))

    _rebuild_work_copy(review_id)
    return get_review(review_id)


def _latest_positive_action(review_id: str) -> dict[str, Any] | None:
    """最近一条未撤销的正向操作（accept/reject/edit）。"""
    return get_db().query_one(
        """
        SELECT * FROM contract_actions
        WHERE review_id = ? AND action IN ('accept','reject','edit') AND undone = 0
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (review_id,),
    )


def undo(review_id: str) -> dict[str, Any]:
    """撤销最近一次接受/拒绝/编辑（REQ-CON-007：操作可撤销）。"""
    _require_writable_review(review_id)
    db = get_db()
    act = _latest_positive_action(review_id)
    if act is None:
        raise ContractError("没有可撤销的操作")

    ts = now_iso()
    with db.transaction() as conn:
        conn.execute("UPDATE contract_actions SET undone = 1 WHERE id = ?", (act["id"],))
        conn.execute(
            """
            UPDATE contract_risk_items
            SET decision = ?, edited_text = ?, decided_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (act["before_decision"], act["before_text"], ts, ts, act["risk_item_id"]),
        )
        conn.execute("UPDATE contract_reviews SET updated_at = ? WHERE id = ?", (ts, review_id))

    _rebuild_work_copy(review_id)
    return get_review(review_id)


def redo(review_id: str) -> dict[str, Any]:
    """重做最近一次被撤销的操作（仅当撤销后未插入新正向操作时允许）。"""
    _require_writable_review(review_id)
    db = get_db()
    act = db.query_one(
        """
        SELECT * FROM contract_actions
        WHERE review_id = ? AND action IN ('accept','reject','edit') AND undone = 1
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (review_id,),
    )
    if act is None:
        raise ContractError("没有可重做的操作")
    newer = db.query_one(
        """
        SELECT 1 FROM contract_actions
        WHERE review_id = ? AND action IN ('accept','reject','edit')
          AND undone = 0 AND created_at > ?
        LIMIT 1
        """,
        (review_id, act["created_at"]),
    )
    if newer is not None:
        raise ContractError("撤销后已有新的操作，无法重做")

    ts = now_iso()
    with db.transaction() as conn:
        conn.execute("UPDATE contract_actions SET undone = 0 WHERE id = ?", (act["id"],))
        conn.execute(
            """
            UPDATE contract_risk_items
            SET decision = ?, edited_text = ?, decided_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (act["after_decision"], act["after_text"], ts, ts, act["risk_item_id"]),
        )
        conn.execute("UPDATE contract_reviews SET updated_at = ? WHERE id = ?", (ts, review_id))

    _rebuild_work_copy(review_id)
    return get_review(review_id)


# ===========================================================================
# 工作副本版本（REQ-CON-006：接受修改只写工作副本，原件只读不变）
# ===========================================================================

def _store_blob_bytes(root: Path, data: bytes, sha: str) -> str:
    """工作副本加密写入内容存储（可写，与原件只读属性区分）。"""
    dst = root / BLOB_DIRNAME / sha[:2] / sha
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(crypto.encrypt_bytes(data, crypto.derive_file_key(get_active_dek())))
        os.replace(tmp, dst)
    return f"{BLOB_DIRNAME}/{sha[:2]}/{sha}"


def _primary_text(review: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    from . import documents as docs

    pages = docs.get_pages(review["primaryDocumentId"])
    return join_pages(pages), pages


def _apply_replacements(full_text: str, clauses: list[dict[str, Any]],
                        risks: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """将已接受/已编辑风险的替换文本应用到条款文本，返回新全文与跳过项原因。"""
    by_clause: dict[str | None, list[dict[str, Any]]] = {}
    for r in risks:
        if r["decision"] not in ("accepted", "edited"):
            continue
        by_clause.setdefault(r["clauseId"], []).append(r)

    skipped: list[str] = []
    pieces: list[str] = []
    cursor = 0
    for c in sorted(clauses, key=lambda x: x["charStart"]):
        if c["charStart"] < cursor:
            continue
        pieces.append(full_text[cursor:c["charStart"]])
        text = c["text"]
        for r in by_clause.get(c["id"], []):
            replacement = r["editedText"] if r["decision"] == "edited" else r["replacementText"]
            original = r["originalText"]
            if replacement and original and original in text:
                text = text.replace(original, replacement, 1)
            else:
                # 定位不到原文（模型给的位置不准）或无替换文本：跳过，不误伤全文
                skipped.append(r["id"])
        pieces.append(text)
        cursor = c["charEnd"]
    pieces.append(full_text[cursor:])
    return "".join(pieces), skipped


def _rebuild_work_copy(review_id: str) -> dict[str, Any] | None:
    """按当前决策状态重建工作副本版本；无已应用修改时删除现有副本记录。"""
    db = get_db()
    review = get_review(review_id)
    risks = list_risk_items(review_id)
    clauses = list_clauses(review_id)
    applied = [r for r in risks if r["decision"] in ("accepted", "edited")
               and (r["replacementText"] or r["editedText"])]
    if not applied:
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM contract_work_copies WHERE review_id = ?", (review_id,)
            )
        return None
    if not clauses:
        return None

    full_text, _pages = _primary_text(review)
    new_text, _skipped = _apply_replacements(full_text, clauses, risks)
    if new_text == full_text:
        return None

    # 与当前最新副本完全一致则跳过（避免 undo/redo 往返产生重复版本）
    latest = db.query_one(
        """
        SELECT w.*, v.sha256 FROM contract_work_copies w
        JOIN document_versions v ON v.id = w.document_version_id
        WHERE w.review_id = ? ORDER BY w.version_no DESC LIMIT 1
        """,
        (review_id,),
    )
    sha = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
    if latest is not None and latest["sha256"] == sha:
        return None

    from . import documents as _docs

    root = _docs._workspace_root()
    blob_uri = _store_blob_bytes(root, new_text.encode("utf-8"), sha)

    version_id = str(uuid.uuid4())
    ts = now_iso()
    version_no = (latest["version_no"] + 1) if latest else 1
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO document_versions(
              id, document_id, parent_id, blob_uri, sha256, size_bytes,
              parse_status, corpus_version, origin, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'ready', 1, 'work_copy', ?)
            """,
            (version_id, review["primaryDocumentId"], review["primaryVersionId"],
             blob_uri, sha, len(new_text.encode("utf-8")), ts),
        )
        conn.execute(
            """
            INSERT INTO document_pages(id, version_id, page_no, text_content, text_quality, ocr_status)
            VALUES (?, ?, 1, ?, 'native', 'none')
            """,
            (str(uuid.uuid4()), version_id, new_text),
        )
        conn.execute(
            """
            INSERT INTO contract_work_copies(
              id, review_id, version_no, document_version_id, base_version_id,
              applied_risk_ids_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), review_id, version_no, version_id,
             review["primaryVersionId"],
             _json([r["id"] for r in applied]), ts),
        )
    return get_work_copy(review_id)


def get_work_copy(review_id: str) -> dict[str, Any] | None:
    """最新工作副本版本信息（无已应用修改时为 None）。"""
    row = get_db().query_one(
        """
        SELECT w.*, v.sha256, v.size_bytes, v.created_at AS version_created_at
        FROM contract_work_copies w
        JOIN document_versions v ON v.id = w.document_version_id
        WHERE w.review_id = ? ORDER BY w.version_no DESC LIMIT 1
        """,
        (review_id,),
    )
    if row is None:
        return None
    return {
        "reviewId": review_id,
        "versionNo": row["version_no"],
        "documentVersionId": row["document_version_id"],
        "baseVersionId": row["base_version_id"],
        "sha256": row["sha256"],
        "sizeBytes": row["size_bytes"],
        "appliedRiskIds": _loads(row["applied_risk_ids_json"], []),
        "createdAt": row["created_at"],
    }


def list_work_copies(review_id: str) -> list[dict[str, Any]]:
    """工作副本版本链（REQ-CON-002 版本比较；v1 起按应用顺序追加）。"""
    rows = get_db().query(
        """
        SELECT w.*, v.sha256, v.size_bytes, v.created_at AS version_created_at
        FROM contract_work_copies w
        JOIN document_versions v ON v.id = w.document_version_id
        WHERE w.review_id = ? ORDER BY w.version_no DESC
        """,
        (review_id,),
    )
    return [
        {
            "reviewId": review_id,
            "versionNo": r["version_no"],
            "documentVersionId": r["document_version_id"],
            "baseVersionId": r["base_version_id"],
            "sha256": r["sha256"],
            "sizeBytes": r["size_bytes"],
            "appliedRiskIds": _loads(r["applied_risk_ids_json"], []),
            "createdAt": r["version_created_at"],
        }
        for r in rows
    ]


def work_copy_text(review_id: str, version_no: int) -> tuple[str, str]:
    """工作副本版本全文；version_no=0 表示原件基准（只读）。返回 (文本, 展示标签)。

    工作副本文本存在 document_pages（origin='work_copy' 的独立版本），原件版本不受影响。
    """
    review = get_review(review_id)
    if version_no == 0:
        full_text, _pages = _primary_text(review)
        return full_text, "原件"
    row = get_db().query_one(
        "SELECT document_version_id FROM contract_work_copies WHERE review_id = ? AND version_no = ?",
        (review_id, version_no),
    )
    if row is None:
        raise ContractError(f"工作副本 v{version_no} 不存在")
    pages = get_db().query(
        "SELECT text_content FROM document_pages WHERE version_id = ? ORDER BY page_no",
        (row["document_version_id"],),
    )
    return "\n".join(p["text_content"] for p in pages), f"工作副本 v{version_no}"


def diff_work_copies(
    review_id: str, from_version_no: int = 0, to_version_no: int | None = None
) -> dict[str, Any]:
    """工作副本版本比较（REQ-CON-002 / DEV-MOD-009）：默认「原件 → 最新工作副本」。"""
    from . import editor

    left, left_label = work_copy_text(review_id, from_version_no)
    if to_version_no is None:
        latest = get_work_copy(review_id)
        if latest is None:
            raise ContractError("尚无工作副本版本可比较")
        to_version_no = latest["versionNo"]
    right, right_label = work_copy_text(review_id, to_version_no)
    return {**editor.diff_texts(left, right), "fromLabel": left_label, "toLabel": right_label}


# ===========================================================================
# 汇总输出（Agent verify 落库 / RPC 查询）
# ===========================================================================

def build_review_payload(review_id: str) -> dict[str, Any]:
    """审查会话完整载荷：会话 + 风险项 + 工作副本（RPC contract.getReview 用）。"""
    review = get_review(review_id)
    risks = list_risk_items(review_id)
    definitions: list[dict[str, str]] = []
    for c in list_clauses(review_id):
        definitions.extend(extract_definitions(c["text"]))
    return {
        "review": review,
        "risks": risks,
        "clauses": list_clauses(review_id),
        "definitions": definitions,
        "workCopy": get_work_copy(review_id),
    }


# ===========================================================================
# 内部工具
# ===========================================================================

def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def _loads(s: str | None, default: Any) -> Any:
    import json

    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default
