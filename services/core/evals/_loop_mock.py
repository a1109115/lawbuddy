# -*- coding: utf-8 -*-
"""确定性 mock 网关注入器：为自主 Agent Loop 的评测提供脚本化模型响应。

背景：自主 Loop 只走 `model_gateway.chat_with_tools`（云端协议）。评测需要在
不联网、不依赖真实模型的情况下验证 Core 的确定性能力，本模块把网关替换为
脚本驱动的确定性实现——按预设步骤发起工具调用 / 输出最终文本，工具执行、
落库、引证绑定全部由真实实现完成（评测断言的是 Core 行为，而非 mock 输出）。

用法（evals 脚本内）：
    from evals._loop_mock import ScriptStep, install_mock_gateway, contract_review_steps
    undo = install_mock_gateway(contract_review_steps())
    try:
        ...  # 发起任务并等待完成
    finally:
        undo()

脚本耗尽时的兜底：返回一条提示文本并结束循环（避免评测挂起），同时在该轮
assistant_message 中可察觉（内容含「脚本步骤已耗尽」）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from lawbuddy.agent import model_gateway


class ScriptStep:
    """一步模型响应：要么发起一次工具调用，要么输出最终文本。

    arguments / text 可以是值，也可以是回调 `fn(messages) -> value`，
    回调在响应时求值（可从对话消息中提取前序工具结果，如 reviewId）。
    """

    def __init__(self, kind: str, payload: Any) -> None:
        self.kind = kind        # "tool" | "final" | "tool_text"
        self.payload = payload  # (name, args) 或 text 或 (name, args, text)

    @classmethod
    def tool(cls, name: str, arguments: Any) -> "ScriptStep":
        return cls("tool", (name, arguments))

    @classmethod
    def final(cls, text: Any) -> "ScriptStep":
        return cls("final", text)

    @classmethod
    def tool_with_text(cls, name: str, arguments: Any, text: Any) -> "ScriptStep":
        """同一轮既输出过程说明文本、又发起工具调用（§7.3 最后一条的边界场景）。"""
        return cls("tool_text", (name, arguments, text))


def user_payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """解析 user 消息首行请求与 JSON 附加段（`[合同审查输入] {...}` 等）。"""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content") or "")
        payload: dict[str, Any] = {"request": content.split("\n", 1)[0].strip()}
        for m in re.finditer(r"^\[([^\]]+)\] (\{.*\})\s*$", content, re.MULTILINE):
            try:
                payload[m.group(1)] = json.loads(m.group(2))
            except json.JSONDecodeError:
                pass
        return payload
    return {}


def last_tool_result(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """取最近一条 tool 消息的 JSON 结果（脚本按序推进时即上一步工具输出）。"""
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            try:
                obj = json.loads(str(msg.get("content") or "{}"))
            except json.JSONDecodeError:
                return {}
            return obj if isinstance(obj, dict) else {}
    return {}


def _resolve(value: Any, messages: list[dict[str, Any]]) -> Any:
    return value(messages) if callable(value) else value


# 最近一次 install_mock_gateway 安装的网关收到的调用参数（模型 + 推理档位 +
# 本次 prompt 字符量 / 工具条数）。评测在 restore() 之后仍可读取，用于断言
# 分阶段档位（§10.4 D3）与上下文成本前后对照（§10.4 D6）。
LAST_EFFORTS: list[dict[str, Any]] = []


def last_efforts() -> list[dict[str, Any]]:
    """最近一次 mock 网关的逐次调用参数：
    [{"model": ..., "effort": ..., "promptChars": ..., "toolCount": ...}, ...]。
    """
    return list(LAST_EFFORTS)


def install_mock_gateway(steps: list[ScriptStep]) -> Callable[[], None]:
    """把 model_gateway.chat_with_tools 替换为脚本驱动的确定性实现。

    返回恢复函数（恢复原始网关）。脚本按调用顺序逐步推进；每步消耗一次
    模型调用。usage 为固定确定性值（评测可断言累计口径）。
    每次调用收到的模型与推理档位记入 `efforts`（评测断言分阶段档位用）。
    """
    script = list(steps)
    cursor = {"i": 0}
    original = model_gateway.chat_with_tools
    efforts: list[dict[str, Any]] = []
    LAST_EFFORTS.clear()

    def fake_chat_with_tools(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str = model_gateway.DEFAULT_MODEL,
        on_text_delta: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        reasoning_effort: str | None = None,
        on_tool_args_delta: Callable[[str, str, int], None] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        idx = cursor["i"]
        cursor["i"] = idx + 1
        record = {
            "model": model,
            "effort": reasoning_effort,
            "promptChars": sum(len(str(m.get("content") or "")) for m in messages),
            "toolCount": len(tools or []),
            # 工具 schema 体积：D2 按需暴露的直接收益项（全量 vs 模式子集）
            "toolSchemaChars": len(json.dumps(tools, ensure_ascii=False)) if tools else 0,
        }
        efforts.append(record)
        LAST_EFFORTS.append(record)
        usage = {"promptTokens": 100, "completionTokens": 40}

        if idx < len(script) and script[idx].kind == "tool":
            name, args = script[idx].payload
            arguments = json.dumps(_resolve(args, messages), ensure_ascii=False)
            return {
                "content": "",
                "toolCalls": [model_gateway.ToolCall(f"mock_call_{idx + 1}", name, arguments)],
                "usage": usage,
            }

        if idx < len(script) and script[idx].kind == "tool_text":
            name, args, text = script[idx].payload
            arguments = json.dumps(_resolve(args, messages), ensure_ascii=False)
            content = str(_resolve(text, messages))
            if on_text_delta is not None and content:
                on_text_delta(content)
            return {
                "content": content,
                "toolCalls": [model_gateway.ToolCall(f"mock_call_{idx + 1}", name, arguments)],
                "usage": usage,
            }

        if idx < len(script):
            content = str(_resolve(script[idx].payload, messages))
        else:
            content = "（mock 脚本步骤已耗尽：请检查评测脚本的步骤覆盖）"
        if on_text_delta is not None and content:
            on_text_delta(content)
        return {"content": content, "toolCalls": [], "usage": usage}

    fake_chat_with_tools.efforts = efforts  # type: ignore[attr-defined]
    model_gateway.chat_with_tools = fake_chat_with_tools

    def restore() -> None:
        model_gateway.chat_with_tools = original

    return restore


# ---------------------------------------------------------------------------
# 预设脚本（按任务模式）
# ---------------------------------------------------------------------------

def contract_review_steps() -> list[ScriptStep]:
    """合同审查评测路径：create_review → split_clauses（内含确定性检查）→ 结论。"""
    def create_args(messages: list[dict[str, Any]]) -> dict[str, Any]:
        ci = user_payload(messages).get("合同审查输入") or {}
        return {
            "documentId": ci.get("primaryDocumentId"),
            "reviewerRole": ci.get("reviewerRole"),
            "customRole": ci.get("customRole"),
            "riskAppetite": ci.get("riskAppetite"),
        }

    def split_args(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {"reviewId": last_tool_result(messages).get("reviewId")}

    def final_text(messages: list[dict[str, Any]]) -> str:
        r = last_tool_result(messages)
        lines = [
            f"已完成合同审查：切分 {r.get('clauseCount')} 个条款，"
            f"识别类型「{r.get('contractType') or '未识别'}」，检出确定性风险 {r.get('riskCount')} 项："
        ]
        for x in r.get("risks") or []:
            lines.append(
                f"- [{x.get('riskLevel')}/{x.get('category')}] "
                f"{x.get('clauseRef') or '（全文）'}：{x.get('riskNote')}"
            )
        lines.append("以上确定性检查结果已落库，语义风险请结合条款原文进一步分析。")
        return "\n".join(lines)

    return [
        ScriptStep.tool("contract_create_review", create_args),
        ScriptStep.tool("contract_split_clauses", split_args),
        ScriptStep.final(final_text),
    ]


def docgen_steps() -> list[ScriptStep]:
    """文书生成评测路径：create_draft（提纲由工具从模板推导落库）→ 结论。"""

    def create_args(messages: list[dict[str, Any]]) -> dict[str, Any]:
        di = user_payload(messages).get("文书生成输入") or {}
        return {
            "templateId": di.get("templateId"),
            "docType": di.get("docType"),
            "purpose": di.get("purpose"),
        }

    def final_text(messages: list[dict[str, Any]]) -> str:
        r = last_tool_result(messages)
        headings = "、".join(o.get("heading") or "" for o in r.get("outline") or [])
        return (
            f"已创建文书草稿（{r.get('docType')}），提纲由模板结构推导并写入，"
            f"共 {len(r.get('outline') or [])} 节：{headings}。"
            "可在文书工作台继续编辑、确认提纲并导出。"
        )

    return [
        ScriptStep.tool("docgen_create_draft", create_args),
        ScriptStep.final(final_text),
    ]


def docgen_decision_steps() -> list[ScriptStep]:
    """文书生成必要决策路径：先提问（暂停）→ 用户回答后续跑建稿 → 结论。

    用于验证「提问前不产生草稿等副作用、回答后从同一检查点续跑不重复执行」。
    """
    decide = {
        "title": "文书生成需要你的决策",
        "contextDigest": "已通读关联案件材料，需先确认诉讼立场",
        "questions": [{
            "id": "q1",
            "title": "以何种诉讼立场起草起诉状？",
            "reason": "立场决定请求项与事实叙述重点",
            "kind": "single_choice",
            "affects": "诉讼请求",
            "options": [
                {"id": "o1", "label": "主张合同解除并返还本金",
                 "description": "请求项围绕解除与返还", "recommended": True,
                 "recommendationReason": "材料显示被告已明确表示不再履行"},
                {"id": "o2", "label": "主张继续履行并支付违约金",
                 "description": "请求项围绕继续履行"},
            ],
        }],
    }
    return [
        ScriptStep.tool("request_user_decision", decide),
        ScriptStep.tool("docgen_create_draft", lambda messages: {
            "templateId": (user_payload(messages).get("文书生成输入") or {}).get("templateId"),
            "docType": (user_payload(messages).get("文书生成输入") or {}).get("docType"),
            "purpose": (user_payload(messages).get("文书生成输入") or {}).get("purpose"),
        }),
        ScriptStep.final("已按你的立场选择完成起草，草稿可在文书工作台确认提纲并导出。"),
    ]
