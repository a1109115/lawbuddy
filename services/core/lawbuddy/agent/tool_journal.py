"""工具调用 Journal、任务租约与恢复对账（LB-HERMES-2026-01 WP01）。

目标：消除「工具已产生副作用、但检查点尚未保存」的不确定窗口。执行路径固定为

    staged（意图落库） → executing（取得租约与 fencing token） → 结果加密落库
                       → succeeded / failed_retryable / failed_final / unknown / cancelled

硬约束：
1. `staged` 落库失败时**禁止执行**工具（抛 `JournalError`，由调用方回灌模型）。
2. 非幂等工具在 `executing` 后崩溃进入 `unknown`，恢复时**不得自动重放**；
   必须先由用户处置（查看目标状态 / 标记已发生 / 重新执行 / 忽略）。
3. `owner_id + fencing_token`：过期 Worker 不能提交旧结果覆盖新状态。
4. 结果正文只进加密 blob；本模块与事件只暴露 ID、状态、计数与错误码。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..storage import get_db
from ..workspace import now_iso

JOURNAL_STATUSES = (
    "staged", "executing", "succeeded", "failed_retryable", "failed_final", "unknown", "cancelled",
)
TERMINAL_STATUSES = ("succeeded", "failed_final", "cancelled")

# 允许的状态迁移：非法迁移直接拒绝，避免状态被静默伪造
_ALLOWED: dict[str, tuple[str, ...]] = {
    "staged": ("executing", "cancelled", "failed_final"),
    "executing": ("succeeded", "failed_retryable", "failed_final", "unknown", "cancelled"),
    "failed_retryable": ("executing", "unknown", "cancelled", "failed_final"),
    "unknown": ("executing", "succeeded", "failed_retryable", "cancelled", "failed_final"),
    "succeeded": (),
    "failed_final": (),
    "cancelled": (),
}

# 恢复对账时可安全重放的错误类别（只读或幂等工具）
_RETRYABLE_CODES = ("disconnect", "timeout", "stale_stream", "provider_unavailable", "persistence")

LEASE_DEFAULT_TTL_S = 300
_RESULT_DIRNAME = "tool_results"


class JournalError(RuntimeError):
    """Journal 不可用或状态迁移非法；调用方必须视为「不得执行副作用」。"""


class LeaseLost(JournalError):
    """租约已过期或已被新所有者接管：当前 Worker 不得再提交结果。"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def args_hash(args: Any) -> str:
    """参数指纹（审批绑定与参数变更失效判定；不含敏感明文到事件）。"""
    return hashlib.sha256(_dumps(args or {}).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 结果 blob：加密落盘，库内只存哈希与路径
# ---------------------------------------------------------------------------

def _result_root() -> Path:
    from ..sandbox import workspace_root

    root = workspace_root() / _RESULT_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _store_result_bytes(payload: bytes, sha: str) -> str:
    """加密写入结果 blob，返回相对工作空间的路径；同哈希复用。"""
    from .. import crypto
    from ..keyring import get_active_dek

    root = _result_root()
    dst = root / sha[:2] / f"{sha}.enc"
    rel = f"{_RESULT_DIRNAME}/{sha[:2]}/{sha}.enc"
    if dst.exists():
        return rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    key = crypto.derive_file_key(get_active_dek())
    tmp = dst.with_suffix(".tmp")
    tmp.write_bytes(crypto.encrypt_bytes(payload, key))
    os.replace(tmp, dst)
    return rel


def read_result(task_id: str, call_id: str) -> Any:
    """按 task_id + call_id 读取工具结果正文（解密；不存在返回 None）。"""
    row = get_db().query_one(
        """
        SELECT b.storage_path FROM tool_call_journal j
        JOIN tool_result_blobs b ON b.journal_id = j.id
        WHERE j.task_id = ? AND j.call_id = ? ORDER BY b.created_at DESC LIMIT 1
        """,
        (task_id, call_id),
    )
    if row is None:
        return None
    from .. import crypto
    from ..keyring import get_active_dek
    from ..sandbox import workspace_root

    path = workspace_root() / str(row["storage_path"])
    if not path.exists():
        return None
    raw = crypto.decrypt_bytes(path, crypto.derive_file_key(get_active_dek()))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# 任务租约与 fencing token
# ---------------------------------------------------------------------------

def new_owner_id(prefix: str = "worker") -> str:
    return f"{prefix}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def acquire_lease(
    task_id: str, owner_id: str, ttl_s: int = LEASE_DEFAULT_TTL_S, *, takeover: bool = True,
) -> int:
    """获取或续租任务租约，返回当前 fencing token。

    `takeover=True`（任务启动/恢复的默认语义）：接管既有租约并递增 fencing token，
    使旧 Worker 的后续提交被拒绝——这覆盖“上一个进程已崩溃但租约行仍在”的情况，
    也是 HUP-T-003 的实际保护点。`takeover=False` 用于运行中续期，此时若租约
    归属其他所有者则抛 `LeaseLost`，不抢占正在执行的 Worker。
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=ttl_s)).isoformat()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT owner_id, fencing_token, lease_expires_at FROM task_leases WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO task_leases(task_id, owner_id, fencing_token, lease_expires_at,
                                        acquired_at, heartbeat_at)
                VALUES (?, ?, 1, ?, ?, ?)
                """,
                (task_id, owner_id, expires, now.isoformat(), now.isoformat()),
            )
            return 1
        alive = str(row["lease_expires_at"] or "") > now.isoformat()
        if alive and str(row["owner_id"]) == owner_id:
            conn.execute(
                "UPDATE task_leases SET lease_expires_at = ?, heartbeat_at = ? WHERE task_id = ?",
                (expires, now.isoformat(), task_id),
            )
            return int(row["fencing_token"] or 1)
        if alive and not takeover:
            raise LeaseLost("任务正由其他执行者持有租约")
        # 过期或接管：递增 fencing token，旧 Worker 的结果提交会被拒绝
        token = int(row["fencing_token"] or 1) + 1
        conn.execute(
            """
            UPDATE task_leases SET owner_id = ?, fencing_token = ?, lease_expires_at = ?,
                                   acquired_at = ?, heartbeat_at = ?
            WHERE task_id = ?
            """,
            (owner_id, token, expires, now.isoformat(), now.isoformat(), task_id),
        )
        return token


def heartbeat(task_id: str, owner_id: str, ttl_s: int = LEASE_DEFAULT_TTL_S) -> bool:
    """续租心跳；租约已归属他人时返回 False（调用方应停止提交结果）。"""
    db = get_db()
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=ttl_s)).isoformat()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE task_leases SET lease_expires_at = ?, heartbeat_at = ? "
            "WHERE task_id = ? AND owner_id = ?",
            (expires, now.isoformat(), task_id, owner_id),
        )
        return bool(cur.rowcount)


def release_lease(task_id: str, owner_id: str) -> None:
    """正常结束时释放租约（不删除行，保留 fencing token 单调性）。"""
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE task_leases SET lease_expires_at = ?, heartbeat_at = ? "
            "WHERE task_id = ? AND owner_id = ?",
            (now, now, task_id, owner_id),
        )


def current_fence(task_id: str) -> int:
    row = get_db().query_one("SELECT fencing_token FROM task_leases WHERE task_id = ?", (task_id,))
    return int(row["fencing_token"] or 0) if row else 0


# ---------------------------------------------------------------------------
# Journal 主体
# ---------------------------------------------------------------------------

def stage(
    task_id: str,
    call_id: str,
    tool_name: str,
    *,
    effect: str,
    idempotent: bool,
    args: Any = None,
    conversation_id: str | None = None,
    subagent_id: str | None = None,
    origin: str = "main",
    tool_version: str = "1",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """把工具意图写入 `staged`；写库失败抛 `JournalError`，调用方不得执行。

    幂等：同一 `task_id + call_id` 重复登记时返回既有行，不新增记录也不重置状态
    （恢复后重放同一调用不会产生第二份副作用登记）。
    """
    db = get_db()
    ts = now_iso()
    digest = args_hash(args)
    journal_id = str(uuid.uuid4())
    try:
        with db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM tool_call_journal WHERE task_id = ? AND call_id = ?",
                (task_id, call_id),
            ).fetchone()
            if existing is not None:
                return _row_to_dict(existing)
            conn.execute(
                """
                INSERT INTO tool_call_journal(
                    id, task_id, call_id, tool_name, tool_version, conversation_id, subagent_id,
                    origin, args_hash, effect, idempotent, idempotency_key, status, attempt,
                    created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', 1, ?, ?)
                """,
                (
                    journal_id, task_id, call_id, tool_name, tool_version, conversation_id,
                    subagent_id, origin, digest, effect, 1 if idempotent else 0,
                    idempotency_key, ts, ts,
                ),
            )
    except Exception as exc:  # noqa: BLE001 落库失败即禁止执行
        raise JournalError(f"工具意图落库失败，禁止执行：{type(exc).__name__}") from exc
    row = db.query_one("SELECT * FROM tool_call_journal WHERE id = ?", (journal_id,))
    if row is None:  # pragma: no cover - 插入后不可见属严重故障
        raise JournalError("工具意图落库后不可见，禁止执行")
    return _row_to_dict(row)


def begin(journal_id: str, owner_id: str, fencing_token: int) -> None:
    """`staged/failed_retryable/unknown → executing`，同时校验 owner 与 fencing token。"""
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT task_id, status, attempt, fencing_token FROM tool_call_journal WHERE id = ?",
            (journal_id,),
        ).fetchone()
        if row is None:
            raise JournalError("工具调用登记不存在")
        status = str(row["status"])
        if status == "executing":
            raise JournalError("工具调用已在执行中，禁止重复进入")
        if status in ("succeeded", "cancelled", "failed_final"):
            raise JournalError(f"工具调用已终结（{status}），禁止重新执行")
        lease = conn.execute(
            "SELECT owner_id, fencing_token FROM task_leases WHERE task_id = ?", (row["task_id"],)
        ).fetchone()
        if lease is not None:
            if str(lease["owner_id"]) != owner_id:
                raise LeaseLost("租约归属已变更，拒绝提交")
            if int(lease["fencing_token"] or 0) != int(fencing_token):
                raise LeaseLost("fencing token 已过期，拒绝提交")
        conn.execute(
            """
            UPDATE tool_call_journal
            SET status = 'executing', owner_id = ?, fencing_token = ?, attempt = attempt + ?,
                started_at = COALESCE(started_at, ?), updated_at = ?
            WHERE id = ?
            """,
            (owner_id, fencing_token, 0 if status == "staged" else 1, ts, ts, journal_id),
        )


def _commit(
    journal_id: str, status: str, *, owner_id: str, fencing_token: int,
    error_code: str | None = None, result_ref: str | None = None,
    result_sha: str | None = None, operator: bool = False,
) -> None:
    """带 owner/fencing 校验的状态提交（过期 Worker 无法覆盖新状态）。

    `operator=True` 用于用户在恢复中心的人工处置：处置不是 Worker 提交，
    因此不校验 owner，但仍必须匹配当前 fencing token——否则一个陈旧的
    UI 快照不能改写已被新 Worker 接管的任务。
    """
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT task_id, status FROM tool_call_journal WHERE id = ?", (journal_id,)
        ).fetchone()
        if row is None:
            raise JournalError("工具调用登记不存在")
        current = str(row["status"])
        if current not in ("executing", "failed_retryable", "unknown", "staged"):
            raise JournalError(f"工具调用状态 {current} 不可提交为 {status}")
        allowed = _ALLOWED.get(current, ())
        if status not in allowed:
            raise JournalError(f"非法状态迁移：{current} → {status}")
        lease = conn.execute(
            "SELECT owner_id, fencing_token FROM task_leases WHERE task_id = ?", (row["task_id"],)
        ).fetchone()
        if lease is not None:
            if not operator and str(lease["owner_id"]) != owner_id:
                raise LeaseLost("租约归属已变更，旧结果被拒绝")
            if operator:
                # 运营处置使用当前 token 校验来源是否过期
                if fencing_token and int(lease["fencing_token"] or 0) != int(fencing_token):
                    raise LeaseLost("fencing token 已过期，请刷新恢复中心后重试")
            elif int(lease["fencing_token"] or 0) != int(fencing_token):
                raise LeaseLost("fencing token 已过期，旧结果被拒绝")
        conn.execute(
            """
            UPDATE tool_call_journal
            SET status = ?, error_code = ?, result_ref = COALESCE(?, result_ref),
                result_sha256 = COALESCE(?, result_sha256), updated_at = ?, finished_at = ?
            WHERE id = ?
            """,
            (status, error_code, result_ref, result_sha, ts,
             ts if status in TERMINAL_STATUSES or status == "failed_final" else None, journal_id),
        )


def succeed(
    journal_id: str, *, owner_id: str, fencing_token: int, result: Any,
) -> str:
    """结果加密落库并标记 `succeeded`，返回结果引用。"""
    db = get_db()
    payload = _dumps(result).encode("utf-8")
    sha = hashlib.sha256(payload).hexdigest()
    row = db.query_one("SELECT task_id FROM tool_call_journal WHERE id = ?", (journal_id,))
    if row is None:
        raise JournalError("工具调用登记不存在")
    task_id = str(row["task_id"])
    rel = _store_result_bytes(payload, sha)
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO tool_result_blobs(id, task_id, journal_id, content_sha256, byte_size,
                                          storage_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), task_id, journal_id, sha, len(payload), rel, ts),
        )
    _commit(
        journal_id, "succeeded", owner_id=owner_id, fencing_token=fencing_token,
        result_ref=rel, result_sha=sha,
    )
    return rel


def fail(
    journal_id: str, *, owner_id: str, fencing_token: int, error_code: str,
    retryable: bool = False,
) -> None:
    """失败落库：`retryable=True → failed_retryable`，否则 `failed_final`。"""
    _commit(
        journal_id, "failed_retryable" if retryable else "failed_final",
        owner_id=owner_id, fencing_token=fencing_token, error_code=error_code,
    )


def mark_unknown(journal_id: str, *, owner_id: str, fencing_token: int, error_code: str) -> None:
    """标记为 `unknown`：副作用可能已发生，禁止盲目重放，等待人工处置。"""
    _commit(
        journal_id, "unknown", owner_id=owner_id, fencing_token=fencing_token,
        error_code=error_code,
    )


def cancel(journal_id: str, *, owner_id: str, fencing_token: int) -> None:
    _commit(
        journal_id, "cancelled", owner_id=owner_id, fencing_token=fencing_token,
        error_code="cancelled",
    )


# ---------------------------------------------------------------------------
# 查询、对账与人工处置
# ---------------------------------------------------------------------------

def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "taskId": row["task_id"],
        "callId": row["call_id"],
        "toolName": row["tool_name"],
        "toolVersion": row["tool_version"],
        "origin": row["origin"],
        "effect": row["effect"],
        "idempotent": bool(row["idempotent"]),
        "status": row["status"],
        "attempt": int(row["attempt"] or 1),
        "ownerId": row["owner_id"],
        "fencingToken": int(row["fencing_token"] or 0),
        "resultRef": row["result_ref"],
        "resultSha256": row["result_sha256"],
        "errorCode": row["error_code"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get(task_id: str, call_id: str) -> dict[str, Any] | None:
    row = get_db().query_one(
        "SELECT * FROM tool_call_journal WHERE task_id = ? AND call_id = ?", (task_id, call_id)
    )
    return _row_to_dict(row) if row else None


def get_by_id(journal_id: str) -> dict[str, Any] | None:
    row = get_db().query_one("SELECT * FROM tool_call_journal WHERE id = ?", (journal_id,))
    return _row_to_dict(row) if row else None


def list_for_task(task_id: str, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    db = get_db()
    if status:
        rows = db.query(
            "SELECT * FROM tool_call_journal WHERE task_id = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (task_id, status, limit),
        )
    else:
        rows = db.query(
            "SELECT * FROM tool_call_journal WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
            (task_id, limit),
        )
    return [_row_to_dict(r) for r in rows]


def unresolved_unknown(task_id: str, tool_name: str | None = None) -> list[dict[str, Any]]:
    """未处置的 `unknown` 条目：同工具存在此类条目时禁止再次执行非幂等调用。"""
    db = get_db()
    if tool_name:
        rows = db.query(
            "SELECT * FROM tool_call_journal WHERE task_id = ? AND status = 'unknown' "
            "AND tool_name = ? ORDER BY created_at ASC",
            (task_id, tool_name),
        )
    else:
        rows = db.query(
            "SELECT * FROM tool_call_journal WHERE task_id = ? AND status = 'unknown' "
            "ORDER BY created_at ASC",
            (task_id,),
        )
    return [_row_to_dict(r) for r in rows]


def reconcile(task_id: str, owner_id: str) -> dict[str, Any]:
    """恢复时对账：把遗留 `executing` 条目转成可解释状态。

    - 幂等工具 → `failed_retryable`（可安全重放）；
    - 非幂等工具 → `unknown`（禁止自动重放，进入人工处置）。
    返回计数，供 resume 事件与 UI 展示。
    """
    db = get_db()
    ts = now_iso()
    retryable = 0
    unknown = 0
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id, tool_name, idempotent FROM tool_call_journal "
            "WHERE task_id = ? AND status = 'executing'",
            (task_id,),
        ).fetchall()
        for row in rows:
            if int(row["idempotent"] or 0):
                conn.execute(
                    "UPDATE tool_call_journal SET status = 'failed_retryable', "
                    "error_code = 'interrupted', owner_id = ?, updated_at = ? WHERE id = ?",
                    (owner_id, ts, row["id"]),
                )
                retryable += 1
            else:
                conn.execute(
                    "UPDATE tool_call_journal SET status = 'unknown', "
                    "error_code = 'interrupted', owner_id = ?, updated_at = ? WHERE id = ?",
                    (owner_id, ts, row["id"]),
                )
                unknown += 1
        staged = conn.execute(
            "SELECT COUNT(*) AS c FROM tool_call_journal WHERE task_id = ? AND status = 'staged'",
            (task_id,),
        ).fetchone()
        staged_count = int(staged["c"] or 0)
        if staged_count:
            # staged 从未执行：直接作废，不留悬挂意图
            conn.execute(
                "UPDATE tool_call_journal SET status = 'cancelled', error_code = 'never_started', "
                "updated_at = ? WHERE task_id = ? AND status = 'staged'",
                (ts, task_id),
            )
    return {"retryable": retryable, "unknown": unknown, "cancelledStaged": staged_count}


def resolve(
    journal_id: str, action: str, *, owner_id: str, fencing_token: int, note: str | None = None,
) -> dict[str, Any]:
    """人工处置 `unknown` 条目。

    - `mark_happened`：副作用已发生，标记为 `succeeded`（不重放）；
    - `re_execute`：允许重新执行（回到 `failed_retryable`，由执行路径重新进入）；
    - `ignore`：确认无副作用，标记为 `cancelled`。
    """
    if journal_id and not get_by_id(journal_id):
        raise JournalError("工具调用登记不存在")
    if action == "mark_happened":
        _commit(journal_id, "succeeded", owner_id=owner_id, fencing_token=fencing_token,
                error_code=None, operator=True)
    elif action == "re_execute":
        _commit(journal_id, "failed_retryable", owner_id=owner_id, fencing_token=fencing_token,
                error_code=note or "manual_retry", operator=True)
    elif action == "ignore":
        _commit(journal_id, "cancelled", owner_id=owner_id, fencing_token=fencing_token,
                error_code=note or "manual_ignore", operator=True)
    else:
        raise JournalError(f"未知处置动作：{action}")
    return get_by_id(journal_id) or {}


def stats(task_id: str) -> dict[str, int]:
    rows = get_db().query(
        "SELECT status, COUNT(*) AS c FROM tool_call_journal WHERE task_id = ? GROUP BY status",
        (task_id,),
    )
    return {str(r["status"]): int(r["c"] or 0) for r in rows}


def delete_task_state(task_id: str) -> None:
    """任务级清理（工作空间重建 / 评测用）。"""
    db = get_db()
    with db.transaction() as conn:
        conn.execute("DELETE FROM tool_result_blobs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM tool_call_journal WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_leases WHERE task_id = ?", (task_id,))


def classify_exception(exc: BaseException) -> str:
    """把异常粗分类为错误码（不含异常正文，避免敏感内容进事件）。"""
    name = type(exc).__name__.lower()
    if "cancelled" in name:
        return "cancelled"
    if "timeout" in name:
        return "timeout"
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "disconnect"
    if isinstance(exc, (MemoryError, OSError)) and "sqlite" in str(exc).lower():
        return "persistence"
    if isinstance(exc, JournalError):
        return "persistence"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "invalid_request"
    return "format"


def with_journal(
    *,
    task_id: str,
    call_id: str,
    tool_name: str,
    effect: str,
    idempotent: bool,
    owner_id: str,
    fencing_token: int,
    executor: Callable[[], Any],
    args: Any = None,
    conversation_id: str | None = None,
    subagent_id: str | None = None,
    origin: str = "main",
) -> Any:
    """完整执行封装：staged → begin → 执行 → 结果落库 → 对账。

    供 `tool_runtime` 与评测共用；任何一步失败都留下可解释状态，不静默吞掉。
    """
    entry = stage(
        task_id, call_id, tool_name, effect=effect, idempotent=idempotent, args=args,
        conversation_id=conversation_id, subagent_id=subagent_id, origin=origin,
    )
    journal_id = str(entry["id"])
    # 同一 call_id 已成功过：直接返回已存结果（幂等重放）
    if entry["status"] == "succeeded" and entry.get("resultRef"):
        cached = read_result(task_id, call_id)
        if cached is not None:
            return cached
    begin(journal_id, owner_id, fencing_token)
    try:
        result = executor()
    except BaseException as exc:  # noqa: BLE001 先落状态再向上抛
        code = classify_exception(exc)
        if code == "cancelled":
            cancel(journal_id, owner_id=owner_id, fencing_token=fencing_token)
            raise
        retryable = bool(idempotent) and code in _RETRYABLE_CODES
        fail(journal_id, owner_id=owner_id, fencing_token=fencing_token,
             error_code=code, retryable=retryable)
        raise
    succeed(journal_id, owner_id=owner_id, fencing_token=fencing_token, result=result)
    return result


def now() -> str:  # 便于评测替换时间源
    return datetime.now(timezone.utc).isoformat()


def sleep_ms(ms: int) -> None:  # 供预算/租约评测使用
    time.sleep(max(0, ms) / 1000.0)
