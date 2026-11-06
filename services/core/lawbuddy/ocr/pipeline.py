"""OCR 任务管线（DEV-DOC-006：按页 OCR、进度、暂停、取消、重试、断点续跑）。

- 识别在独立 Worker 子进程执行（技术方案 §2.2：单页崩溃不拖垮应用）；
- 多 Worker 页级并行 + 渲染/识别/落库流水线（05-OCR迁移方案 §3.4，对齐律阅助手
  处理速度）：生产者线程预渲染页图片，N 个消费者线程各持一个 Worker 子进程
  并行识别（每 Worker 配 cpu/N 线程），落库经 Database._lock 天然串行；
- Core 侧后台线程编排，RPC 立即返回，进度经 document.ocr.status 轮询；
- 按页幂等：只取 ocr_status='pending' 的页，暂停/崩溃后重启即断点续跑；
- Worker 崩溃自动重启（最多 2 次），当前页重试；重启超限后剩余 pending 页
  全部隔离（FR-005）；单页识别失败重试 2 次后隔离该页并继续后续页；
- 结果原子落库：text_blocks（bbox/置信度/块哈希）+ 页文本回填 + FTS 同步；
- 页均置信度 < 0.85 计入 lowConfPages（LOW_OCR_CONFIDENCE 待人工核实，技术方案 §9.4）。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..storage import get_db
from .. import tempstore
from ..workspace import now_iso
from .engine import LOW_CONFIDENCE, close_pdf, iter_page_images, render_pdf_page

WORKER_READY_TIMEOUT_S = 300  # 首次含引擎与模型加载
WORKER_PAGE_TIMEOUT_S = 180  # 单页识别上限：Worker/推理卡死按进程级故障重启，不留「永久解析中」
WORKER_RESTART_MAX = 2
MAX_AUTO_RESUME = 2  # 启动/切换工作空间时自动续跑的文档数上限（每份文档自带并行 Worker，避免进程爆量）
# 系统级守护扫描间隔：文档一旦有 pending 页，Core 自己会在该间隔内接手识别，
# 不依赖渲染进程是否调过 ocr.start、是否打开着文档列表、是否换了工作空间。
WATCHDOG_INTERVAL_S = 3.0
# 停滞判定：识别线程仍在，但「已完成页数」在这么久内毫无变化，视为卡死。
# 单页上限 180s（WORKER_PAGE_TIMEOUT_S）已经能杀掉卡住的一页，超过 4 分钟
# 一页都没完成说明队列/线程链路已坏，必须重启，否则前端永久停在「解析中 0/N」。
OCR_STALL_RESTART_S = 240.0
# 停滞重启次数上限：多次重启仍无任何进展时收口为失败页，显式告知用户，
# 不让文档无限期停在「解析中」。
OCR_STALL_RESTART_MAX = 3

# 允许被 OCR 结果推进为终态的文档状态：导入中断/Core 崩溃可能把文档留在
# parsing / ocr_queued 等中间态，页都识别完了却没人提升，前端就永久「解析中」。
PROMOTABLE_STATUSES = (
    "pending", "parsing", "ocr_queued", "ocr_running", "indexing", "partial", "failed",
)


def _readline_with_timeout(stream: Any, timeout: float) -> str | None:
    """带超时的 `readline`（Windows 不支持对管道 select）。

    超时返回 None；调用方必须终止该 Worker，不能复用超时后的协议通道。
    """
    box: dict[str, str | None] = {}

    def _read() -> None:
        try:
            box["line"] = stream.readline()
        except Exception:  # noqa: BLE001  # 管道已关闭等：按无输出处理
            box["line"] = None

    reader = threading.Thread(target=_read, daemon=True, name="ocr-worker-read")
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        return None
    return box.get("line")


def _worker_command(subcommand: str) -> list[str]:
    """Worker 启动命令：源码运行 `python -m lawbuddy.<worker 模块>`；
    PyInstaller 冻结后 sys.executable 是 lawbuddy-core.exe 自身，
    以子命令分发（见 lawbuddy.__main__）。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, subcommand]
    module = "lawbuddy.ocr.worker" if subcommand == "ocr-worker" else "lawbuddy.decrypt.worker"
    return [sys.executable, "-m", module]


def _worker_count() -> int:
    """并行 Worker 数（05-OCR迁移方案 §3.4）：LAWBUDDY_OCR_WORKERS 覆盖；
    默认按核数配分（≥16 核 4 个、8 核 2 个、≤4 核 1 个），兼顾内存占用。"""
    env = os.environ.get("LAWBUDDY_OCR_WORKERS", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return max(1, min(4, (os.cpu_count() or 8) // 4))

# PDF 源跨页复用（05-OCR迁移方案 §3.2）：同一文档只物料化（解密）一次，
# 是引擎文档级渲染缓存「同一 PDF 多页连跑不重复打开/解析」的前提。
_pdf_hold_lock = threading.Lock()
_pdf_hold: dict[str, tuple[str, Path]] = {}  # document_id -> (blob_uri, 物料化 PDF 路径)


def _pdf_source(root: Path, page: dict[str, Any]) -> Path:
    """取当前页的 PDF 可读源：同一文档跨页复用同一物料化副本。"""
    key = page["document_id"]
    with _pdf_hold_lock:
        held = _pdf_hold.get(key)
        if held is not None and held[0] == page["blob_uri"]:
            return held[1]
    blob = tempstore.materialize(root, page["blob_uri"], page["case_id"], "pdf")
    with _pdf_hold_lock:
        held = _pdf_hold.get(key)
        if held is not None:
            # 防御：同文档活动版本变化时替换旧源
            _release_pdf_source(root, held[1])
        _pdf_hold[key] = (page["blob_uri"], blob)
    return blob


def _release_pdf_source(root: Path, path: Path) -> None:
    close_pdf(path)  # 先关引擎句柄，否则 Windows 下临时文件删除失败
    tempstore.release(root, path)


def _release_pdf_hold(root: Path, document_id: str) -> None:
    """任务结束（完成/暂停/取消/失败）释放该文档持有的 PDF 源。"""
    with _pdf_hold_lock:
        held = _pdf_hold.pop(document_id, None)
    if held is not None:
        _release_pdf_source(root, held[1])


def release_all_pdf_sources(root: Path) -> None:
    """释放全部跨页持有的 PDF 明文副本（切换工作空间前调用）。"""
    with _pdf_hold_lock:
        held = [_pdf_hold.pop(key) for key in list(_pdf_hold)]
    for _, path in held:
        try:
            _release_pdf_source(root, path)
        except Exception:
            pass


class OcrTaskError(RuntimeError):
    """Worker 进程级故障（退出、超时、初始化失败）：重启 Worker 后重试当前页。"""


class OcrPageError(RuntimeError):
    """单页识别失败（Worker 存活，返回 ok=false）：隔离该页并继续后续页（FR-005）。"""


class _OcrWorker:
    """PaddleOCR Worker 子进程封装（启动 → ready → 逐页请求/响应）。

    cpu_threads：多 Worker 并行时的线程配分（经 LAWBUDDY_OCR_CPU_THREADS 传给
    Worker 进程内引擎配方，见 engine._cpu_threads）；None 用引擎默认。
    """

    def __init__(self, cpu_threads: int | None = None) -> None:
        env = {
            **os.environ,
            # 注：不再设 OMP_NUM_THREADS=1——oneDNN/MKLDNN 走 OMP 线程池，
            # 锁死 1 会使多线程配方失效（05-OCR迁移方案 §3.1 线程调优随配方一并生效）
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "MODELSCOPE_OFFLINE": "1",
        }
        if cpu_threads is not None:
            env["LAWBUDDY_OCR_CPU_THREADS"] = str(cpu_threads)
        self.proc = subprocess.Popen(
            _worker_command("ocr-worker"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # 继承 Core 的 stderr（Paddle 日志；Electron Main 持续消费）
            text=True,
            encoding="utf-8",
            env=env,
        )
        # 等待 ready（引擎加载完成）：超时视为进程级故障，终止后由管线重启/隔离
        line = _readline_with_timeout(self.proc.stdout, WORKER_READY_TIMEOUT_S)
        try:
            hello = json.loads(line) if line else {}
        except json.JSONDecodeError:
            hello = {}
        if not hello.get("ready"):
            self.close()
            raise OcrTaskError(
                "OCR Worker 初始化超时" if line is None else "OCR Worker 初始化失败"
            )

    def ocr(self, image_path: str, kind: str | None = None) -> dict[str, Any]:
        self.proc.stdin.write(
            json.dumps({"imagePath": image_path, "kind": kind}, ensure_ascii=False) + "\n"
        )
        self.proc.stdin.flush()
        deadline = time.monotonic() + WORKER_PAGE_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OcrTaskError("OCR Worker 响应超时")
            line = _readline_with_timeout(self.proc.stdout, remaining)
            if line is None:
                raise OcrTaskError("OCR Worker 响应超时")
            if not line:
                raise OcrTaskError("OCR Worker 已退出")
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue  # 防御：跳过任何非协议输出
            if "ok" in resp:
                if resp["ok"]:
                    return resp
                # 页级失败（Worker 存活）：隔离该页，不重启 Worker（FR-005）
                raise OcrPageError(resp.get("error", "OCR failed"))

    def close(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    def terminate(self) -> None:
        """立即终止 Worker（取消路径专用，保证 O-007 取消响应 ≤3 秒）。

        终止后本页保持 pending，重启/恢复时断点续跑。
        """
        try:
            self.proc.kill()
        except Exception:
            pass


class OcrManager:
    """单实例管理全部文档的 OCR 后台任务（同一文档串行，一次只跑一个文档的线程）。"""

    _instance: "OcrManager | None" = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._pause_flags: dict[str, threading.Event] = {}
        self._state: dict[str, dict[str, Any]] = {}
        self._workers: dict[str, list[_OcrWorker]] = {}  # 在跑的 Worker（取消时立即全部终止）
        self._resume_thread: threading.Thread | None = None  # 自动续跑调度线程
        self._resume_stop: threading.Event | None = None
        # 系统级守护线程：无论前端是否轮询/是否换过工作空间，都保证 pending 页被接手
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop: threading.Event | None = None
        # 工作空间切换/退出窗口期：拒绝登记新任务，避免线程在旧库关闭后仍去写入
        self._suspended = False
        # 用户显式暂停/取消的文档：系统级守护不得替用户恢复（否则「暂停」形同虚设）
        self._user_stopped: set[str] = set()
        # 停滞检测：document_id -> (最近观察到的已完成页数, 该进度首次出现时刻, 已重启次数)
        self._progress: dict[str, tuple[int, float, int]] = {}

    @classmethod
    def instance(cls) -> "OcrManager":
        if cls._instance is None:
            cls._instance = OcrManager()
        return cls._instance

    # ---- 对外接口（RPC 调用，stdin 线程） -----------------------------------

    def start(self, document_id: str) -> dict[str, Any]:
        # 用户显式启动 = 恢复意图：解除此前 pause/cancel 留下的自动接管豁免
        with self._lock:
            self._user_stopped.discard(document_id)
        db = get_db()
        doc = db.query_one(
            """
            SELECT d.id, d.kind, d.case_id, d.title,
                       COALESCE(v.unlocked_blob_uri, v.blob_uri) AS blob_uri, v.id AS version_id
            FROM documents d JOIN document_versions v ON v.id = d.active_version_id
            WHERE d.id = ?
            """,
            (document_id,),
        )
        if doc is None:
            raise OcrTaskError("文档不存在")
        counts = self._count_pages(document_id)
        if counts["pending"] == 0:
            state = self._state.get(document_id, {}).get("state", "idle")
            if state in ("", "running", None):
                state = "failed" if counts["failed"] > 0 else "done"
            return self._snapshot(document_id, state or "done", counts)

        with self._lock:
            if self._suspended:
                # 工作空间正在切换/退出：旧库即将关闭，不得再登记任务
                raise OcrTaskError("工作空间正在切换，请稍后重试")
            t = self._threads.get(document_id)
            already_running = t is not None and t.is_alive()
            if not already_running:
                self._cancel_flags[document_id] = threading.Event()
                self._pause_flags[document_id] = threading.Event()
                thread = threading.Thread(
                    target=self._run_worker, args=(document_id,), daemon=True, name=f"ocr-{document_id[:8]}"
                )
                self._threads[document_id] = thread
        if already_running:
            # 重复 start（导入并发 + 轮询续跑）必须立即返回：`_snapshot` 自身要拿同一把
            # 非重入锁，绝不能在 `with self._lock:` 内调用，否则整个 stdin RPC 主循环
            # 被冻死，前端表现为永久「解析中」且 Core 连 ping 都不响应。
            return self._snapshot(document_id, "running", counts)
        thread.start()
        return self._snapshot(document_id, "running", counts)

    def pause(self, document_id: str) -> dict[str, Any]:
        flag = self._pause_flags.get(document_id)
        if flag:
            flag.set()
        # 记录用户主动暂停：守护线程不得替用户恢复
        with self._lock:
            self._user_stopped.add(document_id)
        return self.status(document_id)

    def resume(self, document_id: str) -> dict[str, Any]:
        flag = self._pause_flags.get(document_id)
        if flag:
            flag.clear()
        with self._lock:
            self._user_stopped.discard(document_id)
        return self.start(document_id)

    def resume_pending(self, limit: int = MAX_AUTO_RESUME) -> list[str]:
        """扫描当前工作空间内仍有待识别页的文档并自动续跑（断点续跑）。

        解析能力属于系统级依赖：Core 重启、切换工作空间、进程内状态丢失后，
        待识别页必须能自动恢复，不能依赖渲染进程是否恰好打开了文档列表。
        续跑由独立调度线程**逐份**推进（每份文档自带并行 Worker，同时起多份
        会成倍放大引擎内存），`start` 立即返回，不阻塞 RPC。
        """
        self._start_watchdog()
        self._settle_orphans()
        document_ids = self._pending_ids(limit=limit)
        if not document_ids:
            return []
        with self._lock:
            t = self._resume_thread
            if t is not None and t.is_alive():
                return []  # 已有续跑调度在推进：不重复登记
            stop = threading.Event()
            self._resume_stop = stop
            self._resume_thread = threading.Thread(
                target=self._run_resume_queue,
                args=(document_ids, stop),
                daemon=True,
                name="ocr-resume",
            )
            thread = self._resume_thread
        thread.start()
        return document_ids

    def _settle_orphans(self) -> None:
        """收口「无待识别页却停在解析中」的文档（系统级自愈）。

        导入中断、Core 崩溃、上次运行被杀都会把文档留在 parsing / ocr_queued
        等中间态；此时已没有 pending 页可续跑，前端只会一直显示「解析中」。
        这里按页级结果直接推进到 ready / partial / failed。
        """
        db = get_db()
        placeholders = ",".join("?" * len(PROMOTABLE_STATUSES))
        rows = db.query(
            f"""
            SELECT d.id FROM documents d
            JOIN cases c ON c.id = d.case_id
            WHERE c.status != 'archived'
              AND d.status IN ({placeholders})
              AND d.active_version_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM document_pages p
                  WHERE p.version_id = d.active_version_id
                    AND p.ocr_status IN ('pending', 'running')
              )
              AND EXISTS (SELECT 1 FROM document_pages p WHERE p.version_id = d.active_version_id)
            """,
            PROMOTABLE_STATUSES,
        )
        for row in rows:
            try:
                self._promote_document(row["id"])
            except Exception:  # noqa: BLE001  # 单份文档收口失败不影响其余
                pass

    def _pending_ids(self, limit: int) -> list[str]:
        """仍待识别的文档 id（按创建时间升序，最多 limit 份）。

        只取「页级仍有 pending 且文档未到终态」的文档：已 done/失败隔离完的文档
        不再重复进队，避免自愈调度空转。
        """
        db = get_db()
        rows = db.query(
            """
            SELECT d.id
            FROM documents d
            JOIN cases c ON c.id = d.case_id
            JOIN document_versions v ON v.id = d.active_version_id
            JOIN document_pages p ON p.version_id = v.id AND p.ocr_status = 'pending'
            WHERE c.status != 'archived'
              AND d.status IN ('pending', 'parsing', 'ocr_queued', 'ocr_running', 'indexing', 'partial')
            GROUP BY d.id
            ORDER BY d.created_at ASC
            LIMIT ?
            """,
            (max(1, limit + len(self._user_stopped)),),
        )
        # 用户显式暂停/取消/删除的文档不进自动队列（手动 resume/start 会解除标记）
        picked = [row["id"] for row in rows if row["id"] not in self._user_stopped]
        return picked[: max(1, limit)]

    def stop_and_wait(self, document_id: str, timeout: float = 15.0) -> bool:
        """停止该文档的识别并等待线程真正退出（删除文档/任务前必须调用）。

        `cancel()` 只置标志并杀掉 Worker；线程仍可能在跑收尾逻辑（落库、
        提升文档状态）。此时若调用方立刻删除案件/文档，页面行已消失而
        OCR 又写 `text_blocks`，就会触发外键失败并把消费者线程打崩，
        剩下未处理的页永远停在 pending —— 表现为「解析中」不再前进。
        返回 True 表示已确认无线程在写该文档。
        """
        self.cancel(document_id)
        t = self._threads.get(document_id)
        if t is None:
            return True
        t.join(timeout=timeout)
        if t.is_alive():
            print(
                f"[ocr] document {document_id}: thread still alive after cancel, "
                "deletion continues but page rows may be gone",
                file=sys.stderr,
            )
            return False
        return True

    def _run_resume_queue(self, document_ids: list[str], stop: threading.Event) -> None:
        """逐份续跑：一份文档跑完再起下一份；切换工作空间时由 stop 中断。

        一轮结束后继续补扫：库里待识别文档可能多于单轮上限，不补扫会让超出
        `limit` 的文档永久停在「解析中」。补扫只处理新出现的待识别文档，
        全部处理完即退出调度线程。
        """
        pending = list(document_ids)
        processed = set(document_ids)
        while pending and not stop.is_set():
            for document_id in pending:
                if stop.is_set():
                    return
                try:
                    self.start(document_id)
                except Exception as exc:  # noqa: BLE001  # 单份文档失败不影响其他文档
                    print(
                        f"[ocr] auto resume {document_id} failed: {type(exc).__name__}",
                        file=sys.stderr,
                    )
                    continue
                while not stop.wait(0.5):
                    thread = self._threads.get(document_id)
                    if thread is None or not thread.is_alive():
                        break
            if stop.is_set():
                return
            # 补扫：排除本轮已处理过的文档（它们要么已完成，要么已隔离为 failed）
            try:
                pending = [doc_id for doc_id in self._pending_ids(limit=MAX_AUTO_RESUME * 4)
                           if doc_id not in processed]
            except Exception:  # noqa: BLE001  # 库不可用（正在切换）：退出调度
                return
            processed.update(pending)
        print(f"[ocr] auto resume finished for {len(processed)} document(s)", file=sys.stderr)

    def cancel(self, document_id: str) -> dict[str, Any]:
        """取消：置标志位并**立即终止全部在跑的 Worker**，保证取消响应 ≤3 秒（O-007）。

        被中断页保持 pending，恢复后断点续跑；已 done 的页不受影响。
        """
        flag = self._cancel_flags.get(document_id)
        if flag:
            flag.set()
        with self._lock:
            workers = list(self._workers.get(document_id, []))
            # 用户主动取消/删除的文档：守护线程不得再自动接手
            self._user_stopped.add(document_id)
        for worker in workers:
            worker.terminate()
        return self.status(document_id)

    def status(self, document_id: str) -> dict[str, Any]:
        counts = self._count_pages(document_id)
        state = self._state.get(document_id, {}).get("state", "idle")
        t = self._threads.get(document_id)
        if t is not None and t.is_alive():
            state = "running"
        elif state == "running":
            # 线程异常退出后内存状态可能仍停在 running：按页级进度降级为可续跑状态，
            # 否则前端会永远显示「解析中」且不再重试
            if counts["pending"] > 0:
                state = "idle"
            else:
                state = "failed" if counts["failed"] > 0 else "done"
        return self._snapshot(document_id, state, counts)

    def shutdown(self, timeout: float = 15.0) -> None:
        """stdin 关闭时调用：通知全部任务停止并等待线程退出。

        页级事务原子落库，被中断的页保持 pending，下次启动断点续跑。
        """
        self.suspend(timeout=timeout)

    def suspend(self, timeout: float = 15.0) -> bool:
        """停止全部 OCR 任务（切换工作空间 / 退出用），返回是否全部退出。

        切换工作空间必须先确保没有线程还在写旧库；返回 False 时调用方
        必须中断切换，不能关闭数据库连接。
        """
        with self._lock:
            self._suspended = True
            for flag in self._cancel_flags.values():
                flag.set()
            threads = list(self._threads.values())
            workers = [w for group in self._workers.values() for w in group]
            # 自动续跑调度必须一并停止：否则切到新工作空间后，旧队列仍会继续
            # 往旧库登记任务，产生跨工作空间的错乱
            if self._resume_stop is not None:
                self._resume_stop.set()
            resume_thread = self._resume_thread
            # 守护线程同理：它每 3 秒扫一次库，必须在新工作空间就绪前停下
            if self._watchdog_stop is not None:
                self._watchdog_stop.set()
            watchdog_thread = self._watchdog_thread
        # 先终止在跑的解析子进程，线程才能在超时内退出（页级事务原子落库，可断点续跑）
        for worker in workers:
            try:
                worker.terminate()
            except Exception:
                pass
        alive = False
        for t in threads:
            t.join(timeout=max(0.1, timeout / max(1, len(threads))))
            alive = alive or t.is_alive()
        if resume_thread is not None and resume_thread.is_alive():
            resume_thread.join(timeout=max(0.1, timeout / 4))
            alive = alive or resume_thread.is_alive()
        if watchdog_thread is not None and watchdog_thread.is_alive():
            watchdog_thread.join(timeout=max(0.1, timeout / 4))
            alive = alive or watchdog_thread.is_alive()
        if alive:
            # 收尾失败：调用方会中断切换并继续使用旧库，必须解除闸门，否则
            # 后续 ocr.start 会被永久拒绝
            with self._lock:
                self._suspended = False
                # 守护线程已收到 stop 信号：切换并没有真正发生，旧工作空间的
                # 待识别页仍需有人接手。清掉旧引用（旧线程会自行退出）后重新拉起，
                # 否则用户继续用原工作空间时文档会永久停在「解析中」。
                self._watchdog_stop = None
                self._watchdog_thread = None
                self._resume_stop = None
                self._resume_thread = None
            self._start_watchdog()
            return False
        with self._lock:
            self._threads.clear()
            self._cancel_flags.clear()
            self._pause_flags.clear()
            self._state.clear()
            self._workers.clear()
            # 暂停/取消意图与进程内状态同寿命：切换工作空间后文档仍待识别，
            # 必须允许系统级守护接手（暂停状态本身不跨进程持久化）
            self._user_stopped.clear()
            self._progress.clear()
            self._resume_thread = None
            self._resume_stop = None
            self._watchdog_thread = None
            self._watchdog_stop = None
        return True

    def resume_after_switch(self) -> None:
        """工作空间切换完成（或重新打开）后解除暂停闸门并启动系统级守护。"""
        with self._lock:
            self._suspended = False
        self._start_watchdog()

    # ---- 系统级守护（不依赖渲染进程） ---------------------------------------

    def _start_watchdog(self) -> None:
        """启动守护线程：周期性扫描库中待识别页并自动接手。

        解析属于系统级能力：前端窗口是否打开、是否轮询、是否刚切换/新建了
        工作空间，都不应影响文档能否被识别。此前 OCR 完全依赖渲染进程调用
        `ocr.start`，一旦轮询没跑到（窗口重载、切空间后列表未刷新、并发
        start 被拦截），文档就会永久停在「解析中 0/N」。
        """
        with self._lock:
            t = self._watchdog_thread
            if t is not None and t.is_alive():
                return
            stop = threading.Event()
            self._watchdog_stop = stop
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, args=(stop,), daemon=True, name="ocr-watchdog"
            )
            thread = self._watchdog_thread
        thread.start()

    def _watchdog_loop(self, stop: threading.Event) -> None:
        while not stop.wait(WATCHDOG_INTERVAL_S):
            if stop.is_set():
                return
            try:
                self._watchdog_tick()
            except Exception as exc:  # noqa: BLE001  # 库不可用/切换中：下轮再试
                print(f"[ocr] watchdog tick failed: {type(exc).__name__}", file=sys.stderr)

    def _prune_stopped(self) -> None:
        """清理已不存在文档的暂停标记，避免集合随删除操作无限增长。"""
        with self._lock:
            if not self._user_stopped:
                return
            candidates = list(self._user_stopped)
        db = get_db()
        alive: set[str] = set()
        for start in range(0, len(candidates), 400):
            chunk = candidates[start:start + 400]
            rows = db.query(
                f"SELECT id FROM documents WHERE id IN ({','.join('?' * len(chunk))})",
                tuple(chunk),
            )
            alive.update(row["id"] for row in rows)
        with self._lock:
            self._user_stopped &= alive

    def _watchdog_tick(self) -> None:
        """一次守护扫描：收口孤儿状态、重启卡死任务，并把待识别文档交给续跑调度。"""
        with self._lock:
            if self._suspended:
                return
        self._prune_stopped()
        self._restart_stalled()
        self._settle_orphans()
        document_ids = self._pending_ids(limit=MAX_AUTO_RESUME * 4)
        if not document_ids:
            return
        # 已在跑的文档不再登记；_resume_queue 自身会逐份推进
        with self._lock:
            running = {
                doc_id for doc_id, t in self._threads.items() if t is not None and t.is_alive()
            }
            resume_alive = self._resume_thread is not None and self._resume_thread.is_alive()
            if resume_alive:
                return
            todo = [
                doc_id
                for doc_id in document_ids
                if doc_id not in running and doc_id not in self._user_stopped
            ]
            if not todo:
                return
            stop = threading.Event()
            self._resume_stop = stop
            self._resume_thread = threading.Thread(
                target=self._run_resume_queue, args=(todo, stop), daemon=True, name="ocr-resume"
            )
            thread = self._resume_thread
        print(f"[ocr] watchdog picked up {len(todo)} document(s)", file=sys.stderr)
        thread.start()

    def _restart_stalled(self) -> None:
        """重启「线程还活着但一页都没再完成」的识别任务（系统级自愈）。

        `_threads` 里线程存活不足以证明进度在推进：消费者可能阻塞在永不返回的
        识别调用上、生产者可能卡在队列上，此时线程永远 alive、页永远是 pending，
        前端就一直显示「解析中 0/N」。单页超时 180 秒已经覆盖正常慢页，因此
        「连续 OCR_STALL_RESTART_S 内没有任何一页完成」必然是链路故障。

        重启有上限（OCR_STALL_RESTART_MAX）：多次重启仍毫无进展时，把剩余页
        显式收口为 failed —— 宁可让用户看到「解析失败」并可重试，也不能让它
        无限期停在「解析中」假装还在工作。
        """
        now = time.monotonic()
        restart: list[str] = []
        give_up: list[str] = []
        with self._lock:
            running = {
                doc_id for doc_id, t in self._threads.items() if t is not None and t.is_alive()
            }
            for doc_id in list(self._progress):
                if doc_id not in running:
                    self._progress.pop(doc_id, None)
            candidates = [d for d in running if d not in self._user_stopped]
        for document_id in candidates:
            try:
                counts = self._count_pages(document_id)
            except Exception:  # noqa: BLE001  # 库不可用（切换中）：跳过本轮
                continue
            if counts["pending"] == 0:
                # 已无待识别页：不属于卡死（线程只是在收尾），不参与停滞判定
                with self._lock:
                    self._progress.pop(document_id, None)
                continue
            done = counts["done"]
            with self._lock:
                prev = self._progress.get(document_id)
                if prev is None or prev[0] != done:
                    self._progress[document_id] = (done, now, prev[2] if prev else 0)
                    continue
                since, tries = prev[1], prev[2]
                if now - since < OCR_STALL_RESTART_S:
                    continue
                self._progress[document_id] = (done, now, tries + 1)
            if tries >= OCR_STALL_RESTART_MAX:
                give_up.append(document_id)
            else:
                restart.append(document_id)
        for document_id in restart:
            print(
                f"[ocr] document {document_id}: no page completed in "
                f"{int(OCR_STALL_RESTART_S)}s, restarting task",
                file=sys.stderr,
            )
            self._restart_document(document_id)
        for document_id in give_up:
            print(
                f"[ocr] document {document_id}: still no progress after "
                f"{OCR_STALL_RESTART_MAX} restarts, isolating remaining pages",
                file=sys.stderr,
            )
            try:
                self.cancel(document_id)
                self._isolate_pending(document_id)
                self._promote_document(document_id)
            except Exception:  # noqa: BLE001  # 下轮再试
                pass

    def _restart_document(self, document_id: str) -> None:
        """取消并等待旧线程退出后重新登记识别任务。"""
        self.cancel(document_id)
        t = self._threads.get(document_id)
        if t is not None:
            t.join(timeout=30.0)
            if t.is_alive():
                # 旧线程卡在不可中断的调用上：不并发起第二个（会双写同一份页），
                # 留到下个停滞窗口继续尝试，达到上限后走显式失败收口。
                # `cancel()` 会把文档记入「用户已停止」，但这次是系统自愈而非
                # 用户意图，必须撤销该标记，否则守护线程从此不再接手它。
                with self._lock:
                    self._user_stopped.discard(document_id)
                return
        with self._lock:
            self._user_stopped.discard(document_id)
        try:
            self.start(document_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[ocr] restart {document_id} failed: {type(exc).__name__}", file=sys.stderr)

    # ---- 内部 ---------------------------------------------------------------

    def _count_pages(self, document_id: str) -> dict[str, int]:
        db = get_db()
        rows = db.query(
            """
            SELECT ocr_status, COUNT(*) AS n FROM document_pages p
            JOIN document_versions v ON v.id = p.version_id
            JOIN documents d ON d.active_version_id = v.id
            WHERE d.id = ? GROUP BY ocr_status
            """,
            (document_id,),
        )
        by = {r["ocr_status"]: r["n"] for r in rows}
        total = sum(by.values())
        done = by.get("done", 0)
        failed = by.get("failed", 0)
        pending = by.get("pending", 0)
        return {"total": total, "done": done, "failed": failed, "pending": pending}

    def _snapshot(self, document_id: str, state: str, counts: dict[str, int]) -> dict[str, Any]:
        snap = {
            "documentId": document_id,
            "state": state,
            "total": counts["total"],
            "done": counts["done"],
            "failed": counts["failed"],
            "pending": counts["pending"],
            "currentPage": self._state.get(document_id, {}).get("currentPage"),
            "lowConfPages": self._state.get(document_id, {}).get("lowConfPages", 0),
        }
        with self._lock:
            self._state[document_id] = {**snap, "state": state}
        return snap

    def _run_worker(self, document_id: str) -> None:
        """线程入口：编排线程自身异常也必须落到明确终态。

        否则内存状态会停在 `running`，页面保持 pending，前端表现为「永久解析中」。
        """
        try:
            self._worker(document_id)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[ocr] document {document_id}: orchestrator crashed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            try:
                self._isolate_pending(document_id)
                counts = self._count_pages(document_id)
                self._snapshot(document_id, "failed" if counts["pending"] == 0 else "idle", counts)
                self._promote_document(document_id)
            except Exception:  # noqa: BLE001  # 数据库不可用时不再兜底（进程即将退出）
                pass

    def _worker(self, document_id: str) -> None:
        """任务编排线程：生产者预渲染 + N 个消费者并行识别（05-OCR迁移方案 §3.4）。

        语义与单 Worker 串行版一致：取消（≤3 秒、被中断页保持 pending）、暂停
        （不再取新页、在途页完成后停止）、断点续跑、失败页隔离、Worker 重启
        超限后剩余 pending 页全部隔离；任务结束统一清理临时明文与 PDF 源。
        """
        cancel = self._cancel_flags[document_id]
        pause = self._pause_flags[document_id]
        case_id: list[str | None] = [None]  # 生产者写入、finally 读取（列表闭包传递）
        root = _workspace_root()
        n_workers = _worker_count()
        # 单 Worker 用引擎默认（cpu//2）；多 Worker 按 Worker 数均分配避免 OMP 超订
        cpu_threads: int | None = (
            None if n_workers == 1 else max(2, (os.cpu_count() or 8) // n_workers)
        )
        task_q: "queue.Queue[tuple[dict[str, Any], Path | None, Path | None] | None]" = queue.Queue(
            maxsize=n_workers * 2 + 2
        )
        feed_done = threading.Event()  # 生产者结束（含哨兵投放完成）
        disabled = threading.Event()  # Worker 重启超限：剩余 pending 页全部隔离
        low_conf_lock = threading.Lock()
        counters = {"lowConf": self._state.get(document_id, {}).get("lowConfPages", 0)}
        # 存活消费者计数：消费者若因未捕获异常整体退出，生产者会永远阻塞在满队列
        # 的 put 上（join 不返回、线程一直 alive），前端就永久停在「解析中」。
        alive = {"n": n_workers}
        alive_lock = threading.Lock()

        def _consumer_dead() -> None:
            with alive_lock:
                alive["n"] -= 1

        def _mark_failed(page_id: str) -> None:
            try:
                db = get_db()
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE document_pages SET ocr_status = 'failed' WHERE id = ?",
                        (page_id,),
                    )
            except Exception as exc:  # noqa: BLE001  # 单页落库失败不能拖垮整个消费者线程
                print(f"[ocr] mark page failed error: {type(exc).__name__}", file=sys.stderr)

        def _register(worker: _OcrWorker) -> None:
            with self._lock:
                self._workers.setdefault(document_id, []).append(worker)

        def _unregister(worker: _OcrWorker) -> None:
            with self._lock:
                lst = self._workers.get(document_id, [])
                if worker in lst:
                    lst.remove(worker)

        def _producer() -> None:
            db = get_db()
            try:
                pages = db.query(
                    """
                    SELECT p.id AS page_id, p.page_no, d.id AS document_id, d.case_id, d.title,
                           d.kind, COALESCE(v.unlocked_blob_uri, v.blob_uri) AS blob_uri, v.id AS version_id
                    FROM document_pages p
                    JOIN document_versions v ON v.id = p.version_id
                    JOIN documents d ON d.active_version_id = v.id
                    WHERE d.id = ? AND p.ocr_status = 'pending'
                    ORDER BY p.page_no ASC
                    """,
                    (document_id,),
                )
                for page in pages:
                    if cancel.is_set() or pause.is_set():
                        return
                    with self._lock:
                        st = self._state.get(document_id, {})
                        st["currentPage"] = page["page_no"]
                        st["lowConfPages"] = counters["lowConf"]
                        self._state[document_id] = st
                    case_id[0] = page["case_id"]
                    image, tmp_base = _page_image(root, page)
                    if image is None:
                        # 页面无法渲染为图片 → 隔离该页（FR-005）
                        _release_page_temp(root, image, tmp_base)
                        _mark_failed(page["page_id"])
                        continue
                    item: tuple[dict[str, Any], Path | None, Path | None] | None = (page, image, tmp_base)
                    while True:
                        if cancel.is_set() or pause.is_set():
                            _release_page_temp(root, image, tmp_base)
                            return
                        if disabled.is_set():
                            # Worker 全线不可用：本页与其余 pending 页统一隔离
                            _release_page_temp(root, image, tmp_base)
                            _mark_failed(page["page_id"])
                            return
                        if alive["n"] <= 0:
                            # 消费者全部异常退出：队列永远无人消费，继续 put 会无限
                            # 阻塞（join 不返回、线程恒 alive → 前端永久「解析中」）
                            _release_page_temp(root, image, tmp_base)
                            _mark_failed(page["page_id"])
                            return
                        try:
                            task_q.put(item, timeout=0.5)  # type: ignore[arg-type]
                            break
                        except queue.Full:
                            continue
            finally:
                feed_done.set()
                for _ in range(n_workers):
                    try:
                        task_q.put(None, timeout=5)
                    except queue.Full:  # noqa: SIM105  # 防御：消费者已全部退出时哨兵投不进
                        pass

        def _consumer() -> None:
            restarts = 0
            worker: _OcrWorker | None = None
            try:
                while True:
                    if cancel.is_set() or pause.is_set() or disabled.is_set():
                        return
                    try:
                        task = task_q.get(timeout=0.2)
                    except queue.Empty:
                        if feed_done.is_set() and task_q.empty():
                            return
                        continue
                    if task is None:
                        return
                    page, image, tmp_base = task
                    result: dict[str, Any] | None = None
                    while result is None:
                        try:
                            if worker is None:
                                worker = _OcrWorker(cpu_threads=cpu_threads)
                                _register(worker)
                            result = worker.ocr(str(image), page["kind"])
                        except OcrPageError:
                            # 单页失败隔离（FR-005）：本页标 failed，Worker 保留，继续后续页
                            _mark_failed(page["page_id"])
                            break
                        except OcrTaskError:
                            # Worker 崩溃/超时/被取消终止：重启（最多 2 次）后重试当前页
                            if worker is not None:
                                worker.close()
                                _unregister(worker)
                                worker = None
                            if cancel.is_set():
                                break  # 取消优先：该页保持 pending，本线程退出
                            restarts += 1
                            if restarts > WORKER_RESTART_MAX:
                                disabled.set()
                                _mark_failed(page["page_id"])
                                return
                    if result is not None:
                        avg_conf = result["avgConfidence"]
                        if avg_conf < LOW_CONFIDENCE:
                            with low_conf_lock:
                                counters["lowConf"] += 1
                        try:
                            self._persist_page(page, result, avg_conf)
                        except Exception as exc:  # noqa: BLE001
                            # 页/版本可能已被删除、库瞬断等：隔离该页并继续，避免消费者线程
                            # 静默退出导致整份文档永远停在「解析中」（实际发生过 FK 失败）
                            print(
                                f"[ocr] document {document_id} page {page['page_no']} "
                                f"persist failed: {type(exc).__name__}",
                                file=sys.stderr,
                            )
                            _mark_failed(page["page_id"])
                            _release_page_temp(root, image, tmp_base)
                            continue
                        with self._lock:
                            st = self._state.get(document_id, {})
                            st["lowConfPages"] = counters["lowConf"]
                            self._state[document_id] = st
                    # 单页处理完毕即清理临时明文（REQ-DOC-012）
                    _release_page_temp(root, image, tmp_base)
            finally:
                _consumer_dead()
                if worker is not None:
                    worker.close()
                    _unregister(worker)

        producer = threading.Thread(target=_producer, daemon=True, name=f"ocr-feed-{document_id[:8]}")
        consumers = [
            threading.Thread(target=_consumer, daemon=True, name=f"ocr-run-{document_id[:8]}-{i}")
            for i in range(n_workers)
        ]
        producer.start()
        for t in consumers:
            t.start()
        producer.join()
        for t in consumers:
            t.join()

        # 排空未消费任务（暂停/取消/禁用留下的已渲染页），清理其临时明文
        while True:
            try:
                leftover = task_q.get_nowait()
            except queue.Empty:
                break
            if leftover is not None:
                page, image, tmp_base = leftover
                _release_page_temp(root, image, tmp_base)

        try:
            if disabled.is_set():
                # Worker 全线不可用：剩余 pending 页统一隔离（对齐串行版 worker_disabled）
                self._isolate_pending(document_id)

            counts = self._count_pages(document_id)
            if cancel.is_set():
                final = "cancelled"
            elif pause.is_set():
                final = "paused"
            elif disabled.is_set() or counts["pending"] > 0 or counts["failed"] > 0:
                # 仍有 pending 页（异常退出/线程崩溃）或失败页：必须显式落到失败终态，
                # 不能报 done；同时把剩余 pending 页隔离，避免前端永远停在「解析中」
                if counts["pending"] > 0:
                    self._isolate_pending(document_id)
                    counts = self._count_pages(document_id)
                final = "failed"
            else:
                final = "done"
            self._snapshot(document_id, final, counts)
            if final in ("done", "failed"):
                self._promote_document(document_id)
        finally:
            with self._lock:
                self._workers.pop(document_id, None)
            # 释放该文档持有的 PDF 源（含关闭引擎句柄，Windows 下才能删除）
            _release_pdf_hold(root, document_id)
            # 任务结束（完成 / 暂停 / 取消 / 失败 / 异常退出）清理案件临时明文
            if case_id[0]:
                tempstore.purge_case_temp(root, case_id[0])

    def _isolate_pending(self, document_id: str) -> None:
        """把该文档仍未处理的 pending 页标记为 failed（页级隔离，FR-005）。

        仅用于异常/Worker 不可用等无法继续的终态；取消与暂停保留 pending 以便断点续跑。
        """
        db = get_db()
        rows = db.query(
            """
            SELECT p.id AS page_id FROM document_pages p
            JOIN document_versions v ON v.id = p.version_id
            JOIN documents d ON d.active_version_id = v.id
            WHERE d.id = ? AND p.ocr_status = 'pending'
            """,
            (document_id,),
        )
        with db.transaction() as conn:
            for r in rows:
                conn.execute(
                    "UPDATE document_pages SET ocr_status = 'failed' WHERE id = ?",
                    (r["page_id"],),
                )

    def _persist_page(self, page: dict[str, Any], result: dict[str, Any], avg_conf: float) -> None:
        from ..search import index_page

        db = get_db()
        full_text = result["fullText"]
        with db.transaction() as conn:
            conn.execute("DELETE FROM text_blocks WHERE page_id = ?", (page["page_id"],))
            for order, line in enumerate(result["lines"]):
                conn.execute(
                    """
                    INSERT INTO text_blocks(id, page_id, block_order, type, bbox_json, text_enc, confidence, block_hash)
                    VALUES (?, ?, ?, 'ocr', ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        page["page_id"],
                        order,
                        json.dumps(line["bbox"]) if line["bbox"] else None,
                        line["text"],
                        line["confidence"],
                        hashlib.sha256(line["text"].encode("utf-8")).hexdigest(),
                    ),
                )
            conn.execute(
                """
                UPDATE document_pages
                SET text_content = ?, text_quality = 'ocr', ocr_status = 'done', ocr_confidence = ?
                WHERE id = ?
                """,
                (full_text, avg_conf, page["page_id"]),
            )
            # FTS 同步：删旧插新（空文本页此前无 FTS 行，DELETE 天然无害）
            row = conn.execute(
                "SELECT rowid FROM pages_fts WHERE document_id = ? AND page_no = ?",
                (page["document_id"], page["page_no"]),
            ).fetchone()
            if row is not None:
                conn.execute("DELETE FROM pages_fts WHERE rowid = ?", (row["rowid"],))
            if full_text:
                index_page(
                    conn,
                    text=full_text,
                    case_id=page["case_id"],
                    document_id=page["document_id"],
                    document_title=page["title"],
                    version_id=page["version_id"],
                    page_no=page["page_no"],
                    text_quality="ocr",
                )
            # 同事务内升级文档状态（消除竞态窗口）：并行管线下最后一批页与任务
            # 收尾之间有时间窗，轮询可能抓到「无 pending 页但 status=partial」的
            # 中间态（UI 显示「部分可读」且不再满足轮询条件而卡住）。
            # 存在失败页时不得升级为 ready，否则用户会把残缺文本当作完整原文。
            conn.execute(
                """
                UPDATE documents SET status = 'ready', updated_at = ?
                WHERE id = ? AND status = 'partial' AND NOT EXISTS (
                    SELECT 1 FROM document_pages
                    WHERE version_id = ? AND ocr_status IN ('pending', 'running', 'failed')
                )
                """,
                (now_iso(), page["document_id"], page["version_id"]),
            )

    def _promote_document(self, document_id: str) -> None:
        """按页级结果收口文档状态（PRD 7.3）：全部页成功才 ready。

        有失败页时保持/标记 partial（部分可读）；全部页失败时标记 failed，
        避免把无文本的文档显示为「已就绪」。
        """
        db = get_db()
        counts = db.query_one(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN p.ocr_status IN ('pending','running') THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN p.ocr_status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM document_pages p
            JOIN document_versions v ON v.id = p.version_id
            JOIN documents d ON d.active_version_id = v.id
            WHERE d.id = ?
            """,
            (document_id,),
        )
        if counts is None:
            return
        pending = int(counts["pending"] or 0)
        failed = int(counts["failed"] or 0)
        total = int(counts["total"] or 0)
        if pending > 0:
            return
        target = "ready" if failed == 0 else ("failed" if failed >= total else "partial")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET status = ?, updated_at = ? "
                f"WHERE id = ? AND status IN ({','.join('?' * len(PROMOTABLE_STATUSES))})",
                (target, now_iso(), document_id, *PROMOTABLE_STATUSES),
            )


def _workspace_root() -> Path:
    db = get_db()
    ws = db.query_one("SELECT root_uri FROM workspaces ORDER BY created_at LIMIT 1")
    if ws is None:
        raise OcrTaskError("workspace not initialized")
    return Path(ws["root_uri"])


def _page_image(root: Path, page: dict[str, Any]) -> tuple[Path | None, Path | None]:
    """把待 OCR 页解析为可识别图片路径。

    内容存储为加密格式，先解密到案件专属临时目录（REQ-DOC-012）；tiff 按帧拆分为
    临时 PNG；pdf 经 pypdfium2 渲染当前页为临时 PNG（05-OCR迁移方案 §3.2，
    PDF 源由 _pdf_hold 跨页持有、任务结束释放，基路径返回 None）。
    其余类型返回 (可识别图片路径, 已物料化的临时明文基路径)；基路径为 None
    表示读取的是历史明文 blob（未产生副本）。临时明文由调用方用后 `tempstore.release` 清理。
    """
    kind = page["kind"]
    if kind == "pdf":
        # 扫描 PDF 页渲染（05-OCR迁移方案 §3.2）：PDF 源跨页复用（base 由
        # _pdf_hold 持有、任务结束释放），页位图落案件临时目录、单页用后即清理
        try:
            blob = _pdf_source(root, page)
            png = tempstore.case_temp_dir(root, page["case_id"]) / f"{uuid.uuid4().hex}.png"
            return render_pdf_page(blob, page["page_no"], out_path=png), None
        except Exception:
            return None, None
    try:
        blob = tempstore.materialize(root, page["blob_uri"], page["case_id"], kind)
    except tempstore.TempStoreError:
        return None, None
    base = blob if tempstore.temp_root(root) in blob.parents else None
    if kind in ("png", "jpg"):
        return blob, base
    if kind == "tiff":
        frames = iter_page_images(blob)
        idx = page["page_no"] - 1
        return (frames[idx] if 0 <= idx < len(frames) else None), base
    return None, base


def _release_page_temp(root: Path, image: Path | None, base: Path | None) -> None:
    """清理单页 OCR 产生的临时明文（解密副本与拆帧 PNG）。"""
    if base is not None:
        tempstore.release(root, base)
    if image is not None and image != base:
        tempstore.release(root, image)
