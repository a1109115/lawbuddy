"""受控持续学习：提案—复审—批准（LB-HERMES-2026-01 WP08）。

跨会话学习必须经过**人工/规则门禁**，不能由后台任务直接改动已启用的记忆。
本模块只做一件事：把「想改什么」写成可审计的提案，并在批准时才落地。

硬约束（与方案 §WP08 逐条对应）：

1. **未批准不进上下文**：`pending/rejected/stale` 提案不得出现在 Prompt、
   召回或正式记忆中。本模块的 `approved_only` 是唯一对外暴露的读取入口。
2. **无来源案件事实提案被存储层拒绝**：`propose_case_memory` 会调用
   `memory._validate_case_source`，并通过 `citations.verify_quote` 重新校验引文；
   校验不过直接抛错，不写入提案。
3. **文档换版后进入 stale**：`invalidate_by_document` 把受影响提案标 `stale`，
   同时把已批准并落地的记忆交给 `memory.invalidate_by_document` 处理。
4. **baseHash 冲突不覆盖新值**：批准时比对当前基线指纹，不一致则拒绝并要求重审。
5. **拒绝/回滚无残留**：拒绝只改状态；回滚用 `memory.correct` 写回旧值，
   并清理已写入的提案副作用（不删除审计）。

用户画像只允许语言/格式/模型/权限偏好（复用 `memory.USER_KINDS`），
案件事实一律不得进入 `user` 层（M-001）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .. import memory
from ..storage import get_db
from ..workspace import now_iso

PROPOSAL_KINDS = ("case_memory", "user_profile", "skill_update", "dedupe", "archive")
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
STALE = "stale"
SUPERSEDED = "superseded"
OPEN_STATUSES = (PENDING,)


class LearningError(RuntimeError):
    pass


class LearningDisabled(LearningError):
    """`agent.learningProposals` 关闭：后台提案入口整体停用（WP08 §10.1）。

    关闭时只禁止**生成**提案；已存在的提案仍可查询/拒绝/回滚，
    已批准的提案与用户主动记忆不受影响（关闭功能不得丢审计或改历史）。
    """


def proposals_enabled() -> bool:
    """后台学习提案开关（默认关闭；方案要求关闭后不产生新提案）。"""
    from .tasks import get_setting

    try:
        value = get_setting("agent.learningProposals")
    except Exception:  # noqa: BLE001 设置不可读时按关闭处理（保守）
        return False
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _require_enabled() -> None:
    if not proposals_enabled():
        raise LearningDisabled(
            "后台学习提案已关闭：请在设置中开启 agent.learningProposals 后再生成提案"
        )


class BaseHashConflict(LearningError):
    """提案基线已变化：必须重新审查，禁止用旧提案覆盖新值。"""


class ProposalNotFound(LearningError):
    pass


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()


def _row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "caseId": row["case_id"],
        "taskId": row["task_id"],
        "proposalKind": row["proposal_kind"],
        "targetKey": row["target_key"],
        "baseHash": row["base_hash"],
        "status": row["status"],
        "diff": json.loads(row["diff_json"] or "{}"),
        "source": json.loads(row["source_json"] or "{}"),
        "rationale": row["rationale"],
        "impact": json.loads(row["impact_json"] or "{}"),
        "scan": json.loads(row["scan_json"] or "{}"),
        "reviewerNote": row["reviewer_note"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# 基线指纹
# ---------------------------------------------------------------------------

def memory_baseline_hash(case_id: str | None, key: str) -> str:
    """某记忆键当前值的指纹；不存在时为空串（表示"新增"）。"""
    if not case_id:
        return ""
    row = get_db().query_one(
        "SELECT id, value_text, status FROM memories WHERE case_id = ? AND key_text = ? "
        "AND status = 'active' ORDER BY updated_at DESC LIMIT 1",
        (case_id, key),
    )
    if row is None:
        return ""
    return _hash({"id": row["id"], "value": row["value_text"]})


def skill_baseline_hash(skill_id: str | None) -> str:
    """已启用技能当前版本的指纹（设备级库）。"""
    if not skill_id:
        return ""
    from ..storage import get_profile_db

    row = get_profile_db().query_one(
        "SELECT content_sha256 FROM skill_versions WHERE skill_id = ? AND active = 1 "
        "ORDER BY version_no DESC LIMIT 1",
        (skill_id,),
    )
    if row is not None:
        return str(row["content_sha256"])
    legacy = get_profile_db().query_one(
        "SELECT content_sha256 FROM skills WHERE id = ?", (skill_id,)
    )
    return str(legacy["content_sha256"]) if legacy else ""


# ---------------------------------------------------------------------------
# 提案创建
# ---------------------------------------------------------------------------

def _insert(
    *, case_id: str | None, task_id: str | None, kind: str, target_key: str,
    base_hash: str, diff: dict[str, Any], source: dict[str, Any], rationale: str,
    impact: dict[str, Any] | None = None, scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_enabled()
    proposal_id = str(uuid.uuid4())
    ts = now_iso()
    with get_db().transaction() as conn:
        conn.execute(
            """
            INSERT INTO learning_proposals(
                id, case_id, task_id, proposal_kind, target_key, base_hash, status,
                diff_json, source_json, rationale, impact_json, scan_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (proposal_id, case_id, task_id, kind, target_key, base_hash,
             _dumps(diff), _dumps(source), rationale, _dumps(impact or {}),
             _dumps(scan or {}), ts, ts),
        )
        from .. import audit

        audit.append(
            conn, action="learning.propose", resource_type="learning_proposal",
            resource_id=proposal_id, case_id=case_id,
            metadata={"kind": kind, "targetKey": target_key[:120],
                      "hasSource": bool(source.get("documentId"))},
        )
    row = get_db().query_one("SELECT * FROM learning_proposals WHERE id = ?", (proposal_id,))
    if row is None:  # pragma: no cover
        raise LearningError("提案写入后不可见")
    return _row(row)


def propose_case_memory(
    case_id: str, key: str, value: str, *, kind: str = "fact",
    document_id: str, page_no: int, quote: str, version_id: str | None = None,
    task_id: str | None = None, rationale: str = "",
) -> dict[str, Any]:
    """为案件事实生成记忆提案（存储层强制来源校验，无来源直接拒绝）。"""
    key = str(key or "").strip()
    value = str(value or "").strip()
    if not key or not value:
        raise LearningError("提案需要非空的记忆键与内容")
    if kind not in memory.FACT_KINDS:
        # 非事实类不应走"案件事实提案"入口，避免绕过来源门禁的语义混乱
        raise LearningError(f"propose_case_memory 只接受事实类 kind（{', '.join(memory.FACT_KINDS)}）")
    source = {
        "documentId": str(document_id or "").strip(),
        "pageNo": int(page_no or 0),
        "quote": str(quote or "").strip(),
    }
    if version_id:
        source["versionId"] = str(version_id)
    try:
        # 复用 memory 的来源校验（案件归属、版本有效、页码存在、引文子串匹配）
        validated = memory._validate_case_source(case_id, source)
    except memory.MemoryError as exc:
        raise LearningError(f"提案来源校验失败，已拒绝写入：{exc}") from exc
    base = memory_baseline_hash(case_id, key)
    return _insert(
        case_id=case_id, task_id=task_id, kind="case_memory", target_key=key,
        base_hash=base,
        diff={"operation": "upsert", "value": value, "memoryKind": kind},
        source=validated, rationale=rationale or "后台复审发现新的可核验事实",
        impact={"scope": "case", "caseId": case_id},
    )


def propose_user_profile(
    key: str, value: str, *, kind: str = "preference",
    evidence_text: str = "", rationale: str = "",
) -> dict[str, Any]:
    """为用户偏好生成提案：只允许偏好类 kind，且绝不携带案件事实。"""
    if kind not in memory.USER_KINDS:
        raise LearningError(
            f"用户画像只允许 {', '.join(memory.USER_KINDS)}；拒绝写入 {kind}"
        )
    key = str(key or "").strip()
    value = str(value or "").strip()
    if not key or not value:
        raise LearningError("提案需要非空的偏好键与内容")
    # 结构性护栏：偏好内容里出现案件事实特征时直接拒绝（保守，宁可少记）
    lowered = f"{key} {value}".lower()
    if any(tok in lowered for tok in ("原告", "被告", "判决", "案号", "第", "页")):
        raise LearningError("用户偏好提案疑似包含案件事实，已拒绝（M-001）")
    row = get_db().query_one(
        "SELECT id, value_text FROM memories WHERE scope = 'user' AND key_text = ? "
        "AND status = 'active' ORDER BY updated_at DESC LIMIT 1",
        (key,),
    )
    base = _hash({"id": row["id"], "value": row["value_text"]}) if row else ""
    return _insert(
        case_id=None, task_id=None, kind="user_profile", target_key=key, base_hash=base,
        diff={"operation": "upsert", "value": value, "preferenceKind": kind},
        source={"evidence": str(evidence_text or "")[:500]},
        rationale=rationale or "用户表达的语气/格式/模型偏好",
        impact={"scope": "user"},
    )


def propose_skill_update(
    skill_id: str, name: str, prompt_md: str, *, base_version_id: str | None = None,
    rationale: str = "", scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为技能更新生成提案（技能正文仍走 skills 暂存区）。"""
    from ..storage import get_profile_db

    row = get_profile_db().query_one("SELECT id FROM skills WHERE id = ?", (skill_id,))
    if row is None:
        raise LearningError("技能不存在")
    if not str(prompt_md or "").strip():
        raise LearningError("技能提案需要非空提示词正文")
    base = skill_baseline_hash(skill_id)
    return _insert(
        case_id=None, task_id=None, kind="skill_update", target_key=f"skill:{skill_id}",
        base_hash=base,
        diff={"operation": "skill_version", "name": name, "promptMd": str(prompt_md)},
        source={"skillId": skill_id, "baseVersionId": base_version_id or ""},
        rationale=rationale or "用户要求优化技能提示词",
        impact={"scope": "device"}, scan=scan,
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def list_proposals(
    *, case_id: str | None = None, status: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    db = get_db()
    clauses: list[str] = []
    params: list[Any] = []
    if case_id:
        clauses.append("case_id = ?")
        params.append(case_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.query(
        f"SELECT * FROM learning_proposals{where} ORDER BY created_at DESC LIMIT ?",
        (*params, int(limit)),
    )
    return [_row(r) for r in rows]


def approved_only(case_id: str | None, limit: int = 20) -> list[dict[str, Any]]:
    """**唯一**可进入 Prompt/召回的提案读取入口：只返回已批准且仍新鲜的内容。

    `case_id` 为空时返回设备级（用户画像/技能）已批准提案；传入案件 ID 时
    只返回该案件的已批准提案——两者都不包含 pending / rejected / stale。
    """
    db = get_db()
    if case_id:
        rows = db.query(
            "SELECT * FROM learning_proposals WHERE case_id = ? AND status = 'approved' "
            "ORDER BY updated_at DESC LIMIT ?",
            (case_id, limit),
        )
    else:
        rows = db.query(
            "SELECT * FROM learning_proposals WHERE case_id IS NULL AND status = 'approved' "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
    return [_row(r) for r in rows]


def get_proposal(proposal_id: str) -> dict[str, Any]:
    row = get_db().query_one("SELECT * FROM learning_proposals WHERE id = ?", (proposal_id,))
    if row is None:
        raise ProposalNotFound("提案不存在")
    return _row(row)


# ---------------------------------------------------------------------------
# 批准 / 拒绝 / 回滚
# ---------------------------------------------------------------------------

def approve(proposal_id: str, *, note: str | None = None, accept_diff: dict[str, Any] | None = None) -> dict[str, Any]:
    """批准提案并落地；baseHash 冲突时拒绝并标记 stale（禁止覆盖新值）。"""
    proposal = get_proposal(proposal_id)
    if proposal["status"] != PENDING:
        raise LearningError(f"提案状态为 {proposal['status']}，不能批准")
    diff = dict(proposal["diff"] or {})
    if accept_diff:
        # 「编辑后批准」：只允许改内容字段，不允许改来源（来源必须保持可核验）
        diff.update({k: v for k, v in accept_diff.items() if k in ("value", "promptMd", "name")})
    kind = proposal["proposalKind"]
    applied: dict[str, Any] = {}

    if kind == "case_memory":
        current = memory_baseline_hash(proposal["caseId"], proposal["targetKey"])
        if current != proposal["baseHash"]:
            _mark(proposal_id, STALE, "基线已变化，需重新审查")
            raise BaseHashConflict("基线已变化：请重新生成提案后再审批")
        source = proposal["source"] or {}
        entry = memory.remember(
            scope="case", kind=str(diff.get("memoryKind") or "fact"),
            key=proposal["targetKey"], value=str(diff.get("value") or ""),
            case_id=proposal["caseId"], task_id=proposal["taskId"], source=source,
        )
        applied = {"memoryId": entry["id"]}
    elif kind == "user_profile":
        current = memory_baseline_hash(None, proposal["targetKey"])
        if current and current != proposal["baseHash"]:
            _mark(proposal_id, STALE, "基线已变化，需重新审查")
            raise BaseHashConflict("用户偏好基线已变化：请重新审批")
        entry = memory.remember(
            scope="user", kind=str(diff.get("preferenceKind") or "preference"),
            key=proposal["targetKey"], value=str(diff.get("value") or ""),
        )
        applied = {"memoryId": entry["id"]}
    elif kind == "skill_update":
        from .. import skill_lifecycle

        current = skill_baseline_hash(str((proposal["source"] or {}).get("skillId") or ""))
        if current != proposal["baseHash"]:
            _mark(proposal_id, STALE, "技能基线版本已变化，需重新审查")
            raise BaseHashConflict("技能基线已变化：请重新生成提案")
        applied = skill_lifecycle.apply_approved_skill_version(
            str((proposal["source"] or {}).get("skillId") or ""),
            name=str(diff.get("name") or ""),
            prompt_md=str(diff.get("promptMd") or ""),
            note=note,
        )
    else:
        raise LearningError(f"提案类型 {kind} 不支持自动落地，请人工处理")

    _mark(proposal_id, APPROVED, note)
    from .. import audit
    from ..storage import get_db

    with get_db().transaction() as conn:
        audit.append(
            conn, action="learning.approve", resource_type="learning_proposal",
            resource_id=proposal_id, case_id=proposal["caseId"],
            metadata={"kind": kind, "applied": bool(applied)},
        )
    return {**get_proposal(proposal_id), "applied": applied}


def reject(proposal_id: str, *, note: str | None = None) -> dict[str, Any]:
    proposal = get_proposal(proposal_id)
    if proposal["status"] not in (PENDING, STALE):
        raise LearningError(f"提案状态为 {proposal['status']}，不能拒绝")
    _mark(proposal_id, REJECTED, note)
    from .. import audit
    from ..storage import get_db

    with get_db().transaction() as conn:
        audit.append(
            conn, action="learning.reject", resource_type="learning_proposal",
            resource_id=proposal_id, case_id=proposal["caseId"], metadata={"kind": proposal["proposalKind"]},
        )
    return get_proposal(proposal_id)


def rollback(proposal_id: str, *, note: str | None = None) -> dict[str, Any]:
    """回滚已批准的案件记忆提案：恢复上一版值，提案标记 superseded。

    回滚只影响该键的值，不删除审计、不影响其他提案与其他键。
    """
    proposal = get_proposal(proposal_id)
    if proposal["status"] != APPROVED:
        raise LearningError("只有已批准的提案才能回滚")
    if proposal["proposalKind"] != "case_memory":
        raise LearningError("目前仅支持回滚案件记忆类提案")
    db = get_db()
    key = proposal["targetKey"]
    rows = db.query(
        "SELECT id FROM memories WHERE case_id = ? AND key_text = ? AND status = 'active' "
        "ORDER BY updated_at DESC",
        (proposal["caseId"], key),
    )
    if len(rows) >= 2:
        # 恢复上一条（correct 会把当前值转 superseded 并保留历史）
        previous = db.query_one("SELECT id FROM memories WHERE case_id = ? AND key_text = ? "
                                "AND status = 'superseded' ORDER BY updated_at DESC LIMIT 1",
                                (proposal["caseId"], key))
        if previous:
            memory.correct(str(rows[0]["id"]),
                           str(db.query_one("SELECT value_text FROM memories WHERE id = ?",
                                            (previous["id"],))["value_text"]),
                           note=note or "学习提案回滚")
    _mark(proposal_id, SUPERSEDED, note)
    return get_proposal(proposal_id)


def _mark(proposal_id: str, status: str, note: str | None) -> None:
    with get_db().transaction() as conn:
        conn.execute(
            "UPDATE learning_proposals SET status = ?, reviewer_note = ?, updated_at = ? "
            "WHERE id = ?",
            (status, note, now_iso(), proposal_id),
        )


# ---------------------------------------------------------------------------
# 文档换版失效
# ---------------------------------------------------------------------------

def invalidate_by_document(document_id: str, reason: str = "DOCUMENT_REPLACED") -> int:
    """文档换版/删除后，把依赖该文档的提案标 stale，并同步失效已落地记忆。"""
    db = get_db()
    stale = 0
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id, source_json FROM learning_proposals WHERE status IN ('pending','approved')"
        ).fetchall()
        for row in rows:
            source = json.loads(row["source_json"] or "{}")
            if str(source.get("documentId") or "") == str(document_id):
                conn.execute(
                    "UPDATE learning_proposals SET status = 'stale', reviewer_note = ?, "
                    "updated_at = ? WHERE id = ?",
                    (reason, now_iso(), row["id"]),
                )
                stale += 1
    # 已落地记忆同样失效（无来源不可继续作为事实召回）
    memory.invalidate_by_document(document_id, reason)
    return stale


def stats(case_id: str | None = None) -> dict[str, Any]:
    db = get_db()
    if case_id:
        rows = db.query(
            "SELECT status, COUNT(*) AS c FROM learning_proposals WHERE case_id = ? GROUP BY status",
            (case_id,),
        )
    else:
        rows = db.query("SELECT status, COUNT(*) AS c FROM learning_proposals GROUP BY status")
    return {"byStatus": {str(r["status"]): int(r["c"] or 0) for r in rows},
            "caseId": case_id}
