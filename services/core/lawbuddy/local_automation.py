"""受限本地自动化（LB-HERMES-2026-01 §3 / M8）。

只做三件事，且都**不产生外发与越权副作用**：

1. 本地引用重验：按当前原文判断哪些过期 Claim 可以恢复（只判断，不改状态）；
2. 失效检查：找出源文档已失效但仍被当作有效事实的记忆与提案；
3. 提醒：把待人工核实项汇总给 UI，由用户决定是否处理。

硬约束（与方案 §1.1 不变量一致）：

- 不自动新建任务、不自动调用模型、不自动外发任何内容；
- 不自动批准学习提案、不自动修改技能或记忆；
- 不自动重放 unknown 工具调用；
- 不注册后台定时器（避免"关闭后仍留后台任务"）；
- 每次运行都写审计，但只记计数与 reason code，不记原文。

因此本模块全部动作都是**只读查询**：唯一写入是审计事件 `automation.local_run`。
真正改变状态（`claim.recheck`、`memory` 更正、提案批准）仍必须由用户在界面触发。
调度由用户在应用内显式开启（`agent.localAutomation`），默认关闭。
"""

from __future__ import annotations

from typing import Any

from .storage import get_db
from .workspace import now_iso

# 允许的自动化动作：全部只读，不触碰外部系统
ALLOWED_ACTIONS = ("reverify_claims", "check_stale_documents", "report_pending_human")

# 明确禁止的动作（保留清单用于门禁断言与审计说明）
FORBIDDEN_ACTIONS = (
    "send_message", "http_request", "write_file", "delete_file",
    "approve_learning", "apply_skill", "replay_unknown_tool", "create_task",
)

SETTING_KEY = "agent.localAutomation"

# 视为"源文档已不可信"的文档状态：这些状态下引用不能继续作为已支持事实
UNTRUSTED_DOC_STATUS = ("stale", "partial", "failed", "locked")


class AutomationError(RuntimeError):
    pass


def enabled() -> bool:
    """读设置；默认关闭，任何异常都按关闭处理（fail-closed）。"""
    try:
        from .agent.tasks import get_setting

        value = get_setting(SETTING_KEY)
    except Exception:  # noqa: BLE001
        return False
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _require_enabled() -> None:
    if not enabled():
        raise AutomationError(
            "本地自动化已关闭：请先在设置中开启 agent.localAutomation"
        )


def run(case_id: str, actions: list[str] | None = None) -> dict[str, Any]:
    """执行一次受限本地检查（只读）。返回计数与 reason code，不含原文。"""
    _require_enabled()
    from . import audit
    from .cases import get_case

    case = get_case(case_id)
    chosen = list(actions or ALLOWED_ACTIONS)
    unknown = [a for a in chosen if a not in ALLOWED_ACTIONS]
    if unknown:
        raise AutomationError(
            f"不允许的自动化动作：{', '.join(unknown)}"
            f"（仅允许 {', '.join(ALLOWED_ACTIONS)}）"
        )

    result: dict[str, Any] = {
        "caseId": case["id"],
        "ranAt": now_iso(),
        "actions": [],
        "staleClaims": 0,
        "recoverableClaims": 0,
        "staleMemories": 0,
        "memoriesAtRisk": 0,
        "staleProposals": 0,
        "needsHuman": 0,
        "note": "全部动作只读；任何状态变更仍需用户在界面确认",
    }

    if "reverify_claims" in chosen:
        result["actions"].append(_reverify_claims(case["id"], result))
    if "check_stale_documents" in chosen:
        result["actions"].append(_check_stale_documents(case["id"], result))
    if "report_pending_human" in chosen:
        result["actions"].append(_report_pending_human(case["id"], result))

    db = get_db()
    with db.transaction() as conn:
        audit.append(
            conn,
            action="automation.local_run",
            resource_type="case",
            resource_id=case["id"],
            case_id=case["id"],
            metadata={
                "actions": chosen,
                # 只记计数与状态，不写案件原文、引用片段或查询词
                "staleClaims": result["staleClaims"],
                "recoverableClaims": result["recoverableClaims"],
                "staleMemories": result["staleMemories"],
                "memoriesAtRisk": result["memoriesAtRisk"],
                "staleProposals": result["staleProposals"],
                "needsHuman": result["needsHuman"],
            },
        )
    return result


def _quote_still_valid(claim_id: str) -> bool:
    """只读判断：Claim 的首个来源引语今天是否仍在原文该页（与 recheck 同规则）。"""
    from .citations import LOW_OCR_CONFIDENCE, verify_quote

    ref = get_db().query_one(
        """
        SELECT sr.document_version_id, sr.page_no, sr.quote_norm, sr.ocr_confidence
        FROM source_refs sr
        JOIN claim_sources cs ON cs.source_ref_id = sr.id
        WHERE cs.claim_id = ? ORDER BY sr.created_at ASC LIMIT 1
        """,
        (claim_id,),
    )
    if ref is None or not ref["quote_norm"]:
        return False
    if ref["ocr_confidence"] is not None and ref["ocr_confidence"] < LOW_OCR_CONFIDENCE:
        return False
    return verify_quote(ref["quote_norm"], ref["document_version_id"], ref["page_no"])


def _reverify_claims(case_id: str, acc: dict[str, Any]) -> dict[str, Any]:
    """对已过期 Claim 只读重验：统计仍不可信的、以及可恢复的候选。

    **不改写任何 Claim 状态**：可恢复项只是候选，用户点击"重新核验"才会写库。
    """
    rows = get_db().query(
        "SELECT id, status, reason_code FROM claims "
        "WHERE case_id = ? AND status IN ('stale', 'needs_human', 'conflict')",
        (case_id,),
    )
    reasons: dict[str, int] = {}
    recoverable = 0
    for r in rows:
        code = str(r["reason_code"] or "unknown")
        reasons[code] = reasons.get(code, 0) + 1
        if r["status"] == "stale" and _quote_still_valid(str(r["id"])):
            recoverable += 1
    acc["staleClaims"] = len(rows)
    acc["recoverableClaims"] = recoverable
    return {
        "action": "reverify_claims",
        "count": len(rows),
        "recoverable": recoverable,
        "reasonCodes": reasons,
        "note": "可恢复项需用户在界面执行重新核验后才会变更状态",
    }


def _check_stale_documents(case_id: str, acc: dict[str, Any]) -> dict[str, Any]:
    """找出源文档已失效（换版/解析失败/低质量）但仍被当作有效事实的记忆。"""
    db = get_db()
    docs = db.query(
        f"SELECT id, status FROM documents WHERE case_id = ? AND status IN "
        f"({','.join('?' * len(UNTRUSTED_DOC_STATUS))})",
        (case_id, *UNTRUSTED_DOC_STATUS),
    )
    doc_ids = [str(d["id"]) for d in docs]
    at_risk = 0
    if doc_ids:
        marks = ",".join("?" * len(doc_ids))
        row = db.query_one(
            f"SELECT COUNT(*) AS c FROM memories WHERE source_document_id IN ({marks}) "
            "AND status = 'active'",
            tuple(doc_ids),
        )
        at_risk = int((row or {}).get("c") or 0)
    # memories 状态只有 active/stale/superseded/deleted（无 needs_human）
    stale_mem = db.query_one(
        "SELECT COUNT(*) AS c FROM memories WHERE case_id = ? AND status = 'stale'",
        (case_id,),
    )
    stale_props = db.query_one(
        "SELECT COUNT(*) AS c FROM learning_proposals WHERE case_id = ? AND status = 'stale'",
        (case_id,),
    )
    acc["memoriesAtRisk"] = at_risk
    acc["staleMemories"] = int((stale_mem or {}).get("c") or 0)
    acc["staleProposals"] = int((stale_props or {}).get("c") or 0)
    return {
        "action": "check_stale_documents",
        "untrustedDocuments": len(doc_ids),
        "memoriesAtRisk": at_risk,
        "staleMemories": acc["staleMemories"],
        "staleProposals": acc["staleProposals"],
        "note": "只提示，不自动把记忆或提案标记失效",
    }


def _report_pending_human(case_id: str, acc: dict[str, Any]) -> dict[str, Any]:
    """汇总待人工核实项（只提示，不自动处理）。"""
    db = get_db()
    claims = db.query_one(
        "SELECT COUNT(*) AS c FROM claims WHERE case_id = ? AND status = 'needs_human'",
        (case_id,),
    )
    # 记忆没有 needs_human 状态：用 stale 表达"待人工核实是否仍可作为事实"
    memories = db.query_one(
        "SELECT COUNT(*) AS c FROM memories WHERE case_id = ? AND status = 'stale'",
        (case_id,),
    )
    claim_count = int((claims or {}).get("c") or 0)
    memory_count = int((memories or {}).get("c") or 0)
    acc["needsHuman"] = claim_count + memory_count
    return {
        "action": "report_pending_human",
        "claims": claim_count,
        "memories": memory_count,
        "note": "仅提示：处理方式由用户在界面中确认",
    }


def capability_report() -> dict[str, Any]:
    """自动化能力边界（UI 与门禁共用，避免两处口径漂移）。"""
    return {
        "enabled": enabled(),
        "settingKey": SETTING_KEY,
        "allowedActions": list(ALLOWED_ACTIONS),
        "forbiddenActions": list(FORBIDDEN_ACTIONS),
        "sendsExternalContent": False,
        "schedulesBackgroundJobs": False,
        "requiresUserActionForWrites": True,
        "writesOnlyAudit": True,
    }
