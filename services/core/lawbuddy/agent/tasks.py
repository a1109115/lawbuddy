"""任务存储与事件流（DEV-AGT-001 / 005，技术方案 §4.1-4.2 / §13.1）。

- tasks + task_events（单调递增 seq，UNIQUE 约束保证幂等追加）；
- Renderer 重载后经 get_events(afterSeq) 增量续订（重连恢复的基础）；
- agent_checkpoints 每节点持久化（崩溃恢复入口，本迭代落结构）；
- settings 键值存储（模型 API Key 等；密钥环迁移在 DEV-FND-007，TODO 标注）。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .. import notify
from ..storage import get_db
from ..workspace import now_iso

LIFECYCLES = (
    "CREATED", "PLANNING", "RUNNING", "WAITING_APPROVAL", "WAITING_HUMAN", "PAUSED",
    "CANCELLING", "CANCELLED", "VERIFYING", "COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED",
)

# 工作流预设（PRD 13：法律阅卷四类功能入口；合同/文书模式分叉后续迭代）
WORKFLOW_PRESETS: dict[str, tuple[str, ...]] = {
    "legal_review": ("review_analysis", "visualization", "evidence_analysis", "strategy"),
    "contract_review": (),
    "document_generation": (),
}

# round 检查点保留个数（写放大控制：每轮全量重写 messages JSON，只留最近 N 个；
# 取消/失败续跑只需最近一个，保留 2 个防止边界竞态下误删）
KEEP_ROUND_CHECKPOINTS = 2


def validate_preset(mode: str, preset: str | None) -> str | None:
    """校验模式与预设组合；返回规范化后的预设（None = 模式默认精简流程）。"""
    if preset is None:
        return None
    allowed = WORKFLOW_PRESETS.get(mode, ())
    if preset not in allowed:
        raise TaskError(f"模式 {mode} 不支持工作流预设：{preset}")
    return preset


class TaskError(RuntimeError):
    pass


class TaskCancelled(RuntimeError):
    """用户停止任务（F-012）：流式循环内抛出；已接收内容保留，副作用不重复。"""


class ModelCallUnavailable(RuntimeError):
    """云端模型调用不可用（网关失败 / 响应中断 / 预算耗尽）。

    模型统一走云端，失败不做本地降级：任务置 FAILED，已完成的阅读与观察
    保留在检查点中，用户按 recover 指引处理后重新发起即可续跑。
    """

    def __init__(self, message: str, recover: str):
        super().__init__(message)
        self.recover = recover


def create_task(
    case_id: str,
    mode: str,
    request_text: str,
    selected_document_ids: list[str] | None = None,
    client_position: dict[str, Any] | None = None,
    model_policy: dict[str, Any] | None = None,
    tool_policy: dict[str, Any] | None = None,
    workflow_preset: str | None = None,
    budget: dict[str, Any] | None = None,
    skills: list[str] | None = None,
    history: list[dict[str, Any]] | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    db = get_db()
    request_text = (request_text or "").strip()
    if not request_text:
        raise TaskError("任务输入不能为空")
    if mode not in ("legal_review", "contract_review", "document_generation"):
        raise TaskError(f"无效模式：{mode}")
    from ..cases import require_case_mode

    require_case_mode(case_id, mode)
    workflow_preset = validate_preset(mode, workflow_preset)
    # 技能绑定校验（存在且启用）；失败即拒绝建任务
    if skills:
        from .. import skills as skill_store

        skills = skill_store.validate_skills([str(s) for s in skills])
    else:
        skills = []
    # 跨任务对话历史（多轮流程如 MCP 安装追问密钥）：只保留 user/assistant 文本并截断
    history_clean: list[dict[str, str]] = []
    for h in (history or [])[-12:]:
        entry = h if isinstance(h, dict) else {}
        role = str(entry.get("role") or "")
        text = str(entry.get("text") or "").strip()[:800]
        if role in ("user", "assistant") and text:
            history_clean.append({"role": role, "text": text})

    task_id = str(uuid.uuid4())
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO tasks(id, case_id, mode, lifecycle, stage, request_text, workflow_preset,
                              selected_document_ids_json, client_position_json, skills_json, history_json,
                              model_policy_json, tool_policy_json, budget_json, conversation_id, created_at, updated_at)
            VALUES (?, ?, ?, 'CREATED', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, case_id, mode, request_text, workflow_preset,
                json.dumps(selected_document_ids or [], ensure_ascii=False),
                json.dumps(client_position or {}, ensure_ascii=False),
                json.dumps(skills, ensure_ascii=False),
                json.dumps(history_clean, ensure_ascii=False),
                json.dumps(model_policy or {}, ensure_ascii=False),
                json.dumps(tool_policy or {}, ensure_ascii=False),
                json.dumps(budget or {}, ensure_ascii=False),
                conversation_id,
                ts, ts,
            ),
        )
        from .. import audit

        audit.append(
            conn,
            action="task.create",
            resource_type="task",
            resource_id=task_id,
            case_id=case_id,
            metadata={"mode": mode, "requestLen": len(request_text), "workflowPreset": workflow_preset},
        )
    append_event(task_id, "status", {"lifecycle": "CREATED"})
    return get_task(task_id)


def get_task(task_id: str) -> dict[str, Any]:
    db = get_db()
    row = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise TaskError("任务不存在")
    return {
        "id": row["id"],
        "caseId": row["case_id"],
        "mode": row["mode"],
        "workflowPreset": row["workflow_preset"],
        "lifecycle": row["lifecycle"],
        "stage": row["stage"],
        "request": row["request_text"],
        "modelPolicy": json.loads(row["model_policy_json"] or "{}"),
        "toolPolicy": json.loads(row["tool_policy_json"] or "{}"),
        # 通用任务上下文：合同审查模式承载 PRD 14.2 输入表（runner 注入 contract_input）
        "clientPosition": json.loads(row["client_position_json"] or "{}"),
        # 任务选定的材料范围（空 = 全案；阅卷 Loop 用于锁定语料边界）
        "selectedDocumentIds": json.loads(row["selected_document_ids_json"] or "[]"),
        # 本次任务绑定的技能 id 列表（/ 菜单选中；runner 注入 state.skills）
        "skills": json.loads(row["skills_json"] or "[]") if "skills_json" in row.keys() else [],
        # 跨任务对话历史（多轮流程续接上下文；runner 注入 state.history）
        "history": json.loads(row["history_json"] or "[]") if "history_json" in row.keys() else [],
        # 会话持久化锚点（V26）：同会话多轮任务共享 conversation，runner 归档问答全文
        "conversationId": row["conversation_id"] if "conversation_id" in row.keys() else None,
        # DEV-AGT-006：任务级预算覆盖（缺省用系统默认上限）
        "budget": json.loads(row["budget_json"] or "{}") if "budget_json" in row.keys() else {},
        "createdAt": row["created_at"],
        "finishedAt": row["finished_at"],
    }


def list_active_tasks(case_id: str, exclude_task_id: str | None = None) -> list[dict[str, Any]]:
    """返回案件下仍可能推进的任务，供新任务启动前做互斥取消。"""
    db = get_db()
    terminal = ("COMPLETED", "COMPLETED_WITH_WARNINGS", "CANCELLED", "FAILED")
    rows = db.query(
        "SELECT id FROM tasks WHERE case_id = ? AND lifecycle NOT IN (?, ?, ?, ?) ORDER BY created_at ASC",
        (case_id, *terminal),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        if exclude_task_id and row["id"] == exclude_task_id:
            continue
        try:
            out.append(get_task(row["id"]))
        except TaskError:
            continue
    return out


def update_lifecycle(task_id: str, lifecycle: str, stage: str | None = None) -> None:
    if lifecycle not in LIFECYCLES:
        raise TaskError(f"无效生命周期：{lifecycle}")
    db = get_db()
    ts = now_iso()
    terminal = lifecycle in ("CANCELLED", "COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED")
    finished = ts if terminal else None
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE tasks SET lifecycle = ?, stage = COALESCE(?, stage),
                             updated_at = ?, finished_at = COALESCE(?, finished_at)
            WHERE id = ?
            """,
            (lifecycle, stage, ts, finished, task_id),
        )
    if terminal:
        # 技术方案 §8.1：会话工作记忆随任务期结束清理（失败不阻断状态流转）
        try:
            from .. import memory

            memory.clear_session(task_id)
        except Exception:
            pass
    if lifecycle == "CANCELLED":
        # 取消任务时作废旧问题集：避免 UI 继续展示可提交的待答卡片
        try:
            from . import human_questions

            human_questions.cancel_pending(task_id)
        except Exception:
            pass
    append_event(task_id, "status", {"lifecycle": lifecycle, "stage": stage})


def append_event(task_id: str, event_type: str, payload: dict[str, Any]) -> int:
    """追加事件（seq 单调递增）；task_events 的 UNIQUE(task_id, seq) 保证不重复。

    落库后推送 JSON-RPC notification（REQ-AI-001：推送 + 轮询兜底）；
    事件形状与 get_events 返回一致，对端可复用同一处理逻辑。
    """
    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS last FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()
        seq = row["last"] + 1
        conn.execute(
            "INSERT INTO task_events(task_id, seq, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, seq, event_type, json.dumps(payload, ensure_ascii=False), ts),
        )
    notify.emit(
        "agent.event",
        {"taskId": task_id, "event": {"seq": seq, "type": event_type, "payload": payload, "createdAt": ts}},
    )
    return seq


def get_events(task_id: str, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    db = get_db()
    rows = db.query(
        """
        SELECT seq, event_type, payload_json, created_at FROM task_events
        WHERE task_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?
        """,
        (task_id, after_seq, limit),
    )
    return [
        {
            "seq": r["seq"],
            "type": r["event_type"],
            "payload": json.loads(r["payload_json"] or "{}"),
            "createdAt": r["created_at"],
        }
        for r in rows
    ]


def save_checkpoint(task_id: str, node: str, state: dict[str, Any], parent_id: str | None = None) -> str:
    db = get_db()
    checkpoint_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO agent_checkpoints(checkpoint_id, task_id, node, state_json, parent_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (checkpoint_id, task_id, node, json.dumps(state, ensure_ascii=False), parent_id, now_iso()),
        )
        # 写放大控制（DEV-AGT-005）：自主循环每轮全量重写 messages JSON，
        # 只保留最近 KEEP_ROUND_CHECKPOINTS 个 round 检查点；阶段检查点
        # （understand/corpus/read/…/verify）是恢复依赖，不清理。
        if node.startswith("round_"):
            conn.execute(
                """
                DELETE FROM agent_checkpoints
                WHERE task_id = ? AND node LIKE 'round_%'
                  AND checkpoint_id NOT IN (
                    SELECT checkpoint_id FROM agent_checkpoints
                    WHERE task_id = ? AND node LIKE 'round_%'
                    ORDER BY created_at DESC, checkpoint_id DESC LIMIT ?
                  )
                """,
                (task_id, task_id, KEEP_ROUND_CHECKPOINTS),
            )
    return checkpoint_id


def latest_checkpoint(task_id: str) -> dict[str, Any] | None:
    """最近一个检查点（阅卷 Loop 恢复入口，§11.1：以最近完整检查点为起点）。"""
    db = get_db()
    row = db.query_one(
        """
        SELECT checkpoint_id, node, state_json, created_at FROM agent_checkpoints
        WHERE task_id = ? ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1
        """,
        (task_id,),
    )
    if row is None:
        return None
    try:
        state = json.loads(row["state_json"] or "{}")
    except json.JSONDecodeError:
        return None
    return {"node": row["node"], "state": state, "createdAt": row["created_at"]}


# ---- 设置（模型 API Key 等；敏感值经 DPAPI 加密存储，DEV-FND-007 最小版） ----

# 这些键的值不进日志、不进审计、密文落盘（含各服务商专属键）。
# 自定义服务商的键名在运行时生成（model.<providerId>ApiKey），由
# is_sensitive_setting 统一识别，新增服务商无需再改这张表。
SENSITIVE_SETTINGS = {
    "model.apiKey",
    "model.zhipuApiKey",
    "model.topenrouterApiKey",
    "model.deepseekApiKey",
    "model.qwenApiKey",
    "model.kimiApiKey",
}

# 服务商专属密钥键：model.<providerId>ApiKey（providerId 为小写字母开头）
_PROVIDER_KEY_RE = re.compile(r"^model\.[a-z][a-z0-9-]*ApiKey$")


def is_sensitive_setting(key: str) -> bool:
    """是否敏感设置（静态清单 + 运行时可扩展的服务商专属密钥键）。"""
    name = str(key or "")
    return name in SENSITIVE_SETTINGS or bool(_PROVIDER_KEY_RE.match(name))


def is_device_setting(key: str) -> bool:
    """是否设备级设置（存固定位置的 profile 库，切换工作空间不丢失）。

    模型服务商 / 模型目录 / API Key 以及少量本机偏好属于设备级；案件相关的
    运行态仍留在工作空间库。判定保持前缀式，新增服务商或模型参数无需改这里。
    """
    name = str(key or "")
    if name.startswith("model."):
        return True
    return name in {"agent.maxConcurrent", "ui.theme", "ui.locale"}


def _settings_db(key: str):
    """按设置归属返回库连接：设备级 → profile，其余 → 当前工作空间。"""
    if is_device_setting(key):
        from ..storage import get_profile_db

        return get_profile_db()
    return get_db()


def get_setting(key: str) -> Any:
    from ..security import unprotect

    db = _settings_db(key)
    row = db.query_one("SELECT value_json FROM settings WHERE key = ?", (key,))
    if row is None:
        return None
    value = json.loads(row["value_json"])
    if is_sensitive_setting(key) and isinstance(value, str):
        return unprotect(value)
    return value


def set_setting(key: str, value: Any) -> None:
    from ..security import protect

    db = _settings_db(key)
    if is_sensitive_setting(key) and isinstance(value, str):
        value = protect(value)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now_iso()),
        )
        # 审计只记键名与是否敏感，绝不记敏感值（REQ-AUD-001 / S-009）
        from .. import audit

        audit.append(
            conn,
            action="settings.update",
            resource_type="setting",
            resource_id=key,
            metadata={"sensitive": is_sensitive_setting(key)},
        )
