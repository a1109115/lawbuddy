"""上下文组装器（LB-HARNESS-2026-01 §4.8 / B-04）。

把上下文拆成三层，分别缓存与重建：

1. **稳定前缀**：角色、认知方法、引用纪律、交付格式规则——同一任务内不变，
   可被模型侧前缀缓存命中，不随轮次改写；
2. **任务状态**：任务契约、阅读覆盖、证据/争点缺口、工作单元进度——每轮重建，
   数据来自账本与数据库，不依赖被截断的历史摘要；
3. **当前工作单元**：单元范围、合法选项、已提交/待核实格、失败原因与
   相关证据——只注入当前单元需要的材料，避免把整卷内容塞进主上下文。

硬约束（§2.1 / §4.3）：未提交单元格、合法选项、引文与失败原因**不依赖**历史
压缩摘要保存——压缩只压消息，这些状态始终从账本重建。
"""

from __future__ import annotations

from typing import Any

# 单次注入的未完成格上限：只提示前 N 个，避免 300 格任务把提示词撑爆；
# 完整清单始终可查（work_unit_run / 交付进度），不因截断而丢失状态。
UNIT_BRIEF_MAX_CELLS = 12
EVIDENCE_BRIEF_MAX_ITEMS = 8


def _clip(text: Any, limit: int) -> str:
    raw = str(text or "").strip()
    return raw if len(raw) <= limit else raw[: limit - 1] + "…"


def build_stable_prefix(state: dict[str, Any], base_system: str) -> str:
    """稳定前缀：基础系统提示（规则部分），同一任务内逐字不变。

    调用方负责把「任务状态」「当前工作单元」拼在后面，而不是重写这段，
    这样模型侧的前缀缓存才有机会命中（001 任务 505 万输入 Token 的一部分
    来自每轮重写整段提示）。
    """
    return base_system


def build_state_block(state: dict[str, Any]) -> str:
    """任务状态块：契约 + 阅读覆盖 + 账本缺口 + 工作单元进度（全部来自账本）。"""
    import json as _json

    from . import work_ledger

    task_id = str(state.get("task_id") or "")
    lines: list[str] = []
    contract = state.get("task_contract") or {}
    depth = str(contract.get("taskDepth") or "")
    if depth:
        lines.append(f"任务深度：{depth}（{_clip(contract.get('objective'), 80)}）")
    if not task_id or not state.get("_work_contract"):
        return ("\n\n【当前任务状态】\n" + "\n".join(lines)) if lines else ""
    try:
        units = work_ledger.unit_progress(task_id)
        cells = work_ledger.cell_progress(task_id)
        delivery = work_ledger.delivery_progress(task_id)
    except Exception:  # noqa: BLE001 账本不可用时退回无进度状态
        return ("\n\n【当前任务状态】\n" + "\n".join(lines)) if lines else ""
    if units["total"]:
        lines.append(
            f"工作单元：{units['terminal']}/{units['total']} 已进入终态"
            f"（已提交 {units['committed']}，待核实 {units['needsHuman']}，阻塞 {units['blocked']}）"
        )
    if cells["total"]:
        lines.append(
            f"答案格：已提交 {cells['committed']}，待核实 {cells['needsHuman']}，"
            f"阻塞 {cells['blocked']}，未处理 {cells['pending']}"
        )
    if delivery["artifactCount"]:
        lines.append(
            f"交付物：{delivery['completeCount']}/{delivery['artifactCount']} 完整"
            + (f"，未完整：{'、'.join(delivery['incomplete'])}" if delivery["incomplete"] else "")
            + f"；行覆盖 {delivery['rowsCommitted']}/{delivery['rowsExpected']}"
        )
    pending = _pending_units(task_id, limit=5)
    if pending:
        lines.append("建议下一步处理的工作单元：" + "；".join(pending))
    if not lines:
        return ""
    return "\n\n【当前任务状态】\n" + "\n".join(lines)


def _pending_units(task_id: str, limit: int = 5) -> list[str]:
    """未终态工作单元的简短描述（实体/问题 + 尝试次数 + 上次失败原因）。"""
    from . import work_ledger

    out: list[str] = []
    for unit in work_ledger.list_units(
        task_id, statuses=("pending", "leased", "needs_human", "blocked"), limit=limit * 3):
        scope = unit.get("scope") or {}
        label = " / ".join(str(x) for x in (
            scope.get("entityLabel") or scope.get("entityId") or scope.get("documentTitle")
            or scope.get("documentId") or "",
            scope.get("questionText") or scope.get("fieldText") or scope.get("fieldId") or "",
        ) if str(x or "").strip())
        attempts = f"（尝试 {unit['attempts']}/{unit['maxAttempts']}）"
        error = f" 上次失败：{unit['lastErrorCode']}" if unit.get("lastErrorCode") else ""
        out.append(f"{unit['workUnitId']} {label}{attempts}{error}")
        if len(out) >= limit:
            break
    return out


def build_unit_brief(state: dict[str, Any], unit: dict[str, Any]) -> str:
    """当前工作单元简报：范围、合法选项、已提交/待核实格、失败原因、相关证据。

    只注入该单元需要的材料；不注入其他单元的内容，也不注入整卷正文。
    """
    from . import work_ledger

    task_id = str(state.get("task_id") or "")
    scope = unit.get("scope") or {}
    artifact = str(scope.get("artifact") or "")
    lines = [
        "\n\n【当前工作单元】",
        f"单元：{unit.get('workUnitId')}（{unit.get('scopeType')}，第 {unit.get('attempts')}"
        f"/{unit.get('maxAttempts')} 次尝试）",
    ]
    if artifact:
        lines.append(f"交付物：{artifact}")
    entity = scope.get("entityLabel") or scope.get("entityId")
    if entity:
        lines.append(f"实体：{entity}")
    if scope.get("documentTitle") or scope.get("documentId"):
        lines.append(f"材料：{scope.get('documentTitle') or scope.get('documentId')}")
    question = scope.get("questionText") or scope.get("fieldText") or scope.get("fieldId")
    if question:
        lines.append(f"要回答：{question}")
    if scope.get("multiPoint"):
        lines.append("多选字段：逐项回查原文后以 ` | ` 连接；未逐项核实的选项不得写入。")
    options = _valid_options(state, artifact)
    if options:
        lines.append("合法选项（只能从中选择；无法确认时留空并标为待核实）：" + "、".join(options))
    if unit.get("lastErrorCode"):
        lines.append(f"上次失败原因：{unit['lastErrorCode']}（不要重复相同检索；换词或逐页核对）")
    if task_id and artifact:
        siblings = [
            c for c in work_ledger.list_cells(task_id, artifact_name=artifact)[:UNIT_BRIEF_MAX_CELLS]
        ]
        committed = [c for c in siblings if c["status"] == "committed"]
        needs_human = [c for c in siblings if c["status"] == "needs_human"]
        if committed:
            lines.append(
                "同表已提交示例（仅供格式参考，不得直接套用其结论）："
                + "；".join(f"{c['cellKey']}={_clip(c['value'], 24)}" for c in committed[:3])
            )
        if needs_human:
            lines.append(
                f"同表待核实 {len(needs_human)} 格（例如 {needs_human[0]['cellKey']}："
                f"{needs_human[0].get('reasonCode') or 'UNKNOWN'}）；不要把它们当作已确认事实。"
            )
    evidence = _related_evidence(state, scope)
    if evidence:
        lines.append("本单元相关已回查证据：")
        lines.extend(f"- {item}" for item in evidence)
    lines.append(
        "完成要求：只有能给出文档 + 页码 + 逐字原文短引的答案才可提交；"
        "否则明确标记待核实并说明缺失原因。不要写入其他工作单元的内容。"
    )
    return "\n".join(lines)


def _valid_options(state: dict[str, Any], artifact: str) -> list[str]:
    """交付物声明的合法值（仅当任务显式声明；不按业务惯例补全）。"""
    contract = state.get("_work_contract") or {}
    for art in contract.get("artifacts") or []:
        if art.get("name") == artifact:
            options: list[str] = []
            for rule in contract.get("rules") or []:
                if rule.get("kind") == "value_domain" and rule.get("artifact") == artifact:
                    options.extend(str(v) for v in (rule.get("allowed") or []))
            return options
    return []


def _related_evidence(state: dict[str, Any], scope: dict[str, Any]) -> list[str]:
    """按实体/材料筛选相关证据条目（只给摘要，逐字引文在原文页里核对）。"""
    ledger = state.get("evidence_ledger") or {}
    entity = str(scope.get("entityLabel") or scope.get("entityId") or "")
    document_id = str(scope.get("documentId") or "")
    out: list[str] = []
    for entry in ledger.values():
        if not isinstance(entry, dict):
            continue
        claim = str(entry.get("claim") or "")
        source = entry.get("source") or {}
        if entity and entity not in claim:
            continue
        if document_id and str(source.get("documentId") or "") != document_id:
            continue
        mark = "已回查" if entry.get("status") == "supported" else "待核实"
        out.append(f"{mark}：{_clip(claim, 80)}")
        if len(out) >= EVIDENCE_BRIEF_MAX_ITEMS:
            break
    return out


def build_worker_messages(state: dict[str, Any], unit: dict[str, Any]) -> list[dict[str, Any]]:
    """工作型子代理的独立上下文（不含主对话历史，只含规则前缀 + 单元简报）。"""
    stable = build_stable_prefix(
        state,
        "你是 LawBuddy 的工作单元执行子代理。你只负责当前工作单元。\n"
        "规则：\n"
        "- 只用检索与读页工具定位原文，不展开法律分析或替主 Agent 作最终判断；\n"
        "- 每条结论必须给出文档 ID、页码与逐字原文短引；找不到就明确说明检索范围与缺口，"
        "不得编造页码或引文；\n"
        "- 你只能写暂存结果，不能修改正式交付物；\n"
        "- 输出格式：先一句话结论，随后一个 ```json 代码块：\n"
        '{"answer":"<答案或空字符串>","documentId":"<id>","pageNo":<int>,'
        '"quote":"<逐字短引>","confidence":"high|medium|low",'
        '"reasonCode":"<无法确认时的原因码>","notes":"<补充说明>"}\n'
        "无法确认时 answer 留空，reasonCode 填 NO_HITS / INSUFFICIENT_EVIDENCE / PARSE_FAILED。"
    )
    return [
        {"role": "system", "content": stable + build_unit_brief(state, unit)},
        {"role": "user", "content": _unit_task_text(unit)},
    ]


def _unit_task_text(unit: dict[str, Any]) -> str:
    scope = unit.get("scope") or {}
    parts = [f"请完成工作单元 {unit.get('workUnitId')}。"]
    if scope.get("documentTitle") or scope.get("documentId"):
        parts.append(f"材料：{scope.get('documentTitle') or scope.get('documentId')}。")
    if scope.get("entityLabel") or scope.get("entityId"):
        parts.append(f"对象：{scope.get('entityLabel') or scope.get('entityId')}。")
    question = scope.get("questionText") or scope.get("fieldText") or scope.get("fieldId")
    if question:
        parts.append(f"问题：{question}。")
    parts.append("请检索并读取原文章节后，按约定 JSON 输出结论。")
    return "".join(parts)
