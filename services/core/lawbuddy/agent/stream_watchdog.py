"""流式看门狗（LB-HERMES-2026-01 WP02）。

分别监控三个时间维度，并让取消可以穿透阻塞读取：

1. **首 Token 超时**：建连后迟迟没有第一帧（网关排队、模型冷启动）；
2. **帧间静默**：已开始输出但长时间没有新帧（连接假活、上游卡死）；
3. **总生命周期**：整次请求的最长耗时（防止无限续写）。

用法：

    wd = StreamWatchdog(cancel_check=..., first_token_s=90, idle_s=60, total_s=600)
    for line in stream:
        wd.tick()                 # 每收到一帧调用一次
        if wd.expired:            # 返回过期原因（None 表示健康）
            raise StreamStalled(wd.expired)

`expired` 同时检查取消位，因此即使读阻塞在外层 socket 上，调用方在超时窗口
内也能观察到取消并停止。本模块不直接终止 socket（Python 同步读取无法安全
中断），而是把判定上抛给调用方，由其决定续写、重试或取消。
"""

from __future__ import annotations

import time
from typing import Any, Callable

FIRST_TOKEN_S = 90.0
IDLE_S = 60.0
TOTAL_S = 600.0

REASON_FIRST_FRAME = "first_frame_timeout"
REASON_IDLE = "idle_timeout"
REASON_TOTAL = "total_timeout"
REASON_CANCELLED = "cancelled"


class StreamStalled(RuntimeError):
    """流式读取停滞或超时；`reason` 指明是首帧、帧间还是总时长。"""

    def __init__(self, reason: str, *, elapsed_s: float = 0.0, frames: int = 0):
        super().__init__(f"流式响应停滞（{reason}，已收 {frames} 帧，{elapsed_s:.0f}s）")
        self.reason = reason
        self.elapsed_s = elapsed_s
        self.frames = frames

    @property
    def resumable(self) -> bool:
        """已收到正文的停滞可续写；首帧超时不可续写（需重新建连）。"""
        return self.reason in (REASON_IDLE, REASON_TOTAL) and self.frames > 0


class StreamWatchdog:
    """流式健康监控；所有时间阈值均可按调用覆盖。"""

    def __init__(
        self,
        *,
        first_token_s: float = FIRST_TOKEN_S,
        idle_s: float = IDLE_S,
        total_s: float = TOTAL_S,
        cancel_check: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.first_token_s = float(first_token_s)
        self.idle_s = float(idle_s)
        self.total_s = float(total_s)
        self._cancel_check = cancel_check
        self._clock = clock
        self._started = clock()
        self._last_frame = self._started
        self.frames = 0
        self.bytes = 0

    # -- 观测 ---------------------------------------------------------------

    def tick(self, size: int = 1) -> None:
        """收到一帧时调用（`size` 为估算字节数，仅用于计数，不记录内容）。"""
        self.frames += 1
        self.bytes += max(0, int(size))
        self._last_frame = self._clock()

    @property
    def elapsed(self) -> float:
        return self._clock() - self._started

    @property
    def silent_for(self) -> float:
        return self._clock() - self._last_frame

    def cancelled(self) -> bool:
        try:
            return bool(self._cancel_check and self._cancel_check())
        except Exception:  # noqa: BLE001 取消检查自身异常不得误判为取消
            return False

    def expired(self) -> str | None:
        """返回过期原因；健康返回 None。取消优先级最高。"""
        if self.cancelled():
            return REASON_CANCELLED
        if self.frames == 0:
            if self.elapsed >= self.first_token_s:
                return REASON_FIRST_FRAME
            return None
        if self.elapsed >= self.total_s:
            return REASON_TOTAL
        if self.silent_for >= self.idle_s:
            return REASON_IDLE
        return None

    def check(self) -> None:
        """过期即抛 `StreamStalled`；取消由调用方按 `cancelled()` 自行处理。"""
        reason = self.expired()
        if reason is None or reason == REASON_CANCELLED:
            return
        raise StreamStalled(reason, elapsed_s=self.elapsed, frames=self.frames)

    def snapshot(self) -> dict[str, Any]:
        """技术快照（可进 Span attributes）：只有计数与耗时，没有内容。"""
        return {
            "frames": self.frames,
            "bytes": self.bytes,
            "elapsedMs": int(self.elapsed * 1000),
            "silentMs": int(self.silent_for * 1000),
        }
