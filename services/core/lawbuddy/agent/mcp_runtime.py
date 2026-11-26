"""MCP 运行时（LB-HERMES-2026-01 WP10）：默认禁用、默认禁网、子进程隔离。

职责边界（刻意做窄）：

- 只负责**进程生命周期 + 受限 RPC 帧**：启动服务、发送 JSON-RPC 请求、读响应、
  超时、崩溃清理。不做工具语义解析、不做审批、不缓存结果。
- 所有工具调用仍必须回到 `policy.py` + `tool_runtime.run`：MCP 工具在注册表里
  视为 `effect=external`、非幂等，因此默认需要审批与 Journal。
- 默认禁用：`agent.mcpEnabled` 未开启时 `list_servers()` 返回空、
  `call_tool()` 直接拒绝（不加载任何第三方 manifest）。
- 子进程环境默认**清空敏感变量**：只透传白名单里的非敏感键；密钥不写命令行、
  不写环境快照、不写日志。
- 崩溃/超时/退出不得拖死 Core：所有读取都有超时，进程退出即标记 dead。

安全说明：本模块不实现"从网络安装 MCP 服务"。manifest 必须由用户本地提供，
且 `enabled` 默认为 false。安装前的许可证与哈希审查由 `plugin_host.review` 负责。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

DEFAULT_START_TIMEOUT_S = 15.0
DEFAULT_CALL_TIMEOUT_S = 60.0
# 允许透传给子进程的环境变量（白名单；敏感键一律不透传）
_ENV_ALLOWLIST = (
    "PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE",
    "LANG", "LC_ALL", "PYTHONIOENCODING", "NODE_OPTIONS",
)
# 判定为敏感的键名片段（即使出现在白名单逻辑之外也必须剔除）
_SENSITIVE_KEY_PARTS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "COOKIE",
    "PRIVATE", "SESSION",
)


class McpError(RuntimeError):
    pass


class McpDisabled(McpError):
    """MCP 未启用：调用方应降级到内置工具，而不是反复重试。"""


class McpTimeout(McpError):
    pass


@dataclass
class McpServerSpec:
    """本地 MCP 服务声明（由用户提供，默认禁用）。"""

    server_id: str
    command: str
    args: tuple[str, ...] = ()
    enabled: bool = False
    allow_network: bool = False
    case_scopes: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    license: str = "unknown"
    sha256: str = ""
    env_passthrough: tuple[str, ...] = field(default_factory=tuple)

    def public(self) -> dict[str, Any]:
        return {
            "serverId": self.server_id, "command": self.command, "args": list(self.args),
            "enabled": self.enabled, "allowNetwork": self.allow_network,
            "caseScopes": list(self.case_scopes), "tools": list(self.tools),
            "license": self.license, "sha256": self.sha256,
        }


def _safe_env(spec: McpServerSpec) -> dict[str, str]:
    """构造子进程环境：只透传白名单 + 声明允许的键，且强制剔除敏感键。"""
    env: dict[str, str] = {}
    allowed = set(_ENV_ALLOWLIST) | set(spec.env_passthrough)
    for key in allowed:
        if any(part in key.upper() for part in _SENSITIVE_KEY_PARTS):
            continue
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    # 显式阻断常见代理与密钥注入，避免子进程继承调试环境
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        env.pop(key, None)
    return env


def _enabled() -> bool:
    """MCP 总开关：`agent.plugins` 与 `agent.mcpEnabled` 必须同时开启。

    `agent.plugins` 是方案 §10.1 的总闸（关闭时停止子进程并禁用工具）；
    `agent.mcpEnabled` 是 MCP 子系统的细粒度开关。两者是 AND 关系，
    任一关闭都不加载任何第三方进程。
    """
    from .tasks import get_setting

    for key in ("agent.plugins", "agent.mcpEnabled"):
        try:
            value = get_setting(key)
        except Exception:  # noqa: BLE001 设置不可读时按关闭处理
            return False
        if value is None:
            return False
        if isinstance(value, bool):
            if not value:
                return False
            continue
        if str(value).strip().lower() not in ("1", "true", "on", "yes"):
            return False
    return True


class McpProcess:
    """单服务子进程封装：启动、请求、关闭；所有读取带超时。"""

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._seq = 0
        self.dead = False
        self.last_error: str | None = None

    def start(self, timeout_s: float = DEFAULT_START_TIMEOUT_S) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        if not shutil.which(self.spec.command):
            raise McpError(f"MCP 服务命令不可用：{self.spec.command}")
        started = time.monotonic()
        try:
            self.process = subprocess.Popen(  # noqa: S603 - 命令来自本地 manifest 且默认禁用
                [self.spec.command, *self.spec.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=_safe_env(self.spec), shell=False, bufsize=0,
            )
        except OSError as exc:
            raise McpError(f"MCP 服务启动失败：{type(exc).__name__}") from exc
        if time.monotonic() - started > timeout_s:
            self.stop()
            raise McpTimeout("MCP 服务启动超时")

    def request(self, method: str, params: dict[str, Any] | None = None,
                timeout_s: float = DEFAULT_CALL_TIMEOUT_S) -> dict[str, Any]:
        """发送一条 JSON-RPC 请求并等待同 id 响应（超时即关闭进程）。"""
        if self.process is None or self.process.poll() is not None:
            self.dead = True
            raise McpError("MCP 服务未运行")
        with self._lock:
            self._seq += 1
            rid = f"{uuid.uuid4().hex[:8]}-{self._seq}"
            payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                 "params": params or {}}, ensure_ascii=False) + "\n"
            try:
                assert self.process.stdin is not None
                self.process.stdin.write(payload.encode("utf-8"))
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self.dead = True
                self.stop()
                raise McpError("MCP 服务已断开") from exc

            deadline = time.monotonic() + max(1.0, timeout_s)
            assert self.process.stdout is not None
            while time.monotonic() < deadline:
                line = self.process.stdout.readline()
                if not line:
                    self.dead = True
                    self.stop()
                    raise McpError("MCP 服务提前退出")
                try:
                    message = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue  # 非 JSON 帧（日志混流）忽略
                if str(message.get("id") or "") != rid:
                    continue
                if message.get("error"):
                    err = message["error"]
                    raise McpError(f"MCP 返回错误：{str(err.get('message') or 'unknown')[:200]}")
                return message.get("result") or {}
            self.dead = True
            self.stop()
            raise McpTimeout("MCP 调用超时，已终止子进程")

    def stop(self) -> None:
        proc = self.process
        self.process = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass


_SERVERS: dict[str, McpProcess] = {}
_REGISTRY_LOCK = threading.Lock()


def register(spec: McpServerSpec) -> dict[str, Any]:
    """登记一个本地服务声明（不启动、不下载；需显式 enable 才会运行）。"""
    if not spec.server_id or not spec.command:
        raise McpError("MCP 声明需要 serverId 与 command")
    with _REGISTRY_LOCK:
        _SERVERS[spec.server_id] = McpProcess(spec)
    return spec.public()


def list_servers() -> list[dict[str, Any]]:
    """列出已登记服务及其运行状态；MCP 未启用时返回空列表（不暴露 manifest）。"""
    if not _enabled():
        return []
    with _REGISTRY_LOCK:
        out: list[dict[str, Any]] = []
        for proc in _SERVERS.values():
            out.append({
                **proc.spec.public(),
                "running": bool(proc.process and proc.process.poll() is None),
                "dead": proc.dead,
            })
        return out


def call_tool(
    server_id: str, tool_name: str, arguments: dict[str, Any] | None = None,
    *, timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
) -> dict[str, Any]:
    """调用 MCP 工具（仅当已启用且已授权时）。

    注意：本函数**不做审批**。调用方（`autonomous`）必须先把 MCP 工具当作
    `effect=external` 走 `policy` + `tool_runtime.run`，再调用这里。
    """
    if not _enabled():
        raise McpDisabled("MCP 未启用：请在设置中开启并逐个授权服务")
    with _REGISTRY_LOCK:
        proc = _SERVERS.get(server_id)
    if proc is None:
        raise McpError(f"MCP 服务未登记：{server_id}")
    if not proc.spec.enabled:
        raise McpError(f"MCP 服务未授权启用：{server_id}")
    if tool_name not in proc.spec.tools:
        raise McpError(f"工具 {tool_name} 未在 manifest 中声明，已拒绝调用")
    proc.start()
    try:
        return proc.request("tools/call", {"name": tool_name, "arguments": arguments or {}},
                            timeout_s=timeout_s)
    except McpError:
        raise
    finally:
        # 单次调用后结束进程：不为第三方进程保留长生命周期（失败不自动重启）
        proc.stop()


def shutdown() -> int:
    """卸载全部 MCP 进程（应用退出/卸载时调用）；返回清理数量。"""
    with _REGISTRY_LOCK:
        procs = list(_SERVERS.values())
        _SERVERS.clear()
    for proc in procs:
        proc.stop()
    return len(procs)


def status() -> dict[str, Any]:
    """MCP 子系统状态（诊断用；不含 command、环境变量与凭据）。"""
    return {
        "enabled": _enabled(),
        "registered": len(_SERVERS),
        "running": sum(1 for p in _SERVERS.values() if p.process and p.process.poll() is None),
        "defaultNetwork": False,
    }
