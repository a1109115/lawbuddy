# -*- coding: utf-8 -*-
"""M3 评测器：记忆分层与生命周期（03 清单 §6.2 M-001~M-006 / DEV-AGT-007）。

用法（services/core 目录下）：
    python -m evals.m3_memory [--keep]

覆盖：
1. M-001 分层隔离：案件事实不能进入全局用户偏好（结构性守门），案件记忆强制 case_id 过滤；
2. M-002 记忆来源：案件记忆保存来源文档、版本、页码、原文片段与状态；
3. M-003 来源失效：删除文档后相关记忆立即转为 stale，不再作为有效事实召回；
4. M-004 用户更正：原值保留（superseded 行）、新值生效、后续召回使用新值；
5. M-005 删除：SQLite 状态位与 FTS 索引同时清理，不再可召回；
6. M-006 召回质量：案件金标集 Recall@10 ≥ 0.90，跨案件错误为 0；
7. 会话工作记忆随任务终态清理；用户偏好跨案件可见但无案件事实。

报告：控制台摘要 + evals/reports/m3_memory_<ts>.json。M3 门槛：全部 gates 通过。
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

# 召回质量门槛（M-006）
RECALL_THRESHOLD = 0.90

# 金标集：caseA 写入的（键, 值）与对应检索词；检索词必须能召回目标记忆
GOLD: list[tuple[str, str, str]] = [
    ("王建国", "原告：出借人", "王建国"),
    ("2025年3月1日", "双方签订借款合同", "借款合同"),
    ("招商银行流水", "转账凭证，显示交付十万元", "转账凭证"),
    ("争议焦点：借款合意", "被告主张款项为赠与", "赠与"),
    ("送达地址确认", "被告确认送达地址为户籍地", "送达地址"),
    ("担保人张伟", "承担连带保证责任", "连带保证"),
    ("催收记录", "2025年6月电话催收", "电话催收"),
    ("利息约定", "月息一分五，超过法定上限", "月息"),
    ("还款计划", "分三期归还本金", "分三期"),
    ("证人证言", "邻居证明双方存在借贷关系", "证人证言"),
]


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


def _seed_doc(case_id: str, doc_id: str, pages: dict[int, str]) -> str:
    """造一份可核验来源的文档（documents + document_versions + document_pages）。

    05 方案 §6.3 后案件事实类记忆必须带能通过 verify_quote 的原文来源，
    评测不能再靠伪造 versionId/pageNo 过关。
    """
    from lawbuddy.storage import get_db

    db = get_db()
    ts = "2026-09-20T00:00:00+00:00"
    version_id = f"ver-{doc_id}"
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO documents(id, case_id, kind, title, status, active_version_id, "
            "created_at, updated_at) VALUES (?, ?, 'md', ?, 'ready', ?, ?, ?)",
            (doc_id, case_id, f"评测来源文档-{doc_id}", version_id, ts, ts),
        )
        conn.execute(
            "INSERT OR REPLACE INTO document_versions(id, document_id, blob_uri, sha256, size_bytes, "
            "parse_status, page_count, created_at) VALUES (?, ?, ?, ?, 0, 'ready', ?, ?)",
            (version_id, doc_id, f"blob://{doc_id}", "0" * 64, len(pages), ts),
        )
        for page_no, text in pages.items():
            conn.execute(
                "INSERT OR REPLACE INTO document_pages(id, version_id, page_no, text_content, "
                "text_quality, ocr_status) VALUES (?, ?, ?, ?, 'native', 'none')",
                (f"{version_id}-p{page_no}", version_id, page_no, text),
            )
    return version_id


# ---------------------------------------------------------------------------
# 段 1：分层隔离与结构性守门（M-001）
# ---------------------------------------------------------------------------

def _run_layering(report: dict[str, Any], memory: Any) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case

    rows: list[dict[str, Any]] = []
    case_a = create_case(title="M3评测-记忆A", case_type="civil", client_name="王建国", client_role="原告代理人")
    case_b = create_case(title="M3评测-记忆B", case_type="civil", client_name="李四", client_role="被告代理人")
    a, b = case_a["id"], case_b["id"]
    report["cases"] = {"a": a, "b": b}
    seed_version = _seed_doc(a, "doc-m3-a", {
        1: "王建国，原告，出借人。双方于2025年3月1日签订借款合同。",
    })
    report["seedVersion"] = seed_version

    # 案件事实记忆写入 A（必须带可核验来源，05 方案 §6.3）
    m = memory.remember(scope="case", kind="person", key="王建国", value="原告：出借人", case_id=a,
                        source={"documentId": "doc-m3-a", "pageNo": 1, "quote": "原告，出借人"})
    rows.append({"step": "caseMemoryWritten", "ok": m["status"] == "active"})

    # 事实类无来源 → 拒绝；note 无来源 → 允许（工作记录不伪装成原文支持）
    rejected_no_source = False
    try:
        memory.remember(scope="case", kind="fact", key="借款金额", value="十万元", case_id=a)
    except memory.MemoryError:
        rejected_no_source = True
    rows.append({"step": "factRequiresSource", "ok": rejected_no_source})

    # 伪造引文（页内不存在）→ 拒绝
    rejected_bad_quote = False
    try:
        memory.remember(scope="case", kind="fact", key="借款金额", value="十万元", case_id=a,
                        source={"documentId": "doc-m3-a", "pageNo": 1, "quote": "出借二十万元"})
    except memory.MemoryError:
        rejected_bad_quote = True
    rows.append({"step": "factRejectsUnverifiableQuote", "ok": rejected_bad_quote})

    note = memory.remember(scope="case", kind="note", key="待核实：交付方式", value="待补充银行流水", case_id=a)
    rows.append({"step": "noteWithoutSourceAllowed", "ok": note["status"] == "active"
                 and not note.get("source")})

    # 用户偏好：不得携带案件标识，且 kind 限定为偏好类
    rejected_case_id = False
    try:
        memory.remember(scope="user", kind="preference", key="语言", value="中文", case_id=a)
    except memory.MemoryError:
        rejected_case_id = True
    rows.append({"step": "userScopeRejectsCaseId", "ok": rejected_case_id})

    rejected_fact_kind = False
    try:
        memory.remember(scope="user", kind="person", key="王建国", value="原告：出借人")
    except memory.MemoryError:
        rejected_fact_kind = True
    rows.append({"step": "userScopeRejectsCaseFactKind", "ok": rejected_fact_kind})

    # 合法用户偏好可写入，且跨案件可见；但其中不出现案件事实
    memory.remember(scope="user", kind="preference", key="语言", value="中文")
    user_prefs = memory.list_memories(scope="user")
    prefs_ok = any(p["key"] == "语言" for p in user_prefs) and all(p["caseId"] is None for p in user_prefs)
    rows.append({"step": "userPreferenceWritable", "ok": prefs_ok, "prefs": len(user_prefs)})

    # 案件记忆必须在 case 层：B 案件无法召回 A 的记忆
    leaked = memory.recall(case_id=b, query="王建国")
    case_memories = memory.list_memories(case_id=a, scope="case")
    rows.append({"step": "crossCaseRecallZero", "ok": len(leaked) == 0 and len(case_memories) >= 1,
                 "leaked": len(leaked)})

    # case 层召回必须带 case_id（模型不可绕过）
    rejected_no_case = False
    try:
        memory.recall(case_id="", query="王建国")
    except memory.MemoryError:
        rejected_no_case = True
    rows.append({"step": "recallRequiresCaseId", "ok": rejected_no_case})

    report["layering"] = {"caseA": a, "caseB": b}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 2：来源记录与召回质量（M-002 / M-006）
# ---------------------------------------------------------------------------

def _run_source_recall(report: dict[str, Any], memory: Any) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    a = report["cases"]["a"]

    # 构造带来源的记忆（金标集）：来源文档/版本/页码/原文片段均可核验
    page_text = "。".join(f"{key}：{value}" for key, value, _ in GOLD) + "。"
    version_id = _seed_doc(a, "doc-m3-gold", {1: page_text})
    for key, value, _ in GOLD:
        memory.remember(
            scope="case", kind="note", key=key, value=value, case_id=a,
            source={"documentId": "doc-m3-gold", "versionId": version_id, "pageNo": 1,
                    "quote": f"{key}：{value}"},
        )

    entries = memory.list_memories(case_id=a, scope="case")
    with_source = [e for e in entries if e.get("source") and e["source"].get("documentId")
                   and e["source"].get("pageNo") is not None and e["source"].get("quote")]
    rows.append({"step": "sourcePersisted", "ok": len(with_source) >= len(GOLD),
                 "withSource": len(with_source)})

    # 召回质量：Recall@10（每条金标检索词召回目标记忆）
    hit = 0
    miss: list[str] = []
    for key, _value, query in GOLD:
        found = memory.recall(case_id=a, query=query, limit=10)
        if any(e["key"] == key for e in found):
            hit += 1
        else:
            miss.append(query)
    recall = hit / len(GOLD)
    rows.append({"step": "recallAt10", "ok": recall >= RECALL_THRESHOLD,
                 "recall": round(recall, 3), "threshold": RECALL_THRESHOLD, "miss": miss})

    # 短查询（<3 字符）回退 LIKE 亦可召回
    short = memory.recall(case_id=a, query="王建", limit=5)
    rows.append({"step": "shortQueryFallback", "ok": any(e["key"] == "王建国" for e in short)})

    report["recall"] = {"gold": len(GOLD), "hit": hit, "recallAt10": round(recall, 3)}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 3：更正与删除（M-004 / M-005）
# ---------------------------------------------------------------------------

def _run_correct_forget(report: dict[str, Any], memory: Any) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.storage import get_db

    db = get_db()
    rows: list[dict[str, Any]] = []
    a = report["cases"]["a"]

    original = memory.remember(scope="case", kind="person", key="担保方式（待核对）",
                               value="承担连带保证责任", case_id=a,
                               source={"documentId": "doc-m3-a", "pageNo": 1, "quote": "原告，出借人"})

    corrected = memory.correct(original["id"], "承担一般保证责任（更正后）", note="核对保证合同条款")
    old_row = db.query_one("SELECT status, value_text, superseded_by FROM memories WHERE id = ?", (original["id"],))
    recalled = memory.recall(case_id=a, query="更正后", limit=10)
    actives = [m for m in memory.list_memories(case_id=a) if m["status"] == "active"
               and m["key"] == "担保方式（待核对）"]

    rows.append({"step": "correctionKeepsOldValue", "ok": old_row["status"] == "superseded"
                 and old_row["superseded_by"] == corrected["id"]
                 and "连带保证责任" in old_row["value_text"]})
    # 新值生效：同键仅剩一条生效记忆且为新值，旧值不再被召回
    rows.append({"step": "correctionNewValueEffective",
                 "ok": len(actives) == 1 and "一般保证责任" in actives[0]["value"]
                 and any("一般保证责任" in e["value"] for e in recalled),
                 "activeValues": [m["value"] for m in actives]})

    # 旧值仍可追溯查询（列表包含 superseded 行）
    history = memory.list_memories(case_id=a)
    rows.append({"step": "supersededTraceable", "ok": any(e["id"] == original["id"] and e["status"] == "superseded"
                                                          for e in history)})

    # 审计留痕（不含原文，仅引用 id 与长度）
    audit_rows = db.query("SELECT action, metadata_json FROM audit_logs WHERE action = 'memory.correct'")
    rows.append({"step": "correctionAudited", "ok": len(audit_rows) >= 1})

    # M-005：删除后 SQLite 与 FTS 均不可召回
    target = memory.remember(scope="case", kind="note", key="待删除记忆", value="该内容删除后不应再被召回", case_id=a)
    before = memory.recall(case_id=a, query="删除后不应", limit=10)
    memory.forget(target["id"])
    after = memory.recall(case_id=a, query="删除后不应", limit=10)
    fts_hits = db.query_one("SELECT COUNT(*) AS n FROM memories_fts WHERE memory_id = ?", (target["id"],))["n"]
    row_state = db.query_one("SELECT status FROM memories WHERE id = ?", (target["id"],))
    rows.append({"step": "deletionExcludesRecall", "ok": len(before) >= 1 and len(after) == 0
                 and int(fts_hits) == 0 and row_state["status"] == "deleted",
                 "ftsRows": int(fts_hits)})

    report["correctForget"] = {"supersededId": corrected["supersededId"], "forgotId": target["id"]}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 4：来源失效与会话清理（M-003）
# ---------------------------------------------------------------------------

def _run_invalidation(report: dict[str, Any], memory: Any) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent.tasks import create_task
    from lawbuddy.cases import create_case
    from lawbuddy.documents import delete_document
    from lawbuddy.storage import get_db

    db = get_db()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-记忆失效", case_type="civil", client_name="赵六", client_role="原告代理人")
    cid = case["id"]

    # 造一条带来源文档的记忆（来源文档与页文本落库，保证来源可核验）
    doc_id = "eval-doc-memory"
    _seed_doc(cid, doc_id, {2: "招商银行流水：显示交付十万元。"})
    memory.remember(
        scope="case", kind="evidence", key="转账凭证", value="显示交付十万元", case_id=cid,
        source={"documentId": doc_id, "pageNo": 2, "quote": "交付十万元"},
    )
    hit_before = memory.recall(case_id=cid, query="交付十万元", limit=10)

    delete_document(doc_id)
    hit_after = memory.recall(case_id=cid, query="交付十万元", limit=10)
    stale_row = db.query_one(
        "SELECT status FROM memories WHERE source_document_id = ?", (doc_id,)
    )
    rows.append({"step": "sourceDeleteInvalidates", "ok": len(hit_before) == 1 and len(hit_after) == 0
                 and stale_row["status"] == "stale", "status": stale_row["status"]})
    rows.append({"step": "staleAudited", "ok": int(db.query_one(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'memory.stale'")["n"]) >= 1})

    # 会话工作记忆：任务终态后清理
    task = create_task(case_id=cid, mode="legal_review", request_text="记忆清理评测", model_policy={"remote": False})
    memory.remember(scope="session", kind="observation", key="当前计划", value="待核验的时间线", case_id=cid,
                    task_id=task["id"])
    active_before = [m for m in memory.list_memories(case_id=cid, scope="session") if m["status"] == "active"]
    from lawbuddy.agent.tasks import update_lifecycle

    update_lifecycle(task["id"], "COMPLETED")
    active_after = [m for m in memory.list_memories(case_id=cid, scope="session") if m["status"] == "active"]
    rows.append({"step": "sessionClearedOnTerminal", "ok": len(active_before) == 1 and len(active_after) == 0,
                 "before": len(active_before), "after": len(active_after)})

    report["invalidation"] = {"documentId": doc_id, "taskId": task["id"]}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 5：报告入记忆（M-002：只入有来源条目）
# ---------------------------------------------------------------------------

def _run_ingest(report: dict[str, Any], memory: Any) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case

    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-记忆入库", case_type="civil", client_name="孙七", client_role="原告代理人")
    cid = case["id"]
    _seed_doc(cid, "doc-1", {1: "原告孙七，男，汉族。", 2: "2025年3月1日双方签订借款合同。"})
    _seed_doc(cid, "doc-2", {5: "被告主张讼争款项为赠与。"})
    body = {
        "report": {
            "parties": [
                {"name": "孙七", "role": "原告", "note": "出借人", "cite": {"documentId": "doc-1", "pageNo": 1,
                                                                          "quote": "原告孙七"}},
                {"name": "周八", "role": "被告", "cite": None},  # 无来源 → 不入库
            ],
            "timeline": [
                {"date": "2025年3月1日", "event": "签订借款合同", "cite": {"documentId": "doc-1", "pageNo": 2,
                                                                        "quote": "双方签订借款合同"}},
            ],
            "disputes": [
                {"title": "借款合意", "desc": "被告主张赠与", "cite": {"documentId": "doc-2", "pageNo": 5,
                                                                    "quote": "被告主张讼争款项为赠与"}},
                # 引文与该页不匹配 → 跳过，不写入“已支持”事实
                {"title": "交付事实", "desc": "款项已交付", "cite": {"documentId": "doc-2", "pageNo": 5,
                                                                  "quote": "原告当日转账二十万元"}},
            ],
        }
    }
    result = memory.ingest_report(cid, "task-eval", body)
    entries = memory.list_memories(case_id=cid, scope="case")
    kinds = sorted({e["kind"] for e in entries})
    no_source = [e for e in entries if not e.get("source")]
    rows.append({"step": "ingestSourcedOnly", "ok": result["written"] == 3 and result["skippedNoSource"] == 1,
                 "written": result["written"], "skipped": result["skippedNoSource"]})
    rows.append({"step": "ingestSkipsUnverifiedCite", "ok": result["skippedUnverified"] == 1,
                 "skippedUnverified": result["skippedUnverified"]})
    rows.append({"step": "ingestKinds", "ok": set(kinds) == {"person", "timeline", "dispute"}, "kinds": kinds})
    rows.append({"step": "noSourceNotStored", "ok": not no_source})

    # 幂等：重复入库不产生重复条目（坏引证条目每次都按跳过处理）
    again = memory.ingest_report(cid, "task-eval", body)
    rows.append({"step": "ingestIdempotent", "ok": again["written"] == 0 and again["duplicated"] == 3
                 and again["skippedUnverified"] == 1,
                 "duplicated": again["duplicated"]})

    report["ingest"] = result
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace
    from lawbuddy import memory

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m3_memory",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "local_deterministic",
    }
    try:
        ensure_workspace(str(tmp / "ws"))

        g1 = _seg(report, "layering", _run_layering, memory)
        g2 = _seg(report, "sourceRecall", _run_source_recall, memory)
        g3 = _seg(report, "correctForget", _run_correct_forget, memory)
        g4 = _seg(report, "invalidation", _run_invalidation, memory)
        g5 = _seg(report, "ingest", _run_ingest, memory)

        gates = {
            "layeringIsolationM001": g1,
            "sourceAndRecallM002M006": g2,
            "correctForgetM004M005": g3,
            "sourceInvalidationM003": g4,
            "reportIngestSourcedOnly": g5,
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · 记忆分层与生命周期（{r['mode']}） ===")
    for seg, title in (("layering", "分层与隔离(M-001)"), ("sourceRecall", "来源与召回质量(M-002/M-006)"),
                       ("correctForget", "更正与删除(M-004/M-005)"), ("invalidation", "来源失效与会话清理(M-003)"),
                       ("ingest", "报告入记忆")):
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
    print(f"结论：{'M3 记忆评测通过' if r.get('passed') else 'M3 记忆评测未通过（见上方 ✗ 项）'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M3 记忆分层与生命周期评测")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_memory_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()