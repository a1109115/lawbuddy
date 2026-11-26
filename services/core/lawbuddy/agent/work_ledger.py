"""持久化工作账本（LB-HARNESS-2026-01 §4.3 / B-01）。

把任务的工作全集、答案格、交付物登记与运行时计数从模型上下文挪进数据库：

- `agent_work_units`：最小可调度工作项（文档 × 问题、被告 × 字段等）；
- `agent_cell_results`：交付表的一格，带来源文档/版本/页码/原文短引与证据状态；
- `agent_artifacts`：主/派生交付物的行覆盖与原子写入结果（只记哈希与路径）；
- `agent_runtime_state`：运行时版本钉住与预算计数。

硬约束：
1. **无淘汰**：不做「只保留最近 N 条」，1,865 页 / 300 格以上也完整保留（HAR-T-002）。
2. **幂等**：工作单元按 `task_id + work_unit_id`、答案格按
   `task_id + artifact_name + cell_key` upsert，重复提交不新增行也不重复副作用。
3. **事务化**：每次状态迁移都在单个事务内完成，同时更新 `updated_at` 与尝试次数。
4. 只存 ID、状态、计数与原文短引；不写案件原文全文，不写密钥、个人信息。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..storage import get_db
from ..workspace import now_iso

UNIT_STATUSES = (
    "pending", "leased", "extracting", "staged", "verified", "committed",
    "needs_human", "blocked",
)
TERMINAL_UNIT_STATUSES = ("committed", "needs_human", "blocked")
CELL_STATUSES = ("pending", "committed", "needs_human", "blocked", "stale")
TERMINAL_CELL_STATUSES = ("committed", "needs_human", "blocked")
ARTIFACT_STATUSES = ("pending", "partial", "complete", "invalid")
RUNTIME_VERSIONS = ("v1", "v2", "v3", "legacy")

# 允许的状态迁移。失败重试（staged/verified → leased）回到租约态重新抽取；
# 任何迁移都不得跳过 extracting 直接 committed（除 materialize 类确定性写入）。
_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("leased", "blocked", "needs_human"),
    "leased": ("extracting", "staged", "pending", "blocked", "needs_human"),
    "extracting": ("staged", "leased", "blocked", "needs_human"),
    "staged": ("verified", "leased", "committed", "blocked", "needs_human"),
    "verified": ("committed", "leased", "needs_human", "blocked"),
    "committed": ("leased",),
    "needs_human": ("leased", "committed"),
    "blocked": ("leased",),
}


class LedgerError(RuntimeError):
    pass


class InvalidTransition(LedgerError):
    """非法状态迁移：账本只接受既定状态机，防止状态被静默伪造。"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "null")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return fallback if value is None else value


# ---------------------------------------------------------------------------
# 工作单元
# ---------------------------------------------------------------------------

def seed_units(task_id: str, work_units: Iterable[dict[str, Any]]) -> int:
    """登记契约编译出的工作全集（幂等）：已存在的工作单元不改状态、不重置尝试次数。

    返回新插入的单元数。重复调用（恢复、重复编译）不会产生重复行。
    """
    db = get_db()
    ts = now_iso()
    inserted = 0
    with db.transaction() as conn:
        for unit in work_units:
            unit_id = str(unit.get("workUnitId") or "").strip()
            if not unit_id:
                continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO agent_work_units(
                    task_id, work_unit_id, scope_type, scope_json, dependencies_json,
                    status, priority, attempts, max_attempts, cost_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, ?, '{}', ?, ?)
                """,
                (
                    task_id, unit_id,
                    str(unit.get("scopeType") or "artifact"),
                    _dumps(unit.get("scope") or {}),
                    _dumps([str(d) for d in (unit.get("dependencies") or [])]),
                    int(unit.get("priority") or 100),
                    int(unit.get("maxAttempts") or 3),
                    ts, ts,
                ),
            )
            inserted += cur.rowcount or 0
    return inserted


def seed_cells(task_id: str, cells: Iterable[dict[str, Any]]) -> int:
    """预登记契约答案格为 `pending`（幂等）：用于让「未处理格」可见且可计数。

    不预登记时，`artifact_cells_done` 在还没有任何格子时会把交付物判为"已完成"，
    物化单元会被提前调度（HAR-T-005 之前就写出空表）。预登记后，只有契约声明的
    全部格子都进入终态，物化依赖才成立。
    """
    db = get_db()
    ts = now_iso()
    inserted = 0
    with db.transaction() as conn:
        for cell in cells:
            artifact = str(cell.get("artifact") or "").strip()
            cell_key = str(cell.get("cellKey") or "").strip()
            if not artifact or not cell_key:
                continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO agent_cell_results(
                    task_id, artifact_name, cell_key, value_json, status, updated_at)
                VALUES (?, ?, ?, 'null', 'pending', ?)
                """,
                (task_id, artifact, cell_key, ts),
            )
            inserted += cur.rowcount or 0
    return inserted


def list_units(
    task_id: str,
    *,
    statuses: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """读取工作单元（默认全部，不淘汰）。按优先级、更新时间排序。"""
    db = get_db()
    sql = "SELECT * FROM agent_work_units WHERE task_id = ?"
    params: list[Any] = [task_id]
    wanted = [s for s in (statuses or []) if s]
    if wanted:
        sql += " AND status IN (" + ",".join("?" for _ in wanted) + ")"
        params.extend(wanted)
    sql += " ORDER BY priority ASC, updated_at ASC, work_unit_id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [_unit_row(r) for r in db.query(sql, tuple(params))]


def _unit_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "taskId": row["task_id"],
        "workUnitId": row["work_unit_id"],
        "scopeType": row["scope_type"],
        "scope": _loads(row["scope_json"], {}),
        "dependencies": _loads(row["dependencies_json"], []),
        "status": row["status"],
        "priority": int(row["priority"] or 0),
        "attempts": int(row["attempts"] or 0),
        "maxAttempts": int(row["max_attempts"] or 0),
        "cost": _loads(row["cost_json"], {}),
        "outputRef": row["output_ref"],
        "lastErrorCode": row["last_error_code"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get_unit(task_id: str, work_unit_id: str) -> dict[str, Any] | None:
    row = get_db().query_one(
        "SELECT * FROM agent_work_units WHERE task_id = ? AND work_unit_id = ?",
        (task_id, work_unit_id),
    )
    return _unit_row(row) if row else None


def lease_unit(task_id: str, work_unit_id: str) -> dict[str, Any] | None:
    """租约：pending/可重试 → leased 并 +1 尝试次数；已达 maxAttempts 返回 None。

    同一单元并发租约只有一次生效（条件更新），避免重复执行同一工作单元。
    """
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT status, attempts, max_attempts FROM agent_work_units "
            "WHERE task_id = ? AND work_unit_id = ?",
            (task_id, work_unit_id),
        ).fetchone()
        if row is None:
            return None
        attempts = int(row["attempts"] or 0)
        if attempts >= int(row["max_attempts"] or 0):
            return None
        cur = conn.execute(
            """
            UPDATE agent_work_units
               SET status = 'leased', attempts = attempts + 1, updated_at = ?
             WHERE task_id = ? AND work_unit_id = ? AND status = ? AND attempts = ?
            """,
            (ts, task_id, work_unit_id, row["status"], attempts),
        )
        if not (cur.rowcount or 0):
            return None
    return get_unit(task_id, work_unit_id)


def transition_unit(
    task_id: str,
    work_unit_id: str,
    status: str,
    *,
    error_code: str | None = None,
    output_ref: str | None = None,
    cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """状态迁移（事务内校验）：非法迁移抛 `InvalidTransition`，不静默改状态。"""
    if status not in UNIT_STATUSES:
        raise LedgerError(f"未知工作单元状态：{status}")
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT status, cost_json FROM agent_work_units "
            "WHERE task_id = ? AND work_unit_id = ?",
            (task_id, work_unit_id),
        ).fetchone()
        if row is None:
            raise LedgerError(f"工作单元不存在：{work_unit_id}")
        current = str(row["status"])
        if status != current and status not in _ALLOWED_TRANSITIONS.get(current, ()):
            raise InvalidTransition(f"{current} → {status} 不是合法迁移（{work_unit_id}）")
        merged_cost = _loads(row["cost_json"], {})
        if isinstance(cost, dict):
            for key, value in cost.items():
                if isinstance(value, (int, float)):
                    merged_cost[key] = float(merged_cost.get(key) or 0) + float(value)
                else:
                    merged_cost[key] = value
        conn.execute(
            """
            UPDATE agent_work_units
               SET status = ?, last_error_code = ?, output_ref = COALESCE(?, output_ref),
                   cost_json = ?, updated_at = ?
             WHERE task_id = ? AND work_unit_id = ?
            """,
            (status, error_code, output_ref, _dumps(merged_cost), ts, task_id, work_unit_id),
        )
    unit = get_unit(task_id, work_unit_id)
    if unit is None:  # pragma: no cover - 事务内已校验存在
        raise LedgerError(f"工作单元不存在：{work_unit_id}")
    return unit


def advance_unit(
    task_id: str,
    work_unit_id: str,
    target: str,
    *,
    error_code: str | None = None,
    output_ref: str | None = None,
) -> dict[str, Any]:
    """把工作单元推进到目标状态（按状态机最短合法路径逐级迁移）。

    答案格回写只关心最终结论，而账本不接受跳级（`pending → committed` 非法）。
    这里按 `_ALLOWED_TRANSITIONS` 求最短路径补齐中间态（leased / staged），
    每一步仍走 `transition_unit` 的真实校验：目标不可达就抛 `InvalidTransition`，
    不会伪造状态。`error_code` / `output_ref` 只落在最后一步，避免中间态被污染。
    """
    if target not in UNIT_STATUSES:
        raise LedgerError(f"未知工作单元状态：{target}")
    unit = get_unit(task_id, work_unit_id)
    if unit is None:
        raise LedgerError(f"工作单元不存在：{work_unit_id}")
    for _ in range(len(UNIT_STATUSES) + 2):
        current = str(unit["status"])
        if current == target:
            return unit
        path = _shortest_transition(current, target)
        if len(path) < 2:
            raise InvalidTransition(f"{current} → {target} 无合法路径（{work_unit_id}）")
        step = path[1]
        final = step == target
        unit = transition_unit(
            task_id, work_unit_id, step,
            error_code=error_code if final else None,
            output_ref=output_ref if final else None,
        )
    raise InvalidTransition(f"{target} 未能在有限步内到达（{work_unit_id}）")


def _shortest_transition(start: str, target: str) -> list[str]:
    """状态机最短路径（BFS，含起点）。不可达返回空列表。"""
    if start == target:
        return [start]
    seen = {start}
    queue: list[list[str]] = [[start]]
    while queue:
        path = queue.pop(0)
        for nxt in _ALLOWED_TRANSITIONS.get(path[-1], ()):
            if nxt in seen:
                continue
            if nxt == target:
                return [*path, nxt]
            seen.add(nxt)
            queue.append([*path, nxt])
    return []


def unit_progress(task_id: str) -> dict[str, Any]:
    """工作单元进度汇总（UI 与评测共用）：只统计状态与数量，不含原文。"""
    rows = get_db().query(
        "SELECT status, COUNT(*) AS n FROM agent_work_units WHERE task_id = ? GROUP BY status",
        (task_id,),
    )
    counts = {str(r["status"]): int(r["n"] or 0) for r in rows}
    total = sum(counts.values())
    done = sum(counts.get(s, 0) for s in TERMINAL_UNIT_STATUSES)
    return {
        "total": total,
        "committed": counts.get("committed", 0),
        "needsHuman": counts.get("needs_human", 0),
        "blocked": counts.get("blocked", 0),
        "pending": counts.get("pending", 0),
        "inProgress": counts.get("leased", 0) + counts.get("extracting", 0)
                      + counts.get("staged", 0) + counts.get("verified", 0),
        "terminal": done,
        "terminalRatio": round(done / total, 4) if total else 0.0,
        "byStatus": counts,
    }


def next_unit(task_id: str, *, exclude: Iterable[str] = ()) -> dict[str, Any] | None:
    """取下一个可调度单元：依赖已满足的 pending，其次可重试的失败单元。

    优先级规则（§4.5）：解除他人阻塞 → 未尝试 → 待修复 → 可重试失败。
    """
    excluded = set(exclude)
    db = get_db()
    rows = db.query(
        """
        SELECT work_unit_id FROM agent_work_units
         WHERE task_id = ? AND status IN ('pending', 'leased', 'needs_human', 'blocked')
           AND attempts < max_attempts
         ORDER BY priority ASC, attempts ASC, updated_at ASC
        """,
        (task_id,),
    )
    terminal_by_artifact: dict[str, bool] = {}
    for row in rows:
        unit_id = str(row["work_unit_id"])
        if unit_id in excluded:
            continue
        unit = get_unit(task_id, unit_id)
        if unit is None:
            continue
        if _dependencies_met(task_id, unit, terminal_by_artifact):
            return unit
    return None


def _dependencies_met(
    task_id: str, unit: dict[str, Any], cache: dict[str, bool],
) -> bool:
    """依赖判定：`artifact:<name>` 要求该交付物全部答案格已进入终态。"""
    deps = [str(d) for d in (unit.get("dependencies") or []) if str(d)]
    if not deps:
        return True
    for dep in deps:
        if dep.startswith("artifact:"):
            name = dep.split(":", 1)[1]
            if name not in cache:
                cache[name] = artifact_cells_done(task_id, name)
            if not cache[name]:
                return False
        elif dep.startswith("unit:"):
            other = get_unit(task_id, dep.split(":", 1)[1])
            if other is None or other["status"] not in TERMINAL_UNIT_STATUSES:
                return False
    return True


def artifact_cells_done(task_id: str, artifact_name: str) -> bool:
    """该交付物的答案格是否全部进入终态（无格子时视为不阻塞）。"""
    row = get_db().query_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status IN ('committed','needs_human','blocked') THEN 1 ELSE 0 END) AS done
          FROM agent_cell_results WHERE task_id = ? AND artifact_name = ?
        """,
        (task_id, artifact_name),
    )
    if row is None or not int(row["total"] or 0):
        return True
    return int(row["done"] or 0) >= int(row["total"] or 0)


# ---------------------------------------------------------------------------
# 答案格
# ---------------------------------------------------------------------------

def _active_version_id(document_id: str | None) -> str | None:
    """按文档 ID 解析当前活动版本；解析不到时返回 None。

    模型/文件同步只保证给出 documentId 时，版本 ID 由系统补齐，避免
    `version_id` 长期为空导致换版后无法判定旧答案（HAR-T-012）。
    """
    if not document_id:
        return None
    row = get_db().query_one(
        "SELECT active_version_id FROM documents WHERE id = ?", (document_id,))
    value = str((row or {}).get("active_version_id") or "").strip()
    return value or None


def commit_cell(
    task_id: str,
    artifact_name: str,
    cell_key: str,
    *,
    value: Any = None,
    status: str = "committed",
    document_id: str | None = None,
    version_id: str | None = None,
    page_no: int | None = None,
    quote: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """幂等提交一个答案格：同一 cell_key 重复提交只更新，不新增行。

    `committed` 必须带可定位来源（文档 + 页码 + 原文短引），否则拒绝——
    这是「结论必须基于原文」的硬门禁在存储层的最小实现；无来源时应显式
    以 `needs_human` 提交并给出 reason_code。
    """
    if status not in CELL_STATUSES:
        raise LedgerError(f"未知答案格状态：{status}")
    if status == "committed" and not (document_id and page_no):
        raise LedgerError("committed 必须提供 documentId 与 pageNo（结论须可回溯原文）")
    if status == "committed" and document_id and not version_id:
        version_id = _active_version_id(document_id)
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO agent_cell_results(
                task_id, artifact_name, cell_key, value_json, status, document_id,
                version_id, page_no, quote, reason_code, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, artifact_name, cell_key) DO UPDATE SET
                value_json = excluded.value_json,
                status = excluded.status,
                document_id = excluded.document_id,
                version_id = excluded.version_id,
                page_no = excluded.page_no,
                quote = excluded.quote,
                reason_code = excluded.reason_code,
                updated_at = excluded.updated_at
            """,
            (
                task_id, artifact_name, cell_key,
                _dumps(value), status, document_id, version_id,
                int(page_no) if page_no else None,
                str(quote or "")[:500] or None,
                reason_code, ts,
            ),
        )
    return {
        "artifactId": artifact_name,
        "cellKey": cell_key,
        "status": status,
        "value": value,
        "documentId": document_id,
        "versionId": version_id,
        "pageNo": page_no,
        "reasonCode": reason_code,
        "updatedAt": ts,
    }


def list_cells(
    task_id: str,
    *,
    artifact_name: str | None = None,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    db = get_db()
    sql = "SELECT * FROM agent_cell_results WHERE task_id = ?"
    params: list[Any] = [task_id]
    if artifact_name:
        sql += " AND artifact_name = ?"
        params.append(artifact_name)
    wanted = [s for s in (statuses or []) if s]
    if wanted:
        sql += " AND status IN (" + ",".join("?" for _ in wanted) + ")"
        params.extend(wanted)
    sql += " ORDER BY artifact_name ASC, cell_key ASC"
    return [_cell_row(r) for r in db.query(sql, tuple(params))]


def _cell_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifactId": row["artifact_name"],
        "cellKey": row["cell_key"],
        "value": _loads(row["value_json"], None),
        "status": row["status"],
        "documentId": row["document_id"],
        "versionId": row["version_id"],
        "pageNo": row["page_no"],
        "quote": row["quote"],
        "reasonCode": row["reason_code"],
        "updatedAt": row["updated_at"],
    }


def cell_progress(task_id: str, artifact_name: str | None = None) -> dict[str, Any]:
    sql = "SELECT status, COUNT(*) AS n FROM agent_cell_results WHERE task_id = ?"
    params: list[Any] = [task_id]
    if artifact_name:
        sql += " AND artifact_name = ?"
        params.append(artifact_name)
    sql += " GROUP BY status"
    rows = get_db().query(sql, tuple(params))
    counts = {str(r["status"]): int(r["n"] or 0) for r in rows}
    total = sum(counts.values())
    return {
        "total": total,
        "committed": counts.get("committed", 0),
        "needsHuman": counts.get("needs_human", 0),
        "blocked": counts.get("blocked", 0),
        "stale": counts.get("stale", 0),
        "pending": counts.get("pending", 0),
        "byStatus": counts,
    }


def invalidate_by_version(task_id: str, document_id: str, stale_version_id: str) -> int:
    """文档换版后把旧版本的已提交答案标 `stale`（B-02 §4.7）。

    只降级「来源版本 = 旧版本」的格子；已人工核实（needs_human）与无来源格子
    不动。返回被标 stale 的格子数。
    """
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        cur = conn.execute(
            """
            UPDATE agent_cell_results
               SET status = 'stale', reason_code = 'VERSION_CHANGED', updated_at = ?
             WHERE task_id = ? AND document_id = ? AND version_id = ? AND status = 'committed'
            """,
            (ts, task_id, document_id, stale_version_id),
        )
        return int(cur.rowcount or 0)


def invalidate_changed_versions(
    task_id: str,
    active_versions: dict[str, str],
) -> dict[str, int]:
    """按当前活动版本批量失效旧答案与对应工作单元（B-02 §4.7 / HAR-T-012）。

    - 已提交答案格的 `version_id` 与文档当前活动版本不一致 → `stale`；
    - 对应的工作单元从 `committed` 退回 `leased`，允许调度器重新取证；
    - 物化/派生单元在其依赖的交付物出现失效格时同样退回 `leased`。

    返回 `{"cells": 失效格数, "units": 退回单元数}`。重复执行幂等：已经
    `stale` 的格子不会再次计数。
    """
    db = get_db()
    rows = db.query(
        """
        SELECT DISTINCT document_id, version_id
          FROM agent_cell_results
         WHERE task_id = ? AND status = 'committed'
           AND document_id IS NOT NULL AND version_id IS NOT NULL
        """,
        (task_id,),
    )
    cells = 0
    for row in rows:
        document_id = str(row["document_id"])
        stale_version = str(row["version_id"])
        active = str(active_versions.get(document_id) or "")
        # 文档不在当前任务语料中（删除/移出范围）与版本不一致同样失效
        if active != stale_version:
            cells += invalidate_by_version(task_id, document_id, stale_version)
    units = 0
    if not cells:
        return {"cells": 0, "units": 0}
    stale_cells = list_cells(task_id, statuses=("stale",))
    stale_pairs = {(c["artifactId"], c["cellKey"]) for c in stale_cells}
    stale_artifacts = {artifact for artifact, _cell in stale_pairs}
    for unit in list_units(task_id):
        if unit["status"] != "committed":
            continue
        scope = unit.get("scope") or {}
        artifact = str(scope.get("artifact") or "")
        cell_key = str(scope.get("cellKey") or "")
        matched = (artifact, cell_key) in stale_pairs or (
            artifact in stale_artifacts
            and unit["scopeType"] in ("materialize", "derive", "artifact")
        )
        if not matched:
            continue
        try:
            transition_unit(
                task_id, unit["workUnitId"], "leased", error_code="VERSION_CHANGED")
            units += 1
        except InvalidTransition:
            continue
    return {"cells": cells, "units": units}


# ---------------------------------------------------------------------------
# 交付物登记
# ---------------------------------------------------------------------------

def register_artifact(
    task_id: str,
    name: str,
    *,
    status: str = "pending",
    rows_expected: int = 0,
    rows_committed: int = 0,
    content_sha256: str | None = None,
    storage_path: str | None = None,
    schema_version: int = 1,
) -> None:
    """登记交付物写入结果（幂等 upsert）：只记哈希与相对路径，不存内容。"""
    if status not in ARTIFACT_STATUSES:
        raise LedgerError(f"未知交付物状态：{status}")
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO agent_artifacts(
                task_id, name, schema_version, status, rows_expected, rows_committed,
                content_sha256, storage_path, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, name) DO UPDATE SET
                schema_version = excluded.schema_version,
                status = excluded.status,
                rows_expected = excluded.rows_expected,
                rows_committed = excluded.rows_committed,
                content_sha256 = excluded.content_sha256,
                storage_path = excluded.storage_path,
                updated_at = excluded.updated_at
            """,
            (task_id, name, int(schema_version), status, int(rows_expected),
             int(rows_committed), content_sha256, storage_path, ts),
        )


def list_artifacts(task_id: str) -> list[dict[str, Any]]:
    rows = get_db().query(
        "SELECT * FROM agent_artifacts WHERE task_id = ? ORDER BY name ASC", (task_id,)
    )
    return [
        {
            "name": r["name"],
            "schemaVersion": int(r["schema_version"] or 1),
            "status": r["status"],
            "rowsExpected": int(r["rows_expected"] or 0),
            "rowsCommitted": int(r["rows_committed"] or 0),
            "contentSha256": r["content_sha256"],
            "storagePath": r["storage_path"],
            "updatedAt": r["updated_at"],
        }
        for r in rows
    ]


def delivery_progress(task_id: str) -> dict[str, Any]:
    """交付进度汇总：文件级状态 + 行覆盖 + 工作终态（供 UI 与完成守门共用）。

    `status` 只表达**结构完整性**（文件存在/可解析/行全集齐全），
    `workDone` 表达**工作是否真的做完**（答案格是否全部进入终态）。
    两者必须分开：结构完整但 288 格全为 pending 的任务不能显示完成
    （方案 §1.1-7：运行状态与真实交付状态不得混淆）。
    """
    arts = list_artifacts(task_id)
    total_expected = sum(a["rowsExpected"] for a in arts)
    total_committed = sum(a["rowsCommitted"] for a in arts)
    complete = sum(1 for a in arts if a["status"] == "complete")
    cells = cell_progress(task_id)
    terminal = cells["committed"] + cells["needsHuman"] + cells["blocked"]
    terminal_ratio = round(terminal / cells["total"], 4) if cells["total"] else 1.0
    structural = ("complete" if arts and complete == len(arts)
                  else ("partial" if arts else "pending"))
    return {
        "artifacts": arts,
        "artifactCount": len(arts),
        "completeCount": complete,
        "incomplete": [a["name"] for a in arts if a["status"] != "complete"],
        "rowsExpected": total_expected,
        "rowsCommitted": total_committed,
        "rowCoverage": round(total_committed / total_expected, 4) if total_expected else 0.0,
        # 结构完整 ≠ 工作完成：有未终态答案格时整体仍为 partial。
        "status": structural if structural != "complete" or terminal_ratio >= 1.0
                  else "partial",
        "structuralStatus": structural,
        "workDone": terminal_ratio >= 1.0,
        "terminalRatio": terminal_ratio,
        "cellsPending": cells["pending"],
    }


# ---------------------------------------------------------------------------
# 运行时状态（运行时版本钉住 + 计数）
# ---------------------------------------------------------------------------

def ensure_runtime(task_id: str, runtime_version: str = "v2") -> dict[str, Any]:
    """初始化运行时状态（幂等）；已存在时沿用**已钉住的版本**，不跨版本改写。"""
    if runtime_version not in RUNTIME_VERSIONS:
        runtime_version = "v1"
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_runtime_state(
                task_id, runtime_version, schema_version, started_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (task_id, runtime_version, ts, ts),
        )
    return get_runtime(task_id) or {}


def get_runtime(task_id: str) -> dict[str, Any] | None:
    row = get_db().query_one(
        "SELECT * FROM agent_runtime_state WHERE task_id = ?", (task_id,)
    )
    if row is None:
        return None
    return {
        "runtimeVersion": row["runtime_version"],
        "schemaVersion": int(row["schema_version"] or 1),
        "roundsUsed": int(row["rounds_used"] or 0),
        "toolCallsUsed": int(row["tool_calls_used"] or 0),
        "modelCallsUsed": int(row["model_calls_used"] or 0),
        "promptTokens": int(row["prompt_tokens"] or 0),
        "completionTokens": int(row["completion_tokens"] or 0),
        "exploreRoundsUsed": int(row["explore_rounds_used"] or 0),
        "startedAt": row["started_at"],
        "state": _loads(row["state_json"], {}),
        "updatedAt": row["updated_at"],
    }


def save_runtime(task_id: str, counters: dict[str, Any], state: dict[str, Any] | None = None) -> None:
    """回写计数与恢复点（幂等）：用于检查点旁写，保证预算不因续跑重置。"""
    mapping = {
        "roundsUsed": "rounds_used",
        "toolCallsUsed": "tool_calls_used",
        "modelCallsUsed": "model_calls_used",
        "promptTokens": "prompt_tokens",
        "completionTokens": "completion_tokens",
        "exploreRoundsUsed": "explore_rounds_used",
    }
    db = get_db()
    ts = now_iso()
    ensure_runtime(task_id)
    with db.transaction() as conn:
        current = conn.execute(
            "SELECT * FROM agent_runtime_state WHERE task_id = ?", (task_id,)
        ).fetchone()
        if current is None:  # pragma: no cover - ensure_runtime 已插入
            return
        values = {
            col: max(int(current[col] or 0), int(counters.get(key) or 0))
            for key, col in mapping.items()
        }
        state_json = (
            _dumps(state) if isinstance(state, dict)
            else str(current["state_json"] or "{}")
        )
        conn.execute(
            f"""
            UPDATE agent_runtime_state
               SET {", ".join(f"{col} = ?" for col in mapping.values())},
                   state_json = ?, updated_at = ?
             WHERE task_id = ?
            """,
            (*values.values(), state_json, ts, task_id),
        )


def delete_task_state(task_id: str) -> None:
    """任务删除时清理账本（表本身已 ON DELETE CASCADE，这里兜底手工清理）。"""
    db = get_db()
    with db.transaction() as conn:
        for table in ("agent_cell_results", "agent_work_units", "agent_artifacts"):
            conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM agent_runtime_state WHERE task_id = ?", (task_id,))
