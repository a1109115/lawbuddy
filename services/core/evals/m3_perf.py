# -*- coding: utf-8 -*-
"""M3 评测器：性能与稳定性（03 清单 §13 P-001~P-010 的离线可测部分）。

用法（services/core 目录下）：
    python -m evals.m3_perf [--keep]

覆盖：
1. P-001 冷启动：Core 子进程 → 握手 → 工作空间就绪，多次测量取 P50/P95；
2. P-002 500 页 PDF 首屏：导入耗时、首页可读延迟 ≤2s，且扫描页不阻塞在 OCR 上；
3. P-003 已索引引用跳转：定位到目标页 P95 ≤1s；
4. P-006 2000 页卷宗内存：导入后重复检索，Python 堆无持续增长；
5. P-007 长稳（缩短版）：混合负载持续约 45 秒，成功率 100%、无崩溃、完整性通过；
6. P-008 并发：OCR 管线 + 导出 + Agent 任务并发时控制面 RPC 仍可用；
7. P-009 磁盘空间：剩余空间不足时提前预警，失败写入后数据库完整。

Agent 任务（P-007/P-008）以 mock 网关驱动（自主 Loop + 脚本化最终答复，不联网），
其余负载与断言全部走真实实现。

未覆盖（见报告 gaps）：P-004（OCR 真实吞吐，需引擎与基准集）、P-005（UI 主线程，需 Electron/浏览器）、
P-010（流式输出，需模型端点）、P-001/P-006/P-007 的推荐设备与 8 小时口径（需 RC 阶段基准机）。

报告：控制台摘要 + evals/reports/m3_perf_<ts>.json。
门槛：全部 gates 通过；数值仅代表本机（开发机）基线，发布前须在基准设备复测。
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import tracemalloc
from datetime import datetime
from pathlib import Path
from typing import Any

# 门槛（03 清单 §13）
COLD_START_P95_S = 6.0          # 低配口径上限（推荐设备口径 3.0 单独报告，P-001）
RECOMMENDED_COLD_START_S = 3.0
FIRST_SCREEN_P95_S = 2.0        # P-002
CITATION_JUMP_P95_S = 1.0       # P-003
CONTROL_PLANE_S = 1.0           # P-008 并发下的控制面响应
MEMORY_GROWTH_LIMIT = 0.10      # P-006 堆增幅上限
SOAK_SECONDS = 45.0             # P-007 缩短版长稳时长（完整版 8 小时在 RC 执行）

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_DIR = REPO_ROOT / "services/core"


def _seg(report: dict[str, Any], name: str, fn, *args) -> bool:
    try:
        rows, ok = fn(report, *args)
        entry = report.get(name) or {}
        entry.update({"ok": bool(ok), "rows": rows})
        report[name] = entry
        return bool(ok)
    except Exception:  # noqa: BLE001
        report[name] = {"ok": False, "rows": [], "error": traceback.format_exc(limit=8)}
        return False


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(0.95 * len(ordered))) - 1))
    return ordered[idx]


def _workspace_root() -> Path:
    from lawbuddy.storage import get_db

    row = get_db().query_one("SELECT root_uri FROM workspaces ORDER BY created_at LIMIT 1")
    if row is None:
        raise RuntimeError("workspace not initialized")
    return Path(row["root_uri"])


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _write_pdf(path: Path, pages: int, *, text_per_page: bool = True) -> Path:
    """生成多页 PDF：text_per_page=False 时无文本层（模拟扫描件，页应转 OCR 待处理）。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    for i in range(1, pages + 1):
        if text_per_page:
            c.drawString(60, 780, f"page {i} marker-{i:05d} 案件材料正文")
        else:
            c.rect(60, 600, 400, 200, stroke=1, fill=0)  # 纯图形页（无文本层）
        c.showPage()
    c.save()
    return path


# ---------------------------------------------------------------------------
# 段 1：冷启动（P-001）
# ---------------------------------------------------------------------------

def _run_cold_start(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    samples: list[dict[str, float]] = []
    for i in range(3):
        ws = tmp / f"cold-{i}"
        t0 = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-m", "lawbuddy"],
            cwd=str(CORE_DIR), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        )
        try:
            token = f"perf-{i}"
            assert proc.stdin and proc.stdout
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "core.handshake",
                                         "params": {"token": token}}) + "\n")
            proc.stdin.flush()
            json.loads(proc.stdout.readline())
            t_handshake = time.monotonic() - t0
            proc.stdin.write(json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "workspace.ensure",
                "params": {"_token": token, "rootUri": str(ws)},
            }) + "\n")
            proc.stdin.flush()
            json.loads(proc.stdout.readline())
            t_ready = time.monotonic() - t0
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "shutdown",
                                         "params": {"_token": token}}) + "\n")
            proc.stdin.flush()
        finally:
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        samples.append({"handshake": t_handshake, "ready": t_ready})

    ready = [s["ready"] for s in samples]
    req = [s["handshake"] for s in samples]
    p95_ready = _p95(ready)
    rows.append({"step": "coldStartMeasurement", "ok": p95_ready <= COLD_START_P95_S,
                 "handshakeSec": [round(v, 2) for v in req],
                 "readySec": [round(v, 2) for v in ready],
                 "p50Sec": round(statistics.median(ready), 2), "p95Sec": round(p95_ready, 2),
                 "lowSpecLimit": COLD_START_P95_S})
    rows.append({"step": "coldStartRecommendedDevice",
                 "ok": p95_ready <= RECOMMENDED_COLD_START_S,
                 "limitSec": RECOMMENDED_COLD_START_S, "actualSec": round(p95_ready, 2),
                 "note": "推荐设备口径（≤3s）在基准机复测"})

    report["coldStart"] = {"p95Sec": round(p95_ready, 2), "samples": len(samples)}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 2：500 页首屏与引用跳转（P-002 / P-003）
# ---------------------------------------------------------------------------

def _run_first_screen(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.storage import get_db

    db = get_db()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-首屏", case_type="civil", client_name="甲", client_role="原告代理人")
    cid = case["id"]
    fixtures = tmp / "perf"
    fixtures.mkdir(parents=True, exist_ok=True)

    # 500 页带文本层 PDF：导入 + 首页可读延迟（P-002）
    big = _write_pdf(fixtures / "500页文本卷宗.pdf", 500)
    t0 = time.monotonic()
    outcome = import_documents(cid, [str(big)])[0]
    import_sec = time.monotonic() - t0
    doc_id = outcome.get("documentId")
    page_counts = int(db.query_one(
        "SELECT COUNT(*) AS n FROM document_pages p JOIN documents d ON d.active_version_id = p.version_id "
        "WHERE d.id = ?", (doc_id,))["n"]) if doc_id else 0

    from lawbuddy.documents import get_pages

    first_latencies: list[float] = []
    for _ in range(10):
        t0 = time.monotonic()
        pages = get_pages(doc_id)
        first_latencies.append(time.monotonic() - t0)
    rows.append({"step": "firstScreen500Pages", "ok": page_counts == 500 and _p95(first_latencies) <= FIRST_SCREEN_P95_S,
                 "pages": page_counts, "importSec": round(import_sec, 2),
                 "firstPageP95Sec": round(_p95(first_latencies), 3), "limitSec": FIRST_SCREEN_P95_S})

    # 扫描件（无文本层）不阻塞在 OCR：导入即返回，页保持 pending（P-002 后半句）
    scan = _write_pdf(fixtures / "扫描件20页.pdf", 20, text_per_page=False)
    t0 = time.monotonic()
    scan_outcome = import_documents(cid, [str(scan)])[0]
    scan_sec = time.monotonic() - t0
    scan_id = scan_outcome.get("documentId")
    pending = int(db.query_one(
        "SELECT COUNT(*) AS n FROM document_pages p JOIN documents d ON d.active_version_id = p.version_id "
        "WHERE d.id = ? AND p.ocr_status = 'pending'", (scan_id,))["n"]) if scan_id else 0
    rows.append({"step": "scanNotBlockedByOcr", "ok": scan_sec < 5.0 and pending == 20,
                 "importSec": round(scan_sec, 2), "pendingPages": pending})

    # 引用跳转：定位第 450 页标记（已索引 → 搜索 + 取页）P95 ≤1s（P-003）
    from lawbuddy.search import search_pages

    hits = search_pages(cid, "marker-00450", limit=5)
    jump_latencies: list[float] = []
    for _ in range(40):
        t0 = time.monotonic()
        found = search_pages(cid, "marker-00450", limit=5)
        pages = get_pages(doc_id)
        target = next((p for p in pages if p["pageNo"] == 450), None)
        jump_latencies.append(time.monotonic() - t0)
    rows.append({"step": "citationJumpP95",
                 "ok": bool(hits) and len(hits) >= 1 and target is not None
                 and _p95(jump_latencies) <= CITATION_JUMP_P95_S,
                 "hits": len(hits), "targetPage": target["pageNo"] if target else None,
                 "p95Sec": round(_p95(jump_latencies), 3), "limitSec": CITATION_JUMP_P95_S})

    report["firstScreen"] = {"caseId": cid, "documentId": doc_id, "importSec": round(import_sec, 2),
                             "scanImportSec": round(scan_sec, 2)}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 3：2000 页卷宗内存（P-006）
# ---------------------------------------------------------------------------

def _run_memory(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.search import search_pages

    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-内存", case_type="civil", client_name="乙", client_role="原告代理人")
    fixtures = tmp / "perf"
    big = _write_pdf(fixtures / "2000页卷宗.pdf", 2000)
    t0 = time.monotonic()
    outcome = import_documents(case["id"], [str(big)])[0]
    import_sec = time.monotonic() - t0
    doc_id = outcome.get("documentId")
    rows.append({"step": "largeCorpusImported", "ok": doc_id is not None and import_sec < 120,
                 "importSec": round(import_sec, 2)})

    # 稳态内存：预热后按批次比较堆增量（第二批增幅须 <10%，即无持续增长）
    for _ in range(5):
        search_pages(case["id"], "marker-01000", limit=6)
    tracemalloc.start()
    for i in range(25):
        search_pages(case["id"], f"marker-0{1000 + i}", limit=6)
    c2, _peak2 = tracemalloc.get_traced_memory()
    for i in range(25, 50):
        search_pages(case["id"], f"marker-0{1000 + i}", limit=6)
    c3, peak3 = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    batch_growth_bytes = c3 - c2
    allowed = max(1 * 1024 * 1024, int(peak3 * MEMORY_GROWTH_LIMIT))  # 1MB 或峰值 10%，取大者
    rows.append({"step": "memoryNoContinuousGrowth", "ok": batch_growth_bytes < allowed,
                 "secondBatchGrowthMB": round(batch_growth_bytes / 1024 / 1024, 3),
                 "allowedMB": round(allowed / 1024 / 1024, 2),
                 "heapMB": round(c3 / 1024 / 1024, 2), "peakMB": round(peak3 / 1024 / 1024, 2),
                 "note": "tracemalloc 仅覆盖 Python 堆（SQLite/C 层分配不计入）"})

    report["memory"] = {"importSec": round(import_sec, 2),
                        "secondBatchGrowthMB": round(batch_growth_bytes / 1024 / 1024, 3)}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 4：并发（P-008）
# ---------------------------------------------------------------------------

class _StubWorker:
    """OCR 引擎桩（并发语义验证；真实吞吐属 P-004，需引擎与基准集）。"""

    def __init__(self, cpu_threads: int | None = None) -> None:
        pass

    def ocr(self, image_path: str, kind: str | None = None) -> dict[str, Any]:
        time.sleep(0.02)
        return {"fullText": "并发识别文本", "avgConfidence": 0.9,
                "lines": [{"text": "并发识别文本", "bbox": [0, 0, 5, 5], "confidence": 0.9}]}

    def close(self) -> None:
        pass

    def terminate(self) -> None:
        pass


def _run_concurrency(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    import lawbuddy.ocr.pipeline as pipeline
    from lawbuddy.agent.tasks import create_task
    from lawbuddy.cases import create_case, list_cases
    from lawbuddy.documents import import_documents
    from lawbuddy.search import search_pages

    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-并发", case_type="civil", client_name="丙", client_role="原告代理人")
    fixtures = tmp / "perf"

    # 准备 OCR 负载（多帧 TIFF）
    from PIL import Image

    frames = [Image.new("RGB", (60 + i, 40), (250, 250, 250 - i)) for i in range(20)]
    tiff = fixtures / "并发扫描件.tiff"
    frames[0].save(tiff, save_all=True, append_images=frames[1:], format="TIFF")
    doc_id = import_documents(case["id"], [str(tiff)])[0]["documentId"]

    orig_worker, orig_image = pipeline._OcrWorker, pipeline._page_image  # noqa: SLF001
    pipeline._OcrWorker = _StubWorker  # type: ignore[assignment]  # noqa: SLF001
    pipeline._page_image = lambda root, page: (tiff, None)  # type: ignore[assignment]  # noqa: SLF001
    errors: list[str] = []
    control_latencies: list[float] = []
    stop = threading.Event()
    snapshot: dict[str, Any] = {"total": 0, "done": 0, "failed": 0, "pending": -1}

    try:
        mgr = pipeline.OcrManager()
        mgr.start(doc_id)

        def exporter() -> None:
            from lawbuddy.artifacts import _body_to_blocks  # noqa: SLF001
            from lawbuddy.render import blocks_to_pdf

            try:
                for i in range(3):
                    blocks = _body_to_blocks("并发导出", {"report": {"overview": "并发导出压力样本" * 50}})
                    blocks_to_pdf(blocks, str(tmp / f"concurrent-{i}.pdf"))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"export:{type(exc).__name__}")

        def searcher() -> None:
            try:
                while not stop.is_set():
                    search_pages(case["id"], "并发", limit=3)
                    time.sleep(0.02)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"search:{type(exc).__name__}")

        def agent_task() -> None:
            try:
                from evals._loop_mock import ScriptStep, install_mock_gateway

                task = create_task(case_id=case["id"], mode="legal_review",
                                   request_text="并发压力下的本地分析")
                from lawbuddy.agent.runner import AgentRunner
                from lawbuddy.agent.tasks import get_task

                # 自主 Loop + mock 网关：Loop 在后台线程执行，mock 须保持安装到任务收束
                undo = install_mock_gateway([ScriptStep.final("并发压力下的本地分析结论。")])
                try:
                    AgentRunner.instance().start(task["id"])
                    agent_deadline = time.monotonic() + 30
                    while time.monotonic() < agent_deadline:
                        if get_task(task["id"])["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS",
                                                                "CANCELLED", "FAILED"):
                            break
                        time.sleep(0.05)
                finally:
                    undo()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"agent:{type(exc).__name__}")

        threads = [threading.Thread(target=exporter), threading.Thread(target=searcher),
                   threading.Thread(target=agent_task)]
        for t in threads:
            t.start()
        # 控制面（UI 轮询）在负载下仍须可用
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            list_cases()
            control_latencies.append(time.monotonic() - t0)
            time.sleep(0.05)
        stop.set()
        for t in threads:
            t.join(timeout=30)
        for _ in range(200):
            if mgr.status(doc_id)["pending"] == 0:
                break
            time.sleep(0.05)
        snapshot = mgr.status(doc_id)
    finally:
        pipeline._OcrWorker = orig_worker  # type: ignore[assignment]  # noqa: SLF001
        pipeline._page_image = orig_image  # type: ignore[assignment]  # noqa: SLF001

    rows.append({"step": "concurrentLoadsNoError", "ok": not errors, "errors": errors[:5]})
    rows.append({"step": "ocrCompletedUnderLoad", "ok": snapshot["pending"] == 0,
                 "snapshot": {k: snapshot[k] for k in ("total", "done", "failed")}})
    rows.append({"step": "controlPlaneResponsive", "ok": _p95(control_latencies) <= CONTROL_PLANE_S,
                 "p95Sec": round(_p95(control_latencies), 3), "samples": len(control_latencies),
                 "limitSec": CONTROL_PLANE_S})

    report["concurrency"] = {"samples": len(control_latencies), "errors": len(errors)}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 5：磁盘空间（P-009）
# ---------------------------------------------------------------------------

def _run_disk_space(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    import shutil as _shutil

    import lawbuddy.workspace as ws
    from lawbuddy.storage import get_db

    rows: list[dict[str, Any]] = []
    original = ws.shutil.disk_usage

    class _Usage:
        def __init__(self, free: int) -> None:
            self.total = 10 * 1024 ** 3
            self.used = self.total - free
            self.free = free

    warned = ""
    try:
        ws.shutil.disk_usage = lambda _p: _Usage(100 * 1024 ** 2)  # 仅剩 100MB
        try:
            ws.ensure_workspace(str(tmp / "low-space"))
        except ws.WorkspaceError as exc:
            warned = str(exc)
    finally:
        ws.shutil.disk_usage = original

    rows.append({"step": "lowDiskSpaceWarnsEarly", "ok": bool(warned), "message": warned[:60]})
    rows.append({"step": "diskUsageRestored", "ok": _shutil.disk_usage(str(tmp)).free > 0})

    # 失败写入后数据库完整（写满场景的等价断言：事务回滚 + integrity_check）
    db = get_db()
    before = int(db.query_one("SELECT COUNT(*) AS n FROM cases")["n"])
    try:
        with db.transaction() as conn:
            conn.execute("INSERT INTO cases(id, workspace_id, title, case_type, default_mode, status, created_at, updated_at) "
                         "SELECT 'perf-disk-full', workspace_id, '写满样本', 'civil', 'legal_review', 'active', "
                         "datetime('now'), datetime('now') FROM cases LIMIT 1")
            raise OSError("模拟磁盘写满")
    except OSError:
        pass
    after = int(db.query_one("SELECT COUNT(*) AS n FROM cases")["n"])
    integrity = db.query("PRAGMA integrity_check")
    rows.append({"step": "writeFullRollsBackAndIntact",
                 "ok": after == before and str(list(integrity[0].values())[0]).lower() == "ok",
                 "casesBefore": before, "casesAfter": after})

    report["diskSpace"] = {"warning": warned[:60]}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 6：缩短版长稳（P-007）
# ---------------------------------------------------------------------------

def _run_soak(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import create_task, get_events, get_task
    from lawbuddy.artifacts import _body_to_blocks  # noqa: SLF001
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.render import blocks_to_pdf
    from lawbuddy.search import search_pages
    from lawbuddy.storage import get_db

    db = get_db()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-长稳", case_type="civil", client_name="丁", client_role="原告代理人")
    fixtures = tmp / "perf"
    doc_id = import_documents(case["id"], [str(_write_pdf(fixtures / "长稳样本.pdf", 40))])[0]["documentId"]

    # Agent 任务走 mock 网关（自主 Loop + 脚本化最终答复）；检索/导出/落库全走真实实现。
    # 脚本步骤耗尽后 mock 兜底直接给最终答复，后续轮次仍正常收束。
    from evals._loop_mock import ScriptStep, install_mock_gateway

    cycles = 0
    failures: list[str] = []
    soak_tasks: list[str] = []
    deadline = time.monotonic() + SOAK_SECONDS
    undo = install_mock_gateway([ScriptStep.final("长稳轮次分析结论。")])
    try:
        while time.monotonic() < deadline:
            cycles += 1
            try:
                search_pages(case["id"], f"marker-{cycles % 40 + 1:05d}", limit=5)
                blocks = _body_to_blocks(f"长稳-{cycles}", {"report": {"overview": "长稳样本" * 20}})
                blocks_to_pdf(blocks, str(tmp / f"soak-{cycles % 3}.pdf"))
                task = create_task(case_id=case["id"], mode="legal_review",
                                   request_text=f"第 {cycles} 轮长稳分析")
                soak_tasks.append(task["id"])
                AgentRunner.instance().start(task["id"])
                for _ in range(100):
                    if get_task(task["id"])["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS", "CANCELLED", "FAILED"):
                        break
                    time.sleep(0.05)
                if not get_events(task["id"]):
                    failures.append(f"cycle{cycles}:no-events")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"cycle{cycles}:{type(exc).__name__}")
    finally:
        undo()

    integrity = db.query("PRAGMA integrity_check")
    from lawbuddy.audit import verify

    chain = verify()
    placeholders = ",".join("?" for _ in soak_tasks)
    rows_ok = db.query(
        f"SELECT COUNT(*) AS n FROM tasks WHERE id IN ({placeholders}) "
        "AND lifecycle IN ('COMPLETED','COMPLETED_WITH_WARNINGS')", tuple(soak_tasks),
    ) if soak_tasks else [{"n": 0}]
    completed = int(rows_ok[0]["n"])
    success_rate = completed / cycles if cycles else 0
    rows.append({"step": "soakShortRunNoCrash", "ok": not failures and cycles >= 3,
                 "cycles": cycles, "seconds": SOAK_SECONDS, "failures": failures[:5]})
    rows.append({"step": "soakSuccessRate100", "ok": success_rate >= 0.99,
                 "successRate": round(success_rate, 4), "cycleTasks": len(soak_tasks), "completed": completed})
    rows.append({"step": "soakIntegrityAndChain", "ok": str(list(integrity[0].values())[0]).lower() == "ok"
                 and bool(chain.get("ok")), "auditTotal": chain.get("total"), "documentId": doc_id})

    report["soak"] = {"cycles": cycles, "seconds": SOAK_SECONDS, "failures": len(failures)}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-perf-"))
    report: dict[str, Any] = {
        "evalName": "m3_perf",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "local_deterministic",
        "machine": {"python": sys.version.split()[0], "platform": sys.platform,
                    "cpuCount": __import__("os").cpu_count()},
        "gaps": [
            "P-004 OCR 真实吞吐、P-005 UI 主线程长任务、P-010 模型流式输出：需引擎/浏览器/模型端点，属专项与 E2E",
            "P-001/P-006/P-007 的推荐设备与 8 小时口径：本评测为开发机基线 + 缩短版长稳，发布前须在基准机复测",
        ],
    }
    try:
        ensure_workspace(str(tmp / "ws"))

        g1 = _seg(report, "coldStart", _run_cold_start, tmp)
        g2 = _seg(report, "firstScreen", _run_first_screen, tmp)
        g3 = _seg(report, "memory", _run_memory, tmp)
        g4 = _seg(report, "concurrency", _run_concurrency, tmp)
        g5 = _seg(report, "diskSpace", _run_disk_space, tmp)
        g6 = _seg(report, "soak", _run_soak, tmp)

        gates = {
            "coldStartP001": g1,
            "firstScreenCitationP002P003": g2,
            "memoryP006": g3,
            "concurrencyP008": g4,
            "diskSpaceP009": g5,
            "soakShortP007": g6,
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · 性能与稳定性（{r['mode']}） ===")
    print(f"环境：Python {r['machine']['python']} · {r['machine']['platform']} · {r['machine']['cpuCount']} 核")
    for seg, title in (("coldStart", "冷启动(P-001)"), ("firstScreen", "500 页首屏与引用跳转(P-002/P-003)"),
                       ("memory", "2000 页内存(P-006)"), ("concurrency", "并发(P-008)"),
                       ("diskSpace", "磁盘空间(P-009)"), ("soak", "缩短版长稳(P-007)")):
        entry = r.get(seg, {})
        print(f"{title}：{'通过' if entry.get('ok') else '未通过'}")
        for row in entry.get("rows", []):
            print(f"  {'✓' if row.get('ok') else '✗'} {row.get('step')}: "
                  f"{json.dumps({k: v for k, v in row.items() if k not in ('step', 'ok')}, ensure_ascii=False)}")
        if entry.get("error"):
            print(f"  ✗ 段异常：\n{entry['error']}")
    print("门槛判定：")
    for g, ok in r.get("gates", {}).items():
        print(f"  {'✓' if ok else '✗'} {g}")
    print(f"结论：{'M3 性能评测通过' if r.get('passed') else 'M3 性能评测未通过（见上方 ✗ 项）'}")
    if r.get("gaps"):
        print("范围说明与已知缺口：")
        for g in r["gaps"]:
            print(f"  · {g}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M3 性能与稳定性评测（离线可测部分）")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_perf_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()