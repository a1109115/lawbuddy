"""模型 API 协议适配层（OpenAI Chat Completions / OpenAI Responses / Anthropic Messages）。

- 对话消息在项目内部统一使用 OpenAI Chat Completions 形状（role / content /
  tool_calls / tool_call_id），本模块负责把同一份消息转换为各协议请求体，并把
  各协议响应（含 SSE 流）归一化为同一组事件，供 model_gateway 共用。
- 归一化事件：
  {"type": "text", "text"} / {"type": "reasoning", "text"}
  {"type": "tool_start", "index", "id", "name"} / {"type": "tool_args", "index", "text"}
  {"type": "usage", "promptTokens", "completionTokens"} / {"type": "finish", "reason"}
- 本模块不发起网络请求，也不读取密钥明文；密钥只在 headers() 中落到请求头。
"""

from __future__ import annotations

import json
from typing import Any

SUPPORTED_PROTOCOLS = ("openai", "openai-responses", "anthropic")

PROTOCOL_LABELS = {
    "openai": "OpenAI Chat Completions",
    "openai-responses": "OpenAI Responses API",
    "anthropic": "Anthropic Messages",
}

# 各协议的默认请求路径（用户填的 API 地址为 base，路径按协议推导）
DEFAULT_PATHS = {
    "openai": "/chat/completions",
    "openai-responses": "/responses",
    "anthropic": "/messages",
}

ANTHROPIC_VERSION = "2023-06-01"

# Anthropic 思考预算（thinking.budget_tokens）按档位换算；接口要求
# budget_tokens >= 1024 且小于 max_tokens，build_body 内做收敛
_ANTHROPIC_BUDGETS = {"low": 1024, "medium": 2048, "high": 4096, "max": 8192}


class ProtocolError(ValueError):
    """协议配置或响应结构异常（可直接展示给用户）。"""


def normalize_protocol(value: Any) -> str:
    """校验协议取值，非法值回落 OpenAI Chat Completions。"""
    proto = str(value or "").strip().lower()
    return proto if proto in SUPPORTED_PROTOCOLS else "openai"


def default_path(protocol: str) -> str:
    return DEFAULT_PATHS[normalize_protocol(protocol)]


def protocol_label(protocol: str) -> str:
    return PROTOCOL_LABELS[normalize_protocol(protocol)]


def headers(protocol: str, api_key: str) -> dict[str, str]:
    """请求头（密钥仅在此处出现，绝不落日志）。"""
    proto = normalize_protocol(protocol)
    if proto == "anthropic":
        return {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


# ---------------------------------------------------------------------------
# 消息转换
# ---------------------------------------------------------------------------


def _text_of(content: Any) -> str:
    """把消息 content 拍平成纯文本（忽略图片分片）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


def _json_or_empty(raw: Any) -> dict[str, Any]:
    try:
        obj = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _data_url(part: dict[str, Any]) -> str:
    image = part.get("image_url")
    if isinstance(image, dict):
        return str(image.get("url") or "")
    return str(image or "")


def _anthropic_image(part: dict[str, Any]) -> dict[str, Any] | None:
    url = _data_url(part)
    if not url:
        return None
    if url.startswith("data:"):
        # data:image/png;base64,XXXX
        try:
            head, data = url.split(",", 1)
        except ValueError:
            return None
        media = head[5:].split(";")[0] or "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _append_turn(turns: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    """追加一轮消息；相邻同角色轮次合并（Anthropic 要求 user/assistant 交替）。"""
    if not blocks:
        return
    if turns and turns[-1]["role"] == role:
        turns[-1]["content"].extend(blocks)
        return
    turns.append({"role": role, "content": blocks})


def anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """内部消息 → (system 文本, Anthropic messages 数组)。"""
    system_parts: list[str] = []
    turns: list[dict[str, Any]] = []
    for m in messages:
        role = str(m.get("role") or "")
        if role == "system":
            text = _text_of(m.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            _append_turn(
                turns,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(m.get("tool_call_id") or ""),
                        "content": _text_of(m.get("content")),
                    }
                ],
            )
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = _text_of(m.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tc.get("id") or ""),
                        "name": str(fn.get("name") or ""),
                        "input": _json_or_empty(fn.get("arguments")),
                    }
                )
            _append_turn(turns, "assistant", blocks)
            continue
        blocks = []
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    blocks.append({"type": "text", "text": str(part.get("text") or "")})
                elif part.get("type") == "image_url":
                    image = _anthropic_image(part)
                    if image:
                        blocks.append(image)
        else:
            text = str(content or "")
            if text:
                blocks.append({"type": "text", "text": text})
        _append_turn(turns, "user", blocks)
    return "\n\n".join(system_parts), turns


def responses_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """内部消息 → (instructions 文本, Responses API input 数组)。"""
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for m in messages:
        role = str(m.get("role") or "")
        if role == "system":
            text = _text_of(m.get("content"))
            if text:
                instructions.append(text)
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(m.get("tool_call_id") or ""),
                    "output": _text_of(m.get("content")),
                }
            )
            continue
        if role == "assistant":
            text = _text_of(m.get("content"))
            if text:
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(tc.get("id") or ""),
                        "name": str(fn.get("name") or ""),
                        "arguments": str(fn.get("arguments") or "{}"),
                    }
                )
            continue
        blocks = []
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    blocks.append({"type": "input_text", "text": str(part.get("text") or "")})
                elif part.get("type") == "image_url":
                    url = _data_url(part)
                    if url:
                        blocks.append({"type": "input_image", "image_url": url})
        else:
            text = str(content or "")
            if text:
                blocks.append({"type": "input_text", "text": text})
        if blocks:
            items.append({"role": "user", "content": blocks})
    return "\n\n".join(instructions), items


def _anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = tool.get("function") or {}
        out.append(
            {
                "name": str(fn.get("name") or ""),
                "description": str(fn.get("description") or ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def _responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = tool.get("function") or {}
        out.append(
            {
                "type": "function",
                "name": str(fn.get("name") or ""),
                "description": str(fn.get("description") or ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def _anthropic_thinking(reasoning: str | None, max_tokens: int) -> dict[str, Any] | None:
    """档位 → thinking 预算；预算必须 >=1024 且小于 max_tokens，否则关闭思考。"""
    level = str(reasoning or "").strip().lower()
    if not level or level == "off":
        return None
    try:
        budget = int(level)
    except ValueError:
        budget = _ANTHROPIC_BUDGETS.get(level, 2048)
    budget = max(1024, budget)
    if max_tokens <= budget:
        return None
    return {"type": "enabled", "budget_tokens": budget}


def build_body(
    protocol: str,
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float | None,
    max_tokens: int,
    stream: bool,
    reasoning: str | None = None,
    json_mode: bool = False,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """按协议构造请求体。

    reasoning 为已换算好的档位名（或自定义参数值），None/off 表示不下发。
    """
    proto = normalize_protocol(protocol)
    if proto == "anthropic":
        system, turns = anthropic_messages(messages)
        body: dict[str, Any] = {
            "model": model,
            "messages": turns,
            "max_tokens": max(1, int(max_tokens)),
            "stream": bool(stream),
        }
        if system:
            body["system"] = system
        thinking = _anthropic_thinking(reasoning, int(body["max_tokens"]))
        if thinking:
            # 官方约束：开启 extended thinking 时 temperature 必须为 1
            body["thinking"] = thinking
            body["temperature"] = 1.0
        elif temperature is not None:
            body["temperature"] = min(1.0, max(0.0, float(temperature)))
        if tools:
            body["tools"] = _anthropic_tools(tools)
        return body

    if proto == "openai-responses":
        instructions, items = responses_input(messages)
        body = {
            "model": model,
            "input": items,
            "max_output_tokens": max(1, int(max_tokens)),
            "stream": bool(stream),
        }
        if instructions:
            body["instructions"] = instructions
        if temperature is not None:
            body["temperature"] = temperature
        if reasoning:
            body["reasoning"] = {"effort": str(reasoning)}
        if json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        if tools:
            body["tools"] = _responses_tools(tools)
        return body

    body = {
        "model": model,
        "messages": messages,
        "stream": bool(stream),
    }
    if temperature is not None:
        body["temperature"] = temperature
    if stream:
        body["stream_options"] = {"include_usage": True}
    if reasoning:
        body["reasoning_effort"] = str(reasoning)
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if tools:
        body["tools"] = tools
    body["max_tokens"] = max(1, int(max_tokens))
    return body


# ---------------------------------------------------------------------------
# 非流式响应解析
# ---------------------------------------------------------------------------


def parse_message(protocol: str, data: dict[str, Any]) -> dict[str, Any]:
    """解析非流式响应 → {content, toolCalls[{id,name,arguments}], usage, finishReason}。"""
    proto = normalize_protocol(protocol)
    if proto == "anthropic":
        blocks = data.get("content") or []
        texts: list[str] = []
        calls: list[dict[str, str]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    {
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    }
                )
        usage_raw = data.get("usage") or {}
        return {
            "content": "".join(texts),
            "toolCalls": calls,
            "usage": {
                "promptTokens": int(usage_raw.get("input_tokens") or 0),
                "completionTokens": int(usage_raw.get("output_tokens") or 0),
            },
            "finishReason": data.get("stop_reason"),
        }

    if proto == "openai-responses":
        texts = []
        calls = []
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        texts.append(str(part.get("text") or ""))
            elif item.get("type") == "function_call":
                calls.append(
                    {
                        "id": str(item.get("call_id") or item.get("id") or ""),
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    }
                )
        usage_raw = data.get("usage") or {}
        return {
            "content": "".join(texts),
            "toolCalls": calls,
            "usage": {
                "promptTokens": int(usage_raw.get("input_tokens") or 0),
                "completionTokens": int(usage_raw.get("output_tokens") or 0),
            },
            "finishReason": data.get("status"),
        }

    try:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
    except (IndexError, AttributeError) as exc:
        raise ProtocolError("模型响应结构异常") from exc
    calls = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        calls.append(
            {
                "id": str(tc.get("id") or ""),
                "name": str(fn.get("name") or ""),
                "arguments": str(fn.get("arguments") or "{}"),
            }
        )
    usage_raw = data.get("usage") or {}
    return {
        "content": str(message.get("content") or ""),
        "toolCalls": calls,
        "usage": {
            "promptTokens": int(usage_raw.get("prompt_tokens") or 0),
            "completionTokens": int(usage_raw.get("completion_tokens") or 0),
        },
        "finishReason": choice.get("finish_reason"),
    }


# ---------------------------------------------------------------------------
# 流式响应解析
# ---------------------------------------------------------------------------


class StreamDecoder:
    """把各协议的 SSE 数据帧归一化为统一事件。

    用法：逐帧 `for event in decoder.feed(obj)`；流结束后读 `usage`/`finish_reason`。
    """

    def __init__(self, protocol: str):
        self.protocol = normalize_protocol(protocol)
        self.usage = {"promptTokens": 0, "completionTokens": 0}
        self.finish_reason: str | None = None
        self.failed: str | None = None
        # Responses：item_id → 工具调用序号
        self._call_index: dict[str, int] = {}

    def feed(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        if self.protocol == "anthropic":
            return self._feed_anthropic(obj)
        if self.protocol == "openai-responses":
            return self._feed_responses(obj)
        return self._feed_openai(obj)

    # -- OpenAI Chat Completions ------------------------------------------
    def _feed_openai(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        usage_raw = obj.get("usage") or {}
        if usage_raw:
            self.usage = {
                "promptTokens": int(usage_raw.get("prompt_tokens") or 0),
                "completionTokens": int(usage_raw.get("completion_tokens") or 0),
            }
            events.append({"type": "usage", **self.usage})
        choices = obj.get("choices") or []
        if not choices:
            return events
        choice = choices[0] if isinstance(choices[0], dict) else {}
        if choice.get("finish_reason"):
            self.finish_reason = str(choice["finish_reason"])
            events.append({"type": "finish", "reason": self.finish_reason})
        delta = choice.get("delta") or {}
        text = delta.get("content")
        if text:
            events.append({"type": "text", "text": str(text)})
        reasoning = delta.get("reasoning_content")
        if reasoning:
            events.append({"type": "reasoning", "text": str(reasoning)})
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            index = int(tc.get("index") or 0)
            if tc.get("id") or tc.get("function", {}).get("name"):
                events.append(
                    {
                        "type": "tool_start",
                        "index": index,
                        "id": str(tc.get("id") or ""),
                        "name": str((tc.get("function") or {}).get("name") or ""),
                    }
                )
            args = (tc.get("function") or {}).get("arguments")
            if args:
                events.append({"type": "tool_args", "index": index, "text": str(args)})
        return events

    # -- Anthropic Messages ------------------------------------------------
    def _feed_anthropic(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        kind = str(obj.get("type") or "")
        if kind == "error":
            err = obj.get("error") or {}
            self.failed = str(err.get("message") or "模型服务返回错误")
            return events
        if kind == "message_start":
            usage_raw = (obj.get("message") or {}).get("usage") or {}
            self.usage = {
                "promptTokens": int(usage_raw.get("input_tokens") or 0),
                "completionTokens": int(usage_raw.get("output_tokens") or 0),
            }
            events.append({"type": "usage", **self.usage})
        elif kind == "content_block_start":
            index = int(obj.get("index") or 0)
            block = obj.get("content_block") or {}
            if block.get("type") == "tool_use":
                events.append(
                    {
                        "type": "tool_start",
                        "index": index,
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                    }
                )
        elif kind == "content_block_delta":
            index = int(obj.get("index") or 0)
            delta = obj.get("delta") or {}
            delta_type = str(delta.get("type") or "")
            if delta_type == "text_delta" and delta.get("text"):
                events.append({"type": "text", "text": str(delta["text"])})
            elif delta_type == "thinking_delta" and delta.get("thinking"):
                events.append({"type": "reasoning", "text": str(delta["thinking"])})
            elif delta_type == "input_json_delta" and delta.get("partial_json"):
                events.append({"type": "tool_args", "index": index, "text": str(delta["partial_json"])})
        elif kind == "message_delta":
            stop = (obj.get("delta") or {}).get("stop_reason")
            if stop:
                self.finish_reason = str(stop)
                events.append({"type": "finish", "reason": self.finish_reason})
            usage_raw = obj.get("usage") or {}
            if usage_raw.get("output_tokens") is not None:
                self.usage = {
                    "promptTokens": self.usage["promptTokens"] or int(usage_raw.get("input_tokens") or 0),
                    "completionTokens": int(usage_raw.get("output_tokens") or 0),
                }
                events.append({"type": "usage", **self.usage})
        return events

    # -- OpenAI Responses --------------------------------------------------
    def _feed_responses(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        kind = str(obj.get("type") or "")
        if kind in ("response.failed", "error"):
            err = obj.get("error") or (obj.get("response") or {}).get("error") or {}
            self.failed = str(err.get("message") or "模型服务返回错误")
            return events
        if kind == "response.output_item.added":
            item = obj.get("item") or {}
            if item.get("type") == "function_call":
                item_id = str(item.get("id") or item.get("call_id") or "")
                index = len(self._call_index)
                self._call_index[item_id] = index
                events.append(
                    {
                        "type": "tool_start",
                        "index": index,
                        "id": str(item.get("call_id") or item_id),
                        "name": str(item.get("name") or ""),
                    }
                )
        elif kind == "response.function_call_arguments.delta":
            item_id = str(obj.get("item_id") or "")
            if item_id in self._call_index and obj.get("delta"):
                events.append(
                    {"type": "tool_args", "index": self._call_index[item_id], "text": str(obj["delta"])}
                )
        elif kind == "response.output_text.delta":
            if obj.get("delta"):
                events.append({"type": "text", "text": str(obj["delta"])})
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            if obj.get("delta"):
                events.append({"type": "reasoning", "text": str(obj["delta"])})
        elif kind in ("response.completed", "response.incomplete"):
            response = obj.get("response") or {}
            usage_raw = response.get("usage") or {}
            self.usage = {
                "promptTokens": int(usage_raw.get("input_tokens") or 0),
                "completionTokens": int(usage_raw.get("output_tokens") or 0),
            }
            events.append({"type": "usage", **self.usage})
            self.finish_reason = str(response.get("status") or "")
            if self.finish_reason:
                events.append({"type": "finish", "reason": self.finish_reason})
        return events
