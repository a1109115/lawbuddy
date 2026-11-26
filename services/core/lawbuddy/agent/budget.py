"""任务预算与无进展控制（DEV-AGT-006 / 技术方案 §7；门禁 A-002 / A-003 / A-004 / A-013）。

五类上限（可按任务覆盖，`agent.start` 的 `budget` 入参）：
轮次 maxRounds、墙钟 maxWallSeconds、模型调用 maxModelCalls、工具调用 maxToolCalls、
token maxTokens。

行为：
- 达 80% → `warn`（事件预警，不中断）；
- 达 100% → `blocked`：模型调用降级本地分析、工具调用跳过，并给出可读解释；
- **无进展**（A-003）：连续三轮未新增证据或 Claim → `NO_PROGRESS`，停止后续调用；
- **重复调用**（A-004）：同工具同参数的第三次请求前拦截并重新规划；
- 计数持久化在 `task_budget`（V16），取消/恢复后连续计数，不做静默重置。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..storage import get_db
from ..workspace import now_iso

# 默认上限（技术方案 §7：轮次、时间与 token 预算；本地会话可完成任务即可）
DEFAULT_LIMITS: dict[str, int] = {
    "maxRounds": 8,
    # 完整阅卷超过 5 分钟仍未完成时自动降级为本地结果并交付，避免 UI 无限等待。
    "maxWallSeconds": 300,
    "maxModelCalls": 12,
    "maxToolCalls": 60,
    "maxTokens": 200_000,
}

WARN_RATIO = 0.8           # 预警阈值
NO_PROGRESS_LIMIT = 3      # 连续无进展轮次上限（A-003）
REPEAT_BLOCK_AT = 3        # 同工具同参数第 N 次请求前拦截（A-004）

# 计数字段 → 上限字段
_KIND_LIMIT = {
    "round": "maxRounds",
    "model_call": "maxModelCalls",
    "tool_call": "maxToolCalls",
}


class BudgetExceeded(RuntimeError):
    """预算耗尽：调用方须降级或停止，不得继续调用（A-002/A-013）。"""


class RepeatedCallBlocked(RuntimeError):
    """重复调用拦截：同工具同参数已连续请求，须重新规划（A-004）。"""


def ensure(task_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """初始化任务预算（幂等）：合并任务级覆盖，记录起始时间用于墙钟上限。"""
    db = get_db()
    row = db.query_one("SELECT * FROM task_budget WHERE task_id = ?", (task_id,))
    if row is not None:
        return snapshot(task_id)
    limits = dict(DEFAULT_LIMITS)
    for key, value in (overrides or {}).items():
        if key in limits and isinstance(value, (int, float)) and value > 0:
            limits[key] = int(value)
    used = {"round": 0, "model_call": 0, "tool_call": 0, "tokens": 0,
            "startedAt": now_iso()}
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO task_budget(task_id, limits_json, used_json, no_progress_rounds, stage, reasons_json, updated_at) "
            "VALUES (?, ?, ?, 0, 'ok', '[]', ?)",
            (task_id, json.dumps(limits), json.dumps(used), now_iso()),
        )
    return snapshot(task_id)


def upgrade_limits(task_id: str, overrides: dict[str, Any]) -> dict[str, Any]:
    """按工作量放宽上限（不重置已用量）。

    固定工作流（如合同风险审查逐项调用模型）的调用量与清单项数成正比，
    默认 300s/12 次限额会在长合同上把任务打死，且恢复时同 task_id 预算
    连续计数，旧限额永远无法续跑。此函数只在**新值更宽松**时提高上限，
    used/stage/reasons 原样保留（A-002 资源护栏与降级语义不变）。
    """
    db = get_db()
    row = db.query_one("SELECT limits_json FROM task_budget WHERE task_id = ?", (task_id,))
    if row is None:
        return ensure(task_id, overrides)
    limits = json.loads(row["limits_json"] or "{}")
    changed = False
    for key, value in (overrides or {}).items():
        if key in DEFAULT_LIMITS and isinstance(value, (int, float)) and value > 0 and int(value) > int(limits.get(key) or 0):
            limits[key] = int(value)
            changed = True
    if changed:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE task_budget SET limits_json = ?, updated_at = ? WHERE task_id = ?",
                (json.dumps(limits), now_iso(), task_id),
            )
    return snapshot(task_id)


def snapshot(task_id: str) -> dict[str, Any]:
    """预算快照（UI 展示与评测断言共用）。"""
    db = get_db()
    row = db.query_one("SELECT * FROM task_budget WHERE task_id = ?", (task_id,))
    if row is None:
        return {"taskId": task_id, "stage": "ok", "limits": dict(DEFAULT_LIMITS),
                "used": {}, "reasons": [], "noProgressRounds": 0}
    return {
        "taskId": task_id,
        "stage": row["stage"],
        "limits": json.loads(row["limits_json"] or "{}"),
        "used": json.loads(row["used_json"] or "{}"),
        "reasons": json.loads(row["reasons_json"] or "[]"),
        "noProgressRounds": int(row["no_progress_rounds"] or 0),
    }


# 可对外展示的预算计数：`calls` 里存的是「工具名|参数摘要」，参数可能含查询词或
# 文件路径，不能进入事件 payload、UI 或评测报告（方案 §2.1 第 5 条）。
PUBLIC_USED_KEYS = (
    "round", "model_call", "tool_call", "tokens", "subagent_round", "startedAt",
    "exploreRounds", "deliveryReservedRounds",
)


def public_used(used: dict[str, Any] | None) -> dict[str, Any]:
    """过滤预算已用量，只保留数值计数与时间，不返回工具参数摘要。"""
    return {
        key: value for key, value in (used or {}).items()
        if key in PUBLIC_USED_KEYS and isinstance(value, (int, float, str))
    }


def _evaluate(limits: dict[str, int], used: dict[str, Any]) -> tuple[str, list[str]]:
    """按已用量评估阶段与原因（warn / blocked）。"""
    reasons: list[str] = []
    stage = "ok"
    elapsed = 0.0
    try:
        from datetime import datetime

        started = datetime.fromisoformat(str(used.get("startedAt")))
        elapsed = (datetime.now(started.tzinfo) - started).total_seconds()
    except Exception:  # noqa: BLE001
        elapsed = 0.0

    pairs = (
        ("round", "maxRounds", "轮次"),
        ("model_call", "maxModelCalls", "模型调用次数"),
        ("tool_call", "maxToolCalls", "工具调用次数"),
        ("tokens", "maxTokens", "token 用量"),
    )
    for used_key, limit_key, label in pairs:
        limit = int(limits.get(limit_key) or 0)
        if limit <= 0:
            continue
        amount = float(used.get(used_key) or 0)
        if amount >= limit:
            stage = "blocked"
            reasons.append(f"{label}已达上限（{int(amount)}/{limit}）")
        elif amount >= limit * WARN_RATIO:
            stage = "warn" if stage == "ok" else stage
            reasons.append(f"{label}接近上限（{int(amount)}/{limit}）")
    wall = int(limits.get("maxWallSeconds") or 0)
    if wall > 0 and elapsed >= wall:
        stage = "blocked"
        reasons.append(f"执行时间已达上限（{int(elapsed)}s/{wall}s）")
    elif wall > 0 and elapsed >= wall * WARN_RATIO:
        stage = "warn" if stage == "ok" else stage
        reasons.append(f"执行时间接近上限（{int(elapsed)}s/{wall}s）")
    return stage, reasons


def _save(task_id: str, used: dict[str, Any], stage: str, reasons: list[str],
          no_progress: int | None = None) -> None:
    db = get_db()
    with db.transaction() as conn:
        if no_progress is None:
            conn.execute(
                "UPDATE task_budget SET used_json = ?, stage = ?, reasons_json = ?, updated_at = ? WHERE task_id = ?",
                (json.dumps(used), stage, json.dumps(reasons, ensure_ascii=False), now_iso(), task_id),
            )
        else:
            conn.execute(
                "UPDATE task_budget SET used_json = ?, stage = ?, reasons_json = ?, no_progress_rounds = ?, "
                "updated_at = ? WHERE task_id = ?",
                (json.dumps(used), stage, json.dumps(reasons, ensure_ascii=False), no_progress, now_iso(), task_id),
            )


def check(task_id: str, kind: str) -> dict[str, Any]:
    """调用前检查：已达上限抛 `BudgetExceeded`；接近上限返回预警（不抛）。"""
    snap = snapshot(task_id)
    limits = snap["limits"] or dict(DEFAULT_LIMITS)
    used = snap["used"]
    stage, reasons = _evaluate(limits, used)
    limit_key = _KIND_LIMIT.get(kind)
    if limit_key:
        limit = int(limits.get(limit_key) or 0)
        if limit > 0 and float(used.get(kind) or 0) >= limit:
            raise BudgetExceeded(f"{kind} 已达上限（{used.get(kind)}/{limit}）：{'; '.join(reasons)}")
    if stage == "blocked":
        raise BudgetExceeded("；".join(reasons) or "任务预算已耗尽")
    if stage != snap["stage"]:
        _save(task_id, used, stage, reasons)
    return {"stage": stage, "reasons": reasons}


def spend(task_id: str, kind: str, *, tokens: int = 0, count: int = 1) -> dict[str, Any]:
    """记账：调用成功后累加计数（轮次/模型/工具/token），返回最新快照。"""
    snap = snapshot(task_id)
    limits = snap["limits"] or dict(DEFAULT_LIMITS)
    used = dict(snap["used"] or {})
    used.setdefault("startedAt", now_iso())
    used[kind] = int(used.get(kind) or 0) + count
    if tokens:
        used["tokens"] = int(used.get("tokens") or 0) + int(tokens)
    stage, reasons = _evaluate(limits, used)
    _save(task_id, used, stage, reasons)
    return snapshot(task_id)


def register_call(task_id: str, tool: str, args_key: str) -> int:
    """重复调用检测（A-004）：返回本次为第几次；第 N 次（默认 3）前拦截并提示重新规划。

    计数保存在 used_json（不依赖 SQLite JSON 扩展），随任务持久化。
    """
    snap = snapshot(task_id)
    used = dict(snap["used"] or {})
    used.setdefault("startedAt", now_iso())
    calls = dict(used.get("calls") or {})
    key = f"{tool}|{args_key}"
    count = int(calls.get(key) or 0) + 1
    calls[key] = count
    used["calls"] = calls
    _save(task_id, used, snap["stage"], snap["reasons"])
    if count >= REPEAT_BLOCK_AT:
        raise RepeatedCallBlocked(
            f"同工具同参数已连续请求 {count} 次（{tool}），已拦截并要求重新规划（A-004）"
        )
    return count


def note_progress(task_id: str, *, claims_added: int, evidence_added: int) -> str:
    """进展登记（A-003）：连续三轮无新增证据与 Claim → `NO_PROGRESS`，停止后续调用。"""
    db = get_db()
    row = db.query_one("SELECT no_progress_rounds FROM task_budget WHERE task_id = ?", (task_id,))
    if row is None:
        ensure(task_id)
        rounds = 0
    else:
        rounds = int(row["no_progress_rounds"] or 0)
    if claims_added > 0 or evidence_added > 0:
        rounds = 0
    else:
        rounds += 1
    snap = snapshot(task_id)
    limits = snap["limits"] or dict(DEFAULT_LIMITS)
    used = dict(snap["used"] or {})
    stage, reasons = _evaluate(limits, used)
    result = "ok"
    if rounds >= NO_PROGRESS_LIMIT:
        result = "NO_PROGRESS"
        stage = "blocked"
        reasons = [*reasons, f"连续 {rounds} 轮无新增证据或 Claim（A-003）"]
    _save(task_id, used, stage, reasons, no_progress=rounds)
    return result


def blocked_reason(task_id: str) -> str:
    snap = snapshot(task_id)
    return "；".join(snap.get("reasons") or []) or "任务预算已耗尽"


# ---------------------------------------------------------------------------
# 共享预算预留（WP07）：主/子 Agent 原子「预留—结算—释放」
# ---------------------------------------------------------------------------

# 预留资源 → 对应的 used 计数键（结算时写回同一计数）
_RESOURCE_USED_KEY = {
    "round": "round",
    "tool_call": "tool_call",
    "model_call": "model_call",
    "prompt_token": "tokens",
    "completion_token": "tokens",
    "wall_ms": None,  # 墙钟由 startedAt 推导，不做预留结算
}


class ReservationDenied(BudgetExceeded):
    """预留被拒绝：已用量 + 其他在途预留会超出上限（并发逃逸保护）。"""


def _reserved_total(task_id: str, resource: str) -> int:
    row = get_db().query_one(
        "SELECT COALESCE(SUM(amount - spent), 0) AS c FROM task_budget_reservations "
        "WHERE task_id = ? AND resource = ? AND status = 'reserved'",
        (task_id, resource),
    )
    return int((row or {}).get("c") or 0)


def reserve(
    task_id: str, resource: str, amount: int = 1, *,
    owner_id: str, subagent_id: str | None = None,
) -> dict[str, Any]:
    """原子预留一份预算。

    判据是「已用 + 所有在途预留」，而不是只看已用：否则两个并发子代理各自
    通过检查、合起来超限，就是典型的预算逃逸（WP07 硬约束）。
    检查与插入在同一事务内完成，避免检查-插入竞态。
    """
    if resource not in _RESOURCE_USED_KEY:
        raise ValueError(f"未知预算资源：{resource}")
    amount = max(0, int(amount))
    db = get_db()
    ensure(task_id)
    snap = snapshot(task_id)
    limits = snap["limits"] or dict(DEFAULT_LIMITS)
    used = snap["used"] or {}
    limit_key = {
        "round": "maxRounds", "tool_call": "maxToolCalls", "model_call": "maxModelCalls",
        "prompt_token": "maxTokens", "completion_token": "maxTokens",
    }.get(resource, "")
    limit = int(limits.get(limit_key) or 0) if limit_key else 0
    used_key = _RESOURCE_USED_KEY.get(resource)
    already = int(used.get(used_key) or 0) if used_key else 0
    reservation_id = str(uuid.uuid4())
    ts = now_iso()
    with db.transaction() as conn:
        reserved_row = conn.execute(
            "SELECT COALESCE(SUM(amount - spent), 0) AS c FROM task_budget_reservations "
            "WHERE task_id = ? AND resource = ? AND status = 'reserved'",
            (task_id, resource),
        ).fetchone()
        reserved = int((reserved_row or {"c": 0})["c"] or 0)
        # wall_ms 不参与预留上限判定（墙钟是连续量，由 _evaluate 的 startedAt 推导）
        if limit_key and limit > 0:
            if amount <= 0:
                raise ReservationDenied("预留量必须为正数")
            if already + reserved + amount > limit:
                raise ReservationDenied(
                    f"{resource} 预留被拒绝：已用 {already} + 在途预留 "
                    f"{reserved} + 本次 {amount} 超过上限 {limit}"
                )
        conn.execute(
            """
            INSERT INTO task_budget_reservations(
                id, task_id, owner_id, subagent_id, resource, amount, unit, status,
                spent, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', 0, ?, ?)
            """,
            (reservation_id, task_id, owner_id, subagent_id, resource, amount,
             "token" if "token" in resource else ("ms" if resource == "wall_ms" else "count"),
             ts, ts),
        )
    return {
        "reservationId": reservation_id, "taskId": task_id, "resource": resource,
        "amount": amount, "ownerId": owner_id, "subagentId": subagent_id,
    }


def settle(reservation_id: str, *, spent: int | None = None, tokens: int = 0) -> dict[str, Any]:
    """结算预留：把预留量（或指定实际用量）写回 used 计数。

    结算幂等：已结算的预留重复结算不会二次累加。
    """
    db = get_db()
    row = db.query_one(
        "SELECT * FROM task_budget_reservations WHERE id = ?", (reservation_id,)
    )
    if row is None:
        raise ValueError("预留不存在")
    if str(row["status"]) != "reserved":
        return {"reservationId": reservation_id, "status": str(row["status"]), "applied": False}
    resource = str(row["resource"])
    actual = int(row["amount"] if spent is None else spent)
    used_key = _RESOURCE_USED_KEY.get(resource)
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE task_budget_reservations SET status = 'settled', spent = ?, updated_at = ? "
            "WHERE id = ? AND status = 'reserved'",
            (actual, ts, reservation_id),
        )
    if used_key and actual:
        snap = snapshot(str(row["task_id"]))
        limits = snap["limits"] or dict(DEFAULT_LIMITS)
        used = dict(snap["used"] or {})
        used.setdefault("startedAt", now_iso())
        if resource in ("prompt_token", "completion_token"):
            add = int(tokens or actual)
            used["tokens"] = int(used.get("tokens") or 0) + add
        else:
            used[used_key] = int(used.get(used_key) or 0) + actual
        stage, reasons = _evaluate(limits, used)
        _save(str(row["task_id"]), used, stage, reasons)
    return {
        "reservationId": reservation_id, "status": "settled", "applied": True, "spent": actual,
    }


def release(reservation_id: str) -> dict[str, Any]:
    """释放预留（调用失败或取消）：不写 used，只让额度可被其他子级复用。"""
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE task_budget_reservations SET status = 'released', updated_at = ? "
            "WHERE id = ? AND status = 'reserved'",
            (ts, reservation_id),
        )
        return {"reservationId": reservation_id, "status": "released", "applied": bool(cur.rowcount)}


def release_owner(task_id: str, owner_id: str) -> int:
    """释放某执行者的全部在途预留（子代理取消/超时传播时使用）。"""
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE task_budget_reservations SET status = 'released', updated_at = ? "
            "WHERE task_id = ? AND owner_id = ? AND status = 'reserved'",
            (ts, task_id, owner_id),
        )
        return int(cur.rowcount or 0)


def reservations(task_id: str) -> dict[str, Any]:
    """预留汇总（UI 与评测）：按状态与资源聚合，不返回参数或内容。"""
    rows = get_db().query(
        "SELECT resource, status, COUNT(*) AS c, COALESCE(SUM(amount),0) AS amount, "
        "COALESCE(SUM(spent),0) AS spent FROM task_budget_reservations "
        "WHERE task_id = ? GROUP BY resource, status",
        (task_id,),
    )
    out: dict[str, Any] = {}
    for r in rows:
        key = f"{r['resource']}:{r['status']}"
        out[key] = {"count": int(r["c"] or 0), "amount": int(r["amount"] or 0),
                    "spent": int(r["spent"] or 0)}
    return {"taskId": task_id, "byResource": out, "inflight": sum(
        1 for r in rows if str(r["status"]) == "reserved"
    )}
