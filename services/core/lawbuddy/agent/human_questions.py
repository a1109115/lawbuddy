"""文书生成必要用户决策：问题集持久化与暂停/恢复（升级方案 §5.3 / §8.2）。

设计边界：
- 只承载「会改变下一步生成行为」的决策（立场、诉讼地位、主位/备位请求、
  辩护方向、是否认罪、是否调解、请求金额取舍、是否采用某项敏感事实等）；
  普通缺失信息一律由模型写 xx 占位，不在此提问；
- 问题集落库后任务置 WAITING_HUMAN，前端刷新/应用重启仍可恢复待答卡片；
- 回答经 RPC 校验后写成一条 user 消息追加到最近检查点，任务从同一检查点
  续跑（不新建草稿、不重复用户消息与工具副作用）；重复提交幂等。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..storage import get_db
from ..workspace import now_iso

MAX_QUESTIONS = 3
QUESTION_KINDS = ("single_choice", "multi_choice", "text")


class HumanQuestionError(RuntimeError):
    pass


def _loads(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except json.JSONDecodeError:
        return default


def _row_to_set(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "taskId": row["task_id"],
        "conversationId": row["conversation_id"],
        "draftId": row["draft_id"],
        "title": row["title"],
        "contextDigest": row["context_digest"],
        "questions": _loads(row["questions_json"], []),
        "answers": _loads(row["answers_json"], []) or None,
        "status": row["status"],
        "checkpointId": row["checkpoint_id"],
        "createdAt": row["created_at"],
        "answeredAt": row["answered_at"],
    }


def _normalize_questions(raw: Any) -> list[dict[str, Any]]:
    """校验并规范化模型提交的问题集（一次最多 3 个，只问会改变生成行为的决策）。"""
    if not isinstance(raw, list) or not raw:
        raise HumanQuestionError("至少需要提出一个需要用户决策的问题")
    if len(raw) > MAX_QUESTIONS:
        raise HumanQuestionError(f"一次最多提出 {MAX_QUESTIONS} 个问题，请聚焦最关键的决策")
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HumanQuestionError("问题结构不合法")
        qid = str(item.get("id") or f"q{index + 1}").strip()[:64]
        if not qid or qid in seen:
            qid = f"q{index + 1}"
            while qid in seen:
                qid = f"{qid}_"
        seen.add(qid)
        title = str(item.get("title") or "").strip()[:200]
        if not title:
            raise HumanQuestionError("每个问题都需要一个明确的问题标题")
        kind = str(item.get("kind") or "single_choice").strip()
        if kind not in QUESTION_KINDS:
            raise HumanQuestionError(f"问题类型不合法：{kind}")
        options_raw = item.get("options")
        options: list[dict[str, Any]] = []
        if kind != "text":
            if not isinstance(options_raw, list) or len(options_raw) < 2:
                raise HumanQuestionError("选择题至少需要两个选项")
            opt_seen: set[str] = set()
            for oi, opt in enumerate(options_raw[:8]):
                if not isinstance(opt, dict):
                    raise HumanQuestionError("选项结构不合法")
                oid = str(opt.get("id") or f"o{oi + 1}").strip()[:64] or f"o{oi + 1}"
                while oid in opt_seen:
                    oid = f"{oid}_"
                opt_seen.add(oid)
                label = str(opt.get("label") or "").strip()[:80]
                if not label:
                    raise HumanQuestionError("选项需要有简短标题")
                entry: dict[str, Any] = {
                    "id": oid,
                    "label": label,
                    "description": str(opt.get("description") or "").strip()[:160] or None,
                }
                if opt.get("recommended"):
                    entry["recommended"] = True
                    reason = str(opt.get("recommendationReason") or "").strip()[:160]
                    if reason:
                        entry["recommendationReason"] = reason
                options.append(entry)
            # 推荐项至多一个（方案 §5.1）
            recommended = [o for o in options if o.get("recommended")]
            for extra in recommended[1:]:
                extra.pop("recommended", None)
                extra.pop("recommendationReason", None)
        questions.append({
            "id": qid,
            "title": title,
            "reason": str(item.get("reason") or "").strip()[:300] or None,
            "kind": kind,
            "options": options or None,
            "allowOther": bool(item.get("allowOther", kind == "text")),
            "allowDefer": bool(item.get("allowDefer", False)),
            "required": bool(item.get("required", True)),
            "affects": str(item.get("affects") or "").strip()[:60] or None,
        })
    return questions


def create_question_set(
    *,
    task_id: str,
    case_id: str,
    conversation_id: str | None,
    draft_id: str | None,
    title: str,
    context_digest: str | None,
    questions: Any,
) -> dict[str, Any]:
    """落库问题集（status=pending）；任务由调用方置 WAITING_HUMAN。"""
    normalized = _normalize_questions(questions)
    question_set_id = str(uuid.uuid4())
    ts = now_iso()
    with get_db().transaction() as conn:
        conn.execute(
            """
            INSERT INTO human_question_sets(
              id, task_id, conversation_id, draft_id, title, context_digest,
              questions_json, answers_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '[]', 'pending', ?)
            """,
            (question_set_id, task_id, conversation_id, draft_id,
             (title or "文书生成需要你的决策").strip()[:80],
             (context_digest or "").strip()[:400] or None,
             json.dumps(normalized, ensure_ascii=False), ts),
        )
        from .. import audit

        audit.append(
            conn,
            action="docgen.human_question",
            resource_type="task",
            resource_id=task_id,
            case_id=case_id,
            metadata={"questionSetId": question_set_id, "count": len(normalized),
                      "kinds": [q["kind"] for q in normalized]},
        )
    row = get_db().query_one("SELECT * FROM human_question_sets WHERE id = ?", (question_set_id,))
    return _row_to_set(row)


def get_pending(task_id: str) -> dict[str, Any] | None:
    """任务当前待回答的问题集（刷新/重启后恢复待答卡片）。"""
    row = get_db().query_one(
        "SELECT * FROM human_question_sets WHERE task_id = ? AND status = 'pending' "
        "ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    )
    return _row_to_set(row) if row else None


def get_question_set(question_set_id: str) -> dict[str, Any]:
    row = get_db().query_one("SELECT * FROM human_question_sets WHERE id = ?", (question_set_id,))
    if row is None:
        raise HumanQuestionError("问题集不存在")
    return _row_to_set(row)


def _validate_answers(question_set: dict[str, Any], answers: Any) -> list[dict[str, Any]]:
    if not isinstance(answers, list) or not answers:
        raise HumanQuestionError("请先回答需要决策的问题")
    by_id = {q["id"]: q for q in question_set["questions"]}
    normalized: dict[str, dict[str, Any]] = {}
    for item in answers:
        if not isinstance(item, dict):
            raise HumanQuestionError("回答结构不合法")
        qid = str(item.get("questionId") or "").strip()
        question = by_id.get(qid)
        if question is None:
            raise HumanQuestionError(f"回答包含未知问题：{qid or '空'}")
        option_ids = [str(o) for o in (item.get("optionIds") or []) if str(o or "").strip()]
        if option_ids:
            known = {o["id"] for o in question.get("options") or []}
            unknown = [oid for oid in option_ids if oid not in known]
            if unknown:
                raise HumanQuestionError(f"问题「{question['title']}」的选项无效：{', '.join(unknown)}")
            if question["kind"] == "single_choice" and len(option_ids) > 1:
                raise HumanQuestionError(f"问题「{question['title']}」只能选择一个选项")
        text = str(item.get("text") or "").strip()[:4000]
        deferred = bool(item.get("deferred"))
        if not option_ids and not text and not deferred:
            if question.get("required", True):
                raise HumanQuestionError(f"请回答问题「{question['title']}」")
            continue
        if deferred and not question.get("allowDefer"):
            raise HumanQuestionError(f"问题「{question['title']}」不支持暂不决定")
        normalized[qid] = {
            "questionId": qid,
            "optionIds": option_ids or None,
            "text": text or None,
            "deferred": True if deferred else None,
        }
    missing = [
        q["title"] for q in question_set["questions"]
        if q.get("required", True) and q["id"] not in normalized
    ]
    if missing:
        raise HumanQuestionError(f"还有问题未回答：{'、'.join(missing)}")
    ordered = [normalized[q["id"]] for q in question_set["questions"] if q["id"] in normalized]
    return ordered


def _answer_lines(question_set: dict[str, Any], answers: list[dict[str, Any]]) -> str:
    """把回答转成一条可复述给模型的用户消息（不含模型臆测，只陈述用户选择）。"""
    by_id = {q["id"]: q for q in question_set["questions"]}
    lines = ["【用户决策】已就文书生成中的必要问题作出选择："]
    for answer in answers:
        question = by_id.get(answer["questionId"]) or {}
        options = {o["id"]: o["label"] for o in question.get("options") or []}
        chosen = [options.get(oid, oid) for oid in (answer.get("optionIds") or [])]
        if answer.get("deferred"):
            value = "暂不决定（按待核实处理，不据此编造事实）"
        elif chosen and answer.get("text"):
            value = f"{'、'.join(chosen)}；补充说明：{answer['text']}"
        elif chosen:
            value = "、".join(chosen)
        else:
            value = answer.get("text") or ""
        suffix = f"（影响：{question['affects']}）" if question.get("affects") else ""
        lines.append(f"- {question.get('title') or answer['questionId']}：{value}{suffix}")
    lines.append(
        "请直接按上述决策继续完成文书起草，不要重复询问已回答的问题；"
        "这些决策属于用户确认信息（sourceType=user_confirmed），无需引证案件原文。"
    )
    return "\n".join(lines)


def answer(
    task_id: str,
    question_set_id: str,
    answers: Any,
    *,
    resume: bool = True,
) -> dict[str, Any]:
    """提交回答：校验 → 落库 → 追加用户决策消息到检查点 → 触发续跑。

    幂等：同一问题集已 answered 时直接返回当前状态，不重复追加消息或重启任务。
    """
    from . import tasks as task_store

    question_set = get_question_set(question_set_id)
    if question_set["taskId"] != task_id:
        raise HumanQuestionError("问题集不属于当前任务")
    if question_set["status"] == "answered":
        return {"questionSet": question_set, "resumed": False, "duplicate": True}
    if question_set["status"] == "cancelled":
        raise HumanQuestionError("任务已取消，无法提交回答")
    normalized = _validate_answers(question_set, answers)

    ts = now_iso()
    # 最近检查点（同一检查点续跑的依据）
    ckpt = task_store.latest_checkpoint(task_id)
    checkpoint_id = None
    if ckpt and isinstance(ckpt.get("state"), dict):
        state = dict(ckpt["state"])
        messages = list(state.get("messages") or [])
        if messages:
            messages.append({
                "role": "user",
                "content": _answer_lines(question_set, normalized),
            })
            state["messages"] = messages
            checkpoint_id = task_store.save_checkpoint(task_id, "human_answer", state)

    with get_db().transaction() as conn:
        conn.execute(
            "UPDATE human_question_sets SET status = 'answered', answers_json = ?, "
            "answered_at = ?, checkpoint_id = COALESCE(?, checkpoint_id) "
            "WHERE id = ? AND status = 'pending'",
            (json.dumps(normalized, ensure_ascii=False), ts, checkpoint_id, question_set_id),
        )

    task_store.append_event(task_id, "human_answer", {
        "questionSetId": question_set_id,
        "answers": normalized,
    })
    if resume:
        task_store.append_event(task_id, "task_resumed", {
            "questionSetId": question_set_id,
            "resumed": True,
        })
        from .runner import AgentRunner

        AgentRunner.instance().start(task_id)
    return {"questionSet": get_question_set(question_set_id), "resumed": bool(resume)}


def cancel_pending(task_id: str) -> int:
    """任务取消时作废旧问题集，避免 UI 继续展示可提交的待答卡片。"""
    with get_db().transaction() as conn:
        cur = conn.execute(
            "UPDATE human_question_sets SET status = 'cancelled', answered_at = ? "
            "WHERE task_id = ? AND status = 'pending'",
            (now_iso(), task_id),
        )
        return int(cur.rowcount or 0)
