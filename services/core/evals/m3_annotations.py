# -*- coding: utf-8 -*-
"""M3 评测器：文档页批注（03 清单 DEV-DOC-010 批注部分 + Q-009/Q-010 联动）。

用法（services/core 目录下）：
    python -m evals.m3_annotations [--keep]

覆盖（不依赖 OCR 引擎与外部模型，全程本地确定性）：
1. 批注生命周期：新增 → 列表 → 编辑 → 处理/重开 → 删除；
   空内容、不存在页、不存在文档一律拒绝；批注不改原件（哈希不变）；
2. quote 锚定与重定位（Q-009/Q-010）：规范化（空白/断行）后仍可定位；
   OCR 人工更正导致原文失配时读取端标记 quote_missing，不静默错位；
   未锚定批注不受页文本变化影响；
3. 案件隔离与级联：批注绑定 document/case，列表只返回本文档；
   删除文档级联清除批注；四类审计动作齐全且不落批注正文；归档案件拒绝写入。

报告：控制台摘要 + evals/reports/m3_annotations_<ts>.json。门槛：全部 gates 通过。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any


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
# 夹具（与 m3_ocr 相同的桩 OCR：多帧 TIFF → 每帧一页，确定性识别文本）
# ---------------------------------------------------------------------------

def _write_multi_frame_tiff(path: Path, count: int) -> Path:
    from PIL import Image

    frames = [Image.new("RGB", (60 + i * 5, 40 + i * 4), (250, 250, 250 - i)) for i in range(count)]
    frames[0].save(path, save_all=True, append_images=frames[1:], format="TIFF")
    return path


class _StubWorker:
    """并行管线（05-OCR迁移方案 §3.4）下页完成顺序不确定，页序敏感的结果按
    每页独立的桩图片路径分发（results_by_image），results 列表仅兜底。"""

    delay = 0.02
    results: list[dict[str, Any]] = []
    results_by_image: dict[str, dict[str, Any]] = {}

    def __init__(self, cpu_threads: int | None = None) -> None:
        pass

    def ocr(self, image_path: str, kind: str | None = None) -> dict[str, Any]:
        time.sleep(self.delay)
        result = self.results_by_image.get(image_path) or (
            self.results.pop(0) if self.results else {
                "fullText": "识别文本", "avgConfidence": 0.95,
                "lines": [{"text": "识别文本", "bbox": [0, 0, 10, 10], "confidence": 0.95}],
            }
        )
        return {k: v for k, v in result.items() if k != "raise"}

    def close(self) -> None:
        pass

    def terminate(self) -> None:
        pass


def _ocr_two_pages(doc_id: str, texts: list[str], tmp: Path) -> None:
    """用桩引擎把前 len(texts) 页识别为给定 OCR 文本（确定性）。"""
    import lawbuddy.ocr.pipeline as pipeline

    orig_worker, orig_image = pipeline._OcrWorker, pipeline._page_image  # noqa: SLF001
    pipeline._OcrWorker = _StubWorker  # type: ignore[assignment]  # noqa: SLF001
    pipeline._page_image = lambda root, page: (tmp / f"stub-p{page['page_no']}.png", None)  # type: ignore[assignment]  # noqa: SLF001
    _StubWorker.results = []
    _StubWorker.results_by_image = {
        str(tmp / f"stub-p{i}.png"): {
            "fullText": t, "avgConfidence": 0.95,
            "lines": [{"text": t, "bbox": [0, 0, 10, 10], "confidence": 0.95}],
        }
        for i, t in enumerate(texts, start=1)
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
        _StubWorker.results = []
        _StubWorker.results_by_image = {}


# ---------------------------------------------------------------------------
# 段 1：生命周期与校验
# ---------------------------------------------------------------------------

def _run_lifecycle(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.documents import (
        ImportError_,
        add_annotation,
        delete_annotation,
        import_documents,
        list_annotations,
        set_annotation_resolved,
        update_annotation,
    )
    from lawbuddy.storage import get_db

    db = get_db()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-批注生命周期", case_type="civil", client_name="甲", client_role="原告代理人")
    cid = case["id"]
    doc_id = import_documents(cid, [str(_make_txt(report, "批注生命周期样本正文，含关键句：借款金额为壹佰万元整。"))])[
        0
    ]["documentId"]

    ann = add_annotation(doc_id, 1, body="该金额与转账凭证不一致，需核对。", quote="借款金额为壹佰万元整")
    rows.append({"step": "addReturnsOpenAnnotation",
                 "ok": ann["status"] == "open" and ann["pageNo"] == 1
                 and ann["documentId"] == doc_id and ann["quoteMissing"] is False,
                 "id": ann["id"][:8]})

    listed = list_annotations(doc_id)
    rows.append({"step": "listedWithFields",
                 "ok": len(listed) == 1 and listed[0]["id"] == ann["id"]
                 and listed[0]["quote"] == "借款金额为壹佰万元整" and listed[0]["createdAt"] is not None})

    updated = update_annotation(ann["id"], "该金额与转账凭证一致（已核对 2024-03 回单）。")
    rows.append({"step": "updateKeepsAnchorAndStatus",
                 "ok": updated["body"].startswith("该金额与转账凭证一致") and updated["status"] == "open"
                 and updated["updatedAt"] >= updated["createdAt"]})

    resolved = set_annotation_resolved(ann["id"], True)
    reopened = set_annotation_resolved(ann["id"], False)
    resolved2 = set_annotation_resolved(ann["id"], True)
    rows.append({"step": "resolveAndReopenTransitions",
                 "ok": resolved["status"] == "resolved" and resolved["resolvedAt"] is not None
                 and reopened["status"] == "open" and reopened["resolvedAt"] is None
                 and resolved2["status"] == "resolved"})

    # 校验：空内容 / 不存在页 / 不存在文档 / 不存在批注
    def _reject(fn, *a) -> bool:
        try:
            fn(*a)
            return False
        except ImportError_:
            return True

    rows.append({"step": "emptyBodyRejected", "ok": _reject(add_annotation, doc_id, 1, "   ")})
    rows.append({"step": "missingPageRejected", "ok": _reject(add_annotation, doc_id, 99, "内容")})
    rows.append({"step": "missingDocumentRejected", "ok": _reject(add_annotation, "doc-none", 1, "内容")})
    rows.append({"step": "missingAnnotationRejected",
                 "ok": _reject(update_annotation, "ann-none", "x") and _reject(delete_annotation, "ann-none")})
    rows.append({"step": "updateEmptyRejected", "ok": _reject(update_annotation, ann["id"], " ")})

    # 原件不变：批注不产生新版本，document_versions 哈希保持唯一
    vers = db.query("SELECT sha256 FROM document_versions WHERE id IN "
                    "(SELECT id FROM document_versions WHERE document_id = ?)", (doc_id,))
    rows.append({"step": "noNewVersionsCreatedByAnnotations", "ok": len(vers) == 1, "versions": len(vers)})

    deleted = delete_annotation(ann["id"])
    rows.append({"step": "deleteRemovesAnnotation",
                 "ok": deleted["deleted"] is True and list_annotations(doc_id) == []})
    _ann_case(report, "lifecycle", cid, doc_id)
    return rows, all(r["ok"] for r in rows)


def _make_txt(report: dict[str, Any], text: str) -> Path:
    """轻量文本样本：直接走 import_documents 支持的 txt 解析。"""
    from lawbuddy.storage import get_db

    row = get_db().query_one("SELECT root_uri FROM workspaces ORDER BY created_at LIMIT 1")
    root = Path(row["root_uri"])
    fixtures = root / "annotation-eval"
    fixtures.mkdir(parents=True, exist_ok=True)
    p = fixtures / f"样本-{report.get('_seq', 0)}.txt"
    report["_seq"] = int(report.get("_seq", 0)) + 1
    p.write_text(text, encoding="utf-8")
    return p


def _ann_case(report: dict[str, Any], seg: str, cid: str, doc_id: str) -> None:
    entry = report.get(seg) or {}
    entry.update({"caseId": cid, "documentId": doc_id})
    report[seg] = entry


# ---------------------------------------------------------------------------
# 段 2：quote 锚定与重定位（Q-009 / Q-010）
# ---------------------------------------------------------------------------

def _run_quote_anchor(report: dict[str, Any], tmp: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.documents import (
        add_annotation,
        correct_page_text,
        import_documents,
        list_annotations,
    )

    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-批注锚定", case_type="civil", client_name="乙", client_role="原告代理人")
    cid = case["id"]
    scan = _write_multi_frame_tiff(tmp / "ann-scan.tiff", 2)
    doc_id = import_documents(cid, [str(scan)])[0]["documentId"]
    _ocr_two_pages(doc_id, ["借款金额壹佰万元整。", "还款日期二〇二四年三月十四日。"], tmp)

    # Q-010：锚定片段含多余空白/断行时仍应规范化定位成功
    anchored = add_annotation(doc_id, 1, body="金额存疑，需与银行流水核对。",
                              quote="借款金额\n 壹佰万元整。")
    plain = add_annotation(doc_id, 2, body="日期页备注：以借条落款为准。")
    rows.append({"step": "normalizedQuoteLocatable",
                 "ok": anchored["quoteMissing"] is False and plain["quoteMissing"] is False,
                 "anchoredMissing": anchored["quoteMissing"]})

    # OCR 更正第 1 页 → 锚定引语不再可定位 → quote_missing=True（读取端计算，不落库）
    correct_page_text(doc_id, 1, "借款金额伍拾万元整（经人工核对转账凭证后更正）。")
    listed = {a["id"]: a for a in list_annotations(doc_id)}
    rows.append({"step": "quoteMissingAfterOcrCorrection",
                 "ok": listed[anchored["id"]]["quoteMissing"] is True,
                 "quoteMissing": listed[anchored["id"]]["quoteMissing"]})
    rows.append({"step": "unanchoredAnnotationUnaffected",
                 "ok": listed[plain["id"]]["quoteMissing"] is False
                 and listed[plain["id"]]["pageNo"] == 2})

    # 锚定字段不因失配被改写（原 quote 保留供人工核对）
    rows.append({"step": "originalQuoteKept",
                 "ok": "壹佰万元" in (listed[anchored["id"]]["quote"] or "")})
    _ann_case(report, "quoteAnchor", cid, doc_id)
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# 段 3：案件隔离、级联与审计
# ---------------------------------------------------------------------------

def _run_isolation_audit(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import CaseError, create_case
    from lawbuddy.documents import (
        add_annotation,
        delete_document,
        import_documents,
        list_annotations,
    )
    from lawbuddy.storage import get_db

    db = get_db()
    rows: list[dict[str, Any]] = []
    case_a = create_case(title="M3评测-批注隔离A", case_type="civil", client_name="丙", client_role="原告代理人")
    case_b = create_case(title="M3评测-批注隔离B", case_type="civil", client_name="丁", client_role="被告辩护人")
    doc_a = import_documents(case_a["id"], [str(_make_txt(report, "案件A样本正文：租赁合同押金条款。"))])[0]["documentId"]
    doc_b = import_documents(case_b["id"], [str(_make_txt(report, "案件B样本正文：买卖合同交付条款。"))])[0]["documentId"]

    ann_a = add_annotation(doc_a, 1, body="押金退还期限违反约定。", quote="押金")
    add_annotation(doc_b, 1, body="交付时间需明确。")

    # 隔离：列表按文档返回；行内 case_id/document_id 绑定正确（跨案召回为 0）
    rows.append({"step": "listScopedToDocument",
                 "ok": [a["id"] for a in list_annotations(doc_a)] == [ann_a["id"]]
                 and len(list_annotations(doc_b)) == 1})
    row = db.query_one("SELECT case_id, document_id FROM document_annotations WHERE id = ?", (ann_a["id"],))
    rows.append({"step": "rowBoundToCaseAndDocument",
                 "ok": row["case_id"] == case_a["id"] and row["document_id"] == doc_a})

    # 归档案件拒绝写入（REQ-CASE-006 只读）
    from lawbuddy.cases import archive_case

    archive_case(case_a["id"])
    archived_rejected = False
    try:
        add_annotation(doc_a, 1, body="归档后不应写入")
    except CaseError as exc:
        archived_rejected = "归档" in str(exc) or "只读" in str(exc)
    rows.append({"step": "archivedCaseRejectsWrite", "ok": archived_rejected})

    # 删除文档级联清除批注（page FK ON DELETE CASCADE）
    from lawbuddy.cases import restore_case

    restore_case(case_a["id"])
    delete_document(doc_a)
    left = int(db.query_one("SELECT COUNT(*) AS n FROM document_annotations WHERE document_id = ?", (doc_a,))["n"])
    rows.append({"step": "deleteDocumentCascadesAnnotations", "ok": left == 0, "left": left})

    # 审计：四类动作齐全；metadata 不含批注正文/锚定原文
    actions = {"document.annotate_add", "document.annotate_update", "document.annotate_resolve",
               "document.annotate_delete"}
    found = {r["action"] for r in db.query(
        "SELECT DISTINCT action FROM audit_logs WHERE action LIKE 'document.annotate%'")}
    rows.append({"step": "auditActionsComplete", "ok": actions <= found, "found": sorted(found)})
    leak = db.query(
        "SELECT action, metadata_json FROM audit_logs WHERE action LIKE 'document.annotate%' AND ("
        "metadata_json LIKE '%押金退还%' OR metadata_json LIKE '%交付时间需明确%' OR metadata_json LIKE '%借款金额%')")
    rows.append({"step": "auditWithoutAnnotationText", "ok": not leak, "hits": [r["action"] for r in leak]})
    _ann_case(report, "isolationAudit", case_a["id"], doc_a)
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m3_annotations",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "local_deterministic",
        "gaps": [
            "批注在 DOCX/PDF 导出中的呈现（C-007 导出回读）属导出链路，由 m3_docgen/m3_contract 覆盖范围跟踪",
        ],
    }
    try:
        ensure_workspace(str(tmp / "ws"))
        report.pop("_seq", None)

        g1 = _seg(report, "lifecycle", _run_lifecycle)
        g2 = _seg(report, "quoteAnchor", _run_quote_anchor, tmp)
        g3 = _seg(report, "isolationAudit", _run_isolation_audit)
        report.pop("_seq", None)

        gates = {
            "lifecycleDoc010": g1,
            "quoteAnchorQ009Q010": g2,
            "isolationCascadeAudit": g3,
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · 文档页批注（{r['mode']}） ===")
    for seg, title in (("lifecycle", "批注生命周期与校验(DEV-DOC-010)"),
                       ("quoteAnchor", "quote 锚定与重定位(Q-009/Q-010)"),
                       ("isolationAudit", "案件隔离/级联/审计(REQ-CASE-006)")):
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
    print(f"结论：{'M3 批注评测通过' if r.get('passed') else 'M3 批注评测未通过（见上方 ✗ 项）'}")
    if r.get("gaps"):
        print("范围说明与已知缺口：")
        for g in r["gaps"]:
            print(f"  · {g}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M3 文档页批注评测（确定性部分）")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_annotations_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
