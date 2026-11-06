"""派生文档编辑、版本比较与崩溃恢复（DEV-MOD-009 / PRD §18.3 / REQ-GEN-009/010）。

- 版本比较：行级 difflib 确定性差异（纯函数，无非确定因素，便于评测门禁）；
- 编辑会话：进入编辑视图 open、正常离开 close；closed_at 为空表示上次未正常退出，
  由 pending() 呈现未提交的自动保存，用户选择 recover（保留）或 discard（回到手动版本）；
- 自动保存节奏由 Core 约束：停止输入 2 秒触发一次 autosave（UI 防抖），
  连续编辑每 30 秒形成独立恢复点（同一恢复点在窗口内合并更新，避免版本泛滥）。

本模块不持有任何正文之外的敏感信息；版本比较只返回差异片段，审计只记长度与版本号。
"""

from __future__ import annotations

import difflib
import uuid
from datetime import datetime, timezone
from typing import Any

from . import audit
from .storage import get_db
from .workspace import now_iso


class EditorError(RuntimeError):
    pass


# 连续编辑期间恢复点间隔（PRD 18.3：每 30 秒形成恢复点）
AUTOSAVE_MERGE_SEC = 30
# diff 上下文行数与单次返回的最大差异块数（防止超大文本撑爆 Renderer）
DIFF_CONTEXT = 3
DIFF_MAX_HUNKS = 300

TARGET_KINDS = ("docgen_draft", "document_work_copy")


# ===========================================================================
# 版本比较（纯函数）
# ===========================================================================

def _lines(text: str) -> list[str]:
    if not text:
        return []
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _age_sec(created_at: str, now: str) -> float:
    """两个 ISO 时间戳的秒差（解析失败按“已过期”处理，宁多不少版本）。"""
    try:
        a = datetime.fromisoformat(created_at)
        b = datetime.fromisoformat(now)
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=timezone.utc)
        return (b - a).total_seconds()
    except ValueError:
        return float("inf")


def within_autosave_window(point_created_at: str, now: str) -> bool:
    """自动保存恢复点是否仍在合并窗口内（PRD 18.3：连续编辑每 30 秒形成恢复点）。"""
    return _age_sec(point_created_at, now) < AUTOSAVE_MERGE_SEC


def diff_texts(left: str, right: str, *, context: int = DIFF_CONTEXT,
               max_hunks: int = DIFF_MAX_HUNKS) -> dict[str, Any]:
    """行级差异：返回 {stats{added,removed}, hunks[], truncated}。

    每个 hunk 带双侧上下文行（leftLines/rightLines 均为原文行，UI 可直接并排渲染），
    间隔小于 2*context 的变更为同一块，避免碎片化展示。
    """
    left_lines = _lines(left)
    right_lines = _lines(right)
    matcher = difflib.SequenceMatcher(a=left_lines, b=right_lines, autojunk=False)

    added = removed = 0
    blocks: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        added += j2 - j1
        removed += i2 - i1
        if blocks and i1 - blocks[-1]["i2"] <= 2 * context:
            block = blocks[-1]
            block["i2"], block["j2"] = i2, j2
            block["tags"].append(tag)
        else:
            blocks.append({"i1": i1, "i2": i2, "j1": j1, "j2": j2, "tags": [tag]})

    truncated = len(blocks) > max_hunks
    hunks: list[dict[str, Any]] = []
    for block in blocks[:max_hunks]:
        a_start = max(0, block["i1"] - context)
        a_end = min(len(left_lines), block["i2"] + context)
        b_start = max(0, block["j1"] - context)
        b_end = min(len(right_lines), block["j2"] + context)
        tags = block["tags"]
        if all(t == "insert" for t in tags):
            kind = "insert"
        elif all(t == "delete" for t in tags):
            kind = "delete"
        else:
            kind = "replace"
        hunks.append({
            "type": kind,
            "leftStart": block["i1"] + 1,
            "rightStart": block["j1"] + 1,
            "leftLines": left_lines[a_start:a_end],
            "rightLines": right_lines[b_start:b_end],
        })
    return {
        "stats": {"added": added, "removed": removed},
        "hunks": hunks,
        "truncated": truncated,
    }


# ===========================================================================
# 编辑会话（崩溃恢复入口）
# ===========================================================================

def _has_uncommitted_autosave(target_id: str) -> bool:
    """自动保存版本新于最近手动版本（存在可恢复的未提交内容）。"""
    row = get_db().query_one(
        """
        SELECT 1 AS hit FROM docgen_draft_versions v
        WHERE v.draft_id = ?
          AND COALESCE((SELECT MAX(x.version_no) FROM docgen_draft_versions x
                        WHERE x.draft_id = ? AND x.kind = 'autosave'), 0)
            > COALESCE((SELECT MAX(x.version_no) FROM docgen_draft_versions x
                        WHERE x.draft_id = ? AND x.kind IN ('render','edit','restore')), 0)
        LIMIT 1
        """,
        (target_id, target_id, target_id),
    )
    if row is not None:
        return True
    word = get_db().query_one(
        "SELECT v.kind FROM word_work_copy_versions v JOIN word_work_copies w ON w.id = v.target_id "
        "WHERE w.id = ? AND v.revision = w.revision", (target_id,),
    )
    return bool(word and word["kind"] == "autosave")


def _pending_session(target_kind: str, target_id: str) -> dict[str, Any] | None:
    """同一目标上未关闭且含未提交自动保存的会话（崩溃残留，待用户恢复/放弃）。"""
    if not _has_uncommitted_autosave(target_id):
        return None
    return get_db().query_one(
        """
        SELECT s.* FROM editor_sessions s
        WHERE s.target_kind = ? AND s.target_id = ?
          AND s.closed_at IS NULL AND s.recovered_at IS NULL
        ORDER BY s.opened_at DESC LIMIT 1
        """,
        (target_kind, target_id),
    )


def open_session(target_kind: str, target_id: str, case_id: str) -> dict[str, Any]:
    """开启编辑会话；若目标上已有崩溃残留（未提交自动保存），复用该会话等待用户处理。"""
    if target_kind not in TARGET_KINDS:
        raise EditorError(f"不支持的编辑目标类型：{target_kind}")
    db = get_db()
    existing = _pending_session(target_kind, target_id)
    if existing is not None:
        return _row_to_session(existing, pending=True)

    ts = now_iso()
    session_id = str(uuid.uuid4())
    with db.transaction() as conn:
        # 未决（崩溃残留）会话已在上方复用；此处收尾同目标其它残留会话（无未提交内容）
        conn.execute(
            """
            UPDATE editor_sessions SET closed_at = ?
            WHERE target_kind = ? AND target_id = ? AND closed_at IS NULL AND recovered_at IS NULL
            """,
            (ts, target_kind, target_id),
        )
        conn.execute(
            """
            INSERT INTO editor_sessions(id, target_kind, target_id, case_id, opened_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, target_kind, target_id, case_id, ts),
        )
    return {
        "id": session_id,
        "targetKind": target_kind,
        "targetId": target_id,
        "caseId": case_id,
        "openedAt": ts,
        "pending": False,
    }


def close_session(session_id: str) -> dict[str, Any]:
    """正常离开编辑视图：关闭会话（下次启动不再提示恢复）。

    存在未提交自动保存时返回 keptAutosave=True：恢复点保留在版本历史中（内容不丢），
    但会话已正常关闭，不作为崩溃恢复项呈现；崩溃恢复只针对未关闭的会话。
    """
    db = get_db()
    row = db.query_one("SELECT * FROM editor_sessions WHERE id = ?", (session_id,))
    kept = False
    if row is not None and row["closed_at"] is None and row["target_kind"] in TARGET_KINDS:
        kept = _has_uncommitted_autosave(row["target_id"])
    with db.transaction() as conn:
        conn.execute(
            "UPDATE editor_sessions SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
            (now_iso(), session_id),
        )
    return {"closed": True, "sessionId": session_id, "keptAutosave": kept}


def close_all_open() -> int:
    """应用正常退出（Core `shutdown`）时收尾；强杀不会执行，会话留待恢复提示。"""
    with get_db().transaction() as conn:
        cur = conn.execute(
            "UPDATE editor_sessions SET closed_at = ? WHERE closed_at IS NULL",
            (now_iso(),),
        )
        closed = cur.rowcount or 0
    return closed


def mark_autosave(conn: Any, session_id: str, ts: str) -> None:
    """记录会话最近自动保存时间（复用 save_body 的写事务，不单独提交）。"""
    conn.execute(
        "UPDATE editor_sessions SET last_autosave_at = ? WHERE id = ? AND closed_at IS NULL",
        (ts, session_id),
    )


def _row_to_session(row: dict[str, Any], *, pending: bool = False, **extra: Any) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "targetKind": row["target_kind"],
        "targetId": row["target_id"],
        "caseId": row["case_id"],
        "openedAt": row["opened_at"],
        "lastAutosaveAt": row["last_autosave_at"],
        "pending": pending,
    }
    out.update(extra)
    return out


def list_pending(case_id: str | None = None) -> list[dict[str, Any]]:
    """未正常退出的编辑项：存在未提交自动保存（自动保存版本新于最近手动版本）。

    返回项含草稿标题与版本号，供启动/列表页提示用户恢复或放弃（REQ-GEN-009）。
    """
    sql = """
        SELECT s.id AS session_id, s.target_kind, s.target_id, s.case_id, s.opened_at,
               s.last_autosave_at, d.purpose AS draft_purpose, d.doc_type AS draft_doc_type,
               w.title AS word_title,
               CASE WHEN s.target_kind = 'document_work_copy' THEN
                 (SELECT MAX(v.revision) FROM word_work_copy_versions v
                  WHERE v.target_id = s.target_id AND v.kind = 'autosave')
               ELSE (SELECT MAX(v.version_no) FROM docgen_draft_versions v
                 WHERE v.draft_id = s.target_id AND v.kind = 'autosave') END AS autosave_version_no,
               CASE WHEN s.target_kind = 'document_work_copy' THEN
                 (SELECT MAX(v.revision) FROM word_work_copy_versions v
                  WHERE v.target_id = s.target_id AND v.kind != 'autosave')
               ELSE (SELECT MAX(v.version_no) FROM docgen_draft_versions v
                 WHERE v.draft_id = s.target_id
                   AND v.kind IN ('render','edit','restore')) END AS manual_version_no
        FROM editor_sessions s
        LEFT JOIN docgen_drafts d ON s.target_kind = 'docgen_draft' AND d.id = s.target_id
        LEFT JOIN word_work_copies w ON s.target_kind = 'document_work_copy' AND w.id = s.target_id
        WHERE s.closed_at IS NULL AND s.recovered_at IS NULL
    """
    params: tuple[Any, ...] = ()
    if case_id:
        sql += " AND s.case_id = ?"
        params = (case_id,)
    sql += " ORDER BY s.opened_at DESC"
    rows = [r for r in get_db().query(sql, params) if _has_uncommitted_autosave(r["target_id"])]
    return [
        {
            "sessionId": r["session_id"],
            "targetKind": r["target_kind"],
            "targetId": r["target_id"],
            "caseId": r["case_id"],
            "title": r["word_title"] if r["target_kind"] == "document_work_copy" else
            (r["draft_purpose"] or "（未填写请求事项）")
            if r["draft_doc_type"] is not None
            else "（草稿已删除）",
            "docType": r["draft_doc_type"],
            "openedAt": r["opened_at"],
            "lastAutosaveAt": r["last_autosave_at"],
            "autosaveVersionNo": r["autosave_version_no"],
            "lastManualVersionNo": r["manual_version_no"],
        }
        for r in rows
    ]


def resolve(session_id: str, action: str) -> dict[str, Any]:
    """处理崩溃恢复项：recover = 保留最近自动保存；discard = 回到上次手动版本。

    处理后会话关闭（recovered_at 落库），审计记动作与目标，不记正文。
    """
    if action not in ("recover", "discard"):
        raise EditorError("不支持的恢复动作")
    db = get_db()
    row = db.query_one("SELECT * FROM editor_sessions WHERE id = ?", (session_id,))
    if row is None:
        raise EditorError("编辑会话不存在")
    if row["recovered_at"]:
        raise EditorError("该恢复项已处理")
    if row["closed_at"] or _pending_session(row["target_kind"], row["target_id"]) is None:
        raise EditorError("编辑会话没有待恢复的自动保存")

    draft: dict[str, Any] | None = None
    if row["target_kind"] == "docgen_draft":
        from . import docgen

        if action == "recover":
            draft = docgen.promote_autosave(row["target_id"])
        else:
            draft = docgen.discard_autosaves(row["target_id"])
    elif row["target_kind"] == "document_work_copy":
        from . import word

        current = word.get_model(row["target_id"])
        if action == "recover":
            word.save_model(current["id"], current["model"], current["revision"], str(uuid.uuid4()), "edit")
        else:
            manual = get_db().query_one(
                "SELECT revision FROM word_work_copy_versions WHERE target_id = ? AND kind != 'autosave' "
                "ORDER BY revision DESC LIMIT 1", (current["id"],),
            )
            if manual:
                word.restore_version(current["id"], manual["revision"], current["revision"])

    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE editor_sessions
            SET recovered_at = ?, closed_at = COALESCE(closed_at, ?), recovery_action = ?
            WHERE id = ?
            """,
            (ts, ts, action, session_id),
        )
        audit.append(
            conn,
            action="editor.recovery",
            resource_type="editor_session",
            resource_id=session_id,
            case_id=row["case_id"],
            metadata={"action": action, "targetKind": row["target_kind"], "targetId": row["target_id"]},
        )
    return {"sessionId": session_id, "action": action, "draft": draft}
