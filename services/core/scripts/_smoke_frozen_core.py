"""冻结 lawbuddy-core.exe 冒烟测试（本地验证脚本，不进安装包）。

用法：python scripts/_smoke_frozen_core.py <dist_core 目录>
验证：主进程握手/ping、ocr-worker ready、decrypt-worker 进程可启动。
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path


def main() -> int:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist_core") / "lawbuddy-core"
    exe = dist / "lawbuddy-core.exe"
    assert exe.is_file(), f"missing {exe}"

    # 1) 主进程：握手 + ping
    proc = subprocess.Popen(
        [str(exe)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    lines: list[str] = []
    reader = threading.Thread(
        target=lambda: [lines.append(proc.stdout.readline()) for _ in range(2)], daemon=True
    )
    reader.start()
    proc.stdin.write(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "core.handshake",
                    "params": {"protocol": 1, "token": "t"}}) + "\n"
    )
    proc.stdin.write(
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {"_token": "t"}}) + "\n"
    )
    proc.stdin.flush()
    reader.join(timeout=120)
    proc.stdin.close()
    proc.wait(timeout=30)
    ok = (
        len(lines) == 2
        and '"id":1' in lines[0].replace(" ", "")
        and '"error"' not in lines[0]
        and '"pong":true' in lines[1].replace(" ", "")
    )
    caps = json.loads(lines[0])["result"] if lines and lines[0].strip() else {}
    # 密钥门禁：用户需自行配置大模型服务商 Key，Core 只回传布尔状态
    print(f"[smoke] apiKeyConfigured={caps.get('apiKeyConfigured')}")
    print(f"[smoke] handshake ok={ok} caps={caps}")
    print(f"[smoke] ping={lines[1].strip() if len(lines) > 1 else '<none>'}")
    if not ok:
        return 1

    # 2) ocr-worker：应输出 ready 行（含引擎加载，给足超时）
    w = subprocess.Popen(
        [str(exe), "ocr-worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    t0 = time.monotonic()

    def kill():
        time.sleep(600)
        if w.poll() is None:
            w.kill()

    threading.Thread(target=kill, daemon=True).start()
    ready_line = w.stdout.readline()
    dt = time.monotonic() - t0
    print(f"[smoke] ocr-worker ready ({dt:.1f}s): {ready_line.strip()[:200]}")
    w.stdin.close()
    try:
        w.wait(timeout=15)
    except subprocess.TimeoutExpired:
        w.kill()
    if '"ready":true' not in ready_line.replace(" ", ""):
        print("[smoke] FAIL: ocr-worker not ready")
        return 1

    # 3) decrypt-worker：喂空输入应立即退出（协议层 main 读完 stdin 返回）
    d = subprocess.run(
        [str(exe), "decrypt-worker"],
        input="",
        capture_output=True,
        timeout=120,
    )
    out = (d.stdout or b"").decode("utf-8", errors="replace")
    print(f"[smoke] decrypt-worker rc={d.returncode} out={out.strip()[:200]!r}")
    print("[smoke] ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
