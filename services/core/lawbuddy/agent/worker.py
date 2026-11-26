"""工作型子代理执行器（LB-HARNESS-2026-01 §4.8 / B-04）。

与检索型 `case_explore` 子代理的区别：

| 维度 | 检索子代理（既有） | 工作型子代理（本模块） |
| --- | --- | --- |
| 绑定对象 | 一个自由形式的调查问题 | 一个持久化工作单元（有 ID、优先级、依赖） |
| 上下文 | 主任务共享状态中的检索缓存 | 独立上下文，只含该单元材料（context_builder） |
| 写入范围 | 只返回候选 findings | 只写**暂存区**（`submission/.staging/`），正式交付物由控制器原子写入 |
| 状态 | 不持久化 | 单元状态、尝试次数、失败原因全部落工作账本 |
| 恢复 | 按问题文本缓存 | 按 `taskId + workUnitId + attempt` 幂等，重试不产生重复行 |

硬约束：子代理不能直接写 `submission/` 下的正式交付物；它的输出必须先经
`commit_staged_result` 校验（来源可回溯）后才进入答案格账本。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..sandbox import workspace_root
from . import work_ledger

STAGING_DIRNAME = ".staging"
WORKER_MAX_ROUNDS = 6
WORKER_MAX_TOOL_CALLS = 24
_WORKER_TOOLS = ("documents_list", "document_search", "document_get_pages", "memory_search")
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def _staging_path(task_id: str, unit_id: str) -> Path:
    """暂存区路径：`<workspace>/submission/.staging/<taskId>/<unitId>.json`。

    与正式交付物同在 submission 下但独立子目录，交付控制器不会扫描到它。
    """
    safe_task = re.sub(r"[^\w.-]+", "_", task_id)[:64]
    safe_unit = re.sub(r"[^\w.-]+", "_", unit_id)[:96]
    return workspace_root() / "submission" / STAGING_DIRNAME / safe_task / f"{safe_unit}.json"


def _spend_budget(task_id: str, kind: str, *, tokens: int = 0, count: int = 1) -> None:
    """子代理预算记账（B-02 §4.4）：与主循环共用 task_budget，失败不阻断执行。"""
    if not task_id:
        return
    try:
        from . import budget

        budget.spend(task_id, kind, tokens=int(tokens), count=int(count))
    except Exception:  # noqa: BLE001 记账失败不阻断子代理
        return


def write_staged(task_id: str, unit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """原子写暂存结果（先临时文件再替换）：中断点不会留下半截 JSON。"""
    path = _staging_path(task_id, unit_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "taskId": task_id,
        "workUnitId": unit_id,
        "attempt": int(payload.get("attempt") or 0),
        "result": payload.get("result") or {},
    }
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.replace(path)
    return {
        "path": f"submission/{STAGING_DIRNAME}/{path.parent.name}/{path.name}",
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "bytes": len(raw.encode("utf-8")),
    }


def read_staged(task_id: str, unit_id: str) -> dict[str, Any] | None:
    path = _staging_path(task_id, unit_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def parse_worker_output(text: str) -> dict[str, Any]:
    """解析子代理输出中的结构化结果（容忍缺失与畸形）。

    返回 {"answer", "documentId", "pageNo", "quote", "confidence", "reasonCode", "notes"}。
    缺失字段一律为空值，绝不猜测——无来源的结果随后会被判为 needs_human。
    """
    out: dict[str, Any] = {
        "answer": "", "documentId": "", "pageNo": 0, "quote": "",
        "confidence": "low", "reasonCode": "", "notes": "",
    }
    match = _JSON_BLOCK_RE.search(str(text or ""))
    if not match:
        return out
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return out
    if not isinstance(data, dict):
        return out
    out["answer"] = str(data.get("answer") or "").strip()
    out["documentId"] = str(data.get("documentId") or "").strip()
    raw_page = data.get("pageNo")
    try:
        out["pageNo"] = int(raw_page or 0)
    except (TypeError, ValueError):
        out["pageNo"] = 0
    out["quote"] = str(data.get("quote") or "").strip()
    confidence = str(data.get("confidence") or "low").strip().lower()
    out["confidence"] = confidence if confidence in ("high", "medium", "low") else "low"
    out["reasonCode"] = str(data.get("reasonCode") or "").strip()
    out["notes"] = str(data.get("notes") or "").strip()
    return out


def commit_staged_result(
    task_id: str,
    unit: dict[str, Any],
    parsed: dict[str, Any],
) -> dict[str, Any]:
    """把已暂存的结构化结果提交进答案格账本（幂等，校验来源）。

    - 有文档 + 页码 + 短引 → `committed`；
    - 有答案但来源不全 → `needs_human`（SOURCE_MISSING），不当成已支持事实；
    - 无答案 → 按 reasonCode 判 `needs_human` / `blocked`。
    返回提交后的状态，供调度器推进工作单元。
    """
    from .scheduler import classify_failure

    scope = unit.get("scope") or {}
    artifact = str(scope.get("artifact") or "")
    cell_key = str(scope.get("cellKey") or scope.get("rowKey") or "")
    if not artifact or not cell_key:
        return {"status": "skipped", "reason": "单元未绑定答案格（物化/派生类由控制器处理）"}
    answer = str(parsed.get("answer") or "")
    document_id = str(parsed.get("documentId") or "")
    page_no = int(parsed.get("pageNo") or 0)
    quote = str(parsed.get("quote") or "")
    if answer and document_id and page_no and quote:
        work_ledger.commit_cell(
            task_id, artifact, cell_key, value=answer, status="committed",
            document_id=document_id, page_no=page_no, quote=quote,
        )
        return {"status": "committed", "cellKey": cell_key}
    if answer:
        work_ledger.commit_cell(
            task_id, artifact, cell_key, value=answer, status="needs_human",
            document_id=document_id or None,
            page_no=page_no or None,
            reason_code="SOURCE_MISSING",
        )
        return {"status": "needs_human", "cellKey": cell_key, "reasonCode": "SOURCE_MISSING"}
    reason = str(parsed.get("reasonCode") or "INSUFFICIENT_EVIDENCE")
    status = "blocked" if classify_failure(reason) == "blocked" else "needs_human"
    work_ledger.commit_cell(
        task_id, artifact, cell_key, value=None, status=status, reason_code=reason,
    )
    return {"status": status, "cellKey": cell_key, "reasonCode": reason}


def run_worker(
    state: dict[str, Any],
    unit: dict[str, Any],
    *,
    model_gateway: Any = None,
) -> dict[str, Any]:
    """执行一个工作单元（独立上下文 + 独立轮数上限）。

    只写暂存区，正式交付物由 `delivery_controller` 统一原子写入。失败不抛出到
    主循环：调用方（调度器）据返回的 status 决定 needs_human / blocked / 重试。
    """
    if model_gateway is None:
        from . import model_gateway  # 延迟导入：避免与 autonomous 的循环依赖

    from . import concurrency, context_builder

    task_id = str(state.get("task_id") or "")
    unit_id = str(unit.get("workUnitId") or "")
    attempt = int(unit.get("attempts") or 0)
    messages = context_builder.build_worker_messages(state, unit)
    tools = [s for s in _tool_specs() if s["function"]["name"] in _WORKER_TOOLS]
    policy = state.get("model_policy") or {}
    model_id = str(policy.get("modelId") or model_gateway.DEFAULT_MODEL)
    effort = model_gateway.weakest_reasoning_effort(model_id)
    cancel_check = state.get("_cancel_check") or (lambda: False)
    gate = concurrency.gate()
    used_tools = 0
    rounds_used = 0
    conclusion = ""
    with gate.acquire():
        try:
            for _round in range(1, WORKER_MAX_ROUNDS + 1):
                if cancel_check():
                    return {"status": "cancelled", "workUnitId": unit_id}
                rounds_used = _round
                result = model_gateway.chat_with_tools(
                    messages, tools=tools, model=model_id,
                    cancel_check=cancel_check, reasoning_effort=effort,
                )
                usage = result.get("usage") or {}
                prompt_tokens = int(usage.get("promptTokens") or 0)
                completion_tokens = int(usage.get("completionTokens") or 0)
                # 子代理用量并入主任务总量与检查点，恢复后续接而不是丢失
                usage_total = state.get("_usage_total")
                if isinstance(usage_total, dict):
                    usage_total["promptTokens"] = int(
                        usage_total.get("promptTokens") or 0) + prompt_tokens
                    usage_total["completionTokens"] = int(
                        usage_total.get("completionTokens") or 0) + completion_tokens
                state["_model_calls_used"] = int(state.get("_model_calls_used") or 0) + 1
                _spend_budget(
                    task_id, "model_call",
                    tokens=prompt_tokens + completion_tokens,
                )
                content = str(result.get("content") or "")
                calls = list(result.get("toolCalls") or [])
                if not calls:
                    conclusion = content
                    break
                messages.append({
                    "role": "assistant", "content": content,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": tc.arguments}}
                        for tc in calls
                    ],
                })
                for tc in calls:
                    if used_tools >= WORKER_MAX_TOOL_CALLS:
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id,
                            "content": json.dumps({"error": "工具调用已达子代理上限，请立即输出结论"},
                                                  ensure_ascii=False),
                        })
                        continue
                    used_tools += 1
                    _spend_budget(task_id, "tool_call")
                    args = tc.parsed_arguments or {}
                    if tc.name == "document_get_pages":
                        args["maxChars"] = min(int(args.get("maxChars") or 3000), 3000)
                    try:
                        output = _execute_tool(state, tc.name, args, origin="worker")
                    except Exception as exc:  # noqa: BLE001 工具错误回灌子上下文
                        output = {"error": str(exc)}
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps(output, ensure_ascii=False, default=str)[:4000],
                    })
            else:
                messages.append({
                    "role": "user",
                    "content": "请立即停止调用工具，按约定 JSON 输出结论；无法确认的写空答案并给 reasonCode。",
                })
                result = model_gateway.chat_with_tools(
                    messages, tools=None, model=model_id,
                    cancel_check=cancel_check, reasoning_effort=effort,
                )
                rounds_used += 1
                usage = result.get("usage") or {}
                prompt_tokens = int(usage.get("promptTokens") or 0)
                completion_tokens = int(usage.get("completionTokens") or 0)
                # 子代理用量并入主任务总量与检查点，恢复后续接而不是丢失
                usage_total = state.get("_usage_total")
                if isinstance(usage_total, dict):
                    usage_total["promptTokens"] = int(
                        usage_total.get("promptTokens") or 0) + prompt_tokens
                    usage_total["completionTokens"] = int(
                        usage_total.get("completionTokens") or 0) + completion_tokens
                state["_model_calls_used"] = int(state.get("_model_calls_used") or 0) + 1
                _spend_budget(
                    task_id, "model_call",
                    tokens=prompt_tokens + completion_tokens,
                )
                conclusion = str(result.get("content") or "")
            gate.on_success()
        except Exception as exc:  # noqa: BLE001 失败分类交由调度器判定终态
            gate.on_failure(concurrency.classify_error(exc))
            return {
                "status": "error", "workUnitId": unit_id,
                "errorClass": concurrency.classify_error(exc),
                "error": str(exc)[:300],
            }
        finally:
            # 子代理轮次统一记账（B-02 §4.4）：即使失败也记录实际消耗。
            _spend_budget(task_id, "subagent_round", count=rounds_used)
    parsed = parse_worker_output(conclusion)
    staged = write_staged(task_id, unit_id, {"attempt": attempt, "result": parsed})
    committed = commit_staged_result(task_id, unit, parsed)
    return {
        "status": committed.get("status"),
        "workUnitId": unit_id,
        "attempt": attempt,
        "staged": staged,
        "parsed": parsed,
        "commit": committed,
        "toolsUsed": used_tools,
        "conclusionChars": len(conclusion),
    }


def _tool_specs() -> list[dict[str, Any]]:
    """复用主循环的工具定义（只挑子代理可用的四项）。"""
    from .autonomous import _tool_specs as main_specs

    return main_specs()


def _execute_tool(
    state: dict[str, Any], name: str, args: dict[str, Any], *, origin: str = "worker",
) -> Any:
    """复用主循环的工具实现（子代理不允许写正式交付物，由白名单保证）。

    工具名在白名单内（documents_list / document_search / document_get_pages /
    memory_search），这些工具都不写 `submission/`，因此不存在子代理越权写入。
    """
    from .autonomous import _execute_tool as main_execute

    return main_execute(state, name, args, origin=origin)
