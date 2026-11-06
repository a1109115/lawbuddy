"""模型网关（DEV-AGT-009，技术方案 §10；阅卷升级方案 §8 服务商分层）。

- 服务商：全部由 model_registry 运行时提供——内置 deepseek / 智谱 / 千问 / Kimi
  按官方 OpenAI 兼容文档预设，用户可覆盖地址与模型目录，或新增自定义服务商；
- 模型：归属服务商由模型目录决定（find_model），切换模型即切换端点与 Key；
- API Key 经 DPAPI 加密后存 settings（security.protect，DEV-FND-007 最小版；
  key 不进日志/审计/Prompt）；按服务商专属键隔离，智谱另有内置默认键兜底
  （用户未配置时开箱可用）；
- 结构化输出：要求 JSON，解析失败最多修复 2 次（技术方案 §4.4）；
  首轮流式（on_delta 回调节流推送累积文本，P-010），修复轮降为非流式；
- 调用前由 runner 做权限预检（严格本地禁止外发，技术方案 §7）。
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from http.client import IncompleteRead
from typing import Any

from . import model_protocol, model_registry
from .tasks import get_setting

# ---- 服务商（model_registry：内置官方预设 + 用户自定义，运行时可扩展） ----
# 服务商定义（base/path/协议/模型目录）统一在 model_registry 维护：
# 内置 deepseek / 智谱 / 千问 / kimi 依据各厂商官方 OpenAI 兼容文档预设，
# 用户可在界面覆盖 API 地址与模型目录，或新增自定义服务商。
# 本模块只负责按服务商配置发起请求。

# 默认服务商（用户设置无效值时的兜底；与 registry 默认口径一致）。
# 默认模型不再是代码常量：内置模型目录已清空，模型由用户在「模型设置」中添加，
# 因此默认模型在每次调用时由 model_registry.default_model_id() 解析（见 resolve_model）。
DEFAULT_PROVIDER = "zhipu"
DEFAULT_MODEL = ""

# 单次请求必须有硬上限；多阶段阅卷会进行多次调用，过长的超时会把小案件拖很久。
# 但智谱 GLM-5.3 系列为强制思考模型，单次常需 25s 以上（记忆聚合等大 JSON
# schema 更久），过短会让正常调用被判为失败（模型统一走云端，失败即置 FAILED，
# 不再降级本地）——故非流式取 120s。
TIMEOUT_S = 120
# 传输层重试（幂等安全：仅对未产生任何输出的失败重试）
RETRYABLE_HTTP_CODES = (429, 500, 502, 503, 504)
MAX_TRANSPORT_RETRIES = 2
_RETRY_BACKOFF_S = (1.0, 3.0)
# 流式单次读超时：既是断网/取消感知延迟上限，也是"首个 token 之前的思考等待"
# 上限（思考期间无数据帧）。GLM-5.3 强制思考 + agent 循环多轮工具调用后上下文
# 大，服务端 TTFT（首 token 延迟）实测常超 30s——过短会把正常思考误判为中断
# （2026-09-24 实证：20s 下 chat_with_tools 频发 StreamInterrupted），取 60s。
# 整卷通读等超大输入的 TTFT 随输入量线性增长，调用方可按调用覆盖（通读阶段
# 用 READ_STREAM_TIMEOUT_S），此处是普通调用的默认值。
STREAM_TIMEOUT_S = 60
# 思考失控熔断阈值（chat_with_tools）：reasoning 已累计超过此字符数（≈9~12K
# token）仍零正文、零工具调用时判定为思考死循环，提前中断流（正常深度思考
# 极少到此量级；熔断后由调用方降档立即重试，2026-09-25 实证单轮可省 4 分钟+
# 2 万余 tokens 白耗）
REASONING_RUNAWAY_CHARS = 16_000
# 整卷通读等超大输入调用的首 token 等待上限（秒）：输入 10 万字级时思考 +
# 预填充常需数分钟，60s 默认值会把正常调用误判为超时（2026-09-24 实证：
# read_case_corpus 整卷通读因此失败，模型退化成逐文档手动读取）
READ_STREAM_TIMEOUT_S = 300
MAX_REPAIR_ROUNDS = 2  # JSON 修复上限（技术方案 §4.4）

# 模型思考强度（输入坞「模型」设置）：取值即直接下发给 OpenAI 兼容端点的
# reasoning_effort；off/空值/模型不支持的档位一律不下发参数，保持默认行为
REASONING_EFFORTS = ("low", "medium", "high", "max")

# 模型级思考档位能力与视觉能力统一由 model_registry 的模型目录声明
# （内置预设依据厂商官方文档，用户自定义模型在界面填写），这里只做查询封装，
# 不再维护一份与前端重复的硬编码表。


def resolve_model(model: str | None = None) -> str:
    """实际使用的模型 id。

    调用方未指定（None/空）时取 registry 默认模型（当前服务商的第一个模型）；
    用户一个模型都没添加时返回空串，由 _request 给出可执行的提示。
    """
    return str(model or "").strip() or model_registry.default_model_id()


def reasoning_profile(model: str | None = None) -> dict[str, Any]:
    """模型思考档位能力：{levels: tuple, default: str}；不支持档位时 levels 为空。"""
    mid = resolve_model(model)
    levels = tuple(model_registry.reasoning_levels(mid))
    m = model_registry.model_conf(mid) or {}
    default = str(m.get("reasoningDefault") or "")
    if levels and default not in levels:
        default = levels[-1]
    return {"levels": levels, "default": default if levels else ""}


def supports_vision(model: str | None = None) -> bool:
    """模型是否支持图片理解（未声明的模型按不支持处理，fail-closed）。"""
    return model_registry.supports_vision(resolve_model(model))


# 流式节流（P-010：事件流按批推送，避免高频写库）
STREAM_MIN_CHARS = 24
STREAM_MIN_INTERVAL_S = 0.5


class ModelGatewayError(RuntimeError):
    pass


class ModelNotConfigured(ModelGatewayError):
    """未配置 API Key：调用方应降级为本地分析并提示。"""


class ModelAuthError(ModelGatewayError):
    """认证/授权失败（HTTP 401/403）：Key 无效或过期，重试不会自愈。

    调用方应终止任务并引导用户更新 API Key（更新后重新发起可从检查点续跑）。
    """


class ModelBadRequest(ModelGatewayError):
    """请求被服务端拒绝（HTTP 400）：如 response_format 参数不受支持，调用方可剥参降级重试。

    reasoningRejected / allowedEfforts：错误确属「思考档位不支持」时标记，
    调用方据此改派档位重试，而不是误判成 JSON mode 的问题。
    """

    def __init__(self, message: str, *, reasoning_rejected: bool = False,
                 allowed_efforts: list[str] | None = None):
        super().__init__(message)
        self.reasoning_rejected = reasoning_rejected
        self.allowed_efforts = list(allowed_efforts or [])


class StreamInterrupted(ModelGatewayError):
    """流式响应中断（FR-002）：已接收文本保留在 received，内容不完整。"""

    def __init__(self, received: str, reason: str):
        super().__init__(f"流式响应中断（{reason}）")
        self.received = received


class ReasoningRunaway(ModelGatewayError):
    """思考失控熔断：reasoning 疑似死循环（超阈值仍零正文/零工具调用），已提前中断。

    2026-09-25 实证：GLM-5.3 系 high 档在 topenrouter 偶发思考死循环——reasoning
    烧满整个 max_tokens（32768）后正文 0 字，单轮 6 分钟 + 32K tokens 全部白耗。
    正常深度思考极少超过 1 万 token；熔断后调用方可降档立即重试，不必等烧满。
    """

    def __init__(self, reasoning_chars: int):
        super().__init__(f"模型思考失控（已思考 {reasoning_chars} 字仍无输出），已提前中断")
        self.reasoning_chars = reasoning_chars


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """读取平台错误响应体（智谱返回 {"error":{"code","message"}}），拼进可读错误。"""
    try:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        data = json.loads(body)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            # 平台 code 与 HTTP code 相同（智谱 401 也返回 "401"）时不重复拼接
            suffix = f"（{code}）" if code and code != str(exc.code) else ""
            return f"模型服务返回 {exc.code}{suffix}：{err.get('message')}"
        return f"模型服务返回 {exc.code}：{body[:120]}"
    except Exception:
        return f"模型服务返回 {exc.code}"


def _retry_delay(attempt: int) -> float:
    """第 attempt 次重试前的退避秒数（基础退避 + 随机抖动，防惊群）。"""
    import random

    base = _RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)]
    return base * (0.5 + random.random())


def _http_error_exception(exc: urllib.error.HTTPError) -> ModelGatewayError:
    """把 HTTPError 转成可展示的网关异常（读一次响应体，避免重复消费）。"""
    if exc.code in (401, 403):
        return ModelAuthError(
            f"API Key 无效或无权限（HTTP {exc.code}）：{_http_error_detail(exc)}；"
            "请在「模型 API Key」设置中更新或清除后重新发起任务"
        )
    detail = _http_error_detail(exc)
    if exc.code == 400:
        hint = _vision_unsupported_hint(detail)
        allowed = _allowed_efforts_from_detail(detail)
        return ModelBadRequest(
            f"{detail}{hint}" if hint else detail,
            reasoning_rejected=bool(allowed),
            allowed_efforts=allowed,
        )
    return ModelGatewayError(detail)


def _raise_http_error(exc: urllib.error.HTTPError) -> None:
    raise _http_error_exception(exc)


# 上游拒绝思考档位时的措辞（例：智谱 1210「该模型始终思考，不支持关闭思考；
# 请使用 low、high 或 max」）。只在能解析出可用档位时视为档位问题。
_EFFORT_HINT_RES = (
    re.compile(r"请使用\s*([a-z]+(?:\s*[、,，]\s*[a-z]+)+)"),
    re.compile(r"use\s+([a-z]+(?:\s*[,/]\s*[a-z]+)+)", re.IGNORECASE),
)


def _allowed_efforts_from_detail(detail: str) -> list[str]:
    """从 400 错误文本里解析服务端允许的思考档位（解析不出返回空）。

    模型目录由用户填写，可能与厂商实际能力不一致（如把 glm-5.3 标成
    medium 默认档，而智谱只接受 low/high/max）。这里不猜测原因，只在错误
    明确列出可用档位时返回，交给调用方改派档位重试。
    """
    for pattern in _EFFORT_HINT_RES:
        match = pattern.search(detail)
        if not match:
            continue
        levels = [lv.strip().lower() for lv in re.split(r"[、,，/]", match.group(1))]
        allowed = [lv for lv in levels if lv in REASONING_EFFORTS]
        if allowed:
            return allowed
    return []


def _closest_effort(preferred: str | None, allowed: list[str]) -> str:
    """在服务端允许的档位里挑最接近用户意图的一档。

    「关闭」是强制思考模型无法实现的意图，落到允许的最低档（最接近不思考）；
    其余档位按档位顺序就近取，倾向更强而非更弱（审查结论宁可多思考）。
    """
    order = ["low", "medium", "high", "max"]
    levels = [lv for lv in allowed if lv in order]
    if not levels:
        return allowed[0]
    want = str(preferred or "").strip().lower()
    if want not in order:
        # off / 空 / 未知：最低档（离「不思考」最近）
        return levels[0] if want == "off" else levels[-1]
    target = order.index(want)
    return min(levels, key=lambda lv: (abs(order.index(lv) - target), -order.index(lv)))


# 上游拒绝图片输入时的典型措辞（各厂商中英文报错不一致，取关键词判断）
_VISION_ERROR_HINTS = (
    "image",
    "vision",
    "multimodal",
    "multi-modal",
    "图片",
    "图像",
    "不支持该模型类型",
)


def _vision_unsupported_hint(detail: str) -> str:
    """请求被拒且错误里提到图片时，补一句可直接执行的排查建议。

    模型配置里「图片」默认勾选，用户可能给不支持视觉的模型传了截图；
    这里不猜测原因，只在能判断时把处理办法写清楚（仍需用户自行核实）。
    """
    lowered = detail.lower()
    if not any(k in lowered for k in _VISION_ERROR_HINTS):
        return ""
    return "；该模型可能不支持图片输入：请在「模型设置」中取消勾选该模型的「图片」，或换用支持图片理解的模型后重试"


def get_provider() -> str:
    """当前服务商（settings model.provider；缺省/非法值回退默认智谱）。"""
    return model_registry.default_provider_id()


def set_provider(provider: str) -> None:
    from .tasks import set_setting

    provider = str(provider or "").strip().lower()
    available = model_registry.provider_ids()
    if provider not in available:
        raise ModelGatewayError(f"无效模型服务商：{provider}（可选：{'、'.join(available)}）")
    set_setting("model.provider", provider)


def provider_label(provider: str | None = None) -> str:
    name = provider or get_provider()
    return model_registry.provider_label(name) or name


def provider_for_model(model: str | None = None) -> str:
    """模型实际要走的服务商。

    模型归属由服务商模型目录决定（model_registry.find_model）：自定义模型、
    deepseek-flash 等只在声明它的服务商提供，切换模型即自动切换端点与 Key；
    未声明的模型（如用户直接传了某个 id 但目录里没有）沿用当前服务商。
    """
    name = str(model or "").strip()
    found = model_registry.find_model(name)
    if found:
        return found[0]
    return get_provider()


def provider_for_key_setting(key: str) -> str | None:
    """设置键 → 服务商（model.deepseekApiKey → deepseek）；非服务商专属键返回 None。

    设置写入时必须按「键」而非「当前服务商」定位，否则用户在服务商下拉为智谱时
    保存 DeepSeek Key 会覆盖到智谱的键上。
    """
    target = str(key or "").strip()
    for name, conf in model_registry.all_providers().items():
        if str(conf.get("keySetting") or "") == target:
            return name
    return None


def get_api_key(provider: str | None = None) -> str | None:
    """解析 API Key（按服务商隔离，绝不混用）。

    只读取用户为本服务商配置的 model.<providerId>ApiKey；未配置时返回 None。
    """
    name = provider or get_provider()
    providers = model_registry.all_providers()
    conf = providers.get(name)
    if conf is None:
        # 未知服务商（例如自定义服务商尚未保存时点「测试连通」）：
        # 不复用默认服务商的 Key，避免把别家凭据发到别的端点
        return None
    key_setting = str(conf.get("keySetting") or "")
    value = get_setting(key_setting) if key_setting else None
    if value and str(value).strip():
        return str(value).strip()
    return None


def set_api_key(key: str, provider: str | None = None) -> None:
    """保存 API Key 到当前（或指定）服务商的专属设置键。"""
    from .tasks import set_setting

    name = provider or get_provider()
    conf = model_registry.get_provider_conf(name) or model_registry.get_provider_conf(DEFAULT_PROVIDER) or {}
    key_setting = str(conf.get("keySetting") or f"model.{name}ApiKey")
    set_setting(key_setting, key.strip())


def has_api_key(provider: str | None = None) -> bool:
    return bool(get_api_key(provider))


def normalize_reasoning_effort(value: Any) -> str | None:
    """思考强度取值校验：仅合法档位名通过，其余（含 off/空/未知）返回 None。"""
    level = str(value or "").strip().lower()
    return level if level in REASONING_EFFORTS else None


# 服务端实际拒绝过的思考档位（进程内记忆，键 = 模型 id）：
# 模型目录由用户填写，可能与厂商真实能力不一致（如把 glm-5.3 的默认档写成
# medium，而智谱只接受 low/high/max）。首次被拒后按服务端列出的可用档改派并
# 记住，避免同一进程内反复用同一个必然失败的值撞 400。
_rejected_efforts: dict[str, set[str]] = {}
_allowed_efforts: dict[str, list[str]] = {}


def _clamp_rejected_effort(model: str | None, effort: str | None) -> str | None:
    """已被服务端拒绝的档位改派为最接近的可用档（无可用档信息时原样返回）。"""
    if not effort:
        return effort
    mid = resolve_model(model)
    if effort not in _rejected_efforts.get(mid, set()):
        return effort
    allowed = _allowed_efforts.get(mid) or []
    return _closest_effort(effort, allowed) if allowed else effort


def _record_reasoning_rejection(model: str | None, effort: str | None, exc: "ModelBadRequest") -> str:
    """记录服务端拒绝的档位，返回改派后的档位（供当前请求立即重试）。"""
    mid = resolve_model(model)
    level = str(effort or "").strip().lower()
    if level:
        _rejected_efforts.setdefault(mid, set()).add(level)
    _allowed_efforts[mid] = list(exc.allowed_efforts)
    return _closest_effort(effort, exc.allowed_efforts)


def effective_reasoning_effort(value: Any, model: str | None = None) -> str | None:
    """实际下发的思考强度（模型粒度）：

    - 模型目录未声明档位、或 levels 为空 = 不支持档位调节：一律返回 None
      （UI 设置了也等于没设置，不下发参数，保持模型默认行为）；
    - 用户显式「关闭」（off）：强制思考模型无法真正关闭，映射到最低支持档
      （最接近「不思考」的意图）。2026-09-25 实证：此前 off 落到默认档 high，
      UI 默认状态下问「你好」也深度思考 5909 字仍无正文；
    - value 为空/未传（None）时返回模型默认档位；显式档位不被该模型支持时
      返回 None（同样视为未设置）；
    - 服务商声明不支持档位参数（sendReasoningEffort=False）时返回 None；
    - 该模型曾被服务端拒绝过某档位时，改派服务端列出的最近可用档
      （模型目录是用户填的，可能与厂商真实能力不一致）。
    """
    if not _provider_conf(model).get("sendReasoningEffort"):
        return None
    profile = reasoning_profile(model)
    levels = tuple(profile.get("levels") or ())
    if not levels:
        return None
    if str(value or "").strip().lower() == "off":
        # 模型显式声明了 off（自定义服务商可指定「关闭思考」对应的参数值）
        # 时下发该档；否则把「关闭」映射到最低思考档（强制思考模型无法真正关闭）
        return _clamp_rejected_effort(model, "off" if "off" in levels else levels[0])
    level = normalize_reasoning_effort(value)
    if level is None:
        return _clamp_rejected_effort(model, str(profile.get("default") or ""))
    if level not in levels:
        return None
    return _clamp_rejected_effort(model, level)


def weakest_reasoning_effort(model: str | None = None) -> str | None:
    """模型支持的最低思考档位（不支持档位调节时返回 None）。

    用于提取/调查类子任务（§6.6 第 5 条）：这些步骤只是按问题读原文、检索和汇总，
    不需要与法律综合同等的思考强度。按 levels 声明取首项而非硬编码 "low"，
    避免模型未声明 low 时被静默降级为「不下发档位」。
    """
    if not _provider_conf(model).get("sendReasoningEffort"):
        return None
    levels = reasoning_profile(model).get("levels") or ()
    return levels[0] if levels else None


def strongest_reasoning_effort(model: str | None = None) -> str | None:
    """模型支持的最高思考档位（不支持档位调节时返回 None）。

    用于守门修复轮：引用回查失败、覆盖缺口和产物缺失都需要在原文中精确定位，
    属于本任务里最需要推理强度的局部工作。
    """
    if not _provider_conf(model).get("sendReasoningEffort"):
        return None
    levels = reasoning_profile(model).get("levels") or ()
    return levels[-1] if levels else None


def context_tokens(model: str | None = None) -> int:
    """模型上下文窗口（token）：

    - 指定模型时按其目录声明（内置预设来自官方文档，自定义模型由用户填写；
      用户留空则该模型记为 0，走下面的默认值）；
    - 未指定时读 settings model.contextTokens（用户可覆盖当前默认模型的窗口）；
    - 都不可用时回退保守默认 128K。
    """
    mid = str(model or "").strip()
    if mid:
        conf = model_registry.model_conf(mid)
        if conf and int(conf.get("contextLimit") or 0) >= 32_768:
            return int(conf["contextLimit"])
    try:
        value = int(get_setting("model.contextTokens"))
    except (TypeError, ValueError):
        return 131_072
    return value if value >= 32_768 else 131_072


def max_output_tokens(model: str | None = None) -> int:
    """模型单次输出上限（优先模型目录声明，否则用服务商默认值）。

    用户留空（0）时回落到服务商默认值，再不行用 32K。
    """
    mid = str(model or "").strip()
    conf = model_registry.model_conf(mid)
    if conf and int(conf.get("maxOutput") or 0) > 0:
        return int(conf["maxOutput"])
    return int(_provider_conf(model).get("maxTokens") or 32768)


def _provider_conf(model: str | None = None) -> dict[str, Any]:
    """模型实际使用的服务商配置（按模型目录归属，见 provider_for_model）。"""
    providers = model_registry.all_providers()
    name = provider_for_model(model)
    return providers.get(name) or providers.get(DEFAULT_PROVIDER) or {}


def _protocol(model: str | None = None) -> str:
    """模型实际使用的 API 协议（内置预设为 openai；自定义服务商可选三种）。"""
    return model_protocol.normalize_protocol(_provider_conf(model).get("protocol"))


def _temperature(override: float | None = None) -> float:
    """实际下发温度：显式参数优先；否则读 settings model.temperature（0~1，越界回退 0.2）。"""
    if override is not None:
        return override
    try:
        value = float(get_setting("model.temperature"))
    except (TypeError, ValueError):
        return 0.2
    return value if 0.0 <= value <= 1.0 else 0.2


def _request(body: dict[str, Any], model: str | None = None) -> urllib.request.Request:
    name = provider_for_model(model)
    conf = model_registry.get_provider_conf(name) or {}
    if not resolve_model(model):
        # 内置模型目录已清空、用户还没添加任何模型：给出可执行提示而不是空端点请求
        raise ModelNotConfigured(
            f"{conf.get('label') or name} 尚未添加模型：请在「模型设置」中填写模型 ID 并保存后再试"
        )
    key = get_api_key(name)
    if not key:
        # 按服务商给出可执行提示：模型归属由目录决定，用户可能在设置里
        # 配的是另一家的 Key
        raise ModelNotConfigured(
            f"未配置 {conf.get('label') or name} 的 API Key（请在「模型」设置中选择该服务商并保存）"
        )
    return urllib.request.Request(
        f"{conf.get('base') or ''}{conf.get('path') or model_protocol.default_path(conf.get('protocol'))}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=model_protocol.headers(str(conf.get("protocol") or "openai"), key),
        method="POST",
    )


def list_provider_models(
    provider: str,
    *,
    base: str | None = None,
    key: str | None = None,
    protocol: str | None = None,
) -> list[dict[str, Any]]:
    """调用服务商的模型列表接口（「获取可用模型」）。

    - 三种协议都提供 GET {base}/models（OpenAI / Responses 用 Bearer，
      Anthropic 用 x-api-key + anthropic-version）；
    - base/key/protocol 可由调用方传入（保存前的草稿配置）；缺省用已保存的服务商配置；
    - 返回 [{id, contextLimit, maxOutput}]，平台未声明上限时给保守默认值；
    - 不落日志、不回传密钥；错误统一抛 ModelGatewayError 供界面展示。
    """
    conf = model_registry.get_provider_conf(provider) or {}
    target_base = str(base or conf.get("base") or "").rstrip("/")
    if not target_base:
        raise ModelGatewayError("API 地址为空，无法获取模型列表")
    api_key = key if key is not None else get_api_key(provider)
    if not api_key:
        raise ModelNotConfigured(f"未配置 {conf.get('label') or provider} 的 API Key，无法获取模型列表")
    proto = model_protocol.normalize_protocol(protocol or conf.get("protocol"))
    req = urllib.request.Request(
        f"{target_base}/models",
        headers=model_protocol.headers(proto, api_key),
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _raise_http_error(exc)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ModelGatewayError(f"模型服务不可达：{type(exc).__name__}") from exc
    except json.JSONDecodeError as exc:
        raise ModelGatewayError("模型列表返回非 JSON 响应") from exc

    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ModelGatewayError("模型列表响应结构异常（缺少 data 数组）")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        ctx = row.get("context_length") or row.get("context_window") or 0
        try:
            ctx = int(ctx)
        except (TypeError, ValueError):
            ctx = 0
        out.append(
            {
                "id": mid,
                "label": str(row.get("name") or mid).strip() or mid,
                "contextLimit": ctx if ctx >= 1_000 else 131_072,
                "maxOutput": 32_768,
            }
        )
    return out


# 视觉能力探针用图：64×64 纯红 PNG（约 137 字节，不涉及任何用户数据）。
# 必须是「能被说出来的确定内容」—— 有些模型（如 deepseek-v4-pro）不报错、
# 静默忽略图片照样回话，只看请求成功会把纯文本模型判成支持视觉。
# 因此在下方探针里询问「这张图是什么颜色」，用回答中是否出现颜色词做判定。
# 图片在运行时生成：手抄 base64 字面量容易丢字符导致上游判成格式错误。
def _probe_image() -> str:
    import base64
    import struct
    import zlib

    size = 64
    row = b"\x00" + bytes((220, 20, 60)) * size  # 每行：滤波字节 0 + RGB 像素
    raw = row * size

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# 探针问句与期望看到的颜色词（中英都覆盖，避免因回答语言不同而误判）
_PROBE_VISION_PROMPT = "用一句话回答：这张图片的主色调是什么颜色？只回答颜色名称。"
_PROBE_VISION_EXPECT = ("红", "red", "crimson", "scarlet")


def _vision_answer_ok(content: str) -> bool:
    """回答里是否真的说出了图片颜色 —— 只有这样才能证明模型读到了图片内容。

    没有颜色词说明模型根本没看到图（静默忽略图片）或拒绝看图，两种情况都不能
    算作「图片输入可用」。
    """
    lowered = (content or "").lower()
    return any(word in lowered for word in _PROBE_VISION_EXPECT)


def _probe_chat(
    base: str,
    key: str,
    protocol: str,
    model: str,
    *,
    check_vision: bool = False,
) -> str:
    """用一个最小对话请求验证「地址 + 协议 + 密钥 + 模型 ID」确实可用。

    只发一条极短消息（max_tokens 很小），不落日志、不回传密钥；服务端返回
    可解析响应即视为连通（强制思考模型可能把预算烧在思考上、正文为空，仍算连通）。

    check_vision=True 时附带一张纯色图片并询问颜色，返回模型回答的正文：
    调用方需用 _vision_answer_ok() 判断它是否真的看见了图片。
    """
    proto = model_protocol.normalize_protocol(protocol)
    content: Any = "ping"
    if check_vision:
        content = [
            {"type": "text", "text": _PROBE_VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": _probe_image()}},
        ]
    body = model_protocol.build_body(
        proto,
        model=model,
        messages=[{"role": "user", "content": content}],
        temperature=None,
        # 预算留一点余量：强制思考模型会先消耗思考 token，过小会被判成参数错误；
        # 视觉探针还要留出描述图片的余量
        max_tokens=512 if check_vision else 256,
        stream=False,
    )
    req = urllib.request.Request(
        f"{base}{model_protocol.default_path(proto)}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=model_protocol.headers(proto, key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _raise_http_error(exc)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ModelGatewayError(f"模型服务不可达：{type(exc).__name__}") from exc
    except json.JSONDecodeError as exc:
        raise ModelGatewayError("模型对话返回非 JSON 响应") from exc
    try:
        return str(model_protocol.parse_message(proto, data).get("content") or "")
    except (KeyError, IndexError, TypeError, model_protocol.ProtocolError) as exc:
        raise ModelGatewayError("模型对话响应结构异常（请确认 API 格式是否选对）") from exc


def test_connection(
    provider: str,
    *,
    base: str | None = None,
    key: str | None = None,
    protocol: str | None = None,
    model: str | None = None,
    check_vision: bool = False,
) -> dict[str, Any]:
    """连通性测试。

    - 指定了模型 ID：发一次最小对话请求，真正验证地址 / 协议 / 密钥 / 模型 ID
      这条链路能跑通；
    - check_vision=True（配置里勾了「图片」）：文本通过后再发一次带 1×1 图片的
      请求，确认该模型真的接受图片输入 —— 只测文本无法发现「勾了图片但模型不支持」；
    - 未指定模型：GET /models 探测（便宜、不消耗 token），只说明地址与密钥可用。
    返回 {ok, latencyMs, message}；密钥无效抛 ModelAuthError 由界面提示更新。
    """
    started = time.monotonic()
    target_model = str(model or "").strip()
    try:
        conf = model_registry.get_provider_conf(provider) or {}
        target_base = str(base or conf.get("base") or "").rstrip("/")
        proto = model_protocol.normalize_protocol(protocol or conf.get("protocol"))
        if target_model:
            if not target_base:
                raise ModelGatewayError("API 地址为空，无法测试连通")
            api_key = key if key is not None else get_api_key(provider)
            if not api_key:
                raise ModelNotConfigured(
                    f"未配置 {conf.get('label') or provider or '该服务商'} 的 API Key，无法测试连通"
                )
            _probe_chat(target_base, api_key, proto, target_model)
            if check_vision:
                # 关键：必须让模型说出图片内容。「请求没报错」不足以证明支持视觉 ——
                # deepseek-v4-pro 等模型会静默丢掉图片照常回话，只有回答里出现预期
                # 颜色才算真的看见了图（其他厂商的纯文本模型同理，不按厂商写死）。
                try:
                    answer = _probe_chat(target_base, api_key, proto, target_model, check_vision=True)
                except ModelBadRequest as exc:
                    # 上游明确拒绝图片：证据确凿，直接判定不支持
                    return {
                        "ok": False,
                        "latencyMs": int((time.monotonic() - started) * 1000),
                        "message": (
                            f"模型 {target_model} 可正常对话，但不接受图片输入：{exc}；"
                            "请取消该模型的「图片」勾选后重试"
                        )[:300],
                    }
                except ModelGatewayError as exc:
                    return {
                        "ok": False,
                        "latencyMs": int((time.monotonic() - started) * 1000),
                        "message": f"模型 {target_model} 可正常对话，但图片输入测试失败：{exc}"[:300],
                    }
                if not _vision_answer_ok(answer):
                    # 请求成功但答不出图片内容 = 图片被静默忽略（或不支持），不能算可用
                    return {
                        "ok": False,
                        "latencyMs": int((time.monotonic() - started) * 1000),
                        "message": (
                            f"模型 {target_model} 可正常对话，但未能识别测试图片（回答："
                            f"{answer.strip()[:60] or '空'}），可能不支持图片输入；"
                            "请取消该模型的「图片」勾选后重试"
                        )[:300],
                    }
                return {
                    "ok": True,
                    "latencyMs": int((time.monotonic() - started) * 1000),
                    "message": f"连接正常，模型 {target_model} 可正常对话，图片输入可用",
                }
            return {
                "ok": True,
                "latencyMs": int((time.monotonic() - started) * 1000),
                "message": f"连接正常，模型 {target_model} 可正常对话",
            }
        models = list_provider_models(provider, base=base, key=key, protocol=protocol)
        return {
            "ok": True,
            "latencyMs": int((time.monotonic() - started) * 1000),
            "message": f"连接正常（可用模型 {len(models)} 个）",
        }
    except ModelAuthError:
        raise
    except ModelNotConfigured:
        raise
    except ModelGatewayError as exc:
        return {
            "ok": False,
            "latencyMs": int((time.monotonic() - started) * 1000),
            "message": str(exc)[:200],
        }


class ChatStream:
    """SSE 流式响应（P-010）：按序 yield delta 文本；耗尽后 usage 可读。

    - 平台未回 usage 时按累积文本保守估算，保证用量统计不缺失；
    - 中途断网抛 StreamInterrupted（FR-002：已接收内容保留）。
    """

    def __init__(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        cancel_check: Callable[[], bool] | None = None,
        reasoning_effort: str | None = None,
        json_mode: bool = False,
        stream_timeout: float | None = None,
    ):
        self._messages = messages
        self._model = resolve_model(model)
        self._temperature = temperature
        self._cancel_check = cancel_check
        self._json_mode = bool(json_mode)
        # 按调用覆盖的流式读超时（整卷通读等超大输入 TTFT 长；缺省用默认值）
        self._stream_timeout = float(stream_timeout) if stream_timeout else STREAM_TIMEOUT_S
        # 思考强度按模型档位能力表换算下发（模型不支持档位时为 None 不下发）
        self._reasoning_effort = effective_reasoning_effort(reasoning_effort, self._model)
        self.usage: dict[str, int] = {"promptTokens": 0, "completionTokens": 0}

    def __iter__(self) -> Iterator[str]:
        effort = self._reasoning_effort
        decoder = model_protocol.StreamDecoder(_protocol(self._model))
        received: list[str] = []
        attempt = 0
        while True:
            body = model_protocol.build_body(
                _protocol(self._model),
                model=self._model,
                messages=list(self._messages),
                temperature=self._temperature,
                max_tokens=max_output_tokens(self._model),
                stream=True,
                reasoning=(model_registry.reasoning_param(self._model, effort) if effort else None),
                json_mode=self._json_mode and bool(_provider_conf(self._model).get("jsonMode")),
            )
            req = _request(body, self._model)
            try:
                with urllib.request.urlopen(
                    req, timeout=self._stream_timeout, context=ssl.create_default_context()
                ) as resp:
                    for raw_line in resp:
                        # 逐行检查取消：与 chat_with_tools 对齐，停止在帧间隔内立即生效
                        if self._cancel_check is not None and self._cancel_check():
                            from .tasks import TaskCancelled

                            raise TaskCancelled("用户停止")
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue  # 忽略注释/心跳行
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue  # 单帧损坏跳过，不中断整流
                        if not isinstance(obj, dict):
                            continue  # 标量/心跳帧（如 data: null）跳过，非 dict 无 usage/choices
                        for event in decoder.feed(obj):
                            if event["type"] != "text":
                                continue
                            received.append(event["text"])
                            yield event["text"]
                        if decoder.failed:
                            raise ModelGatewayError(decoder.failed)
                break  # 正常结束
            except urllib.error.HTTPError as exc:
                # 幂等安全重试：仅在零帧接收时（429/5xx 是瞬态；401/403/400 不重试）
                if (
                    exc.code in RETRYABLE_HTTP_CODES
                    and not received
                    and attempt < MAX_TRANSPORT_RETRIES
                ):
                    # 重试退避前检查取消：停止后不再 sleep/重试
                    if self._cancel_check is not None and self._cancel_check():
                        from .tasks import TaskCancelled

                        raise TaskCancelled("用户停止") from exc
                    time.sleep(_retry_delay(attempt))
                    attempt += 1
                    continue
                err = _http_error_exception(exc)
                # 服务端拒绝思考档位：零帧接收时改派可用档立即重试
                if (
                    isinstance(err, ModelBadRequest)
                    and err.reasoning_rejected
                    and not received
                    and attempt < MAX_TRANSPORT_RETRIES
                ):
                    remapped = _record_reasoning_rejection(self._model, effort, err)
                    if remapped != effort:
                        effort = remapped
                        attempt += 1
                        continue
                raise err
            except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead) as exc:
                # F-012：读超时/断连先判用户停止——阻塞读期间取消也能在超时窗口内生效
                if self._cancel_check is not None and self._cancel_check():
                    from .tasks import TaskCancelled

                    raise TaskCancelled("用户停止") from exc
                if not received and attempt < MAX_TRANSPORT_RETRIES:
                    # 零帧接收（首 token 前断网/超时）：退避重试
                    # 退避前检查取消：停止后不再 sleep/重试
                    if self._cancel_check is not None and self._cancel_check():
                        from .tasks import TaskCancelled

                        raise TaskCancelled("用户停止") from exc
                    time.sleep(_retry_delay(attempt))
                    attempt += 1
                    continue
                raise StreamInterrupted("".join(received), type(exc).__name__) from exc
        if decoder.usage["promptTokens"] or decoder.usage["completionTokens"]:
            self.usage = dict(decoder.usage)
        # usage 缺失 fallback：中文 ≈ 1.2 token/字
        if not self.usage["completionTokens"] and received:
            self.usage = {
                "promptTokens": self.usage["promptTokens"],
                "completionTokens": max(1, int(sum(len(s) for s in received) * 1.2)),
            }


def chat(
    messages: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    json_mode: bool = False,
) -> tuple[str, dict[str, int]]:
    """单轮对话调用（非流式），返回 (助手文本, usage{promptTokens, completionTokens})。

    - 传输层瞬态失败（429/5xx/超时/断连）退避重试（最多 2 次）；401/403/400 不重试；
    - temperature：None 时读 settings model.temperature；
    - json_mode：要求原生 JSON 输出（服务商不支持时由调用方剥参重试）；
    - 请求体与响应解析按服务商协议（OpenAI Chat / Responses / Anthropic）适配。
    """
    model = resolve_model(model)
    effort = effective_reasoning_effort(reasoning_effort, model)
    attempt = 0
    while True:
        body = model_protocol.build_body(
            _protocol(model),
            model=model,
            messages=list(messages),
            temperature=_temperature(temperature),
            max_tokens=max_output_tokens(model),
            stream=False,
            reasoning=(model_registry.reasoning_param(model, effort) if effort else None),
            json_mode=json_mode and bool(_provider_conf(model).get("jsonMode")),
        )
        req = _request(body, model)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=ssl.create_default_context()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_HTTP_CODES and attempt < MAX_TRANSPORT_RETRIES:
                time.sleep(_retry_delay(attempt))
                attempt += 1
                continue
            err = _http_error_exception(exc)
            # 服务端拒绝思考档位（模型目录与厂商能力不一致）：改派可用档立即重试。
            # 400 在产出任何 token 之前返回，重试不会重复计费或重复副作用。
            if isinstance(err, ModelBadRequest) and err.reasoning_rejected and attempt < MAX_TRANSPORT_RETRIES:
                remapped = _record_reasoning_rejection(model, effort, err)
                if remapped != effort:
                    effort = remapped
                    attempt += 1
                    continue
            raise err
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < MAX_TRANSPORT_RETRIES:
                time.sleep(_retry_delay(attempt))
                attempt += 1
                continue
            raise ModelGatewayError(f"模型服务不可达：{type(exc).__name__}") from exc
        except json.JSONDecodeError as exc:
            raise ModelGatewayError("模型服务返回非 JSON 响应") from exc

    try:
        parsed = model_protocol.parse_message(_protocol(model), data)
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelGatewayError("模型响应结构异常") from exc
    return str(parsed["content"]), dict(parsed["usage"])


def generate_json(
    system: str,
    user: str,
    schema_hint: str,
    model: str = DEFAULT_MODEL,
    on_delta: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    reasoning_effort: str | None = None,
    stream_timeout: float | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """结构化生成：返回 (解析结果, 累计 usage 含修复轮次)。

    - 原生 JSON mode（服务商支持时）：省修复轮的全量 token 白耗；
      服务端拒绝（400）时剥掉 response_format 降级重试一次；
    - 解析失败带错误反馈重试（最多 2 次），各轮均流式（on_delta 提供时）；
    - cancel_check：流式阻塞读期间的用户停止检查（F-012，超时窗口内生效）；
    - reasoning_effort：模型思考强度（low/medium/high），修复轮次沿用同一强度；
    - stream_timeout：流式读超时按调用覆盖（整卷通读等超大输入传大值）。
    """
    model = resolve_model(model)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": f"{system}\n\n只输出符合以下结构的 JSON，不要任何其他文字：\n{schema_hint}"},
        {"role": "user", "content": user},
    ]
    last_err = "无法解析"
    total_usage = {"promptTokens": 0, "completionTokens": 0}
    # JSON mode 首次启用；被服务端 400 拒绝后本次任务剩余轮次剥参直连
    json_mode_enabled = bool(_provider_conf(model).get("jsonMode"))
    for round_ in range(MAX_REPAIR_ROUNDS + 1):
        if on_delta is not None:
            try:
                content, usage = _chat_json_stream(
                    messages, model, on_delta, cancel_check, reasoning_effort,
                    json_mode=json_mode_enabled, stream_timeout=stream_timeout,
                )
            except ModelBadRequest as exc:
                # 档位不支持不是 JSON mode 的问题：重试剥参只会再白跑一次
                if not json_mode_enabled or exc.reasoning_rejected:
                    raise
                json_mode_enabled = False  # 服务端不支持 response_format：剥参降级
                content, usage = _chat_json_stream(
                    messages, model, on_delta, cancel_check, reasoning_effort,
                    json_mode=False, stream_timeout=stream_timeout,
                )
        else:
            try:
                content, usage = chat(messages, model=model, reasoning_effort=reasoning_effort, json_mode=json_mode_enabled)
            except ModelBadRequest as exc:
                if not json_mode_enabled or exc.reasoning_rejected:
                    raise
                json_mode_enabled = False
                content, usage = chat(messages, model=model, reasoning_effort=reasoning_effort, json_mode=False)
        total_usage["promptTokens"] += usage["promptTokens"]
        total_usage["completionTokens"] += usage["completionTokens"]
        parsed = _try_parse_json(content)
        if parsed is not None:
            return parsed, total_usage
        last_err = f"第 {round_ + 1} 次输出不是合法 JSON"
        messages.append({"role": "assistant", "content": content[:2000]})
        messages.append({"role": "user", "content": f"你上一次的输出无法解析（{last_err}）。请只输出符合要求的 JSON。"})
    raise ModelGatewayError(f"模型结构化输出修复 {MAX_REPAIR_ROUNDS} 次后仍失败（已消耗 {total_usage['promptTokens']}+{total_usage['completionTokens']} tokens）")


def _chat_json_stream(
    messages: list[dict[str, str]],
    model: str,
    on_delta: Callable[[str], None],
    cancel_check: Callable[[], bool] | None = None,
    reasoning_effort: str | None = None,
    json_mode: bool = False,
    stream_timeout: float | None = None,
) -> tuple[str, dict[str, int]]:
    """流式结构化调用：节流推送累积文本（≥24 字或 0.5s），返回 (完整文本, usage)。"""
    stream = ChatStream(
        messages, model, temperature=0.2, cancel_check=cancel_check,
        reasoning_effort=reasoning_effort, json_mode=json_mode,
        stream_timeout=stream_timeout,
    )
    acc = ""
    last_len = 0
    last_emit = time.monotonic()  # 从当前时刻起算（首帧同样走节流）
    for delta in stream:
        acc += delta
        now = time.monotonic()
        if len(acc) - last_len >= STREAM_MIN_CHARS or (now - last_emit) >= STREAM_MIN_INTERVAL_S:
            on_delta(acc)
            last_len, last_emit = len(acc), now
    if len(acc) > last_len:
        on_delta(acc)  # 终帧（确保完整文本可达 UI）
    return acc, stream.usage


def _try_parse_json(content: str) -> dict[str, Any] | None:
    """宽容解析：剥掉 markdown 代码围栏后解析 JSON 对象。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


class ToolCall:
    """模型发起的一次工具调用（OpenAI tool_calls 流式增量聚合结果）。"""

    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.name = name
        self.arguments = arguments

    @property
    def parsed_arguments(self) -> dict[str, Any]:
        """宽容解析参数 JSON（模型输出畸形参数时返回空对象，由工具侧自行兜底）。"""
        try:
            obj = json.loads(self.arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


def chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str = DEFAULT_MODEL,
    on_text_delta: Callable[[str], None] | None = None,
    on_reasoning_delta: Callable[[str], None] | None = None,
    on_tool_args_delta: Callable[[str, str, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    reasoning_effort: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """带工具调用的对话轮次（OpenAI tools 协议，SSE 流式）。

    - tools：OpenAI function 定义清单（{"type":"function","function":{...}}）；
    - on_text_delta(delta)：文本增量实时回调（自主循环的 text 事件源，不节流——
      由调用方决定推送粒度）；
    - on_reasoning_delta(delta)：思考（reasoning_content）增量实时回调（GLM 系
      强制思考模型思考期可达数分钟且无正文输出，调用方借此发进度事件防「假卡死」；
      思考内容不落库不外发，仅用于进度统计）；
    - on_tool_args_delta(call_id, name, total_chars)：工具参数流式累积回调
      （大参数如 render_html 的 HTML 生成可达数十秒，供调用方发进度事件防「假卡死」；
      id 首帧携带后才触发，保证调用方可按 call_id 关联）；
    - 返回 {"content": 全量文本, "toolCalls": [ToolCall], "usage": {...},
      "reasoningChars": 本轮思考累计字符数}；
    - tool_calls 流式帧按 index 聚合（id/name 首帧携带，arguments 分帧拼接）；
    - 零帧接收（首 token 前）遇瞬态失败（超时/断连/429/5xx）退避重试最多 2 次；
    - 中途断网抛 StreamInterrupted（已接收文本保留），取消检查点同 ChatStream。
    """
    model = resolve_model(model)
    effort = effective_reasoning_effort(reasoning_effort, model)
    decoder = model_protocol.StreamDecoder(_protocol(model))
    received: list[str] = []
    reasoning_chars = 0
    calls_by_index: dict[int, dict[str, str]] = {}
    call_order: list[int] = []
    usage = {"promptTokens": 0, "completionTokens": 0}
    usage_raw: dict[str, Any] = {}
    finish_reason: str | None = None
    attempt = 0
    while True:
        body = model_protocol.build_body(
            _protocol(model),
            model=model,
            messages=list(messages),
            temperature=_temperature(temperature),
            max_tokens=max_output_tokens(model),
            stream=True,
            reasoning=(model_registry.reasoning_param(model, effort) if effort else None),
            tools=tools,
        )
        req = _request(body, model)
        try:
            with urllib.request.urlopen(req, timeout=STREAM_TIMEOUT_S, context=ssl.create_default_context()) as resp:
                for raw_line in resp:
                    if cancel_check is not None and cancel_check():
                        from .tasks import TaskCancelled

                        raise TaskCancelled("用户停止")
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    for event in decoder.feed(obj):
                        kind = event["type"]
                        if kind == "text":
                            received.append(event["text"])
                            if on_text_delta is not None:
                                on_text_delta(event["text"])
                        elif kind == "reasoning":
                            reasoning_chars += len(event["text"])
                            if on_reasoning_delta is not None:
                                on_reasoning_delta(event["text"])
                            # 思考失控熔断：reasoning 超阈值仍零正文/零工具调用 →
                            # 疑似死循环，提前中断（不再等烧满 max_tokens 白耗数分钟）
                            if (
                                reasoning_chars >= REASONING_RUNAWAY_CHARS
                                and not received
                                and not calls_by_index
                            ):
                                raise ReasoningRunaway(reasoning_chars)
                        elif kind == "tool_start":
                            idx = int(event["index"])
                            slot = calls_by_index.get(idx)
                            if slot is None:
                                slot = {"id": "", "name": "", "arguments": ""}
                                calls_by_index[idx] = slot
                                call_order.append(idx)
                            if event["id"]:
                                slot["id"] = str(event["id"])
                            if event["name"]:
                                slot["name"] = str(event["name"])
                        elif kind == "tool_args":
                            idx = int(event["index"])
                            slot = calls_by_index.get(idx)
                            if slot is None:
                                slot = {"id": "", "name": "", "arguments": ""}
                                calls_by_index[idx] = slot
                                call_order.append(idx)
                            slot["arguments"] += str(event["text"])
                            if on_tool_args_delta is not None and slot["id"]:
                                on_tool_args_delta(slot["id"], slot["name"], len(slot["arguments"]))
                        elif kind == "usage":
                            usage = {
                                "promptTokens": int(event["promptTokens"]),
                                "completionTokens": int(event["completionTokens"]),
                            }
                            usage_raw = {"prompt_tokens": event["promptTokens"]}
                        elif kind == "finish":
                            finish_reason = str(event["reason"]) or finish_reason
                    if decoder.failed:
                        raise ModelGatewayError(decoder.failed)
            break  # 正常结束
        except urllib.error.HTTPError as exc:
            # 幂等安全重试：仅在零帧接收时（429/5xx 是瞬态；401/403/400 不重试）
            if (
                exc.code in RETRYABLE_HTTP_CODES
                and not received
                and not calls_by_index
                and attempt < MAX_TRANSPORT_RETRIES
            ):
                # 重试退避前检查取消：停止后不再 sleep/重试
                if cancel_check is not None and cancel_check():
                    from .tasks import TaskCancelled

                    raise TaskCancelled("用户停止") from exc
                time.sleep(_retry_delay(attempt))
                attempt += 1
                continue
            err = _http_error_exception(exc)
            # 服务端拒绝思考档位：零帧接收时改派可用档立即重试
            if (
                isinstance(err, ModelBadRequest)
                and err.reasoning_rejected
                and not received
                and not calls_by_index
                and attempt < MAX_TRANSPORT_RETRIES
            ):
                remapped = _record_reasoning_rejection(model, effort, err)
                if remapped != effort:
                    effort = remapped
                    attempt += 1
                    continue
            raise err
        except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead) as exc:
            # F-012：读超时/断连先判用户停止——阻塞读期间取消也能在超时窗口内生效
            if cancel_check is not None and cancel_check():
                from .tasks import TaskCancelled

                raise TaskCancelled("用户停止") from exc
            if not received and not calls_by_index and attempt < MAX_TRANSPORT_RETRIES:
                # 零帧接收（首 token 前断网/超时）：退避重试
                # 退避前检查取消：停止后不再 sleep/重试
                if cancel_check is not None and cancel_check():
                    from .tasks import TaskCancelled

                    raise TaskCancelled("用户停止") from exc
                time.sleep(_retry_delay(attempt))
                attempt += 1
                continue
            raise StreamInterrupted("".join(received), type(exc).__name__) from exc

    usage_final = usage if usage_raw else {
        "promptTokens": 0,
        "completionTokens": max(1, int(sum(len(s) for s in received) * 1.2)) if received else 0,
    }
    tool_calls = [
        ToolCall(calls_by_index[i]["id"] or f"call_{i}", calls_by_index[i]["name"], calls_by_index[i]["arguments"])
        for i in call_order
    ]
    return {
        "content": "".join(received),
        "toolCalls": tool_calls,
        "usage": usage_final,
        # 本轮思考累计字符数（进度展示/空回答诊断用；思考内容本身不落库）
        "reasoningChars": reasoning_chars,
        # 末帧 finish_reason（stop/length/tool_calls/…）：调用方用它区分
        # 「正常空回答」与「思考耗尽输出上限被截断」（2026-09-25 实证：GLM 思考
        # 死循环烧满 max_tokens 后正文 0 字，finish_reason=length）
        "finishReason": finish_reason,
    }
