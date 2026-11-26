"""插件宿主（LB-HERMES-2026-01 WP10）：manifest 审查、默认禁用、零残留卸载。

插件与 MCP 服务的差别：插件可能声明多工具与更强的能力（写文件、网络、案件读取）。
因此这里只做**准入审查与生命周期管理**，不实现"在 Core 进程内导入第三方代码"。

硬约束：

1. **默认禁用**：未显式 `enable` 的插件不加载、不注册工具、不产生任何副作用。
2. **权限最小化**：manifest 未声明的能力一律视为 `false`；`cases` 为空表示
   无案件读取范围（而不是"全部案件"）。
3. **安装前审查**：许可证白名单、SHA256 必填、危险能力（写文件/网络/Shell）
   需要逐项人工确认；命中许可证黑名单直接拒绝。
4. **卸载零残留**：`uninstall` 清理工具注册、授权、订阅与进程；不自动重启。
5. **不在 Core 内 import 插件代码**：插件必须通过子进程（MCP）或受限 RPC 接入，
   本模块只持有 manifest 与状态。
6. 日志与事件不记录插件命令、环境变量或密钥。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

# 允许商用的许可证（保守白名单；未列出的需人工评估）
LICENSE_ALLOWLIST = (
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0", "Unlicense",
)
# 明确拒绝的许可证（传染性或限制商用）
LICENSE_DENYLIST = ("GPL-2.0", "GPL-3.0", "AGPL-3.0", "SSPL-1.0", "BUSL-1.1", "CC-BY-NC-4.0")

# 高风险能力：需要逐项确认
RISKY_CAPABILITIES = ("fs_write", "network", "shell", "case_read", "credential_access")


class PluginError(RuntimeError):
    pass


@dataclass
class PluginManifest:
    plugin_id: str
    name: str
    version: str
    license: str
    sha256: str
    tools: tuple[str, ...] = ()
    capabilities: dict[str, bool] = field(default_factory=dict)
    cases: tuple[str, ...] = ()
    enabled: bool = False
    source: str = "local://manifest"

    def capability(self, name: str) -> bool:
        return bool(self.capabilities.get(name, False))

    def public(self) -> dict[str, Any]:
        return {
            "pluginId": self.plugin_id, "name": self.name, "version": self.version,
            "license": self.license, "sha256": self.sha256[:16], "tools": list(self.tools),
            "capabilities": {k: bool(v) for k, v in self.capabilities.items()
                             if k in RISKY_CAPABILITIES},
            "cases": list(self.cases), "enabled": self.enabled, "source": self.source,
        }


@dataclass
class PluginState:
    manifest: PluginManifest
    installed: bool = True
    running: bool = False


_PLUGINS: dict[str, PluginState] = {}
_LOCK = threading.Lock()
# 是否已从本地库恢复过安装记录（首次访问插件列表/状态时触发）
_LOADED = False


def review(manifest: PluginManifest) -> dict[str, Any]:
    """准入审查：许可证、哈希、能力声明。返回可安全展示的审查结论。"""
    issues: list[str] = []
    warnings: list[str] = []
    if not manifest.plugin_id or not manifest.name:
        issues.append("缺少 pluginId 或 name")
    if manifest.license in LICENSE_DENYLIST:
        issues.append(f"许可证 {manifest.license} 不允许商用")
    elif manifest.license not in LICENSE_ALLOWLIST:
        warnings.append(f"许可证 {manifest.license} 未在白名单内，需人工确认商用条件")
    if not manifest.sha256 or len(manifest.sha256) < 32:
        issues.append("缺少有效 SHA256（无法校验安装包完整性）")
    for cap in RISKY_CAPABILITIES:
        if manifest.capability(cap):
            warnings.append(f"声明了高风险能力：{cap}")
    if manifest.capability("credential_access"):
        issues.append("不允许插件直接访问凭据；请改为受控凭据代理")
    if not manifest.tools:
        warnings.append("未声明任何工具")
    return {
        "pluginId": manifest.plugin_id,
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "riskyCapabilities": [c for c in RISKY_CAPABILITIES if manifest.capability(c)],
        "licenseAllowed": manifest.license in LICENSE_ALLOWLIST,
    }


def _persist_install(manifest: PluginManifest, *, enabled: bool,
                     granted: list[str] | None = None) -> None:
    """写入安装记录（V54）。持久化失败不阻断内存操作，但会如实抛出。

    插件注册必须落库：否则重启后用户看不到自己授过什么能力，也无法审计或卸载。
    """
    from ..storage import get_db
    from ..workspace import now_iso

    ts = now_iso()
    db = get_db()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO plugin_installations(plugin_id, name, version, license, sha256, "
            "tools_json, capabilities_json, granted_json, cases_json, source, installed, "
            "enabled, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?) "
            "ON CONFLICT(plugin_id) DO UPDATE SET name=excluded.name, version=excluded.version, "
            "license=excluded.license, sha256=excluded.sha256, tools_json=excluded.tools_json, "
            "capabilities_json=excluded.capabilities_json, cases_json=excluded.cases_json, "
            "source=excluded.source, installed=1, updated_at=excluded.updated_at"
            + (", granted_json=excluded.granted_json" if granted is not None else "")
            + (", enabled=excluded.enabled" if granted is not None else ""),
            (
                manifest.plugin_id, manifest.name, manifest.version, manifest.license,
                manifest.sha256,
                json.dumps(list(manifest.tools), ensure_ascii=False),
                json.dumps({k: bool(v) for k, v in manifest.capabilities.items()
                            if k in RISKY_CAPABILITIES}, ensure_ascii=False),
                json.dumps(sorted(granted or []), ensure_ascii=False),
                json.dumps(list(manifest.cases), ensure_ascii=False),
                manifest.source, 1 if enabled else 0, ts, ts,
            ),
        )


def _persist_enabled(plugin_id: str, enabled: bool, granted: list[str] | None = None) -> None:
    from ..storage import get_db
    from ..workspace import now_iso

    db = get_db()
    with db.transaction() as conn:
        if granted is None:
            conn.execute(
                "UPDATE plugin_installations SET enabled = ?, updated_at = ? WHERE plugin_id = ?",
                (1 if enabled else 0, now_iso(), plugin_id),
            )
        else:
            conn.execute(
                "UPDATE plugin_installations SET enabled = ?, granted_json = ?, updated_at = ? "
                "WHERE plugin_id = ?",
                (1 if enabled else 0, json.dumps(sorted(granted), ensure_ascii=False),
                 now_iso(), plugin_id),
            )


def _persist_runtime(plugin_id: str, *, running: bool, error_code: str | None = None) -> None:
    """运行状态只记技术字段；不写命令行、环境变量或错误正文。"""
    from ..storage import get_db
    from ..workspace import now_iso

    db = get_db()
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO plugin_runtime_state(plugin_id, running, last_error_code, "
            "last_started_at, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(plugin_id) DO UPDATE SET running=excluded.running, "
            "last_error_code=excluded.last_error_code, updated_at=excluded.updated_at"
            + (", last_started_at=excluded.last_started_at" if running else ""),
            (plugin_id, 1 if running else 0, error_code, ts if running else None, ts),
        )


def _persist_remove(plugin_id: str) -> None:
    from ..storage import get_db

    db = get_db()
    with db.transaction() as conn:
        conn.execute("DELETE FROM plugin_runtime_state WHERE plugin_id = ?", (plugin_id,))
        conn.execute("DELETE FROM plugin_installations WHERE plugin_id = ?", (plugin_id,))


def load_installed() -> int:
    """从本地库恢复已安装插件（**一律以未启用状态载入**）。

    重启即忘记安装记录会让用户无法审计授权范围；而重启后自动启用第三方代码
    又违反默认禁用原则。因此只恢复 manifest，启用必须重新逐项授权。
    """
    global _LOADED
    _LOADED = True
    from ..storage import get_db

    try:
        rows = get_db().query("SELECT * FROM plugin_installations ORDER BY created_at ASC")
    except Exception:  # noqa: BLE001 库未迁移或不可读时不阻断启动
        return 0
    loaded = 0
    with _LOCK:
        for r in rows:
            try:
                caps = json.loads(r["capabilities_json"] or "{}")
                manifest = PluginManifest(
                    plugin_id=str(r["plugin_id"]), name=str(r["name"]),
                    version=str(r["version"]), license=str(r["license"]),
                    sha256=str(r["sha256"]),
                    tools=tuple(json.loads(r["tools_json"] or "[]")),
                    capabilities={k: bool(v) for k, v in caps.items()
                                  if k in RISKY_CAPABILITIES},
                    cases=tuple(json.loads(r["cases_json"] or "[]")),
                    enabled=False, source=str(r["source"] or "local://manifest"),
                )
                _PLUGINS[manifest.plugin_id] = PluginState(manifest=manifest, installed=True)
                loaded += 1
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return loaded


def install(manifest: PluginManifest) -> dict[str, Any]:
    """登记插件（**不启用**）：审查不通过则拒绝安装。"""
    verdict = review(manifest)
    if not verdict["ok"]:
        raise PluginError("插件审查未通过：" + "；".join(verdict["issues"]))
    _ensure_loaded()
    with _LOCK:
        _PLUGINS[manifest.plugin_id] = PluginState(manifest=manifest, installed=True)
    _persist_install(manifest, enabled=False)
    return {**manifest.public(), "review": verdict}


def plugins_enabled() -> bool:
    """插件总开关 `agent.plugins`（方案 §10.1）；默认关闭。"""
    from .tasks import get_setting

    try:
        value = get_setting("agent.plugins")
    except Exception:  # noqa: BLE001 设置不可读时按关闭处理
        return False
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def enable(plugin_id: str, *, granted_capabilities: list[str] | None = None) -> dict[str, Any]:
    """启用插件：必须逐项授予其声明的高风险能力（默认一个都不给）。

    总开关关闭时拒绝启用——避免"个别插件被启用但系统声明插件已关闭"的分裂状态。
    """
    if not plugins_enabled():
        raise PluginError("插件子系统已关闭：请先在设置中开启 agent.plugins")
    with _LOCK:
        state = _PLUGINS.get(plugin_id)
    if state is None:
        raise PluginError(f"插件未安装：{plugin_id}")
    declared = [c for c in RISKY_CAPABILITIES if state.manifest.capability(c)]
    granted = set(granted_capabilities or [])
    missing = [c for c in declared if c not in granted]
    if missing:
        raise PluginError("未授予以下已声明能力，拒绝启用：" + "、".join(missing))
    # 授权能力不得超出声明（防止 UI 传错把能力放大）
    extra = granted - set(declared)
    if extra:
        raise PluginError("授权能力超出 manifest 声明：" + "、".join(sorted(extra)))
    with _LOCK:
        state.manifest.enabled = True
        state.running = True
    _persist_enabled(plugin_id, True, sorted(granted))
    _persist_runtime(plugin_id, running=True)
    return {**state.manifest.public(), "granted": sorted(granted)}


def disable(plugin_id: str) -> dict[str, Any]:
    """停用插件：撤销运行状态与授权（保留 manifest 以便重新启用）。"""
    with _LOCK:
        state = _PLUGINS.get(plugin_id)
        if state is None:
            raise PluginError(f"插件未安装：{plugin_id}")
        state.manifest.enabled = False
        state.running = False
    # 停用即撤销授权：重新启用必须再逐项确认，不能沿用旧授权
    _persist_enabled(plugin_id, False, [])
    _persist_runtime(plugin_id, running=False)
    return state.manifest.public()


def uninstall(plugin_id: str) -> dict[str, Any]:
    """卸载插件：清理注册、授权与运行状态（不自动重启，不留工具别名）。"""
    with _LOCK:
        state = _PLUGINS.pop(plugin_id, None)
    if state is None:
        raise PluginError(f"插件未安装：{plugin_id}")
    # 清理 MCP 侧的进程与声明（若该插件以 MCP 形式接入）
    try:
        from . import mcp_runtime

        mcp_runtime.shutdown()
    except Exception:  # noqa: BLE001 清理失败不掩盖卸载结果
        pass
    _persist_remove(plugin_id)
    return {
        "pluginId": plugin_id, "uninstalled": True,
        "toolsRemoved": len(state.manifest.tools),
        "restartScheduled": False,
    }


def list_plugins(*, include_disabled: bool = True) -> list[dict[str, Any]]:
    """插件列表；默认不暴露 manifest 细节给未授权上下文。"""
    _ensure_loaded()
    with _LOCK:
        items = list(_PLUGINS.values())
    return [s.manifest.public() for s in items if include_disabled or s.manifest.enabled]


def _ensure_loaded() -> None:
    """首次访问时从本地库恢复安装记录（只恢复 manifest，一律未启用）。"""
    if _LOADED:
        return
    load_installed()


def status() -> dict[str, Any]:
    _ensure_loaded()
    with _LOCK:
        total = len(_PLUGINS)
        enabled = sum(1 for s in _PLUGINS.values() if s.manifest.enabled)
    return {
        "installed": total, "enabled": enabled,
        "defaultEnabled": False, "inProcessImport": False,
        "sandbox": "subprocess-only",
    }


def manifest_from_json(raw: str) -> PluginManifest:
    """解析本地 manifest（JSON）；字段缺失按最小权限处理。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PluginError("manifest 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise PluginError("manifest 必须是对象")
    caps = data.get("capabilities") or {}
    if not isinstance(caps, dict):
        raise PluginError("capabilities 必须是对象")
    return PluginManifest(
        plugin_id=str(data.get("pluginId") or "").strip(),
        name=str(data.get("name") or "").strip(),
        version=str(data.get("version") or "0.0.0"),
        license=str(data.get("license") or "unknown"),
        sha256=str(data.get("sha256") or ""),
        tools=tuple(str(t) for t in (data.get("tools") or [])),
        # 只保留已知能力键，未知能力键一律忽略（避免"发明"权限）
        capabilities={k: bool(caps.get(k)) for k in RISKY_CAPABILITIES},
        cases=tuple(str(c) for c in (data.get("cases") or [])),
        enabled=False,
        source=str(data.get("source") or "local://manifest"),
    )
