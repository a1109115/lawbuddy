"""服务层解锁链路端到端验证（REQ-DOC-004/011/012/013）。

通过真实 stdio JSON-RPC 协议驱动 Python Core 子进程，验证：
- 导入加密文档 → need_password；
- 错误密码明确失败，正确密码解锁并解析；
- 密码不进入审计记录（REQ-DOC-011）；
- 解锁后原件摘要不变、页可读。

用法：python scripts/_smoke_unlock_rpc.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'} {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


class Core:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "lawbuddy"],
            cwd=str(CORE_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        )
        self.seq = 1
        self.token: str | None = None

    def call(self, method: str, params: dict | None = None) -> dict:
        body = dict(params or {})
        if self.token:
            body["_token"] = self.token
        req = {"jsonrpc": "2.0", "id": self.seq, "method": method, "params": body}
        self.seq += 1
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("core 已退出")
            msg = json.loads(line)
            if msg.get("id") == req["id"]:
                return msg

    def close(self) -> None:
        try:
            self.call("shutdown")
        except Exception:
            pass
        self.proc.kill()


def main() -> int:
    from pypdf import PdfReader, PdfWriter

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-unlock-rpc-"))
    corpus = Path("E:/project/Slvyue/case_file")
    source = None
    for candidate in sorted(corpus.rglob("*.pdf")):
        try:
            reader = PdfReader(str(candidate))
        except Exception:
            continue
        if reader.pages:
            source = (candidate, reader)
            break
    if source is None:
        check("找到 PDF 语料", False, str(corpus))
        return 1
    _, reader = source
    expected_pages = len(reader.pages)

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password="pass-123", owner_password="owner")
    enc = tmp / "加密证据.pdf"
    with enc.open("wb") as f:
        writer.write(f)

    core = Core()
    try:
        hs = core.call("core.handshake", {"protocol": 1, "token": "t"})
        core.token = "t"
        check("Core 握手成功", "result" in hs, str(hs.get("error", "")))

        ws = core.call("workspace.ensure", {"rootUri": str(tmp / "ws")})
        check("工作空间初始化（加密库）", "result" in ws, str(ws.get("error", "")))

        cases = core.call("case.list", {})
        case_id = cases["result"][0]["id"]

        imported = core.call("document.import_files", {"caseId": case_id, "paths": [str(enc)]})
        outcome = imported["result"][0]
        check("导入标记 need_password", outcome["status"] == "need_password", str(outcome["status"]))
        doc_id = outcome["documentId"]

        before = core.call("document.list", {"caseId": case_id})
        locked_doc = next(d for d in before["result"] if d["id"] == doc_id)
        sha_before = locked_doc["sha256"]

        wrong = core.call("document.request_unlock", {"documentId": doc_id, "password": "wrong"})
        check(
            "错误密码明确失败",
            "error" in wrong and "密码" in str(wrong["error"].get("message", "")),
            str(wrong.get("error", {}).get("message", ""))[:40],
        )

        good = core.call("document.request_unlock", {"documentId": doc_id, "password": "pass-123"})
        check("正确密码解锁成功", good.get("result", {}).get("unlocked") is True, str(good)[:80])

        pages = core.call("document.get_pages", {"documentId": doc_id})
        check("解锁后页数一致", len(pages["result"]) == expected_pages,
              f"{len(pages['result'])} vs {expected_pages}")

        after = core.call("document.list", {"caseId": case_id})
        unlocked_doc = next(d for d in after["result"] if d["id"] == doc_id)
        check("原件摘要不变（REQ-DOC-002）", unlocked_doc["sha256"] == sha_before)
        check("文档状态推进为可用", unlocked_doc["status"] in ("ready", "partial"), unlocked_doc["status"])

        search = core.call("document.search", {"caseId": case_id, "query": "。"})
        check("解锁后全文可检索", len(search["result"]) > 0, f"{len(search['result'])} 条命中")

        logs = core.call("audit.list", {"caseId": case_id, "limit": 50})
        dumped = json.dumps(logs["result"], ensure_ascii=False)
        check("审计不含密码（REQ-DOC-011）", "pass-123" not in dumped and '"wrong"' not in dumped)
        check("审计含解锁记录", any(e.get("action") == "document.unlock" for e in logs["result"]))

        verify = core.call("audit.verify", {})
        check("审计哈希链完整", verify.get("result", {}).get("ok") is True, str(verify.get("result"))[:60])
    finally:
        core.close()

    print(f"\n{'全部通过' if not FAILURES else '失败项: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())