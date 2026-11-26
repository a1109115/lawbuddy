"""动态工作调度器（LB-HARNESS-2026-01 §4.5 / B-03）。

调度输入**只有工作账本与预算**：不读任务请求之外的任何答案来源，也不读官方
参考或评分器数据。优先级规则固定为：

    unblock 其他单元的节点 → 未尝试节点 → 待修复节点 → 可重试失败节点

约束：
- 无饥饿：未尝试节点优先于重试节点，且同一优先级内按 attempts/更新时间轮转；
- 无单点耗尽：单单元连续失败只影响自身终态，不占满全任务预算；
- 依赖顺序：`artifact:<name>` 依赖未终态时不允许提前物化；
- 模型可决定单元内策略，但不能删除系统编译出的工作全集。
"""

from __future__ import annotations

from typing import Any

from . import work_ledger

# 交付保留预算：收尾物化/校验专用，探索阶段不得占用（§4.5 第 2 条）
DELIVERY_RESERVED_RATIO = 0.15
DELIVERY_RESERVED_MIN_ROUNDS = 8

# 连续失败判定：证据类失败（检索无命中/解析失败/证据不足）→ needs_human；
# 服务类失败（模型或网络）→ blocked，不得误标为证据不足（§4.5 第 3 条）。
EVIDENCE_FAILURES = (
    "NO_HITS", "PARSE_FAILED", "INSUFFICIENT_EVIDENCE", "EMPTY_ANSWER", "SOURCE_MISSING",
)
SERVICE_FAILURES = (
    "MODEL_UNAVAILABLE", "NETWORK_ERROR", "RATE_LIMITED", "TIMEOUT", "DISCONNECTED",
)
FAIL_STREAK_LIMIT = 2


def split_budget(max_rounds: int) -> dict[str, int]:
    """把轮数预算拆成探索预算与交付保留预算（交付保留不可被探索占用）。"""
    reserved = max(DELIVERY_RESERVED_MIN_ROUNDS, int(max_rounds * DELIVERY_RESERVED_RATIO))
    reserved = min(reserved, max(0, max_rounds - 1))
    return {"exploreRounds": max(0, max_rounds - reserved), "deliveryReservedRounds": reserved}


def classify_failure(error_code: str) -> str:
    """失败分类：证据类 → needs_human；服务类 → blocked；未知按证据类保守处理。"""
    code = str(error_code or "").strip().upper()
    if code in SERVICE_FAILURES:
        return "blocked"
    return "needs_human"


def record_failure(
    task_id: str,
    work_unit_id: str,
    error_code: str,
    *,
    streak: int = 1,
) -> dict[str, Any] | None:
    """登记一次失败并决定终态：连续两次证据类失败 → needs_human；服务类直接 blocked。

    返回迁移后的工作单元；未达阈值时保持租约态（可继续重试）。
    """
    unit = work_ledger.get_unit(task_id, work_unit_id)
    if unit is None:
        return None
    target = classify_failure(error_code)
    if target == "blocked" or int(streak) >= FAIL_STREAK_LIMIT:
        try:
            return work_ledger.transition_unit(
                task_id, work_unit_id, target, error_code=str(error_code or "UNKNOWN"),
            )
        except work_ledger.InvalidTransition:
            return work_ledger.get_unit(task_id, work_unit_id)
    return unit


def select_next(
    task_id: str,
    *,
    reserve_for_delivery: bool = False,
    exclude: tuple[str, ...] = (),
    scope_types: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """选择下一个工作单元（只读账本）。

    reserve_for_delivery=True 时只允许返回 materialize / derive 类单元，
    保证交付保留预算不被探索类单元占用；`scope_types` 用于让调用方
    （如 work_unit_run 的探索派发）排除物化/派生类单元。
    """
    candidates = work_ledger.list_units(
        task_id, statuses=("pending", "leased", "needs_human", "blocked"),
    )
    if reserve_for_delivery:
        allowed: tuple[str, ...] | None = ("materialize", "derive")
    else:
        allowed = tuple(scope_types) if scope_types else None
    ordered = sorted(candidates, key=lambda u: (
        0 if (delivery_ready(task_id, u)) else 1,
        0 if u["attempts"] == 0 else 1,
        0 if u["status"] in ("needs_human", "blocked") else 1,
        u["priority"], u["attempts"], u["updatedAt"],
    ))
    for unit in ordered:
        if unit["workUnitId"] in exclude:
            continue
        if allowed is not None and unit["scopeType"] not in allowed:
            continue
        if int(unit["attempts"]) >= int(unit["maxAttempts"]):
            continue
        if not dependencies_met(task_id, unit):
            continue
        return unit
    return None


def delivery_ready(task_id: str, unit: dict[str, Any]) -> bool:
    """该单元是否可解除其他单元的阻塞（其答案格全部终态即解锁物化）。"""
    scope = unit.get("scope") or {}
    artifact = str(scope.get("artifact") or "")
    if not artifact:
        return False
    return work_ledger.artifact_cells_done(task_id, artifact)


def dependencies_met(task_id: str, unit: dict[str, Any]) -> bool:
    for dep in unit.get("dependencies") or []:
        token = str(dep)
        if token.startswith("artifact:"):
            if not work_ledger.artifact_cells_done(task_id, token.split(":", 1)[1]):
                return False
        elif token.startswith("unit:"):
            other = work_ledger.get_unit(task_id, token.split(":", 1)[1])
            if other is None or other["status"] not in work_ledger.TERMINAL_UNIT_STATUSES:
                return False
    return True


def progress(task_id: str) -> dict[str, Any]:
    """调度侧进度快照（与 UI 展示同源，全部数值来自账本）。

    只做聚合计数与一次轻量查询：`get_task` 会被前端与评测轮询，不能在这里
    逐个单元解析依赖（300 格任务会变成数百次查询）。
    """
    units = work_ledger.unit_progress(task_id)
    cells = work_ledger.cell_progress(task_id)
    delivery = work_ledger.delivery_progress(task_id)
    current = None
    for statuses in (("leased", "extracting", "staged", "verified"), ("pending",)):
        rows = work_ledger.list_units(task_id, statuses=statuses, limit=1)
        if rows:
            current = rows[0]
            break
    return {
        "units": units,
        "cells": cells,
        "delivery": {
            # status 已包含「结构完整但答案格未终态仍为 partial」的语义，
            # 这里不再用文件级 completeCount 单独判 complete。
            "status": delivery["status"],
            "structuralStatus": delivery.get("structuralStatus"),
            "workDone": delivery.get("workDone"),
            "terminalRatio": delivery.get("terminalRatio"),
            "cellsPending": delivery.get("cellsPending"),
            "artifactCount": delivery["artifactCount"],
            "completeCount": delivery["completeCount"],
            "incomplete": delivery["incomplete"],
            "rowsExpected": delivery["rowsExpected"],
            "rowsCommitted": delivery["rowsCommitted"],
            "rowCoverage": delivery["rowCoverage"],
        },
        "currentUnit": {
            "workUnitId": current["workUnitId"],
            "scopeType": current["scopeType"],
            "attempts": current["attempts"],
        } if current else None,
    }
