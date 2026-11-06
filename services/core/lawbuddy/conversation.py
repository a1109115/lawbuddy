"""会话持久化（V26）：跨任务多轮对话的全文留存与召回。

背景：此前跨任务上下文依赖「前端 localStorage → agent.start history 参数 →
800 字/条截断」的三级有损链路，且合同审查 / 文书模式完全不传 history——
同一案件追问时，此前轮次的关键结论（含引证）随截断丢失，模型被迫重新
通读/检索，多耗轮次与 token。

设计：
- 一个 ChatSession（前端会话）对应一个 conversation；首轮任务由 service
  创建（或校验已存在且归属当前案件），id 随 AgentTaskInfo 回传前端续用；
- 每轮任务正常完成（含 max_rounds 强制终答）后，runner 把 user 全文与
  assistant 终稿全文成对落库——完整不截断，回答内的引证标注一并保留；
- 下一轮任务发起时 runner 从 Core 读最近 N 条全文注入 history（单条上限
  4000 字，远高于旧的 800 字），前端 history 参数退化为无会话时的兜底；
- conversation 不做跨案件共享（case_id 强制校验），与记忆系统的案件隔离
  原则一致（M-001）。
"""

from __future__ import annotations

import uuid
from typing import Any

from .storage import get_db
from .workspace import now_iso

# 召回注入 history 的条数与单条字符上限（12 条 × 4000 字 ≈ 5.8 万 token 内可控，
# 且自主循环历史超阈值时有压缩兜底）
HISTORY_LIMIT = 12
HISTORY_CHAR_LIMIT = 4000


class ConversationError(RuntimeError):
    pass


def ensure_conversation(conversation_id: str | None, case_id: str) -> str:
    """返回有效会话 id：传入时校验（存在且归属当前案件），缺省时新建。"""
    db = get_db()
    if conversation_id:
        row = db.query_one("SELECT id, case_id FROM conversations WHERE id = ?", (conversation_id,))
        if row is None:
            raise ConversationError("会话不存在")
        if row["case_id"] != case_id:
            raise ConversationError("会话不属于当前案件")
        return conversation_id
    cid = str(uuid.uuid4())
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO conversations(id, case_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (cid, case_id, ts, ts),
        )
    return cid


def append_pair(conversation_id: str, task_id: str, user_text: str, assistant_text: str) -> None:
    """归档一轮完整对话（user 提问全文 + assistant 终稿全文，不截断）。"""
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        for role, content in (("user", user_text), ("assistant", assistant_text)):
            conn.execute(
                "INSERT INTO conversation_messages(id, conversation_id, role, content, task_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), conversation_id, role, content, task_id, ts),
            )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, conversation_id))


def recent_history(conversation_id: str, limit: int = HISTORY_LIMIT) -> list[dict[str, str]]:
    """读取最近 N 条全文历史（时间正序），供 runner 注入 state.history。"""
    db = get_db()
    rows = db.query(
        "SELECT role, content FROM conversation_messages WHERE conversation_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (conversation_id, limit),
    )
    return [
        {"role": str(r["role"]), "text": str(r["content"] or "")[:HISTORY_CHAR_LIMIT]}
        for r in reversed(rows)
    ]


def list_conversations(case_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """案件的会话列表（更新时间倒序；UI 恢复/管理用）。"""
    db = get_db()
    rows = db.query(
        """
        SELECT c.id, c.case_id, c.created_at, c.updated_at,
               (SELECT COUNT(*) FROM conversation_messages m WHERE m.conversation_id = c.id) AS messages
        FROM conversations c WHERE c.case_id = ?
        ORDER BY c.updated_at DESC LIMIT ?
        """,
        (case_id, limit),
    )
    return [
        {
            "id": r["id"], "caseId": r["case_id"],
            "createdAt": r["created_at"], "updatedAt": r["updated_at"],
            "messageCount": int(r["messages"] or 0),
        }
        for r in rows
    ]
