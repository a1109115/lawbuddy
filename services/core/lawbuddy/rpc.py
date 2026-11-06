"""JSON-RPC 2.0 over stdio 服务循环。

协议约定：
- 每条消息为一行 JSON；
- 首个请求必须是 core.handshake，携带会话令牌；
- 后续请求需在 params._token 中回带令牌，否则拒绝；
- stderr 仅用于本地日志，协议消息只走 stdout。
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

from . import notify

Handler = Callable[[dict[str, Any]], Any]


class RpcServer:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._token: str | None = None
        self._handshaken = False
        self._shutdown = False

    def method(self, name: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self._handlers[name] = fn
            return fn

        return deco

    # -- 消息收发 -----------------------------------------------------------

    def _write(self, msg: dict[str, Any]) -> None:
        # 经 notify 统一加锁写出：RPC 响应与 runner 线程的 notification 推送串行化
        notify.write_line(msg)

    def _result(self, req_id: Any, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    # -- 主循环 -------------------------------------------------------------

    def serve(self) -> None:
        notify.enable()  # 启用 agent.event 等通知推送（evals 直跑 graph 时不经过此处）
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                self._error(None, -32700, "invalid json")
                continue

            req_id = req.get("id")
            method = req.get("method", "")
            params = req.get("params") or {}

            if not self._handshaken:
                if method != "core.handshake":
                    self._error(req_id, -32001, "handshake required")
                    continue
                self._handle_handshake(req_id, params)
                continue

            if params.get("_token") != self._token:
                self._error(req_id, -32002, "invalid session token")
                continue

            handler = self._handlers.get(method)
            if handler is None:
                self._error(req_id, -32601, f"method not found: {method}")
                continue
            try:
                result = handler(params)
            except (ValueError, RuntimeError) as exc:
                # 领域错误（CaseError / WorkspaceError / 导入失败等）：消息面向用户
                self._error(req_id, -32000, str(exc))
                continue
            except Exception as exc:  # 未预期异常：不出栈到对端，仅给类型摘要
                traceback.print_exc(file=sys.stderr)
                self._error(req_id, -32603, f"internal error: {type(exc).__name__}")
                continue
            self._result(req_id, result)
            if self._shutdown:
                break

        # stdin 关闭：通知后台任务（OCR/Agent）停止并短暂等待（页级/事件级原子性保证可恢复）
        try:
            from .agent.runner import AgentRunner

            AgentRunner.instance().shutdown(timeout=10.0)
        except Exception:
            pass
        try:
            from .ocr.pipeline import OcrManager

            OcrManager.instance().shutdown(timeout=15.0)
        except Exception:
            pass

    def _handle_handshake(self, req_id: Any, params: dict[str, Any]) -> None:
        handler = self._handlers.get("core.handshake")
        if handler is None:
            self._error(req_id, -32601, "core.handshake not registered")
            return
        self._token = str(params.get("token") or "")
        self._handshaken = True
        self._result(req_id, handler(params))

    def request_shutdown(self) -> None:
        self._shutdown = True
