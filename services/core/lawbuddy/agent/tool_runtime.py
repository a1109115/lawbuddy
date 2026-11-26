"""统一工具执行路径（LB-HERMES-2026-01 §4.2 / WP01）。

所有工具调用最终必须经过：

    工具发现/参数校验 → 模式/案件/能力/审批过滤 → Journal staged 持久化
      → 获取/确认租约（owner + fencing token） → 执行工具
      → 结果加密落库 → succeeded/failed/unknown 对账 → 发布 UI 事件

Tool Search、子代理、MCP、插件和 Tool Batch 都不得建立旁路：它们只负责
**找到并调用** `run()`，不自行执行副作用。

兼容策略（§4.1）：
- 只有 `runtime_version == "v3"` 的任务走 Journal 与租约；
- V1/V2 任务保持原执行路径（`journaled=False`），恢复语义不变；
- 新任务默认仍是 V2，直至 M7 验收通过。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from . import observability, tool_journal, tool_registry

# 运行时版本：只有 v3 启用 Journal / 租约 / Span 强制路径
JOURNALED_RUNTIMES = ("v3",)


class ToolDenied(RuntimeError):
    """工具在策略/模式/审批层被拒绝（不是执行失败，不得进入 Journal 的失败计数）。"""


def is_journaled(state: dict[str, Any]) -> bool:
    return str(state.get("_runtime_version") or "") in JOURNALED_RUNTIMES


def _task_id(state: dict[str, Any]) -> str:
    return str(state.get("task_id") or "")


def _owner_id(state: dict[str, Any]) -> str:
    """任务所有者：同一任务在同一进程内复用同一 owner，租约由它持有。"""
    owner = state.get("_owner_id")
    if not owner:
        owner = tool_journal.new_owner_id("core")
        state["_owner_id"] = owner
    return str(owner)


def ensure_lease(state: dict[str, Any], *, takeover: bool = False) -> int:
    """确保当前执行者持有任务租约，返回 fencing token（V3 专用）。

    `takeover=True` 只在任务启动/恢复时使用：接管崩溃遗留的租约并递增
    fencing token，使旧 Worker 的提交被拒绝。工具执行期间一律 `takeover=False`，
    避免误抢其他存活执行者的租约。
    """
    task_id = _task_id(state)
    if not task_id:
        raise tool_journal.JournalError("缺少 task_id，无法获取租约")
    owner = _owner_id(state)
    cached = state.get("_fencing_token")
    if cached and not takeover:
        return int(cached)
    token = tool_journal.acquire_lease(task_id, owner, takeover=takeover)
    state["_fencing_token"] = token
    return token


def release(state: dict[str, Any]) -> None:
    """任务结束时释放租约（幂等；租约已易主时静默跳过）。"""
    task_id = _task_id(state)
    if not task_id:
        return
    try:
        tool_journal.release_lease(task_id, _owner_id(state))
    except Exception:  # noqa: BLE001 释放失败不影响任务结论
        pass


def reconcile(state: dict[str, Any]) -> dict[str, Any]:
    """恢复任务时对账遗留执行态，返回计数供 resume 事件展示。"""
    task_id = _task_id(state)
    if not task_id:
        return {"retryable": 0, "unknown": 0, "cancelledStaged": 0}
    return tool_journal.reconcile(task_id, _owner_id(state))


def _blocked_by_unknown(state: dict[str, Any], tool_name: str, meta: tool_registry.ToolMeta) -> bool:
    """非幂等工具存在未处置 unknown 时禁止再次执行（HUP-T-002）。"""
    if meta.idempotent:
        return False
    task_id = _task_id(state)
    if not task_id:
        return False
    return bool(tool_journal.unresolved_unknown(task_id, tool_name))


def run(
    state: dict[str, Any],
    name: str,
    args: dict[str, Any],
    executor: Callable[[], Any],
    *,
    call_id: str = "",
    origin: str = "main",
    subagent_id: str | None = None,
) -> Any:
    """统一执行入口。

    - 非 V3 任务：直接执行 `executor()`（行为与升级前完全一致）；
    - V3 任务：Journal staged → 租约校验 → 执行 → 结果落库 → Span。

    `executor` 只包含**真正的工具实现**（含审批等待），策略与参数门禁由调用方
    在进入前完成；这样 V1/V2 与 V3 共享同一份策略代码。
    """
    if not is_journaled(state):
        return executor()

    meta = tool_registry.get(name)
    if _blocked_by_unknown(state, name, meta):
        raise ToolDenied(
            f"工具 {name} 存在未处置的 unknown 调用（副作用可能已发生）："
            "请先在恢复中心确认该调用是否已执行，再重试。"
        )

    task_id = _task_id(state)
    cid = call_id or f"auto-{tool_journal.uuid.uuid4().hex[:12]}"
    token = ensure_lease(state)
    owner = _owner_id(state)

    with observability.span(
        "tool", name, task_id=task_id,
        conversation_id=str(state.get("conversation_id") or "") or None,
        call_id=cid, subagent_id=subagent_id,
        runtime_version=str(state.get("_runtime_version") or ""),
        attributes={"effect": meta.effect, "idempotent": meta.idempotent, "origin": origin},
    ) as sp:
        reservation = None
        try:
            # 共享预算预留（WP07）：V3 任务在真正执行前原子预留工具调用额度。
            # 预留失败即拒绝执行（并发子代理不能各自通过检查后合起来超限）。
            reservation = _reserve_tool_call(state, subagent_id)
        except Exception as exc:  # noqa: BLE001 预算不可用时不得静默放行
            sp.set_error(tool_journal.classify_exception(exc))
            raise
        try:
            result = tool_journal.with_journal(
                task_id=task_id, call_id=cid, tool_name=name, effect=meta.effect,
                idempotent=meta.idempotent, owner_id=owner, fencing_token=token,
                executor=executor, args=args,
                conversation_id=str(state.get("conversation_id") or "") or None,
                subagent_id=subagent_id, origin=origin,
            )
        except tool_journal.LeaseLost as exc:
            _settle_or_release(reservation, ok=False)
            sp.set_error("lease_lost")
            raise
        except tool_journal.JournalError as exc:
            _settle_or_release(reservation, ok=False)
            sp.set_error("persistence")
            raise
        except BaseException as exc:  # noqa: BLE001 Span 记录后原样上抛
            _settle_or_release(reservation, ok=False)
            sp.set_error(tool_journal.classify_exception(exc))
            raise
        _settle_or_release(reservation, ok=True)
        sp.add_attribute("hasResult", result is not None)
        if isinstance(result, dict):
            # 只记录是否存在工具级错误；不记录键名或内容（键名也可能泄露业务语义）
            if result.get("error"):
                sp.add_attribute("toolError", True)
        sp.end("ok")
        return result


def _reserve_tool_call(state: dict[str, Any], subagent_id: str | None) -> Any:
    """为一次工具调用预留预算；预算未初始化或不可用时抛错（调用方拒绝执行）。"""
    from . import budget

    task_id = _task_id(state)
    if not task_id:
        return None
    try:
        return budget.reserve(
            task_id, "tool_call", 1, owner_id=_owner_id(state), subagent_id=subagent_id,
        )
    except Exception as exc:  # noqa: BLE001
        # 预算表尚未建立（旧工作空间未迁移）时按"不预留但继续记账"处理，
        # 避免把升级前的任务打死；真正的额度拒绝（ReservationDenied）向上抛。
        from . import budget as budget_mod

        if isinstance(exc, budget_mod.ReservationDenied):
            raise
        return None


def _settle_or_release(reservation: Any, *, ok: bool) -> None:
    """结算成功的预留；失败的释放额度（实际用量仍由 `_budget_spend` 记账）。"""
    if not reservation:
        return
    from . import budget

    try:
        if ok:
            budget.settle(reservation["reservationId"])
        else:
            budget.release(reservation["reservationId"])
    except Exception:  # noqa: BLE001 结算失败不得影响已完成的工具结果
        pass


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


def journal_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """当前任务的 Journal 摘要（供 system 状态摘要与恢复中心展示）。"""
    task_id = _task_id(state)
    if not task_id or not is_journaled(state):
        return {"enabled": False}
    return {
        "enabled": True,
        "counts": tool_journal.stats(task_id),
        "fence": tool_journal.current_fence(task_id),
        "unknown": [
            {k: e[k] for k in ("callId", "toolName", "effect", "attempt", "errorCode", "createdAt")}
            for e in tool_journal.unresolved_unknown(task_id)
        ],
    }
