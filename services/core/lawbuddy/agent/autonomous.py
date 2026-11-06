"""自主 Agent Loop：模型驱动的工具调用循环（取代固定六节点阅卷图）。

设计原则：
- 模型拥有完全自主权——自主决定是否调用工具、调用哪个、几次、什么顺序；
  简单问题直接回答，复杂任务自行拆解推进；工具报错自行调整；
- 唯一硬限制是 MAX_ROUNDS（最大对话轮数）与 MAX_TOOL_CALLS（单次任务累计
  工具调用次数），不做脱敏、预算、审批、人工介入等任何流程闸门，把决策权
  交还模型本身；
- 事件协议核心 6 种（UI 纯事件驱动渲染）：
    text                  文本增量（流式）
    tool_use_start        即将执行某工具
    tool_use_done         工具执行结束（ok / error）
    assistant_message     模型本轮完整输出（含 toolCalls 明细）
    tool_result_message   工具结果消息
    turn_complete         整个任务循环结束
  另有信息性事件：tool_use_progress（大参数生成进度）、compaction（历史
  压缩统计）、plan_update（会话级任务清单更新）——不影响渲染主流程；
- 每轮落一个检查点（messages 序列），崩溃 / 取消后可从断点续跑；
- 上下文窗口管理：历史超阈值自动压缩早期轮次为摘要（确定性，不调模型），
  工具按模式裁剪下发，任务清单（task_plan）每轮注入 system prompt；
- 轮数用尽（max_rounds）仍无终答时追发一次无工具调用强制作答，保证
  用户拿到非空结论；
- 案件边界（caseId）由 Core 注入工具实现，模型无法指定——这是功能
  正确性（跨案召回是 bug），不是权限闸门。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import model_gateway, tasks as task_store

# 循环硬限制之一：最大对话轮数（每轮 = 一次模型调用 + 若干工具执行）。
# 固定值，不按任务深度/语料规模动态调整——避免同类任务因提示词措辞不同
# （如「搜索类案信息」未被识别为 full_review）拿到不同轮数预算。
MAX_ROUNDS = 200
# 循环硬限制之二：单次任务累计工具调用次数（跨轮累加，单个 tool_call 计一次）。
# 轮数与工具调用数是两个独立维度——一轮可并发多次工具调用，只卡轮数会让
# 少数轮次堆积大量调用；只卡调用数则会让模型空转轮数。两者同时生效。
MAX_TOOL_CALLS = 500

# 传输优化参数（不影响模型自主性）
TEXT_FLUSH_INTERVAL_S = 0.25          # text 增量节流推送间隔
PAGE_TEXT_LIMIT = 2400                # 读原文时单页单次回灌上限（字符），超出部分用游标续读
TOTAL_PAGE_READ_CHARS = 6000          # 单次 document_get_pages 原文总量上限，须低于工具结果回灌上限
_PAGE_ENTRY_OVERHEAD = 120            # 单页条目的元数据（页码/质量/游标字段）预留字符
TOOL_RESULT_CHAR_LIMIT = 8000         # 单个工具结果回灌上下文的上限（字符）
MAX_RISK_ITEMS = 40                   # 单次审查落库的风险项上限（与旧管线 MAX_CONTRACT_CLAIMS 一致）

# 检索子代理（case_explore，Trae Search Agent 对应物）：按问题定向查证，
# 主上下文只收聚合结论不收中间检索结果。硬护栏：低思考档 + 轮数上限 +
# 结论截断——与已移除的整卷通读策略（一次性全案重操作）完全不同
CASE_EXPLORE_MAX_ROUNDS = 8
CASE_EXPLORE_CONCLUSION_CHARS = 4000
CASE_EXPLORE_READ_CHARS = 3000        # 子代理单次读原文上限，低于其工具结果回灌上限 4000

# bash 超时熔断：同一命令连续超时达到该次数后，本次任务内不再放行执行，
# 强制模型更换策略（防止反复重试同一卡死命令空转烧轮数）
BASH_TIMEOUT_STREAK_LIMIT = 2

# 完成守门（05 方案 §7）：模型停止调用工具只表示"可以提交草稿"，是否算干净
# 完成由守门判定。修复预算独立于轮数预算，防止守门与模型之间无限循环。
FINALIZE_REPAIR_ROUNDS = 2            # 最多两轮守门修复（每轮复用一次正常循环轮次）
FINALIZE_COVERAGE_REPAIR_PAGES = 6    # 覆盖缺口少于此页数时才值得发起精准补读
FINALIZE_MAX_REPAIRS = 8              # 单次守门最多下发的修复项数（超出部分直接进警告）

# 阅读账本条目上限（大卷宗只保最近读取状态，防止 checkpoint 无限膨胀）
READING_LEDGER_MAX_ENTRIES = 600

# 证据/争点账本（05 方案 §5.5 §5.6）：跨轮次、跨压缩、跨子代理的精确证据状态。
# 只保存必要字段与增量，不把原文全文复制进每轮 Prompt；摘要按需注入（§6.4）。
EVIDENCE_LEDGER_MAX_ENTRIES = 240
ISSUE_LEDGER_MAX_ENTRIES = 40
LEDGER_SUMMARY_CHARS = 900           # 每轮注入的状态摘要上限（动态段保持小）
# 一次任务内所有检索子代理共享的模型轮次预算（§6.5 共享总预算）
EXPLORE_TOTAL_ROUND_BUDGET = 16

# 沙箱工作空间工具名集合（实现见 sandbox.py；bash 需用户逐次审批）
_SANDBOX_TOOLS = {"read_file", "write_file", "edit_file", "glob_files", "grep_search", "bash", "render_html", "render_pptx"}
# 沙箱工具按需暴露（§6.6 D2）：文件/命令类只在用户确实要求操作工作空间或运行脚本时下发，
# 渲染类只在本次交付要点名出图/演示时下发——普通阅卷不承担这些无关工具的固定 token 开销。
_WORKSPACE_TOOLS = {"read_file", "write_file", "edit_file", "glob_files", "grep_search", "bash"}
_RENDER_TOOLS = {"render_html", "render_pptx"}

# ---------------------------------------------------------------------------
# 上下文窗口管理：历史压缩 + 工具裁剪 + 会话级任务清单（harness 优化三件套）
# ---------------------------------------------------------------------------

# 历史压缩触发阈值（字符，中文 ≈1.2 token/字）：≈10.8 万 token。历史消息
# 无限累积曾使 32 轮后期每轮 prompt 达十几万 token（TTFT 实测 >30s）；
# 超过阈值即把早期轮次压缩为一条摘要消息，system + 最近轮次原样保留
CONTEXT_COMPACT_THRESHOLD_CHARS = 90_000
# 压缩时原样保留的最近消息条数（从尾部向前取，起点回退到 assistant 消息
# 以保持 tool 消息与其 tool_calls 父消息的配对完整）
CONTEXT_KEEP_RECENT_MSGS = 8

# 任务清单注入 system prompt 的分隔标记（续跑时据此剥离旧清单段还原基础提示）
_PLAN_MARK = "\n\n【当前任务清单】"
# 任务状态摘要（任务契约/阅读覆盖/证据与争点缺口）附加在清单之后，
# 同样每轮重建；续跑时一并剥离，避免把旧状态当成当前状态（§6.4）
_STATE_MARK = "\n\n【当前任务状态】"

# 工具按模式裁剪（ToolSearch 的最小实现）：无关模式的工具不下发，每轮固定
# token 开销显著下降；模式未登记时不裁剪（新模式默认全量，行为保守）
_MODE_TOOLS: dict[str, set[str]] = {
    "legal_review": {
        "documents_list", "document_search", "document_get_pages",
        "memory_search", "memory_write", "case_explore", "case_ledger",
    },
    "contract_review": {
        "documents_list", "document_search", "document_get_pages",
        "memory_search", "memory_write",
        "contract_create_review", "contract_split_clauses", "contract_get_clauses",
    },
    "document_generation": {
        "documents_list", "document_search", "document_get_pages",
        "memory_search", "memory_write", "case_explore", "case_ledger",
        "docgen_list_templates", "docgen_create_draft",
        "docgen_render_draft",
        "docgen_get_draft", "docgen_revise_draft",
        "docgen_create_free_draft",
        "request_user_decision",
    },
}
# 各模式通用：任务清单与「已安装技能」只读列举。
# 列举类工具不改变任何状态、Schema 极小，但缺了它模型无法自查本机能力，
# 容易把「列表为空」误答成「没有该能力」；安装/卸载技能仍在 _INSTALL_TOOLS 里按关键词下发。
_COMMON_TOOLS = {"task_plan", "skill_list"}
# 安装/管理类一次性流程工具：仅在用户请求明确涉及时下发
_INSTALL_TOOLS = {
    "skill_install", "skill_list", "skill_remove",
}
# 安装/管理类判据：用户说「技能 / skill / 装一个」时下发技能管理工具
_INSTALL_KEYWORDS = ("技能", "skill", "Skill")
# 交付形态判据（§6.6 第 3 条）：只有请求或预设真的要求出图/演示时才暴露渲染工具
_RENDER_KEYWORDS = ("图表", "可视化", "关系图", "时间轴", "流程图", "示意图", "思维导图",
                    "ppt", "PPT", "幻灯片", "演示", "html", "HTML", "网页")
# 工作空间操作判据：用户明确要落盘/跑脚本/装东西时才暴露文件与命令工具
_WORKSPACE_KEYWORDS = ("工作空间", "本地文件", "写成文件", "保存到文件", "导出文件",
                       "脚本", "命令行", "CLI", "安装")
# 文件交付判据：用户要 Word/Excel/PPT 等成品文件时同样需要写文件与命令工具。
# 阅卷/合同模式下没有 docgen 通道，产出 .docx/.xlsx/.pptx 只能靠 write_file
# 与 bash（python-docx / openpyxl / python-pptx 已装在 venv），因此按交付意图放行。
# 关键词按「实际出现的交付词」收：泛词（文件、文档、生成）会把所有分析请求都命中，
# 失去按需下发的意义；这里只收能明确指向落盘产物的说法。
_FILE_DELIVERABLE_KEYWORDS = (
    # 办公格式
    "word", "docx", "excel", "xlsx", "pptx", "csv", "电子表格", "表格文件",
    # 文本/网页格式
    "markdown", ".md", "txt", "文本文件", "pdf", "html", "网页",
    # 落盘动作
    "导出", "另存", "保存为", "保存成", "写成文件", "生成文件", "落盘", "下载",
    # 脚本执行（跑数据处理、算页码、批量重命名等）
    "python", "pandas", "openpyxl", "脚本", "运行代码",
    # 文书类交付（阅卷模式下靠沙箱工具落盘）
    "起草", "草拟", "撰写", "写一份", "拟一份", "生成一份", "写个",
    "文书", "起诉状", "答辩状", "上诉状", "申请书", "意见书",
    "律师函", "代理词", "辩护词",
)


def _wants_render(state: dict[str, Any]) -> bool:
    """本次任务是否需要可视化成图（预设可视化 / 请求点名图表或演示）。"""
    if str(state.get("workflow_preset") or "") == "visualization":
        return True
    if state.get("skill_details"):
        # 绑定技能时技能正文可能要求 HTML/图表展示，按技能执行要求保留
        return True
    request = str(state.get("request") or "")
    return any(k in request for k in _RENDER_KEYWORDS)


def _wants_workspace(state: dict[str, Any]) -> bool:
    """本次任务是否需要本地文件/命令工具。

    判据：请求点名操作工作空间、跑脚本、安装，或要求交付 Word/Excel/PPT
    等成品文件（阅卷与合同模式下没有 docgen 通道，文件类交付必须靠沙箱工具）。
    """
    if state.get("skill_details"):
        return True
    request = str(state.get("request") or "")
    lowered = request.lower()
    return any(k in request for k in _WORKSPACE_KEYWORDS) \
        or any(k in request for k in _INSTALL_KEYWORDS) \
        or any(k in lowered for k in _FILE_DELIVERABLE_KEYWORDS)


def _wants_presentation(state: dict[str, Any]) -> bool:
    """本次任务是否需要呈现格式规则（表格优先）。

    只在分析型任务注入（§6.6 D1 第 2 条）：带预设、限定材料或请求本身是
    争点/证据分析的 focused、full_review；单点事实问答（direct）不承担模板负担。
    """
    if str(state.get("mode") or "") != "legal_review":
        return False
    if state.get("workflow_preset"):
        return True
    contract = state.get("task_contract") or _derive_task_contract(state)
    return str(contract.get("taskDepth") or "") in ("focused", "full_review")


def _exposed_tool_names(state: dict[str, Any]) -> set[str] | None:
    """本次任务实际下发的工具名集合；None 表示模式未登记、全量下发。"""
    allow = _MODE_TOOLS.get(str(state.get("mode") or ""))
    if allow is None:
        return None
    names = set(allow) | _COMMON_TOOLS
    if _wants_workspace(state):
        names |= _WORKSPACE_TOOLS
    if _wants_render(state):
        names |= _RENDER_TOOLS
    if (state.get("contract_input") or {}).get("workflowKind") == "contract_dialogue":
        # Dialogue may inspect sources, but cannot overwrite confirmed workflow artifacts.
        names = names - {"contract_create_review", "contract_split_clauses", "memory_write",
                         "write_file", "edit_file", "bash"}
    if any(k in str(state.get("request") or "") for k in _INSTALL_KEYWORDS):
        names = names | _INSTALL_TOOLS
    return names


def _tools_for(state: dict[str, Any]) -> list[dict[str, Any]]:
    """按任务与交付需求下发工具子集（§6.6 D2）。

    命中已知模式时只保留该模式相关 + 通用工具；沙箱文件/命令工具与渲染工具
    按本次请求的实际需要追加；用户请求涉及安装流程（MCP/技能）时补充安装类工具。
    未登记模式全量下发（行为保守）。
    """
    specs = _tool_specs()
    names = _exposed_tool_names(state)
    if names is None:
        return specs
    return [s for s in specs if s["function"]["name"] in names]


# 推理档位分阶段（§6.6 第 5 条 / §10.4 D3）：同一任务不同阶段的思考强度应当不同。
# 读取/检索轮只是在原文中定位与摘录，综合轮要把材料组织成结论，修复轮要在原文里
# 精确纠正失败引用——把三者统一按用户设置的最高档跑，等于每一轮都付综合成本。
_STAGE_EXTRACT = "extract"      # 提取/通读：本轮只有检索与读页
_STAGE_SYNTHESIS = "synthesis"  # 综合：已积累材料，本轮产出结论或交付物
_STAGE_REPAIR = "repair"        # 守门修复：只针对被点名的缺口做局部补正
# 读取类工具：其结果回灌后的一轮属"消化原文"，可按提取阶段降档；
# 检索/清单/记忆检索结果之后的一轮通常是结论轮，不降档（见 _turn_stage）
_READ_TOOL_NAMES = {"document_get_pages", "case_explore"}


def _trailing_tool_names(messages: list[dict[str, Any]]) -> set[str]:
    """尾部连续工具结果消息对应的工具名（按 tool_call_id 回到 assistant 消息查）。"""
    if not messages or str(messages[-1].get("role") or "") != "tool":
        return set()
    tail: list[dict[str, Any]] = []
    for msg in reversed(messages):
        if str(msg.get("role") or "") != "tool":
            break
        tail.append(msg)
    ids = {str(m.get("tool_call_id") or "") for m in tail}
    for msg in reversed(messages[: len(messages) - len(tail)]):
        if str(msg.get("role") or "") != "assistant":
            continue
        mapping = {
            str(tc.get("id") or ""): str((tc.get("function") or {}).get("name") or "")
            for tc in msg.get("tool_calls") or []
        }
        return {mapping[i] for i in ids if i in mapping}
    return set()


def _turn_stage(messages: list[dict[str, Any]], repair_active: bool) -> str:
    """判定本轮所处阶段（确定性，只看已发生的事，不预判模型意图）。

    - 守门修复期：修复（最高档）；
    - 上一轮的工具结果全部来自读原文（document_get_pages 等）→ 提取（最低档）：
      这一轮模型是在消化刚取回的正文，通常接着读下一段或摘录，不承担法律综合；
    - 其余（首轮、检索/清单结果之后、模型上一轮已产出文本）→ 综合（用户设定档）。

    只看读取类工具而不看检索结果，是为了不让"检索完直接作答"的那一轮降档——
    那正是多数 direct/focused 任务的结论轮，降档等于直接削弱交付质量。
    """
    if repair_active:
        return _STAGE_REPAIR
    names = _trailing_tool_names(messages)
    if names and names <= _READ_TOOL_NAMES:
        return _STAGE_EXTRACT
    return _STAGE_SYNTHESIS


def _stage_effort(
    stage: str, base: str | None, model_id: str, *, downgraded: bool = False,
) -> str | None:
    """按阶段收敛思考档位（模型能力优先：不接受档位的模型一律 None）。

    - 提取：取模型支持的最低档——读页与摘录不需要与法律综合同等的推理；
    - 综合：沿用用户设置档（未设置时为模型默认档）；
    - 修复：取模型支持的最高档——需要把失败引用重新对上原文；
    - downgraded=True（思考失控/空回答后的降级重试）恒取最低档，覆盖阶段策略。
    """
    if downgraded:
        return model_gateway.weakest_reasoning_effort(model_id)
    if stage == _STAGE_EXTRACT:
        return model_gateway.weakest_reasoning_effort(model_id)
    if stage == _STAGE_REPAIR:
        return model_gateway.strongest_reasoning_effort(model_id) or base
    return base


def _messages_chars(messages: list[dict[str, Any]]) -> int:
    """估算消息序列字符量（content + tool_calls 参数；用量口径 ≈1.2 token/字）。"""
    total = 0
    for m in messages:
        total += len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            total += len(str((tc.get("function") or {}).get("arguments") or ""))
    return total


def _compact_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """把早期轮次压缩为一条摘要消息（确定性，不额外调用模型）。

    - 保留 messages[0]（system，含任务清单段）与最近 CONTEXT_KEEP_RECENT_MSGS 条；
      尾部起点向前回退到 assistant 消息，保证 tool 消息与 tool_calls 配对不破坏；
    - 中间段逐条摘录：模型文本 600 字 / 工具调用名+参数摘要 / 工具结果 280 字，
      关键事实不依赖摘要（memory_search 检索与原文工具查证才是事实依据，摘要仅供导航）；
    - 返回 (压缩后序列, 统计)；压缩失败风险为零（纯字符串操作）。
    """
    start = max(1, len(messages) - CONTEXT_KEEP_RECENT_MSGS)
    while start > 1 and messages[start].get("role") != "assistant":
        start -= 1
    if start <= 1:
        return messages, {"before": _messages_chars(messages), "after": _messages_chars(messages),
                          "compactedMessages": 0}
    lines: list[str] = []
    for m in messages[1:start]:
        role = m.get("role")
        text = str(m.get("content") or "").strip()
        if role == "user":
            lines.append(f"[用户] {_clip(text, 400)}")
        elif role == "assistant":
            if text:
                lines.append(f"[模型] {_clip(text, 600)}")
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                lines.append(f"[调用] {fn.get('name')}({_clip(str(fn.get('arguments') or ''), 160)})")
        elif role == "tool":
            lines.append(f"[结果] {_clip(text, 280)}")
    digest = (
        "（系统提示：以下为此前轮次的压缩摘要，非用户新发言；关键案件事实与结论"
        "以 memory_search 记忆检索与已标注的原文出处为准，无需重新查证；"
        "当前任务深度、阅读覆盖、已回查证据与未解决争点见 system 提示末段的状态摘要，"
        "精确原文仍按需用 document_get_pages 读取）\n" + "\n".join(lines)
    )
    compacted = [messages[0], {"role": "user", "content": digest}, *messages[start:]]
    return compacted, {"before": _messages_chars(messages), "after": _messages_chars(compacted),
                       "compactedMessages": start - 1}


_PLAN_DONE_VALUES = {"done", "completed", "complete", "finished", "已完成", "完成", "已完成。"}


def _plan_is_done(status: Any) -> bool:
    """步骤是否已完成（兼容模型输出的中英文同义词）。"""
    return str(status or "").strip().lower() in _PLAN_DONE_VALUES


def _plan_section(plan: list[dict[str, Any]] | None) -> str:
    """任务清单渲染为 system prompt 注入段（每轮重建，反映最新进度）。"""
    if not plan:
        return ""
    lines = [
        f"{i + 1}. {'[已完成] ' if _plan_is_done(s.get('status')) else ''}{s.get('text')}"
        for i, s in enumerate(plan[:12]) if str(s.get("text") or "").strip()
    ]
    return _PLAN_MARK + "（用 task_plan 工具维护，完成一步更新一步）\n" + "\n".join(lines)


def _close_out_plan(state: dict[str, Any], emit: Callable[..., None]) -> None:
    """收尾清单兜底：交付最终答案后，把仍未显式标记 done 的步骤按 done 收口。

    任务清单由模型自行维护（task_plan），实测交付后常忘记最后更新清单，UI 停在
    「0/6 个任务已完成」。最终答案已产出即本次任务已收束，剩余步骤补齐为 done，
    与用户看到的「已交付」一致；仅在确有最终答案时执行，未作答/轮数耗尽的强制
    作答轮保留真实缺口，不伪造完成。
    """
    plan = state.get("task_plan")
    if not isinstance(plan, list) or not plan:
        return
    steps = [s for s in plan if isinstance(s, dict)]
    if not steps or all(_plan_is_done(s.get("status")) for s in steps):
        return
    closed = [{**s, "status": "done"} for s in steps]
    state["task_plan"] = closed
    emit("plan_update", {"steps": closed})


def _strip_plan(content: str) -> str:
    """剥离旧任务清单/状态摘要段，还原基础 system prompt（续跑/重建时使用）。"""
    for mark in (_PLAN_MARK, _STATE_MARK):
        idx = content.find(mark)
        if idx >= 0:
            content = content[:idx]
    return content


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"…（截断，共 {len(text)} 字符）"


def _int_arg(args: dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(args.get(key))
    except (TypeError, ValueError):
        return default
    return value


def _paginate_pages(
    document_id: str,
    pages: list[dict[str, Any]],
    *,
    total_pages: int,
    char_offset: int = 0,
    max_chars: int = PAGE_TEXT_LIMIT,
) -> dict[str, Any]:
    """按字符游标无损分页返回原文（05 方案 §6.2）。

    旧实现给每页文本套 `_clip(PAGE_TEXT_LIMIT)`，超出部分直接丢弃并只回一句
    “已截断”——模型无从得知被截掉的内容，且无法续读，属于静默丢证据。
    现在：起始页从 charOffset 起读，每页最多 maxChars，总量不超过 max_chars；
    未读完时给出 nextCursor（页码 + 该页字符偏移），模型可原样续读。
    """
    remaining = max_chars
    out: list[dict[str, Any]] = []
    cursor: dict[str, int] | None = None
    empty_pages = 0
    returned = 0
    for idx, page in enumerate(pages):
        text = str(page.get("text") or "")
        page_no = int(page.get("pageNo") or 0)
        if not text:
            # 空页/解析失败页必须可见：否则模型会把“没有可读文本”误当成“该页不存在”
            if remaining < _PAGE_ENTRY_OVERHEAD:
                cursor = {"pageNo": page_no, "charOffset": 0}
                break
            out.append({
                "pageNo": page.get("pageNo"),
                "quality": page.get("quality"),
                "printedPage": page.get("printedPage"),
                "text": "",
                "textStart": 0,
                "textTotal": 0,
                "truncated": False,
                "empty": True,
            })
            remaining -= _PAGE_ENTRY_OVERHEAD
            empty_pages += 1
            continue
        start = min(char_offset, len(text)) if idx == 0 else 0
        if start >= len(text):
            # 起始页已读完（charOffset 越界）：跳到下一页继续
            continue
        avail = remaining - _PAGE_ENTRY_OVERHEAD
        if avail <= 0:
            cursor = {"pageNo": page_no, "charOffset": start}
            break
        take = min(len(text) - start, avail)
        ended = start + take < len(text)
        out.append({
            "pageNo": page.get("pageNo"),
            "quality": page.get("quality"),
            "printedPage": page.get("printedPage"),
            "text": text[start:start + take],
            "textStart": start,
            "textTotal": len(text),
            "truncated": ended,
        })
        remaining -= _PAGE_ENTRY_OVERHEAD + take
        returned += take
        if ended:
            cursor = {"pageNo": page_no, "charOffset": start + take}
            break
    result: dict[str, Any] = {
        "documentId": document_id,
        "totalPages": total_pages,
        "pages": out,
        "returnedChars": returned,
        "hasMore": cursor is not None,
    }
    if not out:
        result["error"] = (
            f"未读到内容：起始页的 charOffset 已超出该页长度，或页码范围无文本。"
            f"本次请求范围 {pages[0].get('pageNo')}-{pages[-1].get('pageNo')}，"
            "请从该页 charOffset=0 或下一页重新读取"
        )
        result["hasMore"] = False
        return result
    notes: list[str] = []
    if cursor is not None:
        result["nextCursor"] = cursor
        notes.append(
            "原文未读完：带上 nextCursor.pageNo 作为 pageFrom、nextCursor.charOffset 作为 charOffset "
            "继续读取，直到 hasMore=false；不要凭已读片段推断未读内容。"
        )
    if empty_pages:
        notes.append(
            f"其中 {empty_pages} 页标记 empty：该页没有可读文本（空白页、图片页、解析失败或 OCR 未完成），"
            "不得据此断言该页内容，需要时应请用户补充材料或核对原件。"
        )
    if notes:
        result["note"] = "".join(notes)
    return result


# ---------------------------------------------------------------------------
# 任务契约 / 阅读账本 / 交付账本 / 完成守门（05 方案 §5.3 §5.4 §5.7 §7）
# ---------------------------------------------------------------------------

# 任务深度判据：用户明确限定范围优先，其次工作流预设，最后按请求关键词保守推断。
# 关键词只覆盖意图明确的说法，其余一律按 direct 处理——守门宁可少拦，不可把
# 简单问答强行扩展成全卷阅卷（§8.1 默认避免的额外成本）。
_FULL_REVIEW_KEYWORDS = ("全卷", "全案", "整卷", "全部材料", "系统梳理", "全面阅卷", "通读全案")
_FOCUSED_KEYWORDS = ("争点", "争议焦点", "证据分析", "证据链", "策略", "分析")

# 深度 → 交付与完成要求（§5.3 completionRequirements / §5.4 覆盖策略）
_DEPTH_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "direct": {"citation": "required", "coverage": "sources_verified"},
    "focused": {"citation": "required", "coverage": "issue_materials"},
    "full_review": {"citation": "required", "coverage": "all_pages"},
}


def _derive_task_contract(state: dict[str, Any]) -> dict[str, Any]:
    """在 Loop 开始时形成轻量任务契约（§5.3）：只推导，不额外调用模型。"""
    mode = str(state.get("mode") or "")
    preset = str(state.get("workflow_preset") or "")
    request = str(state.get("request") or "").strip()
    selected = [str(d) for d in (state.get("selected_document_ids") or []) if str(d)]
    if mode in ("contract_review", "document_generation"):
        depth = "focused"
    elif preset:
        depth = "full_review"
    elif selected:
        depth = "focused"
    elif any(k in request for k in _FULL_REVIEW_KEYWORDS):
        depth = "full_review"
    elif any(k in request for k in _FOCUSED_KEYWORDS):
        depth = "focused"
    else:
        depth = "direct"
    requirements = _DEPTH_REQUIREMENTS.get(depth, _DEPTH_REQUIREMENTS["direct"])
    return {
        "objective": request,
        "taskDepth": depth,
        "mode": mode,
        "workflowPreset": preset or None,
        "corpusScope": {
            # 用户显式选定材料时不擅自扩大范围（§5.3）
            "documentIds": selected,
            "allMaterials": not selected,
        },
        "citationPolicy": requirements["citation"],
        "completionRequirements": {
            "citation": requirements["citation"],
            "coverage": requirements["coverage"],
        },
        "budget": {"maxRounds": MAX_ROUNDS, "toolCalls": MAX_TOOL_CALLS,
                   "repairRounds": FINALIZE_REPAIR_ROUNDS},
    }


def _record_reading(
    state: dict[str, Any],
    document_id: str,
    version_id: str | None,
    result: dict[str, Any],
) -> None:
    """把一次 document_get_pages 的实际返回写入阅读账本（§5.4）。

    只记录"系统确实向模型提供了什么"：按页累计已返回字符区间，读完才
    read_complete；空页记 unreadable（不可断言其内容），不因调用过工具就算读完。
    """
    with _LEDGER_LOCK:
        _record_reading_locked(state, document_id, version_id, result)


def _record_reading_locked(
    state: dict[str, Any],
    document_id: str,
    version_id: str | None,
    result: dict[str, Any],
) -> None:
    ledger: dict[str, Any] = state.setdefault("reading_ledger", {})
    round_no = int(state.get("_round") or 0)
    for page in result.get("pages") or []:
        if not isinstance(page, dict):
            continue
        page_no = int(page.get("pageNo") or 0)
        if page_no <= 0:
            continue
        key = f"{document_id}:{page_no}"
        total = int(page.get("textTotal") or 0)
        prev = ledger.get(key) or {}
        entry = {
            "documentId": document_id,
            "versionId": version_id or prev.get("versionId"),
            "pageNo": page_no,
            "textTotal": total,
            "quality": page.get("quality"),
            "round": round_no,
        }
        if not total:
            entry["state"] = "unreadable"
            entry["coveredEnd"] = 0
        else:
            # 累计已覆盖区间：同页多次续读（不同 charOffset）取并集右端
            covered_end = max(
                int(prev.get("coveredEnd") or 0),
                int(page.get("textStart") or 0) + len(str(page.get("text") or "")),
            )
            entry["coveredEnd"] = min(covered_end, total)
            done = covered_end >= total and not page.get("truncated")
            entry["state"] = "read_complete" if done else "read_partial"
        ledger[key] = entry
    if len(ledger) > READING_LEDGER_MAX_ENTRIES:
        for stale_key in list(ledger)[: len(ledger) - READING_LEDGER_MAX_ENTRIES]:
            ledger.pop(stale_key, None)


def _reading_coverage(state: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """按任务深度评估阅读覆盖（§5.4）：只有 full_review 要求范围内全部页面有终态。"""
    depth = str(contract.get("taskDepth") or "")
    if depth != "full_review":
        return {"required": False, "totalPages": 0, "readPages": 0,
                "excluded": 0, "unread": []}
    from .. import documents

    case_id = str(state.get("case_id") or "")
    docs = documents.list_task_documents(case_id)
    scope = [str(d) for d in ((contract.get("corpusScope") or {}).get("documentIds") or [])]
    if scope:
        docs = [d for d in docs if str(d.get("id")) in scope]
    exclusions = _user_exclusions(state, docs)
    with _LEDGER_LOCK:
        ledger: dict[str, Any] = dict(state.get("reading_ledger") or {})
    total = 0
    read = 0
    excluded_pages = 0
    unread: list[dict[str, Any]] = []
    for doc in docs:
        page_count = int(doc.get("pageCount") or 0)
        doc_id = str(doc.get("id"))
        total += page_count
        for page_no in range(1, page_count + 1):
            key = f"{doc_id}:{page_no}"
            entry = ledger.get(key) or {}
            reason = exclusions.get(key)
            if reason and entry.get("state") not in ("read_complete", "unreadable"):
                # 用户明确排除的页：记 excluded 终态并保留理由（§5.4），不伪装成已读
                entry = {**entry, "documentId": doc_id, "pageNo": page_no,
                         "state": "excluded", "excludeReason": reason}
            # 终态：读完（read_complete）、确认不可读（unreadable）或用户明确排除（excluded）；
            # 部分读取不算终态，否则"读过一点"会被当成"已覆盖"，正是账本要避免的误判（§5.4）
            if entry.get("state") in ("read_complete", "unreadable"):
                read += 1
            elif entry.get("state") == "excluded":
                excluded_pages += 1
            elif len(unread) < 40:
                unread.append({
                    "documentId": doc_id, "title": doc.get("title") or "文档",
                    "pageNo": page_no, "partial": entry.get("state") == "read_partial",
                })
    return {"required": True, "totalPages": total, "readPages": read,
            "excluded": excluded_pages, "unread": unread}


# 用户明确排除的表述（§5.4）：只有请求里出现这些词且紧邻某份材料标题时才生效，
# 避免把模型自己的"不必读"或泛泛表述当成用户排除。
_EXCLUDE_KEYWORDS = ("不用读", "不必读", "不需要读", "无需阅读", "无须阅读", "不用看",
                     "不予考虑", "排除", "跳过", "略过")
_EXCLUDE_CLAUSE_SPLIT = "。；;\n，,、"


def _user_exclusions(state: dict[str, Any], docs: list[dict[str, Any]]) -> dict[str, str]:
    """从用户请求中解析"明确排除的材料/页面"，返回 {documentId:pageNo: 理由}。

    保守解析：排除词必须与材料标题出现在同一子句内；子句带「第N页」时只排除该页，
    否则排除该文档全部页面。若排除会覆盖本次范围的全部材料，视为用户表述不清或
    任务不成立，直接忽略——不静默产出"零阅读的完整阅卷"。
    """
    request = str(state.get("request") or "")
    if not request or not any(k in request for k in _EXCLUDE_KEYWORDS):
        return {}
    clauses = [c for c in _re_split(request) if c.strip()]
    out: dict[str, str] = {}
    touched: set[str] = set()
    for clause in clauses:
        if not any(k in clause for k in _EXCLUDE_KEYWORDS):
            continue
        for doc in docs:
            title = str(doc.get("title") or "")
            if not title or title not in clause:
                continue
            doc_id = str(doc.get("id"))
            page_count = int(doc.get("pageCount") or 0)
            pages = _page_numbers_in(clause, page_count)
            reason = _clip(clause.strip(), 60)
            touched.add(doc_id)
            if pages:
                for page_no in pages:
                    out[f"{doc_id}:{page_no}"] = reason
            else:
                for page_no in range(1, page_count + 1):
                    out[f"{doc_id}:{page_no}"] = reason
    if docs and len(touched) >= len(docs):
        # 全部材料都被"排除"：拒绝按此完成，仍要求覆盖，避免零阅读的干净完成
        return {}
    return out


def _re_split(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch in _EXCLUDE_CLAUSE_SPLIT:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _page_numbers_in(clause: str, page_count: int) -> list[int]:
    """子句中的「第N页」「第N至M页」，限制在文档实际页数内。"""
    import re

    out: list[int] = []
    for m in re.finditer(r"第\s*(\d+)\s*(?:[-~至到]\s*(\d+)\s*)?页", clause):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        for n in range(min(start, end), max(start, end) + 1):
            if 1 <= n <= page_count and n not in out:
                out.append(n)
    return out


# ---------------------------------------------------------------------------
# 证据账本 / 争点账本（05 方案 §5.5 §5.6）：跨轮次、跨压缩、跨子代理的精确状态
# ---------------------------------------------------------------------------

LEDGER_SCHEMA_VERSION = 1

# 账本写入锁：并行子代理会通过 _execute_tool → _record_reading 写同一份阅读账本，
# 主 Agent 同时可能写证据/争点账本。账本是"读-改-写"，必须串行化（RPC 可重入，
# 主循环内嵌套调用安全）。
_LEDGER_LOCK = threading.RLock()

# 五层信息（与认知内核一致）：文书直接记载 / 主体主张陈述 / 证据支持事实 / 分析推断 / 法律评价
_EVIDENCE_LAYERS = ("record", "assertion", "supported_fact", "inference", "legal")
_EVIDENCE_DIRECTIONS = ("supports", "opposes", "background")
_ISSUE_STATUSES = ("open", "ready_to_synthesize", "needs_human")


# ---------------------------------------------------------------------------
# 效率指标（05 方案 §4.2 / §10.4 D4）：重复读取、无效检索、子 Agent 采用率
# ---------------------------------------------------------------------------

def _efficiency(state: dict[str, Any]) -> dict[str, Any]:
    """任务内效率计数器（只在内存累计，不进检查点：它度量的是本次运行行为）。"""
    with _LEDGER_LOCK:
        eff = state.get("_efficiency")
        if not isinstance(eff, dict):
            eff = {
                "toolCalls": {},
                "readsByOrigin": {},
                "repeatedReads": 0,
                "repeatedReadPages": 0,
                "searches": 0,
                "searchesByOrigin": {},
                "emptySearches": 0,
                "duplicateSearches": 0,
                "hitPages": set(),
                "searchCache": {},
                "subagentFindingKeys": set(),
                "subagents": {"dispatched": 0, "cached": 0, "denied": 0,
                              "rounds": 0, "findings": 0},
            }
            state["_efficiency"] = eff
        return eff


def _count_tool_call(state: dict[str, Any], name: str) -> None:
    eff = _efficiency(state)
    with _LEDGER_LOCK:
        eff["toolCalls"][name] = int(eff["toolCalls"].get(name) or 0) + 1


def _cached_term_hits(
    state: dict[str, Any], term: str, origin: str,
    fetch: Callable[[], list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], bool]:
    """单个检索词的缓存与记账，返回 (hits, 是否命中缓存)。"""
    eff = _efficiency(state)
    key = " ".join(str(term or "").split())
    with _LEDGER_LOCK:
        eff["searches"] += 1
        eff["searchesByOrigin"][origin] = int(eff["searchesByOrigin"].get(origin) or 0) + 1
        cached = eff["searchCache"].get(key)
    if cached is not None:
        with _LEDGER_LOCK:
            eff["duplicateSearches"] += 1
        return cached, True
    hits = fetch()
    with _LEDGER_LOCK:
        eff["searchCache"][key] = hits
        for hit in hits:
            eff["hitPages"].add((str(hit.get("documentId") or ""), int(hit.get("pageNo") or 0)))
        if not hits:
            eff["emptySearches"] += 1
    return hits, False


def _efficiency_search(
    state: dict[str, Any], query: str, origin: str, limit: int,
    fetch: Callable[[], list[dict[str, Any]]],
) -> dict[str, Any]:
    """执行一次单检索词并记账（§10.4 D4）。

    同一检索词在本任务内重复出现时直接复用上次结果，并把 duplicateSearches
    计入指标——模型常见的空转是换个空格/大小写重复同一查询；命中为 0 时给出
    换词与逐页核对的确定性提示，避免用"没搜到"当作"不存在"。
    """
    key = " ".join(str(query or "").split())
    hits, cached = _cached_term_hits(state, key, origin, fetch)
    if cached:
        return {
            "query": key,
            "limit": limit,
            "hits": hits,
            "cached": True,
            "note": "该检索词本次任务内已检索过（结果未变化），未重复查询。"
                    "如需扩大范围请换同义词/简称/关键人名，或直接 document_get_pages 逐页核对。",
        }
    out: dict[str, Any] = {"hits": hits}
    if not hits:
        out["note"] = (
            f"本案材料中未检索到「{key}」。一次无命中不能断言全案不存在："
            "请换同义词/简称/关键人名再检索，或先 documents_list 确认材料构成后用 "
            "document_get_pages 逐页核对；确实没有的按「本次检索范围内未见记载」表述。"
        )
    return out


def _efficiency_search_multi(
    state: dict[str, Any], terms: list[str], origin: str, limit: int,
    fetch: Callable[[str], list[dict[str, Any]]],
) -> dict[str, Any]:
    """多检索词分别检索后按页合并去重（§6.7 / §10.4 D5 查询拆解）。

    阅读成本按合并后的页数计而不是各词命中数相加：同一页被多个词命中只返回
    一次，并用 matchedTerms 说明它被哪些词命中，模型据此判断该页的相关性。
    """
    from ..search import merge_hits

    by_term: list[tuple[str, list[dict[str, Any]]]] = []
    cached_terms: list[str] = []
    for term in terms:
        key = " ".join(str(term or "").split())
        hits, cached = _cached_term_hits(state, key, origin, lambda t=key: fetch(t))
        if cached:
            cached_terms.append(key)
        by_term.append((key, hits))
    hits = merge_hits(by_term, limit)
    out: dict[str, Any] = {"queries": [t for t, _ in by_term], "limit": limit, "hits": hits}
    if cached_terms:
        out["cachedQueries"] = cached_terms
    if not hits:
        out["note"] = (
            f"本案材料中未检索到「{'、'.join(t for t, _ in by_term)}」中的任何一个。"
            "一次无命中不能断言全案不存在：请换同义词/简称/关键人名再检索，或先 "
            "documents_list 确认材料构成后用 document_get_pages 逐页核对；"
            "确实没有的按「本次检索范围内未见记载」表述。"
        )
    return out


def _efficiency_read(
    state: dict[str, Any],
    document_id: str,
    result: dict[str, Any],
    *,
    char_offset: int,
    origin: str,
) -> list[int]:
    """记录一次读页并统计重复读取（§10.4 D4），返回此前已完整读过的页码。

    只做提示不改内容：上下文压缩后模型确实可能忘记细节而需要复查，
    因此重复读取仍如实返回原文，但会注明这些页此前已完整读取。
    """
    eff = _efficiency(state)
    pages = [p for p in (result.get("pages") or []) if isinstance(p, dict)]
    with _LEDGER_LOCK:
        eff["readsByOrigin"][origin] = int(eff["readsByOrigin"].get(origin) or 0) + 1
        ledger = dict(state.get("reading_ledger") or {})
        page_nos = [int(p.get("pageNo") or 0) for p in pages if int(p.get("pageNo") or 0) > 0]
        already = [
            n for n in page_nos
            if (ledger.get(f"{document_id}:{n}") or {}).get("state") in ("read_complete", "unreadable")
        ]
        if already:
            eff["repeatedReads"] += 1
            eff["repeatedReadPages"] += len(already)
    if already and page_nos and not char_offset:
        result["alreadyReadPages"] = already
        result["note"] = str(result.get("note") or "") + (
            f"（第{'、'.join(str(n) for n in already)}页此前已完整读取，内容未变化；"
            "仅当上下文压缩丢失细节时才需复查，否则请读取未读页或直接作答。）"
        )
    return already


def _subagent_finding_key(finding: dict[str, Any]) -> str | None:
    """子 Agent 候选条目的来源指纹（§10.4 D4 采用率口径：文档 + 页码 + 规范化短引）。"""
    from ..citations import normalize_text

    title = str(finding.get("documentTitle") or "").strip()
    try:
        page_no = int(finding.get("pageNo"))
    except (TypeError, ValueError):
        return None
    quote = normalize_text(str(finding.get("quote") or ""))[:120]
    if not title or not quote or page_no <= 0:
        return None
    return f"{title}|{page_no}|{quote}"


def _subagent_adoption(state: dict[str, Any], keys: set[str], evidence: dict[str, Any]) -> int:
    """子 Agent findings 中最终以同一原文出处进入证据账本的条数。"""
    from ..citations import normalize_text

    if not keys:
        return 0
    adopted = 0
    for entry in evidence.values():
        src = entry.get("source") or {}
        title = str(src.get("documentTitle") or "").strip()
        page_no = src.get("pageNo")
        quote = normalize_text(str(src.get("quoteNorm") or src.get("quote") or ""))[:120]
        if not title or page_no is None or not quote:
            continue
        try:
            page_no = int(page_no)
        except (TypeError, ValueError):
            continue
        if f"{title}|{page_no}|{quote}" in keys:
            adopted += 1
    return adopted


def _efficiency_summary(state: dict[str, Any]) -> dict[str, Any]:
    """任务收尾的效率指标（§4.2 / §10.4 D4）：定位主要浪费点的统一口径。

    同时用于 turn_complete 与每轮状态摘要，二者读同一份数据，避免
    "界面显示的浪费点"和"模型看到的提示"各说一套。
    """
    eff = _efficiency(state)
    with _LEDGER_LOCK:
        keys = set(eff.get("subagentFindingKeys") or ())
        sub = dict(eff.get("subagents") or {})
        evidence = dict(state.get("evidence_ledger") or {})
        tool_calls = dict(eff.get("toolCalls") or {})
        reads_by_origin = dict(eff.get("readsByOrigin") or {})
        searches_by_origin = dict(eff.get("searchesByOrigin") or {})
        hit_pages = len(eff.get("hitPages") or ())
        repeated_reads = int(eff.get("repeatedReads") or 0)
        repeated_pages = int(eff.get("repeatedReadPages") or 0)
        searches = int(eff.get("searches") or 0)
        empty_searches = int(eff.get("emptySearches") or 0)
        duplicate_searches = int(eff.get("duplicateSearches") or 0)
    findings = int(sub.get("findings") or 0)
    adopted = _subagent_adoption(state, keys, evidence)
    sub["findingsAdopted"] = adopted
    sub["adoptionRate"] = round(adopted / findings, 3) if findings else None
    return {
        "toolCalls": sum(int(v or 0) for v in tool_calls.values()),
        "toolCallsByName": tool_calls,
        "reads": sum(int(v or 0) for v in reads_by_origin.values()),
        "readsByOrigin": reads_by_origin,
        "repeatedReads": repeated_reads,
        "repeatedReadPages": repeated_pages,
        "searches": searches,
        "searchesByOrigin": searches_by_origin,
        "emptySearches": empty_searches,
        "duplicateSearches": duplicate_searches,
        "uniqueHitPages": hit_pages,
        "subagents": sub,
    }


def _load_pages_cached(cache: dict[str, list[dict[str, Any]]], document_id: str) -> list[dict[str, Any]]:
    if document_id not in cache:
        from .. import documents

        cache[document_id] = documents.get_pages(document_id)
    return cache[document_id]


def _resolve_evidence_source(
    state: dict[str, Any],
    raw: dict[str, Any],
    cache: dict[str, list[dict[str, Any]]],
    docs: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    """解析并回查一条证据的来源，返回 (source, reasonCode)。

    reasonCode 为 None 表示该短引确实出现在指定文档的指定页（引证忠实）；
    它只证明"确实这样写"，不证明该陈述为真（§6.1 第 4 点）。
    """
    from ..citations import LOW_OCR_CONFIDENCE, normalize_text
    from .verify import _match_doc

    doc_id = str(raw.get("documentId") or "").strip()
    doc = next((d for d in docs if str(d.get("id")) == doc_id), None) if doc_id else None
    if doc is None:
        title = str(raw.get("documentTitle") or "").strip()
        doc = _match_doc(docs, title) if title else None
    if doc is None:
        return None, "OUT_OF_EVIDENCE_SCOPE"
    page_no = raw.get("pageNo")
    try:
        page_no = int(page_no)
    except (TypeError, ValueError):
        return None, "PAGE_NOT_CITED"
    quote = str(raw.get("quote") or "").strip()
    if not quote:
        return None, "QUOTE_NOT_LOCATABLE"
    source = {
        "documentId": doc["id"],
        "documentTitle": str(doc.get("title") or ""),
        "versionId": doc.get("activeVersionId"),
        "pageNo": page_no,
        "quote": quote[:300],
        "quoteNorm": normalize_text(quote)[:300],
    }
    pages = _load_pages_cached(cache, doc["id"])
    page = next((p for p in pages if int(p.get("pageNo") or 0) == page_no), None)
    if page is None:
        return source, "PAGE_NOT_FOUND"
    text = str(page.get("text") or "")
    if not text.strip():
        return source, "PAGE_UNREADABLE"
    if source["quoteNorm"] not in normalize_text(text):
        return source, "QUOTE_MISMATCH"
    confidence = page.get("ocrConfidence")
    if confidence is not None and float(confidence) < LOW_OCR_CONFIDENCE:
        return source, "LOW_OCR_CONFIDENCE"
    return source, None


def _record_evidence(
    state: dict[str, Any],
    raws: list[Any],
    *,
    origin: str = "model",
    docs: list[dict[str, Any]] | None = None,
    cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """把证据条目写入账本并即时回查来源（§5.5）。

    - 同一来源同一短引重复提交时合并为一条（不重复占用账本，也不当作独立印证）；
    - 无来源的推断/法律评价允许保留，但状态为 needs_human（不得当作已支持事实）；
    - origin 区分主 Agent 与子 Agent：子 Agent 的自然语言结论不会直接变成 supported。
    """
    with _LEDGER_LOCK:
        return _record_evidence_locked(state, raws, origin=origin, docs=docs, cache=cache)


def _record_evidence_locked(
    state: dict[str, Any],
    raws: list[Any],
    *,
    origin: str = "model",
    docs: list[dict[str, Any]] | None = None,
    cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    ledger: dict[str, Any] = state.setdefault("evidence_ledger", {})
    if docs is None:
        from .. import documents

        docs = documents.list_task_documents(str(state.get("case_id") or ""))
    if cache is None:
        cache = {}
    added = updated = supported = needs_human = 0
    results: list[dict[str, Any]] = []
    for raw in raws or []:
        if not isinstance(raw, dict):
            continue
        claim = str(raw.get("claim") or "").strip()
        if not claim:
            continue
        layer = str(raw.get("layer") or "supported_fact").strip()
        if layer not in _EVIDENCE_LAYERS:
            layer = "supported_fact"
        direction = str(raw.get("direction") or "supports").strip()
        if direction not in _EVIDENCE_DIRECTIONS:
            direction = "supports"
        source, reason = _resolve_evidence_source(state, raw, cache, docs)
        if source is None and reason is None:
            reason = "OUT_OF_EVIDENCE_SCOPE"
        if reason in ("OUT_OF_EVIDENCE_SCOPE", "PAGE_NOT_CITED", "QUOTE_NOT_LOCATABLE") \
                and layer in ("inference", "legal"):
            # 推断与法律评价本就不来源于原文，允许无来源，但仍非"已支持事实"
            reason = "OUT_OF_EVIDENCE_SCOPE"
        status = "supported" if reason is None else "needs_human"
        key = (
            f"{source['documentId']}:{source['pageNo']}:{source['quoteNorm']}"
            if source else f"claim:{claim[:80]}"
        )
        entry = {
            "id": key,
            "claim": claim[:200],
            "layer": layer,
            "direction": direction,
            "issueId": str(raw.get("issueId") or "").strip() or None,
            "source": source,
            "status": status,
            "reasonCode": reason,
            "origin": str(raw.get("origin") or origin),
            "round": int(state.get("_round") or 0),
        }
        if key in ledger:
            updated += 1
            # 增量更新：保留首次记录轮次与来源，方向/争点归属可变
            ledger[key].update({
                "direction": direction,
                "issueId": entry["issueId"] or ledger[key].get("issueId"),
                "layer": layer,
            })
        else:
            added += 1
            ledger[key] = entry
        supported += 1 if status == "supported" else 0
        needs_human += 1 if status != "supported" else 0
        results.append({
            "claim": entry["claim"], "status": status, "reasonCode": reason,
            "source": (f"{source['documentTitle']}第{source['pageNo']}页" if source else None),
            "direction": direction,
        })
    if len(ledger) > EVIDENCE_LEDGER_MAX_ENTRIES:
        for stale_key in list(ledger)[: len(ledger) - EVIDENCE_LEDGER_MAX_ENTRIES]:
            ledger.pop(stale_key, None)
    return {"added": added, "updated": updated, "supported": supported,
            "needsHuman": needs_human, "entries": results[:20]}


def _record_issues(state: dict[str, Any], raws: list[Any]) -> dict[str, Any]:
    """争点账本（§5.6）：只保存必要字段，不强制简单任务建表。"""
    with _LEDGER_LOCK:
        return _record_issues_locked(state, raws)


def _record_issues_locked(state: dict[str, Any], raws: list[Any]) -> dict[str, Any]:
    ledger: dict[str, Any] = state.setdefault("issue_ledger", {})
    added = updated = 0
    for raw in raws or []:
        if not isinstance(raw, dict):
            continue
        issue = str(raw.get("issue") or "").strip()
        if not issue:
            continue
        key = str(raw.get("id") or "").strip() or issue[:60]
        status = str(raw.get("status") or "open").strip()
        if status not in _ISSUE_STATUSES:
            status = "open"
        entry = {
            "id": key,
            "issue": issue[:200],
            "claimBasis": str(raw.get("claimBasis") or "").strip() or None,
            "burden": str(raw.get("burden") or "").strip() or None,
            "alternatives": [str(a)[:120] for a in (raw.get("alternatives") or []) if str(a).strip()][:5],
            "gaps": [str(g)[:120] for g in (raw.get("gaps") or []) if str(g).strip()][:8],
            "status": status,
            "round": int(state.get("_round") or 0),
        }
        if key in ledger:
            updated += 1
            ledger[key].update(entry)
        else:
            added += 1
            ledger[key] = entry
    if len(ledger) > ISSUE_LEDGER_MAX_ENTRIES:
        for stale_key in list(ledger)[: len(ledger) - ISSUE_LEDGER_MAX_ENTRIES]:
            ledger.pop(stale_key, None)
    return {"added": added, "updated": updated, "issues": list(ledger.values())[:20]}


def _ledger_stats(state: dict[str, Any]) -> dict[str, Any]:
    """账本统计（守门与状态摘要共用）：计数、同源重复组、相反方向组、未解决争点。"""
    # 并行子代理可能同时写入，遍历前先取快照，避免 dict 迭代中途变更
    with _LEDGER_LOCK:
        evidence: dict[str, Any] = dict(state.get("evidence_ledger") or {})
        issues: dict[str, Any] = dict(state.get("issue_ledger") or {})
    by_source: dict[str, int] = {}
    by_issue: dict[str, set[str]] = {}
    supported = needs_human = 0
    for entry in evidence.values():
        if entry.get("status") == "supported":
            supported += 1
        else:
            needs_human += 1
        source = entry.get("source") or {}
        if source.get("documentId") and source.get("pageNo"):
            group = f"{source.get('documentTitle')}第{source.get('pageNo')}页"
            by_source[group] = by_source.get(group, 0) + 1
        issue_id = entry.get("issueId")
        if issue_id:
            by_issue.setdefault(issue_id, set()).add(str(entry.get("direction") or ""))
    same_source = sorted(k for k, n in by_source.items() if n > 1)
    opposite = sorted(k for k, dirs in by_issue.items() if "supports" in dirs and "opposes" in dirs)
    open_issues = [i for i in issues.values() if i.get("status") == "open"]
    return {
        "evidenceTotal": len(evidence),
        "evidenceSupported": supported,
        "evidenceNeedsHuman": needs_human,
        "sameSourceGroups": same_source[:6],
        "oppositeDirectionIssues": opposite[:6],
        "issueTotal": len(issues),
        "openIssues": [str(i.get("issue") or "")[:60] for i in open_issues[:6]],
        "openIssueCount": len(open_issues),
    }


def _ledger_summary(state: dict[str, Any]) -> str:
    """每轮注入的动态状态摘要（§6.4 §6.6）：压缩后仍能看到任务、覆盖与未解决缺口。"""
    contract = state.get("task_contract") or {}
    stats = _ledger_stats(state)
    lines: list[str] = []
    depth = str(contract.get("taskDepth") or "")
    if depth:
        objective = _clip(str(contract.get("objective") or ""), 80)
        lines.append(f"任务深度：{depth}" + (f"（{objective}）" if objective else ""))
    coverage = _reading_coverage(state, contract)
    if coverage.get("required"):
        excluded = int(coverage.get("excluded") or 0)
        lines.append(
            f"阅读覆盖：已读 {coverage['readPages']}/{coverage['totalPages']} 页，"
            f"未读 {len(coverage.get('unread') or [])} 页"
            + (f"，用户排除 {excluded} 页（已在回答说明理由）" if excluded else "")
        )
    if stats["evidenceTotal"]:
        lines.append(
            f"证据账本：{stats['evidenceTotal']} 条（引证已回查 {stats['evidenceSupported']}，"
            f"待核实 {stats['evidenceNeedsHuman']}）"
        )
    if stats["sameSourceGroups"]:
        lines.append("同源重复（不得当作独立印证）：" + "、".join(stats["sameSourceGroups"]))
    if stats["oppositeDirectionIssues"]:
        lines.append("存在相反方向证据的争点：" + "、".join(stats["oppositeDirectionIssues"]))
    if stats["openIssueCount"]:
        lines.append(
            f"未解决争点 {stats['openIssueCount']} 项：" + "、".join(stats["openIssues"])
            + "（用证据关闭，或明确标为待核实）"
        )
    waste = _waste_hint(state)
    if waste:
        lines.append(waste)
    if not lines:
        return ""
    return _STATE_MARK + _clip("\n".join(lines), LEDGER_SUMMARY_CHARS)


def _waste_hint(state: dict[str, Any]) -> str:
    """本轮状态摘要里的浪费提示（§10.4 D4）：只在高频重复时提醒，不逐次打断。"""
    eff = _efficiency(state)
    with _LEDGER_LOCK:
        repeated = int(eff.get("repeatedReads") or 0)
        repeated_pages = int(eff.get("repeatedReadPages") or 0)
        duplicate = int(eff.get("duplicateSearches") or 0)
        empty = int(eff.get("emptySearches") or 0)
    hints: list[str] = []
    if repeated >= 2 and repeated_pages >= 2:
        hints.append(f"已重复读取 {repeated_pages} 页（{repeated} 次）；这些页内容未变化，请改读未读页")
    if duplicate:
        hints.append(f"已有 {duplicate} 次重复检索同一检索词；请换同义词或直接读页")
    if empty >= 2:
        hints.append(f"已有 {empty} 次检索无命中；请先核对材料构成，不要把「未检索到」写成「不存在」")
    return "效率提示：" + "；".join(hints) if hints else ""


def _citation_gaps(state: dict[str, Any], answer: str) -> list[str]:
    """轻量扫描：带页码且能匹配到案内文档的引用，是否给出了紧跟的逐字短引。

    只用于守门修复决策，不建 Claim（Claim 由收尾 verify_answer 一次性建立，
    避免守门多轮重复落库）。
    """
    try:
        from .verify import _CITE_RE, _explicit_quote

        from .. import documents

        docs = documents.list_task_documents(str(state.get("case_id") or ""))
        titles = {str(d.get("title") or "") for d in docs} - {""}
        gaps: list[str] = []
        for m in _CITE_RE.finditer(answer or ""):
            title = m.group(1).strip()
            page_no = int(m.group(2)) if m.group(2) else 0
            if not page_no:
                continue
            if not any(title in t or t in title for t in titles):
                continue  # 法条/书名等非案内文档不参与
            if _explicit_quote(answer, m.end()):
                continue
            label = f"《{title}》第{page_no}页"
            if label not in gaps:
                gaps.append(label)
        return gaps
    except Exception:  # noqa: BLE001 守门扫描失败不放行修复，仅跳过
        return []


def _deliverable_issues(state: dict[str, Any], answer: str) -> list[dict[str, str]]:
    """交付账本检查（§5.7）：声称生成的文件是否真实存在、必需产物是否已建。"""
    issues: list[dict[str, str]] = []
    if answer.strip():
        try:
            from .runner import verify_claimed_files

            missing = verify_claimed_files(answer)
        except Exception:  # noqa: BLE001
            missing = []
        if missing:
            issues.append({
                "code": "MISSING_DELIVERABLE",
                "text": f"声称生成但未检测到实际文件：{'、'.join(missing[:5])}",
                "files": missing[:5],
                "instruction": f"以下文件并未实际落盘：{'、'.join(missing[:5])}。"
                               "请用工具真实生成后再声明，或修改正文表述，不要声称未发生的保存。",
            })
    mode = str(state.get("mode") or "")
    task_id = str(state.get("task_id") or "")
    case_id = str(state.get("case_id") or "")
    if mode == "contract_review" and \
            (state.get("contract_input") or {}).get("workflowKind") != "contract_dialogue":
        try:
            from .. import contract as contract_store

            if not contract_store.list_reviews(case_id):
                issues.append({
                    "code": "MISSING_DELIVERABLE",
                    "text": "合同审查结果尚未落库",
                    "instruction": "本次是合同审查任务但尚未建立审查记录：请先调用 contract_create_review "
                                   "并对其执行条款切分，再输出结论。",
                })
        except Exception:  # noqa: BLE001
            pass
    if mode == "document_generation" and task_id:
        # 修订任务沿用既有草稿，交付物是新版本而不是新草稿，故不要求 task_id 归属
        revision_draft = str((state.get("docgen_input") or {}).get("revisionDraftId") or "")
        try:
            from ..storage import get_db

            if revision_draft:
                rows = get_db().query(
                    "SELECT id FROM docgen_draft_versions WHERE draft_id = ? LIMIT 1",
                    (revision_draft,),
                )
            else:
                rows = get_db().query(
                    "SELECT id FROM docgen_drafts WHERE task_id = ? LIMIT 1", (task_id,)
                )
            if not rows:
                issues.append({
                    "code": "MISSING_DELIVERABLE",
                    "text": "文书草稿尚未创建" if not revision_draft else "修订版本尚未落库",
                    "instruction": "本次是文书生成任务但尚未创建草稿：请调用 docgen_create_draft"
                                   "（或 docgen_create_free_draft）建立草稿后再输出结论。"
                                   if not revision_draft else
                                   "本次是文书修订任务但尚未落库修订版本：请用 docgen_revise_draft "
                                   "把修订稿保存为新版本后再输出结论。",
                })
        except Exception:  # noqa: BLE001
            pass
    return issues


def _finalize_gate(
    state: dict[str, Any],
    answer: str,
    *,
    allow_repairs: bool,
    verify: bool = False,
) -> dict[str, Any]:
    """完成守门（§7.1）：返回可修复项 + 需披露的警告，不通过则带警告完成。

    - allow_repairs=True 时产出结构化、可定位的修复指令（§7.2），由主循环用
      独立的小预算执行，避免整篇重生成；
    - verify=True 时调用收尾 verify_answer 建立 Claim（只在最终轮执行一次）；
      修复轮用轻量引用扫描，避免守门多轮重复落库。
    """
    contract = state.get("task_contract") or _derive_task_contract(state)
    task_id = str(state.get("task_id") or "")
    case_id = str(state.get("case_id") or "")
    repairs: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    verification: dict[str, Any] = {"total": 0, "supported": 0, "pending": [], "cites": []}
    if verify and answer.strip():
        from .verify import verify_answer

        verification = verify_answer(task_id, case_id, answer)
    pending = list(verification.get("pending") or [])
    if pending:
        warnings.append({"code": "PENDING_CITATIONS",
                         "text": f"{len(pending)} 处引用未能回查原文，已在回答末尾标注待人工核实"})
        if allow_repairs and len(pending) <= FINALIZE_MAX_REPAIRS:
            repairs.append({
                "code": "PENDING_CITATIONS",
                "instruction": "以下引用未能通过原文回查，请为对应结论补充可定位的原文短引"
                               "（写作《文档名》第N页，原文「逐字片段」），或改为明确标注待核实："
                               + "；".join(pending[:FINALIZE_MAX_REPAIRS]),
            })
    elif not verification.get("total") and allow_repairs:
        gaps = _citation_gaps(state, answer)
        if gaps:
            warnings.append({"code": "PENDING_CITATIONS",
                             "text": f"{len(gaps)} 处案内引用未附原文短引"})
            if len(gaps) <= FINALIZE_MAX_REPAIRS:
                repairs.append({
                    "code": "PENDING_CITATIONS",
                    "instruction": "以下案内引用未给出可定位的原文短引，请补充逐字片段"
                                   "（写作《文档名》第N页，原文「…」），否则标为待核实："
                                   + "；".join(gaps[:FINALIZE_MAX_REPAIRS]),
                })
    coverage = _reading_coverage(state, contract)
    unread = list(coverage.get("unread") or [])
    if unread:
        sample = "、".join(f"《{u['title']}》第{u['pageNo']}页" for u in unread[:6])
        warnings.append({"code": "UNREAD_PAGES",
                         "text": f"尚有 {len(unread)} 页材料未读到终态（如 {sample}）"})
        if allow_repairs and len(unread) <= FINALIZE_COVERAGE_REPAIR_PAGES:
            repairs.append({
                "code": "UNREAD_PAGES",
                "instruction": f"以下页面尚未读取（{sample}）：用 document_get_pages 逐页补读后再交付；"
                               "页面无文本或无法解析的，明确标注为不可读并说明原因，不要凭其他材料推断。",
            })
    for issue in _deliverable_issues(state, answer):
        warning = {"code": issue["code"], "text": issue["text"]}
        if issue.get("files"):
            warning["files"] = issue["files"]
        warnings.append(warning)
        if allow_repairs:
            repairs.append({"code": issue["code"], "instruction": issue["instruction"]})
    # 缺口驱动补查（§10.3 C7）：未关闭的争点说明调查尚未收敛，先给一次性精准补查机会；
    # 简单定位任务（direct）不因账本存在而被拖入额外调查。
    depth = str(contract.get("taskDepth") or "")
    if depth in ("focused", "full_review"):
        issues = [i for i in (state.get("issue_ledger") or {}).values()
                  if str(i.get("status") or "") == "open"]
        if issues:
            labels = [str(i.get("issue") or "")[:40] for i in issues[:FINALIZE_MAX_REPAIRS]]
            warnings.append({"code": "OPEN_ISSUES",
                             "text": f"{len(issues)} 项争点尚未关闭（{'、'.join(labels[:3])}）"})
            if allow_repairs:
                detail = []
                for i in issues[:FINALIZE_MAX_REPAIRS]:
                    gaps = [str(g) for g in (i.get("gaps") or []) if str(g).strip()]
                    detail.append(str(i.get("issue") or "")
                                  + (f"（缺口：{'、'.join(gaps[:3])}）" if gaps else ""))
                repairs.append({
                    "code": "OPEN_ISSUES",
                    "instruction": "以下争点仍标记为未解决，请用 document_search / document_get_pages "
                                   "补查其缺口；查到新证据后用 case_ledger 更新对应证据条目与争点状态"
                                   "（关闭为 ready_to_synthesize），确实无法查清的改为 needs_human "
                                   "并在回答中说明缺什么材料：" + "；".join(detail),
                })
    return {
        "ok": not warnings,
        "repairs": repairs[:FINALIZE_MAX_REPAIRS],
        "warnings": warnings,
        "verification": verification,
        "coverage": coverage,
    }


def _repair_prompt(repairs: list[dict[str, str]]) -> str:
    """守门修复指令（§7.2）：结构化、可定位，明确禁止整篇重新分析。"""
    lines = ["系统完成守门发现以下可由你局部修复的问题（不是要求重新阅卷或重写全文）："]
    for i, item in enumerate(repairs, 1):
        lines.append(f"{i}. {item.get('instruction')}")
    lines.append(
        "请只针对上述问题做最小必要修复：需要时用 document_get_pages / document_search 补读指定页面；"
        "无法从原文确认的内容明确标为待核实并说明原因。修复后直接输出完整最终回答"
        "（保留原有全部有效内容，不要省略已完成的章节）。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt 构建
# ---------------------------------------------------------------------------

# 模式提示：工具导航 + 领域纪律（自 graph.py 旧固定管线提示词迁移）+ 完成标准。
# 分层组装（05 方案 §6.6 D1）：本段是稳定认知方法与完成标准，交付格式（表格/出图）
# 拆到下面两段，仅在确有该交付需求时注入——普通单点问答不再承担格式规则的固定开销。
_LEGAL_REVIEW_HINT = (
    "当前是法律阅卷任务：围绕用户请求梳理案件事实、证据与争点，案内信息请优先用工具查证后再下结论。"
    "复杂定向调查（多轮检索汇总）可委托 case_explore 子代理，简单查证直接检索。"
    "证据与争点账本：把已核实的关键命题用 case_ledger 记账（命题 + 五层信息 + 支持/反对方向 + "
    "所属争点 + 文档名/页码/逐字短引），系统会即时回查原文并给出 supported 或待核实；"
    "争点用 case_ledger 的 issue 记录构成要件、证明责任、竞争解释与缺口。"
    "账本不是额外文书工作：它让你在上下文压缩、续跑或多轮调查后仍能准确引用来源；"
    "子 Agent 返回的 findings 只是候选，必须经 case_ledger 提交回查后才可作为已支持事实。"
    "分析纪律：\n"
    "- 日期只写材料中出现的形式，只能确定月份或年份时不得补写具体日；\n"
    "- 材料之间存在矛盾的，同时保留两侧说法并明确指出矛盾，不自行取舍；\n"
    "- 可运用法律知识发现候选法条、类案与争点；正式引用应核验适用范围与时效性并注明法源，"
    "未核验的标为待核实，不编造；\n"
    "- 对委托方不利的材料必须完整呈现，不得隐藏。\n"
    "完成标准：用户点名的每项产出都已真实生成（工具成功返回）；事实性前提均有案内来源"
    "（文档名 + 页码），法律命题有法源或标为待核实，分析与建议已说明事实及法律前提，"
    "无依据的事实推断已标注为推测并提示人工核实。"
)

# 交付格式层（§6.6 第 2 条）：只在分析型任务（focused / full_review 或带预设）注入
_PRESENTATION_DISCIPLINE = (
    "呈现纪律（表格优先，让用户一眼看清案件）：\n"
    "- 可逐项对比的结构化信息优先用 Markdown 表格，不要写成大段流水文字："
    "案件基本信息、当事人、时间线、争议焦点、事实与证据对应、证据卡、矛盾点、"
    "缺失材料、下一步工作清单都用表格承载；\n"
    "- 案件基本信息表建议列为「字段｜内容｜出处」，逐行写案号、案由、法院、"
    "程序阶段、我方身份等，材料未记载的格子写「材料未记载」；\n"
    "- 争议焦点表建议列为「焦点｜双方主张｜我方立场与举证责任｜关键证据｜出处」；\n"
    "- 证据分析表建议列为「证据｜类型/形成时间｜来源与保管｜证明目的｜三性及证明力意见｜出处」；\n"
    "- 事实对应表建议列为「关键事实｜支撑证据｜状态｜出处」；\n"
    "- 表格控制在 2-6 列、单元格写短句（约 40 字内），出处标注《文档名》第N页，"
    "状态用「已支持/待核实/存在矛盾」等文字写明，不靠颜色或图标；\n"
    "- 超过约 15 行或 6 列时按主题拆成多张表（如主表 + 明细表）；\n"
    "- 结论理由、法律分析与建议仍用简短段落/分点讲清，表格负责「一眼看清」，"
    "段落负责「讲明道理」；同一信息不要表格与正文重复两遍，纯叙述性内容不强行表格化。"
)

# 交付物格式层：仅当本次任务确实要求出图/演示时注入（对应 render_html / render_pptx 工具）
_DELIVERABLE_DISCIPLINE = (
    "交付物纪律（用户点名要求出图时必须遵守）：\n"
    "- 用户要求生成图表/可视化（资金流向图、人物关系图、时间轴、流程图等）时，"
    "必须在主循环调用 render_html 产出可交互预览卡片；不得以文字或表格描述代替出图，"
    "也不得把出图委托给 case_explore 子代理（它只有检索工具，无法渲染）；\n"
    "- 只有 render_html 成功返回后才能说「已生成」；工具报错（HTML 为空/过大等）时"
    "先精简后重试，重试仍失败则在回答中说明失败原因与替代方案，禁止谎称已完成；\n"
    "- 图中节点与连线只能来自材料已记载的流转关系并标注《文档名》第N页；"
    "金额对不上、去向不明的用虚线并标注「待核实」，不得编造未记载的资金路径；\n"
    "- 回答正文直接讲图的关键结论，不要写「见下方卡片」「见上图」这类方位描述。"
)

_MODE_HINTS = {
    "legal_review": _LEGAL_REVIEW_HINT,
    "contract_review": (
        "当前是合同审查任务：用户消息会附带审查输入（主合同文档、审查方角色、风险偏好等），"
        "可用 contract_create_review 创建审查会话、contract_split_clauses 拆分条款"
        "（交叉引用悬空、关键条款缺失等确定性风险会自动落库），再基于切分结果逐条做语义风险分析。"
        "条款较多时（如超过 40 条）可用 contract_get_clauses 分批读取（建议每批约 20 条），"
        "逐批分析后汇总去重；条款不多时一次读完即可，批大小由你自行判断。"
        "分析纪律：\n"
        "- 围绕审查方利益组织风险说明与修改建议，对该方有利的内容不列为风险；\n"
        "- 风险分级：severe=可能导致重大损失或合同目的落空；high=明显不利，须修改后再签；"
        "medium=存在隐患，建议完善；low=提示性事项；\n"
        "- 引用条款时用切分结果中的条款编号，原文逐字摘录，不改写、不拼接；\n"
        "- 条款交叉引用（如「详见第X条」）需核对被引条款是否存在且内容一致。\n"
        "完成标准：确定性检查与逐条语义分析都已完成，结论按风险分级呈现并附条款引证。"
    ),
    "document_generation": (
        "当前是文书生成任务：用户只需选好模板并发送任务，你要先了解关联案件信息再据此起草，"
        "不要要求用户填写基本案件信息。"
        "可用 docgen_list_templates 查看可用模板（用户未指定模板时按需求选择最匹配的一项；"
        "无合适模板时用 docgen_create_free_draft 按章节自由起草），用 docgen_create_draft 创建文书草稿。"
        "若 docgenInput 给出 docType 但没有 templateId，说明模板库没有完全同名的模板："
        "不要调用 docgen_list_templates 检索，直接按该文书类型用 docgen_create_free_draft 自由起草，"
        "headings 由你按该文书类型拟定；不得回复「没有搜到模板」或因此停下。"
        "关联案件材料与案件记忆是主要事实来源：先用 documents_list/document_search/document_get_pages/"
        "memory_search 通读关键材料，能从原文确认的事实直接使用。"
        "创建草稿后必须调用 docgen_render_draft 生成正文并落盘 Word：先把能从案件材料确认的事实填入模板槽位"
        "（values，案件原文来源必须带 documentId/pageNo/quote，系统会回查原文），再生成正文。"
        "落盘后系统会在对话气泡底部给出 Word 文件卡，同时登记到右侧产物概览，"
        "用户可在文书工作台预览、编辑，或继续提出修改意见让你迭代新版本。"
        "起草纪律：\n"
        "- 文书中的案件事实必须逐字摘录材料原文并可溯源，不得改写拼接；\n"
        "- 姓名、证件号、地址、日期、金额、案号、法院、联系方式等普通信息先自行从材料中提取，"
        "无法从材料确认的写 xx，不追问、不编造；\n"
        "- 涉及必要用户决策（诉讼立场、诉讼地位、主位/备位请求、辩护方向、是否认罪、是否调解、"
        "请求金额取舍、是否采用某项敏感事实）且无法从材料判断时，必须调用 request_user_decision "
        "提问并等待用户回答：不因模板有空字段就提问，不重复询问已回答的问题；"
        "一次只问会改变下一步生成行为的问题，每批不超过 3 个，并说明为什么现在需要决定；"
        "非关键问题不阻塞首稿（先按 xx 起草）；\n"
        "- 章节结构对齐所选模板的固定章节；\n"
        "- 定稿与正式导出仍由用户在文书工作台确认，你只产出草稿。\n"
        "完成标准：草稿已创建，docgen_render_draft 已落盘 Word 草稿，已填事实有材料依据，"
        "普通缺失信息均以 xx 标注，必要决策已由用户确认或明确标注待核实。"
    ),
}

# 律师阅卷认知内核：适用于所有 legal_review 任务，而非仅四个预设。
# 约束的是分析方法，不要求模型机械展示推理过程或套用固定报告模板。
_LEGAL_REASONING_CORE = (
    "律师阅卷认知内核（所有阅卷任务均应遵守）：\n"
    "你不仅要整理卷宗，还要形成可检验、可反驳、可回溯的律师判断。"
    "对可能影响案件结果的问题，根据任务需要自主完成以下分析，不必机械展示全部思考过程：\n"
    "- 先识别我方身份、程序阶段、具体请求或指控，以及当前真正需要决定的问题。"
    "这些信息不明且会实质影响结论时，提出具体、可回答的问题；不影响当前工作的缺失信息不要反复追问；\n"
    "- 将关键争点拆解为法律要件、事实要件或证明命题，逐项检验证明责任、适用证明标准、"
    "支持证据、反对证据与证据缺口。不因某一要件证据充分，就推定整个请求、指控或抗辩成立；\n"
    "- 严格区分五层信息：①文书直接记载；②当事人、证人或其他主体的主张与陈述；"
    "③现有证据能够支持的事实；④你的分析推断；⑤法律评价。"
    "起诉状、供述、证言和答辩意见中的内容，不得仅因有原文出处就写成已经查明的客观事实。"
    "引证通过仅表示陈述忠实于原文，不表示该陈述必然真实；\n"
    "- 对关键争点同时构造我方与对方的最强合理解释，并主动检索可能削弱或推翻当前结论的材料。"
    "区分独立印证与同源重复：多份材料转述同一来源，不当然构成相互印证。"
    "存在合理替代解释时，说明现有证据为何能够或不能够排除它；\n"
    "- 分析证明力时，根据材料考察来源、形成过程、原件或复制件状态、陈述前后变化、"
    "陈述人与案件的利害关系、电子数据完整性以及证据之间是否真正独立。"
    "真实性无争议不等于证明目的成立，证据数量也不等于证明力；\n"
    "- 可以运用法律知识发现候选罪名、请求权基础、抗辩与程序问题，但不得用法律知识虚构案件事实。"
    "正式法律结论应核验法律依据的适用范围与时效性并注明法源；未核验的明确标为待核实；\n"
    "- 结论按对案件结果的影响排序，而不是按文档顺序堆砌。关键结论应说明最有力的依据、"
    "最强反驳、剩余不确定性，以及什么补充材料或事实确认可能改变结论。"
    "证据允许时可以给出倾向性判断，不得以「需律师判断」代替实质分析。"
)

_CRIMINAL_REVIEW_HINT = (
    "刑事案件分析侧重：围绕具体指控与程序阶段选择问题，不机械罗列清单。"
    "重点检验犯罪构成要件、主观故意或特定目的、行为与结果的因果关系、数额与既未遂、"
    "共同犯罪地位等真正存在争议的事项；同时构造无罪、罪轻、此罪与彼罪等可成立的竞争性评价。"
    "证据分析应区分证据能力与证明力，关注非法证据排除线索及合理怀疑是否被排除。"
    "定罪事实、程序事实与量刑事实分别分析，不以定罪争议掩盖独立的量刑从宽线索。"
)

_CIVIL_REVIEW_HINT = (
    "民商事案件分析侧重：以诉讼地位、具体诉讼请求和程序阶段为起点，识别请求权基础与法律关系，"
    "逐要件检验举证责任、支持证据、对方抗辩及抗辩的证明命题；涉及责任承担时，分别检验违约或侵权要件、"
    "损失、因果关系、过错与责任范围。主动审查时效、期间、管辖、主体适格等前置问题，"
    "并检查主位与备位主张在事实和法律上的相容性，不机械拼凑多套方案。"
)

_CASE_TYPE_HINTS = {
    "criminal": _CRIMINAL_REVIEW_HINT,
    "civil": _CIVIL_REVIEW_HINT,
    "commercial": _CIVIL_REVIEW_HINT,
}


# 阅卷预设交付标准（PRD 13 四类功能入口）：用户从初始页选中卡片时随任务传入
# workflowPreset，此处把它翻译成「必须交付哪些内容」，而不是固定执行步骤——
# 执行顺序与工具选择仍由模型自主决定，与 AGENTS.md 的 Agent 自主性原则一致。
# 四个预设共用律师阅卷的执业标准：产出要经得起对方质证和法官追问。
_PRESET_BASELINE = (
    "预设交付标准（律师阅卷实务，本次任务必须遵守）：\n"
    "- 先出卷宗地图：逐份列明文档名、类型、页数与角色（程序文书/证据/附属材料），"
    "并单列完整性缺口（如缺起诉状、送达回证、庭审笔录、鉴定意见原件）——"
    "不能只汇报已有材料；\n"
    "- 深读原文再下结论：不得凭标题、目录或摘要推断内容；大卷宗按文档或主题分批读，"
    "每批带出处的事实先存入案件记忆再汇总；\n"
    "- 严格区分「材料记载」与「分析意见」：前者逐字可回原文并标注《文档名》P页码，"
    "后者注明推断依据，两者不得混写成一段；\n"
    "- 站在委托方立场，但有利与不利必须同时呈现，不得为结论好看隐去不利事实；\n"
    "- 材料互相矛盾时两侧说法与原出处并列保留，交律师判断，不自行取舍；\n"
    "- 按案件类型与程序阶段主动核查真正相关的程序事项：诉讼时效/追诉时效、管辖、举证期限、"
    "上诉期、送达、主体适格；发现期限或程序风险要在开头显著提示。"
    "期限计算须分别列明起算事实与法律依据，任一缺失都不得推算出确定截止日；\n"
    "- 材料未见记载的一律写「待核实」并说明需补什么材料，不编造当事人、金额、日期、法条；\n"
    "- 收尾给出可执行的工作清单：谁来做、做什么、依据哪一页、何时前完成。"
)

_PRESET_HINTS: dict[str, str] = {
    "review_analysis": (
        "本次为完整阅卷分析，交付一份可直接用于办案的阅卷报告，至少覆盖：\n"
        "- 案件概览：案由、当事人及诉讼地位、当前程序阶段、我方是谁；\n"
        "- 当事人关系：主体身份、相互关系，是否存在主体不适格或漏列当事人；\n"
        "- 时间线：程序事件（立案/送达/开庭/举证期满）与事实事件分组排列，逐一标注出处；\n"
        "- 争议焦点：每个焦点写清双方主张、我方立场、举证责任由谁承担；\n"
        "- 事实与证据对应：每条关键事实→支撑证据→原文页码；无证据支撑的事实必须标出；\n"
        "- 矛盾与疑点：金额、日期、主体名称、前后陈述不一致，两侧说法并列保留；\n"
        "- 缺失材料与补证建议：缺哪份、为什么需要、通过什么途径取得；\n"
        "- 法律适用初判：可依据法律知识提出候选依据，正式引用须核验适用范围与时效性并注明法源，"
        "未核验的标「待核实」，不臆造法条；\n"
        "- 下一步工作清单与对应期限；\n"
        "- 阅卷覆盖率：已读文档与页数、未读部分及原因（解析失败、低置信度 OCR、缺页）。\n"
        "呈现方式：上述案件概览/当事人/时间线/争议焦点/事实与证据对应/矛盾/缺失材料/"
        "工作清单以 Markdown 表格逐项列出（列数 2-6、单元格短句、出处与状态独立成列），"
        "表格之间的分析与理由用简短段落说明。"
    ),
    "visualization": (
        "本次为可视化阅卷，必须交付可交互图形而不是文字或表格描述，至少包含：\n"
        "- 时间轴：程序事件与事实事件同轴，节点标注日期与《文档名》P页码；"
        "只能确定月份时不补具体日，区间写「2023-03~2023-04」；\n"
        "- 主体关系图：当事人、代理人、证人、关联公司及其关系性质，边上写关系类型；\n"
        "- 资金或证据流转图：仅在材料确有款项流向或证据形成链条时生成，"
        "金额与节点必须来自原文；材料没有就明确说「材料中未见资金流转记载」；\n"
        "- 每张图必须 render_html 成功返回后才可声称已生成，不得以文字代替出图；\n"
        "- 材料未记载的关联不画；确需提示的用虚线并标注「待核实」，不编造关系；\n"
        "- 图后附两三句实务解读：谁在何时做了什么、对我方有何影响，不要只念节点；\n"
        "- 同一人物或机构合并为一个节点，不按文档重复出现。"
    ),
    "evidence_analysis": (
        "本次为证据分析，按「争点—证据」组织，交付：\n"
        "- 证据卡：逐份列明名称、类型、来源、形成时间、提供方、保管情况、证明目的、"
        "关键原文片段（逐字摘录）与页码；材料未记载的字段留空，不臆造；\n"
        "- 三性与证明力：真实性/合法性/关联性/证明力分别评述，与材料记载分开表述；"
        "无取证程序材料时合法性只能写「无法判断」；\n"
        "- 证据链：按争议焦点分组，说明支持我方主张的证据、对方可能的反证、以及缺口；\n"
        "- 争点×证据矩阵：以 Markdown 表格呈现，行=争议焦点、列=关键证据，"
        "每个单元格标明支持/反对/矛盾/缺失四种状态；\n"
        "- 瑕疵清单：复印件无原件、签名缺失、涂改、孤证、来源不明、形成时间存疑、"
        "取证程序瑕疵，逐条写明在哪一页；\n"
        "- 质证提纲：逐份证据的质证要点（打什么、依据什么、要求什么）；\n"
        "- 补强与调证建议：具体到申请调取哪份材料、申请谁出庭、是否申请鉴定或勘验；\n"
        "- 举证责任分配与举证期限提示。\n"
        "呈现方式：证据卡、瑕疵清单、质证提纲、争点×证据矩阵优先用 Markdown 表格列明"
        "（证据卡列：证据｜类型/形成时间｜来源与保管｜证明目的｜三性及证明力意见｜出处），"
        "三性评述的理由用简短段落说明。\n"
        "证明目的与三性意见必须分开写，不得把分析意见写成材料记载。"
    ),
    "strategy": (
        "本次为辩护/代理策略分析，站在委托方立场输出可执行方案：\n"
        "- 目标与底线：委托目标、可接受结果、不可接受结果、当前程序阶段的约束；\n"
        "- 争点排序：按对我方影响程度排序，说明每个争点的胜负关键是哪份证据；\n"
        "- 有利条件与不利条件：两侧完整，不利条件不得隐藏；对方可能主张与反制逐条写；\n"
        "- 方案比较：至少两套可行方案（如无罪辩护/认罪认罚、调解/判决、主位/备位请求），"
        "每套写清前提事实、法律依据（须核验并注明法源，未核验标「待核实」）、行动步骤、预期收益、"
        "风险与失败后果、对方可能的反制；仅一套可行时必须说明原因；"
        "多方案时用「方案｜前提事实｜法律依据｜行动步骤｜预期收益｜风险与反制」表格对比；\n"
        "- 证据补强与程序动作：申请调证、申请鉴定、申请证人出庭、变更强制措施等，写明时限；\n"
        "- 需律师决定的事项单列（是否认罪、是否调解、当事人风险承受度等）；\n"
        "- 不替用户选定最终方案：给出倾向与理由，但明确最终由律师决定。"
    ),
}

# 修订分叉提示（交付物迭代改写）：docgenInput.revisionDraftId 给定时追加到模式提示，
# 指导模型按「读当前稿 → 按意见改写 → 落 ai_revise 版本」的闭环执行
_REVISION_HINT = (
    "本次是文书修订任务（用户要求修改既有草稿）：先用 docgen_get_draft 读取"
    " revisionDraftId 对应草稿的当前稿全文，再按修订意见输出修订稿，"
    "最后用 docgen_revise_draft 把修订后的全文落库为新版本。"
    "本次不要新建草稿（不要调用 docgen_create_draft / docgen_render_draft）。修订纪律：\n"
    "- 意见未涉及的内容保持原文逐字不变，不做无关润色；\n"
    "- 修订只依据用户意见与案内材料；材料无法确认的新事实不编造，"
    "原有的 xx 占位符一律保留；\n"
    "- 不改文档整体结构，除非意见明确要求增删章节。\n"
    "完成标准：修订稿已落为新版本（工具返回 versionNo），系统会同步落盘带版本号的"
    " Word 草稿（气泡底部文件卡可打开）；向用户说明修改了哪些位置、为何这样改。"
)


# 引用纪律（跨模式统一）：所有阅卷分析基于案件材料，关键结论必须可回原文，
# 无来源/不确定信息明确提示核实补充——「关键结论」而非「每句话」，避免输出死板。
# memory_search 历史结论本身带来源，可作为案内事实依据；禁止的是用模型常识/训练
# 知识推断案内事实。大卷宗采用分批阅卷（任务拆细、按页码范围逐批读取原文），
# 不做一次性整卷通读——该策略实测极慢且易思考空转无产出（2026-09-25 移除）。
CITATION_DISCIPLINE = (
    "阅卷与分析纪律：\n"
    "- 案内事实与法律命题分别溯源：事实判断必须基于案件材料或用户明确确认，"
    "不得用常识或训练知识补足；法律知识可用于发现问题和形成候选解释，正式法律依据另行核验。"
    "先通过 documents_list 了解材料构成；案内事实依据优先使用 document_search / "
    "document_get_pages 读取的原文，其次是 memory_search 中带来源的历史结论。"
    "记忆与原文或其他来源冲突时回原文核对；仍无法消除的冲突继续并列保留，不自行取舍；\n"
    "- 大卷宗或需全案事实梳理时分批阅卷：按文档或主题逐份推进，用 document_get_pages "
    "按页码范围读取原文，逐批提取关键事实并用 memory_write 留档（必须附来源），再汇总作答；"
    "每批页数跨度适中，避免一次性塞入全部材料；\n"
    "- 多轮定向调查可委托 case_explore 子代理，其聚合结论用于定位证据与争点。"
    "涉及定罪、责任成立、关键金额或期限等决定性结论时，主 Agent 必须回读其指向的原文页，"
    "核对陈述主体、上下文与原文片段后再引用；简单查证仍直接用 document_search / document_get_pages；\n"
    "- 关键事实与主体陈述必须标注原文出处，格式统一为《文档名》第N页（如《起诉状》第3页）；"
    "出处证明原文确有该记载，不当然证明记载内容真实。法律命题注明法源，网络资料附链接；\n"
    "- 材料中没有或无法确认的信息，明确写「材料中未见记载，建议核实/补充」，"
    "不得编造、不得用推测冒充案内事实；网络检索结果只能作外部法源或背景参考，不是案件事实来源。"
)


def _system_prompt(state: dict[str, Any]) -> str:
    from ..workspace import now_iso

    parts = [
        "你是 LawBuddy，一个本地优先的法律 AI 桌面应用。",
        f"当前案件：「{state.get('case_title') or '未命名案件'}」",
        f"当前日期：{now_iso()[:10]}",
    ]
    if state.get("client_role"):
        parts.append(f"委托方立场：{state['client_role']}")
    mode = str(state.get("mode") or "")
    if mode == "legal_review":
        # 认知方法先于输出格式注入；案件类型决定专业分支，而不是额外角色口号。
        if state.get("case_type"):
            parts.append(f"案件类型：{state['case_type']}")
        if state.get("case_stage"):
            parts.append(f"程序阶段（案件设置）：{state['case_stage']}；如与卷宗记载不一致，应指出并请求确认")
        parts.append(_LEGAL_REASONING_CORE)
        case_hint = _CASE_TYPE_HINTS.get(str(state.get("case_type") or ""))
        if case_hint:
            parts.append(case_hint)
    hint = _MODE_HINTS.get(mode)
    if hint:
        parts.append(hint)
    if mode == "document_generation":
        # 案件上下文准备（§8.1）：先把关联案件索引摆到模型面前，再让它通读原文
        context_section = _case_context_section(state)
        if context_section:
            parts.append(context_section)
    # 交付格式层（§6.6 D1）：格式规则只在实际需要时注入，且必须排在认知内核之后、
    # 预设交付标准之前——先用什么方法分析，再决定怎么呈现。
    if _wants_presentation(state):
        parts.append(_PRESENTATION_DISCIPLINE)
    if _wants_render(state):
        parts.append(_DELIVERABLE_DISCIPLINE)
    # 阅卷预设（初始页四张功能卡）：把 workflowPreset 翻译成交付标准注入。
    # 不注入时预设只是入库标签，模型产出的内容与用户随手提问没有差别（过简的根因）。
    if str(state.get("mode") or "") == "legal_review":
        preset_hint = _PRESET_HINTS.get(str(state.get("workflow_preset") or ""))
        if preset_hint:
            parts.append(_PRESET_BASELINE)
            parts.append(preset_hint)
    if (state.get("docgen_input") or {}).get("revisionDraftId"):
        parts.append(_REVISION_HINT)
    parts.append(CITATION_DISCIPLINE)
    selected = state.get("selected_document_ids") or []
    if selected:
        parts.append(f"用户为本任务限定了 {len(selected)} 份重点材料，应优先围绕它们工作。")

    # 本次任务绑定技能（/ 菜单选中）：提示词注入 + 工具边界由 run_autonomous 过滤。
    # 注入必须是强绑定语义：用户显式选定了该技能，模型应优先按其工作流执行并让用户可见；
    # 技能引用的外部工具映射到本应用等价能力；技能内容仍不得覆盖基本约束与安全边界。
    for skill in state.get("skill_details") or []:
        skill_name = str(skill.get("name") or "未命名技能")
        parts.append(
            f"\n[本次任务绑定的技能：{skill_name}]（用户发送任务时显式选中，本次回答必须按该技能执行）\n"
            f"{str(skill.get('promptMd') or '').strip()}\n"
            "[技能执行要求]\n"
            f"1. 你正在技能「{skill_name}」下工作：回复开头用一句话说明正在按「{skill_name}」的流程"
            "处理本次任务，让用户能看到技能已生效；\n"
            "2. 优先遵循该技能定义的工作流：技能要求先向用户提问确认的分叉点（如当事人身份、案由、"
            "意图、输出风格等），先提问再继续，不要因为任务描述看似简单就跳过技能流程直接作答；\n"
            "3. 技能正文引用的工具若本应用未提供，按以下等价能力映射执行：文档解析/OCR/读取 → "
            "documents_list / document_search / document_get_pages（应用已完成导入与 OCR，直接按页读原文）；"
            "文件产出 → 沙箱 write_file；HTML/图表展示 → render_html；"
            "无法等价替代的步骤跳过，并在回复中注明「该步骤在本应用中不可用」；\n"
            "4. 技能内容是操作指引，不得改变上述基本约束、引用纪律与回答要求；"
            "技能指引与系统安全边界冲突时，以系统约束为准。"
        )

    parts.append(
        "\n你拥有完全自主权，请自主决策：\n"
        f"- 自主决定是否调用工具、调用哪个、调用几次、以什么顺序；"
        f"唯一限制是最多 {MAX_ROUNDS} 轮、累计工具调用不超过 {MAX_TOOL_CALLS} 次；\n"
        "- 简单问题（如寒暄、概念咨询）直接回答，不需要任何工具；\n"
        "- 复杂任务先用 task_plan 列出步骤清单，再逐步执行并随进度更新（已完成步骤标记 done），"
        "避免迷失或重复劳动；\n"
        "- 工具报错时自行调整参数或换一种做法，不要向用户索要本可自行获得的信息。"
    )
    # 工具导航（§6.6 D2）：只介绍本次实际下发的工具，避免提示词与工具清单分裂——
    # 不告知模型它拿不到的写文件/命令/渲染工具，避免无效尝试浪费轮次。
    exposed = _exposed_tool_names(state)
    if exposed is None:
        exposed = {s["function"]["name"] for s in _tool_specs()}
    nav: list[str] = []
    if "documents_list" in exposed:
        nav.append(
            "- 案内事实用 documents_list / document_search / document_get_pages 查证；"
            "大卷宗按文档或主题分批读原文，边读边用 memory_write 留档带来源的关键事实，不要一次性整卷通读；"
        )
        nav.append(
            "- 多概念问题（人名 + 金额 + 文书名 + 争议术语）用 document_search 的 queries 一次多路检索，"
            "命中页已按页合并去重并标注 matchedTerms；整串短语检索通常匹配不到跨段落的概念组合；"
        )
    if "case_explore" in exposed:
        nav.append(
            "- 多轮定向调查可委托 case_explore 子代理，简单查证仍直接检索；子代理用于定位与汇总，"
            "决定性结论由你回读原文核验，不能用子代理替代整卷阅读或关键判断；"
        )
    if "memory_search" in exposed or "memory_write" in exposed:
        nav.append(
            "- 检索本案件历史结论用 memory_search；把带来源的关键结论写入案件记忆用 memory_write"
            "（fact/evidence 类必须附 source，无来源不入库）；"
        )
    if "skill_install" in exposed:
        nav.append(
            "- 安装技能（用户给出本地技能目录）：用 skill_install 安装，装好即在技能列表；"
            "不要用 write_file 把技能散写到其他位置；若按技能安装说明确需落盘，"
            "必须写到工作空间的 skills/<分组>/<技能名>/SKILL.md——该目录会被自动同步进技能列表"
            "（并保存在本机设备级技能库，切换工作空间后仍在），写到别处的技能文件不会出现在列表里；"
        )
    if "write_file" in exposed:
        nav.append(
            "- 创建技能（用户要求沉淀/固化可复用能力，如\"把这个流程做成技能\"）："
            "先与用户确认技能的适用场景与触发时机，再起草 SKILL.md 落盘到"
            " 工作空间的 skills/<分组>/<技能名>/SKILL.md：frontmatter 含 name（≤60 字符）、"
            "  description（一句话说明适用场景）、可选 allowedTools 工具白名单；"
            "  正文为该技能的可复用提示词（步骤、规范、输出要求）。写完即出现在技能列表，"
            "  需在输入坞 / 菜单中选中后对后续任务生效；"
        )
    if "glob_files" in exposed or "read_file" in exposed:
        nav.append(
            "- 操作本地工作空间（应用程序安装目录）时用沙箱文件工具：glob_files 找文件、"
            "read_file 读文件、grep_search 搜内容、write_file / edit_file 写与改文件；"
        )
    if "bash" in exposed:
        nav.append(
            "- 需要运行脚本、调用 CLI 工具或执行系统命令时用 bash（在工作空间根目录执行，有超时限制）；"
            "  bash 每条命令执行前都会弹出审批卡请求用户批准——被拒绝时不要原样重试，"
            "  应换方案或向用户说明；"
        )
        # 文书生成模式走 docgen 落盘 Word，不需要这条提示；但该模式下用户若点名
        # Excel/PPT，docgen 无法产出，仍需按沙箱脚本落盘
        lowered_request = str(state.get("request") or "").lower()
        if str(state.get("mode") or "") != "document_generation" or any(
            k in lowered_request for k in ("excel", "xlsx", "pptx", "表格文件")
        ):
            nav.append(
                "- 导出 Word/Excel/PPT 文件：本应用已内置 python-docx / openpyxl / python-pptx，"
                "用 bash 跑一段 Python 脚本（或 write_file 先写脚本再执行）即可在工作空间落盘真实"
                " .docx/.xlsx/.pptx。文件写到工作空间根目录（不要放进子目录），"
                "落盘后会自动登记为可预览、可导出的产物卡片。"
                "不要因为「没有接文档生成 MCP」而回复无法生成文件，也不要只用文字让用户自己粘贴到 Word；"
                "脚本失败时读取 stderr 修正后重试。"
            )
    if "render_html" in exposed or "render_pptx" in exposed:
        nav.append("- 需要出图或演示时用 render_html / render_pptx 直接产出可预览卡片，不要用文字描述代替。")
    if nav:
        parts.append("\n工具使用要点：\n" + "\n".join(nav))
    parts.append(
        "\n回答要求：\n"
        "- 用中文，结论先行、分点论述；\n"
        "- 篇幅服从任务复杂度：选中预设的阅卷任务必须覆盖该预设的全部交付项，"
        "不得为求简短省略章节或合并要点；日常咨询仍应简短作答，不套用长模板；\n"
        "- 引用案内事实标注来源（文档名 + 页码，如（某证据卷 P12））；引用网络资料附链接；\n"
        "- 无可靠来源的事实推断要明确标为推测并提示人工核实；法律与策略判断应说明事实、法律前提及不确定性。"
    )
    return "\n".join(parts)


def _prepare_case_context(state: dict[str, Any]) -> dict[str, Any] | None:
    """文书生成「案件上下文准备」阶段（升级方案 §8.1）。

    在模型开始通读前，确定性汇总一次可审计的上下文包：关联案件元数据、
    可读材料清单（页数/解析状态/版本）、案件记忆规模。上下文包只读取元数据，
    不复制来源案件内容、不回写来源案件记忆；原文仍由模型按需 document_get_pages
    回读并逐条引证。材料版本在每次续跑时重新读取，避免把已换版的旧引用当成
    仍然有效的事实。
    """
    if str(state.get("mode") or "") != "document_generation":
        return None
    case_id = str(state.get("case_id") or "")
    if not case_id:
        return None
    try:
        from .. import cases, documents, memory

        case = cases.get_case(case_id)
        linked_id = str(case.get("linkedCaseId") or "")
        linked = cases.get_case(linked_id) if linked_id else None
        docs = documents.list_task_documents(case_id)
        own_docs = [d for d in docs if str(d.get("caseId")) == case_id]
        shared_docs = [d for d in docs if str(d.get("caseId")) != case_id]
        memories = memory.list_memories(
            case_id=case_id, status="active", limit=200,
            extra_case_ids=(linked_id,) if linked_id else (),
        )
    except Exception:
        return None
    return {
        "caseTitle": case.get("title"),
        "caseType": case.get("caseType"),
        "caseStage": case.get("stage"),
        "clientRole": case.get("clientRole"),
        "linkedCaseId": linked_id or None,
        "linkedCaseTitle": (linked or {}).get("title"),
        "ownDocuments": len(own_docs),
        "sharedDocuments": len(shared_docs),
        "totalPages": sum(int(d.get("pageCount") or 0) for d in docs),
        "parsePending": sum(1 for d in docs if str(d.get("status")) != "ready"),
        "ocrPendingPages": sum(int(d.get("ocrPendingPages") or 0) for d in docs),
        "memories": len(memories),
    }


def _case_context_section(state: dict[str, Any]) -> str | None:
    """把上下文包转成一段只读提示；无材料/无关联时明确要求按 xx 起草。"""
    ctx = state.get("case_context")
    if not isinstance(ctx, dict) or not ctx:
        return None
    lines = ["[案件上下文准备（只读，不含原文副本）]"]
    lines.append(
        f"- 本任务案件：「{ctx.get('caseTitle') or '未命名'}」"
        f"（类型：{ctx.get('caseType') or '未设置'}，阶段：{ctx.get('caseStage') or '未设置'}，"
        f"立场：{ctx.get('clientRole') or '未设置'}）"
    )
    if ctx.get("linkedCaseId"):
        lines.append(
            f"- 关联案件：「{ctx.get('linkedCaseTitle') or '已删除'}」只读共享；"
            "其材料与记忆可直接作为事实来源，但不得改写或回写该案件"
        )
    lines.append(
        f"- 可读材料：本任务 {ctx.get('ownDocuments', 0)} 份 + 只读共享 "
        f"{ctx.get('sharedDocuments', 0)} 份，共 {ctx.get('totalPages', 0)} 页"
    )
    if ctx.get("parsePending") or ctx.get("ocrPendingPages"):
        lines.append(
            f"- 注意：{ctx.get('parsePending', 0)} 份材料尚未解析完成、"
            f"{ctx.get('ocrPendingPages', 0)} 页仍在 OCR；引用这些材料前必须回读原文，"
            "无法确认的内容写 xx 并标为待核实"
        )
    if ctx.get("memories"):
        lines.append(
            f"- 案件记忆 {ctx['memories']} 条（含关联案件）：可用 memory_search 召回带来源的历史结论，"
            "与原文冲突时以原文为准"
        )
    if not (ctx.get("ownDocuments") or ctx.get("sharedDocuments")):
        lines.append("- 当前没有可读材料：不要推测案情，未知事实一律写 xx 并说明来源不足")
    lines.append(
        "- 先按要求通读与所选文书相关的材料，再起草；本段只提供索引，"
        "任何事实性结论仍须带文档、页码与原文片段"
    )
    return "\n".join(lines)


def _user_prompt(state: dict[str, Any]) -> str:
    lines = [str(state.get("request") or "")]
    extra: dict[str, Any] = {}
    if state.get("contract_input"):
        extra["合同审查输入"] = state["contract_input"]
        if state["contract_input"].get("workflowKind") == "contract_dialogue":
            from ..contract_workflow import get_checklist

            snapshot = get_checklist(state["contract_input"]["overviewId"],
                                     state["contract_input"].get("checklistSnapshotId"))
            extra["当前审查清单"] = snapshot or {"status": "未生成，不能假定已有结果"}
    if state.get("docgen_input"):
        extra["文书生成输入"] = state["docgen_input"]
    for label, payload in extra.items():
        lines.append(f"\n[{label}] {json.dumps(payload, ensure_ascii=False)}")
    return "\n".join(lines)


def _wire_messages(messages: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    """把随问附件并入首条 user 消息（仅模型请求层；messages 原序列保持纯文本）。

    - 图片附件：base64 data URL 的 image_url 内容分片（视觉模型，不走 OCR，
      不导入案件文档；agent_start 已按 MODEL_VISION_SUPPORT 门禁）；
    - 文本类附件：并入首条 user 文本并标注来源，供模型按补充材料使用。
    附件只在内存随任务传递，不进 messages / 检查点 / 会话归档；每轮模型调用
    均携带（多轮工具循环中图片保持可见，模型可持续对照）。
    """
    protected_dialogue = (
        (state.get("contract_input") or {}).get("workflowKind") == "contract_dialogue" or
        (state.get("docgen_input") or {}).get("workflowKind") == "docgen_dialogue"
    )
    if protected_dialogue:
        from .. import policy as permission, redaction
        from ..cases import require_case_mode

        case = require_case_mode(state["case_id"], state["mode"])
        masked = []
        for message in messages:
            content = message.get("content") or ""
            if not isinstance(content, str):
                raise ValueError("专属对话仅支持已导入的可核验文本材料")
            masked.append({**message, "content": redaction.apply(state["case_id"], content)["redactedText"]})
        digest = permission.payload_digest(json.dumps(masked, ensure_ascii=False))
        scope = state.get("conversation_id") or state["case_id"]
        model_id = str((state.get("model_policy") or {}).get("modelId") or model_gateway.DEFAULT_MODEL)
        decision = permission.decide(permission.CAP_NETWORK_MODEL, case["permissionPreset"])
        if decision == "deny":
            raise ValueError("严格本地任务不允许模型外发")
        if not permission.has_grant(permission.CAP_NETWORK_MODEL, state["task_id"], scope, digest):
            approval = permission.create_approval(
                state["task_id"], state["case_id"], permission.CAP_NETWORK_MODEL,
                model_gateway.provider_label(model_gateway.provider_for_model(model_id)),
                digest, "本次专属对话的脱敏请求与已读取原文",
                fallback="拒绝后保留材料与草稿，不继续模型调用", scope_id=scope,
            )
            status = permission.wait_for_decision(approval["approvalId"], state["task_id"], state.get("_cancel_check"))
            if status == "cancelled":
                raise task_store.TaskCancelled("用户停止")
            if status != "approved":
                raise ValueError("专属对话外发未授权")
            task_store.update_lifecycle(state["task_id"], "RUNNING", "dialogue")
        messages = masked
        if state.get("attachments"):
            raise ValueError("请将参考文件导入当前任务后使用，专属对话不直接外发附件")
    attachments = state.get("attachments") or []
    if not attachments:
        return messages
    imgs: list[tuple[str, str]] = []
    texts: list[str] = []
    for a in attachments:
        name = str(a.get("name") or "附件")
        mime = str(a.get("mime") or "")
        data = str(a.get("dataBase64") or "")
        if not data:
            continue
        if mime.startswith("image/"):
            imgs.append((mime, data))
        else:
            texts.append(f"\n[附件·文本] {name}\n{data}")
    if not imgs and not texts:
        return messages
    extra = ("\n\n以下为用户随本次提问附上的临时材料（未导入案件文档，仅供本轮参考）：\n" + "\n".join(texts)) if texts else ""
    out = [dict(m) for m in messages]
    for i, m in enumerate(out):
        if m.get("role") == "user":
            base = str(m.get("content") or "") + extra
            if imgs:
                parts: list[dict[str, Any]] = [{"type": "text", "text": base}]
                for mime, data in imgs:
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
                out[i] = {**m, "content": parts}
            else:
                out[i] = {**m, "content": base}
            break
    return out


def _restore_dialogue(state: dict[str, Any], value: Any) -> Any:
    if not (
        (state.get("contract_input") or {}).get("workflowKind") == "contract_dialogue" or
        (state.get("docgen_input") or {}).get("workflowKind") == "docgen_dialogue"
    ):
        return value
    from .. import redaction

    if isinstance(value, str):
        return redaction.restore(state["case_id"], value)
    if isinstance(value, list):
        return [_restore_dialogue(state, item) for item in value]
    if isinstance(value, dict):
        return {key: _restore_dialogue(state, item) for key, item in value.items()}
    return value


# ---------------------------------------------------------------------------
# 工具清单（OpenAI function 格式）与实现
# ---------------------------------------------------------------------------

def _fn(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def _tool_specs() -> list[dict[str, Any]]:
    return [
        _fn(
            "documents_list",
            "列出当前案件的全部文档（卷宗材料清单），了解案件材料构成时使用。",
            {"type": "object", "properties": {}},
        ),
        _fn(
            "document_search",
            "在当前案件卷宗内做全文检索（按页命中），返回文档名、页码与命中片段。"
            "查证某个事实、关键词、人名、金额在案内哪些位置出现时使用。"
            "复杂问题可传 queries 数组把问题拆成人名/日期/金额/文书名/争议术语多路检索，"
            "结果按页合并去重（matchedTerms 标明该页被哪些词命中），比逐个词反复搜索更省。",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或短语；与 queries 二选一"},
                    "queries": {
                        "type": "array",
                        "description": "多路检索词（2-5 个实词，如当事人姓名、金额、文书名称），按页合并去重",
                        "items": {"type": "string"},
                    },
                    "limit": {"type": "integer", "description": "返回条数上限，默认 8，最大 50"},
                },
            },
        ),
        _fn(
            "document_get_pages",
            "读取某文档指定页码范围的原文。分页无损：单次返回受字符上限约束，"
            "结果里的 nextCursor 表示还有未读完的原文，带上 charOffset 与 pageFrom/pageTo 继续读即可，"
            "不会像摘要那样被静默截断。检索命中后深入阅读原文时使用。",
            {
                "type": "object",
                "properties": {
                    "documentId": {"type": "string", "description": "文档 ID（来自 documents_list 或 document_search）"},
                    "pageFrom": {"type": "integer", "description": "起始页码（含），默认第 1 页"},
                    "pageTo": {"type": "integer", "description": "结束页码（含），默认与起始页相同"},
                    "charOffset": {
                        "type": "integer",
                        "description": "从起始页第几个字符开始读（续读时传上次的 nextCursor.charOffset），默认 0",
                    },
                    "maxChars": {
                        "type": "integer",
                        "description": f"本次最多返回的原文总字符数，默认 {TOTAL_PAGE_READ_CHARS}，上限 {TOTAL_PAGE_READ_CHARS}",
                    },
                },
                "required": ["documentId"],
            },
        ),
        _fn(
            "task_plan",
            "维护当前任务的步骤清单（会话级）：复杂任务（多步骤分析/报告生成）开始时列出 3-8 个步骤，"
            "每完成一步更新对应状态（done）。清单会持续注入你的上下文帮助跟踪进度、避免重复劳动；"
            "简单问题无需使用。",
            {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "完整步骤清单（每次全量提交，非增量）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "步骤描述（30字内）"},
                                "status": {"type": "string", "description": "pending 或 done"},
                            },
                            "required": ["text"],
                        },
                    },
                },
                "required": ["steps"],
            },
        ),
        _fn(
            "memory_search",
            "检索本案件的记忆（历史分析结论、人物、时间线、争议焦点、用户更正）。"
            "开始分析前先检索，避免重复劳动；结果均带来源（《文档名》第N页），"
            "直接引用其来源标注即可；与其他原文来源矛盾时回原文核对取舍。",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或短语"},
                    "limit": {"type": "integer", "description": "返回条数上限，默认 8"},
                },
                "required": ["query"],
            },
        ),
        _fn(
            "case_explore",
            "检索子代理：把一个需要多轮检索汇总的定向调查问题委托给子代理，"
            "它自主使用 documents_list / document_search / document_get_pages / memory_search "
            "搜读案件材料后，返回带来源与逐字短引的命题、反向或冲突材料、检索覆盖与缺口（约 1200 字内）。"
            "适合「梳理某主题的全部相关事实」「查证分散在多份材料中的问题」这类复杂调查"
            "——中间检索过程不占用你的上下文，聚合结果供你定位与核验，决定性结论仍须回读原文。"
            "简单查证（一两个关键词、读某几页）仍直接用 document_search / document_get_pages，"
            "不必委托。这不是整卷通读：子代理只围绕问题定向查证。"
            "多个彼此独立、互不影响调查方向的问题可一次用 questions 并行派发（最多 3 个，"
            "共享总预算）；共享同一未确定争点或会互相改变方向的问题应分开依次调查。",
            {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "调查问题（要查证什么、汇总什么，具体明确）"},
                    "questions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "多个相互独立的调查问题（并行执行，最多 3 个；与 question 二选一）",
                    },
                },
            },
        ),
        _fn(
            "memory_write",
            "把带来源的关键结论写入案件记忆（跨任务留存）。"
            "kind=fact/evidence/person/timeline/dispute 时必须附 source（documentId+pageNo+quote），"
            "无来源不入库；kind=note 可无来源。只写经过原文核对的内容，不要写入推测。",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "fact/evidence/person/timeline/dispute/correction/note"},
                    "key": {"type": "string", "description": "记忆键（简短标题，如「借款金额」）"},
                    "value": {"type": "string", "description": "记忆内容（一句话结论）"},
                    "source": {
                        "type": "object",
                        "description": "来源（fact/evidence 等必填）",
                        "properties": {
                            "documentId": {"type": "string"},
                            "pageNo": {"type": "integer"},
                            "quote": {"type": "string", "description": "原文逐字片段"},
                        },
                    },
                },
                "required": ["kind", "key", "value"],
            },
        ),
        _fn(
            "case_ledger",
            "维护本案的证据账本与争点账本（跨轮次、跨压缩、跨子代理保留）。"
            "evidence 用于登记关键命题及其原文短引、方向（支持/反对/背景）与所属争点，系统会立即回查"
            "该短引是否真的出现在指定页；issue 用于登记争点、请求权基础/构成要件、证明责任、"
            "竞争解释与缺口，状态 open/ready_to_synthesize/needs_human。"
            "登记后每轮会看到覆盖与未解决缺口摘要；简单问题不必使用。不要用它代替回读原文。",
            {
                "type": "object",
                "properties": {
                    "evidence": {
                        "type": "array",
                        "description": "证据条目（可只提交增量）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim": {"type": "string", "description": "简短命题（一句话）"},
                                "layer": {
                                    "type": "string",
                                    "enum": list(_EVIDENCE_LAYERS),
                                    "description": "record 文书直接记载 / assertion 主体主张 / "
                                                   "supported_fact 证据支持事实 / inference 分析推断 / legal 法律评价",
                                },
                                "direction": {
                                    "type": "string",
                                    "enum": list(_EVIDENCE_DIRECTIONS),
                                    "description": "supports 支持 / opposes 反对 / background 背景",
                                },
                                "issueId": {"type": "string", "description": "所属争点（与 issue.id 对应，可选）"},
                                "documentId": {"type": "string", "description": "来源文档 ID"},
                                "documentTitle": {
                                    "type": "string",
                                    "description": "来源文档名（与 documentId 二选一，可按标题匹配本案材料）",
                                },
                                "pageNo": {"type": "integer", "description": "页码（从 1 开始）"},
                                "quote": {"type": "string", "description": "原文逐字短引（不得改写、拼接）"},
                            },
                            "required": ["claim"],
                        },
                    },
                    "issue": {
                        "type": "array",
                        "description": "争点条目（可只提交增量）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "争点标识（简短，如「付款是否完成」）"},
                                "issue": {"type": "string", "description": "争点描述"},
                                "claimBasis": {"type": "string", "description": "请求权基础/构成要件/指控要素"},
                                "burden": {"type": "string", "description": "证明责任与证明标准"},
                                "alternatives": {
                                    "type": "array", "items": {"type": "string"},
                                    "description": "各方最强合理解释",
                                },
                                "gaps": {
                                    "type": "array", "items": {"type": "string"},
                                    "description": "缺失材料或需要用户确认的问题",
                                },
                                "status": {
                                    "type": "string", "enum": list(_ISSUE_STATUSES),
                                    "description": "open / ready_to_synthesize / needs_human",
                                },
                            },
                            "required": ["issue"],
                        },
                    },
                },
            },
        ),
        _fn(
            "contract_create_review",
            "为一份合同文档创建审查会话，返回 reviewId 与合同基本信息。",
            {
                "type": "object",
                "properties": {
                    "documentId": {"type": "string", "description": "主合同文档 ID"},
                    "reviewerRole": {"type": "string", "description": "审查方角色（如 buyer/seller/landlord/tenant/custom）"},
                    "customRole": {"type": "string", "description": "reviewerRole=custom 时的具体角色描述"},
                    "riskAppetite": {"type": "string", "description": "风险偏好（conservative/balanced/aggressive），默认 balanced"},
                    "contractType": {"type": "string", "description": "合同类型（可留空自动识别）"},
                    "transactionGoal": {"type": "string", "description": "交易目的"},
                    "governingLaw": {"type": "string", "description": "适用法律"},
                },
                "required": ["documentId"],
            },
        ),
        _fn(
            "contract_split_clauses",
            "把审查会话的主合同全文按条款切分并保存，同时完成确定性风险检查"
            "（交叉引用悬空、关键条款缺失，风险项自动绑定引证落库），"
            "返回条款清单与确定性风险清单。语义层面的深度风险分析可在此基础上继续。",
            {
                "type": "object",
                "properties": {
                    "reviewId": {"type": "string", "description": "合同审查会话 ID（来自 contract_create_review）"},
                },
                "required": ["reviewId"],
            },
        ),
        _fn(
            "contract_get_clauses",
            "分批读取审查会话已切分的合同条款全文（offset/limit 分页）。"
            "条款较多时按批读取做语义风险分析，避免一次性上下文过载。",
            {
                "type": "object",
                "properties": {
                    "reviewId": {"type": "string", "description": "合同审查会话 ID"},
                    "offset": {"type": "integer", "description": "起始条序号（从 0 开始），默认 0"},
                    "limit": {"type": "integer", "description": "本批条数上限，默认 20，最大 50"},
                },
                "required": ["reviewId"],
            },
        ),
        _fn(
            "docgen_list_templates",
            "列出可用文书模板（按案件过滤），返回模板 ID、名称、文书类型。",
            {"type": "object", "properties": {}},
        ),
        _fn(
            "docgen_create_free_draft",
            "为自由起草创建任务内参数化结构草案。缺失事实保留槽位，提纲须由用户在文书面板确认。",
            {"type": "object", "properties": {
                "docType": {"type": "string"}, "purpose": {"type": "string"},
                "headings": {"type": "array", "items": {"type": "string"}}
            }, "required": ["docType", "purpose", "headings"]},
        ),
        _fn(
            "docgen_create_draft",
            "基于模板创建文书草稿，提纲由模板章节结构自动推导写入"
            "（用户可在文书工作台继续编辑、确认与导出）。返回草稿 ID 与提纲。",
            {
                "type": "object",
                "properties": {
                    "templateId": {"type": "string", "description": "模板 ID（来自 docgen_list_templates）"},
                    "docType": {"type": "string", "description": "文书类型（如 起诉状/答辩状/律师函）"},
                    "purpose": {"type": "string", "description": "文书用途或请求事项（必填）"},
                },
                "required": ["templateId", "docType", "purpose"],
            },
        ),
        _fn(
            "docgen_render_draft",
            "为草稿填充槽位并生成正文，落盘带「草稿_」前缀的 Word 文件（普通缺失信息用 xx 占位，"
            "不追问、不编造；必要用户决策应先调用 request_user_decision 提问）。案件原文来源必须"
            "提供 documentId/pageNo/quote，系统会回查原文；用户确认与待核实信息不需要引证。",
            {
                "type": "object",
                "properties": {
                    "draftId": {"type": "string", "description": "草稿 ID（来自 docgen_create_draft / docgen_create_free_draft）"},
                    "values": {
                        "type": "array",
                        "description": "模板槽位取值（可省略：省略时按已填槽位直接渲染，未填槽位保留占位符）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "模板字段名（见模板 fields）"},
                                "value": {"type": "string", "description": "字段取值或章节正文"},
                                "sourceType": {
                                    "type": "string",
                                    "enum": ["case_evidence", "user_confirmed", "pending_verify"],
                                    "description": "来源：案件原文 / 用户确认 / 待核实（默认待核实）",
                                },
                                "documentId": {"type": "string", "description": "案件原文来源文档 ID"},
                                "pageNo": {"type": "integer", "description": "案件原文页码（从 1 开始）"},
                                "quote": {"type": "string", "description": "案件原文中被摘录的逐字片段（用于引语回查）"},
                            },
                            "required": ["name", "value"],
                        },
                    },
                },
                "required": ["draftId"],
            },
        ),
        _fn(
            "request_user_decision",
            "就文书生成中的必要决策向用户提问并暂停任务，等待回答后从同一检查点继续。"
            "只问会改变下一步生成行为的问题（诉讼立场、诉讼地位、主位/备位请求、辩护方向、"
            "是否认罪、是否调解、请求金额取舍、是否采用某项敏感事实等）；"
            "普通缺失信息（姓名、证件号、地址、日期、金额、案号、法院、联系方式）先自行从材料提取，"
            "确认不了就直接写 xx，不要提问；不因模板有空字段提问，不重复询问已回答的问题；"
            "一次最多 3 个问题，每个问题需说明为什么现在需要决定。调用后本轮立即结束。",
            {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "卡片顶部上下文标题（如「文书生成需要你的决策」）",
                    },
                    "contextDigest": {
                        "type": "string",
                        "description": "一句话说明当前进展与为何需要决策（如「已通读卷宗，需确认诉讼立场」）",
                    },
                    "questions": {
                        "type": "array",
                        "description": "需要用户决策的问题（1-3 个）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "问题 ID（同一批次内唯一）"},
                                "title": {"type": "string", "description": "问题标题（具体、可回答）"},
                                "reason": {"type": "string", "description": "为什么现在需要决定"},
                                "kind": {
                                    "type": "string",
                                    "enum": ["single_choice", "multi_choice", "text"],
                                    "description": "单选 / 多选 / 文本",
                                },
                                "affects": {"type": "string", "description": "影响的文书部分（如「诉讼请求」）"},
                                "allowOther": {"type": "boolean", "description": "允许「其他」自由输入"},
                                "allowDefer": {"type": "boolean", "description": "允许「暂不决定」"},
                                "required": {"type": "boolean", "description": "是否必须回答（默认是）"},
                                "options": {
                                    "type": "array",
                                    "description": "选择题选项（2-6 个，整行可点击）",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "label": {"type": "string", "description": "短标题"},
                                            "description": {"type": "string", "description": "选择该选项的后果（一行）"},
                                            "recommended": {"type": "boolean", "description": "是否为推荐项（至多一个）"},
                                            "recommendationReason": {"type": "string", "description": "推荐依据"},
                                        },
                                        "required": ["id", "label"],
                                    },
                                },
                            },
                            "required": ["id", "title", "kind"],
                        },
                    },
                },
                "required": ["questions"],
            },
        ),
        _fn(
            "docgen_get_draft",
            "读取文书草稿的当前稿全文与状态（修订任务第一步：获取修订基准正文）。",
            {
                "type": "object",
                "properties": {
                    "draftId": {"type": "string", "description": "草稿 ID（修订任务中为用户指定的 revisionDraftId）"},
                },
                "required": ["draftId"],
            },
        ),
        _fn(
            "docgen_revise_draft",
            "把修订后的文书全文落库为新版本（kind=ai_revise，历史版本保留，用户可在版本历史"
            "比较/恢复）。必须传修订后的完整正文；意见未涉及的内容应保持原文逐字不变。",
            {
                "type": "object",
                "properties": {
                    "draftId": {"type": "string", "description": "草稿 ID（与 docgen_get_draft 读取的一致）"},
                    "instruction": {"type": "string", "description": "用户的修订意见（原样传入，供版本历史回显）"},
                    "revisedBody": {"type": "string", "description": "修订后的文书全文（Markdown）"},
                    "baseVersionNo": {
                        "type": "integer",
                        "description": "docgen_get_draft 返回的 currentVersionNo；用于防止旧稿覆盖用户新编辑的稿",
                    },
                },
                "required": ["draftId", "instruction", "revisedBody"],
            },
        ),
        # 技能管理三件套（REQ-AI-003）：随全量工具一起注册
        _fn(
            "skill_install",
            "从本地技能目录安装技能。支持标准 SKILL.md 提示词包；"
            "用户在消息里给出本地路径时使用；安装成功后即可在后续任务中启用。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "技能目录或 SKILL.md 的本地路径"},
                },
                "required": ["path"],
            },
        ),
        _fn(
            "skill_list",
            "列出已安装的技能（名称、描述、允许工具与启用状态）。",
            {"type": "object", "properties": {}},
        ),
        _fn(
            "skill_remove",
            "删除一个已安装的技能。用户明确要求卸载/删除某技能时使用。",
            {
                "type": "object",
                "properties": {
                    "skillId": {"type": "string", "description": "技能 ID（来自 skill_list）"},
                },
                "required": ["skillId"],
            },
        ),
        # 沙箱工具六件套：本地工作空间文件操作 + 受审批的 Shell 执行
        _fn(
            "read_file",
            "读取本地工作空间内某个文本文件的内容（带行号）。分析用户工作空间里的代码、文档、清单等文件时使用；只允许访问工作空间目录内。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（工作空间内的相对路径或绝对路径）"},
                    "offset": {"type": "integer", "description": "起始行号（从 1 开始），默认 1"},
                    "limit": {"type": "integer", "description": "读取行数上限，默认 500，最大 1000"},
                },
                "required": ["path"],
            },
        ),
        _fn(
            "write_file",
            "在本地工作空间内创建或覆盖文件。需要生成新文件、把整理结果落盘时使用；已有文件的局部修改优先用 edit_file。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径（工作空间内）"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        ),
        _fn(
            "edit_file",
            "对本地工作空间内的文件做精确替换修改：oldString 必须与文件内容逐字符一致（含缩进换行）且唯一，否则报错。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径（工作空间内）"},
                    "oldString": {"type": "string", "description": "要替换的原文片段（需唯一，必要时扩大上下文）"},
                    "newString": {"type": "string", "description": "替换后的内容"},
                    "replaceAll": {"type": "boolean", "description": "true 时替换全部出现，默认 false"},
                },
                "required": ["path", "oldString", "newString"],
            },
        ),
        _fn(
            "glob_files",
            "在本地工作空间内按通配符查找文件（支持 ** 递归），按修改时间新到旧排序。寻找某类文件（如 *.docx、*.py）时使用。",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "通配符模式，如 **/*.py 或 *.md，默认 **/*"},
                    "path": {"type": "string", "description": "起始目录（工作空间内相对路径），默认工作空间根目录"},
                },
            },
        ),
        _fn(
            "grep_search",
            "在本地工作空间内的文本文件里按正则检索内容（返回 文件:行号:内容）。查某个关键词/函数/条款出现在哪些文件时使用。",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "path": {"type": "string", "description": "起始目录（工作空间内），默认根目录"},
                    "glob": {"type": "string", "description": "文件名过滤通配符，如 *.md"},
                    "ignoreCase": {"type": "boolean", "description": "忽略大小写，默认 false"},
                },
                "required": ["pattern"],
            },
        ),
        _fn(
            "bash",
            "在工作空间根目录执行 Shell 命令（如运行脚本、调用 CLI 工具）。每条命令执行前会弹出审批卡请用户批准，批准后才会执行；被拒绝时不要原样重试。",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令（在工作空间根目录下运行）"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 30，最大 120"},
                },
                "required": ["command"],
            },
        ),
        _fn(
            "render_html",
            "把一份完整 HTML 文档渲染为用户可见的交互预览卡片（自动展示在正文预览区、最终回答下方，支持全屏与查看源码）。需要向用户展示可视化结果（如图表、时间轴、表格、交互组件）时使用；HTML 会以沙箱 iframe 运行。单次上限约 200KB。"
            "表述要求：回答正文中不要用方位词描述卡片位置（如「见上方卡片」「见下图」），直接介绍可视化内容即可，卡片会自动出现在回答下方。"
            "未成功调用本工具（未收到 ok 结果）时，禁止声称图表/可视化「已生成」；返回报错时先精简内容重试。"
            "重要约束：文档必须完全自包含——所有 CSS 与 JS 一律内联，严禁引用任何外部资源（CDN、<script src>、<link href>、<img src> 外网图片、字体等）。"
            "沙箱会拦截全部外部请求，引用外链会导致页面空白。图表请用内联 SVG、Canvas 手绘或纯 CSS 实现，不要引入 ECharts/Chart.js 等外部库。",
            {
                "type": "object",
                "properties": {
                    "html": {
                        "type": "string",
                        "description": "完整 HTML 文档（含 <!DOCTYPE html>）。必须自包含：样式写进 <style>、脚本写进 <script>（均内联），严禁出现任何 http(s):// 或 // 开头的外部资源引用；图表用内联 SVG/Canvas/纯 CSS 实现",
                    },
                    "title": {"type": "string", "description": "预览卡片标题，如「案件时间轴」"},
                },
                "required": ["html"],
            },
        ),
        _fn(
            "render_pptx",
            "制作 PPT/幻灯片/演示文稿时必须使用本工具：输出真正可编辑的 .pptx 文件，"
            "每页由原生文本框、矩形、线条、表格组成（PowerPoint/WPS 中可直接改字、移动，不是图片）。"
            "不要用 render_html 或 HTML 网页模拟 PPT。"
            "排版要求：画布 1280×720（16:9），坐标单位 px，元素按数组顺序叠加；"
            "颜色 #RRGGBB；中文自动使用微软雅黑，无需指定字体；"
            "每页信息量适中（要点 ≤6 条），正文字号不小于 18。元素类型：\n"
            "① 文本 {type:\"text\",x,y,w,h,text,size,bold?,color?,align?:\"left|center|right\",lineSpacing?}，text 内用 \\n 分段；\n"
            "② 矩形 {type:\"rect\",x,y,w,h,fill,radius?}（色块/卡片/背景条，文字要单独用 text 元素叠放）；\n"
            "③ 直线 {type:\"line\",x1,y1,x2,y2,color,thickness?}；\n"
            "④ 表格 {type:\"table\",x,y,w,colWidths:[相对列宽],rows:[{cells:[...],header?:true,fill?}],rowHeight?,size?,color?}。\n"
            "页面呈现案件事实时在文本中标注来源（《文档名》第 N 页），材料未记载的内容不得写成确定事实。"
            "最多 30 页；文件在任务结束时自动登记为产物卡片。",
            {
                "type": "object",
                "properties": {
                    "slides": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "background": {"type": "string", "description": "整页背景色 #RRGGBB，缺省白色"},
                                "elements": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                    "description": "页面元素数组（text/rect/line/table），按绘制顺序叠加",
                                },
                            },
                            "required": ["elements"],
                        },
                        "description": "每页一个对象 {background?, elements:[...]} 的数组，按页序排列",
                    },
                    "title": {"type": "string", "description": "演示文稿标题（用于审计与默认文件名）"},
                    "fileName": {
                        "type": "string",
                        "description": "输出文件名（不含扩展名，如「庭审汇报」），默认按时间戳命名",
                    },
                },
                "required": ["slides"],
            },
        ),
    ]


def _parse_explore_payload(conclusion: str, question: str) -> dict[str, Any]:
    """解析子代理结论中的结构化部分（§6.5）。

    优先取 ```json 代码块（findings/conflicts/unresolved/suggestedChecks）；
    模型未给出或格式不合法时退化为确定性行解析（带《文档名》第N页 + 「短引」的行），
    并标记 parsed=False——结构化失败不影响主 Agent 拿到可复核的候选条目。
    """
    from .verify import _CITE_RE, _explicit_quote

    payload: dict[str, Any] = {
        "findings": [], "conflicts": [], "unresolved": [], "suggestedChecks": [],
        "parsed": False,
    }
    block = re.search(r"```json\s*(\{.*?\})\s*```", conclusion or "", re.DOTALL)
    if block:
        try:
            obj = json.loads(block.group(1))
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            for raw in obj.get("findings") or []:
                if not isinstance(raw, dict):
                    continue
                claim = str(raw.get("claim") or "").strip()
                quote = str(raw.get("quote") or "").strip()
                if not claim:
                    continue
                direction = str(raw.get("direction") or "supports").strip()
                if direction not in _EVIDENCE_DIRECTIONS:
                    direction = "supports"
                layer = str(raw.get("layer") or "record").strip()
                if layer not in _EVIDENCE_LAYERS:
                    layer = "record"
                try:
                    page_no = int(raw.get("pageNo"))
                except (TypeError, ValueError):
                    page_no = None
                payload["findings"].append({
                    "claim": claim[:200], "quote": quote[:300],
                    "documentTitle": str(raw.get("documentTitle") or "").strip() or None,
                    "pageNo": page_no, "direction": direction, "layer": layer,
                })
            for key in ("unresolved", "suggestedChecks"):
                payload[key] = [str(x)[:160] for x in (obj.get(key) or []) if str(x).strip()][:10]
            for raw in obj.get("conflicts") or []:
                if not isinstance(raw, dict):
                    continue
                issue = str(raw.get("issue") or "").strip()
                if issue:
                    payload["conflicts"].append({
                        "issue": issue[:160],
                        "sides": [str(s)[:120] for s in (raw.get("sides") or []) if str(s).strip()][:4],
                    })
            payload["parsed"] = bool(payload["findings"] or payload["conflicts"]
                                     or payload["unresolved"] or payload["suggestedChecks"])
    if payload["findings"]:
        return payload
    # 退化路径：从正文行里取「《文档名》第N页 + 紧跟逐字短引」的命题
    for line in (conclusion or "").splitlines():
        m = next(_CITE_RE.finditer(line), None)
        if not m or not m.group(2):
            continue
        quote = _explicit_quote(line, m.end())
        if not quote:
            continue
        claim = line.strip()[:200]
        direction = "opposes" if any(k in claim for k in ("反对", "矛盾", "相反", "抗辩", "不利")) \
            else "supports"
        payload["findings"].append({
            "claim": claim, "quote": quote[:300],
            "documentTitle": m.group(1).strip(), "pageNo": int(m.group(2)),
            "direction": direction, "layer": "record",
        })
    return payload


# 子代理预算记账锁（跨任务共享；只在计数读写期间持锁，不覆盖模型调用）
_EXPLORE_BUDGET_LOCK = threading.Lock()


def _explore_reserve(state: dict[str, Any], count: int) -> list[int]:
    """在共享总预算内为若干独立调查预留轮数（§6.5）。

    预留而非按实际消耗临时扣减，是为了让并行分支不会各自看到"剩余预算充足"
    而合计超支；实际消耗在子代理结束时回填（预留额度按实际返还）。
    预算不足时返回空列表，调用方据此拒绝派发并要求主 Agent 直接检索。
    """
    with _EXPLORE_BUDGET_LOCK:
        used = int(state.get("_explore_rounds_used") or 0)
        reserved = int(state.get("_explore_rounds_reserved") or 0)
        remaining = max(0, EXPLORE_TOTAL_ROUND_BUDGET - used - reserved)
        if remaining <= 0 or count <= 0:
            return []
        per = min(CASE_EXPLORE_MAX_ROUNDS, max(1, remaining // count))
        caps = [per] * count  # 均分：避免先到者独占，独立调查都能起步
        state["_explore_rounds_reserved"] = reserved + sum(caps)
        return caps


def _explore_release(state: dict[str, Any], cap: int, actual: int) -> None:
    """结算一次（或一批）预留：返还未用额度，累计实际消耗。"""
    with _EXPLORE_BUDGET_LOCK:
        reserved = int(state.get("_explore_rounds_reserved") or 0)
        state["_explore_rounds_reserved"] = max(0, reserved - cap)
        state["_explore_rounds_used"] = int(state.get("_explore_rounds_used") or 0) + actual


def _run_case_explore_many(
    state: dict[str, Any], questions: list[str], call_id: str,
) -> dict[str, Any]:
    """并行执行若干互不依赖的调查问题（§6.5 §10.3 C6）。

    只并行彼此独立的调查：同一问题共享缓存不重复派发；每个子代理仍受单次
    轮数上限与共享总预算约束；任一分支取消即整体取消（cancel_check 透传）。
    """
    from concurrent.futures import ThreadPoolExecutor

    unique: list[str] = []
    for q in questions:
        q = str(q or "").strip()
        if q and q not in unique:
            unique.append(q)
    if not unique:
        return {"error": "questions 不能全为空：请给出具体明确的调查问题"}
    unique = unique[:3]  # 单次并行上限：更多独立调查应分批推进，避免成本与上下文失控
    caps = _explore_reserve(state, len(unique))
    if not caps:
        with _LEDGER_LOCK:
            _efficiency(state)["subagents"]["denied"] += 1
        return {"error": f"检索子代理总预算（{EXPLORE_TOTAL_ROUND_BUDGET} 轮）已用尽。"
                         "请直接用 document_search / document_get_pages 完成剩余查证，"
                         "无法查证的内容明确标为待核实。"}
    results: list[dict[str, Any] | None] = [None] * len(unique)
    cancelled: list[BaseException] = []

    def _one(idx: int, question: str, cap: int) -> None:
        try:
            results[idx] = _run_case_explore(state, question, call_id, round_cap=cap, reserved=True)
        except task_store.TaskCancelled as exc:  # 取消必须沿栈穿透到主循环
            cancelled.append(exc)
        except Exception as exc:  # 单个分支失败不影响其他独立调查
            results[idx] = {"question": question, "error": f"子代理执行失败：{exc}"}

    with ThreadPoolExecutor(max_workers=len(unique), thread_name_prefix="explore") as pool:
        futures = [pool.submit(_one, i, q, caps[i]) for i, q in enumerate(unique)]
        for f in futures:
            f.result()
    if cancelled:
        raise cancelled[0]
    return {
        "parallel": len(unique),
        "results": [r for r in results if isinstance(r, dict)],
        "mergeHint": "每个分支的 findings 都只是候选：需用 case_ledger 逐条提交并由系统回查原文；"
                     "不同分支指向同一来源时属于同源重复，不得当作独立印证。",
    }


def _run_case_explore(
    state: dict[str, Any], question: str, call_id: str,
    *, round_cap: int | None = None, reserved: bool = False,
) -> dict[str, Any]:
    """case_explore 工具实现：检索子代理（Trae Search Agent 对应物）。

    - 主模型把定向调查问题委托给本循环：子代理只用检索类工具
      （documents_list / document_search / document_get_pages / memory_search）
      多轮自主搜读，最终返回带来源的聚合结论 + 结构化命题候选；
    - 主上下文只收结论（截断 CASE_EXPLORE_CONCLUSION_CHARS），中间检索
      结果不进主上下文——这是与主循环直接检索的核心区别；
    - 结构化返回（§6.5）：findings 只是**候选**，携带文档名/页码/短引/方向/层次；
      主 Agent 必须用 case_ledger 提交，由 _record_evidence 重新回查原文后才可能
      成为 supported。子代理的自然语言结论永远不会直接变成已支持事实。
    - 预算与复用（§6.5 §8.3）：同一任务内所有子代理共享
      EXPLORE_TOTAL_ROUND_BUDGET；相同调查问题直接返回缓存结果，不重复搜读；
    - 硬护栏（防整卷通读回潮）：低思考档 + 单次轮数上限 + 提示词限定「只查证」；
    - 进度可见：每次内部工具调用后经 explore_progress 事件外发（挂到
      外层工具条目 callId 上，UI 显示「已读取 N 个文件 · 检索 M 次」）；
    - 用量/取消：usage 并入主任务 usage_total 随主任务一次统计；
      cancel_check 透传，用户停止时 TaskCancelled 沿栈穿透。
    """
    if not question:
        return {"error": "question 不能为空：请给出具体明确的调查问题"}
    cache: dict[str, Any] = state.setdefault("_explore_cache", {})
    cached = cache.get(question)
    if isinstance(cached, dict):
        # 同一调查问题不重复派发子代理（§8.3 以问题为键复用子调查结果）
        with _LEDGER_LOCK:
            _efficiency(state)["subagents"]["cached"] += 1
        return {**cached, "cached": True,
                "note": "该调查问题本次任务内已查过，结果来自缓存；如需补充请直接检索/读页。"}
    if reserved:
        # 并行分支的额度由 _run_case_explore_many 统一预留，这里只消费
        cap = max(1, int(round_cap or 1))
    else:
        caps = _explore_reserve(state, 1)
        if not caps:
            with _LEDGER_LOCK:
                _efficiency(state)["subagents"]["denied"] += 1
            return {"error": f"检索子代理总预算（{EXPLORE_TOTAL_ROUND_BUDGET} 轮）已用尽。"
                             "请直接用 document_search / document_get_pages 完成剩余查证，"
                             "无法查证的内容明确标为待核实。"}
        cap = caps[0]
    round_cap = cap
    with _LEDGER_LOCK:
        _efficiency(state)["subagents"]["dispatched"] += 1
    emit = state.get("_emit") or (lambda *_args, **_kw: None)
    usage_total = state.get("_usage_total")
    if not isinstance(usage_total, dict):
        usage_total = {}
        state["_usage_total"] = usage_total
    cancel_check = state.get("_cancel_check") or (lambda: False)

    def _add_usage(usage: dict[str, Any]) -> None:
        usage_total["promptTokens"] = usage_total.get("promptTokens", 0) + int(usage.get("promptTokens") or 0)
        usage_total["completionTokens"] = usage_total.get("completionTokens", 0) + int(usage.get("completionTokens") or 0)

    policy = state.get("model_policy") or {}
    model_id = str(policy.get("modelId") or model_gateway.DEFAULT_MODEL)
    # 调查子代理属提取式任务（§6.6 第 5 条 D3）：定向检索与汇总不需要综合档推理，
    # 按模型能力取最低档（不支持的模型不下发档位参数）
    effort = model_gateway.weakest_reasoning_effort(model_id)
    allowed = {"documents_list", "document_search", "document_get_pages", "memory_search"}
    sub_tools = [s for s in _tool_specs() if s["function"]["name"] in allowed]

    sub_messages: list[dict[str, Any]] = [
        {"role": "system", "content": (
            "你是案件检索子代理，负责按问题定向查证案件材料并汇总结论。遵守：\n"
            "- 可用工具：documents_list（材料构成）、document_search（全文检索，"
            "多概念问题用 queries 一次多路检索）、document_get_pages（读原文页）、memory_search（历史结论）；\n"
            "- 只查证与汇总，不展开法律分析或替主 Agent 作事实真实性判断；自主决定检索词与读取范围，"
            f"本次最多 {round_cap} 轮，尽快收敛；\n"
            "- 对核心命题同时查找支持、反对或冲突材料；单次检索无命中不得断言全案不存在；\n"
            "- 先给人类可读汇总（命题与材料记载 / 反向或冲突材料 / 检索覆盖与缺口，1200 字内），"
            "正文每条都带《文档名》第N页与逐字原文短引并标明陈述主体，不得改写、拼接引文；"
            "确实未查到的写「本次检索范围内未见记载」，不得编造；\n"
            "- 汇总末尾必须附一个 ```json 代码块，供主 Agent 逐条复核后合并，格式：\n"
            '{"findings":[{"claim":"命题","documentTitle":"文档名","pageNo":1,'
            '"quote":"该页逐字短引","direction":"supports|opposes|background",'
            '"layer":"record|assertion|supported_fact|inference|legal"}],'
            '"conflicts":[{"issue":"冲突点","sides":["一方原文短引","另一方原文短引"]}],'
            '"unresolved":["未解决问题"],"suggestedChecks":["建议主 Agent 下一步核查的事项"]}\n'
            "findings 只列你能给出页码与逐字短引的条目；推断与法律评价放 layer=inference/legal 且不得伪造引文。"
        )},
        {"role": "user", "content": question},
    ]

    read_docs: set[str] = set()
    searches = 0
    rounds = 0
    conclusion = ""
    try:
        for round_no in range(1, round_cap + 1):
            if cancel_check():
                raise task_store.TaskCancelled("用户停止")
            rounds = round_no
            result = model_gateway.chat_with_tools(
                sub_messages, tools=sub_tools, model=model_id,
                cancel_check=cancel_check, reasoning_effort=effort,
            )
            _add_usage(result.get("usage") or {})
            content = str(result.get("content") or "")
            tool_calls = list(result.get("toolCalls") or [])
            if not tool_calls:
                conclusion = content
                break
            sub_messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                tc_args = tc.parsed_arguments or {}
                if tc.name == "document_get_pages":
                    doc_id = str(tc_args.get("documentId") or "")
                    if doc_id:
                        read_docs.add(doc_id)
                    # 子代理工具结果上限 4000 字符：压低单次读取量，保证游标与
                    # nextCursor 说明不被外层截断（模型可续读，不丢原文）
                    tc_args["maxChars"] = min(_int_arg(tc_args, "maxChars", CASE_EXPLORE_READ_CHARS),
                                              CASE_EXPLORE_READ_CHARS)
                if tc.name in ("document_search", "memory_search"):
                    searches += 1
                try:
                    output: Any = _execute_tool(state, tc.name, tc_args, origin="subagent")
                except task_store.TaskCancelled:
                    raise
                except Exception as exc:  # 子代理工具报错回灌子上下文，由其自行调整
                    output = {"error": str(exc)}
                sub_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _clip(json.dumps(output, ensure_ascii=False, default=str), 4000),
                })
                emit("explore_progress", {"callId": call_id, "reads": len(read_docs), "searches": searches})
        else:
            # 轮数用尽：追发一次无工具强制作答，保证拿到非空结论
            sub_messages.append({
                "role": "user",
                "content": "请立即停止调用工具，基于已检索信息按「命题与材料记载」「反向或冲突材料」"
                           "「检索覆盖与缺口」汇总并附约定的 ```json 结构化块；每条带来源与逐字短引，1200 字内。",
            })
            result = model_gateway.chat_with_tools(
                sub_messages, tools=None, model=model_id,
                cancel_check=cancel_check, reasoning_effort=effort,
            )
            _add_usage(result.get("usage") or {})
            conclusion = str(result.get("content") or "")

        if not conclusion.strip():
            conclusion = "（子代理未能产出结论：模型无可用输出）"
        # 共享总预算结算（§6.5）：返还未用预留额度、累计实际消耗；
        # 正常收敛与轮数用尽（含强制作答）都按实际轮次记账
        _explore_release(state, cap, rounds)
        payload = _parse_explore_payload(conclusion, question)
        # 子 Agent 采用率口径（§10.4 D4）：记录候选条目来源指纹与实际轮次，
        # 收尾时统计有多少条最终以同一出处进入证据账本
        keys = {k for k in (_subagent_finding_key(f) for f in payload["findings"]) if k}
        with _LEDGER_LOCK:
            sub = _efficiency(state)["subagents"]
            sub["rounds"] = int(sub.get("rounds") or 0) + rounds
            sub["findings"] = int(sub.get("findings") or 0) + len(payload["findings"])
            _efficiency(state)["subagentFindingKeys"].update(keys)
        result_payload = {
            "question": question,
            "conclusion": _clip(conclusion, CASE_EXPLORE_CONCLUSION_CHARS),
            "reads": len(read_docs),
            "searches": searches,
            "rounds": rounds,
            "roundsLeft": max(0, EXPLORE_TOTAL_ROUND_BUDGET - int(state.get("_explore_rounds_used") or 0)),
            "findings": payload["findings"][:20],
            "conflicts": payload["conflicts"][:8],
            "unresolved": payload["unresolved"],
            "suggestedChecks": payload["suggestedChecks"],
            "structured": payload["parsed"],
            "mergeHint": (
                "以上 findings 仅为候选：须用 case_ledger 提交（claim/documentTitle/pageNo/quote/direction），"
                "由系统回查原文后才可能成为 supported；conclusion 只是调查建议，不能直接当作已支持事实。"
            ),
        }
        cache[question] = result_payload
        return result_payload
    except task_store.TaskCancelled:
        # 取消同样结算已消耗轮次，避免预算因异常路径被永久占用
        _explore_release(state, cap, rounds)
        raise
    except Exception as exc:
        _explore_release(state, cap, rounds)
        return {"error": f"子代理执行失败：{exc}"}


def _publish_docgen_file(
    state: dict[str, Any],
    draft_id: str,
    exported: dict[str, Any],
    *,
    note: str,
) -> dict[str, Any]:
    """把任务内落盘的文书 Word 登记为 file 产物（气泡底部文件卡数据源）。

    body 只含文件名/大小/草稿标识等元数据，并带 docgenDraftId，前端据此把
    文件卡点击路由到文书工作台（预览/编辑/AI 修订），而不是普通文档预览。
    """
    from pathlib import Path

    from .. import artifacts as artifact_store
    from .. import docgen

    path = Path(str(exported.get("path") or ""))
    draft = docgen.get_draft(draft_id)
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0
    body = {
        "intro": f"{note}：{path.name}（可在右侧文书工作台预览、编辑或继续 AI 修订）",
        "fileName": path.name,
        "sizeBytes": size_bytes,
        "workspaceDirName": path.parent.name,
        "docgenDraftId": draft_id,
        "docType": draft.get("docType"),
        "draftMarked": bool(exported.get("draftMarked")),
        "versionNo": exported.get("versionNo"),
        "blockingCount": int(exported.get("blockingCount") or 0),
    }
    try:
        artifact_store.save_artifact(
            str(state.get("task_id") or ""), str(state.get("case_id") or ""),
            "file", path.name, body,
        )
    except Exception:
        pass  # 登记失败不阻断：任务收尾的工作空间扫描会兜底登记
    return {
        "fileName": path.name,
        "sizeBytes": size_bytes,
        "draftId": draft_id,
        "versionNo": exported.get("versionNo"),
        "draftMarked": body["draftMarked"],
        "blockingCount": body["blockingCount"],
    }


def _task_draft_id(state: dict[str, Any]) -> str | None:
    """本任务最近一次创建的文书草稿（问题集据此标记回答后继续的草稿）。"""
    task_id = str(state.get("task_id") or "")
    if not task_id:
        return None
    try:
        from ..storage import get_db

        row = get_db().query_one(
            "SELECT id FROM docgen_drafts WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
    except Exception:
        return None
    return str(row["id"]) if row else None


def _ensure_docgen_word_draft(
    state: dict[str, Any],
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    """文书生成收尾兜底：确保首轮产出草稿正文与 Word，并回传结构化摘要。

    - 模型未落正文时按模板+槽位确定性渲染（提纲未确认也产出草稿，占位符不编造）；
    - Word 落盘到工作空间并登记 file 产物（气泡底部文件卡，点击进文书工作台）；
    - 向会话补发 docgen body（提纲/槽位来源/缺失字段），与阅卷模式「摘要 + 文件卡」
      的气泡形态对齐；定稿/正式导出仍由用户在工作台确认。
    """
    if str(state.get("mode") or "") != "document_generation":
        return
    # 修订任务沿用既有草稿，不在此重复出稿（修订工具自身负责落盘与登记）
    if (state.get("docgen_input") or {}).get("revisionDraftId"):
        return
    task_id = str(state.get("task_id") or "")
    case_id = str(state.get("case_id") or "")
    if not task_id or not case_id:
        return
    try:
        from .. import docgen
        from ..storage import get_db

        rows = get_db().query(
            "SELECT id FROM docgen_drafts WHERE task_id = ? ORDER BY created_at DESC",
            (task_id,),
        )
    except Exception:
        return
    if not rows:
        # 模型未建草稿时的确定性兜底：有模板按模板建；只有文书类型时自由起草
        inp = state.get("docgen_input") or {}
        template_id = str(inp.get("templateId") or "")
        doc_type = str(inp.get("docType") or "").strip()
        purpose = str(inp.get("purpose") or "").strip()
        try:
            from .. import docgen

            if template_id and not doc_type:
                doc_type = str((docgen.get_template(template_id) or {}).get("docType") or "").strip()
        except Exception:
            pass
        if not purpose or not (template_id or doc_type):
            return
        try:
            from .. import docgen

            if template_id:
                draft = docgen.create_draft(
                    case_id, template_id, doc_type, purpose,
                    task_id=task_id, mismatch_confirmed=bool(inp.get("confirmMismatch")),
                )
                tpl = docgen.get_template(template_id)
                outline = docgen.outline_from_template(str(tpl.get("contentMd") or ""))
                if outline:
                    docgen.update_outline(draft["id"], outline)
            else:
                draft = docgen.create_free_draft(case_id, doc_type, purpose, task_id=task_id)
            rows = [{"id": draft["id"]}]
        except Exception:
            return
    if not rows:
        return
    draft_id = str(rows[0]["id"])
    try:
        draft = docgen.get_draft(draft_id)
    except Exception:
        return
    try:
        exists = get_db().query_one(
            "SELECT 1 FROM artifacts WHERE task_id = ? AND kind = 'file' AND body_json LIKE ?",
            (task_id, f'%"docgenDraftId": "{draft_id}"%'),
        )
    except Exception:
        exists = None
    if not exists:
        try:
            draft = docgen.get_draft(draft_id)
            if not (draft.get("bodyMd") or "").strip():
                draft = docgen.render_body(draft_id, allow_unconfirmed=True)
            exported = docgen.export_to_workspace(draft_id, "docx")
            _publish_docgen_file(state, draft_id, exported, note="文书草稿已生成")
        except Exception:
            pass
    if emit is None:
        return
    # 结构化摘要（对齐 DocGenBody 契约）：槽位来源三分类与缺失字段供用户核对
    slots = draft.get("slots") or {}
    missing: list[str] = []
    try:
        tpl = docgen.get_template(draft["templateId"]) if draft.get("templateId") else None
        # 字段全部可选：列出未填写的用户输入字段（章节正文由 AI 撰写，不计入）
        missing = [
            str(f.get("name") or "")
            for f in (tpl or {}).get("fields") or []
            if not str(f.get("name") or "").endswith("内容")
            and not str((slots.get(f.get("name")) or {}).get("value") or "").strip()
        ][:20]
    except Exception:
        pass
    body = {
        "docgen": {
            "draftId": draft_id,
            "docType": draft.get("docType"),
            "outline": draft.get("outline"),
            "outlineConfirmed": bool(draft.get("outlineConfirmed")),
            "slots": [
                {
                    "name": name,
                    "value": slot.get("value"),
                    "sourceType": slot.get("sourceType") or "pending_verify",
                    "sourceTypeLabel": docgen.SOURCE_TYPE_LABELS.get(
                        str(slot.get("sourceType") or "pending_verify")
                    ),
                    "sourceRef": slot.get("sourceRef"),
                }
                for name, slot in slots.items()
            ],
            "missingFields": missing,
        },
        "cites": [],
        "audit": "文书草稿由 AI 起草：未填写信息已用 xx 标注，未编造；"
                 "关键事实请以原文为准，定稿与正式导出前请逐项核实",
    }
    try:
        emit("message", {"body": body})
    except Exception:
        pass


def _execute_tool(
    state: dict[str, Any], name: str, args: dict[str, Any], call_id: str = "",
    *, origin: str = "main",
) -> Any:
    """执行一个工具并返回可 JSON 化的结果；异常向上抛（由主循环捕获回灌模型）。

    call_id 为当前工具调用的 ID：子代理（case_explore）需要它把内部进度
    （explore_progress）挂到外层工具条目上；其余工具忽略。
    origin 区分主 Agent 与子 Agent 发起的调用，用于效率指标归因（§10.4 D4）。
    """
    case_id = str(state.get("case_id") or "")
    _count_tool_call(state, name)

    if name == "documents_list":
        from .. import documents

        return {
            "documents": [
                {k: d[k] for k in ("id", "title", "kind", "status", "pageCount", "ocrPendingPages") if k in d}
                for d in documents.list_task_documents(case_id)
            ]
        }

    if name == "document_search":
        from .. import cases, search

        limit = _int_arg(args, "limit", 8)
        raw_terms = args.get("queries")
        terms = [str(t) for t in raw_terms if str(t or "").strip()] if isinstance(raw_terms, list) else []
        if not terms:
            query = str(args.get("query") or "")
            if not query.strip():
                return {"error": "document_search 需要 query 或 queries（至少一个检索词）"}
            return _efficiency_search(
                state, query, origin, limit,
                lambda: search.search_pages(
                    case_id,
                    query,
                    limit,
                    extra_case_ids=tuple(cases.readable_case_ids(case_id)[1:]),
                ),
            )
        extra = tuple(cases.readable_case_ids(case_id)[1:])
        return _efficiency_search_multi(
            state, terms, origin, limit,
            lambda term: search.search_pages(case_id, term, limit, extra_case_ids=extra),
        )

    if name == "document_get_pages":
        from .. import documents

        doc_id = str(args.get("documentId") or "")
        case_docs = documents.list_task_documents(case_id)
        if not any(str(d.get("id")) == doc_id for d in case_docs):
            # 可读范围门禁（05 方案 §11.5）：documentId 只能取本案或显式关联案件，
            # 不让模型凭猜测的 ID 读取其他案件的原文
            hit = next((d for d in case_docs if str(d.get("title") or "") == doc_id), None)
            hint = (
                f"documentId「{doc_id}」不在当前案件的可读材料范围内，"
                + (f"「{doc_id}」是文档标题，其真实 documentId 为 {hit['id']}，请改用后重试；"
                   if hit else "可能是把文档标题误当成了 ID，")
                + "documentId 必须使用 documents_list 或 document_search 返回的 ID"
            )
            return {"error": hint}
        pages = documents.get_pages(doc_id)
        if not pages:
            return {
                "error": f"文档 {doc_id} 尚无可读页（可能仍在解析/OCR 排队或解析失败），"
                         "请先用 documents_list 确认状态或换一份材料",
                "documentId": doc_id,
            }
        page_from = _int_arg(args, "pageFrom", 1)
        page_to = _int_arg(args, "pageTo", page_from)
        picked = [p for p in pages if page_from <= int(p.get("pageNo") or 0) <= page_to]
        if not picked:
            return {
                "error": f"页码范围 {page_from}-{page_to} 内没有内容，本文档共 {len(pages)} 页；"
                         "请用范围内的页码重试",
                "totalPages": len(pages),
            }
        result = _paginate_pages(
            doc_id,
            picked,
            total_pages=len(pages),
            char_offset=max(0, _int_arg(args, "charOffset", 0)),
            max_chars=max(1, min(TOTAL_PAGE_READ_CHARS, _int_arg(args, "maxChars", TOTAL_PAGE_READ_CHARS))),
        )
        # 阅读账本（§5.4）：记录实际提供给模型的页面与字符区间（版本取自文档活动版本）
        if "pages" in result:
            doc_meta = next((d for d in case_docs if str(d.get("id")) == doc_id), {})
            # 重复读取指标（§10.4 D4）：必须在写入本次阅读前判定，否则刚读的页也会被算作重复
            _efficiency_read(
                state, doc_id, result,
                char_offset=max(0, _int_arg(args, "charOffset", 0)), origin=origin,
            )
            _record_reading(state, doc_id, doc_meta.get("activeVersionId"), result)
        return result

    if name == "case_ledger":
        evidence = _record_evidence(state, args.get("evidence") or [])
        issues = _record_issues(state, args.get("issue") or [])
        return {
            "evidence": evidence,
            "issue": issues,
            "stats": _ledger_stats(state),
        }

    if name == "case_explore":
        questions = [str(q) for q in (args.get("questions") or []) if str(q).strip()]
        if len(questions) > 1:
            return _run_case_explore_many(state, questions, call_id)
        question = str(args.get("question") or "").strip() or (questions[0].strip() if questions else "")
        return _run_case_explore(state, question, call_id)

    if name == "task_plan":
        steps = []
        for s in args.get("steps") or []:
            if isinstance(s, dict) and str(s.get("text") or "").strip():
                steps.append({
                    "text": str(s["text"]).strip()[:60],
                    # 模型常写「已完成/completed/finished」等同义词，只认 done 会让
                    # 全部步骤退回 pending（UI 显示 0/N）；统一归一化后再判定
                    "status": "done" if str(s.get("status") or "").strip().lower() in _PLAN_DONE_VALUES
                    else "pending",
                })
        return {"plan": steps[:12]}

    if name == "memory_search":
        from .. import cases, memory
        from ..storage import get_db

        entries = memory.recall(
            case_id=case_id,
            query=str(args.get("query") or ""),
            limit=max(1, min(20, _int_arg(args, "limit", 8))),
            extra_case_ids=tuple(cases.readable_case_ids(case_id)[1:]),
        )
        # 来源解析为《文档名》第N页：条目可直接按此标注引用，无需再查文档列表
        db = get_db()
        for e in entries:
            src = e.get("source") or {}
            doc_id = src.get("documentId")
            if doc_id:
                row = db.query_one("SELECT title FROM documents WHERE id = ?", (doc_id,))
                src["documentTitle"] = (row["title"] if row else None) or doc_id
        return {"memories": entries}

    if name == "memory_write":
        from .. import memory

        src = args.get("source") if isinstance(args.get("source"), dict) else {}
        return memory.remember(
            scope="case",
            kind=str(args.get("kind") or "").strip(),
            key=str(args.get("key") or ""),
            value=str(args.get("value") or ""),
            case_id=case_id,
            task_id=state.get("task_id"),
            source={
                "documentId": str(src.get("documentId") or "") or None,
                "pageNo": src.get("pageNo"),
                "quote": src.get("quote"),
            },
        )

    if name == "contract_create_review":
        from .. import contract

        review = contract.create_review(
            case_id,
            str(args.get("documentId") or ""),
            reviewer_role=str(args.get("reviewerRole") or "custom"),
            custom_role=args.get("customRole"),
            risk_appetite=str(args.get("riskAppetite") or "balanced"),
            contract_type=args.get("contractType"),
            transaction_goal=args.get("transactionGoal"),
            governing_law=args.get("governingLaw"),
            task_id=state.get("task_id"),
        )
        return {"reviewId": review["id"], "status": review["status"], "primaryDocumentTitle": review.get("primaryDocumentTitle")}

    if name == "contract_split_clauses":
        from .. import contract
        from .. import documents
        from ..citations import judge_claim

        review = contract.get_review(str(args.get("reviewId") or ""))
        pages = documents.get_pages(review["primaryDocumentId"])
        full_text = contract.join_pages(pages)
        clauses = contract.split_clauses(full_text)
        # 交叉引用解析：悬空引用检查与 refs_json 落库均依赖该字段。
        # 页码按全文字符偏移回填——split_clauses 只切全文，不携带页码；
        # 缺失时会让所有条款级风险都落到首页，原文定位因此失准。
        for c in clauses:
            c["refs"] = contract.extract_cross_refs(str(c.get("text") or ""), str(c.get("clauseRef") or ""))
            c["pageNo"] = contract.page_of_offset(pages, full_text, int(c.get("charStart") or 0))
        saved = contract.save_clauses(review["id"], clauses)

        # 合同类型识别回填（用户未显式指定时，PRD 14.3 步骤2）
        contract_type = review.get("contractType")
        if not contract_type:
            contract_type = contract.detect_contract_type(full_text)
            if contract_type:
                contract.set_contract_type(review["id"], contract_type)

        # 确定性风险检查（悬空引用 + 关键条款缺失），逐项绑定 Claim 后落库并收尾
        # ——这是不依赖模型的确定性能力子集；语义风险由模型基于切分结果自行分析。
        raw_risks = contract.detect_deterministic_risks(clauses, full_text)[:MAX_RISK_ITEMS]
        clause_by_ref = {str(c.get("clauseRef") or ""): c for c in clauses}
        version_id = review.get("primaryVersionId")
        bound: list[dict[str, Any]] = []
        for r in raw_risks:
            item = dict(r)
            clause = clause_by_ref.get(item["clauseRef"])
            hit = None
            if clause is not None and version_id:
                hit = {
                    "documentId": review["primaryDocumentId"],
                    "versionId": version_id,
                    "pageNo": clause.get("pageNo") or (pages[0]["pageNo"] if pages else None),
                    # 用落库的原文摘录做校验，避免整条跨页条款因不全在同一页而必然失配
                    "snippet": str(item.get("originalText") or clause.get("text") or ""),
                }
            cite = judge_claim(
                task_id=str(state.get("task_id") or ""),
                case_id=case_id,
                text=(item["originalText"] or item["riskNote"])[:200],
                source_hit=hit,
            )
            if cite["claimStatus"] == "needs_human" and item["confidence"] == "verified":
                item["confidence"] = "medium"
            item["claimId"] = cite["claimId"]
            item["cite"] = cite
            bound.append(item)
        contract.save_risk_items(review["id"], bound)
        contract.update_review_status(review["id"], "completed")

        return {
            "reviewId": review["id"],
            "clauseCount": saved,
            "contractType": contract_type,
            "riskCount": len(bound),
            "risks": [
                {k: r.get(k) for k in ("riskLevel", "category", "clauseRef", "riskNote", "claimId")}
                for r in bound
            ],
            "clauses": [
                {
                    "clauseRef": c.get("clauseRef"),
                    "heading": c.get("heading"),
                    "charCount": len(str(c.get("text") or "")),
                    "preview": _clip(str(c.get("text") or ""), 200),
                }
                for c in clauses
            ],
        }

    if name == "contract_get_clauses":
        from .. import contract

        review = contract.get_review(str(args.get("reviewId") or ""))
        if review["caseId"] != case_id:
            raise contract.ContractError("审查结果不属于当前任务")
        clauses = contract.list_clauses(review["id"])
        total = len(clauses)
        offset = max(0, _int_arg(args, "offset", 0))
        limit = max(1, min(50, _int_arg(args, "limit", 20)))
        picked = clauses[offset:offset + limit]
        return {
            "reviewId": review["id"],
            "total": total,
            "offset": offset,
            "returned": len(picked),
            "clauses": [
                {
                    "clauseRef": c.get("clauseRef"),
                    "heading": c.get("heading"),
                    "pageNo": c.get("pageNo"),
                    "text": str(c.get("text") or ""),
                }
                for c in picked
            ],
        }

    if name == "docgen_list_templates":
        from .. import docgen

        return {
            "templates": [
                {
                    "id": t["id"], "title": t["title"], "docType": t["docType"],
                    "versionNo": t["versionNo"], "templateMode": t["templateMode"],
                    "fields": [f.get("name") for f in t.get("fields") or []],
                }
                for t in docgen.list_templates(case_id or None)
                if t.get("status") == "active"
            ]
        }

    if name == "request_user_decision":
        # 必要用户决策：落库问题集 + 置 WAITING_HUMAN + 下发 human_question 事件；
        # 主循环识别 paused 标记后立即结束本轮，等待用户回答后从同一检查点续跑
        from . import human_questions

        question_set = human_questions.create_question_set(
            task_id=str(state.get("task_id") or ""),
            case_id=case_id,
            conversation_id=state.get("conversation_id"),
            draft_id=_task_draft_id(state),
            title=str(args.get("title") or "文书生成需要你的决策"),
            context_digest=str(args.get("contextDigest") or ""),
            questions=args.get("questions"),
        )
        # 先下发问题卡，再进入等待状态；否则前端收到 WAITING_HUMAN 后停止订阅，
        # 会漏掉紧随其后的问题集，只显示工具参数 JSON。
        task_store.append_event(str(state.get("task_id") or ""), "human_question", {
            "questionSet": question_set,
        })
        task_store.update_lifecycle(str(state.get("task_id") or ""), "WAITING_HUMAN", "human_question")
        state["_paused_question_set"] = question_set
        return {
            "paused": True,
            "questionSetId": question_set["id"],
            "message": "已向用户提出决策问题并暂停任务，用户回答后将从当前检查点继续起草。",
        }

    if name == "docgen_create_draft":
        from .. import docgen

        tpl = docgen.get_template(str(args.get("templateId") or ""))
        if tpl["caseId"] not in (None, case_id):
            raise docgen.DocGenError("模板不属于当前任务")
        draft = docgen.create_draft(
            case_id,
            str(args.get("templateId") or ""),
            str(args.get("docType") or ""),
            str(args.get("purpose") or ""),
            task_id=state.get("task_id"),
        )
        # 提纲由模板结构确定性推导并直接写入草稿（用户可在文书工作台继续编辑确认）
        outline = docgen.outline_from_template(str(tpl.get("contentMd") or ""))
        if outline:
            draft = docgen.update_outline(draft["id"], outline)
        return {
            "draftId": draft["id"],
            "status": draft.get("status"),
            "docType": draft.get("docType"),
            "fieldNames": [f.get("name") for f in tpl.get("fields") or []],
            "outline": [{"heading": o["heading"], "points": o.get("points") or ""} for o in outline],
        }

    if name == "docgen_create_free_draft":
        from .. import docgen

        headings = args.get("headings")
        if not isinstance(headings, list) or not 1 <= len(headings) <= 20 or any(
            not isinstance(title, str) or not title.strip() or len(title) > 60 or any(c in title for c in "{}\n")
            for title in headings
        ):
            raise docgen.DocGenError("自由起草章节不合法")
        doc_type = str(args.get("docType") or "").strip()
        purpose = str(args.get("purpose") or "").strip()
        if not doc_type:
            raise docgen.DocGenError("文书类型不能为空")
        if not purpose:
            raise docgen.DocGenError("文书用途或请求事项不能为空")
        draft = docgen.create_free_draft(case_id, doc_type, purpose, headings, task_id=state["task_id"])
        return {"draftId": draft["id"], "status": "outline", "outline": draft["outline"],
                "fieldNames": [f"章节{index + 1}内容" for index in range(len(headings))],
                "nextStep": "请用 docgen_render_draft 按章节填写正文并落盘 Word 草稿"}

    if name == "docgen_render_draft":
        # AI 起草草稿：填槽位 → 渲染正文 → 落盘 Word（提纲未确认也可产出草稿；
        # 定稿与正式导出仍受 status / blocking_count 门禁约束）
        from .. import docgen

        draft_id = str(args.get("draftId") or "")
        draft = docgen.get_draft(draft_id)
        if draft["caseId"] != case_id:
            raise docgen.DocGenError("草稿不属于当前案件")
        raw_values = args.get("values")
        if raw_values is not None:
            if not isinstance(raw_values, list) or len(raw_values) > 80:
                raise docgen.DocGenError("槽位值列表不合法")
            values: list[dict[str, Any]] = []
            for item in raw_values:
                if not isinstance(item, dict):
                    raise docgen.DocGenError("槽位值不合法")
                source_type = str(item.get("sourceType") or "pending_verify")
                if source_type not in docgen.SOURCE_TYPES:
                    raise docgen.DocGenError(f"来源类型不合法：{source_type}")
                source_ref = None
                if source_type == "case_evidence" or item.get("documentId"):
                    page_no = item.get("pageNo")
                    if isinstance(page_no, str) and page_no.strip().isdigit():
                        page_no = int(page_no.strip())
                    source_ref = {
                        "documentId": str(item.get("documentId") or ""),
                        "pageNo": page_no if isinstance(page_no, int) and not isinstance(page_no, bool) else 0,
                        "quote": str(item.get("quote") or ""),
                    }
                values.append({
                    "name": str(item.get("name") or ""),
                    "value": str(item.get("value") or ""),
                    "sourceType": source_type,
                    "sourceRef": source_ref,
                })
            docgen.fill_slots(draft_id, values)
        # DOCX 自定义模板（升级方案 §6.2）：按原格式生成，保留页眉页脚/样式/表格；
        # 其余模板仍走 Markdown 正文渲染，导出时按统一版式转 Word。
        tpl = docgen.get_template(draft["templateId"]) if draft.get("templateId") else None
        if tpl and tpl.get("sourceFormat") == "docx":
            rendered = docgen.render_docx(draft_id, allow_unconfirmed=True)
        else:
            rendered = docgen.render_body(draft_id, allow_unconfirmed=True)
        exported = docgen.export_to_workspace(draft_id, "docx")
        published = _publish_docgen_file(state, draft_id, exported, note="文书草稿已生成")
        return {
            "draftId": draft_id,
            "fileName": published["fileName"],
            "versionNo": published["versionNo"],
            "draftMarked": published["draftMarked"],
            "blockingCount": rendered.get("blockingCount", 0),
            "nextStep": "Word 草稿已落盘；用户可在右侧文书工作台预览/编辑，或继续提出修改意见迭代新版本",
        }

    if name == "docgen_get_draft":
        from .. import docgen

        draft = docgen.get_draft(str(args.get("draftId") or ""))
        if draft["caseId"] != case_id:
            raise docgen.DocGenError("草稿不属于当前案件")
        return {
            "draftId": draft["id"],
            "docType": draft.get("docType"),
            "status": draft.get("status"),
            "outlineConfirmed": draft.get("outlineConfirmed"),
            # 版本锚点：修订时回传，避免基于旧稿覆盖用户在编辑期间产生的新稿
            "currentVersionNo": draft.get("currentVersionNo", 0),
            "bodyMd": draft.get("bodyMd") or "",
        }

    if name == "docgen_revise_draft":
        # 修订落库（交付物迭代改写）：修订稿追加为 kind='ai_revise' 版本，
        # 历史版本保留；守门（归属/正文存在/非空）失败抛错回灌模型。
        # 提纲确认不再是 AI 草稿迭代的前置条件——草稿态同样允许修订。
        from .. import docgen

        draft_id = str(args.get("draftId") or "")
        draft = docgen.get_draft(draft_id)
        if draft["caseId"] != case_id:
            raise docgen.DocGenError("草稿不属于当前案件")
        if not (draft.get("bodyMd") or "").strip():
            raise docgen.DocGenError("草稿尚无正文，请先用 docgen_render_draft 生成正文再修订")
        res = docgen.revise_body(
            draft_id,
            str(args.get("revisedBody") or ""),
            instruction=str(args.get("instruction") or ""),
            allow_unconfirmed=True,
            # 模型若回传它读到的版本号，则做一致性校验（升级方案 §7.4）：
            # 用户在对话期间手动保存过新稿时拒绝覆盖并回灌冲突原因
            base_version_no=(
                int(args["baseVersionNo"]) if args.get("baseVersionNo") is not None else None
            ),
        )
        # 修订即迭代新版本：同步落盘带版本号的 Word 草稿，供气泡文件卡打开预览
        published: dict[str, Any] = {}
        try:
            exported = docgen.export_to_workspace(draft_id, "docx")
            published = _publish_docgen_file(state, draft_id, exported, note="文书修订稿已生成")
        except Exception:
            published = {}
        return {
            "draftId": draft_id,
            "versionNo": res["versionNo"],
            "baseVersionNo": res["baseVersionNo"],
            "kind": res["kind"],
            "blockingCount": res["draft"]["blockingCount"],
            "savedAt": res["savedAt"],
            **({"fileName": published["fileName"]} if published else {}),
        }

    if name == "skill_install":
        from .. import skills

        path = str(args.get("path") or "").strip()
        if not path:
            raise ValueError("缺少技能本地路径")
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"读取技能文件失败：{exc.strerror or exc}") from exc
        installed = skills.install_from_text(raw, source=f"file://{path}")
        return {
            "installed": True,
            "skillId": installed["id"],
            "name": installed["name"],
            "description": installed["description"],
            "allowedTools": installed["allowedTools"],
            "note": "安装成功；新技能需在输入坞 / 菜单中选中后对任务生效",
        }

    if name == "skill_list":
        from .. import skills

        return {
            "skills": [
                {k: s[k] for k in ("id", "name", "description", "allowedTools", "enabled")}
                for s in skills.list_skills()
            ]
        }

    if name == "skill_remove":
        from .. import skills

        removed = skills.remove_skill(str(args.get("skillId") or ""))
        if not removed:
            raise ValueError("技能不存在或已删除")
        return {"removed": True}

    # 沙箱工作空间工具（read/write/edit/glob/grep/bash），见 sandbox.py
    if name in _SANDBOX_TOOLS:
        from .. import sandbox

        return sandbox.execute_tool(state, name, args)

    raise ValueError(f"未知工具：{name}")


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


class _TextThrottle:
    """text 增量节流推送（传输优化：避免每个字符写一条事件记录）。"""

    def __init__(self, emit: Callable[[str, dict[str, Any]], None], interval: float = TEXT_FLUSH_INTERVAL_S):
        self._emit = emit
        self._interval = interval
        self._buf: list[str] = []
        self._last = 0.0

    def feed(self, delta: str) -> None:
        self._buf.append(delta)
        now = time.monotonic()
        if now - self._last >= self._interval:
            self.flush(now)

    def flush(self, now: float | None = None) -> None:
        if not self._buf:
            return
        self._emit("text", {"delta": "".join(self._buf)})
        self._buf.clear()
        self._last = time.monotonic() if now is None else now


# ---------------------------------------------------------------------------
# 简单对话快速通道（效率与质量分工）：问候、元问题、与案件无关的通用问题走
# 单轮轻量直答；只有案件问题才动用完整阅卷循环。模型发现需要查卷时输出约定
# 标记自动回退，回退成本仅一次轻调用，绝不以牺牲案件结论质量换速度。
# ---------------------------------------------------------------------------
FAST_PATH_MARKER = "[[NEED_CASE_REVIEW]]"
FAST_PATH_MAX_REQUEST_CHARS = 200
# 明显指向本案材料的输入保守排除（其余由模型自省回退兜底）：宁可让边界问题
# 走完整循环，也不让需要原文依据的问题被无卷宗轻答
_FAST_PATH_CASE_HINT = re.compile(
    r"本案|该案|此案|本合同|这份合同|该合同|此合同|卷宗|阅卷|全卷|证据|笔录|起诉书|答辩状|"
    r"原告|被告|当事人|证人|案发|犯罪事实|罪名|量刑|涉案金额|送货单|验收单|借条|转账记录|"
    r"判决书|裁定书|仲裁|诉讼请求|举证|质证|材料里|材料中|文档里|上传的|附件|第\s*\d+\s*页|"
    r"(?:分析|梳理|总结|归纳|审查|判断|认定|评估).*(?:争点|争议焦点)"
)
_FAST_PATH_SYSTEM = (
    "你是 LawBuddy，现在处于轻量直答通道：没有案件卷宗、没有可用工具。\n"
    "规则：\n"
    "1. 无需查阅案件材料即可回答的输入（问候、关于你的身份与能力、通用法律或生活知识、"
    "与具体案件无关的问题），直接简洁回答，中文，一般不超过 200 字。\n"
    "2. 若回答必须查阅案件卷宗/证据/合同原文，或需要实时联网信息、文件与工具操作，"
    f"只输出：{FAST_PATH_MARKER}\n"
    "3. 严禁编造案件事实、当事人、金额、日期或法律条文编号；通用法律问题作一般性说明，"
    "不要针对具体案件下结论，也不要声称已查阅任何材料。"
)


def _fast_path_eligible(state: dict[str, Any]) -> bool:
    """快速通道前置硬条件：只放行明显轻量的 legal_review 新任务。"""
    if str(state.get("mode") or "") != "legal_review":
        return False
    if state.get("messages"):            # 检查点续跑：沿用既有任务状态，不分流
        return False
    if state.get("attachments"):
        return False
    if state.get("selected_document_ids"):
        return False
    if state.get("workflow_preset"):
        return False
    if state.get("skills"):
        return False
    request = str(state.get("request") or "").strip()
    if not request or len(request) > FAST_PATH_MAX_REQUEST_CHARS:
        return False
    return not _FAST_PATH_CASE_HINT.search(request)


def _try_fast_path(
    state: dict[str, Any],
    emit: Callable[[str, dict[str, Any]], None],
    check: Callable[[], bool],
    usage_total: dict[str, int],
) -> str | None:
    """命中简单对话时单轮轻量直答；返回 None 表示进入完整 Agent Loop。"""
    if not _fast_path_eligible(state):
        return None
    policy = state.get("model_policy") or {}
    model_id = str(policy.get("modelId") or model_gateway.DEFAULT_MODEL)
    messages: list[dict[str, Any]] = [{"role": "system", "content": _FAST_PATH_SYSTEM}]
    # 历史只带最近 4 条（全文历史是完整循环的上下文，快速通道刻意保持小 prompt）
    for h in (state.get("history") or [])[-4:]:
        role = str((h or {}).get("role") or "")
        text = str((h or {}).get("text") or "").strip()
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": text[:500]})
    messages.append({"role": "user", "content": str(state.get("request") or "").strip()})
    try:
        result = model_gateway.chat_with_tools(
            messages, tools=None, model=model_id,
            cancel_check=check, reasoning_effort="low",
        )
    except Exception:
        # 快速通道失败绝不让任务失败：静默回退完整循环（沿用其重试与降级机制）
        return None
    usage = result.get("usage") or {}
    usage_total["promptTokens"] += int(usage.get("promptTokens") or 0)
    usage_total["completionTokens"] += int(usage.get("completionTokens") or 0)
    content = str(result.get("content") or "").strip()
    if not content:
        return None
    if FAST_PATH_MARKER in content or result.get("toolCalls"):
        emit("text", {"delta": "> 该问题需要查阅案件材料，转入阅卷分析…\n\n"})
        return None
    # 聚合后再上屏（而非逐 token 流式）：若模型中途给出回退标记，避免脏文本留在气泡
    emit("text", {"delta": content})
    emit("assistant_message", {"round": 1, "content": content, "toolCalls": []})
    return content


def run_autonomous(
    state: dict[str, Any],
    emit: Callable[[str, dict[str, Any]], None],
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """执行自主 Agent Loop，事件经 emit(type, payload) 实时外发。

    返回 {"answer": 最终回答, "rounds": 实际轮数, "usage": token 统计}。
    state["messages"] 存在时从检查点续跑（runner 恢复路径注入）。
    """
    task_id = str(state.get("task_id") or "")
    check = cancel_check or (lambda: False)

    # 本次任务绑定技能（/ 菜单选中）：加载详情供 system prompt 注入与工具过滤
    skill_details: list[dict[str, Any]] = []
    skill_ids = [str(s) for s in (state.get("skills") or []) if str(s)]
    if skill_ids:
        from .. import skills as skill_store

        skill_details = skill_store.get_skills(skill_ids)
    state["skill_details"] = skill_details

    # 案件上下文准备（升级方案 §8.1）：每次执行/续跑都重新读取材料与版本，
    # 不沿用旧上下文包——材料换版后不得把失效引用当成已支持事实。
    # 只记录计数类元数据到事件，不写原文或个人信息。
    case_context = _prepare_case_context(state)
    if case_context:
        state["case_context"] = case_context
        emit("case_context", {
            "linkedCase": bool(case_context.get("linkedCaseId")),
            "ownDocuments": case_context.get("ownDocuments", 0),
            "sharedDocuments": case_context.get("sharedDocuments", 0),
            "totalPages": case_context.get("totalPages", 0),
            "parsePending": case_context.get("parsePending", 0),
            "ocrPendingPages": case_context.get("ocrPendingPages", 0),
            "memories": case_context.get("memories", 0),
        })

    messages: list[dict[str, Any]] = list(state.get("messages") or [])
    if not messages:
        messages = [{"role": "system", "content": _system_prompt(state)}]
        # 跨任务对话历史（多轮流程如 MCP 安装：此前任务向用户追问密钥的上下文）
        for h in state.get("history") or []:
            role = str((h or {}).get("role") or "")
            text = str((h or {}).get("text") or "").strip()
            if role in ("user", "assistant") and text:
                messages.append({"role": role, "content": text})
        messages.append({"role": "user", "content": _user_prompt(state)})

    policy = state.get("model_policy") or {}
    model_id = str(policy.get("modelId") or model_gateway.DEFAULT_MODEL)
    # 用户设定档位是「综合/交付」阶段的上限；提取轮与修复轮按阶段收敛（§6.6 D3）
    base_effort = model_gateway.effective_reasoning_effort(policy.get("reasoningEffort"), model_id)
    effort = base_effort
    downgraded = False          # 思考失控/空回答后降档重试：后续轮次保持最低档
    # 守门修复期标记：从下发修复项起持续到模型提交新草稿（中间可能穿插补读页），
    # 整个修复过程都用最高档，而不是只有紧接的一轮（§7.2 / D3）
    repair_pending = False
    stage_counts: dict[str, int] = {}   # 各阶段实际调用次数（效率对照口径 §4.2）
    stage_trace: list[str] = []         # 逐次调用的阶段序列（与模型调用一一对应）
    # 工具按模式裁剪（技能是纯提示词包随 prompt 注入；无关模式工具不下发，
    # 每轮固定 token 开销从 29 个工具全量降到模式相关子集）
    tools = _tools_for(state)
    # 基础 system prompt（剥离旧任务清单段）：每轮重装清单段以反映最新进度
    if messages and messages[0].get("role") == "system":
        state["_system_base"] = _strip_plan(str(messages[0].get("content") or ""))

    # 运行期共享引用（下划线键不进 checkpoint——save_checkpoint 只存 messages）：
    # 子代理（case_explore）经它们外发进度、并入用量、透传取消
    usage_total = {"promptTokens": 0, "completionTokens": 0}
    state["_emit"] = emit
    state["_usage_total"] = usage_total
    state["_cancel_check"] = check
    rounds = 0
    answer = ""
    reason = "max_rounds"
    empty_rounds = 0
    tool_calls_total = 0                 # 本次任务累计工具调用次数（受 MAX_TOOL_CALLS 约束）
    bash_timeout_streaks: dict[str, int] = {}  # 规范化命令 → 连续超时次数
    started = time.monotonic()

    # 任务契约（§5.3）：续跑沿用检查点里已推导的契约，首轮现推
    state["task_contract"] = state.get("task_contract") or _derive_task_contract(state)
    # 固定上限：旧检查点里遗留的轮数预算一并刷新为当前值
    contract = state["task_contract"]
    contract.setdefault("budget", {})["maxRounds"] = MAX_ROUNDS
    contract["budget"]["toolCalls"] = MAX_TOOL_CALLS
    max_rounds = MAX_ROUNDS
    state.setdefault("reading_ledger", {})
    state.setdefault("evidence_ledger", {})
    state.setdefault("issue_ledger", {})
    repair_rounds_left = int(state.get("_repair_rounds_left") or FINALIZE_REPAIR_ROUNDS)
    gate_result: dict[str, Any] | None = None

    # 简单对话快速通道：命中则单轮直答、跳过主循环；未命中或模型要求查卷时
    # 走完整阅卷循环（fast path 的调用用量已计入 usage_total，不重复计量）
    fast_path_answer = _try_fast_path(state, emit, check, usage_total)
    if fast_path_answer is not None:
        answer = fast_path_answer
        rounds = 1
        reason = "fast_path"

    try:
        for round_no in (range(1, max_rounds + 1) if fast_path_answer is None else ()):
            if check():
                raise task_store.TaskCancelled("用户停止")
            rounds = round_no
            state["_round"] = round_no

            # ---- 上下文窗口管理（每轮模型调用前） ----
            # 1) 历史超阈值时压缩早期轮次（确定性摘要，不额外调模型）：
            #    控制 prompt 体积 → TTFT 与费用回落，长任务不再被上下文拖垮
            if len(messages) > CONTEXT_KEEP_RECENT_MSGS + 2 and \
                    _messages_chars(messages) > CONTEXT_COMPACT_THRESHOLD_CHARS:
                messages, cstats = _compact_messages(messages)
                emit("compaction", cstats)
            # 2) 任务清单段与状态摘要重装进 system（最新进度每轮可见；无清单时还原基础提示）
            #    状态摘要来自账本而非历史消息，压缩/恢复后依然成立（§6.4）
            base_system = str(state.get("_system_base") or "")
            if messages and messages[0].get("role") == "system" and base_system:
                messages[0] = {"role": "system", "content": base_system
                               + _plan_section(state.get("task_plan"))
                               + _ledger_summary(state)}

            throttle = _TextThrottle(emit)
            # 工具参数生成进度（大参数如 render_html 可流式生成数十秒，不上报会被误认为卡死）：
            # 每 callId 300ms 节流一次，UI 用占位气泡显示「正在生成… N 字符」
            prog_last: dict[str, tuple[float, int]] = {}

            def on_tool_args(call_id: str, name: str, total_chars: int) -> None:
                now = time.monotonic()
                prev_t, prev_n = prog_last.get(call_id, (0.0, -1))
                if now - prev_t >= 0.3 and total_chars != prev_n:
                    prog_last[call_id] = (now, total_chars)
                    emit("tool_use_progress", {"callId": call_id, "name": name, "chars": total_chars})

            # 思考进度（GLM 系强制思考：high 档偶发数分钟无正文输出的思考死循环
            # 2026-09-25 实证——不上报会让用户面对空气泡「假卡死」后手动取消）：
            # 每 1s 节流上报本轮已累计思考字符数；思考内容不落库，仅统计字数
            reason_state = {"chars": 0, "last": 0.0}

            def on_reasoning(delta: str) -> None:
                reason_state["chars"] += len(delta)
                now = time.monotonic()
                if now - reason_state["last"] >= 1.0:
                    reason_state["last"] = now
                    emit("thinking_progress", {"round": round_no, "chars": reason_state["chars"]})

            reason_state["chars"] = 0
            reason_state["last"] = time.monotonic()

            # 分阶段推理档位（§6.6 第 5 条 / D3）：读页轮用最低档，综合/交付沿用
            # 用户设定，守门修复轮用最高档；降档重试后不再回到高档
            stage = _turn_stage(messages, repair_pending)
            effort = _stage_effort(stage, base_effort, model_id, downgraded=downgraded)
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            stage_trace.append(stage)

            result: dict[str, Any] | None = None
            try:
                result = model_gateway.chat_with_tools(
                    # 随问附件在请求层并入首条 user 消息（messages 原序列保持纯文本）
                    _wire_messages(messages, state),
                    tools=tools,
                    model=model_id,
                    on_text_delta=throttle.feed,
                    on_reasoning_delta=on_reasoning,
                    on_tool_args_delta=on_tool_args,
                    cancel_check=check,
                    reasoning_effort=effort,
                )
            except model_gateway.ReasoningRunaway as exc:
                # 思考失控熔断（model_gateway.REASONING_RUNAWAY_CHARS）：不等烧满
                # max_tokens，立即降档重试（与下方空回答守卫同一套降级语义）；
                # 连续两次熔断则按 empty_answer 收束 → 下方低档强制作答兜底
                empty_rounds += 1
                if empty_rounds >= 2:
                    reason = "empty_answer"
                    break
                emit("text", {"delta": f"\n> ⚠ 本轮模型思考失控（已思考 {exc.reasoning_chars} 字仍无输出，疑似死循环），已中断并降低思考强度重试…\n\n"})
                messages.append({
                    "role": "user",
                    "content": "上一轮你长时间只输出思考内容、没有任何可见正文或工具调用（疑似思考死循环）。"
                               "请立即基于以上已获得的信息输出最终回答正文或调用工具，直接作答、不要再只输出思考。",
                })
                downgraded = True
                continue
            throttle.flush()

            usage = result.get("usage") or {}
            usage_total["promptTokens"] += int(usage.get("promptTokens") or 0)
            usage_total["completionTokens"] += int(usage.get("completionTokens") or 0)

            content = str(result.get("content") or "")
            tool_calls = list(result.get("toolCalls") or [])
            content = _restore_dialogue(state, content)
            for call in tool_calls:
                call.arguments = _restore_dialogue(state, call.arguments)
            # 中间文本（interim text）不等于最终答案：本轮同时发起工具调用时，
            # 这段文字只是过程说明，不能当作终答（否则轮数耗尽时会被误认为已作答，
            # §7.3 最后一条）；此时 answer 保持空串，收尾会走强制作答
            if not tool_calls:
                answer = content

            emit("assistant_message", {
                "round": round_no,
                "content": content,
                "toolCalls": [tc.to_dict() for tc in tool_calls],
            })

            if not tool_calls:
                if content.strip():
                    # 提交草稿（§7.1）：模型停止调用工具只表示"可以提交"，是否干净
                    # 完成由守门判定；守门不通过时下发可定位修复项，用独立小预算修复
                    gate = _finalize_gate(state, content, allow_repairs=repair_rounds_left > 0)
                    if gate["ok"] or not gate["repairs"] or repair_rounds_left <= 0:
                        answer = content
                        reason = "final_answer"
                        break
                    repair_rounds_left -= 1
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": _repair_prompt(gate["repairs"])})
                    emit("text", {"delta": "\n> ⚠ 完成守门发现可修复项，正在做局部补正…\n\n"})
                    # 进入守门修复期：后续（含补读页）轮次都按修复阶段用最高档
                    repair_pending = True
                    continue
                # 空回答守卫（2026-09-25 实证）：GLM 系思考模型偶发思考死循环，
                # reasoning_content 烧满 max_tokens（finish_reason=length）而正文
                # 0 字——直接按 final_answer 结束会让用户拿到空白结果。降思考档
                # 重试一次；仍为空则按 empty_answer 结束（下方强制作答兜底）
                empty_rounds += 1
                if empty_rounds >= 2:
                    reason = "empty_answer"
                    break
                emit("text", {"delta": "\n> ⚠ 上一轮模型未输出可见内容（思考超限），已自动降低思考强度重试…\n\n"})
                messages.append({
                    "role": "user",
                    "content": "上一轮你没有输出任何可见正文（可能原因：思考内容耗尽了输出上限）。"
                               "请立即基于以上已获得的信息输出最终回答正文，直接作答、不要再只输出思考。",
                })
                downgraded = True
                continue

            # 回灌 assistant 消息（OpenAI tool_calls 协议格式）
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in tool_calls
                ],
            })

            tool_budget_exhausted = False
            for tc in tool_calls:
                if check():
                    raise task_store.TaskCancelled("用户停止")
                tool_calls_total += 1
                tool_args = tc.parsed_arguments
                emit("tool_use_start", {"callId": tc.id, "name": tc.name, "arguments": tool_args})
                # bash 超时熔断：同一命令连续超时达上限后直接拒绝执行，强制换策略
                cmd_key = " ".join(str((tool_args or {}).get("command") or "").split()) if tc.name == "bash" else ""
                if tool_calls_total > MAX_TOOL_CALLS:
                    # 累计工具调用超限：不再执行，但仍回灌配对的 tool 消息
                    #（OpenAI 协议要求每个 tool_call 都有响应，缺失会破坏后续请求）
                    output = {
                        "denied": True,
                        "message": (
                            f"本次任务累计工具调用已达上限（{MAX_TOOL_CALLS} 次），本次调用不再执行。"
                            "请立即基于已获得的信息输出最终回答，并列出尚未完成/待核实事项。"
                        ),
                    }
                    status = "ok"
                    tool_budget_exhausted = True
                else:
                    try:
                        permitted = {spec["function"]["name"] for spec in tools}
                        if tc.name not in permitted:
                            raise ValueError("该工具不在当前模式和任务的允许范围")
                        if cmd_key and bash_timeout_streaks.get(cmd_key, 0) >= BASH_TIMEOUT_STREAK_LIMIT:
                            output = {
                                "denied": True,
                                "message": (
                                    f"该命令此前已连续 {BASH_TIMEOUT_STREAK_LIMIT} 次执行超时被强制终止，"
                                    "本次任务内禁止再执行同一命令。必须更换策略：缩小处理范围、拆分为更小步骤、"
                                    "改用 read_file/grep_search 等文件工具，或向用户说明该步骤无法完成。"
                                ),
                            }
                            status = "ok"
                        else:
                            output = _execute_tool(state, tc.name, tool_args, tc.id)
                            status = "ok"
                    except task_store.TaskCancelled:
                        raise
                    except Exception as exc:  # 工具报错回灌模型，由模型自行调整
                        output = {"error": str(exc)}
                        status = "error"
                    # 超时熔断计数：连续超时累加，其他结果（成功/拒绝/报错）清零
                    if cmd_key:
                        if isinstance(output, dict) and output.get("timedOut"):
                            bash_timeout_streaks[cmd_key] = bash_timeout_streaks.get(cmd_key, 0) + 1
                        else:
                            bash_timeout_streaks.pop(cmd_key, None)
                emit("tool_use_done", {"callId": tc.id, "name": tc.name, "status": status})
                emit("tool_result_message", {"callId": tc.id, "name": tc.name, "status": status, "result": output})
                # 会话级任务清单更新（task_plan）：写入 state，下一轮注入 system prompt
                if tc.name == "task_plan" and isinstance(output, dict) and output.get("plan") is not None:
                    state["task_plan"] = output["plan"]
                    emit("plan_update", {"steps": output["plan"]})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _clip(json.dumps(output, ensure_ascii=False, default=str), TOOL_RESULT_CHAR_LIMIT),
                })
                # 已就必要决策提问：本轮剩余工具调用不再执行（避免在用户回答前产生副作用）
                if state.get("_paused_question_set"):
                    break

            # 暂停时补齐未执行的工具调用响应（OpenAI 协议要求每个 tool_call 都有
            # 对应 tool 消息；缺失会破坏用户回答后的续跑请求）
            if state.get("_paused_question_set"):
                answered = {
                    str(m.get("tool_call_id")) for m in messages if m.get("role") == "tool"
                }
                for tc in tool_calls:
                    if tc.id in answered:
                        continue
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(
                            {"paused": True,
                             "message": "已向用户提出决策问题，任务暂停；用户回答后继续。"},
                            ensure_ascii=False,
                        ),
                    })

            # 每轮落检查点（messages + 任务契约 + 阅读账本：崩溃/取消后可续跑，
            # 且续跑时不必重新推导契约、不会把已读页面当成未读）
            task_store.save_checkpoint(task_id, f"round_{round_no}", {
                "messages": messages,
                "taskContract": state.get("task_contract"),
                "readingLedger": state.get("reading_ledger") or {},
                # 任务计划与证据/争点账本（§6.4 C3）：恢复后计划、来源与缺口不丢；
                # 精确 quote 仍在账本里按需展开，不把原文全文塞进每轮 Prompt
                "taskPlan": state.get("task_plan") or [],
                "evidenceLedger": state.get("evidence_ledger") or {},
                "issueLedger": state.get("issue_ledger") or {},
                "ledgerSchemaVersion": LEDGER_SCHEMA_VERSION,
                "repairRoundsLeft": repair_rounds_left,
                "budget": {
                    "roundsUsed": round_no,
                    "maxRounds": max_rounds,
                    "toolCallsUsed": tool_calls_total,
                    "exploreRoundsUsed": int(state.get("_explore_rounds_used") or 0),
                },
            })
            # 累计工具调用超限：本轮剩余调用已按拒绝回灌，保存检查点后收束循环
            if tool_budget_exhausted:
                reason = "max_tool_calls"
                break
            # 必要用户决策：问题集已落库并置 WAITING_HUMAN，立即结束模型调用与工具循环，
            # 等待用户回答后由 runner 从同一检查点续跑（不重复副作用）
            if state.get("_paused_question_set"):
                reason = "waiting_human"
                break
    except task_store.TaskCancelled:
        # 取消：已接收内容保留，由 runner 置 CANCELLED
        raise

    # 无终答兜底：轮数用尽（max_rounds）、工具调用累计超限（max_tool_calls）或
    # 连续空回答（empty_answer，思考死循环）时追发一次「无工具 + 低思考档」
    # 强制作答，保证用户拿到非空结论（已完成的结论 + 未尽事项）而非空白回答
    forced_final = False
    if reason in ("max_rounds", "max_tool_calls", "empty_answer") and not answer.strip():
        messages.append({
            "role": "user",
            "content": "请立即停止调用工具与深度思考，基于已获得的信息输出最终回答："
                       "给出已能确定的结论（含来源标注《文档名》第N页）与尚未完成/待核实事项清单。",
        })
        try:
            throttle = _TextThrottle(emit)
            freason_state = {"chars": 0, "last": 0.0}

            def on_forced_reasoning(delta: str) -> None:
                freason_state["chars"] += len(delta)
                now = time.monotonic()
                if now - freason_state["last"] >= 1.0:
                    freason_state["last"] = now
                    emit("thinking_progress", {"round": rounds, "chars": freason_state["chars"]})

            result = model_gateway.chat_with_tools(
                _wire_messages(messages, state), tools=None, model=model_id,
                on_text_delta=throttle.feed, on_reasoning_delta=on_forced_reasoning,
                cancel_check=check,
                reasoning_effort=model_gateway.weakest_reasoning_effort(model_id),
            )
            throttle.flush()
            usage = result.get("usage") or {}
            usage_total["promptTokens"] += int(usage.get("promptTokens") or 0)
            usage_total["completionTokens"] += int(usage.get("completionTokens") or 0)
            answer = _restore_dialogue(state, str(result.get("content") or ""))
            forced_final = bool(answer.strip())
            emit("assistant_message", {"round": rounds, "content": answer, "toolCalls": [], "forced": True})
        except task_store.TaskCancelled:
            raise
        except Exception:
            pass  # 强制终答失败（网关错误等）沿用原 reason 语义，不阻断收尾
    if reason != "waiting_human" and not answer.strip():
        # 兜底仍为空：如实告知用户生成失败（模型输出异常），不留空白结果；
        # 分析数据（检索命中/已读原文）已在事件流与检查点中保留
        notice = (
            "⚠ 未能生成最终回答：模型输出异常（思考内容耗尽输出上限且重试后仍为空）。\n\n"
            "分析过程数据已保留（检索命中与已读取的原文可在上方步骤中查看），"
            "可重新发起提问或换一种问法重试。"
        )
        emit("text", {"delta": notice})
        emit("assistant_message", {"round": rounds, "content": notice, "toolCalls": [], "forced": True})

    # 正常结束：token/用时统计随 turn_complete 携带给 UI

    # 文书生成收尾：确保首轮即产出草稿 Word（模型未调用渲染工具时确定性兜底），
    # 文件卡在任务收尾的产物登记后出现在对话气泡底部；
    # 等待用户决策时不兜底出稿（避免在用户回答前先产出可能违背其立场的草稿）
    if reason != "waiting_human":
        _ensure_docgen_word_draft(state, emit)

    # 收尾守门（§7.1）：最终稿只在这里做一次完整判定（引用回查 + 阅读覆盖 +
    # 交付核验，verify=True 建 Claim），修复轮内不重复落库；结论随
    # turn_complete 回传，由 Runner 决定干净完成还是带警告完成（§7.3）
    if reason == "waiting_human":
        # 等待用户决策：不判定完成，也不建 Claim；问题集已随 human_question 事件下发
        gate_result = {
            "ok": True, "repairs": [], "warnings": [],
            "verification": {"total": 0, "supported": 0, "pending": [], "cites": []},
            "coverage": {"required": False, "totalPages": 0, "readPages": 0, "unread": [], "excluded": 0},
        }
    elif reason == "fast_path":
        # 轻量直答不含案件原文断言：跳过引用回查与阅读覆盖守门，直接干净完成
        gate_result = {
            "ok": True, "repairs": [], "warnings": [],
            "verification": {"total": 0, "supported": 0, "pending": [], "cites": []},
            "coverage": {"required": False, "totalPages": 0, "readPages": 0, "unread": [], "excluded": 0},
        }
    elif answer.strip():
        gate_result = _finalize_gate(state, answer, allow_repairs=False, verify=True)
    else:
        gate_result = {
            "ok": False,
            "repairs": [],
            "warnings": [{"code": "EMPTY_ANSWER", "text": "未生成最终回答：模型输出异常"}],
            "verification": {"total": 0, "supported": 0, "pending": [], "cites": []},
            "coverage": _reading_coverage(state, state.get("task_contract") or {}),
        }
    verification: dict[str, Any] = gate_result["verification"]
    if answer.strip():
        if verification["pending"]:
            appendix = "\n\n> ⚠ 以下引用未能回查原文，待人工核实：\n" + "\n".join(
                f"> - {p}" for p in verification["pending"]
            )
            answer = answer + appendix
            emit("text", {"delta": appendix})
        emit("answer_verification", {
            "total": verification["total"],
            "supported": verification["supported"],
            "pendingCount": len(verification["pending"]),
            "cites": verification["cites"],
            # 守门未通过的项（含产物缺失、覆盖不足）与角标同源，供气泡统一提示
            "warnings": gate_result["warnings"],
        })
        # 声称产物核验（只标注不拦截）：回复声称"已生成/已保存"的文件若未实际
        # 落盘（典型：原文件被 Word 锁定导致保存失败后模型虚构另存成功），在
        # 回复末尾追加显著警示，与产物卡片状态保持一致，防止用户被文本误导
        missing_files: list[str] = []
        for warning in gate_result["warnings"]:
            for name in warning.get("files") or []:
                if name not in missing_files:
                    missing_files.append(name)
        if missing_files:
            appendix_files = (
                "\n\n> ⚠ 以下文件在本次任务中未检测到实际生成，请以对话气泡下方的"
                "产物文件卡为准，不要视为可用交付物：\n"
                + "\n".join(f"> - {name}" for name in missing_files)
            )
            answer = answer + appendix_files
            emit("text", {"delta": appendix_files})
        # 产物落库（失败不阻断任务完成，与旧管线 persist_final_outputs 一致）
        try:
            from ..artifacts import save_artifact

            case_title = str(state.get("case_title") or "").strip()
            save_artifact(
                task_id, str(state.get("case_id") or ""), "chat",
                f"{case_title} · 对话分析" if case_title else "对话分析",
                {"answer": answer, "cites": verification["cites"], "verification": {
                    "total": verification["total"], "supported": verification["supported"],
                    "pending": len(verification["pending"]),
                    "ok": bool(gate_result["ok"]),
                }},
            )
        except Exception:
            pass

    # 收尾清单兜底：已交付最终结论（正常作答，或轮数耗尽后强制生成的实质回答）时，
    # 把仍停在 pending 的步骤收口为 done，避免 UI 停在「0/6 个任务已完成」与已交付
    # 结论矛盾；空回答等未真正作答的收束不收口，保留真实缺口
    if reason == "final_answer" or forced_final:
        _close_out_plan(state, emit)

    coverage = gate_result.get("coverage") or {}
    emit("turn_complete", {
        "rounds": rounds,
        "reason": reason,
        "forcedFinal": forced_final,
        "promptTokens": usage_total["promptTokens"],
        "completionTokens": usage_total["completionTokens"],
        "durationMs": int((time.monotonic() - started) * 1000),
        # 各推理阶段实际调用次数（§4.2 效率口径）：用于确认优化不是靠少读换速度
        "reasoningStages": stage_counts,
        "reasoningStageTrace": stage_trace,
        # 效率指标（§4.2 / §10.4 D4）：重复读取、无效检索、子 Agent 采用率
        "efficiency": _efficiency_summary(state),
        "finalize": {
            "ok": bool(gate_result["ok"]),
            "taskDepth": str((state.get("task_contract") or {}).get("taskDepth") or ""),
            "warnings": gate_result["warnings"],
            "coverage": {
                "required": bool(coverage.get("required")),
                "totalPages": int(coverage.get("totalPages") or 0),
                "readPages": int(coverage.get("readPages") or 0),
                "unread": len(coverage.get("unread") or []),
                "excluded": int(coverage.get("excluded") or 0),
            },
        },
    })
    return {"answer": answer, "rounds": rounds, "usage": usage_total,
            "finalize": gate_result, "reason": reason}
