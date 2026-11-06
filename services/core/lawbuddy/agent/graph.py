"""LangGraph 主图与模式子图（DEV-AGT-002，技术方案 §4.3 / §5）。

法律阅卷（PRD 13.3 全规格，M2 收尾）：
- 四个工作流预设 review_analysis / visualization / evidence_analysis / strategy
  共用检索骨架，统一输出阅卷报告结构（概览/主体/时间线/争点/事实/矛盾/
  缺失/法源/下一步），预设差异体现在系统提示侧重；
- 专属输出 Schema（TimelineSpec、证据卡、策略方案比较）在后续迭代分叉；
- 报告逐项绑定 Claim Ledger：parties/timeline/disputes/facts/legalBasis 中
  带 sourceDoc 的条目逐一建 Claim 并校验引语（REQ-REV-008）；
- 合同审查（PRD 14 / DEV-MOD-005）：node_plan 调 contract.split_clauses 完成
  条款切分与交叉引用解析，node_analyze 按风险项 Schema 输出，node_verify
  逐项绑定 Claim（originalText 引语回查主合同）并落库 contract_risk_items；
- 文书生成（PRD 15 / DEV-MOD-007/008）：node_plan 建文书草稿并解析模板，
  node_analyze 输出提纲与槽位建议，node_verify 对建议逐条引语校验落库
  （案件原文需引证 REQ-GEN-004）；草稿保持提纲待确认态，正文渲染只能由
  用户确认提纲后经 docgen.render 触发（REQ-GEN-005 门禁）。

循环保护（技术方案 §4.4 / DEV-AGT-006）：
- 关键词检索次数 ≤ 4（工具调用上限）；
- 结构化输出修复 ≤ 2 次（在 model_gateway 内）；
- 模型不可用一律降级本地分析，不重试外呼。

外发脱敏（M3 / REQ-PRIV-006 / GOAL-004：先脱敏再授权）：
- node_analyze 外发前对请求、案件标题、snippet 与文档标题执行
  redaction.apply（案件内稳定别名）；未确认直接标识符 → 阻断外发降级本地；
- 审批卡展示脱敏档位与替换计数；引证绑定与本地分析用原始 hits；
- node_verify 对模型输出递归 restore 恢复原文后绑定 Claim。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from . import tasks as task_store
from ..search import search_pages

# 统一角色系统提示词（用户决策）：产品人格就是「LawBuddy」本身，
# 所有阶段/模式的系统提示词共用这一身份声明，不再自称「XX 助手」
ROLE_PROMPT = "你是 LawBuddy"

# 工具调用上限（循环保护）
MAX_SEARCH_CALLS = 4
# 传给模型的检索片段上限（最小上下文，技术方案 §8.2）
MAX_CONTEXT_HITS = 12
# 报告级 Claim 绑定上限（成本保护）
MAX_REPORT_CLAIMS = 40

WORKFLOW_PRESETS = ("review_analysis", "visualization", "evidence_analysis", "strategy")

# 精简要点结构（contract 分叉前的过渡）
SCHEMA_HINT = """{
  "intro": "一句话开场（说明分析依据）",
  "points": [
    {"title": "要点标题（10字内）", "desc": "分析说明（60字内）", "sourceDoc": "依据的文档标题（必须来自提供的材料）"}
  ],
  "audit": "审计提示语（说明为AI分析意见）"
}"""

# 合同审查风险项 Schema（PRD 14.4 输出：等级/条款位置/原文/说明/影响/修改建议/
# 替换文本/依据/置信状态）。值域与 contract._risk_row 落库校验一致：
# riskLevel ∈ severe/high/medium/low，category ∈ business/legal/completeness，
# confidence ∈ verified/high/medium/low；replacementText 为可直接替换原文的
# 完整句（REQ-CON-006 接受后只写工作副本），缺省空字符串（REQ-CON-008 不编造）。
CONTRACT_SCHEMA_HINT = """{
  "risks": [
    {"riskLevel": "severe或high或medium或low",
     "category": "business或legal或completeness（业务风险/法律风险/条款缺失）",
     "clauseRef": "条款编号（必须使用切分结果中的 clauseRef，如 第3条）",
     "originalText": "条款原文关键句（逐字摘录切分结果，不改写、不拼接）",
     "riskNote": "风险说明（60字内）",
     "impact": "对审查方的影响（60字内）",
     "suggestion": "修改建议（60字内）",
     "replacementText": "建议替换文本（可直接替换原文的完整句子，无则空字符串）",
     "basis": "依据（合同其他条款/法条/交易惯例，30字内）",
     "confidence": "verified或high或medium或low（原文逐字核对无误才可 verified）"}
  ],
  "audit": "审计提示语（说明为AI审查意见，最终以合同原文与律师判断为准）"
}"""

# 合同审查条款注入上限（成本保护：超长合同截断到前 N 条供模型分析，
# verify 按模型返回的 clauseRef 逐项定位，不受截断影响）
MAX_CONTRACT_CLAUSES_IN_PROMPT = 40
# 风险项 Claim 绑定上限（与报告级一致的成本保护）
MAX_CONTRACT_CLAIMS = 40

# 文书生成 Schema（PRD 15.3 / 技术方案 §5.3）：提纲 + 槽位建议。
# value 必须逐字摘录材料原文（quote 供引语校验）；模型无法从材料确认的
# 字段不输出槽位建议（系统以占位符替换，REQ-GEN-003 不编造）。
DOCGEN_SCHEMA_HINT = """{
  "outline": [
    {"heading": "章节标题（与模板固定章节结构对齐）", "points": "该节应写明的内容要点（60字内）", "sourceDoc": "依据的文档标题"}
  ],
  "slots": [
    {"name": "模板字段名（必须来自字段清单）", "value": "建议填入内容（逐字摘录材料原文）", "quote": "材料中被摘录的原文片段（用于引语校验）", "sourceDoc": "依据的文档标题"}
  ],
  "audit": "审计提示语（说明缺失字段将以占位符替换，不编造）"
}"""

# 槽位建议 Claim 绑定上限（成本保护）
MAX_DOCGEN_SLOTS = 20

# 文书修订 Schema（交付物迭代改写）：以当前稿为基准输出修订后的完整正文。
# 只按修订意见修改；未提及内容保持原文；材料无法确认的事实不编造（保留占位符）。
DOCGEN_REVISION_SCHEMA_HINT = """{
  "revisedBody": "修订后的文书全文（Markdown；未提及修改的内容与原文逐字一致）",
  "revisionNotes": "本次修订说明（改了哪些位置、为何这样改，80字内）",
  "audit": "审计提示语（说明修订未新增无出处事实，关键事实以原文为准）"
}"""

# 阅卷报告全规格（PRD 13.3 输出：11 章节）
REVIEW_REPORT_SCHEMA_HINT = """{
  "overview": "案件概览（2-3句：案由、当事人概况、当前程序阶段）",
  "parties": [
    {"name": "姓名或机构", "role": "原告/被告/证人/代理人等", "note": "与本案的关系（30字内）", "sourceDoc": "依据的文档标题"}
  ],
  "timeline": [
    {"date": "2023-03-15（只能确定月份时写 2023-03）", "event": "程序或事实事件（40字内）", "sourceDoc": "依据的文档标题"}
  ],
  "disputes": [
    {"title": "争议焦点（15字内）", "desc": "双方主张与举证责任（60字内）", "sourceDoc": "依据的文档标题"}
  ],
  "facts": [
    {"text": "事实陈述（40字内）", "stance": "favorable或unfavorable或neutral（对委托人立场）", "status": "confirmed或pending（单一来源或含糊时用pending）", "sourceDoc": "依据的文档标题"}
  ],
  "contradictions": [
    {"topic": "矛盾主题（15字内）", "sideA": "一方说法及来源文档", "sideB": "另一方说法及来源文档", "note": "可能解释或核实建议（30字内）"}
  ],
  "missing": ["缺失材料与补充建议（每条一句）"],
  "legalBasis": [
    {"title": "法条或类案名称", "point": "适用要点（40字内）", "sourceDoc": "材料中提及该依据的文档标题，材料未提及则留空字符串"}
  ],
  "nextSteps": ["下一步工作事项（每条一句）"],
  "audit": "审计提示语（说明为AI分析意见、关键事实以原文为准）"
}"""

# 可视化阅卷受限 Schema（PRD 13.4 / ADR-005：TimelineSpec + RelationGraphSpec + FlowSpec，
# 由本地组件渲染（关系图/流程图经 mermaid 布局引擎），禁止执行模型生成的 HTML 或 JavaScript；
# flows 的节点文本只作为 label 渲染，DSL 由客户端从结构化数据确定性生成）
VIZ_SCHEMA_HINT = """{
  "timeline": {
    "nodes": [
      {"date": "2023-03-15（只到月份写 2023-03，区间写 2023-03~2023-04，未知写空字符串）",
       "event": "事件（40字内）", "actors": ["参与者（可空数组）"], "sourceDoc": "依据的文档标题"}
    ]
  },
  "relationGraph": {
    "nodes": [
      {"name": "姓名或机构名", "kind": "person或org", "role": "原告/被告/证人/机构等",
       "note": "与本案关系（20字内，可空）", "sourceDoc": "依据的文档标题"}
    ],
    "edges": [
      {"from": "起始节点名（必须是 nodes 中的 name）", "to": "目标节点名（必须是 nodes 中的 name）",
       "label": "关系（12字内）", "sourceDoc": "依据的文档标题"}
    ]
  },
  "flows": [
    {"title": "图标题（如：资金流转流程、证据关系图；仅在确有流程或多步关系时输出）",
     "direction": "TB（自上而下）或LR（自左向右）",
     "nodes": [
       {"id": "稳定短id（英文数字，如 a1）", "label": "节点文本（20字内）",
        "shape": "rect或stadium或round或diamond（可空，默认rect；diamond 用于判断/分歧）",
        "sourceDoc": "依据的文档标题"}
     ],
     "edges": [
       {"from": "起始节点 id（必须是 nodes 中的 id）", "to": "目标节点 id（必须是 nodes 中的 id）",
        "label": "流转条件或关系（12字内，可空）", "dashed": "true表示待核实的关联（可空）",
        "sourceDoc": "依据的文档标题"}
     ]}
  ],
  "audit": "审计提示语（说明节点均来自材料，无来源的关系不生成）"
}"""

# 可视化节点规模上限（PRD 13.4：节点过多时先聚合，超出部分丢弃并留痕）
MAX_TIMELINE_NODES = 30
MAX_GRAPH_NODES = 16
MAX_GRAPH_EDGES = 40
MAX_FLOWS = 3
MAX_FLOW_NODES = 20
MAX_FLOW_EDGES = 40

# 证据分析受限 Schema（PRD 13.5：证据卡 + 争点证据矩阵）
# - 证据事实字段（名称/类型/形成时间/提供方/证明目的/关键片段）与
#   分析意见（真实性/合法性/关联性/证明力）分开展示（REQ-EVI-001）；
# - 取得程序材料缺失时合法性输出"无法判断"（REQ-EVI-004）。
EVIDENCE_SCHEMA_HINT = """{
  "evidence": [
    {"name": "证据名称（材料无法判断时用文件名，不臆造）", "type": "书证/物证/电子数据/视听资料/证人证言等",
     "sourceDoc": "来源文档标题（必须来自提供的材料）",
     "formedAt": "形成时间（材料中有则写，没有写空字符串）",
     "provider": "提供方（可空字符串）", "custody": "保管情况（可空字符串）",
     "purpose": "证明目的（40字内）", "keyFragment": "原文关键片段（60字内，逐字摘录）",
     "authenticity": "真实性问题（分析意见，30字内）",
     "legality": "合法性问题（取得程序材料缺失时必须写：无法判断）",
     "relevance": "关联性问题（分析意见，30字内）",
     "weight": "证明力分析（分析意见，50字内）",
     "relations": [{"to": "另一证据名称", "relation": "印证或矛盾"}],
     "defects": "矛盾与缺陷（可空字符串）",
     "reinforce": "补强建议（可空字符串）", "crossExam": "质证问题（可空字符串）"}
  ],
  "matrix": {
    "disputes": ["争议焦点（15字内）"],
    "cells": [
      {"dispute": "争议焦点（必须是 disputes 中的文本）", "evidence": "证据名称（必须是 evidence 中的 name）",
       "relation": "supports或opposes或conflict或missing", "note": "备注（可空字符串）"}
    ]
  },
  "audit": "审计提示语（说明分析意见与材料记载已分开标注）"
}"""

# 证据分析规模上限（超出丢弃并留痕）
MAX_EVIDENCE_CARDS = 12
MAX_MATRIX_DISPUTES = 6

# 辩护策略受限 Schema（PRD 13.6）：
# - REQ-STR-003：默认提供两个以上可行方案，仅一个时必须说明原因；
# - REQ-STR-004：不利条件与对方可能主张必须完整呈现；
# - REQ-STR-005：AI 不自动选定最终方案（用户确认经 strategy.decide 入审计）。
STRATEGY_SCHEMA_HINT = """{
  "position": "委托目标与当前限制（复述委托人身份、主要目标、不可接受结果，60字内）",
  "keyDisputes": ["关键争点（15字内）"],
  "favorable": [
    {"text": "有利条件（40字内）", "sourceDoc": "依据文档标题（材料中出现的标题，无明确来源写空字符串）"}
  ],
  "unfavorable": [
    {"text": "不利条件（40字内，必须完整列出不得隐藏）", "sourceDoc": "依据文档标题（可空字符串）"}
  ],
  "infoGaps": ["信息缺口（每条一句）"],
  "plans": [
    {"title": "方案名（12字内）",
     "premises": ["事实或法律前提（每条一句，须有材料依据；依赖未核实事实时在 risks 中说明）"],
     "legalBasis": ["法律依据（材料中提及的写明，无则写：待核实）"],
     "actions": ["行动步骤（每条一句）"],
     "benefits": "预期收益（40字内）",
     "risks": "风险（含未核实依赖，60字内）",
     "counter": "对方可能主张与应对（60字内，必须呈现）",
     "sourceDoc": "方案主要依据的文档标题（可空字符串）"}
  ],
  "singlePlanReason": "仅一个可行方案时的原因（多方案时写空字符串）",
  "evidenceActions": ["证据补充与程序动作（每条一句）"],
  "userDecisions": ["需律师决定的事项（每条一句）"],
  "audit": "审计提示语（说明 AI 不替用户选定最终方案）"
}"""

# 策略方案数上限（超出丢弃留痕；PRD 13.6 方案比较）
MAX_STRATEGY_PLANS = 4

_PRESET_LABELS: dict[str, str] = {
    "review_analysis": "阅卷分析",
    "visualization": "可视化阅卷",
    "evidence_analysis": "证据分析",
    "strategy": "辩护策略分析",
}

_PRESET_FOCUS: dict[str, str] = {
    "review_analysis": "本次为标准阅卷分析：均衡产出全部章节。",
    "visualization": "本次侧重可视化阅卷：重点完善时间线与当事人章节，日期与事件须可绘图，不得虚构时间。",
    "evidence_analysis": "本次侧重证据分析：重点完善事实、矛盾与缺失章节，逐项标注证据状态与补强建议。",
    "strategy": "本次侧重辩护策略分析：重点完善争议焦点、有利与不利事实和下一步工作；不利事实必须完整呈现（REQ-STR-004），不得因立场隐藏。",
}

# 流式预览（P-010）：从累积 JSON 文本中抽取已闭合的字符串字段值
_PREVIEW_FIELD_RE = re.compile(r'"([^"\n]{1,32})"\s*:\s*"((?:[^"\\]|\\.)*)"')
_PREVIEW_MAX_CHARS = 600


def _extract_preview(text: str) -> str | None:
    """抽取累积结构化文本中已完成的字符串字段值，拼成可读成稿片段。

    仅预览用途：最终渲染以 message 事件（解析校验后的报告）为准；
    超长保留尾部（贴近正在生成的段落），解析异常返回 None 由前端降级。
    """
    try:
        values: list[str] = []
        for _key, raw in _PREVIEW_FIELD_RE.findall(text):
            try:
                value = str(json.loads(f'"{raw}"')).strip()
            except json.JSONDecodeError:
                value = raw.strip()
            if value:
                values.append(value)
        if not values:
            return None
        joined = "\n".join(values)
        if len(joined) > _PREVIEW_MAX_CHARS:
            joined = "…" + joined[-_PREVIEW_MAX_CHARS:]
        return joined
    except Exception:
        return None


class AgentState(TypedDict, total=False):
    """统一任务状态（技术方案 §4.1 最小集）。"""

    task_id: str
    case_id: str
    mode: str
    request: str
    case_title: str
    client_role: str | None
    permission_preset: str
    model_policy: dict[str, Any]
    workflow_preset: str | None
    # ---- 合同审查上下文（PRD 14；runner 从 agent.start 入参注入）----
    contract_input: dict[str, Any]  # 14.2 输入表：主合同/审查方/风险偏好/交易目的等
    contract_review_id: str | None  # node_plan 创建的审查会话 id
    contract_source: dict[str, Any]  # 主合同锚点（documentId/versionId/title，verify 建 SourceRef 用）
    contract_clauses: list[dict[str, Any]]  # 切分后的条款（含 refs/pageNo，分析提示与 verify 定位）
    contract_pages: list[dict[str, Any]]  # 主合同页文本（verify 兜底定位原文页码）
    contract_full_text: str  # 主合同全文（verify 兜底定位）
    contract_type: str | None  # 合同类型（用户指定或 node_plan 识别回填）
    redacted_contract_clauses: list[dict[str, Any]]  # 外发前脱敏后的条款（文本已替换为案件内别名）
    # ---- 文书生成上下文（PRD 15；runner 从 agent.start 入参注入）----
    docgen_input: dict[str, Any]  # 15.2 输入表：模板/文书类型/用途（请求事项）
    docgen_draft_id: str | None  # node_plan 创建的文书草稿 id
    docgen_template: dict[str, Any]  # 模板（title/docType/fields/contentMd，提示与 verify 用）
    docgen_revision_instruction: str  # 修订分叉：用户本次的修订意见（revisionDraftId 路径）
    docgen_revision_base: str  # 修订分叉：修订基准（草稿当前稿全文）
    docgen_revision_base_version: int  # 修订分叉：修订基准版本号（防旧稿覆盖新稿）
    docgen_redacted_base: str  # 外发前脱敏后的修订基准
    docgen_redacted_instruction: str  # 外发前脱敏后的修订意见
    keywords: list[str]
    hits: list[dict[str, Any]]
    redacted_hits: list[dict[str, Any]]  # 外发前脱敏后的命中（snippet/标题已替换为案件内别名）
    redacted_title: str
    redacted_request: str
    redaction_meta: dict[str, Any]  # 脱敏元数据（档位/计数，不含原文，§18）
    redaction_applied: bool  # 本次外发的是脱敏文本（verify 按别名恢复）
    analysis: dict[str, Any]
    cites: list[dict[str, Any]]
    degraded_reason: str | None
    cancel_event: Any  # threading.Event（runner 注入，流式循环检查点，F-012；不进 checkpoint）


# ---------------------------------------------------------------------------
# 节点：计划
# ---------------------------------------------------------------------------

def _plan_contract_review(state: AgentState) -> AgentState:
    return prepare_contract_review(state)


def prepare_contract_review(state: AgentState) -> AgentState:
    """合同审查准备（PRD 14 / 技术方案 §5.2 前置步骤；阅卷 Loop 同样调用）。

    - 建审查单（contract.create_review 校验主合同归属与解析状态，出错时
      落 tool_result 事件后上抛，任务置 FAILED）；
    - 条款切分（contract.split_clauses 不产出 refs/pageNo，此处逐条款以
      extract_cross_refs + page_of_offset 补齐后 save_clauses 落库）；
    - 用户未显式指定合同类型时 detect_contract_type 识别回填；
    - 主合同锚点与切分结果存入 state，供分析注入提示与引证定位。
    """
    from .. import contract as contract_store
    from .. import documents as docs

    task_id = state["task_id"]
    inp = state.get("contract_input") or {}
    steps = [
        "确认审查方与风险偏好",
        "切分条款并解析交叉引用",
        "识别合同类型",
        "检索案件材料",
        "识别风险并分级",
        "绑定条款引证",
    ]
    task_store.update_lifecycle(task_id, "PLANNING", "plan")
    task_store.append_event(task_id, "plan", {"steps": steps, "workflowPreset": None})

    try:
        task_store.append_event(task_id, "tool_call", {
            "tool": "contract.create_review",
            "primaryDocumentId": inp.get("primaryDocumentId"),
            "reviewerRole": inp.get("reviewerRole"),
            "riskAppetite": inp.get("riskAppetite"),
        })
        review = contract_store.create_review(
            case_id=state["case_id"],
            primary_document_id=str(inp.get("primaryDocumentId") or ""),
            reviewer_role=str(inp.get("reviewerRole") or "custom"),
            custom_role=inp.get("customRole"),
            risk_appetite=str(inp.get("riskAppetite") or "balanced"),
            contract_type=inp.get("contractType"),
            transaction_goal=inp.get("transactionGoal"),
            governing_law=inp.get("governingLaw"),
            task_id=task_id,
        )
    except contract_store.ContractError as exc:
        task_store.append_event(task_id, "tool_result", {
            "tool": "contract.create_review", "error": str(exc),
        })
        raise

    doc_id = review["primaryDocumentId"]
    pages = docs.get_pages(doc_id)
    full_text = contract_store.join_pages(pages)

    task_store.append_event(task_id, "tool_call", {
        "tool": "contract.split_clauses", "documentTitle": review["primaryDocumentTitle"],
    })
    clauses = contract_store.split_clauses(full_text)
    for c in clauses:
        c["refs"] = contract_store.extract_cross_refs(c["text"], c["clauseRef"])
        c["pageNo"] = contract_store.page_of_offset(pages, full_text, int(c.get("charStart") or 0))
    contract_store.save_clauses(review["id"], clauses)

    contract_type = review["contractType"] or contract_store.detect_contract_type(full_text)
    if contract_type and contract_type != review["contractType"]:
        contract_store.set_contract_type(review["id"], contract_type)

    task_store.append_event(task_id, "tool_result", {
        "tool": "contract.split_clauses", "clauses": len(clauses),
        "chars": len(full_text), "contractType": contract_type,
    })
    task_store.save_checkpoint(task_id, "plan", {
        "mode": "contract_review",
        "reviewId": review["id"],
        "clauses": len(clauses),
        "contractType": contract_type,
    })
    return {
        **state,
        "contract_review_id": review["id"],
        "contract_source": {
            "documentId": doc_id,
            "versionId": review["primaryVersionId"],
            "documentTitle": review["primaryDocumentTitle"],
        },
        "contract_clauses": clauses,
        "contract_pages": pages,
        "contract_full_text": full_text,
        "contract_type": contract_type,
    }


def _plan_docgen(state: AgentState) -> AgentState:
    """文书生成准备（PRD 15 / 技术方案 §5.3 前置步骤）。

    - 请求事项（purpose）缺失在 create_draft 内拒绝（PRD 15.6：阻止生成正文）；
    - 建文书草稿（校验模板可用性与文书类型匹配，不匹配需用户确认后重发）；
    - 模板结构（字段/固定章节）存入 state，供 node_analyze 注入提示与
      node_verify 槽位校验；草稿保持提纲待确认态（REQ-GEN-005）；
    - 修订分叉（docgenInput.revisionDraftId + revisionInstruction）：不建新草稿，
      以既有草稿当前稿为基准按用户意见修订，node_verify 落 kind='ai_revise' 版本。
    """
    from .. import docgen as docgen_store

    task_id = state["task_id"]
    inp = state.get("docgen_input") or {}

    # ---- 修订分叉：既有草稿的 AI 定向改写（交付物迭代） ----
    if inp.get("revisionDraftId"):
        return _plan_docgen_revision(state, inp)

    steps = [
        "理解起草需求与文书类型",
        "检索关联案件材料",
        "确认案件信息与来源",
        "确定文书结构",
        "生成文书与 Word 稿件",
    ]
    task_store.update_lifecycle(task_id, "PLANNING", "plan")
    task_store.append_event(task_id, "plan", {"steps": steps, "workflowPreset": None})

    try:
        task_store.append_event(task_id, "tool_call", {
            "tool": "docgen.create_draft",
            "templateId": inp.get("templateId"),
            "docType": inp.get("docType"),
        })
        draft = docgen_store.create_draft(
            case_id=state["case_id"],
            template_id=str(inp.get("templateId") or ""),
            doc_type=str(inp.get("docType") or ""),
            purpose=str(inp.get("purpose") or ""),
            task_id=task_id,
            mismatch_confirmed=bool(inp.get("confirmMismatch")),
        )
    except docgen_store.DocGenError as exc:
        task_store.append_event(task_id, "tool_result", {
            "tool": "docgen.create_draft", "error": str(exc),
        })
        raise

    tpl = docgen_store.get_template(draft["templateId"])
    task_store.append_event(task_id, "tool_result", {
        "tool": "docgen.create_draft", "draftId": draft["id"],
        "templateTitle": tpl["title"], "fields": len(tpl["fields"]),
    })
    task_store.save_checkpoint(task_id, "plan", {
        "mode": "document_generation", "draftId": draft["id"], "fields": len(tpl["fields"]),
    })
    return {
        **state,
        "docgen_draft_id": draft["id"],
        "docgen_template": tpl,
    }


def _plan_docgen_revision(state: AgentState, inp: dict[str, Any]) -> AgentState:
    """文书修订准备（交付物迭代改写）：以既有草稿当前稿为基准，按用户修订
    意见产出修订稿，落 kind='ai_revise' 版本（历史版本不删）。

    守门：草稿不存在 / 提纲未确认 / 尚无正文 / 修订意见为空均拒绝；
    修订不绕过占位符门禁（REQ-GEN-007：修订后重新审计，定稿需重新确认）。
    """
    from .. import docgen as docgen_store

    task_id = state["task_id"]
    instruction = str(inp.get("revisionInstruction") or "").strip()
    steps = [
        "读取草稿当前稿",
        "理解修订意见",
        "检索案件材料",
        "生成修订稿",
        "落库为新版本",
    ]
    task_store.update_lifecycle(task_id, "PLANNING", "plan")
    task_store.append_event(task_id, "plan", {"steps": steps, "workflowPreset": None})

    try:
        draft_id = str(inp.get("revisionDraftId") or "")
        draft = docgen_store.get_draft(draft_id)
        if not instruction:
            raise docgen_store.DocGenError("修订意见不能为空：请说明需要修改的内容")
        if not draft["outlineConfirmed"]:
            raise docgen_store.DocGenError("提纲尚未确认，确认提纲并生成正文后才能修订")
        if not (draft.get("bodyMd") or "").strip():
            raise docgen_store.DocGenError("草稿尚无正文，请先生成正文再修订")
        if draft["caseId"] != state["case_id"]:
            raise docgen_store.DocGenError("草稿不属于当前案件")
    except docgen_store.DocGenError as exc:
        task_store.append_event(task_id, "tool_result", {
            "tool": "docgen.load_draft_for_revision", "error": str(exc),
        })
        raise

    tpl = (
        docgen_store.get_template(draft["templateId"])
        if draft.get("templateId") else None
    ) or {}
    task_store.append_event(task_id, "tool_call", {
        "tool": "docgen.load_draft_for_revision",
        "draftId": draft["id"], "charCount": len(draft["bodyMd"] or ""),
    })
    task_store.append_event(task_id, "tool_result", {
        "tool": "docgen.load_draft_for_revision", "draftId": draft["id"],
        "docType": draft["docType"],
    })
    task_store.save_checkpoint(task_id, "plan", {
        "mode": "document_generation", "revision": True, "draftId": draft["id"],
    })
    return {
        **state,
        "docgen_draft_id": draft["id"],
        "docgen_template": tpl,
        "docgen_revision_instruction": instruction,
        "docgen_revision_base": draft["bodyMd"],
        # 版本锚点：修订落库时校验，避免用户在编辑期间产生的新稿被旧基准覆盖
        "docgen_revision_base_version": int(draft.get("currentVersionNo") or 0),
    }


def node_plan(state: AgentState) -> AgentState:
    if state["mode"] == "contract_review":
        return _plan_contract_review(state)
    if state["mode"] == "document_generation":
        return _plan_docgen(state)
    preset = state.get("workflow_preset")
    preset_label = _PRESET_LABELS.get(preset or "", "综合分析")
    steps = [
        f"生成{preset_label}计划",
        "检索案件材料",
        "组织分析结论",
        "绑定原文引证",
    ]
    task_store.update_lifecycle(state["task_id"], "PLANNING", "plan")
    task_store.append_event(state["task_id"], "plan", {"steps": steps, "workflowPreset": preset})
    task_store.save_checkpoint(state["task_id"], "plan", {"mode": state["mode"], "workflowPreset": preset})
    # DEV-AGT-006：轮次计数（A-002 最大循环；本图为单轮编排，恢复/重试时连续累加）
    try:
        from . import budget as budget_store

        budget_store.spend(state["task_id"], "round")
    except Exception:
        pass
    return state


# ---------------------------------------------------------------------------
# 节点：检索（工具 document.search，真实 FTS，案件强制过滤）
# ---------------------------------------------------------------------------

_MODE_KEYWORDS: dict[str, list[str]] = {
    "legal_review": ["争议焦点", "合同", "证据"],
    "contract_review": ["违约责任", "合同条款", "争议解决"],
    "document_generation": ["诉讼请求", "事实与理由"],
}


def _extract_keywords(request: str, mode: str) -> list[str]:
    """从用户输入提取检索词：长词优先 + 模式默认词，截断到调用上限。"""
    kws: list[str] = []
    for phrase in re.split(r"[，。？！；、\s]+", request.strip()):
        phrase = phrase.strip()
        if len(phrase) >= 3 and phrase not in kws:
            kws.append(phrase)
    for extra in _MODE_KEYWORDS.get(mode, []):
        if extra not in kws:
            kws.append(extra)
    return kws[:MAX_SEARCH_CALLS]


def node_retrieve(state: AgentState) -> AgentState:
    task_id = state["task_id"]
    case_id = state["case_id"]
    task_store.update_lifecycle(task_id, "RUNNING", "retrieve")

    # 文书生成任务可显式关联案件：材料检索范围包含关联案件的只读共享材料
    from ..cases import readable_case_ids

    extra_case_ids = tuple(readable_case_ids(case_id)[1:])
    keywords = _extract_keywords(state["request"], state["mode"])
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    from . import budget as budget_store

    for kw in keywords:
        # DEV-AGT-006：预算检查（已达上限即停止工具调用）+ 重复调用拦截（A-004）
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
            found = search_pages(case_id, kw, limit=6, extra_case_ids=extra_case_ids)
        except Exception:
            found = []
        budget_store.spend(task_id, "tool_call")
        for h in found:
            key = (h["documentId"], h["pageNo"])
            if key not in seen:
                seen.add(key)
                hits.append(h)
        task_store.append_event(
            task_id, "tool_result", {"tool": "document.search", "query": kw, "count": len(found)}
        )
    task_store.save_checkpoint(task_id, "retrieve", {"keywords": keywords, "hits": len(hits)})
    return {**state, "keywords": keywords, "hits": hits}


# ---------------------------------------------------------------------------
# 节点：分析（权限预检 → 模型 / 本地降级）
# ---------------------------------------------------------------------------

def node_analyze(state: AgentState) -> AgentState:
    task_id = state["task_id"]
    task_store.update_lifecycle(task_id, "RUNNING", "analyze")

    policy: dict[str, Any] = state.get("model_policy") or {}
    use_remote = bool(policy.get("remote"))
    # 未指定模型时交给网关按注册表默认模型解析（内置目录已清空，由用户自行添加）
    from . import model_gateway

    model_id = str(policy.get("modelId") or model_gateway.DEFAULT_MODEL)
    preset = state.get("workflow_preset")
    # 输出结构按模式与预设分叉：可视化/证据分析/策略 → 受限图表与证据卡 Spec；
    # 其余阅卷 → 全规格报告；合同审查 → 风险项结构（条款已在 node_plan 切分）；
    # 其他模式 → 精简要点
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
        schema_hint = (
            DOCGEN_REVISION_SCHEMA_HINT
            if state.get("docgen_revision_instruction")
            else DOCGEN_SCHEMA_HINT
        )
    else:
        schema_hint = SCHEMA_HINT

    degraded_reason: str | None = None

    # 案件记忆（技术方案 §8.1/§8.2）：召回本案件有效记忆，随本轮请求装配到上下文。
    # 记忆条目均带《文档名》第N页来源（写入时强制），直接作为事实依据复用，
    # 减少重复检索；外发路径在 _redact_for_remote 中同步脱敏。
    try:
        from .. import memory as memory_store
        from ..cases import readable_case_ids

        state = {
            **state,
            "memory_lines": memory_store.context_lines(
                state["case_id"],
                state["request"],
                limit=5,
                extra_case_ids=tuple(readable_case_ids(state["case_id"])[1:]),
            ),
        }
    except Exception:
        state = {**state, "memory_lines": []}

    if not use_remote:
        # 离线确定性路径（评测/无远程）：本地检索分析
        analysis = _local_analysis(state)
        degraded_reason = "local_mode"
    else:
        from . import budget as budget_store

        if bool(policy.get("redactionEnabled", True)):
            # GOAL-004：先脱敏再外发（REQ-PRIV-006）——任何外发文本先过脱敏管道
            state, redaction_error, blocked_types = _redact_for_remote(state)
            if blocked_types:
                # 用户决策 2026-09-24：未确认高敏感实体不再阻断外发——apply 已把
                # 策略内实体（含低置信）全部替换为别名，打码文本不含原文；仅留痕
                # 提醒用户在「脱敏设置」中确认实体校准
                task_store.append_event(task_id, "redaction", {
                    "status": "masked_unconfirmed", "stage": "analyze",
                    "blockedTypes": blocked_types,
                })
            if redaction_error:
                # 管道异常 fail-closed：无脱敏文本可用，不允许原文外发（挂起任务）
                task_store.append_event(task_id, "model_call", {
                    "status": "blocked",
                    "reason": "脱敏管道异常：无法安全外发，请在案件设置中检查脱敏配置后重试",
                    "model": model_id,
                })
                # §11.3：不静默降级本地——挂起任务等用户处理后从断点续跑
                from .loop import HumanInterventionRequired

                raise HumanInterventionRequired("analyze", blocked_types)
        # 云端调用失败抛 ModelCallUnavailable（任务置 FAILED），不降级本地
        analysis = _remote_analyze(state, task_id, model_id, schema_hint)
        budget_store.spend(task_id, "model_call")

    task_store.save_checkpoint(task_id, "analyze", {"degraded": degraded_reason})
    return {**state, "analysis": analysis, "degraded_reason": degraded_reason}


def _redact_for_remote(state: AgentState) -> tuple[AgentState, str | None, dict[str, int]]:
    """外发前脱敏（GOAL-004 / REQ-PRIV-006）：用户请求、案件标题、命中片段的
    snippet 与文档标题按案件档位替换为案件内稳定别名。

    - 策略内实体（含低置信未确认项）一律替换为别名后外发，不再阻断
      （用户决策 2026-09-24）；blockedBy 仅作统计留痕，提醒用户确认实体校准；
    - 引证绑定（node_verify）与本地分析仍使用原始 hits，不受别名影响；
    - 管道异常一律 fail-closed（返回 redaction_error，不外发原文）。
    """
    from .. import redaction

    case_id = state["case_id"]
    hits = state["hits"][:MAX_CONTEXT_HITS]
    blocked_types: dict[str, int] = {}

    def _apply(text: str) -> dict[str, Any]:
        return redaction.apply(case_id, text or "")

    def _count_blocked(res: dict[str, Any]) -> int:
        """统计本段文本被阻断的实体数，并按类型累计（供人工确认提示）。"""
        ids = set(res.get("blockedBy") or [])
        for e in res.get("entities") or []:
            if e.get("id") in ids:
                t = str(e.get("type") or "unknown")
                blocked_types[t] = blocked_types.get(t, 0) + 1
        return len(ids)

    try:
        title_res = _apply(state.get("case_title"))
        request_res = _apply(state["request"])
        policy = title_res.get("policy") or "standard"
        replaced = title_res["replacedCount"] + request_res["replacedCount"]
        blocked = _count_blocked(title_res) + _count_blocked(request_res)
        redacted_hits: list[dict[str, Any]] = []
        for h in hits:
            snippet_res = _apply(h.get("snippet"))
            doc_title_res = _apply(h.get("documentTitle"))
            replaced += snippet_res["replacedCount"] + doc_title_res["replacedCount"]
            blocked += _count_blocked(snippet_res) + _count_blocked(doc_title_res)
            redacted_hits.append({
                **h,
                "snippet": snippet_res["redactedText"],
                "documentTitle": doc_title_res["redactedText"],
            })
        redacted_clauses: list[dict[str, Any]] = []
        if state["mode"] == "contract_review":
            # 合同审查的外发正文是切分后的条款：heading/text 同样过脱敏管道
            # （GOAL-004 先脱敏再授权；条款含未确认直接标识符时整单阻断）
            for c in state.get("contract_clauses") or []:
                text_res = _apply(c.get("text"))
                head_res = _apply(c.get("heading"))
                replaced += text_res["replacedCount"] + head_res["replacedCount"]
                blocked += _count_blocked(text_res) + _count_blocked(head_res)
                redacted_clauses.append({
                    **c,
                    "text": text_res["redactedText"],
                    "heading": head_res["redactedText"],
                })
        # 案件记忆行同样外发：逐行过脱敏管道（含未确认直接标识符时整单阻断）
        redacted_memory_lines: list[str] = []
        for line in state.get("memory_lines") or []:
            line_res = _apply(line)
            replaced += line_res["replacedCount"]
            blocked += _count_blocked(line_res)
            redacted_memory_lines.append(line_res["redactedText"])
    except Exception:
        task_store.append_event(state["task_id"], "redaction", {"status": "error"})
        return state, "redaction_error", blocked_types

    meta = {"status": "ok", "policy": policy, "replaced": replaced, "blocked": blocked, "hitCount": len(hits)}
    updates: AgentState = {
        "redacted_hits": redacted_hits,
        "redacted_title": title_res["redactedText"],
        "redacted_request": request_res["redactedText"],
        "redaction_meta": meta,
        # 外发的始终是脱敏文本（含低置信未确认实体也已替换为别名），
        # verify/finalize 需按别名恢复原文
        "redaction_applied": True,
        "redacted_memory_lines": redacted_memory_lines,
    }
    if redacted_clauses:
        updates["redacted_contract_clauses"] = redacted_clauses
    if state["mode"] == "document_generation" and state.get("docgen_revision_base"):
        # 修订分叉外发正文 = 草稿当前稿全文 + 修订意见：同样过脱敏管道
        # （GOAL-004：先脱敏再外发；模型输出中的别名由 verify 按别名恢复）
        base_res = _apply(state.get("docgen_revision_base"))
        instr_res = _apply(state.get("docgen_revision_instruction"))
        replaced += base_res["replacedCount"] + instr_res["replacedCount"]
        blocked += _count_blocked(base_res) + _count_blocked(instr_res)
        updates["docgen_redacted_base"] = base_res["redactedText"]
        updates["docgen_redacted_instruction"] = instr_res["redactedText"]
        meta = {**meta, "replaced": replaced, "blocked": blocked}
    task_store.append_event(state["task_id"], "redaction", meta)
    return {**state, **updates}, None, blocked_types


def _remote_analyze(
    state: AgentState, task_id: str, model_id: str, schema_hint: str
) -> dict[str, Any]:
    """远程分析：脱敏 → 模型调用 → 用量记账。
    失败一律抛 ModelCallUnavailable（任务置 FAILED），不返回本地降级结果。"""
    from . import model_gateway

    task_store.append_event(task_id, "model_call", {"status": "preflight", "model": model_id})

    # ---- 1) 准备外发文本（无额度预检：应用免费，用户自带模型 Key） ----
    system = _system_prompt(state)
    user = _user_prompt(state)
    prompt_tokens = max(64, int(len(system + user) * 1.2) + 200)

    # ---- 2) 模型流式调用 + 按实际 usage 记账（P-010 / FR-002 / F-012） ----
    # 模型思考强度：UI 档位优先，未指定时用该模型默认档；模型不支持档位时
    # 返回 None（不下发参数）
    effort = model_gateway.effective_reasoning_effort(
        (state.get("model_policy") or {}).get("reasoningEffort"), model_id
    )
    task_store.append_event(task_id, "model_call", {
        "status": "calling", "model": model_id, "stream": True, "reasoningEffort": effort or "off",
    })
    received = {"len": 0}  # 已生成字符数（取消时记录保留内容长度）

    def _cancelled() -> bool:
        cancel = state.get("cancel_event")
        return cancel is not None and cancel.is_set()

    def on_delta(text: str) -> None:
        if _cancelled():
            raise task_store.TaskCancelled("用户停止")
        received["len"] = len(text)
        payload: dict[str, Any] = {"text": text}
        preview = _extract_preview(text)
        if preview:
            payload["preview"] = preview  # 可读成稿片段（前端优先渲染，最终以 message 为准）
        task_store.append_event(task_id, "message_delta", payload)

    try:
        result, usage = model_gateway.generate_json(
            system=system, user=user, schema_hint=schema_hint, model=model_id, on_delta=on_delta,
            cancel_check=_cancelled,  # 阻塞读期间的用户停止也能在 20s 超时窗口内生效
            reasoning_effort=effort,  # 模型思考强度（None 表示按网关默认）
        )
    except task_store.TaskCancelled:
        # F-012：停止传播——已接收内容保留，交 runner 置 CANCELLED
        task_store.append_event(task_id, "model_call", {
            "status": "cancelled", "model": model_id,
            "reason": f"用户停止，已接收 {received['len']} 字（内容保留，不完整）",
        })
        raise
    except model_gateway.StreamInterrupted as exc:
        # FR-002：中断网——已接收内容标为不完整（保留在时间线），不产出本地分析
        if exc.received:
            payload = {"text": exc.received, "incomplete": True}
            preview = _extract_preview(exc.received)
            if preview:
                payload["preview"] = preview
            task_store.append_event(task_id, "message_delta", payload)
        task_store.append_event(task_id, "model_call", {
            "status": "failed", "model": model_id,
            "reason": f"流式响应中断，已接收 {len(exc.received)} 字不完整",
        })
        raise task_store.ModelCallUnavailable(
            "云端模型响应中断，本次分析未完成",
            "确认本机网络与模型服务状态后重新发起任务（将从断点继续，已完成的阅读不会重复）",
        ) from exc
    except model_gateway.ModelGatewayError as exc:
        task_store.append_event(task_id, "model_call", {"status": "failed", "reason": str(exc), "model": model_id})
        raise task_store.ModelCallUnavailable(
            f"云端模型调用失败：{exc}",
            "确认本机网络与模型服务状态后重新发起任务（将从断点继续，已完成的阅读不会重复）",
        ) from exc

    task_store.append_event(task_id, "model_call", {
        "status": "ok", "model": model_id, "sections": _report_section_counts(result),
        "promptTokens": usage["promptTokens"], "completionTokens": usage["completionTokens"],
        "estimatedPromptTokens": prompt_tokens,
    })
    # DEV-AGT-006：token 计入任务预算（统计同源，误差为 0）
    try:
        from . import budget as budget_store

        budget_store.spend(
            task_id, "model_call",
            tokens=int(usage["promptTokens"]) + int(usage["completionTokens"]), count=0,
        )
    except Exception:
        pass
    return result


def _count_entries(v: Any) -> int:
    """章节条目计数：兼容图表 {nodes:[...]} 与报告直接列表两种形态（模型输出不保证形态）。"""
    if isinstance(v, list):
        return len(v)
    if isinstance(v, dict):
        return len(v.get("nodes") or [])
    return 0


def _report_section_counts(result: dict[str, Any]) -> dict[str, int]:
    """模型输出各章节/图表条目数（事件留痕，便于评测）。

    timeline/relationGraph/matrix 兼容对象与列表两种形态：
    可视化 spec 为 {nodes:[...]}，全规格报告的 timeline 为条目列表。
    """
    if "risks" in result:
        return {"risks": len(result.get("risks") or [])}
    if "timeline" in result or "relationGraph" in result:
        g = _as_dict(result.get("relationGraph"))
        flows = result.get("flows") or []
        return {
            "timeline": _count_entries(result.get("timeline")),
            "graphNodes": _count_entries(result.get("relationGraph")),
            "graphEdges": len(g.get("edges") or []),
            "flows": len(flows),
            "flowNodes": sum(len(_as_dict(f).get("nodes") or []) for f in flows),
        }
    if "evidence" in result or "matrix" in result:
        m = _as_dict(result.get("matrix"))
        return {
            "evidenceCards": len(result.get("evidence") or []),
            "matrixDisputes": len(m.get("disputes") or []),
            "matrixCells": len(m.get("cells") or []),
        }
    if "plans" in result or "favorable" in result:
        return {
            "plans": len(result.get("plans") or []),
            "favorable": len(result.get("favorable") or []),
            "unfavorable": len(result.get("unfavorable") or []),
        }
    out: dict[str, int] = {}
    for key in ("parties", "timeline", "disputes", "facts", "contradictions", "legalBasis"):
        v = result.get(key)
        out[key] = len(v) if isinstance(v, list) else 0
    return out


def _contract_role_label(state: AgentState) -> str:
    """审查方可读名（PRD 14.2：预设角色或自定义角色）。"""
    from ..contract import REVIEWER_ROLE_LABELS

    inp = state.get("contract_input") or {}
    raw = str(inp.get("reviewerRole") or "custom")
    if raw == "custom":
        return str(inp.get("customRole") or "交易一方").strip() or "交易一方"
    return REVIEWER_ROLE_LABELS.get(raw, raw)


_RISK_APPETITE_LABELS = {
    "conservative": "保守（宁可多报，不放过任何隐患）",
    "balanced": "均衡（按常见交易标准评估）",
    "aggressive": "激进（只报重大风险，容忍常见商业安排）",
}


# ---------------------------------------------------------------------------
# 以下三组模式提示词（_contract_system_prompt / _system_prompt / _docgen_system_prompt）
# 属于旧固定管线（graph/loop）遗产：当前执行路径已统一走 autonomous.py 自主循环，
# 领域纪律已迁移至 autonomous._MODE_HINTS。此处仅被休眠的 graph/loop 代码引用，
# 维护提示词时以 autonomous.py 为准，不要只改此处造成两处漂移。
# ---------------------------------------------------------------------------
def _contract_system_prompt(state: AgentState) -> str:
    """合同审查系统提示（PRD 14.3 / 技术方案 §5.2 审查工作流）。"""
    role = _contract_role_label(state)
    inp = state.get("contract_input") or {}
    appetite = _RISK_APPETITE_LABELS.get(str(inp.get("riskAppetite") or "balanced"), _RISK_APPETITE_LABELS["balanced"])
    ctype = state.get("contract_type") or "未识别（请从条款内容判断）"
    return (
        f"{ROLE_PROMPT}，正在执行合同审查：基于切分后的合同条款输出结构化风险审查结果。遵守：\n"
        f"1. 审查立场：{role}；风险说明（impact）与修改建议（suggestion）必须围绕该方利益组织，"
        "对该方有利的内容不列为风险；\n"
        f"2. 风险偏好：{appetite}；\n"
        f"3. 合同类型：{ctype}；按该类型合同的常见风险点检查；\n"
        "4. 只依据提供的条款分析，不编造合同中不存在的内容（REQ-CON-008）；"
        "关键条款确实缺失（如无违约责任、无争议解决、无终止条款）时，"
        "以 category=completeness 输出缺失风险，originalText 写空字符串；\n"
        "5. clauseRef 必须使用切分结果中出现的条款编号；originalText 逐字摘录对应条款原文，"
        "不改写、不拼接（用于引证回查，改写会被系统判为待人工核实）；\n"
        "6. 风险分级：severe=可能导致重大损失或合同目的落空；high=明显不利，须修改后再签；"
        "medium=存在隐患，建议完善；low=提示性事项；\n"
        "7. replacementText 必须是可直接替换 originalText 的完整表述（与条款上下文衔接）；"
        "无合理替换文本时写空字符串，不要编造；\n"
        "8. confidence=verified 仅当 originalText 与条款原文逐字一致；基于推断的分析用 high/medium/low；\n"
        "9. 条款间交叉引用（如「详见第X条」）需核对被引条款是否存在且内容一致，"
        "指向不明或冲突时列为风险并在 basis 说明。"
    )


def _system_prompt(state: AgentState) -> str:
    role = state.get("client_role") or "未设置"
    if state["mode"] == "contract_review":
        return _contract_system_prompt(state)
    if state["mode"] == "document_generation":
        return _docgen_system_prompt(state)
    if state["mode"] != "legal_review":
        return (
            f"{ROLE_PROMPT}，基于用户提供的案件材料片段进行分析，遵守：\n"
            "1. 结论必须以材料片段为依据，引用 sourceDoc 时必须使用材料中出现的文档标题；\n"
            f"2. 委托人立场：{role}；不利与有利材料都要呈现；\n"
            "3. 不确定的内容明确说明，不编造事实。"
        )
    preset = state.get("workflow_preset") or "review_analysis"
    if preset == "visualization":
        return (
            f"{ROLE_PROMPT}，基于提供的案件材料片段生成可视化数据（时间线与人物关系）。遵守：\n"
            "1. 只从材料中明确记载的事实生成节点和关系，不编造；材料未提及的关系不要生成；\n"
            "2. 每个节点和关系都给出来源文档（sourceDoc 用材料中出现的标题），无来源依据的不要生成；\n"
            f"3. 委托人立场：{role}；\n"
            "4. 日期不确定时：月份写 YYYY-MM，区间写 YYYY-MM~YYYY-MM，完全未知写空字符串；\n"
            "5. 相同人物或机构合并为一个节点，role 写主要身份，note 补充其他身份；\n"
            "6. edges 的 from 和 to 必须是 nodes 中出现的 name；\n"
            "7. 时间线按时间先后排列，程序事件与事实事件都收录。"
        )
    if preset == "evidence_analysis":
        return (
            f"{ROLE_PROMPT}，基于提供的案件材料片段输出证据分析（证据卡与争点证据矩阵）。遵守：\n"
            "1. 证据名称、类型、形成时间、提供方、保管、证明目的和关键片段是材料记载字段，"
            "只写材料中出现的内容；名称无法判断时用文件名，不臆造；\n"
            "2. 真实性、合法性、关联性、证明力是分析意见，与材料记载分开表述，"
            "每项注明推断依据；\n"
            "3. 材料中没有证据取得程序记载时，合法性必须写「无法判断」，不下确定结论（REQ-EVI-004）；\n"
            "4. keyFragment 逐字摘录材料原文，不改写；\n"
            "5. matrix 的 relation 只用 supports/opposes/conflict/missing；"
            "cells 的 dispute 必须来自 disputes，evidence 必须来自证据卡 name；\n"
            f"6. 委托人立场：{role}；矩阵要同时呈现支持与反对本方主张的证据。"
        )
    if preset == "strategy":
        return (
            f"{ROLE_PROMPT}，基于提供的案件材料片段输出辩护或代理策略分析。遵守：\n"
            f"1. 委托人立场：{role}；策略必须围绕该立场组织，不利条件与对方可能主张"
            "必须完整呈现，不得隐藏（REQ-STR-004）；立场未明确时在开头显式说明"
            "「立场未明确」并按中立视角分析，不要虚构立场；\n"
            "2. 默认给出两个以上可行方案；仅有一个可行方案时必须在 singlePlanReason "
            "说明原因（REQ-STR-003）；\n"
            "3. 每个方案的前提（premises）须有材料依据；依赖未核实事实的，"
            "在 risks 中明确说明；\n"
            "4. 类案检索未接入时 legalBasis 只写材料中提及的依据，无则写「待核实」，"
            "不编造法条；\n"
            "5. 事实基础不足时输出调查与补证计划（evidenceActions），不生成确定性结论；\n"
            "6. 你不替用户选定最终方案；需律师决定的事项列入 userDecisions（REQ-STR-005）。"
        )
    focus = _PRESET_FOCUS.get(preset, "")
    return (
        f"{ROLE_PROMPT}，基于用户提供的案件材料片段输出结构化阅卷报告。遵守：\n"
        "1. 依据提供的材料片段与案件记忆（如随附，均带来源），不编造事实；两者都未提及的章节返回空数组；\n"
        "2. sourceDoc 必须使用材料或记忆来源中出现的文档标题，不得引用未提供的文档；\n"
        f"3. 委托人立场：{role}；stance 按该立场判断有利或不利，不利事实必须完整列出，不得隐藏；\n"
        "4. status=confirmed 仅当材料明确记载；单一来源、表述含糊或 OCR 质量不明时用 pending；\n"
        "5. 日期只写材料中出现的形式；只能确定月份或年份时不得补写具体日；\n"
        "6. 材料之间存在矛盾的，列入 contradictions 并同时保留两侧说法，不自行取舍；\n"
        f"7. 法条与类案仅在材料中提及时列入 legalBasis，并注明来源文档。\n{focus}"
    )


def _contract_user_prompt(state: AgentState) -> str:
    """合同审查用户提示：审查方/风险偏好/交易目的 + 切分后的条款（外发优先脱敏版）。"""
    inp = state.get("contract_input") or {}
    lines = [
        f"合同类型：{state.get('contract_type') or '未识别'}",
        f"审查方：{_contract_role_label(state)}",
        f"风险偏好：{str(inp.get('riskAppetite') or 'balanced')}",
    ]
    goal = str(inp.get("transactionGoal") or "").strip()
    law = str(inp.get("governingLaw") or "").strip()
    if goal:
        lines.append(f"交易目的：{goal}")
    if law:
        lines.append(f"适用法律：{law}")
    lines.append(f"用户关注：{state.get('redacted_request') or state['request']}")
    clauses = state.get("redacted_contract_clauses") or (state.get("contract_clauses") or [])
    lines.append("")
    lines.append("切分后的合同条款（clauseRef 为条款编号，originalText 必须逐字摘录其中原文）：")
    shown = clauses[:MAX_CONTRACT_CLAUSES_IN_PROMPT]
    for c in shown:
        text = (c.get("text") or "").replace("【", "[").replace("】", "]")
        lines.append(f"- [{c.get('clauseRef')}] {text}")
    if len(clauses) > len(shown):
        lines.append(f"（合同共 {len(clauses)} 条，超长已截断，仅分析以上 {len(shown)} 条）")
    return "\n".join(lines)


def _docgen_system_prompt(state: AgentState) -> str:
    """文书生成系统提示（PRD 15.3 / 技术方案 §5.3：提纲 + 槽位建议）。"""
    if state.get("docgen_revision_instruction"):
        return _docgen_revision_system_prompt(state)
    tpl = state.get("docgen_template") or {}
    field_names = ", ".join(str(f.get("name") or "") for f in (tpl.get("fields") or []))
    purpose = str((state.get("docgen_input") or {}).get("purpose") or state["request"])
    return (
        f"{ROLE_PROMPT}，正在起草法律文书：基于案件材料片段输出目标文书的提纲与槽位建议。遵守：\n"
        "1. outline 的章节与模板固定章节结构对齐，points 写该节应说明的内容要点，"
        "不撰写成段正文（正文在提纲确认后生成，REQ-GEN-005）；\n"
        f"2. slots.name 必须使用模板字段清单（{field_names or '暂无'}）；\n"
        "3. slots.value 必须逐字摘录材料原文，quote 为被摘录的原文片段；"
        "材料无法确认或需当事人补充的字段，不要输出槽位条目——"
        "缺失字段由系统以「xx」占位标注（REQ-GEN-003，禁止编造）；\n"
        f"4. 文书类型：{tpl.get('docType') or '未指定'}；请求事项：{purpose}；\n"
        "5. sourceDoc 必须使用材料中出现的文档标题；材料片段可能来自本任务或"
        "用户显式关联的案件（只读共享），引用时按片段中的文档标题与页码标注；"
        "slots 超过 20 条将被截断。"
    )


def _docgen_user_prompt(state: AgentState) -> str:
    """文书生成用户提示：模板字段 + 检索片段（外发优先脱敏版）。"""
    if state.get("docgen_revision_instruction"):
        return _docgen_revision_user_prompt(state)
    tpl = state.get("docgen_template") or {}
    fields = [str(f.get("name") or "") for f in (tpl.get("fields") or [])]
    hits = (state.get("redacted_hits") or state["hits"])[:MAX_CONTEXT_HITS]
    lines = [
        f"文书类型：{tpl.get('docType') or '未指定'}",
        f"请求事项：{(state.get('docgen_input') or {}).get('purpose') or state['request']}",
        f"模板字段：{'、'.join(fields) or '暂无'}",
    ]
    memory_lines = state.get("redacted_memory_lines") or state.get("memory_lines") or []
    if memory_lines:
        lines += [
            "",
            "案件记忆（含本任务与关联案件的历史结论与用户更正，均带来源标注；"
            "与材料片段同为事实依据，引用时沿用来源标注）：",
            *memory_lines,
        ]
    lines += [
        "",
        "案件材料片段（sourceDoc 必须使用其中出现的标题）：",
    ]
    for h in hits:
        snippet = (h.get("snippet") or "").replace("【", "[").replace("】", "]")[:200]
        lines.append(f"- 《{h['documentTitle']}》第{h['pageNo']}页：{snippet}")
    return "\n".join(lines)


def _docgen_revision_system_prompt(state: AgentState) -> str:
    """文书修订系统提示（交付物迭代改写）：当前稿 + 修订意见 → 修订稿全文。"""
    tpl = state.get("docgen_template") or {}
    return (
        f"{ROLE_PROMPT}，正在按用户修订意见修订法律文书。遵守：\n"
        "1. revisedBody 必须输出修订后的文书全文（Markdown），不是片段或差异说明；"
        "用户意见未涉及的内容保持原文逐字不变，不做无关润色；\n"
        "2. 修订只依据用户意见与材料片段；材料无法确认的新事实不编造，"
        "「xx」占位一律保留（REQ-GEN-003，禁止以猜测填充）；\n"
        "3. 材料片段能支撑的修改，优先逐字摘录原文；无法确认时保持原文或显式标注待核实；\n"
        f"4. 文书类型：{tpl.get('docType') or '未指定'}；修订不改文档整体结构，"
        "除非意见明确要求增删章节；\n"
        "5. revisionNotes 概述修改位置与理由；audit 提示关键事实以原文为准。"
    )


def _docgen_revision_user_prompt(state: AgentState) -> str:
    """文书修订用户提示：当前稿全文 + 修订意见 + 材料片段（外发优先脱敏版）。"""
    tpl = state.get("docgen_template") or {}
    hits = (state.get("redacted_hits") or state["hits"])[:MAX_CONTEXT_HITS]
    base = (
        state.get("docgen_redacted_base")
        or state.get("docgen_revision_base")
        or ""
    )
    instruction = (
        state.get("docgen_redacted_instruction")
        or state.get("docgen_revision_instruction")
        or ""
    )
    lines = [
        f"文书类型：{tpl.get('docType') or '未指定'}",
        "",
        "修订意见：",
        instruction,
        "",
        "当前稿全文（修订基准）：",
        base,
    ]
    memory_lines = state.get("redacted_memory_lines") or state.get("memory_lines") or []
    if memory_lines:
        lines += [
            "",
            "案件记忆（含本任务与关联案件的历史结论与用户更正，均带来源标注）：",
            *memory_lines,
        ]
    lines += [
        "",
        "案件材料片段（修改涉及事实时的依据，无法确认的不要写入）：",
    ]
    for h in hits:
        snippet = (h.get("snippet") or "").replace("【", "[").replace("】", "]")[:200]
        lines.append(f"- 《{h['documentTitle']}》第{h['pageNo']}页：{snippet}")
    return "\n".join(lines)


def _user_prompt(state: AgentState) -> str:
    if state["mode"] == "contract_review":
        return _contract_user_prompt(state)
    if state["mode"] == "document_generation":
        return _docgen_user_prompt(state)
    # 外发场景优先使用脱敏后文本（别名版）；本地未脱敏时回退原始 hits
    hits = (state.get("redacted_hits") or state["hits"])[:MAX_CONTEXT_HITS]
    title = state.get("redacted_title") or state.get("case_title", "")
    request = state.get("redacted_request") or state["request"]
    lines = [f"案件：{title}", f"用户问题：{request}"]
    memory_lines = state.get("redacted_memory_lines") or state.get("memory_lines") or []
    if memory_lines:
        lines += ["", "案件记忆（历史结论与用户更正，均带来源标注；与材料片段同为事实依据，"
                  "引用时沿用其中的来源标注，无需再检索核对）：", *memory_lines]
    lines += ["", "案件材料片段："]
    for h in hits:
        snippet = (h.get("snippet") or "").replace("【", "[").replace("】", "]")[:200]
        lines.append(f"- 《{h['documentTitle']}》第{h['pageNo']}页：{snippet}")
    return "\n".join(lines)


def _local_analysis(state: AgentState) -> dict[str, Any]:
    """无模型降级：可视化/证据分析/策略生成受限结构；法律阅卷生成受限报告；
    合同审查仅做确定性检查（引用悬空与关键条款缺失）。"""
    if state["mode"] == "contract_review":
        return _local_contract(state)
    if state["mode"] == "document_generation":
        if state.get("docgen_revision_instruction"):
            return _local_docgen_revision(state)
        return _local_docgen(state)
    if state["mode"] == "legal_review":
        preset = state.get("workflow_preset")
        if preset == "visualization":
            return _local_viz(state)
        if preset == "evidence_analysis":
            return _local_evidence(state)
        if preset == "strategy":
            return _local_strategy(state)
        return _local_report(state)
    hits = state["hits"][:3]
    if not hits:
        return {
            "intro": "当前案件暂无匹配材料，请先导入卷宗后再发起分析。",
            "points": [],
            "audit": "本地检索分析（未调用远程模型）· 材料为空，本结论不构成法律意见",
        }
    points = []
    for h in hits:
        snippet = (h.get("snippet") or "").replace("\n", " ")
        points.append({
            "title": h["documentTitle"],
            "desc": f"第{h['pageNo']}页命中相关内容：{snippet[:60]}…",
            "sourceDoc": h["documentTitle"],
        })
    return {
        "intro": f"基于本案件 {len(state['hits'])} 处材料命中，梳理出以下要点：",
        "points": points,
        "audit": "本地检索分析（未调用远程模型）· 以上为材料命中摘要，关键事实请以原文为准",
    }


def _local_report(state: AgentState) -> dict[str, Any]:
    """本地降级报告：基于真实检索命中，结构完整但章节受限（明确标注降级）。"""
    from ..citations import clean_snippet

    role = state.get("client_role") or "未设置"
    hits = state["hits"][:6]
    if not hits:
        return {
            "overview": "当前案件暂无匹配材料。请先导入卷宗后再发起阅卷分析。",
            "parties": [], "timeline": [], "disputes": [], "facts": [], "contradictions": [],
            "missing": ["本地降级模式无法进行完整阅卷分析，请配置模型后重新发起任务"],
            "legalBasis": [],
            "nextSteps": ["导入卷宗材料后重新发起阅卷分析"],
        }
    facts = [
        {
            "text": f"材料命中：{clean_snippet(h.get('snippet') or '')[:60]}",
            "stance": "neutral",
            "status": "pending",
            "sourceDoc": h["documentTitle"],
        }
        for h in hits
    ]
    return {
        "overview": (
            f"本地检索分析：在案件材料中命中 {len(state['hits'])} 处片段"
            f"（委托人立场：{role}）。以下为材料命中摘要，非完整阅卷报告。"
        ),
        "parties": [], "timeline": [], "disputes": [], "facts": facts, "contradictions": [],
        "missing": ["本地降级模式无法提取当事人、时间线与争议焦点，请配置模型后重新发起任务"],
        "legalBasis": [],
        "nextSteps": ["配置模型 API Key 并以远程模式重新发起阅卷分析"],
    }


def _local_viz(state: AgentState) -> dict[str, Any]:
    """本地降级可视化：材料命中作为时间线节点（日期未知），关系图留空并说明。"""
    from ..citations import clean_snippet

    hits = state["hits"][:8]
    nodes = [
        {
            "date": "",
            "event": f"材料命中：{clean_snippet(h.get('snippet') or '')[:60]}",
            "actors": [],
            "sourceDoc": h["documentTitle"],
        }
        for h in hits
    ]
    return {
        "timeline": {"nodes": nodes},
        "relationGraph": {"nodes": [], "edges": []},
        "flows": [],
        "audit": (
            "本地检索分析（未调用远程模型）· 时间线为材料命中摘要（日期未知），"
            "关系图需模型生成；请配置模型后重新发起可视化阅卷"
        ),
    }


def _local_evidence(state: AgentState) -> dict[str, Any]:
    """本地降级证据分析：按检索命中生成受限证据卡（分析意见留待人工/模型）。"""
    from ..citations import clean_snippet

    hits = state["hits"][:6]
    cards = [
        {
            "name": h["documentTitle"],
            "type": "待核实",
            "sourceDoc": h["documentTitle"],
            "formedAt": "",
            "provider": "",
            "custody": "",
            "purpose": "材料命中（证明目的待模型分析）",
            "keyFragment": clean_snippet(h.get("snippet") or "")[:60],
            "authenticity": "",
            "legality": "无法判断",
            "relevance": "",
            "weight": "",
            "relations": [],
            "defects": "",
            "reinforce": "",
            "crossExam": "",
        }
        for h in hits
    ]
    return {
        "evidence": cards,
        "matrix": {"disputes": [], "cells": []},
        "audit": (
            "本地检索分析（未调用远程模型）· 证据卡为材料命中摘要，"
            "合法性无法判断（取得程序材料未检索）；请配置模型后重新发起证据分析"
        ),
    }


def _local_strategy(state: AgentState) -> dict[str, Any]:
    """本地降级策略：材料命中作为有利/不利线索（待核实），不生成方案。"""
    from ..citations import clean_snippet

    role = state.get("client_role") or "未设置"
    hits = state["hits"][:6]
    favorable = [
        {"text": f"材料命中：{clean_snippet(h.get('snippet') or '')[:50]}", "sourceDoc": h["documentTitle"]}
        for h in hits
    ]
    return {
        "position": f"委托人立场：{role}（本地降级模式，未生成完整策略）",
        "keyDisputes": [],
        "favorable": favorable,
        "unfavorable": [],
        "infoGaps": ["本地降级模式无法评估不利条件与信息缺口，请配置模型后重新发起策略分析"],
        "plans": [],
        "singlePlanReason": "本地降级模式未调用远程模型，无法生成可行方案比较（REQ-STR-003）",
        "evidenceActions": ["配置模型 API Key 后以远程模式重新发起辩护策略分析"],
        "userDecisions": [],
        "audit": "本地检索分析（未调用远程模型）· 以上为材料命中线索，不构成策略意见",
    }


def _local_contract(state: AgentState) -> dict[str, Any]:
    """本地降级合同审查：只做两类确定性检查——
    1. 交叉引用悬空：条款引用的「第X条」在切分结果中不存在；
    2. 关键条款缺失：全文检索不到违约责任/争议解决/终止相关表述。
    不做语义风险分析（REQ-CON-008 不编造）；降级状态由 node_verify 写入审查会话。
    """
    clauses = state.get("contract_clauses") or []
    full_text = state.get("contract_full_text") or ""
    ref_set = {str(c.get("clauseRef") or "") for c in clauses}
    risks: list[dict[str, Any]] = []

    for c in clauses[:MAX_CONTRACT_CLAIMS]:
        dangling = [r for r in (c.get("refs") or []) if r not in ref_set]
        if not dangling:
            continue
        risks.append({
            "riskLevel": "medium",
            "category": "legal",
            "clauseRef": str(c.get("clauseRef") or ""),
            "originalText": str(c.get("text") or "")[:120],
            "riskNote": f"引用的{dangling[0]}在合同中不存在",
            "impact": "被引条款缺失，权利义务指向不明",
            "suggestion": f"核对{dangling[0]}编号或补充该条款",
            "replacementText": "",
            "basis": "条款切分结果中无该编号",
            "confidence": "verified",
        })
    for kw, note in (
        ("违约", "缺少违约责任条款，违约后难以追责"),
        ("争议解决", "缺少争议解决条款，纠纷处理方式与管辖不明"),
        ("解除", "缺少解除/终止条款，合同退出机制不明"),
    ):
        if kw not in full_text:
            risks.append({
                "riskLevel": "high",
                "category": "completeness",
                "clauseRef": "",
                "originalText": "",
                "riskNote": f"未检索到「{kw}」相关条款",
                "impact": note,
                "suggestion": f"补充{kw}相关条款",
                "replacementText": "",
                "basis": "合同全文关键词检索",
                "confidence": "verified",
            })
    return {
        "risks": risks[:MAX_CONTRACT_CLAIMS],
        "audit": (
            "本地规则检查（未调用远程模型）· 仅覆盖条款引用悬空与关键条款缺失，"
            "语义风险识别需配置模型后重新发起合同审查"
        ),
    }


def _local_docgen(state: AgentState) -> dict[str, Any]:
    """本地降级文书生成：提纲从模板固定章节确定性推导；槽位建议为空
    （无引语校验支撑，不冒充案件事实 REQ-GEN-004），由用户补缺或配置模型。
    草稿保持提纲待确认态，正文由用户确认提纲后确定性渲染（REQ-GEN-005）。"""
    from .. import docgen as docgen_store

    tpl = state.get("docgen_template") or {}
    outline = docgen_store.outline_from_template(str(tpl.get("contentMd") or ""))
    return {
        "outline": outline,
        "slots": [],
        "audit": (
            "本地规则分析（未调用远程模型）· 提纲由模板结构推导；槽位建议需配置模型，"
            "缺失字段将以占位符替换，请补全后导出"
        ),
    }


def _local_docgen_revision(state: AgentState) -> dict[str, Any]:
    """本地降级文书修订：语义修订必须依赖模型，本地模式不伪造修订稿
    （不落 ai_revise 版本，不改动当前稿），显式告知用户配置模型后重试。"""
    return {
        "revisedBody": state.get("docgen_revision_base") or "",
        "revisionNotes": "",
        "audit": (
            "本地模式未调用远程模型，未执行语义修订（当前稿保持不变，未落新版本）；"
            "请在配置模型 API Key 后重新发起修订"
        ),
    }


# ---------------------------------------------------------------------------
# 节点：校验与引证绑定（Claim → SourceRef）
# ---------------------------------------------------------------------------

def _as_str(v: Any) -> str:
    return str(v) if v is not None else ""


def _as_items(v: Any) -> list[dict[str, Any]]:
    return [x for x in (v or []) if isinstance(x, dict)] if isinstance(v, list) else []


def _as_dict(v: Any) -> dict[str, Any]:
    """模型输出形态容错：期望对象时非 dict（如报告形态的列表）降级为空对象，不抛错。"""
    return v if isinstance(v, dict) else {}


# 报告中带 sourceDoc 的章节与 Claim 文本组装（REQ-REV-008：核心事实绑定引证）
_REPORT_SECTIONS: tuple[tuple[str, Callable[[dict[str, Any]], str]], ...] = (
    ("parties", lambda it: f"{_as_str(it.get('name'))}（{_as_str(it.get('role'))}）：{_as_str(it.get('note'))}"),
    ("timeline", lambda it: f"{_as_str(it.get('date'))} {_as_str(it.get('event'))}"),
    ("disputes", lambda it: f"{_as_str(it.get('title'))}：{_as_str(it.get('desc'))}"),
    ("facts", lambda it: _as_str(it.get("text"))),
    ("legalBasis", lambda it: f"{_as_str(it.get('title'))}：{_as_str(it.get('point'))}"),
)


def _find_hit(hits: list[dict[str, Any]], source_doc: Any) -> dict[str, Any] | None:
    s = str(source_doc or "").strip()
    if not s:
        return None
    return next((h for h in hits if s in h["documentTitle"]), None)


def _find_hit_in_text(hits: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    """反向匹配：文档标题出现在自由文本中（矛盾 sideA/sideB 为"说法+来源文档"混排）。"""
    t = str(text or "")
    if not t.strip():
        return None
    return next((h for h in hits if h["documentTitle"] and h["documentTitle"] in t), None)


def _bind_report_claims(
    task_id: str,
    case_id: str,
    analysis: dict[str, Any],
    hits: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """规范化报告结构，并为带来源的条目逐一绑定 Claim（引语校验）。

    - facts 为核心事实：模型未指明来源时按序兜底绑定检索命中（保持可回原文）；
    - 其余章节仅绑定模型指明且能匹配的来源；无来源时进入 needs_human。
    """
    from ..citations import judge_claim

    report: dict[str, Any] = {
        "overview": _as_str(analysis.get("overview")),
        "parties": _as_items(analysis.get("parties")),
        "timeline": _as_items(analysis.get("timeline")),
        "disputes": _as_items(analysis.get("disputes")),
        "facts": _as_items(analysis.get("facts")),
        "contradictions": _as_items(analysis.get("contradictions")),
        "missing": [str(x) for x in (analysis.get("missing") or []) if x],
        "legalBasis": _as_items(analysis.get("legalBasis")),
        "nextSteps": [str(x) for x in (analysis.get("nextSteps") or []) if x],
    }
    # stance / status 规范化（防模型输出越界值）
    for f in report["facts"]:
        if f.get("stance") not in ("favorable", "unfavorable", "neutral"):
            f["stance"] = "neutral"
        if f.get("status") not in ("confirmed", "pending"):
            f["status"] = "pending"

    cites: list[dict[str, Any]] = []
    needs_human = 0
    bound = 0
    for section, text_of in _REPORT_SECTIONS:
        for item in report[section]:
            if not str(text_of(item)).strip():
                continue
            if bound >= MAX_REPORT_CLAIMS:
                break
            hit = _find_hit(hits, item.get("sourceDoc"))
            if hit is None and section == "facts" and hits:
                # 核心事实兜底：绑定检索命中保持可回原文（REQ-REV-008）
                hit = hits[bound % len(hits)]
            cite = judge_claim(
                task_id=task_id,
                case_id=case_id,
                text=str(text_of(item))[:200],
                source_hit=hit,
            )
            item["cite"] = cite
            cites.append(cite)
            bound += 1
            if cite["claimStatus"] == "needs_human":
                needs_human += 1

    # 矛盾证据（R-004）：双侧各建 Claim 并校验引语；双侧均 supported 时互指 conflict
    from ..citations import mark_conflict

    for item in report["contradictions"]:
        if bound >= MAX_REPORT_CLAIMS:
            break
        side_a, side_b = _as_str(item.get("sideA")), _as_str(item.get("sideB"))
        if not side_a.strip() or not side_b.strip():
            continue
        cite_a = judge_claim(task_id, case_id, side_a[:200], _find_hit_in_text(hits, side_a))
        cite_b = judge_claim(task_id, case_id, side_b[:200], _find_hit_in_text(hits, side_b))
        item["citeA"], item["citeB"] = cite_a, cite_b
        cites.extend([cite_a, cite_b])
        bound += 2
        for c in (cite_a, cite_b):
            if c["claimStatus"] == "needs_human":
                needs_human += 1
        if (
            cite_a["claimStatus"] == "supported"
            and cite_b["claimStatus"] == "supported"
            and mark_conflict(cite_a["claimId"], cite_b["claimId"])
        ):
            cite_a["claimStatus"] = "conflict"
            cite_b["claimStatus"] = "conflict"
    return report, cites, needs_human


def _bind_viz_claims(
    task_id: str,
    case_id: str,
    analysis: dict[str, Any],
    hits: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """规范化可视化 Spec 并为时间线节点、关系图节点与边逐一绑定 Claim。

    - REQ-VIS-001：每个事实节点有来源（cite）或待核实标记（status），
      模型未指明来源时按序兜底绑定检索命中（保持可回原文）；
    - 规模保护（PRD 13.4 节点过多先聚合）：超出上限的节点丢弃并计数留痕；
    - 边的 from/to 必须指向存在的节点名，悬空边与重复边（同向同标签）丢弃。
    """
    from ..citations import judge_claim

    # 形态容错：schema 要求 {nodes:[...]}，模型可能直接输出节点列表（报告形态）
    tl_raw = analysis.get("timeline")
    tl_nodes = _as_items(tl_raw.get("nodes") if isinstance(tl_raw, dict) else tl_raw)[:MAX_TIMELINE_NODES]
    g_raw = analysis.get("relationGraph")
    g_nodes = _as_items(g_raw.get("nodes") if isinstance(g_raw, dict) else g_raw)[:MAX_GRAPH_NODES]
    g_edges = _as_items(_as_dict(g_raw).get("edges"))[:MAX_GRAPH_EDGES]

    # 边规范化：悬空边 / 重复边丢弃
    names = {str(n.get("name") or "").strip() for n in g_nodes if str(n.get("name") or "").strip()}
    seen_edges: set[tuple[str, str, str]] = set()
    edges: list[dict[str, Any]] = []
    dropped_edges = 0
    for e in g_edges:
        f, t = str(e.get("from") or "").strip(), str(e.get("to") or "").strip()
        key = (f, t, str(e.get("label") or "").strip())
        if f not in names or t not in names or key in seen_edges:
            dropped_edges += 1
            continue
        seen_edges.add(key)
        edges.append({"from": f, "to": t, "label": key[2], "sourceDoc": e.get("sourceDoc")})

    viz: dict[str, Any] = {
        "timeline": {
            "nodes": [
                {
                    "date": _as_str(n.get("date")),
                    "event": _as_str(n.get("event")),
                    "actors": [str(a) for a in (n.get("actors") or []) if a],
                    "sourceDoc": n.get("sourceDoc"),
                }
                for n in tl_nodes
            ]
        },
        "relationGraph": {
            "nodes": [
                {
                    "name": _as_str(n.get("name")),
                    "kind": _as_str(n.get("kind")) or "person",
                    "role": _as_str(n.get("role")),
                    "note": _as_str(n.get("note")),
                    "sourceDoc": n.get("sourceDoc"),
                }
                for n in g_nodes
                if _as_str(n.get("name")).strip()
            ],
            "edges": edges,
        },
    }
    # 悬空边可能引用被规模上限丢弃的节点，二次过滤
    final_names = {n["name"] for n in viz["relationGraph"]["nodes"]}
    kept_edges = [e for e in edges if e["from"] in final_names and e["to"] in final_names]
    dropped_edges += len(edges) - len(kept_edges)
    viz["relationGraph"]["edges"] = kept_edges

    # flows 规范化：title 非空才保留；shape 越界回退 rect；悬空边/重复边丢弃
    flows_raw = _as_items(analysis.get("flows"))[:MAX_FLOWS]
    flows: list[dict[str, Any]] = []
    dropped_flow_edges = 0
    for f in flows_raw:
        # 形态容错：nodes 可能是 {nodes:[...]} 包裹
        f_nodes_raw = f.get("nodes")
        if isinstance(f_nodes_raw, dict):
            f_nodes_raw = f_nodes_raw.get("nodes")
        f_nodes = _as_items(f_nodes_raw)[:MAX_FLOW_NODES]
        f_ids = {str(n.get("id") or "").strip() for n in f_nodes if str(n.get("id") or "").strip()}
        f_edges_raw = _as_items(f.get("edges"))[:MAX_FLOW_EDGES]
        f_seen: set[tuple[str, str, str]] = set()
        f_edges: list[dict[str, Any]] = []
        for e in f_edges_raw:
            fid, tid = str(e.get("from") or "").strip(), str(e.get("to") or "").strip()
            lab = str(e.get("label") or "").strip()
            if fid not in f_ids or tid not in f_ids or (fid, tid, lab) in f_seen:
                dropped_flow_edges += 1
                continue
            f_seen.add((fid, tid, lab))
            f_edges.append({"from": fid, "to": tid, "label": lab, "dashed": bool(e.get("dashed")),
                            "sourceDoc": e.get("sourceDoc")})
        title = _as_str(f.get("title"))
        if title and f_nodes:
            flows.append({
                "title": title,
                "direction": "LR" if _as_str(f.get("direction")) == "LR" else "TB",
                "nodes": [
                    {
                        "id": _as_str(n.get("id")),
                        "label": _as_str(n.get("label")),
                        "shape": _as_str(n.get("shape")) if _as_str(n.get("shape")) in ("rect", "stadium", "round", "diamond", "circle") else "rect",
                        "sourceDoc": n.get("sourceDoc"),
                    }
                    for n in f_nodes
                    if _as_str(n.get("id")).strip()
                ],
                "edges": f_edges,
            })
    if flows:
        viz["flows"] = flows
    dropped_edges += dropped_flow_edges
    if dropped_edges:
        viz["warnings"] = [f"已丢弃 {dropped_edges} 条悬空或重复关系边"]

    cites: list[dict[str, Any]] = []
    needs_human = 0
    bound = 0

    def _bind(item: dict[str, Any], text: str) -> None:
        nonlocal bound, needs_human
        if bound >= MAX_REPORT_CLAIMS or not text.strip():
            item["status"] = "pending"  # 超出绑定上限：显式待核实标记（REQ-VIS-001）
            return
        hit = _find_hit(hits, item.get("sourceDoc"))
        if hit is None and hits:
            hit = hits[bound % len(hits)]  # 兜底绑定保持可回原文
        cite = judge_claim(task_id, case_id, text[:200], hit)
        item["cite"] = cite
        item["status"] = cite["claimStatus"]
        cites.append(cite)
        bound += 1
        if cite["claimStatus"] == "needs_human":
            needs_human += 1

    for n in viz["timeline"]["nodes"]:
        _bind(n, f"{n['date']} {n['event']}".strip())
    for n in viz["relationGraph"]["nodes"]:
        _bind(n, f"{n['name']}（{n['role'] or n['kind']}）")
    for e in viz["relationGraph"]["edges"]:
        _bind(e, f"{e['from']} → {e['to']}：{e['label']}")
    for f in viz.get("flows", []):
        for n in f["nodes"]:
            _bind(n, n["label"])
        for e in f["edges"]:
            _bind(e, f"{e['from']} → {e['to']}：{e['label']}".rstrip("："))
    return viz, cites, needs_human


_MATRIX_RELATIONS = ("supports", "opposes", "conflict", "missing")


def _bind_evidence_claims(
    task_id: str,
    case_id: str,
    analysis: dict[str, Any],
    hits: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """规范化证据分析并逐张证据卡绑定 Claim（PRD 13.5）。

    - REQ-EVI-002：每个证明目的可回原文 —— 每张卡以「名称+证明目的+关键片段」建 Claim；
    - REQ-EVI-004：合法性为空或含「无法判断」→ 卡级 legalityPending 标记并计入待核实；
    - 矩阵 cells 的悬空引用（dispute/evidence 不存在）或 relation 越界时丢弃并留痕；
    - 事实字段与分析意见字段在结构上分开（REQ-EVI-001，UI 分区渲染）。
    """
    from ..citations import judge_claim

    cards_raw = _as_items(analysis.get("evidence"))[:MAX_EVIDENCE_CARDS]
    matrix_raw = _as_dict(analysis.get("matrix"))
    disputes = [str(d).strip() for d in (matrix_raw.get("disputes") or []) if str(d).strip()]
    disputes = disputes[:MAX_MATRIX_DISPUTES]
    cells_raw = _as_items(matrix_raw.get("cells"))

    cards: list[dict[str, Any]] = []
    for c in cards_raw:
        name = _as_str(c.get("name")).strip()
        if not name:
            continue
        cards.append(
            {
                "name": name,
                "type": _as_str(c.get("type")),
                "sourceDoc": c.get("sourceDoc"),
                "formedAt": _as_str(c.get("formedAt")),
                "provider": _as_str(c.get("provider")),
                "custody": _as_str(c.get("custody")),
                "purpose": _as_str(c.get("purpose")),
                "keyFragment": _as_str(c.get("keyFragment")),
                # 以下为分析意见字段（REQ-EVI-001：与材料记载分开）
                "authenticity": _as_str(c.get("authenticity")),
                "legality": _as_str(c.get("legality")),
                "relevance": _as_str(c.get("relevance")),
                "weight": _as_str(c.get("weight")),
                "relations": [
                    {"to": _as_str(r.get("to")), "relation": _as_str(r.get("relation"))}
                    for r in _as_items(c.get("relations"))
                    if _as_str(r.get("to")).strip()
                ],
                "defects": _as_str(c.get("defects")),
                "reinforce": _as_str(c.get("reinforce")),
                "crossExam": _as_str(c.get("crossExam")),
            }
        )

    names = {c["name"] for c in cards}
    dispute_set = set(disputes)
    cells: list[dict[str, Any]] = []
    dropped = 0
    for cell in cells_raw:
        d = _as_str(cell.get("dispute")).strip()
        e = _as_str(cell.get("evidence")).strip()
        rel = _as_str(cell.get("relation")).strip()
        if d not in dispute_set or e not in names or rel not in _MATRIX_RELATIONS:
            dropped += 1
            continue
        cells.append({"dispute": d, "evidence": e, "relation": rel, "note": _as_str(cell.get("note"))})

    cites: list[dict[str, Any]] = []
    needs_human = 0
    for i, card in enumerate(cards):
        if i < MAX_REPORT_CLAIMS:
            text = f"{card['name']}：{card['purpose'] or '证明目的待补充'}"
            if card["keyFragment"]:
                text += f"（{card['keyFragment'][:60]}）"
            hit = _find_hit(hits, card.get("sourceDoc"))
            if hit is None and hits:
                hit = hits[i % len(hits)]
            cite = judge_claim(task_id, case_id, text[:200], hit)
            card["cite"] = cite
            cites.append(cite)
            if cite["claimStatus"] == "needs_human":
                needs_human += 1
        # REQ-EVI-004：合法性无法判断 → 显式待核实标记并计入核实队列
        if not card["legality"].strip() or "无法判断" in card["legality"]:
            card["legalityPending"] = True
            needs_human += 1

    spec: dict[str, Any] = {"evidence": cards, "matrix": {"disputes": disputes, "cells": cells}}

    # 卡间矛盾（R-004）：relations 含"矛盾"且双侧均有 Claim 时互指 conflict
    from ..citations import mark_conflict

    claim_ids = {c["name"]: c["cite"]["claimId"] for c in cards if c.get("cite", {}).get("claimId")}
    for card in cards:
        for rel in card.get("relations") or []:
            to_name = str(rel.get("to") or "").strip()
            if "矛盾" not in str(rel.get("relation") or ""):
                continue
            a_id, b_id = claim_ids.get(card["name"]), claim_ids.get(to_name)
            if a_id and b_id and a_id != b_id and mark_conflict(a_id, b_id):
                card["cite"]["claimStatus"] = "conflict"
                counterpart = next((c for c in cards if c["name"] == to_name), None)
                if counterpart and counterpart.get("cite"):
                    counterpart["cite"]["claimStatus"] = "conflict"

    if dropped:
        spec["warnings"] = [f"已丢弃 {dropped} 条无效矩阵关系（悬空引用或关系类型越界）"]
    return spec, cites, needs_human


def _bind_strategy_claims(
    task_id: str,
    case_id: str,
    analysis: dict[str, Any],
    hits: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """规范化策略分析并逐项绑定 Claim（PRD 13.6）。

    - 有利/不利条件与每个方案逐项建 Claim（REQ-STR-002：关键前提有来源或待核实）；
    - REQ-STR-003：方案数为 1 且无原因说明 → 警告并计入待核实；
      方案数为 0 → 警告（事实基础不足，应有补证计划）；
    - 方案数超过上限截断留痕。
    """
    from ..citations import judge_claim

    plans_raw = _as_items(analysis.get("plans"))
    spec: dict[str, Any] = {
        "position": _as_str(analysis.get("position")),
        "keyDisputes": [str(d) for d in (analysis.get("keyDisputes") or []) if d],
        "favorable": _as_items(analysis.get("favorable")),
        "unfavorable": _as_items(analysis.get("unfavorable")),
        "infoGaps": [str(x) for x in (analysis.get("infoGaps") or []) if x],
        "singlePlanReason": _as_str(analysis.get("singlePlanReason")),
        "evidenceActions": [str(x) for x in (analysis.get("evidenceActions") or []) if x],
        "userDecisions": [str(x) for x in (analysis.get("userDecisions") or []) if x],
    }
    warnings: list[str] = []
    if len(plans_raw) > MAX_STRATEGY_PLANS:
        warnings.append(f"方案数超过 {MAX_STRATEGY_PLANS}，已截断")
        plans_raw = plans_raw[:MAX_STRATEGY_PLANS]
    if len(plans_raw) == 1 and not spec["singlePlanReason"].strip():
        warnings.append("仅一个方案且未说明原因（REQ-STR-003）")
        spec["singlePlanReason"] = "（模型未说明单一方案原因，需人工复核）"
    if len(plans_raw) == 0 and not spec["evidenceActions"]:
        warnings.append("未生成可行方案，且缺少补证计划")

    plans: list[dict[str, Any]] = [
        {
            "title": _as_str(p.get("title")).strip() or "未命名方案",
            "premises": [str(x) for x in (p.get("premises") or []) if x],
            "legalBasis": [str(x) for x in (p.get("legalBasis") or []) if x],
            "actions": [str(x) for x in (p.get("actions") or []) if x],
            "benefits": _as_str(p.get("benefits")),
            "risks": _as_str(p.get("risks")),
            "counter": _as_str(p.get("counter")),
            "sourceDoc": p.get("sourceDoc"),
        }
        for p in plans_raw
    ]

    cites: list[dict[str, Any]] = []
    needs_human = 0
    bound = 0

    def _bind(item: dict[str, Any], text: str) -> None:
        nonlocal bound, needs_human
        if bound >= MAX_REPORT_CLAIMS or not text.strip():
            return
        hit = _find_hit(hits, item.get("sourceDoc"))
        if hit is None and hits:
            hit = hits[bound % len(hits)]
        cite = judge_claim(task_id, case_id, text[:200], hit)
        item["cite"] = cite
        cites.append(cite)
        bound += 1
        if cite["claimStatus"] == "needs_human":
            needs_human += 1

    for item in spec["favorable"]:
        _bind(item, _as_str(item.get("text")))
    for item in spec["unfavorable"]:
        _bind(item, _as_str(item.get("text")))
    for p in plans:
        _bind(p, f"方案「{p['title']}」：{p['benefits'] or p['risks'] or '前提见清单'}")

    spec["plans"] = plans
    if warnings:
        spec["warnings"] = warnings
    return spec, cites, needs_human


def _restore_analysis(case_id: str, value: Any) -> Any:
    """递归把模型输出中的案件内别名恢复为原文（REQ-PRIV-001，本机受信场景）。

    模型基于脱敏片段生成，文本中可能出现「人物A」等别名；引证校验比对
    材料原文，故绑定 Claim 前先恢复。restore 未命中时不改变文本。
    """
    from .. import redaction

    if isinstance(value, str):
        return redaction.restore(case_id, value)
    if isinstance(value, list):
        return [_restore_analysis(case_id, v) for v in value]
    if isinstance(value, dict):
        return {k: _restore_analysis(case_id, v) for k, v in value.items()}
    return value


def _bind_contract_claims(
    state: AgentState,
    analysis: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """规范化风险项并逐项绑定 Claim（合同条款引证，PRD 14.4）。

    - clauseRef 匹配切分条款 → 以该条款所在页为来源，条款原文逐字可回时 supported；
    - clauseRef 未匹配但原文能定位 → 原文前缀匹配条款，再退全文偏移定位页码；
    - 缺失类风险（completeness，无条款原文可引）→ 不绑来源，进 needs_human；
    - 值域规范化与落库校验一致；模型自报 verified 但引证未通过时降为 medium。
    """
    from .. import contract as contract_store
    from ..citations import judge_claim

    task_id = state["task_id"]
    case_id = state["case_id"]
    src = state.get("contract_source") or {}
    clauses = state.get("contract_clauses") or []
    pages = state.get("contract_pages") or []
    full_text = state.get("contract_full_text") or ""
    clause_by_ref = {str(c.get("clauseRef") or ""): c for c in clauses}

    risks: list[dict[str, Any]] = []
    cites: list[dict[str, Any]] = []
    needs_human = 0
    for r in _as_items(analysis.get("risks"))[:MAX_CONTRACT_CLAIMS]:
        item = {
            "riskLevel": r.get("riskLevel") if r.get("riskLevel") in contract_store.RISK_LEVELS else "medium",
            "category": r.get("category") if r.get("category") in contract_store.RISK_CATEGORIES else "legal",
            "clauseRef": _as_str(r.get("clauseRef")),
            "originalText": _as_str(r.get("originalText")),
            "riskNote": _as_str(r.get("riskNote")),
            "impact": _as_str(r.get("impact")),
            "suggestion": _as_str(r.get("suggestion")),
            "replacementText": _as_str(r.get("replacementText")),
            "basis": _as_str(r.get("basis")),
            "confidence": r.get("confidence") if r.get("confidence") in contract_store.CONFIDENCES else "medium",
        }
        # 来源定位：条款编号 → 原文前缀匹配切分条款 → 全文偏移
        clause = clause_by_ref.get(item["clauseRef"])
        hit: dict[str, Any] | None = None
        if clause is None and item["originalText"]:
            head = item["originalText"][:30]
            clause = next((c for c in clauses if head in str(c.get("text") or "")), None)
        if clause is not None and src.get("versionId"):
            hit = {
                "documentId": src.get("documentId"),
                "versionId": src["versionId"],
                "pageNo": clause.get("pageNo") or (pages[0]["pageNo"] if pages else None),
                "snippet": str(clause.get("text") or ""),
            }
        elif item["originalText"] and src.get("versionId"):
            offset = full_text.find(item["originalText"][:30])
            if offset >= 0:
                hit = {
                    "documentId": src.get("documentId"),
                    "versionId": src["versionId"],
                    "pageNo": contract_store.page_of_offset(pages, full_text, offset),
                    "snippet": item["originalText"],
                }
        cite = judge_claim(
            task_id=task_id,
            case_id=case_id,
            text=(item["originalText"] or item["riskNote"])[:200],
            source_hit=hit,
        )
        if cite["claimStatus"] == "needs_human":
            needs_human += 1
            if item["confidence"] == "verified":
                item["confidence"] = "medium"
        item["claimId"] = cite["claimId"]
        item["cite"] = cite
        cites.append(cite)
        risks.append(item)
    return {"risks": risks}, cites, needs_human


def _bind_docgen_suggestions(
    task_id: str,
    case_id: str,
    analysis: dict[str, Any],
    hits: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """规范化文书生成输出（提纲 + 槽位建议）并逐条绑定 Claim（PRD 15.3）。

    - 槽位建议必须带 quote（逐字摘录的原文片段）才能按案件原文引证：
      引语校验通过 → case_evidence（附 sourceRef，REQ-GEN-004）；
      无 quote 或校验未过 → pending_verify 待人工核实，不冒充案件原文；
    - outline 为提纲条目（heading/points/sourceDoc），落库后待用户确认。
    """
    from ..citations import judge_claim

    outline: list[dict[str, Any]] = []
    for it in _as_items(analysis.get("outline"))[:30]:
        heading = _as_str(it.get("heading")).strip()
        if not heading:
            continue
        outline.append({
            "heading": heading[:60],
            "points": _as_str(it.get("points")).strip(),
            "sourceDoc": _as_str(it.get("sourceDoc")).strip(),
        })
    if not outline:
        outline = [{"heading": "正文", "points": "", "sourceDoc": ""}]

    slots: list[dict[str, Any]] = []
    cites: list[dict[str, Any]] = []
    needs_human = 0
    for s in _as_items(analysis.get("slots"))[:MAX_DOCGEN_SLOTS]:
        name = _as_str(s.get("name")).strip()
        value = _as_str(s.get("value")).strip()
        quote = _as_str(s.get("quote")).strip()
        if not name or not value:
            continue
        hit = _find_hit(hits, s.get("sourceDoc")) if quote else None
        cite = judge_claim(task_id, case_id, (quote or value)[:200], hit)
        verified = cite["claimStatus"] == "supported"
        source_ref = None
        if verified and hit is not None:
            source_ref = {
                "documentId": hit.get("documentId"),
                "documentTitle": hit.get("documentTitle"),
                "pageNo": hit.get("pageNo"),
                "quote": quote[:200],
            }
        if not verified:
            needs_human += 1
        slots.append({
            "name": name, "value": value, "verified": verified,
            "sourceRef": source_ref, "claimId": cite["claimId"],
        })
        cites.append(cite)
    return {"outline": outline, "slots": slots}, cites, needs_human


def build_final_body(
    state: AgentState,
    analysis: dict[str, Any],
    hits: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], int, dict[str, Any]]:
    """按模式/预设规范化分析结果、绑定 Claim 并组装最终 message body。

    node_verify（固定流程）与阅卷 Loop（loop.py）共用同一套绑定与产物结构，
    保证两条路径输出形态一致（评测与 UI 无需分叉）。extra 中的键合并进
    body（Loop 用于附加通读覆盖率与校验报告）。
    """
    from ..citations import judge_claim

    task_id = state["task_id"]

    if state["mode"] == "contract_review":
        # 合同审查：风险项绑定 Claim → 落库 contract_risk_items → 审查会话收尾（PRD 14.4）
        from .. import contract as contract_store

        spec, cites, needs_human = _bind_contract_claims(state, analysis)
        review_id = state.get("contract_review_id")
        if review_id:
            contract_store.save_risk_items(review_id, spec["risks"])
            contract_store.update_review_status(
                review_id, "completed", degraded_reason=state.get("degraded_reason"),
            )
        risk_stats = {
            "severe": 0, "high": 0, "medium": 0, "low": 0,
            "total": len(spec["risks"]), "pending": len(spec["risks"]),
        }
        for r in spec["risks"]:
            risk_stats[r["riskLevel"]] += 1
        body: dict[str, Any] = {
            "contract": {
                "reviewId": review_id,
                "contractType": state.get("contract_type"),
                "risks": spec["risks"],
                "riskStats": risk_stats,
            },
            "cites": cites,
            "audit": (analysis.get("audit") or "以上为 AI 审查意见，风险认定以合同原文与律师判断为准")
            + (f" · {needs_human} 项待人工核实" if needs_human else ""),
            "degradedReason": state.get("degraded_reason"),
        }
        checkpoint = {
            "reviewId": review_id,
            "risks": len(spec["risks"]),
            "needsHuman": needs_human,
            "degraded": state.get("degraded_reason"),
        }
    elif state["mode"] == "document_generation":
        # 文书生成：提纲落库；槽位建议引语校验后填充（案件原文需引证 REQ-GEN-004）；
        # 草稿保持提纲待确认态，正文由用户确认提纲后经 docgen.render 生成（REQ-GEN-005）
        from .. import docgen as docgen_store

        # 修订分叉（交付物迭代改写）：修订稿落 kind='ai_revise' 新版本。
        # 本地降级不伪造修订（revisedBody 与当前稿一致时不落库，显式告知）。
        if state.get("docgen_revision_instruction"):
            draft_id = state.get("docgen_draft_id")
            revised_body = _as_str(analysis.get("revisedBody")).strip()
            notes = _as_str(analysis.get("revisionNotes")).strip()
            applied = bool(
                draft_id
                and revised_body
                and revised_body != (state.get("docgen_revision_base") or "").strip()
            )
            revision: dict[str, Any] = {"applied": applied, "instruction": state["docgen_revision_instruction"]}
            if applied:
                try:
                    res = docgen_store.revise_body(
                        str(draft_id), revised_body, state["docgen_revision_instruction"],
                        base_version_no=state.get("docgen_revision_base_version"),
                    )
                except docgen_store.DocGenError as e:
                    # 版本冲突（升级方案 §7.4）：修订基准已被用户编辑或另一修订推进，
                    # 拒绝落库并显式告知，不覆盖较新的当前稿。
                    if "VERSION_CONFLICT" not in str(e):
                        raise
                    applied = False
                    revision["applied"] = False
                    revision["reason"] = "version_conflict"
                    revision["notes"] = str(e)
                else:
                    revision["versionNo"] = res["versionNo"]
                    revision["baseVersionNo"] = res.get("baseVersionNo")
                    if notes:
                        revision["notes"] = notes
                    task_store.append_event(task_id, "tool_result", {
                        "tool": "docgen.revise_body", "draftId": draft_id,
                        "versionNo": res["versionNo"],
                        "baseVersionNo": res.get("baseVersionNo"),
                    })
            else:
                revision["reason"] = "local_mode"
            audit_text = (
                analysis.get("audit")
                or (
                    "修订稿基于旧版本，未覆盖当前稿，请基于最新稿重新修订，关键事实请以原文为准"
                    if revision.get("reason") == "version_conflict"
                    else "修订稿已按意见生成并落为新版本，关键事实请以原文为准"
                )
            )
            # 与 DocGenBody 契约对齐：修订不带提纲/槽位建议（outline 传 null 不渲染）
            cur_draft = docgen_store.get_draft(str(draft_id)) if draft_id else None
            body = {
                "docgen": {
                    "draftId": draft_id,
                    "docType": (state.get("docgen_template") or {}).get("docType"),
                    "outline": None,
                    "outlineConfirmed": bool(cur_draft and cur_draft.get("outlineConfirmed")),
                    "versionNo": (cur_draft or {}).get("currentVersionNo"),
                    "slots": [],
                    "missingFields": [],
                    "revision": revision,
                },
                "cites": [],
                "audit": audit_text,
                "degradedReason": state.get("degraded_reason"),
            }
            checkpoint = {
                "draftId": draft_id, "revision": True, "applied": applied,
                "versionNo": revision.get("versionNo"),
            }
            return body, [], 0, checkpoint

        draft_id = state.get("docgen_draft_id")
        spec, cites, needs_human = _bind_docgen_suggestions(task_id, state["case_id"], analysis, hits)
        draft: dict[str, Any] | None = None
        missing: list[str] = []
        if draft_id:
            docgen_store.update_outline(draft_id, spec["outline"])
            if spec["slots"]:
                docgen_store.fill_slots(draft_id, [
                    {
                        "name": s["name"], "value": s["value"],
                        "sourceType": "case_evidence" if s["verified"] else "pending_verify",
                        "sourceRef": s["sourceRef"],
                    }
                    for s in spec["slots"]
                ])
            draft = docgen_store.get_draft(draft_id)
            tpl_fields = [
                str(f.get("name") or "")
                for f in (state.get("docgen_template") or {}).get("fields") or []
            ]
            filled = {s["name"] for s in spec["slots"]}
            missing = [n for n in tpl_fields if n not in filled]
        slots_out = [
            {
                "name": s["name"],
                "value": s["value"],
                "sourceType": "case_evidence" if s["verified"] else "pending_verify",
                "sourceTypeLabel": docgen_store.SOURCE_TYPE_LABELS[
                    "case_evidence" if s["verified"] else "pending_verify"
                ],
                "sourceRef": s["sourceRef"],
                "claimId": s["claimId"],
            }
            for s in spec["slots"]
        ]
        body: dict[str, Any] = {
            "docgen": {
                "draftId": draft_id,
                "docType": (state.get("docgen_template") or {}).get("docType"),
                "outline": spec["outline"],
                "outlineConfirmed": bool(draft and draft["outlineConfirmed"]),
                # 版本锚点：消息卡展示的当前稿版本号（升级方案 §7.4）
                "versionNo": (draft or {}).get("currentVersionNo"),
                "slots": slots_out,
                "missingFields": missing,
            },
            "cites": cites,
            "audit": (analysis.get("audit") or "提纲与槽位建议待用户确认，缺失字段将以占位符替换，不编造")
            + (f" · {needs_human} 项待人工核实" if needs_human else ""),
            "degradedReason": state.get("degraded_reason"),
        }
        checkpoint = {
            "draftId": draft_id,
            "outline": len(spec["outline"]),
            "slots": len(spec["slots"]),
            "missing": len(missing),
            "needsHuman": needs_human,
        }
    elif state["mode"] == "legal_review" and state.get("workflow_preset") == "strategy":
        # 策略预设：有利/不利条件与方案逐项绑定 Claim（PRD 13.6）
        spec, cites, needs_human = _bind_strategy_claims(task_id, state["case_id"], analysis, hits)
        body: dict[str, Any] = {
            "strategy": spec,
            "cites": cites,
            "audit": (analysis.get("audit") or "AI 不替用户选定最终方案，最终选择由律师确认后写入审计（REQ-STR-005）")
            + (f" · {needs_human} 项待人工核实" if needs_human else ""),
            "degradedReason": state.get("degraded_reason"),
        }
        checkpoint = {
            "plans": len(spec["plans"]),
            "favorable": len(spec["favorable"]),
            "unfavorable": len(spec["unfavorable"]),
            "needsHuman": needs_human,
        }
    elif state["mode"] == "legal_review" and state.get("workflow_preset") == "evidence_analysis":
        # 证据分析预设：证据卡逐张绑定 Claim + 争点证据矩阵（PRD 13.5）
        spec, cites, needs_human = _bind_evidence_claims(task_id, state["case_id"], analysis, hits)
        body: dict[str, Any] = {
            "evidence": spec,
            "cites": cites,
            "audit": (analysis.get("audit") or "分析意见与材料记载已分开标注，关键事实请以原文为准")
            + (f" · {needs_human} 项待人工核实" if needs_human else ""),
            "degradedReason": state.get("degraded_reason"),
        }
        checkpoint = {
            "evidenceCards": len(spec["evidence"]),
            "matrixDisputes": len(spec["matrix"]["disputes"]),
            "matrixCells": len(spec["matrix"]["cells"]),
            "needsHuman": needs_human,
        }
    elif state["mode"] == "legal_review" and state.get("workflow_preset") == "visualization":
        # 可视化预设：时间线节点 / 关系图节点与边逐一绑定 Claim（REQ-VIS-001）
        viz, cites, needs_human = _bind_viz_claims(task_id, state["case_id"], analysis, hits)
        body: dict[str, Any] = {
            "viz": viz,
            "cites": cites,
            "audit": (analysis.get("audit") or "图表节点均来自案件材料，关键事实请以原文为准")
            + (f" · {needs_human} 项待人工核实" if needs_human else ""),
            "degradedReason": state.get("degraded_reason"),
        }
        checkpoint = {
            "timeline": len(viz["timeline"]["nodes"]),
            "graphNodes": len(viz["relationGraph"]["nodes"]),
            "graphEdges": len(viz["relationGraph"]["edges"]),
            "flows": len(viz.get("flows") or []),
            "needsHuman": needs_human,
        }
    elif state["mode"] == "legal_review":
        # 全规格报告：逐项绑定 Claim（DEV-AGT-008 / REQ-REV-008）
        report, cites, needs_human = _bind_report_claims(task_id, state["case_id"], analysis, hits)
        body: dict[str, Any] = {
            "report": report,
            "cites": cites,
            "audit": (analysis.get("audit") or "以上为 AI 分析意见，关键事实请以原文为准")
            + (f" · {needs_human} 项待人工核实" if needs_human else ""),
            "degradedReason": state.get("degraded_reason"),
        }
        checkpoint = {"sections": _report_section_counts(report), "cites": len(cites), "needsHuman": needs_human}
    else:
        # 精简要点结构：逐要点绑定来源并做引语校验
        points = analysis.get("points") or []
        cites = []
        needs_human = 0
        for p in points[:6]:
            source_doc = str(p.get("sourceDoc") or "").strip()
            hit = next((h for h in hits if source_doc and source_doc in h["documentTitle"]), None)
            if hit is None and hits:
                # 模型未指明来源时按序兜底绑定（保持可回原文），仍走完整校验
                fallback = hits[len(cites) % len(hits)]
                cite = judge_claim(task_id, state["case_id"],
                                   f"{p.get('title', '')}：{p.get('desc', '')}", fallback)
            else:
                cite = judge_claim(
                    task_id=task_id,
                    case_id=state["case_id"],
                    text=f"{p.get('title', '')}：{p.get('desc', '')}",
                    source_hit=hit,
                )
            if cite["claimStatus"] == "needs_human":
                needs_human += 1
            cites.append(cite)
        body = {
            "intro": analysis.get("intro") or "",
            "points": [
                {"title": str(p.get("title") or ""), "desc": str(p.get("desc") or "")}
                for p in points[:6]
            ],
            "cites": cites,
            "audit": (analysis.get("audit") or "以上为 AI 分析意见，关键事实请以原文为准")
            + (f" · {needs_human} 项待人工核实" if needs_human else ""),
            "degradedReason": state.get("degraded_reason"),
        }
        checkpoint = {"points": len(points), "cites": len(cites), "needsHuman": needs_human}

    if extra:
        body.update(extra)
    return body, cites, needs_human, checkpoint


def persist_final_outputs(
    state: AgentState, body: dict[str, Any], cites: list[dict[str, Any]]
) -> None:
    """阅卷类产物的持久化（node_verify 与阅卷 Loop 共用；失败不阻断任务完成）。

    - 产物落库（M2 收尾：产物概览/阅读视图/导出的数据源，REQ-DOC-005 基础）；
    - 案件记忆（DEV-AGT-007 / M-002）：只把带引证的条目写入案件记忆；
    - DEV-AGT-006 无进展检测（A-003）：连续三轮无新增证据与 Claim → 停止后续调用。
    """
    task_id = state["task_id"]
    _kind_title = {
        "evidence": ("evidence", "证据分析"),
        "viz": ("viz", "可视化阅卷"),
        "strategy": ("strategy", "辩护策略分析"),
        "report": ("report", "阅卷分析报告"),
        "contract": ("contract", "合同审查"),
    }
    body_key = next((k for k in ("evidence", "viz", "strategy", "report", "contract") if k in body), None)
    kind, kind_title = _kind_title.get(body_key, ("points", "分析要点"))
    case_title = _as_str(state.get("case_title")).strip()
    title = f"{case_title} · {kind_title}" if case_title else kind_title
    try:
        from ..artifacts import save_artifact

        # 版本组（交付物迭代改写）：同案件同类型同标题的产物归入同一版本组，
        # 用户在对话中要求重出/修改时，新结果即组内新版本；组内最新为当前稿
        from ..storage import get_db

        prev = get_db().query_one(
            """
            SELECT group_id FROM artifacts
            WHERE case_id = ? AND kind = ? AND title = ?
            ORDER BY version_no DESC LIMIT 1
            """,
            (state["case_id"], kind, title),
        )
        save_artifact(
            task_id, state["case_id"], kind, title, body,
            group_id=(prev["group_id"] if prev else None),
        )
    except Exception:
        # 产物落库失败不阻断任务完成（事件流中 message 已可渲染）
        pass

    # 案件记忆（DEV-AGT-007 / M-002）：只把带引证的条目写入案件记忆，无来源不入库
    try:
        from .. import memory as memory_store

        ingest = memory_store.ingest_report(state["case_id"], task_id, body)
        if ingest["written"]:
            task_store.append_event(task_id, "memory", ingest)
    except Exception:
        pass

    # DEV-AGT-006 无进展检测（A-003）：连续三轮无新增证据与 Claim → 停止后续调用
    try:
        from . import budget as budget_store

        supported_now = sum(1 for c in cites if str(c.get("claimStatus")) == "supported")
        progress = budget_store.note_progress(
            task_id, claims_added=supported_now, evidence_added=len(state.get("hits") or []),
        )
        if progress == "NO_PROGRESS":
            task_store.append_event(task_id, "budget", {
                "stage": "blocked", "scope": "no_progress",
                "reason": budget_store.blocked_reason(task_id),
            })
    except Exception:
        pass


def node_verify(state: AgentState) -> AgentState:
    task_id = state["task_id"]
    task_store.update_lifecycle(task_id, "VERIFYING", "verify")

    analysis = state.get("analysis") or {}
    hits = state["hits"]

    # 脱敏外发任务的模型输出含案件内别名：恢复原文后再绑定引证，
    # 使引语校验与材料原文一致（hits 始终是原始命中，绑定不受替换影响）
    if state.get("redaction_applied"):
        try:
            analysis = _restore_analysis(state["case_id"], analysis)
        except Exception:
            pass  # 恢复失败不阻断任务：别名文本本身不含敏感原文

    body, cites, needs_human, checkpoint = build_final_body(state, analysis, hits)
    task_store.append_event(task_id, "message", {"body": body})

    # 文书生成除外：artifacts.kind 为阅卷类 CHECK，docgen_drafts 自身即数据源
    if state["mode"] != "document_generation":
        persist_final_outputs(state, body, cites)

    task_store.save_checkpoint(task_id, "verify", checkpoint)
    return {**state, "analysis": analysis, "cites": cites}


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------

def build_agent_graph():
    """主执行图：plan → retrieve → analyze → verify（三模式共用骨架，后续迭代分叉）。"""
    g: StateGraph = StateGraph(AgentState)
    g.add_node("plan", node_plan)
    g.add_node("retrieve", node_retrieve)
    g.add_node("analyze", node_analyze)
    g.add_node("verify", node_verify)
    g.set_entry_point("plan")
    g.add_edge("plan", "retrieve")
    g.add_edge("retrieve", "analyze")
    g.add_edge("analyze", "verify")
    g.add_edge("verify", END)
    return g.compile()


# ---------------------------------------------------------------------------
# 三模式子图注册（DEV-AGT-002）
#
# 当前三模式共用同一执行骨架（检索词与模式标签不同），合同审查与文书生成
# 子图在后续迭代分叉；agent.graph_info RPC（service.py）消费本注册表，
# runner 经 build_agent_graph 拿到同一编译结果。
# ---------------------------------------------------------------------------

MODE_GRAPHS: dict[str, dict[str, Any]] = {
    "legal_review": {"label": "法律阅卷", "defaultKeywords": _MODE_KEYWORDS["legal_review"]},
    "contract_review": {"label": "合同审查", "defaultKeywords": _MODE_KEYWORDS["contract_review"]},
    "document_generation": {"label": "文书生成", "defaultKeywords": _MODE_KEYWORDS["document_generation"]},
}

_GRAPH_NODES = ("plan", "retrieve", "analyze", "verify")
_GRAPH_EDGES = ("plan→retrieve", "retrieve→analyze", "analyze→verify", "verify→END")


class ModeGraph:
    """模式图包装：info() 供 agent.graph_info 消费；invoke 委托编译后的图。"""

    def __init__(self, mode: str, compiled: Any) -> None:
        self.mode = mode
        self._compiled = compiled

    def info(self) -> dict[str, Any]:
        meta = MODE_GRAPHS[self.mode]
        return {
            "mode": self.mode,
            "label": meta["label"],
            "nodes": list(_GRAPH_NODES),
            "edges": list(_GRAPH_EDGES),
            "sharedSkeleton": True,  # 三模式共用骨架（子图分叉前的过渡态）
            "defaultKeywords": list(meta["defaultKeywords"]),
            "workflowPresets": list(WORKFLOW_PRESETS) if self.mode == "legal_review" else [],
        }

    def invoke(self, state: AgentState) -> Any:
        return self._compiled.invoke(state)


def build_graph(mode: str) -> ModeGraph:
    """按模式构建执行图；模式非法时抛 ValueError（service.py agent.graph_info）。"""
    if mode not in MODE_GRAPHS:
        raise ValueError(f"无效模式：{mode}")
    return ModeGraph(mode, build_agent_graph())
