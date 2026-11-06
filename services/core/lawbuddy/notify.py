"""stdout 唯一写出口（JSON-RPC notification 推送，REQ-AI-001 / P-010）。

- RPC 响应（rpc.py）与 Agent 事件推送（tasks.py，runner 线程）共用一把锁，
  保证每条消息单行原子写出，对端按行解析不受交错影响；
- 仅在 RpcServer.serve() 启动后启用（enable），evals 直跑 graph 时不启用，
  stdout 不受污染；
- 不 import 任何 lawbuddy 模块，杜绝循环依赖。
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

_lock = threading.Lock()
_enabled = False


def enable() -> None:
    global _enabled
    _enabled = True


def write_line(msg: dict[str, Any]) -> None:
    """单行写出 JSON 并 flush（RPC 响应与 notification 共用）。

    ensure_ascii=True：输出纯 ASCII（中文转 \\uXXXX，JSON 协议等价），对任意
    stdout 编码免疫——冻结模式标准流编码异常时协议通道也不会损坏。
    """
    with _lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=True) + "\n")
        sys.stdout.flush()


def emit(method: str, params: dict[str, Any]) -> None:
    """推送 JSON-RPC notification（无 id）；失败静默，不影响任务本身。"""
    if not _enabled:
        return
    try:
        write_line({"jsonrpc": "2.0", "method": method, "params": params})
    except Exception:
        pass  # 推送失败不中断任务；漏推由对端 afterSeq 轮询兜底
