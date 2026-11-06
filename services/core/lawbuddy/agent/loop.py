"""有状态阅卷 Agent Loop（docs/05-阅卷Agent升级方案.md §4-§6 / §9 / §11）。

与固定流程（graph.py 的 plan→retrieve→analyze→verify）的根本差异：

- 全量通读（用户决策 §6 修订）：语料在上下文预算内（≤ SINGLE_PASS_MAX_CHARS
  且 ≤ SINGLE_PASS_MAX_UNITS 页）时整卷一次性输入模型，逐页观察与案件工作
  记忆一次调用同时产出（memory_direct）；超大卷宗自动回退 48 页分批串行；
- 任务理解先行（§7.1）：生成任务解释卡（目标/交付物/立场/范围/完成条件），
  目标含糊时先完成可确定部分，关键问题进 openQuestions；
- 案件工作记忆（§5.3）：观察聚合为时间线/主体/关键事实/矛盾/待核实，
  作答基于全案记忆而非检索命中片段；
- 网络搜索（智谱 Web Search，§8.1 法律研究工具）：作答前做法源线索
  检索，结果明确标注为外部网络来源，不改变案件事实、不作为案件引证；
- 质量不受费用约束（§3.6）：通读与记忆生成不做 token/轮次预算
  拦截，token 仅计量统计；定向检索仍受工具调用预算约束（资源安全）；
- 失败可恢复（§11）：每阶段写检查点（阅读游标/观察/记忆），CANCELLED
  或 FAILED 任务从最近检查点续跑，已完成阅读不重复。

本地降级：仅离线确定性路径（model_policy.remote=False，评测/无远程）使用
本地规则观察与检索分析；云端调用失败一律置 FAILED，不产出本地结果。
合同审查（§10）共用同一 Loop，语料准备阶段完成条款切分与交叉引用解析。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from langgraph.graph import END, StateGraph

from . import model_gateway, tasks as task_store
from .graph import (
    AgentState,
    MAX_SEARCH_CALLS,
    ROLE_PROMPT,
    _extract_keywords,
    _extract_preview,
    _local_analysis,
    _restore_analysis,
    _system_prompt,
    build_final_body,
    persist_final_outputs,
    prepare_contract_review,
)

# 每次模型调用打包的阅读单元数（页）：仅超大卷宗回退分批时使用；
# 语料在上下文预算内时整卷一次性输入（用户决策：尽量不分批）
# 48 页 ≈ 1.2 万 token 输出（约 250 token/页），在 max_tokens=32768 内安全；
# 相比旧值 12 页，超大卷宗的分批模型调用次数降为 1/4
READ_BATCH_UNITS = 48
# 单页注入提示的文本上限（超长截断，观察仍覆盖全页语义由摘要承担）；
# 上下文扩容后放宽（1500 → 3000 字），长页更少被截断
PAGE_TEXT_MAX_CHARS = 3000
# 整卷单次通读的页数上限：单次调用需逐页产出观察并与记忆合并输出，输出受
# 服务商 max_tokens=32768 硬约束（逐页观察 ≈150 token/页 + 记忆 ≈3K token，
# 100 页 ≈ 1.8 万 token）；页数超限时整卷输出必被截断 → 回退分批模式
# （分批路径输出随批数摊薄，不受此限，页数不限）
SINGLE_PASS_MAX_UNITS = 100
# 整卷一次性输入的字符上限从模型上下文动态推导（_single_pass_char_cap）：
# (上下文 token − 输出预留) ÷ 1.2 token/字。更换更大上下文模型时改
# settings model.contextTokens 即自动放大，无需改代码
SINGLE_PASS_OUTPUT_RESERVE_TOKENS = 40_000  # 输出 + 思考 + 记忆余量


def _single_pass_char_cap() -> int:
    """整卷一次性输入允许的最大字符量（按模型上下文推导，最小 3 万字兜底）。"""
    from . import model_gateway

    budget = model_gateway.context_tokens() - SINGLE_PASS_OUTPUT_RESERVE_TOKENS
    return max(30_000, int(budget / 1.2))


# 记忆聚合提示中注入的观察条数上限（超大卷宗截断留痕）
OBSERVATIONS_IN_PROMPT_CAP = 240
# 本地记忆聚合的观察处理上限
LOCAL_OBSERVATION_CAP = 600
# 时间线/事实等记忆章节注入作答提示的上限；过小会逼下游（自主模式）逐页
# 重读原文补细节，整卷通读收益被抵消——按大案规模放宽
_MEMORY_SECTION_CAPS = {"timeline": 60, "entities": 24, "keyFacts": 60, "evidence": 24}

TASK_CARD_SCHEMA = """{
  "goal": "任务目标（25字内）",
  "deliverables": ["交付物（每条一句）"],
  "workflowPreset": "工作流预设四选一：review_analysis(阅卷分析报告)|visualization(时间线/人物关系图/流程与证据关系图)|evidence_analysis(证据卡与争点证据矩阵)|strategy(辩护策略方案比较)；按用户请求意图选择",
  "clientPosition": "委托人立场（能从输入确定的写明，否则写：未明确）",
  "mustInclude": ["必须覆盖的内容（如 不利事实、相反证据、待核实事项）"],
  "completionCriteria": ["完成条件（每条一句）"],
  "openQuestions": ["影响结论但当前无法确定的问题，无可空数组"]
}"""

OBSERVATION_SCHEMA = """{
  "observations": [
    {"unitId": "阅读单元标识（必须原样使用输入中【】括起的 unitId）",
     "summary": "本页内容摘要（40字内；无重要内容也要给出概括）",
     "facts": [{"text": "本页明确记载的事实（40字内）"}],
     "events": [{"date": "2023-03-15（无法确定写空字符串）", "event": "事件（30字内）"}],
     "issues": ["疑点或需核对事项（可空数组）"]}
  ]
}"""

CASE_MEMORY_SCHEMA = """{
  "timeline": [{"date": "2023-03-15", "event": "事件（40字内）", "sourceDoc": "来源文档标题", "pageNo": "页码（整数）"}],
  "entities": [{"name": "人物或机构", "role": "身份或与本案关系（20字内）", "sourceDoc": "来源文档标题"}],
  "keyFacts": [{"text": "关键事实（40字内）", "stance": "favorable或unfavorable或neutral", "sourceDoc": "来源文档标题", "pageNo": "页码（整数）"}],
  "contradictions": [{"topic": "矛盾主题（15字内）", "sideA": "一方说法及来源文档", "sideB": "另一方说法及来源文档"}],
  "evidence": [{"name": "证据名称", "type": "书证/物证/电子数据等", "sourceDoc": "来源文档标题", "purpose": "证明目的（30字内）"}],
  "openQuestions": ["待核实事项（每条一句）"]
}"""

_READ_SYSTEM = (
    f"{ROLE_PROMPT}，正在逐页阅读案件卷宗并产出结构化观察。遵守：\n"
    "1. 每个 unitId 必须原样返回，不得遗漏或合并页面；\n"
    "2. summary 概括本页实际内容（40字内）；无重要内容的页面也要概括"
    "（如“程序性文书页”），不得留空；\n"
    "3. facts 只写本页最关键的明确记载（每页最多 2 条，每条 40字内）；\n"
    "4. events 每页最多 2 条，date 只写页面中出现的时间形式，不确定写空字符串；\n"
    "5. issues 记录签名缺失、涂改、前后矛盾等需后续核对的疑点（每页最多 1 条）。"
)

_MEMORY_SYSTEM = (
    f"{ROLE_PROMPT}，正在基于全案逐页阅读观察建立案件工作记忆。遵守：\n"
    "1. 只使用观察中出现的信息，不推断观察之外的事实；\n"
    "2. sourceDoc 必须使用观察中出现的文档标题，pageNo 用观察中的页码；\n"
    "3. 同一人物或机构合并为一个条目；时间线按时间先后排列；\n"
    "4. 不同文档对同一事实表述不一致的列入 contradictions，两侧说法都保留；\n"
    "5. 观察中的疑点（issues）汇总到 openQuestions。"
)

# 整卷一次性输入的合并提示（通读 + 记忆一次完成，减少一次模型往返）。
# 输出纪律：单次调用要为每一页产出观察，输出总量受 max_tokens 约束，
# 每页条目数设上限防止整卷 JSON 被截断。观察不逐页输出原文摘录（quote）与
# 实体清单（entities）：下游聚合与引证不消费（引证需要精确原文时由定向
# 检索 document_get_pages 补齐，主体汇总由 memory.entities 承担），每页
# 少输出约 1/3 tokens，整卷通读耗时显著下降（2026-09-24 实证调优）
_READ_ALL_SYSTEM = (
    f"{ROLE_PROMPT}，正在整卷阅读全案材料。输入是全部页面文本，请分两步一次完成：\n"
    "1. 逐页细读：pages 数组按页给出观察——unitId 必须原样使用输入中【】括起的"
    "标识，不得遗漏或合并页面；summary 概括本页实际内容（40字内，无重要内容的"
    "页面也要概括，不得留空）；facts 只写本页最关键的明确记载（每页最多 2 条，"
    "每条 40字内）；events 每页最多 2 条，date 只写页面中出现的时间形式，不确定"
    "写空字符串；issues 记录签名缺失、涂改、前后矛盾等疑点（每页最多 1 条）；\n"
    "2. 基于逐页观察直接聚合 memory：只使用页面中出现的信息；sourceDoc 用"
    "文档标题、pageNo 用页码；同一人物或机构合并一条；时间线按时间先后；"
    "不同文档对同一事实表述不一致列入 contradictions（两侧说法都保留）；"
    "疑点汇总到 openQuestions。"
)

_READ_ALL_SCHEMA = """{
  "pages": [
    {"unitId": "阅读单元标识（必须原样使用输入中【】括起的 unitId）",
     "summary": "本页内容摘要（40字内；无重要内容也要给出概括）",
     "facts": [{"text": "本页明确记载的事实（40字内）"}],
     "events": [{"date": "2023-03-15（无法确定写空字符串）", "event": "事件（30字内）"}],
     "issues": ["疑点或需核对事项（可空数组）"]}
  ],
  "memory": %s
}""" % CASE_MEMORY_SCHEMA

_ANSWER_SYSTEM_SUFFIX = (
    "\n\n本次分析基于全案通读建立的记忆与定向检索命中：sourceDoc 使用材料中"
    "出现的文档标题；外部网络检索结果只能用于 legalBasis 参考，不得作为"
    "案件事实来源。"
)

# 检查点保留的 Loop 字段（runner 恢复时合并回初始状态）
_LOOP_FIELDS = (
    "task_card", "workflow_preset", "corpus_manifest", "corpus_version", "reading_queue", "unread_reasons",
    "observations", "case_memory", "memory_direct", "legal_research", "anchor_hits", "decisions",
    "contract_review_id", "contract_source", "contract_clauses", "contract_pages",
    "contract_full_text", "contract_type", "redaction_applied",
)

_DATE_RE = re.compile(r"^\d{4}(-\d{1,2}){0,2}")

# 降级原因的可读标签（仅离线确定性路径 remote=False 使用；云端调用失败不降级）
_DEGRADED_LABELS: dict[str, str] = {
    "local_mode": "未启用云端模型（离线确定性路径）",
}


def _degraded_label(reason: str | None) -> str:
    return _DEGRADED_LABELS.get(str(reason or ""), str(reason or "本地降级"))


_ENTITY_TYPE_LABELS = {
    "id_card": "身份证号", "bank_card": "银行卡号", "phone": "电话号码",
    "email": "邮箱", "plate": "车牌号",
}


class HumanInterventionRequired(RuntimeError):
    """脱敏阻断等必须人工确认的前置条件（§11.3）：任务转 WAITING_HUMAN，
    用户确认后重新 agent.start 从最近检查点续跑，已完成阅读不重复。"""

    def __init__(self, stage: str, blocked_types: dict[str, int]):
        self.stage = stage
        self.blocked_types = blocked_types
        labeled = {
            _ENTITY_TYPE_LABELS.get(t, t): n for t, n in sorted(blocked_types.items())
        }
        self.blocked_types_labeled = labeled
        type_desc = "、".join(f"{t}×{n}" for t, n in labeled.items())
        super().__init__(f"检测到 {sum(blocked_types.values())} 个待确认的高敏感实体（{type_desc}）")


class ReviewState(AgentState):
    """阅卷 Loop 状态（AgentState 之上叠加 §5 的有状态字段）。"""

    task_card: dict[str, Any]
    corpus_version: str
    corpus_manifest: list[dict[str, Any]]
    reading_queue: list[dict[str, Any]]
    unread_reasons: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    case_memory: dict[str, Any]
    # 整卷单次通读直接产出记忆的标记（True 时 synthesize 跳过模型聚合）
    memory_direct: bool
    # 整卷单次通读失败后禁用（本次任务回退分批；自动回退时置位）
    single_pass_disabled: bool
    legal_research: list[dict[str, Any]]
    anchor_hits: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    selected_document_ids: list[str]
    completion_warnings: list[str]


# ---------------------------------------------------------------------------
# 通用助手
# ---------------------------------------------------------------------------

def _cancelled(state: ReviewState) -> bool:
    cancel = state.get("cancel_event")
    return cancel is not None and cancel.is_set()


def _decision(state: ReviewState, phase: str, action: str, reason: str, next_step: str) -> dict[str, Any]:
    """可审计 decision（§4.2）：我在做什么、为什么、下一步、失败怎么办。"""
    return {
        "phase": phase,
        "action": action,
        "reason": reason,
        "nextStep": next_step,
        "fallback": "模型不可用或被拒绝时降级本地确定性处理，任务不中断",
    }


def _record_decision(state: ReviewState, entry: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = list(state.get("decisions") or [])
    decisions.append(entry)
    return decisions


def _redact_out(state: ReviewState, text: str) -> tuple[str, str | None, dict[str, int]]:
    """外发文本脱敏（GOAL-004）：返回 (可外发文本, 阻断原因, 被阻断实体类型计数)。

    策略内实体（含低置信未确认项）一律替换为别名后外发，不再阻断
    （用户决策 2026-09-24）；blockedBy 仅触发 masked_unconfirmed 留痕事件。
    管道异常时放行原文并留 error 事件（极端罕见，不打断任务）。
    """
    from .. import redaction

    try:
        res = redaction.apply(state["case_id"], text or "")
    except Exception:
        task_store.append_event(state["task_id"], "redaction", {"status": "error"})
        return text, "redaction_error", {}
    if res["blockedBy"]:
        # 低置信高敏感实体（用户决策 2026-09-24：不再阻断外发）——apply 已把
        # 策略内实体全部替换为别名（含低置信），打码文本本身不含原文，外发
        # 不泄露；阻断只会卡死整卷通读并迫使模型退化成逐文档手动读取。
        # 仅记录事件，提醒用户在「脱敏设置」中确认实体校准误伤
        blocked_ids = set(res["blockedBy"])
        type_counts: dict[str, int] = {}
        for e in res.get("entities") or []:
            if e.get("id") in blocked_ids:
                t = str(e.get("type") or "unknown")
                type_counts[t] = type_counts.get(t, 0) + 1
        task_store.append_event(state["task_id"], "redaction", {
            "status": "masked_unconfirmed", "types": type_counts,
        })
    return res["redactedText"], None, {}


def _redaction_enabled(state: ReviewState) -> bool:
    """本次任务是否启用脱敏（模型全走云端，外发前是否替换敏感信息由用户在
    发起任务时选择；入参缺省按开启处理——安全兜底，评测路径不受影响）。"""
    policy = state.get("model_policy") or {}
    return bool(policy.get("redactionEnabled", True))


def _remote_json(
    state: ReviewState,
    system: str,
    user: str,
    schema_hint: str,
    stage: str,
    on_delta=None,
    raw_suffix: str = "",
    stream_timeout: float | None = None,
    reasoning_effort: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """远程结构化调用（Loop 各阶段共用）：脱敏 → 调用 → 用量记账。

    - 模型统一走云端服务，不做权限/审批拦截；外发是否脱敏由用户在发起
      任务时选择（redactionEnabled），选择权在用户；
    - 模型调用不做预算拦截（05 方案 §3.6：质量不受费用约束，token 仅计量统计），
      避免大卷宗通读中途被上限截断而换成非云端结果；
    - 调用失败（网关错误/响应中断）抛 ModelCallUnavailable，
      由 runner 置 FAILED——不降级本地，用户处理后可从检查点续跑；
    - 脱敏：启用时 system+user 整体过脱敏管道，策略内实体（含低置信未确认项）
      一律替换为别名后外发，不再挂起任务（用户决策 2026-09-24）；
    - raw_suffix：脱敏后原样拼接的补充上下文（仅限外部公开来源内容，如
      网络搜索结果——查询词已脱敏、内容不含案件数据，不参与案件实体扫描，
      避免外部 URL 数字串误触发直接标识符拦截）。

    返回 (解析结果, 需合并回 state 的更新)。
    """
    from . import budget as budget_store
    from . import model_gateway

    task_id = state["task_id"]
    updates: dict[str, Any] = {}
    model_id = str((state.get("model_policy") or {}).get("modelId") or model_gateway.DEFAULT_MODEL)

    # ---- 1) 脱敏（整体外发文本过管道） ----
    # 用户选择开启脱敏时，策略内实体一律替换为别名后外发（含低置信实体）；
    # 低置信实体不再阻断外发（用户决策 2026-09-24），仅记录 masked_unconfirmed 事件
    red_system, red_user = system, user
    if _redaction_enabled(state):
        red_system = _redact_out(state, system)[0]
        red_user = _redact_out(state, user)[0]
        if red_user != user or red_system != system:
            updates["redaction_applied"] = True
    # raw_suffix：外部公开来源内容，脱敏后原样拼接（不参与案件实体扫描）
    if raw_suffix:
        red_user = f"{red_user}\n{raw_suffix}" if red_user else raw_suffix

    # ---- 2) 模型调用（流式仅作答阶段；失败置 FAILED，不降级本地） ----
    # 显式档位（如 read 机械抽取用轻量推理提速）优先于用户全局设置；
    # 档位按模型能力校验，模型不支持时返回 None（不下发参数）
    effort = model_gateway.effective_reasoning_effort(
        reasoning_effort or (state.get("model_policy") or {}).get("reasoningEffort"),
        model_id,
    )
    task_store.append_event(task_id, "model_call", {
        "status": "calling", "stage": stage, "model": model_id,
        "provider": model_gateway.provider_label(model_gateway.provider_for_model(model_id)),
        "reasoningEffort": effort or "off",
    })
    # 先登记一次调用。这样超时、认证失败和网络失败也不会绕过 maxModelCalls。
    budget_store.spend(task_id, "model_call")

    def _check_cancel() -> bool:
        return _cancelled(state)

    received = {"len": 0}

    def _on_delta(text: str) -> None:
        if _check_cancel():
            raise task_store.TaskCancelled("用户停止")
        received["len"] = len(text)
        if on_delta is not None:
            on_delta(text)

    prompt_tokens = max(64, int(len(red_system + red_user) * 1.2) + 200)
    try:
        result, usage = model_gateway.generate_json(
            system=red_system, user=red_user, schema_hint=schema_hint,
            model=model_id, on_delta=_on_delta if on_delta else None,
            cancel_check=_check_cancel, reasoning_effort=effort,
            stream_timeout=stream_timeout,
        )
    except task_store.TaskCancelled:
        task_store.append_event(task_id, "model_call", {
            "status": "cancelled", "stage": stage, "model": model_id,
            "reason": f"用户停止，已接收 {received['len']} 字（内容保留，不完整）",
        })
        raise
    except model_gateway.ModelAuthError as exc:
        # 401/403：Key 无效不会自愈——终止任务（FAILED），用户更新 Key 后重新
        # 发起即可从检查点续跑（§11.1）
        task_store.append_event(task_id, "model_call", {
            "status": "failed", "stage": stage, "reason": str(exc), "model": model_id,
        })
        raise
    except model_gateway.ModelGatewayError as exc:
        # 网关失败（超时/5xx/结构异常）不自愈：抛错置 FAILED，不产出本地降级
        # 结果（避免用户拿到"假远程"结论）
        task_store.append_event(task_id, "model_call", {
            "status": "failed", "stage": stage, "reason": str(exc), "model": model_id,
        })
        raise task_store.ModelCallUnavailable(
            f"云端模型调用失败（{stage} 阶段）：{exc}",
            "确认本机网络与模型服务状态后重新发起任务（将从断点继续，已完成的阅读不会重复）",
        ) from exc

    task_store.append_event(task_id, "model_call", {
        "status": "ok", "stage": stage, "model": model_id,
        "provider": model_gateway.provider_label(model_gateway.provider_for_model(model_id)),
        "promptTokens": usage["promptTokens"], "completionTokens": usage["completionTokens"],
        "estimatedPromptTokens": prompt_tokens,
    })
    # §3.6：预算仅计量（不做拦截），保持任务统计与模型调用事件同源
    try:
        budget_store.spend(
            task_id, "model_call",
            tokens=int(usage["promptTokens"]) + int(usage["completionTokens"]), count=0,
        )
    except Exception:
        pass
    return result, updates


def _loop_checkpoint(state: ReviewState) -> dict[str, Any]:
    """检查点保留的 Loop 状态子集（§11：恢复时合并回初始状态）。"""
    return {k: state[k] for k in _LOOP_FIELDS if k in state}


def _reading_stats(state: ReviewState) -> dict[str, int]:
    queue = state.get("reading_queue") or []
    total = len(queue)
    read = sum(1 for u in queue if u.get("status") == "read")
    return {"total": total, "read": read, "remaining": total - read}


# ---------------------------------------------------------------------------
# 节点：任务理解（§7.1 任务解释卡）
# ---------------------------------------------------------------------------

_PRESET_DELIVERABLES: dict[str, list[str]] = {
    "review_analysis": ["阅卷分析报告"],
    "visualization": ["时间线、人物关系图与流程/证据关系图"],
    "evidence_analysis": ["证据卡与争点证据矩阵"],
    "strategy": ["辩护策略方案比较"],
}


def _local_task_card(state: ReviewState) -> dict[str, Any]:
    """本地任务理解：从请求与模式确定性推导（无模型时的降级）。"""
    mode = state["mode"]
    preset = state.get("workflow_preset")
    request = str(state.get("request") or "").strip()
    if mode == "contract_review":
        deliverables = ["合同风险审查结果"]
    elif preset in _PRESET_DELIVERABLES:
        deliverables = list(_PRESET_DELIVERABLES[preset])
    else:
        deliverables = ["分析要点"]
    return {
        "goal": (request[:40] or "完成案件分析"),
        "deliverables": deliverables,
        "clientPosition": str(state.get("client_role") or "未明确"),
        "mustInclude": ["不利事实", "相反证据", "待核实事项"],
        "completionCriteria": ["全案通读", "核心事实全部引用", "反方校验"],
        "openQuestions": [],
        "degraded": "local",
    }


def node_understand(state: ReviewState) -> ReviewState:
    if state.get("task_card"):
        return state  # 恢复：任务理解已完成
    task_id = state["task_id"]
    task_store.update_lifecycle(task_id, "PLANNING", "understand")

    task_card: dict[str, Any] | None = None
    degraded: str | None = None
    policy = state.get("model_policy") or {}
    if policy.get("remote"):
        system = (
            f"{ROLE_PROMPT}，正在理解任务：把用户的阅卷/审查请求拆解为任务解释卡。遵守：\n"
            "1. goal 用一句动宾短语概括用户要什么；\n"
            "2. clientPosition 只写能从输入确定的立场，不确定写「未明确」；\n"
            "3. 影响结论但无法确定的问题放 openQuestions，不要因此停止任务。"
        )
        user = (
            f"案件：{state.get('case_title') or ''}\n"
            f"模式：{state['mode']}"
            + (
                f"（预设：{state.get('workflow_preset')}）"
                if state.get("workflow_preset")
                else "（预设：未指定，请按用户请求意图从 workflowPreset 四选一）"
            )
            + f"\n委托人立场（案件设置）：{state.get('client_role') or '未设置'}\n"
            f"用户请求：{state['request']}"
        )
        task_card, updates = _remote_json(state, system, user, TASK_CARD_SCHEMA, "understand")
        if updates:
            state = {**state, **updates}
        # 预设自主选择（UI 仅在初始页提供显式入口）：无显式预设时按任务卡推断结果
        # 写回 state，交付物与最终输出 Schema（node_answer）随之分叉；显式预设优先
        if not state.get("workflow_preset"):
            inferred = str((task_card or {}).get("workflowPreset") or "")
            if inferred in _PRESET_DELIVERABLES:
                state = {**state, "workflow_preset": inferred}
    else:
        # 离线确定性路径（评测/无远程）：本地任务卡
        task_card = _local_task_card(state)
        degraded = "local_mode"

    task_store.append_event(task_id, "task_card", {"card": task_card, "degraded": degraded})
    task_store.save_checkpoint(task_id, "understand", _loop_checkpoint({**state, "task_card": task_card}))
    return {
        **state,
        "task_card": task_card,
        "decisions": _record_decision(state, _decision(
            state, "understand", "生成任务解释卡", "任务目标拆解是规划与完成判定的前提",
            "确认案件边界并建立阅读队列",
        )),
    }


# ---------------------------------------------------------------------------
# 节点：语料边界（§5.2 阅读队列 / §10 合同准备）
# ---------------------------------------------------------------------------

def node_corpus(state: ReviewState) -> ReviewState:
    if state.get("reading_queue"):
        return state  # 恢复：阅读队列已建立
    task_id = state["task_id"]
    task_store.update_lifecycle(task_id, "PLANNING", "corpus")

    from .. import documents as docs

    all_docs = docs.list_documents(state["case_id"])
    selected = set(state.get("selected_document_ids") or [])
    case_docs = [d for d in all_docs if not selected or d["id"] in selected]

    queue: list[dict[str, Any]] = []
    unread_reasons: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    for d in case_docs:
        if d["status"] not in ("ready", "partial"):
            unread_reasons.append({
                "documentId": d["id"], "documentTitle": d["title"],
                "reason": f"文档解析状态为 {d['status']}，暂不可读",
            })
            continue
        pages = docs.get_pages(d["id"])
        if not pages:
            unread_reasons.append({
                "documentId": d["id"], "documentTitle": d["title"], "reason": "无已解析页面",
            })
            continue
        manifest.append({
            "documentId": d["id"], "documentTitle": d["title"], "versionId": d["activeVersionId"],
            "pages": len(pages), "status": d["status"],
        })
        for p in pages:
            queue.append({
                "unitId": f"{d['id']}/page-{p['pageNo']}",
                "documentId": d["id"], "documentTitle": d["title"],
                "versionId": d["activeVersionId"], "pageNo": p["pageNo"],
                "quality": p.get("quality") or "", "status": "queued",
            })

    digest = hashlib.sha256(
        ";".join(f"{m['documentId']}@{m['versionId']}:{m['pages']}" for m in manifest).encode("utf-8")
    ).hexdigest()[:16]

    # 合同审查（§10）：共用 Loop，语料阶段完成审查会话与条款切分
    if state["mode"] == "contract_review":
        state = prepare_contract_review(state)

    task_store.append_event(task_id, "corpus", {
        "documents": len(manifest), "units": len(queue),
        "unread": len(unread_reasons), "corpusVersion": digest,
        "titles": [m["documentTitle"] for m in manifest[:12]],
        "unreadDetails": unread_reasons,
    })
    # 细粒度执行计划（用户可见）：逐文档列出阅读单元数，未读文档单列原因
    task_store.append_event(task_id, "plan", {
        "steps": [
            *(f"通读《{m['documentTitle']}》（{m.get('pages', '?')} 页）" for m in manifest),
            *(f"记录不可读：《{r['documentTitle']}》" for r in unread_reasons),
            "聚合案件工作记忆",
            "定向检索定位原文",
            "生成结论与引证校验",
        ],
    })
    new_state: ReviewState = {
        **state,
        "corpus_manifest": manifest,
        "corpus_version": digest,
        "reading_queue": queue,
        "unread_reasons": unread_reasons,
        "decisions": _record_decision(state, _decision(
            state, "corpus", f"锁定语料边界：{len(manifest)} 份文档 / {len(queue)} 个阅读单元",
            "全案通读需要明确的对象清单与版本锚点（§5.2）", "按序通读全部阅读单元",
        )),
    }
    task_store.save_checkpoint(task_id, "corpus", _loop_checkpoint(new_state))
    # 脱敏前置预检（REQ-PRIV-006 / §11.3）：远程模式下先对全部待读页面过一遍
    # 脱敏管道，一次性注册并收集所有未确认高敏感实体——用户只需确认一次，
    # 而不是每读一批被阻断一次；确认后从检查点续跑即可畅通完成全案通读
    _precheck_redaction(new_state)
    return new_state


def _precheck_redaction(state: ReviewState) -> None:
    """全语料脱敏预检：远程模式且用户启用脱敏时执行；只做统计留痕，不再阻断（用户决策 2026-09-24）。"""
    policy = state.get("model_policy") or {}
    if not policy.get("remote") or not _redaction_enabled(state):
        return
    from .. import documents as docs
    from .. import redaction

    blocked_types: dict[str, int] = {}
    scanned = 0

    def _scan(text: str) -> None:
        nonlocal scanned
        if not (text or "").strip():
            return
        scanned += 1
        res = redaction.apply(state["case_id"], text)
        if not res["blockedBy"]:
            return
        blocked_ids = set(res["blockedBy"])
        for e in res.get("entities") or []:
            if e.get("id") in blocked_ids:
                t = str(e.get("type") or "unknown")
                blocked_types[t] = blocked_types.get(t, 0) + 1

    queue = state.get("reading_queue") or []
    titles = {str(u.get("documentTitle") or "") for u in queue}
    for t in titles:
        _scan(t)  # 文档标题同样进入通读提示词，一并注册实体
    for doc_id in {u["documentId"] for u in queue}:
        for p in docs.get_pages(doc_id):
            _scan(p.get("text") or "")
    task_store.append_event(state["task_id"], "redaction", {
        "status": "precheck", "scannedPages": scanned,
        "blockedCount": sum(blocked_types.values()),
    })
    # 低置信高敏感实体不再挂起任务：apply 已把策略内实体（含低置信）全部替换为
    # 别名，打码文本不含原文，外发不泄露。预检仅统计留痕，提醒用户在「脱敏
    # 设置」中确认实体校准，避免阻断整卷通读。


# ---------------------------------------------------------------------------
# 节点：全案通读（§6 逐页观察；批间循环）
# ---------------------------------------------------------------------------

def _local_observation(unit: dict[str, Any], text: str) -> dict[str, Any]:
    """确定性观察降级：页首摘录 + 质量疑点（无模型/被阻断时）。"""
    from ..citations import clean_snippet

    summary = clean_snippet(text or "")[:80] or "（本页无可用文本）"
    issues: list[str] = []
    if not (text or "").strip():
        issues.append("本页无可用文本（OCR 未完成或空白页），需人工确认")
    elif str(unit.get("quality") or "") == "low":
        issues.append("页面文本质量较低（OCR），建议人工核对")
    return {
        "unitId": unit["unitId"], "summary": summary,
        "facts": [], "events": [], "entities": [], "issues": issues,
    }


def _normalize_observations(batch: list[dict[str, Any]], result: Any) -> dict[str, dict[str, Any]]:
    """模型观察结果规范化：按 unitId 对齐，缺失/畸形单元回退本地观察。"""
    out: dict[str, dict[str, Any]] = {}
    items = result.get("observations") if isinstance(result, dict) else None
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            unit_id = str(it.get("unitId") or "").strip()
            if unit_id:
                out[unit_id] = it
    for unit in batch:  # 缺失单元补本地观察，保证不漏页
        if unit["unitId"] not in out:
            out[unit["unitId"]] = _local_observation(unit, "")
    return out


def _normalize_memory(raw: Any) -> dict[str, Any]:
    """记忆聚合输出规范化（形态容错：模型输出不保证结构，dict/list/None 三态）。"""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for key in ("timeline", "entities", "keyFacts", "contradictions", "evidence"):
        v = raw.get(key)
        out[key] = [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    q = raw.get("openQuestions")
    out["openQuestions"] = [str(x) for x in q if str(x).strip()] if isinstance(q, list) else []
    return out


def node_read(state: ReviewState) -> ReviewState:
    if _cancelled(state):
        raise task_store.TaskCancelled("用户停止")
    task_id = state["task_id"]
    import time

    read_started = time.monotonic()
    task_store.update_lifecycle(task_id, "RUNNING", "read")

    from .. import documents as docs

    queue = state.get("reading_queue") or []
    queued = [u for u in queue if u.get("status") == "queued"]
    if not queued:
        return state

    # 墙钟/模型预算到达时不再继续逐批读取；将剩余单元标记为“有原因未读”，
    # 让图进入记忆与交付阶段，用户能拿到部分结果和明确覆盖率，而不是无限循环。
    from . import budget as budget_store
    try:
        budget_store.check(task_id, "model_call")
    except budget_store.BudgetExceeded as exc:
        remaining = [u for u in queue if u.get("status") == "queued"]
        for unit in remaining:
            unit["status"] = "skipped"
        unread = list(state.get("unread_reasons") or [])
        unread.extend({
            "documentId": u.get("documentId"),
            "documentTitle": u.get("documentTitle"),
            "pageNo": u.get("pageNo"),
            "reason": f"达到任务保护上限，未继续读取：{exc}",
        } for u in remaining)
        task_store.append_event(task_id, "budget", {
            "stage": "blocked", "scope": "reading", "reason": str(exc),
            "skipped": len(remaining),
        })
        new_state: ReviewState = {**state, "reading_queue": queue, "unread_reasons": unread}
        task_store.save_checkpoint(task_id, "read_budget_stop", _loop_checkpoint(new_state))
        return new_state

    # 批内文档页文本缓存（单次全量路径需全部排队单元的文本以估算总量）
    page_text: dict[tuple[str, int], str] = {}
    for doc_id in {u["documentId"] for u in queued}:
        for p in docs.get_pages(doc_id):
            page_text[(doc_id, int(p["pageNo"]))] = p.get("text") or ""

    # 用户决策：语料在模型上下文预算内时整卷一次性输入（通读+记忆合并一次调用，
    # 上限由 _single_pass_char_cap 按模型上下文动态推导）；超过或页数超过
    # SINGLE_PASS_MAX_UNITS（输出上限装不下逐页观察）的超大卷宗自动回退 48 页分批
    total_chars = sum(
        len(page_text.get((u["documentId"], int(u["pageNo"])), "")) for u in queued
    )
    policy = state.get("model_policy") or {}
    single_pass = (
        bool(policy.get("remote"))
        and not state.get("single_pass_disabled")
        and total_chars <= _single_pass_char_cap()
        and len(queued) <= SINGLE_PASS_MAX_UNITS
    )
    # 大卷宗拆成最多两批并行做机械抽取，降低单次首 token 等待；每批结果
    # 仍按 unitId 合并，随后统一写检查点。超过两批的部分由下一次 read 节点继续。
    if single_pass:
        batches = [queued]
    else:
        batches = [queued[i : i + READ_BATCH_UNITS] for i in range(0, min(len(queued), READ_BATCH_UNITS * 2), READ_BATCH_UNITS)]
    batch = [u for group in batches for u in group]

    observations = list(state.get("observations") or [])
    remote_obs: dict[str, dict[str, Any]] = {}
    if policy.get("remote"):
        def build_lines(group: list[dict[str, Any]]) -> list[str]:
            lines: list[str] = []
            for u in group:
                text = page_text.get((u["documentId"], int(u["pageNo"])), "")
                # 先对完整页文本脱敏再截断（与语料预检同源）：避免截断把数字串
                # 切成新片段被识别为未注册的新实体而再次触发阻断
                red_text = _redact_out(state, text)[0] if _redaction_enabled(state) else text
                lines.append(
                    f"【{u['unitId']} 《{u['documentTitle']}》第{u['pageNo']}页】\n"
                    f"{red_text[:PAGE_TEXT_MAX_CHARS] or '（本页无可用文本）'}"
                )
            return lines

        lines = build_lines(batch)
        if single_pass:
            try:
                # 整卷一次调用：逐页观察 + 案件记忆合并输出；超大输入的 TTFT
                # 以分钟计，单独放宽流式读超时（60s 默认值会把正常调用判超时）。
                # 逐页摘录属机械抽取，用轻量推理（服务端默认 max 档思考会
                # 白耗数分钟，2026-09-24 实证 71 页通读 295s 中大头是思考）
                result, updates = _remote_json(
                    state, _READ_ALL_SYSTEM, "\n\n".join(lines), _READ_ALL_SCHEMA, "read",
                    on_delta=lambda _text: None,
                    stream_timeout=model_gateway.READ_STREAM_TIMEOUT_S,
                    reasoning_effort="low",
                )
            except model_gateway.ModelGatewayError as exc:
                # 整卷失败（上下文超限/网关超时等）自动回退分批重试，不让
                # read_case_corpus 直接报错——否则自主模式会退化成逐文档手动读取
                task_store.append_event(task_id, "reading", {
                    "mode": "single_pass_fallback", "reason": str(exc)[:300],
                    "corpusChars": total_chars, "units": len(queued),
                })
                state = {**state, "single_pass_disabled": True}
                batch = queued[:READ_BATCH_UNITS]
                lines = []
                for u in batch:
                    text = page_text.get((u["documentId"], int(u["pageNo"])), "")
                    red_text = _redact_out(state, text)[0] if _redaction_enabled(state) else text
                    lines.append(
                        f"【{u['unitId']} 《{u['documentTitle']}》第{u['pageNo']}页】\n"
                        f"{red_text[:PAGE_TEXT_MAX_CHARS] or '（本页无可用文本）'}"
                    )
            else:
                if updates:
                    state = {**state, **updates}
                # 合并 schema 的逐页观察在 pages 键，复用同一规范化逻辑
                remote_obs = _normalize_observations(batch, {"observations": result.get("pages")})
                memory = _normalize_memory(result.get("memory") if isinstance(result, dict) else None)
                if any(memory.values()):
                    # 记忆已在通读中产出：synthesize 跳过模型聚合（省一次往返）
                    state = {**state, "case_memory": memory, "memory_direct": True}
        if not single_pass or state.get("single_pass_disabled"):
            # 分批阅读同样是机械抽取，轻量推理提速
            if len(batches) == 1:
                result, updates = _remote_json(
                    state, _READ_SYSTEM, "\n\n".join(lines), OBSERVATION_SCHEMA, "read",
                    reasoning_effort="low",
                )
                if updates:
                    state = {**state, **updates}
                remote_obs = _normalize_observations(batch, result)
            else:
                # 两批并行仅用于无状态的页面观察；每个 worker 使用同一只读 state，
                # 结果按 unitId 合并，任何一批失败仍由上层任务显式失败并可续跑。
                from concurrent.futures import ThreadPoolExecutor

                def read_batch(group: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
                    result, updates = _remote_json(
                        state, _READ_SYSTEM, "\n\n".join(build_lines(group)), OBSERVATION_SCHEMA, "read",
                        reasoning_effort="low",
                    )
                    normalized = _normalize_observations(group, result)
                    return list(normalized.values()), updates

                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-read") as pool:
                    results = list(pool.map(read_batch, batches))
                for group_result, updates in results:
                    if updates:
                        state = {**state, **updates}
                    for item in group_result:
                        remote_obs[str(item.get("unitId") or "")] = item

    for u in batch:
        text = page_text.get((u["documentId"], int(u["pageNo"])), "")
        raw = remote_obs.get(u["unitId"]) or _local_observation(u, text)
        obs = {
            "unitId": u["unitId"],
            "documentId": u["documentId"],
            "documentTitle": u["documentTitle"],
            "pageNo": u["pageNo"],
            "summary": str(raw.get("summary") or "")[:120] or "（本页无可用文本）",
            "facts": [f for f in (raw.get("facts") or []) if isinstance(f, dict)][:6],
            "events": [e for e in (raw.get("events") or []) if isinstance(e, dict)][:6],
            "entities": [str(e) for e in (raw.get("entities") or []) if e][:10],
            "issues": [str(i) for i in (raw.get("issues") or []) if i][:6],
        }
        observations.append(obs)
        u["status"] = "read"

    stats = _reading_stats(state)
    task_store.append_event(task_id, "reading", {
        "read": stats["read"], "total": stats["total"],
        "coveragePct": round(stats["read"] * 100 / stats["total"], 1) if stats["total"] else 100.0,
        "lastUnit": f"《{batch[-1]['documentTitle']}》第{batch[-1]['pageNo']}页",
        "observations": len(observations),
        "mode": "single_pass" if single_pass else "batch",
        "corpusChars": total_chars if single_pass else None,
        "durationMs": round((time.monotonic() - read_started) * 1000),
        "parallelBatches": len(batches) if not single_pass else 1,
    })
    new_state: ReviewState = {
        **state,
        "reading_queue": queue,
        "observations": observations,
    }
    task_store.save_checkpoint(task_id, "read", _loop_checkpoint(new_state))
    return new_state


# ---------------------------------------------------------------------------
# 节点：案件工作记忆（§5.3 观察聚合）+ 法源检索（§8.1）
# ---------------------------------------------------------------------------

def _local_memory(state: ReviewState) -> dict[str, Any]:
    """确定性记忆聚合：事件按日期排序、主体计数、事实与疑点收录。"""
    observations = (state.get("observations") or [])[:LOCAL_OBSERVATION_CAP]
    timeline: list[dict[str, Any]] = []
    entity_count: dict[str, int] = {}
    key_facts: list[dict[str, Any]] = []
    open_questions: list[str] = []
    for obs in observations:
        for e in obs.get("events") or []:
            date = str(e.get("date") or "").strip()
            event = str(e.get("event") or "").strip()
            if event:
                timeline.append({
                    "date": date if _DATE_RE.match(date) else "",
                    "event": event[:60],
                    "sourceDoc": obs.get("documentTitle"), "pageNo": obs.get("pageNo"),
                })
        for name in obs.get("entities") or []:
            entity_count[name] = entity_count.get(name, 0) + 1
        for f in obs.get("facts") or []:
            text = str(f.get("text") or "").strip()
            if text:
                key_facts.append({
                    "text": text[:60], "stance": "neutral",
                    "sourceDoc": obs.get("documentTitle"), "pageNo": obs.get("pageNo"),
                })
        for i in obs.get("issues") or []:
            if i not in open_questions:
                open_questions.append(i)
    timeline.sort(key=lambda t: t["date"])
    entities = [
        {"name": n, "role": "通读出现", "sourceDoc": ""}
        for n, _c in sorted(entity_count.items(), key=lambda kv: -kv[1])[:20]
    ]
    return {
        "timeline": timeline[:40],
        "entities": entities,
        "keyFacts": key_facts[:30],
        "contradictions": [],
        "evidence": [],
        "openQuestions": open_questions[:12],
        "degraded": "local",
    }


def _memory_lines(memory: dict[str, Any]) -> list[str]:
    """记忆压缩为提示行（作答上下文；各章节截断保护）。"""
    lines: list[str] = []
    for t in (memory.get("timeline") or [])[:_MEMORY_SECTION_CAPS["timeline"]]:
        date = t.get("date") or "日期未知"
        lines.append(f"- {date} {t.get('event')}（《{t.get('sourceDoc')}》第{t.get('pageNo')}页）")
    for e in (memory.get("entities") or [])[:_MEMORY_SECTION_CAPS["entities"]]:
        lines.append(f"- 主体：{e.get('name')}（{e.get('role')}）")
    for f in (memory.get("keyFacts") or [])[:_MEMORY_SECTION_CAPS["keyFacts"]]:
        lines.append(f"- 事实：{f.get('text')}（《{f.get('sourceDoc')}》第{f.get('pageNo')}页）")
    for c in memory.get("contradictions") or []:
        lines.append(f"- 矛盾：{c.get('topic')}——{c.get('sideA')} / {c.get('sideB')}")
    for e in (memory.get("evidence") or [])[:_MEMORY_SECTION_CAPS["evidence"]]:
        lines.append(f"- 证据：{e.get('name')}（{e.get('type')}，{e.get('purpose')}）")
    return lines


def _build_anchor_hits(state: ReviewState) -> list[dict[str, Any]]:
    """每份文档一个引用锚点（首页原文前缀），保证任意文档的 Claim 可回原文。"""
    from .. import documents as docs

    anchors: list[dict[str, Any]] = []
    for m in (state.get("corpus_manifest") or [])[:30]:
        try:
            pages = docs.get_pages(m["documentId"])
        except Exception:
            continue
        page = next((p for p in pages if (p.get("text") or "").strip()), None)
        if page is None:
            continue
        anchors.append({
            "documentId": m["documentId"],
            "documentTitle": m["documentTitle"],
            "versionId": m["versionId"],
            "pageNo": page["pageNo"],
            "snippet": (page.get("text") or "")[:60],
            "matchType": "anchor",
        })
    return anchors


def node_synthesize(state: ReviewState) -> ReviewState:
    if _cancelled(state):
        raise task_store.TaskCancelled("用户停止")
    task_id = state["task_id"]
    task_store.update_lifecycle(task_id, "RUNNING", "synthesize")

    observations = state.get("observations") or []
    memory: dict[str, Any] | None = None
    degraded: str | None = None
    policy = state.get("model_policy") or {}
    if state.get("memory_direct") and state.get("case_memory"):
        # 整卷单次通读已在 read 阶段直接产出案件记忆：跳过聚合调用（省一次往返）
        memory = state["case_memory"]
    elif policy.get("remote") and observations:
        shown = observations[:OBSERVATIONS_IN_PROMPT_CAP]
        lines = []
        for obs in shown:
            facts = "；".join(str(f.get("text") or "") for f in obs.get("facts") or [])
            events = "；".join(
                f"{e.get('date') or ''}{e.get('event') or ''}".strip() for e in obs.get("events") or []
            )
            entry = f"【{obs['unitId']} 《{obs['documentTitle']}》第{obs['pageNo']}页】{obs.get('summary') or ''}"
            if facts:
                entry += f"｜事实：{facts}"
            if events:
                entry += f"｜事件：{events}"
            lines.append(entry)
        if len(observations) > len(shown):
            lines.append(f"（共 {len(observations)} 个阅读单元，超长截断，仅聚合以上 {len(shown)} 个）")
        result, updates = _remote_json(
            state, _MEMORY_SYSTEM, "\n".join(lines), CASE_MEMORY_SCHEMA, "synthesize",
        )
        if updates:
            state = {**state, **updates}
        memory = result
    else:
        # 离线确定性路径（评测/无远程）：规则聚合
        memory = _local_memory(state)
        degraded = "local_mode"

    legal_research: list[dict[str, Any]] = []
    anchor_hits = _build_anchor_hits(state)

    task_store.append_event(task_id, "case_memory", {
        "timeline": len(memory.get("timeline") or []),
        "entities": len(memory.get("entities") or []),
        "keyFacts": len(memory.get("keyFacts") or []),
        "contradictions": len(memory.get("contradictions") or []),
        "evidence": len(memory.get("evidence") or []),
        "openQuestions": len(memory.get("openQuestions") or []),
        "legalResearch": len(legal_research),
        "degraded": degraded,
        "fromRead": bool(state.get("memory_direct")),
    })
    new_state: ReviewState = {
        **state,
        "case_memory": memory,
        "legal_research": legal_research,
        "anchor_hits": anchor_hits,
        "decisions": _record_decision(state, _decision(
            state, "synthesize", "聚合案件工作记忆",
            "作答必须基于全案记忆而非检索片段（§3.1）", "定向检索定位原文后生成结论",
        )),
    }
    task_store.save_checkpoint(task_id, "synthesize", _loop_checkpoint(new_state))
    return new_state


# ---------------------------------------------------------------------------
# 节点：作答（定向检索 §6.3 + 模式 Schema）
# ---------------------------------------------------------------------------

def _gated_search(state: ReviewState, keywords: list[str]) -> list[dict[str, Any]]:
    """通读后的定向检索（辅助定位）：保留工具调用预算门控（资源安全，§3.6）。"""
    from ..search import search_pages
    from . import budget as budget_store

    task_id = state["task_id"]
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for kw in keywords:
        try:
            gate = budget_store.check(task_id, "tool_call")
        except budget_store.BudgetExceeded as exc:
            task_store.append_event(task_id, "budget", {
                "stage": "blocked", "reason": str(exc), "scope": "tool_call",
                "used": budget_store.snapshot(task_id)["used"],
            })
            break
        if gate["stage"] == "warn":
            task_store.append_event(task_id, "budget", {
                "stage": "warn", "reason": "；".join(gate["reasons"]), "scope": "tool_call",
            })
        try:
            budget_store.register_call(task_id, "document.search", kw)
        except budget_store.RepeatedCallBlocked as exc:
            task_store.append_event(task_id, "budget", {
                "stage": "blocked", "reason": str(exc), "scope": "repeated_call", "query": kw,
            })
            continue
        task_store.append_event(task_id, "tool_call", {"tool": "document.search", "query": kw})
        try:
            found = search_pages(state["case_id"], kw, limit=6)
        except Exception:
            found = []
        budget_store.spend(task_id, "tool_call")
        for h in found:
            key = (h["documentId"], h["pageNo"])
            if key not in seen:
                seen.add(key)
                hits.append(h)
        task_store.append_event(task_id, "tool_result", {
            "tool": "document.search", "query": kw, "count": len(found),
        })
    return hits


def _research_suffix(state: ReviewState) -> str:
    """外部网络检索结果（§8.1）：作为 raw_suffix 在脱敏后拼接——内容来自
    公开网络（查询词已脱敏），不参与案件实体扫描，不作案件事实来源。"""
    research = state.get("legal_research") or []
    if not research:
        return ""
    lines = ["", "外部网络检索结果（法源线索；只能用于 legalBasis 参考，不得作为案件事实来源）："]
    for r in research:
        for item in r.get("results") or []:
            lines.append(f"- {item.get('title')}：{str(item.get('content'))[:80]}（{item.get('link')}）")
    return "\n".join(lines)


def _answer_user_prompt(state: ReviewState, hits: list[dict[str, Any]]) -> str:
    """作答上下文：任务卡 + 案件记忆 + 定向检索命中（+合同条款）。

    外部网络检索结果经 _research_suffix 单独拼接（脱敏后原文），不在此处。
    """
    card = state.get("task_card") or {}
    memory = state.get("case_memory") or {}
    lines = [
        f"案件：{state.get('case_title') or ''}",
        f"任务目标：{card.get('goal') or state['request']}",
        f"委托人立场：{card.get('clientPosition') or state.get('client_role') or '未明确'}",
        "",
        "全案通读已完成的案件记忆（引用时 sourceDoc 使用其中出现的文档标题）：",
    ]
    lines += _memory_lines(memory) or ["（通读未发现可聚合的信息）"]
    lines.append("")
    lines.append("定向检索命中（用于定位原文页码）：")
    for h in hits[:12]:
        snippet = (h.get("snippet") or "").replace("【", "[").replace("】", "]")[:200]
        lines.append(f"- 《{h['documentTitle']}》第{h['pageNo']}页：{snippet}")
    if state["mode"] == "contract_review":
        from .graph import MAX_CONTRACT_CLAUSES_IN_PROMPT

        clauses = state.get("contract_clauses") or []
        lines.append("")
        lines.append("切分后的合同条款（clauseRef 为条款编号，originalText 必须逐字摘录其中原文）：")
        shown = clauses[:MAX_CONTRACT_CLAUSES_IN_PROMPT]
        for c in shown:
            text = (c.get("text") or "").replace("【", "[").replace("】", "]")
            lines.append(f"- [{c.get('clauseRef')}] {text}")
        if len(clauses) > len(shown):
            lines.append(f"（合同共 {len(clauses)} 条，超长截断，仅分析以上 {len(shown)} 条）")
    return "\n".join(lines)


def node_answer(state: ReviewState) -> ReviewState:
    if _cancelled(state):
        raise task_store.TaskCancelled("用户停止")
    task_id = state["task_id"]
    task_store.update_lifecycle(task_id, "RUNNING", "answer")

    # 定向检索（§6.3：通读后定位，关键词来自用户请求与任务卡）
    card = state.get("task_card") or {}
    keywords = _extract_keywords(
        f"{state['request']} {card.get('goal') or ''}", state["mode"]
    )[:MAX_SEARCH_CALLS]
    hits = _gated_search(state, keywords)
    all_hits = (state.get("anchor_hits") or []) + hits
    state = {**state, "hits": all_hits, "keywords": keywords}

    analysis: dict[str, Any] | None = None
    degraded: str | None = None
    policy = state.get("model_policy") or {}
    if policy.get("remote"):
        # 输出结构复用固定流程的模式/预设 Schema（产物形态与旧路径一致）
        from . import model_gateway
        from .graph import (
            CONTRACT_SCHEMA_HINT, DOCGEN_SCHEMA_HINT, EVIDENCE_SCHEMA_HINT,
            REVIEW_REPORT_SCHEMA_HINT, SCHEMA_HINT, STRATEGY_SCHEMA_HINT, VIZ_SCHEMA_HINT,
        )

        preset = state.get("workflow_preset")
        if state["mode"] == "legal_review" and preset == "visualization":
            schema_hint = VIZ_SCHEMA_HINT
        elif state["mode"] == "legal_review" and preset == "evidence_analysis":
            schema_hint = EVIDENCE_SCHEMA_HINT
        elif state["mode"] == "legal_review" and preset == "strategy":
            schema_hint = STRATEGY_SCHEMA_HINT
        elif state["mode"] == "legal_review":
            schema_hint = REVIEW_REPORT_SCHEMA_HINT
        elif state["mode"] == "contract_review":
            schema_hint = CONTRACT_SCHEMA_HINT
        elif state["mode"] == "document_generation":
            schema_hint = DOCGEN_SCHEMA_HINT
        else:
            schema_hint = SCHEMA_HINT

        system = _system_prompt(state) + _ANSWER_SYSTEM_SUFFIX
        user = _answer_user_prompt(state, all_hits)

        def _on_delta(text: str) -> None:
            payload: dict[str, Any] = {"text": text}
            preview = _extract_preview(text)
            if preview:
                payload["preview"] = preview
            task_store.append_event(task_id, "message_delta", payload)

        analysis, updates = _remote_json(
            state, system, user, schema_hint, "answer",
            on_delta=_on_delta, raw_suffix=_research_suffix(state),
        )
        if updates:
            state = {**state, **updates}
    else:
        # 离线确定性路径（评测/无远程）：本地检索分析
        analysis = _local_analysis(state)
        degraded = "local_mode"
        # 降级原因前置到 audit（用户必须第一眼知道这不是云端模型结论）
        analysis = {
            **analysis,
            "audit": f"{_degraded_label(degraded)}，以下为本地检索分析。{analysis.get('audit') or ''}",
        }

    new_state: ReviewState = {**state, "analysis": analysis, "degraded_reason": degraded}
    task_store.save_checkpoint(task_id, "answer", {"degraded": degraded, "analysisKeys": list(analysis)[:8]})
    return new_state


# ---------------------------------------------------------------------------
# 节点：校验与交付（§9 质量门禁 + §4.3 完成条件）
# ---------------------------------------------------------------------------

def node_finalize(state: ReviewState) -> ReviewState:
    task_id = state["task_id"]
    task_store.update_lifecycle(task_id, "VERIFYING", "verify")

    analysis = state.get("analysis") or {}
    # 脱敏外发的模型输出含案件内别名：恢复原文后再绑定引证（与固定流程一致）
    if state.get("redaction_applied"):
        try:
            analysis = _restore_analysis(state["case_id"], analysis)
        except Exception:
            pass

    # 完整性校验（§9.2-4）：通读覆盖率与未读原因可计算
    stats = _reading_stats(state)
    unread = state.get("unread_reasons") or []
    coverage_pct = round(stats["read"] * 100 / stats["total"], 1) if stats["total"] else 100.0
    verification = {
        "readingCoverage": f"{stats['read']}/{stats['total']}",
        "coveragePct": coverage_pct,
        "unreadWithReason": len(unread),
        "documents": len(state.get("corpus_manifest") or []),
        "observations": len(state.get("observations") or []),
        "legalResearch": sum(len(r.get("results") or []) for r in state.get("legal_research") or []),
        "openQuestions": len((state.get("case_memory") or {}).get("openQuestions") or []),
    }

    body, cites, needs_human, checkpoint = build_final_body(
        state, analysis, state.get("hits") or [], extra={"verification": verification},
    )
    task_store.append_event(task_id, "message", {"body": body})

    warnings: list[str] = []
    if state.get("degraded_reason"):
        warnings.append(f"降级：{_degraded_label(state.get('degraded_reason'))}")
    if needs_human:
        warnings.append(f"{needs_human} 项待人工核实")
    if stats["remaining"] > 0:
        warnings.append(f"{stats['remaining']} 个阅读单元未读")
    if unread:
        warnings.append(f"{len(unread)} 份文档不可读（已记录原因）")
    task_store.append_event(task_id, "verification", {**verification, "warnings": warnings})

    if state["mode"] != "document_generation":
        persist_final_outputs(state, body, cites)

    checkpoint = {**checkpoint, "verification": verification, "warnings": warnings}
    task_store.save_checkpoint(task_id, "verify", checkpoint)
    return {**state, "analysis": analysis, "cites": cites, "completion_warnings": warnings}


# ---------------------------------------------------------------------------
# 图构建（§4.1 Loop：理解 → 语料 → 通读（循环）→ 记忆 → 作答 → 校验）
# ---------------------------------------------------------------------------

def _route_entry(state: ReviewState) -> str:
    """恢复入口：已有阅读队列则直接续读（§11.1 从最近检查点继续）。"""
    return "read" if state.get("reading_queue") else "understand"


def _route_after_read(state: ReviewState) -> str:
    queue = state.get("reading_queue") or []
    return "read" if any(u.get("status") == "queued" for u in queue) else "synthesize"


def build_review_graph():
    """阅卷 Loop 主图（法律阅卷与合同审查共用；文书生成仍走固定流程）。"""
    g: StateGraph = StateGraph(ReviewState)
    g.add_node("understand", node_understand)
    g.add_node("corpus", node_corpus)
    g.add_node("read", node_read)
    g.add_node("synthesize", node_synthesize)
    g.add_node("answer", node_answer)
    g.add_node("finalize", node_finalize)
    g.set_conditional_entry_point(_route_entry, {"understand": "understand", "read": "read"})
    g.add_edge("understand", "corpus")
    g.add_edge("corpus", "read")
    g.add_conditional_edges("read", _route_after_read, {"read": "read", "synthesize": "synthesize"})
    g.add_edge("synthesize", "answer")
    g.add_edge("answer", "finalize")
    g.add_edge("finalize", END)
    return g.compile()
