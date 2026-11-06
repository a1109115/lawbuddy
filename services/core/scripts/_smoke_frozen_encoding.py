"""冻结 exe 中文编码冒烟（打包前必跑）：

背景：PyInstaller 冻结模式下 PYTHONIOENCODING 失效，Windows 中文系统标准流
落回 cp936 —— RPC 中文响应被 Electron 按 UTF-8 解码成 U+FFFD 乱码。修复见
lawbuddy/__main__.py(_force_utf8_stdio) 与 notify.py(ensure_ascii=True)。

验证：workspace.ensure → case.list 的中文（演示案件标题）往返无损。
用法：.venv/Scripts/python.exe scripts/_smoke_frozen_encoding.py [dist_core目录]
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def main() -> int:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist_core") / "lawbuddy-core"
    exe = dist / "lawbuddy-core.exe"
    assert exe.is_file(), f"missing {exe}"

    root = tempfile.mkdtemp(prefix="lawbuddy-enc-test-")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}

    proc = subprocess.Popen(
        [str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    raw: list[bytes] = []
    reader = threading.Thread(
        target=lambda: [raw.append(proc.stdout.readline()) for _ in range(3)], daemon=True
    )
    reader.start()
    for msg in (
        {"jsonrpc": "2.0", "id": 1, "method": "core.handshake", "params": {"protocol": 1, "token": "t"}},
        {"jsonrpc": "2.0", "id": 2, "method": "workspace.ensure", "params": {"rootUri": root, "_token": "t"}},
        {"jsonrpc": "2.0", "id": 3, "method": "case.list", "params": {"includeArchived": False, "_token": "t"}},
    ):
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    proc.stdin.flush()
    reader.join(timeout=180)
    proc.stdin.close()
    proc.wait(timeout=30)

    if len(raw) < 3:
        print("[enc-smoke] FAIL: 响应不足")
        return 1
    # 双重校验：原始字节必须纯 ASCII（协议通道编码免疫）+ 解析后中文完整
    for i, r in enumerate(raw):
        if any(b > 0x7F for b in r):
            print(f"[enc-smoke] FAIL: resp {i + 1} 含非 ASCII 字节（编码未免疫）")
            return 1
    cases = json.loads(raw[2].decode("utf-8"))["result"]
    items = cases if isinstance(cases, list) else cases.get("cases", [])
    title = items[0].get("title", "") if items else ""
    if "张三诉李四民间借贷纠纷案" not in title:
        print(f"[enc-smoke] FAIL: 演示案件标题损坏: {title!r}")
        return 1
    print(f"[enc-smoke] PASS: 协议纯 ASCII，案件标题往返无损 {title!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
