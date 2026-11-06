# -*- coding: utf-8 -*-
"""Electron 桌面冒烟（需求文档 12.2 第三层）。

验证浏览器 mock 无法覆盖的桌面链路：
  1. IPC 桥：renderer → preload → 主进程 → Python Core（SQLCipher 工作区）真实读写
  2. 系统文件对话框：document.pick 打开原生选择框（Win32 窗口探测）并正常取消
  3. 加密落盘：导入的 docx 在工作区 blobs/ 下为 LAWBUDDY 加密格式，而非明文 PK zip
  4. 本机 Word 回读：word.createWorkCopy 对真实 docx 走「解密 blob → from_docx」
     全链路，模型文本须与原文档一致；getModel 经 IPC 读取同一模型

导入绕行说明：document.pick 是原生对话框 + 一次性内存 token，无法由 CDP 代选文件。
本脚本在启动 Electron 前，先用同一工作区以 JSON-RPC（与主进程导入同一 core 代码路径）
预置案件与 docx；随后由运行中的 App 通过真实 IPC 完成读取与 Word 回读。

前置条件：
  - 根目录执行过 npm run build（apps/desktop/out 产物存在）
  - services/core/.venv 可用（Core 依赖 SQLCipher / python-docx 等）
  - 无其他实例占用 CDP 9223（含 npm run dev）

使用方式（仓库根目录，系统 Python 需含 playwright + Chromium）：
  python scripts/e2e_desktop.py
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ELECTRON = REPO / "node_modules" / "electron" / "dist" / "electron.exe"
APP_DIR = REPO / "apps" / "desktop"
VENV_PY = REPO / "services" / "core" / ".venv" / "Scripts" / "python.exe"
CORE_DIR = REPO / "services" / "core"
SAMPLE_DOCX = REPO / "case_file" / "卷八_辩护及上诉" / "57_刑事上诉状.docx"
CDP_PORT = 9223
# 加密文件二进制格式标识，须与 services/core/lawbuddy/crypto.py 的 MAGIC 一致，禁止按品牌名修改
CRYPTO_MAGIC = b"SLVYUE\x01\x00"
DIALOG_TITLE = "选择要导入的案件文件"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return ok


# ---- Windows 工具 ---------------------------------------------------------


def documents_dir() -> Path:
    """Known Folder 'Documents'，与 Electron app.getPath('documents') 同源。"""
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        folderid_documents = GUID(
            0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7)
        )
        buf = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folderid_documents), 0, None, ctypes.byref(buf)) == 0:
            return Path(buf.value)
    except Exception:
        pass
    return Path.home() / "Documents"


def find_window_by_title(title: str, timeout_s: float) -> int:
    """轮询查找指定标题的顶层窗口（原生文件对话框），返回句柄（0 = 未找到）。"""
    import ctypes

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            return hwnd
        time.sleep(0.2)
    return 0


def close_window(hwnd: int) -> None:
    import ctypes

    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE → showOpenDialog 返回 canceled


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _clear_readonly(func, path, _exc) -> None:  # noqa: ANN001 — shutil 回调签名
    """rmtree 回调：core 将原件 blob 设为只读（documents._store_blob），须先清属性再删。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def rmtree_force(path: Path, retries: int = 3) -> None:
    """强制删除目录树（含只读 blob），重试后仍存在则抛错。

    不能用 ignore_errors=True：静默残留的旧 DEK 密文 blob 会被内容寻址去重
    复用，导致下次运行以新 DEK 解密旧密文 → CryptoError（parse failed）。
    """
    for _ in range(retries):
        if not path.exists():
            return
        shutil.rmtree(path, onexc=_clear_readonly)
        if not path.exists():
            return
        time.sleep(0.5)
    raise RuntimeError(f"无法清理工作区（文件被占用？）：{path}")


def kill_tree(pid: int, graceful_first: bool = True) -> None:
    if graceful_first:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
        try:
            time.sleep(1.5)
        except Exception:
            pass
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


# ---- 预置阶段：JSON-RPC 驱动 Core ----------------------------------------


class CoreRpc:
    """与 lawbuddy Core 的换行 JSON-RPC 会话（协议：首请求 core.handshake）。"""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._id = 0
        self._token = "e2e-desktop-seed"

    def call(self, method: str, params: dict | None = None, timeout_s: float = 90.0) -> object:
        self._id += 1
        body = dict(params or {})
        if method != "core.handshake":
            body["_token"] = self._token
        else:
            body["token"] = self._token
        req = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": body})
        assert self.proc.stdin
        self.proc.stdin.write(req + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            assert self.proc.stdout
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("Core 进程输出中断（stderr 见临时文件）")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("id") != self._id:
                continue  # 跳过 notification 推送
            if "error" in msg:
                raise RuntimeError(f"{method} 失败：{msg['error'].get('message')}")
            return msg.get("result")
        raise TimeoutError(f"{method} 超时")


def seed_workspace(ws_root: Path, docx_path: Path) -> tuple[str, str, str]:
    """预置：workspace.ensure → case.create → document.import_files → document.list。

    返回 (caseId, documentId, caseTitle)；与 Electron 主进程 document:import 走同一
    core 导入管线（加密落盘、SQLCipher 元数据），保证冒烟验证的是真实存储边界。
    """
    title = "桌面冒烟-阅卷案件"
    stderr_file = tempfile.NamedTemporaryFile(prefix="lawbuddy-seed-core-", suffix=".log", delete=False)
    proc = subprocess.Popen(
        [str(VENV_PY), "-m", "lawbuddy"],
        cwd=str(CORE_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_file,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        rpc = CoreRpc(proc)
        rpc.call("core.handshake")
        rpc.call("workspace.ensure", {"rootUri": str(ws_root)})
        case = rpc.call("case.create", {
            "title": title, "caseType": "other", "clientName": "冒烟客户", "defaultMode": "legal_review",
        })
        case_id = str(case["id"])
        outcome = rpc.call("document.import_files", {"caseId": case_id, "paths": [str(docx_path)]})
        imported = [o for o in outcome if o.get("status") == "imported"] if isinstance(outcome, list) else []
        if not imported:
            raise RuntimeError(f"docx 导入失败：{outcome}")
        doc_id = str(imported[0]["documentId"])
        docs = rpc.call("document.list", {"caseId": case_id})
        kinds = {d["id"]: d.get("kind") for d in docs}
        if kinds.get(doc_id) != "docx":
            raise RuntimeError(f"导入后 kind 异常：{kinds.get(doc_id)}")
        rpc.call("shutdown")
        return case_id, doc_id, title
    finally:
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
        stderr_file.close()
        Path(stderr_file.name).unlink(missing_ok=True)


# ---- 独立提取原文文本（不经 Core，作为对照基准） --------------------------


def docx_reference_text(path: Path) -> str:
    """直接从 docx 的 word/document.xml 提取最长 <w:t> 文本作为回读对照。"""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    texts = [html.unescape(t).strip() for t in re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S)]
    texts = [t for t in texts if len(t) >= 8]
    if not texts:
        raise RuntimeError("样本 docx 中未找到可断言的文本")
    return max(texts, key=len)


def model_texts(node: object) -> list[str]:
    out: list[str] = []

    def walk(n: object) -> None:
        if isinstance(n, dict):
            t = n.get("text")
            if isinstance(t, str):
                out.append(t)
            for c in n.get("content") or []:
                walk(c)
        elif isinstance(n, list):
            for c in n:
                walk(c)

    walk(node)
    return out


# ---- 主流程 ---------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="LawBuddy Electron 桌面冒烟")
    parser.add_argument("--ws-name", default="LawBuddyWorkspace-e2e-desktop", help="Documents 下的工作区目录名")
    parser.add_argument("--docx", default=str(SAMPLE_DOCX), help="导入用的样本 docx")
    parser.add_argument("--keep", action="store_true", help="结束后保留工作区与进程日志，便于排查")
    args = parser.parse_args()

    missing = [p for p in (ELECTRON, APP_DIR / "out" / "main" / "index.js", APP_DIR / "out" / "renderer" / "index.html", VENV_PY, Path(args.docx)) if not p.exists()]
    if missing:
        print("缺少前置文件：\n  " + "\n  ".join(str(p) for p in missing), file=sys.stderr)
        print("请先在根目录执行 npm run build，并确认 services/core/.venv 存在", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：请用系统 Python（含 playwright + Chromium）运行", file=sys.stderr)
        return 2

    if port_in_use(CDP_PORT):
        print(f"CDP 端口 {CDP_PORT} 已被占用：请先关闭正在运行的 LawBuddy 实例（含 npm run dev）", file=sys.stderr)
        return 2

    ws_root = documents_dir() / args.ws_name
    if ws_root.exists():
        rmtree_force(ws_root)  # 清理上次冒烟遗留（含只读 blob）；失败即中止，避免脏工作区毒化本次运行

    print("== 预置工作区（Core JSON-RPC） ==")
    case_id, doc_id, case_title = seed_workspace(ws_root, Path(args.docx))
    reference = docx_reference_text(Path(args.docx))
    print(f"  caseId={case_id} documentId={doc_id}")
    print(f"  回读对照文本（前 40 字）：{reference[:40]}")

    # ---- 加密落盘断言（磁盘层） ----
    print("== 加密落盘检查 ==")
    sha = hashlib.sha256(Path(args.docx).read_bytes()).hexdigest()
    blob = ws_root / "blobs" / sha[:2] / sha
    check("原件 blob 已落盘", blob.is_file(), str(blob))
    head = blob.read_bytes()[:8] if blob.is_file() else b""
    check("blob 为 LAWBUDDY 加密格式（非明文 docx）", head == CRYPTO_MAGIC, f"head={head!r}")

    # ---- 启动 Electron（生产模式：无 ELECTRON_RENDERER_URL，加载 out/renderer） ----
    print("== 启动 Electron ==")
    electron_log = tempfile.NamedTemporaryFile(prefix="lawbuddy-e2e-electron-", suffix=".log", delete=False)
    env = {**os.environ, "LAWBUDDY_WORKSPACE_DIR": args.ws_name, "LAWBUDDY_PYTHON": str(VENV_PY)}
    # CDP 说明：源码中 appendSwitch('remote-debugging-port') 仅 dev 模式生效；且
    # Chromium 136+ 在默认 user-data-dir 下会忽略该开关。因此走 CLI 开关并显式
    # 指定临时 user-data-dir（须置于 app 路径之前，Chromium 级解析）。
    user_data = tempfile.mkdtemp(prefix="lawbuddy-e2e-userdata-")
    app = subprocess.Popen(
        [str(ELECTRON), f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={user_data}", str(APP_DIR)],
        cwd=str(REPO),
        stdout=electron_log,
        stderr=subprocess.STDOUT,
        env=env,
    )

    print(f"== 桌面冒烟 → CDP http://127.0.0.1:{CDP_PORT} ==")
    try:
        with sync_playwright() as p:
            browser = None
            deadline = time.time() + 90
            while time.time() < deadline:
                if app.poll() is not None:
                    raise RuntimeError(f"Electron 提前退出（code={app.returncode}），日志：{electron_log.name}")
                try:
                    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
                    break
                except Exception:
                    time.sleep(1)
            if browser is None:
                raise TimeoutError("无法连接 CDP 9223")

            page = None
            deadline = time.time() + 30
            while time.time() < deadline:
                for ctx in browser.contexts:
                    for pg in ctx.pages:
                        if pg.url.startswith("file://"):
                            page = pg
                            break
                if page:
                    break
                time.sleep(0.5)
            if page is None:
                raise RuntimeError("未找到应用页面（file://）")

            run(page, case_id, doc_id, case_title, reference)
            browser.close()
    except Exception as exc:  # noqa: BLE001 — 冒烟脚本需要完整失败信息
        print(f"\n[ERROR] {exc}")
        raise
    finally:
        kill_tree(app.pid)
        electron_log.close()
        if not args.keep:
            Path(electron_log.name).unlink(missing_ok=True)
            try:
                rmtree_force(ws_root)
            except RuntimeError as exc:
                print(f"[WARN] {exc}", file=sys.stderr)
            shutil.rmtree(user_data, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n桌面冒烟结果：{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


def run(page, case_id: str, doc_id: str, case_title: str, reference: str) -> None:
    # ---- 1. 应用加载（真实主进程 + 真实 Core） ----
    # 注：ModeTabs 在选中案件后不渲染（store 启动即自动选中最近活跃案件），
    # 不能以 .mode-tabs 作为加载锚点；以常驻的 .sidebar 为锚，案件标题可见性由下一项断言覆盖。
    page.wait_for_selector(".sidebar", timeout=90000)
    check("应用加载（标题）", page.title() == "LawBuddy", page.title())

    try:
        page.get_by_text(case_title).first.wait_for(state="visible", timeout=20000)
        check("侧栏可见预置案件（UI 读取真实工作区）", True)
    except Exception as exc:  # noqa: BLE001
        check("侧栏可见预置案件（UI 读取真实工作区）", False, str(exc)[:120])

    has_bridge = page.evaluate("typeof window.lawbuddy === 'object' && window.lawbuddy !== null")
    check("window.lawbuddy IPC 桥可用", bool(has_bridge))

    # ---- 2. IPC 真实读写（renderer → preload → main → Core → SQLCipher） ----
    cases = page.evaluate("window.lawbuddy.case.list(false)")
    check("IPC case.list 返回预置案件", any(c.get("id") == case_id for c in cases))

    docs = page.evaluate(f"window.lawbuddy.document.list({json.dumps(case_id)})")
    doc = next((d for d in docs if d.get("id") == doc_id), None)
    check("IPC document.list 返回 docx 文档",
          doc is not None and doc.get("kind") == "docx",
          json.dumps(doc, ensure_ascii=False)[:120] if doc else "missing")

    # ---- 3. 本机 Word 回读：createWorkCopy → 解密 → from_docx ----
    created = page.evaluate(
        f"window.lawbuddy.word.createWorkCopy({json.dumps({'caseId': case_id, 'documentId': doc_id, 'simplify': True})})"
    )
    ok_created = isinstance(created, dict) and isinstance(created.get("model"), dict)
    check("IPC word.createWorkCopy 成功", ok_created,
          json.dumps(created, ensure_ascii=False)[:160] if not ok_created else "")

    model_text = "".join(model_texts(created.get("model"))) if ok_created else ""
    check("工作副本模型含原文文本（解密→from_docx 链路）", reference in model_text,
          f"model text len={len(model_text)}")

    got = page.evaluate(f"window.lawbuddy.word.getModel({json.dumps(created['id'])})") if ok_created else {}
    got_text = "".join(model_texts(got.get("model") if isinstance(got, dict) else None))
    check("IPC word.getModel 模型与原文一致", reference in got_text, f"get text len={len(got_text)}")

    copies = page.evaluate(f"window.lawbuddy.word.listWorkCopies({json.dumps(case_id)})")
    check("IPC word.listWorkCopies 含新副本",
          any(c.get("id") == created.get("id") for c in copies) if ok_created else False)

    # ---- 4. 系统文件对话框：原生窗口出现并取消 ----
    page.evaluate(
        "() => { window.__pick = null;"
        " window.lawbuddy.document.pick()"
        "   .then(v => { window.__pick = { ok: true, value: v } })"
        "   .catch(e => { window.__pick = { ok: false, err: String(e) } });"
        " return 'started'; }"
    )
    hwnd = find_window_by_title(DIALOG_TITLE, timeout_s=15)
    check("原生文件对话框打开（Win32 窗口探测）", hwnd != 0, "未找到标题匹配的对话框窗口")
    if hwnd:
        close_window(hwnd)
    else:
        # 兜底：窗口标题不匹配时用 ESC 尝试关闭可能存在的对话框，避免挂起
        import ctypes

        ctypes.windll.user32.keybd_event(0x1B, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0x1B, 0, 2, 0)
    deadline = time.time() + 10
    pick_result = None
    while time.time() < deadline:
        pick_result = page.evaluate("window.__pick")
        if pick_result:
            break
        time.sleep(0.3)
    check("对话框取消后 pick 返回 null（取消路径）",
          bool(pick_result) and pick_result.get("ok") and pick_result.get("value") is None,
          json.dumps(pick_result, ensure_ascii=False) if pick_result else "no result")


if __name__ == "__main__":
    sys.exit(main())
