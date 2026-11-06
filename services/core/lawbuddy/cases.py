"""案件服务（REQ-CASE-002 / 005 / 006 / 007）。

- 案件名称允许重复，内部 ID 唯一；
- 归档后只读：归档案件的文档导入被拒绝（Agent 侧同样校验）；
- 永久删除需案件名复核（delete_case），删除范围与保留策略见其 docstring。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from .storage import get_db
from .workspace import WorkspaceError, now_iso

CASE_TYPES = ("criminal", "civil", "commercial", "administrative", "contract", "other")
MODES = ("legal_review", "contract_review", "document_generation")
PERMISSION_PRESETS = ("strict_local", "controlled_external", "custom")


class CaseError(RuntimeError):
    pass


def create_case(
    title: str,
    case_type: str,
    client_name: str,
    workspace_id: str | None = None,
    client_role: str | None = None,
    stage: str | None = None,
    default_mode: str = "legal_review",
    permission_preset: str = "strict_local",
    client_request_id: str | None = None,
    mode_config: dict[str, Any] | None = None,
    linked_case_id: str | None = None,
) -> dict[str, Any]:
    """新建案件。路径/空间检查由 workspace.ensure 承担，这里做字段校验。"""
    db = get_db()
    title = (title or "").strip()
    client_name = (client_name or "").strip()
    requested_title = title
    if default_mode == "document_generation" and not title:
        title = f"文书生成 {datetime.now():%Y-%m-%d %H:%M}"
    if not title:
        raise CaseError("任务名称不能为空")
    if default_mode == "legal_review" and not client_name:
        raise CaseError("委托人或客户不能为空")
    if case_type not in CASE_TYPES:
        raise CaseError(f"无效的案件类型：{case_type}")
    if default_mode not in MODES:
        raise CaseError(f"无效的默认模式：{default_mode}")
    if permission_preset not in PERMISSION_PRESETS:
        raise CaseError(f"无效的权限预设：{permission_preset}")
    config = mode_config or {}
    allowed = {"contractType", "reviewBackground"} if default_mode == "contract_review" else set()
    if not isinstance(config, dict) or set(config) - allowed:
        raise CaseError("任务模式配置不合法")
    if any(not isinstance(v, str) or len(v) > 4000 for v in config.values()):
        raise CaseError("任务模式配置值不合法")
    request_id = (client_request_id or "").strip()
    if client_request_id is not None and (not request_id or len(request_id) > 128):
        raise CaseError("clientRequestId 不合法")
    linked_id = (linked_case_id or "").strip()
    if linked_id and default_mode != "document_generation":
        raise CaseError("仅文书生成任务可关联案件")

    if workspace_id is None:
        ws = db.query_one("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
        if ws is None:
            raise WorkspaceError("workspace not initialized")
        workspace_id = ws["id"]

    if linked_id:
        source = db.query_one(
            "SELECT id, workspace_id, status FROM cases WHERE id = ?", (linked_id,),
        )
        if source is None or source["workspace_id"] != workspace_id:
            raise CaseError("关联案件不存在或不属于当前工作空间")
        if source["status"] in ("pending_delete",):
            raise CaseError("关联案件待删除，无法共享")
        if source["status"] == "archived":
            raise CaseError("关联案件已归档，请先恢复后再关联")

    case_id = str(uuid.uuid4())
    ts = now_iso()
    payload_hash = hashlib.sha256(json.dumps(
        [requested_title, case_type, client_name, client_role, stage, default_mode,
         permission_preset, config, linked_id or None], sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    with db.transaction() as conn:
        if request_id:
            prior = conn.execute(
                "SELECT case_id, payload_hash FROM case_creation_requests "
                "WHERE workspace_id = ? AND request_id = ?", (workspace_id, request_id),
            ).fetchone()
            if prior:
                if prior["payload_hash"] != payload_hash:
                    raise CaseError("IDEMPOTENCY_CONFLICT")
                return get_case(prior["case_id"])
        conn.execute(
            """
            INSERT INTO cases(id, workspace_id, title, case_type, client_name, client_role,
                              stage, default_mode, status, permission_preset, created_at, updated_at,
                              mode_config_json, linked_case_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
            """,
            (case_id, workspace_id, title, case_type, client_name, client_role or None,
             stage or None, default_mode, permission_preset, ts, ts,
             json.dumps(config, ensure_ascii=False), linked_id or None),
        )
        if request_id:
            conn.execute(
                "INSERT INTO case_creation_requests VALUES (?, ?, ?, ?)",
                (workspace_id, request_id, case_id, payload_hash),
            )
        from . import audit

        audit.append(
            conn,
            action="case.create",
            resource_type="case",
            resource_id=case_id,
            case_id=case_id,
            metadata={
                "caseType": case_type, "defaultMode": default_mode,
                "permissionPreset": permission_preset,
                **({"linkedCaseId": linked_id} if linked_id else {}),
            },
        )
    return get_case(case_id)


def _row_to_case(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspaceId": row["workspace_id"],
        "title": row["title"],
        "caseType": row["case_type"],
        "clientName": row["client_name"],
        "clientRole": row["client_role"],
        "stage": row["stage"],
        "defaultMode": row["default_mode"],
        "linkedCaseId": row.get("linked_case_id"),
        "modeConfig": json.loads(row.get("mode_config_json") or "{}"),
        "status": row["status"],
        "pinned": bool(row["pinned"]),
        "permissionPreset": row["permission_preset"],
        "redactionPolicy": row["redaction_policy"],
        "redactionCustomTypes": _json_list(row["redaction_custom_types_json"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "archivedAt": row["archived_at"],
    }


def _json_list(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        import json

        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def list_cases(include_archived: bool = False) -> list[dict[str, Any]]:
    db = get_db()
    sql = "SELECT * FROM cases"
    params: tuple[Any, ...] = ()
    if not include_archived:
        sql += " WHERE status != 'archived'"
    sql += " ORDER BY pinned DESC, updated_at DESC"
    return [_row_to_case(r) for r in db.query(sql, params)]


def get_case(case_id: str) -> dict[str, Any]:
    db = get_db()
    row = db.query_one("SELECT * FROM cases WHERE id = ?", (case_id,))
    if row is None:
        raise CaseError("案件不存在")
    return _row_to_case(row)


def readable_case_ids(case_id: str) -> list[str]:
    """任务可读取的案件范围：自身 + 显式关联案件（文书生成只读共享）。

    关联仅在同一工作空间内生效；来源案件待删除时不再纳入读取范围。
    案件记忆与材料检索仍按案件维度隔离，只有显式建立关联的文书生成任务
    才能读取来源案件，其他任务不受影响。
    """
    db = get_db()
    row = db.query_one("SELECT id, workspace_id, linked_case_id FROM cases WHERE id = ?", (case_id,))
    if row is None:
        return [case_id]
    ids = [case_id]
    linked = str(row.get("linked_case_id") or "").strip()
    if linked and linked != case_id:
        source = db.query_one(
            "SELECT id, workspace_id, status FROM cases WHERE id = ?", (linked,),
        )
        if (
            source is not None
            and source["workspace_id"] == row["workspace_id"]
            and source["status"] != "pending_delete"
        ):
            ids.append(linked)
    return ids


def linked_case(case_id: str) -> dict[str, Any] | None:
    """读取任务的显式关联案件（无关联、跨空间或已删除时返回 None）。"""
    ids = readable_case_ids(case_id)
    if len(ids) < 2:
        return None
    return get_case(ids[1])


def require_active_case(case_id: str) -> dict[str, Any]:
    """取案件并强制其为可写状态（归档只读，REQ-CASE-006）。"""
    case = get_case(case_id)
    if case["status"] == "archived":
        raise CaseError("案件已归档，处于只读状态；请先恢复案件")
    if case["status"] in ("pending_delete",):
        raise CaseError("案件处于待删除状态，禁止写入")
    return case


def require_case_mode(case_id: str, mode: str) -> dict[str, Any]:
    """All write paths use the persisted business-container mode."""
    case = require_active_case(case_id)
    if case["defaultMode"] not in MODES:
        raise CaseError("TASK_MODE_UNCONFIRMED")
    if case["defaultMode"] != mode:
        raise CaseError("TASK_MODE_MISMATCH")
    return case


REDACTION_POLICIES = ("light", "standard", "strict", "custom")


def update_redaction_policy(
    case_id: str, policy: str, custom_types: list[str] | None = None
) -> dict[str, Any]:
    """设置案件脱敏档位（REQ-PRIV-004 / D-011）。custom 档须显式给出类型集合。"""
    import json

    if policy not in REDACTION_POLICIES:
        raise CaseError(f"无效的脱敏档位：{policy}")
    case = require_active_case(case_id)
    types_json = None
    if policy == "custom":
        allowed = set(custom_types or [])
        if not allowed:
            raise CaseError("自定义档位必须至少选择一种敏感字段类型")
        unknown = allowed - ENTITY_TYPE_ORDER.keys()
        if unknown:
            raise CaseError(f"未知的敏感字段类型：{', '.join(sorted(unknown))}")
        types_json = json.dumps(sorted(allowed), ensure_ascii=False)
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE cases SET redaction_policy = ?, redaction_custom_types_json = ?, updated_at = ? WHERE id = ?",
            (policy, types_json, ts, case_id),
        )
        from . import audit

        audit.append(
            conn, action="case.redaction_policy", resource_type="case", resource_id=case_id,
            case_id=case_id,
            metadata={"policy": policy, "customTypes": sorted(allowed) if policy == "custom" else None},
        )
    return get_case(case_id)


# 敏感字段类型（redaction 识别器全集；顺序即 UI 展示顺序）
# org（组织机构）为历史类型：机构名不属于个人敏感信息，已不再参与脱敏；
# 保留键仅为兼容旧案件 redaction_custom_types 配置与既有映射行，不在 UI 露出。
ENTITY_TYPE_ORDER: dict[str, str] = {
    "id_card": "身份证号",
    "bank_card": "银行卡号",
    "phone": "手机号",
    "email": "邮箱",
    "credit_code": "统一社会信用代码",
    "plate": "车牌号",
    "case_number": "案号",
    "name": "姓名",
    "org": "组织机构",
    "address": "地址",
    "custom": "自定义敏感词",
}


def archive_case(case_id: str) -> dict[str, Any]:
    db = get_db()
    ts = now_iso()
    title = db.query_one("SELECT title FROM cases WHERE id = ?", (case_id,))
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE cases SET status = 'archived', archived_at = ?, updated_at = ? WHERE id = ? AND status != 'archived'",
            (ts, ts, case_id),
        )
        if cur.rowcount == 0:
            raise CaseError("案件不存在或已归档")
        from . import audit

        audit.append(
            conn,
            action="case.archive",
            resource_type="case",
            resource_id=case_id,
            case_id=case_id,
            metadata={"title": title["title"] if title else None},
        )
    return get_case(case_id)


def restore_case(case_id: str) -> dict[str, Any]:
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE cases SET status = 'active', archived_at = NULL, updated_at = ? WHERE id = ? AND status = 'archived'",
            (ts, case_id),
        )
        if cur.rowcount == 0:
            raise CaseError("案件不存在或不是归档状态")
        from . import audit

        audit.append(
            conn,
            action="case.restore",
            resource_type="case",
            resource_id=case_id,
            case_id=case_id,
        )
    return get_case(case_id)


def copy_materials(from_case_id: str, to_case_id: str, document_ids: list[str],
                   request_id: str, confirmed: bool) -> dict[str, Any]:
    """Copy encrypted material and parsed pages locally without moving original ownership."""
    from . import audit, search

    source = get_case(from_case_id)
    target = require_active_case(to_case_id)
    if confirmed is not True:
        raise CaseError("请明确确认复制材料")
    if from_case_id == to_case_id or source["workspaceId"] != target["workspaceId"]:
        raise CaseError("只能复制到同一工作空间的另一任务")
    if not isinstance(document_ids, list) or not 1 <= len(document_ids) <= 200 or (
        any(not isinstance(item, str) for item in document_ids) or len(set(document_ids)) != len(document_ids)
    ):
        raise CaseError("材料选择不合法")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise CaseError("clientRequestId 不合法")
    digest = hashlib.sha256(json.dumps([from_case_id, to_case_id, document_ids]).encode()).hexdigest()
    db = get_db()
    mappings = []
    ts = now_iso()
    with db.transaction() as conn:
        prior = conn.execute("SELECT * FROM material_copy_requests WHERE request_id = ?", (request_id,)).fetchone()
        if prior:
            if prior["payload_hash"] != digest:
                raise CaseError("IDEMPOTENCY_CONFLICT")
            return json.loads(prior["result_json"])
        for selected_id in document_ids:
            doc = conn.execute("SELECT * FROM documents WHERE id = ? AND case_id = ?",
                               (selected_id, from_case_id)).fetchone()
            if not doc or doc["status"] not in ("ready", "partial"):
                raise CaseError("材料归属不符或未完成解析")
            version = conn.execute("SELECT * FROM document_versions WHERE id = ?",
                                   (doc["active_version_id"],)).fetchone()
            if not version:
                raise CaseError("材料版本不存在")
            new_doc, new_version = str(uuid.uuid4()), str(uuid.uuid4())

            def insert_row(table, values):
                columns = list(values)
                conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                             tuple(values[column] for column in columns))

            insert_row("documents", {**dict(doc), "id": new_doc, "case_id": to_case_id,
                                     "active_version_id": new_version, "origin_document_id": None,
                                     "created_at": ts, "updated_at": ts})
            insert_row("document_versions", {**dict(version), "id": new_version, "document_id": new_doc,
                                             "parent_id": None, "created_by_task": None, "created_at": ts})
            pages = conn.execute("SELECT * FROM document_pages WHERE version_id = ?", (version["id"],)).fetchall()
            for page in pages:
                new_page = str(uuid.uuid4())
                insert_row("document_pages", {**dict(page), "id": new_page, "version_id": new_version})
                for block in conn.execute("SELECT * FROM text_blocks WHERE page_id = ?", (page["id"],)).fetchall():
                    insert_row("text_blocks", {**dict(block), "id": str(uuid.uuid4()), "page_id": new_page})
                search.index_page(conn, text=page["text_content"] or "", case_id=to_case_id,
                                  document_id=new_doc, document_title=doc["title"], version_id=new_version,
                                  page_no=page["page_no"], text_quality=page["text_quality"])
            conn.execute("INSERT INTO material_copy_sources VALUES (?, ?, ?, ?, ?)",
                         (new_doc, from_case_id, selected_id, version["id"], ts))
            mappings.append({"sourceDocumentId": selected_id, "sourceVersionId": version["id"],
                             "documentId": new_doc, "versionId": new_version})
        result = {"fromCaseId": from_case_id, "toCaseId": to_case_id, "mappings": mappings}
        conn.execute("INSERT INTO material_copy_requests VALUES (?, ?, ?, ?)",
                     (request_id, digest, json.dumps(result), to_case_id))
        audit.append(conn, action="case.copy_materials", resource_type="case", resource_id=to_case_id,
                     case_id=to_case_id, metadata={"sourceCaseId": from_case_id, "documentCount": len(mappings)})
    return result


def delete_case(case_id: str) -> dict[str, Any]:
    """永久删除任务。

    删除范围（单事务，失败整体回滚）：
    - FTS 索引、文档树（版本/页/文本块）、引证链（Claim/SourceRef/关联）、
      任务链（事件/检查点/审批）、合同审查会话、
      对话会话、编辑会话、案件级授权、案件行；
    保留策略：
    - 审计日志（只追加哈希链）：含本次删除记录；
    - 内容存储 blob：按 SHA 寻址、可跨案件共享，孤立块由后续清理策略回收。
    """
    db = get_db()
    case = get_case(case_id)

    # 先停止该案件文档的 OCR 并等待线程真正退出：页/块随案件级联删除，
    # 后台仍落库会触发外键失败并把消费者线程打崩
    try:
        from .ocr.pipeline import OcrManager

        manager = OcrManager.instance()
        for row in db.query("SELECT id FROM documents WHERE case_id = ?", (case_id,)):
            manager.stop_and_wait(row["id"])
    except Exception:  # noqa: BLE001  # OCR 未启动或已结束：删除流程不受影响
        pass

    def _count(conn: Any, sql: str) -> int:
        return int(conn.execute(sql, (case_id,)).fetchone()[0])

    with db.transaction() as conn:
        stats = {
            "documents": _count(conn, "SELECT COUNT(*) FROM documents WHERE case_id = ?"),
            "tasks": _count(conn, "SELECT COUNT(*) FROM tasks WHERE case_id = ?"),
        }
        # 1) 全文索引（FTS5 虚拟表，无外键）
        conn.execute("DELETE FROM pages_fts WHERE case_id = ?", (case_id,))
        # 1.5) 关联案件（文书生成只读共享）：来源案件删除后，其他任务中引用
        # 该案件材料的 Claim / 记忆立即转 stale，关联指针置空，避免继续读取已删案件
        conn.execute(
            "UPDATE claims SET status = 'stale', reason_code = 'SOURCE_CASE_DELETED' "
            "WHERE id IN ("
            "  SELECT cs.claim_id FROM claim_sources cs "
            "  JOIN source_refs sr ON sr.id = cs.source_ref_id "
            "  WHERE sr.document_id IN (SELECT id FROM documents WHERE case_id = ?)"
            ")",
            (case_id,),
        )
        conn.execute(
            "UPDATE memories SET status = 'stale', updated_at = ? "
            "WHERE case_id != ? AND status = 'active' AND source_document_id IN "
            "(SELECT id FROM documents WHERE case_id = ?)",
            (now_iso(), case_id, case_id),
        )
        conn.execute("UPDATE cases SET linked_case_id = NULL WHERE linked_case_id = ?", (case_id,))
        # 2) 引证链：按依赖顺序 claim_sources → claims → source_refs
        conn.execute(
            "DELETE FROM claim_sources WHERE claim_id IN (SELECT id FROM claims WHERE case_id = ?)",
            (case_id,),
        )
        conn.execute("DELETE FROM claims WHERE case_id = ?", (case_id,))
        conn.execute(
            "DELETE FROM source_refs WHERE document_id IN (SELECT id FROM documents WHERE case_id = ?)",
            (case_id,),
        )
        # 3) 任务链：事件/检查点/审批 → 任务
        conn.execute(
            "DELETE FROM task_events WHERE task_id IN (SELECT id FROM tasks WHERE case_id = ?)",
            (case_id,),
        )
        conn.execute(
            "DELETE FROM agent_checkpoints WHERE task_id IN (SELECT id FROM tasks WHERE case_id = ?)",
            (case_id,),
        )
        conn.execute("DELETE FROM approval_requests WHERE case_id = ?", (case_id,))
        conn.execute("DELETE FROM tasks WHERE case_id = ?", (case_id,))
        # 3.5) 会话类（对案件/文档带 RESTRICT 外键，必须先于 documents/cases 删除；
        #      子表随各自 CASCADE 级联：条款/风险项/操作史/工作副本、消息）
        stats["contractReviews"] = _count(conn, "SELECT COUNT(*) FROM contract_reviews WHERE case_id = ?")
        conn.execute("DELETE FROM contract_reviews WHERE case_id = ?", (case_id,))
        stats["conversations"] = _count(conn, "SELECT COUNT(*) FROM conversations WHERE case_id = ?")
        # 会话范围的授权（V32：allow_case 绑定 conversation_id）必须在删会话前清理，避免孤儿授权
        conn.execute(
            "DELETE FROM permission_grants WHERE scope_type = 'case' AND scope_id IN "
            "(SELECT id FROM conversations WHERE case_id = ?)",
            (case_id,),
        )
        conn.execute("DELETE FROM conversations WHERE case_id = ?", (case_id,))
        # 编辑会话无外键约束，随案件清理避免残留孤儿行
        conn.execute("DELETE FROM editor_sessions WHERE case_id = ?", (case_id,))
        # 4) 文档树：派生版本（父版本引用）先删，原件随 documents 级联（页/块 CASCADE）
        conn.execute(
            "DELETE FROM document_versions WHERE parent_id IS NOT NULL "
            "AND document_id IN (SELECT id FROM documents WHERE case_id = ?)",
            (case_id,),
        )
        conn.execute("DELETE FROM documents WHERE case_id = ?", (case_id,))
        # 5) 案件级授权与案件行
        conn.execute(
            "DELETE FROM permission_grants WHERE scope_type = 'case' AND scope_id = ?",
            (case_id,),
        )
        conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))

        from . import audit

        audit.append(
            conn,
            action="case.delete",
            resource_type="case",
            resource_id=case_id,
            case_id=case_id,
            metadata={"title": case["title"], **stats, "auditRetained": True},
        )
    return {"deleted": True, "caseId": case_id, "title": case["title"], **stats}
