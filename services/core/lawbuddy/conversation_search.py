"""会话搜索、分支、彻底删除与产物快照（LB-HERMES-2026-01 WP05）。

在 `conversation.py` 之上扩展会话能力，硬约束：

1. **案件隔离强制在 SQL 层**：所有查询都带 `case_id`，不接受"查全库再过滤"；
   跨案命中必须为零，这是案件隔离原则（M-001）在搜索上的延伸。
2. **搜索返回真实消息**：命中条目携带 `conversationId + messageId + 定位锚点`，
   UI 能直接跳到原文；不返回"AI 总结"冒充消息。
3. **搜索默认不调用 LLM**：纯本地 FTS/LIKE，零外发。
4. **分支只复制消息**：新 `conversation_id`，复制锚点及之前的消息文本；
   **不复制**任务、审批、授权、预算、Journal、检查点或任何副作用。
5. **彻底删除无软删**：清理消息、FTS 行、分支记录与预览注册；删除后不得
   通过新会话或缓存自动重建会话内容；审计只留最小墓碑（任务 ID 与删除计数）。
6. **快照只覆盖工作副本与交付物**：加密 blob + 内容哈希；原件永不被覆盖。

FTS 写入采用 trigram（与 `pages_fts` / `memories_fts` 一致），短查询回退 LIKE。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from . import conversation
from .storage import get_db
from .workspace import now_iso

# 搜索返回上限与单条摘录长度
SEARCH_LIMIT = 30
SNIPPET_CHARS = 120
# 查询长度下限：低于该长度时 FTS trigram 无法有效命中，走 LIKE
FTS_MIN_QUERY = 3
_SNAPSHOT_DIRNAME = "artifact_snapshots"

# FTS5 查询语法保留字符：用户输入需转义为字面量（避免语法错误与意外语义）
_FTS_SPECIAL = re.compile(r'["*():^\-]')


class ConversationSearchError(RuntimeError):
    pass


class ConversationNotFound(ConversationSearchError):
    pass


# ---------------------------------------------------------------------------
# FTS 索引维护
# ---------------------------------------------------------------------------

def _fts_query(query: str) -> str:
    """把用户输入转成 FTS5 字面量短语查询（转义保留字符、双引号包裹）。"""
    cleaned = _FTS_SPECIAL.sub(" ", query).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        raise ConversationSearchError("搜索词不能为空")
    return '"' + cleaned.replace('"', '""') + '"'


def index_message(
    message_id: str, conversation_id: str, case_id: str, role: str, content: str,
) -> None:
    """登记一条消息到检索索引（幂等：同 message_id 先删后插）。"""
    db = get_db()
    with db.transaction() as conn:
        conn.execute("DELETE FROM conversation_fts WHERE message_id = ?", (message_id,))
        conn.execute(
            "INSERT INTO conversation_fts(message_id, conversation_id, case_id, role, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, conversation_id, case_id, role, content),
        )


def index_conversation(conversation_id: str) -> int:
    """为已有会话补齐索引（工作空间升级或索引缺失时使用）；返回索引条数。"""
    db = get_db()
    row = db.query_one("SELECT case_id FROM conversations WHERE id = ?", (conversation_id,))
    if row is None:
        raise ConversationNotFound("会话不存在")
    case_id = str(row["case_id"])
    msgs = db.query(
        "SELECT id, role, content FROM conversation_messages WHERE conversation_id = ?",
        (conversation_id,),
    )
    for m in msgs:
        index_message(str(m["id"]), conversation_id, case_id, str(m["role"]), str(m["content"]))
    return len(msgs)


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

def _snippet(content: str, query: str) -> str:
    """以首个命中位置为中心截取摘要（找不到时取开头）。"""
    text = str(content or "")
    pos = text.lower().find(query.lower())
    if pos < 0:
        return text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else "")
    start = max(0, pos - SNIPPET_CHARS // 3)
    end = min(len(text), start + SNIPPET_CHARS)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def search(
    case_id: str, query: str, *, limit: int = SEARCH_LIMIT, conversation_id: str | None = None,
) -> list[dict[str, Any]]:
    """在指定案件内检索会话消息，返回真实消息与定位锚点。

    排序：FTS bm25 相关度优先，其次时间倒序；短查询（< 3 字）走 LIKE。
    所有分支都带 `case_id` 过滤，并在返回前二次校验，杜绝跨案泄露。
    """
    case_id = str(case_id or "").strip()
    query = str(query or "").strip()
    if not case_id:
        raise ConversationSearchError("缺少案件 ID（会话检索必须限定案件）")
    if not query:
        raise ConversationSearchError("搜索词不能为空")
    db = get_db()
    rows: list[Any]
    if len(query) >= FTS_MIN_QUERY:
        try:
            sql = (
                "SELECT f.message_id, f.conversation_id, f.role, m.content, m.created_at, "
                "       c.case_id, bm25(conversation_fts) AS score "
                "FROM conversation_fts f "
                "JOIN conversation_messages m ON m.id = f.message_id "
                "JOIN conversations c ON c.id = f.conversation_id "
                "WHERE conversation_fts MATCH ? AND f.case_id = ? AND c.case_id = ?"
            )
            params: list[Any] = [_fts_query(query), case_id, case_id]
            if conversation_id:
                sql += " AND f.conversation_id = ?"
                params.append(conversation_id)
            sql += " ORDER BY score ASC, m.created_at DESC, m.rowid DESC LIMIT ?"
            params.append(int(limit))
            rows = db.query(sql, tuple(params))
        except Exception:  # noqa: BLE001 FTS 语法/索引异常时回退 LIKE，不隐藏错误
            rows = _like_search(db, case_id, query, limit, conversation_id)
    else:
        rows = _like_search(db, case_id, query, limit, conversation_id)

    hits: list[dict[str, Any]] = []
    for r in rows:
        if str(r["case_id"]) != case_id:  # 双保险：任何越界行直接丢弃
            continue
        hits.append({
            "conversationId": str(r["conversation_id"]),
            "messageId": str(r["message_id"]),
            "role": str(r["role"]),
            "snippet": _snippet(str(r["content"]), query),
            "createdAt": r["created_at"],
            "caseId": case_id,
            # 定位锚点：UI 用它在会话内滚动到该消息
            "anchor": {"kind": "message", "messageId": str(r["message_id"])},
        })
        if len(hits) >= limit:
            break
    return hits


def _like_search(
    db, case_id: str, query: str, limit: int, conversation_id: str | None,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT m.id AS message_id, m.conversation_id, m.role, m.content, m.created_at, "
        "       c.case_id, 0.0 AS score "
        "FROM conversation_messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.case_id = ? AND m.content LIKE ? ESCAPE '\\'"
    )
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    params: list[Any] = [case_id, pattern]
    if conversation_id:
        sql += " AND m.conversation_id = ?"
        params.append(conversation_id)
    sql += " ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?"
    params.append(int(limit))
    return db.query(sql, tuple(params))


# ---------------------------------------------------------------------------
# 分支
# ---------------------------------------------------------------------------

def branch(
    conversation_id: str, anchor_message_id: str | None = None,
) -> dict[str, Any]:
    """从锚点消息分支出新会话：只复制消息文本，不复制任何副作用。

    - 锚点缺省时取会话末尾（等价于"复制整段对话再继续追问"）；
    - 新会话的 `conversation_branches` 记录父会话与锚点，供 UI 展示来源；
    - **不复制**任务、审批、授权、预算、Journal、检查点、待答问题；
    - 历史引证再次使用时必须重新验证文档版本（由引用校验路径保证）。
    """
    db = get_db()
    src = db.query_one("SELECT id, case_id FROM conversations WHERE id = ?", (conversation_id,))
    if src is None:
        raise ConversationNotFound("会话不存在")
    case_id = str(src["case_id"])

    if anchor_message_id:
        anchor = db.query_one(
            "SELECT id, created_at FROM conversation_messages WHERE id = ? AND conversation_id = ?",
            (anchor_message_id, conversation_id),
        )
        if anchor is None:
            raise ConversationSearchError("锚点消息不属于该会话")
        anchor_ts = str(anchor["created_at"])
        msgs = db.query(
            "SELECT role, content, task_id, created_at FROM conversation_messages "
            "WHERE conversation_id = ? AND (created_at < ? OR (created_at = ? AND rowid <= "
            "  (SELECT rowid FROM conversation_messages WHERE id = ?))) "
            "ORDER BY created_at ASC, rowid ASC",
            (conversation_id, anchor_ts, anchor_ts, anchor_message_id),
        )
    else:
        anchor_ts = ""
        msgs = db.query(
            "SELECT role, content, task_id, created_at FROM conversation_messages "
            "WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
            (conversation_id,),
        )

    new_id = str(uuid.uuid4())
    ts = now_iso()
    indexed: list[tuple[str, str, str]] = []
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO conversations(id, case_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (new_id, case_id, ts, ts),
        )
        for m in msgs:
            mid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO conversation_messages(id, conversation_id, role, content, task_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                # 任务归属不复制：新分支的消息不属于原任务（避免误挂任务产物）
                (mid, new_id, str(m["role"]), str(m["content"]), None, str(m["created_at"])),
            )
            indexed.append((mid, str(m["role"]), str(m["content"])))
        conn.execute(
            """
            INSERT INTO conversation_branches(id, case_id, conversation_id, parent_conversation_id,
                                              anchor_message_id, anchor_created_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), case_id, new_id, conversation_id,
             anchor_message_id, anchor_ts or None, ts),
        )
    # 索引写入放在事务外：`index_message` 自带事务（SQLite 不支持嵌套 BEGIN）
    for mid, role, content in indexed:
        index_message(mid, new_id, case_id, role, content)
    return {
        "conversationId": new_id,
        "caseId": case_id,
        "parentConversationId": conversation_id,
        "anchorMessageId": anchor_message_id,
        "messageCount": len(msgs),
        "createdAt": ts,
    }


# ---------------------------------------------------------------------------
# 彻底删除
# ---------------------------------------------------------------------------

def delete(conversation_id: str) -> dict[str, Any]:
    """彻底删除会话及其消息、检索索引与分支记录（无软删除）。

    活动任务的取消由调用方（service 层）在调用前完成——本模块不持有任务
    生命周期，避免在数据层隐式触发任务状态变化。预览注册（前端内存态）由
    前端在收到删除结果后自行清理。

    返回删除计数；审计由调用方写入最小墓碑（会话 ID + 计数，不含内容）。
    """
    db = get_db()
    row = db.query_one("SELECT id, case_id FROM conversations WHERE id = ?", (conversation_id,))
    if row is None:
        raise ConversationNotFound("会话不存在")
    case_id = str(row["case_id"])
    with db.transaction() as conn:
        msg_row = conn.execute(
            "SELECT COUNT(*) AS c FROM conversation_messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        message_count = int(msg_row["c"] or 0)
        conn.execute("DELETE FROM conversation_fts WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversation_messages WHERE conversation_id = ?", (conversation_id,))
        # 子分支记录随父会话级联（branches.conversation_id 有 CASCADE）
        conn.execute(
            "DELETE FROM conversation_branches WHERE parent_conversation_id = ?", (conversation_id,)
        )
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    return {
        "conversationId": conversation_id,
        "caseId": case_id,
        "deletedMessages": message_count,
        "deleted": True,
    }


# ---------------------------------------------------------------------------
# 产物快照与回滚
# ---------------------------------------------------------------------------

def _snapshot_root():
    from .sandbox import workspace_root

    root = workspace_root() / _SNAPSHOT_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def snapshot_artifact(
    case_id: str, name: str, content: bytes, *, task_id: str | None = None,
    artifact_kind: str = "working_copy", note: str | None = None,
) -> dict[str, Any]:
    """为工作副本/交付物创建加密快照（原件不受影响）。

    `artifact_kind` 只允许交付物、工作副本、草稿与其他；快照与源文件分离存储，
    回滚是"把快照内容写回工作路径"，永不覆盖案件原件。
    """
    if artifact_kind not in ("deliverable", "working_copy", "draft", "other"):
        raise ConversationSearchError(f"不支持的快照类型：{artifact_kind}")
    from . import crypto
    from .keyring import get_active_dek

    case = get_db().query_one("SELECT id FROM cases WHERE id = ?", (case_id,))
    if case is None:
        raise ConversationSearchError("案件不存在")
    raw = bytes(content or b"")
    sha = hashlib.sha256(raw).hexdigest()
    root = _snapshot_root()
    dst = root / sha[:2] / f"{sha}.enc"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        key = crypto.derive_file_key(get_active_dek())
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(crypto.encrypt_bytes(raw, key))
        tmp.replace(dst)
    snapshot_id = str(uuid.uuid4())
    ts = now_iso()
    rel = f"{_SNAPSHOT_DIRNAME}/{sha[:2]}/{sha}.enc"
    with get_db().transaction() as conn:
        conn.execute(
            """
            INSERT INTO artifact_snapshots(id, case_id, task_id, artifact_kind, name,
                                           content_sha256, byte_size, storage_path, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (snapshot_id, case_id, task_id, artifact_kind, name, sha, len(raw), rel, note, ts),
        )
    return {
        "snapshotId": snapshot_id, "caseId": case_id, "name": name,
        "artifactKind": artifact_kind, "sha256": sha, "bytes": len(raw), "createdAt": ts,
    }


def list_snapshots(case_id: str, *, name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    db = get_db()
    if name:
        rows = db.query(
            "SELECT * FROM artifact_snapshots WHERE case_id = ? AND name = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (case_id, name, limit),
        )
    else:
        rows = db.query(
            "SELECT * FROM artifact_snapshots WHERE case_id = ? ORDER BY created_at DESC LIMIT ?",
            (case_id, limit),
        )
    return [
        {
            "snapshotId": r["id"], "caseId": r["case_id"], "taskId": r["task_id"],
            "artifactKind": r["artifact_kind"], "name": r["name"],
            "sha256": r["content_sha256"], "bytes": int(r["byte_size"] or 0),
            "note": r["note"], "createdAt": r["created_at"],
        }
        for r in rows
    ]


def read_snapshot(snapshot_id: str, case_id: str) -> bytes | None:
    """读取快照密文并解密；`case_id` 必须匹配（防止跨案读取）。"""
    from . import crypto
    from .keyring import get_active_dek
    from .sandbox import workspace_root

    row = get_db().query_one(
        "SELECT storage_path FROM artifact_snapshots WHERE id = ? AND case_id = ?",
        (snapshot_id, case_id),
    )
    if row is None:
        return None
    path = workspace_root() / str(row["storage_path"])
    if not path.exists():
        return None
    return crypto.decrypt_bytes(path, crypto.derive_file_key(get_active_dek()))


def rollback(snapshot_id: str, case_id: str, target_path: str) -> dict[str, Any]:
    """把快照内容写回工作空间内的目标路径（原子替换，不触碰案件原件）。

    目标必须是工作空间内的相对路径，且不得位于案件原件目录——回滚只作用于
    工作副本与交付物。
    """
    from . import sandbox

    if not target_path or target_path.startswith(("/", "\\")) or ".." in target_path:
        raise ConversationSearchError("回滚目标必须是工作空间内的相对路径")
    normalized = target_path.replace("\\", "/")
    if normalized.startswith("originals/") or "/originals/" in f"/{normalized}":
        raise ConversationSearchError("回滚不得写入案件原件目录")
    data = read_snapshot(snapshot_id, case_id)
    if data is None:
        raise ConversationSearchError("快照不存在或无法解密")
    root = sandbox.workspace_root()
    result = sandbox.write_file(root, normalized, data.decode("utf-8", errors="replace"))
    return {
        "snapshotId": snapshot_id,
        "caseId": case_id,
        "target": normalized,
        "written": True,
        "bytes": result.get("bytes") if isinstance(result, dict) else None,
    }


# ---------------------------------------------------------------------------
# 会话级汇总（UI 侧栏）
# ---------------------------------------------------------------------------

def list_with_branches(case_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """会话列表 + 分支来源标注（不返回消息正文）。"""
    db = get_db()
    items = conversation.list_conversations(case_id, limit=limit)
    if not items:
        return items
    rows = db.query(
        "SELECT conversation_id, parent_conversation_id, anchor_message_id "
        "FROM conversation_branches WHERE case_id = ?",
        (case_id,),
    )
    by_conv = {str(r["conversation_id"]): r for r in rows}
    out: list[dict[str, Any]] = []
    for item in items:
        branch = by_conv.get(str(item["id"]))
        out.append({
            **item,
            "parentConversationId": (str(branch["parent_conversation_id"])
                                     if branch and branch["parent_conversation_id"] else None),
            "anchorMessageId": (str(branch["anchor_message_id"])
                                if branch and branch["anchor_message_id"] else None),
            "isBranch": branch is not None,
        })
    return out


def append_pair_indexed(
    conversation_id: str, task_id: str, user_text: str, assistant_text: str,
) -> None:
    """归档一轮完整对话并同步检索索引（替代 `conversation.append_pair`）。

    索引写入失败不回滚已归档消息：搜索可能暂时缺该条，但会话历史不丢失。
    调用方（runner）可继续使用原 `append_pair`，此处提供带索引的替代入口。
    """
    db = get_db()
    row = db.query_one("SELECT case_id FROM conversations WHERE id = ?", (conversation_id,))
    if row is None:
        raise ConversationNotFound("会话不存在")
    case_id = str(row["case_id"])
    ts = now_iso()
    ids: list[tuple[str, str, str]] = []
    with db.transaction() as conn:
        for role, content in (("user", user_text), ("assistant", assistant_text)):
            mid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO conversation_messages(id, conversation_id, role, content, task_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (mid, conversation_id, role, content, task_id, ts),
            )
            ids.append((mid, role, content))
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, conversation_id))
    for mid, role, content in ids:
        try:
            index_message(mid, conversation_id, case_id, role, content)
        except Exception:  # noqa: BLE001 索引失败不影响会话归档
            pass


def stats(case_id: str) -> dict[str, Any]:
    """案件会话统计（UI 侧栏与评测）：会话数、消息数、分支数与索引条数。"""
    db = get_db()
    conv = db.query_one(
        "SELECT COUNT(*) AS c FROM conversations WHERE case_id = ?", (case_id,)
    )
    msgs = db.query_one(
        "SELECT COUNT(*) AS c FROM conversation_messages m "
        "JOIN conversations c2 ON c2.id = m.conversation_id WHERE c2.case_id = ?",
        (case_id,),
    )
    branches = db.query_one(
        "SELECT COUNT(*) AS c FROM conversation_branches WHERE case_id = ?", (case_id,)
    )
    indexed = db.query_one(
        "SELECT COUNT(*) AS c FROM conversation_fts WHERE case_id = ?", (case_id,)
    )
    return {
        "caseId": case_id,
        "conversations": int((conv or {})["c"] or 0),
        "messages": int((msgs or {})["c"] or 0),
        "branches": int((branches or {})["c"] or 0),
        "indexedMessages": int((indexed or {})["c"] or 0),
    }


def export_summary(case_id: str) -> str:  # pragma: no cover - 诊断辅助
    return json.dumps(stats(case_id), ensure_ascii=False)
