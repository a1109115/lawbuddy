"""执行后端抽象（LB-HERMES-2026-01 WP10）。

只抽象 `local` 与后续可选的 `docker`；**本地仍是唯一默认实现**。
本模块存在的意义是给沙箱工具（读/写文件、Shell）一个明确的边界类型，
避免以后接入 Docker 时把网络、挂载与凭据策略散落在业务代码里。

设计约束：

1. **默认 local**：未显式配置时一律 `local`，行为与升级前完全一致。
2. **docker 未实现即拒绝**：`get_backend("docker")` 抛 `BackendUnavailable`，
   不做"看起来能跑但语义不对"的半实现。
3. **容器内不等于免审批**：`requires_approval` 恒为真（Shell 类操作），
   后端切换不得降低审批等级。
4. **能力声明显式**：网络、文件系统写入、PID 隔离、挂载点都由后端声明，
   由调用方据此做策略判定，而不是假设"容器是安全的"。
5. 后端不持有密钥；凭据代理（若未来需要）必须单独实现，不得写进环境快照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LOCAL = "local"
DOCKER = "docker"
BACKENDS = (LOCAL, DOCKER)

# 当前实际可用的后端（docker 需显式实现后加入）
AVAILABLE = (LOCAL,)


class BackendUnavailable(RuntimeError):
    """请求的执行后端尚未实现/不可用；必须显式失败，不得静默降级。"""


@dataclass(frozen=True)
class ExecBackend:
    """执行后端能力声明（不可变）。"""

    name: str
    network: bool
    writable_fs: bool
    pid_isolated: bool
    mount_roots: tuple[str, ...] = field(default_factory=tuple)
    # Shell 类操作在任何后端都需要审批（容器化不是审批豁免理由）
    requires_approval: bool = True

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "network": self.network,
            "writableFs": self.writable_fs,
            "pidIsolated": self.pid_isolated,
            "mountRoots": list(self.mount_roots),
            "requiresApproval": self.requires_approval,
        }

    def allows_path(self, path: str) -> bool:
        """路径是否落在后端允许的挂载范围内（local 允许工作空间相对路径）。"""
        normalized = str(path or "").replace("\\", "/").lstrip("/")
        if not normalized or ".." in normalized:
            return False
        if self.name == LOCAL:
            return True
        return any(normalized.startswith(root.strip("/")) for root in self.mount_roots)


_LOCAL_BACKEND = ExecBackend(
    name=LOCAL, network=False, writable_fs=True, pid_isolated=False,
    mount_roots=(), requires_approval=True,
)


def get_backend(name: str | None = None) -> ExecBackend:
    """取执行后端；未实现的后端显式失败（不静默回退到 local）。"""
    requested = str(name or LOCAL).strip().lower()
    if requested == LOCAL:
        return _LOCAL_BACKEND
    if requested == DOCKER:
        raise BackendUnavailable(
            "Docker 执行后端尚未实现；本地后端仍是默认且唯一可用的选择"
        )
    raise BackendUnavailable(f"未知执行后端：{requested}")


def configured_backend() -> ExecBackend:
    """读取设置 `agent.execBackend`（默认 local）；未知值回退 local 并保持审批。"""
    from .tasks import get_setting

    try:
        value = str(get_setting("agent.execBackend") or LOCAL).strip().lower()
    except Exception:  # noqa: BLE001 设置不可读时用默认后端
        value = LOCAL
    if value not in AVAILABLE:
        # 配置了不可用后端时**不崩溃**，退回 local（但仍保持审批要求）；
        # 显式调用方应通过 get_backend(value) 得到明确的不可用错误。
        return _LOCAL_BACKEND
    return get_backend(value)


def capabilities() -> dict[str, Any]:
    """当前可用后端清单（UI 与诊断使用；不含环境变量与凭据）。"""
    return {
        "available": list(AVAILABLE),
        "declared": list(BACKENDS),
        "unimplemented": [b for b in BACKENDS if b not in AVAILABLE],
        "active": configured_backend().describe(),
    }
