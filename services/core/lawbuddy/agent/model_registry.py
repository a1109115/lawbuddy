"""模型服务商注册表（内置官方预设 + 用户自定义服务商）。

设计要点：

- 内置预设（deepseek / zhipu / qwen / kimi）依据各厂商官方 API 文档预设
  base_url、路径、协议与模型目录；用户只需填写 API Key 即可使用，也可在
  「自定义设置」中覆盖 API 地址与模型目录（覆盖值存 settings，不修改代码常量）。
- 自定义服务商（OpenAI Chat Completions 兼容）由用户在界面新增，存
  settings `model.customProviders`（JSON 数组），API Key 存
  `model.<providerId>ApiKey`（敏感键，加密落盘）。
- 模型条目统一形状：id / label / contextLimit / maxOutput / vision /
  reasoningLevels / reasoningDefault / reasoningParams，供网关与前端共用。
- 服务商与模型均为运行时可扩展：新增服务商不需要改代码。

本模块不直接发起网络请求，也不读取密钥明文（只由 model_gateway 在发请求时解析）。
"""

from __future__ import annotations

import copy
import re
from typing import Any

from . import model_protocol

# ---- 设置键 ---------------------------------------------------------------
CUSTOM_PROVIDERS_KEY = "model.customProviders"
PROVIDER_OVERRIDES_KEY = "model.providerOverrides"
# 当前选中的服务商 / 上下文窗口 / 温度（沿用既有键名，保持向后兼容）
CURRENT_PROVIDER_KEY = "model.provider"

# 服务商 id 规则：小写字母开头，允许小写字母/数字/连字符
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_MODEL_LIMITS = (1_000, 100_000_000)
# 模型可声明的思考档位（off = 关闭思考，自定义服务商可指定对应 API 参数值）
LEVEL_ORDER = ("off", "low", "medium", "high", "max")

# 各厂商 reasoning_effort 参数可接受的取值（仅用于把档位名收敛到官方值）
_PARAM_ORDER = ("none", "off", "minimal", "low", "medium", "high", "xhigh", "max")

# 内置服务商的官方推理档位预设（2026-11 依据各厂商 API 文档核对）：
# - 智谱 GLM-5.2 / 5.3 系列：reasoning_effort 仅接受 low / high / max
#   （GLM-5.3 传 medium 直接报错；GLM-5.2 虽接受 medium 但官方会映射为 high）；
# - DeepSeek：none / low / high / max（none 即关闭思考）；
# - Kimi K3 系列：low / high / max（官方默认 max）。
# 这些值是「新建模型」时的默认档位与 API 参数值，用户仍可在界面逐项修改；
# 不把 off 放进预设：档位顺序同时决定阶段降档（最低档用于读页），
# 放 off 会让提取阶段直接关闭思考。
REASONING_PRESETS: dict[str, dict[str, Any]] = {
    "zhipu": {
        "levels": ["low", "high", "max"],
        "default": "high",
        "params": {"low": "low", "high": "high", "max": "max"},
    },
    "deepseek": {
        "levels": ["low", "high", "max"],
        "default": "high",
        "params": {"low": "low", "high": "high", "max": "max"},
    },
    "kimi": {
        "levels": ["low", "high", "max"],
        "default": "high",
        "params": {"low": "low", "high": "high", "max": "max"},
    },
}

# 无官方预设的服务商（自定义 / 千问不下发 reasoning_effort）：
# 新建模型时的兜底档位，取 OpenAI 兼容端点最常见的三档
FALLBACK_REASONING_PRESET: dict[str, Any] = {
    "levels": ["low", "medium", "high"],
    "default": "medium",
    "params": {"low": "low", "medium": "medium", "high": "high"},
}


# ---- 内置官方预设 ---------------------------------------------------------
# 只预设官方 API 地址与协议，不预设任何模型（2026-10 调整）：
# 模型目录默认为空，由用户在「模型设置」里自行填写模型 ID（或点「获取可用
# 模型」自动拉取），填完点「测试连通」验证地址 / 密钥 / 模型是否真的可用。
# 地址依据官方文档核对：
# - DeepSeek：https://api.deepseek.com（OpenAI 兼容，官方亦接受 /v1 前缀）
# - 智谱 BigModel：https://open.bigmodel.cn/api/paas/v4（OpenAI 兼容）
# - 阿里云百炼（千问）：https://dashscope.aliyuncs.com/compatible-mode/v1
# - Kimi（月之暗面）：https://api.moonshot.cn/v1（OpenAI 兼容）
BUILTIN_PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "id": "deepseek",
        "label": "DeepSeek 官方",
        "base": "https://api.deepseek.com",
        "path": "/chat/completions",
        "protocol": "openai",
        "keySetting": "model.deepseekApiKey",
        "builtin": True,
        "builtinKey": False,
        "jsonMode": True,
        "sendReasoningEffort": True,
        "maxTokens": 32768,
        "models": [],
    },
    "zhipu": {
        "id": "zhipu",
        "label": "智谱开放平台",
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "path": "/chat/completions",
        "protocol": "openai",
        "keySetting": "model.zhipuApiKey",
        "builtin": True,
        "builtinKey": False,
        "jsonMode": True,
        "sendReasoningEffort": True,
        "maxTokens": 32768,
        "models": [],
    },
    "qwen": {
        "id": "qwen",
        "label": "阿里云百炼（千问）",
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "path": "/chat/completions",
        "protocol": "openai",
        "keySetting": "model.qwenApiKey",
        "builtin": True,
        "builtinKey": False,
        "jsonMode": True,
        # 千问兼容模式用 enable_thinking 控制思考，不下发 reasoning_effort
        "sendReasoningEffort": False,
        "maxTokens": 32768,
        "models": [],
    },
    "kimi": {
        "id": "kimi",
        "label": "Kimi（月之暗面）",
        "base": "https://api.moonshot.cn/v1",
        "path": "/chat/completions",
        "protocol": "openai",
        "keySetting": "model.kimiApiKey",
        "builtin": True,
        "builtinKey": False,
        "jsonMode": True,
        "sendReasoningEffort": True,
        "maxTokens": 32768,
        "models": [],
    },
}

# 内置服务商展示顺序（界面下拉顺序）
BUILTIN_ORDER = ("zhipu", "deepseek", "qwen", "kimi")

# 允许的 API 协议（自定义服务商可选；内置预设沿用官方 OpenAI 兼容协议）：
# - openai：OpenAI Chat Completions（/chat/completions）
# - openai-responses：OpenAI Responses API（/responses）
# - anthropic：Anthropic Messages（/messages）
SUPPORTED_PROTOCOLS = model_protocol.SUPPORTED_PROTOCOLS


# ---- 设置读写（延迟导入，避免模块循环） -----------------------------------


def _get_setting(key: str) -> Any:
    from .tasks import get_setting

    try:
        return get_setting(key)
    except Exception:  # noqa: BLE001  # 工作空间未初始化：按未配置处理
        return None


def _set_setting(key: str, value: Any) -> None:
    from .tasks import set_setting

    set_setting(key, value)


# ---- 自定义服务商 / 覆盖值 -------------------------------------------------


def _normalize_model(raw: dict[str, Any], provider_id: str | None = None) -> dict[str, Any] | None:
    """规范化用户填写的模型条目（非法条目返回 None，由调用方跳过）。

    上下文窗口与最大输出允许留空（存 0）：表示「交给网关按最佳默认值处理」，
    与用户显式填写的数值区分开，避免留空被静默写成某个具体值。
    """
    model_id = str(raw.get("id") or "").strip()
    if not model_id:
        return None

    def _num(value: Any, fallback: int) -> int:
        if value is None or str(value).strip() == "":
            return 0
        try:
            n = int(float(str(value).replace(",", "").strip()))
        except (TypeError, ValueError):
            return fallback
        lo, hi = _MODEL_LIMITS
        return n if lo <= n <= hi else fallback

    levels = [str(x).strip().lower() for x in (raw.get("reasoningLevels") or []) if str(x).strip()]
    levels = [lv for lv in levels if lv in LEVEL_ORDER]
    # 内置服务商按官方文档收敛档位：老数据 / 手填的 medium 等官方不接受的档位
    # 不能进入目录，否则界面会展示并下发一个必然被拒的值（智谱 GLM-5.3 传
    # medium 直接 400）。全部档位都不合法时回落到官方预设，而不是变成「不支持」。
    preset = REASONING_PRESETS.get(str(provider_id or "").strip().lower())
    if preset and levels:
        allowed = [lv for lv in levels if lv in preset["levels"]]
        levels = allowed or list(preset["levels"])
    # 推理默认开启：未显式声明 reasoningEnabled 的条目（老数据 / 未填）按开启处理
    enabled = raw.get("reasoningEnabled")
    enabled = True if enabled is None else bool(enabled)
    if not enabled:
        levels = []
    default = str(raw.get("reasoningDefault") or "").strip().lower()
    if levels and default not in levels:
        default = levels[-1]
    params_raw = raw.get("reasoningParams") or {}
    params = {
        lv: str(params_raw.get(lv) or lv).strip() or lv
        for lv in levels
        if isinstance(params_raw, dict)
    }
    # 内置服务商的参数值同样收敛到官方取值：界面展示与实际下发保持一致，
    # 不留下「设置里写 medium、请求时被悄悄改成 high」的偏差
    if preset:
        allowed_params = list(dict.fromkeys(preset["params"].values()))
        params = {lv: _nearest_param(v, allowed_params) for lv, v in params.items()}
    return {
        "id": model_id,
        "label": str(raw.get("label") or "").strip() or model_id,
        "contextLimit": _num(raw.get("contextLimit"), 131_072),
        "maxOutput": _num(raw.get("maxOutput"), 32_768),
        "vision": bool(raw.get("vision")),
        "reasoningEnabled": enabled,
        "reasoningLevels": levels,
        "reasoningDefault": default if levels else "",
        "reasoningParams": params,
    }


def _normalize_models(raw: Any, provider_id: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        m = _normalize_model(item, provider_id)
        if m is None or m["id"] in seen:
            continue
        seen.add(m["id"])
        out.append(m)
    return out


def _normalize_base(raw: Any) -> str:
    """API 地址规范化：去尾斜杠；允许用户填写到 /v1 或完整端点。"""
    return str(raw or "").strip().rstrip("/")


def _normalize_path(raw: Any, protocol: str = "openai") -> str:
    """请求路径：未填写时按协议取默认路径（自定义服务商可显式覆盖）。"""
    path = str(raw or "").strip()
    if not path:
        return model_protocol.default_path(protocol)
    return path if path.startswith("/") else f"/{path}"


def custom_providers() -> list[dict[str, Any]]:
    """用户自定义服务商（settings model.customProviders）。"""
    raw = _get_setting(CUSTOM_PROVIDERS_KEY)
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip().lower()
        if not _PROVIDER_ID_RE.match(pid) or pid in BUILTIN_PROVIDERS or pid in seen:
            continue
        seen.add(pid)
        protocol = model_protocol.normalize_protocol(item.get("protocol"))
        out.append(
            {
                "id": pid,
                "label": str(item.get("label") or "").strip() or pid,
                "base": _normalize_base(item.get("base")),
                "path": _normalize_path(item.get("path"), protocol),
                "protocol": protocol,
                "keySetting": f"model.{pid}ApiKey",
                "builtin": False,
                "builtinKey": False,
                "jsonMode": bool(item.get("jsonMode", True)),
                # Anthropic Messages 用 thinking.budget_tokens 表达档位，
                # 与其他协议一样需要按档位下发强度，故默认开启
                "sendReasoningEffort": bool(item.get("sendReasoningEffort", True)),
                "maxTokens": int(item.get("maxTokens") or 32768),
                "models": _normalize_models(item.get("models"), pid),
            }
        )
    return out


def provider_overrides() -> dict[str, dict[str, Any]]:
    """内置服务商的用户覆盖（settings model.providerOverrides）。

    只允许覆盖 base（API 地址）与 models（模型目录）；路径/协议等
    影响请求正确性的字段保持官方预设，避免用户误改后无法定位问题。
    """
    raw = _get_setting(PROVIDER_OVERRIDES_KEY)
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    return raw if isinstance(raw, dict) else {}


def _apply_override(conf: dict[str, Any], override: Any) -> dict[str, Any]:
    if not isinstance(override, dict):
        return conf
    out = copy.deepcopy(conf)
    base = _normalize_base(override.get("base"))
    if base:
        out["base"] = base
    models = _normalize_models(override.get("models"), out.get("id"))
    if models:
        out["models"] = models
    out["customized"] = True
    return out


def all_providers() -> dict[str, dict[str, Any]]:
    """全部可用服务商（内置预设 + 覆盖 + 自定义），按展示顺序。

    每个服务商都带上官方推理档位预设（reasoningPreset）：catalog 下发给前端
    用于生成档位行，save/get 返回值保持同一形状（共享契约要求该字段必填）。
    """
    overrides = provider_overrides()
    out: dict[str, dict[str, Any]] = {}
    for pid in BUILTIN_ORDER:
        conf = copy.deepcopy(BUILTIN_PROVIDERS[pid])
        out[pid] = _apply_override(conf, overrides.get(pid))
    for conf in custom_providers():
        out[conf["id"]] = conf
    for pid, conf in out.items():
        conf["reasoningPreset"] = provider_reasoning_preset(pid)
    return out


def get_provider_conf(provider_id: str | None) -> dict[str, Any] | None:
    pid = str(provider_id or "").strip().lower()
    return all_providers().get(pid)


def provider_ids() -> list[str]:
    return list(all_providers())


def default_provider_id() -> str:
    """默认服务商：当前设置为有效值时沿用，否则智谱（有内置默认键）。"""
    current = str(_get_setting(CURRENT_PROVIDER_KEY) or "").strip().lower()
    return current if current in all_providers() else "zhipu"


def provider_models(provider_id: str | None) -> list[dict[str, Any]]:
    conf = get_provider_conf(provider_id)
    return list(conf.get("models") or []) if conf else []


def default_model_id() -> str:
    """默认模型：当前（或默认）服务商的第一个模型；都没有时取任一服务商的第一个。

    内置预设不再带模型目录，全部模型由用户添加；一个模型都没有时返回空串，
    由网关给出「尚未选择模型」的明确提示，而不是静默指向某个已移除的模型。
    """
    conf = get_provider_conf(default_provider_id()) or {}
    models = conf.get("models") or []
    if models:
        return str(models[0]["id"])
    for item in all_providers().values():
        models = item.get("models") or []
        if models:
            return str(models[0]["id"])
    return ""


def find_model(model_id: str | None) -> tuple[str, dict[str, Any]] | None:
    """按模型 id 反查归属服务商与模型条目（同名模型取首个声明者）。"""
    mid = str(model_id or "").strip()
    if not mid:
        return None
    for pid, conf in all_providers().items():
        for m in conf.get("models") or []:
            if m["id"] == mid:
                return pid, m
    return None


def model_conf(model_id: str | None) -> dict[str, Any] | None:
    found = find_model(model_id)
    return found[1] if found else None


def supports_vision(model_id: str | None) -> bool:
    m = model_conf(model_id)
    return bool(m and m.get("vision"))


def reasoning_levels(model_id: str | None) -> list[str]:
    m = model_conf(model_id)
    return list(m.get("reasoningLevels") or []) if m else []


def provider_reasoning_preset(provider_id: str | None) -> dict[str, Any]:
    """服务商的官方推理档位预设（自定义服务商回落通用三档）。"""
    pid = str(provider_id or "").strip().lower()
    preset = REASONING_PRESETS.get(pid) or FALLBACK_REASONING_PRESET
    return {
        "levels": list(preset["levels"]),
        "default": str(preset["default"]),
        "params": dict(preset["params"]),
    }


def _nearest_param(value: str, allowed: list[str]) -> str:
    """在官方允许的取值里挑最接近的一档（倾向更强，避免审查结论被降智）。"""
    candidates = [v for v in allowed if v in _PARAM_ORDER]
    if not candidates:
        return allowed[0]
    want = str(value or "").strip().lower()
    if want in candidates:
        return want
    if want not in _PARAM_ORDER:
        return candidates[-1]
    target = _PARAM_ORDER.index(want)
    return min(candidates, key=lambda v: (abs(_PARAM_ORDER.index(v) - target), -_PARAM_ORDER.index(v)))


def reasoning_param(model_id: str | None, level: str) -> str:
    """档位 → 实际下发的 API 参数值（默认与档位同名，自定义可改）。

    内置服务商的官方文档只接受特定取值（如智谱 GLM-5.3 仅 low/high/max，
    传 medium 直接 400）。用户目录里的参数值若不在官方取值内，按最接近的
    官方档位下发，而不是发一个必然被拒的值。
    """
    found = find_model(model_id)
    m = found[1] if found else {}
    params = m.get("reasoningParams") or {}
    value = str(params.get(level) or level)
    preset = REASONING_PRESETS.get(found[0]) if found else None
    if preset:
        allowed = list(dict.fromkeys(preset["params"].values()))
        value = _nearest_param(value, allowed)
    return value


def provider_label(provider_id: str | None) -> str:
    conf = get_provider_conf(provider_id)
    return str(conf.get("label") or "") if conf else str(provider_id or "")


# ---- 写入（保存 / 删除服务商） ---------------------------------------------


class RegistryError(ValueError):
    """服务商配置校验失败（可直接展示给用户）。"""


def _clean_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    pid = str(payload.get("id") or "").strip().lower()
    label = str(payload.get("label") or "").strip()
    base = _normalize_base(payload.get("base"))
    if not base:
        raise RegistryError("API 地址不能为空")
    if not base.startswith(("http://", "https://")):
        raise RegistryError("API 地址需以 http:// 或 https:// 开头")
    models = _normalize_models(payload.get("models"), pid)
    if not models:
        raise RegistryError("请至少添加一个模型（模型 ID 不能为空）")
    protocol = model_protocol.normalize_protocol(payload.get("protocol"))
    return {
        "id": pid,
        "label": label or pid,
        "base": base,
        "path": _normalize_path(payload.get("path"), protocol),
        "protocol": protocol,
        "jsonMode": bool(payload.get("jsonMode", True)),
        "sendReasoningEffort": bool(payload.get("sendReasoningEffort", True)),
        "maxTokens": int(payload.get("maxTokens") or 32768),
        "models": models,
    }


def save_provider(payload: dict[str, Any]) -> dict[str, Any]:
    """保存服务商配置。

    - 内置服务商：只写入 base 与 models 覆盖（settings model.providerOverrides）；
    - 自定义服务商：新增或更新（settings model.customProviders）。
    API Key 不在此处保存（由 settings.set 单独写入敏感键）。
    """
    if not isinstance(payload, dict):
        raise RegistryError("服务商配置不合法")
    pid = str(payload.get("id") or "").strip().lower()
    if pid in BUILTIN_PROVIDERS:
        conf = _clean_provider_payload({**payload, "id": pid})
        overrides = dict(provider_overrides())
        overrides[pid] = {"base": conf["base"], "models": conf["models"]}
        _set_setting(PROVIDER_OVERRIDES_KEY, overrides)
        return get_provider_conf(pid) or conf

    if not _PROVIDER_ID_RE.match(pid):
        raise RegistryError("Provider ID 需以小写字母开头，仅含小写字母、数字与连字符（2-40 位）")
    conf = _clean_provider_payload({**payload, "id": pid})
    providers = custom_providers()
    replaced = False
    for i, item in enumerate(providers):
        if item["id"] == pid:
            providers[i] = conf
            replaced = True
            break
    if not replaced:
        providers.append(conf)
    _set_setting(CUSTOM_PROVIDERS_KEY, providers)
    return get_provider_conf(pid) or conf


def remove_provider(provider_id: str) -> bool:
    """删除自定义服务商（内置服务商只能恢复默认，不能删除）。

    同时清除该服务商的 API Key 设置键，避免残留凭据。
    """
    pid = str(provider_id or "").strip().lower()
    if pid in BUILTIN_PROVIDERS:
        raise RegistryError("内置服务商不可删除（可在自定义设置中恢复默认模型目录）")
    providers = custom_providers()
    remaining = [p for p in providers if p["id"] != pid]
    if len(remaining) == len(providers):
        return False
    _set_setting(CUSTOM_PROVIDERS_KEY, remaining)
    # 清除该服务商的专属 Key（密文整行置空，不保留任何凭据）
    try:
        from .tasks import set_setting

        set_setting(f"model.{pid}ApiKey", "")
    except Exception:  # noqa: BLE001  # 密钥环不可用时不影响服务商删除
        pass
    if str(_get_setting(CURRENT_PROVIDER_KEY) or "").strip().lower() == pid:
        _set_setting(CURRENT_PROVIDER_KEY, "zhipu")
    return True


def reset_provider(provider_id: str) -> bool:
    """恢复内置服务商的官方预设（清空该服务商的覆盖值）。"""
    pid = str(provider_id or "").strip().lower()
    if pid not in BUILTIN_PROVIDERS:
        return False
    overrides = dict(provider_overrides())
    if pid not in overrides:
        return False
    overrides.pop(pid, None)
    _set_setting(PROVIDER_OVERRIDES_KEY, overrides)
    return True


def catalog() -> dict[str, Any]:
    """下发给前端的完整目录（不含任何密钥明文）。"""
    from .tasks import is_sensitive_setting

    providers: list[dict[str, Any]] = []
    for pid, conf in all_providers().items():
        key_setting = str(conf.get("keySetting") or "")
        providers.append(
            {
                **conf,
                "configured": False,  # 由 service 层按密钥状态补齐
                "keySetting": key_setting,
                "sensitive": is_sensitive_setting(key_setting),
            }
        )
    return {"providers": providers, "defaultProvider": default_provider_id()}
