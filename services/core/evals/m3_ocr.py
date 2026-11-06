# -*- coding: utf-8 -*-
"""M3 评测器：OCR 与文档解析（03 清单 §7 O-001~O-012 的确定性可自动化部分）。

用法（services/core 目录下）：
    python -m evals.m3_ocr [--keep]

覆盖（不依赖 OCR 引擎与外部模型，全程本地确定性）：
1. O-009 加密 / 损坏 / 不支持文件：明确状态、不崩溃、密码不进日志与数据库字段；
2. O-011 文本层异常：隐藏乱码文本层页自动回退 OCR，乱码不进入索引与 Agent 上下文；
3. O-012 压缩炸弹与畸形文件：按资源上限终止，主进程与数据库保持可用；
4. O-007 OCR 取消 / 暂停 / 恢复 / 失败页隔离（引擎以桩替换，验证管线状态机与 ≤3 秒取消）；
5. O-010 去重与缓存隔离：同内容重复导入不重复解析，缓存不跨案件；
6. O-008 / Q-009 OCR 人工更正：原值与更正值并存（追加式历史）、页文本与索引同步、
   原件与页图不变、引语不再可定位的 Claim 转 stale(OCR_CORRECTED)、审计不落正文；
7. 扫描 PDF 页渲染（05-OCR迁移方案 §3.2）：ocr_pending 页经 pypdfium2 渲染后完成 OCR
   （引擎以桩替换），任务结束案件临时目录无 PNG/PDF 明文残留（DB-009 扫描口径）。

未覆盖（需金标图集与真实引擎，见报告 gaps）：O-001~O-006 的 CER / 阅读顺序 / 表格 F1 门槛。

报告：控制台摘要 + evals/reports/m3_ocr_<ts>.json。门槛：全部 gates 通过。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

# 取消响应门槛（O-007：取消响应不超过 3 秒）
CANCEL_DEADLINE_S = 3.0


def _workspace_root() -> Path:
    from lawbuddy.storage import get_db

    row = get_db().query_one("SELECT root_uri FROM workspaces ORDER BY created_at LIMIT 1")
    if row is None:
        raise RuntimeError("workspace not initialized")
    return Path(row["root_uri"])


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


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _write_text_pdf(path: Path, pages: list[tuple[str, bool]]) -> Path:
    """用 reportlab 生成带文本层的 PDF；每页 (文本, 是否为符号乱码文本层)。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    for text, garbled in pages:
        payload = text if not garbled else "§±×÷¶¤¦¨ª¬¯°±²³´µ·¸¹º»¼½¾¿※†‡‰‱" * 3
        c.drawString(60, 780, payload)
        c.showPage()
    c.save()
    return path


def _write_zip_bomb(path: Path) -> Path:
    """构造压缩炸弹 DOCX（声明解压体积 200MB，压缩后几十 KB）。"""
    import zipfile

    payload = b"A" * (200 * 1024 * 1024)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", payload)
    return path


def _write_docx(path: Path, text: str) -> Path:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))
    return path


def _write_multi_frame_tiff(path: Path, count: int) -> Path:
    """生成多帧 TIFF（导入时为每帧建一个待 OCR 页），模拟多页扫描件。"""
    from PIL import Image

    frames = [Image.new("RGB", (60 + i * 5, 40 + i * 4), (250, 250, 250 - i)) for i in range(count)]
    frames[0].save(path, save_all=True, append_images=frames[1:], format="TIFF")
    return path


# ---------------------------------------------------------------------------
# 段 1：异常文件（O-009 / O-012）
# ---------------------------------------------------------------------------

def _run_bad_files(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.storage import get_db

    db = get_db()
    root = _workspace_root()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-异常文件", case_type="civil", client_name="甲", client_role="原告代理人")
    cid = case["id"]
    fixtures = root / "ocr-eval"
    fixtures.mkdir(parents=True, exist_ok=True)

    # 损坏 PDF（截断的文件头）
    broken = fixtures / "损坏样本.pdf"
    broken.write_bytes(b"%PDF-1.7\n" + b"\x00" * 512 + b"%%EOF")
    # 不支持类型（二进制、无魔数、非 UTF-8 文本）
    unsupported = fixtures / "固件样本.bin"
    unsupported.write_bytes(bytes(range(256)) * 4)
    # 压缩炸弹 DOCX
    bomb = _write_zip_bomb(fixtures / "炸弹样本.docx")

    outcomes = {o["name"]: o for o in import_documents(cid, [str(broken), str(unsupported), str(bomb)])}
    rows.append({"step": "importOutcomes", "ok": True, "raw": outcomes})
    rows.append({"step": "corruptedPdfFailedCleanly",
                 "ok": outcomes.get("损坏样本.pdf", {}).get("status") == "failed",
                 "status": outcomes.get("损坏样本.pdf", {}).get("status"),
                 "reason": outcomes.get("损坏样本.pdf", {}).get("reason")})
    rows.append({"step": "unsupportedTypeRejected",
                 "ok": outcomes.get("固件样本.bin", {}).get("status") == "failed"
                 and "不支持" in (outcomes.get("固件样本.bin", {}).get("reason") or ""),
                 "reason": outcomes.get("固件样本.bin", {}).get("reason")})
    rows.append({"step": "zipBombTerminatedWithinLimits",
                 "ok": outcomes.get("炸弹样本.docx", {}).get("status") == "failed"
                 and "资源上限" in (outcomes.get("炸弹样本.docx", {}).get("reason") or ""),
                 "reason": outcomes.get("炸弹样本.docx", {}).get("reason")})

    # 主进程/数据库仍可用，且失败文件未留下可检索内容
    usable = bool(db.query_one("SELECT COUNT(*) AS n FROM cases"))
    fts_rows = int(db.query_one("SELECT COUNT(*) AS n FROM pages_fts WHERE case_id = ?", (cid,))["n"])
    rows.append({"step": "dbUsableAfterBadFiles", "ok": usable and fts_rows == 0, "ftsRows": fts_rows})

    # 密码类字段不落盘：documents 表无密码列，audit 不含密码字样
    audit_leak = db.query(
        "SELECT action, metadata_json FROM audit_logs WHERE metadata_json LIKE '%password%' "
        "OR metadata_json LIKE '%密码%'"
    )
    rows.append({"step": "noPasswordInAudit", "ok": not audit_leak, "hits": [r["action"] for r in audit_leak][:3]})

    report["badFiles"] = {"caseId": cid}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 2：乱码文本层回退（O-011）
# ---------------------------------------------------------------------------

def _run_garbled_fallback(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.storage import get_db

    db = get_db()
    root = _workspace_root()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-乱码回退", case_type="criminal", client_name="乙", client_role="被告人辩护人")
    cid = case["id"]
    fixtures = root / "ocr-eval"

    # 正常文本页 + 乱码文本层页 混合 PDF
    mixed = _write_text_pdf(fixtures / "混合文本层.pdf", [("normal page text", False), ("garbled page", True)])
    outcome = import_documents(cid, [str(mixed)])[0]
    doc_id = outcome.get("documentId")
    pages = db.query(
        "SELECT page_no, text_quality, ocr_status, text_content FROM document_pages "
        "WHERE version_id = (SELECT active_version_id FROM documents WHERE id = ?) ORDER BY page_no",
        (doc_id,),
    )
    rows.append({"step": "garbledPageFallsBackToOcr",
                 "ok": len(pages) == 2 and pages[0]["text_quality"] == "native"
                 and pages[1]["text_quality"] == "ocr_pending" and pages[1]["ocr_status"] == "pending"
                 and not (pages[1]["text_content"] or "").strip(),
                 "pages": [{"quality": p["text_quality"], "status": p["ocr_status"]} for p in pages]})

    garbled_in_fts = int(db.query_one(
        "SELECT COUNT(*) AS n FROM pages_fts WHERE document_id = ? AND text_content LIKE ?",
        (doc_id, "%" + chr(0xE000) + "%"),
    )["n"])
    rows.append({"step": "garbledTextNotIndexed", "ok": garbled_in_fts == 0, "ftsHits": garbled_in_fts})

    # 文档状态为 partial（有待 OCR 页），且 OCR 待处理计数可见
    doc = db.query_one("SELECT status FROM documents WHERE id = ?", (doc_id,))
    rows.append({"step": "documentMarkedPartial", "ok": doc["status"] == "partial", "status": doc["status"]})

    report["garbled"] = {"documentId": doc_id}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 3：OCR 管线状态机（O-007）
# ---------------------------------------------------------------------------

class _StubWorker:
    """替代真实 PaddleOCR Worker：可控耗时、可模拟失败，验证管线语义。

    并行管线（05-OCR迁移方案 §3.4）下多个 Worker 同时识别、页完成顺序不确定，
    页序敏感的结果必须用 results_by_image 按图片路径分发（评测侧为每页生成
    独立的桩图片路径）；results 列表仅用于序不敏感的夹具。
    """

    delay = 0.05
    results: list[dict[str, Any]] = []
    results_by_image: dict[str, dict[str, Any]] = {}

    def __init__(self, cpu_threads: int | None = None) -> None:
        self.closed = False
        self.terminated = False

    def ocr(self, image_path: str, kind: str | None = None) -> dict[str, Any]:
        time.sleep(self.delay)
        if self.terminated:
            from lawbuddy.ocr.pipeline import OcrTaskError

            raise OcrTaskError("Worker 已终止")
        result = self.results_by_image.get(image_path)
        if result is None:
            result = self.results.pop(0) if self.results else {
                "fullText": "识别文本", "avgConfidence": 0.95,
                "lines": [{"text": "识别文本", "bbox": [0, 0, 10, 10], "confidence": 0.95}],
            }
        if result.get("raise"):
            # 页级失败（Worker 存活）：隔离该页并继续后续页
            from lawbuddy.ocr.pipeline import OcrPageError

            raise OcrPageError("模拟单页识别失败")
        return {k: v for k, v in result.items() if k != "raise"}

    def close(self) -> None:
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True


def _run_pipeline(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    import lawbuddy.ocr.pipeline as pipeline
    from lawbuddy.cases import create_case, delete_case
    from lawbuddy.documents import import_documents
    from lawbuddy.storage import get_db

    db = get_db()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-OCR管线", case_type="civil", client_name="丙", client_role="原告代理人")
    cid = case["id"]

    # 多帧 TIFF：每帧一个待 OCR 页，模拟扫描卷宗
    pages_n = 6
    scan = _write_multi_frame_tiff(tmp / "scan-main.tiff", pages_n)
    outcomes = import_documents(cid, [str(scan)])
    doc_id = next((o["documentId"] for o in outcomes if o.get("documentId")), None)
    pending_pages = int(db.query_one(
        "SELECT COUNT(*) AS n FROM document_pages p JOIN documents d ON d.active_version_id = p.version_id "
        "WHERE d.id = ? AND p.ocr_status = 'pending'", (doc_id,),
    )["n"]) if doc_id else 0
    rows.append({"step": "scannedPagesPendingOcr",
                 "ok": pending_pages == pages_n, "pending": pending_pages, "expected": pages_n,
                 "outcome": outcomes[0] if outcomes else None})
    if doc_id is None:
        return rows, False

    # 引擎与页面渲染替换为桩（确定性、无模型依赖）
    orig_worker, orig_image = pipeline._OcrWorker, pipeline._page_image  # noqa: SLF001
    pipeline._OcrWorker = _StubWorker  # type: ignore[assignment]  # noqa: SLF001
    pipeline._page_image = lambda root, page: (tmp / "stub.png", None)  # type: ignore[assignment]  # noqa: SLF001
    try:
        mgr = pipeline.OcrManager()

        # 取消响应 ≤3 秒：加大单页耗时，取消后管线必须立即停止并保住已完成页
        _StubWorker.delay = 1.5
        mgr.start(doc_id)
        time.sleep(0.4)
        t0 = time.monotonic()
        mgr.cancel(doc_id)
        while time.monotonic() - t0 < 10:
            if not mgr._threads[doc_id].is_alive():  # noqa: SLF001
                break
            time.sleep(0.02)
        stopped_in = time.monotonic() - t0
        settled = mgr.status(doc_id)
        rows.append({"step": "cancelRespondsWithin3s", "ok": stopped_in < CANCEL_DEADLINE_S,
                     "seconds": round(stopped_in, 2)})
        rows.append({"step": "cancelStopsFurtherPages",
                     "ok": settled["pending"] >= 1,
                     "done": settled["done"], "pending": settled["pending"]})

        # 恢复：断点续跑（剩余页全部完成，总数守恒）
        _StubWorker.delay = 0.02
        mgr.resume(doc_id)
        for _ in range(200):
            snap = mgr.status(doc_id)
            if snap["pending"] == 0:
                break
            time.sleep(0.05)
        final = mgr.status(doc_id)
        rows.append({"step": "resumeContinuesFromCheckpoint",
                     "ok": final["pending"] == 0 and final["done"] == pages_n and final["failed"] == 0,
                     "snapshot": {k: final[k] for k in ("total", "done", "pending", "failed")}})

        # 失败页隔离：首帧识别失败（Worker 存活）→ 该页 failed，其余页继续完成（FR-005）
        case2 = create_case(title="M3评测-OCR失败隔离", case_type="civil", client_name="丁", client_role="原告代理人")
        scan2 = _write_multi_frame_tiff(tmp / "scan-fail.tiff", 3)
        doc2 = import_documents(case2["id"], [str(scan2)])[0]["documentId"]
        _StubWorker.results = [{"raise": True}]
        mgr.start(doc2)
        for _ in range(300):
            s2 = mgr.status(doc2)
            t2 = mgr._threads.get(doc2)  # noqa: SLF001
            # pending 归零后线程仍在收尾（写终态快照），必须等线程真正退出再断言
            if s2["pending"] == 0 and (t2 is None or not t2.is_alive()):
                break
            time.sleep(0.05)
        rows.append({"step": "failedPageIsolated",
                     "ok": s2["failed"] == 1 and s2["done"] == 2 and s2["state"] == "failed",
                     "snapshot": {k: s2[k] for k in ("total", "done", "failed", "state")}})

        # 暂停：置 paused 状态且不再推进（并行管线下在途页会完成、其余页保持
        # pending，故夹具页数须多于并行在途窗口）
        case3 = create_case(title="M3评测-OCR暂停", case_type="civil", client_name="戊", client_role="原告代理人")
        scan3 = _write_multi_frame_tiff(tmp / "scan-pause.tiff", 8)
        doc3 = import_documents(case3["id"], [str(scan3)])[0]["documentId"]
        _StubWorker.results = []
        _StubWorker.delay = 0.3
        mgr.start(doc3)
        time.sleep(0.05)
        mgr.pause(doc3)
        for _ in range(80):
            if not mgr._threads[doc3].is_alive():  # noqa: SLF001
                break
            time.sleep(0.05)
        paused = mgr.status(doc3)
        rows.append({"step": "pauseStopsAdvancing", "ok": paused["state"] == "paused" and paused["pending"] >= 1,
                     "state": paused["state"], "pending": paused["pending"]})

        # 重复 start()（导入并发 + 轮询续跑的真实触发路径）必须秒级返回：
        # 曾经的锁重入会把 stdin RPC 主循环冻死，整个 Core（含 ping）不再响应，
        # 前端表现为永久「解析中 0/N」。
        _StubWorker.delay = 0.3
        mgr.start(doc3)
        time.sleep(0.1)
        box: dict[str, object] = {}

        def _repeat_start() -> None:
            t0 = time.monotonic()
            snap = mgr.start(doc3)
            box["seconds"] = time.monotonic() - t0
            box["state"] = snap["state"]

        caller = threading.Thread(target=_repeat_start, daemon=True, name="eval-repeat-start")
        caller.start()
        caller.join(timeout=5)
        elapsed = box.get("seconds")
        rows.append({"step": "repeatStartReturnsImmediately",
                     "ok": not caller.is_alive() and elapsed is not None and float(elapsed) < 1.0
                     and box.get("state") == "running",
                     "seconds": round(float(elapsed), 3) if elapsed is not None else None,
                     "state": box.get("state")})
        mgr.cancel(doc3)
        for _ in range(100):
            if not mgr._threads[doc3].is_alive():  # noqa: SLF001
                break
            time.sleep(0.05)

        # 系统级自愈：进程内状态丢失 / 切换工作空间 / Core 重启后，仍为 pending 的
        # 页必须由 Core 自行续跑，不能等渲染进程轮询触发。
        # 显式留两页 pending（模拟被中断的断点），保证用例不依赖取消时机。
        with db.transaction() as conn:
            page_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT p.id FROM document_pages p JOIN documents d ON d.active_version_id = p.version_id "
                    "WHERE d.id = ? ORDER BY p.page_no LIMIT 2",
                    (doc3,),
                ).fetchall()
            ]
            for page_id in page_ids:
                conn.execute("UPDATE document_pages SET ocr_status = 'pending' WHERE id = ?", (page_id,))
            conn.execute("UPDATE documents SET status = 'partial' WHERE id = ?", (doc3,))
        suspended = mgr.suspend(timeout=10)
        _StubWorker.delay = 0.02
        # 真实切换流程：新工作空间库就绪后由 workspace.* 解除闸门再续跑
        mgr.resume_after_switch()
        resumed = mgr.resume_pending(limit=50)
        for _ in range(200):
            if mgr.status(doc3)["pending"] == 0:
                break
            time.sleep(0.05)
        healed = mgr.status(doc3)
        if mgr._resume_thread is not None:  # noqa: SLF001  # 等调度线程自行收尾
            mgr._resume_thread.join(timeout=10)  # noqa: SLF001
        rows.append({"step": "autoResumeAfterSuspend",
                     "ok": suspended and doc3 in resumed and healed["pending"] == 0
                     and healed["done"] == 8 and healed["failed"] == 0,
                     "suspended": suspended, "resumed": doc3 in resumed,
                     "snapshot": {k: healed[k] for k in ("total", "done", "pending", "failed")}})

        # 孤儿收口：导入中断 / Core 崩溃会留下「已无待识别页却停在 parsing」的文档，
        # 此时没有 pending 页可续跑，必须由 Core 直接按页级结果推进终态，
        # 否则前端会永久显示「解析中」。
        _StubWorker.delay = 0.02
        with db.transaction() as conn:
            conn.execute("UPDATE documents SET status = 'parsing' WHERE id = ?", (doc_id,))
        mgr.resume_pending(limit=50)
        settled = db.query_one("SELECT status FROM documents WHERE id = ?", (doc_id,))
        rows.append({"step": "orphanParsingSettled",
                     "ok": settled is not None and settled["status"] == "ready",
                     "status": settled["status"] if settled else None})

        # 用户真实路径：识别进行中删除该任务，再在同一工作空间导入同一份文件。
        # 曾经的缺陷是 cancel() 只置标志不等线程退出，删除事务与 OCR 落库并发，
        # text_blocks 外键失败把消费者线程打崩，剩余页永远 pending（「解析中」）。
        _StubWorker.delay = 0.3
        _StubWorker.results = []
        case_del = create_case(title="M3评测-删除中任务", case_type="civil",
                               client_name="己", client_role="原告代理人")
        scan_del = _write_multi_frame_tiff(tmp / "scan-del.tiff", 8)
        doc_del = import_documents(case_del["id"], [str(scan_del)])[0]["documentId"]
        mgr.start(doc_del)
        time.sleep(0.2)
        deleted = delete_case(case_del["id"])
        waited = mgr.stop_and_wait(doc_del, timeout=1.0)  # 已删除：线程应已随取消退出
        rows.append({"step": "deleteCaseWhileOcrRunning",
                     "ok": bool(deleted.get("deleted")) and waited,
                     "deleted": bool(deleted.get("deleted")), "threadStopped": waited})

        # 删除后重新发起新任务（同工作空间、同一份扫描件）必须能正常识别完成
        _StubWorker.delay = 0.02
        _StubWorker.results = []
        case_new = create_case(title="M3评测-删除后新建", case_type="civil",
                               client_name="庚", client_role="原告代理人")
        scan_new = _write_multi_frame_tiff(tmp / "scan-new.tiff", 6)
        doc_new = import_documents(case_new["id"], [str(scan_new)])[0]["documentId"]
        mgr.start(doc_new)
        for _ in range(300):
            snap_new = mgr.status(doc_new)
            if snap_new["pending"] == 0:
                break
            time.sleep(0.05)
        rows.append({"step": "newTaskAfterDeleteCompletes",
                     "ok": snap_new["pending"] == 0 and snap_new["done"] == 6
                     and snap_new["failed"] == 0,
                     "snapshot": {k: snap_new[k] for k in ("total", "done", "pending", "failed")}})

        # 停滞自愈：线程 alive 但一页都完不成（消费者阻塞在永不返回的调用上、
        # 生产者卡在队列上）时，守护线程必须重启任务；多次重启仍无进展则显式
        # 失败收口。否则前端会永久停在「解析中 0/N」并假装还在工作。
        _StubWorker.delay = 0.02
        _StubWorker.results = []
        scan_stall = _write_multi_frame_tiff(tmp / "scan-stall.tiff", 4)
        case_stall = create_case(title="M3评测-停滞自愈", case_type="civil",
                                 client_name="辛", client_role="原告代理人")
        doc_stall = import_documents(case_stall["id"], [str(scan_stall)])[0]["documentId"]
        # 用「活着但不会推进」的线程占位，模拟识别链路卡死（不占用真实 Worker）
        stuck = threading.Thread(target=time.sleep, args=(3.0,), daemon=True, name="ocr-stuck-fake")
        stuck.start()
        with mgr._lock:  # noqa: SLF001
            mgr._threads[doc_stall] = stuck  # noqa: SLF001
            # 已完成页数长时间未变化：首次观察时间推到停滞阈值之前
            mgr._progress[doc_stall] = (0, time.monotonic() - pipeline.OCR_STALL_RESTART_S * 2, 0)  # noqa: SLF001
        mgr._restart_stalled()  # noqa: SLF001
        for _ in range(300):
            snap_stall = mgr.status(doc_stall)
            if snap_stall["pending"] == 0:
                break
            time.sleep(0.05)
        rows.append({"step": "stalledTaskRestarted",
                     "ok": snap_stall["pending"] == 0 and snap_stall["done"] == 4
                     and snap_stall["failed"] == 0,
                     "snapshot": {k: snap_stall[k] for k in ("total", "done", "pending", "failed")}})

        # 重启上限用尽：必须把剩余页显式隔离为 failed，并让文档不再是「解析中」
        _StubWorker.delay = 0.02
        scan_giveup = _write_multi_frame_tiff(tmp / "scan-giveup.tiff", 3)
        case_giveup = create_case(title="M3评测-停滞放弃", case_type="civil",
                                  client_name="壬", client_role="原告代理人")
        doc_giveup = import_documents(case_giveup["id"], [str(scan_giveup)])[0]["documentId"]
        stuck2 = threading.Thread(target=time.sleep, args=(3.0,), daemon=True, name="ocr-stuck-fake2")
        stuck2.start()
        with mgr._lock:  # noqa: SLF001
            mgr._threads[doc_giveup] = stuck2  # noqa: SLF001
            mgr._progress[doc_giveup] = (  # noqa: SLF001
                0, time.monotonic() - pipeline.OCR_STALL_RESTART_S * 2,
                pipeline.OCR_STALL_RESTART_MAX,
            )
        mgr._restart_stalled()  # noqa: SLF001
        snap_giveup = mgr.status(doc_giveup)
        doc_row = db.query_one("SELECT status FROM documents WHERE id = ?", (doc_giveup,))
        with mgr._lock:  # noqa: SLF001
            mgr._threads.pop(doc_giveup, None)  # noqa: SLF001
            mgr._progress.pop(doc_giveup, None)  # noqa: SLF001
        rows.append({"step": "stalledTaskGivesUpExplicitly",
                     "ok": snap_giveup["pending"] == 0 and snap_giveup["failed"] == 3
                     and doc_row is not None and doc_row["status"] == "failed",
                     "snapshot": {k: snap_giveup[k] for k in ("total", "done", "pending", "failed")},
                     "documentStatus": doc_row["status"] if doc_row else None})
    finally:
        pipeline._OcrWorker = orig_worker  # type: ignore[assignment]  # noqa: SLF001
        pipeline._page_image = orig_image  # type: ignore[assignment]  # noqa: SLF001
        _StubWorker.delay = 0.05
        _StubWorker.results = []

    report["pipeline"] = {"documentId": doc_id, "pages": pages_n}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 4：去重与缓存隔离（O-010）
# ---------------------------------------------------------------------------

def _run_dedup_cache(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.storage import get_db

    db = get_db()
    root = _workspace_root()
    rows: list[dict[str, Any]] = []
    fixtures = root / "ocr-eval"
    sample = _write_docx(fixtures / "缓存样本.docx", "缓存与去重评测正文")

    case_a = create_case(title="M3评测-缓存A", case_type="civil", client_name="己", client_role="原告代理人")
    case_b = create_case(title="M3评测-缓存B", case_type="civil", client_name="庚", client_role="原告代理人")

    first = import_documents(case_a["id"], [str(sample)])[0]
    again = import_documents(case_a["id"], [str(sample)])[0]
    other_case = import_documents(case_b["id"], [str(sample)])[0]

    rows.append({"step": "sameContentDeduped",
                 "ok": first["status"] == "imported" and again["status"] == "duplicate"
                 and again.get("documentId") == first.get("documentId")})

    # 缓存不跨案件：另一案件独立建档，且检索不跨界
    rows.append({"step": "cacheNotSharedAcrossCases",
                 "ok": other_case["status"] == "imported"
                 and other_case.get("documentId") != first.get("documentId")})

    from lawbuddy.search import search_pages

    hits_a = search_pages(case_a["id"], "缓存与去重评测正文", limit=5)
    hits_b = search_pages(case_b["id"], "缓存与去重评测正文", limit=5)
    rows.append({"step": "searchScopedToCase",
                 "ok": bool(hits_a) and bool(hits_b)
                 and all(h["caseId"] == case_a["id"] for h in hits_a) if "caseId" in (hits_a[0] if hits_a else {})
                 else bool(hits_a) and bool(hits_b)})

    # 同内容在原案件仅一份原件（复用哈希，不重复落盘）
    blobs = list((root / "blobs").rglob("*"))
    files = [b for b in blobs if b.is_file()]
    rows.append({"step": "blobStoredOnceByHash", "ok": len({f.name for f in files}) == len(files),
                 "blobs": len(files)})

    report["dedup"] = {"caseA": case_a["id"], "caseB": case_b["id"]}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 5：OCR 人工更正（O-008 / Q-009 / DEV-DOC-010）
# ---------------------------------------------------------------------------

def _run_correction(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    """更正留痕与引用失效：原值与更正值并存、索引同步、原件只读、Claim 转 stale、审计不落正文。"""
    import lawbuddy.ocr.pipeline as pipeline
    from lawbuddy.agent.tasks import create_task
    from lawbuddy.cases import create_case
    from lawbuddy.citations import judge_claim
    from lawbuddy.documents import (
        correct_page_text, get_page_text_blocks, get_pages, import_documents, list_page_corrections,
    )
    from lawbuddy.search import search_pages
    from lawbuddy.storage import get_db

    db = get_db()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-OCR更正", case_type="civil", client_name="辛", client_role="原告代理人")
    cid = case["id"]

    page1_ocr = "借款金额壹佰万元整。"
    page2_ocr = "还款日期二〇二四年三月十四日。"
    scan = _write_multi_frame_tiff(tmp / "scan-fix.tiff", 2)
    doc_id = import_documents(cid, [str(scan)])[0]["documentId"]

    # 用桩引擎把两页识别为各自的 OCR 文本（确定性，无模型依赖；并行管线下页
    # 完成顺序不确定，结果按每页独立的桩图片路径分发）
    orig_worker, orig_image = pipeline._OcrWorker, pipeline._page_image  # noqa: SLF001
    prev_delay, prev_results = _StubWorker.delay, _StubWorker.results
    prev_by_image = _StubWorker.results_by_image
    pipeline._OcrWorker = _StubWorker  # type: ignore[assignment]  # noqa: SLF001
    pipeline._page_image = lambda root, page: (tmp / f"stub-p{page['page_no']}.png", None)  # type: ignore[assignment]  # noqa: SLF001
    _StubWorker.delay = 0.02
    _StubWorker.results = []
    _StubWorker.results_by_image = {
        str(tmp / "stub-p1.png"): {
            "fullText": page1_ocr, "avgConfidence": 0.95,
            "lines": [{"text": page1_ocr, "bbox": [0, 0, 10, 10], "confidence": 0.95}]},
        str(tmp / "stub-p2.png"): {
            "fullText": page2_ocr, "avgConfidence": 0.95,
            "lines": [{"text": page2_ocr, "bbox": [0, 0, 10, 10], "confidence": 0.95}]},
    }
    try:
        mgr = pipeline.OcrManager()
        mgr.start(doc_id)
        for _ in range(200):
            if mgr.status(doc_id)["pending"] == 0:
                break
            time.sleep(0.05)
    finally:
        pipeline._OcrWorker = orig_worker  # type: ignore[assignment]  # noqa: SLF001
        pipeline._page_image = orig_image  # type: ignore[assignment]  # noqa: SLF001
        _StubWorker.delay, _StubWorker.results = prev_delay, prev_results
        _StubWorker.results_by_image = prev_by_image

    pages = get_pages(doc_id)
    rows.append({"step": "ocrTextReadyForCorrection",
                 "ok": len(pages) == 2 and pages[0]["quality"] == "ocr" and page1_ocr in pages[0]["text"],
                 "pages": [{"quality": p["quality"], "len": len(p["text"])} for p in pages]})
    blocks = get_page_text_blocks(doc_id, 1)
    rows.append({"step": "ocrBlocksExposeFragmentCoordinates",
                 "ok": bool(blocks) and blocks[0]["text"] == page1_ocr
                 and blocks[0]["bbox"] == [0, 0, 10, 10],
                 "count": len(blocks), "bbox": blocks[0]["bbox"] if blocks else None})

    version_id = db.query_one("SELECT active_version_id FROM documents WHERE id = ?", (doc_id,))[
        "active_version_id"
    ]
    # 更正前：引语可定位 → Claim 为 supported（后续应因更正转 stale）
    task = create_task(cid, "legal_review", "OCR 更正评测任务")
    cite = judge_claim(
        task["id"], cid, "借款金额为壹佰万元整。",
        {"documentId": doc_id, "versionId": version_id, "pageNo": 1, "snippet": "借款金额壹佰万元整"},
    )
    rows.append({"step": "claimSupportedBeforeCorrection",
                 "ok": cite.get("claimStatus") == "supported", "claimStatus": cite.get("claimStatus")})

    ver = db.query_one("SELECT sha256, blob_uri FROM document_versions WHERE id = ?", (version_id,))
    sha_before = ver["sha256"]
    blob_path = _workspace_root() / ver["blob_uri"]
    blob_before = blob_path.read_bytes() if blob_path.is_file() else None

    corrected = "借款金额伍拾万元整（经人工核对转账凭证后更正）。"
    res = correct_page_text(doc_id, 1, corrected, note="按转账凭证更正金额")
    corr = db.query_one(
        "SELECT original_text, corrected_text, original_confidence FROM ocr_corrections "
        "WHERE document_id = ? ORDER BY created_at DESC LIMIT 1", (doc_id,),
    )
    rows.append({"step": "correctionKeepsOriginalValue",
                 "ok": corr["original_text"] == page1_ocr and corr["corrected_text"] == corrected
                 and corr["original_confidence"] is not None,
                 "originalLen": len(corr["original_text"] or ""), "correctedLen": len(corr["corrected_text"] or "")})

    after = db.query_one(
        "SELECT text_content, ocr_confidence FROM document_pages WHERE version_id = ? AND page_no = 1",
        (version_id,),
    )
    rows.append({"step": "pageTextUpdatedForSearchAndAgent",
                 "ok": (after["text_content"] or "").strip() == corrected,
                 "text": (after["text_content"] or "")[:24]})

    sha_after = db.query_one("SELECT sha256 FROM document_versions WHERE id = ?", (version_id,))["sha256"]
    blob_after = blob_path.read_bytes() if blob_path.is_file() else None
    rows.append({"step": "originalDocumentUnchanged",
                 "ok": sha_after == sha_before and blob_after == blob_before and blob_after is not None,
                 "sha256": sha_after})

    new_hits = search_pages(cid, "伍拾万元", limit=5)
    old_hits = search_pages(cid, "壹佰万元", limit=5)
    rows.append({"step": "searchFollowsCorrectedText",
                 "ok": any(h["documentId"] == doc_id and h["pageNo"] == 1 for h in new_hits)
                 and not any(h["documentId"] == doc_id and h["pageNo"] == 1 for h in old_hits),
                 "newHits": len(new_hits), "oldHits": len(old_hits)})

    claim = db.query_one("SELECT status, reason_code FROM claims WHERE id = ?", (cite["claimId"],))
    rows.append({"step": "staleUnresolvableReference",
                 "ok": claim["status"] == "stale" and claim["reason_code"] == "OCR_CORRECTED"
                 and res["staleClaims"] == 1,
                 "status": claim["status"], "reasonCode": claim["reason_code"]})

    corrs = list_page_corrections(doc_id, 1)
    meta = get_pages(doc_id)[0]
    rows.append({"step": "correctionHistoryAndPageFlag",
                 "ok": len(corrs) == 1 and corrs[0]["originalText"] == page1_ocr
                 and meta["corrected"] is True and meta["correctionCount"] == 1,
                 "history": len(corrs), "corrected": meta["corrected"]})

    page_id = db.query_one(
        "SELECT id FROM document_pages WHERE version_id = ? AND page_no = 1", (version_id,)
    )["id"]
    audit_row = db.query_one(
        "SELECT metadata_json FROM audit_logs WHERE action = 'document.ocr_correct' AND resource_id = ?",
        (page_id,),
    )
    meta_json = audit_row["metadata_json"] if audit_row else ""
    stale_audit = db.query_one(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'claim.stale' AND resource_id = ?",
        (cite["claimId"],),
    )
    rows.append({"step": "auditedWithoutDocumentText",
                 "ok": bool(audit_row) and corrected not in meta_json and page1_ocr not in meta_json
                 and int(stale_audit["n"]) >= 1,
                 "metadata": meta_json})

    # 追加式：再次更正不覆盖上一条记录（原值链完整）
    corrected2 = "借款金额伍拾万元整（第二稿核对）。"
    kind = db.query_one("SELECT text_content FROM document_pages WHERE version_id = ? AND page_no = 2", (version_id,))
    correct_page_text(doc_id, 1, corrected2)
    corrs2 = list_page_corrections(doc_id, 1)
    rows.append({"step": "secondCorrectionAppendsHistory",
                 "ok": len(corrs2) == 2 and corrs2[0]["originalText"] == corrected
                 and corrs2[0]["correctedText"] == corrected2
                 and kind["text_content"] == page2_ocr,  # 其他页不受影响
                 "history": len(corrs2)})

    rejected = False
    try:
        correct_page_text(doc_id, 1, "   ")
    except Exception as exc:  # noqa: BLE001
        rejected = "不能为空" in str(exc)
    rows.append({"step": "emptyCorrectionRejected", "ok": rejected})

    report["correction"] = {"caseId": cid, "documentId": doc_id}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 6：扫描 PDF 页渲染（05-OCR迁移方案 §3.2 / §4 验收）
# ---------------------------------------------------------------------------

def _run_pdf_render(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    """扫描 PDF 页渲染补缺：ocr_pending 页经 pypdfium2 渲染后完成 OCR、临时明文无残留。

    页面渲染走真实路径（materialize 解密 + render_pdf_page + _pdf_hold 跨页复用），
    仅 OCR 引擎以桩替换（确定性，无模型依赖）。
    """
    import lawbuddy.ocr.pipeline as pipeline
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.storage import get_db

    db = get_db()
    root = _workspace_root()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-扫描PDF渲染", case_type="civil", client_name="壬", client_role="原告代理人")
    cid = case["id"]

    # 混合 PDF：正常文本页 + 2 页乱码文本层（导入即标记 ocr_pending）
    mixed = _write_text_pdf(tmp / "扫描样本.pdf", [("normal page text", False), ("garbled", True), ("garbled", True)])
    doc_id = import_documents(cid, [str(mixed)])[0]["documentId"]

    orig_worker = pipeline._OcrWorker  # noqa: SLF001
    pipeline._OcrWorker = _StubWorker  # type: ignore[assignment]  # noqa: SLF001
    _StubWorker.delay = 0.02
    try:
        mgr = pipeline.OcrManager()
        mgr.start(doc_id)
        for _ in range(200):
            if mgr.status(doc_id)["pending"] == 0:
                break
            time.sleep(0.05)
        # 等收尾清理（finally 段：释放 PDF 源 + purge 案件临时目录）完成
        t = mgr._threads.get(doc_id)  # noqa: SLF001
        if t is not None:
            t.join(timeout=10)
    finally:
        pipeline._OcrWorker = orig_worker  # type: ignore[assignment]  # noqa: SLF001

    final = mgr.status(doc_id)
    rows.append({"step": "scannedPdfPagesOcrDone",
                 "ok": final["pending"] == 0 and final["done"] == 2 and final["failed"] == 0,
                 "snapshot": {k: final[k] for k in ("total", "done", "pending", "failed")}})

    pages = db.query(
        "SELECT page_no, text_quality, ocr_status, text_content FROM document_pages "
        "WHERE version_id = (SELECT active_version_id FROM documents WHERE id = ?) ORDER BY page_no",
        (doc_id,),
    )
    rows.append({"step": "renderedPagesQualityOcr",
                 "ok": [p["text_quality"] for p in pages] == ["native", "ocr", "ocr"]
                 and [p["ocr_status"] for p in pages] == ["none", "done", "done"]
                 and all((p["text_content"] or "").strip() == "识别文本" for p in pages[1:]),
                 "pages": [{"quality": p["text_quality"], "status": p["ocr_status"]} for p in pages]})

    # 临时明文残留（DB-009 扫描口径）：任务结束后案件临时目录无 PNG / PDF 残留
    residues = list(tempstore_residues(root))
    rows.append({"step": "noTempResidueAfterPdfOcr", "ok": not residues, "residues": [str(p) for p in residues[:5]]})

    report["pdfRender"] = {"caseId": cid, "documentId": doc_id}
    ok = all(r["ok"] for r in rows)
    return rows, ok


def tempstore_residues(root: Path) -> list[Path]:
    """扫描工作空间临时目录中残留的 PNG/PDF 明文（评测口径）。"""
    from lawbuddy import tempstore

    base = tempstore.temp_root(root)
    if not base.exists():
        return []
    return [p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in (".png", ".pdf")]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m3_ocr",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "local_deterministic",  # OCR 引擎以桩替换，零模型与网络依赖
        "gaps": [
            "O-001~O-006 CER / 阅读顺序 / 表格 F1 / 手写签名门槛：需金标图集与真实 PaddleOCR 引擎，"
            "属人工与专项评测范围",
        ],
    }
    try:
        ensure_workspace(str(tmp / "ws"))

        # 强制 4 路并行 Worker：桩替换下无进程/内存开销，确定性覆盖并行管线
        # （取消 ≤3s、暂停保留 pending、失败隔离、按页结果分发）的竞态路径
        os.environ["LAWBUDDY_OCR_WORKERS"] = "4"

        g1 = _seg(report, "badFiles", _run_bad_files)
        g2 = _seg(report, "garbledFallback", _run_garbled_fallback)
        g3 = _seg(report, "pipeline", _run_pipeline, tmp)
        g4 = _seg(report, "dedupCache", _run_dedup_cache)
        g5 = _seg(report, "correction", _run_correction, tmp)
        g6 = _seg(report, "pdfRender", _run_pdf_render, tmp)

        gates = {
            "badFilesO009O012": g1,
            "garbledFallbackO011": g2,
            "pipelineO007": g3,
            "dedupCacheO010": g4,
            "correctionO008Q009": g5,
            "scannedPdfRender": g6,
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · OCR 与文档解析（{r['mode']}） ===")
    for seg, title in (("badFiles", "异常文件(O-009/O-012)"), ("garbledFallback", "乱码文本层回退(O-011)"),
                       ("pipeline", "OCR 管线状态机(O-007)"), ("dedupCache", "去重与缓存隔离(O-010)"),
                       ("correction", "OCR 人工更正(O-008/Q-009)"), ("pdfRender", "扫描 PDF 页渲染(05方案§3.2)")):
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
    print(f"结论：{'M3 OCR 评测通过' if r.get('passed') else 'M3 OCR 评测未通过（见上方 ✗ 项）'}")
    if r.get("gaps"):
        print("范围说明与已知缺口：")
        for g in r["gaps"]:
            print(f"  · {g}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M3 OCR 与文档解析评测（确定性部分）")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_ocr_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()