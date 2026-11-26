"""子代理运行登记（LB-HERMES-2026-01 §7.5 / WP07）。

UI「子代理进度」需要看到工作单元、状态与预算占用，但不该暴露内部工具查找过程。
因此这里只登记**技术字段**：

- kind（explore / work_unit / batch）、status、轮次与工具调用计数、取消原因；
- 调查问题（scope）写入工作空间加密库，用于人工核对；不写日志、不进 Span；
- 结果正文不落本表，只写 `result_ref`（指向 `tool_result_blobs` 或答案格账本）。

状态机与 Tool Journal 一致地收紧：终态不可回退；取消必须带原因。
"""

from __future__ import annotations

import uuid
from typing import Any

from ..storage import get_db
from ..workspace import now_iso

KINDS = ("explore", "work_unit", "batch")
STATUSES = ("pending", "running", "staged", "accepted", "rejected", "failed", "cancelled")
TERMINAL = ("accepted", "rejected", "failed", "cancelled")

_ALLOWED: dict[str, tuple[str, ...]] = {
    "pending": ("running", "cancelled", "failed"),
    "running": ("staged", "accepted", "rejected", "failed", "cancelled"),
    "staged": ("accepted", "rejected", "cancelled", "failed"),
    "accepted": (),
    "rejected": (),
    "failed": (),
    "cancelled": (),
}


class SubagentRunError(RuntimeError):
    pass


def start(
    task_id: str,
    *,
    kind: str = "explore",
    parent_id: str | None = None,
    scope: dict[str, Any] | None = None,
    rounds_limit: int | None = None,
) -> dict[str, Any]:
    """登记一次子代理运行（pending → running）。返回可继续更新的条目。"""
    from . import observability

    if not task_id:
        raise SubagentRunError("缺少任务 ID，无法登记子代理运行")
    if kind not in KINDS:
        raise SubagentRunError(f"未知子代理类型：{kind}")
    run_id = f"sub-{uuid.uuid4().hex[:16]}"
    ts = now_iso()
    scope_json = dict(scope or {})
    if rounds_limit is not None:
        scope_json.setdefault("roundsLimit", int(rounds_limit))
    import json

    db = get_db()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO subagent_runs(id, task_id, parent_id, kind, status, scope_json, "
            "rounds_used, tool_calls_used, created_at, updated_at) "
            "VALUES (?,?,?,?,'running',?,0,0,?,?)",
            (run_id, task_id, parent_id, kind,
             json.dumps(scope_json, ensure_ascii=False), ts, ts),
        )
    # Span 只记类型与标识，不记调查问题
    with observability.span("subagent", kind, task_id=task_id,
                           attributes={"subagentId": run_id}) as sp:
        sp.end("ok")
    return get(run_id)


def finish(
    run_id: str,
    *,
    status: str,
    rounds_used: int | None = None,
    tool_calls_used: int | None = None,
    result_ref: str | None = None,
    cancel_reason: str | None = None,
) -> dict[str, Any]:
    """更新子代理运行状态（合法迁移校验；终态不可回退）。"""
    if status not in STATUSES:
        raise SubagentRunError(f"未知子代理状态：{status}")
    entry = get(run_id)
    current = entry["status"]
    if status != current and status not in _ALLOWED.get(current, ()):
        raise SubagentRunError(f"子代理状态不能从 {current} 变为 {status}")
    ts = now_iso()
    db = get_db()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE subagent_runs SET status = ?, rounds_used = COALESCE(?, rounds_used), "
            "tool_calls_used = COALESCE(?, tool_calls_used), result_ref = COALESCE(?, result_ref), "
            "cancel_reason = COALESCE(?, cancel_reason), updated_at = ? WHERE id = ?",
            (status, rounds_used, tool_calls_used, result_ref,
             cancel_reason, ts, run_id),
        )
    return get(run_id)


def cancel(run_id: str, reason: str = "用户取消") -> dict[str, Any]:
    entry = get(run_id)
    if entry["status"] in TERMINAL:
        return entry
    return finish(run_id, status="cancelled", cancel_reason=reason)


def get(run_id: str) -> dict[str, Any]:
    row = get_db().query_one("SELECT * FROM subagent_runs WHERE id = ?", (run_id,))
    if row is None:
        raise SubagentRunError("子代理运行记录不存在")
    return _row_to_dict(row)


def list_for_task(task_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = get_db().query(
        "SELECT * FROM subagent_runs WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
        (task_id, max(1, int(limit))),
    )
    return [_row_to_dict(r) for r in rows]


def stats(task_id: str) -> dict[str, int]:
    """按状态计数（恢复中心与任务详情用）。"""
    rows = get_db().query(
        "SELECT status, COUNT(*) AS c FROM subagent_runs WHERE task_id = ? GROUP BY status",
        (task_id,),
    )
    return {str(r["status"]): int(r["c"]) for r in rows}


def release_task(task_id: str) -> int:
    """任务取消/删除时把未终态子代理记为 cancelled，避免 UI 永久显示执行中。"""
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE subagent_runs SET status = 'cancelled', cancel_reason = COALESCE(cancel_reason, "
            "'任务结束'), updated_at = ? WHERE task_id = ? AND status IN ('pending','running','staged')",
            (ts, task_id),
        )
    return int(cur.rowcount or 0)


def _row_to_dict(row: Any) -> dict[str, Any]:
    import json

    try:
        scope = json.loads(row["scope_json"] or "{}")
    except (TypeError, ValueError):
        scope = {}
    return {
        "id": row["id"],
        "taskId": row["task_id"],
        "parentId": row["parent_id"],
        "kind": row["kind"],
        "status": row["status"],
        "scope": scope,
        "resultRef": row["result_ref"],
        "roundsUsed": int(row["rounds_used"] or 0),
        "toolCallsUsed": int(row["tool_calls_used"] or 0),
        "cancelReason": row["cancel_reason"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
