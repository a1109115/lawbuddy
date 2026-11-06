# -*- coding: utf-8 -*-
"""M3 评测器：文书生成模式（03 清单 §5 DEV-MOD-007/008 / REQ-GEN-001~010、REQ-TPL-001~005）。

用法（services/core 目录下）：
    python -m evals.m3_docgen [--keep]

流程（自主 Loop + mock 网关：模型响应脚本化，不联网；草稿/提纲推导/渲染/导出全走真实实现）：
1. 独立临时工作空间 → 民事起诉状模板夹具（6 个 {{字段}} 槽位 + 固定章节）；
2. G1 模板解析：字段识别完整且去重保序（REQ-TPL-002）；
3. G2 缺失槽位不编造：渲染后所有未填字段均为「xx」占位，无残留 {{变量}}（REQ-GEN-003）；
4. G3 提纲确认门禁：未确认提纲时 render 拒绝（REQ-GEN-005）；
5. G4 占位符阻断：存在占位符时 confirm 拒绝、正式导出被阻断（REQ-GEN-007）；
6. G5 草稿标识导出：blocking>0 时导出文件名带「草稿_」前缀（REQ-GEN-007）；
7. G6 版本可查可恢复：两次渲染 v1/v2，restore v1 恢复正文与占位符审计
   （REQ-GEN-009/010）；
7. G7 模板解析失败留痕：坏模板 status=parse_failed 且 parse_error 含行号，
   不可用于生成（REQ-TPL-004）；
8. G8 输入守门：缺模板/类型不匹配仍拒绝；文书类型与请求事项选填（缺省取模板类型/用户输入）；
9. G9 来源三分类：case_evidence 缺引证拒绝；带引证与 user_confirmed 填充成功
   （REQ-GEN-004）；补齐后 confirm 通过、正式导出无草稿标识；
10. G10 导出格式矩阵与回读：DOCX / PDF / Markdown 结构与内容一致（DEV-MOD-010）；
11. G11 编辑与崩溃恢复（DEV-MOD-009 / REQ-GEN-009/010、PRD §18.3）：自动保存落恢复点、
   30 秒窗口内合并、手动保存落编辑版本、崩溃（会话未关闭）时恢复项可见、
   与当前稿比较出差差异、放弃回退到手动版本、正常离开视图不产生恢复项、
   保留则自动保存转正、恢复历史版本不删新版；
12. G12 模板版本比较与回退（REQ-TPL-005 / G-002）：更新落新版本并前移当前指针、
   版本组完整、解析失败的更新独立留痕不入组、可选历史版本生成且草稿保存
   模板版本与内容哈希、版本比较出差异、回退不删除较新版本、版本操作写审计；
13. G13 结构化预览与排版规范（DEV-MOD-011）：get_draft 返回 preview，
    A4 与参考页边距、7 段结构、已填/待补充状态、使用提示与 blockingCount 一致；
14. G14 无同名模板自由起草兜底：只给文书类型也能产出四段提纲草稿，
    同名自由模板复用版本组不重复膨胀；
15. G15/G17 升级方案 §4.1/§5.3：只选模板空文本发起时生成内部任务描述、
    必要决策提问使任务暂停在 WAITING_HUMAN（回答前不产生草稿）、
    待答问题集可跨刷新拉取、回答幂等且从同一检查点续跑至完成；
16. G16 占位符统一 xx（旧 xxx 仅兼容审计）；G18/G19 AI 修订与手工编辑的版本锚点
    与冲突门禁；G20 DOCX 模板 strict/reference 两路径（含原件只读与 U9 不回灌示例事实）；
17. G21 连续三轮修订 + 比较 + 恢复 + 定稿复核；G22 来源失效（待核实/删除文档 → stale）；
    G23 低 OCR 置信度引证被拒；G24 非必要偏好不触发提问；
18. G25 真实卷宗 30 份 .docx 的格式保留与长文书三轮生成修订；
19. G26 真实卷宗（一审刑事判决书）起草：普通缺失不提问、原文事实可追溯、不重复读取；
20. 报告：控制台摘要 + evals/reports/m3_docgen_<ts>.json。

M3 门槛：全部 gates 必须通过。
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ._loop_mock import docgen_steps, install_mock_gateway

# ---------------------------------------------------------------------------
# 夹具：民事起诉状模板（{{字段}} 语法；固定章节内容渲染时锁定 REQ-GEN-006）
# ---------------------------------------------------------------------------

TEMPLATE_MD = """民事起诉状

原告：{{原告姓名}}，{{原告身份信息}}。
被告：{{被告姓名}}，{{被告身份信息}}。

诉讼请求：
一、判令被告向原告支付欠款人民币{{欠款金额}}元；
二、本案诉讼费用由被告承担。

事实与理由：
原告与被告于{{签约时间}}签订借款协议，约定还款期限届满后，被告经多次催告仍未履行还款义务。

此致
××人民法院

具状人：
"""

TEMPLATE_FIELDS = ["原告姓名", "原告身份信息", "被告姓名", "被告身份信息", "欠款金额", "签约时间"]
BAD_TEMPLATE_MD = """借款合同

甲方：{{甲方姓名
乙方：{{乙方姓名}}
"""

CITE = {
    "documentId": "doc-0001",
    "documentTitle": "借款协议",
    "pageNo": 1,
    "quote": "约定还款期限届满后，被告经多次催告仍未履行还款义务",
}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _gate(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    _log(f"  [{mark}] {name}{('：' + detail) if detail else ''}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M3 文书生成评测")
    parser.add_argument("--keep", action="store_true", help="保留临时工作空间")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from lawbuddy.rpc import RpcServer
    from lawbuddy import service
    from lawbuddy.workspace import ensure_workspace
    from lawbuddy.sandbox import workspace_root
    from lawbuddy.cases import create_case
    from lawbuddy.agent.tasks import get_task
    from lawbuddy import docgen as docgen_store
    from lawbuddy.storage.db import get_db

    results: dict[str, Any] = {"gates": {}, "details": {}}
    tmp = Path(tempfile.mkdtemp(prefix="eval_m3_docgen_"))
    t0 = time.time()
    all_ok = True
    try:
        ensure_workspace(str(tmp / "ws"))
        case = create_case(title="评测-文书生成", case_type="civil", client_name="原告", default_mode="document_generation")
        from lawbuddy.documents import import_documents

        evidence = tmp / "source.txt"
        evidence.write_text("欠款金额为十万元。约定还款期限届满后，被告经多次催告仍未履行还款义务", encoding="utf-8")
        evidence_id = import_documents(case["id"], [str(evidence)])[0]["documentId"]
        verified_cite = {**CITE, "documentId": evidence_id, "quote": "欠款金额为十万元。"}
        server = RpcServer()
        service.register(server)
        agent_start = server._handlers["agent.start"]

        # G1 模板解析（REQ-TPL-002：字段识别）
        _log("[G1] 模板导入与字段识别")
        tpl = server._handlers["docgen.import_template"]({
            "title": "民事起诉状模板", "docType": "民事起诉状",
            "templateMd": TEMPLATE_MD, "caseId": case["id"],
            "sourceName": "起诉状模板.md",
        })
        got_fields = [f["name"] for f in tpl["fields"]]
        g1 = (
            _gate("状态 active", tpl["status"] == "active", tpl["status"])
            and _gate("字段识别完整且保序", got_fields == TEMPLATE_FIELDS, f"{got_fields}")
        )
        results["gates"]["G1_template_parse"] = g1
        results["details"]["fields"] = got_fields
        all_ok &= g1

        # G7 模板解析失败留痕（REQ-TPL-004：保留原文件并标记位置）
        _log("[G7] 模板解析失败留痕")
        bad = server._handlers["docgen.import_template"]({
            "title": "坏模板", "docType": "民事起诉状", "templateMd": BAD_TEMPLATE_MD,
        })
        g7 = (
            _gate("状态 parse_failed", bad["status"] == "parse_failed", bad["status"])
            and _gate("parse_error 标记行号", "第 3 行" in (bad["parseError"] or ""), bad["parseError"])
            and _gate("原文保留", "甲方" in docgen_store.get_template(bad["id"])["contentMd"])
            and _gate("不可用于生成", "解析失败" in _err_of(
                lambda: docgen_store.create_draft(case["id"], bad["id"], "民事起诉状", "测试请求事项")))
        )
        results["gates"]["G7_parse_failed_trace"] = g7
        all_ok &= g7

        # G8 输入守门（PRD 15.6）
        _log("[G8] 发起输入守门")

        def expect_reject(tag: str, params: dict[str, Any], fragment: str) -> bool:
            try:
                agent_start({"caseId": case["id"], "mode": "document_generation", **params})
            except ValueError as e:
                return fragment in str(e)
            return False

        base_inp = {"templateId": tpl["id"], "docType": "民事起诉状", "purpose": "追讨借款本金"}
        g8 = all([
            _gate("缺模板", expect_reject("no-tpl", {"docgenInput": {"docType": "民事起诉状", "purpose": "x"}}, "模板")),
            _gate("模板类型不匹配", expect_reject(
                "mismatch",
                {"docgenInput": {**base_inp, "docType": "答辩状"}},
                "不匹配")),
        ])
        results["gates"]["G8_input_guard"] = g8
        all_ok &= g8

        # 端到端（自主 Loop + mock 网关）：agent.start → 提纲由模板推导落库 → 草稿待确认
        undo = install_mock_gateway(docgen_steps())
        try:
            task = agent_start({
                "caseId": case["id"], "mode": "document_generation",
                "docgenInput": base_inp,
            })
            deadline = time.time() + 90
            while (
                get_task(task["id"])["lifecycle"] in ("CREATED", "PLANNING", "RUNNING", "VERIFYING")
                and time.time() < deadline
            ):
                time.sleep(0.4)
        finally:
            undo()
        lifecycle = get_task(task["id"])["lifecycle"]
        if not _gate("任务完成（自主 Loop + mock 网关）", lifecycle == "COMPLETED", lifecycle):
            raise RuntimeError("任务未完成，后续 gate 无法执行")

        drafts = server._handlers["docgen.list_drafts"]({"caseId": case["id"]})
        draft_id = drafts[0]["id"]
        # G24（升级方案 §11.2「非必要偏好不触发提问」）：本次任务只缺普通字段/措辞偏好，
        # 应直接产出草稿，不产生任何问题集（避免把文书生成退化成表单问答）
        _log("[非必要偏好] 直接起草不触发提问")
        g_no_ask = (
            _gate("普通字段缺失不触发提问（无待答问题集）",
                  not server._handlers["agent.pending_questions"]({"taskId": task["id"]}),
                  str(get_task(task["id"])["lifecycle"]))
            and _gate("任务直接产出草稿", bool(drafts), str(len(drafts)))
            and _gate("草稿创建后任务未被挂起为待人工",
                  get_task(task["id"])["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS"),
                  get_task(task["id"])["lifecycle"])
        )
        results["gates"]["G24_no_unnecessary_questions"] = g_no_ask
        all_ok &= g_no_ask
        # 首轮即产出草稿正文与 Word（AI 起草草稿态渲染）：提纲仍待用户确认
        draft = server._handlers["docgen.get_draft"]({"draftId": draft_id})
        _gate("草稿首轮自动生成正文", draft["status"] == "draft" and bool(draft["bodyMd"]),
              f"status={draft['status']} bodyLen={len(draft['bodyMd'] or '')}")
        _gate("提纲仍待用户确认", not draft["outlineConfirmed"],
              f"confirmed={draft['outlineConfirmed']}")
        outline_headings = [it["heading"] for it in (draft["outline"] or [])]
        _gate("提纲由模板结构推导且不含变量/内容行",
              bool(outline_headings) and all("{{" not in h for h in outline_headings),
              str(outline_headings))

        # 首轮 Word 草稿：文件实际落盘 + 产物卡元数据可路由到文书工作台
        arts = [a for a in server._handlers["artifact.list"]({"caseId": case["id"]})
                if a.get("docgenDraftId") == draft_id]
        word_path = (workspace_root() / arts[0]["title"]) if arts else None
        _gate("首轮 Word 产物已登记（带 docgenDraftId）",
              bool(arts) and arts[0]["kind"] == "file" and arts[0].get("draftMarked") is True,
              str(arts[:1]))
        _gate("Word 草稿文件实际落盘且为 docx",
              bool(word_path and word_path.exists() and word_path.suffix == ".docx"),
              str(word_path))

        # AI 起草工具：填槽位（原文引证 + 待核实）→ 渲染正文 → 落盘 Word
        from lawbuddy.agent.tasks import create_task
        from lawbuddy.agent.autonomous import _execute_tool

        ai_task = create_task(case["id"], "document_generation", "AI 起草评测")
        ai_draft = docgen_store.create_draft(case["id"], tpl["id"], "民事起诉状", "AI 起草评测")
        docgen_store.update_outline(
            ai_draft["id"],
            docgen_store.outline_from_template(docgen_store.get_template(tpl["id"])["contentMd"]),
        )
        tool_res = _execute_tool(
            {"case_id": case["id"], "task_id": ai_task["id"], "mode": "document_generation"},
            "docgen_render_draft",
            {
                "draftId": ai_draft["id"],
                "values": [
                    {"name": "欠款金额", "value": "十万元", "sourceType": "case_evidence",
                     "documentId": evidence_id, "pageNo": 1, "quote": "欠款金额为十万元。"},
                    {"name": "原告姓名", "value": "王某", "sourceType": "pending_verify"},
                ],
            },
        )
        ai_draft = docgen_store.get_draft(ai_draft["id"])
        _gate("AI 起草填入原文槽位并保留未填占位",
              "十万元" in ai_draft["bodyMd"] and "王某" in ai_draft["bodyMd"]
              and "xx" in ai_draft["bodyMd"])
        _gate("AI 起草 Word 落盘", (workspace_root() / str(tool_res["fileName"])).exists(),
              str(tool_res.get("fileName")))

        # G3 提纲确认门禁（REQ-GEN-005）
        _log("[G3] 提纲确认门禁")
        g3 = _gate("未确认提纲时 render 拒绝", "提纲" in _err_of(
            lambda: server._handlers["docgen.render"]({"draftId": draft_id})))
        results["gates"]["G3_outline_gate"] = g3
        all_ok &= g3

        server._handlers["docgen.confirm_outline"]({"draftId": draft_id})
        draft = server._handlers["docgen.get_draft"]({"draftId": draft_id})
        _gate("确认提纲生效", draft["outlineConfirmed"] is True)

        # 首次渲染：所有槽位缺失 → 全占位符
        draft = server._handlers["docgen.render"]({"draftId": draft_id})
        body = draft["bodyMd"]

        # G2 缺失槽位不编造（REQ-GEN-003）
        _log("[G2] 缺失槽位占位符不编造")
        all_ph = body.count("xx") == len(TEMPLATE_FIELDS)
        no_var = "{{" not in body and "}}" not in body
        g2 = (
            _gate("全部缺失字段为占位符", all_ph)
            and _gate("无残留模板变量", no_var)
            and _gate("审计计数一致", draft["blockingCount"] == len(TEMPLATE_FIELDS), str(draft["blockingCount"]))
            and _gate("固定章节保留", "诉讼请求" in body and "此致" in body)
        )
        results["gates"]["G2_no_fabrication"] = g2
        all_ok &= g2

        # G16 占位符统一 xx，旧 xxx 仅作兼容审计（升级方案 §11.2 G16）
        _log("[G16] 占位符统一 xx 与旧 xxx 兼容审计")
        g16_legacy = docgen_store.audit_placeholders("原告：xxx，被告：xx。住址：xxx。")
        g16_render = docgen_store.render_content(
            "原告：{{原告姓名}}，被告：{{被告姓名}}。", {})
        g16 = (
            _gate("新渲染统一输出 xx（不出现 xxx）",
                  g16_render.count("xx") == 2 and "xxx" not in g16_render, g16_render)
            and _gate("旧 xxx 仍被审计计为待补充",
                      docgen_store._blocking_total(g16_legacy) == 3,
                      str(g16_legacy))
            and _gate("旧【待补充 字段】仍在审计范围",
                      docgen_store._blocking_total(
                          docgen_store.audit_placeholders("住址：【待补充 原告住址】")) == 1)
        )
        results["gates"]["G16_placeholder_unified"] = g16
        all_ok &= g16

        # G4 占位符阻断正式导出 + G5 草稿标识导出（REQ-GEN-007）
        _log("[G4/G5] 占位符阻断与草稿标识导出")
        confirm_err = _err_of(lambda: server._handlers["docgen.confirm"]({"draftId": draft_id}))
        out_dir = tmp / "export"
        out_dir.mkdir()
        exported = server._handlers["docgen.export"]({"draftId": draft_id, "destDir": str(out_dir)})
        exported_path = Path(exported["path"])
        g45 = (
            _gate("confirm 被阻断", "占位符" in confirm_err, confirm_err)
            and _gate("草稿导出带「草稿_」标识", exported["draftMarked"] and exported_path.name.startswith("草稿_"),
                      exported_path.name)
            and _gate("草稿文件内容完整", exported_path.read_text(encoding="utf-8") == body)
        )
        results["gates"]["G4G5_draft_export"] = g45
        all_ok &= g45

        # G9 来源三分类 + 定稿正式导出（REQ-GEN-004/007）
        _log("[G9] 来源三分类与正式导出")
        g9a = _gate("case_evidence 缺引证拒绝", "引证" in _err_of(
            lambda: server._handlers["docgen.fill_slots"]({
                "draftId": draft_id,
                "values": [{"name": "欠款金额", "value": "十万元", "sourceType": "case_evidence"}],
            })))
        server._handlers["docgen.fill_slots"]({
            "draftId": draft_id,
            "values": [
                {"name": "欠款金额", "value": "十万元", "sourceType": "case_evidence", "sourceRef": verified_cite},
                {"name": "原告姓名", "value": "王某", "sourceType": "user_confirmed"},
            ],
        })
        draft = server._handlers["docgen.render"]({"draftId": draft_id})
        # v1 = 首轮 AI 草稿自动渲染，v2 = 确认提纲后渲染，v3 = 填槽位后渲染
        v2_ok = _gate("二次渲染落版本 v3", docgen_store.list_versions(draft_id)[0]["versionNo"] == 3)
        g9b = _gate("已填槽位替换、其余仍占位",
                    "王某" in draft["bodyMd"] and "十万元" in draft["bodyMd"]
                    and "xx" in draft["bodyMd"])

        # §11.1：用户确认与模型假设不得被渲染成原文证据。
        # 即便调用方给 user_confirmed 携带 documentId/quote，也必须落为空来源，
        # 否则预览分段会显示可跳转的原文引证（把用户口述冒充成原文记载）。
        server._handlers["docgen.fill_slots"]({
            "draftId": draft_id,
            "values": [{
                "name": "被告姓名", "value": "李某", "sourceType": "user_confirmed",
                "sourceRef": {"documentId": evidence_id, "pageNo": 1, "quote": "欠款金额为十万元。"},
            }],
        })
        slots = docgen_store.get_draft(draft_id)["slots"]
        preview_sections = (docgen_store.get_draft(draft_id).get("preview") or {}).get("sections") or []
        party_section = next((s for s in preview_sections if s["key"] == "parties"), {})
        g9d = (
            _gate("user_confirmed 槽位不落引证",
                  slots["被告姓名"]["sourceType"] == "user_confirmed"
                  and slots["被告姓名"].get("sourceRef") is None,
                  str(slots["被告姓名"]))
            and _gate("预览当事人段为「用户确认」而非原文引证",
                      party_section.get("sourceType") == "user_confirmed"
                      and not party_section.get("sourceRef"),
                      f"{party_section.get('sourceType')} / {party_section.get('sourceRef')}")
            and _gate("用户确认值不产生 Claim（不进入任务级引证台账）",
                      not [c for c in server._handlers["claim.list"]({"caseId": case["id"]})
                           if "李某" in str(c.get("text") or "")])
        )
        results["gates"]["G9D_user_confirmed_not_evidence"] = g9d
        all_ok &= g9d

        # G6 版本可查可恢复（REQ-GEN-009/010）
        _log("[G6] 草稿版本可查可恢复")
        server._handlers["docgen.restore_version"]({"draftId": draft_id, "versionNo": 1})
        restored = server._handlers["docgen.get_draft"]({"draftId": draft_id})
        g6 = (
            v2_ok
            and _gate("恢复 v1 后占位符审计还原", restored["blockingCount"] == len(TEMPLATE_FIELDS),
                      str(restored["blockingCount"]))
            and _gate("恢复 v1 正文无已填值", "王某" not in restored["bodyMd"])
        )
        results["gates"]["G6_version_restore"] = g6
        all_ok &= g6

        # 补齐全部槽位 → blocking=0 → confirm → 正式导出
        server._handlers["docgen.restore_version"]({"draftId": draft_id, "versionNo": 3})
        server._handlers["docgen.fill_slots"]({
            "draftId": draft_id,
            "values": [
                {"name": "被告姓名", "value": "李某", "sourceType": "user_confirmed"},
                {"name": "原告身份信息", "value": "男，1980年1月1日出生", "sourceType": "user_confirmed"},
                {"name": "被告身份信息", "value": "男，1979年2月2日出生", "sourceType": "user_confirmed"},
                {"name": "签约时间", "value": "2025年3月", "sourceType": "user_confirmed"},
            ],
        })
        draft = server._handlers["docgen.render"]({"draftId": draft_id})
        formal_dir = tmp / "export_formal"
        formal_dir.mkdir()
        g9c = (
            g9a
            and g9b
            and _gate("补齐后 blocking=0", draft["blockingCount"] == 0, str(draft["blockingCount"]))
            and _gate("confirm 通过", server._handlers["docgen.confirm"]({"draftId": draft_id})["status"] == "confirmed")
            and _gate("正式导出无草稿标识",
                      not server._handlers["docgen.export"]({"draftId": draft_id, "destDir": str(formal_dir)})["draftMarked"])
        )
        results["gates"]["G9_source_types_formal_export"] = g9c
        all_ok &= g9c

        # G10 导出格式矩阵与回读（G-008 / DEV-MOD-010：DOCX、PDF、Markdown 结构与内容一致）
        _log("[G10] DOCX/PDF 导出与回读")
        docx_exp = server._handlers["docgen.export"]({"draftId": draft_id, "destDir": str(formal_dir), "format": "docx"})
        pdf_exp = server._handlers["docgen.export"]({"draftId": draft_id, "destDir": str(formal_dir), "format": "pdf"})
        docx_path, pdf_path = Path(docx_exp["path"]), Path(pdf_exp["path"])

        from docx import Document as _Docx

        docx_text = "\n".join(p.text for p in _Docx(str(docx_path)).paragraphs)
        from pypdf import PdfReader

        pdf_text = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(pdf_path)).pages)
        # 关键内容（标题、已填槽位值、锁定章节）在三种格式中一致
        keys = ("王某", "李某", "十万元", "诉讼请求", "此致")
        g10 = (
            _gate("DOCX 与 PDF 均落盘且非草稿标识",
                  docx_path.exists() and pdf_path.exists() and docx_path.stat().st_size > 0
                  and pdf_path.stat().st_size > 0
                  and not docx_exp["draftMarked"] and not pdf_exp["draftMarked"]
                  and not docx_path.name.startswith("草稿_") and not pdf_path.name.startswith("草稿_"))
            and _gate("DOCX 回读含标题、锁定章节与已填槽位值", all(k in docx_text for k in keys))
            and _gate("PDF 回读含标题、锁定章节与已填槽位值", all(k in pdf_text for k in keys))
            and _gate("PDF 无占位符残留（已定稿版本）", "xx" not in pdf_text and "xx" not in docx_text)
        )
        results["gates"]["G10_export_formats_backread"] = g10
        all_ok &= g10

        # G11 正文编辑、自动保存、崩溃恢复与版本比较（DEV-MOD-009 / REQ-GEN-009/010、PRD §18.3）
        _log("[G11] 正文编辑、自动保存与崩溃恢复")
        base_body = draft["bodyMd"]
        session = server._handlers["editor.open"]({"draftId": draft_id})
        edited = base_body + "\n\n（补充：被告经多次催告仍未履行，原告保留追究利息的权利。）"

        s1 = server._handlers["docgen.save_body"]({
            "draftId": draft_id, "bodyMd": edited, "kind": "autosave", "sessionId": session["id"],
        })
        g11a = (
            _gate("自动保存落恢复点", s1["kind"] == "autosave" and not s1["merged"])
            and _gate("编辑使已定稿回到草稿态（占位符重新审计）", s1["draft"]["status"] == "draft", s1["draft"]["status"])
            and _gate("恢复点版本类型为 autosave", docgen_store.list_versions(draft_id)[0]["kind"] == "autosave")
        )
        results["gates"]["G11a_autosave"] = g11a
        all_ok &= g11a

        s2 = server._handlers["docgen.save_body"]({
            "draftId": draft_id, "bodyMd": edited + "（第二次自动保存）", "kind": "autosave",
            "sessionId": session["id"],
        })
        g11b = (
            _gate("30 秒窗口内合并同一恢复点", s2["merged"] and s2["versionNo"] == s1["versionNo"],
                  f"v{s2['versionNo']} merged={s2['merged']}")
            and _gate("恢复点取最新文本",
                      docgen_store.list_versions(draft_id)[0]["bodyMd"] == edited + "（第二次自动保存）")
        )
        results["gates"]["G11b_recovery_point"] = g11b
        all_ok &= g11b

        manual_body = base_body.replace("此致", "此致（已按法官意见修改）")
        s3 = server._handlers["docgen.save_body"]({
            "draftId": draft_id, "bodyMd": manual_body, "kind": "edit", "sessionId": session["id"],
        })
        # 手动版本之后再次自动保存 → 崩溃前存在未提交自动保存（强杀：会话保持打开，不调 close）
        s4 = server._handlers["docgen.save_body"]({
            "draftId": draft_id, "bodyMd": manual_body + "\n（崩溃前未保存的补充）", "kind": "autosave",
            "sessionId": session["id"],
        })
        pend = server._handlers["editor.pending"]({"caseId": case["id"]})
        item = next((p for p in pend if p["targetId"] == draft_id), None)
        g11c = (
            _gate("手动保存落编辑版本", s3["kind"] == "edit" and not s3["merged"])
            and _gate("崩溃恢复项可见且指向自动保存版本",
                      item is not None and item["autosaveVersionNo"] == s4["versionNo"], str(item))
            and _gate("恢复项带出草稿信息", item is not None and item["title"] == "追讨借款本金", str(item))
        )
        results["gates"]["G11c_crash_pending"] = g11c
        all_ok &= g11c

        diff_auto = server._handlers["docgen.diff_versions"]({"draftId": draft_id, "fromVersionNo": s3["versionNo"]})
        g11d = (
            _gate("与当前稿比较出差异", diff_auto["stats"]["added"] >= 1, str(diff_auto["stats"]))
            and _gate("差异右列含未保存的补充文本",
                      any("崩溃前未保存" in "".join(h["rightLines"]) for h in diff_auto["hunks"]))
        )
        results["gates"]["G11d_diff_current"] = g11d
        all_ok &= g11d

        # 放弃恢复 → 回到最近手动版本；保留恢复 → 自动保存转为编辑版本
        res_discard = server._handlers["editor.resolve"]({"sessionId": item["sessionId"], "action": "discard"})
        vers_after = docgen_store.list_versions(draft_id)
        g11e = (
            _gate("放弃后正文回到最近手动版本", res_discard["draft"]["bodyMd"] == manual_body)
            and _gate("自动保存恢复点已清理", all(v["kind"] != "autosave" for v in vers_after))
            and _gate("恢复项处理后可再次开启会话",
                      not any(p["targetId"] == draft_id for p in server._handlers["editor.pending"]({})))
        )
        results["gates"]["G11e_discard"] = g11e
        all_ok &= g11e

        # 正常离开编辑视图：关闭会话 → 不产生崩溃恢复项，自动保存内容仍在版本历史（不丢内容）
        ses_leave = server._handlers["editor.open"]({"draftId": draft_id})
        leave_body = manual_body + "\n（离开视图时的自动保存）"
        server._handlers["docgen.save_body"]({
            "draftId": draft_id, "bodyMd": leave_body, "kind": "autosave", "sessionId": ses_leave["id"],
        })
        closed = server._handlers["editor.close"]({"sessionId": ses_leave["id"]})
        g11h = (
            _gate("离开视图关闭会话并保留自动保存标记",
                  closed["closed"] is True and closed["keptAutosave"] is True, str(closed))
            and _gate("正常离开不产生崩溃恢复项",
                      not any(p["targetId"] == draft_id for p in server._handlers["editor.pending"]({})))
            and _gate("离开时正文保留自动保存内容",
                      server._handlers["docgen.get_draft"]({"draftId": draft_id})["bodyMd"] == leave_body)
        )
        results["gates"]["G11h_leave_view"] = g11h
        all_ok &= g11h

        ses2 = server._handlers["editor.open"]({"draftId": draft_id})
        keep_body = manual_body + "\n（恢复演练：保留这段自动保存）"
        s5 = server._handlers["docgen.save_body"]({
            "draftId": draft_id, "bodyMd": keep_body, "kind": "autosave", "sessionId": ses2["id"],
        })
        item2 = next(
            p for p in server._handlers["editor.pending"]({"caseId": case["id"]}) if p["targetId"] == draft_id
        )
        res_keep = server._handlers["editor.resolve"]({"sessionId": item2["sessionId"], "action": "recover"})
        vers_keep = docgen_store.list_versions(draft_id)
        recovery_audits = get_db().query_one(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'editor.recovery'"
        )
        g11f = (
            _gate("保留恢复后正文为自动保存内容", res_keep["draft"]["bodyMd"] == keep_body)
            and _gate("自动保存版本转为编辑版本（不再悬挂）",
                      vers_keep[0]["versionNo"] == s5["versionNo"] and vers_keep[0]["kind"] == "edit",
                      f"v{vers_keep[0]['versionNo']} {vers_keep[0]['kind']}")
            and _gate("恢复动作写入审计", (recovery_audits["n"] or 0) >= 2, str(recovery_audits["n"]))
        )
        results["gates"]["G11f_recover"] = g11f
        all_ok &= g11f

        # 历史版本恢复不删除较新版本（REQ-GEN-010：不静默覆盖当前稿）
        before_n = len(vers_keep)
        server._handlers["docgen.restore_version"]({"draftId": draft_id, "versionNo": 1})
        after = docgen_store.list_versions(draft_id)
        g11g = (
            _gate("恢复历史版本不删除较新版本", len(after) == before_n + 1 and after[-1]["versionNo"] == 1,
                  f"{before_n}→{len(after)}")
            and _gate("恢复动作自身落版本", after[0]["kind"] == "restore", after[0]["kind"])
        )
        results["gates"]["G11g_restore_keeps_history"] = g11g
        all_ok &= g11g

        # G12 模板版本比较与回退（REQ-TPL-005 / G-002：可选版本生成、产物保存版本与哈希）
        _log("[G12] 模板版本比较与回退")
        tpl_v1 = docgen_store.get_template(tpl["id"])
        updated_md = TEMPLATE_MD.replace("原告与被告", "原告与被告（经修订）") + "\n附：证据清单见卷宗。"
        tpl_v2 = server._handlers["docgen.import_template"]({
            "title": "民事起诉状模板（修订）", "docType": "民事起诉状",
            "templateMd": updated_md, "caseId": case["id"],
            "sourceName": "起诉状模板-v2.md", "baseTemplateId": tpl["id"],
        })
        vers = server._handlers["docgen.list_template_versions"]({"templateId": tpl["id"]})
        # 解析失败的更新不进版本组：独立留痕行（REQ-TPL-004），版本组不受影响
        bad_tpl = server._handlers["docgen.import_template"]({
            "title": "坏更新", "docType": "民事起诉状", "templateMd": BAD_TEMPLATE_MD,
            "baseTemplateId": tpl_v2["id"],
        })
        # G-002：更新模板后可选版本生成，草稿保存所用版本与内容哈希
        draft_v1 = docgen_store.create_draft(case["id"], tpl_v1["id"], "民事起诉状", "旧版本生成演练")
        draft_v2 = docgen_store.create_draft(case["id"], tpl_v2["id"], "民事起诉状", "新版本生成演练")
        diff_t = server._handlers["docgen.diff_template_versions"]({
            "templateId": tpl_v2["id"], "fromVersionNo": 1,
        })
        rolled = server._handlers["docgen.rollback_template"]({"templateId": tpl_v2["id"], "versionNo": 1})
        vers_after = server._handlers["docgen.list_template_versions"]({"templateId": tpl["id"]})
        listed = server._handlers["docgen.list_templates"]({"caseId": case["id"]})
        tpl_audits = get_db().query_one(
            "SELECT COUNT(*) AS n FROM audit_logs "
            "WHERE action IN ('template.version_create', 'template.rollback')"
        )
        g12 = (
            _gate("更新落新版本并前移当前指针",
                  tpl_v2["versionNo"] == 2 and tpl_v2["isCurrent"] and tpl_v2["rootId"] == tpl_v1["rootId"],
                  f"v{tpl_v2['versionNo']} cur={tpl_v2['isCurrent']}")
            and _gate("原版本让位但不删除（回退前快照）",
                      [v["versionNo"] for v in vers] == [2, 1] and not vers[-1]["isCurrent"],
                      str([(v["versionNo"], v["isCurrent"]) for v in vers]))
            and _gate("版本组完整且指针唯一",
                      [v["versionNo"] for v in vers] == [2, 1] and sum(1 for v in vers if v["isCurrent"]) == 1,
                      str([v["versionNo"] for v in vers]))
            and _gate("解析失败的更新独立留痕不入组",
                      bad_tpl["status"] == "parse_failed" and bad_tpl["rootId"] == bad_tpl["id"]
                      and all(v["versionNo"] != 3 for v in vers), str(bad_tpl["status"]))
            and _gate("可选历史版本生成（v1）且哈希留痕",
                      draft_v1["templateVersionNo"] == 1
                      and draft_v1["templateContentSha256"] == tpl_v1["sourceSha256"],
                      str(draft_v1["templateVersionNo"]))
            and _gate("当前版本生成（v2）且哈希留痕",
                      draft_v2["templateVersionNo"] == 2
                      and draft_v2["templateContentSha256"] == tpl_v2["sourceSha256"], "")
            and _gate("版本比较出差异", diff_t["stats"]["added"] >= 1 and diff_t["fromLabel"] == "v1",
                      str(diff_t["stats"]))
            and _gate("回退后当前版本为 v1", rolled["versionNo"] == 1 and rolled["isCurrent"],
                      f"v{rolled['versionNo']}")
            and _gate("回退不删除较新版本（v2 保留且非当前）",
                      [(v["versionNo"], v["isCurrent"]) for v in vers_after] == [(2, False), (1, True)],
                      str([(v["versionNo"], v["isCurrent"]) for v in vers_after]))
            and _gate("清单只列组内当前版本",
                      sum(1 for t in listed if t["rootId"] == tpl_v1["rootId"]) == 1, "")
            and _gate("版本操作写审计", (tpl_audits["n"] or 0) >= 2, str(tpl_audits["n"]))
        )
        results["gates"]["G12_template_versions"] = g12
        all_ok &= g12

        # G13 结构化预览与排版规范（工作台 / 对话卡：结构、来源、使用提示）
        _log("[G13] 结构化预览与排版规范派生")
        g13_draft = docgen_store.create_draft(
            case["id"], tpl["id"], "民事起诉状", "结构化预览演练"
        )
        server._handlers["docgen.update_outline"]({
            "draftId": g13_draft["id"],
            "items": [
                {"heading": "诉讼请求", "points": "判令被告支付欠款", "sourceDoc": ""},
                {"heading": "事实与理由", "points": "借款到期未还", "sourceDoc": ""},
            ],
        })
        server._handlers["docgen.confirm_outline"]({"draftId": g13_draft["id"]})
        server._handlers["docgen.fill_slots"]({
            "draftId": g13_draft["id"],
            "values": [
                {"name": "原告姓名", "value": "王某", "sourceType": "user_confirmed"},
                {"name": "欠款金额", "value": "十万元", "sourceType": "case_evidence",
                 "sourceRef": verified_cite},
            ],
        })
        g13_result = server._handlers["docgen.render"]({"draftId": g13_draft["id"]})
        preview = g13_result.get("preview") or {}
        sections = preview.get("sections") or []
        status_by_key = {s["key"]: s["status"] for s in sections}
        layout = preview.get("layout") or {}
        g13 = (
            _gate("get_draft 返回 preview", bool(preview), str(bool(preview)))
            and _gate("结构段齐全（7 段）",
                      [s["key"] for s in sections]
                      == ["title", "parties", "cause", "claims", "facts", "evidence", "tail"],
                      str([s["key"] for s in sections]))
            and _gate("已填当事人为 filled 且带来源",
                      status_by_key.get("parties") == "filled"
                      and any(s["key"] == "parties" and s["sourceType"] == "user_confirmed"
                              for s in sections))
            and _gate("未填案由为 pending", status_by_key.get("cause") == "pending",
                      str(status_by_key.get("cause")))
            and _gate("证据段待补充", status_by_key.get("evidence") == "pending",
                      str(status_by_key.get("evidence")))
            and _gate("A4 纵向与参考页边距",
                      layout.get("paper") == "A4"
                      and layout.get("paperSizeMm") == {"width": 210, "height": 297}
                      and layout.get("orientation") == "portrait"
                      and layout.get("marginsCm")
                      == {"top": 3.7, "bottom": 3.5, "left": 2.8, "right": 2.6},
                      str(layout.get("marginsCm")))
            and _gate("使用提示含 xx 替换",
                      any("xx" in tip for tip in preview.get("usageTips") or []))
            and _gate("blockingCount 与占位符审计一致",
                      preview.get("blockingCount") == g13_result["blockingCount"] == 4,
                      str(preview.get("blockingCount")))
        )
        results["gates"]["G13_preview_layout"] = g13
        all_ok &= g13

        # G14 推荐卡无同名模板时自由起草兜底（只给 docType、不给 templateId）
        _log("[G14] 无同名模板自由起草")
        before_templates = docgen_store.list_templates(case["id"])
        g14_draft = docgen_store.create_free_draft(case["id"], "民事起诉状", "推荐卡起草演练")
        g14_again = docgen_store.create_free_draft(case["id"], "民事起诉状", "第二轮推荐卡起草演练")
        after_templates = docgen_store.list_templates(case["id"])
        g14_root = docgen_store.get_template(g14_draft["templateId"])["rootId"]
        g14_again_root = docgen_store.get_template(g14_again["templateId"])["rootId"]
        g14 = (
            _gate("无模板仍产出提纲草稿",
                  g14_draft["status"] == "outline" and len(g14_draft.get("outline") or []) >= 4,
                  f"{g14_draft['status']} / {len(g14_draft.get('outline') or [])}")
            and _gate("起诉状使用默认四段章节",
                      [o["heading"] for o in g14_draft.get("outline") or []]
                      == ["当事人信息", "请求事项", "事实与理由", "证据与落款"],
                      str([o["heading"] for o in g14_draft.get("outline") or []]))
            and _gate("同名自由模板复用版本组",
                      g14_root == g14_again_root and len(after_templates) == len(before_templates) + 1,
                      f"{len(before_templates)}→{len(after_templates)}")
        )
        results["gates"]["G14_free_draft_fallback"] = g14
        all_ok &= g14

        # G18 修订版本锚点（升级方案 §7.4/§10 阶段二）：AI 修订必须基于当前稿版本，
        # 基于旧稿的修订被拒绝，避免覆盖用户在编辑期间产生的新稿
        _log("[G18] 修订版本锚点与冲突拒绝")
        g18_draft = docgen_store.create_free_draft(case["id"], "民事起诉状", "版本锚点演练")
        docgen_store.confirm_outline(g18_draft["id"])
        docgen_store.render_body(g18_draft["id"])
        docgen_store.save_body(g18_draft["id"], "第一稿正文。\n", kind="edit")
        g18_v1 = docgen_store.latest_version_no(g18_draft["id"])
        # 模拟用户在工作台又保存了一稿，当前稿前进
        docgen_store.save_body(g18_draft["id"], "第二稿正文（用户编辑）。\n", kind="edit")
        g18_v2 = docgen_store.latest_version_no(g18_draft["id"])
        stale_err = _err_of(lambda: docgen_store.revise_body(
            g18_draft["id"], "基于旧稿的 AI 修订。\n", "按旧稿修订", base_version_no=g18_v1))
        fresh = docgen_store.revise_body(
            g18_draft["id"], "基于当前稿的 AI 修订。\n", "按当前稿修订", base_version_no=g18_v2)
        g18 = (
            _gate("草稿返回当前稿版本号", g18_v2 > g18_v1, f"v{g18_v1}→v{g18_v2}")
            and _gate("基于旧版本的修订被拒绝", "VERSION_CONFLICT" in stale_err, stale_err)
            and _gate("基于当前版本的修订落为新版本",
                      fresh["baseVersionNo"] == g18_v2 and fresh["versionNo"] == g18_v2 + 1,
                      f"base=v{fresh['baseVersionNo']} → v{fresh['versionNo']}")
        )
        results["gates"]["G18_revision_version_anchor"] = g18
        all_ok &= g18

        # G19 手工编辑并发冲突（升级方案 §7.4/§10 阶段二）：编辑器的保存基准与当前稿
        # 不一致时拒绝落库，避免另一窗口编辑或 AI 修订产生的新稿被静默覆盖
        _log("[G19] 手工编辑版本冲突与最新稿载入")
        g19_draft = docgen_store.create_free_draft(case["id"], "民事起诉状", "并发编辑演练")
        docgen_store.confirm_outline(g19_draft["id"])
        docgen_store.render_body(g19_draft["id"])
        g19_base = docgen_store.latest_version_no(g19_draft["id"])
        # 另一窗口先保存：当前稿推进
        docgen_store.save_body(g19_draft["id"], "另一窗口保存的正文。\n", kind="edit")
        g19_now = docgen_store.latest_version_no(g19_draft["id"])
        g19_err = _err_of(lambda: docgen_store.save_body(
            g19_draft["id"], "本窗口基于旧基准的编辑。\n", kind="edit", expected_version_no=g19_base))
        g19_ok = docgen_store.save_body(
            g19_draft["id"], "载入最新稿后的编辑。\n", kind="edit", expected_version_no=g19_now)
        g19 = (
            _gate("编辑器基准落后于当前稿", g19_now > g19_base, f"v{g19_base}→v{g19_now}")
            and _gate("基于旧基准的手工保存被拒绝", "VERSION_CONFLICT" in g19_err, g19_err)
            and _gate("基于最新稿的手工保存落为新版本", g19_ok["versionNo"] == g19_now + 1,
                      f"v{g19_now} → v{g19_ok['versionNo']}")
        )
        results["gates"]["G19_edit_version_conflict"] = g19
        all_ok &= g19

        # G20 DOCX 自定义模板（升级方案 §6.1/§6.2/§6.4 / §10 阶段三、§11.1 U8/U9）：
        # 上传 .docx 范本 → 变量与页眉页脚解析 → strict 在原 OOXML 上替换（未填写 xx，
        # 非变量文字/页眉页脚/样式保留）→ reference 以副本为基底重写正文 → 原件只读不变
        _log("[G20] DOCX 模板导入与 strict/reference 生成")
        import hashlib as _hashlib

        from docx import Document as _Document

        from lawbuddy import docgen_docx as _docgen_docx

        def _build_docx(with_vars: bool) -> bytes:
            doc = _Document()
            doc.add_paragraph("民事起诉状")
            if with_vars:
                doc.add_paragraph("原告：{{原告姓名}}，住所地：{{原告住所}}。")
                doc.add_paragraph("被告：{{被告姓名}}。")
            else:
                doc.add_paragraph("原告：张三，住所地：示例市示例区。")
                doc.add_paragraph("被告：李四。")
            table = doc.add_table(rows=2, cols=2)
            table.rows[0].cells[0].text = "标的额"
            table.rows[0].cells[1].text = "{{标的额}}" if with_vars else "100000 元"
            table.rows[1].cells[0].text = "利息"
            table.rows[1].cells[1].text = "按约定"
            doc.sections[0].header.paragraphs[0].text = "示例律师事务所"
            buf = io.BytesIO()
            doc.save(buf)
            return buf.getvalue()

        g20_docx_vars = _build_docx(True)
        g20_docx_ref = _build_docx(False)
        g20_parsed = _docgen_docx.extract_template(g20_docx_vars)
        g20_ref_parsed = _docgen_docx.extract_template(g20_docx_ref)
        g20_src_sha = _hashlib.sha256(g20_docx_vars).hexdigest()
        g20_tpl = docgen_store.import_docx_template(
            g20_docx_vars, "DOCX 起诉状模板", "民事起诉状", case_id=case["id"],
            source_name="起诉状模板.docx",
        )
        g20_ref_tpl = docgen_store.import_docx_template(
            g20_docx_ref, "DOCX 参考范本", "民事起诉状", case_id=case["id"],
            source_name="参考范本.docx",
        )
        g20_field_names = [f["name"] for f in g20_tpl["fields"]]
        g20_draft = docgen_store.create_draft(case["id"], g20_tpl["id"], "民事起诉状", "DOCX 生成演练")
        docgen_store.update_outline(g20_draft["id"], [
            {"heading": "当事人信息", "points": "", "sourceDoc": ""},
            {"heading": "请求事项", "points": "", "sourceDoc": ""},
        ])
        docgen_store.confirm_outline(g20_draft["id"])
        docgen_store.fill_slots(g20_draft["id"], [
            {"name": "原告姓名", "value": "王五", "sourceType": "user_confirmed"},
            {"name": "被告姓名", "value": "赵六", "sourceType": "user_confirmed"},
        ])
        g20_strict = docgen_store.render_docx(g20_draft["id"])
        # 读回生成文件：变量替换、未填字段写 xx、页眉与表格保留
        from lawbuddy import crypto as _crypto
        from lawbuddy import documents as _docs
        from lawbuddy.keyring import get_active_dek as _dek

        g20_blob_uri = get_db().query_one(
            "SELECT word_blob_uri FROM docgen_draft_versions WHERE draft_id = ? ORDER BY version_no DESC LIMIT 1",
            (g20_draft["id"],),
        )["word_blob_uri"]
        g20_bytes = _crypto.read_maybe_encrypted(
            _docs._workspace_root() / g20_blob_uri, _crypto.derive_file_key(_dek())
        )
        g20_text = "\n".join(p.text for p in _Document(io.BytesIO(g20_bytes)).paragraphs)
        g20_header = "\n".join(
            p.text for p in _Document(io.BytesIO(g20_bytes)).sections[0].header.paragraphs
        )
        g20_table = "\n".join(
            cell.text
            for row in _Document(io.BytesIO(g20_bytes)).tables[0].rows
            for cell in row.cells
        )
        # 原件只读校验：模板原件的字节哈希在生成前后不变
        g20_origin = docgen_store._read_template_docx(g20_tpl["id"])
        g20_ref_draft = docgen_store.create_draft(
            case["id"], g20_ref_tpl["id"], "民事起诉状", "DOCX 参考范本生成")
        docgen_store.update_outline(g20_ref_draft["id"], [
            {"heading": "当事人信息", "points": "", "sourceDoc": ""},
        ])
        docgen_store.confirm_outline(g20_ref_draft["id"])
        # 参考范本只提供版式：未生成正文时必须拒绝，不得把范本里的示例事实当成新文书内容
        g20_ref_guard = _err_of(lambda: docgen_store.render_docx(g20_ref_draft["id"]))
        # 由 AI 起草正文（此处直接给定内容，等价于 docgen_render_draft 的 body 通道）
        g20_ref_body = "# 民事起诉状\n\n原告：王五。\n被告：赵六。\n\n请求判令被告偿还借款。\n"
        docgen_store.render_body(g20_ref_draft["id"], g20_ref_body, allow_unconfirmed=True)
        g20_ref_out = docgen_store.render_docx(g20_ref_draft["id"])
        g20_ref_blob = get_db().query_one(
            "SELECT word_blob_uri FROM docgen_draft_versions WHERE draft_id = ? ORDER BY version_no DESC LIMIT 1",
            (g20_ref_draft["id"],),
        )["word_blob_uri"]
        g20_ref_bytes = _crypto.read_maybe_encrypted(
            _docs._workspace_root() / g20_ref_blob, _crypto.derive_file_key(_dek())
        )
        g20_ref_text = "\n".join(p.text for p in _Document(io.BytesIO(g20_ref_bytes)).paragraphs)
        g20 = (
            _gate("解析出正文变量（含表格单元格）",
                  g20_field_names == ["原告姓名", "原告住所", "被告姓名", "标的额"], f"{g20_field_names}")
            and _gate("含变量按 strict 模式导入", g20_tpl["templateMode"] == "strict", g20_tpl["templateMode"])
            and _gate("无变量按 reference 模式导入",
                      g20_ref_tpl["templateMode"] == "reference", g20_ref_tpl["templateMode"])
            and _gate("记录来源格式与原件位置",
                      g20_tpl["sourceFormat"] == "docx" and bool(g20_tpl["sourceBlobUri"]),
                      f"{g20_tpl['sourceFormat']} {g20_tpl['sourceBlobUri']}")
            and _gate("原件哈希为字节哈希", g20_tpl["sourceSha256"] == g20_src_sha, str(g20_tpl["sourceSha256"]))
            and _gate("strict 替换已填字段", "王五" in g20_text and "赵六" in g20_text, g20_text[:80])
            and _gate("strict 未填字段写 xx", "xx" in g20_text and "{{" not in g20_text, g20_text[:80])
            and _gate("strict 保留非变量文字", "民事起诉状" in g20_text, g20_text[:40])
            and _gate("strict 保留页眉", "示例律师事务所" in g20_header, g20_header)
            and _gate("strict 保留表格固定文字", "标的额" in g20_table and "按约定" in g20_table, g20_table)
            and _gate("模板原件生成前后不变", g20_origin == g20_docx_vars, f"{len(g20_origin)} bytes")
            and _gate("参考范本无正文时拒绝（示例事实不入新文书）",
                      "参考范本需要先生成正文" in g20_ref_guard, g20_ref_guard)
            and _gate("reference 生成新正文并保留页眉",
                      g20_ref_out["templateMode"] == "reference" and "民事起诉状" in g20_ref_text)
            and _gate("reference 不回灌范本示例事实",
                      "张三" not in g20_ref_text and "示例市示例区" not in g20_ref_text,
                      g20_ref_text[:80])
            and _gate("格式警告可见（不静默丢弃）",
                      isinstance(g20_ref_parsed["formatWarnings"], list), str(g20_ref_parsed["formatWarnings"]))
        )
        results["gates"]["G20_docx_template"] = g20
        all_ok &= g20

        # G25 真实 Word 范本与长文书（升级方案 §10 阶段四）：
        # 用仓库真实卷宗里的 30 份 .docx 验证格式保留（除 document.xml 外全部部件
        # 字节不变、页眉页脚原样），并用长正文与多轮修订验证生成稳定性。
        _log("[G25] 真实 Word 范本格式保留与长文书多轮修订")
        import zipfile as _zipfile

        corpus = Path(__file__).resolve().parents[3] / "case_file"
        real_files = sorted(
            p for p in corpus.rglob("*.docx") if p.is_file() and not p.parent.name.startswith("_scripts")
        ) if corpus.is_dir() else []
        long_md = "\n\n".join(
            ["# 长文书演练"]
            + [
                f"## 第{index}部分\n\n"
                + "本部分依据案件材料归纳如下：" + "相关事实与法律依据应当逐项核对原文页码。"
                * 40
                for index in range(1, 9)
            ]
        )
        g25_checked = 0
        g25_failures: list[str] = []
        g25_hdr_ok = False
        # 仓库真实卷宗本身不含页眉页脚，单独取一份真实范本注入页眉页脚后，
        # 验证 reference 生成仍保留这些部件（否则该断言在真实语料上恒为空）。
        if real_files:
            probe = _Document(io.BytesIO(real_files[0].read_bytes()))
            probe.sections[0].header.paragraphs[0].text = "示例律师事务所 · 卷宗范本"
            probe.sections[0].footer.paragraphs[0].text = "第 1 页 共 1 页"
            probe_buf = io.BytesIO()
            probe.save(probe_buf)
            probe_base = probe_buf.getvalue()
            probe_out = _docgen_docx.render_reference(probe_base, long_md, title="长文书演练")
            probe_doc = _Document(io.BytesIO(probe_out))
            probe_hdr = [p.text for p in probe_doc.sections[0].header.paragraphs if p.text.strip()]
            probe_ftr = [p.text for p in probe_doc.sections[0].footer.paragraphs if p.text.strip()]
            with _zipfile.ZipFile(io.BytesIO(probe_base)) as before, \
                    _zipfile.ZipFile(io.BytesIO(probe_out)) as after:
                changed = [
                    part for part in before.namelist()
                    if part != "word/document.xml" and before.read(part) != after.read(part)
                ]
            g25_hdr_ok = (
                probe_hdr == ["示例律师事务所 · 卷宗范本"]
                and probe_ftr == ["第 1 页 共 1 页"]
                and not changed
            )
            if not g25_hdr_ok:
                g25_failures.append(f"页眉页脚注入范本未保留：{probe_hdr}/{probe_ftr}/{changed[:2]}")
        for path in real_files:
            original = path.read_bytes()
            try:
                parsed = _docgen_docx.extract_template(original)
                rendered = _docgen_docx.render_reference(original, long_md, title="长文书演练")
                reopened = _Document(io.BytesIO(rendered))
            except Exception as exc:  # noqa: BLE001 - 逐份记录，不因单份失败中断
                g25_failures.append(f"{path.name}: {type(exc).__name__} {exc}")
                continue
            g25_checked += 1
            if not parsed["contentMd"].strip():
                g25_failures.append(f"{path.name}: 未提取到正文")
            names_in = _zipfile.ZipFile(io.BytesIO(original)).namelist()
            names_out = _zipfile.ZipFile(io.BytesIO(rendered)).namelist()
            if names_in != names_out:
                g25_failures.append(f"{path.name}: ZIP 部件集合变化")
            with _zipfile.ZipFile(io.BytesIO(original)) as before, \
                    _zipfile.ZipFile(io.BytesIO(rendered)) as after:
                for part in before.namelist():
                    if part == "word/document.xml":
                        continue
                    if before.read(part) != after.read(part):
                        g25_failures.append(f"{path.name}: {part} 被改写")
                        break
                header_before = [
                    p.text for s in _Document(io.BytesIO(original)).sections
                    for p in s.header.paragraphs if p.text.strip()
                ]
                header_after = [
                    p.text for s in reopened.sections for p in s.header.paragraphs if p.text.strip()
                ]
            if header_before != header_after:
                g25_failures.append(f"{path.name}: 页眉内容变化 {header_before}→{header_after}")
            text = "\n".join(p.text for p in reopened.paragraphs)
            if "第1部分" not in text or "第8部分" not in text or len(text) < 2000:
                g25_failures.append(f"{path.name}: 长正文未完整写入（{len(text)} 字）")
            if path.read_bytes() != original:
                g25_failures.append(f"{path.name}: 原件被改写")

        # 以真实范本走完整草稿链路：导入 → 建稿 → 长正文 → 连续三轮修订
        g25_rounds: list[int] = []
        g25_headers: list[list[str]] = []
        g25_sections: list[int] = []
        g25_tpl = None
        if real_files:
            g25_src = real_files[[p.name for p in real_files].index("57_刑事上诉状.docx")] \
                if "57_刑事上诉状.docx" in [p.name for p in real_files] else real_files[0]
            g25_tpl = docgen_store.import_docx_template(
                g25_src.read_bytes(), "真实范本-刑事上诉状", "刑事上诉状",
                case_id=case["id"], source_name=g25_src.name,
            )
            g25_draft = docgen_store.create_draft(
                case["id"], g25_tpl["id"], "刑事上诉状", "真实范本长文书演练")
            docgen_store.update_outline(g25_draft["id"], [
                {"heading": f"第{i}部分", "points": "", "sourceDoc": ""} for i in range(1, 9)
            ])
            docgen_store.confirm_outline(g25_draft["id"])
            body = long_md
            docgen_store.render_body(g25_draft["id"], body, allow_unconfirmed=True)
            for round_no in range(1, 4):
                base = docgen_store.latest_version_no(g25_draft["id"])
                res = docgen_store.revise_body(
                    g25_draft["id"], body, f"第 {round_no} 轮长文书修订",
                    allow_unconfirmed=True, base_version_no=base)
                g25_rounds.append(res["versionNo"])
                rendered = docgen_store.render_docx(g25_draft["id"])
                blob = get_db().query_one(
                    "SELECT word_blob_uri FROM docgen_draft_versions WHERE draft_id = ? "
                    "ORDER BY version_no DESC LIMIT 1", (g25_draft["id"],))["word_blob_uri"]
                data = _crypto.read_maybe_encrypted(
                    _docs._workspace_root() / blob, _crypto.derive_file_key(_dek()))
                doc = _Document(io.BytesIO(data))
                g25_headers.append(
                    [p.text for s in doc.sections for p in s.header.paragraphs if p.text.strip()])
                text = "\n".join(p.text for p in doc.paragraphs)
                headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
                g25_sections.append(len(headings))
                if "第8部分" not in text:
                    g25_failures.append(f"第 {round_no} 轮生成缺失章节")
                if not rendered.get("versionNo"):
                    g25_failures.append(f"第 {round_no} 轮未落新版本")
                body = body + f"\n\n第 {round_no} 轮补充说明：事实与法律依据已复核。\n"

        g25 = (
            _gate("真实卷宗 .docx 全部可解析（30 份内）",
                  bool(real_files) and g25_checked == len(real_files),
                  f"{g25_checked}/{len(real_files)}")
            and _gate("真实范本生成后除 document.xml 外部件字节不变",
                      not [f for f in g25_failures if "被改写" in f or "部件集合" in f],
                      str(g25_failures[:2]))
            and _gate("真实范本页眉页脚原样保留",
                      g25_hdr_ok and not [f for f in g25_failures if "页眉" in f],
                      str(g25_failures[:2]))
            and _gate("长正文完整写入且原件只读",
                      not [f for f in g25_failures if "长正文" in f or "原件被改写" in f],
                      str(g25_failures[:2]))
            and _gate("真实范本连续三轮修订 + 生成稳定",
                      len(g25_rounds) == 3 and g25_rounds == sorted(set(g25_rounds))
                      and not [f for f in g25_failures if "轮" in f],
                      f"{g25_rounds}")
            and _gate("多轮生成版式稳定（页眉与章节数不漂移）",
                      bool(g25_headers) and all(h == g25_headers[0] for h in g25_headers)
                      and len(set(g25_sections)) == 1 and g25_sections[0] >= 9,
                      f"headers={[len(h) for h in g25_headers]} sections={g25_sections}")
        )
        results["gates"]["G25_real_docx_corpus"] = g25
        results["details"]["realDocxCorpus"] = {
            "files": len(real_files), "checked": g25_checked,
            "failures": g25_failures[:10], "versions": g25_rounds,
        }
        all_ok &= g25

        # G26 真实案件：普通缺失不提问 + 不重复读取（升级方案 §10 阶段四 / §11.3）
        # 用仓库真实卷宗（一审刑事判决书）走一次完整起草：模型按需读原文一次即取得
        # 可引证事实，任务不产生任何问题集，效率指标中重复读取为 0。
        _log("[G26] 真实案件：提问必要性与重复读取")
        from evals._loop_mock import ScriptStep, install_mock_gateway as _install
        from evals._loop_mock import last_tool_result as _last_tool_result
        from lawbuddy import conversation as _conversation_store
        from lawbuddy import policy as _policy_store

        real_case_docs = sorted(
            (p for p in corpus.rglob("*.pdf") if "判决书" in p.name), key=lambda p: p.name
        )
        if not real_case_docs:
            g26 = _gate("真实卷宗样本存在", False, str(corpus))
            results["gates"]["G26_real_case_question_and_reads"] = g26
            all_ok &= g26
        else:
            g26_case = create_case(
                title="评测-真实案件起草", case_type="criminal", client_name="王志远",
                default_mode="document_generation", permission_preset="controlled_external",
            )
            g26_doc = import_documents(g26_case["id"], [str(real_case_docs[0])])[0]
            g26_pages = get_db().query(
                "SELECT page_no, text_content FROM document_pages WHERE version_id = "
                "(SELECT active_version_id FROM documents WHERE id = ?) ORDER BY page_no",
                (g26_doc["documentId"],),
            )
            g26_quote_amt = None
            for page in g26_pages:
                text = str(page["text_content"] or "")
                if g26_quote_amt is None and "120万元" in text:
                    idx = text.find("120万元")
                    g26_quote_amt = {"pageNo": page["page_no"],
                                     "quote": text[max(0, idx - 20): idx + 5].replace("\n", "")}
            g26_tpl_md = (
                "刑事辩护意见\n\n"
                "被告人：{{被告人姓名}}。\n"
                "指控金额：{{涉案金额}}。\n\n"
                "辩护意见：\n一、关于指控金额的认定需以证据为准。\n二、其他事实与理由：xx。\n"
            )
            g26_tpl = server._handlers["docgen.import_template"]({
                "title": "真实案件辩护意见模板", "docType": "刑事辩护意见",
                "templateMd": g26_tpl_md, "caseId": g26_case["id"],
            })
            g26_conv = _conversation_store.ensure_conversation(None, g26_case["id"])
            with get_db().transaction() as conn:
                conn.execute(
                    "INSERT INTO permission_grants(id, scope_type, scope_id, capability, payload_hash, created_at) "
                    "VALUES (?, 'case', ?, ?, ?, ?)",
                    ("grant-eval-g26", g26_conv, _policy_store.CAP_NETWORK_MODEL, g26_conv,
                     datetime.now().isoformat(timespec="seconds")),
                )
            values = [{"name": "被告人姓名", "value": "王志远", "sourceType": "user_confirmed"}]
            if g26_quote_amt:
                values.append({
                    "name": "涉案金额", "value": "120万元", "sourceType": "case_evidence",
                    "documentId": g26_doc["documentId"],
                    "pageNo": g26_quote_amt["pageNo"], "quote": g26_quote_amt["quote"],
                })
            g26_steps = [
                ScriptStep.tool("documents_list", {}),
                # 一次读全（真实判决书 4 页 < 单次上限），不重复读同一页
                ScriptStep.tool("document_get_pages", {
                    "documentId": g26_doc["documentId"], "pageFrom": 1,
                    "pageTo": len(g26_pages),
                }),
                ScriptStep.tool("docgen_create_draft", {
                    "templateId": g26_tpl["id"], "docType": "刑事辩护意见",
                    "purpose": "就指控金额与量刑情节提出辩护意见",
                }),
                ScriptStep.tool("docgen_render_draft", lambda messages: {
                    "draftId": _last_tool_result(messages).get("draftId"),
                    "values": values,
                }),
                ScriptStep.final("已基于判决书指控事实生成辩护意见草稿，普通缺失信息以 xx 标注。"),
            ]
            g26_undo = _install(g26_steps)
            try:
                g26_task = agent_start({
                    "caseId": g26_case["id"], "mode": "document_generation",
                    "workflowKind": "docgen_dialogue", "conversationId": g26_conv,
                    "docgenInput": {"templateId": g26_tpl["id"], "docType": "刑事辩护意见"},
                    "input": "请就指控金额提出辩护意见",
                })
                g26_deadline = time.time() + 120
                while (
                    get_task(g26_task["id"])["lifecycle"]
                    in ("CREATED", "PLANNING", "RUNNING", "VERIFYING")
                    and time.time() < g26_deadline
                ):
                    time.sleep(0.3)
            finally:
                g26_undo()
            g26_events = server._handlers["agent.get_events"]({"taskId": g26_task["id"]})
            g26_turn = next((e["payload"] for e in reversed(g26_events)
                             if e["type"] == "turn_complete"), {})
            g26_eff = g26_turn.get("efficiency") or {}
            g26_drafts = server._handlers["docgen.list_drafts"]({"caseId": g26_case["id"]})
            g26_body = (
                server._handlers["docgen.get_draft"]({"draftId": g26_drafts[0]["id"]})["bodyMd"]
                if g26_drafts else ""
            ) or ""
            g26 = (
                _gate("真实案件起草任务完成",
                      get_task(g26_task["id"])["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS"),
                      get_task(g26_task["id"])["lifecycle"])
                and _gate("普通缺失不触发提问（真实案件无待答问题集）",
                          not server._handlers["agent.pending_questions"]({"taskId": g26_task["id"]}))
                and _gate("真实原文事实自动带入且可追溯",
                          "120万元" in g26_body, g26_body[:80])
                and _gate("未重复读取已完整读过的页",
                          int(g26_eff.get("repeatedReads") or 0) == 0, str(g26_eff.get("repeatedReads")))
                and _gate("原文读取次数与材料页数匹配（一次读全）",
                          int(g26_eff.get("reads") or 0) == 1, str(g26_eff.get("readsByOrigin")))
            )
            results["gates"]["G26_real_case_question_and_reads"] = g26
            results["details"]["realCaseEfficiency"] = {
                "reads": g26_eff.get("reads"), "repeatedReads": g26_eff.get("repeatedReads"),
                "uniqueHitPages": g26_eff.get("uniqueHitPages"),
            }
            all_ok &= g26

        # G21 连续三次修订：每次基于当前稿落新版本，可比较与恢复，定稿需复核
        # （升级方案 §11.1 U7 / §11.2 G20）
        _log("[G21] 三次连续修订、版本比较与恢复")
        g21_draft = docgen_store.create_free_draft(case["id"], "民事起诉状", "多轮修订演练")
        docgen_store.confirm_outline(g21_draft["id"])
        docgen_store.render_body(g21_draft["id"])
        g21_versions: list[int] = []
        for round_no in range(1, 4):
            base = docgen_store.latest_version_no(g21_draft["id"])
            res = docgen_store.revise_body(
                g21_draft["id"],
                f"第 {round_no} 轮修订后的正文。\n" + ("补充内容。\n" * round_no),
                f"第 {round_no} 轮修改意见",
                base_version_no=base,
            )
            g21_versions.append(res["versionNo"])
        g21_diff = docgen_store.diff_versions(g21_draft["id"], g21_versions[0])
        g21_restored = docgen_store.restore_version(g21_draft["id"], g21_versions[0])
        g21_after = docgen_store.latest_version_no(g21_draft["id"])
        g21_confirm_err = _err_of(
            lambda: docgen_store.confirm_draft(g21_draft["id"]))
        g21 = (
            _gate("三轮修订各自递增版本", g21_versions == sorted(set(g21_versions))
                  and len(g21_versions) == 3,
                  str(g21_versions))
            and _gate("版本可比较（相对当前稿）",
                      "第 1 轮修订后的正文" in str(g21_diff.get("left") or g21_diff), str(list(g21_diff))[:80])
            and _gate("恢复历史版本后追加为新版本且内容回到该版",
                      g21_after == g21_versions[-1] + 1
                      and "第 1 轮修订后的正文" in (g21_restored["bodyMd"] or ""),
                      f"v{g21_after}")
            and _gate("修订后定稿需先人工复核（不静默定稿）",
                      "CONTENT_REVIEW_REQUIRED" in g21_confirm_err, g21_confirm_err)
        )
        results["gates"]["G21_multi_revision_cycle"] = g21
        all_ok &= g21

        # G22 来源失效：关联案件删除 / 文档换版 / 低 OCR 置信度 → stale / needs_human
        # （升级方案 §11.1 U10 / §11.2 G22）
        _log("[G22] 来源失效与人工核实")
        from lawbuddy import cases as cases_store
        from lawbuddy import citations as citations_store

        g22_case = create_case(title="评测-来源失效", case_type="civil", client_name="原告",
                               default_mode="document_generation")
        g22_src = tmp / "g22_source.txt"
        g22_src.write_text("借款本金为五万元。约定于 2025 年 3 月归还。", encoding="utf-8")
        g22_doc = import_documents(g22_case["id"], [str(g22_src)])[0]
        g22_tpl = server._handlers["docgen.import_template"]({
            "title": "来源失效模板", "docType": "民事起诉状",
            "templateMd": TEMPLATE_MD, "caseId": g22_case["id"],
        })
        g22_draft = docgen_store.create_draft(
            g22_case["id"], g22_tpl["id"], "民事起诉状", "来源失效演练")
        docgen_store.update_outline(
            g22_draft["id"], docgen_store.outline_from_template(g22_tpl["contentMd"]))
        docgen_store.confirm_outline(g22_draft["id"])
        g22_cite = {"documentId": g22_doc["documentId"], "pageNo": 1, "quote": "借款本金为五万元。"}
        docgen_store.fill_slots(g22_draft["id"], [{
            "name": "欠款金额", "value": "五万元", "sourceType": "case_evidence", "sourceRef": g22_cite,
        }])
        docgen_store.fill_slots(g22_draft["id"], [{
            "name": "签约时间", "value": "2025年3月", "sourceType": "pending_verify",
        }])
        docgen_store.fill_slots(g22_draft["id"], [
            {"name": "原告姓名", "value": "王某", "sourceType": "user_confirmed"},
            {"name": "原告身份信息", "value": "男，1980年出生", "sourceType": "user_confirmed"},
            {"name": "被告姓名", "value": "李某", "sourceType": "user_confirmed"},
            {"name": "被告身份信息", "value": "男，1979年出生", "sourceType": "user_confirmed"},
        ])
        docgen_store.render_body(g22_draft["id"])
        # 待核实来源阻止定稿
        g22_pending_err = _err_of(lambda: docgen_store.confirm_draft(g22_draft["id"]))
        # 落一条引用该文档的 Claim（模拟阅卷结论），删除文档后应转 stale 并保留
        g22_task_id = "eval-g22-claim"
        from lawbuddy.agent.tasks import create_task as _create_task
        g22_task = _create_task(g22_case["id"], "document_generation", "G22 引证")
        g22_ref = citations_store.make_source_ref({
            "documentId": g22_doc["documentId"],
            "versionId": get_db().query_one(
                "SELECT active_version_id FROM documents WHERE id = ?",
                (g22_doc["documentId"],))["active_version_id"],
            "pageNo": 1,
            "snippet": "借款本金为五万元。",
        })
        g22_claim = citations_store.create_claim(
            g22_task["id"], g22_case["id"], "fact", "借款本金为五万元。",
            "supported", None, "exact_quote", g22_ref,
        )
        # 删除来源文档：引用该文档的 Claim 转 stale，旧稿保留
        server._handlers["document.delete"]({"documentId": g22_doc["documentId"]})
        g22_claims = get_db().query(
            "SELECT status, reason_code FROM claims WHERE id = ?", (g22_claim["claimId"],)
        )
        g22_kept = docgen_store.get_draft(g22_draft["id"])
        # 案件删除后关联指针置空、Claim 转 stale（不继续读取已删案件）
        g22_linked = create_case(title="评测-关联删除", case_type="civil", client_name="原告",
                                 default_mode="document_generation",
                                 linked_case_id=g22_case["id"])
        cases_store.delete_case(g22_case["id"])
        g22_after_link = get_db().query_one(
            "SELECT linked_case_id FROM cases WHERE id = ?", (g22_linked["id"],)
        )
        g22 = (
            _gate("待核实来源阻止定稿", "待核实" in g22_pending_err or "核实" in g22_pending_err,
                  g22_pending_err)
            and _gate("删除来源文档后引用转 stale",
                      bool(g22_claims) and all(c["status"] == "stale" for c in g22_claims),
                      str([dict(c) for c in g22_claims][:2]))
            and _gate("来源失效原因可追溯（DOCUMENT_DELETED）",
                      any(c["reason_code"] == "DOCUMENT_DELETED" for c in g22_claims),
                      str([c["reason_code"] for c in g22_claims]))
            and _gate("历史草稿保留（不因来源删除而丢失）",
                      bool((g22_kept.get("bodyMd") or "").strip()))
            and _gate("关联案件删除后指针置空",
                      bool(g22_after_link) and g22_after_link["linked_case_id"] is None,
                      str(g22_after_link and g22_after_link["linked_case_id"]))
        )
        results["gates"]["G22_source_staleness"] = g22
        all_ok &= g22

        # G23 低 OCR 置信度不得作为已支持来源（fail-closed 到人工核实）
        _log("[G23] 低 OCR 置信度引证降级")
        g23_case = create_case(title="评测-低置信度", case_type="civil", client_name="原告",
                               default_mode="document_generation")
        g23_src = tmp / "g23_source.txt"
        g23_src.write_text("借款金额为八千元。", encoding="utf-8")
        g23_doc = import_documents(g23_case["id"], [str(g23_src)])[0]
        g23_version = get_db().query_one(
            "SELECT active_version_id FROM documents WHERE id = ?", (g23_doc["documentId"],)
        )["active_version_id"]
        with get_db().transaction() as conn:
            conn.execute(
                "UPDATE document_pages SET ocr_confidence = ? WHERE version_id = ? AND page_no = 1",
                (citations_store.LOW_OCR_CONFIDENCE - 0.2, g23_version),
            )
        g23_err = _err_of(lambda: docgen_store._verify_slot_source(
            g23_case["id"], "八千元",
            {"documentId": g23_doc["documentId"], "pageNo": 1, "quote": "借款金额为八千元。"},
        ))
        g23 = _gate("低 OCR 置信度引证被拒绝并要求人工核实",
                    "人工核实" in g23_err or "质量不足" in g23_err, g23_err)
        results["gates"]["G23_low_ocr_confidence"] = g23
        all_ok &= g23

        # G15/G16/G17 仅选模板空文本发起 → 必要决策提问暂停 → 回答幂等恢复（升级方案 §4.1/§5.3）
        _log("[G15-G17] 空文本发起、必要决策暂停与幂等恢复")
        from evals._loop_mock import docgen_decision_steps
        from lawbuddy import conversation as conversation_store
        from lawbuddy import policy as policy_store

        # 文书生成对话属于模型外发（脱敏 + 审批）通道：评测案件按受控外部建档，
        # 并预置会话级模型授权，等价于用户已在审批卡上选择「本会话允许」，
        # 使评测聚焦暂停/恢复行为而不阻塞在审批等待
        # 同时显式关联首个案件：验证上下文准备阶段能读到只读共享材料
        case2 = create_case(title="评测-文书决策", case_type="civil", client_name="原告",
                            default_mode="document_generation",
                            permission_preset="controlled_external",
                            linked_case_id=case["id"])
        tpl2 = server._handlers["docgen.import_template"]({
            "title": "民事起诉状模板2", "docType": "民事起诉状",
            "templateMd": TEMPLATE_MD, "caseId": case2["id"],
        })
        conv2 = conversation_store.ensure_conversation(None, case2["id"])
        permission_db = get_db()
        with permission_db.transaction() as conn:
            conn.execute(
                "INSERT INTO permission_grants(id, scope_type, scope_id, capability, payload_hash, created_at) "
                "VALUES (?, 'case', ?, ?, ?, ?)",
                ("grant-eval-docgen", conv2, policy_store.CAP_NETWORK_MODEL, conv2,
                 datetime.now().isoformat(timespec="seconds")),
            )
        undo2 = install_mock_gateway(docgen_decision_steps())
        try:
            task2 = agent_start({
                "caseId": case2["id"], "mode": "document_generation",
                "workflowKind": "docgen_dialogue",
                "conversationId": conv2,
                # 只选模板、不写任何文本：Core 生成内部任务描述
                "docgenInput": {"templateId": tpl2["id"], "docType": "民事起诉状"},
                "input": "",
            })
            _gate("空文本发起后生成内部任务描述",
                  "起草" in str(get_task(task2["id"])["request"])
                  and "xx" in str(get_task(task2["id"])["request"]),
                  str(get_task(task2["id"])["request"])[:60])
            deadline2 = time.time() + 90
            while (
                get_task(task2["id"])["lifecycle"] in ("CREATED", "PLANNING", "RUNNING", "VERIFYING")
                and time.time() < deadline2
            ):
                time.sleep(0.3)
        finally:
            undo2()
        pending = get_task(task2["id"])["lifecycle"]
        qs = server._handlers["agent.pending_questions"]({"taskId": task2["id"]})
        drafts_before = server._handlers["docgen.list_drafts"]({"caseId": case2["id"]})
        # G15 案件上下文准备（升级方案 §8.1）：只下发计数元数据，不含原文/个人信息
        events2 = server._handlers["agent.get_events"]({"taskId": task2["id"]})
        ctx_events = [e for e in events2 if e["type"] == "case_context"]
        ctx_payload = ctx_events[-1]["payload"] if ctx_events else {}
        g15 = (
            _gate("下发案件上下文准备事件（含材料页数与关联状态）",
                  bool(ctx_events) and ctx_payload.get("totalPages", 0) >= 0
                  and set(ctx_payload) <= {
                      "linkedCase", "ownDocuments", "sharedDocuments", "totalPages",
                      "parsePending", "ocrPendingPages", "memories",
                  },
                  str(ctx_payload))
            and _gate("上下文准备读到关联案件只读共享材料",
                      ctx_payload.get("linkedCase") is True
                      and int(ctx_payload.get("sharedDocuments", 0)) >= 1
                      and int(ctx_payload.get("totalPages", 0)) >= 1,
                      str({k: ctx_payload.get(k) for k in ("linkedCase", "sharedDocuments", "totalPages")}))
        )
        results["gates"]["G15_case_context_prep"] = g15
        all_ok &= g15
        question_seq = next((e["seq"] for e in events2 if e["type"] == "human_question"), None)
        waiting_seq = next(
            (e["seq"] for e in events2 if e["type"] == "status"
             and e["payload"].get("lifecycle") == "WAITING_HUMAN"),
            None,
        )
        g17a = (
            _gate("必要决策使任务进入 WAITING_HUMAN", pending == "WAITING_HUMAN", pending)
            and _gate("问题卡先于等待状态下发（前端不会因停止订阅漏卡）",
                      question_seq is not None and waiting_seq is not None and question_seq < waiting_seq,
                      f"question={question_seq}, waiting={waiting_seq}")
            and _gate("待答问题集可跨刷新拉取",
                      bool(qs) and qs["status"] == "pending" and len(qs["questions"]) == 1,
                      str(qs and qs["status"]))
            and _gate("回答前不产生草稿等副作用", not drafts_before, str(len(drafts_before)))
        )
        results["gates"]["G17_pause_before_draft"] = g17a
        all_ok &= g17a
        if not g17a:
            raise RuntimeError(f"必要决策暂停未成立（lifecycle={pending}，questionSet={bool(qs)}），无法验证续跑")

        undo3 = install_mock_gateway(docgen_decision_steps()[1:])
        try:
            answer_req = {
                "taskId": task2["id"], "questionSetId": qs["id"],
                "answers": [{"questionId": "q1", "optionIds": ["o1"]}],
            }
            first = server._handlers["agent.answer_human_questions"](answer_req)
            # 重复提交：幂等返回，不重复追加用户决策消息或重启任务
            again = server._handlers["agent.answer_human_questions"](answer_req)
            deadline3 = time.time() + 90
            while (
                get_task(task2["id"])["lifecycle"] in ("CREATED", "PLANNING", "RUNNING", "VERIFYING", "WAITING_HUMAN")
                and time.time() < deadline3
            ):
                time.sleep(0.3)
        finally:
            undo3()
        drafts_after = server._handlers["docgen.list_drafts"]({"caseId": case2["id"]})
        final_task = get_task(task2["id"])
        answered_set = server._handlers["agent.pending_questions"]({"taskId": task2["id"]})
        # 用户决策以一条 user 消息写入续跑检查点（审计与版本追溯依据）
        from lawbuddy.agent.tasks import latest_checkpoint as _latest_ckpt

        ckpt2 = _latest_ckpt(task2["id"]) or {}
        decision_in_ckpt = any(
            "【用户决策】" in str(m.get("content") or "")
            for m in ((ckpt2.get("state") or {}).get("messages") or [])
            if isinstance(m, dict)
        )
        g17b = (
            _gate("回答后任务从检查点续跑至完成",
                  final_task["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS"),
                  final_task["lifecycle"])
            and _gate("回答幂等（重复提交不重复建稿）", len(drafts_after) == 1, str(len(drafts_after)))
            and _gate("已回答问题集不再处于待答状态", answered_set is None,
                      str(answered_set and answered_set["status"]))
            and _gate("用户决策写入续跑检查点（可追溯）", decision_in_ckpt)
        )
        results["gates"]["G17_resume_idempotent"] = g17b
        all_ok &= g17b

    except Exception as exc:  # noqa: BLE001
        all_ok = False
        results["error"] = f"{type(exc).__name__}: {exc}"
        _log(f"  [FAIL] 异常：{results['error']}")
        import traceback

        traceback.print_exc()
    finally:
        results["ok"] = all_ok
        results["elapsedSec"] = round(time.time() - t0, 2)
        report_dir = Path(__file__).resolve().parent / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / f"m3_docgen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        report.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        _log(f"\n{'全部通过' if all_ok else '存在失败'} · 报告 {report} · {results['elapsedSec']}s")
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            _log(f"（保留工作空间 {tmp}）")

    return 0 if all_ok else 1


def _err_of(fn) -> str:
    """执行函数并返回异常消息（未抛异常返回空串）。"""
    try:
        fn()
        return ""
    except Exception as exc:  # noqa: BLE001
        return str(exc)


if __name__ == "__main__":
    raise SystemExit(main())
