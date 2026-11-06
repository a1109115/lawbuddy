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
