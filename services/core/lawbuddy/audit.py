"""审计日志（REQ-AUD-001，技术方案 §13.1 / §18）。

- 追加式哈希链：event_hash 覆盖前一条哈希与本条全部字段，普通用户无法静默改写历史；
- 只记录操作元数据（动作、资源、结果、内容哈希摘要），不写卷宗正文、密码、密钥与脱敏映射；
- append() 必须在调用方的写事务（BEGIN IMMEDIATE）内执行，保证业务与审计原子落库；
- verify() 全链重算，返回首个断点，支撑 DB-007 / A-012 门禁。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .storage import get_db
from .workspace import now_iso

GENESIS = "0" * 64
ACTOR_LOCAL_OWNER = "local_owner"  # MVP：本机所有者（PRD A-001），多角色在 P1 启用

# 禁止进入 metadata 的键（敏感字段遮蔽，技术方案 §18）
_FORBIDDEN_META_KEYS = {"password", "apiKey", "token", "content", "text", "prompt"}


def _sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    return {k: v for k, v in metadata.items() if k not in _FORBIDDEN_META_KEYS}


def _event_hash(
    prev_hash: str,
    seq: int,
    created_at: str,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    case_id: str | None,
    result: str,
    metadata_json: str,
) -> str:
    canonical = "|".join(
        [
            prev_hash,
            str(seq),
            created_at,
            actor,
            action,
            resource_type,
            resource_id,
            case_id or "",
            result,
            metadata_json,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def append(
    conn: sqlite3.Connection,
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    case_id: str | None = None,
    result: str = "ok",
    metadata: dict[str, Any] | None = None,
    actor: str = ACTOR_LOCAL_OWNER,
) -> int:
    """在既有写事务内追加一条审计记录，返回 seq。

    调用方必须处于 db.transaction()（BEGIN IMMEDIATE）中，seq 分配因此串行安全。
    """
    row = conn.execute("SELECT seq, event_hash FROM audit_logs ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = row["event_hash"] if row else GENESIS
    seq = (row["seq"] + 1) if row else 1

    created_at = now_iso()
    meta_json = json.dumps(_sanitize_metadata(metadata), ensure_ascii=False, sort_keys=True)
    event_hash = _event_hash(
        prev_hash, seq, created_at, actor, action, resource_type, resource_id, case_id, result, meta_json
    )
    conn.execute(
        """
        INSERT INTO audit_logs(seq, created_at, actor, action, resource_type, resource_id,
                               case_id, result, metadata_json, prev_hash, event_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (seq, created_at, actor, action, resource_type, resource_id, case_id, result, meta_json, prev_hash, event_hash),
    )
    return seq


def log_standalone(
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    case_id: str | None = None,
    result: str = "ok",
    metadata: dict[str, Any] | None = None,
) -> int:
    """独立事务记一条（用于拒绝/失败等无业务事务的场景）。"""
    db = get_db()
    with db.transaction() as conn:
        return append(
            conn,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            case_id=case_id,
            result=result,
            metadata=metadata,
        )


def log_standalone_profile(
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    result: str = "ok",
    metadata: dict[str, Any] | None = None,
) -> int:
    """设备级配置库（profile）的独立审计（设置变更 / 连接器 / 技能）。"""
    from .storage import get_profile_db

    db = get_profile_db()
    with db.transaction() as conn:
        return append(
            conn,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            metadata=metadata,
        )


def verify() -> dict[str, Any]:
    """全链重算校验。ok=True 表示完整；否则返回首个断点 seq。"""
    db = get_db()
    rows = db.query("SELECT * FROM audit_logs ORDER BY seq ASC")
    prev_hash = GENESIS
    expected_seq = 1
    for row in rows:
        if row["seq"] != expected_seq or row["prev_hash"] != prev_hash:
            return {"ok": False, "brokenAt": row["seq"], "total": len(rows), "reason": "chain link mismatch"}
        recomputed = _event_hash(
            row["prev_hash"],
            row["seq"],
            row["created_at"],
            row["actor"],
            row["action"],
            row["resource_type"],
            row["resource_id"],
            row["case_id"],
            row["result"],
            row["metadata_json"],
        )
        if recomputed != row["event_hash"]:
            return {"ok": False, "brokenAt": row["seq"], "total": len(rows), "reason": "event hash mismatch"}
        prev_hash = row["event_hash"]
        expected_seq += 1
    return {"ok": True, "total": len(rows)}


def list_logs(
    case_id: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """按案件 / 动作筛选审计记录，时间倒序。"""
    db = get_db()
    limit = max(1, min(500, limit))
    sql = "SELECT * FROM audit_logs"
    conds: list[str] = []
    params: list[Any] = []
    if case_id:
        conds.append("case_id = ?")
        params.append(case_id)
    if action:
        conds.append("action = ?")
        params.append(action)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    rows = db.query(sql, tuple(params))
    out = []
    for r in rows:
        try:
            meta = json.loads(r["metadata_json"])
        except json.JSONDecodeError:
            meta = {}
        out.append(
            {
                "seq": r["seq"],
                "createdAt": r["created_at"],
                "actor": r["actor"],
                "action": r["action"],
                "resourceType": r["resource_type"],
                "resourceId": r["resource_id"],
                "caseId": r["case_id"],
                "result": r["result"],
                "metadata": meta,
                "eventHash": r["event_hash"][:16],
            }
        )
    return out
