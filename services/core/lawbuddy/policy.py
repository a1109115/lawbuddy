"""权限引擎（DEV-AGT-004，技术方案 §7 / PRD 12.5）。

- 策略只返回 ALLOW / DENY / REQUIRE_APPROVAL（确定性规则，模型不能自我批准）；
- 严格本地：一切外发能力 DENY；受控外部：外发需审批（已有授权范围内可放行）；
- 审批卡字段：用途/工具/目标/数据摘要/授权期限/拒绝后降级（PRD 12.5）；
- 参数哈希：批准时固化 payload_hash，执行前重算比对，不一致即失效（S-004）；
- 授权范围：once（单次，绑定哈希）/ task（当前任务）/ case（名义案件级，V32 起实际
  绑定审批时的会话 conversation_id——重开对话窗口即失效，需重新审批）；支持撤权。
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .storage import get_db
from .workspace import now_iso

CAP_NETWORK_MODEL = "NETWORK_MODEL"
CAP_RAW_DOCUMENT_EGRESS = "RAW_DOCUMENT_EGRESS"  # 本迭代不启用，策略默认拒绝
CAP_SHELL_EXEC = "SHELL_EXEC"  # Agent 执行 Shell 命令（用户开启的可执行通道，逐次强审批）

_APPROVAL_TTL_MINUTES = 10  # 审批卡有效期（超时进入 expired → 任务降级）
_WAIT_POLL_S = 1.0
_WAIT_TIMEOUT_S = 600  # 等待用户决定的上限（超时视为拒绝并降级）


class PolicyError(RuntimeError):
    pass


def payload_digest(text: str) -> str:
    """审批参数摘要（S-004）：批准时固化，执行前重算比对，不一致即失效。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def decide(capability: str, permission_preset: str) -> str:
    """确定性策略判定：allow / deny / require_approval。"""
    if permission_preset == "strict_local":
        return "deny" if capability in (CAP_NETWORK_MODEL, CAP_RAW_DOCUMENT_EGRESS) else "allow"
    # controlled_external / custom：外发需审批
    if capability == CAP_NETWORK_MODEL:
        return "require_approval"
    if capability == CAP_RAW_DOCUMENT_EGRESS:
        return "require_approval"  # 即使受控模式，整卷外发也必须逐次强审批
    return "allow"


def has_grant(capability: str, task_id: str, scope_id: str, payload_hash: str | None = None) -> bool:
    """已有有效授权？（once 需匹配 payload_hash；task/case 范围内放行）

    scope_id：授权作用域 id——调用方传当前任务的会话 conversation_id
    （无会话时传 case_id），与 grant 写入时的作用域一致才匹配。
    """
    db = get_db()
    ts = now_iso()
    rows = db.query(
        """
        SELECT scope_type, payload_hash FROM permission_grants
        WHERE capability = ? AND (expires_at IS NULL OR expires_at > ?)
        """,
        (capability, ts),
    )
    for r in rows:
        if r["scope_type"] == "once":
            if payload_hash and r["payload_hash"] == payload_hash:
                return True
        elif r["scope_type"] == "task" and r["payload_hash"] == task_id:
            return True
        elif r["scope_type"] == "case" and r["payload_hash"] == scope_id:
            return True
    return False


def create_approval(
    task_id: str,
    case_id: str,
    capability: str,
    target: str,
    payload_hash: str,
    data_summary: str,
    fallback: str = "拒绝后将降级为本地检索分析（不发起外部请求）",
    scope_id: str | None = None,
) -> dict[str, Any]:
    """创建审批请求（任务进入 WAITING_APPROVAL，事件推送审批卡内容）。

    scope_id：授权作用域（V32 会话隔离）——"案件内允许"的长效授权绑定到
    该 id（调用方传会话 conversation_id；缺省回退 case_id）。重开新对话
    窗口产生新会话 id，旧授权不再匹配，需重新审批。
    """
    db = get_db()
    approval_id = str(uuid.uuid4())
    effective_scope = str(scope_id or "").strip() or case_id
    expires = (datetime.now(timezone.utc) + timedelta(minutes=_APPROVAL_TTL_MINUTES)).isoformat()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO approval_requests(id, task_id, case_id, capability, target, payload_hash,
                                          data_summary, status, expires_at, created_at, scope_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (approval_id, task_id, case_id, capability, target, payload_hash, data_summary, expires, now_iso(), effective_scope),
        )
        from . import audit

        audit.append(
            conn, action="approval.request", resource_type="approval", resource_id=approval_id, case_id=case_id,
            metadata={"taskId": task_id, "capability": capability, "target": target,
                      "payloadHash": payload_hash[:16], "dataSummaryLen": len(data_summary)},
        )
    from .agent import tasks as task_store

    task_store.update_lifecycle(task_id, "WAITING_APPROVAL", "approval")
    task_store.append_event(task_id, "approval_request", {
        "approvalId": approval_id,
        "capability": capability,
        "target": target,
        "dataSummary": data_summary,
        "payloadHash": payload_hash[:16],
        "expiresInMinutes": _APPROVAL_TTL_MINUTES,
        "fallback": fallback,
    })
    return {"approvalId": approval_id, "expiresAt": expires}


def wait_for_decision(approval_id: str, task_id: str, cancel_check: Any = None) -> str:
    """后台线程内等待用户决定：approved / denied / expired（超时按拒绝处理）。

    cancel_check：任务取消轮询（可选）。任务被取消时审批立即作废并返回 cancelled，
    避免用户停止任务后 worker 仍阻塞等待到超时。
    """
    db = get_db()
    deadline = threading.Event()
    timer = threading.Timer(_WAIT_TIMEOUT_S, deadline.set)
    timer.daemon = True
    timer.start()
    try:
        while not deadline.is_set():
            if cancel_check is not None and cancel_check():
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE approval_requests SET status = 'cancelled', decided_at = ? WHERE id = ? AND status = 'pending'",
                        (now_iso(), approval_id),
                    )
                return "cancelled"
            row = db.query_one("SELECT status FROM approval_requests WHERE id = ?", (approval_id,))
            if row is None or row["status"] != "pending":
                return row["status"] if row else "expired"
            deadline.wait(_WAIT_POLL_S)
        # 等待超时：标记 expired
        with db.transaction() as conn:
            conn.execute(
                "UPDATE approval_requests SET status = 'expired', decided_at = ? WHERE id = ? AND status = 'pending'",
                (now_iso(), approval_id),
            )
        from .agent import tasks as task_store

        task_store.append_event(task_id, "model_call", {
            "status": "denied", "reason": "审批超时未响应，已降级本地分析",
        })
        return "expired"
    finally:
        timer.cancel()


def respond(approval_id: str, decision: str, approved_payload_hash: str | None = None) -> dict[str, Any]:
    """用户审批决定（RPC）：approved → 写授权；denied → 仅记录。参数哈希不一致即失效（S-004）。"""
    if decision not in ("allow_once", "allow_task", "allow_case", "deny"):
        raise PolicyError(f"无效审批决定：{decision}")
    db = get_db()
    row = db.query_one(
        "SELECT task_id, case_id, capability, payload_hash, status, scope_id FROM approval_requests WHERE id = ?",
        (approval_id,),
    )
    if row is None:
        raise PolicyError("审批请求不存在")
    if row["status"] != "pending":
        raise PolicyError(f"审批请求已处理（{row['status']}）")
    # 参数哈希校验：审批时的哈希与执行 payload 必须一致（S-004）
    if approved_payload_hash and approved_payload_hash != row["payload_hash"]:
        raise PolicyError("审批内容与实际参数不一致（payload hash 校验失败）")

    # 授权作用域（V32 会话隔离）：优先审批卡固化的 scope_id（会话），
    # 旧数据/无会话回退 case_id
    grant_scope = row["scope_id"] if "scope_id" in row.keys() and row["scope_id"] else row["case_id"]

    final_status = "denied" if decision == "deny" else "approved"
    with db.transaction() as conn:
        conn.execute(
            "UPDATE approval_requests SET status = ?, decision = ?, decided_at = ? WHERE id = ?",
            (final_status, decision, now_iso(), approval_id),
        )
        grant_id: str | None = None
        if decision != "deny":
            if decision == "allow_once":
                # 单次授权不跨请求复用：仅本次审批对应的调用有效（PRD 12.5 高风险能力只支持本次授权）。
                # 执行前哈希一致性由调用方在同流程内保证（S-004 防参数替换）。
                pass
            else:
                grant_id = str(uuid.uuid4())
                if decision == "allow_task":
                    scope_type, scope_id, payload = "task", row["task_id"], row["task_id"]
                else:
                    # 「案件内允许」实际绑定到会话（重开对话窗口需重新审批），无会话回退案件
                    scope_type, scope_id, payload = "case", grant_scope, grant_scope
                conn.execute(
                    "INSERT INTO permission_grants(id, scope_type, scope_id, capability, payload_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (grant_id, scope_type, scope_id, row["capability"], payload, now_iso()),
                )
        from . import audit

        audit.append(
            conn, action=f"approval.{final_status}", resource_type="approval", resource_id=approval_id,
            case_id=row["case_id"],
            metadata={"decision": decision, "capability": row["capability"], "grantId": grant_id},
        )
    # 审批结论写入任务事件：窗口重开后 UI 可据此识别“该审批已有结论”，
    # 不再把历史 approval_request 当成待处理审批重复弹出。
    from .agent import tasks as task_store

    task_store.append_event(row["task_id"], "approval_decision", {
        "approvalId": approval_id,
        "status": final_status,
        "decision": decision,
    })
    return {"approvalId": approval_id, "status": final_status}


def revoke_grants(capability: str, scope_id: str) -> int:
    """撤权：删除范围内授权（REQ-PERM-004）。返回撤销条数。"""
    db = get_db()
    with db.transaction() as conn:
        cur = conn.execute(
            "DELETE FROM permission_grants WHERE capability = ? AND scope_id = ?",
            (capability, scope_id),
        )
        count = cur.rowcount or 0
        if count:
            from . import audit

            audit.append(
                conn, action="permission.revoke", resource_type="permission", resource_id=scope_id,
                metadata={"capability": capability, "revoked": count},
            )
    return count
