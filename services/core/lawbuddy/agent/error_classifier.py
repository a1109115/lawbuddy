"""统一错误分类（LB-HERMES-2026-01 WP02）。

把模型网关、存储、工具与流式读取的异常收敛为**有限错误类别**，供重试策略、
用户提示、Span 与 Journal 共用同一份判定。硬约束：

1. 分类只输出类别码，不输出异常正文、URL、密钥或案件内容。
2. 认证 / 权限 / 计费类**永不自动重试**（避免无限失败与费用风险）。
3. 上下文溢出单独成类，交给压缩路径处理，不按普通失败重试。
4. 流式停滞（stale_stream）与网络断连（disconnect）区分：前者可续写，
   后者需重新建连；两者都不允许把不完整工具参数当完整调用执行。
5. 未知异常归为 `unknown`，调用方按**不可重试**处理（保守）。
"""

from __future__ import annotations

import ssl
import urllib.error
from typing import Any

# 统一错误类别（UI、事件、Span、Journal 共用）
AUTH = "auth"
BILLING = "billing"
PERMISSION = "permission"
RATE_LIMIT = "rate_limit"
CONTEXT_OVERFLOW = "context_overflow"
INVALID_REQUEST = "invalid_request"
FORMAT = "format"
TIMEOUT = "timeout"
DISCONNECT = "disconnect"
STALE_STREAM = "stale_stream"
PROVIDER_UNAVAILABLE = "provider_unavailable"
PERSISTENCE = "persistence"
CANCELLED = "cancelled"
UNKNOWN = "unknown"

CATEGORIES = (
    AUTH, BILLING, PERMISSION, RATE_LIMIT, CONTEXT_OVERFLOW, INVALID_REQUEST, FORMAT,
    TIMEOUT, DISCONNECT, STALE_STREAM, PROVIDER_UNAVAILABLE, PERSISTENCE, CANCELLED, UNKNOWN,
)

# 允许自动重试的类别（其余一律不重试）
RETRYABLE = frozenset({RATE_LIMIT, TIMEOUT, DISCONNECT, STALE_STREAM, PROVIDER_UNAVAILABLE})

# 需要用户介入修复配置的类别（任务应停止并给出可操作指引）
NEEDS_USER_ACTION = frozenset({AUTH, BILLING, PERMISSION, INVALID_REQUEST})

# 提示文案：只描述可执行的下一步，不回显任何密钥或响应正文
_USER_HINTS: dict[str, str] = {
    AUTH: "API Key 无效或已过期：请在「模型 API Key」设置中更新后重新发起任务",
    BILLING: "服务账户余额或配额不足：请充值或切换可用服务商后重试",
    PERMISSION: "当前密钥无该模型或接口权限：请确认模型已开通，或更换有权限的密钥",
    RATE_LIMIT: "请求过于频繁：系统已按退避重试；仍失败时请稍后再试",
    CONTEXT_OVERFLOW: "输入超出模型上下文窗口：将先做引证安全压缩再重试",
    INVALID_REQUEST: "请求参数被服务端拒绝：请检查模型与思考档位设置",
    FORMAT: "模型返回格式不符合要求：已保留已完成部分，可重试该步骤",
    TIMEOUT: "模型响应超时：已保留已完成内容，可从未完成处继续",
    DISCONNECT: "网络连接中断：已保留已完成内容，可重新发起续跑",
    STALE_STREAM: "流式响应停滞：已中断本次读取，可继续生成剩余内容",
    PROVIDER_UNAVAILABLE: "模型服务暂时不可用：已按退避重试，请稍后再试",
    PERSISTENCE: "本地存储写入失败：请检查磁盘空间与文件权限后重试",
    CANCELLED: "任务已被用户停止",
    UNKNOWN: "任务执行失败：请稍后重试；若持续失败请检查本地服务状态",
}

# 服务端错误文本里出现这些关键词时判定为上下文溢出（各厂商措辞不一致）
_OVERFLOW_HINTS = (
    "context length", "context_length", "maximum context", "too many tokens",
    "token limit", "input is too long", "prompt is too long", "exceeds the maximum",
    "上下文长度", "超过最大长度", "输入过长", "tokens 超出",
)
_BILLING_HINTS = (
    "insufficient", "balance", "quota", "billing", "payment", "arrears",
    "余额不足", "欠费", "配额", "账户余额",
)
_PERMISSION_HINTS = (
    "permission", "forbidden", "not authorized", "no access", "无权", "未授权", "没有权限",
)
_RATE_HINTS = ("rate limit", "too many requests", "频率", "限流", "请求过于频繁")
_AUTH_HINTS = ("invalid api key", "incorrect api key", "unauthorized", "密钥无效", "认证失败")


def hint_for(category: str) -> str:
    return _USER_HINTS.get(category, _USER_HINTS[UNKNOWN])


def is_retryable(category: str) -> bool:
    return category in RETRYABLE


def needs_user_action(category: str) -> bool:
    return category in NEEDS_USER_ACTION


def _text(exc: BaseException) -> str:
    """取异常用于分类的最小文本（仅内部判定，不写入事件或日志）。"""
    try:
        return f"{type(exc).__name__} {exc}".lower()
    except Exception:  # noqa: BLE001
        return type(exc).__name__.lower()


def _from_http_status(status: int, detail: str) -> str:
    lowered = (detail or "").lower()
    if status in (401,):
        return AUTH
    if status == 402:
        return BILLING
    if status == 403:
        # 403 既可能是权限，也可能是欠费（部分厂商）；文案关键词优先
        if any(k in lowered for k in _BILLING_HINTS):
            return BILLING
        return PERMISSION
    if status == 413:
        return CONTEXT_OVERFLOW
    if status == 429:
        return RATE_LIMIT
    if status == 400:
        if any(k in lowered for k in _OVERFLOW_HINTS):
            return CONTEXT_OVERFLOW
        if any(k in lowered for k in _BILLING_HINTS):
            return BILLING
        if any(k in lowered for k in _PERMISSION_HINTS):
            return PERMISSION
        if any(k in lowered for k in _AUTH_HINTS):
            return AUTH
        return INVALID_REQUEST
    if status == 408:
        return TIMEOUT
    if status in (500, 502, 503, 504, 529):
        return PROVIDER_UNAVAILABLE
    if 400 <= status < 500:
        return INVALID_REQUEST
    if status >= 500:
        return PROVIDER_UNAVAILABLE
    return UNKNOWN


def classify(exc: BaseException, *, status: int | None = None, detail: str | None = None) -> str:
    """把异常归类为统一错误类别。

    - `status` / `detail` 可显式传入（HTTP 错误在响应体已被读取时使用）；
    - 异常自身声明了合法的 `category`（如流式停滞的 stale_stream）时优先采信，
      避免按类型一刀切丢失调用点掌握的更精确语义；
    - 其余情况按异常类型与文本推断；
    - 判定顺序：显式类别 → 取消 → HTTP 状态 → 具体类型 → 文本关键词 → unknown。
    """
    name = type(exc).__name__

    declared = getattr(exc, "category", None)
    if isinstance(declared, str) and declared in CATEGORIES and status is None:
        return declared

    # 取消优先：用户停止不是失败
    if "cancelled" in name.lower() or "cancel" in name.lower():
        return CANCELLED

    if status is not None:
        return _from_http_status(int(status), detail or _text(exc))

    # 模型网关自定义异常按类型映射（避免依赖文本）
    mapping = {
        "ModelAuthError": AUTH,
        "ModelNotConfigured": AUTH,
        "ModelBadRequest": INVALID_REQUEST,
        "ModelGatewayError": PROVIDER_UNAVAILABLE,
        "StreamInterrupted": DISCONNECT,
        "ReasoningRunaway": FORMAT,
        "BudgetExceeded": RATE_LIMIT,
        "RepeatedCallBlocked": INVALID_REQUEST,
        "ContractWorkflowError": INVALID_REQUEST,
        "LeaseLost": PERSISTENCE,
        "JournalError": PERSISTENCE,
    }
    if name in mapping:
        return mapping[name]

    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            body = ""
        return _from_http_status(int(exc.code), body)
    if isinstance(exc, (TimeoutError,)):
        return TIMEOUT
    if isinstance(exc, (ConnectionError, urllib.error.URLError, ssl.SSLError)):
        return DISCONNECT
    if isinstance(exc, MemoryError):
        return PERSISTENCE
    if isinstance(exc, OSError):
        text = _text(exc)
        if "sqlite" in text or "database" in text or "disk" in text or "no space" in text:
            return PERSISTENCE
        return DISCONNECT
    if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
        return INVALID_REQUEST

    text = _text(exc)
    if any(k in text for k in _OVERFLOW_HINTS):
        return CONTEXT_OVERFLOW
    if any(k in text for k in _BILLING_HINTS):
        return BILLING
    if any(k in text for k in _RATE_HINTS):
        return RATE_LIMIT
    if any(k in text for k in _PERMISSION_HINTS):
        return PERMISSION
    if any(k in text for k in _AUTH_HINTS):
        return AUTH
    if "timeout" in text or "timed out" in text:
        return TIMEOUT
    if "stream" in text and ("stall" in text or "idle" in text):
        return STALE_STREAM
    if "json" in text or "parse" in text or "decode" in text:
        return FORMAT
    return UNKNOWN


def describe(exc: BaseException, *, status: int | None = None, detail: str | None = None) -> dict[str, Any]:
    """分类结果（可安全外发：只含类别、可重试性与用户提示）。"""
    category = classify(exc, status=status, detail=detail)
    return {
        "category": category,
        "retryable": is_retryable(category),
        "needsUserAction": needs_user_action(category),
        "hint": hint_for(category),
    }
