# -*- coding: utf-8 -*-
"""M3 评测器：真实 Claim—Quote—Source 校验与原文无损续读（05-阅卷Agent升级方案 §10.1 A3/A4/A6/A7）。

用法（services/core 目录下）：
    python -m evals.m3_evidence [--keep]

覆盖（不依赖模型与外部服务，全程本地确定性）：
1. 引用校验（§6.1 / A3 / A4）：
   - 页面存在但结论文本没有可定位的短引 → needs_human(QUOTE_NOT_LOCATABLE)，不得 supported；
   - 只有《文档名》没有页码 → needs_human(PAGE_NOT_CITED)，不得当作精确引文；
   - 引文只存在于其他页 → QUOTE_MISMATCH；引文只存在于其他文档/版本 → QUOTE_MISMATCH；
   - 低 OCR 置信度页 → LOW_OCR_CONFIDENCE；
   - 空白/断行/全角空格差异可通过，实质文字差异不通过；
   - 跨案件文档名 → 待核实且不建引用；页码越界 → 待核实；
   - 文档删除后引用来源不可定位，不再显示为 supported。
2. 无损续读（§6.2 / A6 / A7）：
   - 长页按 nextCursor 续读，多次拼接与原始页文本逐字符一致；
   - 多页总量受限时后续页仍可读，结果 JSON 恒定可解析；
   - 空页/解析失败页可见（empty 标记），不被静默跳过；
   - charOffset 越界、重复游标、页码范围无内容有明确行为；
   - 工具层 document_get_pages 只允许读取本案或显式关联案件的材料。

报告：控制台摘要 + evals/reports/m3_evidence_<ts>.json。门槛：全部 gates 通过。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

# 与实现保持一致的分页上限（评测断言的是协议行为，不硬编码具体数值时从此处读取）
PAGE_TEXT_LIMIT = 2400
TOTAL_PAGE_READ_CHARS = 6000


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
# 夹具：真实文档 / 版本 / 页（引用校验与分页都依赖可核验的原文）
# ---------------------------------------------------------------------------

def _seed_doc(
    case_id: str,
    doc_id: str,
    title: str,
    pages: dict[int, str],
    *,
    quality: str = "native",
    ocr_confidence: float | None = None,
) -> str:
    """造一份可核验来源的文档，返回 active version id。"""
    from lawbuddy.storage import get_db

    db = get_db()
    ts = "2026-10-28T00:00:00+00:00"
    version_id = f"ver-{doc_id}"
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO documents(id, case_id, kind, title, status, active_version_id, "
            "created_at, updated_at) VALUES (?, ?, 'md', ?, 'ready', ?, ?, ?)",
            (doc_id, case_id, title, version_id, ts, ts),
        )
        conn.execute(
            "INSERT OR REPLACE INTO document_versions(id, document_id, blob_uri, sha256, size_bytes, "
            "parse_status, page_count, created_at) VALUES (?, ?, ?, ?, 0, 'ready', ?, ?)",
            (version_id, doc_id, f"blob://{doc_id}", "0" * 64, len(pages), ts),
        )
        for page_no, text in pages.items():
            confidence = ocr_confidence if quality == "ocr" else None
            conn.execute(
                "INSERT OR REPLACE INTO document_pages(id, version_id, page_no, text_content, "
                "text_quality, ocr_status, ocr_confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"{version_id}-p{page_no}", version_id, page_no, text, quality,
                 "done" if quality == "ocr" else "none", confidence),
            )
    return version_id


def _pending_text(result: dict[str, Any]) -> str:
    return " | ".join(result.get("pending") or [])


def _seed_old_version(doc_id: str, version_id: str, pages: dict[int, str]) -> None:
    """为既有文档追加一个非活跃版本（用于「引文只在旧版本」的错版本断言）。"""
    from lawbuddy.storage import get_db

    db = get_db()
    ts = "2026-10-28T00:00:00+00:00"
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO document_versions(id, document_id, blob_uri, sha256, size_bytes, "
            "parse_status, page_count, created_at) VALUES (?, ?, ?, ?, 0, 'ready', ?, ?)",
            (version_id, doc_id, f"blob://{version_id}", "0" * 64, len(pages), ts),
        )
        for page_no, text in pages.items():
            conn.execute(
                "INSERT OR REPLACE INTO document_pages(id, version_id, page_no, text_content, "
                "text_quality, ocr_status, ocr_confidence) VALUES (?, ?, ?, ?, 'native', 'none', NULL)",
                (f"{version_id}-p{page_no}", version_id, page_no, text),
            )


def _claims_of(task_id: str) -> list[dict[str, Any]]:
    from lawbuddy.citations import list_claims

    return list_claims(task_id=task_id)


# ---------------------------------------------------------------------------
# 段 1：引用校验 —— 真实短引与失败原因（§6.1）
# ---------------------------------------------------------------------------

def _run_quote_verification(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.tasks import create_task
    from lawbuddy.agent.verify import verify_answer
    from lawbuddy.cases import create_case

    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-引用校验", case_type="civil", client_name="甲", client_role="原告代理人")
    cid = case["id"]
    _seed_doc(cid, "doc-loan", "借款合同", {
        1: "第一条 甲方王建国向乙方李四出借人民币二十万元整，借款期限一年。",
        2: "第三条 利息按月息一分五计算，逾期按日万分之五计收违约金。",
        3: "第五条 双方确认款项已通过银行转账交付，收款账户为招商银行。",
    })
    _seed_doc(cid, "doc-receipt", "收据", {1: "今收到王建国交付的借款人民币二十万元整。"})
    _seed_doc(cid, "doc-scan", "扫描件笔录", {1: "被告陈述：款项系赠与而非借款。"},
              quality="ocr", ocr_confidence=0.62)
    task = create_task(case_id=cid, mode="legal_review", request_text="引用校验评测")
    tid = task["id"]

    # 1) 正确短引 → supported
    answer_ok = "双方约定借款二十万元。《借款合同》第1页，原文「出借人民币二十万元整」。"
    r_ok = verify_answer(tid, cid, answer_ok)
    rows.append({"step": "explicitQuoteSupported",
                 "ok": r_ok["total"] == 1 and r_ok["supported"] == 1 and not r_ok["pending"]
                 and r_ok["cites"][0].get("claimStatus") == "supported"
                 and r_ok["cites"][0].get("quote"),
                 "total": r_ok["total"], "supported": r_ok["supported"],
                 "claimStatus": r_ok["cites"][0].get("claimStatus") if r_ok["cites"] else None})

    # 2) 页面存在但结论没给可定位短引 → needs_human(QUOTE_NOT_LOCATABLE)
    answer_no_quote = "本案借款关系成立，双方已达成合意。《借款合同》第1页。"
    r_no_quote = verify_answer(tid, cid, answer_no_quote)
    statuses = {c.get("reasonCode") for c in r_no_quote["cites"]}
    rows.append({"step": "noQuoteNotSupported",
                 "ok": r_no_quote["supported"] == 0 and "QUOTE_NOT_LOCATABLE" in statuses
                 and r_no_quote["pending"],
                 "supported": r_no_quote["supported"], "reasonCodes": sorted(statuses),
                 "pending": _pending_text(r_no_quote)[:80]})

    # 3) 只有《文档名》→ PAGE_NOT_CITED（旧格式不得当作精确引文）
    r_doc_only = verify_answer(tid, cid, "收款账户信息见《收据》，可与转账流水对照。")
    codes = {c.get("reasonCode") for c in r_doc_only["cites"]}
    rows.append({"step": "docOnlyNeedsPage",
                 "ok": r_doc_only["supported"] == 0 and "PAGE_NOT_CITED" in codes,
                 "reasonCodes": sorted(codes)})

    # 3a) 卷宗材料清单式罗列 → 不算引证、不进待核实（避免噪音淹没真实缺口）
    catalog_answer = (
        "本次阅卷覆盖以下材料：\n"
        "- 《借款合同》\n"
        "- 《收据》\n"
        "- 《扫描件笔录》\n"
        "以上材料已分层标注。"
    )
    r_catalog = verify_answer(tid, cid, catalog_answer)
    rows.append({"step": "catalogNotCountedAsPending",
                 "ok": r_catalog["total"] == 0 and not r_catalog["pending"]
                 and not r_catalog["cites"] and len(r_catalog.get("mentions") or []) == 3,
                 "total": r_catalog["total"], "pending": len(r_catalog["pending"]),
                 "mentions": [m["title"] for m in r_catalog.get("mentions") or []]})

    # 3b) 一行并列多个文档名且无断言 → 同样视为清单
    r_inline_catalog = verify_answer(
        tid, cid, "卷宗包含《借款合同》《收据》《扫描件笔录》三类材料。"
    )
    rows.append({"step": "inlineCatalogNotCounted",
                 "ok": not r_inline_catalog["pending"],
                 "pending": len(r_inline_catalog["pending"]),
                 "mentions": len(r_inline_catalog.get("mentions") or [])})

    # 3b-2) 同行带断言（「载明」）时不得被当成清单放过
    r_inline_assert = verify_answer(
        tid, cid, "《借款合同》《收据》均载明借款二十万元这一事实。"
    )
    rows.append({"step": "inlineAssertionStillChecked",
                 "ok": r_inline_assert["pending"],
                 "pending": len(r_inline_assert["pending"])})

    # 3c) 漏写页码但结论句子在该文档内逐字唯一命中 → 系统补定页码并核验为 supported；
    #     补定的页码必须来自原文，不是猜测（pageLocatedBySystem 标记可见）。
    r_located = verify_answer(
        tid, cid, "双方确认款项已通过银行转账交付，收款账户为招商银行。《借款合同》"
    )
    rows.append({"step": "pageLocatedByUniqueQuote",
                 "ok": r_located["supported"] == 1 and not r_located["pending"]
                 and r_located["cites"][0].get("pageNo") == 3
                 and r_located["cites"][0].get("pageLocatedBySystem") is True,
                 "supported": r_located["supported"],
                 "pageNo": r_located["cites"][0].get("pageNo") if r_located["cites"] else None})

    # 3d) 该句子在文档内多页重复 → 不猜页码，仍标待核实
    _seed_doc(cid, "doc-dup", "重复条款合同", {
        1: "第八条 双方确认争议提交杭州市仲裁委员会仲裁解决。",
        2: "第八条 双方确认争议提交杭州市仲裁委员会仲裁解决（补充版）。",
    })
    r_dup = verify_answer(tid, cid, "双方确认争议提交杭州市仲裁委员会仲裁解决。《重复条款合同》")
    rows.append({"step": "ambiguousPageNotGuessed",
                 "ok": r_dup["supported"] == 0 and "PAGE_NOT_CITED" in
                 {c.get("reasonCode") for c in r_dup["cites"]},
                 "reasonCodes": sorted({c.get("reasonCode") for c in r_dup["cites"]}),
                 "pending": _pending_text(r_dup)[:80]})

    # 4) 引文只存在于其他页 → QUOTE_MISMATCH
    r_wrong_page = verify_answer(
        tid, cid, "利息约定过高。《借款合同》第1页，原文「利息按月息一分五计算」。"
    )
    rows.append({"step": "quoteOnOtherPageMismatch",
                 "ok": r_wrong_page["supported"] == 0
                 and any(c.get("reasonCode") == "QUOTE_MISMATCH" for c in r_wrong_page["cites"]),
                 "reasonCodes": sorted({c.get("reasonCode") for c in r_wrong_page["cites"]})})

    # 5) 引文只存在于其他文档 → QUOTE_MISMATCH（不得张冠李戴）
    r_wrong_doc = verify_answer(
        tid, cid, "款项已实际交付。《借款合同》第1页，原文「今收到王建国交付的借款人民币二十万元整」。"
    )
    rows.append({"step": "quoteFromOtherDocMismatch",
                 "ok": r_wrong_doc["supported"] == 0
                 and any(c.get("reasonCode") == "QUOTE_MISMATCH" for c in r_wrong_doc["cites"]),
                 "reasonCodes": sorted({c.get("reasonCode") for c in r_wrong_doc["cites"]})})

    # 6) 引文只存在于同一文档的旧版本 → QUOTE_MISMATCH（不得用旧版本内容冒充当前原文）
    _seed_old_version("doc-loan", "ver-doc-loan-v0", {
        1: "第一条 甲方王建国向乙方李四出借人民币十万元整，借款期限两年。",
    })
    r_old_ver = verify_answer(
        tid, cid, "借款金额十万元。《借款合同》第1页，原文「出借人民币十万元整，借款期限两年」。"
    )
    rows.append({"step": "quoteFromOldVersionMismatch",
                 "ok": r_old_ver["supported"] == 0
                 and any(c.get("reasonCode") == "QUOTE_MISMATCH" for c in r_old_ver["cites"]),
                 "reasonCodes": sorted({c.get("reasonCode") for c in r_old_ver["cites"]})})

    # 7) 低 OCR 置信度页 → LOW_OCR_CONFIDENCE（即使引文逐字一致）
    r_low_ocr = verify_answer(
        tid, cid, "被告否认借贷关系。《扫描件笔录》第1页，原文「款项系赠与而非借款」。"
    )
    rows.append({"step": "lowOcrNeedsHuman",
                 "ok": r_low_ocr["supported"] == 0
                 and any(c.get("reasonCode") == "LOW_OCR_CONFIDENCE" for c in r_low_ocr["cites"]),
                 "reasonCodes": sorted({c.get("reasonCode") for c in r_low_ocr["cites"]})})

    # 7) 规范化差异可通过；实质文字差异不通过
    r_ws = verify_answer(
        tid, cid, "借款期限一年。《借款合同》第1页，原文「出借人民币　二十万元整，\n借款期限一年」。"
    )
    r_typo = verify_answer(
        tid, cid, "借款期限两年。《借款合同》第1页，原文「出借人民币二十万元整，借款期限两年」。"
    )
    rows.append({"step": "normalizationPassAndDiffFail",
                 "ok": r_ws["supported"] == 1 and r_typo["supported"] == 0,
                 "normalizedSupported": r_ws["supported"], "typoSupported": r_typo["supported"]})

    # 8) 跨案件文档名 → 待核实且不建引用
    other = create_case(title="M3评测-引用校验他案", case_type="civil", client_name="乙",
                        client_role="被告代理人")
    _seed_doc(other["id"], "doc-other", "他案判决书", {1: "他案认定借款关系不成立。"})
    r_cross = verify_answer(tid, cid, "同类案件认定借款不成立。《他案判决书》第1页，原文「借款关系不成立」。")
    rows.append({"step": "crossCaseCitePending",
                 "ok": r_cross["supported"] == 0 and r_cross["total"] == 1 and not r_cross["cites"]
                 and "未在案件文档中找到该文档" in _pending_text(r_cross),
                 "pending": _pending_text(r_cross)[:80]})

    # 9) 页码越界 → 待核实
    r_page_over = verify_answer(tid, cid, "合同另有附件。《借款合同》第9页，原文「附件清单」。")
    rows.append({"step": "pageOutOfRangePending",
                 "ok": r_page_over["supported"] == 0 and "页码超出文档范围" in _pending_text(r_page_over),
                 "pending": _pending_text(r_page_over)[:80]})

    # 10) Claim 落库与来源绑定：cites 必须带 claimId 与逐字 quote（前端角标数据源）
    all_claims = _claims_of(tid)
    rows.append({"step": "claimsPersistedWithQuoteSource",
                 "ok": len(all_claims) >= 5
                 and all(c["status"] in ("supported", "needs_human") for c in all_claims)
                 and any(c["status"] == "supported" and c["confidenceBasis"] == "exact_quote"
                         for c in all_claims),
                 "claims": len(all_claims),
                 "byStatus": {s: sum(1 for c in all_claims if c["status"] == s)
                              for s in {c["status"] for c in all_claims}}})

    report["quote"] = {"caseId": cid, "taskId": tid}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 2：来源失效 —— 文档删除后不再 supported
# ---------------------------------------------------------------------------

def _run_source_invalidation(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.tasks import create_task
    from lawbuddy.agent.verify import verify_answer
    from lawbuddy.cases import create_case
    from lawbuddy.documents import delete_document
    from lawbuddy.storage import get_db

    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-来源失效", case_type="civil", client_name="丙",
                       client_role="原告代理人")
    cid = case["id"]
    _seed_doc(cid, "doc-del", "送货单", {1: "今收到货物办公设备一批，共计人民币三万元整。"})
    task = create_task(case_id=cid, mode="legal_review", request_text="来源失效评测")
    tid = task["id"]

    answer = "货款为三万元。《送货单》第1页，原文「共计人民币三万元整」。"
    before = verify_answer(tid, cid, answer)
    claim_id = before["cites"][0].get("claimId") if before["cites"] else None
    rows.append({"step": "supportedBeforeDelete",
                 "ok": before["supported"] == 1 and bool(claim_id),
                 "supported": before["supported"], "claimId": bool(claim_id)})

    deleted = delete_document("doc-del")
    db = get_db()
    stale = db.query_one("SELECT status, reason_code FROM claims WHERE id = ?", (claim_id,)) if claim_id else None
    rows.append({"step": "claimStaleAfterDelete",
                 "ok": deleted["deleted"] and stale is not None and stale["status"] == "stale"
                 and stale["reason_code"] == "DOCUMENT_DELETED",
                 "status": stale["status"] if stale else None,
                 "reasonCode": stale["reason_code"] if stale else None})

    after = verify_answer(tid, cid, answer)
    rows.append({"step": "noSupportedAfterDelete",
                 "ok": after["supported"] == 0 and bool(after["pending"]),
                 "supported": after["supported"], "pending": _pending_text(after)[:80]})

    report["invalidation"] = {"caseId": cid, "taskId": tid}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 3：无损续读（纯分页协议，不触 DB）
# ---------------------------------------------------------------------------

def _run_pagination(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.autonomous import PAGE_TEXT_LIMIT, _paginate_pages

    rows: list[dict[str, Any]] = []
    long_text = ("第一段：双方确认借款金额为人民币二十万元整。\n"
                 "第二段：还款期限为二〇二五年十二月三十一日。\n") * 120
    pages = [
        {"pageNo": 1, "text": long_text, "quality": "native", "printedPage": "1"},
        {"pageNo": 2, "text": "第二页：担保人张伟承担连带保证责任。", "quality": "native", "printedPage": "2"},
    ]

    # 1) 长页续读拼接与原始页文本逐字符一致
    chunks: list[str] = []
    cursor: dict[str, int] | None = {"pageNo": 1, "charOffset": 0}
    reads = 0
    while cursor is not None and reads < 40:
        out = _paginate_pages("doc-long", pages, total_pages=len(pages),
                              char_offset=cursor["charOffset"],
                              max_chars=PAGE_TEXT_LIMIT)
        reads += 1
        if out.get("error"):
            break
        chunks.extend(str(p.get("text") or "") for p in out["pages"] if p.get("pageNo") == 1)
        cursor = out.get("nextCursor")
        if out["pages"] and out["pages"][-1].get("pageNo") != 1:
            break
    joined = "".join(chunks)
    rows.append({"step": "longPageResumesToEnd",
                 "ok": joined == long_text and reads > 1,
                 "pageChars": len(long_text), "readChars": len(joined), "reads": reads,
                 "charAccurate": joined == long_text})

    # 2) 单次返回量受 maxChars 约束且 JSON 可解析（结构化限量而非事后截断）
    one = _paginate_pages("doc-long", pages, total_pages=len(pages), max_chars=PAGE_TEXT_LIMIT)
    serialized = json.dumps(one, ensure_ascii=False)
    rows.append({"step": "singleReadBounded",
                 "ok": one["returnedChars"] <= PAGE_TEXT_LIMIT and one["hasMore"] is True
                 and bool(one.get("nextCursor")) and json.loads(serialized)["hasMore"] is True,
                 "returnedChars": one["returnedChars"], "limit": PAGE_TEXT_LIMIT})

    # 3) 多页总量受限：后续页仍可读（有明确 nextCursor 指向）
    multi = _paginate_pages("doc-long", pages, total_pages=len(pages), max_chars=800)
    rows.append({"step": "multiPageCursorPointsNext",
                 "ok": multi["hasMore"] and multi["returnedChars"] <= 800
                 and multi["nextCursor"]["pageNo"] in (1, 2),
                 "returnedChars": multi["returnedChars"], "nextCursor": multi.get("nextCursor")})

    # 4) 空页可见（empty 标记 + 提示不得据空页断言内容）
    empty_pages = [
        {"pageNo": 1, "text": "", "quality": "empty", "printedPage": None},
        {"pageNo": 2, "text": "第二页有正文。", "quality": "native", "printedPage": "2"},
    ]
    empty_out = _paginate_pages("doc-empty", empty_pages, total_pages=2, max_chars=PAGE_TEXT_LIMIT)
    rows.append({"step": "emptyPageVisible",
                 "ok": [p.get("pageNo") for p in empty_out["pages"]] == [1, 2]
                 and empty_out["pages"][0].get("empty") is True
                 and "空" not in str(empty_out["pages"][0].get("text") or "")
                 and "empty" in str(empty_out.get("note") or ""),
                 "pageNos": [p.get("pageNo") for p in empty_out["pages"]],
                 "note": str(empty_out.get("note") or "")[:60]})

    # 5) charOffset 越界：跳到下一页而不是返回空/报错丢内容
    over = _paginate_pages("doc-long", pages, total_pages=len(pages),
                           char_offset=10 ** 6, max_chars=PAGE_TEXT_LIMIT)
    rows.append({"step": "charOffsetOverflowSkipsPage",
                 "ok": [p.get("pageNo") for p in over["pages"]] == [2]
                 and over["pages"][0]["text"] == pages[1]["text"],
                 "pageNos": [p.get("pageNo") for p in over["pages"]]})

    # 6) 全部为空/无文本：不制造假游标，明确说明该范围没有可读文本
    blank = _paginate_pages("doc-blank", [{"pageNo": 4, "text": "", "quality": "empty"}],
                            total_pages=1, char_offset=10 ** 6)
    rows.append({"step": "allEmptyPageExplained",
                 "ok": blank["hasMore"] is False and not blank.get("nextCursor")
                 and [p.get("pageNo") for p in blank["pages"]] == [4]
                 and blank["pages"][0].get("empty") is True
                 and "empty" in str(blank.get("note") or ""),
                 "pageNos": [p.get("pageNo") for p in blank["pages"]],
                 "hasMore": blank["hasMore"], "note": str(blank.get("note") or "")[:60]})

    # 7) 边界字符（换行、全角空格、多字节）切片无损
    boundary_text = "甲\u3000乙\r\n丙—丁…戊" * 40
    b_pages = [{"pageNo": 1, "text": boundary_text, "quality": "native"}]
    b_chunks: list[str] = []
    b_cursor = 0
    guard = 0
    while guard < 60:
        guard += 1
        out = _paginate_pages("doc-boundary", b_pages, total_pages=1,
                              char_offset=b_cursor, max_chars=137)
        if out.get("error"):
            break
        b_chunks.append(out["pages"][0]["text"])
        if not out["hasMore"]:
            break
        b_cursor = out["nextCursor"]["charOffset"]
    rows.append({"step": "boundaryCharsAccurate",
                 "ok": "".join(b_chunks) == boundary_text and guard > 1,
                 "chars": len(boundary_text), "reads": guard})

    # 8) 重复游标幂等：同一请求两次结果一致，不得重复消费或跳页
    again = _paginate_pages("doc-long", pages, total_pages=len(pages), max_chars=800)
    rows.append({"step": "repeatedCursorIdempotent",
                 "ok": json.dumps(again, ensure_ascii=False) == json.dumps(multi, ensure_ascii=False)})

    report["pagination"] = {"pageChars": len(long_text), "reads": reads}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 4：工具层无损续读与读取范围门禁
# ---------------------------------------------------------------------------

def _run_tool_reading(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.autonomous import _execute_tool
    from lawbuddy.cases import create_case

    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-工具读原文", case_type="civil", client_name="丁",
                       client_role="原告代理人")
    cid = case["id"]
    long_text = "".join(f"第{i}段：银行流水显示交付{1000 + i}元。\n" for i in range(1, 260))
    _seed_doc(cid, "doc-stream", "银行流水", {
        1: long_text,
        2: "第二页：账户余额为人民币零元。",
        3: "",
    })
    other = create_case(title="M3评测-工具读原文他案", case_type="civil", client_name="戊",
                        client_role="被告代理人")
    _seed_doc(other["id"], "doc-foreign", "他案材料", {1: "他案秘密材料：交付五百万元。"})
    state = {"case_id": cid, "mode": "legal_review"}

    # 1) 工具层续读：按 nextCursor 反复读取，拼接与页文本一致
    #    显式缩小 maxChars，确保单页也必须多次续读才能读完（默认上限下一页就读完，测不出续读）
    collected: list[str] = []
    cursor = {"pageFrom": 1, "pageTo": 1, "charOffset": 0, "maxChars": 2000}
    guard = 0
    while guard < 40:
        guard += 1
        out = _execute_tool(state, "document_get_pages", {
            "documentId": "doc-stream", **cursor,
        })
        assert isinstance(out, dict), out
        if out.get("error"):
            break
        collected.append("".join(str(p.get("text") or "") for p in out["pages"]))
        if not out.get("hasMore"):
            break
        nxt = out.get("nextCursor") or {}
        cursor = {"pageFrom": nxt.get("pageNo"), "pageTo": nxt.get("pageNo"),
                  "charOffset": nxt.get("charOffset"), "maxChars": 2000}
    rows.append({"step": "toolResumeMatchesPage",
                 "ok": "".join(collected) == long_text and guard > 1,
                 "pageChars": len(long_text), "readChars": sum(len(c) for c in collected),
                 "reads": guard})

    # 2) 序列化后的工具结果始终可解析（结构化限量，不靠事后截断）
    single = _execute_tool(state, "document_get_pages", {"documentId": "doc-stream", "pageFrom": 1, "pageTo": 3})
    rows.append({"step": "toolResultSerializable",
                 "ok": isinstance(json.loads(json.dumps(single, ensure_ascii=False)), dict)
                 and single["returnedChars"] <= 6000,
                 "returnedChars": single["returnedChars"],
                 "pageNos": [p.get("pageNo") for p in single["pages"]]})

    # 3) 空页在工具结果中可见并带说明
    rows.append({"step": "toolEmptyPageVisible",
                 "ok": any(p.get("pageNo") == 3 and p.get("empty") for p in single["pages"])
                 and "empty" in str(single.get("note") or ""),
                 "pageNos": [p.get("pageNo") for p in single["pages"]],
                 "note": str(single.get("note") or "")[:60]})

    # 4) 跨案件 documentId → 明确拒绝，不返回他案原文
    foreign = _execute_tool(state, "document_get_pages", {"documentId": "doc-foreign"})
    rows.append({"step": "foreignDocumentRejected",
                 "ok": bool(foreign.get("error")) and "不在当前案件的可读材料范围内" in str(foreign["error"])
                 and "pages" not in foreign,
                 "error": str(foreign.get("error") or "")[:70]})

    # 5) 文档标题误当 ID → 报错并给出真实 ID，不静默返回空
    by_title = _execute_tool(state, "document_get_pages", {"documentId": "银行流水"})
    rows.append({"step": "titleAsIdGuided",
                 "ok": bool(by_title.get("error")) and "doc-stream" in str(by_title["error"]),
                 "error": str(by_title.get("error") or "")[:80]})

    # 6) 页码范围无内容 → 明确报错并给出总页数
    out_of_range = _execute_tool(state, "document_get_pages",
                                 {"documentId": "doc-stream", "pageFrom": 20, "pageTo": 25})
    rows.append({"step": "pageRangeEmptyGuided",
                 "ok": bool(out_of_range.get("error")) and out_of_range.get("totalPages") == 3,
                 "error": str(out_of_range.get("error") or "")[:70]})

    report["toolReading"] = {"caseId": cid}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m3_evidence",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "local_deterministic",
    }
    try:
        ensure_workspace(str(tmp / "ws"))

        g1 = _seg(report, "quoteVerification", _run_quote_verification)
        g2 = _seg(report, "sourceInvalidation", _run_source_invalidation)
        g3 = _seg(report, "pagination", _run_pagination)
        g4 = _seg(report, "toolReading", _run_tool_reading)

        gates = {
            "realQuoteClaimA3A4": g1,
            "sourceStaleAfterDelete": g2,
            "losslessPaginationA6A7": g3,
            "toolReadingScopeAndResume": g4,
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · 引用校验与无损续读（{r['mode']}） ===")
    for seg, title in (("quoteVerification", "真实短引校验(A3/A4)"),
                       ("sourceInvalidation", "删除后来源失效"),
                       ("pagination", "分页无损续读(A6/A7)"),
                       ("toolReading", "工具读原文与范围门禁")):
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
    print(f"结论：{'M3 引用与续读评测通过' if r.get('passed') else 'M3 引用与续读评测未通过（见上方 ✗ 项）'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M3 引用校验与原文无损续读评测")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_evidence_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
