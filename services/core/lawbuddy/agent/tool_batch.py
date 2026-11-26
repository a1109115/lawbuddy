"""受限 Tool Batch DSL（LB-HERMES-2026-01 WP07）。

设计目标：让模型声明**一批只读并行调用**，由运行时决定能并行几路、如何聚合与
在部分失败时继续，而不是让模型自己"多写几段"或开 Shell 并发。

DSL（声明式，不接受任意代码）：

    {
      "calls": [
        {"id": "a", "tool": "document_get_pages", "arguments": {"documentId": "d1", "pages": [1,2]}},
        {"id": "b", "tool": "document_search", "arguments": {"query": "借款", "limit": 5}},
        {"id": "c", "tool": "memory_search", "arguments": {"query": "时间线"}}
      ],
      "concurrency": 3,          # 可选；上限由运行时封顶
      "onError": "collect"       # collect（默认，收集错误继续）| fail_fast
    }

硬约束：

1. **只允许只读工具**：按 `tool_registry` 的 `effect == read` 判定；写入、
   交付、记忆写入、外部副作用与 Shell 一律拒绝（不执行任何一条）。
2. **不允许嵌套批量**：batch 内不能再调用 batch 自身或工具发现类工具。
3. **不共享状态**：每个子调用仍是 `tool_runtime.run` 的一次独立调用，
   各自经过策略/审批/Journal/预算，批处理只是在受控并发下把它们组装起来。
4. **参数与结果不落日志**：只记录 `id/tool/status/errorCode/durationMs`。
5. 并发上限固定封顶，且只对只读调用开放；写入仍由主任务串行提交。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import error_classifier, tool_registry

# 并发硬上限：本地 SQLite 读 + HTTP 上游都难以承受更高并发
MAX_CONCURRENCY = 4
MAX_CALLS = 12
# 不允许出现在 batch 中的工具（自引用 / 发现类）
_FORBIDDEN = {"tool_batch", "tool_call", "tool_search", "tool_describe"}


class BatchRejected(RuntimeError):
    """批量请求不合法（整批拒绝，不执行任何一条）。"""


def _normalize_plan(plan: Any) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        raise BatchRejected("批量调用需要对象：{calls: [...], concurrency?: n}")
    calls = plan.get("calls")
    if not isinstance(calls, list) or not calls:
        raise BatchRejected("calls 必须是非空数组")
    if len(calls) > MAX_CALLS:
        raise BatchRejected(f"单批最多 {MAX_CALLS} 个调用")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(calls):
        if not isinstance(item, dict):
            raise BatchRejected(f"第 {idx + 1} 个调用必须是对象")
        tool = str(item.get("tool") or "").strip()
        args = item.get("arguments") or {}
        cid = str(item.get("id") or f"c{idx + 1}")
        if not tool:
            raise BatchRejected(f"第 {idx + 1} 个调用缺少 tool")
        if cid in seen:
            raise BatchRejected(f"调用 id 重复：{cid}")
        seen.add(cid)
        if not isinstance(args, dict):
            raise BatchRejected(f"调用 {cid} 的 arguments 必须是对象")
        if tool in _FORBIDDEN:
            raise BatchRejected(f"批量中不允许调用 {tool}（自引用或工具发现类）")
        meta = tool_registry.get(tool)
        if not tool_registry.is_registered(tool):
            raise BatchRejected(f"工具 {tool} 未登记，批量调用只接受已登记只读工具")
        if not meta.read_only:
            raise BatchRejected(
                f"工具 {tool} 的副作用类别为 {meta.effect}，批量只允许只读工具；"
                "写入/交付类必须由主任务串行提交"
            )
        if not meta.batch_allowed:
            raise BatchRejected(
                f"工具 {tool} 未声明允许批处理（batchAllowed=false）；请单独调用"
            )
        out.append({"id": cid, "tool": tool, "arguments": args})
    return out


def plan_concurrency(plan: dict[str, Any], *, cap: int = MAX_CONCURRENCY) -> int:
    raw = plan.get("concurrency")
    try:
        wanted = int(raw) if raw is not None else 2
    except (TypeError, ValueError):
        wanted = 2
    return max(1, min(wanted, cap, MAX_CONCURRENCY))


def run(
    plan: dict[str, Any],
    invoke: Callable[[str, dict[str, Any]], Any],
    *,
    on_error: str = "collect",
) -> dict[str, Any]:
    """执行一批只读调用。

    `invoke(tool, args)` 由调用方提供（通常是 `autonomous._execute_tool`），
    因此批处理不持有执行逻辑，仍走统一执行路径。

    返回：{"ok": bool, "results": [...], "errors": [...], "durationMs": int}
    每条结果只含 id/tool/status/errorCode/durationMs 与（成功时的）data。
    """
    calls = _normalize_plan(plan)
    mode = str(plan.get("onError") or on_error or "collect").lower()
    if mode not in ("collect", "fail_fast"):
        raise BatchRejected("onError 只支持 collect 或 fail_fast")
    concurrency = plan_concurrency(plan)
    started = time.monotonic()

    def one(call: dict[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            data = invoke(call["tool"], call["arguments"])
            # 工具自身返回的 error 字段视为工具级失败（不抛异常也需标出）
            if isinstance(data, dict) and data.get("error"):
                return {
                    "id": call["id"], "tool": call["tool"], "status": "error",
                    "errorCode": error_classifier.INVALID_REQUEST,
                    "durationMs": int((time.monotonic() - t0) * 1000),
                    "data": data,
                }
            return {
                "id": call["id"], "tool": call["tool"], "status": "ok",
                "durationMs": int((time.monotonic() - t0) * 1000), "data": data,
            }
        except BaseException as exc:  # noqa: BLE001 收集错误，按类别返回
            return {
                "id": call["id"], "tool": call["tool"], "status": "error",
                "errorCode": error_classifier.classify(exc),
                "durationMs": int((time.monotonic() - t0) * 1000),
                "data": None,
            }

    results: list[dict[str, Any]] = []
    if concurrency == 1:
        for call in calls:
            results.append(one(call))
            if mode == "fail_fast" and results[-1]["status"] == "error":
                break
    else:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="lawbuddy-batch") as pool:
            futures = [pool.submit(one, call) for call in calls]
            for fut in futures:
                results.append(fut.result())
                if mode == "fail_fast" and results[-1]["status"] == "error":
                    for pending in futures:
                        pending.cancel()
                    break

    errors = [r for r in results if r["status"] == "error"]
    return {
        "ok": not errors,
        "concurrency": concurrency,
        "total": len(calls),
        "completed": len(results),
        "results": results,
        "errors": [
            {k: e[k] for k in ("id", "tool", "errorCode", "durationMs")} for e in errors
        ],
        "durationMs": int((time.monotonic() - started) * 1000),
    }


def validate(plan: Any) -> dict[str, Any]:
    """只校验不执行（供 UI/诊断与评测断言使用）。"""
    try:
        calls = _normalize_plan(plan)
        return {
            "ok": True, "calls": calls,
            "concurrency": plan_concurrency(plan if isinstance(plan, dict) else {}),
        }
    except BatchRejected as exc:
        return {"ok": False, "reason": str(exc)}
