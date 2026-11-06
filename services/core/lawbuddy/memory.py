"""记忆管理器（技术方案 §8.1 / DEV-AGT-007，测试 M-001~M-006）。

分层与隔离：
- `session` 会话工作记忆：任务期内的计划、观察与未决问题；任务进入终态后清理；
- `case` 案件记忆：人物、时间线、证据目录与用户更正；随案件保存，**强制案件隔离**；
- `user` 用户偏好：语言、格式、常用模型与默认权限；跨案件，但 kind 限定为偏好类且
  禁止携带 case_id —— 结构性杜绝案件事实进入全局偏好（M-001）；
- 知识库层（法规/案例/模板）按当前范围暂不实现。

约束：
- 所有召回查询强制追加 case_id 过滤，调用方（含模型）不能覆盖（M-001/F-002）；
- 案件记忆保存来源文档、版本、页码、原文片段与状态（M-002）；
- 来源文档被删除或替换后，相关记忆进入 `stale`，不再作为有效事实召回（M-003）；
- 用户更正保留原值（旧行转 `superseded` 并留存），新值生效（M-004）；
- 删除同时清理 SQLite 状态位与 FTS 索引，不再可召回（M-005）；
- 无可靠来源（无引证）的报告条目不入库，保持“没有来源不进记忆”。
"""

from __future__ import annotations

import uuid
from typing import Any

from .storage import get_db
from .workspace import now_iso

SCOPES = ("session", "case", "user")

# 用户偏好允许的 kind（M-001：案件事实不得进入全局偏好）
USER_KINDS = ("preference", "language", "format", "model", "permission")

# 案件/会话记忆允许的 kind
CASE_KINDS = ("person", "timeline", "evidence", "fact", "dispute", "correction", "note", "observation")

# 案件事实类（写入时必须带可核验的原文来源，05-阅卷Agent升级方案 §6.3）。
# note/observation 承载计划、待核实问题与工作记录，correction 承载用户更正，
# 不强制原文来源，但同样不能伪装成“已由原文支持”的既成事实。
FACT_KINDS = ("person", "timeline", "evidence", "fact", "dispute")

ACTIVE = "active"


class MemoryError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def _validate_case_source(case_id: str, src: dict[str, Any]) -> dict[str, Any]:
    """校验案件事实类记忆的原文来源，返回规范化后的来源（05 方案 §6.3）。

    守门规则（fail-closed，与 memory_write 工具说明一致）：
    - 必须给出 documentId、pageNo 与 quote；
    - 文档必须属于当前案件或显式可读的关联案件；
    - 版本缺省时取文档当前活动版本；显式给出的版本必须是该文档的版本；
    - 页码必须存在，quote 必须能通过 citations.verify_quote 的规范化子串校验。
    """
    from .citations import verify_quote
    from .documents import get_pages

    doc_id = str(src.get("documentId") or "").strip()
    quote = str(src.get("quote") or "").strip()
    try:
        page_no = int(src.get("pageNo"))
    except (TypeError, ValueError):
        raise MemoryError("案件事实记忆必须给出具体页码（pageNo）") from None
    if not doc_id:
        raise MemoryError("案件事实记忆必须给出原文来源文档（documentId）")
    if not quote:
        raise MemoryError("案件事实记忆必须给出逐字原文片段（quote），否则只能记为 note/待核实")

    from .cases import readable_case_ids

    db = get_db()
    doc = db.query_one(
        "SELECT id, case_id, active_version_id FROM documents WHERE id = ?", (doc_id,)
    )
    if doc is None:
        raise MemoryError("来源文档不存在，不能作为案内事实写入记忆")
    if str(doc["case_id"]) not in readable_case_ids(case_id):
        raise MemoryError("来源文档不属于当前案件可读范围，拒绝跨案件写入事实记忆")

    version_id = str(src.get("versionId") or "").strip() or str(doc["active_version_id"] or "")
    if not version_id:
        raise MemoryError("来源文档尚未解析出版本，不能作为案内事实写入记忆")
    if version_id != str(doc["active_version_id"] or ""):
        owned = db.query_one(
            "SELECT id FROM document_versions WHERE id = ? AND document_id = ?",
            (version_id, doc_id),
        )
        if owned is None:
            raise MemoryError("来源版本不属于该文档，拒绝写入事实记忆")

    pages = get_pages(doc_id)
    if not any(int(p.get("pageNo") or 0) == page_no for p in pages):
        raise MemoryError(f"来源页码超出文档范围（共 {len(pages)} 页），不能写入事实记忆")
    from .citations import normalize_text

    if not verify_quote(normalize_text(quote), version_id, page_no):
        raise MemoryError("原文片段与该页内容不匹配（仅允许空白/断行差异），不能作为已支持事实写入记忆")

    return {"documentId": doc_id, "versionId": version_id, "pageNo": page_no, "quote": quote}


def remember(
    *,
    scope: str,
    kind: str,
    key: str,
    value: str,
    case_id: str | None = None,
    task_id: str | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """写入一条记忆（scope/kind 守门 + 来源校验 + FTS 索引 + 审计）。

    - scope=user：kind 必须在 USER_KINDS 内，且不得携带 case_id（M-001）；
    - scope=case/session：必须携带 case_id，kind 必须在 CASE_KINDS 内；
    - 案件事实类 kind（FACT_KINDS）必须带可核验原文来源，无来源或无引文拒绝写入。
    """
    db = get_db()
    scope = (scope or "").strip()
    kind = (kind or "").strip()
    key = (key or "").strip()
    value = (value or "").strip()
    if scope not in SCOPES:
        raise MemoryError(f"无效记忆层级：{scope}")
    if not key or not value:
        raise MemoryError("记忆键与内容不能为空")

    if scope == "user":
        if case_id:
            raise MemoryError("用户偏好层级不得携带案件标识（M-001：案件事实不能进入全局偏好）")
        if kind not in USER_KINDS:
            raise MemoryError(f"用户偏好仅允许 {', '.join(USER_KINDS)}，拒绝写入 {kind}")
    else:
        if not case_id:
            raise MemoryError(f"{scope} 层记忆必须携带案件标识")
        if kind not in CASE_KINDS:
            raise MemoryError(f"非法记忆类型：{kind}")

    src = source or {}
    if any(src.get(k) for k in ("documentId", "pageNo", "quote")):
        try:
            src = _validate_case_source(str(case_id), src)
        except MemoryError:
            if kind in FACT_KINDS:
                raise
            # note/observation 允许无来源；但给出的来源无法核验时不落引用，
            # 以免工作记录在后续召回中被当成“已由原文支持”的事实。
            src = {}
    elif kind in FACT_KINDS:
        raise MemoryError(
            f"{kind} 属于案件事实，必须提供 documentId + pageNo + quote；"
            "无来源的推测请记为 note 或列入待核实"
        )
    memory_id = str(uuid.uuid4())
    ts = now_iso()
    workspace_id = db.query_one("SELECT workspace_id FROM cases WHERE id = ?", (case_id,)) if case_id else None
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO memories(id, workspace_id, scope, case_id, task_id, kind, key_text, value_text,
                                 source_document_id, source_version_id, source_page_no, source_quote,
                                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                memory_id, workspace_id["workspace_id"] if workspace_id else None, scope, case_id, task_id,
                kind, key, value,
                src.get("documentId"), src.get("versionId"), src.get("pageNo"), src.get("quote"),
                ts, ts,
            ),
        )
        _index(conn, memory_id, case_id, scope, key, value)
        from . import audit

        audit.append(
            conn, action="memory.write", resource_type="memory", resource_id=memory_id, case_id=case_id,
            metadata={"scope": scope, "kind": kind, "key": key, "hasSource": bool(src.get("documentId"))},
        )
    return {"id": memory_id, "scope": scope, "kind": kind, "key": key, "value": value, "status": ACTIVE}


def _index(conn: Any, memory_id: str, case_id: str | None, scope: str, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO memories_fts(memory_id, case_id, scope, text) VALUES (?, ?, ?, ?)",
        (memory_id, case_id, scope, f"{key} {value}"),
    )


# ---------------------------------------------------------------------------
# 召回（全部强制 case_id 过滤，模型不可覆盖）
# ---------------------------------------------------------------------------

def _row_to_entry(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "scope": row["scope"], "caseId": row["case_id"], "taskId": row["task_id"],
        "kind": row["kind"], "key": row["key_text"], "value": row["value_text"],
        "source": {
            "documentId": row["source_document_id"],
            "versionId": row["source_version_id"],
            "pageNo": row["source_page_no"],
            "quote": row["source_quote"],
        } if row["source_document_id"] else None,
        "status": row["status"],
        "supersededBy": row["superseded_by"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def recall(
    *,
    case_id: str,
    query: str,
    limit: int = 10,
    scopes: tuple[str, ...] = ("case",),
    extra_case_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """按查询召回记忆（仅 active；强制案件过滤；trigram FTS，短查询回退 LIKE）。

    extra_case_ids 仅用于文书生成任务显式关联的案件（只读共享），
    其他调用保持单案件召回。
    """
    query = (query or "").strip()
    if not query:
        return []
    if "case" in scopes and not case_id:
        raise MemoryError("召回案件记忆必须指定案件")
    ids = [case_id, *[cid for cid in extra_case_ids if cid and cid != case_id]]
    in_clause = ",".join("?" for _ in ids)
    db = get_db()
    placeholders = ",".join("?" for _ in scopes)
    if len(query) < 3:
        # trigram 分词对短串无命中，回退键/值包含匹配
        rows = db.query(
            f"""
            SELECT * FROM memories
            WHERE status = 'active' AND scope IN ({placeholders}) AND case_id IN ({in_clause})
              AND (key_text LIKE ? OR value_text LIKE ?)
            ORDER BY updated_at DESC LIMIT ?
            """,
            (*scopes, *ids, f"%{query}%", f"%{query}%", limit),
        )
        return [_row_to_entry(r) for r in rows]

    hits = db.query(
        f"""
        SELECT m.* FROM memories_fts f
        JOIN memories m ON m.id = f.memory_id
        WHERE memories_fts MATCH ? AND f.case_id IN ({in_clause}) AND f.scope IN ({placeholders})
          AND m.status = 'active'
        ORDER BY bm25(memories_fts) LIMIT ?
        """,
        (query, *ids, *scopes, limit),
    )
    if hits:
        return [_row_to_entry(r) for r in hits]
    rows = db.query(
        f"""
        SELECT * FROM memories
        WHERE status = 'active' AND scope IN ({placeholders}) AND case_id IN ({in_clause})
          AND (key_text LIKE ? OR value_text LIKE ?)
        ORDER BY updated_at DESC LIMIT ?
        """,
        (*scopes, *ids, f"%{query}%", f"%{query}%", limit),
    )
    return [_row_to_entry(r) for r in rows]


def context_lines(
    case_id: str,
    query: str,
    limit: int = 5,
    extra_case_ids: tuple[str, ...] = (),
) -> list[str]:
    """上下文装配用：案件记忆（含《文档名》第N页来源标注），可直接作为事实依据复用。

    记忆条目写入时强制带来源（memory_write 无 source 不入库），来源解析为文档
    标题后可被下游直接引用；与整卷通读产出的案件工作记忆同属已核对事实。
    extra_case_ids 仅由文书生成关联案件传入（只读共享）。
    """
    entries = recall(
        case_id=case_id,
        query=query,
        limit=limit,
        scopes=("case",),
        extra_case_ids=extra_case_ids,
    )
    db = get_db()
    title_cache: dict[str, str] = {}
    lines: list[str] = []
    for e in entries:
        src = e.get("source") or {}
        doc_id = src.get("documentId")
        if doc_id:
            if doc_id not in title_cache:
                row = db.query_one("SELECT title FROM documents WHERE id = ?", (doc_id,))
                title_cache[doc_id] = (row["title"] if row else None) or doc_id
            lines.append(
                f"- [{e['kind']}] {e['key']}：{e['value']}（来源：《{title_cache[doc_id]}》第 {src.get('pageNo')} 页）"
            )
        else:
            lines.append(f"- [{e['kind']}] {e['key']}：{e['value']}")
    return lines


def list_memories(
    *,
    case_id: str | None = None,
    scope: str | None = None,
    status: str | None = None,
    limit: int = 100,
    extra_case_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    db = get_db()
    conds = ["status != 'deleted'"] if status is None else ["status = ?"]
    args: list[Any] = [] if status is None else [status]
    if case_id:
        ids = [case_id, *[cid for cid in extra_case_ids if cid and cid != case_id]]
        conds.append(f"case_id IN ({','.join('?' for _ in ids)})")
        args.extend(ids)
    if scope:
        conds.append("scope = ?")
        args.append(scope)
    rows = db.query(
        f"SELECT * FROM memories WHERE {' AND '.join(conds)} ORDER BY updated_at DESC LIMIT ?",
        (*args, limit),
    )
    return [_row_to_entry(r) for r in rows]


# ---------------------------------------------------------------------------
# 更正 / 删除（M-004 / M-005）
# ---------------------------------------------------------------------------

def correct(memory_id: str, value: str, note: str = "") -> dict[str, Any]:
    """用户更正：原值保留（旧行转 superseded，可由审计与列表追溯），新值生成新行并生效。"""
    db = get_db()
    value = (value or "").strip()
    if not value:
        raise MemoryError("更正内容不能为空")
    old = db.query_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
    if old is None or old["status"] == "deleted":
        raise MemoryError("记忆不存在或已删除")
    if old["status"] != ACTIVE:
        raise MemoryError(f"仅可更正生效中的记忆（当前 {old['status']}）")

    new_id = str(uuid.uuid4())
    ts = now_iso()
    # 更正内容来自用户，可能不再与旧引文一致：不可核验时不再沿用旧来源，
    # 避免把用户更正渲染成“已由原文支持”（05 方案 §6.3 用户确认与原文支持的语义区别）。
    source = (old["source_document_id"], old["source_version_id"],
              old["source_page_no"], old["source_quote"])
    if old["source_document_id"] and old["source_version_id"] and old["source_page_no"] is not None:
        from .citations import normalize_text, verify_quote

        if not verify_quote(normalize_text(value), old["source_version_id"], int(old["source_page_no"])):
            source = (None, None, None, None)
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO memories(id, workspace_id, scope, case_id, task_id, kind, key_text, value_text,
                                 source_document_id, source_version_id, source_page_no, source_quote,
                                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                new_id, old["workspace_id"], old["scope"], old["case_id"], old["task_id"], old["kind"],
                old["key_text"], value, source[0], source[1], source[2], source[3], ts, ts,
            ),
        )
        _index(conn, new_id, old["case_id"], old["scope"], old["key_text"], value)
        conn.execute(
            "UPDATE memories SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE id = ?",
            (new_id, ts, memory_id),
        )
        from . import audit

        audit.append(
            conn, action="memory.correct", resource_type="memory", resource_id=memory_id,
            case_id=old["case_id"],
            metadata={"kind": old["kind"], "key": old["key_text"], "supersededBy": new_id,
                      "note": note, "oldValueLength": len(old["value_text"]), "newValueLength": len(value)},
        )
    return {"id": new_id, "supersededId": memory_id, "key": old["key_text"], "value": value, "status": ACTIVE}


def forget(memory_id: str, note: str = "") -> dict[str, Any]:
    """删除记忆：状态位转 deleted + 清理 FTS 索引，两处均不可再召回（M-005）。"""
    db = get_db()
    row = db.query_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
    if row is None or row["status"] == "deleted":
        raise MemoryError("记忆不存在或已删除")
    with db.transaction() as conn:
        conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
        conn.execute(
            "UPDATE memories SET status = 'deleted', updated_at = ? WHERE id = ?", (now_iso(), memory_id)
        )
        from . import audit

        audit.append(
            conn, action="memory.forget", resource_type="memory", resource_id=memory_id, case_id=row["case_id"],
            metadata={"kind": row["kind"], "key": row["key_text"], "note": note},
        )
    return {"id": memory_id, "deleted": True}


# ---------------------------------------------------------------------------
# 来源失效 / 会话清理（M-003）
# ---------------------------------------------------------------------------

def invalidate_by_document(document_id: str, reason: str = "DOCUMENT_DELETED") -> int:
    """来源文档删除或替换：相关记忆转 stale，重新计算前不得召回为有效事实（M-003）。"""
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id, case_id FROM memories WHERE source_document_id = ? AND status = 'active'",
            (document_id,),
        ).fetchall()
        if not rows:
            return 0
        conn.execute(
            "UPDATE memories SET status = 'stale', updated_at = ? WHERE source_document_id = ? AND status = 'active'",
            (ts, document_id),
        )
        from . import audit

        for r in rows:
            audit.append(
                conn, action="memory.stale", resource_type="memory", resource_id=r["id"], case_id=r["case_id"],
                metadata={"reason": reason, "documentId": document_id},
            )
    return len(rows)


def clear_session(task_id: str) -> int:
    """任务终态：清理该任务的会话工作记忆（技术方案 §8.1：任务期结束后压缩或删除）。"""
    db = get_db()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE task_id = ? AND scope = 'session' AND status != 'deleted'",
            (task_id,),
        ).fetchone()
        if not row["n"]:
            return 0
        conn.execute("DELETE FROM memories_fts WHERE memory_id IN (SELECT id FROM memories WHERE task_id = ?)", (task_id,))
        conn.execute(
            "UPDATE memories SET status = 'deleted', updated_at = ? WHERE task_id = ? AND scope = 'session'",
            (now_iso(), task_id),
        )
    return int(row["n"])


# ---------------------------------------------------------------------------
# 报告入记忆（M-002：只入有来源的条目）
# ---------------------------------------------------------------------------

def ingest_report(case_id: str, task_id: str, body: dict[str, Any]) -> dict[str, int]:
    """任务完成后把报告中的有来源条目写入案件记忆（人物 / 时间线 / 争议焦点）。

    无引证（无可靠来源）的条目不入库；引证无法核验（文档/页码不存在、引文不匹配）
    同样跳过并单独计数，不因单条坏来源中断整份报告入库。重复条目按
    (kind, key, value) 去重。
    """
    report = (body or {}).get("report")
    if not isinstance(report, dict):
        return {"written": 0, "skippedNoSource": 0, "skippedUnverified": 0, "duplicated": 0}
    db = get_db()
    written = skipped = unverified = duplicated = 0
    candidates: list[tuple[str, str, str, dict[str, Any]]] = []
    for p in report.get("parties") or []:
        if isinstance(p, dict) and str(p.get("name") or "").strip():
            candidates.append(("person", str(p["name"]), f"{p.get('role') or ''}：{p.get('note') or ''}".strip("："),
                               p.get("cite") or {}))
    for t in report.get("timeline") or []:
        if isinstance(t, dict) and str(t.get("event") or "").strip():
            candidates.append(("timeline", str(t.get("date") or ""), str(t["event"]), t.get("cite") or {}))
    for d in report.get("disputes") or []:
        if isinstance(d, dict) and str(d.get("title") or "").strip():
            candidates.append(("dispute", str(d["title"]), str(d.get("desc") or ""), d.get("cite") or {}))

    for kind, key, value, cite in candidates:
        doc_id = str(cite.get("documentId") or "").strip() if isinstance(cite, dict) else ""
        if not doc_id:
            skipped += 1
            continue
        existing = db.query_one(
            "SELECT id FROM memories WHERE case_id = ? AND kind = ? AND key_text = ? AND value_text = ? "
            "AND status = 'active'",
            (case_id, kind, key, value),
        )
        if existing:
            duplicated += 1
            continue
        try:
            remember(
                scope="case", kind=kind, key=key, value=value, case_id=case_id, task_id=task_id,
                source={
                    "documentId": doc_id,
                    "pageNo": cite.get("pageNo"),
                    "quote": cite.get("quote"),
                },
            )
        except MemoryError:
            unverified += 1
            continue
        written += 1
    return {"written": written, "skippedNoSource": skipped, "skippedUnverified": unverified,
            "duplicated": duplicated}


def stats(case_id: str) -> dict[str, Any]:
    """案件记忆概览（UI 与评测共用）。"""
    db = get_db()
    rows = db.query(
        "SELECT scope, status, COUNT(*) AS n FROM memories WHERE case_id = ? GROUP BY scope, status",
        (case_id,),
    )
    by: dict[str, int] = {}
    for r in rows:
        by[f"{r['scope']}:{r['status']}"] = int(r["n"])
    return {"caseId": case_id, "counts": by}