"""结构化观测：内容无关 Span 与本地诊断（LB-HERMES-2026-01 WP03）。

设计原则（硬约束）：

1. **Span 只记技术事实**：耗时、状态、错误码、重试次数、Token、预算与恢复结果。
   禁止写入查询词、文件路径、工具参数、引文、错误响应正文或任何案件原文。
2. **业务审计与技术 Span 分离**：审计走 `audit.py`（追加式、不可因队列满而丢失）；
   本模块的写入失败一律降级为丢弃计数，绝不阻断任务。
3. **有界队列 + 丢弃计数**：性能 Span 用容量受限的异步队列，丢弃必须可观测
   （`dropped` 计数），不允许静默表现为「无事件」。
4. **默认仅本地**：不新增任何外部遥测出口。

用法：

    with observability.span("tool", "document_search", task_id=tid, call_id=cid) as sp:
        ...
        sp.set_status("ok")

未初始化数据库时（如单元测试直接调用）`span` 退化为空操作。
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator

from ..storage import get_db

# 性能 Span 队列容量：满时丢弃并计数（丢弃不丢审计）
QUEUE_CAPACITY = 2048
_FLUSH_BATCH = 64

_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=QUEUE_CAPACITY)
_dropped = 0
_drop_lock = threading.Lock()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_enabled = True

# 当前 Span 上下文（供模型/工具/子代理自动挂父子关系，不依赖调用方手工传递）
_current_span: ContextVar[str | None] = ContextVar("lawbuddy_span", default=None)
_current_trace: ContextVar[str | None] = ContextVar("lawbuddy_trace", default=None)


def enable(value: bool = True) -> None:
    global _enabled
    _enabled = bool(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_ready() -> bool:
    try:
        get_db()
        return True
    except Exception:  # noqa: BLE001 未初始化工作空间时静默降级
        return False


def _drain_once(batch: list[dict[str, Any]]) -> None:
    global _dropped
    if not batch:
        return
    try:
        db = get_db()
        with db.transaction() as conn:
            for rec in batch:
                conn.execute(
                    """
                    INSERT INTO runtime_spans(
                        span_id, trace_id, parent_span_id, task_id, conversation_id, call_id,
                        subagent_id, runtime_version, kind, name, status, error_code, retry_count,
                        prompt_tokens, completion_tokens, budget_cost_json, started_at, ended_at,
                        duration_ms, attributes_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rec["spanId"], rec["traceId"], rec.get("parentSpanId"), rec.get("taskId"),
                        rec.get("conversationId"), rec.get("callId"), rec.get("subagentId"),
                        rec.get("runtimeVersion"), rec["kind"], rec["name"], rec["status"],
                        rec.get("errorCode"), int(rec.get("retryCount") or 0),
                        int(rec.get("promptTokens") or 0), int(rec.get("completionTokens") or 0),
                        json.dumps(rec.get("budgetCost") or {}, ensure_ascii=False),
                        rec["startedAt"], rec.get("endedAt"), rec.get("durationMs"),
                        json.dumps(rec.get("attributes") or {}, ensure_ascii=False),
                    ),
                )
    except Exception:  # noqa: BLE001 观测失败不得影响任务
        with _drop_lock:
            _dropped += len(batch)


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run_worker, name="lawbuddy-spans", daemon=True)
        _worker.start()


def _run_worker() -> None:
    while True:
        item = _queue.get()
        batch: list[dict[str, Any]] = []
        if item is None:
            _queue.task_done()
            return
        batch.append(item)
        # 批量合并：最多等 50ms，避免每条 Span 一次事务
        deadline = time.monotonic() + 0.05
        while len(batch) < _FLUSH_BATCH and time.monotonic() < deadline:
            try:
                nxt = _queue.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                _queue.task_done()
                _drain_once(batch)
                return
            batch.append(nxt)
        _drain_once(batch)
        for _ in batch:
            _queue.task_done()


def _enqueue(record: dict[str, Any]) -> None:
    global _dropped
    if not _enabled or not _db_ready():
        return
    try:
        _ensure_worker()
        _queue.put_nowait(record)
    except queue.Full:
        with _drop_lock:
            _dropped += 1


def dropped_count() -> int:
    with _drop_lock:
        return int(_dropped)


def flush(timeout: float = 5.0) -> bool:
    """等待队列落库（评测与任务收尾使用）。

    返回值表示是否在超时前排空；任务收尾**不应**因 flush 失败而失败，
    未落库的 Span 计入丢弃计数并在诊断中可见。
    """
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if _queue.empty():
            # 队列空不代表 worker 已完成最后一批 INSERT，短暂让出
            time.sleep(0.02)
            return True
        time.sleep(0.01)
    return False


def reset() -> None:
    """测试用：清空队列与上下文（不删除数据库已写 Span）。"""
    global _dropped
    while True:
        try:
            _queue.get_nowait()
            _queue.task_done()
        except queue.Empty:
            break
    with _drop_lock:
        _dropped = 0
    _current_span.set(None)
    _current_trace.set(None)


class Span:
    """一个可手动结束的观测片段；作为上下文管理器使用时自动记录耗时与状态。"""

    def __init__(
        self, kind: str, name: str, *, task_id: str | None = None,
        conversation_id: str | None = None, call_id: str | None = None,
        subagent_id: str | None = None, runtime_version: str | None = None,
        trace_id: str | None = None, parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.span_id = str(uuid.uuid4())
        self.trace_id = trace_id or _current_trace.get() or str(uuid.uuid4())
        self.parent_span_id = parent_span_id or _current_span.get()
        self.kind = kind
        self.name = name
        self.task_id = task_id
        self.conversation_id = conversation_id
        self.call_id = call_id
        self.subagent_id = subagent_id
        self.runtime_version = runtime_version
        self.status = "running"
        self.error_code: str | None = None
        self.retry_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.budget_cost: dict[str, Any] = {}
        self.attributes = dict(attributes or {})
        self.started_at = _now_iso()
        self._t0 = time.monotonic()
        self._ended = False
        self._token_span = _current_span.set(self.span_id)
        self._token_trace = _current_trace.set(self.trace_id)

    # -- 记录 ---------------------------------------------------------------

    def set_error(self, code: str, *, retry: bool = False) -> None:
        self.status = "error"
        self.error_code = str(code or "unknown")
        if retry:
            self.retry_count += 1

    def set_tokens(self, prompt: int = 0, completion: int = 0) -> None:
        self.prompt_tokens += max(0, int(prompt or 0))
        self.completion_tokens += max(0, int(completion or 0))

    def set_budget(self, **cost: Any) -> None:
        self.budget_cost.update({k: v for k, v in cost.items() if v is not None})

    def add_attribute(self, key: str, value: Any) -> None:
        """附加非敏感技术字段（如 status/count/hasResult）；禁止放原文与参数。"""
        self.attributes[str(key)] = value

    def end(self, status: str | None = None) -> None:
        if self._ended:
            return
        self._ended = True
        if status:
            self.status = status
        ended_at = _now_iso()
        duration_ms = int((time.monotonic() - self._t0) * 1000)
        _enqueue({
            "spanId": self.span_id, "traceId": self.trace_id,
            "parentSpanId": self.parent_span_id, "taskId": self.task_id,
            "conversationId": self.conversation_id, "callId": self.call_id,
            "subagentId": self.subagent_id, "runtimeVersion": self.runtime_version,
            "kind": self.kind, "name": self.name, "status": self.status,
            "errorCode": self.error_code, "retryCount": self.retry_count,
            "promptTokens": self.prompt_tokens, "completionTokens": self.completion_tokens,
            "budgetCost": self.budget_cost, "startedAt": self.started_at,
            "endedAt": ended_at, "durationMs": duration_ms, "attributes": self.attributes,
        })
        try:
            _current_span.reset(self._token_span)
            _current_trace.reset(self._token_trace)
        except (ValueError, RuntimeError):  # 跨线程结束时 reset 可能失败，忽略
            pass

    # -- 上下文管理 ---------------------------------------------------------

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            from .tool_journal import classify_exception

            self.set_error(classify_exception(exc))
        self.end()
        return False  # 不吞异常


def span(kind: str, name: str, **kwargs: Any) -> Span:
    """创建 Span（未初始化数据库时仍可用，只在结束时静默丢弃）。"""
    return Span(kind, name, **kwargs)


def current_trace() -> str | None:
    return _current_trace.get()


def current_span() -> str | None:
    return _current_span.get()


# ---------------------------------------------------------------------------
# 查询与本地诊断
# ---------------------------------------------------------------------------

def trace(task_id: str, trace_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """按任务（可选 trace）取 Span，按开始时间升序，用于重建父子调用链。"""
    db = get_db()
    if trace_id:
        rows = db.query(
            "SELECT * FROM runtime_spans WHERE task_id = ? AND trace_id = ? "
            "ORDER BY started_at ASC LIMIT ?",
            (task_id, trace_id, limit),
        )
    else:
        rows = db.query(
            "SELECT * FROM runtime_spans WHERE task_id = ? ORDER BY started_at ASC LIMIT ?",
            (task_id, limit),
        )
    return [
        {
            "spanId": r["span_id"],
            "traceId": r["trace_id"],
            "parentSpanId": r["parent_span_id"],
            "taskId": r["task_id"],
            "conversationId": r["conversation_id"],
            "callId": r["call_id"],
            "subagentId": r["subagent_id"],
            "runtimeVersion": r["runtime_version"],
            "kind": r["kind"],
            "name": r["name"],
            "status": r["status"],
            "errorCode": r["error_code"],
            "retryCount": int(r["retry_count"] or 0),
            "promptTokens": int(r["prompt_tokens"] or 0),
            "completionTokens": int(r["completion_tokens"] or 0),
            "budgetCost": _loads(r["budget_cost_json"]),
            "startedAt": r["started_at"],
            "endedAt": r["ended_at"],
            "durationMs": r["duration_ms"],
            "attributes": _loads(r["attributes_json"]),
        }
        for r in rows
    ]


def trace_summary(task_id: str) -> dict[str, Any]:
    """任务级技术摘要：按 kind 聚合数量、失败与耗时（不含任何内容字段）。"""
    rows = trace(task_id)
    by_kind: dict[str, dict[str, Any]] = {}
    for r in rows:
        bucket = by_kind.setdefault(r["kind"], {"count": 0, "errors": 0, "durationMs": 0})
        bucket["count"] += 1
        if r["status"] == "error":
            bucket["errors"] += 1
        bucket["durationMs"] += int(r["durationMs"] or 0)
    return {
        "taskId": task_id,
        "spanCount": len(rows),
        "byKind": by_kind,
        "droppedSpans": dropped_count(),
        "traces": sorted({r["traceId"] for r in rows}),
    }


def diagnostics() -> dict[str, Any]:
    """本地诊断包：版本、迁移版本、Span 计数与错误码；不含原文与密钥。"""
    from ..storage.migrations import MIGRATIONS

    info: dict[str, Any] = {
        "generatedAt": _now_iso(),
        "latestMigration": max(v for v, _ in MIGRATIONS),
        "droppedSpans": dropped_count(),
        "externalTelemetry": False,
    }
    try:
        db = get_db()
        row = db.query_one("SELECT COUNT(*) AS c FROM runtime_spans")
        info["spanCount"] = int((row or {}).get("c") or 0)
        codes = db.query(
            "SELECT error_code, COUNT(*) AS c FROM runtime_spans "
            "WHERE error_code IS NOT NULL GROUP BY error_code ORDER BY c DESC LIMIT 20"
        )
        info["errorCodes"] = {str(r["error_code"]): int(r["c"] or 0) for r in codes}
    except Exception:  # noqa: BLE001
        info["spanCount"] = None
    return info


# 诊断包**显式**排除的字段类别（用于 UI 展示与门禁断言，不依赖"恰好没写进去"）
EXCLUDED_FIELDS = (
    "case_text", "query_terms", "tool_arguments", "tool_results",
    "provider_credentials", "api_keys",
)


def diagnostics_bundle() -> dict[str, Any]:
    """可导出的本地诊断包（§7.7）：技术字段 + 明确排除清单，无外部遥测。

    Core RPC 与评测定性门禁都调用本函数，避免两处口径漂移。
    """
    info = diagnostics()
    try:
        from . import plugin_host

        plugins = plugin_host.status()
    except Exception:  # noqa: BLE001 插件子系统不可读不影响诊断本身
        plugins = {"error": "unavailable"}
    return {
        "kind": "lawbuddy-diagnostics",
        "version": 1,
        "exportedAt": info.get("generatedAt"),
        "workspaceSchema": {
            "latestMigration": info.get("latestMigration"),
            "spanCount": info.get("spanCount"),
            "droppedSpans": info.get("droppedSpans"),
        },
        "errorCodes": info.get("errorCodes") or {},
        "externalTelemetry": False,
        "plugins": plugins,
        "excluded": list(EXCLUDED_FIELDS),
    }


def _loads(raw: Any) -> Any:
    try:
        return json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
