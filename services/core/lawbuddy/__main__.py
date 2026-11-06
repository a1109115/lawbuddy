"""入口：`python -m lawbuddy`。

Electron Main 以子进程方式启动本模块，通过 stdin/stdout 进行
换行分隔的 JSON-RPC 2.0 通信（技术方案 §2.2）。

PyInstaller 冻结模式（打包产物 lawbuddy-core.exe）复用同一入口：
主进程无参数启动 RPC 服务；OCR/解密 Worker 由冻结主进程以
`lawbuddy-core.exe ocr-worker` / `lawbuddy-core.exe decrypt-worker`
子命令拉起（冻结后 sys.executable 是自身 exe，`-m` 不可用）。
"""

import os
import sys


def _run_worker(name: str) -> None:
    if name == "ocr-worker":
        from .ocr.worker import main as worker_main

        worker_main()
    elif name == "decrypt-worker":
        from .decrypt.worker import main as worker_main

        raise SystemExit(worker_main())
    else:
        raise SystemExit(f"unknown subcommand: {name}")


def _force_utf8_stdio() -> None:
    """强制标准流 UTF-8。

    PyInstaller 冻结模式下 PYTHONIOENCODING 不生效（bootloader 不透传），
    Windows 中文系统 stdout/stdin 落回 ANSI 代码页（cp936/GBK）：中文 RPC
    响应被 Electron 按 UTF-8 解码成乱码（U+FFFD），中文请求在 stdin 侧同样
    损坏。显式 reconfigure 覆盖主进程与 worker 子命令全部路径。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001  # 非常规流（evals 直跑等）：忽略
            pass


def main() -> None:
    _force_utf8_stdio()

    # multiprocessing freeze_support：冻结环境下必须在主模块最先调用（无害于源码运行）
    import multiprocessing

    if getattr(sys, "frozen", False):
        multiprocessing.freeze_support()
        # Worker 子进程在 Windows 冻结模式下由 multiprocessing 派生时会带
        # --multiprocessing-fork 参数，交给 freeze_support 处理后直接返回
        if "--multiprocessing-fork" in sys.argv:
            return

    if len(sys.argv) > 1:
        _run_worker(sys.argv[1])
        return

    # RapidTable 表格还原模型（O-015 阶段 4）：打包产物随包分发（PyInstaller
    # datas → _MEIPASS/models），缺省时 rapid_table 会尝试联网下载——打包环境
    # 必须离线可用，故冻结启动时指向捆绑模型
    if not os.environ.get("LAWBUDDY_TABLE_MODEL_PATH", "").strip() and getattr(sys, "frozen", False):
        for base in (getattr(sys, "_MEIPASS", None), os.path.dirname(sys.executable)):
            if not base:
                continue
            bundled = os.path.join(base, "models", "slanet-plus.onnx")
            if os.path.isfile(bundled):
                os.environ["LAWBUDDY_TABLE_MODEL_PATH"] = bundled
                break

    from .rpc import RpcServer
    from .service import register

    server = RpcServer()
    register(server)
    server.serve()


if __name__ == "__main__":
    main()
