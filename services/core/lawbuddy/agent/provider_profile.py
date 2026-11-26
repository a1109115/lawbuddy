"""类型化服务商 Profile（LB-HERMES-2026-01 WP02）。

`model_registry.py` 的字典配置是**唯一的配置来源**（内置预设 + 用户自定义 +
覆盖值）。本模块把它投影为不可变 Profile，供网关、错误分类与评测统一读取，
避免各处重复解释字段含义。

设计约束：

- **不引入第二份配置**：Profile 完全由 `model_registry.all_providers()` 派生，
  不落盘、不缓存用户密钥；新增服务商仍然只需在 registry 增加条目。
- **能力声明显式化**：视觉、流式、JSON 模式、思考档位下发方式、上下文与输出
  上限都在 Profile 中给出明确语义，不再由调用方 `conf.get("xxx")` 猜。
- **错误分类可覆盖**：允许 Profile 声明服务商特有的状态码语义（如把某厂商的
  400 当上下文溢出），未声明时用全局 `error_classifier`。
- 密钥只以 `keySetting` 键名出现；Profile 不含密钥值，可安全序列化进诊断。

新服务商接入只需提供符合 `ProviderProfile` 的 registry 条目并跑 conformance
测试（`evals/m6_hermes_upgrade.py` 的 provider_conformance 门禁），不需要改主 Loop。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import error_classifier, model_protocol, model_registry


@dataclass(frozen=True)
class ProviderProfile:
    """单个服务商的能力与端点描述（不可变）。"""

    provider_id: str
    label: str
    base: str
    path: str
    protocol: str
    key_setting: str | None
    streaming: bool = True
    vision: bool = True
    json_mode: bool = True
    send_reasoning_effort: bool = True
    max_tokens: int = 32768
    reasoning_levels: tuple[str, ...] = ()
    reasoning_default: str = ""
    # 服务商特有错误语义：状态码 → 统一类别（覆盖全局判定）
    status_overrides: dict[int, str] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return f"{self.base.rstrip('/')}{self.path}"

    @property
    def configured_key_hint(self) -> str:
        """设置项键名（不是密钥值；用于 UI 引导用户配置）。"""
        return self.key_setting or f"model.{self.provider_id}ApiKey"

    def classify(self, exc: BaseException, *, status: int | None = None,
                 detail: str | None = None) -> str:
        """按服务商覆盖规则分类，未命中时回落到全局分类器。"""
        if status is not None and int(status) in self.status_overrides:
            return self.status_overrides[int(status)]
        return error_classifier.classify(exc, status=status, detail=detail)

    def public(self) -> dict[str, Any]:
        """可外发描述（不含密钥、不含用户填写的 base 之外的敏感信息）。"""
        return {
            "providerId": self.provider_id,
            "label": self.label,
            "protocol": self.protocol,
            "streaming": self.streaming,
            "vision": self.vision,
            "jsonMode": self.json_mode,
            "sendReasoningEffort": self.send_reasoning_effort,
            "maxTokens": self.max_tokens,
            "reasoningLevels": list(self.reasoning_levels),
            "reasoningDefault": self.reasoning_default,
            "keyConfiguredKey": self.configured_key_hint,
        }


# 已知服务商的错误语义覆盖（仅声明与通用规则不同的部分）
_STATUS_OVERRIDES: dict[str, dict[int, str]] = {
    # 智谱在余额不足时返回 429 但语义是计费问题，需与限流区分
    "zhipu": {429: error_classifier.RATE_LIMIT},
    # 千问兼容模式把超长输入返回 400，文案判定已覆盖；此处显式兜底 413
    "qwen": {413: error_classifier.CONTEXT_OVERFLOW},
}


def _coerce_profile(conf: dict[str, Any]) -> ProviderProfile:
    pid = str(conf.get("id") or "").strip().lower()
    preset = conf.get("reasoningPreset") or model_registry.provider_reasoning_preset(pid)
    levels = tuple(str(x) for x in (preset.get("levels") or []) if str(x))
    return ProviderProfile(
        provider_id=pid,
        label=str(conf.get("label") or pid),
        base=str(conf.get("base") or ""),
        path=str(conf.get("path") or model_protocol.default_path(
            model_protocol.normalize_protocol(conf.get("protocol")))),
        protocol=model_protocol.normalize_protocol(conf.get("protocol")),
        key_setting=(str(conf["keySetting"]) if conf.get("keySetting") else None),
        streaming=bool(conf.get("streaming", True)),
        vision=bool(conf.get("vision", True)),
        json_mode=bool(conf.get("jsonMode", True)),
        send_reasoning_effort=bool(conf.get("sendReasoningEffort", True)),
        max_tokens=int(conf.get("maxTokens") or 32768),
        reasoning_levels=levels,
        reasoning_default=str(preset.get("default") or ""),
        status_overrides=dict(_STATUS_OVERRIDES.get(pid, {})),
    )


def profile_for(provider_id: str | None) -> ProviderProfile | None:
    """按服务商 id 取 Profile；不存在返回 None（不抛错，调用方给可读提示）。"""
    conf = model_registry.get_provider_conf(provider_id)
    return _coerce_profile(conf) if conf else None


def profile_for_model(model_id: str | None) -> ProviderProfile | None:
    """按模型 id 反查其归属服务商的 Profile。"""
    found = model_registry.find_model(model_id)
    if not found:
        return None
    return profile_for(found[0])


def active_profile() -> ProviderProfile | None:
    """当前生效服务商（设置中选中且存在）的 Profile。"""
    return profile_for(model_registry.default_provider_id())


def all_profiles() -> list[ProviderProfile]:
    return [_coerce_profile(conf) for conf in model_registry.all_providers().values()]


# ---------------------------------------------------------------------------
# Conformance：新服务商必须满足的最小契约（评测复用）
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("id", "label", "base", "protocol")


def conformance(provider_id: str | None = None) -> dict[str, Any]:
    """检查 Profile 完整性；返回问题清单（空表示通过）。

    新服务商接入的验收标准：端点可拼装、协议受支持、密钥设置键存在、
    推理档位与 maxTokens 合法。不做网络请求。
    """
    issues: list[str] = []
    targets = (
        [profile_for(provider_id)] if provider_id else all_profiles()
    )
    checked = 0
    for prof in targets:
        if prof is None:
            issues.append(f"服务商不存在：{provider_id}")
            continue
        checked += 1
        conf = model_registry.get_provider_conf(prof.provider_id) or {}
        for key in _REQUIRED_FIELDS:
            if not str(conf.get(key) or "").strip():
                issues.append(f"{prof.provider_id}: 缺少字段 {key}")
        if prof.protocol not in model_protocol.SUPPORTED_PROTOCOLS:
            issues.append(f"{prof.provider_id}: 不支持的协议 {prof.protocol}")
        if not prof.base.startswith(("http://", "https://")):
            issues.append(f"{prof.provider_id}: API 地址不是 http(s)")
        if not prof.path.startswith("/"):
            issues.append(f"{prof.provider_id}: 请求路径必须以 / 开头")
        if prof.max_tokens <= 0 or prof.max_tokens > 1_000_000:
            issues.append(f"{prof.provider_id}: maxTokens 超出合理范围")
        if prof.send_reasoning_effort and not prof.reasoning_levels:
            issues.append(f"{prof.provider_id}: 声明下发思考档位但未提供档位列表")
        for lv in prof.reasoning_levels:
            if lv not in model_registry.LEVEL_ORDER:
                issues.append(f"{prof.provider_id}: 非法思考档位 {lv}")
        for status, category in prof.status_overrides.items():
            if category not in error_classifier.CATEGORIES:
                issues.append(f"{prof.provider_id}: 非法错误类别覆盖 {status}→{category}")
    return {"checked": checked, "issues": issues, "ok": not issues}


def classify_with_profile(
    model_id: str | None, exc: BaseException, *, status: int | None = None,
    detail: str | None = None,
) -> str:
    """按模型归属服务商的规则分类（找不到 Profile 时用全局分类器）。"""
    prof = profile_for_model(model_id)
    if prof is None:
        return error_classifier.classify(exc, status=status, detail=detail)
    return prof.classify(exc, status=status, detail=detail)
