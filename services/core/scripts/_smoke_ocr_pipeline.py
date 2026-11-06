# -*- coding: utf-8 -*-
"""管线级 OCR 速度实测（05-OCR迁移方案 §3.4 多 Worker 并行）。

用法（services/core 目录）：
    python scripts/_smoke_ocr_pipeline.py <PDF路径> [Worker数]

流程：临时工作空间导入 PDF → OcrManager 跑完全部 ocr_pending 页 → 计时。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    pdf = Path(sys.argv[1])
    if len(sys.argv) > 2:
        os.environ["LAWBUDDY_OCR_WORKERS"] = sys.argv[2]

    from lawbuddy.workspace import ensure_workspace

    ws = tempfile.mkdtemp(prefix="lawbuddy-ocr-smoke-")
    ensure_workspace(str(Path(ws) / "ws"))

    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.ocr.pipeline import OcrManager, _worker_count
    from lawbuddy.storage import get_db

    print(f"workers={_worker_count()} cpu={os.cpu_count()}")
    case = create_case(title="OCR管线实测", case_type="civil", client_name="测", client_role="原告代理人")
    t0 = time.monotonic()
    outcome = import_documents(case["id"], [str(pdf)])[0]
    doc_id = outcome.get("documentId")
    assert doc_id, outcome
    t_imp = time.monotonic() - t0

    db = get_db()

    if "--all-pending" in sys.argv:  # 模拟全扫描件：全部页标记待 OCR
        with db.transaction() as conn:
            conn.execute(
                "UPDATE document_pages SET ocr_status = 'pending', text_quality = 'ocr_pending', "
                "text_content = NULL WHERE version_id = "
                "(SELECT active_version_id FROM documents WHERE id = ?)",
                (doc_id,),
            )

    def counts() -> dict[str, int]:
        rows = db.query(
            "SELECT ocr_status, COUNT(*) AS n FROM document_pages p "
            "JOIN documents d ON d.active_version_id = p.version_id "
            "WHERE d.id = ? GROUP BY ocr_status",
            (doc_id,),
        )
        by = {r["ocr_status"]: r["n"] for r in rows}
        return {
            "total": sum(by.values()),
            "done": by.get("done", 0),
            "pending": by.get("pending", 0),
            "failed": by.get("failed", 0),
        }

    c0 = counts()
    print(f"import={t_imp:.1f}s pages={c0}")

    mgr = OcrManager.instance()
    t1 = time.monotonic()
    mgr.start(doc_id)
    last_done = -1
    while True:
        c = counts()
        if c["done"] != last_done:
            elapsed = time.monotonic() - t1
            print(f"  [{elapsed:6.1f}s] done={c['done']}/{c['total']} failed={c['failed']}")
            last_done = c["done"]
        t = mgr._threads.get(doc_id)  # noqa: SLF001
        if (t is None or not t.is_alive()) and c["pending"] == 0:
            break
        time.sleep(0.5)
    t_ocr = time.monotonic() - t1
    c = counts()
    avg = c["total"] and t_ocr / c["total"]
    print(f"ocr={t_ocr:.1f}s done={c['done']} failed={c['failed']} "
          f"perPage={avg and round(avg, 2)}s" if avg else f"ocr={t_ocr:.1f}s")


if __name__ == "__main__":
    main()
