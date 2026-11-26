"""统一并发控制与熔断（LB-HARNESS-2026-01 §4.9 / B-04）。

模型工作流（主循环之外的子代理、并行调查）共用同一个闸门：

- 初始最多 2 个并发模型工作流；
- 429 / 连续断连 / 超时自动降并发（2 → 1 → 熔断冷却），恢复成功后逐步放开；
- 熔断期间新的工作流请求直接等待或拒绝，不产生静默空结果；
- 恢复后按工作单元重试，已提交的副作用不重放（幂等键由工作账本保证）。

闸门只维护计数与状态，不感知任务内容，也不记录任何案件原文。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

INITIAL_CONCURRENCY = 2
MIN_CONCURRENCY = 1
# 连续瞬态失败达到该次数即熔断：暂停新工作流，等待冷却期
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_S = 20.0
MAX_COOLDOWN_S = 120.0


class ConcurrencyGate:
    """线程安全的模型工作流闸门（进程内单例，见 `gate()`）。"""

    def __init__(self, limit: int = INITIAL_CONCURRENCY) -> None:
        self._lock = threading.Condition()
        self._limit = max(MIN_CONCURRENCY, int(limit))
        self._in_flight = 0
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._cooldown = BREAKER_COOLDOWN_S
        self._degraded = False

    # ---- 采集 ----
    @property
    def limit(self) -> int:
        with self._lock:
            return self._limit

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "limit": self._limit,
                "inFlight": self._in_flight,
                "consecutiveFailures": self._consecutive_failures,
                "open": time.monotonic() < self._open_until,
                "degraded": self._degraded,
            }

    # ---- 闸门 ----
    @contextmanager
    def acquire(self, *, timeout: float = 120.0) -> Iterator[None]:
        """占用一个工作流名额；熔断期内等待冷却，不静默返回空结果。"""
        deadline = time.monotonic() + max(1.0, timeout)
        with self._lock:
            while True:
                now = time.monotonic()
                if now < self._open_until:
                    remaining = min(self._open_until - now, deadline - now)
                    if remaining <= 0:
                        raise TimeoutError("模型工作流熔断中，等待冷却超时")
                    self._lock.wait(timeout=remaining)
                    continue
                if self._in_flight < self._limit:
                    self._in_flight += 1
                    break
                if deadline - now <= 0:
                    raise TimeoutError("模型工作流并发已满，等待超时")
                self._lock.wait(timeout=min(5.0, deadline - now))
        try:
            yield
        finally:
            with self._lock:
                self._in_flight = max(0, self._in_flight - 1)
                self._lock.notify_all()

    # ---- 反馈 ----
    def on_success(self) -> None:
        """成功一次：清连续失败计数，冷却减半，逐步恢复并发。"""
        with self._lock:
            self._consecutive_failures = 0
            self._cooldown = BREAKER_COOLDOWN_S
            if self._degraded and self._limit < INITIAL_CONCURRENCY:
                self._limit += 1
                if self._limit >= INITIAL_CONCURRENCY:
                    self._degraded = False
            self._lock.notify_all()

    def on_failure(self, kind: str) -> None:
        """瞬态失败（429/timeout/disconnect）：降并发；连续失败则熔断冷却。"""
        with self._lock:
            self._consecutive_failures += 1
            if self._limit > MIN_CONCURRENCY:
                self._limit = MIN_CONCURRENCY
                self._degraded = True
            if self._consecutive_failures >= BREAKER_THRESHOLD:
                self._open_until = time.monotonic() + self._cooldown
                self._cooldown = min(MAX_COOLDOWN_S, self._cooldown * 2)
                self._consecutive_failures = 0
            self._lock.notify_all()

    def reset(self) -> None:
        """任务结束或显式恢复时复位（不影响其他任务的进行中计数）。"""
        with self._lock:
            self._limit = INITIAL_CONCURRENCY
            self._in_flight = 0
            self._consecutive_failures = 0
            self._open_until = 0.0
            self._cooldown = BREAKER_COOLDOWN_S
            self._degraded = False
            self._lock.notify_all()


_GATE_LOCK = threading.Lock()
_GATE: ConcurrencyGate | None = None


def gate() -> ConcurrencyGate:
    """进程级单例：所有模型工作流共用同一并发上限与熔断状态。"""
    global _GATE
    with _GATE_LOCK:
        if _GATE is None:
            _GATE = ConcurrencyGate()
        return _GATE


def classify_error(exc: BaseException) -> str:
    """把异常归到闸门可识别的失败类别（未知按 transient 保守处理）。"""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "rate" in text or "429" in text or "限流" in text:
        return "rate_limited"
    if "timeout" in name or "timed out" in text or "超时" in text:
        return "timeout"
    if "connection" in name or "disconnect" in text or "remote" in text or "中断" in text:
        return "disconnect"
    return "transient"
