"""Version-bound contract overview and immutable checklist snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from . import audit, citations, documents
from .cases import require_case_mode
from .storage import get_db
from .workspace import now_iso

INTENSITIES = ("basic", "standard", "deep")


class WorkflowError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# 引文近似定位：按标点/省略号切段，长段优先。仅用于定位，不改变证据状态。
_LOCATOR_SPLIT = re.compile(r"[。；;，,、：:！!？?…⋯]+|\.{3,}|\n+")
_LOCATOR_MIN_SEGMENT = 6
# 首尾噪音：模型常用省略号表示截断，或用引号/括号包裹摘录；只影响证据片段边界，
# 不改变内部内容（内部省略号仍视为不连续摘录，走近似定位与待核实）。
_EDGE_TRIM = " \t\n\r\u3000…⋯.。；;，,、：:！!？?“”\"'‘’「」『』【】（）()"


def _locator_segments(quote: str) -> list[str]:
    parts = [citations.normalize_text(p) for p in _LOCATOR_SPLIT.split(quote or "")]
    return sorted({p for p in parts if len(p) >= _LOCATOR_MIN_SEGMENT}, key=len, reverse=True)


def locate_quote(pages: list[dict[str, Any]], quote: str, preferred_page: int | None = None) -> dict[str, Any]:
    """把引文解析到页码与实际原文片段（证据绑定与前端原件定位共用）。

    - 页码优先采用模型给出的页码；该页不含引文时按全文定位纠正模型页码。
    - `exact` 仅在规范化后全文一致（允许空白/断行差异，Q-010）时为真；
      首尾的省略号/包裹引号视为截断标记不计入差异；引文内部含省略号或存在改述时
      返回最长可匹配片段，只用于定位，不能当作已支持证据。
    - `ambiguous` 表示多页都出现引文且模型页码无法消歧，需人工确认页码。
    """
    norm_quote = citations.normalize_text(quote)
    if not norm_quote or not pages:
        return {"pageNo": None, "quote": "", "exact": False, "ambiguous": False}
    normalized = [(p, citations.normalize_text(str(p.get("text") or ""))) for p in pages]

    def choose(hits: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        picked = next((p for p in hits if p["pageNo"] == preferred_page), None)
        return (picked or hits[0]), picked is None and len(hits) > 1

    # 先按原引文匹配；再按去掉首尾截断标记后的引文匹配（仍是连续原文片段）
    candidates: list[tuple[str, str]] = [(quote, norm_quote)]
    trimmed = quote.strip(_EDGE_TRIM)
    trimmed_norm = citations.normalize_text(trimmed)
    if trimmed_norm and trimmed_norm != norm_quote:
        candidates.append((trimmed, trimmed_norm))
    for raw, candidate in candidates:
        exact = [p for p, text in normalized if candidate in text]
        if exact:
            page, ambiguous = choose(exact)
            return {"pageNo": page["pageNo"], "quote": raw, "exact": True, "ambiguous": ambiguous}
    for segment in _locator_segments(quote):
        hits = [p for p, text in normalized if segment in text]
        if hits:
            page, ambiguous = choose(hits)
            return {"pageNo": page["pageNo"], "quote": segment, "exact": False, "ambiguous": ambiguous}
    return {"pageNo": None, "quote": "", "exact": False, "ambiguous": False}


def get_overview(overview_id: str) -> dict[str, Any]:
    row = get_db().query_one("SELECT * FROM contract_overviews WHERE id = ?", (overview_id,))
    if not row:
        raise WorkflowError("审查概览不存在")
    config = json.loads(row["config_json"])
    doc_id = config.get("primaryDocumentId")
    doc = get_db().query_one(
        "SELECT status, active_version_id FROM documents WHERE id = ? AND case_id = ?",
        (doc_id, row["case_id"]),
    ) if doc_id else None
    pages = documents.get_pages(doc_id) if doc else []
    has_text = any(p["text"].strip() for p in pages)
    low_confidence = any(
        p["ocrConfidence"] is not None and p["ocrConfidence"] < citations.LOW_OCR_CONFIDENCE
        for p in pages
    )
    version_changed = bool(doc and doc["active_version_id"] != config.get("primaryVersionId"))
    parse_status = (
        "no_document" if not doc_id else "missing" if not doc else
        "stale" if version_changed else "limited_scope" if has_text and
        (doc["status"] == "partial" or low_confidence) else
        "ready" if has_text and doc["status"] == "ready" else "parsing"
    )
    return {
        "id": row["id"], "caseId": row["case_id"], "revision": row["revision"],
        "config": config, "parseStatus": parse_status,
        "pageCount": len(pages), "updatedAt": row["updated_at"],
        "latestChecklistTaskId": _latest_checklist_task(row["case_id"], row["id"]),
    }


def _latest_checklist_task(case_id: str, overview_id: str) -> str | None:
    rows = get_db().query(
        "SELECT id, client_position_json FROM tasks WHERE case_id = ? AND mode = 'contract_review' "
        "ORDER BY created_at DESC", (case_id,),
    )
    for row in rows:
        context = json.loads(row["client_position_json"] or "{}")
        if context.get("workflowKind") == "contract_checklist" and context.get("overviewId") == overview_id:
            return row["id"]
    return None


def create_overview(case_id: str) -> dict[str, Any]:
    require_case_mode(case_id, "contract_review")
    db = get_db()
    with db.transaction() as conn:
        prior = conn.execute("SELECT id FROM contract_overviews WHERE case_id = ?", (case_id,)).fetchone()
        if prior:
            overview_id = prior["id"]
        else:
            overview_id = str(uuid.uuid4())
            ts = now_iso()
            conn.execute("INSERT INTO contract_overviews(id, case_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                         (overview_id, case_id, ts, ts))
    return get_overview(overview_id)


def update_overview(overview_id: str, config: dict[str, Any], expected_revision: int) -> dict[str, Any]:
    overview = get_overview(overview_id)
    require_case_mode(overview["caseId"], "contract_review")
    allowed = {"primaryDocumentId", "stance", "reviewIntensity", "limitedScopeAcknowledged",
               "contractType", "transactionGoal", "governingLaw"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise WorkflowError("概览配置不合法")
    clean = dict(config)
    for field in ("primaryDocumentId", "stance", "contractType", "transactionGoal", "governingLaw"):
        if field in clean and (not isinstance(clean[field], str) or len(clean[field]) > 4000):
            raise WorkflowError("概览字段不合法")
    if clean.get("reviewIntensity") not in INTENSITIES:
        raise WorkflowError("请选择基础、标准或深度审查")
    if "limitedScopeAcknowledged" in clean and not isinstance(clean["limitedScopeAcknowledged"], bool):
        raise WorkflowError("限定范围确认不合法")
    doc_id = clean.get("primaryDocumentId")
    if doc_id:
        doc = get_db().query_one(
            "SELECT active_version_id FROM documents WHERE id = ? AND case_id = ?",
            (doc_id, overview["caseId"]),
        )
        if not doc:
            raise WorkflowError("主合同不属于当前任务")
        clean["primaryVersionId"] = doc["active_version_id"]
    with get_db().transaction() as conn:
        changed = conn.execute(
            "UPDATE contract_overviews SET config_json = ?, revision = revision + 1, updated_at = ? "
            "WHERE id = ? AND revision = ?",
            (_json(clean), now_iso(), overview_id, expected_revision),
        )
        if changed.rowcount != 1:
            raise WorkflowError("VERSION_CONFLICT")
        conn.execute("UPDATE contract_checklist_versions SET status = 'stale' WHERE overview_id = ?", (overview_id,))
        audit.append(conn, action="contract.overview.update", resource_type="contract_overview",
                     resource_id=overview_id, case_id=overview["caseId"],
                     metadata={"revision": expected_revision + 1})
    return get_overview(overview_id)


def require_ready(overview_id: str) -> dict[str, Any]:
    overview = get_overview(overview_id)
    require_case_mode(overview["caseId"], "contract_review")
    if not str(overview["config"].get("stance") or "").strip():
        raise WorkflowError("请确认审查立场")
    if overview["config"].get("reviewIntensity") not in INTENSITIES:
        raise WorkflowError("请确认审查强度")
    if overview["parseStatus"] == "limited_scope":
        if not overview["config"].get("limitedScopeAcknowledged"):
            raise WorkflowError("PARSE_INCOMPLETE：请补全材料或确认限定范围审查")
    elif overview["parseStatus"] != "ready":
        raise WorkflowError("PARSE_INCOMPLETE：主合同尚无当前版本的可核验原文")
    return overview


def get_checklist(overview_id: str, snapshot_id: str | None = None) -> dict[str, Any] | None:
    overview = get_overview(overview_id)
    row = get_db().query_one(
        "SELECT * FROM contract_checklist_versions WHERE overview_id = ? " +
        ("AND id = ?" if snapshot_id else "ORDER BY revision DESC LIMIT 1"),
        (overview_id, snapshot_id) if snapshot_id else (overview_id,),
    )
    if not row:
        return None
    stale = row["overview_revision"] != overview["revision"] or overview["parseStatus"] in ("stale", "missing")
    return {
        "id": row["id"], "overviewId": overview_id, "revision": row["revision"],
        "overviewRevision": row["overview_revision"], "items": json.loads(row["items_json"]),
        "status": "stale" if stale else row["status"], "confirmedAt": row["confirmed_at"],
        "hash": row["snapshot_hash"],
    }


def update_checklist(
    overview_id: str, items: list[dict[str, Any]], expected_revision: int, *, _origin: str = "user",
) -> dict[str, Any]:
    overview = require_ready(overview_id)
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise WorkflowError("清单必须包含 1 至 200 项")
    clean = []
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise WorkflowError("清单项不合法")
        item_id = item.get("id") or str(uuid.uuid4())
        if not isinstance(item_id, str) or len(item_id) > 128 or item_id in ids:
            raise WorkflowError("清单 ID 重复或不合法")
        ids.add(item_id)
        title, criterion = item.get("title"), item.get("criterion")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise WorkflowError("请填写具体检查问题")
        if not isinstance(criterion, str) or not criterion.strip() or len(criterion) > 4000:
            raise WorkflowError("请填写判断标准")
        category = item.get("category", "custom")
        if category not in ("subject", "business", "legal", "completeness", "custom"):
            raise WorkflowError("清单分类不合法")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise WorkflowError("清单启用状态不合法")
        # Manual criteria are not contract facts and must not fabricate source references.
        clean.append({"id": item_id, "title": title.strip(), "criterion": criterion.strip(),
                      "category": category, "enabled": enabled, "origin": _origin,
                      "relatedSources": []})
    db = get_db()
    snapshot_id = str(uuid.uuid4())
    with db.transaction() as conn:
        current_overview = conn.execute("SELECT revision FROM contract_overviews WHERE id = ?", (overview_id,)).fetchone()
        if current_overview["revision"] != overview["revision"]:
            raise WorkflowError("VERSION_CONFLICT")
        latest = conn.execute("SELECT MAX(revision) AS revision FROM contract_checklist_versions WHERE overview_id = ?",
                              (overview_id,)).fetchone()["revision"] or 0
        if latest != expected_revision:
            raise WorkflowError("VERSION_CONFLICT")
        conn.execute(
            "INSERT INTO contract_checklist_versions(id, overview_id, revision, overview_revision, items_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'draft', ?)",
            (snapshot_id, overview_id, latest + 1, overview["revision"], _json(clean), now_iso()),
        )
        audit.append(conn, action="contract.checklist.save", resource_type="contract_checklist",
                     resource_id=snapshot_id, case_id=overview["caseId"],
                     metadata={"revision": latest + 1, "itemCount": len(clean)})
    return get_checklist(overview_id, snapshot_id)


def confirm_checklist(overview_id: str, expected_revision: int) -> dict[str, Any]:
    overview = require_ready(overview_id)
    snapshot = get_checklist(overview_id)
    if not snapshot or snapshot["revision"] != expected_revision:
        raise WorkflowError("VERSION_CONFLICT")
    if snapshot["status"] == "stale":
        raise WorkflowError("SNAPSHOT_STALE")
    if not any(item["enabled"] for item in snapshot["items"]):
        raise WorkflowError("请至少启用一个清单项")
    if snapshot["status"] == "confirmed":
        return snapshot
    digest = hashlib.sha256(_json([overview["config"], snapshot["items"]]).encode("utf-8")).hexdigest()
    with get_db().transaction() as conn:
        row = conn.execute("SELECT revision FROM contract_overviews WHERE id = ?", (overview_id,)).fetchone()
        latest = conn.execute("SELECT MAX(revision) AS revision FROM contract_checklist_versions WHERE overview_id = ?",
                              (overview_id,)).fetchone()["revision"]
        if row["revision"] != snapshot["overviewRevision"] or latest != expected_revision:
            raise WorkflowError("VERSION_CONFLICT")
        conn.execute("UPDATE contract_checklist_versions SET status = 'confirmed', confirmed_at = ?, snapshot_hash = ? "
                     "WHERE id = ? AND status = 'draft'", (now_iso(), digest, snapshot["id"]))
        audit.append(conn, action="contract.checklist.confirm", resource_type="contract_checklist",
                     resource_id=snapshot["id"], case_id=overview["caseId"],
                     metadata={"revision": expected_revision, "snapshotHash": digest})
    return get_checklist(overview_id, snapshot["id"])


def start_checklist(overview_id: str, expected_revision: int, model_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    from .agent.runner import AgentRunner
    from .agent.tasks import create_task

    overview = require_ready(overview_id)
    if overview["revision"] != expected_revision:
        raise WorkflowError("VERSION_CONFLICT")
    previous = get_checklist(overview_id)
    task = create_task(
        overview["caseId"], "contract_review", "生成合同审查清单",
        client_position={"workflowKind": "contract_checklist", "overviewId": overview_id,
                         "overviewRevision": expected_revision, "checklistRevision": previous["revision"] if previous else 0},
        model_policy=model_policy or {"remote": True},
    )
    return AgentRunner.instance().start(task["id"])


def _model_json(state: dict[str, Any], system: str, user: str, schema: str, cancel_check) -> dict[str, Any]:
    """Fixed jobs keep the existing gateway but enforce egress and budget boundaries."""
    from . import policy, redaction
    from .agent import budget, model_gateway, tasks

    task_id, case_id = state["task_id"], state["case_id"]
    config = state.get("model_policy") or {}
    if not config.get("remote", True):
        raise WorkflowError("未启用模型，不能生成 AI 清单")
    case = require_case_mode(case_id, "contract_review")
    budget.ensure(task_id)
    budget.check(task_id, "model_call")
    model_id = config.get("modelId") or model_gateway.DEFAULT_MODEL
    # Fail closed: a redaction failure must never fall back to sending the original.
    try:
        masked_system = redaction.apply(case_id, system)["redactedText"]
        masked_user = redaction.apply(case_id, user)["redactedText"]
    except Exception as exc:
        raise WorkflowError("脱敏失败，未发送合同内容") from exc
    payload_hash = policy.payload_digest(masked_system + "\n" + masked_user)
    decision = policy.decide(policy.CAP_NETWORK_MODEL, case["permissionPreset"])
    if decision == "deny":
        raise WorkflowError("严格本地模式不允许模型外发")
    if decision == "require_approval" and not policy.has_grant(
        policy.CAP_NETWORK_MODEL, task_id, case_id, payload_hash,
    ):
        approval = policy.create_approval(
            task_id, case_id, policy.CAP_NETWORK_MODEL,
            model_gateway.provider_label(model_gateway.provider_for_model(model_id)),
            payload_hash, f"脱敏后合同内容与审查设置，共 {len(masked_user)} 字",
            fallback="拒绝后保留原文和手工清单，不生成 AI 审查结论",
        )
        result = policy.wait_for_decision(approval["approvalId"], task_id, cancel_check)
        if cancel_check():
            raise tasks.TaskCancelled("用户停止")
        if result != "approved":
            raise WorkflowError("外发未授权，未生成 AI 清单")
        tasks.update_lifecycle(task_id, "RUNNING", "contract_checklist")
    if cancel_check():
        raise tasks.TaskCancelled("用户停止")
    budget.spend(task_id, "model_call")
    result, usage = model_gateway.generate_json(
        masked_system, masked_user, schema, model=model_id, cancel_check=cancel_check,
        reasoning_effort=model_gateway.effective_reasoning_effort(config.get("reasoningEffort"), model_id),
        on_delta=lambda _text: None,
    )
    budget.spend(task_id, "model_call", count=0,
                 tokens=usage["promptTokens"] + usage["completionTokens"])

    def restore(value):
        if isinstance(value, str):
            return redaction.restore(case_id, value)
        if isinstance(value, list):
            return [restore(v) for v in value]
        if isinstance(value, dict):
            return {k: restore(v) for k, v in value.items()}
        return value

    return restore(result)


def _validated_model(state, system, user, model_type, cancel_check):
    """Only schema failures get two repairs; network and authorization failures propagate."""
    from pydantic import ValidationError

    schema = _json(model_type.model_json_schema())
    repair = ""
    for attempt in range(3):
        response = _model_json(state, system + repair, user, schema, cancel_check)
        try:
            return model_type.model_validate(response)
        except ValidationError as exc:
            if attempt == 2:
                raise WorkflowError("MODEL_SCHEMA_INVALID：结构修复达到上限，请重试或人工审查") from None
            # Error input values can contain contract text; send only schema paths and codes.
            errors = [{"path": list(error["loc"]), "code": error["type"]}
                      for error in exc.errors(include_input=False, include_url=False)]
            repair = "\n上次输出结构不符合 Schema，请重新输出完整 JSON。错误位置与类型：" + _json(errors)
    raise WorkflowError("MODEL_SCHEMA_INVALID")


def run_checklist(state: dict[str, Any], emit, cancel_check) -> dict[str, Any]:
    from langgraph.graph import END, StateGraph
    from pydantic import BaseModel, ConfigDict, Field
    from typing import Literal
    from .agent import tasks

    context = state["contract_input"]
    overview = require_ready(context["overviewId"])
    if overview["revision"] != context["overviewRevision"]:
        raise WorkflowError("SNAPSHOT_STALE")

    class GeneratedItem(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str = Field(min_length=1, max_length=200)
        criterion: str = Field(min_length=1, max_length=4000)
        category: Literal["subject", "business", "legal", "completeness", "custom"]

    class GeneratedChecklist(BaseModel):
        model_config = ConfigDict(extra="forbid")
        items: list[GeneratedItem] = Field(min_length=1, max_length=200)

    def generate(current):
        if cancel_check():
            raise tasks.TaskCancelled("用户停止")
        emit("status", {"lifecycle": "RUNNING", "stage": "contract_checklist"})
        pages = documents.get_pages(overview["config"]["primaryDocumentId"])
        material = "\n".join(f"[第{p['pageNo']}页]\n{p['text']}" for p in pages)
        if len(material) > 200_000:
            raise WorkflowError("合同超出单次清单生成范围，请拆分材料；未静默截断")
        validated = _validated_model(
            state, "你是 LawBuddy。根据合同原文、用户立场和审查强度生成具体审查问题与判断标准。"
            "检查标准不是已确认事实，不编造条款或法律依据。只覆盖当前可核验材料，缺失范围需人工核实。",
            _json(overview["config"]) + "\n" + material,
            GeneratedChecklist, cancel_check,
        )
        seen = set()
        items = []
        for item in validated.items:
            key = citations.normalize_text(item.title)
            if key not in seen:
                seen.add(key)
                items.append({**item.model_dump(), "id": str(uuid.uuid4()), "enabled": True})
        return {"items": items}

    def persist(current):
        if cancel_check():
            raise tasks.TaskCancelled("用户停止")
        latest = require_ready(overview["id"])
        if latest["revision"] != context["overviewRevision"]:
            raise WorkflowError("SNAPSHOT_STALE")
        update_checklist(overview["id"], current["items"], context["checklistRevision"], _origin="ai")
        emit("assistant_message", {"content": "审查清单草案已生成，请逐项核对、编辑并确认后开始风险审查。"})
        emit("turn_complete", {"rounds": 1, "reason": "completed"})
        return {"answer": "审查清单草案已生成，请核对并确认。"}

    graph = StateGraph(dict)
    graph.add_node("generate", generate)
    graph.add_node("persist", persist)
    graph.set_entry_point("generate")
    graph.add_edge("generate", "persist")
    graph.add_edge("persist", END)
    return graph.compile().invoke({})


def start_risk_review(
    overview_id: str, snapshot_id: str, request_id: str, model_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .agent.runner import AgentRunner
    from .agent import tasks

    overview = require_ready(overview_id)
    snapshot = get_checklist(overview_id, snapshot_id)
    if not snapshot or snapshot["status"] != "confirmed":
        raise WorkflowError("CHECKLIST_UNCONFIRMED")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise WorkflowError("clientRequestId 不合法")
    previous = get_db().query_one(
        "SELECT task_id, snapshot_id FROM contract_review_runs WHERE overview_id = ? AND request_id = ?",
        (overview_id, request_id),
    )
    if previous:
        if previous["snapshot_id"] != snapshot_id:
            raise WorkflowError("IDEMPOTENCY_CONFLICT")
        return tasks.get_task(previous["task_id"])
    run_id = str(uuid.uuid4())
    task = tasks.create_task(
        overview["caseId"], "contract_review", "按已确认清单逐项审查合同",
        client_position={"workflowKind": "contract_risk", "runId": run_id},
        model_policy=model_policy or {"remote": True},
    )
    try:
        with get_db().transaction() as conn:
            conn.execute(
                "INSERT INTO contract_review_runs(id, overview_id, snapshot_id, task_id, request_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (run_id, overview_id, snapshot_id, task["id"], request_id, now_iso()),
            )
    except Exception:
        tasks.update_lifecycle(task["id"], "CANCELLED")
        prior = get_db().query_one(
            "SELECT task_id, snapshot_id FROM contract_review_runs WHERE overview_id = ? AND request_id = ?",
            (overview_id, request_id),
        )
        if prior and prior["snapshot_id"] == snapshot_id:
            return tasks.get_task(prior["task_id"])
        raise
    return AgentRunner.instance().start(task["id"])


def get_risk_run(overview_id: str) -> dict[str, Any] | None:
    overview = get_overview(overview_id)
    run = get_db().query_one(
        "SELECT * FROM contract_review_runs WHERE overview_id = ? ORDER BY created_at DESC LIMIT 1", (overview_id,),
    )
    if not run:
        return None
    from .agent.tasks import get_task

    snapshot = get_checklist(overview_id, run["snapshot_id"])
    results = get_db().query("SELECT result_json FROM contract_item_results WHERE run_id = ?", (run["id"],))
    items = [json.loads(row["result_json"]) for row in results]
    applied = get_db().query(
        "SELECT risk_id, target_id, revision FROM word_risk_applications WHERE run_id = ?", (run["id"],),
    )
    for item in items:
        for risk in item["risks"]:
            risk["applications"] = [{"targetId": entry["target_id"], "revision": entry["revision"],
                                     "applyState": "applied"} for entry in applied if entry["risk_id"] == risk["id"]]
    completed = {item["itemId"] for item in items}
    if run["status"] == "partial":
        items.extend({
            "itemId": item["id"], "title": item["title"], "status": "needs_human",
            "reason": "此项执行未完成，未形成审查结论。请恢复作业或人工核实。",
            "reasonCode": "REVIEW_INCOMPLETE", "cite": None, "risks": [],
        } for item in snapshot["items"] if item["enabled"] and item["id"] not in completed)
    return {"id": run["id"], "task": get_task(run["task_id"]), "snapshotId": run["snapshot_id"],
            "reviewId": run["review_id"], "status": "stale" if snapshot["status"] == "stale" else run["status"],
            "items": items,
            "total": sum(1 for item in snapshot["items"] if item["enabled"]),
            "limitedScope": overview["parseStatus"] == "limited_scope"}


def run_risk_review(state: dict[str, Any], emit, cancel_check) -> dict[str, Any]:
    from pydantic import BaseModel, ConfigDict, Field
    from typing import Literal
    from . import contract
    from .agent import tasks

    run_id = state["contract_input"]["runId"]
    run = get_db().query_one("SELECT * FROM contract_review_runs WHERE id = ?", (run_id,))
    if not run:
        raise WorkflowError("审查作业不存在")
    overview = require_ready(run["overview_id"])
    snapshot = get_checklist(overview["id"], run["snapshot_id"])
    if not snapshot or snapshot["status"] != "confirmed":
        raise WorkflowError("SNAPSHOT_STALE")

    class Finding(BaseModel):
        model_config = ConfigDict(extra="forbid")
        riskLevel: Literal["high", "medium", "low"]
        riskNote: str = Field(min_length=1, max_length=4000)
        originalText: str = Field(max_length=4000)
        pageNo: int | None = None
        clauseRef: str = ""
        impact: str = ""
        suggestion: str = ""
        replacementText: str = ""

    class ItemResult(BaseModel):
        model_config = ConfigDict(extra="forbid")
        status: Literal["risk", "pass", "needs_human", "not_applicable"]
        reason: str = Field(min_length=1, max_length=4000)
        originalText: str = ""
        pageNo: int | None = None
        risks: list[Finding] = Field(default_factory=list, max_length=20)

    pages = documents.get_pages(overview["config"]["primaryDocumentId"])
    page_by_no = {p["pageNo"]: p for p in pages}
    material = "\n".join(f"[第{p['pageNo']}页]\n{p['text']}" for p in pages)
    if len(material) > 200_000:
        raise WorkflowError("合同超出单次风险分析范围，未静默截断原文")
    if not run["review_id"]:
        review = contract.create_review(
            overview["caseId"], overview["config"]["primaryDocumentId"],
            "custom", custom_role=overview["config"]["stance"],
            transaction_goal=overview["config"].get("transactionGoal"), task_id=state["task_id"],
        )
        # 逐页切分以绑定 pageNo，但 split_clauses 每次调用 seq 从 0 起，
        # 多页拼接须全局重排 seq，否则撞 contract_clauses 的 UNIQUE(review_id, seq)。
        clauses: list[dict[str, Any]] = []
        for page in pages:
            for clause in contract.split_clauses(page["text"]):
                clauses.append({**clause, "seq": len(clauses), "pageNo": page["pageNo"]})
        contract.save_clauses(review["id"], clauses)
        with get_db().transaction() as conn:
            conn.execute("UPDATE contract_review_runs SET review_id = ?, status = 'running' WHERE id = ?", (review["id"], run_id))
        run["review_id"] = review["id"]
    if run["status"] == "completed":
        return {"answer": "此清单审查已完成，历史结果保留。"}
    completed = {row["item_id"] for row in get_db().query("SELECT item_id FROM contract_item_results WHERE run_id = ?", (run_id,))}
    existing = get_db().query("SELECT result_json FROM contract_item_results WHERE run_id = ?", (run_id,))
    accumulated_risks = [risk for row in existing for risk in json.loads(row["result_json"]).get("risks", [])]

    def bind(text: str, quote: str, page_no: int | None) -> dict[str, Any]:
        version = overview["config"]["primaryVersionId"]
        located = locate_quote(pages, quote, page_no)
        located_page = page_by_no.get(located["pageNo"]) if located["pageNo"] is not None else None
        if located_page is not None and located["exact"] and not located["ambiguous"]:
            cite = citations.judge_claim(state["task_id"], overview["caseId"], text, {
                "documentId": overview["config"]["primaryDocumentId"], "versionId": version,
                "pageNo": located_page["pageNo"], "snippet": located["quote"] or quote,
            })
        elif located_page is not None:
            # 近似定位 / 页码歧义：保留可定位页码，但证据状态必须待核实
            ref = citations.make_source_ref({
                "documentId": overview["config"]["primaryDocumentId"], "versionId": version,
                "pageNo": located_page["pageNo"], "snippet": located["quote"] or quote,
            })
            cite = citations.create_claim(
                state["task_id"], overview["caseId"], "fact", text, citations.STATUS_NEEDS_HUMAN,
                "PAGE_AMBIGUOUS" if located["ambiguous"] else "QUOTE_MISMATCH", None, ref,
            )
        else:
            cite = citations.judge_claim(state["task_id"], overview["caseId"], text, None)
        return {"cite": cite, "pageNo": located_page["pageNo"] if located_page else None,
                # 证据片段与模型摘录不同（截断标记/近似匹配）时回传，供前端展示真实原文并定位
                "locatorQuote": located["quote"] if located["quote"] and located["quote"] != (quote or "") else ""}

    enabled = [item for item in snapshot["items"] if item["enabled"]]
    # 逐项审查的调用量/耗时/Token 与清单项数成正比，默认预算（300s/12 次）
    # 会在长合同上把任务打死，且恢复续跑时预算同 task_id 连续计数，旧限额
    # 永远无法续跑。这里按工作量放宽上限：只在更宽松时提高，已用量不重置
    # （A-002 费用护栏与降级语义保留），恢复任务时同样生效。
    from .agent import budget as agent_budget
    enabled_count = len(enabled)
    agent_budget.upgrade_limits(state["task_id"], {
        "maxModelCalls": 3 * enabled_count + 10,
        "maxWallSeconds": max(900, 90 * enabled_count),
        "maxTokens": (3 * enabled_count + 10) * (len(material) // 2 + 4000),
    })
    for item in enabled:
        if item["id"] in completed:
            continue
        if cancel_check():
            raise tasks.TaskCancelled("用户停止")
        if get_checklist(overview["id"], snapshot["id"])["status"] != "confirmed":
            raise WorkflowError("SNAPSHOT_STALE")
        try:
            parsed = _validated_model(
                state, "你是 LawBuddy。针对给定清单项审查合同，不能省略未发现问题的项。"
                "风险、通过或不适用判断均须摘录实际原文并给出真实页码；缺条款或信息不足标 needs_human。"
                "originalText 必须是原文中连续、可直接检索的片段，不得自行改写、拼接或插入省略号；"
                "pageNo 以原文的 [第N页] 标记为准。不编造法律依据。风险等级与证据状态不同，低置信度来源需人工核实。",
                _json({"settings": overview["config"], "check": item}) + "\n" + material,
                ItemResult, cancel_check,
            )
        except Exception:
            with get_db().transaction() as conn:
                conn.execute("UPDATE contract_review_runs SET status = 'partial' WHERE id = ?", (run_id,))
            raise
        bound = bind(parsed.reason, parsed.originalText, parsed.pageNo)
        cite = bound["cite"]
        status = parsed.status
        if status in ("pass", "not_applicable") and cite["claimStatus"] != "supported":
            status = "needs_human"
        risks = []
        for finding in parsed.risks:
            source = bind(finding.riskNote, finding.originalText, finding.pageNo)
            risks.append({**finding.model_dump(), "id": str(uuid.uuid4()), "claimId": source["cite"]["claimId"], "cite": source["cite"],
                          "pageNo": source["pageNo"] if source["pageNo"] is not None else finding.pageNo,
                          "locatorQuote": source["locatorQuote"],
                          "category": item["category"] if item["category"] in contract.RISK_CATEGORIES else "legal",
                          "confidence": "high" if source["cite"]["claimStatus"] == "supported" else "low"})
        if parsed.status == "risk" and not risks:
            status = "needs_human"
        result = {"itemId": item["id"], "title": item["title"], "status": status, "reason": parsed.reason,
                  "cite": cite, "risks": risks}
        if get_checklist(overview["id"], snapshot["id"])["status"] != "confirmed":
            raise WorkflowError("SNAPSHOT_STALE")
        with get_db().transaction() as conn:
            conn.execute("INSERT INTO contract_item_results VALUES (?, ?, ?)", (run_id, item["id"], _json(result)))
        accumulated_risks.extend(risks)
        completed.add(item["id"])
        emit("status", {"lifecycle": "RUNNING", "stage": f"contract_risk {len(completed)}/{len(enabled)}"})
        tasks.save_checkpoint(state["task_id"], "contract_risk", {"runId": run_id, "completedItems": sorted(completed)})
    contract.save_risk_items(run["review_id"], accumulated_risks)
    contract.update_review_status(run["review_id"], "completed")
    with get_db().transaction() as conn:
        conn.execute("UPDATE contract_review_runs SET status = 'completed' WHERE id = ?", (run_id,))
    answer = f"清单审查完成，覆盖 {len(enabled)} 项。请核对原文与待人工核实项。"
    emit("assistant_message", {"content": answer})
    emit("turn_complete", {"rounds": len(enabled), "reason": "completed"})
    return {"answer": answer}


def apply_risk_suggestion(
    overview_id: str, risk_id: str, target_id: str, expected_revision: int, replacement: str, request_id: str,
) -> dict[str, Any]:
    from . import word

    overview = require_ready(overview_id)
    run = get_risk_run(overview_id)
    if not run or run["status"] != "completed":
        raise WorkflowError("SNAPSHOT_STALE：请先完成当前版本风险审查")
    matches = [(item, risk) for item in run["items"] for risk in item["risks"] if risk.get("id") == risk_id]
    if len(matches) != 1:
        raise WorkflowError("风险项不存在或已变化")
    item, risk = matches[0]
    claim = get_db().query_one("SELECT status FROM claims WHERE id = ?", (risk["claimId"],))
    if not claim or claim["status"] != "supported" or not risk["originalText"]:
        raise WorkflowError("SOURCE_MISMATCH：风险来源未核验，请先人工核实")
    copy = word.get_model(target_id)
    if copy["caseId"] != overview["caseId"] or copy["documentId"] != overview["config"]["primaryDocumentId"] or (
        copy["sourceVersionId"] != overview["config"]["primaryVersionId"]
    ):
        raise WorkflowError("SOURCE_MISMATCH：工作副本不属于本次合同原文版本")
    saved = word.suggest_replacement(
        target_id,
        str(risk["originalText"]),
        replacement,
        expected_revision,
        request_id,
        risk={
            "runId": run["id"], "riskId": risk_id,
            "riskNote": risk.get("riskNote"), "suggestion": risk.get("suggestion"),
            "cite": risk.get("cite"), "claimStatus": claim["status"],
        },
    )
    return {"workCopy": saved, "riskId": risk_id, "applyState": "applied"}
